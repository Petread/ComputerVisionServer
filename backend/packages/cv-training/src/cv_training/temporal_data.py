from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cv_datasets.annotation_schemas import get_annotation_schema
from cv_datasets.models import (
    CanonicalSequenceDataset,
    CanonicalSequenceItem,
    DerivedTarget,
    SequenceChoiceAnnotation,
)
from cv_datasets.target_builders.base import RotationTraceSample, TargetBuildContext
from cv_status import TaskType

from cv_training.paths import read_json, write_json
from cv_training.trainer_types import TrainerDatasetArtifact

TEMPORAL_TRAINING_CONTRACT_VERSION = "temporal-training.v1"
STEM_ORIENTATION_FEATURE_SET_ID = "stem_orientation.frame_features.v1"
_SPLITS = ("train", "val", "test")
_BANNED_FEATURE_TOKENS = (
    "action",
    "button",
    "operator_input",
    "plc",
    "sps",
    "taster",
)


class TemporalDatasetPreparationError(ValueError):
    pass


@dataclass(frozen=True)
class TemporalFeatureSpec:
    name: str
    path: str
    kind: Literal["scalar", "vector", "point_x", "point_y"] = "scalar"
    length: int = 1
    validity_path: str | None = None
    divisor_path: str | None = None
    scale: bool = True
    required: bool = False


@dataclass(frozen=True)
class _RawTemporalSample:
    sample_id: str
    sequence_id: str
    group_id: str
    split: str
    label_quality_tier: str
    features: list[list[float]]
    valid: list[list[bool]]
    targets: dict[str, Any]
    metadata: dict[str, Any]
    deterministic_baseline: dict[str, Any]
    prefix_augmented: bool = False


@dataclass(frozen=True)
class _Scaler:
    mean: list[float]
    scale: list[float]
    observed_count: list[int]
    scaled: list[bool]


STEM_ORIENTATION_FEATURE_SPECS: tuple[TemporalFeatureSpec, ...] = (
    TemporalFeatureSpec(
        "residual_profile_normalized",
        "geometry.residual_profile_normalized",
        kind="vector",
        length=32,
        validity_path="geometry.residual_valid_mask",
        required=True,
    ),
    TemporalFeatureSpec(
        "centerline_x_normalized",
        "geometry.centerline_image_xy",
        kind="point_x",
        length=32,
        divisor_path="geometry.image_width",
        validity_path="geometry.residual_valid_mask",
        required=True,
    ),
    TemporalFeatureSpec(
        "centerline_y_normalized",
        "geometry.centerline_image_xy",
        kind="point_y",
        length=32,
        divisor_path="geometry.image_height",
        validity_path="geometry.residual_valid_mask",
        required=True,
    ),
    TemporalFeatureSpec(
        "width_profile_normalized",
        "geometry.width_profile_px",
        kind="vector",
        length=16,
        divisor_path="geometry.median_width_px",
        validity_path="geometry.width_valid_mask",
        required=True,
    ),
    TemporalFeatureSpec("signed_curvature", "geometry.signed_curvature"),
    TemporalFeatureSpec("signed_bow_normalized", "geometry.signed_bow_normalized"),
    TemporalFeatureSpec("rms_residual_normalized", "geometry.rms_residual_normalized"),
    TemporalFeatureSpec(
        "max_abs_residual_normalized",
        "geometry.max_abs_residual_px",
        divisor_path="geometry.median_width_px",
    ),
    TemporalFeatureSpec(
        "peak_to_peak_normalized",
        "geometry.peak_to_peak_px",
        divisor_path="geometry.median_width_px",
    ),
    TemporalFeatureSpec("visible_fraction", "geometry.visible_fraction", required=True),
    TemporalFeatureSpec(
        "valid_cross_section_fraction",
        "geometry.valid_cross_section_fraction",
        required=True,
    ),
    TemporalFeatureSpec("geometry_quality", "geometry.quality_score", required=True),
    TemporalFeatureSpec("segmentation_score", "geometry.segmentation_score"),
    TemporalFeatureSpec("selected_component_fraction", "geometry.selected_component_fraction"),
    TemporalFeatureSpec("geometry_valid", "geometry.valid", scale=False, required=True),
    TemporalFeatureSpec("tracker_selection_confidence", "tracking.selection_confidence"),
    TemporalFeatureSpec("tracker_visible_fraction", "tracking.visible_length_fraction"),
    TemporalFeatureSpec(
        "tracker_centroid",
        "tracking.centroid_normalized",
        kind="vector",
        length=2,
    ),
    TemporalFeatureSpec("tracker_bbox", "tracking.bbox_normalized", kind="vector", length=4),
    TemporalFeatureSpec("tracker_length", "tracking.projected_length_normalized"),
    TemporalFeatureSpec("tracker_width", "tracking.projected_width_normalized"),
    TemporalFeatureSpec("tracker_age_frames", "tracking.track_age_frames"),
    TemporalFeatureSpec("tracker_lost_frames", "tracking.lost_frames"),
    TemporalFeatureSpec("motion_delta_x", "tracking.movement.delta_x_normalized"),
    TemporalFeatureSpec("motion_delta_y", "tracking.movement.delta_y_normalized"),
    TemporalFeatureSpec(
        "motion_velocity_x",
        "tracking.movement.velocity_x_normalized_per_frame",
    ),
    TemporalFeatureSpec(
        "motion_velocity_y",
        "tracking.movement.velocity_y_normalized_per_frame",
    ),
    TemporalFeatureSpec("motion_speed", "tracking.movement.speed_normalized_per_frame"),
    TemporalFeatureSpec(
        "motion_axis_change_deg",
        "tracking.movement.projected_axis_change_degrees",
    ),
    TemporalFeatureSpec("rotation_delta_deg", "rotation.delta_rotation_deg"),
    TemporalFeatureSpec(
        "rotation_cumulative_deg",
        "rotation.cumulative_rotation_deg_unwrapped",
    ),
    TemporalFeatureSpec("rotation_confidence", "rotation.rotation_confidence", required=True),
    TemporalFeatureSpec("rotation_detected_features", "rotation.detected_feature_count"),
    TemporalFeatureSpec("rotation_matched_features", "rotation.matched_feature_count"),
    TemporalFeatureSpec("rotation_inlier_features", "rotation.inlier_feature_count"),
    TemporalFeatureSpec("rotation_inlier_ratio", "rotation.inlier_ratio"),
    TemporalFeatureSpec("rotation_translation_x", "rotation.translation_component.x_px"),
    TemporalFeatureSpec("rotation_translation_y", "rotation.translation_component.y_px"),
    TemporalFeatureSpec(
        "rotation_translation_magnitude",
        "rotation.translation_component.magnitude_px",
    ),
    TemporalFeatureSpec("rotation_valid", "rotation.valid", scale=False, required=True),
    TemporalFeatureSpec(
        "rotation_calibration_valid",
        "rotation.calibration_valid",
        scale=False,
        required=True,
    ),
    TemporalFeatureSpec(
        "rotation_trace_discontinuity",
        "rotation.trace_discontinuity",
        scale=False,
        required=True,
    ),
    TemporalFeatureSpec("elapsed_seconds", "$elapsed_seconds"),
    TemporalFeatureSpec(
        "normalized_sequence_progress",
        "$normalized_sequence_progress",
        scale=False,
    ),
)


def stage_temporal_sequence_dataset(
    *,
    training_job_id: str,
    snapshot_id: str,
    snapshot_root: Path,
    stage_root: Path,
    recipe: Mapping[str, Any],
) -> TrainerDatasetArtifact:
    """Project an immutable temporal snapshot into deterministic, scaled sequence tensors."""

    canonical_path = snapshot_root / "annotations" / "canonical_dataset.json"
    validation_path = snapshot_root / "manifests" / "validation_report.json"
    split_path = snapshot_root / "manifests" / "split_metadata.json"
    snapshot_manifest_path = snapshot_root / "snapshot_manifest.json"
    for path in (canonical_path, validation_path, split_path, snapshot_manifest_path):
        if not path.is_file():
            raise TemporalDatasetPreparationError(
                f"Temporal snapshot {snapshot_root} is missing {path.relative_to(snapshot_root)}."
            )

    dataset = CanonicalSequenceDataset.model_validate(read_json(canonical_path))
    validation = _mapping(read_json(validation_path))
    split_metadata = _mapping(read_json(split_path))
    snapshot_manifest = _mapping(read_json(snapshot_manifest_path))
    if dataset.task_type != TaskType.TEMPORAL_SEQUENCE.value:
        raise TemporalDatasetPreparationError("Snapshot is not a temporal_sequence dataset.")
    if str(validation.get("status") or "").lower() != "passed":
        raise TemporalDatasetPreparationError(
            "The temporal snapshot must pass validation before real training."
        )
    validation_checks = _mapping(validation.get("checks"))
    required_validation_checks = (
        "group_split_integrity",
        "no_post_decision_frames",
        "no_duplicate_asset_hashes_across_splits",
        "no_time_overlap_across_splits",
        "checksums_valid",
        "fit_statistics_train_only",
    )
    missing_checks = [
        name for name in required_validation_checks if validation_checks.get(name) is not True
    ]
    if missing_checks:
        raise TemporalDatasetPreparationError(
            "The temporal snapshot does not prove required leakage/integrity checks: "
            f"{', '.join(missing_checks)}."
        )
    constraints = _mapping(snapshot_manifest.get("training_constraints"))
    required_constraints = {
        "group_split_key": "group_id",
        "primary_input_ends_at_decision_frame": True,
        "fit_scalers_and_thresholds_on": "train_only",
        "gold_and_silver_metrics_separate": True,
    }
    for key, expected in required_constraints.items():
        if constraints.get(key) != expected:
            raise TemporalDatasetPreparationError(
                f"Temporal snapshot training constraint {key!r} is missing or incompatible."
            )

    temporal_config = _mapping(recipe.get("temporal"))
    feature_specs = resolve_temporal_feature_specs(temporal_config)
    raw_feature_order = _expanded_feature_order(feature_specs)
    if not raw_feature_order:
        raise TemporalDatasetPreparationError("Temporal feature order must not be empty.")
    exclude_unclear = {
        str(value)
        for value in temporal_config.get(
            "exclude_unclear_labels",
            ["bow_relevance", "target_visible"],
        )
    }
    eligible_lengths = [
        len(item.frames)
        for item in dataset.items
        if _training_exclusion_reason(item, exclude_unclear=exclude_unclear) is None
    ]
    if not eligible_lengths:
        raise TemporalDatasetPreparationError("No usable temporal samples remain after filtering.")
    maximum_steps = _bounded_int(
        temporal_config,
        "maximum_window_steps",
        max(eligible_lengths),
        2,
        100_000,
    )
    minimum_steps = _bounded_int(
        temporal_config,
        "minimum_window_steps",
        min(eligible_lengths),
        2,
        maximum_steps,
    )

    raw_samples: list[_RawTemporalSample] = []
    exclusion_counts: dict[str, int] = {}
    source_feature_schemas: set[str] = set()
    rotation_schemas: set[str] = set()
    calibration_schemas: set[str] = set()
    target_builders: set[tuple[str, str]] = set()
    viewpoints: set[str] = set()
    positive_directions: set[str] = set()
    for item in dataset.items:
        reason = _training_exclusion_reason(item, exclude_unclear=exclude_unclear)
        if reason is not None:
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
            continue
        if len(item.frames) < minimum_steps:
            exclusion_counts["sequence_too_short"] = (
                exclusion_counts.get("sequence_too_short", 0) + 1
            )
            continue
        if len(item.frames) > maximum_steps:
            raise TemporalDatasetPreparationError(
                f"Sequence {item.sequence_id!r} has {len(item.frames)} frames, exceeding "
                f"maximum_window_steps={maximum_steps}; full primary sequences may not "
                "be truncated."
            )

        feature_rows = _read_verified_jsonl(
            snapshot_root,
            item.feature_manifest.uri,
            item.feature_manifest.sha256,
        )
        rotation_rows = _read_verified_jsonl(
            snapshot_root,
            item.rotation_trace.uri,
            item.rotation_trace.sha256,
        )
        _validate_alignment(item, feature_rows, rotation_rows)
        sample = _prepare_raw_sample(
            item=item,
            feature_rows=feature_rows,
            rotation_rows=rotation_rows,
            feature_specs=feature_specs,
            targets=item.derived_targets,
            snapshot_root=snapshot_root,
        )
        raw_samples.append(sample)
        raw_samples.extend(
            _prefix_augmented_samples(
                item=item,
                feature_rows=feature_rows,
                rotation_rows=rotation_rows,
                feature_specs=feature_specs,
                temporal_config=temporal_config,
                minimum_steps=minimum_steps,
                snapshot_root=snapshot_root,
            )
        )
        source_feature_schemas.add(item.schemas.feature_schema)
        rotation_schemas.add(item.schemas.rotation_schema)
        calibration_schemas.add(item.schemas.calibration_schema)
        target_builders.update(
            (target.builder_id, target.builder_version)
            for target in item.derived_targets
        )
        viewpoint = _optional_string(
            item.provenance.attributes.get("rotation_viewpoint_convention")
        )
        positive_direction = _optional_string(
            item.provenance.attributes.get("positive_rotation_direction")
        )
        if viewpoint:
            viewpoints.add(viewpoint)
        if positive_direction:
            positive_directions.add(positive_direction)

    if not raw_samples:
        raise TemporalDatasetPreparationError("No usable temporal samples remain after filtering.")
    if not any(sample.split == "train" for sample in raw_samples):
        raise TemporalDatasetPreparationError("Temporal training requires a non-empty train split.")
    if not any(sample.split == "val" for sample in raw_samples):
        raise TemporalDatasetPreparationError(
            "Temporal training requires a non-empty validation split for checkpoint "
            "and confidence calibration."
        )
    if bool(temporal_config.get("require_angle_targets", False)):
        for split in ("train", "val"):
            if not any(
                sample.split == split and bool(sample.targets["angle_target_valid"])
                for sample in raw_samples
            ):
                raise TemporalDatasetPreparationError(
                    f"This temporal-angle recipe requires at least one valid {split} angle target."
                )
    if len(viewpoints) != 1 or len(positive_directions) != 1:
        raise TemporalDatasetPreparationError(
            "A temporal angle model requires exactly one versioned viewpoint and "
            "positive direction."
        )

    _validate_group_splits(raw_samples)
    scaler = _fit_train_only_scaler(raw_samples, feature_specs)
    stage_root.mkdir(parents=True, exist_ok=True)
    prepared_root = stage_root / "prepared"
    manifests_root = stage_root / "manifests"
    prepared_root.mkdir(parents=True, exist_ok=True)
    manifests_root.mkdir(parents=True, exist_ok=True)

    feature_order = [
        *raw_feature_order,
        *(f"{name}__valid" for name in raw_feature_order),
        "__sequence_present",
    ]
    scaler_path = stage_root / "scaler.json"
    feature_contract_path = stage_root / "temporal-contract.json"
    angle_contract_path = stage_root / "angle-contract.json"
    head_class_order_path = stage_root / "head-class-order.json"
    split_manifest_path = stage_root / "split_manifest.json"
    validation_report_path = stage_root / "validation_report.json"
    conversion_summary_path = stage_root / "conversion_summary.json"
    manifest_path = stage_root / "dataset_manifest.json"

    scaler_payload = {
        "schema_version": "temporal-scaler.v1",
        "fit_split": "train",
        "feature_order": raw_feature_order,
        "mean": scaler.mean,
        "scale": scaler.scale,
        "observed_count": scaler.observed_count,
        "scaled": scaler.scaled,
        "missing_value_after_scaling": 0.0,
        "validity_masks_appended": True,
    }
    scaler_payload["sha256"] = _json_sha256(scaler_payload)
    write_json(scaler_path, scaler_payload)

    manifest_rows: dict[str, list[dict[str, Any]]] = {split: [] for split in _SPLITS}
    for sample in sorted(raw_samples, key=lambda value: (value.split, value.sample_id)):
        normalized = _normalize_sample(sample, scaler)
        file_key = hashlib.sha256(sample.sample_id.encode("utf-8")).hexdigest()[:24]
        prepared_path = prepared_root / f"{file_key}.json"
        prepared_payload = {
            "schema_version": "temporal-prepared-sample.v1",
            "sample_id": sample.sample_id,
            "sequence_id": sample.sequence_id,
            "group_id": sample.group_id,
            "split": sample.split,
            "label_quality_tier": sample.label_quality_tier,
            "sequence_length": len(sample.features),
            "features": normalized,
            "targets": sample.targets,
            "metadata": sample.metadata,
            "deterministic_baseline": sample.deterministic_baseline,
            "prefix_augmented": sample.prefix_augmented,
        }
        write_json(prepared_path, prepared_payload)
        manifest_rows[sample.split].append(
            {
                "sample_id": sample.sample_id,
                "sequence_id": sample.sequence_id,
                "group_id": sample.group_id,
                "split": sample.split,
                "sequence_length": len(sample.features),
                "label_quality_tier": sample.label_quality_tier,
                "angle_target_valid": bool(sample.targets["angle_target_valid"]),
                "prepared_path": str(prepared_path),
                "prepared_sha256": _sha256_file(prepared_path),
                "prefix_augmented": sample.prefix_augmented,
            }
        )
    split_manifest_paths: dict[str, str] = {}
    for split, rows in manifest_rows.items():
        path = manifests_root / f"{split}.jsonl"
        _write_jsonl(path, rows)
        split_manifest_paths[split] = str(path)

    feature_contract = {
        "schema_version": TEMPORAL_TRAINING_CONTRACT_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_content_checksum": snapshot_manifest.get("content_checksum"),
        "task_type": TaskType.TEMPORAL_SEQUENCE.value,
        "annotation_schema": dataset.annotation_schema,
        "source_feature_schemas": sorted(source_feature_schemas),
        "rotation_schemas": sorted(rotation_schemas),
        "calibration_schemas": sorted(calibration_schemas),
        "target_builders": [
            {"builder_id": builder_id, "builder_version": builder_version}
            for builder_id, builder_version in sorted(target_builders)
        ],
        "feature_set_id": str(
            temporal_config.get("feature_set_id") or STEM_ORIENTATION_FEATURE_SET_ID
        ),
        "feature_specs": [_feature_spec_payload(spec) for spec in feature_specs],
        "feature_order": feature_order,
        "raw_feature_count": len(raw_feature_order),
        "model_feature_count": len(feature_order),
        "minimum_window_steps": minimum_steps,
        "maximum_window_steps": maximum_steps,
        "padding": {"direction": "left", "value": 0.0},
        "sequence_presence_feature": "__sequence_present",
        "causal": True,
        "input_end": "decision_frame_or_earlier",
        "post_decision_frames_allowed": False,
        "group_split_key": "group_id",
        "scaler_uri": "scaler.json",
        "scaler_sha256": _sha256_file(scaler_path),
        "rotation_viewpoint_convention": next(iter(viewpoints)),
        "positive_rotation_direction": next(iter(positive_directions)),
        "forbidden_feature_tokens": list(_BANNED_FEATURE_TOKENS),
    }
    feature_contract["schema_digest"] = _json_sha256(feature_contract)
    write_json(feature_contract_path, feature_contract)
    write_json(
        angle_contract_path,
        {
            "schema_version": "temporal-angle.v1",
            "representation": ["sin_2e", "cos_2e"],
            "periodicity_deg": 180.0,
            "decode": "0.5 * atan2(sin_2e, cos_2e)",
            "wrap_interval_deg": [-90.0, 90.0],
            "wrap_upper_bound_exclusive": True,
            "deadband_deg": float(temporal_config.get("direction_deadband_deg") or 5.0),
            "rotation_viewpoint_convention": next(iter(viewpoints)),
            "positive_rotation_direction": next(iter(positive_directions)),
            "invalid_target_masks_angle_loss_only": True,
            "target_builders": [
                {"builder_id": builder_id, "builder_version": builder_version}
                for builder_id, builder_version in sorted(target_builders)
            ],
        },
    )
    write_json(
        head_class_order_path,
        {
            "schema_version": "temporal-head-order.v1",
            "bow_relevance": ["nearly_straight", "relevant"],
            "target_observed": ["no", "yes"],
            "final_orientation_quality": ["bad", "acceptable", "good"],
            "angle_vector": ["sin_2e", "cos_2e"],
        },
    )
    write_json(split_manifest_path, split_metadata)
    write_json(validation_report_path, validation)
    split_counts = {split: len(rows) for split, rows in manifest_rows.items()}
    conversion_summary = {
        "training_job_id": training_job_id,
        "snapshot_id": snapshot_id,
        "task_type": TaskType.TEMPORAL_SEQUENCE.value,
        "source_item_count": len(dataset.items),
        "prepared_item_count": len(raw_samples),
        "prefix_augmented_count": sum(sample.prefix_augmented for sample in raw_samples),
        "split_counts": split_counts,
        "angle_target_counts": {
            split: sum(
                bool(sample.targets["angle_target_valid"])
                for sample in raw_samples
                if sample.split == split
            )
            for split in _SPLITS
        },
        "label_quality_counts": {
            tier: sum(sample.label_quality_tier == tier for sample in raw_samples)
            for tier in ("gold", "silver", "weak")
        },
        "exclusion_counts": dict(sorted(exclusion_counts.items())),
        "feature_count": len(feature_order),
        "scaler_fit_split": "train",
        "session_leakage": False,
        "post_decision_frames": False,
    }
    write_json(conversion_summary_path, conversion_summary)
    manifest_payload = {
        "schema_version": "temporal-staged-dataset.v1",
        "training_job_id": training_job_id,
        "snapshot_id": snapshot_id,
        "task_type": TaskType.TEMPORAL_SEQUENCE.value,
        "stage_root": str(stage_root),
        "manifests": split_manifest_paths,
        "temporal_contract_path": str(feature_contract_path),
        "angle_contract_path": str(angle_contract_path),
        "scaler_path": str(scaler_path),
        "head_class_order_path": str(head_class_order_path),
        "validation_report_path": str(validation_report_path),
        "split_manifest_path": str(split_manifest_path),
        "conversion_summary_path": str(conversion_summary_path),
        "automatic_production_promotion": False,
    }
    write_json(manifest_path, manifest_payload)
    return TrainerDatasetArtifact(
        task_type=TaskType.TEMPORAL_SEQUENCE,
        stage_root=str(stage_root),
        manifest_path=str(manifest_path),
        conversion_summary_path=str(conversion_summary_path),
        validation_report_path=str(validation_report_path),
        split_manifest_path=str(split_manifest_path),
        class_map_path=str(head_class_order_path),
        metadata={
            **manifest_payload,
            "feature_count": len(feature_order),
            "maximum_window_steps": maximum_steps,
            "minimum_window_steps": minimum_steps,
            "split_counts": split_counts,
        },
    )


def resolve_temporal_feature_specs(config: Mapping[str, Any]) -> tuple[TemporalFeatureSpec, ...]:
    feature_set_id = str(config.get("feature_set_id") or STEM_ORIENTATION_FEATURE_SET_ID)
    raw_specs = config.get("feature_specs")
    if raw_specs is None:
        if feature_set_id != STEM_ORIENTATION_FEATURE_SET_ID:
            raise TemporalDatasetPreparationError(
                f"Unknown temporal feature set {feature_set_id!r}; provide versioned feature_specs."
            )
        specs = STEM_ORIENTATION_FEATURE_SPECS
    else:
        if not isinstance(raw_specs, list) or not raw_specs:
            raise TemporalDatasetPreparationError(
                "temporal.feature_specs must be a non-empty list."
            )
        parsed: list[TemporalFeatureSpec] = []
        for value in raw_specs:
            if not isinstance(value, Mapping):
                raise TemporalDatasetPreparationError(
                    "Each temporal feature spec must be a mapping."
                )
            parsed.append(
                TemporalFeatureSpec(
                    name=str(value.get("name") or ""),
                    path=str(value.get("path") or ""),
                    kind=str(value.get("kind") or "scalar"),  # type: ignore[arg-type]
                    length=int(value.get("length") or 1),
                    validity_path=_optional_string(value.get("validity_path")),
                    divisor_path=_optional_string(value.get("divisor_path")),
                    scale=bool(value.get("scale", True)),
                    required=bool(value.get("required", False)),
                )
            )
        specs = tuple(parsed)
    names: set[str] = set()
    for spec in specs:
        if not spec.name or not spec.path or spec.length < 1:
            raise TemporalDatasetPreparationError(
                "Temporal feature specs require name, path and length."
            )
        if spec.kind not in {"scalar", "vector", "point_x", "point_y"}:
            raise TemporalDatasetPreparationError(
                f"Temporal feature {spec.name!r} has unsupported kind {spec.kind!r}."
            )
        if spec.name in names:
            raise TemporalDatasetPreparationError(f"Duplicate temporal feature name {spec.name!r}.")
        names.add(spec.name)
        normalized = f"{spec.name}.{spec.path}".lower()
        if any(token in normalized for token in _BANNED_FEATURE_TOKENS):
            raise TemporalDatasetPreparationError(
                f"Temporal feature {spec.name!r} contains a forbidden action/SPS input token."
            )
    return specs


def load_staged_temporal_samples(
    artifact: TrainerDatasetArtifact,
    split: str,
) -> list[dict[str, Any]]:
    manifest = _mapping(read_json(Path(artifact.manifest_path)))
    manifests = _mapping(manifest.get("manifests"))
    path_value = manifests.get(split)
    if not isinstance(path_value, str):
        raise TemporalDatasetPreparationError(f"Staged temporal split {split!r} is missing.")
    rows = _read_jsonl(Path(path_value))
    samples: list[dict[str, Any]] = []
    for row in rows:
        prepared_path = Path(str(row.get("prepared_path") or ""))
        if not prepared_path.is_file():
            raise TemporalDatasetPreparationError(
                f"Prepared temporal sample is missing: {prepared_path}."
            )
        if _sha256_file(prepared_path) != str(row.get("prepared_sha256") or ""):
            raise TemporalDatasetPreparationError(
                f"Prepared temporal sample checksum changed: {prepared_path.name}."
            )
        samples.append(_mapping(read_json(prepared_path)))
    return samples


def wrap_to_half_turn(value: float) -> float:
    wrapped = (float(value) + 90.0) % 180.0 - 90.0
    return -90.0 if wrapped >= 90.0 - 1e-12 else wrapped


def _prepare_raw_sample(
    *,
    item: CanonicalSequenceItem,
    feature_rows: Sequence[Mapping[str, Any]],
    rotation_rows: Sequence[Mapping[str, Any]],
    feature_specs: Sequence[TemporalFeatureSpec],
    targets: Sequence[DerivedTarget],
    snapshot_root: Path,
    sample_id: str | None = None,
    prefix_augmented: bool = False,
) -> _RawTemporalSample:
    values: list[list[float]] = []
    valid_rows: list[list[bool]] = []
    first_timestamp = item.frames[0].timestamp
    denominator = max(len(item.frames) - 1, 1)
    for index, (frame, feature_row, rotation_row) in enumerate(
        zip(item.frames, feature_rows, rotation_rows, strict=True)
    ):
        root = {
            **dict(feature_row),
            "rotation": dict(rotation_row),
            "$elapsed_seconds": frame.timestamp - first_timestamp,
            "$normalized_sequence_progress": index / denominator,
        }
        row_values: list[float] = []
        row_valid: list[bool] = []
        for spec in feature_specs:
            extracted, extracted_valid = _extract_feature(root, spec)
            row_values.extend(extracted)
            row_valid.extend(extracted_valid)
        values.append(row_values)
        valid_rows.append(row_valid)

    target_map = _target_map(targets)
    sequence_labels = _sequence_label_values(item)
    bow_value = sequence_labels.get("bow_relevance")
    target_visible = sequence_labels.get("target_visible")
    quality_value = sequence_labels.get("final_orientation_quality")
    angle_valid = bool(target_map.get("angle_target_valid", False))
    if bow_value == "nearly_straight":
        angle_valid = False
    sin_2e = _finite_number(target_map.get("sin_2e")) if angle_valid else None
    cos_2e = _finite_number(target_map.get("cos_2e")) if angle_valid else None
    correction = _finite_number(target_map.get("signed_correction_deg")) if angle_valid else None
    if angle_valid and (sin_2e is None or cos_2e is None or correction is None):
        raise TemporalDatasetPreparationError(
            f"Sequence {item.sequence_id!r} marks angle_target_valid without builder angle targets."
        )
    quality_order = {"bad": 0, "acceptable": 1, "good": 2}
    prepared_targets = {
        "bow_relevance": 1.0 if bow_value == "relevant" else 0.0,
        "target_observed": 1.0 if target_visible == "yes" else 0.0,
        "angle_target_valid": angle_valid,
        "sin_2e": sin_2e or 0.0,
        "cos_2e": cos_2e if cos_2e is not None else 1.0,
        "signed_correction_deg": correction,
        "angle_label_confidence": float(
            _finite_number(target_map.get("angle_label_confidence")) or 0.0
        ),
        "final_quality_valid": quality_value in quality_order,
        "final_quality_ordinal": quality_order.get(str(quality_value), 0),
        "nearly_straight": bow_value == "nearly_straight",
    }
    metadata = _sample_metadata(item, feature_rows, rotation_rows)
    baseline = _deterministic_baseline(item, rotation_rows, snapshot_root=snapshot_root)
    return _RawTemporalSample(
        sample_id=sample_id or item.sequence_id,
        sequence_id=item.sequence_id,
        group_id=item.group_id,
        split=item.split,
        label_quality_tier=item.label_quality.tier or "weak",
        features=values,
        valid=valid_rows,
        targets=prepared_targets,
        metadata=metadata,
        deterministic_baseline=baseline,
        prefix_augmented=prefix_augmented,
    )


def _prefix_augmented_samples(
    *,
    item: CanonicalSequenceItem,
    feature_rows: Sequence[Mapping[str, Any]],
    rotation_rows: Sequence[Mapping[str, Any]],
    feature_specs: Sequence[TemporalFeatureSpec],
    temporal_config: Mapping[str, Any],
    minimum_steps: int,
    snapshot_root: Path,
) -> list[_RawTemporalSample]:
    config = _mapping(temporal_config.get("prefix_augmentation"))
    if not bool(config.get("enabled", False)) or item.split != "train":
        return []
    if "minimum_rotation_confidence" not in config:
        raise TemporalDatasetPreparationError(
            "Enabled prefix augmentation requires a versioned minimum_rotation_confidence."
        )
    target_interval = next(
        (
            value
            for value in item.human_labels.timeline_labels
            if value.name == "target_orientation" and value.kind == "timeline_interval"
        ),
        None,
    )
    if target_interval is None:
        return []
    end_position = next(
        (
            index
            for index, frame in enumerate(item.frames)
            if frame.frame_index == target_interval.end_frame_index
        ),
        None,
    )
    if end_position is None:
        return []
    stride = _bounded_int(config, "stride_steps", 4, 1, 10_000)
    maximum = _bounded_int(config, "maximum_prefixes_per_sequence", 4, 1, 100)
    first_end = max(end_position, minimum_steps - 1)
    candidate_positions = list(range(first_end, len(item.frames) - 1, stride))[-maximum:]
    if not candidate_positions:
        return []
    schema = get_annotation_schema(item.schemas.annotation_schema)
    trace_samples = _rotation_trace_samples(item, rotation_rows)
    output: list[_RawTemporalSample] = []
    for position in candidate_positions:
        frames = list(item.frames[: position + 1])
        payload = item.model_dump(mode="json")
        payload.update(
            {
                "frames": [frame.model_dump(mode="json") for frame in frames],
                "decision_frame_index": frames[-1].frame_index,
                "decision_timestamp": frames[-1].timestamp,
                "derived_targets": [],
            }
        )
        prefix_item = CanonicalSequenceItem.model_validate(payload)
        target_result = schema.build_targets_with_context(
            prefix_item,
            TargetBuildContext(
                resources={"rotation_trace": tuple(trace_samples[: position + 1])},
                numeric_options={
                    "minimum_rotation_confidence": float(
                        config["minimum_rotation_confidence"]
                    )
                },
            ),
        )
        if not target_result.angle_target_generated:
            continue
        output.append(
            _prepare_raw_sample(
                item=prefix_item,
                feature_rows=feature_rows[: position + 1],
                rotation_rows=rotation_rows[: position + 1],
                feature_specs=feature_specs,
                targets=target_result.targets,
                snapshot_root=snapshot_root,
                sample_id=f"{item.sequence_id}__prefix_{frames[-1].frame_index}",
                prefix_augmented=True,
            )
        )
    return output


def _training_exclusion_reason(
    item: CanonicalSequenceItem,
    *,
    exclude_unclear: set[str],
) -> str | None:
    if item.exclusion_reason:
        return item.exclusion_reason
    labels = _sequence_label_values(item)
    if labels.get("sequence_valid") != "usable":
        return "sequence_not_usable"
    if labels.get("bow_relevance") not in {"relevant", "nearly_straight"}:
        return "bow_relevance_unusable_or_unclear"
    if labels.get("target_visible") not in {"yes", "no", "unclear"}:
        return "target_visible_missing"
    for name in exclude_unclear:
        if labels.get(name) == "unclear":
            return f"{name}_unclear_excluded"
    return None


def _extract_feature(
    root: Mapping[str, Any], spec: TemporalFeatureSpec
) -> tuple[list[float], list[bool]]:
    raw = _path_value(root, spec.path)
    divisor = _finite_number(_path_value(root, spec.divisor_path)) if spec.divisor_path else 1.0
    divisor_valid = divisor is not None and abs(divisor) > 1e-12
    validity_value = _path_value(root, spec.validity_path) if spec.validity_path else None
    values: list[float] = []
    present: list[bool] = []
    if spec.kind == "scalar":
        number = float(raw) if isinstance(raw, bool) else _finite_number(raw)
        valid = number is not None and divisor_valid
        if spec.required and not valid:
            raise TemporalDatasetPreparationError(
                f"Required temporal feature {spec.name!r} is missing or non-finite."
            )
        values = [(number / float(divisor)) if valid else 0.0]
        present = [valid]
    else:
        source = list(raw) if isinstance(raw, list | tuple) else []
        mask = list(validity_value) if isinstance(validity_value, list | tuple) else []
        for index in range(spec.length):
            item = source[index] if index < len(source) else None
            if spec.kind in {"point_x", "point_y"}:
                coordinate = "x" if spec.kind == "point_x" else "y"
                item = item.get(coordinate) if isinstance(item, Mapping) else None
            number = _finite_number(item)
            mask_valid = bool(mask[index]) if index < len(mask) else True
            valid = number is not None and divisor_valid and mask_valid
            values.append((number / float(divisor)) if valid else 0.0)
            present.append(valid)
        if spec.required and len(source) != spec.length:
            raise TemporalDatasetPreparationError(
                f"Required temporal feature {spec.name!r} expected length {spec.length}, "
                f"received {len(source)}."
            )
    return values, present


def _fit_train_only_scaler(
    samples: Sequence[_RawTemporalSample],
    specs: Sequence[TemporalFeatureSpec],
) -> _Scaler:
    scale_flags = [flag for spec in specs for flag in [spec.scale] * spec.length]
    dimension = len(scale_flags)
    counts = [0] * dimension
    means = [0.0] * dimension
    squared = [0.0] * dimension
    for sample in samples:
        if sample.split != "train":
            continue
        for row, valid in zip(sample.features, sample.valid, strict=True):
            for index, (value, is_valid) in enumerate(zip(row, valid, strict=True)):
                if not is_valid:
                    continue
                counts[index] += 1
                delta = value - means[index]
                means[index] += delta / counts[index]
                squared[index] += delta * (value - means[index])
    scales: list[float] = []
    for index in range(dimension):
        if counts[index] == 0:
            means[index] = 0.0
            scales.append(1.0)
            continue
        variance = squared[index] / max(counts[index] - 1, 1)
        standard_deviation = math.sqrt(max(variance, 0.0))
        scales.append(
            standard_deviation
            if scale_flags[index] and standard_deviation > 1e-12
            else 1.0
        )
        if not scale_flags[index]:
            means[index] = 0.0
    return _Scaler(mean=means, scale=scales, observed_count=counts, scaled=scale_flags)


def _normalize_sample(sample: _RawTemporalSample, scaler: _Scaler) -> list[list[float]]:
    output: list[list[float]] = []
    for row, valid in zip(sample.features, sample.valid, strict=True):
        normalized = [
            ((value - scaler.mean[index]) / scaler.scale[index]) if valid[index] else 0.0
            for index, value in enumerate(row)
        ]
        output.append([*normalized, *(1.0 if value else 0.0 for value in valid), 1.0])
    return output


def _validate_alignment(
    item: CanonicalSequenceItem,
    feature_rows: Sequence[Mapping[str, Any]],
    rotation_rows: Sequence[Mapping[str, Any]],
) -> None:
    if len(feature_rows) != len(item.frames) or len(rotation_rows) != len(item.frames):
        raise TemporalDatasetPreparationError(
            f"Sequence {item.sequence_id!r} does not have one feature and rotation row per frame."
        )
    for frame, features, rotation in zip(item.frames, feature_rows, rotation_rows, strict=True):
        for label, row in (("feature", features), ("rotation", rotation)):
            if row.get("frame_index") != frame.frame_index:
                raise TemporalDatasetPreparationError(
                    f"Sequence {item.sequence_id!r} has a misaligned {label} frame index."
                )
            timestamp = _finite_number(
                row.get("video_timestamp_seconds", row.get("timestamp"))
            )
            if timestamp is None or abs(timestamp - frame.timestamp) > 1e-6:
                raise TemporalDatasetPreparationError(
                    f"Sequence {item.sequence_id!r} has a misaligned {label} timestamp."
                )
            if frame.frame_index > item.decision_frame_index:
                raise TemporalDatasetPreparationError(
                    f"Sequence {item.sequence_id!r} contains a post-decision model input."
                )


def _validate_group_splits(samples: Sequence[_RawTemporalSample]) -> None:
    split_by_group: dict[str, str] = {}
    for sample in samples:
        previous = split_by_group.setdefault(sample.group_id, sample.split)
        if previous != sample.split:
            raise TemporalDatasetPreparationError(
                f"Session/group {sample.group_id!r} crosses temporal dataset splits."
            )


def _sample_metadata(
    item: CanonicalSequenceItem,
    feature_rows: Sequence[Mapping[str, Any]],
    rotation_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    geometry_rows = [
        _mapping(row.get("geometry")) for row in feature_rows if isinstance(row, Mapping)
    ]
    visibility = [
        value
        for row in geometry_rows
        if (value := _finite_number(row.get("visible_fraction"))) is not None
    ]
    curvature = [
        abs(value)
        for row in geometry_rows
        if (value := _finite_number(row.get("signed_bow_normalized"))) is not None
    ]
    stem_size: list[float] = []
    for row in geometry_rows:
        area = _finite_number(row.get("selected_component_area_px"))
        width = _finite_number(row.get("image_width"))
        height = _finite_number(row.get("image_height"))
        if area is not None and width and height:
            stem_size.append(area / (width * height))
    trace_quality = [
        value
        for row in rotation_rows
        if (value := _finite_number(row.get("rotation_confidence"))) is not None
    ]
    return {
        "decision_frame_index": item.decision_frame_index,
        "decision_timestamp": item.decision_timestamp,
        "sequence_length": len(item.frames),
        "visibility": statistics.fmean(visibility) if visibility else None,
        "curvature_strength": max(curvature) if curvature else None,
        "rotation_trace_quality": min(trace_quality) if trace_quality else None,
        "stem_size": statistics.fmean(stem_size) if stem_size else None,
        "feature_schema": item.schemas.feature_schema,
        "rotation_schema": item.schemas.rotation_schema,
        "calibration_schema": item.schemas.calibration_schema,
        "annotation_schema": item.schemas.annotation_schema,
    }


def _deterministic_baseline(
    item: CanonicalSequenceItem,
    rotation_rows: Sequence[Mapping[str, Any]],
    *,
    snapshot_root: Path,
) -> dict[str, Any]:
    source_manifest_path = Path(item.feature_manifest.uri).parent / "source-sequence-manifest.json"
    manifest_path = (snapshot_root / source_manifest_path).resolve()
    try:
        manifest_path.relative_to(snapshot_root.resolve())
    except ValueError:
        return {"available": False, "reason": "deterministic_baseline_path_invalid"}
    baseline: Mapping[str, Any] = {}
    if manifest_path.is_file():
        baseline = _mapping(_mapping(read_json(manifest_path)).get("baseline_prediction"))
    if not baseline:
        return {"available": False, "reason": "deterministic_baseline_missing"}
    start = _nonnegative_int(baseline.get("frame_start_index"))
    end = _nonnegative_int(baseline.get("frame_end_index"))
    if start is None or end is None or end < start or end >= len(rotation_rows):
        return {"available": False, "reason": "deterministic_baseline_interval_invalid"}
    target_angles = [
        value
        for row in rotation_rows[start : end + 1]
        if (value := _finite_number(row.get("cumulative_rotation_deg_unwrapped"))) is not None
    ]
    decision_angle = _finite_number(
        rotation_rows[-1].get("cumulative_rotation_deg_unwrapped")
    )
    if not target_angles or decision_angle is None:
        return {"available": False, "reason": "deterministic_baseline_rotation_missing"}
    correction = wrap_to_half_turn(statistics.median(target_angles) - decision_angle)
    return {
        "available": True,
        "signed_correction_deg": correction,
        "confidence": float(_finite_number(baseline.get("confidence")) or 0.0),
        "estimate_method": str(baseline.get("estimate_method") or "projected_baseline"),
        "is_ground_truth": False,
    }


def _rotation_trace_samples(
    item: CanonicalSequenceItem,
    rows: Sequence[Mapping[str, Any]],
) -> list[RotationTraceSample]:
    output: list[RotationTraceSample] = []
    for frame, row in zip(item.frames, rows, strict=True):
        output.append(
            RotationTraceSample(
                frame_index=frame.frame_index,
                timestamp=frame.timestamp,
                cumulative_rotation_deg_unwrapped=float(
                    _finite_number(row.get("cumulative_rotation_deg_unwrapped")) or 0.0
                ),
                rotation_confidence=float(
                    _bounded_confidence(row.get("rotation_confidence")) or 0.0
                ),
                calibration_id=str(row.get("calibration_id") or "calibration-id-missing"),
                calibration_method=str(
                    row.get("calibration_method") or "calibration-method-missing"
                ),
                calibration_version=str(
                    row.get("calibration_version") or item.schemas.calibration_schema
                ),
                calibration_valid=bool(row.get("calibration_valid")),
                valid=bool(row.get("valid")),
                trace_discontinuity=bool(row.get("trace_discontinuity")),
                trace_generation=_nonnegative_int(row.get("trace_generation")),
                reason_codes=[str(value) for value in row.get("reason_codes") or []],
            )
        )
    return output


def _target_map(targets: Sequence[DerivedTarget]) -> dict[str, Any]:
    return {target.name: target.value for target in targets if target.valid}


def _sequence_label_values(item: CanonicalSequenceItem) -> dict[str, str]:
    return {
        annotation.name: annotation.value
        for annotation in item.human_labels.sequence_labels
        if isinstance(annotation, SequenceChoiceAnnotation)
    }


def _read_verified_jsonl(
    snapshot_root: Path,
    uri: str,
    expected_sha256: str,
) -> list[dict[str, Any]]:
    path = (snapshot_root / uri).resolve()
    try:
        path.relative_to(snapshot_root.resolve())
    except ValueError as exc:
        raise TemporalDatasetPreparationError(
            "Temporal artifact URI escapes the snapshot root."
        ) from exc
    if not path.is_file():
        raise TemporalDatasetPreparationError(f"Temporal artifact is missing: {uri}.")
    if _sha256_file(path) != _normalize_sha256(expected_sha256):
        raise TemporalDatasetPreparationError(f"Temporal artifact checksum changed: {uri}.")
    return _read_jsonl(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TemporalDatasetPreparationError(
                f"Temporal JSONL row {line_number} in {path.name} is not an object."
            )
        output.append(value)
    return output


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{json.dumps(dict(row), sort_keys=True)}\n" for row in rows),
        encoding="utf-8",
    )


def _path_value(root: Mapping[str, Any], path: str | None) -> Any:
    if path is None:
        return None
    if path.startswith("$"):
        return root.get(path)
    value: Any = root
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _expanded_feature_order(specs: Sequence[TemporalFeatureSpec]) -> list[str]:
    output: list[str] = []
    for spec in specs:
        if spec.length == 1:
            output.append(spec.name)
        else:
            output.extend(f"{spec.name}[{index}]" for index in range(spec.length))
    return output


def _feature_spec_payload(spec: TemporalFeatureSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "path": spec.path,
        "kind": spec.kind,
        "length": spec.length,
        "validity_path": spec.validity_path,
        "divisor_path": spec.divisor_path,
        "scale": spec.scale,
        "required": spec.required,
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _bounded_confidence(value: Any) -> float | None:
    number = _finite_number(value)
    if number is None or not 0.0 <= number <= 1.0:
        return None
    return number


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = int(value)
    return number if number >= 0 else None


def _optional_string(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _bounded_int(
    values: Mapping[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise TemporalDatasetPreparationError(f"{key} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise TemporalDatasetPreparationError(
            f"{key} must be between {minimum} and {maximum}."
        )
    return value


def _normalize_sha256(value: str) -> str:
    normalized = value.removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise TemporalDatasetPreparationError("Temporal artifact SHA256 is invalid.")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "STEM_ORIENTATION_FEATURE_SET_ID",
    "STEM_ORIENTATION_FEATURE_SPECS",
    "TEMPORAL_TRAINING_CONTRACT_VERSION",
    "TemporalDatasetPreparationError",
    "TemporalFeatureSpec",
    "load_staged_temporal_samples",
    "resolve_temporal_feature_specs",
    "stage_temporal_sequence_dataset",
    "wrap_to_half_turn",
]
