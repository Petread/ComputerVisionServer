from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import Field

from cv_datasets.models import CanonicalSequenceItem, DerivedTarget, StrictSequenceModel


class RotationTraceSample(StrictSequenceModel):
    frame_index: int = Field(ge=0)
    timestamp: float = Field(ge=0.0)
    cumulative_rotation_deg_unwrapped: float
    rotation_confidence: float = Field(ge=0.0, le=1.0)
    calibration_id: str = Field(min_length=1, max_length=128)
    calibration_method: str = Field(min_length=1, max_length=128)
    calibration_version: str = Field(default="unversioned", min_length=1, max_length=128)
    calibration_valid: bool
    valid: bool = True
    trace_discontinuity: bool = False
    trace_generation: int | None = Field(default=None, ge=0)
    reason_codes: list[str] = Field(default_factory=list, max_length=64)


class TargetBuildResult(StrictSequenceModel):
    targets: list[DerivedTarget] = Field(default_factory=list, max_length=256)
    angle_target_generated: bool = False
    exclusion_reason: str | None = Field(default=None, max_length=512)
    reason_codes: list[str] = Field(default_factory=list, max_length=64)


@dataclass(frozen=True)
class TargetBuildContext:
    """Schema-neutral access to versioned snapshot resources and numeric options."""

    resources: Mapping[str, object]
    numeric_options: Mapping[str, float]


TargetBuilder = Callable[
    [CanonicalSequenceItem, TargetBuildContext],
    TargetBuildResult,
]
