from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from cv_contracts import ResponseMeta, SessionUser
from cv_core import (
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
    PlatformError,
    get_correlation_id,
    get_request_id,
    utc_now_iso,
)
from fastapi import Depends, Request

RESULT_ENDPOINT_SCOPE = "runtime.results.read"


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
    session_user: Annotated[SessionUser | None, Depends(get_optional_session_user)],
) -> SessionUser:
    if session_user is None:
        raise AuthenticationError("Authentication required")
    return session_user


CurrentUser = Annotated[SessionUser, Depends(require_session_user)]


def require_permission(
    permission: str,
    *allowed_roles: UserRole,
) -> Callable[..., SessionUser]:
    def _resolve(request: Request, current_user: CurrentUser) -> SessionUser:
        if current_user.role not in allowed_roles:
            raise AuthorizationError(
                "The current user is not allowed to access this runtime-result operation."
            )
        request.state.required_permission = permission
        return current_user

    return _resolve


def resolve_result_endpoint_access(
    request: Request,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
) -> None:
    auth_required = settings.runtime.result_endpoint_auth_required
    authorization_header = request.headers.get(settings.api_keys.authorization_header_name)
    legacy_api_key_header = request.headers.get(settings.api_keys.legacy_header_name)

    if (
        not auth_required
        and not settings.api_keys.enabled
        and not _has_machine_auth_headers(authorization_header, legacy_api_key_header)
    ):
        return None

    if (
        not auth_required
        and not _has_machine_auth_headers(authorization_header, legacy_api_key_header)
    ):
        return None

    machine_token = parse_machine_token(
        authorization_header=authorization_header,
        legacy_api_key_header=legacy_api_key_header,
    )
    if not settings.api_keys.enabled:
        raise PlatformError(
            code="runtime_result_auth_unavailable",
            message=(
                "Runtime result endpoint authentication is required, but API keys are disabled."
            ),
            status_code=503,
            retryable=False,
        )
    if machine_token is None:
        if auth_required:
            raise AuthenticationError(
                "Machine authentication is required for runtime result endpoints."
            )
        return None

    key_id, secret = machine_token
    machine_identity = authenticate_machine_identity(
        settings,
        key_id=key_id,
        secret=secret,
        required_scopes={RESULT_ENDPOINT_SCOPE},
    )
    request.state.machine_identity = machine_identity
    request.state.required_permission = RESULT_ENDPOINT_SCOPE
    return None


def _has_machine_auth_headers(
    authorization_header: str | None,
    legacy_api_key_header: str | None,
) -> bool:
    if legacy_api_key_header and legacy_api_key_header.strip():
        return True
    if not authorization_header:
        return False
    scheme, _, raw_value = authorization_header.partition(" ")
    return scheme.lower() == "bearer" and bool(raw_value.strip())


def build_meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )
