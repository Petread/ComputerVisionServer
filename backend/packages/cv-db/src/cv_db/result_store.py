from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cv_contracts import ResponseMeta
from cv_contracts.runtime import (
    RuntimeResultDetailResponse,
    RuntimeResultListResponse,
    RuntimeResultRecordDetail,
    RuntimeResultRecordSummary,
    RuntimeStemOrientationReview,
    RuntimeStemOrientationReviewCreateRequest,
)
from cv_core import generate_prefixed_id
from cv_status import RuntimeMode
from cv_utils import utc_now_iso
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from cv_db.models import ResultRecord, RuntimeChannel, StemOrientationReview


@dataclass(frozen=True)
class RuntimeResultQuery:
    channel_id: str | None = None
    pipeline_version_id: str | None = None
    mode: RuntimeMode | None = None
    status: str | None = None
    result_type: str | None = None
    completed_from: datetime | None = None
    completed_to: datetime | None = None
    search: str | None = None
    limit: int = 50
    offset: int = 0


def list_runtime_result_records(
    session: Session,
    query: RuntimeResultQuery,
) -> tuple[list[RuntimeResultRecordSummary], int]:
    stmt = _filtered_result_query(query).order_by(ResultRecord.completed_at.desc()).limit(query.limit).offset(query.offset)
    rows = session.execute(stmt).all()
    count_stmt = _filtered_result_count_query(query)
    total = int(session.scalar(count_stmt) or 0)
    return [result_record_to_summary(row, channel) for row, channel in rows], total


def list_runtime_results_response(
    session: Session,
    *,
    query: RuntimeResultQuery,
    meta: ResponseMeta,
) -> RuntimeResultListResponse:
    items, total = list_runtime_result_records(session, query)
    return RuntimeResultListResponse(
        items=items,
        total=total,
        limit=query.limit,
        offset=query.offset,
        meta=meta,
    )


def get_runtime_result_detail(
    session: Session,
    result_record_id: str,
) -> RuntimeResultRecordDetail | None:
    row = session.get(ResultRecord, result_record_id)
    if row is None or not row.history_retained:
        return None
    channel = session.get(RuntimeChannel, row.channel_id)
    return result_record_to_detail(row, channel)


def get_latest_runtime_result_detail(
    session: Session,
    *,
    channel_id: str,
    mode: RuntimeMode | None = None,
) -> RuntimeResultRecordDetail | None:
    stmt = select(ResultRecord).where(
        ResultRecord.channel_id == channel_id,
        ResultRecord.history_retained.is_(True),
    )
    if mode is not None:
        stmt = stmt.where(ResultRecord.mode == mode.value)
    row = session.scalar(
        stmt.order_by(ResultRecord.completed_at.desc(), ResultRecord.id.desc()).limit(1)
    )
    if row is None:
        return None
    channel = session.get(RuntimeChannel, row.channel_id)
    return result_record_to_detail(row, channel)


def runtime_result_detail_response(
    item: RuntimeResultRecordDetail,
    *,
    meta: ResponseMeta,
) -> RuntimeResultDetailResponse:
    return RuntimeResultDetailResponse(item=item, meta=meta)


def list_stem_orientation_reviews(
    session: Session,
    *,
    result_record_id: str,
) -> list[RuntimeStemOrientationReview]:
    rows = session.scalars(
        select(StemOrientationReview)
        .where(StemOrientationReview.result_record_id == result_record_id)
        .order_by(StemOrientationReview.revision.desc())
    ).all()
    return [_stem_orientation_review_to_contract(row) for row in rows]


def create_stem_orientation_review(
    session: Session,
    *,
    result_record_id: str,
    payload: RuntimeStemOrientationReviewCreateRequest,
    reviewer_user_id: str,
    reviewer_username: str,
) -> RuntimeStemOrientationReview:
    result_row = session.scalar(
        select(ResultRecord)
        .where(
            ResultRecord.id == result_record_id,
            ResultRecord.history_retained.is_(True),
        )
        .with_for_update()
    )
    if result_row is None:
        raise LookupError(f"Runtime result {result_record_id} does not exist.")
    orientation = stem_orientation_result_from_record(result_row)
    if orientation is None or not _stem_orientation_decision_ready(orientation):
        raise ValueError(
            "Stem-orientation reviews require a retained, decision-ready "
            "stem.orientation.v2 or stem.orientation.v3 result."
        )
    if orientation.get("schema_version") == "stem.orientation.v3":
        if payload.target_frame_start is not None or payload.target_frame_end is not None:
            raise ValueError(
                "Curvature-minimum technical reviews must not select a subjective target frame."
            )
        if payload.final_position != "not_assessed":
            raise ValueError(
                "Curvature-minimum technical reviews only capture plausibility and error causes."
            )
        if (
            payload.direction_assessment != "unclear"
            or payload.angle_assessment != "not_assessable"
            or payload.bow_relevance != "unclear"
            or payload.comment.strip()
        ):
            raise ValueError(
                "Shadow feedback only accepts plausible, implausible, or uncertain plus "
                "an optional technical error reason."
            )
    elif payload.final_position == "not_assessed":
        raise ValueError("Legacy orientation reviews require an end-position assessment.")
    previous = session.scalar(
        select(StemOrientationReview)
        .where(StemOrientationReview.result_record_id == result_record_id)
        .order_by(StemOrientationReview.revision.desc())
        .limit(1)
    )
    row = StemOrientationReview(
        id=generate_prefixed_id("sor"),
        result_record_id=result_record_id,
        revision=(previous.revision + 1) if previous is not None else 1,
        review_schema_version="stem.orientation.review.v1",
        proposal_plausibility=payload.proposal_plausibility,
        direction_assessment=payload.direction_assessment,
        angle_assessment=payload.angle_assessment,
        bow_relevance=payload.bow_relevance,
        final_position=payload.final_position,
        target_frame_start=payload.target_frame_start,
        target_frame_end=payload.target_frame_end,
        comment=payload.comment.strip(),
        special_case=payload.special_case.strip(),
        supersedes_review_id=previous.id if previous is not None else None,
        reviewer_user_id=reviewer_user_id,
        reviewer_username=reviewer_username,
    )
    session.add(row)
    session.flush()
    return _stem_orientation_review_to_contract(row)


def stem_orientation_result_from_record(row: ResultRecord) -> dict[str, Any] | None:
    sections = _sections(row)
    candidates: list[object] = [sections["trigger_response"]]
    for entry in sections["derived_outputs"]:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if isinstance(payload, dict):
            candidates.append(payload.get("result"))
    legacy = dict(row.result or {})
    for entry in _list(legacy.get("persisted_results")):
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if isinstance(payload, dict):
            candidates.append(payload.get("result"))
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("schema_version") in {
            "stem.orientation.v2",
            "stem.orientation.v3",
        }:
            return dict(candidate)
    return None


def _stem_orientation_decision_ready(result: dict[str, Any]) -> bool:
    decision = result.get("decision")
    return bool(result.get("decision_ready")) or (
        isinstance(decision, dict) and bool(decision.get("ready"))
    )


def _stem_orientation_review_to_contract(
    row: StemOrientationReview,
) -> RuntimeStemOrientationReview:
    return RuntimeStemOrientationReview(
        schema_version=row.review_schema_version,  # type: ignore[arg-type]
        review_id=row.id,
        result_record_id=row.result_record_id,
        revision=row.revision,
        proposal_plausibility=row.proposal_plausibility,  # type: ignore[arg-type]
        direction_assessment=row.direction_assessment,  # type: ignore[arg-type]
        angle_assessment=row.angle_assessment,  # type: ignore[arg-type]
        bow_relevance=row.bow_relevance,  # type: ignore[arg-type]
        final_position=row.final_position,  # type: ignore[arg-type]
        target_frame_start=row.target_frame_start,
        target_frame_end=row.target_frame_end,
        comment=row.comment,
        special_case=row.special_case,
        supersedes_review_id=row.supersedes_review_id,
        reviewer_user_id=row.reviewer_user_id,
        reviewer_username=row.reviewer_username,
        created_at=_iso(row.created_at) or utc_now_iso(),
    )


def result_record_to_summary(
    row: ResultRecord,
    channel: RuntimeChannel | None = None,
) -> RuntimeResultRecordSummary:
    sections = _sections(row)
    return RuntimeResultRecordSummary(
        result_record_id=row.id,
        channel_id=row.channel_id,
        channel_name=channel.name if channel is not None else None,
        technical_place=channel.technical_place if channel is not None else None,
        pipeline_version_id=row.pipeline_version_id or _legacy_pipeline_version_id(row),
        runtime_execution_id=row.runtime_execution_id,
        trigger_endpoint_id=row.trigger_endpoint_id,
        correlation_id=row.correlation_id,
        source_timestamp=_iso(row.source_timestamp),
        completed_at=_iso(row.completed_at) or utc_now_iso(),
        mode=_mode(row),
        status=row.status,  # type: ignore[arg-type]
        result_type=_result_type(row),
        schema_version=row.schema_version,
        result=dict(row.result or {}),
        trigger_response=sections["trigger_response"],
        derived_preview=_derived_preview(sections["derived_outputs"]),
        inference_output_count=len(sections["normalized_inference_outputs"]),
        derived_output_count=len(sections["derived_outputs"]),
        overlay_primitive_count=len(sections["overlay_primitives"]),
        timing=dict(row.timing or {}),
        model_routes=list(row.model_routes or []),
    )


def result_record_to_detail(
    row: ResultRecord,
    channel: RuntimeChannel | None = None,
) -> RuntimeResultRecordDetail:
    summary = result_record_to_summary(row, channel)
    sections = _sections(row)
    return RuntimeResultRecordDetail(
        **summary.model_dump(mode="json"),
        raw_inference_outputs=sections["raw_inference_outputs"],
        normalized_inference_outputs=sections["normalized_inference_outputs"],
        derived_outputs=sections["derived_outputs"],
        overlay_primitives=sections["overlay_primitives"],
        runtime_metadata=sections["runtime_metadata"],
        execution_result=dict(row.result or {}),
    )


def _filtered_result_query(query: RuntimeResultQuery):
    stmt = select(ResultRecord, RuntimeChannel).outerjoin(
        RuntimeChannel,
        RuntimeChannel.id == ResultRecord.channel_id,
    )
    return _apply_filters(stmt, query)


def _filtered_result_count_query(query: RuntimeResultQuery):
    stmt = select(func.count(ResultRecord.id)).outerjoin(
        RuntimeChannel,
        RuntimeChannel.id == ResultRecord.channel_id,
    )
    return _apply_filters(stmt, query)


def _apply_filters(stmt, query: RuntimeResultQuery):
    stmt = stmt.where(ResultRecord.history_retained.is_(True))
    if query.channel_id:
        stmt = stmt.where(ResultRecord.channel_id == query.channel_id)
    if query.pipeline_version_id:
        stmt = stmt.where(ResultRecord.pipeline_version_id == query.pipeline_version_id)
    if query.mode:
        stmt = stmt.where(ResultRecord.mode == query.mode.value)
    if query.status:
        stmt = stmt.where(ResultRecord.status == query.status)
    if query.result_type:
        stmt = stmt.where(ResultRecord.result_type == query.result_type)
    if query.completed_from:
        stmt = stmt.where(ResultRecord.completed_at >= _aware(query.completed_from))
    if query.completed_to:
        stmt = stmt.where(ResultRecord.completed_at <= _aware(query.completed_to))
    if query.search:
        pattern = f"%{query.search.strip()}%"
        stmt = stmt.where(
            or_(
                ResultRecord.correlation_id.ilike(pattern),
                RuntimeChannel.name.ilike(pattern),
                RuntimeChannel.technical_place.ilike(pattern),
                cast(ResultRecord.derived_outputs, String).ilike(pattern),
                cast(ResultRecord.trigger_response, String).ilike(pattern),
            )
        )
    return stmt


def _sections(row: ResultRecord) -> dict[str, Any]:
    legacy = dict(row.result or {})
    raw = _list(row.raw_inference_outputs) or _list(legacy.get("raw_inference_outputs"))
    normalized = _list(row.normalized_inference_outputs) or _list(legacy.get("normalized_inference_outputs"))
    derived = _list(row.derived_outputs) or _list(legacy.get("derived_outputs"))
    overlay = _list(row.overlay_primitives)
    if not overlay:
        overlay = _overlay_primitives_from_legacy(legacy.get("overlay_outputs"))
    trigger_response = dict(row.trigger_response or {})
    if not trigger_response and isinstance(legacy.get("final_response"), dict):
        trigger_response = dict(legacy["final_response"])
    runtime_metadata = dict(row.runtime_metadata or {})
    runtime_metadata.setdefault("runtime_execution_id", row.runtime_execution_id)
    runtime_metadata.setdefault("trigger_endpoint_id", row.trigger_endpoint_id)
    runtime_metadata.setdefault("correlation_id", row.correlation_id)
    runtime_metadata.setdefault("pipeline_version_id", row.pipeline_version_id or _legacy_pipeline_version_id(row))
    runtime_metadata.setdefault("mode", _mode(row).value)
    return {
        "raw_inference_outputs": raw,
        "normalized_inference_outputs": normalized,
        "derived_outputs": derived,
        "overlay_primitives": overlay,
        "trigger_response": trigger_response,
        "runtime_metadata": runtime_metadata,
    }


def _derived_preview(derived_outputs: list[dict[str, Any]]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    for entry in derived_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("payload_type")
        if payload_type == "measurements":
            for item in payload.get("measurements") or []:
                if isinstance(item, dict) and item.get("key"):
                    preview[str(item["key"])] = item.get("value")
        elif payload_type == "result":
            result = payload.get("result")
            if isinstance(result, dict):
                preview.update(result)
        elif payload_type == "feature_measurements":
            measurements = payload.get("measurements")
            if isinstance(measurements, list):
                preview["feature_measurement_count"] = len(measurements)
                for item in measurements:
                    if isinstance(item, dict):
                        basis = item.get("basis")
                        feature_type = item.get("feature_type")
                        if basis and feature_type:
                            preview.setdefault(f"{feature_type}_{basis}_px", item.get("value_px"))
                            if item.get("value_mm") is not None:
                                preview.setdefault(f"{feature_type}_{basis}_mm", item.get("value_mm"))
        elif payload_type == "clearance_zones":
            zones = payload.get("zones")
            if isinstance(zones, list):
                preview["clearance_zone_count"] = len(zones)
        elif payload_type == "free_region":
            free_region = payload.get("free_region")
            if isinstance(free_region, dict):
                preview["free_region_found"] = free_region.get("found")
                preview["distance_px"] = free_region.get("distance_from_origin_px")
                preview["distance_mm"] = free_region.get("distance_from_origin_mm")
        elif payload_type == "quality_decision":
            decision = payload.get("decision")
            if isinstance(decision, dict):
                preview["status"] = decision.get("status")
                preview["result_label"] = decision.get("result_label")
        elif payload_type == "events":
            events = payload.get("events")
            if isinstance(events, list):
                preview["events"] = len(events)
    return preview


def _overlay_primitives_from_legacy(value: object) -> list[dict[str, Any]]:
    primitives: list[dict[str, Any]] = []
    for entry in _list(value):
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        for primitive in payload.get("primitives") or []:
            if isinstance(primitive, dict):
                primitives.append(primitive)
    return primitives


def _legacy_pipeline_version_id(row: ResultRecord) -> str | None:
    payload = row.result or {}
    value = payload.get("pipeline_version_id") if isinstance(payload, dict) else None
    return str(value) if value else None


def _result_type(row: ResultRecord) -> str:
    if row.result_type:
        return row.result_type
    if row.status == "timeout":
        return "timeout"
    if row.trigger_endpoint_id:
        return "trigger_execution"
    if _mode(row) == RuntimeMode.REALTIME:
        return "realtime_frame"
    return "pipeline_execution"


def _mode(row: ResultRecord) -> RuntimeMode:
    try:
        return RuntimeMode(row.mode)
    except (TypeError, ValueError):
        return RuntimeMode.TRIGGER if row.trigger_endpoint_id else RuntimeMode.REALTIME


def _list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


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
