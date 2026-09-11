from __future__ import annotations

from datetime import datetime

from cv_contracts import EdgeDeploymentAttemptSummary, EdgeDiagnosticReport
from cv_core import generate_prefixed_id
from cv_status import HealthState, RuntimeChannelDesiredState, RuntimeObservedState
from cv_utils import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_db.edge_deployment_store import edge_deployment_attempt_to_api
from cv_db.models import (
    EdgeDeploymentAttemptRecord,
    EdgeDevice,
    EdgeDiagnosticReportRecord,
    RuntimeChannel,
)


def save_edge_diagnostic_report(
    session: Session,
    report: EdgeDiagnosticReport,
) -> EdgeDiagnosticReport:
    row = EdgeDiagnosticReportRecord(
        id=report.diagnostic_report_id,
        edge_device_id=report.edge_device_id,
        status=report.status,
        generated_at=_parse_datetime(report.generated_at),
        created_by=report.created_by,
        checks=[item.model_dump(mode="json") for item in report.checks],
        channels=[item.model_dump(mode="json") for item in report.channels],
    )
    session.add(row)
    session.flush()
    return report


def latest_edge_diagnostic_report(
    session: Session,
    edge_device_id: str,
) -> EdgeDiagnosticReport | None:
    row = session.scalar(
        select(EdgeDiagnosticReportRecord)
        .where(EdgeDiagnosticReportRecord.edge_device_id == edge_device_id)
        .order_by(
            EdgeDiagnosticReportRecord.generated_at.desc(),
            EdgeDiagnosticReportRecord.id.desc(),
        )
        .limit(1)
    )
    return None if row is None else edge_diagnostic_report_to_api(session, row)


def list_latest_edge_diagnostic_reports(session: Session) -> list[EdgeDiagnosticReport]:
    rows = session.scalars(
        select(EdgeDiagnosticReportRecord).order_by(
            EdgeDiagnosticReportRecord.generated_at.desc(),
            EdgeDiagnosticReportRecord.id.desc(),
        )
    ).all()
    latest: dict[str, EdgeDiagnosticReportRecord] = {}
    for row in rows:
        latest.setdefault(row.edge_device_id, row)
    return [edge_diagnostic_report_to_api(session, row) for row in latest.values()]


def edge_diagnostic_report_to_api(
    session: Session,
    row: EdgeDiagnosticReportRecord,
) -> EdgeDiagnosticReport:
    device = session.get(EdgeDevice, row.edge_device_id)
    return EdgeDiagnosticReport(
        diagnostic_report_id=row.id,
        edge_device_id=row.edge_device_id,
        edge_device_name=device.name if device is not None else row.edge_device_id,
        status=row.status,
        generated_at=row.generated_at.isoformat(),
        created_by=row.created_by,
        checks=list(row.checks or []),
        channels=list(row.channels or []),
    )


def mark_previous_edge_deployment_standby(
    session: Session,
    edge_deployment_id: str,
) -> None:
    active = session.get(EdgeDeploymentAttemptRecord, edge_deployment_id)
    if active is None or not active.previous_edge_deployment_id:
        return
    previous = session.get(
        EdgeDeploymentAttemptRecord,
        active.previous_edge_deployment_id,
    )
    if previous is None:
        return
    previous.status = "standby"
    previous.remote_status = "standby"
    previous.desired_state = "running"
    previous.updated_at = utc_now()
    previous.finished_at = None
    session.flush()


def validate_manual_rollback_target(
    session: Session,
    edge_deployment_id: str,
) -> tuple[RuntimeChannel, EdgeDeploymentAttemptRecord, EdgeDeploymentAttemptRecord]:
    active = session.get(EdgeDeploymentAttemptRecord, edge_deployment_id)
    if active is None:
        raise ValueError("Edge deployment does not exist.")
    channel = session.get(RuntimeChannel, active.channel_id)
    if (
        channel is None
        or channel.deleted_at is not None
        or channel.execution_target != "edge_device"
        or channel.edge_deployment_id != active.id
    ):
        raise ValueError("Only the currently active Edge deployment can be rolled back.")
    if active.status != "running":
        raise ValueError("The active deployment must be running before rollback.")
    if not active.previous_edge_deployment_id:
        raise ValueError("No previous A/B deployment is available for rollback.")
    previous = session.get(EdgeDeploymentAttemptRecord, active.previous_edge_deployment_id)
    if (
        previous is None
        or previous.channel_id != channel.id
        or previous.edge_device_id != active.edge_device_id
        or previous.status != "standby"
    ):
        raise ValueError("The previous A/B deployment is not a valid standby rollback target.")
    return channel, active, previous


def complete_manual_edge_rollback(
    session: Session,
    *,
    edge_deployment_id: str,
) -> tuple[EdgeDeploymentAttemptSummary, EdgeDeploymentAttemptSummary]:
    channel, active, previous = validate_manual_rollback_target(session, edge_deployment_id)
    now = utc_now()
    active.status = "rolled_back"
    active.remote_status = "rolled_back"
    active.desired_state = "stopped"
    active.finished_at = now
    active.updated_at = now
    previous.status = "running"
    previous.remote_status = "running"
    previous.desired_state = "running"
    previous.activated_at = now
    previous.finished_at = None
    previous.updated_at = now
    channel.edge_deployment_id = previous.id
    channel.edge_workload_release_id = previous.workload_release_id
    channel.desired_state = RuntimeChannelDesiredState.RUNNING.value
    channel.observed_state = RuntimeObservedState.RUNNING.value
    channel.health_state = HealthState.OK.value
    channel.health_summary = {
        "state": HealthState.OK.value,
        "status_text": "Previous Edge workload restored by manual rollback",
        "edge_deployment_id": previous.id,
        "workload_release_id": previous.workload_release_id,
        "rolled_back_deployment_id": active.id,
    }
    channel.last_error = None
    channel.last_activity_at = now
    session.flush()
    return edge_deployment_attempt_to_api(active), edge_deployment_attempt_to_api(previous)


def new_diagnostic_report_id() -> str:
    return generate_prefixed_id("edgr")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
