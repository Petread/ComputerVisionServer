from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from cv_contracts import (
    EdgeAdmissionReport,
    EdgeDeploymentAttemptSummary,
    EdgeDeploymentManifest,
    EdgeWorkloadReleaseDetail,
    EdgeWorkloadReleaseSummary,
)
from cv_core import generate_prefixed_id
from cv_status import (
    HealthState,
    RuntimeChannelDesiredState,
    RuntimeControlCommandStatus,
    RuntimeObservedState,
)
from cv_utils import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_db.models import (
    EdgeDeploymentAttemptRecord,
    EdgeDevice,
    EdgeWorkloadReleaseRecord,
    RuntimeChannel,
    RuntimeControlCommandRecord,
)


def mark_edge_deployment_validated(session: Session, edge_device_id: str) -> None:
    row = session.get(EdgeDevice, edge_device_id)
    if row is None or row.deleted_at is not None:
        raise ValueError("Edge device does not exist.")
    row.deployment_revalidation_required = False
    row.updated_at = utc_now()
    session.flush()


def save_edge_workload_release(
    session: Session,
    *,
    release_id: str,
    release_digest: str,
    channel_id: str,
    pipeline_version_id: str,
    edge_device_id: str,
    capability_digest: str,
    platform: str,
    manifest: EdgeDeploymentManifest,
    artifact_sources: list[dict[str, Any]],
    admission_report: EdgeAdmissionReport,
    required_node_types: list[str],
    created_by: str | None,
) -> EdgeWorkloadReleaseDetail:
    existing = session.scalar(
        select(EdgeWorkloadReleaseRecord).where(
            EdgeWorkloadReleaseRecord.release_digest == release_digest
        )
    )
    if existing is not None:
        if admission_report.status == "admitted" or existing.status != "admitted":
            existing.status = "admitted" if admission_report.status == "admitted" else "rejected"
            existing.admission_report = admission_report.model_dump(mode="json")
            session.flush()
            return edge_workload_release_to_detail(existing)
        return edge_workload_release_to_detail(existing).model_copy(
            update={"status": "rejected", "admission_report": admission_report}
        )
    row = EdgeWorkloadReleaseRecord(
        id=release_id,
        release_digest=release_digest,
        channel_id=channel_id,
        pipeline_version_id=pipeline_version_id,
        edge_device_id=edge_device_id,
        capability_digest=capability_digest,
        platform=platform,
        status="admitted" if admission_report.status == "admitted" else "rejected",
        manifest=manifest.model_dump(mode="json"),
        artifact_sources=artifact_sources,
        admission_report=admission_report.model_dump(mode="json"),
        artifact_count=len(manifest.artifacts),
        total_artifact_bytes=sum(item.size_bytes for item in manifest.artifacts),
        required_node_types=required_node_types,
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    return edge_workload_release_to_detail(row)


def get_edge_workload_release(
    session: Session, workload_release_id: str
) -> EdgeWorkloadReleaseDetail | None:
    row = session.get(EdgeWorkloadReleaseRecord, workload_release_id)
    return None if row is None else edge_workload_release_to_detail(row)


def get_edge_workload_release_row(
    session: Session, workload_release_id: str
) -> EdgeWorkloadReleaseRecord | None:
    return session.get(EdgeWorkloadReleaseRecord, workload_release_id)


def list_edge_workload_releases(
    session: Session,
    *,
    channel_id: str | None = None,
    edge_device_id: str | None = None,
) -> list[EdgeWorkloadReleaseSummary]:
    query = select(EdgeWorkloadReleaseRecord)
    if channel_id:
        query = query.where(EdgeWorkloadReleaseRecord.channel_id == channel_id)
    if edge_device_id:
        query = query.where(EdgeWorkloadReleaseRecord.edge_device_id == edge_device_id)
    rows = session.scalars(query.order_by(EdgeWorkloadReleaseRecord.created_at.desc())).all()
    return [edge_workload_release_to_summary(row) for row in rows]


def create_edge_deployment_attempt(
    session: Session,
    *,
    command_id: str,
    channel_id: str,
    edge_device_id: str,
    workload_release_id: str,
) -> EdgeDeploymentAttemptSummary:
    existing = session.scalar(
        select(EdgeDeploymentAttemptRecord).where(
            EdgeDeploymentAttemptRecord.command_id == command_id
        )
    )
    if existing is not None:
        return edge_deployment_attempt_to_api(existing)
    previous = session.scalar(
        select(EdgeDeploymentAttemptRecord)
        .where(
            EdgeDeploymentAttemptRecord.channel_id == channel_id,
            EdgeDeploymentAttemptRecord.status == "running",
        )
        .order_by(EdgeDeploymentAttemptRecord.created_at.desc())
    )
    row = EdgeDeploymentAttemptRecord(
        id=generate_prefixed_id("edep"),
        channel_id=channel_id,
        edge_device_id=edge_device_id,
        workload_release_id=workload_release_id,
        previous_edge_deployment_id=previous.id if previous else None,
        command_id=command_id,
        desired_state="running",
        status="queued",
        progress={},
        updated_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return edge_deployment_attempt_to_api(row)


def update_edge_deployment_attempt(
    session: Session,
    edge_deployment_id: str,
    *,
    status: str,
    remote_status: str | None = None,
    progress: dict[str, Any] | None = None,
    last_error: str | None = None,
) -> EdgeDeploymentAttemptSummary:
    row = _deployment_row(session, edge_deployment_id)
    row.status = status
    if status in {"stopping", "stopped"}:
        row.desired_state = "stopped"
    if remote_status is not None:
        row.remote_status = remote_status
    if progress is not None:
        row.progress = progress
    row.last_error = last_error
    row.updated_at = utc_now()
    if status == "running":
        row.desired_state = "running"
        row.activated_at = row.activated_at or utc_now()
    if status in {"stopped", "rolled_back", "failed", "admission_failed"}:
        row.finished_at = utc_now()
    session.flush()
    return edge_deployment_attempt_to_api(row)


def get_edge_deployment_attempt(
    session: Session, edge_deployment_id: str
) -> EdgeDeploymentAttemptSummary | None:
    row = session.get(EdgeDeploymentAttemptRecord, edge_deployment_id)
    return None if row is None else edge_deployment_attempt_to_api(row)


def list_edge_deployment_attempts(
    session: Session,
    *,
    channel_id: str | None = None,
    edge_device_id: str | None = None,
) -> list[EdgeDeploymentAttemptSummary]:
    query = select(EdgeDeploymentAttemptRecord)
    if channel_id:
        query = query.where(EdgeDeploymentAttemptRecord.channel_id == channel_id)
    if edge_device_id:
        query = query.where(EdgeDeploymentAttemptRecord.edge_device_id == edge_device_id)
    rows = session.scalars(query.order_by(EdgeDeploymentAttemptRecord.created_at.desc())).all()
    return [edge_deployment_attempt_to_api(row) for row in rows]


def latest_edge_deployment_row(
    session: Session, channel_id: str
) -> EdgeDeploymentAttemptRecord | None:
    return session.scalar(
        select(EdgeDeploymentAttemptRecord)
        .where(EdgeDeploymentAttemptRecord.channel_id == channel_id)
        .order_by(EdgeDeploymentAttemptRecord.created_at.desc())
    )


def claim_next_edge_runtime_control_command(
    session: Session,
    *,
    worker_id: str,
    claim_timeout: timedelta = timedelta(minutes=30),
) -> RuntimeControlCommandRecord | None:
    _repair_completed_edge_start_states(session)
    stale_before = utc_now() - claim_timeout
    row = session.scalar(
        select(RuntimeControlCommandRecord)
        .join(RuntimeChannel, RuntimeChannel.id == RuntimeControlCommandRecord.channel_id)
        .where(
            (RuntimeControlCommandRecord.status == RuntimeControlCommandStatus.QUEUED.value)
            | (
                (RuntimeControlCommandRecord.status == RuntimeControlCommandStatus.PROCESSING.value)
                & (RuntimeControlCommandRecord.claimed_at < stale_before)
            ),
            RuntimeChannel.execution_target == "edge_device",
        )
        .order_by(RuntimeControlCommandRecord.created_at.asc())
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return None
    row.status = RuntimeControlCommandStatus.PROCESSING.value
    row.claimed_by = worker_id
    row.claimed_at = utc_now()
    row.consumed_at = row.claimed_at
    row.attempt_count = int(row.attempt_count or 0) + 1
    session.flush()
    return row


def _repair_completed_edge_start_states(session: Session) -> None:
    """Repair request states left by old idempotent retries, without a new rollout."""
    candidates = session.scalars(
        select(RuntimeChannel)
        .join(
            EdgeDeploymentAttemptRecord,
            EdgeDeploymentAttemptRecord.id == RuntimeChannel.edge_deployment_id,
        )
        .where(
            RuntimeChannel.execution_target == "edge_device",
            RuntimeChannel.deleted_at.is_(None),
            RuntimeChannel.desired_state == RuntimeChannelDesiredState.START_REQUESTED.value,
            RuntimeChannel.observed_state == RuntimeObservedState.STARTING.value,
            EdgeDeploymentAttemptRecord.status == "running",
        )
        .with_for_update(of=RuntimeChannel, skip_locked=True)
    ).all()
    for channel in candidates:
        latest_command = session.scalar(
            select(RuntimeControlCommandRecord)
            .where(RuntimeControlCommandRecord.channel_id == channel.id)
            .order_by(
                RuntimeControlCommandRecord.created_at.desc(),
                RuntimeControlCommandRecord.id.desc(),
            )
            .limit(1)
        )
        deployment = session.get(EdgeDeploymentAttemptRecord, channel.edge_deployment_id)
        if (
            latest_command is None
            or deployment is None
            or latest_command.status != RuntimeControlCommandStatus.SUCCEEDED.value
            or latest_command.command not in {"start", "restart"}
            or deployment.command_id != latest_command.id
            or deployment.channel_id != channel.id
            or deployment.edge_device_id != channel.edge_device_id
            or deployment.workload_release_id != channel.edge_workload_release_id
        ):
            continue
        _apply_completed_edge_channel_state(
            channel,
            deployment_id=deployment.id,
            workload_release_id=deployment.workload_release_id,
            running=True,
        )
    session.flush()


def complete_edge_runtime_command(
    session: Session,
    *,
    command_id: str,
    deployment_id: str | None,
    workload_release_id: str | None,
    running: bool,
) -> None:
    command = session.get(RuntimeControlCommandRecord, command_id)
    if command is None:
        raise ValueError("Runtime control command does not exist.")
    channel = session.get(RuntimeChannel, command.channel_id)
    if channel is None:
        raise ValueError("Runtime channel does not exist.")
    command.status = RuntimeControlCommandStatus.SUCCEEDED.value
    command.finished_at = utc_now()
    command.last_error = None
    _apply_completed_edge_channel_state(
        channel,
        deployment_id=deployment_id,
        workload_release_id=workload_release_id,
        running=running,
    )
    session.flush()


def _apply_completed_edge_channel_state(
    channel: RuntimeChannel,
    *,
    deployment_id: str | None,
    workload_release_id: str | None,
    running: bool,
) -> None:
    if running:
        channel.desired_state = RuntimeChannelDesiredState.RUNNING.value
        channel.observed_state = RuntimeObservedState.RUNNING.value
        channel.health_state = HealthState.OK.value
        channel.edge_workload_release_id = workload_release_id
        channel.edge_deployment_id = deployment_id
        channel.health_summary = {
            "state": "ok",
            "status_text": "Edge workload is running",
            "edge_deployment_id": deployment_id,
            "workload_release_id": workload_release_id,
        }
    else:
        channel.desired_state = RuntimeChannelDesiredState.STOPPED.value
        channel.observed_state = RuntimeObservedState.STOPPED.value
        channel.health_state = HealthState.DOWN.value
        channel.edge_deployment_id = None
        channel.health_summary = {
            "state": "down",
            "status_text": "Edge workload is stopped",
        }
    channel.last_error = None
    channel.last_activity_at = utc_now()


def fail_edge_runtime_command(session: Session, *, command_id: str, error: str) -> None:
    command = session.get(RuntimeControlCommandRecord, command_id)
    if command is None:
        raise ValueError("Runtime control command does not exist.")
    command.status = RuntimeControlCommandStatus.FAILED.value
    command.finished_at = utc_now()
    command.last_error = error[:2000]
    channel = session.get(RuntimeChannel, command.channel_id)
    if channel is not None:
        if channel.edge_deployment_id:
            channel.observed_state = RuntimeObservedState.RUNNING.value
            channel.health_state = HealthState.DEGRADED.value
            if command.command != "stop":
                channel.desired_state = RuntimeChannelDesiredState.RUNNING.value
            channel.last_error = error[:2000]
            channel.health_summary = {
                "state": "degraded",
                "status_text": "Previous Edge workload remains active",
                "last_error": error[:2000],
                "edge_deployment_id": channel.edge_deployment_id,
                "workload_release_id": channel.edge_workload_release_id,
            }
        else:
            channel.desired_state = RuntimeChannelDesiredState.STOPPED.value
            channel.observed_state = RuntimeObservedState.FAILED.value
            channel.health_state = HealthState.DOWN.value
            channel.last_error = error[:2000]
            channel.health_summary = {
                "state": "down",
                "status_text": "Edge deployment failed",
                "last_error": error[:2000],
            }
        channel.last_activity_at = utc_now()
    session.flush()


def edge_workload_release_to_summary(
    row: EdgeWorkloadReleaseRecord,
) -> EdgeWorkloadReleaseSummary:
    return EdgeWorkloadReleaseSummary(
        workload_release_id=row.id,
        release_digest=row.release_digest,
        channel_id=row.channel_id,
        pipeline_version_id=row.pipeline_version_id,
        edge_device_id=row.edge_device_id,
        capability_digest=row.capability_digest,
        platform=row.platform,
        status=row.status,
        artifact_count=row.artifact_count,
        total_artifact_bytes=row.total_artifact_bytes,
        required_node_types=list(row.required_node_types or []),
        created_at=_iso(row.created_at) or utc_now().isoformat(),
        created_by=row.created_by,
    )


def edge_workload_release_to_detail(
    row: EdgeWorkloadReleaseRecord,
) -> EdgeWorkloadReleaseDetail:
    summary = edge_workload_release_to_summary(row)
    manifest = EdgeDeploymentManifest.model_validate(row.manifest)
    return EdgeWorkloadReleaseDetail(
        **summary.model_dump(),
        manifest=manifest,
        artifacts=manifest.artifacts,
        admission_report=EdgeAdmissionReport.model_validate(row.admission_report),
    )


def edge_deployment_attempt_to_api(
    row: EdgeDeploymentAttemptRecord,
) -> EdgeDeploymentAttemptSummary:
    return EdgeDeploymentAttemptSummary(
        edge_deployment_id=row.id,
        channel_id=row.channel_id,
        edge_device_id=row.edge_device_id,
        workload_release_id=row.workload_release_id,
        previous_edge_deployment_id=row.previous_edge_deployment_id,
        command_id=row.command_id,
        desired_state=row.desired_state,
        status=row.status,
        remote_status=row.remote_status,
        progress=dict(row.progress or {}),
        last_error=row.last_error,
        created_at=_iso(row.created_at) or utc_now().isoformat(),
        updated_at=_iso(row.updated_at) or utc_now().isoformat(),
        activated_at=_iso(row.activated_at),
        finished_at=_iso(row.finished_at),
    )


def _deployment_row(session: Session, edge_deployment_id: str) -> EdgeDeploymentAttemptRecord:
    row = session.get(EdgeDeploymentAttemptRecord, edge_deployment_id)
    if row is None:
        raise ValueError("Edge deployment attempt does not exist.")
    return row


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
