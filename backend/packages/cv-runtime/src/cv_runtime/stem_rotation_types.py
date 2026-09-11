from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

STEM_ROTATION_GEOMETRY_VERSION = "stem-rotation-geometry.symmetric-chord.v1"
STEM_ROTATION_OPTIMIZER_VERSION = "stem-rotation-optimizer.coarse-fine.v1"
STEM_SLICE_PREPROCESSING_VERSION = "stem-slice-preprocessing.robust-grid.v1"


class PriorityMode(StrEnum):
    NONE = "NONE"
    VORSCHNITT_KRITISCH = "VORSCHNITT_KRITISCH"
    NACHSCHNITT_KRITISCH = "NACHSCHNITT_KRITISCH"


class PriorityAxis(StrEnum):
    HORIZONTAL_Y = "HORIZONTAL_Y"
    VERTICAL_Z = "VERTICAL_Z"

    @property
    def unit_vector(self) -> tuple[float, float]:
        """Return the axis in the canonical q=(y, z) coordinate order."""
        if self is PriorityAxis.HORIZONTAL_Y:
            return (1.0, 0.0)
        return (0.0, 1.0)


class StemRotationReasonCode(StrEnum):
    OK = "OK"
    LOW_GAIN = "LOW_GAIN"
    ROTATION_BELOW_MINIMUM = "ROTATION_BELOW_MINIMUM"
    INVALID_PRIORITY_MODE = "INVALID_PRIORITY_MODE"
    INVALID_MODEL = "INVALID_MODEL"
    MODEL_QUALITY_LOW = "MODEL_QUALITY_LOW"
    VALID_SLICE_COUNT_INSUFFICIENT = "VALID_SLICE_COUNT_INSUFFICIENT"
    VALID_SLICE_RATIO_LOW = "VALID_SLICE_RATIO_LOW"
    NO_VALID_SLICES_IN_EVALUATION_RANGE = "NO_VALID_SLICES_IN_EVALUATION_RANGE"
    NO_REAL_INTERSECTION = "NO_REAL_INTERSECTION"
    MACHINE_AXIS_OUTSIDE_SLICE = "MACHINE_AXIS_OUTSIDE_SLICE"
    ZERO_SYMMETRIC_SPAN = "ZERO_SYMMETRIC_SPAN"
    NO_USABLE_GEOMETRY = "NO_USABLE_GEOMETRY"
    ROTATION_CONSTRAINT_CONFLICT = "ROTATION_CONSTRAINT_CONFLICT"
    NUMERICAL_ERROR = "NUMERICAL_ERROR"


@dataclass(frozen=True)
class StemSliceGeometry:
    """One elliptical cross-section in the canonical machine y-z plane."""

    x_mm: float
    cy_mm: float
    cz_mm: float
    a_mm: float
    b_mm: float
    phi_rad: float
    quality: float = 1.0
    valid: bool = True
    interpolated: bool = False
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def validate(self) -> StemSliceGeometry:
        values = {
            "x_mm": self.x_mm,
            "cy_mm": self.cy_mm,
            "cz_mm": self.cz_mm,
            "a_mm": self.a_mm,
            "b_mm": self.b_mm,
            "phi_rad": self.phi_rad,
            "quality": self.quality,
        }
        for name, value in values.items():
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.a_mm < self.b_mm or self.b_mm <= 0:
            raise ValueError("slice axes must satisfy a_mm >= b_mm > 0")
        if not 0 <= self.quality <= 1:
            raise ValueError("quality must be between zero and one")
        return self


@dataclass(frozen=True)
class StemSliceModel:
    slices: tuple[StemSliceGeometry, ...]
    model_quality: float = 1.0
    x_eval_start_mm: float | None = None
    x_eval_end_mm: float | None = None
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def validate(self) -> StemSliceModel:
        if not math.isfinite(self.model_quality) or not 0 <= self.model_quality <= 1:
            raise ValueError("model_quality must be finite and between zero and one")
        for name, value in (
            ("x_eval_start_mm", self.x_eval_start_mm),
            ("x_eval_end_mm", self.x_eval_end_mm),
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite when provided")
        if (
            self.x_eval_start_mm is not None
            and self.x_eval_end_mm is not None
            and self.x_eval_end_mm < self.x_eval_start_mm
        ):
            raise ValueError("x evaluation range must not be reversed")
        for stem_slice in self.slices:
            stem_slice.validate()
        return self


@dataclass(frozen=True)
class RotationConstraints:
    """Signed mechanical limits for the relative rotation around machine +x."""

    negative_limit_deg: float = -90.0
    positive_limit_deg: float = 90.0

    def validate(self) -> RotationConstraints:
        if not math.isfinite(self.negative_limit_deg) or not math.isfinite(self.positive_limit_deg):
            raise ValueError("rotation limits must be finite")
        if self.negative_limit_deg > 0 or self.positive_limit_deg < 0:
            raise ValueError("rotation limits must contain the current orientation at 0 degrees")
        if self.positive_limit_deg < self.negative_limit_deg:
            raise ValueError("positive rotation limit must not be below negative limit")
        return self


@dataclass(frozen=True)
class SliceScore:
    x_mm: float
    symmetric_span_mm: float
    valid: bool
    t_negative_mm: float | None
    t_positive_mm: float | None
    reason_code: StemRotationReasonCode = StemRotationReasonCode.OK


@dataclass(frozen=True)
class StemRotationScore:
    valid: bool
    axis: PriorityAxis
    angle_rad: float
    available_geometry_mm: float | None
    limiting_x_mm: float | None
    slice_scores: tuple[SliceScore, ...] = field(default_factory=tuple)
    diagnostic_quantile_mm: float | None = None
    reason_code: StemRotationReasonCode = StemRotationReasonCode.OK


@dataclass(frozen=True)
class AngleScorePoint:
    angle_deg: float
    score_mm: float | None
    limiting_x_mm: float | None
    valid: bool
    reason_code: StemRotationReasonCode = StemRotationReasonCode.OK


@dataclass(frozen=True)
class StemRotationOptimizationResult:
    valid: bool
    mode: PriorityMode
    delta_angle_deg: float | None
    gain_mm: float | None
    should_rotate: bool
    limiting_x_current_mm: float | None
    limiting_x_optimal_mm: float | None
    score_current_mm: float | None
    score_optimal_mm: float | None
    model_quality: float
    reason_code: StemRotationReasonCode
    optimal_candidate_angle_deg: float | None = None
    plateau_width_deg: float | None = None
    valid_slice_count: int = 0
    evaluated_slice_count: int = 0
    score_curve: tuple[AngleScorePoint, ...] = field(default_factory=tuple)
    algorithm_version: str = STEM_ROTATION_OPTIMIZER_VERSION


def priority_axis_for_mode(mode: PriorityMode) -> PriorityAxis:
    if mode is PriorityMode.VORSCHNITT_KRITISCH:
        return PriorityAxis.VERTICAL_Z
    if mode is PriorityMode.NACHSCHNITT_KRITISCH:
        return PriorityAxis.HORIZONTAL_Y
    raise ValueError("priority mode NONE has no geometry axis")


# Concise domain aliases for callers that do not need the runtime-specific prefix.
StemSlice = StemSliceGeometry
StemModel = StemSliceModel
OptimizationResult = StemRotationOptimizationResult
