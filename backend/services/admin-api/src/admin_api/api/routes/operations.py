from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from cv_contracts import (
    OfflineUpdateApplyRequest,
    OfflineUpdateApplyResult,
    OfflineUpdateBundleSummary,
    OfflineUpdateChange,
    OfflineUpdatePlanResponse,
    OfflineUpdateScheduleRequest,
    OfflineUpdateScheduleResult,
    OfflineUpdateServicePlan,
    OperationWorkerSummary,
    OperationWorkersResponse,
    ResponseMeta,
    SessionUser,
)
from cv_status import HealthState
from cv_status import UserRole
from cv_utils import AuthorizationError, utc_now_iso

from admin_api.application.audit import write_admin_audit_event
from admin_api.dependencies import get_settings, require_permission

router = APIRouter(prefix="/operations", tags=["operations"])

OperationsReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "operations.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.VIEWER,
        )
    ),
]

OperationsAdmin = Annotated[
    SessionUser,
    Depends(require_permission("operations.admin", UserRole.PLATFORM_ADMIN)),
]

_MANAGED_UNITS: tuple[tuple[str, str, str, bool], ...] = (
    ("caddy", "caddy.service", "Reverse Proxy", True),
    ("label-studio", "ybb-label-studio.service", "Label Studio", True),
    ("mlflow", "ybb-mlflow.service", "MLflow", True),
    ("admin-api", "ybb-admin-api.service", "Admin API", False),
    ("dataset-service", "ybb-dataset-service.service", "Dataset Service", True),
    ("training-orchestrator", "ybb-training-orchestrator.service", "Training Orchestrator", True),
    ("model-registry-service", "ybb-model-registry-service.service", "Model Registry", True),
    ("deployment-controller", "ybb-deployment-controller.service", "Deployment Controller", True),
    ("webhook-receiver", "ybb-webhook-receiver.service", "Webhook Receiver", True),
    ("inference-gateway-rt", "ybb-inference-gateway-rt.service", "Inference Gateway RT", True),
    ("inference-gateway-batch", "ybb-inference-gateway-batch.service", "Inference Gateway Batch", True),
    ("label-studio-ml-adapter", "ybb-label-studio-ml-adapter.service", "Label Studio ML Adapter", True),
    ("runtime-admin-api", "ybb-runtime-admin-api.service", "Runtime Admin API", True),
    ("mediamtx", "ybb-mediamtx.service", "MediaMTX Stream Fanout", True),
    ("channel-service", "ybb-channel-service.service", "Channel Service", True),
    ("pipeline-definition-service", "ybb-pipeline-definition-service.service", "Pipeline Definition Service", True),
    ("runtime-orchestrator", "ybb-runtime-orchestrator.service", "Runtime Orchestrator", True),
    ("realtime-executor", "ybb-realtime-executor.service", "Realtime Executor", True),
    ("trigger-executor", "ybb-trigger-executor.service", "Trigger Executor", True),
    ("overlay-service", "ybb-overlay-service.service", "Overlay Service", True),
    ("capture-service", "ybb-capture-service.service", "Capture Service", True),
    ("result-service", "ybb-result-service.service", "Result Service", True),
    ("connector-service", "ybb-connector-service.service", "Connector Service", True),
    ("control-worker", "ybb-control-worker.service", "Control Worker", True),
    ("training-worker", "ybb-training-worker.service", "Training Worker", True),
    ("batch-worker", "ybb-batch-worker.service", "Batch Worker", True),
    ("maintenance-worker", "ybb-maintenance-worker.service", "Maintenance Worker", True),
)
_UNIT_BY_SERVICE = {service_name: unit for service_name, unit, _label, _restartable in _MANAGED_UNITS}
_RESTARTABLE_BY_SERVICE = {
    service_name: restartable for service_name, _unit, _label, restartable in _MANAGED_UNITS
}

_WINDOWS_UPDATE_SERVICES = [
    service_name
    for service_name, _unit, _label, _restartable in _MANAGED_UNITS
    if service_name != "admin-api"
]
_WINDOWS_UPDATE_WORKERS = ["control-worker", "training-worker", "batch-worker", "maintenance-worker"]
_MODEL_UPDATE_SERVICES = [
    "model-registry-service",
    "deployment-controller",
    "inference-gateway-rt",
    "inference-gateway-batch",
    "runtime-orchestrator",
]


def _is_windows_hosted_mediamtx() -> bool:
    return os.getenv("YBB_MEDIAMTX_HOST_PLATFORM", "").strip().lower() == "windows"


def _iter_managed_units() -> tuple[tuple[str, str, str, bool], ...]:
    if not _is_windows_hosted_mediamtx():
        return _MANAGED_UNITS
    return tuple(item for item in _MANAGED_UNITS if item[0] != "mediamtx")


@router.get("/workers", response_model=OperationWorkersResponse)
def operation_workers(_current_user: OperationsReader) -> OperationWorkersResponse:
    return _read_operation_workers()


@router.post("/workers/{service_name}:restart", response_model=OperationWorkersResponse)
def restart_operation_worker(
    service_name: str,
    request: Request,
    current_user: OperationsAdmin,
    settings=Depends(get_settings),
) -> OperationWorkersResponse:
    _require_platform_admin(current_user)
    if service_name == "mediamtx" and _is_windows_hosted_mediamtx():
        raise HTTPException(status_code=409, detail="MediaMTX is hosted by Windows and is not managed by WSL systemd.")
    unit_name = _UNIT_BY_SERVICE.get(service_name)
    if unit_name is None:
        raise HTTPException(status_code=404, detail=f"Unknown managed service: {service_name}")
    if not _RESTARTABLE_BY_SERVICE.get(service_name, True):
        raise HTTPException(status_code=409, detail=f"{service_name} cannot be restarted from this view.")

    result = subprocess.run(
        ["systemctl", "restart", unit_name],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"systemctl exited with {result.returncode}").strip()
        raise HTTPException(status_code=500, detail=detail)

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="operations.worker_restarted",
        aggregate_type="systemd_unit",
        aggregate_id=unit_name,
        payload={"service_name": service_name, "unit_name": unit_name},
    )
    return _read_operation_workers()


@router.get("/offline-update/plan", response_model=OfflineUpdatePlanResponse)
def offline_update_plan(_current_user: OperationsReader) -> OfflineUpdatePlanResponse:
    try:
        payload = _agent_request("GET", "/api/v1/operations-agent/offline-update/plan")
    except RuntimeError:
        return _native_offline_update_plan()
    return OfflineUpdatePlanResponse.model_validate(payload)


@router.post("/offline-update:mark-current", response_model=OfflineUpdatePlanResponse)
def mark_current_update_baseline(
    request: Request,
    current_user: OperationsAdmin,
    settings=Depends(get_settings),
) -> OfflineUpdatePlanResponse:
    _require_platform_admin(current_user)
    try:
        payload = _agent_request("POST", "/api/v1/operations-agent/offline-update:mark-current")
        response = OfflineUpdatePlanResponse.model_validate(payload)
    except RuntimeError as exc:
        response = _native_offline_update_plan(agent_warning=str(exc))
        response.warnings.append("Native Windows updates do not use a Git baseline.")
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="offline_update.baseline_marked",
        aggregate_type="offline_update",
        aggregate_id=response.current_git_sha or "unknown",
        payload={
            "current_git_ref": response.current_git_ref,
            "current_git_sha": response.current_git_sha,
            "repo_root": response.repo_root,
        },
    )
    return response


@router.post("/offline-update:apply", response_model=OfflineUpdateApplyResult)
def apply_offline_update(
    payload: OfflineUpdateApplyRequest,
    request: Request,
    current_user: OperationsAdmin,
    settings=Depends(get_settings),
) -> OfflineUpdateApplyResult:
    _require_platform_admin(current_user)
    native_plan = _native_offline_update_plan()
    if _find_bundle_by_token(native_plan, payload.apply_token) is not None:
        response = _native_apply_update(payload, native_plan)
    else:
        response_payload = _agent_request(
            "POST",
            "/api/v1/operations-agent/offline-update:apply",
            payload.model_dump(mode="json"),
        )
        response = OfflineUpdateApplyResult.model_validate(response_payload)
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="offline_update.apply_requested",
        aggregate_type="offline_update",
        aggregate_id=response.operation_id,
        payload={
            "status": response.status,
            "service_names": response.service_names,
            "changed_files": response.changed_files,
            "blockers": response.blockers,
            "warnings": response.warnings,
            "reason": payload.reason,
            "force": payload.force,
        },
    )
    return response


@router.post("/offline-update:schedule", response_model=OfflineUpdateScheduleResult)
def schedule_offline_update(
    payload: OfflineUpdateScheduleRequest,
    request: Request,
    current_user: OperationsAdmin,
    settings=Depends(get_settings),
) -> OfflineUpdateScheduleResult:
    _require_platform_admin(current_user)
    native_plan = _native_offline_update_plan()
    response = _native_schedule_update(payload, native_plan)
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="offline_update.scheduled",
        aggregate_type="offline_update",
        aggregate_id=response.operation_id,
        payload={
            "status": response.status,
            "scheduled_for": response.scheduled_for,
            "blockers": response.blockers,
            "warnings": response.warnings,
            "reason": payload.reason,
        },
    )
    return response


def _require_platform_admin(current_user: SessionUser) -> None:
    if current_user.role != UserRole.PLATFORM_ADMIN:
        raise AuthorizationError("Only platform administrators can apply offline updates.")


def _read_operation_workers() -> OperationWorkersResponse:
    return OperationWorkersResponse(
        items=[
            _read_systemd_unit(service_name, unit_name, label, restartable)
            for service_name, unit_name, label, restartable in _iter_managed_units()
        ],
        meta=ResponseMeta(generated_at=utc_now_iso()),
    )


def _read_systemd_unit(
    service_name: str,
    unit_name: str,
    label: str,
    restartable: bool,
) -> OperationWorkerSummary:
    observed_at = utc_now_iso()
    result = subprocess.run(
        [
            "systemctl",
            "show",
            unit_name,
            "--no-pager",
            "--property=LoadState,ActiveState,SubState,Description,MainPID,NRestarts,Result,ExecMainStatus",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "systemctl status could not be read.").strip()
        return OperationWorkerSummary(
            service_name=service_name,
            unit_name=unit_name,
            display_name=label,
            state=HealthState.DOWN,
            status="unavailable",
            detail=detail,
            last_error=detail,
            observed_at=observed_at,
            restartable=restartable,
        )

    props = _parse_systemctl_properties(result.stdout)
    load_state = props.get("LoadState", "")
    active_state = props.get("ActiveState", "")
    sub_state = props.get("SubState", "")
    main_pid = _parse_optional_int(props.get("MainPID"))
    restart_count = _parse_optional_int(props.get("NRestarts")) or 0
    unit_result = props.get("Result", "")
    exec_status = props.get("ExecMainStatus", "")
    state = HealthState.OK
    if active_state in {"activating", "reloading", "deactivating"}:
        state = HealthState.DEGRADED
    elif active_state != "active":
        state = HealthState.DOWN
    if load_state == "not-found":
        state = HealthState.DOWN
        detail = "Systemd unit is not installed."
    elif state == HealthState.OK:
        detail = f"{active_state}/{sub_state}"
        if main_pid:
            detail += f" (PID {main_pid})"
    else:
        parts = [item for item in (active_state, sub_state, unit_result) if item]
        detail = "/".join(parts) or "Service is not active."
        if exec_status and exec_status != "0":
            detail += f" Exit status {exec_status}."

    last_error = _last_journal_line(unit_name) if state != HealthState.OK else None
    return OperationWorkerSummary(
        service_name=service_name,
        unit_name=unit_name,
        display_name=label,
        state=state,
        status=f"{active_state or 'unknown'}/{sub_state or 'unknown'}",
        detail=detail,
        active_state=active_state or None,
        sub_state=sub_state or None,
        main_pid=main_pid if main_pid and main_pid > 0 else None,
        restart_count=restart_count,
        last_error=last_error,
        observed_at=observed_at,
        restartable=restartable,
    )


def _native_offline_update_plan(agent_warning: str | None = None) -> OfflineUpdatePlanResponse:
    generated_at = utc_now_iso()
    warnings = [agent_warning] if agent_warning else []
    app_dir = _resolve_windows_app_dir()
    if app_dir is None:
        return OfflineUpdatePlanResponse(
            enabled=False,
            apply_available=False,
            blocked=True,
            blockers=[
                "Windows installation directory could not be found. Set YBB_WINDOWS_APP_DIR or keep the default C:\\YBB_Inferenzserver\\ybb-inferenzserver layout."
            ],
            warnings=warnings,
            meta=ResponseMeta(generated_at=generated_at),
        )

    incoming_dir = app_dir / "incoming"
    if not incoming_dir.exists():
        return OfflineUpdatePlanResponse(
            enabled=True,
            apply_available=False,
            incoming_dir=str(incoming_dir),
            blocked=True,
            blockers=[f"Incoming directory is missing: {incoming_dir}"],
            warnings=warnings,
            meta=ResponseMeta(generated_at=generated_at),
        )

    bundles = _scan_incoming_bundles(incoming_dir)
    selected_bundle = bundles[0] if bundles else None
    blockers = list(selected_bundle.blockers) if selected_bundle else []
    updater_script = app_dir / "updater" / "apply-windows-update.ps1"
    if selected_bundle and not updater_script.exists():
        blockers.append(f"Windows updater script is missing: {updater_script}")
    if selected_bundle:
        warnings.extend(selected_bundle.warnings)

    service_plans = _service_plans_for_bundle(selected_bundle)
    changed_files = _changes_for_bundle(selected_bundle)
    return OfflineUpdatePlanResponse(
        enabled=True,
        apply_available=bool(selected_bundle) and not blockers,
        pending_change_count=len(bundles),
        changed_files=changed_files,
        service_plans=service_plans,
        blocked=bool(blockers),
        blockers=blockers,
        warnings=warnings,
        apply_token=selected_bundle.apply_token if selected_bundle else None,
        incoming_dir=str(incoming_dir),
        incoming_bundles=bundles,
        selected_bundle=selected_bundle,
        meta=ResponseMeta(generated_at=generated_at),
    )


def _native_apply_update(
    payload: OfflineUpdateApplyRequest,
    plan: OfflineUpdatePlanResponse,
) -> OfflineUpdateApplyResult:
    generated_at = utc_now_iso()
    operation_id = f"opu_{uuid.uuid4().hex}"
    bundle = _find_bundle_by_token(plan, payload.apply_token)
    if bundle is None:
        return OfflineUpdateApplyResult(
            operation_id=operation_id,
            status="blocked",
            accepted_at=generated_at,
            message="Update bundle was not found in incoming.",
            blockers=["The supplied apply token does not match an incoming bundle."],
            meta=ResponseMeta(generated_at=generated_at),
        )
    if plan.blocked:
        return OfflineUpdateApplyResult(
            operation_id=operation_id,
            status="blocked",
            accepted_at=generated_at,
            message="Update cannot be applied because the plan is blocked.",
            blockers=plan.blockers,
            warnings=plan.warnings,
            meta=ResponseMeta(generated_at=generated_at),
        )

    app_dir = _resolve_windows_app_dir()
    if app_dir is None:
        blockers = ["Windows installation directory could not be found."]
        return OfflineUpdateApplyResult(
            operation_id=operation_id,
            status="blocked",
            accepted_at=generated_at,
            message=blockers[0],
            blockers=blockers,
            meta=ResponseMeta(generated_at=generated_at),
        )

    try:
        unit_name, log_path = _start_native_update_unit(
            app_dir=app_dir,
            bundle_path=Path(bundle.path),
            bundle=bundle,
            operation_id=operation_id,
            scheduled_for=None,
        )
    except Exception as exc:  # noqa: BLE001
        return OfflineUpdateApplyResult(
            operation_id=operation_id,
            status="blocked",
            accepted_at=generated_at,
            message=f"Update unit could not be started: {exc}",
            blockers=[str(exc)],
            warnings=plan.warnings,
            meta=ResponseMeta(generated_at=generated_at),
        )

    return OfflineUpdateApplyResult(
        operation_id=operation_id,
        status="accepted",
        accepted_at=generated_at,
        service_names=payload.service_names or bundle.affected_services,
        changed_files=[bundle.file_name],
        message=f"Update {bundle.file_name} wurde als {unit_name} gestartet. Log: {log_path}",
        warnings=plan.warnings,
        meta=ResponseMeta(generated_at=generated_at),
    )


def _native_schedule_update(
    payload: OfflineUpdateScheduleRequest,
    plan: OfflineUpdatePlanResponse,
) -> OfflineUpdateScheduleResult:
    generated_at = utc_now_iso()
    operation_id = f"opu_{uuid.uuid4().hex}"
    bundle = _find_bundle_by_token(plan, payload.apply_token)
    if bundle is None:
        return OfflineUpdateScheduleResult(
            operation_id=operation_id,
            status="blocked",
            scheduled_for=payload.scheduled_for,
            message="Update bundle was not found in incoming.",
            blockers=["The supplied apply token does not match an incoming bundle."],
            meta=ResponseMeta(generated_at=generated_at),
        )
    if plan.blocked:
        return OfflineUpdateScheduleResult(
            operation_id=operation_id,
            status="blocked",
            scheduled_for=payload.scheduled_for,
            message="Update cannot be scheduled because the plan is blocked.",
            blockers=plan.blockers,
            warnings=plan.warnings,
            meta=ResponseMeta(generated_at=generated_at),
        )

    scheduled_for = _parse_schedule_datetime(payload.scheduled_for)
    if scheduled_for is None:
        return OfflineUpdateScheduleResult(
            operation_id=operation_id,
            status="blocked",
            scheduled_for=payload.scheduled_for,
            message="Invalid schedule timestamp.",
            blockers=["Use a valid date and time."],
            meta=ResponseMeta(generated_at=generated_at),
        )
    if scheduled_for <= datetime.now(tz=UTC):
        return OfflineUpdateScheduleResult(
            operation_id=operation_id,
            status="blocked",
            scheduled_for=payload.scheduled_for,
            message="Scheduled time is in the past.",
            blockers=["Choose a future date and time."],
            meta=ResponseMeta(generated_at=generated_at),
        )

    app_dir = _resolve_windows_app_dir()
    if app_dir is None:
        blockers = ["Windows installation directory could not be found."]
        return OfflineUpdateScheduleResult(
            operation_id=operation_id,
            status="blocked",
            scheduled_for=payload.scheduled_for,
            message=blockers[0],
            blockers=blockers,
            meta=ResponseMeta(generated_at=generated_at),
        )

    try:
        unit_name, log_path = _start_native_update_unit(
            app_dir=app_dir,
            bundle_path=Path(bundle.path),
            bundle=bundle,
            operation_id=operation_id,
            scheduled_for=scheduled_for,
        )
    except Exception as exc:  # noqa: BLE001
        return OfflineUpdateScheduleResult(
            operation_id=operation_id,
            status="blocked",
            scheduled_for=payload.scheduled_for,
            message=f"Systemd timer could not be created: {exc}",
            blockers=[str(exc)],
            warnings=plan.warnings,
            meta=ResponseMeta(generated_at=generated_at),
        )

    return OfflineUpdateScheduleResult(
        operation_id=operation_id,
        status="accepted",
        scheduled_for=scheduled_for.isoformat(),
        message=f"Update wurde als {unit_name} geplant. Log: {log_path}",
        warnings=plan.warnings,
        meta=ResponseMeta(generated_at=generated_at),
    )


def _scan_incoming_bundles(incoming_dir: Path) -> list[OfflineUpdateBundleSummary]:
    bundles: list[OfflineUpdateBundleSummary] = []
    for bundle_path in incoming_dir.glob("*.zip"):
        name = bundle_path.name
        if not (name.endswith("-windows-update.zip") or "pretrained-models-update" in name):
            continue
        bundles.append(_summarize_bundle(bundle_path))
    bundles.sort(key=lambda item: item.modified_at, reverse=True)
    return bundles


def _summarize_bundle(bundle_path: Path) -> OfflineUpdateBundleSummary:
    stat = bundle_path.stat()
    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat().replace("+00:00", "Z")
    token = hashlib.sha256(f"{bundle_path}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")).hexdigest()
    blockers: list[str] = []
    warnings: list[str] = []
    manifest: dict[str, object] = {}
    bundle_type = "unknown"
    try:
        manifest, bundle_type = _read_bundle_manifest(bundle_path)
    except Exception as exc:  # noqa: BLE001
        blockers.append(f"Bundle manifest could not be read: {exc}")
    if bundle_type == "unknown":
        blockers.append("No known bundle manifest was found.")
    app_name = _manifest_str(manifest, "AppName")
    app_version = _manifest_str(manifest, "AppVersion")
    if bundle_type == "windows_update":
        if not app_name:
            blockers.append("windows-bundle-manifest.json does not contain AppName.")
        if not app_version:
            blockers.append("windows-bundle-manifest.json does not contain AppVersion.")
        _validate_windows_streaming_bundle(
            bundle_path=bundle_path,
            manifest=manifest,
            blockers=blockers,
            warnings=warnings,
        )
    affected_services = _MODEL_UPDATE_SERVICES if bundle_type == "pretrained_models" else _WINDOWS_UPDATE_SERVICES
    affected_workers = [
        service_name for service_name in affected_services if service_name in _WINDOWS_UPDATE_WORKERS
    ]
    return OfflineUpdateBundleSummary(
        file_name=bundle_path.name,
        path=str(bundle_path),
        size_bytes=stat.st_size,
        modified_at=modified_at,
        bundle_type=bundle_type,
        app_name=app_name,
        app_version=app_version,
        distro_name=_manifest_str(manifest, "DistroName"),
        app_host_name=_manifest_str(manifest, "AppHostName"),
        build_timestamp=_manifest_str(manifest, "BuildTimestamp"),
        apply_token=token,
        affected_services=affected_services,
        affected_workers=affected_workers,
        blockers=blockers,
        warnings=warnings,
    )


def _read_bundle_manifest(bundle_path: Path) -> tuple[dict[str, object], str]:
    with zipfile.ZipFile(bundle_path) as archive:
        names = set(archive.namelist())
        if "windows-bundle-manifest.json" in names:
            return json.loads(archive.read("windows-bundle-manifest.json").decode("utf-8-sig")), "windows_update"
        if "pretrained-models-manifest.json" in names:
            return json.loads(archive.read("pretrained-models-manifest.json").decode("utf-8-sig")), "pretrained_models"
    return {}, "unknown"


def _validate_windows_streaming_bundle(
    *,
    bundle_path: Path,
    manifest: dict[str, object],
    blockers: list[str],
    warnings: list[str],
) -> None:
    media_archive = _manifest_str(manifest, "MediaMtxArchive")
    media_sha = _manifest_str(manifest, "MediaMtxArchiveSha256")
    media_version = _manifest_str(manifest, "MediaMtxVersion")
    media_platform = (_manifest_str(manifest, "MediaMtxHostPlatform") or "linux").lower()
    media_skipped = _manifest_bool(manifest, "MediaMtxSkipped", default=False)
    wsl_networking_mode = (_manifest_str(manifest, "WslNetworkingMode") or "unchanged").lower()
    wsl_hyperv_inbound = (
        _manifest_str(manifest, "WslHyperVFirewallDefaultInboundAction") or "unchanged"
    ).lower()
    gstreamer_bundle = _manifest_str(manifest, "GStreamerDebBundle")
    gstreamer_sha = _manifest_str(manifest, "GStreamerDebBundleSha256")
    gstreamer_skipped = _manifest_bool(manifest, "GStreamerDebBundleSkipped", default=False)
    stream_auth_enabled = _manifest_bool(manifest, "StreamAuthEnabled", default=False)
    for key, default in (
        ("StreamingRtspPort", 8554),
        ("StreamingHlsPort", 8888),
        ("StreamingWebRtcPort", 8889),
        ("StreamingWebRtcIcePort", 8189),
        ("StreamingWebRtcIceUdpPort", 8189),
        ("StreamingWebRtcIceTcpPort", 8189),
        ("StreamingTurnTcpPort", 3478),
    ):
        value = manifest.get(key, default)
        try:
            port = int(value)
        except (TypeError, ValueError):
            blockers.append(f"windows-bundle-manifest.json contains invalid {key}: {value!r}.")
            continue
        if port < 1 or port > 65535:
            blockers.append(f"windows-bundle-manifest.json contains out-of-range {key}: {port}.")
    if media_platform not in {"linux", "windows"}:
        blockers.append(
            f"windows-bundle-manifest.json contains invalid MediaMtxHostPlatform: {media_platform!r}."
        )
    if wsl_networking_mode not in {"mirrored", "nat", "unchanged"}:
        blockers.append(
            f"windows-bundle-manifest.json contains invalid WslNetworkingMode: {wsl_networking_mode!r}."
        )
    if wsl_hyperv_inbound not in {"allow", "block", "unchanged"}:
        blockers.append(
            "windows-bundle-manifest.json contains invalid "
            f"WslHyperVFirewallDefaultInboundAction: {wsl_hyperv_inbound!r}."
        )
    if wsl_networking_mode == "mirrored":
        warnings.append(
            "WSL2 mirrored networking will be applied by the offline updater; WSL is shut down once so the .wslconfig change becomes active."
        )

    if not media_archive:
        if media_skipped:
            warnings.append(
                "MediaMTX was skipped for this bundle; RTSP/WebRTC livestream fanout will not be installed by this update."
            )
            return
        blockers.append(
            "MediaMTX archive is missing from the bundle manifest; livestream RTSP/WebRTC fanout cannot be installed offline."
        )
        return
    if not media_sha:
        blockers.append("MediaMTX archive SHA256 is missing from windows-bundle-manifest.json.")
    if not media_version:
        warnings.append("MediaMTX version is missing from windows-bundle-manifest.json.")
    if media_platform == "windows" and media_archive and not media_archive.lower().endswith(".zip"):
        blockers.append("Windows-hosted MediaMTX bundles must contain a runtime/mediamtx.zip archive.")
    if stream_auth_enabled:
        blockers.append(
            "StreamAuthEnabled=true is not supported by the current offline streaming bundle; leave it false until explicit MediaMTX stream credentials are configured."
        )
    else:
        warnings.append("Stream authorization is disabled by default; RTSP/WebRTC URLs will be LAN-open.")

    if not gstreamer_bundle:
        if gstreamer_skipped:
            warnings.append(
                "GStreamer Debian package bundle was skipped; production H.264 stream publication may be unavailable on offline targets."
            )
        else:
            blockers.append(
                "GStreamer Debian package bundle is missing from the manifest; production stream publishing cannot be installed offline."
            )
    elif not gstreamer_sha:
        blockers.append("GStreamer Debian package bundle SHA256 is missing from windows-bundle-manifest.json.")

    try:
        with zipfile.ZipFile(bundle_path) as archive:
            names = set(archive.namelist())
            normalized_archive = media_archive.replace("\\", "/")
            if normalized_archive not in names:
                blockers.append(f"MediaMTX archive listed in manifest is missing: {media_archive}.")
            normalized_gstreamer_bundle = gstreamer_bundle.replace("\\", "/") if gstreamer_bundle else ""
            if normalized_gstreamer_bundle and normalized_gstreamer_bundle not in names:
                blockers.append(
                    f"GStreamer Debian package bundle listed in manifest is missing: {gstreamer_bundle}."
                )
            if "manifest.sha256" not in names:
                blockers.append("manifest.sha256 is missing from the bundle.")
                return
            manifest_lines = archive.read("manifest.sha256").decode("utf-8-sig").splitlines()
    except Exception as exc:  # noqa: BLE001
        blockers.append(f"MediaMTX bundle validation failed: {exc}")
        return
    checksum_paths = {
        line.split(maxsplit=1)[1].strip().replace("\\", "/")
        for line in manifest_lines
        if line.strip() and len(line.split(maxsplit=1)) == 2
    }
    if media_archive.replace("\\", "/") not in checksum_paths:
        blockers.append(f"manifest.sha256 does not contain MediaMTX archive: {media_archive}.")
    if gstreamer_bundle and gstreamer_bundle.replace("\\", "/") not in checksum_paths:
        blockers.append(
            f"manifest.sha256 does not contain GStreamer Debian package bundle: {gstreamer_bundle}."
        )


def _manifest_bool(manifest: dict[str, object], name: str, *, default: bool) -> bool:
    value = manifest.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _service_plans_for_bundle(bundle: OfflineUpdateBundleSummary | None) -> list[OfflineUpdateServicePlan]:
    if bundle is None:
        return []
    return [
        OfflineUpdateServicePlan(
            service_name=service_name,
            action="restart",
            risk="medium" if bundle.bundle_type == "windows_update" else "low",
            changed_files=[bundle.file_name],
            safe_to_restart_independently=False,
            expected_impact=(
                "Service is restarted by the Windows/WSL offline updater."
                if bundle.bundle_type == "windows_update"
                else "Model artifacts may be refreshed; inference services can reload routes."
            ),
        )
        for service_name in bundle.affected_services
    ]


def _changes_for_bundle(bundle: OfflineUpdateBundleSummary | None) -> list[OfflineUpdateChange]:
    if bundle is None:
        return []
    return [
        OfflineUpdateChange(
            path=bundle.file_name,
            change_type="added",
            category=bundle.bundle_type,
            affected_services=bundle.affected_services,
            restart_required=bool(bundle.affected_services),
            rebuild_required=False,
            migration_required=bundle.bundle_type == "windows_update",
            hot_apply_supported=False,
            reason=f"Incoming offline update bundle {bundle.app_version or bundle.file_name}.",
        )
    ]


def _find_bundle_by_token(
    plan: OfflineUpdatePlanResponse,
    apply_token: str,
) -> OfflineUpdateBundleSummary | None:
    for bundle in plan.incoming_bundles:
        if bundle.apply_token == apply_token:
            return bundle
    return None


def _resolve_windows_app_dir() -> Path | None:
    env_candidates = [
        os.getenv("YBB_WINDOWS_APP_DIR"),
        os.getenv("CV_PLATFORM_WINDOWS_APP_DIR"),
        os.getenv("WINDOWS_APP_DIR_WSL"),
        os.getenv("WINDOWS_APP_DIR"),
    ]
    candidates: list[Path] = []
    for value in env_candidates:
        if not value:
            continue
        candidates.append(_coerce_to_wsl_path(value))
    candidates.extend(
        [
            Path("/mnt/c/YBB_Inferenzserver/ybb-inferenzserver"),
            Path("/mnt/c/YBB_Inferenzserver"),
            Path("/mnt/c/KI/YBB_Inferenzserver/ybb-inferenzserver"),
            Path("/mnt/c/KI/YBB_Inferenzserver"),
        ]
    )
    for candidate in candidates:
        if (candidate / "incoming").is_dir() and (
            (candidate / "updater" / "apply-windows-update.ps1").is_file()
            or (candidate / "update-ybb-inferenzserver.cmd").is_file()
        ):
            return candidate
    return None


def _coerce_to_wsl_path(value: str) -> Path:
    normalized = value.strip().strip('"')
    if len(normalized) >= 3 and normalized[1] == ":" and normalized[2] in {"\\", "/"}:
        drive = normalized[0].lower()
        rest = normalized[3:].replace("\\", "/")
        return Path(f"/mnt/{drive}/{rest}")
    return Path(normalized)


def _start_native_update_unit(
    *,
    app_dir: Path,
    bundle_path: Path,
    bundle: OfflineUpdateBundleSummary,
    operation_id: str,
    scheduled_for: datetime | None,
) -> tuple[str, Path]:
    run_root = Path(os.getenv("CV_PLATFORM__STORAGE__STATE_ROOT", "/srv/cv-platform"))
    operations_dir = run_root / "operations" / "offline-update-runs" / operation_id
    operations_dir.mkdir(parents=True, exist_ok=True)
    script_path, log_path = _write_native_update_script(
        operations_dir=operations_dir,
        app_dir=app_dir,
        bundle_path=bundle_path,
        bundle=bundle,
    )
    unit_name = f"ybb-offline-update-{operation_id.removeprefix('opu_')[:12]}"
    command = [
        "systemd-run",
        "--unit",
        unit_name,
        "--collect",
    ]
    if scheduled_for is not None:
        command.extend(
            [
                "--on-calendar",
                scheduled_for.strftime("%Y-%m-%d %H:%M:%S UTC"),
            ]
        )
    command.extend(["/bin/bash", str(script_path)])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "systemd-run failed.").strip())
    return unit_name, log_path


def _write_native_update_script(
    *,
    operations_dir: Path,
    app_dir: Path,
    bundle_path: Path,
    bundle: OfflineUpdateBundleSummary,
) -> tuple[Path, Path]:
    if not bundle.app_name or not bundle.app_version:
        raise RuntimeError("Bundle AppName/AppVersion is missing.")
    script_path = operations_dir / "run-update.sh"
    log_path = operations_dir / "run-update.log"
    work_dir = operations_dir / "work"
    extract_dir = work_dir / "bundle"
    host_arg = (
        f" --host-name {shlex.quote(bundle.app_host_name)}"
        if bundle.app_host_name
        else ""
    )
    script = f"""#!/usr/bin/env bash
set -Eeuo pipefail
log_path={shlex.quote(str(log_path))}
bundle_zip={shlex.quote(str(bundle_path))}
app_dir={shlex.quote(str(app_dir))}
file_name={shlex.quote(bundle.file_name)}
work_dir={shlex.quote(str(work_dir))}
extract_dir={shlex.quote(str(extract_dir))}
app_name={shlex.quote(bundle.app_name)}
app_version={shlex.quote(bundle.app_version)}

exec >>"$log_path" 2>&1

move_bundle() {{
  local target="$1"
  if [[ ! -f "$bundle_zip" ]]; then
    return 0
  fi
  mkdir -p "$app_dir/$target"
  local destination="$app_dir/$target/$file_name"
  if [[ -e "$destination" ]]; then
    local stem="${{file_name%.zip}}"
    destination="$app_dir/$target/${{stem}}-$(date -u +%Y%m%d-%H%M%S).zip"
  fi
  mv "$bundle_zip" "$destination"
  echo "Bundle moved to $destination"
}}

on_exit() {{
  local code=$?
  if [[ "$code" -ne 0 ]]; then
    echo "Update failed with exit code $code"
    move_bundle failed || true
  fi
  exit "$code"
}}
trap on_exit EXIT

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting YBB offline update"
echo "Bundle: $bundle_zip"
rm -rf "$work_dir"
mkdir -p "$extract_dir"
python3 - "$bundle_zip" "$extract_dir" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    archive.extractall(sys.argv[2])
PY

chmod +x "$extract_dir/wsl/apply-release.sh"
bash "$extract_dir/wsl/apply-release.sh" --bundle "$extract_dir" --version "$app_version" --app-name "$app_name" --mode update{host_arg}
mkdir -p "$app_dir/updater" "$app_dir/incoming" "$app_dir/deployed" "$app_dir/failed"
for updater_file in update-windows-offline.ps1 apply-windows-update.ps1 configure-windows-portproxy.ps1 install-windows-autostart.ps1 export-pretrained-models-update.ps1; do
  if [[ -f "$extract_dir/$updater_file" ]]; then
    cp "$extract_dir/$updater_file" "$app_dir/updater/$updater_file"
    if [[ "$updater_file" == "install-windows-autostart.ps1" ]]; then
      cp "$extract_dir/$updater_file" "$app_dir/$updater_file"
    fi
  fi
done
for ui_file in YBB-Deployment-UI.exe windows-deployment-ui.ps1; do
  if [[ -f "$extract_dir/$ui_file" ]]; then
    cp "$extract_dir/$ui_file" "$app_dir/updater/$ui_file"
    cp "$extract_dir/$ui_file" "$app_dir/$ui_file"
  fi
done
move_bundle deployed
rm -rf "$work_dir"
trap - EXIT
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Update finished successfully"
"""
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o700)
    return script_path, log_path


def _parse_schedule_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _manifest_str(manifest: dict[str, object], key: str) -> str | None:
    value = manifest.get(key)
    return str(value) if value not in {None, ""} else None


def _parse_systemctl_properties(output: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key] = value
    return props


def _parse_optional_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _last_journal_line(unit_name: str) -> str | None:
    result = subprocess.run(
        ["journalctl", "-u", unit_name, "-n", "20", "--no-pager", "-l", "--output", "cat"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in reversed(result.stdout.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:500]
    return None


def _agent_request(method: str, path: str, payload: dict[str, object] | None = None) -> object:
    base_url = os.getenv("OPERATIONS_AGENT_URL", "http://operations-agent:8101").rstrip("/")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            response_body = response.read().decode("utf-8", errors="ignore")
            return json.loads(response_body) if response_body else {}
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="ignore")
        detail = response_body or str(exc)
        raise HTTPException(status_code=exc.code, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Operations agent request failed: {exc}") from exc
