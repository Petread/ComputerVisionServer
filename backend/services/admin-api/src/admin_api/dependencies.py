from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from cv_contracts import ResponseMeta, SessionUser
from cv_core import (
    PlatformSettings,
    SessionIdentity,
    build_session_serializer,
    read_session_token,
)
from cv_db import resolve_active_user
from cv_status import UserRole
from cv_utils import AuthenticationError


def get_settings(request: Request) -> PlatformSettings:
    return request.app.state.settings


def get_optional_session_user(
    settings: Annotated[PlatformSettings, Depends(get_settings)],
    request: Request,
) -> SessionUser | None:
    token = request.cookies.get(settings.session.cookie_name)
    if not token:
        return None
    serializer = build_session_serializer(settings.session.secret())
    identity: SessionIdentity = read_session_token(
        serializer=serializer,
        token=token,
        max_age_seconds=settings.session.max_age_seconds,
    )
    active_user = resolve_active_user(
        settings,
        username=identity.username,
        session_version=identity.session_version,
    )
    if active_user is None:
        raise AuthenticationError("The authenticated account is no longer active or its session was invalidated.")
    request.state.session_identity = identity
    return SessionUser(
        user_id=active_user.id,
        username=active_user.username,
        display_name=active_user.display_name,
        role=UserRole(active_user.role),
    )


def require_session_user(
    request: Request,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
    session_user: Annotated[SessionUser | None, Depends(get_optional_session_user)],
) -> SessionUser:
    if session_user is None:
        raise AuthenticationError("Authentication required")
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        session_identity = getattr(request.state, "session_identity", None)
        csrf_header = request.headers.get(settings.session.csrf_header_name)
        csrf_cookie = request.cookies.get(settings.session.csrf_cookie_name)
        if (
            session_identity is None
            or not csrf_header
            or not csrf_cookie
            or csrf_header != csrf_cookie
            or csrf_header != session_identity.csrf_token
        ):
            raise AuthenticationError("CSRF validation failed for the current browser session.")
    return session_user


CurrentUser = Annotated[SessionUser, Depends(require_session_user)]


def build_meta() -> ResponseMeta:
    from cv_utils import get_correlation_id, get_request_id, utc_now_iso

    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )


def require_roles(*allowed_roles: UserRole):
    def _resolve(current_user: CurrentUser) -> SessionUser:
        return current_user

    return _resolve


def require_permission(permission: str, *allowed_roles: UserRole):
    def _resolve(request: Request, current_user: CurrentUser) -> SessionUser:
        request.state.required_permission = permission
        return current_user

    return _resolve
