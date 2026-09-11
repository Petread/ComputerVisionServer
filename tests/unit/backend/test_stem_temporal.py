from __future__ import annotations

import pytest
from cv_runtime.stem_temporal import (
    StemTemporalBaselineConfig,
    StemTemporalObservation,
    evaluate_projected_pre_entry_baseline,
    wrap_to_half_turn,
)


def _observation(
    frame: int,
    rotation: float,
    bow: float,
    *,
    shape_class: str = "c_bow",
    rotation_confidence: float = 0.92,
) -> StemTemporalObservation:
    return StemTemporalObservation(
        frame_sequence=frame,
        timestamp=f"2026-08-03T10:00:0{frame}+00:00",
        cumulative_rotation_deg=rotation,
        rotation_confidence=rotation_confidence,
        inlier_ratio=0.9,
        signed_bow_normalized=bow,
        rms_residual_normalized=abs(bow) * 0.5,
        geometry_quality=0.9,
        visible_fraction=0.85,
        shape_class=shape_class,
        center_x_normalized=0.1 * frame,
        center_y_normalized=0.5,
    )


def test_f1_to_f4_sequence_interpolates_target_between_f3_and_f4() -> None:
    observations = [
        _observation(1, 0.0, 0.8),
        _observation(2, 20.0, 0.4),
        _observation(3, 40.0, 0.1),
        _observation(4, 60.0, -0.1),
    ]

    decision = evaluate_projected_pre_entry_baseline(
        observations,
        decision_observation=observations[-1],
        calibration_valid=True,
        trace_continuous=True,
    )

    assert decision.valid_angle is True
    assert decision.status == "pre_entry_angle_ready"
    assert decision.target_observed is True
    assert decision.target_candidate is not None
    assert decision.target_candidate.frame_start == 3
    assert decision.target_candidate.frame_end == 4
    assert decision.target_candidate.rotation_deg == pytest.approx(50.0)
    assert decision.correction_deg == pytest.approx(-10.0)
    assert decision.direction_sign == -1
    assert "projected_baseline_shadow_only" in decision.reason_codes


def test_half_turn_equivalence_wraps_near_plus_and_minus_89_degrees() -> None:
    assert wrap_to_half_turn(89.0 - (-89.0)) == pytest.approx(-2.0)
    assert wrap_to_half_turn(-89.0 - 89.0) == pytest.approx(2.0)
    assert wrap_to_half_turn(90.0) == pytest.approx(-90.0)
    assert -90.0 <= wrap_to_half_turn(1089.0) < 90.0


@pytest.mark.parametrize("calibration_valid,trace_continuous", [(False, True), (True, False)])
def test_invalid_calibration_or_discontinuous_trace_never_returns_degrees(
    calibration_valid: bool,
    trace_continuous: bool,
) -> None:
    observations = [
        _observation(1, 0.0, 0.8),
        _observation(2, 20.0, 0.4),
        _observation(3, 40.0, 0.1),
        _observation(4, 60.0, -0.1),
    ]

    decision = evaluate_projected_pre_entry_baseline(
        observations,
        decision_observation=observations[-1],
        calibration_valid=calibration_valid,
        trace_continuous=trace_continuous,
    )

    assert decision.valid_angle is False
    assert decision.status == "manual_required"
    assert decision.correction_deg is None


def test_s_bow_and_two_equally_plausible_crossings_are_ambiguous() -> None:
    observations = [
        _observation(1, 0.0, 0.7, shape_class="s_bow_or_complex"),
        _observation(2, 30.0, -0.2, shape_class="s_bow_or_complex"),
        _observation(3, 60.0, 0.2, shape_class="s_bow_or_complex"),
        _observation(4, 90.0, -0.7, shape_class="s_bow_or_complex"),
    ]

    decision = evaluate_projected_pre_entry_baseline(
        observations,
        decision_observation=observations[-1],
        calibration_valid=True,
        trace_continuous=True,
    )

    assert decision.valid_angle is False
    assert decision.correction_deg is None
    assert "ambiguous_s_bow" in decision.reason_codes


def test_straight_sequence_only_returns_no_rotation_after_quality_gates() -> None:
    config = StemTemporalBaselineConfig(minimum_result_confidence=0.45)
    observations = [
        _observation(1, 0.0, 0.01, shape_class="straight"),
        _observation(2, 10.0, 0.015, shape_class="straight"),
        _observation(3, 20.0, -0.01, shape_class="straight"),
        _observation(4, 30.0, -0.005, shape_class="straight"),
    ]
    ready = evaluate_projected_pre_entry_baseline(
        observations,
        decision_observation=observations[-1],
        calibration_valid=True,
        trace_continuous=True,
        config=config,
    )
    insufficient = evaluate_projected_pre_entry_baseline(
        observations[:2],
        decision_observation=observations[1],
        calibration_valid=True,
        trace_continuous=True,
        config=config,
    )

    assert ready.valid_angle is True
    assert ready.status == "no_rotation"
    assert ready.correction_deg == pytest.approx(0.0)
    assert insufficient.valid_angle is False
    assert insufficient.correction_deg is None
