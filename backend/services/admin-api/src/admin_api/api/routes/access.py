from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from admin_api.application.audit import write_admin_audit_event
from admin_api.dependencies import build_meta, get_settings, require_permission
from cv_contracts import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyDetail,
    ApiKeyRevokeResponse,
    ApiKeyScopeSummary,
    ApiKeySummary,
    ListResponse,
    PlcWriteAccessConnectionSummary,
    PlcWriteAccessPipelineRef,
    PlcWriteAccessStatusResponse,
    PlcWriteAccessUpdateRequest,
    SelfPasswordChangeRequest,
    SessionResponse,
    SessionUser,
    UserCreateRequest,
    UserDetail,
    UserPasswordChangeRequest,
    UserSummary,
    UserUpdateRequest,
)
from cv_core import (
    build_session_serializer,
    encode_machine_token,
    generate_csrf_token,
    hash_machine_secret,
    hash_password,
    issue_session_token,
    join_scopes,
    parse_scope_string,
    verify_password,
)
from cv_db import (
    create_managed_api_key,
    create_user,
    get_api_key_by_record_id,
    get_user_record,
    list_api_key_records,
    list_user_records,
    revoke_api_key,
    session_scope,
    set_user_active,
    update_user_password,
    update_user_profile,
)
from cv_db.models import PipelineDefinition, PipelineVersion
from cv_runtime.siemens_sps import snap7_dependency_available
from cv_status import EndpointMode, UserRole
from cv_utils import AuthenticationError, AuthorizationError, NotFoundError, PlatformError
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

router = APIRouter(tags=["access"])

AccessAdmin = Annotated[
    SessionUser,
    Depends(require_permission("access.admin", UserRole.OPERATOR)),
]

_SCOPE_REGISTRY = [
    ApiKeyScopeSummary(
        scope="realtime.predict",
        label="Realtime inference",
        description="Call the realtime inference routes for synchronous predictions.",
        endpoint_mode=EndpointMode.REALTIME,
        assignable=True,
    ),
    ApiKeyScopeSummary(
        scope="batch.submit",
        label="Batch inference",
        description="Submit, inspect, and collect asynchronous batch inference jobs.",
        endpoint_mode=EndpointMode.BATCH,
        assignable=True,
    ),
    ApiKeyScopeSummary(
        scope="runtime.trigger.invoke",
        label="Trigger endpoints",
        description="Invoke runtime trigger endpoints for PLC/control-system integrations.",
        endpoint_mode=EndpointMode.REALTIME,
        assignable=True,
    ),
    ApiKeyScopeSummary(
        scope="runtime.results.read",
        label="Runtime results",
        description="Read persisted runtime result endpoints for live and trigger pipelines.",
        endpoint_mode=EndpointMode.REALTIME,
        assignable=True,
    ),
    ApiKeyScopeSummary(
        scope="runtime.control",
        label="Internal runtime control",
        description="Reserved for runtime-control and orchestrator service endpoints.",
        endpoint_mode=EndpointMode.REALTIME,
        assignable=False,
    ),
    ApiKeyScopeSummary(
        scope="runtime.executor.invoke",
        label="Internal runtime execution",
        description="Reserved for executor service frame and trigger handoff endpoints.",
        endpoint_mode=EndpointMode.REALTIME,
        assignable=False,
    ),
    ApiKeyScopeSummary(
        scope="runtime.overlay.read",
        label="Internal runtime overlay",
        description="Reserved for overlay-service reads performed through platform routing.",
        endpoint_mode=EndpointMode.REALTIME,
        assignable=False,
    ),
    ApiKeyScopeSummary(
        scope="runtime.capture.drain",
        label="Internal capture drain",
        description="Reserved for capture-service queue drain endpoints.",
        endpoint_mode=EndpointMode.REALTIME,
        assignable=False,
    ),
    ApiKeyScopeSummary(
        scope="runtime.connector.drain",
        label="Internal connector drain",
        description="Reserved for connector-service queue drain endpoints.",
        endpoint_mode=EndpointMode.REALTIME,
        assignable=False,
    ),
    ApiKeyScopeSummary(
        scope="internal.batch.execute",
        label="Internal batch execution",
        description="Reserved for the platform batch worker on internal chunk execution endpoints.",
        endpoint_mode=EndpointMode.BATCH,
        assignable=False,
    ),
]

_PLC_WRITE_ENV_KEY = "CV_PLATFORM__RUNTIME__PLC_WRITES_ALLOWED"
_PLC_RUNTIME_ENV_FILE_ENV_KEYS = ("YBB_RUNTIME_ENV_FILE", "CV_PLATFORM_RUNTIME_ENV_FILE")
_DEFAULT_RUNTIME_ENV_FILE = "/etc/ybb-inferenzserver/runtime.env"


@router.get("/users", response_model=ListResponse[UserSummary])
def list_users(
    _current_user: AccessAdmin,
    active: bool | None = None,
    settings=Depends(get_settings),
) -> ListResponse[UserSummary]:
    with session_scope(settings) as session:
        items = [_user_summary(row) for row in list_user_records(session, active=active)]
    return ListResponse(items=items, meta=build_meta())


@router.post("/users", response_model=UserDetail, status_code=201)
def create_platform_user(
    payload: UserCreateRequest,
    request: Request,
    current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> UserDetail:
    try:
        with session_scope(settings) as session:
            row = create_user(
                session,
                username=payload.username.strip(),
                display_name=payload.display_name.strip(),
                description=_normalize_optional_text(payload.description),
                password_hash=hash_password(payload.password),
                created_by=current_user.username,
                role=UserRole.OPERATOR.value,
            )
            detail = _user_detail(row)
    except ValueError as exc:
        raise PlatformError(
            code="user_already_exists",
            message="A user with this username already exists.",
            detail=str(exc),
            status_code=409,
        ) from exc

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="user.created.v1",
        aggregate_type="user",
        aggregate_id=detail.user_id,
        payload={
            "username": detail.username,
            "display_name": detail.display_name,
            "created_by": current_user.username,
        },
    )
    return detail


@router.get("/users/{user_id}", response_model=UserDetail)
def get_user_detail(
    user_id: str,
    _current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> UserDetail:
    with session_scope(settings) as session:
        row = get_user_record(session, user_id)
        if row is None:
            raise NotFoundError("User not found", detail=user_id)
        return _user_detail(row)


@router.put("/users/{user_id}", response_model=UserDetail)
def update_platform_user(
    user_id: str,
    payload: UserUpdateRequest,
    request: Request,
    current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> UserDetail:
    with session_scope(settings) as session:
        row = get_user_record(session, user_id)
        if row is None:
            raise NotFoundError("User not found", detail=user_id)
        update_user_profile(
            session,
            row,
            display_name=payload.display_name.strip(),
            description=_normalize_optional_text(payload.description),
        )
        detail = _user_detail(row)

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="user.updated.v1",
        aggregate_type="user",
        aggregate_id=detail.user_id,
        payload={
            "username": detail.username,
            "display_name": detail.display_name,
        },
    )
    return detail


@router.post("/users/{user_id}:deactivate", response_model=UserDetail)
def deactivate_user(
    user_id: str,
    request: Request,
    current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> UserDetail:
    with session_scope(settings) as session:
        row = get_user_record(session, user_id)
        if row is None:
            raise NotFoundError("User not found", detail=user_id)
        if row.is_active:
            active_users = list_user_records(session, active=True)
            if len(active_users) <= 1:
                raise PlatformError(
                    code="last_active_user",
                    message="The last active user cannot be deactivated.",
                    detail="Create or reactivate another user before deactivating this account.",
                    status_code=409,
                )
        set_user_active(session, row, is_active=False)
        detail = _user_detail(row)

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="user.deactivated.v1",
        aggregate_type="user",
        aggregate_id=detail.user_id,
        payload={"username": detail.username},
    )
    return detail


@router.post("/users/{user_id}:reactivate", response_model=UserDetail)
def reactivate_user(
    user_id: str,
    request: Request,
    current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> UserDetail:
    with session_scope(settings) as session:
        row = get_user_record(session, user_id)
        if row is None:
            raise NotFoundError("User not found", detail=user_id)
        set_user_active(session, row, is_active=True)
        detail = _user_detail(row)

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="user.reactivated.v1",
        aggregate_type="user",
        aggregate_id=detail.user_id,
        payload={"username": detail.username},
    )
    return detail


@router.post("/users/{user_id}:change-password", response_model=UserDetail)
def change_user_password(
    user_id: str,
    payload: UserPasswordChangeRequest,
    request: Request,
    current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> UserDetail:
    with session_scope(settings) as session:
        row = get_user_record(session, user_id)
        if row is None:
            raise NotFoundError("User not found", detail=user_id)
        update_user_password(session, row, password_hash=hash_password(payload.new_password))
        detail = _user_detail(row)

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="user.password.changed.v1",
        aggregate_type="user",
        aggregate_id=detail.user_id,
        payload={
            "username": detail.username,
            "self_service": current_user.user_id == detail.user_id,
        },
    )
    return detail


@router.post("/users/me/change-password", response_model=SessionResponse)
def change_own_password(
    payload: SelfPasswordChangeRequest,
    request: Request,
    response: Response,
    current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> SessionResponse:
    with session_scope(settings) as session:
        row = get_user_record(session, current_user.user_id)
        if row is None or not row.is_active:
            raise AuthenticationError("The authenticated account is no longer active.")
        if not verify_password(payload.current_password, row.password_hash):
            raise PlatformError(
                code="invalid_current_password",
                message="The current password is incorrect.",
                status_code=422,
            )
        update_user_password(session, row, password_hash=hash_password(payload.new_password))
        user = SessionUser(
            user_id=row.id,
            username=row.username,
            display_name=row.display_name,
            role=UserRole(row.role),
        )
        csrf_token = _set_session_cookies(response, settings=settings, user=row)
        create_audit_payload = {
            "self_service": True,
            "username": row.username,
        }
        event_aggregate_id = row.id

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="user.password.changed.v1",
        aggregate_type="user",
        aggregate_id=event_aggregate_id,
        payload=create_audit_payload,
    )
    return SessionResponse(
        authenticated=True,
        user=user,
        csrf_token=csrf_token,
    )


@router.get("/api-key-scopes", response_model=ListResponse[ApiKeyScopeSummary])
def list_api_key_scopes(
    _current_user: AccessAdmin,
) -> ListResponse[ApiKeyScopeSummary]:
    return ListResponse(items=_SCOPE_REGISTRY, meta=build_meta())


@router.get("/plc-write-access", response_model=PlcWriteAccessStatusResponse)
def get_plc_write_access(
    _current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> PlcWriteAccessStatusResponse:
    return _plc_write_access_status(settings)


@router.put("/plc-write-access", response_model=PlcWriteAccessStatusResponse)
def update_plc_write_access(
    payload: PlcWriteAccessUpdateRequest,
    request: Request,
    current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> PlcWriteAccessStatusResponse:
    if current_user.role != UserRole.PLATFORM_ADMIN:
        raise AuthorizationError("Only platform administrators can change PLC write access.")

    env_state = _runtime_env_state()
    env_path = env_state["path"]
    if env_path is None or not bool(env_state["update_supported"]):
        raise PlatformError(
            code="plc_write_access_read_only",
            message="PLC write access cannot be changed from this deployment.",
            detail=str(env_state.get("detail") or "The runtime env file is not writable."),
            status_code=409,
        )

    before = env_state.get("allow_writes")
    _write_runtime_env_bool(env_path, _PLC_WRITE_ENV_KEY, payload.allow_writes)
    response = _plc_write_access_status(settings)

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="plc_write_access.updated.v1",
        aggregate_type="runtime_safety_setting",
        aggregate_id="plc_writes_allowed",
        payload={
            "before": before,
            "after": payload.allow_writes,
            "runtime_env_path": str(env_path),
            "restart_required": response.restart_required,
        },
    )
    return response


@router.get("/api-keys", response_model=ListResponse[ApiKeySummary])
def list_api_keys(
    request: Request,
    current_user: AccessAdmin,
    state: Literal["all", "active", "revoked", "expired"] = "all",
    source_type: Literal["bootstrap", "managed"] | None = None,
    scope: str | None = None,
    search: str | None = None,
    settings=Depends(get_settings),
) -> ListResponse[ApiKeySummary]:
    with session_scope(settings) as session:
        rows = list_api_key_records(session, source_type=source_type)
        items = [
            item
            for item in (_api_key_summary(row) for row in rows)
            if _matches_key_filters(item, state=state, scope=scope, search=search)
        ]

    if any(item.source_type == "bootstrap" for item in items):
        write_admin_audit_event(
            settings=settings,
            request=request,
            current_user=current_user,
            event_type="api_key.bootstrap.visible.v1",
            aggregate_type="api_key_source",
            aggregate_id="bootstrap",
            payload={"count": sum(1 for item in items if item.source_type == "bootstrap")},
        )
    return ListResponse(items=items, meta=build_meta())


@router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=201)
def create_api_key(
    payload: ApiKeyCreateRequest,
    request: Request,
    current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> ApiKeyCreateResponse:
    requested_scopes = sorted({scope_name.strip() for scope_name in payload.scopes if scope_name.strip()})
    assignable_scopes = {scope_def.scope for scope_def in _SCOPE_REGISTRY if scope_def.assignable}
    invalid_scopes = [scope_name for scope_name in requested_scopes if scope_name not in assignable_scopes]
    if not requested_scopes or invalid_scopes:
        raise PlatformError(
            code="invalid_api_key_scopes",
            message="One or more requested scopes are not assignable to managed API keys.",
            detail=", ".join(invalid_scopes) if invalid_scopes else None,
            status_code=422,
        )
    expires_at = payload.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise PlatformError(
            code="invalid_api_key_expiration",
            message="API key expiration must be in the future.",
            status_code=422,
        )

    key_id = _generate_managed_key_id()
    secret = secrets.token_urlsafe(32)
    token = encode_machine_token(key_id=key_id, secret=secret)
    try:
        with session_scope(settings) as session:
            row = create_managed_api_key(
                session,
                key_id=key_id,
                label=payload.label.strip(),
                description=_normalize_optional_text(payload.description),
                intended_usage=_normalize_optional_text(payload.intended_usage),
                subject_type=payload.subject_type.strip(),
                subject_id=payload.subject_name.strip(),
                scope=join_scopes(requested_scopes),
                hashed_secret=hash_machine_secret(secret),
                secret_prefix=secret[:6],
                created_by=current_user.username,
                expires_at=expires_at,
                endpoint_mode_hint=payload.endpoint_mode_hint.value if payload.endpoint_mode_hint else None,
            )
            detail = _api_key_detail(row)
    except ValueError as exc:
        raise PlatformError(
            code="api_key_conflict",
            message="The generated API key identifier conflicted with an existing key. Please retry.",
            detail=str(exc),
            status_code=409,
        ) from exc

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="api_key.created.v1",
        aggregate_type="api_key",
        aggregate_id=detail.api_key_id,
        payload={
            "key_id": detail.key_id,
            "label": detail.label,
            "subject_id": detail.subject_id,
            "scopes": detail.scopes,
            "source_type": detail.source_type,
        },
    )
    return ApiKeyCreateResponse(
        api_key=detail,
        token=token,
        token_preview=detail.masked_key,
        one_time_reveal=True,
    )


@router.get("/api-keys/{api_key_id}", response_model=ApiKeyDetail)
def get_api_key_detail(
    api_key_id: str,
    request: Request,
    current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> ApiKeyDetail:
    with session_scope(settings) as session:
        row = get_api_key_by_record_id(session, api_key_id)
        if row is None:
            raise NotFoundError("API key not found", detail=api_key_id)
        detail = _api_key_detail(row)

    if detail.source_type == "bootstrap":
        write_admin_audit_event(
            settings=settings,
            request=request,
            current_user=current_user,
            event_type="api_key.bootstrap.visible.v1",
            aggregate_type="api_key",
            aggregate_id=detail.api_key_id,
            payload={"key_id": detail.key_id},
        )
    return detail


@router.post("/api-keys/{api_key_id}:revoke", response_model=ApiKeyRevokeResponse)
def revoke_platform_api_key(
    api_key_id: str,
    request: Request,
    current_user: AccessAdmin,
    settings=Depends(get_settings),
) -> ApiKeyRevokeResponse:
    with session_scope(settings) as session:
        row = get_api_key_by_record_id(session, api_key_id)
        if row is None:
            raise NotFoundError("API key not found", detail=api_key_id)
        if row.source_type == "bootstrap":
            raise PlatformError(
                code="bootstrap_key_read_only",
                message="Bootstrap/config-managed API keys are read-only in the UI.",
                detail="Remove or change this key through configuration and mounted secret files.",
                status_code=409,
            )
        revoke_api_key(session, row, revoked_by=current_user.username)
        detail = _api_key_detail(row)

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="api_key.revoked.v1",
        aggregate_type="api_key",
        aggregate_id=detail.api_key_id,
        payload={
            "key_id": detail.key_id,
            "label": detail.label,
            "source_type": detail.source_type,
        },
    )
    return ApiKeyRevokeResponse(api_key=detail, revoked=True)


def _user_summary(row) -> UserSummary:
    return UserSummary(
        user_id=row.id,
        username=row.username,
        display_name=row.display_name,
        description=row.description,
        is_active=bool(row.is_active),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
        created_by=row.created_by,
        last_login_at=_iso(row.last_login_at),
    )


def _user_detail(row) -> UserDetail:
    return UserDetail(
        **_user_summary(row).model_dump(),
        password_changed_at=_iso(row.password_changed_at),
        session_version=int(row.session_version or 1),
    )


def _api_key_summary(row) -> ApiKeySummary:
    scopes = sorted(parse_scope_string(row.scope))
    now = datetime.now(timezone.utc)
    is_revoked = row.revoked_at is not None
    is_expired = row.expires_at is not None and row.expires_at <= now
    is_active = not is_revoked and not is_expired
    return ApiKeySummary(
        api_key_id=row.record_id or row.id,
        key_id=row.id,
        label=row.label,
        description=row.description,
        intended_usage=row.intended_usage,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        scopes=scopes,
        endpoint_mode_hint=EndpointMode(row.endpoint_mode_hint) if row.endpoint_mode_hint else None,
        source_type=row.source_type,
        masked_key=_masked_token(row.id, row.secret_prefix),
        secret_prefix=row.secret_prefix,
        is_active=is_active,
        is_revoked=is_revoked,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
        created_by=row.created_by,
        last_used_at=_iso(row.last_used_at),
        revoked_at=_iso(row.revoked_at),
        revoked_by=row.revoked_by,
        expires_at=_iso(row.expires_at),
        mutable=row.source_type != "bootstrap",
    )


def _api_key_detail(row) -> ApiKeyDetail:
    summary = _api_key_summary(row)
    status_reason = "Active and accepted for authentication."
    if summary.is_revoked:
        status_reason = "Revoked and no longer accepted for authentication."
    elif summary.expires_at and not summary.is_active:
        status_reason = "Expired and no longer accepted for authentication."
    elif summary.source_type == "bootstrap":
        status_reason = "Bootstrap/config-managed key. Edit or remove it through configuration files."
    return ApiKeyDetail(**summary.model_dump(), status_reason=status_reason)


def _matches_key_filters(
    item: ApiKeySummary,
    *,
    state: Literal["all", "active", "revoked", "expired"],
    scope: str | None,
    search: str | None,
) -> bool:
    if state == "active" and not item.is_active:
        return False
    if state == "revoked" and not item.is_revoked:
        return False
    if state == "expired" and (item.expires_at is None or item.is_active):
        return False
    if scope and scope not in item.scopes:
        return False
    if search:
        normalized = search.strip().lower()
        haystack = " ".join(
            part
            for part in (
                item.key_id,
                item.label,
                item.description or "",
                item.subject_id,
                item.source_type,
            )
            if part
        ).lower()
        if normalized not in haystack:
            return False
    return True


def _masked_token(key_id: str, secret_prefix: str | None) -> str:
    if secret_prefix:
        return f"{key_id}.{secret_prefix}..."
    return f"{key_id}.********"


def _generate_managed_key_id() -> str:
    return f"key_{secrets.token_hex(12)}"


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _plc_write_access_status(settings) -> PlcWriteAccessStatusResponse:
    env_state = _runtime_env_state()
    env_value = env_state.get("allow_writes")
    active_allow_writes = bool(settings.runtime.plc_writes_allowed)
    desired_allow_writes = bool(env_value) if env_value is not None else active_allow_writes
    dependency_available = snap7_dependency_available()
    connections = _configured_plc_connections(
        settings=settings,
        allow_writes=active_allow_writes,
        dependency_available=dependency_available,
    )
    return PlcWriteAccessStatusResponse(
        allow_writes=active_allow_writes,
        desired_allow_writes=desired_allow_writes,
        runtime_env_allow_writes=env_value if isinstance(env_value, bool) else None,
        runtime_env_path=str(env_state["path"]) if env_state.get("path") is not None else None,
        runtime_env_writable=bool(env_state["writable"]),
        update_supported=bool(env_state["update_supported"]),
        restart_required=(
            isinstance(env_value, bool) and env_value != active_allow_writes
        ),
        dependency_available=dependency_available,
        connections=connections,
        meta=build_meta(),
    )


def _runtime_env_state() -> dict[str, object]:
    path = _runtime_env_file_path()
    if path is None:
        return {
            "path": None,
            "allow_writes": None,
            "writable": False,
            "update_supported": False,
            "detail": "No runtime env file path is configured.",
        }
    value: bool | None = None
    detail = ""
    if path.exists():
        try:
            value = _read_runtime_env_bool(path, _PLC_WRITE_ENV_KEY)
        except OSError as exc:
            detail = str(exc)
    writable = _path_writable_for_update(path)
    return {
        "path": path,
        "allow_writes": value,
        "writable": writable,
        "update_supported": writable,
        "detail": detail,
    }


def _runtime_env_file_path() -> Path | None:
    for env_key in _PLC_RUNTIME_ENV_FILE_ENV_KEYS:
        raw_path = os.environ.get(env_key)
        if raw_path and raw_path.strip():
            return Path(raw_path.strip())
    return Path(_DEFAULT_RUNTIME_ENV_FILE)


def _path_writable_for_update(path: Path) -> bool:
    if path.exists():
        return os.access(path, os.W_OK)
    return path.parent.exists() and os.access(path.parent, os.W_OK)


def _read_runtime_env_bool(path: Path, key: str) -> bool | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        raw_key, raw_value = stripped.split("=", 1)
        if raw_key.strip() == key:
            return _parse_env_bool(raw_value.strip().strip('"').strip("'"))
    return None


def _write_runtime_env_bool(path: Path, key: str, value: bool) -> None:
    if not _path_writable_for_update(path):
        raise PlatformError(
            code="plc_write_access_env_not_writable",
            message="The runtime env file is not writable by the admin service.",
            detail=str(path),
            status_code=409,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"{key}={'true' if value else 'false'}"
    changed = False
    next_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            raw_key, _raw_value = stripped.split("=", 1)
            if raw_key.strip() == key:
                next_lines.append(replacement)
                changed = True
                continue
        next_lines.append(line)
    if not changed:
        next_lines.append(replacement)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text("\n".join(next_lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _parse_env_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _configured_plc_connections(
    *,
    settings,
    allow_writes: bool,
    dependency_available: bool,
) -> list[PlcWriteAccessConnectionSummary]:
    grouped: dict[str, dict[str, object]] = {}
    with session_scope(settings) as session:
        definitions = session.scalars(
            select(PipelineDefinition)
            .where(PipelineDefinition.template.is_(False))
            .order_by(PipelineDefinition.name.asc())
        ).all()
        definitions = [row for row in definitions if row.status != "archived"]
        version_ids = {
            version_id
            for definition in definitions
            for version_id in (definition.published_version_id, definition.latest_version_id)
            if version_id
        }
        versions = (
            {
                row.id: row
                for row in session.scalars(
                    select(PipelineVersion).where(PipelineVersion.id.in_(version_ids))
                ).all()
            }
            if version_ids
            else {}
        )
        for definition in definitions:
            scoped_versions: list[tuple[str, PipelineVersion]] = []
            if definition.published_version_id and definition.published_version_id in versions:
                scoped_versions.append(("published", versions[definition.published_version_id]))
            if (
                definition.latest_version_id
                and definition.latest_version_id != definition.published_version_id
                and definition.latest_version_id in versions
            ):
                scoped_versions.append(("latest", versions[definition.latest_version_id]))
            for version_scope, version in scoped_versions:
                for node in _siemens_sps_write_nodes(version.graph):
                    _add_plc_connection_row(
                        grouped,
                        definition=definition,
                        version=version,
                        version_scope=version_scope,
                        node=node,
                        allow_writes=allow_writes,
                        dependency_available=dependency_available,
                    )
    return [_build_plc_connection_summary(item) for item in grouped.values()]


def _siemens_sps_write_nodes(graph: object) -> list[dict[str, object]]:
    if not isinstance(graph, dict):
        return []
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [
        node
        for node in nodes
        if isinstance(node, dict) and node.get("node_type") == "output.siemens_sps_write"
    ]


def _add_plc_connection_row(
    grouped: dict[str, dict[str, object]],
    *,
    definition: PipelineDefinition,
    version: PipelineVersion,
    version_scope: str,
    node: dict[str, object],
    allow_writes: bool,
    dependency_available: bool,
) -> None:
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    assert isinstance(config, dict)
    connection = config.get("connection") if isinstance(config.get("connection"), dict) else {}
    assert isinstance(connection, dict)
    host = str(connection.get("host") or "").strip()
    port = _int_or_default(connection.get("port"), 102)
    rack = _int_or_default(connection.get("rack"), 0)
    slot = _int_or_default(connection.get("slot"), 1)
    display_name = str(connection.get("name") or "Siemens SPS").strip() or "Siemens SPS"
    connection_ref = connection.get("connection_ref")
    connection_ref = str(connection_ref).strip() if connection_ref else None
    connection_key = _plc_connection_key(host, port, rack, slot, connection_ref)
    reconnect_policy = (
        "disabled" if connection.get("reconnect") == "disabled" else "on_failure"
    )
    reuse_policy = (
        "per_runtime" if connection.get("reuse_policy") == "per_runtime" else "per_execution"
    )
    mappings = config.get("mappings") if isinstance(config.get("mappings"), list) else []
    assert isinstance(mappings, list)
    enabled_mappings = [
        mapping
        for mapping in mappings
        if isinstance(mapping, dict) and bool(mapping.get("enabled", True))
    ]
    node_enabled = bool(config.get("enabled", False))
    dry_run = bool(config.get("dry_run", True))
    status, status_reason = _plc_connection_status(
        host=host,
        node_enabled=node_enabled,
        dry_run=dry_run,
        allow_writes=allow_writes,
        dependency_available=dependency_available,
    )
    row = grouped.setdefault(
        connection_key,
        {
            "connection_key": connection_key,
            "display_name": display_name,
            "host": host,
            "port": port,
            "rack": rack,
            "slot": slot,
            "connection_ref": connection_ref,
            "reuse_policies": [],
            "reconnect_policies": [],
            "statuses": [],
            "status_reasons": [],
            "node_count": 0,
            "mapping_count": 0,
            "enabled_mapping_count": 0,
            "sample_targets": [],
            "pipelines": [],
        },
    )
    row["node_count"] = int(row["node_count"]) + 1
    row["mapping_count"] = int(row["mapping_count"]) + len(mappings)
    row["enabled_mapping_count"] = int(row["enabled_mapping_count"]) + len(enabled_mappings)
    cast_statuses = row["statuses"]
    cast_reasons = row["status_reasons"]
    assert isinstance(cast_statuses, list)
    assert isinstance(cast_reasons, list)
    cast_statuses.append(status)
    cast_reasons.append(status_reason)
    reuse_policies = row["reuse_policies"]
    reconnect_policies = row["reconnect_policies"]
    assert isinstance(reuse_policies, list)
    assert isinstance(reconnect_policies, list)
    reuse_policies.append(reuse_policy)
    reconnect_policies.append(reconnect_policy)
    sample_targets = row["sample_targets"]
    assert isinstance(sample_targets, list)
    for target in _sample_plc_targets(mappings):
        if target not in sample_targets and len(sample_targets) < 6:
            sample_targets.append(target)
    pipelines = row["pipelines"]
    assert isinstance(pipelines, list)
    pipelines.append(
        PlcWriteAccessPipelineRef(
            pipeline_definition_id=definition.id,
            pipeline_name=definition.name,
            pipeline_version_id=version.id,
            version_no=int(version.version_no),
            version_scope=version_scope,  # type: ignore[arg-type]
            mode=str(version.mode),
            node_key=str(node.get("node_key") or "sps_write"),
            node_enabled=node_enabled,
            dry_run=dry_run,
            reuse_policy=reuse_policy,  # type: ignore[arg-type]
            reconnect_policy=reconnect_policy,  # type: ignore[arg-type]
            mapping_count=len(mappings),
            enabled_mapping_count=len(enabled_mappings),
        )
    )


def _build_plc_connection_summary(row: dict[str, object]) -> PlcWriteAccessConnectionSummary:
    statuses = row.get("statuses")
    status = _dominant_plc_status(statuses if isinstance(statuses, list) else [])
    reasons = row.get("status_reasons")
    reason = _dominant_plc_reason(status, reasons if isinstance(reasons, list) else [])
    reuse_policy = _dominant_plc_policy(
        row.get("reuse_policies"),
        allowed={"per_execution", "per_runtime"},
        default="per_execution",
    )
    reconnect_policy = _dominant_plc_policy(
        row.get("reconnect_policies"),
        allowed={"disabled", "on_failure"},
        default="on_failure",
    )
    safety_status, safety_reason, persistent_limit = _plc_connection_safety(
        reuse_policy=reuse_policy,
        reconnect_policy=reconnect_policy,
    )
    return PlcWriteAccessConnectionSummary(
        connection_key=str(row["connection_key"]),
        display_name=str(row["display_name"]),
        host=str(row["host"]),
        port=int(row["port"]),
        rack=int(row["rack"]),
        slot=int(row["slot"]),
        connection_ref=(
            row.get("connection_ref") if isinstance(row.get("connection_ref"), str) else None
        ),
        status=status,  # type: ignore[arg-type]
        status_reason=reason,
        reuse_policy=reuse_policy,  # type: ignore[arg-type]
        reconnect_policy=reconnect_policy,  # type: ignore[arg-type]
        connection_safety_status=safety_status,  # type: ignore[arg-type]
        connection_safety_reason=safety_reason,
        persistent_connection_limit=persistent_limit,
        node_count=int(row["node_count"]),
        mapping_count=int(row["mapping_count"]),
        enabled_mapping_count=int(row["enabled_mapping_count"]),
        sample_targets=[
            str(item)
            for item in (
                row.get("sample_targets") if isinstance(row.get("sample_targets"), list) else []
            )
        ],
        pipelines=[
            item
            for item in (row.get("pipelines") if isinstance(row.get("pipelines"), list) else [])
            if isinstance(item, PlcWriteAccessPipelineRef)
        ],
    )


def _plc_connection_status(
    *,
    host: str,
    node_enabled: bool,
    dry_run: bool,
    allow_writes: bool,
    dependency_available: bool,
) -> tuple[str, str]:
    if not host:
        return "invalid_config", "The PLC host is empty."
    if not node_enabled:
        return "disabled", "The Siemens SPS write node is disabled."
    if dry_run:
        return "dry_run", "The node is enabled but still in dry-run mode."
    if not dependency_available:
        return "dependency_missing", "python-snap7 is not installed in this runtime."
    if not allow_writes:
        return "environment_blocked", "The runtime PLC write allow gate is disabled."
    return "ready", "The node is configured for hardware writes after runtime execution."


def _dominant_plc_status(statuses: list[object]) -> str:
    priority = [
        "ready",
        "environment_blocked",
        "dependency_missing",
        "dry_run",
        "invalid_config",
        "disabled",
    ]
    status_set = {str(status) for status in statuses}
    for status in priority:
        if status in status_set:
            return status
    return "disabled"


def _dominant_plc_reason(status: str, reasons: list[object]) -> str:
    for reason in reasons:
        if isinstance(reason, str) and reason:
            if status in reason or status.replace("_", " ") in reason.lower():
                return reason
    for reason in reasons:
        if isinstance(reason, str) and reason:
            return reason
    return "No Siemens SPS write configuration is active."


def _dominant_plc_policy(
    values: object,
    *,
    allowed: set[str],
    default: str,
) -> str:
    if not isinstance(values, list):
        return default
    normalized = {str(value) for value in values if str(value) in allowed}
    if len(normalized) == 1:
        return next(iter(normalized))
    if len(normalized) > 1:
        return "mixed"
    return default


def _plc_connection_safety(
    *,
    reuse_policy: str,
    reconnect_policy: str,
) -> tuple[str, str, int]:
    if reuse_policy == "mixed" or reconnect_policy == "mixed":
        return (
            "mixed_policy",
            "Multiple nodes use different connection or reconnect policies for this PLC endpoint.",
            1 if reuse_policy in {"per_runtime", "mixed"} else 0,
        )
    if reuse_policy == "per_runtime":
        if reconnect_policy == "on_failure":
            return (
                "bounded",
                "Runtime reuse keeps one cached client per PLC endpoint; "
                "failed clients are closed before reconnect.",
                1,
            )
        return (
            "bounded",
            "Runtime reuse keeps one cached client per PLC endpoint and automatic reconnect "
            "is disabled.",
            1,
        )
    if reconnect_policy == "on_failure":
        return (
            "ephemeral",
            "Connections are opened per execution, closed after each execution, "
            "and failed clients are closed before reconnect.",
            0,
        )
    return (
        "ephemeral",
        "Connections are opened per execution and closed after each execution.",
        0,
    )


def _sample_plc_targets(mappings: list[object]) -> list[str]:
    targets: list[str] = []
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        target = mapping.get("target")
        if not isinstance(target, dict):
            continue
        targets.append(_format_plc_target(target))
    return targets


def _format_plc_target(target: dict[str, object]) -> str:
    db_number = _int_or_default(target.get("db_number"), 1)
    byte_offset = _int_or_default(target.get("byte_offset"), 0)
    bit_offset = target.get("bit_offset")
    data_type = str(target.get("data_type") or "INT").upper()
    if data_type == "BOOL":
        return f"DB{db_number}.DBX{byte_offset}.{_int_or_default(bit_offset, 0)} / BOOL"
    if data_type == "BYTE":
        return f"DB{db_number}.DBB{byte_offset} / BYTE"
    if data_type in {"WORD", "INT"}:
        return f"DB{db_number}.DBW{byte_offset} / {data_type}"
    if data_type in {"DWORD", "DINT", "REAL"}:
        return f"DB{db_number}.DBD{byte_offset} / {data_type}"
    return f"DB{db_number}.{byte_offset} / {data_type}"


def _plc_connection_key(
    host: str,
    port: int,
    rack: int,
    slot: int,
    connection_ref: str | None,
) -> str:
    raw_key = f"{connection_ref or ''}|{host}|{port}|{rack}|{slot}"
    return "plc_" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]


def _int_or_default(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _set_session_cookies(response: Response, *, settings, user) -> str:
    csrf_token = generate_csrf_token()
    token = issue_session_token(
        serializer=build_session_serializer(settings.session.secret()),
        username=user.username,
        role=UserRole(user.role),
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
    response.headers["X-CSRF-Token"] = csrf_token
    return csrf_token
