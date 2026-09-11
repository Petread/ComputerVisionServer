from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

TEMPORAL_ANALYZER_VERSION = "stem.temporal.projected-baseline.v1"


class StemTemporalReasonCode(StrEnum):
    PROJECTED_BASELINE_SHADOW_ONLY = "projected_baseline_shadow_only"
    PRE_ENTRY_ANGLE_READY = "pre_entry_angle_ready"
    ROTATION_CALIBRATION_INVALID = "rotation_calibration_invalid"
    ROTATION_TRACE_DISCONTINUOUS = "rotation_trace_discontinuous"
    ROTATION_QUALITY_INSUFFICIENT = "rotation_quality_insufficient"
    GEOMETRY_QUALITY_INSUFFICIENT = "geometry_quality_insufficient"
    OBSERVATION_COUNT_INSUFFICIENT = "observation_count_insufficient"
    OBSERVED_ROTATION_INSUFFICIENT = "insufficient_observed_rotation"
    TARGET_NOT_OBSERVED = "target_not_observed"
    TARGET_AMBIGUOUS = "target_candidate_ambiguous"
    S_BOW_AMBIGUOUS = "ambiguous_s_bow"
    DECISION_FRAME_MISSING = "decision_frame_missing"
    BOW_NOT_RELEVANT = "bow_not_relevant"
    NO_ROTATION_DEADBAND = "no_rotation_deadband"


@dataclass(frozen=True)
class StemTemporalObservation:
    frame_sequence: int
    timestamp: str
    cumulative_rotation_deg: float
    rotation_confidence: float
    inlier_ratio: float
    signed_bow_normalized: float
    rms_residual_normalized: float
    geometry_quality: float
    visible_fraction: float
    shape_class: str
    center_x_normalized: float
    center_y_normalized: float


@dataclass(frozen=True)
class StemTemporalBaselineConfig:
    minimum_observation_count: int = 4
    minimum_observed_rotation_deg: float = 20.0
    minimum_rotation_confidence: float = 0.45
    minimum_geometry_quality: float = 0.5
    minimum_visible_fraction: float = 0.3
    minimum_bow_relevance: float = 0.12
    minimum_target_improvement: float = 0.04
    target_ambiguity_margin: float = 0.08
    target_merge_distance_deg: float = 5.0
    minimum_result_confidence: float = 0.5
    deadband_deg: float = 5.0


@dataclass(frozen=True)
class StemTargetCandidate:
    rotation_deg: float
    frame_start: int
    frame_end: int
    candidate_type: str
    score: float
    bow_magnitude: float


@dataclass(frozen=True)
class StemTemporalDecision:
    valid_angle: bool
    status: str
    correction_deg: float | None
    direction_sign: int
    confidence: float
    target_observed: bool
    bow_relevant: bool
    target_candidate: StemTargetCandidate | None
    decision_observation: StemTemporalObservation | None
    observed_rotation_span_deg: float
    improvement_observed: bool
    worsening_observed: bool
    sign_change_observed: bool
    projected_minimum_bracketed: bool
    reason_codes: tuple[str, ...]
    target_candidates: tuple[StemTargetCandidate, ...] = ()
    estimate_method: str = "projected_baseline"
    analyzer_version: str = TEMPORAL_ANALYZER_VERSION


def wrap_to_half_turn(angle_deg: float) -> float:
    wrapped = (float(angle_deg) + 90.0) % 180.0 - 90.0
    return -90.0 if math.isclose(wrapped, 90.0, abs_tol=1e-12) else wrapped


def evaluate_projected_pre_entry_baseline(
    observations: Sequence[StemTemporalObservation],
    *,
    decision_observation: StemTemporalObservation | None,
    calibration_valid: bool,
    trace_continuous: bool,
    config: StemTemporalBaselineConfig | None = None,
) -> StemTemporalDecision:
    resolved = config or StemTemporalBaselineConfig()
    reasons: list[str] = [StemTemporalReasonCode.PROJECTED_BASELINE_SHADOW_ONLY.value]
    if not calibration_valid:
        reasons.append(StemTemporalReasonCode.ROTATION_CALIBRATION_INVALID.value)
    if not trace_continuous:
        reasons.append(StemTemporalReasonCode.ROTATION_TRACE_DISCONTINUOUS.value)
    if decision_observation is None:
        reasons.append(StemTemporalReasonCode.DECISION_FRAME_MISSING.value)

    ordered = sorted(observations, key=lambda item: item.frame_sequence)
    quality_observations = [
        item
        for item in ordered
        if item.geometry_quality >= resolved.minimum_geometry_quality
        and item.rotation_confidence >= resolved.minimum_rotation_confidence
        and item.visible_fraction >= resolved.minimum_visible_fraction
        and math.isfinite(item.cumulative_rotation_deg)
        and math.isfinite(item.signed_bow_normalized)
    ]
    if len(quality_observations) < resolved.minimum_observation_count:
        reasons.append(StemTemporalReasonCode.OBSERVATION_COUNT_INSUFFICIENT.value)
        if any(
            item.geometry_quality < resolved.minimum_geometry_quality
            or item.visible_fraction < resolved.minimum_visible_fraction
            for item in ordered
        ):
            reasons.append(StemTemporalReasonCode.GEOMETRY_QUALITY_INSUFFICIENT.value)
        if any(item.rotation_confidence < resolved.minimum_rotation_confidence for item in ordered):
            reasons.append(StemTemporalReasonCode.ROTATION_QUALITY_INSUFFICIENT.value)

    rotation_span = (
        max(item.cumulative_rotation_deg for item in quality_observations)
        - min(item.cumulative_rotation_deg for item in quality_observations)
        if quality_observations
        else 0.0
    )
    if rotation_span < resolved.minimum_observed_rotation_deg:
        reasons.append(StemTemporalReasonCode.OBSERVED_ROTATION_INSUFFICIENT.value)

    max_bow = max(
        (abs(item.signed_bow_normalized) for item in quality_observations),
        default=0.0,
    )
    bow_relevant = max_bow >= resolved.minimum_bow_relevance
    if not bow_relevant:
        reasons.append(StemTemporalReasonCode.BOW_NOT_RELEVANT.value)

    if any(item.shape_class == "s_bow_or_complex" for item in quality_observations):
        reasons.append(StemTemporalReasonCode.S_BOW_AMBIGUOUS.value)

    candidates, diagnostics = _target_candidates(quality_observations, resolved)
    candidates = _merge_candidates(candidates, resolved.target_merge_distance_deg)
    target_candidate: StemTargetCandidate | None = None
    target_ambiguous = False
    if candidates:
        ranked = sorted(candidates, key=lambda item: (-item.score, item.frame_start))
        target_candidate = ranked[0]
        if (
            len(ranked) > 1
            and ranked[0].score - ranked[1].score <= resolved.target_ambiguity_margin
        ):
            target_ambiguous = True
            reasons.append(StemTemporalReasonCode.TARGET_AMBIGUOUS.value)
    else:
        reasons.append(StemTemporalReasonCode.TARGET_NOT_OBSERVED.value)

    hard_failures = {
        StemTemporalReasonCode.ROTATION_CALIBRATION_INVALID.value,
        StemTemporalReasonCode.ROTATION_TRACE_DISCONTINUOUS.value,
        StemTemporalReasonCode.OBSERVED_ROTATION_INSUFFICIENT.value,
        StemTemporalReasonCode.TARGET_NOT_OBSERVED.value,
        StemTemporalReasonCode.TARGET_AMBIGUOUS.value,
        StemTemporalReasonCode.S_BOW_AMBIGUOUS.value,
        StemTemporalReasonCode.DECISION_FRAME_MISSING.value,
    }
    if len(quality_observations) < resolved.minimum_observation_count:
        hard_failures.add(StemTemporalReasonCode.OBSERVATION_COUNT_INSUFFICIENT.value)

    straight_confidence = _observation_confidence(
        quality_observations,
        rotation_span,
        resolved,
    )
    straight_result_valid = (
        not bow_relevant
        and calibration_valid
        and trace_continuous
        and decision_observation is not None
        and len(quality_observations) >= resolved.minimum_observation_count
        and rotation_span >= resolved.minimum_observed_rotation_deg
        and straight_confidence >= resolved.minimum_result_confidence
    )
    if straight_result_valid:
        return StemTemporalDecision(
            valid_angle=True,
            status="no_rotation",
            correction_deg=0.0,
            direction_sign=0,
            confidence=straight_confidence,
            target_observed=False,
            bow_relevant=False,
            target_candidate=None,
            decision_observation=decision_observation,
            observed_rotation_span_deg=rotation_span,
            improvement_observed=diagnostics["improvement"],
            worsening_observed=diagnostics["worsening"],
            sign_change_observed=diagnostics["sign_change"],
            projected_minimum_bracketed=diagnostics["minimum_bracketed"],
            target_candidates=tuple(candidates),
            reason_codes=tuple(
                dict.fromkeys(
                    [
                        StemTemporalReasonCode.PROJECTED_BASELINE_SHADOW_ONLY.value,
                        StemTemporalReasonCode.BOW_NOT_RELEVANT.value,
                    ]
                )
            ),
        )

    if any(reason in hard_failures for reason in reasons) or target_candidate is None:
        return _manual_decision(
            reasons=reasons,
            decision_observation=decision_observation,
            rotation_span=rotation_span,
            diagnostics=diagnostics,
            target_candidate=None if target_ambiguous else target_candidate,
            target_candidates=candidates,
            bow_relevant=bow_relevant,
        )

    assert decision_observation is not None
    correction = wrap_to_half_turn(
        target_candidate.rotation_deg - decision_observation.cumulative_rotation_deg
    )
    confidence = _result_confidence(
        quality_observations,
        candidate=target_candidate,
        rotation_span=rotation_span,
        config=resolved,
    )
    if confidence < resolved.minimum_result_confidence:
        reasons.append(StemTemporalReasonCode.ROTATION_QUALITY_INSUFFICIENT.value)
        return _manual_decision(
            reasons=reasons,
            decision_observation=decision_observation,
            rotation_span=rotation_span,
            diagnostics=diagnostics,
            target_candidate=target_candidate,
            target_candidates=candidates,
            bow_relevant=bow_relevant,
            confidence=confidence,
        )

    if abs(correction) < resolved.deadband_deg:
        correction = 0.0
        status = "no_rotation"
        reasons.append(StemTemporalReasonCode.NO_ROTATION_DEADBAND.value)
        direction_sign = 0
    else:
        status = "pre_entry_angle_ready"
        reasons.append(StemTemporalReasonCode.PRE_ENTRY_ANGLE_READY.value)
        direction_sign = 1 if correction > 0 else -1
    return StemTemporalDecision(
        valid_angle=True,
        status=status,
        correction_deg=correction,
        direction_sign=direction_sign,
        confidence=confidence,
        target_observed=True,
        bow_relevant=True,
        target_candidate=target_candidate,
        decision_observation=decision_observation,
        observed_rotation_span_deg=rotation_span,
        improvement_observed=diagnostics["improvement"],
        worsening_observed=diagnostics["worsening"],
        sign_change_observed=diagnostics["sign_change"],
        projected_minimum_bracketed=diagnostics["minimum_bracketed"],
        target_candidates=tuple(candidates),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _target_candidates(
    observations: Sequence[StemTemporalObservation],
    config: StemTemporalBaselineConfig,
) -> tuple[list[StemTargetCandidate], dict[str, bool]]:
    if len(observations) < 2:
        return [], {
            "improvement": False,
            "worsening": False,
            "sign_change": False,
            "minimum_bracketed": False,
        }
    bow = np.asarray(
        [item.signed_bow_normalized for item in observations],
        dtype=np.float64,
    )
    magnitude = np.abs(bow)
    smoothed = _median_smooth(magnitude)
    deltas = np.diff(smoothed)
    improvement = bool(np.any(deltas <= -config.minimum_target_improvement))
    worsening = bool(np.any(deltas >= config.minimum_target_improvement))
    candidates: list[StemTargetCandidate] = []
    sign_change = False
    minimum_bracketed = False
    for index in range(len(observations) - 1):
        left = observations[index]
        right = observations[index + 1]
        left_bow = bow[index]
        right_bow = bow[index + 1]
        if left_bow == 0 or right_bow == 0 or left_bow * right_bow < 0:
            denominator = abs(left_bow) + abs(right_bow)
            fraction = abs(left_bow) / denominator if denominator > 1e-12 else 0.5
            rotation = left.cumulative_rotation_deg + fraction * (
                right.cumulative_rotation_deg - left.cumulative_rotation_deg
            )
            candidates.append(
                StemTargetCandidate(
                    rotation_deg=rotation,
                    frame_start=left.frame_sequence,
                    frame_end=right.frame_sequence,
                    candidate_type="zero_crossing_interval",
                    score=min(
                        1.0,
                        0.75 + 0.25 * min(left.rotation_confidence, right.rotation_confidence),
                    ),
                    bow_magnitude=0.0,
                )
            )
            sign_change = True
    for index in range(1, len(observations) - 1):
        improvement_left = smoothed[index - 1] - smoothed[index]
        worsening_right = smoothed[index + 1] - smoothed[index]
        if (
            improvement_left >= config.minimum_target_improvement
            and worsening_right >= config.minimum_target_improvement
        ):
            depth = min(improvement_left, worsening_right)
            candidates.append(
                StemTargetCandidate(
                    rotation_deg=observations[index].cumulative_rotation_deg,
                    frame_start=observations[index].frame_sequence,
                    frame_end=observations[index].frame_sequence,
                    candidate_type="projected_minimum_bracketed",
                    score=min(
                        1.0,
                        0.55
                        + depth
                        + 0.2 * observations[index].geometry_quality
                        + 0.15 * observations[index].rotation_confidence,
                    ),
                    bow_magnitude=float(smoothed[index]),
                )
            )
            minimum_bracketed = True
    return candidates, {
        "improvement": improvement,
        "worsening": worsening,
        "sign_change": sign_change,
        "minimum_bracketed": minimum_bracketed,
    }


def _merge_candidates(
    candidates: Sequence[StemTargetCandidate],
    merge_distance_deg: float,
) -> list[StemTargetCandidate]:
    merged: list[StemTargetCandidate] = []
    for candidate in sorted(candidates, key=lambda item: (-item.score, item.frame_start)):
        if any(
            abs(candidate.rotation_deg - existing.rotation_deg) <= merge_distance_deg
            for existing in merged
        ):
            continue
        merged.append(candidate)
    return merged


def _result_confidence(
    observations: Sequence[StemTemporalObservation],
    *,
    candidate: StemTargetCandidate,
    rotation_span: float,
    config: StemTemporalBaselineConfig,
) -> float:
    observation_quality = _observation_confidence(observations, rotation_span, config)
    uniqueness = candidate.score
    return min(max(0.65 * observation_quality + 0.35 * uniqueness, 0.0), 1.0)


def _observation_confidence(
    observations: Sequence[StemTemporalObservation],
    rotation_span: float,
    config: StemTemporalBaselineConfig,
) -> float:
    if not observations:
        return 0.0
    geometry = float(np.mean([item.geometry_quality for item in observations]))
    rotation = float(np.mean([item.rotation_confidence for item in observations]))
    visibility = float(np.mean([item.visible_fraction for item in observations]))
    inliers = float(np.mean([item.inlier_ratio for item in observations]))
    span = min(rotation_span / max(config.minimum_observed_rotation_deg * 2.0, 1e-6), 1.0)
    amount = min(len(observations) / max(config.minimum_observation_count * 2, 1), 1.0)
    return min(
        max(
            0.2 * geometry
            + 0.2 * rotation
            + 0.15 * visibility
            + 0.15 * inliers
            + 0.15 * span
            + 0.15 * amount,
            0.0,
        ),
        1.0,
    )


def _manual_decision(
    *,
    reasons: Sequence[str],
    decision_observation: StemTemporalObservation | None,
    rotation_span: float,
    diagnostics: dict[str, bool],
    target_candidate: StemTargetCandidate | None,
    target_candidates: Sequence[StemTargetCandidate],
    bow_relevant: bool,
    confidence: float = 0.0,
) -> StemTemporalDecision:
    return StemTemporalDecision(
        valid_angle=False,
        status="manual_required",
        correction_deg=None,
        direction_sign=0,
        confidence=confidence,
        target_observed=bool(target_candidates),
        bow_relevant=bow_relevant,
        target_candidate=target_candidate,
        decision_observation=decision_observation,
        observed_rotation_span_deg=rotation_span,
        improvement_observed=diagnostics["improvement"],
        worsening_observed=diagnostics["worsening"],
        sign_change_observed=diagnostics["sign_change"],
        projected_minimum_bracketed=diagnostics["minimum_bracketed"],
        target_candidates=tuple(target_candidates),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _median_smooth(values: np.ndarray) -> np.ndarray:
    if values.size < 3:
        return values.copy()
    result = values.copy()
    for index in range(1, len(values) - 1):
        result[index] = float(np.median(values[index - 1 : index + 2]))
    return result
