from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Literal

from cv_contracts import (
    RuntimeStemRotationAngleScoreView,
    RuntimeStemRotationChordView,
    RuntimeStemRotationCrossSectionView,
    RuntimeStemRotationDeltaGeometryView,
    RuntimeStemRotationDiagnosticsView,
    RuntimeStemRotationLongitudinalSliceView,
    RuntimeStemRotationViewModel,
    RuntimeStemRotationViewPoint,
)

from cv_runtime.payloads import StemRotationOptimizationPayload
from cv_runtime.stem_rotation_geometry import rotate_slice, symmetric_usable_span
from cv_runtime.stem_rotation_types import PriorityAxis, SliceScore, StemSliceGeometry

_ELLIPSE_POINT_COUNT = 64
_X_TOLERANCE_MM = 1.0e-6


def build_stem_rotation_view_model(
    result: StemRotationOptimizationPayload,
) -> RuntimeStemRotationViewModel:
    """Prepare all domain geometry consumed by the read-only industrial HMI."""
    axis = _priority_axis(result.priority_axis)
    slices = _domain_slices(result)
    comparison_angle_deg = _comparison_angle(result)
    current_limiting_x = result.limiting_x_current_mm
    optimal_limiting_x = result.limiting_x_optimal_mm
    x_eval_start = _optional_float(result.replay.optimizer_config.get("x_eval_start_mm"))
    x_eval_end = _optional_float(result.replay.optimizer_config.get("x_eval_end_mm"))

    longitudinal: list[RuntimeStemRotationLongitudinalSliceView] = []
    delta_by_x: list[RuntimeStemRotationDeltaGeometryView] = []
    if axis is not None:
        for stem_slice in slices:
            current = rotate_slice(stem_slice, 0.0)
            optimal = (
                rotate_slice(stem_slice, math.radians(comparison_angle_deg))
                if comparison_angle_deg is not None
                else None
            )
            current_score = symmetric_usable_span(current, axis)
            optimal_score = symmetric_usable_span(optimal, axis) if optimal is not None else None
            current_center, current_negative, current_positive = _axis_projection(current, axis)
            optimal_projection = _axis_projection(optimal, axis) if optimal is not None else None
            in_range = _in_evaluation_range(stem_slice.x_mm, x_eval_start, x_eval_end)
            limiting_current = _same_x(stem_slice.x_mm, current_limiting_x)
            limiting_optimal = _same_x(stem_slice.x_mm, optimal_limiting_x)
            longitudinal.append(
                RuntimeStemRotationLongitudinalSliceView(
                    x_mm=stem_slice.x_mm,
                    in_evaluation_range=in_range,
                    current_center_axis_mm=current_center,
                    current_outline_negative_mm=current_negative,
                    current_outline_positive_mm=current_positive,
                    optimal_center_axis_mm=(
                        optimal_projection[0] if optimal_projection is not None else None
                    ),
                    optimal_outline_negative_mm=(
                        optimal_projection[1] if optimal_projection is not None else None
                    ),
                    optimal_outline_positive_mm=(
                        optimal_projection[2] if optimal_projection is not None else None
                    ),
                    mean_diameter_mm=stem_slice.a_mm + stem_slice.b_mm,
                    ovality_ratio=(stem_slice.a_mm - stem_slice.b_mm) / stem_slice.a_mm,
                    quality=stem_slice.quality,
                    interpolated=stem_slice.interpolated,
                    limiting_current=limiting_current,
                    limiting_optimal=limiting_optimal,
                )
            )
            delta_by_x.append(
                _delta_geometry(
                    stem_slice.x_mm,
                    current_score,
                    optimal_score,
                    in_evaluation_range=in_range,
                    limiting_current=limiting_current,
                    limiting_optimal=limiting_optimal,
                )
            )

    current_critical = _critical_cross_section(
        slices,
        current_limiting_x,
        axis,
        comparison_angle_deg,
    )
    optimal_critical = _critical_cross_section(
        slices,
        optimal_limiting_x,
        axis,
        comparison_angle_deg,
    )
    config = dict(result.replay.optimizer_config)
    score_tolerance = _optional_float(config.get("score_tolerance_mm"))
    fit_diagnostics = result.replay.fit_diagnostics
    return RuntimeStemRotationViewModel(
        rotation_reference_view=result.rotation_reference_view,
        positive_rotation=result.positive_rotation,
        timestamp=result.timestamp,
        state=result.state,
        result_ready=result.result_ready,
        result_valid=result.result_valid,
        priority_source=result.priority_source,
        priority_mode=result.priority_mode,
        priority_axis=result.priority_axis,
        priority_revision=result.priority_revision,
        optimization_enable=result.optimization_enable,
        should_rotate=result.should_rotate,
        rotation_delta_deg=result.rotation_delta_deg,
        rotation_direction=_rotation_direction(result),
        gain_mm=max(0.0, result.gain_mm) if result.gain_mm is not None else None,
        model_quality=result.model_quality,
        stem_id=result.stem_id,
        session_id=result.session_id,
        model_hash=result.model_hash,
        reason_code=result.reason_code,
        reason_codes=result.reason_codes,
        limiting_x_current_mm=current_limiting_x,
        limiting_x_optimal_mm=optimal_limiting_x,
        comparison_angle_deg=comparison_angle_deg,
        stem_longitudinal_geometry=longitudinal,
        current_critical_cross_section=current_critical,
        optimal_critical_cross_section=optimal_critical,
        delta_geometry_by_x=delta_by_x,
        diagnostics=RuntimeStemRotationDiagnosticsView(
            score_current_mm=result.score_current_mm,
            score_optimal_mm=result.score_optimal_mm,
            optimal_candidate_angle_deg=result.optimal_candidate_angle_deg,
            plateau_width_deg=result.plateau_width_deg,
            score_tolerance_mm=score_tolerance,
            valid_slice_count=result.valid_slice_count,
            evaluated_slice_count=result.evaluated_slice_count,
            mean_fit_quality=_mean(item.fit_quality for item in fit_diagnostics),
            mean_observation_quality=_mean(
                item.mean_observation_quality for item in fit_diagnostics
            ),
            mean_center_residual_rms_mm=_mean(
                item.center_residual_rms_mm for item in fit_diagnostics
            ),
            mean_width_residual_rms_mm=_mean(
                item.width_residual_rms_mm for item in fit_diagnostics
            ),
            minimum_phase_coverage_ratio=_minimum(
                item.phase_coverage_ratio for item in fit_diagnostics
            ),
            fit_diagnostics=[
                item.model_dump(mode="json") for item in fit_diagnostics
            ],
            active_configuration={
                **config,
                "rotation_negative_limit_deg": result.replay.rotation_constraints.get(
                    "negative_limit_deg"
                ),
                "rotation_positive_limit_deg": result.replay.rotation_constraints.get(
                    "positive_limit_deg"
                ),
            },
            angle_score_curve=[
                RuntimeStemRotationAngleScoreView(
                    angle_deg=item.angle_deg,
                    score_mm=item.score_mm,
                    valid=item.valid,
                    current=abs(item.angle_deg) <= 1.0e-9,
                    selected=(
                        result.optimal_candidate_angle_deg is not None
                        and abs(item.angle_deg - result.optimal_candidate_angle_deg) <= 1.0e-7
                    ),
                )
                for item in result.score_curve
            ],
        ),
    )


def _domain_slices(result: StemRotationOptimizationPayload) -> tuple[StemSliceGeometry, ...]:
    slices: list[StemSliceGeometry] = []
    for item in result.replay.processed_slices:
        if (
            not item.valid
            or item.cy_mm is None
            or item.cz_mm is None
            or item.a_mm is None
            or item.b_mm is None
            or item.phi_rad is None
        ):
            continue
        try:
            slices.append(
                StemSliceGeometry(
                    x_mm=item.x_mm,
                    cy_mm=item.cy_mm,
                    cz_mm=item.cz_mm,
                    a_mm=item.a_mm,
                    b_mm=item.b_mm,
                    phi_rad=item.phi_rad,
                    quality=item.quality,
                    valid=True,
                    interpolated=item.interpolated,
                    reason_codes=tuple(item.reason_codes),
                ).validate()
            )
        except ValueError:
            continue
    return tuple(sorted(slices, key=lambda item: item.x_mm))


def _critical_cross_section(
    slices: Sequence[StemSliceGeometry],
    limiting_x_mm: float | None,
    axis: PriorityAxis | None,
    comparison_angle_deg: float | None,
) -> RuntimeStemRotationCrossSectionView | None:
    if not slices or limiting_x_mm is None or axis is None:
        return None
    stem_slice = min(slices, key=lambda item: abs(item.x_mm - limiting_x_mm))
    if not _same_x(stem_slice.x_mm, limiting_x_mm):
        return None
    current = rotate_slice(stem_slice, 0.0)
    optimal = (
        rotate_slice(stem_slice, math.radians(comparison_angle_deg))
        if comparison_angle_deg is not None
        else None
    )
    current_score = symmetric_usable_span(current, axis)
    optimal_score = symmetric_usable_span(optimal, axis) if optimal is not None else None
    return RuntimeStemRotationCrossSectionView(
        x_mm=stem_slice.x_mm,
        current_center=_point(current.cy_mm, current.cz_mm),
        optimal_center=_point(optimal.cy_mm, optimal.cz_mm) if optimal is not None else None,
        current_ellipse=_ellipse_points(current),
        optimal_ellipse=_ellipse_points(optimal) if optimal is not None else [],
        current_symmetric_chord=_chord(current_score, axis),
        optimal_symmetric_chord=(
            _chord(optimal_score, axis) if optimal_score is not None else None
        ),
        current_phi_deg=math.degrees(current.phi_rad),
        optimal_phi_deg=math.degrees(optimal.phi_rad) if optimal is not None else None,
        a_mm=stem_slice.a_mm,
        b_mm=stem_slice.b_mm,
        quality=stem_slice.quality,
    )


def _ellipse_points(stem_slice: StemSliceGeometry) -> list[RuntimeStemRotationViewPoint]:
    cosine = math.cos(stem_slice.phi_rad)
    sine = math.sin(stem_slice.phi_rad)
    points: list[RuntimeStemRotationViewPoint] = []
    for index in range(_ELLIPSE_POINT_COUNT):
        parameter = 2.0 * math.pi * index / _ELLIPSE_POINT_COUNT
        local_major = stem_slice.a_mm * math.cos(parameter)
        local_minor = stem_slice.b_mm * math.sin(parameter)
        points.append(
            _point(
                stem_slice.cy_mm + cosine * local_major - sine * local_minor,
                stem_slice.cz_mm + sine * local_major + cosine * local_minor,
            )
        )
    return points


def _axis_projection(
    stem_slice: StemSliceGeometry,
    axis: PriorityAxis,
) -> tuple[float, float, float]:
    u_y, u_z = axis.unit_vector
    major_y = math.cos(stem_slice.phi_rad)
    major_z = math.sin(stem_slice.phi_rad)
    minor_y = -major_z
    minor_z = major_y
    center = u_y * stem_slice.cy_mm + u_z * stem_slice.cz_mm
    half_extent = math.sqrt(
        (stem_slice.a_mm * (u_y * major_y + u_z * major_z)) ** 2
        + (stem_slice.b_mm * (u_y * minor_y + u_z * minor_z)) ** 2
    )
    return center, center - half_extent, center + half_extent


def _delta_geometry(
    x_mm: float,
    current: SliceScore,
    optimal: SliceScore | None,
    *,
    in_evaluation_range: bool,
    limiting_current: bool,
    limiting_optimal: bool,
) -> RuntimeStemRotationDeltaGeometryView:
    valid = current.valid and optimal is not None and optimal.valid
    current_geometry = current.symmetric_span_mm if current.valid else None
    optimal_geometry = optimal.symmetric_span_mm if optimal is not None and optimal.valid else None
    delta = (
        optimal_geometry - current_geometry
        if current_geometry is not None and optimal_geometry is not None
        else None
    )
    reason = (
        "OK"
        if valid
        else optimal.reason_code.value
        if optimal is not None and not optimal.valid
        else current.reason_code.value
    )
    return RuntimeStemRotationDeltaGeometryView(
        x_mm=x_mm,
        in_evaluation_range=in_evaluation_range,
        current_geometry_mm=current_geometry,
        optimal_geometry_mm=optimal_geometry,
        delta_geometry_mm=delta,
        valid=valid,
        reason_code=reason,
        limiting_current=limiting_current,
        limiting_optimal=limiting_optimal,
    )


def _chord(score: SliceScore, axis: PriorityAxis) -> RuntimeStemRotationChordView:
    radius = score.symmetric_span_mm / 2.0 if score.valid else 0.0
    u_y, u_z = axis.unit_vector
    return RuntimeStemRotationChordView(
        start=_point(-radius * u_y, -radius * u_z),
        end=_point(radius * u_y, radius * u_z),
        span_mm=2.0 * radius,
        valid=score.valid,
    )


def _point(y_mm: float, z_mm: float) -> RuntimeStemRotationViewPoint:
    return RuntimeStemRotationViewPoint(y_mm=y_mm, z_mm=z_mm)


def _priority_axis(value: str) -> PriorityAxis | None:
    if value == PriorityAxis.VERTICAL_Z.value:
        return PriorityAxis.VERTICAL_Z
    if value == PriorityAxis.HORIZONTAL_Y.value:
        return PriorityAxis.HORIZONTAL_Y
    return None


def _comparison_angle(result: StemRotationOptimizationPayload) -> float | None:
    candidate = result.optimal_candidate_angle_deg
    if candidate is not None and math.isfinite(candidate):
        return candidate
    delta = result.rotation_delta_deg
    return delta if delta is not None and math.isfinite(delta) else None


def _rotation_direction(
    result: StemRotationOptimizationPayload,
) -> Literal["POSITIVE", "NEGATIVE", "NONE", "UNAVAILABLE"]:
    if not result.result_valid:
        return "UNAVAILABLE"
    if not result.should_rotate or result.rotation_delta_deg is None:
        return "NONE"
    if result.rotation_delta_deg > 0:
        return "POSITIVE"
    if result.rotation_delta_deg < 0:
        return "NEGATIVE"
    return "NONE"


def _in_evaluation_range(
    x_mm: float,
    start_mm: float | None,
    end_mm: float | None,
) -> bool:
    return (start_mm is None or x_mm >= start_mm) and (end_mm is None or x_mm <= end_mm)


def _same_x(value: float, expected: float | None) -> bool:
    return expected is not None and abs(value - expected) <= _X_TOLERANCE_MM


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _mean(values: Iterable[float | None]) -> float | None:
    finite_values = [value for value in values if value is not None and math.isfinite(value)]
    return sum(finite_values) / len(finite_values) if finite_values else None


def _minimum(values: Iterable[float | None]) -> float | None:
    finite_values = [value for value in values if value is not None and math.isfinite(value)]
    return min(finite_values) if finite_values else None
