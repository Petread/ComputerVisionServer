from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any

import numpy as np

from cv_runtime.stem_rotation_geometry import normalize_ellipse_angle_rad
from cv_runtime.stem_rotation_types import StemSliceGeometry, StemSliceModel
from cv_runtime.stem_slice_preprocessing import (
    StemSlicePreprocessingConfig,
    StemSlicePreprocessingResult,
    preprocess_stem_slice_model,
)

STEM_SLICE_MEASUREMENT_VERSION = "stem-slice-measurement.robust-projection-fit.v2"
STEM_PROJECTION_OBSERVATION_SCHEMA = "stem.projection-observations.v1"
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class StemSliceModelStatus(StrEnum):
    VALID = "VALID"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"


@dataclass(frozen=True)
class StemProjectionObservation:
    """One calibrated orthographic projection sample at one material x station.

    ``projected_center_mm`` and ``visible_width_mm`` are measured along machine y in
    the process-calibrated reference plane. ``phase_deg`` is the calibrated, unwrapped
    rotation around machine +x using the bound runtime phase convention.
    """

    x_mm: float
    phase_deg: float
    projected_center_mm: float
    visible_width_mm: float
    quality: float
    frame_sequence: int | None = None
    timestamp: str | None = None
    geometry_quality: float = 1.0
    phase_quality: float = 1.0
    segmentation_quality: float = 1.0
    tracking_quality: float = 1.0

    def validate(self) -> StemProjectionObservation:
        for name in (
            "x_mm",
            "phase_deg",
            "projected_center_mm",
            "visible_width_mm",
            "quality",
            "geometry_quality",
            "phase_quality",
            "segmentation_quality",
            "tracking_quality",
        ):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if self.visible_width_mm <= 0:
            raise ValueError("visible_width_mm must be positive")
        for name in (
            "quality",
            "geometry_quality",
            "phase_quality",
            "segmentation_quality",
            "tracking_quality",
        ):
            if not 0 <= float(getattr(self, name)) <= 1:
                raise ValueError(f"{name} must be between zero and one")
        if self.frame_sequence is not None and self.frame_sequence < 0:
            raise ValueError("frame_sequence must not be negative")
        return self

    @property
    def effective_quality(self) -> float:
        return min(
            self.quality,
            self.geometry_quality,
            self.phase_quality,
            self.segmentation_quality,
            self.tracking_quality,
        )


@dataclass(frozen=True)
class StemSliceFitConfig:
    minimum_observations: int = 7
    minimum_unique_phases: int = 6
    minimum_phase_span_deg: float = 90.0
    phase_separation_deg: float = 8.0
    minimum_observation_quality: float = 0.1
    robust_iterations: int = 8
    huber_delta: float = 1.5
    maximum_condition_number: float = 100_000.0
    maximum_center_residual_rms_mm: float = 5.0
    maximum_width_residual_rms_mm: float = 5.0
    minimum_axis_mm: float = 5.0
    maximum_axis_mm: float = 1_000.0
    near_circle_axis_ratio: float = 1.02
    minimum_slice_quality: float = 0.25

    def validate(self) -> StemSliceFitConfig:
        if self.minimum_observations < 3 or self.minimum_unique_phases < 3:
            raise ValueError("at least three observations and unique phases are required")
        if self.robust_iterations < 1:
            raise ValueError("robust_iterations must be positive")
        positive = (
            self.minimum_phase_span_deg,
            self.phase_separation_deg,
            self.huber_delta,
            self.maximum_condition_number,
            self.maximum_center_residual_rms_mm,
            self.maximum_width_residual_rms_mm,
            self.minimum_axis_mm,
            self.maximum_axis_mm,
            self.near_circle_axis_ratio,
        )
        if not all(math.isfinite(value) and value > 0 for value in positive):
            raise ValueError("fit thresholds must be finite and positive")
        if self.maximum_axis_mm < self.minimum_axis_mm:
            raise ValueError("maximum_axis_mm must not be below minimum_axis_mm")
        if self.near_circle_axis_ratio < 1:
            raise ValueError("near_circle_axis_ratio must be at least one")
        for value in (self.minimum_observation_quality, self.minimum_slice_quality):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("quality thresholds must be between zero and one")
        return self


@dataclass(frozen=True)
class StemSliceModelBuilderConfig:
    slice_spacing_mm: float = 50.0
    x_grid_origin_mm: float | None = None
    minimum_valid_slice_count: int = 3
    minimum_valid_slice_ratio: float = 0.6
    maximum_invalid_gap_mm: float = 100.0
    minimum_model_quality: float = 0.25
    degraded_model_quality: float = 0.6
    maximum_neighbor_center_jump_mm: float = 40.0
    maximum_neighbor_axis_change_ratio: float = 0.35
    fit: StemSliceFitConfig = field(default_factory=StemSliceFitConfig)
    preprocessing: StemSlicePreprocessingConfig = field(
        default_factory=StemSlicePreprocessingConfig
    )

    def validate(self) -> StemSliceModelBuilderConfig:
        self.fit.validate()
        self.preprocessing.validate()
        if not math.isfinite(self.slice_spacing_mm) or self.slice_spacing_mm <= 0:
            raise ValueError("slice_spacing_mm must be finite and positive")
        if self.x_grid_origin_mm is not None and not math.isfinite(self.x_grid_origin_mm):
            raise ValueError("x_grid_origin_mm must be finite when provided")
        if self.minimum_valid_slice_count < 1:
            raise ValueError("minimum_valid_slice_count must be positive")
        for value in (
            self.minimum_valid_slice_ratio,
            self.minimum_model_quality,
            self.degraded_model_quality,
        ):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("model quality thresholds must be between zero and one")
        for value in (
            self.maximum_invalid_gap_mm,
            self.maximum_neighbor_center_jump_mm,
            self.maximum_neighbor_axis_change_ratio,
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError("builder distance thresholds must be finite and non-negative")
        return self


@dataclass(frozen=True)
class StemSliceFitDiagnostics:
    x_mm: float
    valid: bool
    observation_count: int
    accepted_observation_count: int
    unique_phase_count: int
    phase_span_deg: float
    phase_coverage_ratio: float
    center_residual_rms_mm: float | None
    width_residual_rms_mm: float | None
    center_condition_number: float | None
    width_condition_number: float | None
    center_inlier_weight_ratio: float
    width_inlier_weight_ratio: float
    mean_observation_quality: float
    fit_quality: float
    near_circle: bool
    phi_informative: bool
    neighbor_consistency: float = 1.0
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StemSliceFitResult:
    geometry: StemSliceGeometry | None
    diagnostics: StemSliceFitDiagnostics


@dataclass(frozen=True)
class StemSliceModelBuildResult:
    raw_model: StemSliceModel
    processed_model: StemSliceModel
    preprocessing: StemSlicePreprocessingResult
    station_fits: tuple[StemSliceFitResult, ...]
    status: StemSliceModelStatus
    model_quality: float
    requested_station_count: int
    valid_raw_slice_count: int
    valid_processed_slice_count: int
    valid_raw_slice_ratio: float
    maximum_invalid_gap_mm: float
    reference_phase_deg: float
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    algorithm_version: str = STEM_SLICE_MEASUREMENT_VERSION


@dataclass(frozen=True)
class _RobustFit:
    coefficients: np.ndarray
    predicted: np.ndarray
    weights: np.ndarray
    condition_number: float
    inlier_weight_ratio: float


def fit_stem_slice(
    observations: Sequence[StemProjectionObservation],
    *,
    reference_phase_deg: float,
    config: StemSliceFitConfig | None = None,
) -> StemSliceFitResult:
    """Fit center and ellipse support matrix for one material-fixed x station.

    Relative to the requested current pose, the calibrated process-y observations obey
    ``y = offset0 + cy*cos(theta) - cz*sin(theta)``. ``offset0`` absorbs the static
    camera/machine projection baseline and is deliberately not copied into the rotating
    slice center. With half-width ``h`` and ``v=(cos(theta), -sin(theta))``, the
    silhouette support obeys ``h^2=v.T*S*v``. Both systems are linear and are solved by
    deterministic weighted Huber IRLS.
    """

    resolved = (config or StemSliceFitConfig()).validate()
    if not math.isfinite(reference_phase_deg):
        raise ValueError("reference_phase_deg must be finite")
    checked: list[StemProjectionObservation] = []
    for item in observations:
        try:
            checked_item = item.validate()
        except ValueError:
            continue
        if checked_item.effective_quality >= resolved.minimum_observation_quality:
            checked.append(checked_item)
    x_mm = (
        float(np.median([item.x_mm for item in checked or observations]))
        if observations
        else 0.0
    )
    phase_values = [item.phase_deg for item in checked]
    phase_span = _phase_span_deg(phase_values)
    unique_phases = _unique_phase_count(phase_values, resolved.phase_separation_deg)
    coverage = min(1.0, phase_span / 180.0)
    base_reasons: list[str] = []
    if len(checked) < resolved.minimum_observations:
        base_reasons.append("OBSERVATION_COUNT_INSUFFICIENT")
    if unique_phases < resolved.minimum_unique_phases:
        base_reasons.append("UNIQUE_PHASE_COUNT_INSUFFICIENT")
    if phase_span + 1.0e-9 < resolved.minimum_phase_span_deg:
        base_reasons.append("PHASE_COVERAGE_INSUFFICIENT")
    if base_reasons:
        return _failed_fit(
            x_mm,
            observations,
            checked,
            unique_phases,
            phase_span,
            coverage,
            base_reasons,
        )

    relative_rad = np.radians(
        np.asarray([item.phase_deg - reference_phase_deg for item in checked], dtype=np.float64)
    )
    cosine = np.cos(relative_rad)
    sine = np.sin(relative_rad)
    base_weights = np.asarray(
        [max(item.effective_quality, 1.0e-9) for item in checked], dtype=np.float64
    )
    centers = np.asarray([item.projected_center_mm for item in checked], dtype=np.float64)
    widths = np.asarray([item.visible_width_mm for item in checked], dtype=np.float64)

    # The measured profile center contains a station-specific but phase-independent
    # projection baseline. Omitting this intercept biases cy/cz and turns the baseline
    # into a large harmonic residual, especially for the partial natural-roll arcs seen
    # during transfer. Coefficients are [offset0, cy, cz].
    center_design = np.column_stack((np.ones_like(cosine), cosine, -sine))
    center_fit = _robust_linear_fit(
        center_design,
        centers,
        base_weights,
        iterations=resolved.robust_iterations,
        huber_delta=resolved.huber_delta,
    )

    half_width_squared = np.square(widths * 0.5)
    width_design = np.column_stack(
        (np.square(cosine), -2.0 * cosine * sine, np.square(sine))
    )

    def width_residual(coefficients: np.ndarray) -> np.ndarray:
        predicted_squared = np.maximum(width_design @ coefficients, 0.0)
        return 2.0 * np.sqrt(predicted_squared) - widths

    width_fit = _robust_linear_fit(
        width_design,
        half_width_squared,
        base_weights,
        iterations=resolved.robust_iterations,
        huber_delta=resolved.huber_delta,
        residual_for_weights=width_residual,
    )

    center_residual = center_fit.predicted - centers
    predicted_half_squared = width_design @ width_fit.coefficients
    center_rms = _weighted_rms(center_residual, center_fit.weights)
    width_rms = _weighted_rms(width_residual(width_fit.coefficients), width_fit.weights)
    reasons: list[str] = []
    if not math.isfinite(center_fit.condition_number) or (
        center_fit.condition_number > resolved.maximum_condition_number
    ):
        reasons.append("CENTER_FIT_ILL_CONDITIONED")
    if not math.isfinite(width_fit.condition_number) or (
        width_fit.condition_number > resolved.maximum_condition_number
    ):
        reasons.append("WIDTH_FIT_ILL_CONDITIONED")
    if center_rms > resolved.maximum_center_residual_rms_mm:
        reasons.append("CENTER_RESIDUAL_TOO_HIGH")
    if width_rms > resolved.maximum_width_residual_rms_mm:
        reasons.append("WIDTH_RESIDUAL_TOO_HIGH")
    if np.any(predicted_half_squared <= 0):
        reasons.append("ELLIPSE_SUPPORT_NON_POSITIVE")

    shape = np.asarray(
        [
            [width_fit.coefficients[0], width_fit.coefficients[1]],
            [width_fit.coefficients[1], width_fit.coefficients[2]],
        ],
        dtype=np.float64,
    )
    eigenvalues, eigenvectors = np.linalg.eigh(shape)
    if np.any(~np.isfinite(eigenvalues)) or float(eigenvalues[0]) <= 0:
        reasons.append("ELLIPSE_MATRIX_NOT_POSITIVE_DEFINITE")
        a_mm = b_mm = 0.0
        phi_rad = 0.0
        near_circle = False
    else:
        b_mm = math.sqrt(float(eigenvalues[0]))
        a_mm = math.sqrt(float(eigenvalues[1]))
        major_vector = eigenvectors[:, 1]
        phi_rad = normalize_ellipse_angle_rad(
            math.atan2(float(major_vector[1]), float(major_vector[0]))
        )
        near_circle = a_mm / b_mm <= resolved.near_circle_axis_ratio
        if near_circle:
            phi_rad = 0.0
    if not (
        resolved.minimum_axis_mm <= b_mm <= a_mm <= resolved.maximum_axis_mm
    ):
        reasons.append("ELLIPSE_AXES_IMPLAUSIBLE")

    mean_quality = float(np.mean(base_weights))
    fit_quality = _fit_quality(
        mean_observation_quality=mean_quality,
        phase_coverage=coverage,
        center_rms=center_rms,
        width_rms=width_rms,
        center_inlier_ratio=center_fit.inlier_weight_ratio,
        width_inlier_ratio=width_fit.inlier_weight_ratio,
        config=resolved,
    )
    if fit_quality < resolved.minimum_slice_quality:
        reasons.append("SLICE_QUALITY_LOW")
    valid = not reasons
    geometry = None
    if valid:
        geometry = StemSliceGeometry(
            x_mm=x_mm,
            cy_mm=float(center_fit.coefficients[1]),
            cz_mm=float(center_fit.coefficients[2]),
            a_mm=a_mm,
            b_mm=b_mm,
            phi_rad=phi_rad,
            quality=fit_quality,
            valid=True,
            reason_codes=("ELLIPSE_ORIENTATION_UNINFORMATIVE",) if near_circle else (),
        )
        geometry.validate()
    diagnostics = StemSliceFitDiagnostics(
        x_mm=x_mm,
        valid=valid,
        observation_count=len(observations),
        accepted_observation_count=len(checked),
        unique_phase_count=unique_phases,
        phase_span_deg=phase_span,
        phase_coverage_ratio=coverage,
        center_residual_rms_mm=center_rms,
        width_residual_rms_mm=width_rms,
        center_condition_number=center_fit.condition_number,
        width_condition_number=width_fit.condition_number,
        center_inlier_weight_ratio=center_fit.inlier_weight_ratio,
        width_inlier_weight_ratio=width_fit.inlier_weight_ratio,
        mean_observation_quality=mean_quality,
        fit_quality=fit_quality,
        near_circle=near_circle,
        phi_informative=not near_circle,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
    return StemSliceFitResult(geometry=geometry, diagnostics=diagnostics)


def build_stem_slice_model(
    observations: Sequence[StemProjectionObservation],
    *,
    reference_phase_deg: float,
    config: StemSliceModelBuilderConfig | None = None,
) -> StemSliceModelBuildResult:
    """Build, quality-gate, grid, interpolate, and smooth a complete slice model."""

    resolved = (config or StemSliceModelBuilderConfig()).validate()
    checked = [item.validate() for item in observations]
    if not checked:
        empty_model = StemSliceModel(
            slices=(),
            model_quality=0.0,
            reason_codes=("NO_OBSERVATIONS",),
        )
        preprocessing = preprocess_stem_slice_model(
            empty_model,
            config=replace(
                resolved.preprocessing,
                slice_spacing_mm=resolved.slice_spacing_mm,
            ),
        )
        return StemSliceModelBuildResult(
            raw_model=empty_model,
            processed_model=preprocessing.model,
            preprocessing=preprocessing,
            station_fits=(),
            status=StemSliceModelStatus.INVALID,
            model_quality=0.0,
            requested_station_count=0,
            valid_raw_slice_count=0,
            valid_processed_slice_count=0,
            valid_raw_slice_ratio=0.0,
            maximum_invalid_gap_mm=0.0,
            reference_phase_deg=reference_phase_deg,
            reason_codes=("NO_OBSERVATIONS",),
        )

    origin = (
        resolved.x_grid_origin_mm
        if resolved.x_grid_origin_mm is not None
        else min(item.x_mm for item in checked)
    )
    grouped: dict[int, list[StemProjectionObservation]] = defaultdict(list)
    for item in checked:
        index = int(math.floor(((item.x_mm - origin) / resolved.slice_spacing_mm) + 0.5))
        grouped[index].append(replace(item, x_mm=origin + index * resolved.slice_spacing_mm))
    first_index = min(grouped)
    last_index = max(grouped)
    station_fits: list[StemSliceFitResult] = []
    for index in range(first_index, last_index + 1):
        station_x = origin + index * resolved.slice_spacing_mm
        station_fit = fit_stem_slice(
            grouped.get(index, ()),
            reference_phase_deg=reference_phase_deg,
            config=resolved.fit,
        )
        station_fits.append(
            replace(
                station_fit,
                diagnostics=replace(station_fit.diagnostics, x_mm=station_x),
                geometry=(
                    replace(station_fit.geometry, x_mm=station_x)
                    if station_fit.geometry is not None
                    else None
                ),
            )
        )
    station_fits = _apply_neighbor_consistency(station_fits, resolved)
    raw_slices = tuple(
        station_fit.geometry
        for station_fit in station_fits
        if station_fit.geometry is not None
    )
    requested_count = len(station_fits)
    valid_raw_count = len(raw_slices)
    valid_raw_ratio = valid_raw_count / requested_count if requested_count else 0.0
    maximum_gap = _maximum_invalid_gap_mm(station_fits, resolved.slice_spacing_mm)
    quality_components = [
        valid_raw_ratio,
        statistics_mean(
            item.diagnostics.phase_coverage_ratio for item in station_fits if item.diagnostics.valid
        ),
        statistics_mean(
            item.diagnostics.fit_quality for item in station_fits if item.diagnostics.valid
        ),
        statistics_mean(
            item.diagnostics.neighbor_consistency
            for item in station_fits
            if item.diagnostics.valid
        ),
    ]
    complete_quality_components = [
        value for value in quality_components if value is not None
    ]
    model_quality = (
        min(complete_quality_components)
        if len(complete_quality_components) == len(quality_components)
        else 0.0
    )
    model_quality = float(max(0.0, min(1.0, model_quality)))
    reasons: list[str] = []
    if valid_raw_count < resolved.minimum_valid_slice_count:
        reasons.append("VALID_SLICE_COUNT_INSUFFICIENT")
    if valid_raw_ratio < resolved.minimum_valid_slice_ratio:
        reasons.append("VALID_SLICE_RATIO_LOW")
    if maximum_gap > resolved.maximum_invalid_gap_mm + 1.0e-9:
        reasons.append("INVALID_GAP_TOO_LONG")
    if model_quality < resolved.minimum_model_quality:
        reasons.append("MODEL_QUALITY_LOW")

    raw_model = StemSliceModel(
        slices=raw_slices,
        model_quality=model_quality,
        x_eval_start_mm=raw_slices[0].x_mm if raw_slices else None,
        x_eval_end_mm=raw_slices[-1].x_mm if raw_slices else None,
        reason_codes=tuple(reasons),
    ).validate()
    preprocessing_config = replace(
        resolved.preprocessing,
        slice_spacing_mm=resolved.slice_spacing_mm,
    )
    preprocessing = preprocess_stem_slice_model(raw_model, config=preprocessing_config)
    processed = replace(
        preprocessing.model,
        model_quality=model_quality,
        reason_codes=tuple(dict.fromkeys((*reasons, *preprocessing.reason_codes))),
    ).validate()
    valid_processed_count = sum(item.valid for item in processed.slices)
    if reasons:
        status = StemSliceModelStatus.INVALID
    elif (
        model_quality < resolved.degraded_model_quality
        or any(not item.valid or item.interpolated for item in processed.slices)
    ):
        status = StemSliceModelStatus.DEGRADED
    else:
        status = StemSliceModelStatus.VALID
    return StemSliceModelBuildResult(
        raw_model=raw_model,
        processed_model=processed,
        preprocessing=preprocessing,
        station_fits=tuple(station_fits),
        status=status,
        model_quality=model_quality,
        requested_station_count=requested_count,
        valid_raw_slice_count=valid_raw_count,
        valid_processed_slice_count=valid_processed_count,
        valid_raw_slice_ratio=valid_raw_ratio,
        maximum_invalid_gap_mm=maximum_gap,
        reference_phase_deg=reference_phase_deg,
        reason_codes=tuple(dict.fromkeys((*reasons, *preprocessing.reason_codes))),
    )


def parse_projection_observation_document(
    document: Mapping[str, Any],
) -> tuple[tuple[StemProjectionObservation, ...], float]:
    """Parse the versioned, camera-independent offline replay document."""

    if document.get("schema_version") != STEM_PROJECTION_OBSERVATION_SCHEMA:
        raise ValueError("unsupported stem projection observation schema")
    reference_phase_deg = float(document.get("reference_phase_deg", 0.0))
    raw_observations = document.get("observations")
    if not isinstance(raw_observations, list):
        raise ValueError("observations must be a list")
    observations: list[StemProjectionObservation] = []
    for index, value in enumerate(raw_observations):
        if not isinstance(value, Mapping):
            raise ValueError(f"observation {index} must be an object")
        observations.append(StemProjectionObservation(**dict(value)).validate())
    return tuple(observations), reference_phase_deg


def replay_projection_observation_document(
    document: Mapping[str, Any],
    *,
    config: StemSliceModelBuilderConfig | None = None,
) -> StemSliceModelBuildResult:
    observations, reference_phase_deg = parse_projection_observation_document(document)
    return build_stem_slice_model(
        observations,
        reference_phase_deg=reference_phase_deg,
        config=config,
    )


def stem_slice_build_result_to_dict(result: StemSliceModelBuildResult) -> dict[str, Any]:
    """Return a JSON-safe diagnostic representation for replay and regression."""

    serialized = _json_safe(asdict(result))
    if not isinstance(serialized, dict):
        raise TypeError("serialized build result must be an object")
    return serialized


def _robust_linear_fit(
    design: np.ndarray,
    response: np.ndarray,
    base_weights: np.ndarray,
    *,
    iterations: int,
    huber_delta: float,
    residual_for_weights: Callable[[np.ndarray], np.ndarray] | None = None,
) -> _RobustFit:
    weights = np.maximum(np.asarray(base_weights, dtype=np.float64), 1.0e-9)
    coefficients = np.zeros(design.shape[1], dtype=np.float64)
    condition_number = math.inf
    for _ in range(iterations):
        root_weights = np.sqrt(weights)
        weighted_design = design * root_weights[:, None]
        weighted_response = response * root_weights
        coefficients, _, _, _ = np.linalg.lstsq(
            weighted_design,
            weighted_response,
            rcond=None,
        )
        condition_number = float(np.linalg.cond(weighted_design))
        residual = (
            residual_for_weights(coefficients)
            if residual_for_weights is not None
            else design @ coefficients - response
        )
        scale = _robust_scale(residual)
        threshold = max(huber_delta * scale, 1.0e-9)
        robust_weights = np.ones_like(residual)
        outside = np.abs(residual) > threshold
        robust_weights[outside] = threshold / np.abs(residual[outside])
        updated = np.maximum(base_weights * robust_weights, 1.0e-9)
        if np.allclose(updated, weights, rtol=1.0e-8, atol=1.0e-10):
            weights = updated
            break
        weights = updated
    predicted = design @ coefficients
    inlier_ratio = float(np.sum(weights) / max(np.sum(base_weights), 1.0e-9))
    return _RobustFit(
        coefficients=coefficients,
        predicted=predicted,
        weights=weights,
        condition_number=condition_number,
        inlier_weight_ratio=max(0.0, min(1.0, inlier_ratio)),
    )


def _failed_fit(
    x_mm: float,
    all_observations: Sequence[StemProjectionObservation],
    checked: Sequence[StemProjectionObservation],
    unique_phases: int,
    phase_span: float,
    coverage: float,
    reasons: Sequence[str],
) -> StemSliceFitResult:
    diagnostics = StemSliceFitDiagnostics(
        x_mm=x_mm,
        valid=False,
        observation_count=len(all_observations),
        accepted_observation_count=len(checked),
        unique_phase_count=unique_phases,
        phase_span_deg=phase_span,
        phase_coverage_ratio=coverage,
        center_residual_rms_mm=None,
        width_residual_rms_mm=None,
        center_condition_number=None,
        width_condition_number=None,
        center_inlier_weight_ratio=0.0,
        width_inlier_weight_ratio=0.0,
        mean_observation_quality=(
            float(np.mean([item.effective_quality for item in checked])) if checked else 0.0
        ),
        fit_quality=0.0,
        near_circle=False,
        phi_informative=False,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
    return StemSliceFitResult(geometry=None, diagnostics=diagnostics)


def _apply_neighbor_consistency(
    fits: Sequence[StemSliceFitResult],
    config: StemSliceModelBuilderConfig,
) -> list[StemSliceFitResult]:
    output: list[StemSliceFitResult] = []
    for index, fit in enumerate(fits):
        geometry = fit.geometry
        if geometry is None:
            output.append(fit)
            continue
        neighbors: list[StemSliceGeometry] = []
        for neighbor_index in (index - 1, index + 1):
            if not 0 <= neighbor_index < len(fits):
                continue
            neighbor = fits[neighbor_index].geometry
            if neighbor is not None:
                neighbors.append(neighbor)
        if not neighbors:
            output.append(fit)
            continue
        center_jump = min(
            math.hypot(geometry.cy_mm - item.cy_mm, geometry.cz_mm - item.cz_mm)
            for item in neighbors
        )
        axis_change = min(
            max(
                abs(geometry.a_mm - item.a_mm) / max(item.a_mm, 1.0e-9),
                abs(geometry.b_mm - item.b_mm) / max(item.b_mm, 1.0e-9),
            )
            for item in neighbors
        )
        center_score = max(
            0.0,
            1.0 - center_jump / max(config.maximum_neighbor_center_jump_mm, 1.0e-9),
        )
        axis_score = max(
            0.0,
            1.0 - axis_change / max(config.maximum_neighbor_axis_change_ratio, 1.0e-9),
        )
        consistency = min(center_score, axis_score)
        # Local bottlenecks remain valid: consistency is diagnostic and lowers quality,
        # but never discards an otherwise well-supported measured constriction.
        adjusted_quality = min(geometry.quality, 0.5 + 0.5 * consistency)
        output.append(
            replace(
                fit,
                geometry=replace(geometry, quality=adjusted_quality),
                diagnostics=replace(
                    fit.diagnostics,
                    fit_quality=adjusted_quality,
                    neighbor_consistency=consistency,
                    reason_codes=(
                        (*fit.diagnostics.reason_codes, "LOCAL_CONSISTENCY_LOW")
                        if consistency <= 0
                        else fit.diagnostics.reason_codes
                    ),
                ),
            )
        )
    return output


def _fit_quality(
    *,
    mean_observation_quality: float,
    phase_coverage: float,
    center_rms: float,
    width_rms: float,
    center_inlier_ratio: float,
    width_inlier_ratio: float,
    config: StemSliceFitConfig,
) -> float:
    components = (
        mean_observation_quality,
        phase_coverage,
        max(0.0, 1.0 - center_rms / config.maximum_center_residual_rms_mm),
        max(0.0, 1.0 - width_rms / config.maximum_width_residual_rms_mm),
        center_inlier_ratio,
        width_inlier_ratio,
    )
    product = math.prod(max(0.0, min(1.0, value)) for value in components)
    return float(product ** (1.0 / len(components)))


def _phase_span_deg(phases: Sequence[float]) -> float:
    if len(phases) < 2:
        return 0.0
    return min(360.0, max(phases) - min(phases))


def _unique_phase_count(phases: Sequence[float], separation_deg: float) -> int:
    if not phases:
        return 0
    ordered = sorted(phases)
    selected = [ordered[0]]
    for phase in ordered[1:]:
        if phase - selected[-1] >= separation_deg:
            selected.append(phase)
    return len(selected)


def _maximum_invalid_gap_mm(
    fits: Sequence[StemSliceFitResult], spacing_mm: float
) -> float:
    longest = current = 0
    for fit in fits:
        if fit.geometry is None:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest * spacing_mm


def _weighted_rms(residual: np.ndarray, weights: np.ndarray) -> float:
    return math.sqrt(float(np.sum(weights * np.square(residual)) / max(np.sum(weights), 1.0e-9)))


def _robust_scale(residual: np.ndarray) -> float:
    median = float(np.median(residual))
    mad = float(np.median(np.abs(residual - median)))
    if mad > 1.0e-12:
        return 1.4826 * mad
    rms = math.sqrt(float(np.mean(np.square(residual))))
    return max(rms, 1.0e-9)


def statistics_mean(values: Iterable[float]) -> float | None:
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else None


def _json_safe(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")
