from __future__ import annotations

import math

import pytest
from cv_runtime.stem_rotation_geometry import (
    ellipse_quadratic_matrix,
    rotate_slice,
    rotation_matrix,
    score_curve_for_angles,
    score_stem,
    symmetric_usable_span,
)
from cv_runtime.stem_rotation_types import (
    PriorityAxis,
    StemRotationReasonCode,
    StemSliceGeometry,
)


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


def test_rotation_uses_canonical_y_z_order_and_does_not_mutate_input() -> None:
    source = _slice(cy_mm=10.0, cz_mm=20.0, a_mm=120.0, b_mm=80.0, phi_deg=15.0)

    matrix = rotation_matrix(math.pi / 2.0)
    rotated = rotate_slice(source, math.pi / 2.0)

    assert matrix[0] == pytest.approx((0.0, -1.0), abs=1.0e-12)
    assert matrix[1] == pytest.approx((1.0, 0.0), abs=1.0e-12)
    assert rotated.cy_mm == pytest.approx(-20.0)
    assert rotated.cz_mm == pytest.approx(10.0)
    assert rotated.phi_rad == pytest.approx(math.radians(-75.0))
    assert source.cy_mm == 10.0
    assert source.cz_mm == 20.0
    assert source.phi_rad == pytest.approx(math.radians(15.0))


def test_ellipse_matrix_and_centered_ellipse_spans_are_analytic() -> None:
    matrix = ellipse_quadratic_matrix(120.0, 80.0, 0.0)
    ellipse = _slice(a_mm=120.0, b_mm=80.0)

    horizontal = symmetric_usable_span(ellipse, PriorityAxis.HORIZONTAL_Y)
    vertical = symmetric_usable_span(ellipse, PriorityAxis.VERTICAL_Z)

    assert matrix[0][0] == pytest.approx(1.0 / 120.0**2)
    assert matrix[0][1] == pytest.approx(0.0)
    assert matrix[1][1] == pytest.approx(1.0 / 80.0**2)
    assert horizontal.symmetric_span_mm == pytest.approx(240.0)
    assert vertical.symmetric_span_mm == pytest.approx(160.0)


@pytest.mark.parametrize("angle_deg", [-180.0, -93.0, -10.5, 0.0, 44.0, 90.0, 180.0])
def test_centered_circle_score_is_independent_of_angle(angle_deg: float) -> None:
    circle = _slice(a_mm=87.5, b_mm=87.5)

    for axis in (PriorityAxis.HORIZONTAL_Y, PriorityAxis.VERTICAL_Z):
        score = score_stem((circle,), axis, delta_angle_rad=math.radians(angle_deg))
        assert score.valid is True
        assert score.available_geometry_mm == pytest.approx(175.0)


def test_offset_circle_rotates_center_and_changes_each_axis_correctly() -> None:
    circle = _slice(cy_mm=30.0, a_mm=100.0, b_mm=100.0)

    current_horizontal = symmetric_usable_span(circle, PriorityAxis.HORIZONTAL_Y)
    current_vertical = symmetric_usable_span(circle, PriorityAxis.VERTICAL_Z)
    quarter_turn = rotate_slice(circle, math.pi / 2.0)
    rotated_horizontal = symmetric_usable_span(quarter_turn, PriorityAxis.HORIZONTAL_Y)
    rotated_vertical = symmetric_usable_span(quarter_turn, PriorityAxis.VERTICAL_Z)

    assert current_horizontal.symmetric_span_mm == pytest.approx(140.0)
    assert current_vertical.symmetric_span_mm == pytest.approx(2.0 * math.sqrt(9100.0))
    assert rotated_horizontal.symmetric_span_mm == pytest.approx(current_vertical.symmetric_span_mm)
    assert rotated_vertical.symmetric_span_mm == pytest.approx(current_horizontal.symmetric_span_mm)


def test_multiple_slices_use_minimum_and_honor_evaluation_range() -> None:
    slices = (
        _slice(x_mm=0.0, a_mm=100.0, b_mm=100.0),
        _slice(x_mm=50.0, a_mm=80.0, b_mm=80.0),
        _slice(x_mm=100.0, a_mm=90.0, b_mm=90.0),
        _slice(x_mm=150.0, a_mm=60.0, b_mm=60.0, valid=False),
    )

    full = score_stem(slices, PriorityAxis.VERTICAL_Z)
    cropped = score_stem(
        slices,
        PriorityAxis.VERTICAL_Z,
        x_eval_start_mm=75.0,
        x_eval_end_mm=150.0,
    )

    assert full.available_geometry_mm == pytest.approx(160.0)
    assert full.limiting_x_mm == pytest.approx(50.0)
    assert cropped.available_geometry_mm == pytest.approx(180.0)
    assert cropped.limiting_x_mm == pytest.approx(100.0)


def test_limiting_slice_can_change_after_rotation() -> None:
    slices = (
        _slice(x_mm=0.0, a_mm=100.0, b_mm=60.0),
        _slice(x_mm=50.0, a_mm=72.0, b_mm=72.0),
    )

    current = score_stem(slices, PriorityAxis.VERTICAL_Z)
    rotated = score_stem(
        slices,
        PriorityAxis.VERTICAL_Z,
        delta_angle_rad=math.pi / 2.0,
    )

    assert current.available_geometry_mm == pytest.approx(120.0)
    assert current.limiting_x_mm == pytest.approx(0.0)
    assert rotated.available_geometry_mm == pytest.approx(144.0)
    assert rotated.limiting_x_mm == pytest.approx(50.0)


def test_score_has_180_degree_symmetry_for_offset_ellipse() -> None:
    slices = (
        _slice(x_mm=0.0, cy_mm=18.0, cz_mm=-12.0, a_mm=125.0, b_mm=78.0, phi_deg=27.0),
        _slice(x_mm=50.0, cy_mm=-8.0, cz_mm=14.0, a_mm=108.0, b_mm=88.0, phi_deg=-41.0),
    )
    for axis in (PriorityAxis.HORIZONTAL_Y, PriorityAxis.VERTICAL_Z):
        first = score_stem(slices, axis, delta_angle_rad=math.radians(23.75))
        equivalent = score_stem(slices, axis, delta_angle_rad=math.radians(203.75))
        assert first.available_geometry_mm == pytest.approx(
            equivalent.available_geometry_mm,
            abs=1.0e-9,
        )


def test_origin_outside_and_missing_axis_intersection_return_zero_with_reason() -> None:
    outside_on_axis = _slice(cy_mm=110.0, a_mm=100.0, b_mm=100.0)
    line_misses_circle = _slice(cz_mm=110.0, a_mm=100.0, b_mm=100.0)

    outside = symmetric_usable_span(outside_on_axis, PriorityAxis.HORIZONTAL_Y)
    missed = symmetric_usable_span(line_misses_circle, PriorityAxis.HORIZONTAL_Y)

    assert outside.valid is False
    assert outside.symmetric_span_mm == 0.0
    assert outside.reason_code is StemRotationReasonCode.MACHINE_AXIS_OUTSIDE_SLICE
    assert missed.valid is False
    assert missed.symmetric_span_mm == 0.0
    assert missed.reason_code is StemRotationReasonCode.NO_REAL_INTERSECTION


def test_tangent_and_near_tangent_cases_are_numerically_stable() -> None:
    tangent = symmetric_usable_span(
        _slice(cy_mm=100.0),
        PriorityAxis.HORIZONTAL_Y,
    )
    near_tangent = symmetric_usable_span(
        _slice(cy_mm=99.999999),
        PriorityAxis.HORIZONTAL_Y,
    )

    assert tangent.valid is False
    assert tangent.symmetric_span_mm == 0.0
    assert tangent.reason_code is StemRotationReasonCode.ZERO_SYMMETRIC_SPAN
    assert near_tangent.valid is True
    assert near_tangent.symmetric_span_mm == pytest.approx(0.000002, rel=1.0e-5)


def test_angle_score_curve_preserves_requested_angles_and_backend_scores() -> None:
    ellipse = _slice(a_mm=120.0, b_mm=80.0)
    requested = (-90.0, 0.0, 90.0)

    curve = score_curve_for_angles((ellipse,), PriorityAxis.VERTICAL_Z, requested)

    assert tuple(point.angle_deg for point in curve) == requested
    assert tuple(point.score_mm for point in curve) == pytest.approx((240.0, 160.0, 240.0))
