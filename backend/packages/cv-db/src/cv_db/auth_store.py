from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_core import generate_prefixed_id
from cv_utils import utc_now

from cv_db.models import ApiKey, User


def get_user_by_username(session: Session, username: str) -> User | None:
    return session.scalar(select(User).where(User.username == username))


def get_user_record(session: Session, user_id: str) -> User | None:
    return session.get(User, user_id)


def list_user_records(session: Session, *, active: bool | None = None) -> list[User]:
    stmt = select(User).order_by(User.created_at.asc(), User.username.asc())
    if active is not None:
        stmt = stmt.where(User.is_active == active)
    return session.scalars(stmt).all()


def create_user(
    session: Session,
    *,
    username: str,
    display_name: str,
    password_hash: str,
    description: str | None = None,
    created_by: str | None = None,
    role: str,
    is_active: bool = True,
    user_id: str | None = None,
) -> User:
    existing = get_user_by_username(session, username)
    if existing is not None:
        raise ValueError(f"User '{username}' already exists.")
    row = User(
        id=user_id or generate_prefixed_id("usr"),
        username=username,
        display_name=display_name,
        description=description,
        password_hash=password_hash,
        role=role,
        is_active=is_active,
        created_by=created_by,
        password_changed_at=utc_now(),
        session_version=1,
    )
    session.add(row)
    session.flush()
    return row


def ensure_bootstrap_user(
    session: Session,
    *,
    username: str,
    password_hash: str,
    role: str,
    display_name: str | None = None,
    user_id: str | None = None,
) -> User:
    row = get_user_by_username(session, username)
    if row is not None:
        if not row.display_name:
            row.display_name = display_name or username
        if row.created_by is None:
            row.created_by = "bootstrap"
        session.flush()
        return row
    return create_user(
        session,
        user_id=user_id,
        username=username,
        display_name=display_name or username,
        description="Bootstrap administrator created during initial bring-up.",
        password_hash=password_hash,
        created_by="bootstrap",
        role=role,
        is_active=True,
    )


def update_user_profile(
    session: Session,
    user: User,
    *,
    display_name: str | None = None,
    description: str | None = None,
) -> User:
    if display_name is not None:
        user.display_name = display_name
    if description is not None:
        user.description = description
    session.flush()
    return user


def update_user_password(
    session: Session,
    user: User,
    *,
    password_hash: str,
    increment_session_version: bool = True,
) -> User:
    user.password_hash = password_hash
    user.password_changed_at = utc_now()
    if increment_session_version:
        user.session_version = int(user.session_version or 0) + 1
    session.flush()
    return user


def set_user_active(session: Session, user: User, *, is_active: bool) -> User:
    user.is_active = is_active
    if not is_active:
        user.session_version = int(user.session_version or 0) + 1
    session.flush()
    return user


def record_user_login(session: Session, user: User, *, occurred_at: datetime | None = None) -> User:
    user.last_login_at = occurred_at or utc_now()
    session.flush()
    return user


def get_api_key_record(session: Session, key_id: str) -> ApiKey | None:
    return session.get(ApiKey, key_id)


def get_api_key_by_record_id(session: Session, record_id: str) -> ApiKey | None:
    return session.scalar(select(ApiKey).where(ApiKey.record_id == record_id))


def list_api_key_records(
    session: Session,
    *,
    source_type: str | None = None,
) -> list[ApiKey]:
    stmt = select(ApiKey).order_by(ApiKey.created_at.desc(), ApiKey.id.asc())
    if source_type is not None:
        stmt = stmt.where(ApiKey.source_type == source_type)
    return session.scalars(stmt).all()


def create_managed_api_key(
    session: Session,
    *,
    key_id: str,
    label: str,
    description: str | None,
    intended_usage: str | None,
    subject_type: str,
    subject_id: str,
    scope: str,
    hashed_secret: str,
    secret_prefix: str,
    created_by: str,
    expires_at: datetime | None = None,
    endpoint_mode_hint: str | None = None,
) -> ApiKey:
    existing = get_api_key_record(session, key_id)
    if existing is not None:
        raise ValueError(f"API key '{key_id}' already exists.")
    row = ApiKey(
        id=key_id,
        record_id=generate_prefixed_id("apk"),
        label=label,
        description=description,
        intended_usage=intended_usage,
        subject_type=subject_type,
        subject_id=subject_id,
        scope=scope,
        hashed_secret=hashed_secret,
        secret_prefix=secret_prefix,
        endpoint_mode_hint=endpoint_mode_hint,
        source_type="managed",
        created_by=created_by,
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    return row


def upsert_api_key(
    session: Session,
    *,
    key_id: str,
    subject_type: str,
    subject_id: str,
    scope: str,
    hashed_secret: str,
    expires_at: str | None = None,
    label: str | None = None,
    description: str | None = None,
    intended_usage: str | None = None,
    secret_prefix: str | None = None,
    endpoint_mode_hint: str | None = None,
    source_type: str = "bootstrap",
    created_by: str | None = None,
) -> ApiKey:
    row = get_api_key_record(session, key_id)
    parsed_expires_at = _parse_datetime(expires_at)
    if row is None:
        row = ApiKey(
            id=key_id,
            record_id=generate_prefixed_id("apk"),
            label=label or key_id,
            description=description,
            intended_usage=intended_usage,
            subject_type=subject_type,
            subject_id=subject_id,
            scope=scope,
            hashed_secret=hashed_secret,
            secret_prefix=secret_prefix,
            endpoint_mode_hint=endpoint_mode_hint,
            source_type=source_type,
            created_by=created_by,
            expires_at=parsed_expires_at,
            revoked_at=None,
            revoked_by=None,
        )
        session.add(row)
    else:
        row.label = label or row.label or key_id
        row.description = description
        row.intended_usage = intended_usage
        row.subject_type = subject_type
        row.subject_id = subject_id
        row.scope = scope
        row.hashed_secret = hashed_secret
        row.secret_prefix = secret_prefix
        row.endpoint_mode_hint = endpoint_mode_hint
        row.source_type = source_type
        row.created_by = created_by or row.created_by
        row.expires_at = parsed_expires_at
        row.revoked_at = None
        row.revoked_by = None
        if row.record_id is None:
            row.record_id = generate_prefixed_id("apk")
    session.flush()
    return row


def reconcile_bootstrap_api_keys(
    session: Session,
    *,
    active_key_ids: set[str],
) -> None:
    now = utc_now()
    for row in list_api_key_records(session, source_type="bootstrap"):
        if row.id in active_key_ids:
            continue
        if row.revoked_at is None:
            row.revoked_at = now
            row.revoked_by = "bootstrap-config"
    session.flush()


def revoke_api_key(
    session: Session,
    row: ApiKey,
    *,
    revoked_by: str,
    revoked_at: datetime | None = None,
) -> ApiKey:
    row.revoked_at = revoked_at or utc_now()
    row.revoked_by = revoked_by
    session.flush()
    return row


def record_api_key_use(
    session: Session,
    row: ApiKey,
    *,
    occurred_at: datetime | None = None,
) -> ApiKey:
    row.last_used_at = occurred_at or utc_now()
    session.flush()
    return row


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)
