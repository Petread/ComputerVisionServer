from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from cv_contracts.runtime import (
    RuntimeStemSurfacePhaseCalibrationCalculateRequest,
    RuntimeStemSurfacePhaseCalibrationProfile,
    RuntimeStemSurfacePhaseCalibrationProvenance,
)
from cv_utils import utc_now_iso

from cv_runtime.stem_rotation import StemAxisObservation, SurfaceFeatureMatch

SURFACE_PHASE_ALGORITHM_VERSION = "stem.surface-phase.lk-keyframe-relock.v1"
SURFACE_PHASE_V2_ALGORITHM_VERSION = "stem.surface-phase.cylindrical-high-rate.v2"


@dataclass(frozen=True)
class StemSurfacePhaseCalibrationComputation:
    status: Literal["valid", "invalid"]
    profile: RuntimeStemSurfacePhaseCalibrationProfile | None
    mean_abs_error_deg: float | None = None
    rms_error_deg: float | None = None
    maximum_abs_error_deg: float | None = None
    failure_code: str | None = None
    failure_message: str | None = None


@dataclass(frozen=True)
class RawSurfacePhaseConfig:
    minimum_matched_features: int = 8
    minimum_inlier_ratio: float = 0.55
    maximum_tracking_error_px: float = 20.0
    maximum_longitudinal_shift_fraction: float = 0.18
    absolute_inlier_tolerance_raw: float = 0.025
    mad_multiplier: float = 3.5
    maximum_abs_delta_raw_phase: float = 0.75
    minimum_confidence: float = 0.35


@dataclass(frozen=True)
class CylindricalSurfacePhaseConfig:
    minimum_matched_features: int = 12
    minimum_inlier_ratio: float = 0.70
    maximum_tracking_error_px: float = 12.0
    maximum_longitudinal_shift_fraction: float = 0.20
    minimum_feature_span_ratio: float = 0.35
    maximum_surface_coordinate: float = 0.46
    residual_tolerance_normalized: float = 0.018
    mad_multiplier: float = 3.5
    maximum_abs_delta_raw_phase: float = 0.70
    minimum_confidence: float = 0.45


@dataclass(frozen=True)
class RawSurfacePhaseEstimate:
    valid: bool
    delta_raw_phase: float | None
    matched_feature_count: int
    inlier_feature_count: int
    inlier_ratio: float
    median_tracking_error_px: float | None
    translation_x_px: float
    translation_y_px: float
    translation_magnitude_px: float
    axis_rotation_deg: float
    confidence: float
    inlier_match_indices: tuple[int, ...]
    reason_codes: tuple[str, ...]
    algorithm_version: str = SURFACE_PHASE_ALGORITHM_VERSION


@dataclass(frozen=True)
class PhaseRelockCandidate:
    keyframe_id: str
    raw_phase: float
    score: float
    matched_feature_count: int
    inlier_ratio: float


@dataclass(frozen=True)
class PhaseRelockDecision:
    status: Literal["RELOCKED", "AMBIGUOUS", "FAILED"]
    candidate: PhaseRelockCandidate | None
    reason_codes: tuple[str, ...]


def calculate_stem_surface_phase_calibration(
    *,
    source_channel_id: str,
    request: RuntimeStemSurfacePhaseCalibrationCalculateRequest,
    operator_user_id: str | None = None,
    generated_at: str | None = None,
) -> StemSurfacePhaseCalibrationComputation:
    observations = list(request.observations)
    raw = np.asarray([item.raw_phase for item in observations], dtype=np.float64)
    angle = np.asarray([item.known_angle_deg for item in observations], dtype=np.float64)
    weights = np.asarray([item.weight for item in observations], dtype=np.float64)
    if not np.all(np.isfinite(raw)) or not np.all(np.isfinite(angle)):
        return _invalid_calibration(
            "phase_calibration_non_finite", "Calibration pairs must be finite."
        )
    required_angle_span = max(
        request.minimum_angle_span_deg,
        90.0 if request.phase_estimator_version == "CYLINDRICAL_SURFACE_V2" else 0.0,
    )
    if float(np.ptp(angle)) < required_angle_span:
        return _invalid_calibration(
            "phase_calibration_angle_span_insufficient",
            (
                "Known-angle observations do not span the required minimum angle "
                f"of {required_angle_span:.0f} degrees for this estimator."
            ),
        )
    denominator = float(np.sum(weights * raw * raw))
    if denominator <= 1e-12:
        return _invalid_calibration(
            "phase_calibration_degenerate_fit",
            (
                "No raw rotation was measured between the selected frames; all raw-phase "
                "values are zero. Raw phase is calculated automatically from tracked surface "
                "features and must not be entered manually. Record one continuous trace with "
                "INCREMENTAL or KEYFRAME_RELOCK samples."
            ),
        )
    slope = float(np.sum(weights * raw * angle) / denominator)
    if not math.isfinite(slope) or abs(slope) <= 1e-9:
        return _invalid_calibration(
            "phase_calibration_degenerate_fit", "Calibration slope is invalid."
        )

    residual = angle - slope * raw
    if len(observations) == 4:
        # Four pairs are the documented minimum. Rejecting one can never leave a
        # valid fit, so report the actual fit error instead of a sample-count error.
        inliers = np.ones(len(observations), dtype=np.bool_)
    else:
        center = float(np.median(residual))
        mad = float(np.median(np.abs(residual - center)))
        limit = max(3.5 * 1.4826 * mad, 1.0)
        inliers = np.abs(residual - center) <= limit
    if int(np.count_nonzero(inliers)) < 4:
        rejected = [
            item.actual_marking.strip() or item.observation_id
            for item, keep in zip(observations, inliers, strict=True)
            if not bool(keep)
        ]
        preliminary_errors = np.abs(residual)
        return StemSurfacePhaseCalibrationComputation(
            status="invalid",
            profile=None,
            mean_abs_error_deg=float(np.mean(preliminary_errors)),
            rms_error_deg=float(np.sqrt(np.mean(preliminary_errors**2))),
            maximum_abs_error_deg=float(np.max(preliminary_errors)),
            failure_code="phase_calibration_samples_insufficient",
            failure_message=(
                f"Robust fitting rejected {len(rejected)} calibration pair(s) "
                f"({', '.join(rejected[:4])}); only {int(np.count_nonzero(inliers))} "
                "remain, but four are required. Correct the visible reference angles or "
                "select additional frames from the same uninterrupted trace."
            ),
        )
    slope = float(
        np.sum(weights[inliers] * raw[inliers] * angle[inliers])
        / np.sum(weights[inliers] * raw[inliers] * raw[inliers])
    )
    accepted = [item for item, keep in zip(observations, inliers, strict=True) if bool(keep)]
    errors = np.abs(
        np.asarray([item.known_angle_deg for item in accepted], dtype=np.float64)
        - slope * np.asarray([item.raw_phase for item in accepted], dtype=np.float64)
    )
    mean_error = float(np.mean(errors))
    rms_error = float(np.sqrt(np.mean(errors**2)))
    maximum_error = float(np.max(errors))
    if (
        mean_error > request.maximum_mean_abs_error_deg
        or maximum_error > request.maximum_abs_error_deg
    ):
        worst_index = int(np.argmax(errors))
        worst_observation = accepted[worst_index]
        worst_label = (
            worst_observation.actual_marking.strip()
            or worst_observation.observation_id
        )
        worst_calculated_angle = float(slope * worst_observation.raw_phase)
        return StemSurfacePhaseCalibrationComputation(
            status="invalid",
            profile=None,
            mean_abs_error_deg=mean_error,
            rms_error_deg=rms_error,
            maximum_abs_error_deg=maximum_error,
            failure_code="phase_calibration_error_too_high",
            failure_message=(
                f"Phase-to-angle fit error is mean {mean_error:.2f} deg and maximum "
                f"{maximum_error:.2f} deg. Largest mismatch: {worst_label} was entered as "
                f"{worst_observation.known_angle_deg:.2f} deg while the fit gives "
                f"{worst_calculated_angle:.2f} deg. Check the actual signed marker angle "
                "and any backward roll; do not edit raw phase manually."
            ),
        )

    evaluated_observations = [
        item.model_copy(
            update={
                "calculated_phase_deg": float(slope * item.raw_phase),
                "error_deg": abs(float(item.known_angle_deg - slope * item.raw_phase)),
                "accepted": bool(keep),
            }
        )
        for item, keep in zip(observations, inliers, strict=True)
    ]

    accepted_diameters = np.asarray(
        [item.diameter_mm for item in accepted], dtype=np.float64
    )
    observed_diameter_min = float(np.min(accepted_diameters))
    observed_diameter_max = float(np.max(accepted_diameters))
    if (
        request.valid_diameter_min_mm is not None
        and request.valid_diameter_max_mm is not None
    ):
        valid_diameter_min = float(request.valid_diameter_min_mm)
        valid_diameter_max = float(request.valid_diameter_max_mm)
    else:
        # Raw phase is normalized by the measured cross-section width. A moderate,
        # explicit tolerance around the commissioning body is therefore justified,
        # while a configured product class remains the authoritative option.
        diameter_margin = max(
            float(np.median(accepted_diameters)) * request.diameter_tolerance_ratio,
            (observed_diameter_max - observed_diameter_min) * 0.5,
        )
        valid_diameter_min = max(observed_diameter_min - diameter_margin, 1e-6)
        valid_diameter_max = observed_diameter_max + diameter_margin

    accepted_center_x = [item.image_center_x_px for item in accepted]
    accepted_center_y = [item.image_center_y_px for item in accepted]
    x_margin = max(
        request.image_width * request.image_center_tolerance_x_ratio,
        (max(accepted_center_x) - min(accepted_center_x)) * 0.5,
    )
    y_margin = max(
        request.image_height * request.image_center_tolerance_y_ratio,
        (max(accepted_center_y) - min(accepted_center_y)) * 0.5,
    )

    profile = RuntimeStemSurfacePhaseCalibrationProfile(
        calibration_id=request.calibration_id,
        calibration_version=request.calibration_version,
        status="valid",
        source_channel_id=source_channel_id,
        image_width=request.image_width,
        image_height=request.image_height,
        intrinsic_calibration_hash=request.intrinsic_calibration_hash,
        process_calibration_hash=request.process_calibration_hash,
        rotation_reference_view=request.rotation_reference_view,
        positive_rotation=request.positive_rotation,
        phase_estimator_version=request.phase_estimator_version,
        degrees_per_raw_phase=slope,
        observations=evaluated_observations,
        accepted_observation_count=len(accepted),
        mean_abs_error_deg=mean_error,
        rms_error_deg=rms_error,
        maximum_abs_error_deg=maximum_error,
        maximum_mean_abs_error_deg=request.maximum_mean_abs_error_deg,
        maximum_abs_error_limit_deg=request.maximum_abs_error_deg,
        requested_valid_diameter_min_mm=request.valid_diameter_min_mm,
        requested_valid_diameter_max_mm=request.valid_diameter_max_mm,
        diameter_tolerance_ratio=request.diameter_tolerance_ratio,
        image_center_tolerance_x_ratio=request.image_center_tolerance_x_ratio,
        image_center_tolerance_y_ratio=request.image_center_tolerance_y_ratio,
        valid_diameter_min_mm=valid_diameter_min,
        valid_diameter_max_mm=valid_diameter_max,
        valid_image_center_x_min_px=max(min(accepted_center_x) - x_margin, 0.0),
        valid_image_center_x_max_px=min(
            max(accepted_center_x) + x_margin,
            float(request.image_width - 1),
        ),
        valid_image_center_y_min_px=max(min(accepted_center_y) - y_margin, 0.0),
        valid_image_center_y_max_px=min(
            max(accepted_center_y) + y_margin,
            float(request.image_height - 1),
        ),
        provenance=RuntimeStemSurfacePhaseCalibrationProvenance(
            method=request.method,
            created_at=generated_at or utc_now_iso(),
            operator_user_id=operator_user_id,
            reference_asset_id=request.reference_asset_id,
            notes=request.notes,
        ),
        calibration_hash="0" * 64,
    )
    profile = profile.model_copy(
        update={"calibration_hash": stem_surface_phase_calibration_hash(profile)}
    )
    validate_stem_surface_phase_calibration(profile)
    return StemSurfacePhaseCalibrationComputation(
        status="valid",
        profile=profile,
        mean_abs_error_deg=mean_error,
        rms_error_deg=rms_error,
        maximum_abs_error_deg=maximum_error,
    )


def stem_surface_phase_calibration_hash(
    value: RuntimeStemSurfacePhaseCalibrationProfile | dict[str, Any],
) -> str:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, RuntimeStemSurfacePhaseCalibrationProfile)
        else dict(value)
    )
    payload.pop("calibration_hash", None)
    payload = _normalize_signed_zero(payload)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_signed_zero(value: Any) -> Any:
    """Make calibration hashes stable across JSON/browser round-trips.

    JavaScript serializes ``-0`` as ``0``. A negative through-origin fit can produce
    ``-0.0`` for the calculated phase of the 0-degree reference even though both values
    are numerically identical. Normalizing signed zero before hashing prevents an
    applied profile from becoming invalid when the pipeline editor sends it back.
    """

    if isinstance(value, dict):
        return {key: _normalize_signed_zero(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_signed_zero(item) for item in value]
    if isinstance(value, float) and value == 0.0:
        return 0.0
    return value


def _pre_signed_zero_normalization_hash(
    value: RuntimeStemSurfacePhaseCalibrationProfile,
) -> str:
    """Accept profiles created before signed-zero normalization was introduced."""

    payload = value.model_dump(mode="json")
    payload.pop("calibration_hash", None)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pre_persisted_validity_inputs_hash(
    value: RuntimeStemSurfacePhaseCalibrationProfile,
) -> str:
    """Accept profiles created before calculation inputs were persisted.

    Older v1 profiles contain only the resulting diameter/image ranges. Keeping the
    original request values now lets the editor restore its controls after saving,
    but those optional additions must not invalidate an already deployed profile.
    """

    payload = value.model_dump(mode="json")
    payload.pop("calibration_hash", None)
    for key in (
        "requested_valid_diameter_min_mm",
        "requested_valid_diameter_max_mm",
        "diameter_tolerance_ratio",
        "image_center_tolerance_x_ratio",
        "image_center_tolerance_y_ratio",
    ):
        payload.pop(key, None)
    payload = _normalize_signed_zero(payload)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pre_estimator_binding_hash(
    value: RuntimeStemSurfacePhaseCalibrationProfile,
) -> str:
    """Accept v1 profiles created before estimator binding was persisted."""

    payload = value.model_dump(mode="json")
    payload.pop("calibration_hash", None)
    payload.pop("phase_estimator_version", None)
    payload = _normalize_signed_zero(payload)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pre_estimator_and_validity_inputs_hash(
    value: RuntimeStemSurfacePhaseCalibrationProfile,
) -> str:
    payload = value.model_dump(mode="json")
    payload.pop("calibration_hash", None)
    for key in (
        "phase_estimator_version",
        "requested_valid_diameter_min_mm",
        "requested_valid_diameter_max_mm",
        "diameter_tolerance_ratio",
        "image_center_tolerance_x_ratio",
        "image_center_tolerance_y_ratio",
    ):
        payload.pop(key, None)
    payload = _normalize_signed_zero(payload)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _recomputed_evaluation_surface_phase_calibration_hash(
    value: RuntimeStemSurfacePhaseCalibrationProfile,
) -> str:
    """Recover an old hash after a browser converted derived ``-0.0`` to ``0``.

    Only server-derived fit diagnostics are recomputed. Operator observations and all
    calibration bindings remain protected by the content hash.
    """

    payload = value.model_dump(mode="json")
    payload.pop("calibration_hash", None)
    slope = value.degrees_per_raw_phase
    if slope is None:
        return ""
    for observation in payload.get("observations", []):
        if not isinstance(observation, dict):
            continue
        calculated = float(slope * float(observation["raw_phase"]))
        if observation.get("calculated_phase_deg") is not None:
            observation["calculated_phase_deg"] = calculated
        if observation.get("error_deg") is not None:
            observation["error_deg"] = abs(
                float(observation["known_angle_deg"]) - calculated
            )
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_surface_phase_calibration_hash(
    value: RuntimeStemSurfacePhaseCalibrationProfile,
) -> str:
    payload = value.model_dump(mode="json")
    payload.pop("calibration_hash", None)
    for observation in payload.get("observations", []):
        if not isinstance(observation, dict):
            continue
        observation.pop("actual_marking", None)
        observation.pop("calculated_phase_deg", None)
        observation.pop("error_deg", None)
        observation.pop("accepted", None)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_stem_surface_phase_calibration(
    value: object,
) -> RuntimeStemSurfacePhaseCalibrationProfile:
    profile = RuntimeStemSurfacePhaseCalibrationProfile.model_validate(value)
    if profile.status != "valid" or profile.degrees_per_raw_phase is None:
        raise ValueError("stem surface-phase calibration is not valid")
    if profile.accepted_observation_count < 4:
        raise ValueError("stem surface-phase calibration has too few accepted observations")
    if (
        profile.mean_abs_error_deg is None
        or profile.maximum_abs_error_deg is None
        or profile.mean_abs_error_deg > profile.maximum_mean_abs_error_deg
        or profile.maximum_abs_error_deg > profile.maximum_abs_error_limit_deg
    ):
        raise ValueError("stem surface-phase calibration exceeds its error limits")
    if (
        stem_surface_phase_calibration_hash(profile) != profile.calibration_hash
        and _pre_signed_zero_normalization_hash(profile) != profile.calibration_hash
        and _recomputed_evaluation_surface_phase_calibration_hash(profile)
        != profile.calibration_hash
        and _legacy_surface_phase_calibration_hash(profile) != profile.calibration_hash
        and _pre_persisted_validity_inputs_hash(profile) != profile.calibration_hash
        and _pre_estimator_binding_hash(profile) != profile.calibration_hash
        and _pre_estimator_and_validity_inputs_hash(profile) != profile.calibration_hash
    ):
        raise ValueError("stem surface-phase calibration hash does not match")
    return profile


def phase_calibration_runtime_reasons(
    profile: RuntimeStemSurfacePhaseCalibrationProfile | None,
    *,
    image_width: int,
    image_height: int,
    intrinsic_calibration_hash: str | None,
    process_calibration_hash: str | None,
    source_channel_id: str | None,
    diameter_mm: float | None,
    center_x_px: float,
    center_y_px: float,
    rotation_reference_view: str,
    positive_rotation: str,
    phase_estimator_version: str = "LK_KEYFRAME_RELOCK_V1",
) -> tuple[str, ...]:
    if profile is None:
        return ("PHASE_CALIBRATION_MISSING",)
    reasons: list[str] = []
    if (image_width, image_height) != (profile.image_width, profile.image_height):
        reasons.append("PHASE_CALIBRATION_RESOLUTION_MISMATCH")
    if intrinsic_calibration_hash != profile.intrinsic_calibration_hash:
        reasons.append("PHASE_CALIBRATION_INTRINSIC_MISMATCH")
    if process_calibration_hash != profile.process_calibration_hash:
        reasons.append("PHASE_CALIBRATION_PROCESS_MISMATCH")
    if source_channel_id is not None and source_channel_id != profile.source_channel_id:
        reasons.append("PHASE_CALIBRATION_CHANNEL_MISMATCH")
    if (
        rotation_reference_view != profile.rotation_reference_view
        or positive_rotation != profile.positive_rotation
    ):
        reasons.append("PHASE_CALIBRATION_DIRECTION_MISMATCH")
    if phase_estimator_version != profile.phase_estimator_version:
        reasons.append("PHASE_CALIBRATION_ESTIMATOR_MISMATCH")
    if diameter_mm is None or not _inside(
        diameter_mm, profile.valid_diameter_min_mm, profile.valid_diameter_max_mm
    ):
        reasons.append("PHASE_CALIBRATION_OUTSIDE_DIAMETER_RANGE")
    if not _inside(
        center_x_px, profile.valid_image_center_x_min_px, profile.valid_image_center_x_max_px
    ) or not _inside(
        center_y_px, profile.valid_image_center_y_min_px, profile.valid_image_center_y_max_px
    ):
        reasons.append("PHASE_CALIBRATION_OUTSIDE_IMAGE_RANGE")
    return tuple(dict.fromkeys(reasons))


def estimate_raw_surface_phase_from_matches(
    matches: Sequence[SurfaceFeatureMatch],
    *,
    previous_axis: StemAxisObservation,
    current_axis: StemAxisObservation,
    config: RawSurfacePhaseConfig | None = None,
) -> RawSurfacePhaseEstimate:
    resolved = config or RawSurfacePhaseConfig()
    translation = current_axis.center - previous_axis.center
    axis_rotation = _signed_axis_rotation_deg(previous_axis.direction, current_axis.direction)
    common = {
        "translation_x_px": float(translation[0]),
        "translation_y_px": float(translation[1]),
        "translation_magnitude_px": float(np.linalg.norm(translation)),
        "axis_rotation_deg": axis_rotation,
    }
    if previous_axis.length_px <= 0 or current_axis.length_px <= 0:
        return _invalid_raw(len(matches), ("PHASE_GEOMETRY_INVALID",), **common)
    accepted: list[tuple[int, float, float]] = []
    for index, match in enumerate(matches):
        values = (
            match.previous_x_px,
            match.previous_y_px,
            match.current_x_px,
            match.current_y_px,
            match.tracking_error_px,
        )
        if (
            not all(math.isfinite(float(value)) for value in values)
            or match.tracking_error_px > resolved.maximum_tracking_error_px
        ):
            continue
        previous_local = (
            np.asarray((match.previous_x_px, match.previous_y_px)) - previous_axis.center
        )
        current_local = np.asarray((match.current_x_px, match.current_y_px)) - current_axis.center
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
        return _invalid_raw(len(accepted), ("PHASE_FEATURE_COUNT_INSUFFICIENT",), **common)
    displacement = np.asarray([item[1] for item in accepted], dtype=np.float64)
    median = float(np.median(displacement))
    residual = np.abs(displacement - median)
    limit = max(
        resolved.absolute_inlier_tolerance_raw,
        resolved.mad_multiplier * 1.4826 * float(np.median(residual)),
    )
    mask = residual <= limit
    inlier_count = int(np.count_nonzero(mask))
    ratio = inlier_count / len(accepted)
    indices = tuple(accepted[index][0] for index, keep in enumerate(mask) if bool(keep))
    if inlier_count < resolved.minimum_matched_features or ratio < resolved.minimum_inlier_ratio:
        return _invalid_raw(
            len(accepted), ("PHASE_INLIER_RATIO_LOW",), inlier_count, ratio, indices, **common
        )
    raw_delta = float(np.median(displacement[mask]))
    errors = np.asarray([accepted[index][2] for index, keep in enumerate(mask) if bool(keep)])
    median_error = float(np.median(errors)) if errors.size else None
    if abs(raw_delta) > resolved.maximum_abs_delta_raw_phase:
        return _invalid_raw(
            len(accepted),
            ("PHASE_STEP_IMPLAUSIBLE",),
            inlier_count,
            ratio,
            indices,
            median_error,
            **common,
        )
    count_quality = min(inlier_count / max(resolved.minimum_matched_features * 2, 1), 1.0)
    error_quality = max(0.0, 1.0 - float(median_error or 0.0) / resolved.maximum_tracking_error_px)
    confidence = min(max(0.55 * ratio + 0.25 * count_quality + 0.20 * error_quality, 0.0), 1.0)
    if confidence < resolved.minimum_confidence:
        return _invalid_raw(
            len(accepted),
            ("PHASE_FEATURE_QUALITY_LOW",),
            inlier_count,
            ratio,
            indices,
            median_error,
            confidence,
            **common,
        )
    return RawSurfacePhaseEstimate(
        True,
        raw_delta,
        len(accepted),
        inlier_count,
        ratio,
        median_error,
        confidence=confidence,
        inlier_match_indices=indices,
        reason_codes=(),
        **common,
    )


def estimate_cylindrical_surface_phase_from_matches(
    matches: Sequence[SurfaceFeatureMatch],
    *,
    previous_axis: StemAxisObservation,
    current_axis: StemAxisObservation,
    config: CylindricalSurfacePhaseConfig | None = None,
) -> RawSurfacePhaseEstimate:
    """Jointly estimate transverse translation and cylindrical surface rotation.

    For a visible point on an approximately circular cross-section, its normalized
    image coordinate is ``u = 0.5 * sin(theta)``.  For the small inter-frame motion
    of the high-rate path, ``du = 0.5 * cos(theta) * dtheta``.  A robust regression
    with a free intercept separates whole-stem translation from the phase term.
    """

    resolved = config or CylindricalSurfacePhaseConfig()
    center_translation = current_axis.center - previous_axis.center
    axis_rotation = _signed_axis_rotation_deg(previous_axis.direction, current_axis.direction)
    common = {
        "translation_x_px": float(center_translation[0]),
        "translation_y_px": float(center_translation[1]),
        "translation_magnitude_px": float(np.linalg.norm(center_translation)),
        "axis_rotation_deg": axis_rotation,
    }
    if previous_axis.length_px <= 0 or current_axis.length_px <= 0:
        return _invalid_raw(len(matches), ("PHASE_GEOMETRY_INVALID",), **common)

    accepted: list[tuple[int, float, float, float, float]] = []
    longitudinal_displacements: list[float] = []
    for index, match in enumerate(matches):
        values = (
            match.previous_x_px,
            match.previous_y_px,
            match.current_x_px,
            match.current_y_px,
            match.tracking_error_px,
        )
        if (
            not all(math.isfinite(float(value)) for value in values)
            or match.tracking_error_px > resolved.maximum_tracking_error_px
        ):
            continue
        previous_local = (
            np.asarray((match.previous_x_px, match.previous_y_px), dtype=np.float64)
            - previous_axis.center
        )
        current_local = (
            np.asarray((match.current_x_px, match.current_y_px), dtype=np.float64)
            - current_axis.center
        )
        previous_s = float(
            np.dot(previous_local, previous_axis.direction) / previous_axis.length_px
        )
        current_s = float(np.dot(current_local, current_axis.direction) / current_axis.length_px)
        if abs(current_s - previous_s) > resolved.maximum_longitudinal_shift_fraction:
            continue
        previous_u = float(
            np.dot(previous_local, previous_axis.normal)
            / max(previous_axis.median_width_px, 1e-9)
        )
        current_u = float(
            np.dot(current_local, current_axis.normal)
            / max(current_axis.median_width_px, 1e-9)
        )
        if abs(previous_u) > resolved.maximum_surface_coordinate:
            continue
        phase_basis = math.sqrt(max(0.25 - previous_u * previous_u, 1e-6))
        accepted.append(
            (index, phase_basis, current_u - previous_u, match.tracking_error_px, previous_u)
        )
        previous_longitudinal_px = float(
            np.dot(previous_local, previous_axis.direction)
        )
        current_longitudinal_px = float(
            np.dot(current_local, current_axis.direction)
        )
        longitudinal_displacements.append(
            current_longitudinal_px - previous_longitudinal_px
        )

    if len(accepted) < resolved.minimum_matched_features:
        return _invalid_raw(
            len(accepted), ("PHASE_FEATURE_COUNT_INSUFFICIENT",), **common
        )
    feature_coordinates = np.asarray([item[4] for item in accepted], dtype=np.float64)
    feature_span = float(np.ptp(feature_coordinates))
    if feature_span < resolved.minimum_feature_span_ratio:
        return _invalid_raw(
            len(accepted), ("PHASE_FEATURE_COVERAGE_INSUFFICIENT",), **common
        )

    design = np.asarray([[1.0, item[1]] for item in accepted], dtype=np.float64)
    displacement = np.asarray([item[2] for item in accepted], dtype=np.float64)
    try:
        coefficients, *_unused = np.linalg.lstsq(design, displacement, rcond=None)
    except np.linalg.LinAlgError:
        return _invalid_raw(len(accepted), ("PHASE_CYLINDRICAL_FIT_FAILED",), **common)
    residual = np.abs(displacement - design @ coefficients)
    residual_center = float(np.median(residual))
    limit = max(
        resolved.residual_tolerance_normalized,
        resolved.mad_multiplier * 1.4826 * float(np.median(np.abs(residual - residual_center))),
    )
    inlier_mask = residual <= limit
    inlier_count = int(np.count_nonzero(inlier_mask))
    inlier_ratio = inlier_count / len(accepted)
    indices = tuple(
        accepted[index][0] for index, keep in enumerate(inlier_mask) if bool(keep)
    )
    if (
        inlier_count < resolved.minimum_matched_features
        or inlier_ratio < resolved.minimum_inlier_ratio
    ):
        return _invalid_raw(
            len(accepted),
            ("PHASE_INLIER_RATIO_LOW",),
            inlier_count,
            inlier_ratio,
            indices,
            **common,
        )
    inlier_design = design[inlier_mask]
    inlier_displacement = displacement[inlier_mask]
    try:
        coefficients, *_unused = np.linalg.lstsq(
            inlier_design, inlier_displacement, rcond=None
        )
    except np.linalg.LinAlgError:
        return _invalid_raw(len(accepted), ("PHASE_CYLINDRICAL_FIT_FAILED",), **common)
    transverse_translation_normalized = float(coefficients[0])
    raw_delta = float(coefficients[1])
    errors = np.asarray(
        [accepted[index][3] for index, keep in enumerate(inlier_mask) if bool(keep)],
        dtype=np.float64,
    )
    median_error = float(np.median(errors)) if errors.size else None
    longitudinal_translation_px = (
        float(np.median(np.asarray(longitudinal_displacements, dtype=np.float64)))
        if longitudinal_displacements
        else 0.0
    )
    transverse_translation_px = (
        transverse_translation_normalized * current_axis.median_width_px
    )
    translation_vector = (
        current_axis.direction * longitudinal_translation_px
        + current_axis.normal * transverse_translation_px
        + center_translation
    )
    common.update(
        {
            "translation_x_px": float(translation_vector[0]),
            "translation_y_px": float(translation_vector[1]),
            "translation_magnitude_px": float(np.linalg.norm(translation_vector)),
        }
    )
    if abs(raw_delta) > resolved.maximum_abs_delta_raw_phase:
        return _invalid_raw(
            len(accepted),
            ("PHASE_STEP_IMPLAUSIBLE",),
            inlier_count,
            inlier_ratio,
            indices,
            median_error,
            **common,
        )
    count_quality = min(inlier_count / max(resolved.minimum_matched_features * 2, 1), 1.0)
    error_quality = max(
        0.0,
        1.0 - float(median_error or 0.0) / resolved.maximum_tracking_error_px,
    )
    coverage_quality = min(
        feature_span / max(resolved.minimum_feature_span_ratio * 1.5, 1e-9),
        1.0,
    )
    confidence = min(
        max(
            0.40 * inlier_ratio
            + 0.20 * count_quality
            + 0.20 * error_quality
            + 0.20 * coverage_quality,
            0.0,
        ),
        1.0,
    )
    if confidence < resolved.minimum_confidence:
        return _invalid_raw(
            len(accepted),
            ("PHASE_FEATURE_QUALITY_LOW",),
            inlier_count,
            inlier_ratio,
            indices,
            median_error,
            confidence,
            **common,
        )
    return RawSurfacePhaseEstimate(
        valid=True,
        delta_raw_phase=raw_delta,
        matched_feature_count=len(accepted),
        inlier_feature_count=inlier_count,
        inlier_ratio=inlier_ratio,
        median_tracking_error_px=median_error,
        confidence=confidence,
        inlier_match_indices=indices,
        reason_codes=(),
        algorithm_version=SURFACE_PHASE_V2_ALGORITHM_VERSION,
        **common,
    )


def select_phase_relock_candidate(
    candidates: Sequence[PhaseRelockCandidate],
    *,
    last_safe_raw_phase: float,
    minimum_score: float = 0.45,
    uniqueness_margin: float = 0.10,
    maximum_raw_jump: float = 0.75,
) -> PhaseRelockDecision:
    plausible = [
        item
        for item in candidates
        if item.score >= minimum_score
        and abs(item.raw_phase - last_safe_raw_phase) <= maximum_raw_jump
    ]
    plausible.sort(key=lambda item: item.score, reverse=True)
    if not plausible:
        return PhaseRelockDecision(
            "FAILED", None, ("PHASE_RELOCK_NO_PLAUSIBLE_MATCH", "PHASE_TRACK_LOST")
        )
    if len(plausible) > 1 and plausible[0].score - plausible[1].score < uniqueness_margin:
        return PhaseRelockDecision(
            "AMBIGUOUS", None, ("PHASE_RELOCK_AMBIGUOUS", "PHASE_TRACK_LOST")
        )
    return PhaseRelockDecision("RELOCKED", plausible[0], ("PHASE_RELOCKED",))


def _invalid_calibration(code: str, message: str) -> StemSurfacePhaseCalibrationComputation:
    return StemSurfacePhaseCalibrationComputation(
        "invalid", None, failure_code=code, failure_message=message
    )


def _invalid_raw(
    matched_count: int,
    reasons: tuple[str, ...],
    inlier_count: int = 0,
    inlier_ratio: float = 0.0,
    indices: tuple[int, ...] = (),
    median_error: float | None = None,
    confidence: float = 0.0,
    **common: float,
) -> RawSurfacePhaseEstimate:
    return RawSurfacePhaseEstimate(
        valid=False,
        delta_raw_phase=None,
        matched_feature_count=matched_count,
        inlier_feature_count=inlier_count,
        inlier_ratio=inlier_ratio,
        median_tracking_error_px=median_error,
        confidence=confidence,
        inlier_match_indices=indices,
        reason_codes=reasons,
        **common,
    )


def _signed_axis_rotation_deg(previous: np.ndarray, current: np.ndarray) -> float:
    cross = float(previous[0] * current[1] - previous[1] * current[0])
    dot = float(np.dot(previous, current))
    return math.degrees(math.atan2(cross, dot))


def _inside(value: float, lower: float, upper: float) -> bool:
    tolerance = max((upper - lower) * 0.05, 1e-6)
    return lower - tolerance <= value <= upper + tolerance
