from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cv_core import (
    MachineIdentity,
    PlatformSettings,
    hash_machine_secret,
    hash_password,
    join_scopes,
    parse_scope_string,
    verify_machine_secret,
    verify_password,
)
from cv_status import EnvironmentName, UserRole
from cv_utils import AuthenticationError

from cv_db.auth_store import (
    ensure_bootstrap_user,
    get_api_key_record,
    get_user_by_username,
    record_api_key_use,
    reconcile_bootstrap_api_keys,
    upsert_api_key,
)
from cv_db.models import User
from cv_db.session import session_scope

_BOOTSTRAP_CACHE: dict[str, tuple[str, ...]] = {}
_MACHINE_AUTH_CACHE: dict[str, dict[str, "_CachedMachineCredential"]] = {}
_MACHINE_LAST_USED_CACHE: dict[tuple[str, str], float] = {}
_MACHINE_LAST_USED_PENDING: set[tuple[str, str]] = set()
_MACHINE_LAST_USED_LOCK = threading.Lock()
_MACHINE_AUTH_CACHE_LOCK = threading.RLock()
_MACHINE_AUTH_CACHE_TTL_SECONDS = 2.0
_MACHINE_LAST_USED_WRITE_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class _CachedMachineCredential:
    key_id: str
    subject_type: str
    subject_id: str
    scope: str
    hashed_secret: str
    expires_at: datetime | None
    revoked_at: datetime | None
    source_type: str
    loaded_at_monotonic: float


def ensure_bootstrap_auth_state(settings: PlatformSettings) -> None:
    cache_key = _bootstrap_cache_key(settings)
    signature = _bootstrap_signature(settings)
    if _BOOTSTRAP_CACHE.get(cache_key) == signature:
        return

    bootstrap_hash = settings.session.bootstrap_admin_password_hash()
    if not bootstrap_hash:
        if settings.env == EnvironmentName.DEV:
            bootstrap_hash = hash_password("adminadmin")
        else:
            raise AuthenticationError("Bootstrap admin password hash is not configured")

    with session_scope(settings) as session:
        ensure_bootstrap_user(
            session,
            user_id=_stable_user_id(settings.session.bootstrap_admin_username),
            username=settings.session.bootstrap_admin_username,
            password_hash=bootstrap_hash,
            role=UserRole(settings.session.bootstrap_admin_role).value,
            display_name=settings.session.bootstrap_admin_username,
        )

        active_bootstrap_keys: set[str] = set()
        for bootstrap_key in settings.api_keys.bootstrap_keys:
            secret = bootstrap_key.secret()
            if not secret:
                if settings.env == EnvironmentName.PROD:
                    raise AuthenticationError(
                        f"Bootstrap API key secret is missing for {bootstrap_key.key_id}"
                    )
                continue
            active_bootstrap_keys.add(bootstrap_key.key_id)
            upsert_api_key(
                session,
                key_id=bootstrap_key.key_id,
                label=bootstrap_key.label or bootstrap_key.key_id,
                description=bootstrap_key.description
                or "Bootstrap/config-managed key. Modify through configuration and mounted secret files.",
                intended_usage=bootstrap_key.intended_usage
                or f"Bootstrap identity for {bootstrap_key.subject_type}:{bootstrap_key.subject_id}.",
                subject_type=bootstrap_key.subject_type,
                subject_id=bootstrap_key.subject_id,
                scope=join_scopes(bootstrap_key.scopes),
                hashed_secret=hash_machine_secret(secret),
                secret_prefix=secret[:6],
                endpoint_mode_hint=bootstrap_key.endpoint_mode_hint,
                expires_at=bootstrap_key.expires_at,
                source_type="bootstrap",
                created_by="bootstrap",
            )
        reconcile_bootstrap_api_keys(session, active_key_ids=active_bootstrap_keys)

    _BOOTSTRAP_CACHE[cache_key] = signature
    with _MACHINE_AUTH_CACHE_LOCK:
        _MACHINE_AUTH_CACHE.pop(cache_key, None)


def authenticate_user_password(
    settings: PlatformSettings,
    *,
    username: str,
    password: str,
) -> User | None:
    ensure_bootstrap_auth_state(settings)
    with session_scope(settings) as session:
        user = get_user_by_username(session, username)
        if user is None or not user.is_active:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user


def resolve_active_user(
    settings: PlatformSettings,
    *,
    username: str,
    session_version: int | None = None,
) -> User | None:
    ensure_bootstrap_auth_state(settings)
    with session_scope(settings) as session:
        user = get_user_by_username(session, username)
        if user is None or not user.is_active:
            return None
        if session_version is not None and int(user.session_version or 0) != session_version:
            return None
        return user


def authenticate_machine_identity(
    settings: PlatformSettings,
    *,
    key_id: str,
    secret: str,
    required_scopes: set[str],
    allowed_subject_types: set[str] | None = None,
    allowed_subject_ids: set[str] | None = None,
) -> MachineIdentity:
    ensure_bootstrap_auth_state(settings)

    with _MACHINE_AUTH_CACHE_LOCK:
        record = _get_cached_machine_credential(settings, key_id=key_id)
        if record is None:
            # Single-flight the cache refresh so concurrent realtime requests
            # cannot all query and verify the same credential record.
            with session_scope(settings) as session:
                row = get_api_key_record(session, key_id)
                if row is None:
                    raise AuthenticationError(
                        "The supplied machine credential is not recognized."
                    )
                record = _cache_machine_credential(settings, row)

    now = datetime.now(timezone.utc)
    if record.revoked_at is not None:
        raise AuthenticationError("The supplied machine credential has been revoked.")
    if record.expires_at and record.expires_at <= now:
        raise AuthenticationError("The supplied machine credential has expired.")
    if allowed_subject_types and record.subject_type not in allowed_subject_types:
        raise AuthenticationError("The supplied machine credential is not valid for this API.")
    if allowed_subject_ids and record.subject_id not in allowed_subject_ids:
        raise AuthenticationError("The supplied machine credential is not valid for this API.")
    if not verify_machine_secret(secret, record.hashed_secret):
        raise AuthenticationError("The supplied machine credential secret is invalid.")

    scopes = parse_scope_string(record.scope)
    missing = required_scopes - set(scopes)
    if missing:
        raise AuthenticationError(
            f"The supplied machine credential is missing required scopes: {', '.join(sorted(missing))}."
        )

    _touch_machine_identity(settings, key_id=key_id)
    return MachineIdentity(
        key_id=record.key_id,
        subject_type=record.subject_type,
        subject_id=record.subject_id,
        scopes=scopes,
    )


def _bootstrap_cache_key(settings: PlatformSettings) -> str:
    return f"{settings.service.name}:{settings.database.sqlalchemy_dsn()}"


def _get_cached_machine_credential(
    settings: PlatformSettings,
    *,
    key_id: str,
) -> _CachedMachineCredential | None:
    with _MACHINE_AUTH_CACHE_LOCK:
        cache = _MACHINE_AUTH_CACHE.get(_bootstrap_cache_key(settings), {})
        record = cache.get(key_id)
        if record is None:
            return None
        if (time.monotonic() - record.loaded_at_monotonic) > _MACHINE_AUTH_CACHE_TTL_SECONDS:
            cache.pop(key_id, None)
            return None
        return record


def _cache_machine_credential(settings: PlatformSettings, row) -> _CachedMachineCredential:
    record = _CachedMachineCredential(
        key_id=row.id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        scope=row.scope,
        hashed_secret=row.hashed_secret,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        source_type=row.source_type,
        loaded_at_monotonic=time.monotonic(),
    )
    with _MACHINE_AUTH_CACHE_LOCK:
        cache = _MACHINE_AUTH_CACHE.setdefault(_bootstrap_cache_key(settings), {})
        cache[row.id] = record
    return record


def _touch_machine_identity(settings: PlatformSettings, *, key_id: str) -> None:
    cache_key = _bootstrap_cache_key(settings)
    usage_key = (cache_key, key_id)
    now_monotonic = time.monotonic()
    with _MACHINE_LAST_USED_LOCK:
        last_write = _MACHINE_LAST_USED_CACHE.get(usage_key)
        if (
            usage_key in _MACHINE_LAST_USED_PENDING
            or (
                last_write is not None
                and (now_monotonic - last_write)
                < _MACHINE_LAST_USED_WRITE_INTERVAL_SECONDS
            )
        ):
            return
        # This timestamp is operational telemetry, not part of the authentication
        # decision. Reserve the interval before starting the write so concurrent
        # realtime requests cannot form a thundering herd while PostgreSQL is slow.
        _MACHINE_LAST_USED_CACHE[usage_key] = now_monotonic
        _MACHINE_LAST_USED_PENDING.add(usage_key)

    worker = threading.Thread(
        target=_record_machine_identity_use,
        kwargs={
            "settings": settings,
            "key_id": key_id,
            "usage_key": usage_key,
        },
        name=f"machine-usage-{key_id}",
        daemon=True,
    )
    try:
        worker.start()
    except Exception:
        with _MACHINE_LAST_USED_LOCK:
            _MACHINE_LAST_USED_PENDING.discard(usage_key)


def _record_machine_identity_use(
    *,
    settings: PlatformSettings,
    key_id: str,
    usage_key: tuple[str, str],
) -> None:
    try:
        with session_scope(settings) as session:
            row = get_api_key_record(session, key_id)
            if row is None or row.revoked_at is not None:
                return
            record_api_key_use(session, row)
    except Exception:
        # Usage telemetry must never make a valid realtime request fail. Credential
        # loading and secret verification have already completed before this write.
        return
    finally:
        with _MACHINE_LAST_USED_LOCK:
            _MACHINE_LAST_USED_PENDING.discard(usage_key)


def _stable_user_id(username: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", username.lower()).strip("-")
    return f"usr_{normalized or 'bootstrap'}"


def _bootstrap_signature(settings: PlatformSettings) -> tuple[str, ...]:
    file_signatures = [
        _file_signature(settings.session.bootstrap_admin_password_hash_file),
        _file_signature(settings.session.secret_file),
    ]
    for bootstrap_key in settings.api_keys.bootstrap_keys:
        file_signatures.append(_file_signature(bootstrap_key.secret_file))
    return (
        settings.session.bootstrap_admin_username,
        settings.session.bootstrap_admin_role,
        settings.env.value,
        *file_signatures,
        *(
            (
                f"{bootstrap_key.key_id}:{bootstrap_key.subject_type}:{bootstrap_key.subject_id}:"
                f"{join_scopes(bootstrap_key.scopes)}:{bootstrap_key.expires_at or ''}:"
                f"{bootstrap_key.label or ''}:{bootstrap_key.endpoint_mode_hint or ''}"
            )
            for bootstrap_key in settings.api_keys.bootstrap_keys
        ),
    )


def _file_signature(path_value: str) -> str:
    path = Path(path_value)
    if not path.exists():
        return f"{path_value}:missing"
    stat = path.stat()
    return f"{path_value}:{stat.st_mtime_ns}:{stat.st_size}"
