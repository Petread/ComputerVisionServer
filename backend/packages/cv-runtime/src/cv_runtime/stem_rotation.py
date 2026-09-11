from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

import numpy as np

ROTATION_CALIBRATION_SCHEMA_VERSION = "stem.rotation-calibration.v1"
ROTATION_ALGORITHM_VERSION = "stem.surface-rotation.lk-local-axis.v1"


class StemRotationCalibrationMethod(StrEnum):
    KNOWN_ANGLE_POSITIONS = "known_angle_positions"
    TEMPORARY_CIRCUMFERENCE_MARKERS = "temporary_circumference_markers"


class StemRotationReasonCode(StrEnum):
    CALIBRATION_INVALID = "rotation_calibration_invalid"
    CALIBRATION_HASH_MISMATCH = "rotation_calibration_hash_mismatch"
    CALIBRATION_INTRINSIC_MISMATCH = "rotation_calibration_intrinsic_mismatch"
    CALIBRATION_OUTSIDE_WIDTH_RANGE = "rotation_calibration_outside_width_range"
    CALIBRATION_OUTSIDE_IMAGE_REGION = "rotation_calibration_outside_image_region"
    CALIBRATION_SAMPLES_INSUFFICIENT = "rotation_calibration_samples_insufficient"
    CALIBRATION_ANGLE_SPAN_INSUFFICIENT = "rotation_calibration_angle_span_insufficient"
    CALIBRATION_ERROR_TOO_HIGH = "rotation_calibration_error_too_high"
    FEATURE_COUNT_INSUFFICIENT = "rotation_feature_count_insufficient"
    FEATURE_INLIER_RATIO_LOW = "rotation_feature_inlier_ratio_low"
    FEATURE_QUALITY_LOW = "rotation_feature_quality_low"
    ROTATION_STEP_IMPLAUSIBLE = "rotation_step_implausible"
    GEOMETRY_INVALID = "rotation_geometry_invalid"


_SUPPORTED_VIEWPOINTS = {
    "viewed_from_configured_log_end_a",
    "viewed_from_configured_log_end_b",
}
_SUPPORTED_POSITIVE_DIRECTIONS = {
    "clockwise_viewed_from_configured_log_end",
    "counter_clockwise_viewed_from_configured_log_end",
}


@dataclass(frozen=True)
class StemRotationCalibrationObservation:
    observed_normalized_displacement: float
    known_rotation_deg: float
    stem_width_fraction: float
    image_center_x: float
    image_center_y: float
    weight: float = 1.0


@dataclass(frozen=True)
class StemRotationCalibrationProfile:
    calibration_id: str
    version: str
    method: str
    source_intrinsic_calibration_hash: str
    rotation_viewpoint_convention: str
    positive_rotation_direction: str
    degrees_per_normalized_displacement: float | None
    validation_sample_count: int
    mean_abs_error_deg: float | None
    max_abs_error_deg: float | None
    validation_mean_error_limit_deg: float
    validation_max_error_limit_deg: float
    valid_stem_width_fraction_min: float | None
    valid_stem_width_fraction_max: float | None
    valid_image_center_x_min: float | None
    valid_image_center_x_max: float | None
    valid_image_center_y_min: float | None
    valid_image_center_y_max: float | None
    calibration_hash: str
    reason_codes: tuple[str, ...] = ()
    schema_version: str = ROTATION_CALIBRATION_SCHEMA_VERSION

    @property
    def valid(self) -> bool:
        return not self.reason_codes and self.degrees_per_normalized_displacement is not None

    def runtime_config(self) -> dict[str, Any]:
        return {
            "rotation_calibration_id": self.calibration_id,
            "rotation_calibration_version": self.version,
            "rotation_calibration_method": self.method,
            "rotation_calibration_intrinsic_hash": self.source_intrinsic_calibration_hash,
            "rotation_viewpoint_convention": self.rotation_viewpoint_convention,
            "positive_rotation_direction": self.positive_rotation_direction,
            "rotation_degrees_per_normalized_displacement": (
                self.degrees_per_normalized_displacement
            ),
            "rotation_calibration_sample_count": self.validation_sample_count,
            "rotation_calibration_mean_abs_error_deg": self.mean_abs_error_deg,
            "rotation_calibration_max_abs_error_deg": self.max_abs_error_deg,
            "rotation_calibration_mean_error_limit_deg": (self.validation_mean_error_limit_deg),
            "rotation_calibration_max_error_limit_deg": (self.validation_max_error_limit_deg),
            "rotation_calibration_width_min": self.valid_stem_width_fraction_min,
            "rotation_calibration_width_max": self.valid_stem_width_fraction_max,
            "rotation_calibration_center_x_min": self.valid_image_center_x_min,
            "rotation_calibration_center_x_max": self.valid_image_center_x_max,
            "rotation_calibration_center_y_min": self.valid_image_center_y_min,
            "rotation_calibration_center_y_max": self.valid_image_center_y_max,
            "rotation_calibration_hash": self.calibration_hash,
        }


@dataclass(frozen=True)
class StemAxisObservation:
    center_x_px: float
    center_y_px: float
    direction_x: float
    direction_y: float
    length_px: float
    median_width_px: float
    image_width: int
    image_height: int

    @property
    def center(self) -> np.ndarray:
        return np.asarray((self.center_x_px, self.center_y_px), dtype=np.float64)

    @property
    def direction(self) -> np.ndarray:
        vector = np.asarray((self.direction_x, self.direction_y), dtype=np.float64)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 1e-12 else np.asarray((1.0, 0.0))

    @property
    def normal(self) -> np.ndarray:
        direction = self.direction
        return np.asarray((-direction[1], direction[0]), dtype=np.float64)

    @property
    def stem_width_fraction(self) -> float:
        diagonal = math.hypot(self.image_width, self.image_height)
        return self.median_width_px / diagonal if diagonal > 0 else 0.0

    @property
    def image_center_normalized(self) -> tuple[float, float]:
        return (
            self.center_x_px / max(self.image_width, 1),
            self.center_y_px / max(self.image_height, 1),
        )


@dataclass(frozen=True)
class SurfaceFeatureMatch:
    previous_x_px: float
    previous_y_px: float
    current_x_px: float
    current_y_px: float
    tracking_error_px: float = 0.0


@dataclass(frozen=True)
class SurfaceRotationEstimationConfig:
    minimum_matched_features: int = 8
    minimum_inlier_ratio: float = 0.55
    maximum_tracking_error_px: float = 20.0
    maximum_longitudinal_shift_fraction: float = 0.18
    absolute_inlier_tolerance_normalized: float = 0.025
    mad_multiplier: float = 3.5
    maximum_abs_delta_rotation_deg: float = 60.0
    minimum_confidence: float = 0.35


@dataclass(frozen=True)
class SurfaceRotationEstimate:
    valid: bool
    delta_rotation_deg: float | None
    normalized_transverse_displacement: float | None
    matched_feature_count: int
    inlier_feature_count: int
    inlier_ratio: float
    median_tracking_error_px: float | None
    translation_x_px: float
    translation_y_px: float
    translation_magnitude_px: float
    axis_rotation_deg: float
    rotation_confidence: float
    inlier_match_indices: tuple[int, ...]
    reason_codes: tuple[str, ...]
    algorithm_version: str = ROTATION_ALGORITHM_VERSION


def calibrate_stem_rotation(
    observations: Sequence[StemRotationCalibrationObservation],
    *,
    calibration_id: str,
    version: str,
    method: StemRotationCalibrationMethod | str,
    source_intrinsic_calibration_hash: str,
    rotation_viewpoint_convention: str,
    positive_rotation_direction: str,
    minimum_samples: int = 4,
    minimum_angle_span_deg: float = 30.0,
    mean_error_limit_deg: float = 3.0,
    max_error_limit_deg: float = 7.5,
) -> StemRotationCalibrationProfile:
    method_value = str(method)
    reasons = _calibration_identity_reasons(
        calibration_id=calibration_id,
        version=version,
        method=method_value,
        source_intrinsic_calibration_hash=source_intrinsic_calibration_hash,
        rotation_viewpoint_convention=rotation_viewpoint_convention,
        positive_rotation_direction=positive_rotation_direction,
    )
    valid_observations = [item for item in observations if _valid_observation(item)]
    if len(valid_observations) < minimum_samples:
        reasons.append(StemRotationReasonCode.CALIBRATION_SAMPLES_INSUFFICIENT.value)

    angle_span = (
        max(item.known_rotation_deg for item in valid_observations)
        - min(item.known_rotation_deg for item in valid_observations)
        if valid_observations
        else 0.0
    )
    if angle_span < minimum_angle_span_deg:
        reasons.append(StemRotationReasonCode.CALIBRATION_ANGLE_SPAN_INSUFFICIENT.value)

    slope: float | None = None
    mean_error: float | None = None
    max_error: float | None = None
    inlier_observations = valid_observations
    if not reasons:
        displacement = np.asarray(
            [item.observed_normalized_displacement for item in valid_observations],
            dtype=np.float64,
        )
        known_angle = np.asarray(
            [item.known_rotation_deg for item in valid_observations],
            dtype=np.float64,
        )
        weights = np.asarray(
            [item.weight for item in valid_observations],
            dtype=np.float64,
        )
        slope = _weighted_origin_slope(displacement, known_angle, weights)
        if slope is None:
            reasons.append(StemRotationReasonCode.CALIBRATION_INVALID.value)
        else:
            residual = known_angle - slope * displacement
            residual_median = float(np.median(residual))
            mad = float(np.median(np.abs(residual - residual_median)))
            residual_limit = max(3.5 * 1.4826 * mad, 1.0)
            inlier_mask = np.abs(residual - residual_median) <= residual_limit
            if int(np.count_nonzero(inlier_mask)) >= minimum_samples:
                slope = _weighted_origin_slope(
                    displacement[inlier_mask],
                    known_angle[inlier_mask],
                    weights[inlier_mask],
                )
                inlier_observations = [
                    item
                    for item, keep in zip(valid_observations, inlier_mask, strict=True)
                    if bool(keep)
                ]
            if slope is None:
                reasons.append(StemRotationReasonCode.CALIBRATION_INVALID.value)
            else:
                errors = np.abs(
                    np.asarray(
                        [item.known_rotation_deg for item in inlier_observations],
                        dtype=np.float64,
                    )
                    - slope
                    * np.asarray(
                        [item.observed_normalized_displacement for item in inlier_observations],
                        dtype=np.float64,
                    )
                )
                mean_error = float(np.mean(errors)) if errors.size else None
                max_error = float(np.max(errors)) if errors.size else None
                if (
                    mean_error is None
                    or max_error is None
                    or mean_error > mean_error_limit_deg
                    or max_error > max_error_limit_deg
                ):
                    reasons.append(StemRotationReasonCode.CALIBRATION_ERROR_TOO_HIGH.value)

    profile = StemRotationCalibrationProfile(
        calibration_id=calibration_id,
        version=version,
        method=method_value,
        source_intrinsic_calibration_hash=source_intrinsic_calibration_hash,
        rotation_viewpoint_convention=rotation_viewpoint_convention,
        positive_rotation_direction=positive_rotation_direction,
        degrees_per_normalized_displacement=slope,
        validation_sample_count=len(inlier_observations),
        mean_abs_error_deg=mean_error,
        max_abs_error_deg=max_error,
        validation_mean_error_limit_deg=mean_error_limit_deg,
        validation_max_error_limit_deg=max_error_limit_deg,
        valid_stem_width_fraction_min=_observation_min(inlier_observations, "stem_width_fraction"),
        valid_stem_width_fraction_max=_observation_max(inlier_observations, "stem_width_fraction"),
        valid_image_center_x_min=_observation_min(inlier_observations, "image_center_x"),
        valid_image_center_x_max=_observation_max(inlier_observations, "image_center_x"),
        valid_image_center_y_min=_observation_min(inlier_observations, "image_center_y"),
        valid_image_center_y_max=_observation_max(inlier_observations, "image_center_y"),
        calibration_hash="",
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
    return replace(profile, calibration_hash=stem_rotation_calibration_hash(profile))


def stem_rotation_calibration_from_config(
    config: Mapping[str, Any],
) -> StemRotationCalibrationProfile:
    reasons = _calibration_identity_reasons(
        calibration_id=str(config.get("rotation_calibration_id") or ""),
        version=str(config.get("rotation_calibration_version") or ""),
        method=str(config.get("rotation_calibration_method") or ""),
        source_intrinsic_calibration_hash=str(
            config.get("rotation_calibration_intrinsic_hash") or ""
        ),
        rotation_viewpoint_convention=str(config.get("rotation_viewpoint_convention") or ""),
        positive_rotation_direction=str(config.get("positive_rotation_direction") or ""),
    )
    try:
        profile = StemRotationCalibrationProfile(
            calibration_id=str(config.get("rotation_calibration_id") or ""),
            version=str(config.get("rotation_calibration_version") or ""),
            method=str(config.get("rotation_calibration_method") or ""),
            source_intrinsic_calibration_hash=str(
                config.get("rotation_calibration_intrinsic_hash") or ""
            ),
            rotation_viewpoint_convention=str(config.get("rotation_viewpoint_convention") or ""),
            positive_rotation_direction=str(config.get("positive_rotation_direction") or ""),
            degrees_per_normalized_displacement=_optional_float(
                config.get("rotation_degrees_per_normalized_displacement")
            ),
            validation_sample_count=int(config.get("rotation_calibration_sample_count") or 0),
            mean_abs_error_deg=_optional_float(
                config.get("rotation_calibration_mean_abs_error_deg")
            ),
            max_abs_error_deg=_optional_float(config.get("rotation_calibration_max_abs_error_deg")),
            validation_mean_error_limit_deg=float(
                config.get("rotation_calibration_mean_error_limit_deg") or 3.0
            ),
            validation_max_error_limit_deg=float(
                config.get("rotation_calibration_max_error_limit_deg") or 7.5
            ),
            valid_stem_width_fraction_min=_optional_float(
                config.get("rotation_calibration_width_min")
            ),
            valid_stem_width_fraction_max=_optional_float(
                config.get("rotation_calibration_width_max")
            ),
            valid_image_center_x_min=_optional_float(
                config.get("rotation_calibration_center_x_min")
            ),
            valid_image_center_x_max=_optional_float(
                config.get("rotation_calibration_center_x_max")
            ),
            valid_image_center_y_min=_optional_float(
                config.get("rotation_calibration_center_y_min")
            ),
            valid_image_center_y_max=_optional_float(
                config.get("rotation_calibration_center_y_max")
            ),
            calibration_hash=str(config.get("rotation_calibration_hash") or ""),
            reason_codes=(),
        )
    except (TypeError, ValueError):
        reasons.append(StemRotationReasonCode.CALIBRATION_INVALID.value)
        profile = StemRotationCalibrationProfile(
            calibration_id=str(config.get("rotation_calibration_id") or ""),
            version=str(config.get("rotation_calibration_version") or ""),
            method=str(config.get("rotation_calibration_method") or ""),
            source_intrinsic_calibration_hash=str(
                config.get("rotation_calibration_intrinsic_hash") or ""
            ),
            rotation_viewpoint_convention=str(config.get("rotation_viewpoint_convention") or ""),
            positive_rotation_direction=str(config.get("positive_rotation_direction") or ""),
            degrees_per_normalized_displacement=None,
            validation_sample_count=0,
            mean_abs_error_deg=None,
            max_abs_error_deg=None,
            validation_mean_error_limit_deg=3.0,
            validation_max_error_limit_deg=7.5,
            valid_stem_width_fraction_min=None,
            valid_stem_width_fraction_max=None,
            valid_image_center_x_min=None,
            valid_image_center_x_max=None,
            valid_image_center_y_min=None,
            valid_image_center_y_max=None,
            calibration_hash=str(config.get("rotation_calibration_hash") or ""),
            reason_codes=(),
        )

    if profile.validation_sample_count < 4:
        reasons.append(StemRotationReasonCode.CALIBRATION_SAMPLES_INSUFFICIENT.value)
    if (
        profile.degrees_per_normalized_displacement is None
        or not math.isfinite(profile.degrees_per_normalized_displacement)
        or abs(profile.degrees_per_normalized_displacement) < 1e-6
    ):
        reasons.append(StemRotationReasonCode.CALIBRATION_INVALID.value)
    if (
        profile.mean_abs_error_deg is None
        or profile.max_abs_error_deg is None
        or profile.mean_abs_error_deg > profile.validation_mean_error_limit_deg
        or profile.max_abs_error_deg > profile.validation_max_error_limit_deg
    ):
        reasons.append(StemRotationReasonCode.CALIBRATION_ERROR_TOO_HIGH.value)
    if not _valid_profile_ranges(profile):
        reasons.append(StemRotationReasonCode.CALIBRATION_INVALID.value)

    expected_hash = stem_rotation_calibration_hash(replace(profile, calibration_hash=""))
    if not profile.calibration_hash or profile.calibration_hash != expected_hash:
        reasons.append(StemRotationReasonCode.CALIBRATION_HASH_MISMATCH.value)
    return replace(profile, reason_codes=tuple(dict.fromkeys(reasons)))


def stem_rotation_calibration_hash(profile: StemRotationCalibrationProfile) -> str:
    payload = {
        "schema_version": profile.schema_version,
        "calibration_id": profile.calibration_id,
        "version": profile.version,
        "method": profile.method,
        "source_intrinsic_calibration_hash": profile.source_intrinsic_calibration_hash,
        "rotation_viewpoint_convention": profile.rotation_viewpoint_convention,
        "positive_rotation_direction": profile.positive_rotation_direction,
        "degrees_per_normalized_displacement": profile.degrees_per_normalized_displacement,
        "validation_sample_count": profile.validation_sample_count,
        "mean_abs_error_deg": profile.mean_abs_error_deg,
        "max_abs_error_deg": profile.max_abs_error_deg,
        "validation_mean_error_limit_deg": profile.validation_mean_error_limit_deg,
        "validation_max_error_limit_deg": profile.validation_max_error_limit_deg,
        "valid_stem_width_fraction_min": profile.valid_stem_width_fraction_min,
        "valid_stem_width_fraction_max": profile.valid_stem_width_fraction_max,
        "valid_image_center_x_min": profile.valid_image_center_x_min,
        "valid_image_center_x_max": profile.valid_image_center_x_max,
        "valid_image_center_y_min": profile.valid_image_center_y_min,
        "valid_image_center_y_max": profile.valid_image_center_y_max,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def estimate_surface_rotation_from_matches(
    matches: Sequence[SurfaceFeatureMatch],
    *,
    previous_axis: StemAxisObservation,
    current_axis: StemAxisObservation,
    calibration: StemRotationCalibrationProfile,
    config: SurfaceRotationEstimationConfig | None = None,
) -> SurfaceRotationEstimate:
    resolved = config or SurfaceRotationEstimationConfig()
    translation = current_axis.center - previous_axis.center
    translation_x = float(translation[0])
    translation_y = float(translation[1])
    axis_rotation_deg = _signed_axis_rotation_deg(
        previous_axis.direction,
        current_axis.direction,
    )
    common = {
        "translation_x_px": translation_x,
        "translation_y_px": translation_y,
        "translation_magnitude_px": math.hypot(translation_x, translation_y),
        "axis_rotation_deg": axis_rotation_deg,
    }
    calibration_reasons = _runtime_calibration_reasons(calibration, current_axis)
    if calibration_reasons:
        return _invalid_estimate(
            matched_count=len(matches),
            reasons=calibration_reasons,
            **common,
        )
    if previous_axis.length_px <= 0 or current_axis.length_px <= 0:
        return _invalid_estimate(
            matched_count=len(matches),
            reasons=[StemRotationReasonCode.GEOMETRY_INVALID.value],
            **common,
        )

    accepted: list[tuple[int, float, float]] = []
    for index, match in enumerate(matches):
        values = (
            match.previous_x_px,
            match.previous_y_px,
            match.current_x_px,
            match.current_y_px,
            match.tracking_error_px,
        )
        if not all(math.isfinite(float(value)) for value in values):
            continue
        if match.tracking_error_px > resolved.maximum_tracking_error_px:
            continue
        previous_point = np.asarray(
            (match.previous_x_px, match.previous_y_px),
            dtype=np.float64,
        )
        current_point = np.asarray(
            (match.current_x_px, match.current_y_px),
            dtype=np.float64,
        )
        previous_local = previous_point - previous_axis.center
        current_local = current_point - current_axis.center
        previous_s = float(
            np.dot(previous_local, previous_axis.direction) / previous_axis.length_px
        )
        current_s = float(np.dot(current_local, current_axis.direction) / current_axis.length_px)
        if abs(current_s - previous_s) > resolved.maximum_longitudinal_shift_fraction:
            continue
        previous_u = float(
            np.dot(previous_local, previous_axis.normal) / max(previous_axis.median_width_px, 1e-9)
        )
        current_u = float(
            np.dot(current_local, current_axis.normal) / max(current_axis.median_width_px, 1e-9)
        )
        accepted.append((index, current_u - previous_u, match.tracking_error_px))

    if len(accepted) < resolved.minimum_matched_features:
        return _invalid_estimate(
            matched_count=len(accepted),
            reasons=[StemRotationReasonCode.FEATURE_COUNT_INSUFFICIENT.value],
            **common,
        )

    displacement = np.asarray([item[1] for item in accepted], dtype=np.float64)
    median_displacement = float(np.median(displacement))
    residual = np.abs(displacement - median_displacement)
    mad = float(np.median(residual))
    inlier_limit = max(
        resolved.absolute_inlier_tolerance_normalized,
        resolved.mad_multiplier * 1.4826 * mad,
    )
    inlier_mask = residual <= inlier_limit
    inlier_count = int(np.count_nonzero(inlier_mask))
    inlier_ratio = inlier_count / max(len(accepted), 1)
    inlier_indices = tuple(
        accepted[index][0] for index, keep in enumerate(inlier_mask) if bool(keep)
    )
    if (
        inlier_count < resolved.minimum_matched_features
        or inlier_ratio < resolved.minimum_inlier_ratio
    ):
        return _invalid_estimate(
            matched_count=len(accepted),
            inlier_count=inlier_count,
            inlier_ratio=inlier_ratio,
            inlier_indices=inlier_indices,
            reasons=[StemRotationReasonCode.FEATURE_INLIER_RATIO_LOW.value],
            **common,
        )

    robust_displacement = float(np.median(displacement[inlier_mask]))
    tracking_errors = np.asarray(
        [accepted[index][2] for index, keep in enumerate(inlier_mask) if bool(keep)],
        dtype=np.float64,
    )
    median_tracking_error = float(np.median(tracking_errors)) if tracking_errors.size else None
    delta_rotation = robust_displacement * float(calibration.degrees_per_normalized_displacement)
    if abs(delta_rotation) > resolved.maximum_abs_delta_rotation_deg:
        return _invalid_estimate(
            matched_count=len(accepted),
            inlier_count=inlier_count,
            inlier_ratio=inlier_ratio,
            inlier_indices=inlier_indices,
            median_tracking_error=median_tracking_error,
            reasons=[StemRotationReasonCode.ROTATION_STEP_IMPLAUSIBLE.value],
            **common,
        )

    count_quality = min(inlier_count / max(resolved.minimum_matched_features * 2, 1), 1.0)
    error_quality = (
        max(
            0.0,
            1.0 - median_tracking_error / resolved.maximum_tracking_error_px,
        )
        if median_tracking_error is not None
        else 0.0
    )
    calibration_quality = max(
        0.0,
        1.0
        - float(calibration.mean_abs_error_deg or 0.0)
        / max(calibration.validation_mean_error_limit_deg, 1e-6),
    )
    confidence = min(
        max(
            0.45 * inlier_ratio
            + 0.25 * count_quality
            + 0.15 * error_quality
            + 0.15 * calibration_quality,
            0.0,
        ),
        1.0,
    )
    if confidence < resolved.minimum_confidence:
        return _invalid_estimate(
            matched_count=len(accepted),
            inlier_count=inlier_count,
            inlier_ratio=inlier_ratio,
            inlier_indices=inlier_indices,
            median_tracking_error=median_tracking_error,
            reasons=[StemRotationReasonCode.FEATURE_QUALITY_LOW.value],
            confidence=confidence,
            **common,
        )
    return SurfaceRotationEstimate(
        valid=True,
        delta_rotation_deg=delta_rotation,
        normalized_transverse_displacement=robust_displacement,
        matched_feature_count=len(accepted),
        inlier_feature_count=inlier_count,
        inlier_ratio=inlier_ratio,
        median_tracking_error_px=median_tracking_error,
        rotation_confidence=confidence,
        inlier_match_indices=inlier_indices,
        reason_codes=(),
        **common,
    )


def _calibration_identity_reasons(
    *,
    calibration_id: str,
    version: str,
    method: str,
    source_intrinsic_calibration_hash: str,
    rotation_viewpoint_convention: str,
    positive_rotation_direction: str,
) -> list[str]:
    reasons: list[str] = []
    if not calibration_id.strip() or not version.strip() or not source_intrinsic_calibration_hash:
        reasons.append(StemRotationReasonCode.CALIBRATION_INVALID.value)
    if method not in {item.value for item in StemRotationCalibrationMethod}:
        reasons.append(StemRotationReasonCode.CALIBRATION_INVALID.value)
    if rotation_viewpoint_convention not in _SUPPORTED_VIEWPOINTS:
        reasons.append(StemRotationReasonCode.CALIBRATION_INVALID.value)
    if positive_rotation_direction not in _SUPPORTED_POSITIVE_DIRECTIONS:
        reasons.append(StemRotationReasonCode.CALIBRATION_INVALID.value)
    return reasons


def _runtime_calibration_reasons(
    profile: StemRotationCalibrationProfile,
    axis: StemAxisObservation,
) -> list[str]:
    if not profile.valid:
        return list(profile.reason_codes) or [StemRotationReasonCode.CALIBRATION_INVALID.value]
    width = axis.stem_width_fraction
    center_x, center_y = axis.image_center_normalized
    if not _inside_range(
        width,
        profile.valid_stem_width_fraction_min,
        profile.valid_stem_width_fraction_max,
    ):
        return [StemRotationReasonCode.CALIBRATION_OUTSIDE_WIDTH_RANGE.value]
    if not _inside_range(
        center_x,
        profile.valid_image_center_x_min,
        profile.valid_image_center_x_max,
    ) or not _inside_range(
        center_y,
        profile.valid_image_center_y_min,
        profile.valid_image_center_y_max,
    ):
        return [StemRotationReasonCode.CALIBRATION_OUTSIDE_IMAGE_REGION.value]
    return []


def _valid_observation(observation: StemRotationCalibrationObservation) -> bool:
    values = (
        observation.observed_normalized_displacement,
        observation.known_rotation_deg,
        observation.stem_width_fraction,
        observation.image_center_x,
        observation.image_center_y,
        observation.weight,
    )
    return (
        all(math.isfinite(float(value)) for value in values)
        and abs(observation.observed_normalized_displacement) > 1e-6
        and observation.stem_width_fraction > 0
        and 0 <= observation.image_center_x <= 1
        and 0 <= observation.image_center_y <= 1
        and observation.weight > 0
    )


def _weighted_origin_slope(
    x: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
) -> float | None:
    denominator = float(np.sum(weights * x * x))
    if denominator <= 1e-12:
        return None
    slope = float(np.sum(weights * x * y) / denominator)
    return slope if math.isfinite(slope) and abs(slope) > 1e-9 else None


def _valid_profile_ranges(profile: StemRotationCalibrationProfile) -> bool:
    pairs = (
        (
            profile.valid_stem_width_fraction_min,
            profile.valid_stem_width_fraction_max,
        ),
        (profile.valid_image_center_x_min, profile.valid_image_center_x_max),
        (profile.valid_image_center_y_min, profile.valid_image_center_y_max),
    )
    return all(
        lower is not None
        and upper is not None
        and math.isfinite(lower)
        and math.isfinite(upper)
        and 0 <= lower <= upper <= 1
        for lower, upper in pairs
    )


def _inside_range(value: float, lower: float | None, upper: float | None) -> bool:
    return lower is not None and upper is not None and lower <= value <= upper


def _signed_axis_rotation_deg(previous: np.ndarray, current: np.ndarray) -> float:
    dot = min(max(float(np.dot(previous, current)), -1.0), 1.0)
    cross = float(previous[0] * current[1] - previous[1] * current[0])
    return math.degrees(math.atan2(cross, dot))


def _invalid_estimate(
    *,
    matched_count: int,
    translation_x_px: float,
    translation_y_px: float,
    translation_magnitude_px: float,
    axis_rotation_deg: float,
    reasons: Sequence[str],
    inlier_count: int = 0,
    inlier_ratio: float = 0.0,
    inlier_indices: tuple[int, ...] = (),
    median_tracking_error: float | None = None,
    confidence: float = 0.0,
) -> SurfaceRotationEstimate:
    return SurfaceRotationEstimate(
        valid=False,
        delta_rotation_deg=None,
        normalized_transverse_displacement=None,
        matched_feature_count=matched_count,
        inlier_feature_count=inlier_count,
        inlier_ratio=inlier_ratio,
        median_tracking_error_px=median_tracking_error,
        translation_x_px=translation_x_px,
        translation_y_px=translation_y_px,
        translation_magnitude_px=translation_magnitude_px,
        axis_rotation_deg=axis_rotation_deg,
        rotation_confidence=confidence,
        inlier_match_indices=inlier_indices,
        reason_codes=tuple(dict.fromkeys(str(reason) for reason in reasons)),
    )


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _observation_min(
    observations: Sequence[StemRotationCalibrationObservation],
    attribute: str,
) -> float | None:
    return min((float(getattr(item, attribute)) for item in observations), default=None)


def _observation_max(
    observations: Sequence[StemRotationCalibrationObservation],
    attribute: str,
) -> float | None:
    return max((float(getattr(item, attribute)) for item in observations), default=None)
