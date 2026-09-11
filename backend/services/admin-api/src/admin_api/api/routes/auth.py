from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from cv_contracts import LoginRequest, SessionResponse, SessionUser
from cv_core import build_session_serializer, generate_csrf_token, issue_session_token
from cv_db import (
    authenticate_user_password,
    create_audit_event,
    get_user_by_username,
    record_user_login,
    session_scope,
)
from cv_status import UserRole
from cv_utils import AuthenticationError

from admin_api.dependencies import CurrentUser, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=SessionResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings = Depends(get_settings),
) -> SessionResponse:
    user = authenticate_user_password(
        settings,
        username=payload.username,
        password=payload.password,
    )
    if user is None:
        with session_scope(settings) as session:
            create_audit_event(
                session,
                event_type="auth.login.failed.v1",
                aggregate_type="user",
                aggregate_id=payload.username,
                source_service="admin-api",
                actor_type="anonymous",
                actor_id=payload.username,
                payload={
                    "path": str(request.url.path),
                    "remote_addr": request.client.host if request.client else None,
                    "user_agent": request.headers.get("user-agent"),
                },
            )
        raise AuthenticationError("Invalid username or password")
    role = UserRole(user.role)
    csrf_token = generate_csrf_token()
    token = issue_session_token(
        serializer=build_session_serializer(settings.session.secret()),
        username=payload.username,
        role=role,
        csrf_token=csrf_token,
        session_version=int(user.session_version or 1),
    )
    response.set_cookie(
        key=settings.session.cookie_name,
        value=token,
        max_age=settings.session.max_age_seconds,
        httponly=True,
        secure=settings.session.secure_cookie,
        samesite=settings.session.same_site,
        path="/",
    )
    response.set_cookie(
        key=settings.session.csrf_cookie_name,
        value=csrf_token,
        max_age=settings.session.max_age_seconds,
        httponly=False,
        secure=settings.session.secure_cookie,
        samesite=settings.session.same_site,
        path="/",
    )
    with session_scope(settings) as session:
        persisted_user = get_user_by_username(session, user.username)
        if persisted_user is not None:
            record_user_login(session, persisted_user)
        create_audit_event(
            session,
            event_type="auth.login.succeeded.v1",
            aggregate_type="user",
            aggregate_id=persisted_user.id if persisted_user is not None else user.id,
            source_service="admin-api",
            actor_type="user",
            actor_id=user.username,
            payload={
                "role": role.value,
                "path": str(request.url.path),
                "remote_addr": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
            },
        )
    return SessionResponse(
        authenticated=True,
        user=SessionUser(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=role,
        ),
        csrf_token=csrf_token,
    )


@router.post("/logout", response_model=SessionResponse)
def logout(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    settings=Depends(get_settings),
) -> SessionResponse:
    with session_scope(settings) as session:
        create_audit_event(
            session,
            event_type="auth.logout.succeeded.v1",
            aggregate_type="user",
            aggregate_id=current_user.user_id,
            source_service="admin-api",
            actor_type="user",
            actor_id=current_user.username,
            payload={
                "path": str(request.url.path),
                "remote_addr": request.client.host if request.client else None,
            },
        )
    response.delete_cookie(settings.session.cookie_name, path="/")
    response.delete_cookie(settings.session.csrf_cookie_name, path="/")
    return SessionResponse(authenticated=False, user=None, csrf_token=None)


@router.get("/me", response_model=SessionResponse)
def me(request: Request, current_user: CurrentUser) -> SessionResponse:
    session_identity = getattr(request.state, "session_identity", None)
    return SessionResponse(
        authenticated=True,
        user=current_user,
        csrf_token=session_identity.csrf_token if session_identity is not None else None,
    )
