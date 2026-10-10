from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Callable
from typing import Any, Literal

from cv_contracts import (
    EdgeChannelDiagnostic,
    EdgeDeploymentExecuteRequest,
    EdgeDeploymentPreflightReport,
    EdgeDeploymentPreflightRequest,
    EdgeDeploymentWorkflowResponse,
    EdgeDeviceDetail,
    EdgeDiagnosticReport,
    EdgeFleetAlert,
    EdgeFleetHealthOverview,
    EdgeOperatorCheck,
    EdgeRollbackRequest,
    EdgeRollbackResponse,
    EdgeWorkloadReleaseDetail,
)
from cv_core import PlatformSettings
from cv_db import (
    complete_manual_edge_rollback,
    create_edge_fleet_command,
    finish_edge_fleet_command,
    get_edge_deployment_attempt,
    get_edge_device_detail,
    get_edge_workload_release,
    latest_edge_diagnostic_report,
    list_edge_devices,
    new_diagnostic_report_id,
    request_runtime_channel_restart,
    request_runtime_channel_start,
    save_edge_diagnostic_report,
    session_scope,
    start_edge_fleet_command,
    validate_manual_rollback_target,
)
from cv_db.models import (
    EdgeDeploymentAttemptRecord,
    EdgeResultCursorRecord,
    ResultRecord,
    RuntimeChannel,
)
from cv_edge import EdgeAgentClient, EdgeNetworkPolicy
from cv_utils import utc_now_iso
from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime_admin_api.edge_workload_builder import build_edge_workload_release

ClientFactory = Callable[[EdgeDeviceDetail], EdgeAgentClient]


def build_edge_deployment_preflight(
    session: Session,
    *,
    settings: PlatformSettings,
    payload: EdgeDeploymentPreflightRequest,
    created_by: str | None,
) -> EdgeDeploymentPreflightReport:
    release = build_edge_workload_release(
        session,
        settings=settings,
        channel_id=payload.channel_id,
        created_by=created_by,
        precision=payload.precision,
    )
    return _preflight_for_release(session, release, precision=payload.precision)


def execute_edge_deployment_workflow(
    session: Session,
    *,
    settings: PlatformSettings,
    payload: EdgeDeploymentExecuteRequest,
    requested_by: str | None,
) -> EdgeDeploymentWorkflowResponse:
    release = get_edge_workload_release(session, payload.workload_release_id)
    if release is None or release.channel_id != payload.channel_id:
        raise ValueError("The selected workload release does not belong to the channel.")
    preflight = _preflight_for_release(
        session,
        release,
        precision=_release_precision(release),
    )
    if not preflight.ready:
        failures = [
            f"{item.key}: {item.message}"
            for item in preflight.checks
            if item.outcome == "fail"
        ]
        raise ValueError("Deployment preflight failed: " + "; ".join(failures))
    channel = session.get(RuntimeChannel, payload.channel_id)
    if channel is None:
        raise ValueError("Runtime channel does not exist.")
    command_payload = {
        "operator_workflow": "m5",
        "expected_edge_workload_release_id": release.workload_release_id,
        "expected_release_digest": release.release_digest,
        "preflight_generated_at": preflight.generated_at,
    }
    reason = payload.reason or "M5 deployment wizard"
    if channel.observed_state in {"running", "degraded"} or channel.edge_deployment_id:
        _channel, command = request_runtime_channel_restart(
            session,
            payload.channel_id,
            requested_by=requested_by,
            reason=reason,
            idempotency_key=payload.idempotency_key,
            source_secret_root=settings.storage.runtime_root() / "source-secrets",
            command_payload=command_payload,
        )
        command_name = "restart"
    else:
        _channel, command = request_runtime_channel_start(
            session,
            payload.channel_id,
            requested_by=requested_by,
            reason=reason,
            idempotency_key=payload.idempotency_key,
            source_secret_root=settings.storage.runtime_root() / "source-secrets",
            command_payload=command_payload,
        )
        command_name = "start"
    return EdgeDeploymentWorkflowResponse(
        channel_id=payload.channel_id,
        edge_device_id=release.edge_device_id,
        workload_release_id=release.workload_release_id,
        command_id=command.command_id,
        command=command_name,
        status=command.status.value,
        accepted_at=command.accepted_at,
        preflight=preflight,
    )


def run_edge_diagnostics(
    session: Session,
    *,
    edge_device_id: str,
    created_by: str | None,
    persist: bool = True,
) -> EdgeDiagnosticReport:
    device = get_edge_device_detail(session, edge_device_id)
    if device is None:
        raise ValueError("Edge device does not exist.")
    checks = _device_checks(device)
    result_lag = sum(
        max(0, row.last_persisted_sequence - row.last_acknowledged_sequence)
        for row in session.scalars(
            select(EdgeResultCursorRecord).where(
                EdgeResultCursorRecord.edge_device_id == edge_device_id
            )
        ).all()
    )
    checks.append(
        EdgeOperatorCheck(
            key="results.ack_lag",
            category="results",
            outcome="fail" if result_lag > 1000 else "warning" if result_lag else "pass",
            message=(
                f"{result_lag} persisted result sequence(s) are awaiting acknowledgement."
                if result_lag
                else "All persisted Edge result sequences are acknowledged."
            ),
            remediation_hint=(
                "Check the result synchronizer and Edge reachability."
                if result_lag
                else None
            ),
            detail={"result_lag": result_lag},
        )
    )
    channels: list[EdgeChannelDiagnostic] = []
    channel_rows = session.scalars(
        select(RuntimeChannel)
        .where(
            RuntimeChannel.edge_device_id == edge_device_id,
            RuntimeChannel.deleted_at.is_(None),
        )
        .order_by(RuntimeChannel.name)
    ).all()
    for channel in channel_rows:
        diagnostic, channel_checks = _channel_diagnostic(session, channel, result_lag=result_lag)
        channels.append(diagnostic)
        checks.extend(channel_checks)
    status = _diagnostic_status(checks)
    report = EdgeDiagnosticReport(
        diagnostic_report_id=new_diagnostic_report_id(),
        edge_device_id=edge_device_id,
        edge_device_name=device.name,
        status=status,
        generated_at=utc_now_iso(),
        created_by=created_by,
        checks=checks,
        channels=channels,
    )
    if persist:
        save_edge_diagnostic_report(session, report)
    return report


def latest_or_current_edge_diagnostic(
    session: Session,
    *,
    edge_device_id: str,
) -> EdgeDiagnosticReport:
    existing = latest_edge_diagnostic_report(session, edge_device_id)
    return existing or run_edge_diagnostics(
        session,
        edge_device_id=edge_device_id,
        created_by=None,
        persist=False,
    )


def build_edge_fleet_health_overview(session: Session) -> EdgeFleetHealthOverview:
    reports = [
        run_edge_diagnostics(
            session,
            edge_device_id=device.edge_device_id,
            created_by=None,
            persist=False,
        )
        for device in list_edge_devices(session, enabled=True)
    ]
    generated_at = utc_now_iso()
    alerts: list[EdgeFleetAlert] = []
    for report in reports:
        for check in report.checks:
            if check.outcome == "pass":
                continue
            channel_id = str(check.detail.get("channel_id") or "").strip() or None
            alert_digest = hashlib.sha256(
                f"{report.edge_device_id}:{channel_id}:{check.key}".encode()
            ).hexdigest()
            alerts.append(
                EdgeFleetAlert(
                    alert_id=f"ealt_{alert_digest[:24]}",
                    severity="critical" if check.outcome == "fail" else "warning",
                    edge_device_id=report.edge_device_id,
                    channel_id=channel_id,
                    check_key=check.key,
                    title=check.key.replace(".", " ").title(),
                    detail=check.message,
                    remediation_hint=check.remediation_hint,
                    observed_at=report.generated_at,
                )
            )
    return EdgeFleetHealthOverview(
        generated_at=generated_at,
        total_devices=len(reports),
        healthy_devices=sum(item.status == "healthy" for item in reports),
        degraded_devices=sum(item.status == "degraded" for item in reports),
        critical_devices=sum(item.status == "critical" for item in reports),
        evaluated_channels=sum(len(item.channels) for item in reports),
        alerts=sorted(alerts, key=lambda item: (item.severity != "critical", item.alert_id)),
        reports=reports,
    )


def execute_manual_edge_rollback(
    settings: PlatformSettings,
    *,
    edge_deployment_id: str,
    payload: EdgeRollbackRequest,
    requested_by: str | None,
    client_factory: ClientFactory | None = None,
) -> EdgeRollbackResponse:
    factory = client_factory or _edge_client
    with session_scope(settings) as session:
        active_record = session.get(EdgeDeploymentAttemptRecord, edge_deployment_id)
        if active_record is None or not active_record.previous_edge_deployment_id:
            raise ValueError("No previous A/B deployment is available for rollback.")
        previous_record = session.get(
            EdgeDeploymentAttemptRecord,
            active_record.previous_edge_deployment_id,
        )
        if previous_record is None:
            raise ValueError("The previous A/B deployment no longer exists.")
        channel = session.get(RuntimeChannel, active_record.channel_id)
        if channel is None:
            raise ValueError("Rollback channel does not exist.")
        device = get_edge_device_detail(session, active_record.edge_device_id)
        if device is None:
            raise ValueError("Rollback target device does not exist.")
        command = create_edge_fleet_command(
            session,
            active_record.edge_device_id,
            command_type="deployment_rollback",
            idempotency_key=payload.idempotency_key,
            request_payload={
                "edge_deployment_id": active_record.id,
                "restore_edge_deployment_id": previous_record.id,
                "channel_id": channel.id,
                "reason": payload.reason,
                "requested_by": requested_by,
            },
        )
        if command.status == "succeeded":
            rolled_back = get_edge_deployment_attempt(session, active_record.id)
            restored = get_edge_deployment_attempt(session, previous_record.id)
            if rolled_back is None or restored is None:
                raise ValueError("Completed rollback history is incomplete.")
            return EdgeRollbackResponse(
                command=command,
                rolled_back_deployment=rolled_back,
                restored_deployment=restored,
                completed_at=command.finished_at or utc_now_iso(),
            )
        _channel, _active, previous = validate_manual_rollback_target(
            session,
            edge_deployment_id,
        )
        command = start_edge_fleet_command(session, command.command_id)
        command_id = command.command_id

    try:
        with factory(device) as client:
            remote = client.rollback_deployment(
                edge_deployment_id,
                idempotency_key=f"operator-rollback:{command_id}",
            )
            if remote.status != "rolled_back":
                raise RuntimeError("Edge did not confirm the requested rollback.")
            restored_remote = client.deployment(previous.id)
            if restored_remote.status != "running":
                raise RuntimeError("Edge did not restore the previous standby deployment.")
        with session_scope(settings) as session:
            rolled_back, restored = complete_manual_edge_rollback(
                session,
                edge_deployment_id=edge_deployment_id,
            )
            command = finish_edge_fleet_command(
                session,
                command_id,
                succeeded=True,
                response_payload={
                    "rolled_back_deployment_id": rolled_back.edge_deployment_id,
                    "restored_deployment_id": restored.edge_deployment_id,
                },
            )
        return EdgeRollbackResponse(
            command=command,
            rolled_back_deployment=rolled_back,
            restored_deployment=restored,
            completed_at=command.finished_at or utc_now_iso(),
        )
    except Exception as exc:
        with session_scope(settings) as session:
            finish_edge_fleet_command(
                session,
                command_id,
                succeeded=False,
                last_error=str(exc),
            )
        raise


def _preflight_for_release(
    session: Session,
    release: EdgeWorkloadReleaseDetail,
    *,
    precision: Literal["fp32", "fp16", "int8"],
) -> EdgeDeploymentPreflightReport:
    channel = session.get(RuntimeChannel, release.channel_id)
    device = get_edge_device_detail(session, release.edge_device_id)
    if channel is None or device is None:
        raise ValueError("Deployment preflight lost its channel or Edge device.")
    checks = [
        EdgeOperatorCheck(
            key=f"admission.{item.key}"[:120],
            category=(
                "capability"
                if "capab" in item.key or "platform" in item.key
                else "deployment"
            ),
            outcome=item.outcome,
            message=item.message,
            remediation_hint=(
                "In Edge-Orchestrierung ein signiertes Runtime-Paket mit dem Codestand "
                "der Zentrale importieren und dieses Gerät aktualisieren. Danach Status "
                "abrufen und den Kanal-Preflight erneut ausführen."
                if item.outcome == "fail" and item.key == "runtime.code"
                else "Resolve the failed admission check and run preflight again."
                if item.outcome == "fail"
                else None
            ),
            detail=item.detail,
        )
        for item in release.admission_report.checks
    ]
    checks.extend(_workflow_checks(channel, device, release))
    rollback_id: str | None = None
    if channel.edge_deployment_id:
        current = session.get(EdgeDeploymentAttemptRecord, channel.edge_deployment_id)
        if current is not None and current.previous_edge_deployment_id:
            previous = session.get(
                EdgeDeploymentAttemptRecord,
                current.previous_edge_deployment_id,
            )
            if previous is not None and previous.status == "standby":
                rollback_id = previous.id
    return EdgeDeploymentPreflightReport(
        ready=not any(item.outcome == "fail" for item in checks),
        channel_id=channel.id,
        channel_name=channel.name,
        edge_device_id=device.edge_device_id,
        edge_device_name=device.name,
        management_ip=device.address.management_ip,
        workload_release_id=release.workload_release_id,
        release_digest=release.release_digest,
        precision=precision,
        artifact_count=release.artifact_count,
        total_artifact_bytes=release.total_artifact_bytes,
        current_deployment_id=channel.edge_deployment_id,
        rollback_deployment_id=rollback_id,
        checks=checks,
        generated_at=utc_now_iso(),
    )


def _workflow_checks(
    channel: RuntimeChannel,
    device: EdgeDeviceDetail,
    release: EdgeWorkloadReleaseDetail,
) -> list[EdgeOperatorCheck]:
    placement_valid = (
        channel.execution_target == "edge_device"
        and channel.edge_device_id == release.edge_device_id
        and channel.pipeline_version_id == release.pipeline_version_id
    )
    workflow_idle = (
        channel.desired_state not in {"start_requested", "stop_requested"}
        and channel.observed_state not in {"starting", "stopping"}
    )
    capability_current = (
        device.latest_capability_snapshot is not None
        and device.latest_capability_snapshot.digest == release.capability_digest
    )
    return [
        _check(
            "placement.binding",
            "placement",
            placement_valid,
            "Channel, pipeline and fixed-IP target match the release.",
            "Re-run placement and release preflight after editing the channel.",
        ),
        _check(
            "connectivity.online",
            "connectivity",
            device.enabled and device.connectivity_state == "online",
            "The Edge device is enabled and online.",
            "Reconcile the device and restore VLAN connectivity.",
        ),
        _check(
            "capability.current",
            "capability",
            capability_current,
            (
                "The release matches the latest capability snapshot."
                if capability_current
                else "The Edge capability fingerprint changed after this release was built."
            ),
            "Reconcile capabilities and build a new release.",
        ),
        _check(
            "deployment.workflow_idle",
            "deployment",
            workflow_idle,
            "No conflicting channel control command is active.",
            "Wait for the current start/stop operation to finish.",
        ),
    ]


def _device_checks(device: EdgeDeviceDetail) -> list[EdgeOperatorCheck]:
    capability_available = device.latest_capability_snapshot is not None
    revalidation_pending = capability_available and device.deployment_revalidation_required
    checks = [
        _check(
            "connectivity.management",
            "connectivity",
            device.enabled and device.connectivity_state == "online",
            f"Management connectivity is {device.connectivity_state}.",
            "Check fixed IP, VLAN ACL and the Edge service on TCP 9443.",
        ),
        _check(
            "capability.snapshot",
            "capability",
            capability_available and not revalidation_pending,
            (
                "Device identity changed; deployment revalidation is pending."
                if revalidation_pending
                else "A current capability snapshot is available."
                if capability_available
                else "No capability snapshot is available."
            ),
            (
                "Run device reconciliation before the next deployment."
                if not capability_available
                else "Run an admitted deployment; a successful commit clears revalidation."
            ),
            warning=revalidation_pending,
        ),
    ]
    runtime = device.health.runtime if device.health is not None else None
    storage = runtime.storage if runtime is not None else None
    checks.append(
        _check(
            "storage.pressure",
            "storage",
            storage is not None and not storage.pressure,
            "Edge artifact, outbox and free-space quotas are within limits.",
            "Free Edge storage or increase the explicitly configured quota.",
            warning=storage is None,
        )
    )
    return checks


def _channel_diagnostic(
    session: Session,
    channel: RuntimeChannel,
    *,
    result_lag: int,
) -> tuple[EdgeChannelDiagnostic, list[EdgeOperatorCheck]]:
    timings = [
        _latency_ms(row.timing)
        for row in session.scalars(
            select(ResultRecord)
            .where(ResultRecord.channel_id == channel.id)
            .order_by(ResultRecord.completed_at.desc())
            .limit(100)
        ).all()
    ]
    measured = sorted(item for item in timings if item is not None)
    p95 = measured[max(0, math.ceil(len(measured) * 0.95) - 1)] if measured else None
    slo = 500.0 if channel.mode == "trigger" else 100.0
    running_expected = channel.desired_state in {"running", "start_requested"}
    runtime_ok = not running_expected or channel.observed_state in {"running", "degraded"}
    latency_ok = p95 is None or p95 <= slo
    channel_status = (
        "critical"
        if not runtime_ok or (p95 is not None and p95 > slo * 2)
        else "degraded"
        if not latency_ok or p95 is None or result_lag
        else "healthy"
    )
    detail = {"channel_id": channel.id, "observed_state": channel.observed_state}
    checks = [
        _check(
            f"runtime.channel.{channel.id}"[:120],
            "runtime",
            runtime_ok,
            (
                f"Channel {channel.name} is {channel.observed_state} "
                f"(desired {channel.desired_state})."
            ),
            "Inspect the deployment attempt and Edge supervisor diagnostics.",
            detail=detail,
        ),
        EdgeOperatorCheck(
            key=f"slo.channel.{channel.id}"[:120],
            category="slo",
            outcome=(
                "pass"
                if latency_ok and p95 is not None
                else "fail"
                if not latency_ok
                else "warning"
            ),
            message=(
                f"P95 latency is {p95:.1f} ms against a {slo:.0f} ms target."
                if p95 is not None
                else f"No latency sample is available for the {slo:.0f} ms target."
            ),
            remediation_hint=(
                "Inspect frame sampling, model precision and device utilization."
                if not latency_ok
                else "Run the channel to collect latency evidence."
                if p95 is None
                else None
            ),
            detail={**detail, "p95_ms": p95, "target_ms": slo},
        ),
    ]
    return (
        EdgeChannelDiagnostic(
            channel_id=channel.id,
            channel_name=channel.name,
            mode=channel.mode,
            desired_state=channel.desired_state,
            observed_state=channel.observed_state,
            deployment_id=channel.edge_deployment_id,
            workload_release_id=channel.edge_workload_release_id,
            result_lag=result_lag,
            latency_p95_ms=p95,
            latency_slo_ms=slo,
            status=channel_status,
        ),
        checks,
    )


def _check(
    key: str,
    category: Literal[
        "placement",
        "connectivity",
        "capability",
        "deployment",
        "runtime",
        "storage",
        "results",
        "slo",
    ],
    passed: bool,
    message: str,
    remediation: str,
    *,
    warning: bool = False,
    detail: dict[str, Any] | None = None,
) -> EdgeOperatorCheck:
    return EdgeOperatorCheck(
        key=key,
        category=category,
        outcome="pass" if passed else "warning" if warning else "fail",
        message=message,
        remediation_hint=None if passed else remediation,
        detail=detail or {},
    )


def _diagnostic_status(
    checks: list[EdgeOperatorCheck],
) -> Literal["healthy", "degraded", "critical"]:
    if any(item.outcome == "fail" for item in checks):
        return "critical"
    if any(item.outcome == "warning" for item in checks):
        return "degraded"
    return "healthy"


def _latency_ms(timing: object) -> float | None:
    if not isinstance(timing, dict):
        return None
    value = timing.get("total_ms")
    if value is None:
        value = timing.get("latency_ms")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _release_precision(
    release: EdgeWorkloadReleaseDetail,
) -> Literal["fp32", "fp16", "int8"]:
    requirements = release.manifest.runtime_requirements
    builds = requirements.get("engine_builds")
    if isinstance(builds, list) and builds and isinstance(builds[0], dict):
        value = str(builds[0].get("precision") or "fp16")
        if value in {"fp32", "fp16", "int8"}:
            return value
    return "fp16"


def _edge_client(device: EdgeDeviceDetail) -> EdgeAgentClient:
    policy = EdgeNetworkPolicy.from_strings(
        allowed_cidrs=[str(device.allowed_cidr)],
        registered_addresses=[str(device.address.management_ip)],
    )
    return EdgeAgentClient(
        address=device.address,
        network_policy=policy,
        connect_timeout_seconds=3.0,
        read_timeout_seconds=float(os.getenv("YBB_EDGE_DEPLOYMENT_TIMEOUT_SECONDS", "1200")),
        transfer_timeout_seconds=float(os.getenv("YBB_EDGE_TRANSFER_TIMEOUT_SECONDS", "120")),
    )
