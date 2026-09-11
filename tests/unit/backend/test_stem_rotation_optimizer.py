from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

import pytest
from cv_runtime.stem_rotation_optimizer import (
    StemRotationOptimizationConfig,
    build_stem_angle_score_curve,
    effective_search_bounds,
    optimize_stem_rotation,
)
from cv_runtime.stem_rotation_types import (
    PriorityMode,
    RotationConstraints,
    StemRotationReasonCode,
    StemSliceGeometry,
    StemSliceModel,
)
from tests.support.stem_rotation_models import (
    aligned_curvature_and_ovality_stem,
    local_bottleneck_stem,
    noisy_stem,
    opposed_curvature_and_ovality_stem,
    stem_with_short_gap,
    straight_round_stem,
    strongly_curved_stem,
    strongly_oval_stem,
)


def _model(
    *slices: StemSliceGeometry,
    quality: float = 1.0,
) -> StemSliceModel:
    return StemSliceModel(slices=tuple(slices), model_quality=quality)


def _slice(
    *,
    x_mm: float = 0.0,
    cy_mm: float = 0.0,
    cz_mm: float = 0.0,
    a_mm: float = 100.0,
    b_mm: float = 100.0,
    phi_deg: float = 0.0,
    valid: bool = True,
) -> StemSliceGeometry:
    return StemSliceGeometry(
        x_mm=x_mm,
        cy_mm=cy_mm,
        cz_mm=cz_mm,
        a_mm=a_mm,
        b_mm=b_mm,
        phi_rad=math.radians(phi_deg),
        valid=valid,
    )


def _precise_config() -> StemRotationOptimizationConfig:
    return StemRotationOptimizationConfig(
        coarse_angle_step_deg=1.0,
        fine_angle_step_deg=0.1,
        fine_search_half_width_deg=2.0,
        score_tolerance_mm=1.0e-6,
        min_gain_mm=0.1,
        min_rotation_deg=0.1,
        minimum_model_quality=0.0,
        minimum_valid_slice_ratio=0.5,
        minimum_valid_slice_count=1,
        diagnostic_quantile=0.05,
        numerical_tolerance=1.0e-9,
    )


def test_centered_circle_prefers_zero_for_every_mode() -> None:
    model = straight_round_stem()

    for mode in (
        PriorityMode.VORSCHNITT_KRITISCH,
        PriorityMode.NACHSCHNITT_KRITISCH,
    ):
        result = optimize_stem_rotation(model, mode)
        assert result.valid is True
        assert result.should_rotate is False
        assert result.delta_angle_deg == 0.0
        assert result.optimal_candidate_angle_deg == 0.0
        assert result.gain_mm == pytest.approx(0.0)
        assert result.reason_code is StemRotationReasonCode.LOW_GAIN


def test_centered_ellipse_aligns_large_axis_with_vertical_priority() -> None:
    model = _model(_slice(a_mm=120.0, b_mm=80.0, phi_deg=0.0))

    result = optimize_stem_rotation(
        model,
        PriorityMode.VORSCHNITT_KRITISCH,
        config=_precise_config(),
    )

    assert result.valid is True
    assert result.should_rotate is True
    assert result.delta_angle_deg == pytest.approx(90.0, abs=0.11)
    assert result.score_current_mm == pytest.approx(160.0)
    assert result.score_optimal_mm == pytest.approx(240.0)
    assert result.gain_mm == pytest.approx(80.0)


def test_centered_ellipse_aligns_large_axis_with_horizontal_priority() -> None:
    model = _model(_slice(a_mm=120.0, b_mm=80.0, phi_deg=90.0))

    result = optimize_stem_rotation(
        model,
        PriorityMode.NACHSCHNITT_KRITISCH,
        config=_precise_config(),
    )

    assert result.valid is True
    assert result.should_rotate is True
    assert result.delta_angle_deg == pytest.approx(90.0, abs=0.11)
    assert result.score_current_mm == pytest.approx(160.0)
    assert result.score_optimal_mm == pytest.approx(240.0)


def test_optimizer_returns_negative_rotation_when_that_is_the_shortest_optimum() -> None:
    model = _model(_slice(a_mm=120.0, b_mm=80.0, phi_deg=-60.0))

    result = optimize_stem_rotation(
        model,
        PriorityMode.VORSCHNITT_KRITISCH,
        config=_precise_config(),
    )

    assert result.valid is True
    assert result.should_rotate is True
    assert result.delta_angle_deg == pytest.approx(-30.0, abs=0.11)


def test_offset_circle_rotates_correctly_for_horizontal_priority() -> None:
    model = _model(_slice(cy_mm=30.0))

    result = optimize_stem_rotation(
        model,
        PriorityMode.NACHSCHNITT_KRITISCH,
        config=_precise_config(),
    )

    assert result.valid is True
    assert result.delta_angle_deg == pytest.approx(90.0, abs=0.11)
    assert result.score_current_mm == pytest.approx(140.0)
    assert result.score_optimal_mm == pytest.approx(2.0 * math.sqrt(9100.0), rel=1.0e-6)


def test_offset_ellipse_finds_compromise_between_center_and_ovality() -> None:
    model = _model(_slice(cy_mm=50.0, cz_mm=50.0, a_mm=120.0, b_mm=70.0, phi_deg=0.0))

    result = optimize_stem_rotation(
        model,
        PriorityMode.VORSCHNITT_KRITISCH,
        config=replace(_precise_config(), fine_angle_step_deg=0.05),
    )

    assert result.valid is True
    assert result.should_rotate is True
    selected_angle = result.delta_angle_deg
    current_score = result.score_current_mm
    optimal_score = result.score_optimal_mm
    assert selected_angle is not None
    assert current_score is not None
    assert optimal_score is not None
    assert 5.0 < abs(selected_angle) < 85.0
    assert optimal_score > current_score


def test_worst_slice_controls_score_and_can_switch_at_optimum() -> None:
    model = _model(
        _slice(x_mm=0.0, a_mm=100.0, b_mm=60.0),
        _slice(x_mm=50.0, a_mm=72.0, b_mm=72.0),
    )

    result = optimize_stem_rotation(
        model,
        PriorityMode.VORSCHNITT_KRITISCH,
        config=_precise_config(),
    )

    assert result.valid is True
    assert result.limiting_x_current_mm == pytest.approx(0.0)
    assert result.limiting_x_optimal_mm == pytest.approx(50.0)
    assert result.score_current_mm == pytest.approx(120.0)
    assert result.score_optimal_mm == pytest.approx(144.0)


def test_score_tolerance_prefers_shorter_path() -> None:
    almost_round = _model(_slice(a_mm=100.0, b_mm=99.0))

    result = optimize_stem_rotation(
        almost_round,
        PriorityMode.VORSCHNITT_KRITISCH,
        config=replace(
            _precise_config(),
            score_tolerance_mm=3.0,
            min_gain_mm=0.0,
        ),
    )

    assert result.valid is True
    assert result.optimal_candidate_angle_deg == 0.0
    assert result.delta_angle_deg == 0.0
    assert result.should_rotate is False


def test_minimum_rotation_suppresses_a_small_stell_angle() -> None:
    almost_aligned = _model(_slice(a_mm=120.0, b_mm=80.0, phi_deg=89.5))

    result = optimize_stem_rotation(
        almost_aligned,
        PriorityMode.VORSCHNITT_KRITISCH,
        config=replace(
            _precise_config(),
            score_tolerance_mm=0.0,
            min_gain_mm=0.0,
            min_rotation_deg=1.0,
        ),
    )

    assert result.valid is True
    assert result.should_rotate is False
    assert result.delta_angle_deg == 0.0
    assert result.optimal_candidate_angle_deg == pytest.approx(0.5, abs=0.11)
    assert result.gain_mm is not None and result.gain_mm > 0
    assert result.reason_code is StemRotationReasonCode.ROTATION_BELOW_MINIMUM


def test_minimum_gain_suppresses_command_but_keeps_potential_gain() -> None:
    model = _model(_slice(a_mm=120.0, b_mm=80.0))

    result = optimize_stem_rotation(
        model,
        PriorityMode.VORSCHNITT_KRITISCH,
        config=replace(_precise_config(), min_gain_mm=100.0),
    )

    assert result.valid is True
    assert result.should_rotate is False
    assert result.delta_angle_deg == 0.0
    assert result.optimal_candidate_angle_deg == pytest.approx(90.0)
    assert result.gain_mm == pytest.approx(80.0)
    assert result.reason_code is StemRotationReasonCode.LOW_GAIN


def test_mechanical_limits_bound_the_selected_rotation() -> None:
    model = _model(_slice(a_mm=120.0, b_mm=80.0))
    constraints = RotationConstraints(negative_limit_deg=-10.0, positive_limit_deg=20.0)

    result = optimize_stem_rotation(
        model,
        PriorityMode.VORSCHNITT_KRITISCH,
        constraints=constraints,
        config=_precise_config(),
    )

    assert result.valid is True
    assert result.should_rotate is True
    assert result.delta_angle_deg == pytest.approx(20.0)
    assert (
        constraints.negative_limit_deg <= result.delta_angle_deg <= constraints.positive_limit_deg
    )


def test_wide_mechanical_range_is_reduced_to_one_equivalent_180_degree_interval() -> None:
    assert effective_search_bounds(
        RotationConstraints(negative_limit_deg=-10.0, positive_limit_deg=200.0)
    ) == pytest.approx((-10.0, 170.0))
    assert effective_search_bounds(
        RotationConstraints(negative_limit_deg=-200.0, positive_limit_deg=10.0)
    ) == pytest.approx((-170.0, 10.0))


def test_invalid_mode_model_quality_and_geometry_fail_closed() -> None:
    valid_model = straight_round_stem()
    invalid_mode = optimize_stem_rotation(valid_model, PriorityMode.NONE)
    low_quality = optimize_stem_rotation(
        StemSliceModel(slices=valid_model.slices, model_quality=0.2),
        PriorityMode.VORSCHNITT_KRITISCH,
        config=replace(_precise_config(), minimum_model_quality=0.5),
    )
    outside = optimize_stem_rotation(
        _model(_slice(cy_mm=150.0)),
        PriorityMode.VORSCHNITT_KRITISCH,
        config=_precise_config(),
    )
    constraint_conflict = optimize_stem_rotation(
        valid_model,
        PriorityMode.VORSCHNITT_KRITISCH,
        constraints=RotationConstraints(negative_limit_deg=5.0, positive_limit_deg=20.0),
    )

    assert invalid_mode.valid is False
    assert invalid_mode.delta_angle_deg is None
    assert invalid_mode.reason_code is StemRotationReasonCode.INVALID_PRIORITY_MODE
    assert low_quality.valid is False
    assert low_quality.reason_code is StemRotationReasonCode.MODEL_QUALITY_LOW
    assert outside.valid is False
    assert outside.reason_code is StemRotationReasonCode.NO_USABLE_GEOMETRY
    assert constraint_conflict.valid is False
    assert constraint_conflict.reason_code is StemRotationReasonCode.ROTATION_CONSTRAINT_CONFLICT


def test_result_is_deterministic_and_gain_is_never_negative() -> None:
    model = opposed_curvature_and_ovality_stem()
    config = replace(_precise_config(), fine_angle_step_deg=0.25)

    first = optimize_stem_rotation(model, PriorityMode.VORSCHNITT_KRITISCH, config=config)
    second = optimize_stem_rotation(model, PriorityMode.VORSCHNITT_KRITISCH, config=config)

    assert first == second
    assert first.gain_mm is not None
    assert first.gain_mm >= -config.numerical_tolerance
    assert any(point.angle_deg == 0.0 for point in first.score_curve)


def test_coarse_fine_result_matches_dense_reference_curve() -> None:
    model = _model(
        _slice(
            x_mm=0.0,
            cy_mm=35.0,
            cz_mm=-20.0,
            a_mm=122.0,
            b_mm=74.0,
            phi_deg=17.0,
        ),
        _slice(
            x_mm=50.0,
            cy_mm=-25.0,
            cz_mm=18.0,
            a_mm=108.0,
            b_mm=82.0,
            phi_deg=-38.0,
        ),
    )
    constraints = RotationConstraints(negative_limit_deg=-80.0, positive_limit_deg=80.0)
    config = replace(
        _precise_config(),
        coarse_angle_step_deg=5.0,
        fine_angle_step_deg=0.1,
        fine_search_half_width_deg=5.0,
        score_tolerance_mm=0.0,
    )

    result = optimize_stem_rotation(
        model,
        PriorityMode.VORSCHNITT_KRITISCH,
        constraints=constraints,
        config=config,
    )
    dense_curve = build_stem_angle_score_curve(
        model,
        PriorityMode.VORSCHNITT_KRITISCH,
        constraints=constraints,
        angle_step_deg=0.1,
    )
    dense_maximum = max(point.score_mm for point in dense_curve if point.score_mm is not None)

    assert result.valid is True
    assert result.score_optimal_mm == pytest.approx(dense_maximum, abs=1.0e-8)


@pytest.mark.parametrize(
    "builder",
    [
        straight_round_stem,
        strongly_oval_stem,
        strongly_curved_stem,
        aligned_curvature_and_ovality_stem,
        opposed_curvature_and_ovality_stem,
        local_bottleneck_stem,
        noisy_stem,
        stem_with_short_gap,
    ],
)
def test_synthetic_model_fixtures_are_valid_and_reproducible(
    builder: Callable[[], StemSliceModel],
) -> None:
    first = builder()
    second = builder()

    assert first.validate() == second.validate()
    assert first.slices


def test_complete_diagnostic_curve_has_expected_resolution_and_current_angle() -> None:
    model = strongly_oval_stem(count=120)

    curve = build_stem_angle_score_curve(
        model,
        PriorityMode.VORSCHNITT_KRITISCH,
        angle_step_deg=0.5,
    )

    assert len(curve) == 361
    assert any(point.angle_deg == 0.0 for point in curve)
    assert all(point.valid and point.score_mm is not None for point in curve)
