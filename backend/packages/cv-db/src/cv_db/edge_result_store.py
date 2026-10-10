from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cv_contracts import (
    EdgeResultBatch,
    EdgeResultEnvelope,
    EdgeViewerLeaseRequest,
    EdgeViewerLeaseSummary,
    RuntimeResultHistoryPolicy,
)
from cv_core import generate_prefixed_id
from cv_status import RuntimeMode
from cv_utils import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_db.models import (
    EdgeDevice,
    EdgeResultCursorRecord,
    EdgeResultReceiptRecord,
    EdgeViewerLeaseRecord,
    EdgeWorkloadReleaseRecord,
    ResultRecord,
    RuntimeChannel,
)


@dataclass(frozen=True)
class EdgePersistBatchOutcome:
    boot_id: str
    persisted_through_sequence: int
    inserted_count: int
    duplicate_count: int


def persist_edge_result_batch(
    session: Session,
    *,
    edge_device_id: str,
    batch: EdgeResultBatch,
) -> EdgePersistBatchOutcome:
    device = session.get(EdgeDevice, edge_device_id)
    if device is None or device.deleted_at is not None:
        raise ValueError("Edge result target device does not exist.")
    if str(batch.management_ip) != device.management_ip:
        raise ValueError("Edge result batch management IP does not match the registered device.")
    cursor = _cursor(session, device, batch.boot_id)
    persisted = int(cursor.last_persisted_sequence)
    inserted = 0
    duplicates = 0
    for envelope in batch.items:
        digest = _envelope_digest(envelope)
        existing = session.scalar(
            select(EdgeResultReceiptRecord).where(
                EdgeResultReceiptRecord.edge_device_id == edge_device_id,
                EdgeResultReceiptRecord.boot_id == batch.boot_id,
                EdgeResultReceiptRecord.sequence == envelope.sequence,
            )
        )
        if existing is not None:
            if existing.envelope_sha256 != digest:
                raise ValueError("An Edge result sequence was replayed with different content.")
            if envelope.sequence > persisted:
                persisted = envelope.sequence
            duplicates += 1
            continue
        if envelope.sequence != persisted + 1:
            raise ValueError(
                f"Edge result gap for boot {batch.boot_id}: expected {persisted + 1}, "
                f"received {envelope.sequence}."
            )
        channel, release = _validate_envelope_binding(
            session,
            edge_device_id=edge_device_id,
            envelope=envelope,
        )
        occurred_at = _parse_datetime(envelope.occurred_at)
        result_record = _result_record(
            envelope,
            channel=channel,
            release=release,
            edge_device_id=edge_device_id,
            occurred_at=occurred_at,
            history_retained=_edge_result_history_is_due(
                session,
                channel=channel,
                occurred_at=occurred_at,
                sampled_on_edge=(
                    _mapping(envelope.payload.get("runtime_metadata")).get(
                        "edge_history_projection"
                    )
                    is True
                ),
            ),
        )
        session.add(result_record)
        session.add(
            EdgeResultReceiptRecord(
                id=f"erct_{digest[:24]}",
                edge_device_id=edge_device_id,
                management_ip=device.management_ip,
                boot_id=batch.boot_id,
                sequence=envelope.sequence,
                channel_id=envelope.channel_id,
                workload_release_id=envelope.workload_release_id,
                result_record_id=result_record.id,
                envelope_sha256=digest,
                occurred_at=occurred_at,
            )
        )
        channel.last_activity_at = utc_now()
        persisted = envelope.sequence
        inserted += 1
    cursor.last_persisted_sequence = max(cursor.last_persisted_sequence, persisted)
    cursor.updated_at = utc_now()
    session.flush()
    return EdgePersistBatchOutcome(
        boot_id=batch.boot_id,
        persisted_through_sequence=persisted,
        inserted_count=inserted,
        duplicate_count=duplicates,
    )


def acknowledge_edge_result_cursor(
    session: Session,
    *,
    edge_device_id: str,
    boot_id: str,
    acknowledged_sequence: int,
) -> None:
    device = session.get(EdgeDevice, edge_device_id)
    if device is None or device.deleted_at is not None:
        raise ValueError("Edge result target device does not exist.")
    cursor = _cursor(session, device, boot_id)
    if acknowledged_sequence > cursor.last_persisted_sequence:
        raise ValueError("Cannot acknowledge an Edge result before it is persisted.")
    cursor.last_acknowledged_sequence = max(
        cursor.last_acknowledged_sequence, acknowledged_sequence
    )
    cursor.updated_at = utc_now()
    session.flush()


def edge_result_cursor_sequences(
    session: Session,
    *,
    edge_device_id: str,
    boot_id: str,
) -> tuple[int, int]:
    row = session.scalar(
        select(EdgeResultCursorRecord).where(
            EdgeResultCursorRecord.edge_device_id == edge_device_id,
            EdgeResultCursorRecord.boot_id == boot_id,
        )
    )
    if row is None:
        return 0, 0
    return row.last_persisted_sequence, row.last_acknowledged_sequence


def upsert_edge_viewer_lease(
    session: Session,
    *,
    channel_id: str,
    payload: EdgeViewerLeaseRequest,
) -> EdgeViewerLeaseSummary:
    channel = session.get(RuntimeChannel, channel_id)
    if (
        channel is None
        or channel.deleted_at is not None
        or channel.execution_target != "edge_device"
        or not channel.edge_device_id
    ):
        raise ValueError("Viewer leases are only available for Edge-placed channels.")
    if channel.observed_state not in {"running", "degraded"}:
        raise ValueError("The Edge channel must be running before a viewer lease is created.")
    row = session.get(EdgeViewerLeaseRecord, payload.lease_id) if payload.lease_id else None
    if row is not None and row.channel_id != channel_id:
        raise ValueError("Viewer lease belongs to another channel.")
    now = utc_now()
    if row is None:
        row = EdgeViewerLeaseRecord(
            id=payload.lease_id or generate_prefixed_id("evl"),
            channel_id=channel_id,
            edge_device_id=channel.edge_device_id,
            status="active",
            expires_at=now + timedelta(seconds=payload.ttl_seconds),
            max_fps=payload.max_fps,
            max_width=payload.max_width,
            include_overlay=payload.include_overlay,
            updated_at=now,
        )
        session.add(row)
    else:
        row.edge_device_id = channel.edge_device_id
        row.status = "active"
        row.expires_at = now + timedelta(seconds=payload.ttl_seconds)
        row.max_fps = payload.max_fps
        row.max_width = payload.max_width
        row.include_overlay = payload.include_overlay
        row.updated_at = now
    session.flush()
    return edge_viewer_lease_to_api(row)


def release_edge_viewer_lease(
    session: Session,
    *,
    channel_id: str,
    lease_id: str,
) -> EdgeViewerLeaseSummary:
    row = session.get(EdgeViewerLeaseRecord, lease_id)
    if row is None or row.channel_id != channel_id:
        raise ValueError("Viewer lease does not exist for the channel.")
    row.status = "released"
    row.expires_at = min(_aware(row.expires_at), utc_now())
    row.updated_at = utc_now()
    session.flush()
    return edge_viewer_lease_to_api(row)


def active_edge_viewer_leases(
    session: Session,
    *,
    edge_device_id: str | None = None,
) -> list[EdgeViewerLeaseRecord]:
    now = utc_now()
    session.query(EdgeViewerLeaseRecord).filter(
        EdgeViewerLeaseRecord.status == "active",
        EdgeViewerLeaseRecord.expires_at <= now,
    ).update({"status": "expired", "updated_at": now}, synchronize_session=False)
    query = select(EdgeViewerLeaseRecord).where(
        EdgeViewerLeaseRecord.status == "active",
        EdgeViewerLeaseRecord.expires_at > now,
    )
    if edge_device_id:
        query = query.where(EdgeViewerLeaseRecord.edge_device_id == edge_device_id)
    return list(session.scalars(query.order_by(EdgeViewerLeaseRecord.channel_id)).all())


def edge_viewer_lease_to_api(row: EdgeViewerLeaseRecord) -> EdgeViewerLeaseSummary:
    status = row.status
    if status == "active" and _aware(row.expires_at) <= utc_now():
        status = "expired"
    return EdgeViewerLeaseSummary(
        lease_id=row.id,
        channel_id=row.channel_id,
        edge_device_id=row.edge_device_id,
        status=status,
        expires_at=_aware(row.expires_at).isoformat(),
        max_fps=row.max_fps,
        max_width=row.max_width,
        include_overlay=row.include_overlay,
        created_at=_aware(row.created_at).isoformat(),
        updated_at=_aware(row.updated_at).isoformat(),
    )


def _cursor(session: Session, device: EdgeDevice, boot_id: str) -> EdgeResultCursorRecord:
    row = session.scalar(
        select(EdgeResultCursorRecord).where(
            EdgeResultCursorRecord.edge_device_id == device.id,
            EdgeResultCursorRecord.boot_id == boot_id,
        )
    )
    if row is None:
        row = EdgeResultCursorRecord(
            id=generate_prefixed_id("ecur"),
            edge_device_id=device.id,
            management_ip=device.management_ip,
            boot_id=boot_id,
            last_persisted_sequence=0,
            last_acknowledged_sequence=0,
            updated_at=utc_now(),
        )
        session.add(row)
        session.flush()
    return row


def _validate_envelope_binding(
    session: Session,
    *,
    edge_device_id: str,
    envelope: EdgeResultEnvelope,
) -> tuple[RuntimeChannel, EdgeWorkloadReleaseRecord]:
    channel = session.get(RuntimeChannel, envelope.channel_id)
    if (
        channel is None
        or channel.deleted_at is not None
        or channel.execution_target != "edge_device"
        or channel.edge_device_id != edge_device_id
    ):
        raise ValueError("Edge result references a channel that is not assigned to the device.")
    release = session.get(EdgeWorkloadReleaseRecord, envelope.workload_release_id)
    if (
        release is None
        or release.channel_id != channel.id
        or release.edge_device_id != edge_device_id
    ):
        raise ValueError("Edge result references an unknown or mismatched workload release.")
    return channel, release


def _result_record(
    envelope: EdgeResultEnvelope,
    *,
    channel: RuntimeChannel,
    release: EdgeWorkloadReleaseRecord,
    edge_device_id: str,
    occurred_at: datetime,
    history_retained: bool,
) -> ResultRecord:
    payload = dict(envelope.payload or {})
    execution = payload.get("execution_result")
    sanitized_result = _without_inline_image_data(
        dict(execution) if isinstance(execution, dict) else payload
    )
    result = dict(sanitized_result) if isinstance(sanitized_result, dict) else {}
    status = str(result.get("status") or payload.get("status") or "completed")
    if status not in {"completed", "failed", "timeout"}:
        status = "completed"
    runtime_metadata = _mapping(result.get("runtime_metadata"))
    runtime_metadata.update(
        {
            "result_created_by": "edge_result_sync",
            "edge_device_id": edge_device_id,
            "edge_management_ip": str(envelope.management_ip),
            "edge_boot_id": envelope.boot_id,
            "edge_sequence": envelope.sequence,
            "workload_release_id": envelope.workload_release_id,
            "edge_correlation_id": envelope.correlation_id,
        }
    )
    result_id_digest = hashlib.sha256(
        f"{edge_device_id}:{envelope.boot_id}:{envelope.sequence}".encode()
    ).hexdigest()
    return ResultRecord(
        id=f"rrc_edge_{result_id_digest[:24]}",
        channel_id=channel.id,
        pipeline_version_id=release.pipeline_version_id,
        runtime_execution_id=channel.edge_deployment_id,
        trigger_endpoint_id=None,
        correlation_id=envelope.correlation_id[:64],
        source_timestamp=occurred_at,
        completed_at=occurred_at,
        mode=RuntimeMode.REALTIME.value,
        status=status,
        result_type=envelope.result_type,
        schema_version=str(result.get("schema_version") or envelope.schema_version),
        result=result,
        raw_inference_outputs=_object_list(result.get("raw_inference_outputs")),
        normalized_inference_outputs=_object_list(result.get("normalized_inference_outputs")),
        derived_outputs=(
            _object_list(result.get("derived_outputs"))
            or _object_list(result.get("persisted_results"))
        ),
        overlay_primitives=_object_list(result.get("overlay_primitives")),
        trigger_response=_mapping(result.get("trigger_response") or result.get("final_response")),
        runtime_metadata=runtime_metadata,
        raw_outputs_ref=None,
        timing=_mapping(result.get("timing")),
        model_routes=_object_list(result.get("model_routes")),
        history_retained=history_retained,
    )


def _edge_result_history_is_due(
    session: Session,
    *,
    channel: RuntimeChannel,
    occurred_at: datetime,
    sampled_on_edge: bool = False,
) -> bool:
    try:
        policy = RuntimeResultHistoryPolicy.model_validate(channel.result_history_policy or {})
    except ValueError:
        policy = RuntimeResultHistoryPolicy()
    if not policy.enabled:
        return False
    if sampled_on_edge:
        # The Edge already applied this release's sampling interval. Reapplying
        # it with wall-clock timestamps could hide samples due to sub-ms jitter.
        return True
    latest_completed_at = session.scalar(
        select(ResultRecord.completed_at)
        .where(
            ResultRecord.channel_id == channel.id,
            ResultRecord.history_retained.is_(True),
        )
        .order_by(ResultRecord.completed_at.desc(), ResultRecord.id.desc())
        .limit(1)
    )
    if latest_completed_at is None:
        return True
    return occurred_at - _aware(latest_completed_at) >= timedelta(
        seconds=policy.sample_interval_seconds
    )


def _envelope_digest(envelope: EdgeResultEnvelope) -> str:
    return hashlib.sha256(envelope.model_dump_json().encode("utf-8")).hexdigest()


def _parse_datetime(value: str) -> datetime:
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise ValueError("Edge result occurred_at must be a valid ISO-8601 timestamp.") from exc


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _without_inline_image_data(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_inline_image_data(item)
            for key, item in value.items()
            if key not in {"image_base64", "data_uri"}
        }
    if isinstance(value, list):
        return [_without_inline_image_data(item) for item in value]
    return value
