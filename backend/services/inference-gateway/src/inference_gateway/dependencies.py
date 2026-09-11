from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from cv_contracts import SessionUser
from cv_core import (
    PlatformSettings,
    SessionIdentity,
    build_session_serializer,
    parse_machine_token,
    read_session_token,
)
from cv_db import (
    authenticate_machine_identity,
    create_audit_event,
    resolve_active_user,
    session_scope,
)
from cv_status import UserRole
from cv_utils import AuthenticationError, PlatformError


@dataclass(frozen=True)
class RequestIdentity:
    auth_mode: str
    principal_id: str
    user: SessionUser | None = None
    machine_subject: str | None = None
    machine_subject_type: str | None = None


class FixedWindowRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, int], int] = {}

    def check(self, *, key: str, limit: int, window_seconds: int = 60) -> None:
        now_bucket = int(time.time() // window_seconds)
        bucket_key = (key, now_bucket)
        with self._lock:
            for counter_key in list(self._counters):
                if counter_key[1] < now_bucket - 1:
                    self._counters.pop(counter_key, None)
            current = self._counters.get(bucket_key, 0) + 1
            self._counters[bucket_key] = current
        if current > limit:
            raise PlatformError(
                code="rate_limit_exceeded",
                message="The request rate for this endpoint exceeded the configured limit.",
                status_code=429,
            )


def get_settings(request: Request) -> PlatformSettings:
    return request.app.state.settings


def require_realtime_access(
    request: Request,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
) -> RequestIdentity:
    identity = _resolve_request_identity(
        request=request,
        settings=settings,
        required_machine_scopes={"realtime.predict"},
        allow_browser_session=True,
    )
    if not _is_internal_runtime_identity(identity):
        _enforce_rate_limit(
            request=request,
            principal_key=f"realtime:{identity.principal_id}",
            limit=settings.inference_limits.realtime_requests_per_minute,
        )
    return identity


def require_batch_access(
    request: Request,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
) -> RequestIdentity:
    identity = _resolve_request_identity(
        request=request,
        settings=settings,
        required_machine_scopes={"batch.submit"},
        allow_browser_session=True,
    )
    _enforce_rate_limit(
        request=request,
        principal_key=f"batch:{identity.principal_id}",
        limit=settings.inference_limits.batch_requests_per_minute,
    )
    return identity


def require_internal_batch_access(
    request: Request,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
) -> RequestIdentity:
    return _resolve_request_identity(
        request=request,
        settings=settings,
        required_machine_scopes={"internal.batch.execute"},
        allow_browser_session=False,
        allowed_machine_subject_types={"service"},
        allowed_machine_subject_ids={"batch-worker"},
    )


def require_public_realtime_machine_access(
    request: Request,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
) -> RequestIdentity:
    identity = _resolve_request_identity(
        request=request,
        settings=settings,
        required_machine_scopes={"realtime.predict"},
        allow_browser_session=False,
    )
    _enforce_rate_limit(
        request=request,
        principal_key=f"realtime:{identity.principal_id}",
        limit=settings.inference_limits.realtime_requests_per_minute,
    )
    return identity


def require_public_batch_machine_access(
    request: Request,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
) -> RequestIdentity:
    identity = _resolve_request_identity(
        request=request,
        settings=settings,
        required_machine_scopes={"batch.submit"},
        allow_browser_session=False,
    )
    _enforce_rate_limit(
        request=request,
        principal_key=f"batch:{identity.principal_id}",
        limit=settings.inference_limits.batch_requests_per_minute,
    )
    return identity


def _resolve_request_identity(
    *,
    request: Request,
    settings: PlatformSettings,
    required_machine_scopes: set[str],
    allow_browser_session: bool,
    allowed_machine_subject_types: set[str] | None = None,
    allowed_machine_subject_ids: set[str] | None = None,
) -> RequestIdentity:
    if not settings.api_keys.enabled:
        return RequestIdentity(
            auth_mode="open",
            principal_id=_open_principal_id(request),
        )

    machine_token = parse_machine_token(
        authorization_header=request.headers.get(settings.api_keys.authorization_header_name),
        legacy_api_key_header=request.headers.get(settings.api_keys.legacy_header_name),
    )
    if machine_token is not None:
        key_id, secret = machine_token
        try:
            machine_identity = authenticate_machine_identity(
                settings,
                key_id=key_id,
                secret=secret,
                required_scopes=required_machine_scopes,
                allowed_subject_types=allowed_machine_subject_types,
                allowed_subject_ids=allowed_machine_subject_ids,
            )
        except AuthenticationError as exc:
            _audit_machine_auth_failure(
                settings,
                request=request,
                key_id=key_id,
                required_machine_scopes=required_machine_scopes,
                detail=str(exc),
            )
            raise
        return RequestIdentity(
            auth_mode="machine",
            principal_id=machine_identity.key_id,
            machine_subject=machine_identity.subject_id,
            machine_subject_type=machine_identity.subject_type,
        )

    if not allow_browser_session:
        raise AuthenticationError("Machine authentication is required for this endpoint.")

    token = request.cookies.get(settings.session.cookie_name)
    if not token:
        raise AuthenticationError("Authentication required")
    session_identity: SessionIdentity = read_session_token(
        serializer=build_session_serializer(settings.session.secret()),
        token=token,
        max_age_seconds=settings.session.max_age_seconds,
    )
    active_user = resolve_active_user(
        settings,
        username=session_identity.username,
        session_version=session_identity.session_version,
    )
    if active_user is None:
        raise AuthenticationError(
            "The authenticated account is no longer active or its session was invalidated."
        )
    role = UserRole(active_user.role)
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        csrf_header = request.headers.get(settings.session.csrf_header_name)
        csrf_cookie = request.cookies.get(settings.session.csrf_cookie_name)
        if (
            not csrf_header
            or not csrf_cookie
            or csrf_header != csrf_cookie
            or csrf_header != session_identity.csrf_token
        ):
            raise AuthenticationError("CSRF validation failed for the current browser session.")
    return RequestIdentity(
        auth_mode="session",
        principal_id=active_user.username,
        user=SessionUser(
            user_id=active_user.id,
            username=active_user.username,
            display_name=active_user.display_name,
            role=role,
        ),
    )


def _open_principal_id(request: Request) -> str:
    if request.client and request.client.host:
        return f"anonymous:{request.client.host}"
    return "anonymous"


def _is_internal_runtime_identity(identity: RequestIdentity) -> bool:
    return (
        identity.auth_mode == "machine"
        and identity.principal_id == "ybb-inference-runtime"
        and identity.machine_subject_type == "service"
        and identity.machine_subject == "runtime-inference-client"
    )


def _audit_machine_auth_failure(
    settings: PlatformSettings,
    *,
    request: Request,
    key_id: str,
    required_machine_scopes: set[str],
    detail: str,
) -> None:
    with session_scope(settings) as session:
        create_audit_event(
            session,
            event_type="api_key.auth.failed.v1",
            aggregate_type="api_key",
            aggregate_id=key_id,
            source_service=settings.service.name,
            actor_type="anonymous",
            actor_id=key_id,
            payload={
                "path": str(request.url.path),
                "required_scopes": sorted(required_machine_scopes),
                "remote_addr": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
                "detail": detail,
            },
        )


def _enforce_rate_limit(*, request: Request, principal_key: str, limit: int) -> None:
    limiter = getattr(request.app.state, "fixed_window_rate_limiter", None)
    if limiter is None:
        limiter = FixedWindowRateLimiter()
        request.app.state.fixed_window_rate_limiter = limiter
    limiter.check(key=principal_key, limit=max(limit, 1))
