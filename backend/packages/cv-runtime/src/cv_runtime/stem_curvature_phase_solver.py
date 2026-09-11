from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

ALGORITHM_VERSION = "stem-curvature-phase.robust-harmonic.v1"


@dataclass(frozen=True)
class StemCurvaturePhaseObservation:
    track_id: str
    frame_id: str
    timestamp: str | None
    cumulative_phase_deg: float
    signed_bow_mm: float
    curvature_rms_mm: float
    curvature_maxabs_mm: float | None = None
    curvature_peak_to_peak_mm: float | None = None
    geometry_confidence: float = 1.0
    phase_confidence: float = 1.0
    common_coverage: float = 1.0
    segment_x_start_mm: float | None = None
    segment_x_end_mm: float | None = None
    bow_shape_hint: str = "uncertain"

    def validate(self) -> StemCurvaturePhaseObservation:
        if not self.track_id:
            raise ValueError("track_id is required")
        if not self.frame_id:
            raise ValueError("frame_id is required")
        numeric = (
            self.cumulative_phase_deg,
            self.signed_bow_mm,
            self.curvature_rms_mm,
            self.geometry_confidence,
            self.phase_confidence,
            self.common_coverage,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("observation values must be finite")
        if self.curvature_rms_mm < 0:
            raise ValueError("curvature_rms_mm must not be negative")
        if not 0 <= self.geometry_confidence <= 1:
            raise ValueError("geometry_confidence must be between zero and one")
        if not 0 <= self.phase_confidence <= 1:
            raise ValueError("phase_confidence must be between zero and one")
        if not 0 <= self.common_coverage <= 1:
            raise ValueError("common_coverage must be between zero and one")
        if (self.segment_x_start_mm is None) != (self.segment_x_end_mm is None):
            raise ValueError("common segment bounds must be provided together")
        if self.segment_x_start_mm is not None and (
            not math.isfinite(self.segment_x_start_mm)
            or not math.isfinite(self.segment_x_end_mm)
            or self.segment_x_end_mm <= self.segment_x_start_mm
        ):
            raise ValueError("common segment bounds must define a finite positive interval")
        return self


@dataclass(frozen=True)
class StemCurvaturePhaseSolverConfig:
    minimum_observations: int = 7
    minimum_phase_span_deg: float = 90.0
    minimum_geometry_confidence: float = 0.4
    minimum_phase_confidence: float = 0.4
    minimum_common_coverage: float = 0.4
    maximum_observations: int = 240
    robust_huber_delta: float = 1.5
    robust_iterations: int = 8
    maximum_fit_condition: float = 1.0e8
    maximum_signed_residual_ratio: float = 0.45
    maximum_energy_residual_ratio: float = 0.4
    minimum_curvature_span_mm: float = 1.5
    bow_relevance_threshold_mm: float = 3.0
    target_observation_tolerance_deg: float = 12.0
    maximum_target_extrapolation_deg: float = 30.0
    s_bow_residual_minimum_mm: float = 2.0
    s_bow_residual_ratio: float = 0.3

    def validate(self) -> StemCurvaturePhaseSolverConfig:
        if self.minimum_observations < 3:
            raise ValueError("minimum_observations must be at least three")
        if self.maximum_observations < self.minimum_observations:
            raise ValueError("maximum_observations must cover minimum_observations")
        if not 0 < self.minimum_phase_span_deg <= 180:
            raise ValueError("minimum_phase_span_deg must be in (0, 180]")
        for name in (
            "minimum_geometry_confidence",
            "minimum_phase_confidence",
            "minimum_common_coverage",
        ):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")
        if self.robust_huber_delta <= 0 or self.robust_iterations < 1:
            raise ValueError("robust fit settings must be positive")
        if self.maximum_fit_condition <= 1:
            raise ValueError("maximum_fit_condition must be greater than one")
        for name in (
            "maximum_signed_residual_ratio",
            "maximum_energy_residual_ratio",
            "minimum_curvature_span_mm",
            "bow_relevance_threshold_mm",
            "target_observation_tolerance_deg",
            "maximum_target_extrapolation_deg",
            "s_bow_residual_minimum_mm",
            "s_bow_residual_ratio",
        ):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} must not be negative")
        return self


@dataclass(frozen=True)
class HarmonicFit:
    coefficients: tuple[float, float, float]
    residual_rms: float
    residual_ratio: float
    condition_number: float
    inlier_weight_ratio: float
    stable: bool
    sample_weights: tuple[float, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StemCurvaturePhaseFitResult:
    observation_count: int
    observed_phase_span_deg: float
    observed_min_curvature_mm: float | None
    observed_min_phase_deg: float | None
    observed_max_curvature_mm: float | None
    observed_max_phase_deg: float | None
    observed_min_signed_bow_mm: float | None
    observed_max_signed_bow_mm: float | None
    fitted_min_curvature_mm: float | None
    fitted_max_curvature_mm: float | None
    fitted_min_signed_bow_mm: float | None
    fitted_max_signed_bow_mm: float | None
    curvature_span_mm: float | None
    target_phase_deg: float | None
    maximum_phase_deg: float | None
    target_phase_source: Literal["observed", "extrapolated", "unavailable"]
    target_extrapolation_deg: float | None
    minimum_unique: bool
    bow_relevant: bool | None
    s_bow_residual_minimum: bool
    signed_fit: HarmonicFit | None
    energy_fit: HarmonicFit | None
    confidence: float
    state: Literal[
        "COLLECTING",
        "INSUFFICIENT_ROTATION",
        "FIT_UNSTABLE",
        "AMBIGUOUS",
        "BOW_NOT_RELEVANT",
        "SOLVED",
    ]
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


def solve_stem_curvature_phase(
    observations: list[StemCurvaturePhaseObservation],
    *,
    config: StemCurvaturePhaseSolverConfig | None = None,
) -> StemCurvaturePhaseFitResult:
    cfg = (config or StemCurvaturePhaseSolverConfig()).validate()
    accepted = [item.validate() for item in observations]
    if accepted and len({item.track_id for item in accepted}) != 1:
        raise ValueError("all observations must belong to one track")
    if len(accepted) > cfg.maximum_observations:
        accepted = accepted[-cfg.maximum_observations :]

    count = len(accepted)
    if not accepted:
        return _empty_result("COLLECTING", "INSUFFICIENT_OBSERVATIONS")

    phases = np.asarray([item.cumulative_phase_deg for item in accepted], dtype=np.float64)
    signed = np.asarray([item.signed_bow_mm for item in accepted], dtype=np.float64)
    curvature = np.asarray([item.curvature_rms_mm for item in accepted], dtype=np.float64)
    weights = np.asarray(
        [
            max(
                1.0e-6,
                item.geometry_confidence * item.phase_confidence * item.common_coverage,
            )
            for item in accepted
        ],
        dtype=np.float64,
    )
    phase_span = float(min(180.0, max(phases) - min(phases)))
    min_index = int(np.argmin(curvature))
    max_index = int(np.argmax(curvature))
    observed = {
        "observed_min_curvature_mm": float(curvature[min_index]),
        "observed_min_phase_deg": modulo_180(float(phases[min_index])),
        "observed_max_curvature_mm": float(curvature[max_index]),
        "observed_max_phase_deg": modulo_180(float(phases[max_index])),
        "observed_min_signed_bow_mm": float(np.min(signed)),
        "observed_max_signed_bow_mm": float(np.max(signed)),
    }
    reasons: list[str] = []
    if count < cfg.minimum_observations:
        reasons.append("INSUFFICIENT_OBSERVATIONS")
        return _partial_result(count, phase_span, observed, "COLLECTING", reasons)

    signed_fit = _fit_harmonic(
        phases,
        signed,
        weights,
        harmonic=1,
        config=cfg,
        residual_scale_floor=1.0,
        maximum_residual_ratio=cfg.maximum_signed_residual_ratio,
    )
    energy_values = np.square(curvature)
    energy_fit = _fit_harmonic(
        phases,
        energy_values,
        weights,
        harmonic=2,
        config=cfg,
        residual_scale_floor=1.0,
        maximum_residual_ratio=cfg.maximum_energy_residual_ratio,
    )

    signed_offset, signed_cos, signed_sin = signed_fit.coefficients
    signed_amplitude = math.hypot(signed_cos, signed_sin)
    fitted_min_signed = signed_offset - signed_amplitude
    fitted_max_signed = signed_offset + signed_amplitude

    energy_offset, energy_cos, energy_sin = energy_fit.coefficients
    energy_amplitude = math.hypot(energy_cos, energy_sin)
    fitted_min_energy = energy_offset - energy_amplitude
    fitted_max_energy = energy_offset + energy_amplitude
    energy_floor_tolerance = max(1.0, 0.2 * max(fitted_max_energy, 0.0))
    energy_physical = fitted_min_energy >= -energy_floor_tolerance and fitted_max_energy >= 0
    if not energy_physical:
        energy_fit = HarmonicFit(
            coefficients=energy_fit.coefficients,
            residual_rms=energy_fit.residual_rms,
            residual_ratio=energy_fit.residual_ratio,
            condition_number=energy_fit.condition_number,
            inlier_weight_ratio=energy_fit.inlier_weight_ratio,
            stable=False,
            sample_weights=energy_fit.sample_weights,
        )
    fitted_min_curvature = math.sqrt(max(0.0, fitted_min_energy))
    fitted_max_curvature = math.sqrt(max(0.0, fitted_max_energy))
    curvature_span = fitted_max_curvature - fitted_min_curvature
    bow_relevant = fitted_max_curvature >= cfg.bow_relevance_threshold_mm
    minimum_unique = energy_fit.stable and curvature_span >= cfg.minimum_curvature_span_mm
    s_bow = any(item.bow_shape_hint == "s_bow" for item in accepted)
    s_bow_residual = s_bow and fitted_min_curvature >= max(
        cfg.s_bow_residual_minimum_mm,
        cfg.s_bow_residual_ratio * max(fitted_max_curvature, 1.0e-9),
    )

    fit_values = {
        **observed,
        "fitted_min_curvature_mm": fitted_min_curvature,
        "fitted_max_curvature_mm": fitted_max_curvature,
        "fitted_min_signed_bow_mm": fitted_min_signed,
        "fitted_max_signed_bow_mm": fitted_max_signed,
        "curvature_span_mm": curvature_span,
        "minimum_unique": minimum_unique,
        "bow_relevant": bow_relevant,
        "s_bow_residual_minimum": s_bow_residual,
        "signed_fit": signed_fit,
        "energy_fit": energy_fit,
    }
    if s_bow_residual:
        reasons.append("S_BOW_RESIDUAL_MINIMUM")
    if phase_span < cfg.minimum_phase_span_deg:
        reasons.append("INSUFFICIENT_ROTATION")
        return _fit_result(
            count,
            phase_span,
            fit_values,
            state="INSUFFICIENT_ROTATION",
            reasons=reasons,
        )
    if not signed_fit.stable:
        reasons.append("SIGNED_FIT_UNSTABLE")
    if not energy_fit.stable:
        reasons.append("ENERGY_FIT_UNSTABLE")
    if not signed_fit.stable or not energy_fit.stable:
        return _fit_result(count, phase_span, fit_values, state="FIT_UNSTABLE", reasons=reasons)
    if not bow_relevant:
        reasons.append("BOW_NOT_RELEVANT")
        return _fit_result(count, phase_span, fit_values, state="BOW_NOT_RELEVANT", reasons=reasons)
    if not minimum_unique:
        reasons.append("CURVATURE_MINIMUM_AMBIGUOUS")
        return _fit_result(count, phase_span, fit_values, state="AMBIGUOUS", reasons=reasons)

    maximum_phase = modulo_180(math.degrees(math.atan2(energy_sin, energy_cos)) / 2.0)
    target_phase = modulo_180(maximum_phase + 90.0)
    target_distance = min(
        circular_distance_180(target_phase, modulo_180(float(value))) for value in phases
    )
    extrapolation = _target_extrapolation_distance(target_phase, phases)
    if extrapolation > cfg.maximum_target_extrapolation_deg:
        reasons.append("TARGET_EXTRAPOLATION_EXCEEDS_LIMIT")
        return _fit_result(count, phase_span, fit_values, state="AMBIGUOUS", reasons=reasons)
    target_source: Literal["observed", "extrapolated"] = (
        "observed" if target_distance <= cfg.target_observation_tolerance_deg else "extrapolated"
    )
    if target_source == "extrapolated":
        reasons.append("TARGET_PHASE_EXTRAPOLATED")

    mean_quality = float(np.mean(np.clip(weights, 0.0, 1.0)))
    support_quality = min(1.0, count / max(cfg.minimum_observations + 4, 1))
    span_quality = min(1.0, phase_span / max(cfg.minimum_phase_span_deg, 1.0e-9))
    residual_quality = max(
        0.0,
        1.0
        - 0.5
        * (
            signed_fit.residual_ratio / max(cfg.maximum_signed_residual_ratio, 1.0e-9)
            + energy_fit.residual_ratio / max(cfg.maximum_energy_residual_ratio, 1.0e-9)
        ),
    )
    uniqueness_quality = min(1.0, curvature_span / max(2.0 * cfg.minimum_curvature_span_mm, 1.0e-9))
    extrapolation_quality = max(
        0.0,
        1.0 - extrapolation / max(cfg.maximum_target_extrapolation_deg, 1.0e-9),
    )
    confidence = _bounded(
        mean_quality
        * support_quality
        * span_quality
        * residual_quality
        * uniqueness_quality
        * (0.75 + 0.25 * extrapolation_quality)
    )
    return StemCurvaturePhaseFitResult(
        observation_count=count,
        observed_phase_span_deg=phase_span,
        target_phase_deg=target_phase,
        maximum_phase_deg=maximum_phase,
        target_phase_source=target_source,
        target_extrapolation_deg=extrapolation,
        confidence=confidence,
        state="SOLVED",
        reason_codes=tuple(dict.fromkeys(reasons)),
        **fit_values,
    )


def modulo_180(value: float) -> float:
    result = float(value) % 180.0
    return 0.0 if math.isclose(result, 180.0, abs_tol=1.0e-9) else result


def circular_distance_180(left: float, right: float) -> float:
    difference = abs(modulo_180(left) - modulo_180(right))
    return min(difference, 180.0 - difference)


def wrap_to_minus_90_plus_90(value: float) -> float:
    """Return the shortest signed correction for a 180-degree-periodic target."""

    wrapped = (float(value) + 90.0) % 180.0 - 90.0
    return 0.0 if math.isclose(wrapped, 0.0, abs_tol=1.0e-9) else wrapped


def _fit_harmonic(
    phases_deg: np.ndarray,
    values: np.ndarray,
    base_weights: np.ndarray,
    *,
    harmonic: int,
    config: StemCurvaturePhaseSolverConfig,
    residual_scale_floor: float,
    maximum_residual_ratio: float,
) -> HarmonicFit:
    radians = np.deg2rad(phases_deg)
    design = np.column_stack(
        (
            np.ones_like(radians),
            np.cos(harmonic * radians),
            np.sin(harmonic * radians),
        )
    )
    weights = np.clip(base_weights, 1.0e-6, None)
    coefficients = np.zeros(3, dtype=np.float64)
    for _ in range(config.robust_iterations):
        root = np.sqrt(weights)
        weighted_design = design * root[:, None]
        weighted_values = values * root
        coefficients, *_unused = np.linalg.lstsq(weighted_design, weighted_values, rcond=None)
        residuals = values - design @ coefficients
        center = float(np.median(residuals))
        mad = float(np.median(np.abs(residuals - center)))
        scale = max(1.4826 * mad, residual_scale_floor * 1.0e-6, 1.0e-9)
        limit = config.robust_huber_delta * scale
        absolute = np.abs(residuals - center)
        robust = np.ones_like(absolute)
        outside = absolute > limit
        robust[outside] = limit / np.maximum(absolute[outside], 1.0e-12)
        updated = np.clip(base_weights, 1.0e-6, None) * robust
        if np.allclose(updated, weights, rtol=1.0e-5, atol=1.0e-7):
            weights = updated
            break
        weights = updated

    residuals = values - design @ coefficients
    residual_rms = math.sqrt(
        float(np.sum(weights * np.square(residuals)) / max(np.sum(weights), 1.0e-12))
    )
    amplitude = math.hypot(float(coefficients[1]), float(coefficients[2]))
    residual_ratio = residual_rms / max(amplitude, residual_scale_floor, 1.0e-9)
    normal = design.T @ (weights[:, None] * design)
    condition = float(np.linalg.cond(normal))
    if not math.isfinite(condition):
        condition = config.maximum_fit_condition * 10.0
    inlier_weight_ratio = float(
        np.sum(weights) / max(np.sum(np.clip(base_weights, 1.0e-6, None)), 1.0e-12)
    )
    stable = condition <= config.maximum_fit_condition and residual_ratio <= maximum_residual_ratio
    return HarmonicFit(
        coefficients=tuple(float(value) for value in coefficients),
        residual_rms=residual_rms,
        residual_ratio=residual_ratio,
        condition_number=condition,
        inlier_weight_ratio=_bounded(inlier_weight_ratio),
        stable=stable,
        sample_weights=tuple(_bounded(float(value)) for value in weights),
    )


def _target_extrapolation_distance(target_phase_deg: float, phases: np.ndarray) -> float:
    lower = float(np.min(phases))
    upper = float(np.max(phases))
    center = 0.5 * (lower + upper)
    turn = round((center - target_phase_deg) / 180.0)
    candidates = [target_phase_deg + 180.0 * (turn + offset) for offset in (-1, 0, 1)]
    return min(
        0.0 if lower <= candidate <= upper else min(abs(candidate - lower), abs(candidate - upper))
        for candidate in candidates
    )


def _empty_result(state: Literal["COLLECTING"], reason: str) -> StemCurvaturePhaseFitResult:
    return StemCurvaturePhaseFitResult(
        observation_count=0,
        observed_phase_span_deg=0.0,
        observed_min_curvature_mm=None,
        observed_min_phase_deg=None,
        observed_max_curvature_mm=None,
        observed_max_phase_deg=None,
        observed_min_signed_bow_mm=None,
        observed_max_signed_bow_mm=None,
        fitted_min_curvature_mm=None,
        fitted_max_curvature_mm=None,
        fitted_min_signed_bow_mm=None,
        fitted_max_signed_bow_mm=None,
        curvature_span_mm=None,
        target_phase_deg=None,
        maximum_phase_deg=None,
        target_phase_source="unavailable",
        target_extrapolation_deg=None,
        minimum_unique=False,
        bow_relevant=None,
        s_bow_residual_minimum=False,
        signed_fit=None,
        energy_fit=None,
        confidence=0.0,
        state=state,
        reason_codes=(reason,),
    )


def _partial_result(
    count: int,
    phase_span: float,
    observed: dict[str, float],
    state: Literal["COLLECTING"],
    reasons: list[str],
) -> StemCurvaturePhaseFitResult:
    base = _empty_result(state, reasons[0])
    return StemCurvaturePhaseFitResult(
        **{
            **base.__dict__,
            "observation_count": count,
            "observed_phase_span_deg": phase_span,
            **observed,
            "reason_codes": tuple(dict.fromkeys(reasons)),
        }
    )


def _fit_result(
    count: int,
    phase_span: float,
    values: dict[str, object],
    *,
    state: Literal["INSUFFICIENT_ROTATION", "FIT_UNSTABLE", "AMBIGUOUS", "BOW_NOT_RELEVANT"],
    reasons: list[str],
) -> StemCurvaturePhaseFitResult:
    return StemCurvaturePhaseFitResult(
        observation_count=count,
        observed_phase_span_deg=phase_span,
        target_phase_deg=None,
        maximum_phase_deg=None,
        target_phase_source="unavailable",
        target_extrapolation_deg=None,
        confidence=0.0,
        state=state,
        reason_codes=tuple(dict.fromkeys(reasons)),
        **values,
    )


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
