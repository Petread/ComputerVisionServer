from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

from admin_api.application.audit import write_admin_audit_event
from admin_api.application.operational_probes import probe_dependencies
from admin_api.dependencies import build_meta, get_settings, require_permission
from cv_contracts import (
    AuthSupportSummary,
    LabelStudioCredentialStatus,
    LabelStudioTokenUpdateRequest,
    LabelStudioTokenUpdateResponse,
    SessionUser,
    SettingsStatusResponse,
    StoragePathSummary,
)
from cv_core import PlatformSettings
from cv_datasets import LabelStudioClient, LabelStudioError
from cv_db import list_deployment_targets, session_scope
from cv_status import UserRole
from cv_utils import AuthorizationError, PlatformError, utc_now_iso
from fastapi import APIRouter, Depends, Request

router = APIRouter(prefix="/settings", tags=["settings"])

SettingsReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "settings.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.VIEWER,
        )
    ),
]
SettingsWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "settings.write",
            UserRole.PLATFORM_ADMIN,
        )
    ),
]
SettingsConfig = Annotated[PlatformSettings, Depends(get_settings)]


@router.get("/integration-status", response_model=SettingsStatusResponse)
async def get_integration_status(
    current_user: SettingsReader,
    settings: SettingsConfig,
) -> SettingsStatusResponse:
    integrations = await probe_dependencies(settings)
    storage_paths = [
        _storage_path("state_root", "Platform State Root", settings.storage.state_root),
        _storage_path("control_root", "Control Plane Root", str(settings.storage.control_root())),
        _storage_path("snapshot_root", "Snapshot Root", str(settings.storage.snapshots_root())),
        _storage_path("training_root", "Training Root", settings.storage.training_root),
        _storage_path("serving_root", "Serving Root", settings.storage.serving_root),
        _storage_path("audit_root", "Audit Root", settings.storage.audit_root),
    ]
    with session_scope(settings) as session:
        deployment_targets = list_deployment_targets(session)
    return SettingsStatusResponse(
        environment=settings.env.value,
        public_base_url=settings.service.public_base_url,
        storage_paths=storage_paths,
        integrations=integrations,
        deployment_targets=deployment_targets,
        auth=AuthSupportSummary(
            cookie_name=settings.session.cookie_name,
            csrf_cookie_name=settings.session.csrf_cookie_name,
            csrf_header_name=settings.session.csrf_header_name,
            secure_cookie=settings.session.secure_cookie,
            session_max_age_seconds=settings.session.max_age_seconds,
            current_user=current_user,
            supported_roles=[role for role in UserRole if role != UserRole.INFERENCE_CLIENT],
            api_key_auth_enabled=settings.api_keys.enabled,
            api_key_bootstrap_count=len(settings.api_keys.bootstrap_keys),
        ),
        label_studio_credentials=_label_studio_credential_status(settings),
        feature_flags={
            "webhook_auth_enabled": settings.label_studio.webhook_auth.enabled,
            "training_start_allowed": settings.gpu.allow_training_start,
            "training_pause_on_slo_pressure": settings.gpu.pause_training_on_slo_pressure,
        },
        meta=build_meta(),
    )


@router.post("/label-studio-token", response_model=LabelStudioTokenUpdateResponse)
async def update_label_studio_token(
    payload: LabelStudioTokenUpdateRequest,
    request: Request,
    current_user: SettingsWriter,
    settings: SettingsConfig,
) -> LabelStudioTokenUpdateResponse:
    if current_user.role != UserRole.PLATFORM_ADMIN:
        raise AuthorizationError("Only platform administrators can update integration secrets.")

    token = payload.token.strip()
    if not token:
        raise PlatformError(
            code="label_studio_token_invalid",
            message="The Label Studio API token cannot be empty.",
            status_code=422,
        )

    try:
        project_count = await asyncio.to_thread(_verify_label_studio_token, settings, token)
    except LabelStudioError as exc:
        raise PlatformError(
            code="label_studio_token_rejected",
            message="Label Studio rejected the API token.",
            status_code=400,
            detail=str(exc),
        ) from exc

    target_path = await asyncio.to_thread(_write_label_studio_token, settings, token)
    status = _label_studio_credential_status(
        settings,
        write_target_path=target_path,
        last_verified=True,
        project_count=project_count,
        message=f"Token saved and verified against {project_count} Label Studio projects.",
    )
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="settings.label_studio_token.updated.v1",
        aggregate_type="integration",
        aggregate_id="label-studio",
        payload={
            "token_file_path": status.token_file_path,
            "write_target_path": status.write_target_path,
            "project_count": project_count,
            "verified": True,
        },
    )
    return LabelStudioTokenUpdateResponse(credentials=status, meta=build_meta())


def _storage_path(key: str, label: str, path_value: str) -> StoragePathSummary:
    path = Path(path_value)
    writable = False
    try:
        if path.exists():
            writable = path.is_dir() and path.stat() is not None
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            writable = path.parent.exists()
    except Exception:
        writable = False
    return StoragePathSummary(
        key=key,
        label=label,
        path=str(path),
        exists=path.exists(),
        writable=writable,
    )


def _label_studio_credential_status(
    settings: PlatformSettings,
    *,
    write_target_path: Path | None = None,
    last_verified: bool | None = None,
    project_count: int | None = None,
    message: str | None = None,
) -> LabelStudioCredentialStatus:
    token_file = Path(settings.label_studio.token_file)
    candidates = _label_studio_token_write_candidates(settings)
    selected_target = write_target_path or next(
        (path for path in candidates if _can_write_secret(path)),
        None,
    )
    configured = bool(settings.label_studio.token())
    return LabelStudioCredentialStatus(
        configured=configured,
        token_file_path=str(token_file),
        write_target_path=str(selected_target) if selected_target else None,
        writable=selected_target is not None,
        last_verified=last_verified,
        project_count=project_count,
        message=message
        or (
            "A Label Studio API token is configured."
            if configured
            else "No Label Studio API token is configured."
        ),
        updated_at=utc_now_iso(),
    )


def _verify_label_studio_token(settings: PlatformSettings, token: str) -> int:
    client = LabelStudioClient(
        base_url=settings.label_studio.base_url,
        token=token,
        timeout_seconds=settings.label_studio.request_timeout_seconds,
    )
    return len(client.list_projects())


def _write_label_studio_token(settings: PlatformSettings, token: str) -> Path:
    errors: list[str] = []
    for candidate in _label_studio_token_write_candidates(settings):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            temp_path = candidate.with_name(f".{candidate.name}.tmp")
            temp_path.write_text(token, encoding="utf-8")
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            temp_path.replace(candidate)
            return candidate
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    raise PlatformError(
        code="label_studio_token_not_writable",
        message="The Label Studio API token file could not be written.",
        status_code=500,
        detail="; ".join(errors) if errors else None,
    )


def _label_studio_token_write_candidates(settings: PlatformSettings) -> list[Path]:
    token_path = Path(settings.label_studio.token_file)
    candidates = [token_path]
    normalized = token_path.as_posix()
    if normalized.startswith("/run/secrets/"):
        workspace_secret_path = Path("/workspace/infra/secrets") / token_path.name
        candidates.append(workspace_secret_path)
    return list(dict.fromkeys(candidates))


def _can_write_secret(path: Path) -> bool:
    try:
        if path.exists():
            return path.is_file() and os.access(path, os.W_OK)
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.access(path.parent, os.W_OK)
    except OSError:
        return False
