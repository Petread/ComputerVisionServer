from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass

from cv_datasets.models import (
    CanonicalSequenceItem,
    DerivedTarget,
    SequenceChoiceAnnotation,
    TimelineIntervalAnnotation,
)
from cv_datasets.target_builders.base import (
    RotationTraceSample,
    TargetBuildContext,
    TargetBuildResult,
)

BUILDER_ID = "stem_orientation_angle"
BUILDER_VERSION = "v1"
_ANGLE_EPSILON_DEG = 1e-9


@dataclass(frozen=True)
class _InterpolatedRotation:
    angle_deg: float
    confidence: float
    support: tuple[RotationTraceSample, ...]


def build_stem_orientation_targets(
    item: CanonicalSequenceItem,
    context: TargetBuildContext,
) -> TargetBuildResult:
    """Build schema-specific targets without leaking stem rules into dataset materialization."""

    raw_rotation_trace = context.resources.get("rotation_trace")
    if not isinstance(raw_rotation_trace, list | tuple) or not all(
        isinstance(sample, RotationTraceSample) for sample in raw_rotation_trace
    ):
        rotation_trace: list[RotationTraceSample] = []
    else:
        rotation_trace = list(raw_rotation_trace)
    minimum_rotation_confidence = context.numeric_options.get(
        "minimum_rotation_confidence", 0.0
    )
    values = {
        annotation.name: annotation.value
        for annotation in item.human_labels.sequence_labels
        if isinstance(annotation, SequenceChoiceAnnotation)
    }
    if values.get("sequence_valid") == "unusable":
        return TargetBuildResult(
            exclusion_reason="sequence_unusable",
            reason_codes=["sequence_unusable"],
        )

    targets = [
        _classification_target(item, name, value)
        for name in ("bow_relevance", "target_visible", "final_orientation_quality")
        if isinstance((value := values.get(name)), str)
    ]
    if values.get("bow_relevance") == "nearly_straight":
        return _without_angle(
            item,
            targets,
            "angle_target_not_required_nearly_straight",
        )
    if values.get("target_visible") != "yes":
        return _without_angle(item, targets, "target_orientation_not_visible")
    if not _attribute_string(item, "rotation_viewpoint_convention") or not _attribute_string(
        item, "positive_rotation_direction"
    ):
        return _without_angle(item, targets, "rotation_direction_convention_missing")

    target_intervals = [
        annotation
        for annotation in item.human_labels.timeline_labels
        if isinstance(annotation, TimelineIntervalAnnotation)
        and annotation.name == "target_orientation"
    ]
    if len(target_intervals) != 1:
        return _without_angle(item, targets, "target_orientation_interval_invalid")
    interval = target_intervals[0]

    trace_issue, ordered_trace = _validated_trace(rotation_trace)
    if trace_issue is not None:
        return _without_angle(item, targets, trace_issue)

    frame_by_index = {frame.frame_index: frame for frame in item.frames}
    target_frames = [
        frame
        for frame in item.frames
        if interval.start_frame_index <= frame.frame_index <= interval.end_frame_index
    ]
    decision_frame = frame_by_index[item.decision_frame_index]
    interpolated_targets = [
        _interpolate_rotation(ordered_trace, frame.timestamp) for frame in target_frames
    ]
    interpolated_decision = _interpolate_rotation(ordered_trace, decision_frame.timestamp)
    if not interpolated_targets or any(value is None for value in interpolated_targets):
        return _without_angle(item, targets, "rotation_trace_samples_missing")
    if interpolated_decision is None:
        return _without_angle(item, targets, "rotation_trace_samples_missing")

    rotations = [
        value for value in [*interpolated_targets, interpolated_decision] if value is not None
    ]
    support = _unique_support(rotations)
    interval_start_timestamp = target_frames[0].timestamp
    decision_timestamp = decision_frame.timestamp
    trace_window = [
        sample
        for sample in ordered_trace
        if interval_start_timestamp <= sample.timestamp <= decision_timestamp
    ]
    support_and_window = _unique_samples([*support, *trace_window])
    if any(not sample.valid for sample in support_and_window):
        return _without_angle(item, targets, "rotation_trace_sample_invalid")
    if any(not sample.calibration_valid for sample in support_and_window):
        return _without_angle(item, targets, "rotation_calibration_invalid")
    if any(sample.trace_discontinuity for sample in support_and_window):
        return _without_angle(item, targets, "rotation_trace_discontinuous")
    generations = {
        sample.trace_generation
        for sample in support_and_window
        if sample.trace_generation is not None
    }
    if len(generations) > 1:
        return _without_angle(item, targets, "rotation_trace_discontinuous")
    if len({sample.calibration_id for sample in support_and_window}) != 1:
        return _without_angle(item, targets, "rotation_calibration_changed")
    if len({sample.calibration_method for sample in support_and_window}) != 1:
        return _without_angle(item, targets, "rotation_calibration_changed")
    if len({sample.calibration_version for sample in support_and_window}) != 1:
        return _without_angle(item, targets, "rotation_calibration_changed")
    quality = min(sample.rotation_confidence for sample in support_and_window)
    if quality < minimum_rotation_confidence:
        return _without_angle(item, targets, "rotation_confidence_too_low")

    target_angles = [
        value.angle_deg for value in interpolated_targets if value is not None
    ]
    target_rotation = float(statistics.median(target_angles))
    decision_rotation = interpolated_decision.angle_deg
    correction = wrap_to_half_turn(target_rotation - decision_rotation)
    relative_interval = [
        correction
        + wrap_to_half_turn((angle - decision_rotation) - correction)
        for angle in target_angles
    ]
    interval_bounds = [min(relative_interval), max(relative_interval)]
    tolerance = max(abs(angle - correction) for angle in relative_interval)
    direction = (
        "positive"
        if correction > _ANGLE_EPSILON_DEG
        else "negative"
        if correction < -_ANGLE_EPSILON_DEG
        else "no_rotation"
    )
    label_tier = item.label_quality.tier or "silver"
    label_source = (
        "gold_verified_rotation_reference"
        if label_tier == "gold"
        else "silver_human_interval_visual_rotation_trace"
    )
    calibration = support_and_window[0]
    common_metadata: dict[str, str | float | bool | list[str] | list[float] | None] = {
        "rotation_calibration_id": calibration.calibration_id,
        "rotation_calibration_method": calibration.calibration_method,
        "rotation_calibration_version": calibration.calibration_version,
        "rotation_schema_version": item.schemas.rotation_schema,
        "calibration_schema_version": item.schemas.calibration_schema,
        "angle_label_source": label_source,
        "angle_label_quality_tier": label_tier,
        "rotation_viewpoint_convention": _attribute_string(
            item, "rotation_viewpoint_convention"
        ),
        "positive_rotation_direction": _attribute_string(
            item, "positive_rotation_direction"
        ),
    }
    targets.extend(
        [
            _target(item, "angle_target_valid", "boolean", True, "decision_frame", quality),
            _target(
                item,
                "signed_correction_deg",
                "regression",
                correction,
                "decision_frame",
                quality,
                metadata=common_metadata,
                reason_codes=["derived_from_human_interval_and_visual_rotation_trace"],
            ),
            # Kept as a compatibility alias for snapshots created from the initial v1 schema.
            _target(
                item,
                "recommended_rotation_deg_signed",
                "regression",
                correction,
                "decision_frame",
                quality,
                metadata={"alias_of": "signed_correction_deg"},
            ),
            _target(item, "rotation_direction", "classification", direction, "decision_frame"),
            _target(
                item,
                "target_angle_interval_deg",
                "timeline_interval",
                interval_bounds,
                "decision_window",
                quality,
                metadata={"reference": "decision_frame_correction_modulo_180"},
            ),
            _target(
                item,
                "angle_tolerance_deg",
                "regression",
                tolerance,
                "decision_frame",
                quality,
            ),
            _target(
                item,
                "sin_2e",
                "regression",
                math.sin(math.radians(2.0 * correction)),
                "decision_frame",
                quality,
            ),
            _target(
                item,
                "cos_2e",
                "regression",
                math.cos(math.radians(2.0 * correction)),
                "decision_frame",
                quality,
            ),
            _target(
                item,
                "angle_label_confidence",
                "regression",
                quality,
                "decision_frame",
                quality,
            ),
            _target(item, "angle_label_source", "classification", label_source, "sequence"),
            _target(item, "angle_label_quality", "classification", label_tier, "sequence"),
        ]
    )
    return TargetBuildResult(
        targets=targets,
        angle_target_generated=True,
        reason_codes=["angle_target_generated"],
    )


def wrap_to_half_turn(value: float) -> float:
    """Map an orientation delta to [-90, 90), preserving the modulo-180 contract."""

    return ((float(value) + 90.0) % 180.0) - 90.0


def _validated_trace(
    rotation_trace: list[RotationTraceSample],
) -> tuple[str | None, list[RotationTraceSample]]:
    ordered = sorted(rotation_trace, key=lambda sample: sample.timestamp)
    if not ordered:
        return "rotation_trace_samples_missing", ordered
    if len({sample.frame_index for sample in ordered}) != len(ordered):
        return "rotation_trace_duplicate_frames", ordered
    if [sample.frame_index for sample in ordered] != sorted(
        sample.frame_index for sample in ordered
    ):
        return "rotation_trace_frame_order_invalid", ordered
    timestamps = [sample.timestamp for sample in ordered]
    if len(set(timestamps)) != len(timestamps):
        return "rotation_trace_duplicate_timestamps", ordered
    if any(
        current <= previous
        for previous, current in zip(timestamps, timestamps[1:], strict=False)
    ):
        return "rotation_trace_timestamp_order_invalid", ordered
    if any(
        not math.isfinite(sample.cumulative_rotation_deg_unwrapped)
        or not math.isfinite(sample.rotation_confidence)
        for sample in ordered
    ):
        return "rotation_trace_non_finite", ordered
    return None, ordered


def _interpolate_rotation(
    ordered: list[RotationTraceSample], timestamp: float
) -> _InterpolatedRotation | None:
    timestamps = [sample.timestamp for sample in ordered]
    position = bisect.bisect_left(timestamps, timestamp)
    if position < len(ordered) and abs(ordered[position].timestamp - timestamp) <= 1e-9:
        sample = ordered[position]
        return _InterpolatedRotation(
            angle_deg=sample.cumulative_rotation_deg_unwrapped,
            confidence=sample.rotation_confidence,
            support=(sample,),
        )
    if position == 0 or position == len(ordered):
        return None
    before = ordered[position - 1]
    after = ordered[position]
    fraction = (timestamp - before.timestamp) / (after.timestamp - before.timestamp)
    return _InterpolatedRotation(
        angle_deg=(
            before.cumulative_rotation_deg_unwrapped
            + fraction
            * (
                after.cumulative_rotation_deg_unwrapped
                - before.cumulative_rotation_deg_unwrapped
            )
        ),
        confidence=min(before.rotation_confidence, after.rotation_confidence),
        support=(before, after),
    )


def _unique_support(
    values: list[_InterpolatedRotation],
) -> list[RotationTraceSample]:
    return _unique_samples([sample for value in values for sample in value.support])


def _unique_samples(values: list[RotationTraceSample]) -> list[RotationTraceSample]:
    by_frame: dict[int, RotationTraceSample] = {}
    for value in values:
        by_frame[value.frame_index] = value
    return [by_frame[key] for key in sorted(by_frame)]


def _without_angle(
    item: CanonicalSequenceItem,
    targets: list[DerivedTarget],
    reason_code: str,
) -> TargetBuildResult:
    targets.append(
        _target(
            item,
            "angle_target_valid",
            "boolean",
            False,
            "decision_frame",
            reason_codes=[reason_code],
        )
    )
    return TargetBuildResult(targets=targets, reason_codes=[reason_code])


def _classification_target(
    item: CanonicalSequenceItem,
    name: str,
    value: str,
) -> DerivedTarget:
    return _target(
        item,
        name,
        "classification",
        value,
        "decision_window" if name == "final_orientation_quality" else "sequence",
    )


def _target(
    item: CanonicalSequenceItem,
    name: str,
    kind: str,
    value: str | bool | int | float | list[str | bool | int | float],
    scope: str,
    quality: float | None = None,
    *,
    reason_codes: list[str] | None = None,
    metadata: dict[str, str | float | bool | list[str] | list[float] | None] | None = None,
) -> DerivedTarget:
    return DerivedTarget(
        name=name,
        kind=kind,
        value=value,
        scope=scope,
        builder_id=BUILDER_ID,
        builder_version=BUILDER_VERSION,
        source_annotation_schema=item.schemas.annotation_schema,
        quality=quality,
        reason_codes=reason_codes or [],
        metadata=metadata or {},
    )


def _attribute_string(item: CanonicalSequenceItem, key: str) -> str | None:
    value = item.provenance.attributes.get(key)
    return value if isinstance(value, str) and value else None
