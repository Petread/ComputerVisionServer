from __future__ import annotations

import math

import pytest
from cv_runtime.stem_curvature_phase_solver import (
    StemCurvaturePhaseObservation,
    StemCurvaturePhaseSolverConfig,
    circular_distance_180,
    solve_stem_curvature_phase,
    wrap_to_minus_90_plus_90,
)


def _observations(
    phases: list[float],
    *,
    energy_max_phase_deg: float = 20.0,
    energy_offset_mm2: float = 25.0,
    energy_amplitude_mm2: float = 16.0,
    shape: str = "positive_c_bow",
) -> list[StemCurvaturePhaseObservation]:
    result: list[StemCurvaturePhaseObservation] = []
    for index, phase in enumerate(phases):
        radians = math.radians(phase)
        signed_bow = 2.0 + 8.0 * math.cos(radians - math.radians(40.0))
        energy = energy_offset_mm2 + energy_amplitude_mm2 * math.cos(
            2.0 * (radians - math.radians(energy_max_phase_deg))
        )
        result.append(
            StemCurvaturePhaseObservation(
                track_id="track-a",
                frame_id=str(index),
                timestamp=f"2026-08-04T12:00:{index:02d}Z",
                cumulative_phase_deg=phase,
                signed_bow_mm=signed_bow,
                curvature_rms_mm=math.sqrt(max(energy, 0.0)),
                curvature_maxabs_mm=abs(signed_bow),
                curvature_peak_to_peak_mm=2 * abs(signed_bow),
                geometry_confidence=0.95,
                phase_confidence=0.92,
                common_coverage=0.85,
                bow_shape_hint=shape,
            )
        )
    return result


def test_known_signed_and_energy_curves_recover_minimum_and_maximum() -> None:
    result = solve_stem_curvature_phase(_observations(list(range(-30, 211, 15))))

    assert result.state == "SOLVED"
    assert result.target_phase_deg == pytest.approx(110.0, abs=0.2)
    assert result.maximum_phase_deg == pytest.approx(20.0, abs=0.2)
    assert result.fitted_min_curvature_mm == pytest.approx(3.0, abs=0.05)
    assert result.fitted_max_curvature_mm == pytest.approx(math.sqrt(41), abs=0.05)
    assert result.fitted_min_signed_bow_mm == pytest.approx(-6.0, abs=0.05)
    assert result.fitted_max_signed_bow_mm == pytest.approx(10.0, abs=0.05)
    assert result.minimum_unique and result.bow_relevant
    assert result.target_phase_source == "observed"
    assert result.confidence > 0.5


def test_negative_phases_and_half_turn_shift_have_same_modulo_180_solution() -> None:
    negative = solve_stem_curvature_phase(_observations(list(range(-170, 11, 15))))
    shifted = solve_stem_curvature_phase(_observations(list(range(10, 191, 15))))

    assert negative.state == shifted.state == "SOLVED"
    assert circular_distance_180(negative.target_phase_deg, shifted.target_phase_deg) < 0.2
    assert circular_distance_180(negative.maximum_phase_deg, shifted.maximum_phase_deg) < 0.2


def test_partial_observation_allows_bounded_extrapolation() -> None:
    config = StemCurvaturePhaseSolverConfig(
        minimum_phase_span_deg=80,
        target_observation_tolerance_deg=4,
        maximum_target_extrapolation_deg=20,
    )
    result = solve_stem_curvature_phase(_observations(list(range(0, 101, 10))), config=config)

    assert result.state == "SOLVED"
    assert result.target_phase_deg == pytest.approx(110, abs=0.3)
    assert result.target_phase_source == "extrapolated"
    assert result.target_extrapolation_deg == pytest.approx(10, abs=0.3)
    assert "TARGET_PHASE_EXTRAPOLATED" in result.reason_codes


def test_insufficient_rotation_never_publishes_a_minimum() -> None:
    result = solve_stem_curvature_phase(_observations(list(range(0, 46, 5))))

    assert result.state == "INSUFFICIENT_ROTATION"
    assert result.target_phase_deg is None
    assert result.maximum_phase_deg is None
    assert "INSUFFICIENT_ROTATION" in result.reason_codes


def test_robust_fit_rejects_a_single_large_outlier() -> None:
    observations = _observations(list(range(0, 181, 10)))
    original = observations[8]
    observations[8] = StemCurvaturePhaseObservation(
        **{
            **original.__dict__,
            "signed_bow_mm": 80.0,
            "curvature_rms_mm": 40.0,
        }
    )

    result = solve_stem_curvature_phase(observations)

    assert result.state == "SOLVED"
    assert result.target_phase_deg == pytest.approx(110, abs=1.0)
    assert result.signed_fit is not None and result.signed_fit.inlier_weight_ratio < 1
    assert result.energy_fit is not None and result.energy_fit.inlier_weight_ratio < 1


def test_s_bow_energy_keeps_a_residual_minimum_marker() -> None:
    result = solve_stem_curvature_phase(
        _observations(
            list(range(0, 181, 10)),
            energy_offset_mm2=25,
            energy_amplitude_mm2=16,
            shape="s_bow",
        )
    )

    assert result.state == "SOLVED"
    assert result.s_bow_residual_minimum
    assert result.fitted_min_curvature_mm == pytest.approx(3, abs=0.05)
    assert "S_BOW_RESIDUAL_MINIMUM" in result.reason_codes


def test_flat_high_curve_is_ambiguous_and_nearly_straight_is_not_relevant() -> None:
    phases = list(range(0, 181, 10))
    flat = solve_stem_curvature_phase(
        _observations(phases, energy_offset_mm2=25, energy_amplitude_mm2=0)
    )
    straight = solve_stem_curvature_phase(
        _observations(phases, energy_offset_mm2=1, energy_amplitude_mm2=0)
    )

    assert flat.state == "AMBIGUOUS"
    assert flat.target_phase_deg is None and not flat.minimum_unique
    assert "CURVATURE_MINIMUM_AMBIGUOUS" in flat.reason_codes
    assert straight.state == "BOW_NOT_RELEVANT"
    assert straight.target_phase_deg is None and straight.bow_relevant is False
    assert "BOW_NOT_RELEVANT" in straight.reason_codes


def test_shortest_periodic_correction_is_signed_and_half_open() -> None:
    assert wrap_to_minus_90_plus_90(20) == pytest.approx(20)
    assert wrap_to_minus_90_plus_90(-20) == pytest.approx(-20)
    assert wrap_to_minus_90_plus_90(100) == pytest.approx(-80)
    assert wrap_to_minus_90_plus_90(-100) == pytest.approx(80)
    assert wrap_to_minus_90_plus_90(90) == pytest.approx(-90)
    assert wrap_to_minus_90_plus_90(180) == pytest.approx(0)
