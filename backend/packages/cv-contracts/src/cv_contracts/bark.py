"""Versioned contracts for photograph/CSV end-face inspection."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SideMapping = Literal["left_first", "left_last"]


class BarkContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class StemBarkJob(BarkContract):
    schema_version: Literal["stem.bark-job.v1"] = "stem.bark-job.v1"
    csv_text: str = Field(min_length=1, max_length=1_000_000)
    csv_filename: str = Field(default="measurement.csv", max_length=240)
    side_mapping: SideMapping | None = None


class StemCsvSlice(BarkContract):
    csv_line: int = Field(ge=7)
    position_mm: float = Field(ge=0)
    points_mm: list[tuple[float, float]] = Field(min_length=36, max_length=36)


class StemCsvMeasurement(BarkContract):
    schema_version: Literal["stem.csv-measurement.v1"] = "stem.csv-measurement.v1"
    workpiece_id: str
    customer: str
    measurement_name: str
    measured_at: str
    length_mm: float = Field(gt=0)
    nominal_length_mm: float = Field(gt=0)
    leading_end: Literal["S", "Z"]
    speed_m_per_min: float = Field(ge=0)
    diameters_and_shape_mm: list[float] = Field(min_length=5, max_length=5)
    volumes_m3: list[float] = Field(min_length=2, max_length=2)
    slices: list[StemCsvSlice] = Field(min_length=2, max_length=70)
    csv_sha256: str
    csv_filename: str


class EndfaceReference(BarkContract):
    side: Literal["left", "right"]
    slice_role: Literal["first", "last"]
    side_mapping: SideMapping
    mapping_source: Literal["pipeline", "job"]
    workpiece_id: str
    leading_end: Literal["S", "Z"]
    csv_sha256: str
    slice: StemCsvSlice


class BarkRadialSample(BarkContract):
    angle_deg: float
    valid: bool
    thickness_mm: float | None = None
    wood_radius_mm: float | None = None
    outer_radius_mm: float | None = None
    reason: str | None = None


class BarkSideMeasurement(BarkContract):
    reference: EndfaceReference
    valid: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    mean_mm: float | None = None
    min_mm: float | None = None
    max_mm: float | None = None
    valid_fraction: float = 0
    maximum_invalid_gap_deg: float = 360
    samples: list[BarkRadialSample] = Field(default_factory=list)
    registration_hash: str | None = None
    registration_error_mm: float | None = None
    registration_stability_mm: float | None = None
    sampling_stability_mm: float | None = None
    effective_source_mm_per_pixel: float | None = None
    model_versions: dict[str, str] = Field(default_factory=dict)


class BarkMeasurementResult(BarkContract):
    schema_version: Literal["stem.bark-measurement.v1"] = "stem.bark-measurement.v1"
    workpiece_id: str
    valid: bool
    metrology_status: Literal["not_validated"] = "not_validated"
    metrology_label: str = "noch nicht messtechnisch freigegeben"
    mean_mm: float | None = None
    left: BarkSideMeasurement
    right: BarkSideMeasurement
