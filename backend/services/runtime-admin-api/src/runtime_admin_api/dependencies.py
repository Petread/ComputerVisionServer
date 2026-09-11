from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from cv_contracts import ResponseMeta, SessionUser
from cv_core import (
    MachineIdentity,
    PlatformSettings,
    SessionIdentity,
    build_session_serializer,
    parse_machine_token,
    read_session_token,
)
from cv_db import authenticate_machine_identity, resolve_active_user
from cv_status import UserRole
from cv_utils import (
    AuthenticationError,
    AuthorizationError,
    get_correlation_id,
    get_request_id,
    utc_now_iso,
)
from fastapi import Depends, Request


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
        raise AuthenticationError(
            "The authenticated account is no longer active or its session was invalidated."
        )
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
RuntimeAdminPrincipal = SessionUser | MachineIdentity
_MACHINE_READ_SCOPE = "runtime.results.read"
_MACHINE_READ_PERMISSIONS = frozenset({"runtime.channels.read"})
_MACHINE_CONTROL_SCOPE = "runtime.control"
_MACHINE_CONTROL_PERMISSIONS = frozenset({"runtime.channels.control"})


def build_meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )


def require_permission(
    permission: str,
    *allowed_roles: UserRole,
) -> Callable[..., RuntimeAdminPrincipal]:
    def _resolve(
        request: Request,
        settings: Annotated[PlatformSettings, Depends(get_settings)],
        session_user: Annotated[SessionUser | None, Depends(get_optional_session_user)],
    ) -> RuntimeAdminPrincipal:
        if session_user is not None:
            current_user = require_session_user(request, settings, session_user)
            if allowed_roles and current_user.role not in allowed_roles:
                raise AuthorizationError(
                    "The current user is not allowed to perform this runtime operation."
                )
            request.state.required_permission = permission
            return current_user
        machine_identity = _resolve_machine_principal(request, settings, permission=permission)
        if machine_identity is not None:
            return machine_identity
        raise AuthenticationError("Authentication required")

    return _resolve


def _resolve_machine_principal(
    request: Request,
    settings: PlatformSettings,
    *,
    permission: str,
) -> MachineIdentity | None:
    required_scope: str | None = None
    allowed_subject_types: set[str] | None = None
    if (
        request.method.upper() in {"GET", "HEAD", "OPTIONS"}
        and permission in _MACHINE_READ_PERMISSIONS
    ):
        required_scope = _MACHINE_READ_SCOPE
    elif permission in _MACHINE_CONTROL_PERMISSIONS:
        required_scope = _MACHINE_CONTROL_SCOPE
        allowed_subject_types = {"service"}
    if required_scope is None:
        return None
    machine_token = parse_machine_token(
        authorization_header=request.headers.get(settings.api_keys.authorization_header_name),
        legacy_api_key_header=request.headers.get(settings.api_keys.legacy_header_name),
    )
    if machine_token is None:
        return None
    if not settings.api_keys.enabled:
        raise AuthenticationError("Machine authentication is not enabled for runtime admin APIs.")
    key_id, secret = machine_token
    try:
        machine_identity = authenticate_machine_identity(
            settings,
            key_id=key_id,
            secret=secret,
            required_scopes={required_scope},
            allowed_subject_types=allowed_subject_types,
        )
    except AuthenticationError:
        raise
    except Exception as exc:
        raise AuthenticationError("Machine authentication failed.") from exc
    request.state.machine_identity = machine_identity
    request.state.required_permission = required_scope
    return machine_identity
