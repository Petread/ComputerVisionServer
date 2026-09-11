from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from cv_contracts.runtime import (
    RuntimeCaptureRequestSummary,
    RuntimeManualCaptureResult,
)
from cv_core import generate_prefixed_id
from cv_runtime.payloads import RuntimeExecutionResult
from cv_status import LabelProjectStatus
from cv_utils import utc_now
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from cv_db.manual_capture_store import (
    get_runtime_channel_for_manual_capture,
    record_manual_capture_result,
    validate_manual_capture_target,
)
from cv_db.models import (
    CaptureRule,
    LabelProject,
    ResultRecord,
    RuntimeCaptureRequest,
    RuntimeChannel,
    RuntimeManualCaptureRequestRecord,
    RuntimeManualCaptureSession,
)

_SELECTABLE_PROJECT_STATES = {
    LabelProjectStatus.DISCOVERED.value,
    LabelProjectStatus.MAPPED.value,
    LabelProjectStatus.ACTIVE.value,
}

_RETRYABLE_STATUSES = {"queued", "failed_retryable", "rate_limited"}
_ACTIVE_STATUSES = {
    "queued",
    "processing",
    "rate_limited",
    "failed_retryable",
    "delivered",
    "predicted",
}
_DEFAULT_PROCESSING_TIMEOUT_SECONDS = 120


def enqueue_manual_capture_request(
    session: Session,
    *,
    channel_id: str,
    capture_target_id: str,
    mode: Literal["single", "interval"],
    operator_user_id: str,
    live_session_id: str | None = None,
    manual_capture_session_id: str | None = None,
    interval_seconds: int | None = None,
    request_predictions: bool = True,
    capture_source: str = "LIVE_VIEW_FRAME",
    view_profile_id: str | None = None,
    require_eligibility: bool = False,
) -> RuntimeManualCaptureResult:
    channel = get_runtime_channel_for_manual_capture(session, channel_id)
    target = validate_manual_capture_target(session, capture_target_id)
    queued_at = utc_now()
    manual_result = record_manual_capture_result(
        session,
        channel_id=channel.id,
        channel_display_name=channel.name,
        capture_target_id=target.id,
        target_display_name=target.title,
        label_studio_project_id=target.label_studio_project_id,
        mode=mode,
        interval_seconds=interval_seconds,
        captured_at=queued_at,
        operator_user_id=operator_user_id,
        live_session_id=live_session_id,
        manual_capture_session_id=manual_capture_session_id,
        source_frame_metadata={
            "source": "capture_service_latest_live_frame",
            "capture_source": capture_source,
            "view_profile_id": view_profile_id,
            "require_eligibility": require_eligibility,
        },
        status="queued",
        metadata={
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
            "duplicate_policy": "manual_captures_are_kept",
            "request_predictions": request_predictions,
        },
    )
    row = RuntimeCaptureRequest(
        id=generate_prefixed_id("rcr"),
        channel_id=channel.id,
        capture_rule_id="manual_live_capture",
        result_record_id=None,
        manual_capture_session_id=manual_capture_session_id,
        manual_capture_request_id=manual_result.capture_request_id,
        target_label_project_id=target.id,
        label_studio_project_id=target.label_studio_project_id,
        target_project_title=target.title,
        source_type="manual",
        capture_mode="full_frame",
        asset_payload={},
        roi={},
        source_frame_metadata={
            "source": "capture_service_latest_live_frame",
            "capture_source": capture_source,
            "view_profile_id": view_profile_id,
            "require_eligibility": require_eligibility,
        },
        capture_intent={},
        status="queued",
        retry_count=0,
        max_attempts=3,
        queued_at=queued_at,
        metadata_json={
            "manual_mode": mode,
            "operator_user_id": operator_user_id,
            "live_session_id": live_session_id,
            "interval_seconds": interval_seconds,
            "duplicate_policy": "manual_captures_are_kept",
            "request_predictions": request_predictions,
            "capture_source": capture_source,
            "view_profile_id": view_profile_id,
            "require_eligibility": require_eligibility,
        },
    )
    session.add(row)
    if manual_result.capture_request_id:
        manual_row = session.get(
            RuntimeManualCaptureRequestRecord,
            manual_result.capture_request_id,
        )
        if manual_row is not None:
            manual_row.metadata_json = {
                **(manual_row.metadata_json or {}),
                "runtime_capture_request_id": row.id,
                "request_predictions": request_predictions,
                "capture_source": capture_source,
                "view_profile_id": view_profile_id,
                "require_eligibility": require_eligibility,
            }
    session.flush()
    return manual_result.model_copy(
        update={
            "metadata": {
                **manual_result.metadata,
                "runtime_capture_request_id": row.id,
                "request_predictions": request_predictions,
                "capture_source": capture_source,
                "view_profile_id": view_profile_id,
                "require_eligibility": require_eligibility,
            }
        }
    )


def enqueue_due_manual_interval_captures(
    session: Session,
    *,
    now: datetime | None = None,
) -> int:
    observed_at = now or utc_now()
    rows = session.scalars(
        select(RuntimeManualCaptureSession)
        .where(RuntimeManualCaptureSession.state == "running")
        .order_by(RuntimeManualCaptureSession.started_at.asc())
    ).all()
    enqueued = 0
    for row in rows:
        interval = max(int(row.interval_seconds or 1), 1)
        last_capture_at = row.last_capture_at or row.started_at
        if last_capture_at and (observed_at - _aware(last_capture_at)).total_seconds() < interval:
            continue
        if _manual_session_has_in_flight_capture(session, row.id):
            continue
        enqueue_manual_capture_request(
            session,
            channel_id=row.channel_id,
            capture_target_id=row.target_label_project_id,
            mode="interval",
            operator_user_id=row.operator_user_id,
            live_session_id=row.live_session_id,
            manual_capture_session_id=row.id,
            interval_seconds=row.interval_seconds,
            request_predictions=bool((row.metadata_json or {}).get("request_predictions", True)),
            capture_source=str(
                (row.metadata_json or {}).get("capture_source") or "LIVE_VIEW_FRAME"
            ),
            view_profile_id=(
                str((row.metadata_json or {}).get("view_profile_id"))
                if (row.metadata_json or {}).get("view_profile_id")
                else None
            ),
            require_eligibility=bool(
                (row.metadata_json or {}).get("require_eligibility", False)
            ),
        )
        enqueued += 1
    return enqueued


def enqueue_capture_requests_for_result(
    session: Session,
    *,
    result_record: ResultRecord,
    execution_result: RuntimeExecutionResult,
) -> list[RuntimeCaptureRequest]:
    rows: list[RuntimeCaptureRequest] = []
    if not execution_result.capture_intents:
        return rows
    channel = session.get(RuntimeChannel, result_record.channel_id)
    for entry in execution_result.capture_intents:
        intent = entry.get("intent") if isinstance(entry, dict) else None
        if not isinstance(intent, dict):
            continue
        node_key = str(
            (
                intent.get("metadata")
                if isinstance(intent.get("metadata"), dict)
                else {}
            ).get("node_key")
            or entry.get("node_key")
            or "capture"
        )
        rule, capture_rule_id = _resolve_capture_rule(
            session,
            channel_id=result_record.channel_id,
            pipeline_version_id=execution_result.pipeline_version_id,
            rule_key=node_key,
        )
        if str(intent.get("capture_mode") or "") == "temporal_sequence":
            rows.append(
                _add_capture_request(
                    session,
                    result_record=result_record,
                    channel=channel,
                    rule=rule,
                    capture_rule_id=capture_rule_id,
                    target_id="temporal_sequence_dataset",
                    target=None,
                    intent=intent,
                    entry=entry,
                    status="queued",
                    last_error=None,
                )
            )
            continue
        target_ids = _routing_targets(intent, rule)
        if not target_ids:
            rows.append(
                _add_capture_request(
                    session,
                    result_record=result_record,
                    channel=channel,
                    rule=rule,
                    capture_rule_id=capture_rule_id,
                    target_id="unrouted",
                    target=None,
                    intent=intent,
                    entry=entry,
                    status="failed_terminal",
                    last_error="Capture intent did not include any Label Studio target projects.",
                )
            )
            continue
        for target_id in target_ids:
            target, unavailable_error = _resolve_capture_target(session, target_id)
            status = (
                "queued"
                if target is not None and unavailable_error is None
                else "failed_terminal"
            )
            last_error = unavailable_error
            rows.append(
                _add_capture_request(
                    session,
                    result_record=result_record,
                    channel=channel,
                    rule=rule,
                    capture_rule_id=capture_rule_id,
                    target_id=target_id,
                    target=target,
                    intent=intent,
                    entry=entry,
                    status=status,
                    last_error=last_error,
                )
            )
    session.flush()
    return rows


def claim_due_capture_requests(
    session: Session,
    *,
    limit: int,
    now: datetime | None = None,
    processing_timeout_seconds: int = _DEFAULT_PROCESSING_TIMEOUT_SECONDS,
) -> list[RuntimeCaptureRequest]:
    observed_at = now or utc_now()
    stale_claim_cutoff = observed_at - timedelta(
        seconds=max(int(processing_timeout_seconds or 1), 1)
    )
    rows = session.scalars(
        select(RuntimeCaptureRequest)
        .where(
            or_(
                (
                    RuntimeCaptureRequest.status.in_(_RETRYABLE_STATUSES)
                    & or_(
                        RuntimeCaptureRequest.next_attempt_at.is_(None),
                        RuntimeCaptureRequest.next_attempt_at <= observed_at,
                    )
                ),
                (
                    (RuntimeCaptureRequest.status == "processing")
                    & or_(
                        RuntimeCaptureRequest.claimed_at.is_(None),
                        RuntimeCaptureRequest.claimed_at <= stale_claim_cutoff,
                    )
                ),
            ),
        )
        .order_by(RuntimeCaptureRequest.created_at.asc())
        .limit(max(min(limit, 500), 1))
    ).all()
    claimed: list[RuntimeCaptureRequest] = []
    for row in rows:
        if row.status == "processing":
            attempts = int(row.retry_count or 0) + 1
            row.retry_count = attempts
            if attempts >= int(row.max_attempts or 3):
                row.status = "failed_terminal"
                row.next_attempt_at = None
                row.last_error = (
                    "Capture delivery was abandoned while processing and exceeded "
                    "the retry limit."
                )
                update_manual_capture_from_delivery(
                    session,
                    row,
                    status="failed",
                    asset_uri=row.asset_uri,
                    digest=row.digest,
                    label_studio_task_ids=row.label_studio_task_ids or [],
                    source_frame_metadata=row.source_frame_metadata or {},
                    runtime_execution_id=(row.source_frame_metadata or {}).get(
                        "runtime_execution_id"
                    ),
                    error_summary=row.last_error,
                )
                continue
        row.status = "processing"
        row.claimed_at = observed_at
        row.last_error = None
        claimed.append(row)
    session.flush()
    return claimed


def list_runtime_capture_history(
    session: Session,
    *,
    channel_id: str | None = None,
    source_type: Literal["manual", "pipeline", "temporal_sample"] | None = None,
    limit: int = 50,
) -> tuple[list[RuntimeCaptureRequestSummary], int]:
    stmt = select(RuntimeCaptureRequest)
    count_stmt = select(func.count(RuntimeCaptureRequest.id))
    if channel_id:
        stmt = stmt.where(RuntimeCaptureRequest.channel_id == channel_id)
        count_stmt = count_stmt.where(RuntimeCaptureRequest.channel_id == channel_id)
    if source_type:
        stmt = stmt.where(RuntimeCaptureRequest.source_type == source_type)
        count_stmt = count_stmt.where(RuntimeCaptureRequest.source_type == source_type)
    rows = session.scalars(
        stmt.order_by(RuntimeCaptureRequest.created_at.desc()).limit(max(min(limit, 100), 1))
    ).all()
    total = int(session.scalar(count_stmt) or 0)
    return [runtime_capture_request_to_summary(row) for row in rows], total


def runtime_capture_request_to_summary(row: RuntimeCaptureRequest) -> RuntimeCaptureRequestSummary:
    return RuntimeCaptureRequestSummary(
        capture_request_id=row.id,
        channel_id=row.channel_id,
        capture_rule_id=row.capture_rule_id,
        result_record_id=row.result_record_id,
        manual_capture_session_id=row.manual_capture_session_id,
        manual_capture_request_id=row.manual_capture_request_id,
        target_label_project_id=row.target_label_project_id,
        target_display_name=row.target_project_title,
        label_studio_project_id=row.label_studio_project_id,
        source_type=row.source_type,  # type: ignore[arg-type]
        capture_mode=row.capture_mode,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        retry_count=int(row.retry_count or 0),
        max_attempts=int(row.max_attempts or 3),
        queued_at=_iso(row.queued_at),
        claimed_at=_iso(row.claimed_at),
        submitted_at=_iso(row.submitted_at),
        delivered_at=_iso(row.delivered_at),
        next_attempt_at=_iso(row.next_attempt_at),
        rate_limited_until=_iso(row.rate_limited_until),
        asset_uri=row.asset_uri,
        digest=row.digest,
        roi=row.roi or {},
        label_studio_task_ids=row.label_studio_task_ids or [],
        duplicate_of_request_id=row.duplicate_of_request_id,
        last_error=row.last_error,
        metadata=row.metadata_json or {},
        created_at=_iso(row.created_at),
    )


def update_manual_capture_from_delivery(
    session: Session,
    row: RuntimeCaptureRequest,
    *,
    status: Literal["success", "failed"],
    asset_uri: str | None = None,
    digest: str | None = None,
    label_studio_task_ids: list[int] | None = None,
    source_frame_metadata: dict[str, Any] | None = None,
    runtime_execution_id: str | None = None,
    error_summary: str | None = None,
) -> None:
    if not row.manual_capture_request_id:
        return
    manual = session.get(RuntimeManualCaptureRequestRecord, row.manual_capture_request_id)
    if manual is None:
        return
    previous_status = manual.status
    manual.status = status
    manual.asset_uri = asset_uri
    manual.digest = digest
    manual.label_studio_task_ids = label_studio_task_ids or []
    manual.source_frame_metadata = source_frame_metadata or manual.source_frame_metadata or {}
    manual.runtime_execution_id = runtime_execution_id
    manual.error_summary = error_summary
    manual.metadata_json = {
        **(manual.metadata_json or {}),
        "runtime_capture_request_id": row.id,
        "delivery_status": row.status,
    }
    if manual.manual_capture_session_id:
        session_row = session.get(RuntimeManualCaptureSession, manual.manual_capture_session_id)
        if session_row is not None:
            session_row.last_capture_request_id = manual.id
            session_row.last_capture_at = manual.captured_at
            if status == "success":
                session_row.last_success_at = manual.captured_at
                session_row.last_error = None
            elif previous_status != "failed":
                session_row.failure_count = int(session_row.failure_count or 0) + 1
                session_row.last_failure_at = manual.captured_at
                session_row.last_error = error_summary


def _add_capture_request(
    session: Session,
    *,
    result_record: ResultRecord,
    channel: RuntimeChannel | None,
    rule: CaptureRule | None,
    capture_rule_id: str,
    target_id: str,
    target: LabelProject | None,
    intent: dict[str, Any],
    entry: dict[str, Any],
    status: str,
    last_error: str | None,
) -> RuntimeCaptureRequest:
    asset_payload = _asset_payload(intent)
    digest = _asset_payload_digest(asset_payload)
    metadata = dict(intent.get("metadata") or {})
    duplicate_policy = _dedupe_policy(rule, metadata)
    dedupe_key = (
        _dedupe_key(
            channel_id=result_record.channel_id,
            capture_rule_id=capture_rule_id,
            target_id=target.id if target is not None else target_id,
            digest=digest,
        )
        if digest
        else None
    )
    duplicate = (
        _find_duplicate(session, dedupe_key)
        if dedupe_key and duplicate_policy != "keep"
        else None
    )
    if duplicate is not None and status == "queued":
        status = "skipped_duplicate"
        last_error = f"Duplicate capture suppressed; original request is {duplicate.id}."
    row = RuntimeCaptureRequest(
        id=generate_prefixed_id("rcr"),
        channel_id=result_record.channel_id,
        capture_rule_id=capture_rule_id,
        result_record_id=result_record.id,
        target_label_project_id=target.id if target is not None else target_id,
        label_studio_project_id=target.label_studio_project_id if target is not None else None,
        target_project_title=target.title if target is not None else None,
        source_type=(
            "temporal_sample"
            if str(intent.get("capture_mode") or "") == "temporal_sequence"
            else "pipeline"
        ),
        capture_mode=str(intent.get("capture_mode") or "full_frame"),
        asset_payload=asset_payload,
        roi=_roi_metadata(asset_payload),
        digest=digest,
        dedupe_key=dedupe_key,
        duplicate_of_request_id=duplicate.id if duplicate is not None else None,
        source_frame_metadata={
            "result_record_id": result_record.id,
            "runtime_execution_id": result_record.runtime_execution_id,
            "trigger_endpoint_id": result_record.trigger_endpoint_id,
            "correlation_id": result_record.correlation_id,
            "channel_name": channel.name if channel is not None else None,
            "technical_place": channel.technical_place if channel is not None else None,
        },
        capture_intent={"entry": entry, "intent": intent},
        status=status,
        retry_count=0,
        max_attempts=(
            20
            if str(intent.get("capture_mode") or "") == "temporal_sequence"
            else _max_attempts(rule)
        ),
        queued_at=utc_now(),
        last_error=last_error,
        metadata_json={
            **metadata,
            "node_key": entry.get("node_key") or metadata.get("node_key"),
            "node_type": entry.get("node_type"),
            "condition": intent.get("condition"),
            "duplicate_policy": duplicate_policy,
            "rate_limit": rule.rate_limit if rule is not None else {},
            **(
                {
                    "retention_days": metadata.get("retention_days"),
                    "annotation_schema_id": metadata.get("annotation_schema_id"),
                    "dataset_id": metadata.get("dataset_id"),
                    "auto_import_enabled": metadata.get("auto_import_enabled", True),
                    "import_sample_every_n": metadata.get("import_sample_every_n", 1),
                    "import_maximum_per_hour": metadata.get(
                        "import_maximum_per_hour", 120
                    ),
                    "ground_truth_status": metadata.get("ground_truth_status"),
                }
                if str(intent.get("capture_mode") or "") == "temporal_sequence"
                else {}
            ),
        },
    )
    session.add(row)
    return row


def _resolve_capture_rule(
    session: Session,
    *,
    channel_id: str,
    pipeline_version_id: str,
    rule_key: str,
) -> tuple[CaptureRule | None, str]:
    rows = session.scalars(
        select(CaptureRule).where(
            CaptureRule.channel_id == channel_id,
            CaptureRule.rule_key == rule_key,
            or_(
                CaptureRule.pipeline_version_id == pipeline_version_id,
                CaptureRule.pipeline_version_id.is_(None),
            ),
        )
    ).all()
    if rows:
        exact = [row for row in rows if row.pipeline_version_id == pipeline_version_id]
        chosen = exact[0] if exact else rows[0]
        return chosen, chosen.id
    return None, f"node_{rule_key[:59]}"


def _routing_targets(intent: dict[str, Any], rule: CaptureRule | None) -> list[str]:
    if rule is not None and rule.target_label_project_ids:
        return [str(item) for item in rule.target_label_project_ids if str(item).strip()]
    raw = intent.get("target_project_ids") or []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return [str(item) for item in raw if str(item).strip()]


def _resolve_capture_target(
    session: Session,
    target_id: str,
) -> tuple[LabelProject | None, str | None]:
    row = session.get(LabelProject, target_id)
    if row is None or row.deleted_at is not None:
        return None, (
            "Label Studio capture target does not exist. Select a synced platform "
            "project from the Label Studio capture target list."
        )
    if row.status not in _SELECTABLE_PROJECT_STATES:
        return None, (
            f"Label Studio project {row.title} is not available for runtime capture "
            f"(status: {row.status})."
        )
    return row, None


def _manual_session_has_in_flight_capture(session: Session, session_id: str) -> bool:
    return bool(
        session.scalar(
            select(RuntimeCaptureRequest.id)
            .where(
                RuntimeCaptureRequest.manual_capture_session_id == session_id,
                RuntimeCaptureRequest.status.in_(
                    {"queued", "processing", "rate_limited", "failed_retryable"}
                ),
            )
            .limit(1)
        )
    )


def _asset_payload(intent: dict[str, Any]) -> dict[str, Any]:
    asset = intent.get("asset")
    return dict(asset) if isinstance(asset, dict) else {}


def _asset_payload_digest(asset_payload: dict[str, Any]) -> str | None:
    image_base64 = _find_image_base64(asset_payload)
    if image_base64:
        try:
            return hashlib.sha256(base64.b64decode(image_base64.encode("ascii"))).hexdigest()
        except ValueError:
            pass
    if not asset_payload:
        return None
    encoded = json.dumps(asset_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _find_image_base64(payload: dict[str, Any]) -> str | None:
    value = payload.get("image_base64")
    if isinstance(value, str) and value:
        return value
    frame = payload.get("frame")
    if isinstance(frame, dict):
        value = frame.get("image_base64")
        if isinstance(value, str) and value:
            return value
    return None


def _roi_metadata(asset_payload: dict[str, Any]) -> dict[str, Any]:
    if "box" not in asset_payload:
        return {}
    return {
        "roi_key": asset_payload.get("roi_key"),
        "box": asset_payload.get("box"),
        "label": asset_payload.get("label"),
        "source_region_key": asset_payload.get("source_region_key"),
        "metadata": asset_payload.get("metadata") or {},
    }


def _dedupe_policy(rule: CaptureRule | None, metadata: dict[str, Any]) -> str:
    configured = metadata.get("duplicate_policy")
    if isinstance(configured, str) and configured:
        return configured
    if rule is not None:
        raw = rule.dedupe_policy or {}
        policy = raw.get("policy") or raw.get("mode")
        if isinstance(policy, str) and policy:
            return policy
    return "suppress_by_digest"


def _max_attempts(rule: CaptureRule | None) -> int:
    if rule is not None:
        raw = rule.rate_limit or {}
        max_attempts = raw.get("max_attempts") or raw.get("retry_max_attempts")
        if isinstance(max_attempts, int) and max_attempts > 0:
            return min(max_attempts, 20)
    return 3


def _dedupe_key(
    *,
    channel_id: str,
    capture_rule_id: str,
    target_id: str,
    digest: str,
) -> str:
    raw = f"{channel_id}|{capture_rule_id}|{target_id}|{digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _find_duplicate(session: Session, dedupe_key: str | None) -> RuntimeCaptureRequest | None:
    if not dedupe_key:
        return None
    return session.scalar(
        select(RuntimeCaptureRequest)
        .where(
            RuntimeCaptureRequest.dedupe_key == dedupe_key,
            RuntimeCaptureRequest.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(RuntimeCaptureRequest.created_at.asc())
        .limit(1)
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _iso(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)
