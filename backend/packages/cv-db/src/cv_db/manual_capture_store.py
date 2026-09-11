from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from cv_contracts.runtime import (
    RuntimeManualCaptureResult,
    RuntimeManualCaptureSessionStatus,
    RuntimeManualCaptureTarget,
)
from cv_core import generate_prefixed_id
from cv_status import LabelProjectStatus, RuntimeChannelDesiredState
from cv_utils import utc_now, utc_now_iso
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from cv_db.models import (
    Dataset,
    LabelProject,
    RuntimeChannel,
    RuntimeManualCaptureRequestRecord,
    RuntimeManualCaptureSession,
)

_SELECTABLE_PROJECT_STATES = {
    LabelProjectStatus.DISCOVERED.value,
    LabelProjectStatus.MAPPED.value,
    LabelProjectStatus.ACTIVE.value,
}


def list_manual_capture_targets(session: Session) -> list[RuntimeManualCaptureTarget]:
    rows = session.scalars(
        select(LabelProject)
        .where(LabelProject.deleted_at.is_(None))
        .order_by(LabelProject.title.asc(), LabelProject.label_studio_project_id.asc())
    ).all()
    dataset_names = {row.id: row.name for row in session.scalars(select(Dataset)).all()}
    return [
        _capture_target(row, dataset_name=dataset_names.get(row.dataset_id))
        for row in rows
        if row.status in _SELECTABLE_PROJECT_STATES
    ]


def validate_manual_capture_target(
    session: Session,
    capture_target_id: str,
) -> LabelProject:
    row = session.get(LabelProject, capture_target_id)
    if row is None or row.deleted_at is not None:
        raise ValueError("Label Studio capture target does not exist.")
    if row.status not in _SELECTABLE_PROJECT_STATES:
        raise ValueError(
            "The selected Label Studio project is not available for manual live capture."
        )
    return row


def get_runtime_channel_for_manual_capture(
    session: Session,
    channel_id: str,
) -> RuntimeChannel:
    channel = session.get(RuntimeChannel, channel_id)
    if channel is None or channel.deleted_at is not None:
        raise ValueError("Channel does not exist.")
    if channel.desired_state == RuntimeChannelDesiredState.DISABLED.value:
        raise ValueError("Disabled channels cannot be used for manual live capture.")
    return channel


def create_manual_capture_session(
    session: Session,
    *,
    channel_id: str,
    capture_target_id: str,
    interval_seconds: int,
    operator_user_id: str,
    live_session_id: str | None,
    request_predictions: bool = True,
    capture_source: str = "LIVE_VIEW_FRAME",
    view_profile_id: str | None = None,
    require_eligibility: bool = False,
) -> RuntimeManualCaptureSessionStatus:
    channel = get_runtime_channel_for_manual_capture(session, channel_id)
    target = validate_manual_capture_target(session, capture_target_id)
    existing = _running_session_for_channel(session, channel_id)
    if existing is not None:
        raise ValueError("An interval manual capture session is already running for this channel.")
    now = utc_now()
    row = RuntimeManualCaptureSession(
        id=generate_prefixed_id("mcs"),
        channel_id=channel.id,
        target_label_project_id=target.id,
        label_studio_project_id=target.label_studio_project_id,
        interval_seconds=interval_seconds,
        state="running",
        operator_user_id=operator_user_id,
        live_session_id=live_session_id,
        started_at=now,
        metadata_json={
            "capture_scope": (
                "pipeline_image_output"
                if capture_source == "PIPELINE_IMAGE_OUTPUT"
                else "full_frame"
            ),
            "capture_source": capture_source,
            "view_profile_id": view_profile_id,
            "require_eligibility": require_eligibility,
            "execution_owner": "capture-service",
            "retry_policy": "retry_transient_label_studio_failures",
            "backpressure": "one_in_flight_per_session",
            "duplicates": "manual_captures_are_kept",
            "request_predictions": request_predictions,
        },
    )
    session.add(row)
    session.flush()
    return _session_status(row, target=target)


def stop_manual_capture_session(
    session: Session,
    *,
    channel_id: str,
    state: Literal["stopped", "failed"] = "stopped",
    error_summary: str | None = None,
) -> RuntimeManualCaptureSessionStatus:
    row = _running_session_for_channel(session, channel_id)
    if row is None:
        latest = _latest_session_for_channel(session, channel_id)
        if latest is None:
            get_runtime_channel_for_manual_capture(session, channel_id)
            return RuntimeManualCaptureSessionStatus(channel_id=channel_id, state="idle")
        return _session_status(
            latest,
            target=session.get(LabelProject, latest.target_label_project_id),
        )
    row.state = state
    row.stopped_at = utc_now()
    row.last_error = error_summary
    session.flush()
    return _session_status(row, target=session.get(LabelProject, row.target_label_project_id))


def get_manual_capture_session_status(
    session: Session,
    channel_id: str,
) -> RuntimeManualCaptureSessionStatus:
    get_runtime_channel_for_manual_capture(session, channel_id)
    row = _running_session_for_channel(session, channel_id) or _latest_session_for_channel(
        session,
        channel_id,
    )
    if row is None:
        return RuntimeManualCaptureSessionStatus(channel_id=channel_id, state="idle")
    return _session_status(row, target=session.get(LabelProject, row.target_label_project_id))


def record_manual_capture_result(
    session: Session,
    *,
    channel_id: str,
    channel_display_name: str,
    capture_target_id: str,
    target_display_name: str,
    label_studio_project_id: int,
    mode: Literal["single", "interval"],
    captured_at: datetime,
    operator_user_id: str,
    status: Literal["queued", "success", "failed"],
    manual_capture_session_id: str | None = None,
    interval_seconds: int | None = None,
    live_session_id: str | None = None,
    runtime_execution_id: str | None = None,
    source_frame_metadata: dict[str, Any] | None = None,
    asset_uri: str | None = None,
    digest: str | None = None,
    label_studio_task_ids: list[int] | None = None,
    error_summary: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RuntimeManualCaptureResult:
    row = RuntimeManualCaptureRequestRecord(
        id=generate_prefixed_id("mcr"),
        manual_capture_session_id=manual_capture_session_id,
        channel_id=channel_id,
        channel_display_name=channel_display_name,
        target_label_project_id=capture_target_id,
        target_project_title=target_display_name,
        label_studio_project_id=label_studio_project_id,
        capture_mode=mode,
        interval_seconds=interval_seconds,
        captured_at=captured_at,
        operator_user_id=operator_user_id,
        live_session_id=live_session_id,
        runtime_execution_id=runtime_execution_id,
        source_frame_metadata=source_frame_metadata or {},
        asset_uri=asset_uri,
        digest=digest,
        label_studio_task_ids=label_studio_task_ids or [],
        status=status,
        error_summary=error_summary,
        metadata_json=metadata or {},
    )
    session.add(row)
    if manual_capture_session_id:
        _update_session_after_capture(session, manual_capture_session_id, row)
    session.flush()
    return _capture_result(row)


def list_manual_capture_history(
    session: Session,
    *,
    channel_id: str,
    limit: int = 20,
) -> list[RuntimeManualCaptureResult]:
    get_runtime_channel_for_manual_capture(session, channel_id)
    rows = session.scalars(
        select(RuntimeManualCaptureRequestRecord)
        .where(RuntimeManualCaptureRequestRecord.channel_id == channel_id)
        .order_by(RuntimeManualCaptureRequestRecord.captured_at.desc())
        .limit(max(min(limit, 100), 1))
    ).all()
    return [_capture_result(row) for row in rows]


def get_manual_capture_session_record(
    session: Session,
    manual_capture_session_id: str,
) -> RuntimeManualCaptureSession | None:
    return session.get(RuntimeManualCaptureSession, manual_capture_session_id)


def _update_session_after_capture(
    session: Session,
    manual_capture_session_id: str,
    capture: RuntimeManualCaptureRequestRecord,
) -> None:
    capture_session = session.get(RuntimeManualCaptureSession, manual_capture_session_id)
    if capture_session is None:
        return
    capture_session.capture_count = int(capture_session.capture_count or 0) + 1
    capture_session.last_capture_at = capture.captured_at
    capture_session.last_capture_request_id = capture.id
    if capture.status == "success":
        capture_session.last_success_at = capture.captured_at
        capture_session.last_error = None
    elif capture.status == "failed":
        capture_session.failure_count = int(capture_session.failure_count or 0) + 1
        capture_session.last_failure_at = capture.captured_at
        capture_session.last_error = capture.error_summary


def _running_session_for_channel(
    session: Session,
    channel_id: str,
) -> RuntimeManualCaptureSession | None:
    return session.scalar(
        select(RuntimeManualCaptureSession)
        .where(
            RuntimeManualCaptureSession.channel_id == channel_id,
            RuntimeManualCaptureSession.state == "running",
        )
        .order_by(RuntimeManualCaptureSession.started_at.desc())
    )


def _latest_session_for_channel(
    session: Session,
    channel_id: str,
) -> RuntimeManualCaptureSession | None:
    return session.scalar(
        select(RuntimeManualCaptureSession)
        .where(RuntimeManualCaptureSession.channel_id == channel_id)
        .order_by(RuntimeManualCaptureSession.started_at.desc())
    )


def _latest_capture_for_session(
    session: Session,
    manual_capture_session_id: str | None,
) -> RuntimeManualCaptureRequestRecord | None:
    if not manual_capture_session_id:
        return None
    return session.scalar(
        select(RuntimeManualCaptureRequestRecord)
        .where(
            RuntimeManualCaptureRequestRecord.manual_capture_session_id
            == manual_capture_session_id
        )
        .order_by(RuntimeManualCaptureRequestRecord.captured_at.desc())
    )


def _capture_target(
    row: LabelProject,
    *,
    dataset_name: str | None,
) -> RuntimeManualCaptureTarget:
    task_type = row.task_type
    status = row.status
    detail_parts = [task_type.replace("_", " ")]
    if dataset_name:
        detail_parts.append(dataset_name)
    if status:
        detail_parts.append(status.replace("_", " "))
    return RuntimeManualCaptureTarget(
        capture_target_id=row.id,
        display_name=row.title,
        detail=" | ".join(detail_parts),
        task_type=task_type,
        status=status,
        dataset_name=dataset_name,
        label_studio_project_id=row.label_studio_project_id,
        selectable=status in _SELECTABLE_PROJECT_STATES,
    )


def _session_status(
    row: RuntimeManualCaptureSession,
    *,
    target: LabelProject | None,
) -> RuntimeManualCaptureSessionStatus:
    session = object_session(row)
    last_capture = _latest_capture_for_session(session, row.id) if session is not None else None
    return RuntimeManualCaptureSessionStatus(
        manual_capture_session_id=row.id,
        channel_id=row.channel_id,
        state=row.state,
        capture_target_id=row.target_label_project_id,
        target_display_name=target.title if target else None,
        label_studio_project_id=row.label_studio_project_id,
        interval_seconds=row.interval_seconds,
        operator_user_id=row.operator_user_id,
        live_session_id=row.live_session_id,
        started_at=_iso(row.started_at),
        stopped_at=_iso(row.stopped_at),
        last_capture_at=_iso(row.last_capture_at),
        capture_count=int(row.capture_count or 0),
        failure_count=int(row.failure_count or 0),
        last_success_at=_iso(row.last_success_at),
        last_failure_at=_iso(row.last_failure_at),
        last_result=_capture_result(last_capture) if last_capture is not None else None,
        error_summary=row.last_error,
    )


def _capture_result(row: RuntimeManualCaptureRequestRecord) -> RuntimeManualCaptureResult:
    return RuntimeManualCaptureResult(
        capture_request_id=row.id,
        manual_capture_session_id=row.manual_capture_session_id,
        channel_id=row.channel_id,
        channel_display_name=row.channel_display_name,
        capture_target_id=row.target_label_project_id,
        target_display_name=row.target_project_title,
        label_studio_project_id=row.label_studio_project_id,
        mode=row.capture_mode,
        interval_seconds=row.interval_seconds,
        captured_at=_iso(row.captured_at) or utc_now_iso(),
        operator_user_id=row.operator_user_id,
        live_session_id=row.live_session_id,
        source_frame_metadata=row.source_frame_metadata or {},
        status=row.status,
        asset_uri=row.asset_uri,
        label_studio_task_ids=row.label_studio_task_ids or [],
        error_summary=row.error_summary,
        metadata=row.metadata_json or {},
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)
