from __future__ import annotations

import hashlib
import html
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from cv_contracts.runtime import (
    RuntimeStemMinimumPrincipleValidationArtifact,
    RuntimeStemMinimumPrincipleValidationCalculateRequest,
    RuntimeStemMinimumPrincipleValidationCheck,
    RuntimeStemMinimumPrincipleValidationEvaluation,
    RuntimeStemMinimumPrincipleValidationMetricGroup,
    RuntimeStemMinimumPrincipleValidationMetrics,
    RuntimeStemMinimumPrincipleValidationObservation,
    RuntimeStemMinimumPrincipleValidationProvenance,
)
from cv_utils import utc_now_iso

MINIMUM_PRINCIPLE_VALIDATION_ALGORITHM_VERSION = (
    "stem.minimum-principle-objective-validation.v1"
)
ValidationStatus = Literal[
    "VALIDATION_PASSED", "VALIDATION_FAILED", "INSUFFICIENT_DATA"
]


@dataclass(frozen=True)
class StemMinimumPrincipleValidationComputation:
    status: ValidationStatus
    artifact: RuntimeStemMinimumPrincipleValidationArtifact


def calculate_stem_minimum_principle_validation(
    *,
    source_channel_id: str,
    request: RuntimeStemMinimumPrincipleValidationCalculateRequest,
    operator_user_id: str | None = None,
    generated_at: str | None = None,
) -> StemMinimumPrincipleValidationComputation:
    evaluations = [
        _evaluate_observation(
            item,
            image_width=request.image_width,
            image_height=request.image_height,
        )
        for item in request.observations
    ]
    metrics = _metrics(evaluations)
    checks = _checks(evaluations, metrics, request)
    status: ValidationStatus
    if any(item.status == "INSUFFICIENT_DATA" for item in checks):
        status = "INSUFFICIENT_DATA"
    elif any(item.status == "FAIL" for item in checks):
        status = "VALIDATION_FAILED"
    else:
        status = "VALIDATION_PASSED"
    reason_codes = [
        (
            f"VALIDATION_DATA_{item.check_id.upper()}"
            if item.status == "INSUFFICIENT_DATA"
            else f"VALIDATION_THRESHOLD_{item.check_id.upper()}"
        )
        for item in checks
        if item.status != "PASS"
    ]
    report_markdown = _markdown_report(
        artifact_id=request.artifact_id,
        artifact_version=request.artifact_version,
        status=status,
        metrics=metrics,
        checks=checks,
        process_calibration_id=request.process_calibration_id,
        phase_calibration_id=request.phase_calibration_id,
        phase_calibration_version=request.phase_calibration_version,
    )
    report_html = _html_report(report_markdown)
    artifact = RuntimeStemMinimumPrincipleValidationArtifact(
        artifact_id=request.artifact_id,
        artifact_version=request.artifact_version,
        status=status,
        source_channel_id=source_channel_id,
        image_width=request.image_width,
        image_height=request.image_height,
        process_calibration_id=request.process_calibration_id,
        process_calibration_hash=request.process_calibration_hash,
        phase_calibration_id=request.phase_calibration_id,
        phase_calibration_version=request.phase_calibration_version,
        phase_calibration_hash=request.phase_calibration_hash,
        algorithm_version=MINIMUM_PRINCIPLE_VALIDATION_ALGORITHM_VERSION,
        thresholds=request.thresholds,
        observations=evaluations,
        metrics=metrics,
        checks=checks,
        reason_codes=list(dict.fromkeys(reason_codes)),
        provenance=RuntimeStemMinimumPrincipleValidationProvenance(
            created_at=generated_at or utc_now_iso(),
            operator_user_id=operator_user_id,
            reference_asset_ids=request.reference_asset_ids,
            notes=request.notes,
        ),
        report_markdown=report_markdown,
        report_html=report_html,
        artifact_hash="0" * 64,
    )
    artifact = artifact.model_copy(
        update={"artifact_hash": stem_minimum_principle_validation_hash(artifact)}
    )
    validate_stem_minimum_principle_validation_artifact(artifact)
    return StemMinimumPrincipleValidationComputation(status=status, artifact=artifact)


def stem_minimum_principle_validation_hash(
    value: RuntimeStemMinimumPrincipleValidationArtifact | Mapping[str, Any],
) -> str:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, RuntimeStemMinimumPrincipleValidationArtifact)
        else dict(value)
    )
    payload.pop("artifact_hash", None)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_stem_minimum_principle_validation_artifact(
    value: object,
) -> RuntimeStemMinimumPrincipleValidationArtifact:
    artifact = RuntimeStemMinimumPrincipleValidationArtifact.model_validate(value)
    if stem_minimum_principle_validation_hash(artifact) != artifact.artifact_hash:
        raise ValueError("stem minimum-principle validation artifact hash does not match")
    if artifact.status == "VALIDATION_PASSED" and any(
        item.status != "PASS" for item in artifact.checks
    ):
        raise ValueError("a passed minimum-principle validation contains non-passing checks")
    if artifact.status == "VALIDATION_FAILED" and not any(
        item.status == "FAIL" for item in artifact.checks
    ):
        raise ValueError("a failed minimum-principle validation contains no failed check")
    if artifact.status == "INSUFFICIENT_DATA" and not any(
        item.status == "INSUFFICIENT_DATA" for item in artifact.checks
    ):
        raise ValueError("an insufficient validation contains no insufficient-data check")
    return artifact


def _evaluate_observation(
    item: RuntimeStemMinimumPrincipleValidationObservation,
    *,
    image_width: int,
    image_height: int,
) -> RuntimeStemMinimumPrincipleValidationEvaluation:
    phase_error = _paired_angle_error(
        item.calculated_phase_deg,
        item.known_angle_deg,
        period=360.0,
    )
    target_error = _paired_angle_error(
        item.calculated_target_phase_deg,
        item.expected_target_phase_deg,
        period=180.0,
    )
    correction_error = _paired_angle_error(
        item.calculated_correction_deg,
        item.expected_correction_deg,
        period=180.0,
    )
    preferred_error = next(
        (
            abs(value)
            for value in (correction_error, target_error, phase_error)
            if value is not None
        ),
        None,
    )
    sign_error = False
    if (
        item.expected_correction_deg is not None
        and item.calculated_correction_deg is not None
        and abs(item.expected_correction_deg) > 1e-6
        and abs(item.calculated_correction_deg) > 1e-6
        and item.expected_correction_deg * item.calculated_correction_deg < 0
        and abs(correction_error or 0.0) > 1.0
    ):
        sign_error = True
    homography_error = None
    if None not in (
        item.known_x_mm,
        item.known_y_mm,
        item.measured_x_mm,
        item.measured_y_mm,
    ):
        homography_error = math.hypot(
            float(item.measured_x_mm) - float(item.known_x_mm),
            float(item.measured_y_mm) - float(item.known_y_mm),
        )
    return RuntimeStemMinimumPrincipleValidationEvaluation(
        **item.model_dump(mode="python"),
        angle_error_deg=preferred_error,
        phase_error_deg_signed=phase_error,
        target_phase_error_deg_signed=target_error,
        correction_error_deg_signed=correction_error,
        sign_error=sign_error,
        homography_error_mm=homography_error,
        curvature_min_error_mm=_absolute_pair_error(
            item.measured_curvature_min_mm,
            item.expected_curvature_min_mm,
        ),
        curvature_max_error_mm=_absolute_pair_error(
            item.measured_curvature_max_mm,
            item.expected_curvature_max_mm,
        ),
        image_position_bucket=_image_position_bucket(
            item.image_center_x_px,
            item.image_center_y_px,
            image_width=image_width,
            image_height=image_height,
        ),
        phase_span_bucket=_phase_span_bucket(item.observed_phase_span_deg),
    )


def _metrics(
    observations: Sequence[RuntimeStemMinimumPrincipleValidationEvaluation],
) -> RuntimeStemMinimumPrincipleValidationMetrics:
    angle_errors = _present(item.angle_error_deg for item in observations)
    correction_pairs = [
        item
        for item in observations
        if item.expected_correction_deg is not None
        and item.calculated_correction_deg is not None
        and abs(item.expected_correction_deg) > 1e-6
    ]
    homography_errors = _present(item.homography_error_mm for item in observations)
    straight = [
        item
        for item in observations
        if item.reference_setup == "STRAIGHT_REFERENCE_CYLINDER"
    ]
    straight_curvature = [
        max(
            float(item.measured_curvature_min_mm or 0.0),
            float(item.measured_curvature_max_mm or 0.0),
        )
        for item in straight
        if item.measured_curvature_min_mm is not None
        or item.measured_curvature_max_mm is not None
    ]
    translation_errors = [
        abs(float(item.phase_error_deg_signed))
        for item in straight
        if item.translation_distance_mm > 0 and item.phase_error_deg_signed is not None
    ]
    curved = [
        item
        for item in observations
        if item.reference_setup != "STRAIGHT_REFERENCE_CYLINDER"
    ]
    minimum_errors = _present(item.curvature_min_error_mm for item in curved)
    maximum_errors = _present(item.curvature_max_error_mm for item in curved)
    minimum_repeatability = _linear_repeatability(
        curved,
        value=lambda item: item.measured_curvature_min_mm,
    )
    maximum_repeatability = _linear_repeatability(
        curved,
        value=lambda item: item.measured_curvature_max_mm,
    )
    target_repeatability = _circular_repeatability(
        curved,
        value=lambda item: item.calculated_target_phase_deg,
        period=180.0,
    )
    relock_attempts = [item for item in observations if item.relock_attempted]
    relock_successes = sum(1 for item in relock_attempts if item.relock_succeeded)
    uncertain = sum(1 for item in observations if item.decision == "UNCERTAIN")
    return RuntimeStemMinimumPrincipleValidationMetrics(
        total_observation_count=len(observations),
        valid_angle_observation_count=len(angle_errors),
        median_angle_error_deg=_percentile(angle_errors, 50),
        p90_angle_error_deg=_percentile(angle_errors, 90),
        p95_angle_error_deg=_percentile(angle_errors, 95),
        sign_error_count=sum(1 for item in correction_pairs if item.sign_error),
        sign_error_rate=(
            sum(1 for item in correction_pairs if item.sign_error) / len(correction_pairs)
            if correction_pairs
            else 0.0
        ),
        homography_error_median_mm=_percentile(homography_errors, 50),
        homography_error_p95_mm=_percentile(homography_errors, 95),
        straight_curvature_p95_mm=_percentile(straight_curvature, 95),
        translation_phase_error_p95_deg=_percentile(translation_errors, 95),
        curvature_min_abs_error_p95_mm=_percentile(minimum_errors, 95),
        curvature_max_abs_error_p95_mm=_percentile(maximum_errors, 95),
        curvature_min_repeatability_p95_mm=_percentile(minimum_repeatability, 95),
        curvature_max_repeatability_p95_mm=_percentile(maximum_repeatability, 95),
        target_phase_repeatability_p95_deg=_percentile(target_repeatability, 95),
        relock_attempt_count=len(relock_attempts),
        relock_success_count=relock_successes,
        relock_success_rate=(
            relock_successes / len(relock_attempts) if relock_attempts else None
        ),
        uncertainty_count=uncertain,
        uncertainty_rate=uncertain / len(observations) if observations else 1.0,
        by_phase_span=_group_metrics(observations, lambda item: item.phase_span_bucket),
        by_diameter=_group_metrics(observations, lambda item: item.diameter_class),
        by_image_position=_group_metrics(
            observations, lambda item: item.image_position_bucket
        ),
    )


def _checks(
    observations: Sequence[RuntimeStemMinimumPrincipleValidationEvaluation],
    metrics: RuntimeStemMinimumPrincipleValidationMetrics,
    request: RuntimeStemMinimumPrincipleValidationCalculateRequest,
) -> list[RuntimeStemMinimumPrincipleValidationCheck]:
    limits = request.thresholds
    setup_counts = Counter(item.reference_setup for item in observations)
    correction_pair_count = sum(
        1
        for item in observations
        if item.expected_correction_deg is not None
        and item.calculated_correction_deg is not None
        and abs(item.expected_correction_deg) > 1e-6
    )
    checks = [
        _minimum_check(
            "total_observations",
            len(observations),
            limits.minimum_total_observations,
            "Total objective reference observations",
        )
    ]
    for setup in (
        "STRAIGHT_REFERENCE_CYLINDER",
        "CURVED_REFERENCE_BODY",
        "MARKED_TEST_STEM",
    ):
        checks.append(
            _minimum_check(
                f"setup_{setup.lower()}",
                setup_counts[setup],
                limits.minimum_observations_per_setup,
                f"Observations for {setup}",
            )
        )
    checks.extend(
        [
            _minimum_check(
                "complete_straight_reference_evidence",
                sum(
                    1
                    for item in observations
                    if item.reference_setup == "STRAIGHT_REFERENCE_CYLINDER"
                    and item.phase_error_deg_signed is not None
                    and item.homography_error_mm is not None
                    and item.curvature_min_error_mm is not None
                    and item.curvature_max_error_mm is not None
                    and item.translation_distance_mm > 0
                ),
                limits.minimum_observations_per_setup,
                "Straight-cylinder runs with X/Y, zero-curvature and translation evidence",
            ),
            _minimum_check(
                "complete_curved_reference_evidence",
                sum(
                    1
                    for item in observations
                    if item.reference_setup == "CURVED_REFERENCE_BODY"
                    and item.target_phase_error_deg_signed is not None
                    and item.correction_error_deg_signed is not None
                    and item.curvature_min_error_mm is not None
                    and item.curvature_max_error_mm is not None
                ),
                limits.minimum_observations_per_setup,
                "Curved-body runs with minimum, maximum, target and correction evidence",
            ),
            _minimum_check(
                "complete_marked_stem_evidence",
                sum(
                    1
                    for item in observations
                    if item.reference_setup == "MARKED_TEST_STEM"
                    and item.phase_error_deg_signed is not None
                    and item.correction_error_deg_signed is not None
                ),
                limits.minimum_observations_per_setup,
                "Marked-stem runs with marked phase and correction evidence",
            ),
        ]
    )
    checks.extend(
        [
            _minimum_check(
                "valid_angle_observations",
                metrics.valid_angle_observation_count,
                limits.minimum_valid_angle_observations,
                "Observations with an objective angle error",
            ),
            _maximum_check(
                "median_angle_error",
                metrics.median_angle_error_deg,
                limits.maximum_median_angle_error_deg,
                metrics.valid_angle_observation_count,
                "Median absolute angle error [deg]",
            ),
            _maximum_check(
                "p95_angle_error",
                metrics.p95_angle_error_deg,
                limits.maximum_p95_angle_error_deg,
                metrics.valid_angle_observation_count,
                "P95 absolute angle error [deg]",
            ),
            _maximum_check(
                "sign_errors",
                metrics.sign_error_count if correction_pair_count else None,
                limits.maximum_sign_error_count,
                correction_pair_count,
                "Correction-direction sign errors",
            ),
            _maximum_check(
                "target_phase_repeatability",
                metrics.target_phase_repeatability_p95_deg,
                limits.maximum_target_phase_repeatability_deg,
                _repeatability_sample_count(
                    observations, lambda item: item.calculated_target_phase_deg
                ),
                "P95 target-phase deviation within repeated reference-body runs [deg]",
            ),
            _maximum_check(
                "homography_error",
                metrics.homography_error_p95_mm,
                limits.maximum_homography_error_mm,
                sum(1 for item in observations if item.homography_error_mm is not None),
                "P95 X/Y reference-position error [mm]",
            ),
            _maximum_check(
                "straight_curvature",
                metrics.straight_curvature_p95_mm,
                limits.maximum_straight_curvature_mm,
                sum(
                    1
                    for item in observations
                    if item.reference_setup == "STRAIGHT_REFERENCE_CYLINDER"
                    and (
                        item.measured_curvature_min_mm is not None
                        or item.measured_curvature_max_mm is not None
                    )
                ),
                "P95 measured curvature of the straight reference cylinder [mm]",
            ),
            _maximum_check(
                "translation_phase_error",
                metrics.translation_phase_error_p95_deg,
                limits.maximum_translation_phase_error_deg,
                sum(
                    1
                    for item in observations
                    if item.reference_setup == "STRAIGHT_REFERENCE_CYLINDER"
                    and item.translation_distance_mm > 0
                    and item.phase_error_deg_signed is not None
                ),
                "P95 phase error under pure translation [deg]",
            ),
            _maximum_check(
                "curvature_min_accuracy",
                metrics.curvature_min_abs_error_p95_mm,
                limits.maximum_curvature_abs_error_mm,
                sum(1 for item in observations if item.curvature_min_error_mm is not None),
                "P95 minimum-curvature absolute error [mm]",
            ),
            _maximum_check(
                "curvature_max_accuracy",
                metrics.curvature_max_abs_error_p95_mm,
                limits.maximum_curvature_abs_error_mm,
                sum(1 for item in observations if item.curvature_max_error_mm is not None),
                "P95 maximum-curvature absolute error [mm]",
            ),
            _maximum_check(
                "curvature_min_repeatability",
                metrics.curvature_min_repeatability_p95_mm,
                limits.maximum_curvature_repeatability_mm,
                _repeatability_sample_count(
                    observations, lambda item: item.measured_curvature_min_mm
                ),
                "P95 repeated minimum-curvature deviation [mm]",
            ),
            _maximum_check(
                "curvature_max_repeatability",
                metrics.curvature_max_repeatability_p95_mm,
                limits.maximum_curvature_repeatability_mm,
                _repeatability_sample_count(
                    observations, lambda item: item.measured_curvature_max_mm
                ),
                "P95 repeated maximum-curvature deviation [mm]",
            ),
            _minimum_optional_check(
                "relock_success",
                metrics.relock_success_rate,
                limits.minimum_relock_success_rate,
                metrics.relock_attempt_count,
                "Successful Re-Locks / attempted Re-Locks",
            ),
            _maximum_check(
                "uncertainty_rate",
                metrics.uncertainty_rate,
                limits.maximum_uncertainty_rate,
                len(observations),
                "UNCERTAIN decisions / all observations",
            ),
        ]
    )
    return checks


def _group_metrics(
    observations: Sequence[RuntimeStemMinimumPrincipleValidationEvaluation],
    key: Callable[[RuntimeStemMinimumPrincipleValidationEvaluation], str],
) -> list[RuntimeStemMinimumPrincipleValidationMetricGroup]:
    groups: dict[str, list[RuntimeStemMinimumPrincipleValidationEvaluation]] = defaultdict(
        list
    )
    for item in observations:
        groups[str(key(item))].append(item)
    result = []
    for group_key, values in sorted(groups.items()):
        errors = _present(item.angle_error_deg for item in values)
        uncertain = sum(1 for item in values if item.decision == "UNCERTAIN")
        result.append(
            RuntimeStemMinimumPrincipleValidationMetricGroup(
                group_key=group_key,
                observation_count=len(values),
                valid_angle_count=len(errors),
                median_angle_error_deg=_percentile(errors, 50),
                p90_angle_error_deg=_percentile(errors, 90),
                p95_angle_error_deg=_percentile(errors, 95),
                uncertainty_rate=uncertain / len(values),
            )
        )
    return result


def _linear_repeatability(
    observations: Sequence[RuntimeStemMinimumPrincipleValidationEvaluation],
    *,
    value: Callable[[RuntimeStemMinimumPrincipleValidationEvaluation], float | None],
) -> list[float]:
    groups = _repeatability_groups(observations, value)
    deviations: list[float] = []
    for values in groups.values():
        center = float(_percentile(values, 50) or 0.0)
        deviations.extend(abs(item - center) for item in values)
    return deviations


def _circular_repeatability(
    observations: Sequence[RuntimeStemMinimumPrincipleValidationEvaluation],
    *,
    value: Callable[[RuntimeStemMinimumPrincipleValidationEvaluation], float | None],
    period: float,
) -> list[float]:
    groups = _repeatability_groups(observations, value)
    deviations: list[float] = []
    for values in groups.values():
        center = _circular_center(values, period=period)
        deviations.extend(abs(_wrap_signed(item - center, period=period)) for item in values)
    return deviations


def _repeatability_groups(
    observations: Sequence[RuntimeStemMinimumPrincipleValidationEvaluation],
    value: Callable[[RuntimeStemMinimumPrincipleValidationEvaluation], float | None],
) -> dict[str, list[float]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for item in observations:
        if item.reference_setup == "STRAIGHT_REFERENCE_CYLINDER":
            continue
        resolved = value(item)
        if resolved is not None:
            groups[item.reference_body_id].append(float(resolved))
    return {key: values for key, values in groups.items() if len(values) >= 2}


def _repeatability_sample_count(
    observations: Sequence[RuntimeStemMinimumPrincipleValidationEvaluation],
    value: Callable[[RuntimeStemMinimumPrincipleValidationEvaluation], float | None],
) -> int:
    return sum(len(values) for values in _repeatability_groups(observations, value).values())


def _minimum_check(
    check_id: str, value: int, limit: int, message: str
) -> RuntimeStemMinimumPrincipleValidationCheck:
    return RuntimeStemMinimumPrincipleValidationCheck(
        check_id=check_id,
        status="PASS" if value >= limit else "INSUFFICIENT_DATA",
        sample_count=value,
        value=value,
        comparison=">=",
        limit=limit,
        message=message,
    )


def _maximum_check(
    check_id: str,
    value: float | int | None,
    limit: float | int,
    sample_count: int,
    message: str,
) -> RuntimeStemMinimumPrincipleValidationCheck:
    if value is None or sample_count <= 0:
        status = "INSUFFICIENT_DATA"
    else:
        status = "PASS" if float(value) <= float(limit) else "FAIL"
    return RuntimeStemMinimumPrincipleValidationCheck(
        check_id=check_id,
        status=status,
        sample_count=sample_count,
        value=value,
        comparison="<=",
        limit=limit,
        message=message,
    )


def _minimum_optional_check(
    check_id: str,
    value: float | None,
    limit: float,
    sample_count: int,
    message: str,
) -> RuntimeStemMinimumPrincipleValidationCheck:
    if value is None or sample_count <= 0:
        status = "INSUFFICIENT_DATA"
    else:
        status = "PASS" if value >= limit else "FAIL"
    return RuntimeStemMinimumPrincipleValidationCheck(
        check_id=check_id,
        status=status,
        sample_count=sample_count,
        value=value,
        comparison=">=",
        limit=limit,
        message=message,
    )


def _markdown_report(
    *,
    artifact_id: str,
    artifact_version: str,
    status: ValidationStatus,
    metrics: RuntimeStemMinimumPrincipleValidationMetrics,
    checks: Sequence[RuntimeStemMinimumPrincipleValidationCheck],
    process_calibration_id: str,
    phase_calibration_id: str,
    phase_calibration_version: str,
) -> str:
    lines = [
        "# Objective curvature-minimum validation",
        "",
        f"- Artifact: `{artifact_id}` version `{artifact_version}`",
        f"- Status: **{status}**",
        f"- Process calibration: `{process_calibration_id}`",
        (
            f"- Surface-phase calibration: `{phase_calibration_id}` "
            f"version `{phase_calibration_version}`"
        ),
        f"- Observations: {metrics.total_observation_count}",
        (
            "- Angle-error precedence: correction angle, otherwise target phase, "
            "otherwise marked surface phase"
        ),
        "",
        "## Accuracy metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Median angle error | {_format_metric(metrics.median_angle_error_deg, 'deg')} |",
        f"| P90 angle error | {_format_metric(metrics.p90_angle_error_deg, 'deg')} |",
        f"| P95 angle error | {_format_metric(metrics.p95_angle_error_deg, 'deg')} |",
        f"| Sign errors | {metrics.sign_error_count} |",
        (
            "| Minimum-curvature repeatability P95 | "
            f"{_format_metric(metrics.curvature_min_repeatability_p95_mm, 'mm')} |"
        ),
        (
            "| Maximum-curvature repeatability P95 | "
            f"{_format_metric(metrics.curvature_max_repeatability_p95_mm, 'mm')} |"
        ),
        (
            "| Target-phase repeatability P95 | "
            f"{_format_metric(metrics.target_phase_repeatability_p95_deg, 'deg')} |"
        ),
        f"| Re-Lock success | {_format_ratio(metrics.relock_success_rate)} |",
        f"| Uncertainty rate | {_format_ratio(metrics.uncertainty_rate)} |",
        "",
        "## Acceptance checks",
        "",
        "| Check | Status | Samples | Value | Rule |",
        "|---|---|---:|---:|---|",
    ]
    for item in checks:
        lines.append(
            f"| {item.message} | {item.status} | {item.sample_count} | "
            f"{_format_metric(item.value)} | {item.comparison} {_format_metric(item.limit)} |"
        )
    for title, groups in (
        ("Error by observed phase span", metrics.by_phase_span),
        ("Error by diameter class", metrics.by_diameter),
        ("Error by image position", metrics.by_image_position),
    ):
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Group | N | Valid angles | Median | P90 | P95 | Uncertain |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for group in groups:
            lines.append(
                f"| {group.group_key} | {group.observation_count} | "
                f"{group.valid_angle_count} | "
                f"{_format_metric(group.median_angle_error_deg, 'deg')} | "
                f"{_format_metric(group.p90_angle_error_deg, 'deg')} | "
                f"{_format_metric(group.p95_angle_error_deg, 'deg')} | "
                f"{_format_ratio(group.uncertainty_rate)} |"
            )
    lines.extend(
        [
            "",
            (
                "The report uses only known reference geometry, marked angles and runtime "
                "measurements; no target frame was selected by a person."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def _html_report(markdown: str) -> str:
    paragraphs = "\n".join(
        f"<div>{html.escape(line)}</div>" if line else "<br>"
        for line in markdown.splitlines()
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Objective curvature-minimum validation</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;"
        "padding:0 1rem}div{white-space:pre-wrap;font-family:ui-monospace,monospace}</style>"
        f"</head><body>{paragraphs}</body></html>"
    )


def _paired_angle_error(
    calculated: float | None, expected: float | None, *, period: float
) -> float | None:
    if calculated is None or expected is None:
        return None
    return _wrap_signed(float(calculated) - float(expected), period=period)


def _absolute_pair_error(measured: float | None, expected: float | None) -> float | None:
    if measured is None or expected is None:
        return None
    return abs(float(measured) - float(expected))


def _wrap_signed(value: float, *, period: float) -> float:
    half = period / 2.0
    wrapped = (float(value) + half) % period - half
    return 0.0 if math.isclose(wrapped, 0.0, abs_tol=1e-12) else wrapped


def _circular_center(values: Sequence[float], *, period: float) -> float:
    angles = [2.0 * math.pi * float(value) / period for value in values]
    sine = sum(math.sin(value) for value in angles)
    cosine = sum(math.cos(value) for value in angles)
    return (math.atan2(sine, cosine) * period / (2.0 * math.pi)) % period


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * float(percentile) / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _present(values: Iterable[float | None]) -> list[float]:
    return [float(value) for value in values if value is not None]


def _phase_span_bucket(value: float) -> str:
    if value < 60.0:
        return "phase_span_lt_60_deg"
    if value < 120.0:
        return "phase_span_60_to_120_deg"
    return "phase_span_ge_120_deg"


def _image_position_bucket(
    x: float, y: float, *, image_width: int, image_height: int
) -> str:
    columns = ("left", "center", "right")
    rows = ("top", "middle", "bottom")
    column = min(int(3.0 * x / max(image_width, 1)), 2)
    row = min(int(3.0 * y / max(image_height, 1)), 2)
    return f"{rows[row]}_{columns[column]}"


def _format_metric(value: object, unit: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        rendered = f"{value:.3f}"
    else:
        rendered = str(value)
    return f"{rendered} {unit}".strip()


def _format_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.1f}%"
