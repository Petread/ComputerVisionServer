from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import replace

from cv_runtime.stem_rotation_types import (
    AngleScorePoint,
    PriorityAxis,
    SliceScore,
    StemRotationReasonCode,
    StemRotationScore,
    StemSliceGeometry,
)

Matrix2 = tuple[tuple[float, float], tuple[float, float]]


def rotation_matrix(angle_rad: float) -> Matrix2:
    """Return the right-handed rotation matrix in canonical q=(y, z) order."""
    if not math.isfinite(angle_rad):
        raise ValueError("angle_rad must be finite")
    cosine = math.cos(angle_rad)
    sine = math.sin(angle_rad)
    return ((cosine, -sine), (sine, cosine))


def normalize_ellipse_angle_rad(angle_rad: float) -> float:
    """Normalize an unoriented ellipse axis to the half-open interval [-pi/2, pi/2)."""
    if not math.isfinite(angle_rad):
        raise ValueError("angle_rad must be finite")
    normalized = (angle_rad + math.pi / 2.0) % math.pi - math.pi / 2.0
    return 0.0 if abs(normalized) < 1.0e-15 else normalized


def rotate_slice(stem_slice: StemSliceGeometry, delta_angle_rad: float) -> StemSliceGeometry:
    """Rotate center and ellipse orientation together around the machine x-axis."""
    stem_slice.validate()
    matrix = rotation_matrix(delta_angle_rad)
    cy_rotated = matrix[0][0] * stem_slice.cy_mm + matrix[0][1] * stem_slice.cz_mm
    cz_rotated = matrix[1][0] * stem_slice.cy_mm + matrix[1][1] * stem_slice.cz_mm
    return replace(
        stem_slice,
        cy_mm=cy_rotated,
        cz_mm=cz_rotated,
        phi_rad=normalize_ellipse_angle_rad(stem_slice.phi_rad + delta_angle_rad),
    )


def ellipse_quadratic_matrix(a_mm: float, b_mm: float, phi_rad: float) -> Matrix2:
    """Return M=R(phi) diag(1/a^2, 1/b^2) R(phi)^T for one ellipse."""
    if not all(math.isfinite(value) for value in (a_mm, b_mm, phi_rad)):
        raise ValueError("ellipse parameters must be finite")
    if a_mm < b_mm or b_mm <= 0:
        raise ValueError("ellipse axes must satisfy a_mm >= b_mm > 0")
    cosine = math.cos(phi_rad)
    sine = math.sin(phi_rad)
    inv_a2 = 1.0 / (a_mm * a_mm)
    inv_b2 = 1.0 / (b_mm * b_mm)
    m_yy = cosine * cosine * inv_a2 + sine * sine * inv_b2
    m_yz = cosine * sine * (inv_a2 - inv_b2)
    m_zz = sine * sine * inv_a2 + cosine * cosine * inv_b2
    return ((m_yy, m_yz), (m_yz, m_zz))


def symmetric_usable_span(
    stem_slice: StemSliceGeometry,
    axis: PriorityAxis,
    *,
    numerical_tolerance: float = 1.0e-12,
) -> SliceScore:
    """Intersect the axis through the machine origin with one rotated ellipse.

    Substituting p(t)=t*u in (p-c)^T M (p-c)=1 yields
    A*t^2+B*t+C=0. The usable full span is twice the shorter signed distance
    from the origin to the two intersections.
    """
    stem_slice.validate()
    if numerical_tolerance <= 0 or not math.isfinite(numerical_tolerance):
        raise ValueError("numerical_tolerance must be finite and positive")
    axis_value = PriorityAxis(axis)
    u_y, u_z = axis_value.unit_vector
    matrix = ellipse_quadratic_matrix(
        stem_slice.a_mm,
        stem_slice.b_mm,
        stem_slice.phi_rad,
    )
    m_yy, m_yz = matrix[0]
    _, m_zz = matrix[1]
    cy = stem_slice.cy_mm
    cz = stem_slice.cz_mm

    a_coefficient = u_y * (m_yy * u_y + m_yz * u_z) + u_z * (m_yz * u_y + m_zz * u_z)
    b_coefficient = -2.0 * (u_y * (m_yy * cy + m_yz * cz) + u_z * (m_yz * cy + m_zz * cz))
    c_coefficient = cy * (m_yy * cy + m_yz * cz) + cz * (m_yz * cy + m_zz * cz) - 1.0
    if (
        not all(math.isfinite(value) for value in (a_coefficient, b_coefficient, c_coefficient))
        or a_coefficient <= 0
    ):
        return SliceScore(
            x_mm=stem_slice.x_mm,
            symmetric_span_mm=0.0,
            valid=False,
            t_negative_mm=None,
            t_positive_mm=None,
            reason_code=StemRotationReasonCode.NUMERICAL_ERROR,
        )

    discriminant = b_coefficient * b_coefficient - 4.0 * a_coefficient * c_coefficient
    discriminant_scale = max(
        1.0,
        b_coefficient * b_coefficient,
        abs(4.0 * a_coefficient * c_coefficient),
    )
    discriminant_tolerance = numerical_tolerance * discriminant_scale
    if discriminant < -discriminant_tolerance:
        return SliceScore(
            x_mm=stem_slice.x_mm,
            symmetric_span_mm=0.0,
            valid=False,
            t_negative_mm=None,
            t_positive_mm=None,
            reason_code=StemRotationReasonCode.NO_REAL_INTERSECTION,
        )
    discriminant = max(0.0, discriminant)
    roots = _stable_quadratic_roots(
        a_coefficient,
        b_coefficient,
        c_coefficient,
        math.sqrt(discriminant),
        numerical_tolerance=numerical_tolerance,
    )
    if roots is None:
        return SliceScore(
            x_mm=stem_slice.x_mm,
            symmetric_span_mm=0.0,
            valid=False,
            t_negative_mm=None,
            t_positive_mm=None,
            reason_code=StemRotationReasonCode.NUMERICAL_ERROR,
        )
    t_negative, t_positive = roots
    root_scale = max(1.0, abs(t_negative), abs(t_positive))
    root_tolerance = numerical_tolerance * root_scale
    if (
        t_negative > root_tolerance
        or t_positive < -root_tolerance
        or c_coefficient > numerical_tolerance
    ):
        return SliceScore(
            x_mm=stem_slice.x_mm,
            symmetric_span_mm=0.0,
            valid=False,
            t_negative_mm=t_negative,
            t_positive_mm=t_positive,
            reason_code=StemRotationReasonCode.MACHINE_AXIS_OUTSIDE_SLICE,
        )

    symmetric_radius = max(0.0, min(t_positive, -t_negative))
    symmetric_span = 2.0 * symmetric_radius
    if symmetric_span <= root_tolerance:
        return SliceScore(
            x_mm=stem_slice.x_mm,
            symmetric_span_mm=0.0,
            valid=False,
            t_negative_mm=t_negative,
            t_positive_mm=t_positive,
            reason_code=StemRotationReasonCode.ZERO_SYMMETRIC_SPAN,
        )
    return SliceScore(
        x_mm=stem_slice.x_mm,
        symmetric_span_mm=symmetric_span,
        valid=True,
        t_negative_mm=t_negative,
        t_positive_mm=t_positive,
    )


def score_stem(
    slices: Sequence[StemSliceGeometry],
    axis: PriorityAxis,
    *,
    delta_angle_rad: float = 0.0,
    x_eval_start_mm: float | None = None,
    x_eval_end_mm: float | None = None,
    diagnostic_quantile: float | None = None,
    numerical_tolerance: float = 1.0e-12,
) -> StemRotationScore:
    """Return the minimum symmetric span over all valid slices in the x range."""
    if not math.isfinite(delta_angle_rad):
        raise ValueError("delta_angle_rad must be finite")
    _validate_x_range(x_eval_start_mm, x_eval_end_mm)
    if diagnostic_quantile is not None and not 0 <= diagnostic_quantile <= 1:
        raise ValueError("diagnostic_quantile must be between zero and one")
    axis_value = PriorityAxis(axis)
    validated = [stem_slice.validate() for stem_slice in slices]
    selected = [
        stem_slice
        for stem_slice in validated
        if stem_slice.valid
        and (x_eval_start_mm is None or stem_slice.x_mm >= x_eval_start_mm)
        and (x_eval_end_mm is None or stem_slice.x_mm <= x_eval_end_mm)
    ]
    selected.sort(key=lambda item: item.x_mm)
    if not selected:
        return StemRotationScore(
            valid=False,
            axis=axis_value,
            angle_rad=delta_angle_rad,
            available_geometry_mm=None,
            limiting_x_mm=None,
            reason_code=StemRotationReasonCode.NO_VALID_SLICES_IN_EVALUATION_RANGE,
        )

    slice_scores = tuple(
        symmetric_usable_span(
            rotate_slice(stem_slice, delta_angle_rad),
            axis_value,
            numerical_tolerance=numerical_tolerance,
        )
        for stem_slice in selected
    )
    limiting = min(slice_scores, key=lambda item: (item.symmetric_span_mm, item.x_mm))
    spans = [item.symmetric_span_mm for item in slice_scores]
    diagnostic = (
        _linear_quantile(spans, diagnostic_quantile) if diagnostic_quantile is not None else None
    )
    return StemRotationScore(
        valid=True,
        axis=axis_value,
        angle_rad=delta_angle_rad,
        available_geometry_mm=limiting.symmetric_span_mm,
        limiting_x_mm=limiting.x_mm,
        slice_scores=slice_scores,
        diagnostic_quantile_mm=diagnostic,
        reason_code=limiting.reason_code,
    )


def score_curve_for_angles(
    slices: Sequence[StemSliceGeometry],
    axis: PriorityAxis,
    angles_deg: Iterable[float],
    *,
    x_eval_start_mm: float | None = None,
    x_eval_end_mm: float | None = None,
    diagnostic_quantile: float | None = None,
) -> tuple[AngleScorePoint, ...]:
    """Evaluate caller-provided relative angles without changing their order."""
    points: list[AngleScorePoint] = []
    for angle_deg in angles_deg:
        if not math.isfinite(angle_deg):
            raise ValueError("all candidate angles must be finite")
        score = score_stem(
            slices,
            axis,
            delta_angle_rad=math.radians(angle_deg),
            x_eval_start_mm=x_eval_start_mm,
            x_eval_end_mm=x_eval_end_mm,
            diagnostic_quantile=diagnostic_quantile,
        )
        points.append(
            AngleScorePoint(
                angle_deg=float(angle_deg),
                score_mm=score.available_geometry_mm,
                limiting_x_mm=score.limiting_x_mm,
                valid=score.valid,
                reason_code=score.reason_code,
            )
        )
    return tuple(points)


def _stable_quadratic_roots(
    a_coefficient: float,
    b_coefficient: float,
    c_coefficient: float,
    sqrt_discriminant: float,
    *,
    numerical_tolerance: float,
) -> tuple[float, float] | None:
    if sqrt_discriminant <= numerical_tolerance and abs(b_coefficient) <= numerical_tolerance:
        root = -b_coefficient / (2.0 * a_coefficient)
        return (root, root) if math.isfinite(root) else None
    sign = 1.0 if b_coefficient >= 0 else -1.0
    q_value = -0.5 * (b_coefficient + sign * sqrt_discriminant)
    if abs(q_value) <= numerical_tolerance:
        root = -b_coefficient / (2.0 * a_coefficient)
        return (root, root) if math.isfinite(root) else None
    first = q_value / a_coefficient
    second = c_coefficient / q_value
    if not math.isfinite(first) or not math.isfinite(second):
        return None
    return (min(first, second), max(first, second))


def _linear_quantile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = quantile * (len(ordered) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] * (1.0 - fraction) + ordered[upper_index] * fraction


def _validate_x_range(start_mm: float | None, end_mm: float | None) -> None:
    for name, value in (("x_eval_start_mm", start_mm), ("x_eval_end_mm", end_mm)):
        if value is not None and not math.isfinite(value):
            raise ValueError(f"{name} must be finite when provided")
    if start_mm is not None and end_mm is not None and end_mm < start_mm:
        raise ValueError("x evaluation range must not be reversed")
