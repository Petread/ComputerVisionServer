from __future__ import annotations

import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated

from cv_contracts import (
    EdgeDeploymentAttemptListResponse,
    EdgeDeploymentAttemptSummary,
    EdgeDeploymentControllerStatus,
    EdgeDeploymentExecuteRequest,
    EdgeDeploymentPreflightReport,
    EdgeDeploymentPreflightRequest,
    EdgeDeploymentWorkflowResponse,
    EdgeDeviceDetail,
    EdgeDeviceListResponse,
    EdgeDeviceReconcileResponse,
    EdgeDeviceRegistrationRequest,
    EdgeDeviceUpdateRequest,
    EdgeDiagnosticReport,
    EdgeDiagnosticRunRequest,
    EdgeFleetHealthOverview,
    EdgeFleetReconcilerStatus,
    EdgeReconcileRequest,
    EdgeResultSyncStatus,
    EdgeRollbackRequest,
    EdgeRollbackResponse,
    EdgeViewerLeaseRequest,
    EdgeViewerLeaseSummary,
    EdgeWorkloadReleaseBuildRequest,
    EdgeWorkloadReleaseDetail,
    EdgeWorkloadReleaseListResponse,
    SessionUser,
)
from cv_contracts.edge import (
    EdgeConnectivityState,
    EdgeFleetCommandSummary,
    EdgeSettingsCommand,
    EdgeSettingsStatus,
    EdgeSoftwareOverview,
    EdgeSoftwareRolloutRequest,
)
from cv_core import PlatformSettings
from cv_db import (
    archive_edge_device,
    create_edge_device,
    create_edge_fleet_command,
    finish_edge_fleet_command,
    get_edge_deployment_attempt,
    get_edge_device_detail,
    get_edge_workload_release,
    list_edge_deployment_attempts,
    list_edge_devices,
    list_edge_workload_releases,
    record_edge_connectivity_failure,
    record_edge_observation,
    release_edge_viewer_lease,
    session_scope,
    start_edge_fleet_command,
    update_edge_device,
    upsert_edge_viewer_lease,
)
from cv_edge import EdgeAgentClient, EdgeClientError, EdgeNetworkPolicy
from cv_status import UserRole
from cv_utils import NotFoundError, PlatformError
from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

from runtime_admin_api.audit import write_runtime_audit_event
from runtime_admin_api.dependencies import build_meta, get_settings, require_permission
from runtime_admin_api.edge_operator_workflows import (
    build_edge_deployment_preflight,
    build_edge_fleet_health_overview,
    execute_edge_deployment_workflow,
    execute_manual_edge_rollback,
    latest_or_current_edge_diagnostic,
    run_edge_diagnostics,
)
from runtime_admin_api.edge_reconciliation import probe_edge_device
from runtime_admin_api.edge_software import (
    catalogue_root,
    import_package,
    queue_rollout,
    software_overview,
)
from runtime_admin_api.edge_workload_builder import build_edge_workload_release

router = APIRouter(tags=["edge-fleet"])

FleetReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "runtime.edge_devices.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.VIEWER,
        )
    ),
]
FleetWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "runtime.edge_devices.write",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
        )
    ),
]
SettingsDep = Annotated[PlatformSettings, Depends(get_settings)]


def _settings_client(settings: PlatformSettings, edge_device_id: str) -> EdgeAgentClient:
    with session_scope(settings) as session:
        device = get_edge_device_detail(session, edge_device_id)
    if device is None or not device.enabled:
        raise _edge_error(
            "edge_settings_unavailable", ValueError("Gerät fehlt oder ist deaktiviert.")
        )
    return EdgeAgentClient(
        address=device.address,
        network_policy=EdgeNetworkPolicy.from_strings(
            allowed_cidrs=[str(device.allowed_cidr)],
            registered_addresses=[str(device.address.management_ip)],
        ),
    )


@router.get("/runtime/edge-devices/{edge_device_id}/settings", response_model=EdgeSettingsStatus)
def get_edge_settings(
    edge_device_id: str, _current_user: FleetReader, settings: SettingsDep
) -> EdgeSettingsStatus:
    try:
        with _settings_client(settings, edge_device_id) as client:
            return client.settings_status()
    except EdgeClientError as exc:
        if exc.status_code == 404:
            return EdgeSettingsStatus(
                supported=False,
                message=(
                    "Die installierte Edge-Runtime unterstützt zentrale Einstellungen noch nicht. "
                    "Edge-Update und aktuellen Offline-Installer ausführen."
                ),
            )
        raise PlatformError(
            code="edge_settings_unreachable",
            message="Edge-Einstellungen sind derzeit nicht erreichbar.",
            detail=str(exc),
            status_code=503,
        ) from exc


@router.put(
    "/runtime/edge-devices/{edge_device_id}/settings",
    response_model=EdgeSettingsStatus,
    status_code=202,
)
def put_edge_settings(
    edge_device_id: str,
    payload: EdgeSettingsCommand,
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeSettingsStatus:
    try:
        with _settings_client(settings, edge_device_id) as client:
            result = client.update_settings(payload)
    except EdgeClientError as exc:
        raise PlatformError(
            code="edge_settings_rejected",
            message="Edge-Einstellungen konnten nicht übernommen werden.",
            detail=str(exc),
            status_code=409 if exc.status_code == 409 else 503,
        ) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="edge.settings.requested.v1",
        aggregate_type="edge_device",
        aggregate_id=edge_device_id,
        payload=payload.model_dump(mode="json"),
    )
    return result


@router.get("/runtime/edge-software", response_model=EdgeSoftwareOverview)
def get_edge_software(_current_user: FleetReader, settings: SettingsDep) -> EdgeSoftwareOverview:
    with session_scope(settings) as session:
        return software_overview(session, settings)


@router.post("/runtime/edge-software/packages", response_model=EdgeSoftwareOverview)
def upload_edge_software(
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
) -> EdgeSoftwareOverview:
    path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=catalogue_root(settings), suffix=".upload", delete=False
        ) as stream:
            path = Path(stream.name)
            total = 0
            while chunk := file.file.read(1024 * 1024):
                total += len(chunk)
                if total > 8 * 1024**3:
                    raise ValueError("Runtime package exceeds 8 GiB.")
                stream.write(chunk)
        descriptor = import_package(settings, path)
    except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile, KeyError) as exc:
        raise _edge_error("edge_software_package_invalid", ValueError(str(exc))) from exc
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="edge.software.package.imported.v1",
        aggregate_type="edge_software",
        aggregate_id=descriptor["package_sha256"],
        payload=descriptor,
    )
    with session_scope(settings) as session:
        return software_overview(session, settings)


@router.post(
    "/runtime/edge-devices/{edge_device_id}/software",
    response_model=EdgeFleetCommandSummary,
    status_code=202,
)
def update_edge_software(
    edge_device_id: str,
    payload: EdgeSoftwareRolloutRequest,
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeFleetCommandSummary:
    try:
        with session_scope(settings) as session:
            command = queue_rollout(session, settings, edge_device_id, payload)
    except ValueError as exc:
        raise _edge_error("edge_software_update_invalid", exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="edge.software.command.queued.v1",
        aggregate_type="edge_device",
        aggregate_id=edge_device_id,
        payload=command.model_dump(mode="json"),
    )
    return command


@router.post(
    "/runtime/edge-operator/deployments:preflight",
    response_model=EdgeDeploymentPreflightReport,
)
def preflight_edge_deployment(
    payload: EdgeDeploymentPreflightRequest,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeDeploymentPreflightReport:
    try:
        with session_scope(settings) as session:
            return build_edge_deployment_preflight(
                session,
                settings=settings,
                payload=payload,
                created_by=current_user.username,
            )
    except ValueError as exc:
        raise _edge_error("edge_deployment_preflight_invalid", exc) from exc


@router.post(
    "/runtime/edge-operator/deployments",
    response_model=EdgeDeploymentWorkflowResponse,
    status_code=202,
)
def execute_edge_deployment(
    payload: EdgeDeploymentExecuteRequest,
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeDeploymentWorkflowResponse:
    try:
        with session_scope(settings) as session:
            response = execute_edge_deployment_workflow(
                session,
                settings=settings,
                payload=payload,
                requested_by=current_user.username,
            )
    except ValueError as exc:
        raise _edge_error("edge_deployment_workflow_invalid", exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="edge.operator.deployment.queued.v1",
        aggregate_type="runtime_channel",
        aggregate_id=response.channel_id,
        payload=response.model_dump(mode="json"),
    )
    return response


@router.post(
    "/runtime/edge-deployments/{edge_deployment_id}:rollback",
    response_model=EdgeRollbackResponse,
)
def rollback_edge_deployment(
    edge_deployment_id: str,
    payload: EdgeRollbackRequest,
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeRollbackResponse:
    try:
        response = execute_manual_edge_rollback(
            settings,
            edge_deployment_id=edge_deployment_id,
            payload=payload,
            requested_by=current_user.username,
        )
    except (ValueError, RuntimeError, EdgeClientError) as exc:
        raise _edge_error("edge_deployment_rollback_invalid", ValueError(str(exc))) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="edge.operator.deployment.rolled-back.v1",
        aggregate_type="edge_deployment",
        aggregate_id=edge_deployment_id,
        payload=response.model_dump(mode="json"),
    )
    return response


@router.post(
    "/runtime/edge-devices/{edge_device_id}/diagnostics",
    response_model=EdgeDiagnosticReport,
)
def run_device_diagnostics(
    edge_device_id: str,
    payload: EdgeDiagnosticRunRequest,
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeDiagnosticReport:
    if payload.refresh_device:
        with session_scope(settings) as session:
            device = get_edge_device_detail(session, edge_device_id)
        if device is None:
            raise _edge_error(
                "edge_diagnostic_target_missing",
                ValueError("Edge device does not exist."),
            )
        try:
            observation = probe_edge_device(device)
        except (EdgeClientError, ValueError) as exc:
            with session_scope(settings) as session:
                record_edge_connectivity_failure(session, edge_device_id, message=str(exc))
        else:
            with session_scope(settings) as session:
                record_edge_observation(
                    session,
                    edge_device_id,
                    identity=observation.identity,
                    health=observation.health,
                    capabilities=observation.capabilities,
                )
    try:
        with session_scope(settings) as session:
            report = run_edge_diagnostics(
                session,
                edge_device_id=edge_device_id,
                created_by=current_user.username,
            )
    except ValueError as exc:
        raise _edge_error("edge_diagnostic_invalid", exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="edge.operator.diagnostic.completed.v1",
        aggregate_type="edge_device",
        aggregate_id=edge_device_id,
        payload={"report_id": report.diagnostic_report_id, "status": report.status},
    )
    return report


@router.get(
    "/runtime/edge-devices/{edge_device_id}/diagnostics/latest",
    response_model=EdgeDiagnosticReport,
)
def get_latest_device_diagnostics(
    edge_device_id: str,
    _current_user: FleetReader,
    settings: SettingsDep,
) -> EdgeDiagnosticReport:
    try:
        with session_scope(settings) as session:
            return latest_or_current_edge_diagnostic(
                session,
                edge_device_id=edge_device_id,
            )
    except ValueError as exc:
        raise _edge_error("edge_diagnostic_invalid", exc) from exc


@router.get("/runtime/edge-fleet/health", response_model=EdgeFleetHealthOverview)
def get_edge_fleet_health(
    _current_user: FleetReader,
    settings: SettingsDep,
) -> EdgeFleetHealthOverview:
    with session_scope(settings) as session:
        return build_edge_fleet_health_overview(session)


@router.get("/runtime/edge-result-sync", response_model=EdgeResultSyncStatus)
def get_edge_result_sync_status(
    request: Request,
    _current_user: FleetReader,
) -> EdgeResultSyncStatus:
    synchronizer = getattr(request.app.state, "edge_result_synchronizer", None)
    if synchronizer is None:
        return EdgeResultSyncStatus(running=False, interval_seconds=1.0, batch_limit=100)
    return synchronizer.status()


@router.post(
    "/runtime/channels/{channel_id}/edge-viewer-leases",
    response_model=EdgeViewerLeaseSummary,
)
def acquire_edge_viewer_lease(
    channel_id: str,
    payload: EdgeViewerLeaseRequest,
    _current_user: FleetReader,
    settings: SettingsDep,
) -> EdgeViewerLeaseSummary:
    try:
        with session_scope(settings) as session:
            return upsert_edge_viewer_lease(session, channel_id=channel_id, payload=payload)
    except ValueError as exc:
        raise _edge_error("edge_viewer_lease_invalid", exc) from exc


@router.delete(
    "/runtime/channels/{channel_id}/edge-viewer-leases/{lease_id}",
    response_model=EdgeViewerLeaseSummary,
)
def release_channel_edge_viewer_lease(
    channel_id: str,
    lease_id: str,
    _current_user: FleetReader,
    settings: SettingsDep,
) -> EdgeViewerLeaseSummary:
    try:
        with session_scope(settings) as session:
            return release_edge_viewer_lease(
                session,
                channel_id=channel_id,
                lease_id=lease_id,
            )
    except ValueError as exc:
        raise _edge_error("edge_viewer_lease_invalid", exc) from exc


@router.get(
    "/runtime/edge-deployment-controller",
    response_model=EdgeDeploymentControllerStatus,
)
def get_edge_deployment_controller_status(
    request: Request,
    _current_user: FleetReader,
) -> EdgeDeploymentControllerStatus:
    controller = getattr(request.app.state, "edge_deployment_controller", None)
    if controller is None:
        return EdgeDeploymentControllerStatus(
            running=False,
            worker_id="not-started",
            interval_seconds=1.0,
            processed_commands=0,
            failed_commands=0,
        )
    return controller.status()


@router.get(
    "/runtime/edge-workload-releases",
    response_model=EdgeWorkloadReleaseListResponse,
)
def get_edge_workload_releases(
    _current_user: FleetReader,
    settings: SettingsDep,
    channel_id: str | None = Query(default=None, min_length=3, max_length=64),
    edge_device_id: str | None = Query(default=None, min_length=3, max_length=64),
) -> EdgeWorkloadReleaseListResponse:
    with session_scope(settings) as session:
        items = list_edge_workload_releases(
            session, channel_id=channel_id, edge_device_id=edge_device_id
        )
    return EdgeWorkloadReleaseListResponse(items=items, meta=build_meta())


@router.post(
    "/runtime/edge-workload-releases",
    response_model=EdgeWorkloadReleaseDetail,
    status_code=201,
)
def create_edge_workload_release(
    payload: EdgeWorkloadReleaseBuildRequest,
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeWorkloadReleaseDetail:
    try:
        with session_scope(settings) as session:
            item = build_edge_workload_release(
                session,
                settings=settings,
                channel_id=payload.channel_id,
                created_by=current_user.username,
                precision=payload.precision,
            )
    except ValueError as exc:
        raise _edge_error("edge_workload_build_invalid", exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="edge.workload.release.built.v1",
        aggregate_type="edge_workload_release",
        aggregate_id=item.workload_release_id,
        payload={
            "channel_id": item.channel_id,
            "edge_device_id": item.edge_device_id,
            "release_digest": item.release_digest,
            "admission_status": item.admission_report.status,
            "idempotency_key": payload.idempotency_key,
        },
    )
    return item


@router.get(
    "/runtime/edge-workload-releases/{workload_release_id}",
    response_model=EdgeWorkloadReleaseDetail,
)
def get_edge_workload_release_detail(
    workload_release_id: str,
    _current_user: FleetReader,
    settings: SettingsDep,
) -> EdgeWorkloadReleaseDetail:
    with session_scope(settings) as session:
        item = get_edge_workload_release(session, workload_release_id)
    if item is None:
        raise NotFoundError(
            message="Edge workload release not found",
            detail=f"No Edge workload release exists for id {workload_release_id}.",
        )
    return item


@router.get(
    "/runtime/edge-deployments",
    response_model=EdgeDeploymentAttemptListResponse,
)
def get_edge_deployments(
    _current_user: FleetReader,
    settings: SettingsDep,
    channel_id: str | None = Query(default=None, min_length=3, max_length=64),
    edge_device_id: str | None = Query(default=None, min_length=3, max_length=64),
) -> EdgeDeploymentAttemptListResponse:
    with session_scope(settings) as session:
        items = list_edge_deployment_attempts(
            session, channel_id=channel_id, edge_device_id=edge_device_id
        )
    return EdgeDeploymentAttemptListResponse(items=items, meta=build_meta())


@router.get(
    "/runtime/edge-deployments/{edge_deployment_id}",
    response_model=EdgeDeploymentAttemptSummary,
)
def get_edge_deployment_detail(
    edge_deployment_id: str,
    _current_user: FleetReader,
    settings: SettingsDep,
) -> EdgeDeploymentAttemptSummary:
    with session_scope(settings) as session:
        item = get_edge_deployment_attempt(session, edge_deployment_id)
    if item is None:
        raise NotFoundError(
            message="Edge deployment not found",
            detail=f"No Edge deployment exists for id {edge_deployment_id}.",
        )
    return item


@router.get("/runtime/edge-reconciler", response_model=EdgeFleetReconcilerStatus)
def get_edge_reconciler_status(
    request: Request,
    _current_user: FleetReader,
) -> EdgeFleetReconcilerStatus:
    reconciler = getattr(request.app.state, "edge_fleet_reconciler", None)
    if reconciler is None:
        return EdgeFleetReconcilerStatus(running=False, interval_seconds=15.0)
    return reconciler.status()


@router.get("/runtime/edge-devices", response_model=EdgeDeviceListResponse)
def get_edge_devices(
    _current_user: FleetReader,
    settings: SettingsDep,
    search: str | None = Query(default=None, min_length=1, max_length=160),
    connectivity_state: EdgeConnectivityState | None = None,
    enabled: bool | None = None,
) -> EdgeDeviceListResponse:
    with session_scope(settings) as session:
        items = list_edge_devices(
            session,
            search=search,
            connectivity_state=connectivity_state,
            enabled=enabled,
        )
    return EdgeDeviceListResponse(items=items, meta=build_meta())


@router.post("/runtime/edge-devices", response_model=EdgeDeviceDetail, status_code=201)
def register_edge_device(
    payload: EdgeDeviceRegistrationRequest,
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeDeviceDetail:
    with session_scope(settings) as session:
        try:
            item = create_edge_device(session, payload, created_by=current_user.username)
        except ValueError as exc:
            raise _edge_error("edge_device_registration_invalid", exc) from exc
    _audit(
        settings,
        request,
        current_user,
        event_type="edge.device.registered.v1",
        item=item,
    )
    return item


@router.get("/runtime/edge-devices/{edge_device_id}", response_model=EdgeDeviceDetail)
def get_edge_device(
    edge_device_id: str,
    _current_user: FleetReader,
    settings: SettingsDep,
) -> EdgeDeviceDetail:
    with session_scope(settings) as session:
        item = get_edge_device_detail(session, edge_device_id)
    if item is None:
        raise NotFoundError(
            message="Edge device not found",
            detail=f"No edge device exists for id {edge_device_id}.",
        )
    return item


@router.put("/runtime/edge-devices/{edge_device_id}", response_model=EdgeDeviceDetail)
def edit_edge_device(
    edge_device_id: str,
    payload: EdgeDeviceUpdateRequest,
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeDeviceDetail:
    with session_scope(settings) as session:
        try:
            item = update_edge_device(
                session,
                edge_device_id,
                payload,
                updated_by=current_user.username,
            )
        except ValueError as exc:
            raise _edge_error("edge_device_update_invalid", exc) from exc
    _audit(
        settings,
        request,
        current_user,
        event_type="edge.device.updated.v1",
        item=item,
    )
    return item


@router.delete("/runtime/edge-devices/{edge_device_id}", response_model=EdgeDeviceDetail)
def delete_edge_device(
    edge_device_id: str,
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeDeviceDetail:
    with session_scope(settings) as session:
        try:
            item = archive_edge_device(
                session,
                edge_device_id,
                archived_by=current_user.username,
            )
        except ValueError as exc:
            raise _edge_error("edge_device_archive_invalid", exc) from exc
    _audit(
        settings,
        request,
        current_user,
        event_type="edge.device.archived.v1",
        item=item,
    )
    return item


@router.post(
    "/runtime/edge-devices/{edge_device_id}:reconcile",
    response_model=EdgeDeviceReconcileResponse,
)
def reconcile_edge_device(
    edge_device_id: str,
    payload: EdgeReconcileRequest,
    request: Request,
    current_user: FleetWriter,
    settings: SettingsDep,
) -> EdgeDeviceReconcileResponse:
    with session_scope(settings) as session:
        try:
            command = create_edge_fleet_command(
                session,
                edge_device_id,
                command_type="reconcile",
                idempotency_key=payload.idempotency_key,
                request_payload={"edge_device_id": edge_device_id},
            )
            device = get_edge_device_detail(session, edge_device_id)
        except ValueError as exc:
            raise _edge_error("edge_device_reconcile_invalid", exc) from exc
    assert device is not None
    if command.status in {"succeeded", "failed"}:
        return EdgeDeviceReconcileResponse(device=device, command=command, meta=build_meta())

    with session_scope(settings) as session:
        start_edge_fleet_command(session, command.command_id)

    try:
        observation = probe_edge_device(device)
    except (EdgeClientError, ValueError) as exc:
        with session_scope(settings) as session:
            device = record_edge_connectivity_failure(
                session,
                edge_device_id,
                message=str(exc),
            )
            command = finish_edge_fleet_command(
                session,
                command.command_id,
                succeeded=False,
                last_error=str(exc),
            )
    else:
        with session_scope(settings) as session:
            device = record_edge_observation(
                session,
                edge_device_id,
                identity=observation.identity,
                health=observation.health,
                capabilities=observation.capabilities,
            )
            command = finish_edge_fleet_command(
                session,
                command.command_id,
                succeeded=True,
                response_payload={
                    "boot_id": observation.identity.boot_id,
                    "platform": observation.identity.platform,
                    "health_state": observation.health.state,
                    **observation.software_observation(),
                },
            )

    _audit(
        settings,
        request,
        current_user,
        event_type="edge.device.reconciled.v1",
        item=device,
        payload={"command_id": command.command_id, "status": command.status},
    )
    return EdgeDeviceReconcileResponse(device=device, command=command, meta=build_meta())


def _edge_error(code: str, exc: ValueError) -> PlatformError:
    return PlatformError(
        code=code,
        message="The edge fleet operation could not be completed.",
        detail=str(exc),
        status_code=422,
    )


def _audit(
    settings: PlatformSettings,
    request: Request,
    current_user: SessionUser,
    *,
    event_type: str,
    item: EdgeDeviceDetail,
    payload: dict[str, object] | None = None,
) -> None:
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type=event_type,
        aggregate_type="edge_device",
        aggregate_id=item.edge_device_id,
        payload={
            "management_ip": str(item.address.management_ip),
            "name": item.name,
            **(payload or {}),
        },
    )
