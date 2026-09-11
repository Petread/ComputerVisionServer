from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import floor
from statistics import mean
from typing import Any

from cv_contracts import ResponseMeta
from cv_contracts.runtime import (
    RuntimeStemShadowDriftSummary,
    RuntimeStemShadowEvaluationResponse,
    RuntimeStemShadowFeedbackSummary,
    RuntimeStemShadowHistogramBin,
    RuntimeStemShadowLatencySummary,
    RuntimeStemShadowMetricCount,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cv_db.models import PipelineNode, PipelineVersion, ResultRecord, StemOrientationReview
from cv_db.result_store import stem_orientation_result_from_record


@dataclass(frozen=True)
class RuntimeStemShadowEvaluationQuery:
    completed_from: datetime | None = None
    completed_to: datetime | None = None
    maximum_sessions: int = 2_000


def stem_shadow_evaluation_response(
    session: Session,
    *,
    pipeline_version_id: str,
    query: RuntimeStemShadowEvaluationQuery,
    meta: ResponseMeta,
) -> RuntimeStemShadowEvaluationResponse:
    if session.get(PipelineVersion, pipeline_version_id) is None:
        raise LookupError(f"Pipeline version {pipeline_version_id} does not exist.")
    solver = session.scalar(
        select(PipelineNode)
        .where(
            PipelineNode.pipeline_version_id == pipeline_version_id,
            PipelineNode.node_type == "analysis.stem_curvature_phase_solver",
        )
        .order_by(PipelineNode.created_at.asc(), PipelineNode.id.asc())
        .limit(1)
    )
    config = dict(solver.config or {}) if solver is not None else {}
    shadow_enabled = bool(config.get("shadow_mode_enabled", False))

    filters = [
        ResultRecord.pipeline_version_id == pipeline_version_id,
        ResultRecord.history_retained.is_(True),
        ResultRecord.result_type == "stem_orientation",
    ]
    if query.completed_from is not None:
        filters.append(ResultRecord.completed_at >= _aware(query.completed_from))
    if query.completed_to is not None:
        filters.append(ResultRecord.completed_at <= _aware(query.completed_to))
    sessions_available = int(
        session.scalar(select(func.count(ResultRecord.id)).where(*filters)) or 0
    )
    newest_rows = list(
        session.scalars(
            select(ResultRecord)
            .where(*filters)
            .order_by(ResultRecord.completed_at.desc(), ResultRecord.id.desc())
            .limit(query.maximum_sessions)
        ).all()
    )
    rows = list(reversed(newest_rows))
    sessions: list[tuple[ResultRecord, dict[str, Any]]] = []
    for row in rows:
        orientation = stem_orientation_result_from_record(row)
        if not isinstance(orientation, dict):
            continue
        if orientation.get("schema_version") != "stem.orientation.v3":
            continue
        if not (shadow_enabled or orientation.get("shadow_mode") is True):
            continue
        sessions.append((row, orientation))

    total = len(sessions)
    decision_counter = Counter(
        str(result.get("decision") or "UNKNOWN") for _, result in sessions
    )
    reason_counter: Counter[str] = Counter()
    for _, result in sessions:
        reason_counter.update(
            {
                str(code).strip()
                for code in result.get("reason_codes") or []
                if str(code).strip()
            }
        )
    angles = [
        value
        for _, result in sessions
        if (value := _number(result.get("recommended_rotation_deg_signed"))) is not None
    ]
    confidences = [
        value
        for _, result in sessions
        if (value := _bounded_confidence(result.get("overall_confidence"))) is not None
    ]
    latencies = [
        value
        for _, result in sessions
        if (value := _decision_latency_ms(result)) is not None
    ]
    deadline_misses = sum(
        1 for _, result in sessions if result.get("deadline_met") is False
    )
    feedback = _feedback_summary(session, sessions)
    drift = _drift_summary(sessions, config=config, total=total)
    completed_from = _iso(sessions[0][0].completed_at) if sessions else None
    completed_to = _iso(sessions[-1][0].completed_at) if sessions else None

    decision_distribution = _metric_counts(
        decision_counter,
        total=total,
        required_keys=("ROTATE", "NO_ROTATION", "UNCERTAIN"),
    )
    reason_distribution = _metric_counts(reason_counter, total=total)
    angle_histogram = _histogram(
        angles,
        boundaries=(-90.0, -60.0, -30.0, -15.0, -5.0, 5.0, 15.0, 30.0, 60.0, 90.0),
    )
    confidence_histogram = _histogram(
        confidences,
        boundaries=tuple(index / 10.0 for index in range(11)),
    )
    latency = RuntimeStemShadowLatencySummary(
        measured_count=len(latencies),
        missing_count=total - len(latencies),
        minimum_ms=min(latencies) if latencies else None,
        median_ms=_percentile(latencies, 0.5),
        p90_ms=_percentile(latencies, 0.9),
        p95_ms=_percentile(latencies, 0.95),
        maximum_ms=max(latencies) if latencies else None,
    )
    report_json = {
        "schema_version": "stem.shadow-evaluation-report.v1",
        "pipeline_version_id": pipeline_version_id,
        "shadow_mode_enabled": shadow_enabled,
        "period": {"completed_from": completed_from, "completed_to": completed_to},
        "sessions": {
            "available": sessions_available,
            "analyzed": total,
            "maximum_sessions": query.maximum_sessions,
            "limit_reached": sessions_available > query.maximum_sessions,
        },
        "decision_distribution": [item.model_dump(mode="json") for item in decision_distribution],
        "angle_histogram": [item.model_dump(mode="json") for item in angle_histogram],
        "confidence_histogram": [
            item.model_dump(mode="json") for item in confidence_histogram
        ],
        "confidence_percentiles": _confidence_percentiles(confidences),
        "reason_code_distribution": [
            item.model_dump(mode="json") for item in reason_distribution
        ],
        "decision_latency": latency.model_dump(mode="json"),
        "drift": drift.model_dump(mode="json"),
        "feedback": feedback.model_dump(mode="json"),
    }
    return RuntimeStemShadowEvaluationResponse(
        pipeline_version_id=pipeline_version_id,
        shadow_mode_enabled=shadow_enabled,
        evaluation_status=(
            "INACTIVE" if not shadow_enabled else "NO_SESSIONS" if not total else "ACTIVE"
        ),
        sessions_available=sessions_available,
        sessions_analyzed=total,
        excluded_session_count=max(0, sessions_available - total),
        maximum_sessions=query.maximum_sessions,
        limit_reached=sessions_available > query.maximum_sessions,
        completed_from=completed_from,
        completed_to=completed_to,
        decision_distribution=decision_distribution,
        no_rotation_share=_share(decision_counter["NO_ROTATION"], total),
        uncertain_share=_share(decision_counter["UNCERTAIN"], total),
        deadline_miss_share=_share(deadline_misses, total),
        angle_histogram=angle_histogram,
        angle_missing_count=total - len(angles),
        confidence_histogram=confidence_histogram,
        confidence_percentiles=_confidence_percentiles(confidences),
        reason_code_distribution=reason_distribution,
        decision_latency=latency,
        drift=drift,
        feedback=feedback,
        report_json=report_json,
        report_markdown=_markdown_report(report_json),
        meta=meta,
    )


def _feedback_summary(
    session: Session,
    sessions: Sequence[tuple[ResultRecord, dict[str, Any]]],
) -> RuntimeStemShadowFeedbackSummary:
    result_ids = [row.id for row, _ in sessions]
    if not result_ids:
        return RuntimeStemShadowFeedbackSummary()
    reviews = session.scalars(
        select(StemOrientationReview)
        .where(StemOrientationReview.result_record_id.in_(result_ids))
        .order_by(
            StemOrientationReview.result_record_id.asc(),
            StemOrientationReview.revision.desc(),
        )
    ).all()
    latest: dict[str, StemOrientationReview] = {}
    for review in reviews:
        latest.setdefault(review.result_record_id, review)
    rating_names = {"yes": "plausible", "no": "implausible", "unclear": "uncertain"}
    ratings = Counter(
        rating_names.get(review.proposal_plausibility, "uncertain")
        for review in latest.values()
    )
    technical_reasons = Counter(
        review.special_case.strip()
        for review in latest.values()
        if review.special_case and review.special_case.strip()
    )
    return RuntimeStemShadowFeedbackSummary(
        reviewed_session_count=len(latest),
        ratings=_metric_counts(
            ratings,
            total=len(latest),
            required_keys=("plausible", "implausible", "uncertain"),
        ),
        technical_error_reasons=_metric_counts(technical_reasons, total=len(latest)),
    )


def _drift_summary(
    sessions: Sequence[tuple[ResultRecord, dict[str, Any]]],
    *,
    config: Mapping[str, Any],
    total: int,
) -> RuntimeStemShadowDriftSummary:
    process_hashes = [
        str(result.get("process_calibration_hash") or "MISSING")
        for _, result in sessions
    ]
    rotation_hashes = [
        str(result.get("rotation_calibration_hash") or "MISSING")
        for _, result in sessions
    ]
    drift_reasons: Counter[str] = Counter()
    tokens = ("CAMERA", "CALIBRATION", "RESOLUTION", "REPROJECTION", "HOMOGRAPHY", "DRIFT", "SHIFT")
    for _, result in sessions:
        drift_reasons.update(
            {
                str(code)
                for code in result.get("reason_codes") or []
                if any(token in str(code).upper() for token in tokens)
            }
        )

    process_changes = _change_count(process_hashes)
    rotation_changes = _change_count(rotation_hashes)
    configured_window = _bounded_integer(
        config.get("shadow_drift_window_sessions"), default=50, minimum=10, maximum=1000
    )
    window = min(configured_window, total // 2)
    baseline: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    if window >= 10:
        baseline = [result for _, result in sessions[:window]]
        recent = [result for _, result in sessions[-window:]]
    baseline_confidence = _mean_confidence(baseline)
    recent_confidence = _mean_confidence(recent)
    confidence_delta = (
        recent_confidence - baseline_confidence
        if baseline_confidence is not None and recent_confidence is not None
        else None
    )
    baseline_uncertain = _uncertain_share(baseline)
    recent_uncertain = _uncertain_share(recent)
    uncertain_delta = (
        recent_uncertain - baseline_uncertain
        if baseline_uncertain is not None and recent_uncertain is not None
        else None
    )
    confidence_threshold = _bounded_number(
        config.get("shadow_confidence_drop_threshold"), default=0.15, minimum=0, maximum=1
    )
    uncertain_threshold = _bounded_number(
        config.get("shadow_uncertain_rate_increase_threshold"),
        default=0.15,
        minimum=0,
        maximum=1,
    )
    signals: list[str] = []
    if process_changes:
        signals.append("PROCESS_CALIBRATION_IDENTITY_CHANGED")
    if rotation_changes:
        signals.append("ROTATION_CALIBRATION_IDENTITY_CHANGED")
    if drift_reasons:
        signals.append("CAMERA_OR_CALIBRATION_REASON_CODES_PRESENT")
    if confidence_delta is not None and confidence_delta <= -confidence_threshold:
        signals.append("RECENT_CONFIDENCE_DROPPED")
    if uncertain_delta is not None and uncertain_delta >= uncertain_threshold:
        signals.append("RECENT_UNCERTAIN_SHARE_INCREASED")
    status = (
        "DRIFT_SIGNAL"
        if signals
        else "NO_DRIFT_DETECTED"
        if baseline and recent
        else "INSUFFICIENT_DATA"
    )
    return RuntimeStemShadowDriftSummary(
        status=status,
        baseline_count=len(baseline),
        recent_count=len(recent),
        baseline_mean_confidence=baseline_confidence,
        recent_mean_confidence=recent_confidence,
        confidence_delta=confidence_delta,
        baseline_uncertain_share=baseline_uncertain,
        recent_uncertain_share=recent_uncertain,
        uncertain_share_delta=uncertain_delta,
        process_calibration_hashes=_metric_counts(Counter(process_hashes), total=total),
        rotation_calibration_hashes=_metric_counts(Counter(rotation_hashes), total=total),
        process_calibration_change_count=process_changes,
        rotation_calibration_change_count=rotation_changes,
        drift_reason_codes=_metric_counts(drift_reasons, total=total),
        signals=signals,
    )


def _metric_counts(
    counter: Counter[str],
    *,
    total: int,
    required_keys: Iterable[str] = (),
) -> list[RuntimeStemShadowMetricCount]:
    keys = list(dict.fromkeys([*required_keys, *counter.keys()]))
    required = set(required_keys)
    keys.sort(key=lambda key: (0 if key in required else 1, -counter[key], key))
    return [
        RuntimeStemShadowMetricCount(
            key=key,
            count=counter[key],
            share=_share(counter[key], total),
        )
        for key in keys
    ]


def _histogram(
    values: Sequence[float],
    *,
    boundaries: Sequence[float],
) -> list[RuntimeStemShadowHistogramBin]:
    bins = [0 for _ in range(max(0, len(boundaries) - 1))]
    for value in values:
        for index, (lower, upper) in enumerate(
            zip(boundaries, boundaries[1:], strict=False)
        ):
            if lower <= value < upper or (index == len(bins) - 1 and value == upper):
                bins[index] += 1
                break
    return [
        RuntimeStemShadowHistogramBin(
            lower_bound=float(lower),
            upper_bound=float(upper),
            count=bins[index],
        )
        for index, (lower, upper) in enumerate(
            zip(boundaries, boundaries[1:], strict=False)
        )
    ]


def _confidence_percentiles(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "median": _percentile(values, 0.5),
        "p90": _percentile(values, 0.9),
        "p95": _percentile(values, 0.95),
    }


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _decision_latency_ms(result: Mapping[str, Any]) -> float | None:
    started = _parse_timestamp(result.get("session_started_timestamp"))
    decided = _parse_timestamp(result.get("decision_timestamp"))
    if started is None or decided is None or decided < started:
        return None
    return (decided - started).total_seconds() * 1000.0


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def _mean_confidence(results: Sequence[Mapping[str, Any]]) -> float | None:
    values = [
        value
        for result in results
        if (value := _bounded_confidence(result.get("overall_confidence"))) is not None
    ]
    return mean(values) if values else None


def _uncertain_share(results: Sequence[Mapping[str, Any]]) -> float | None:
    if not results:
        return None
    uncertain_count = sum(
        1 for result in results if result.get("decision") == "UNCERTAIN"
    )
    return _share(uncertain_count, len(results))


def _change_count(values: Sequence[str]) -> int:
    return sum(
        current != previous
        for previous, current in zip(values, values[1:], strict=False)
    )


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if number == number else None


def _bounded_confidence(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= 1 else None


def _bounded_number(
    value: object,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    number = _number(value)
    return number if number is not None and minimum <= number <= maximum else default


def _bounded_integer(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    return int(value) if isinstance(value, int) and minimum <= value <= maximum else default


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")


def _markdown_report(report: Mapping[str, Any]) -> str:
    sessions = report["sessions"]
    decisions = report["decision_distribution"]
    latency = report["decision_latency"]
    drift = report["drift"]
    feedback = report["feedback"]
    lines = [
        "# Stem Shadow Mode evaluation",
        "",
        f"- Pipeline version: `{report['pipeline_version_id']}`",
        f"- Shadow mode enabled: `{str(report['shadow_mode_enabled']).lower()}`",
        f"- Sessions analyzed: {sessions['analyzed']} of {sessions['available']}",
        (
            f"- Period: {report['period']['completed_from'] or 'n/a'} to "
            f"{report['period']['completed_to'] or 'n/a'}"
        ),
        "",
        "## Decisions",
        "",
        "| Decision | Count | Share |",
        "|---|---:|---:|",
        *[
            f"| {item['key']} | {item['count']} | {item['share']:.1%} |"
            for item in decisions
        ],
        "",
        "## Runtime and drift",
        "",
        (
            "- Decision latency median/P90/P95: "
            f"{_format_ms(latency['median_ms'])} / "
            f"{_format_ms(latency['p90_ms'])} / "
            f"{_format_ms(latency['p95_ms'])}"
        ),
        f"- Drift assessment: `{drift['status']}`",
        f"- Drift signals: {', '.join(drift['signals']) if drift['signals'] else 'none'}",
        f"- Reviewed sessions: {feedback['reviewed_session_count']}",
        "",
        (
            "This report evaluates runtime behavior only. Operator feedback contains "
            "no target frame or estimated ground-truth angle."
        ),
    ]
    return "\n".join(lines) + "\n"


def _format_ms(value: object) -> str:
    number = _number(value)
    return f"{number:.1f} ms" if number is not None else "n/a"
