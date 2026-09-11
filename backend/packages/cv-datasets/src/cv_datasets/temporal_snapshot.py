from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cv_datasets.annotation_schemas import AnnotationSchemaError, get_annotation_schema
from cv_datasets.models import (
    CanonicalSequenceDataset,
    CanonicalSequenceItem,
    SequenceArtifactReference,
    SequenceLabelQuality,
    SnapshotArtifacts,
    SplitSummary,
    ValidationIssue,
)
from cv_datasets.target_builders.base import RotationTraceSample, TargetBuildContext
from cv_datasets.validation import validate_temporal_sequence_dataset

TEMPORAL_SNAPSHOT_SCHEMA = "temporal.snapshot.v1"
TEMPORAL_SNAPSHOT_MATERIALIZER_VERSION = "temporal-snapshot-materializer.v1"
DEFAULT_MINIMUM_ROTATION_CONFIDENCE = 0.6
_TEMPORAL_SAMPLE_MANIFEST_SCHEMA = "temporal.sample.manifest.v1"
_SHA256_PATTERN = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
_GOLD_REFERENCE_METHODS = {
    "known_angle_reference",
    "independent_rotation_measurement",
}


class TemporalSnapshotError(ValueError):
    pass


class _SequenceIssueError(TemporalSnapshotError):
    def __init__(self, code: str, message: str, *, sequence_id: str | None = None) -> None:
        self.code = code
        self.sequence_id = sequence_id
        super().__init__(message)


_SequenceIssue = _SequenceIssueError


def materialize_temporal_snapshot(
    *,
    snapshot_id: str,
    dataset_id: str,
    export_format: str,
    annotation_schema_id: str,
    project_label_schema_version: str | None,
    minimum_annotation_count: int,
    train_ratio: int,
    val_ratio: int,
    test_ratio: int,
    export_payload: list[dict[str, Any]],
    manifest_paths_by_task_id: Mapping[str | int, Path | str],
    target_root: Path,
    minimum_rotation_confidence: float = DEFAULT_MINIMUM_ROTATION_CONFIDENCE,
) -> SnapshotArtifacts:
    """Materialize Label Studio video tasks into one immutable temporal snapshot tree."""

    if export_format.upper() != "JSON":
        raise TemporalSnapshotError("Temporal snapshots currently require Label Studio JSON.")
    if train_ratio + val_ratio + test_ratio != 100:
        raise TemporalSnapshotError("Temporal split ratios must add up to 100.")
    if not 0.0 <= minimum_rotation_confidence <= 1.0:
        raise TemporalSnapshotError("minimum_rotation_confidence must be between 0 and 1.")
    schema = get_annotation_schema(annotation_schema_id)
    normalized_manifest_paths = {
        str(task_id): Path(path) for task_id, path in manifest_paths_by_task_id.items()
    }
    if target_root.exists() and any(target_root.iterdir()):
        raise TemporalSnapshotError("Temporal snapshot target must be empty and immutable.")
    target_root.mkdir(parents=True, exist_ok=True)
    annotations_root = target_root / "annotations"
    manifests_root = target_root / "manifests"
    sequences_root = target_root / "sequences"
    training_root = annotations_root / "training-ready" / "temporal_sequence"
    for path in (annotations_root, manifests_root, sequences_root, training_root):
        path.mkdir(parents=True, exist_ok=True)
    _write_json(annotations_root / "labelstudio_export.json", export_payload)

    issues: list[ValidationIssue] = []
    items: list[CanonicalSequenceItem] = []
    for task in sorted(export_payload, key=_task_sort_key):
        task_id = str(task.get("id") or task.get("task_id") or "task-id-missing")
        sequence_id = _task_sequence_id(task)
        sample_root: Path | None = None
        try:
            task_id = _task_id(task)
            manifest_path = normalized_manifest_paths.get(task_id)
            if manifest_path is None:
                raise _SequenceIssue(
                    "sequence_manifest_missing",
                    "No recorded temporal manifest is mapped to the Label Studio task.",
                    sequence_id=sequence_id,
                )
            manifest, template = _load_and_validate_manifest(
                manifest_path,
                annotation_schema_id=annotation_schema_id,
                task=task,
            )
            sequence_id = template.sequence_id
            annotation = _selected_submitted_annotation(task)
            results = annotation.get("result")
            if not isinstance(results, list):
                raise _SequenceIssue(
                    "label_studio_annotation_missing",
                    "The latest submitted Label Studio annotation has no result list.",
                    sequence_id=sequence_id,
                )
            human_labels = schema.parse_label_studio_results(
                [result for result in results if isinstance(result, dict)],
                frames=template.frames,
                decision_frame_index=template.decision_frame_index,
            ).model_copy(
                update={
                    "source_annotation_id": _optional_string(annotation.get("id")),
                    "source_revision": _annotation_revision(annotation),
                    "source_updated_at": _optional_string(
                        annotation.get("updated_at") or annotation.get("created_at")
                    ),
                    "source_annotator": _annotation_actor(annotation),
                }
            )
            label_tier, reference_metadata = _label_quality_tier(manifest)
            direction = manifest.get("direction_provenance")
            direction = dict(direction) if isinstance(direction, Mapping) else {}
            source_window = _source_time_window(manifest)
            provenance_attributes = {
                **template.provenance.attributes,
                "label_studio_task_id": task_id,
                "label_studio_annotation_id": human_labels.source_annotation_id,
                "label_studio_annotation_revision": human_labels.source_revision,
                "rotation_viewpoint_convention": _optional_string(
                    direction.get("rotation_viewpoint_convention")
                ),
                "positive_rotation_direction": _optional_string(
                    direction.get("positive_rotation_direction")
                ),
                "source_start_epoch_seconds": source_window[0],
                "source_end_epoch_seconds": source_window[1],
                **reference_metadata,
            }
            split = _assign_group_split(
                dataset_id=dataset_id,
                group_id=template.group_id,
                train_ratio=train_ratio,
                val_ratio=val_ratio,
            )
            item = template.model_copy(
                update={
                    "human_labels": human_labels,
                    "derived_targets": [],
                    "label_source": "label_studio",
                    "label_quality": SequenceLabelQuality(
                        status="verified" if label_tier == "gold" else "reviewed",
                        tier=label_tier,
                        score=None,
                        reason_codes=[
                            (
                                "independent_rotation_reference_verified"
                                if label_tier == "gold"
                                else "human_interval_with_visual_rotation_trace"
                            )
                        ],
                    ),
                    "split": split,
                    "provenance": template.provenance.model_copy(
                        update={"attributes": provenance_attributes}
                    ),
                }
            )
            rotation_trace = _load_rotation_trace(manifest_path.parent, item)
            _validate_feature_alignment(manifest_path.parent, item)
            target_result = schema.build_targets_with_context(
                item,
                TargetBuildContext(
                    resources={"rotation_trace": tuple(rotation_trace)},
                    numeric_options={
                        "minimum_rotation_confidence": minimum_rotation_confidence,
                    },
                ),
            )
            effective_label_tier = (
                item.label_quality.tier if target_result.angle_target_generated else "weak"
            )
            item = item.model_copy(
                update={
                    "derived_targets": target_result.targets,
                    "exclusion_reason": target_result.exclusion_reason,
                    "label_quality": item.label_quality.model_copy(
                        update={
                            "tier": effective_label_tier,
                            "reason_codes": [
                                *item.label_quality.reason_codes,
                                *target_result.reason_codes,
                            ]
                        }
                    ),
                }
            )
            sample_root = sequences_root / _safe_sequence_directory(item.sequence_id)
            item = _copy_sequence_artifacts(
                manifest_path=manifest_path,
                manifest=manifest,
                item=item,
                sample_root=sample_root,
                snapshot_root=target_root,
            )
            schema.validate_item(item)
            items.append(item)
        except (AnnotationSchemaError, ValidationError, _SequenceIssue, OSError, ValueError) as exc:
            if sample_root is not None:
                shutil.rmtree(sample_root, ignore_errors=True)
            issue_code = (
                exc.code if isinstance(exc, _SequenceIssue) else "temporal_sequence_invalid"
            )
            issues.append(
                ValidationIssue(
                    severity="error",
                    code=issue_code,
                    message=str(exc),
                    item_ref=sequence_id or task_id,
                    metadata={"label_studio_task_id": task_id},
                )
            )

    canonical_dataset = CanonicalSequenceDataset(
        dataset_id=dataset_id,
        annotation_schema=annotation_schema_id,
        items=sorted(items, key=lambda item: item.sequence_id),
    )
    split_summary = _split_summary(
        canonical_dataset,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        test_ratio=test_ratio,
    )
    validation = validate_temporal_sequence_dataset(
        dataset=canonical_dataset,
        split_summary=split_summary,
        issues=issues,
        minimum_annotation_count=minimum_annotation_count,
        dataset_label_schema_version=annotation_schema_id,
        project_label_schema_version=project_label_schema_version,
    )
    _write_json(
        annotations_root / "canonical_dataset.json",
        canonical_dataset.model_dump(mode="json"),
    )
    _write_json(manifests_root / "validation_report.json", validation.model_dump(mode="json"))
    _write_json(manifests_root / "split_metadata.json", split_summary.model_dump(mode="json"))
    _write_training_manifests(training_root, canonical_dataset)

    content_sha256 = _content_checksum(
        canonical_dataset,
        annotation_schema_id=annotation_schema_id,
        export_format=export_format,
    )
    quality_counts = {
        tier: sum(item.label_quality.tier == tier for item in canonical_dataset.items)
        for tier in ("gold", "silver", "weak")
    }
    angle_target_count = sum(
        any(
            target.name == "angle_target_valid" and target.value is True
            for target in item.derived_targets
        )
        for item in canonical_dataset.items
    )
    snapshot_manifest = {
        "schema_version": TEMPORAL_SNAPSHOT_SCHEMA,
        "materializer_version": TEMPORAL_SNAPSHOT_MATERIALIZER_VERSION,
        "snapshot_id": snapshot_id,
        "dataset_id": dataset_id,
        "task_type": "temporal_sequence",
        "annotation_schema_id": annotation_schema_id,
        "target_builder_id": schema.definition.target_builder_id,
        "target_builder_version": schema.definition.target_builder_version,
        "export_format": export_format,
        "source_task_count": len(export_payload),
        "item_count": len(canonical_dataset.items),
        "eligible_item_count": canonical_dataset.eligible_item_count,
        "angle_target_count": angle_target_count,
        "label_quality_counts": quality_counts,
        "content_checksum": content_sha256,
        "split_metadata": split_summary.model_dump(mode="json"),
        "validation_status": validation.status,
        "training_constraints": {
            "group_split_key": "group_id",
            "primary_input_ends_at_decision_frame": True,
            "fit_scalers_and_thresholds_on": "train_only",
            "gold_and_silver_metrics_separate": True,
        },
    }
    _write_json(target_root / "snapshot_manifest.json", snapshot_manifest)
    return SnapshotArtifacts(
        canonical_dataset=canonical_dataset,
        split_summary=split_summary,
        validation=validation,
        content_sha256=content_sha256,
        manifest_paths={
            "source_export": "annotations/labelstudio_export.json",
            "canonical_dataset": "annotations/canonical_dataset.json",
            "validation_report": "manifests/validation_report.json",
            "split_metadata": "manifests/split_metadata.json",
            "snapshot_manifest": "snapshot_manifest.json",
        },
    )


def _load_and_validate_manifest(
    manifest_path: Path,
    *,
    annotation_schema_id: str,
    task: Mapping[str, Any],
) -> tuple[dict[str, Any], CanonicalSequenceItem]:
    manifest_path = manifest_path.resolve()
    if not manifest_path.is_file():
        raise _SequenceIssue("sequence_manifest_missing", "Temporal sample manifest is missing.")
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != _TEMPORAL_SAMPLE_MANIFEST_SCHEMA:
        raise _SequenceIssue(
            "sequence_manifest_schema_invalid",
            "Temporal sample manifest schema is unsupported.",
        )
    if manifest.get("sample_status") != "recorded":
        raise _SequenceIssue(
            "sequence_not_released",
            "Only released temporal samples can enter an immutable snapshot.",
        )
    try:
        item = CanonicalSequenceItem.model_validate(manifest["canonical_sequence_item"])
    except (KeyError, ValidationError) as exc:
        raise _SequenceIssue(
            "canonical_sequence_item_invalid",
            "Temporal manifest contains no valid canonical sequence item.",
        ) from exc
    if item.schemas.annotation_schema != annotation_schema_id:
        raise _SequenceIssue(
            "annotation_schema_mismatch",
            "Temporal sample annotation schema differs from the snapshot schema.",
            sequence_id=item.sequence_id,
        )
    if manifest.get("sequence_id") != item.sequence_id:
        raise _SequenceIssue(
            "sequence_identity_mismatch",
            "Temporal manifest sequence identities differ.",
            sequence_id=item.sequence_id,
        )
    if manifest.get("decision_frame_index") != item.decision_frame_index:
        raise _SequenceIssue(
            "decision_frame_mismatch",
            "Decision frame differs between temporal manifest sections.",
            sequence_id=item.sequence_id,
        )
    mapping = manifest.get("frame_index_mapping")
    if not isinstance(mapping, list) or len(mapping) != len(item.frames):
        raise _SequenceIssue(
            "frame_mapping_invalid",
            "Frame mapping is missing or differs from the canonical sequence.",
            sequence_id=item.sequence_id,
        )
    for frame, raw in zip(item.frames, mapping, strict=True):
        if not isinstance(raw, Mapping):
            raise _SequenceIssue("frame_mapping_invalid", "Frame mapping row is malformed.")
        if raw.get("frame_index") != frame.frame_index or not _same_number(
            raw.get("video_timestamp_seconds"), frame.timestamp
        ):
            raise _SequenceIssue(
                "frame_mapping_invalid",
                "Frame mapping does not match canonical frame indices and timestamps.",
                sequence_id=item.sequence_id,
            )
    metadata = task.get("meta")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    task_sequence_id = metadata.get("sequence_id") or _task_sequence_id(task)
    if task_sequence_id and task_sequence_id != item.sequence_id:
        raise _SequenceIssue(
            "label_studio_task_identity_mismatch",
            "Label Studio task sequence_id differs from its recorded manifest.",
            sequence_id=item.sequence_id,
        )
    task_decision_frame = metadata.get("decision_frame_index")
    if task_decision_frame is not None and task_decision_frame != item.decision_frame_index:
        raise _SequenceIssue(
            "label_studio_decision_frame_mismatch",
            "Label Studio task decision frame differs from its recorded manifest.",
            sequence_id=item.sequence_id,
        )
    task_hash = metadata.get("primary_asset_sha256")
    if task_hash and _normalize_sha256(str(task_hash)) != _normalize_sha256(
        item.primary_video.sha256
    ):
        raise _SequenceIssue(
            "primary_video_checksum_mismatch",
            "Label Studio task primary asset hash differs from the temporal manifest.",
            sequence_id=item.sequence_id,
        )
    for metadata_key, reference, issue_label in (
        ("feature_manifest", item.feature_manifest, "feature_manifest"),
        ("rotation_trace", item.rotation_trace, "rotation_trace"),
    ):
        task_reference = metadata.get(metadata_key)
        if not isinstance(task_reference, Mapping):
            continue
        task_reference_sha = task_reference.get("sha256")
        if task_reference_sha and _normalize_sha256(
            str(task_reference_sha)
        ) != _normalize_sha256(reference.sha256):
            raise _SequenceIssue(
                f"label_studio_{issue_label}_checksum_mismatch",
                f"Label Studio task {issue_label} hash differs from the temporal manifest.",
                sequence_id=item.sequence_id,
            )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise _SequenceIssue("sequence_artifacts_invalid", "Temporal artifact map is missing.")
    for key, reference in (
        ("sequence", item.primary_video),
        ("features", item.feature_manifest),
        ("rotation_trace", item.rotation_trace),
    ):
        raw_reference = artifacts.get(key)
        if not isinstance(raw_reference, Mapping) or _normalize_sha256(
            str(raw_reference.get("sha256") or "")
        ) != _normalize_sha256(reference.sha256):
            raise _SequenceIssue(
                f"{key}_checksum_mismatch",
                f"Artifact {key!r} differs between temporal manifest sections.",
                sequence_id=item.sequence_id,
            )
    _verified_artifact_path(manifest_path.parent, item.primary_video, "primary_video")
    _verified_artifact_path(manifest_path.parent, item.feature_manifest, "feature_manifest")
    _verified_artifact_path(manifest_path.parent, item.rotation_trace, "rotation_trace")
    return manifest, item


def _load_rotation_trace(root: Path, item: CanonicalSequenceItem) -> list[RotationTraceSample]:
    path = _verified_artifact_path(root, item.rotation_trace, "rotation_trace")
    rows = _read_jsonl(path, label="rotation trace")
    _validate_trace_frame_alignment(rows, item, label="rotation trace")
    samples: list[RotationTraceSample] = []
    for row in rows:
        angle = _finite_float(row.get("cumulative_rotation_deg_unwrapped"))
        confidence = _bounded_confidence(row.get("rotation_confidence"))
        calibration_id = _optional_string(row.get("calibration_id"))
        calibration_method = _optional_string(row.get("calibration_method"))
        calibration_present = calibration_id is not None and calibration_method is not None
        row_valid = bool(row.get("valid")) and angle is not None and confidence is not None
        row_reason_codes = [
            str(value) for value in (row.get("reason_codes") or []) if str(value)
        ]
        if angle is None or confidence is None:
            row_reason_codes.append("rotation_trace_fields_invalid")
        samples.append(
            RotationTraceSample(
                frame_index=row.get("frame_index"),
                timestamp=row.get("video_timestamp_seconds", row.get("timestamp")),
                cumulative_rotation_deg_unwrapped=angle if angle is not None else 0.0,
                rotation_confidence=confidence if confidence is not None else 0.0,
                calibration_id=calibration_id or "calibration-id-missing",
                calibration_method=calibration_method or "calibration-method-missing",
                calibration_version=str(
                    row.get("calibration_version") or item.schemas.calibration_schema
                ),
                calibration_valid=bool(row.get("calibration_valid"))
                and calibration_present,
                valid=row_valid,
                trace_discontinuity=bool(row.get("trace_discontinuity")),
                trace_generation=_nonnegative_int(row.get("trace_generation")),
                reason_codes=row_reason_codes,
            )
        )
    return samples


def _validate_feature_alignment(root: Path, item: CanonicalSequenceItem) -> None:
    path = _verified_artifact_path(root, item.feature_manifest, "feature_manifest")
    rows = _read_jsonl(path, label="feature manifest")
    _validate_trace_frame_alignment(rows, item, label="feature manifest")


def _validate_trace_frame_alignment(
    rows: list[dict[str, Any]], item: CanonicalSequenceItem, *, label: str
) -> None:
    if len(rows) != len(item.frames):
        raise _SequenceIssue(
            f"{label.replace(' ', '_')}_alignment_invalid",
            f"The {label} does not contain exactly one row per primary frame.",
            sequence_id=item.sequence_id,
        )
    for frame, row in zip(item.frames, rows, strict=True):
        if row.get("frame_index") != frame.frame_index or not _same_number(
            row.get("video_timestamp_seconds", row.get("timestamp")), frame.timestamp
        ):
            raise _SequenceIssue(
                f"{label.replace(' ', '_')}_alignment_invalid",
                f"The {label} is not aligned to frame indices and timestamps.",
                sequence_id=item.sequence_id,
            )
        if frame.frame_index > item.decision_frame_index:
            raise _SequenceIssue(
                "post_decision_frame_present",
                "A temporal main-model artifact contains a post-decision frame.",
                sequence_id=item.sequence_id,
            )


def _copy_sequence_artifacts(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    item: CanonicalSequenceItem,
    sample_root: Path,
    snapshot_root: Path,
) -> CanonicalSequenceItem:
    if sample_root.exists():
        raise _SequenceIssue(
            "sequence_path_collision",
            "Two temporal sequences resolve to the same snapshot directory.",
            sequence_id=item.sequence_id,
        )
    sample_root.mkdir(parents=True)
    source_root = manifest_path.parent.resolve()
    primary = _copy_reference(
        source_root,
        item.primary_video,
        sample_root / "sequence.mp4",
        snapshot_root,
        "primary_video",
    )
    features = _copy_reference(
        source_root,
        item.feature_manifest,
        sample_root / "features.jsonl",
        snapshot_root,
        "feature_manifest",
    )
    rotation = _copy_reference(
        source_root,
        item.rotation_trace,
        sample_root / "rotation-trace.jsonl",
        snapshot_root,
        "rotation_trace",
    )
    diagnostics: list[SequenceArtifactReference] = []
    for index, reference in enumerate(item.diagnostic_assets):
        suffix = Path(reference.uri).suffix.lower() or ".bin"
        diagnostics.append(
            _copy_reference(
                source_root,
                reference,
                sample_root / f"diagnostic-{index:02d}{suffix}",
                snapshot_root,
                f"diagnostic_{index}",
            )
        )
    shutil.copy2(manifest_path, sample_root / "source-sequence-manifest.json")
    _write_json(
        sample_root / "source-provenance.json",
        {
            "source_manifest_sha256": _sha256_file(manifest_path),
            "source_manifest_schema": manifest.get("schema_version"),
            "source_recorder_version": manifest.get("recorder_version"),
        },
    )
    return item.model_copy(
        update={
            "primary_video": primary,
            "feature_manifest": features,
            "rotation_trace": rotation,
            "diagnostic_assets": diagnostics,
        }
    )


def _copy_reference(
    source_root: Path,
    reference: SequenceArtifactReference,
    destination: Path,
    snapshot_root: Path,
    label: str,
) -> SequenceArtifactReference:
    source = _verified_artifact_path(source_root, reference, label)
    shutil.copy2(source, destination)
    return SequenceArtifactReference(
        uri=destination.relative_to(snapshot_root).as_posix(),
        sha256=_sha256_file(destination),
        media_type=reference.media_type,
    )


def _verified_artifact_path(
    root: Path, reference: SequenceArtifactReference, label: str
) -> Path:
    raw_path = Path(reference.uri)
    path = raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise _SequenceIssue(
            f"{label}_path_invalid",
            f"Temporal {label} is outside its recorded sample directory.",
        ) from exc
    if not path.is_file():
        raise _SequenceIssue(f"{label}_missing", f"Temporal {label} is missing.")
    actual = _sha256_file(path)
    if actual != _normalize_sha256(reference.sha256):
        raise _SequenceIssue(
            f"{label}_checksum_mismatch",
            f"Temporal {label} checksum does not match its manifest.",
        )
    return path


def _selected_submitted_annotation(task: Mapping[str, Any]) -> dict[str, Any]:
    annotations = task.get("annotations")
    if not isinstance(annotations, list):
        raise _SequenceIssue(
            "label_studio_annotation_missing",
            "Label Studio task has no submitted annotation.",
        )
    candidates = [
        annotation
        for annotation in annotations
        if isinstance(annotation, dict) and _annotation_is_submitted(annotation)
    ]
    if not candidates:
        raise _SequenceIssue(
            "label_studio_annotation_missing",
            "Label Studio task has no submitted annotation.",
        )
    candidates.sort(key=_annotation_sort_key)
    return candidates[-1]


def _annotation_is_submitted(annotation: Mapping[str, Any]) -> bool:
    if annotation.get("was_cancelled") or annotation.get("skipped"):
        return False
    if not isinstance(annotation.get("result"), list):
        return False
    if annotation.get("completed_by") is not None or annotation.get("completed_at") is not None:
        return True
    if annotation.get("draft_created_at"):
        return False
    return any(annotation.get(key) is True for key in ("is_labeled", "ground_truth", "accepted"))


def _annotation_revision(annotation: Mapping[str, Any]) -> str:
    return str(
        annotation.get("updated_at")
        or annotation.get("created_at")
        or annotation.get("id")
        or "revision-unavailable"
    )


def _annotation_sort_key(annotation: Mapping[str, Any]) -> tuple[str, int, str]:
    timestamp = str(annotation.get("updated_at") or annotation.get("created_at") or "")
    raw_id = annotation.get("id")
    numeric_id = int(raw_id) if str(raw_id).isdigit() else -1
    return timestamp, numeric_id, str(raw_id or "")


def _annotation_actor(annotation: Mapping[str, Any]) -> str | None:
    actor = annotation.get("completed_by")
    if isinstance(actor, Mapping):
        for key in ("email", "username", "id"):
            value = actor.get(key)
            if value is not None:
                return str(value)
        return None
    return _optional_string(actor)


def _label_quality_tier(manifest: Mapping[str, Any]) -> tuple[str, dict[str, str | bool]]:
    reference = manifest.get("verified_rotation_reference")
    if not isinstance(reference, Mapping) or reference.get("verified") is not True:
        return "silver", {}
    method = _optional_string(reference.get("method"))
    version = _optional_string(reference.get("version"))
    reference_id = _optional_string(reference.get("reference_id"))
    if (
        method not in _GOLD_REFERENCE_METHODS
        or version is None
        or reference_id is None
    ):
        return "silver", {}
    return "gold", {
        "verified_rotation_reference": True,
        "verified_rotation_reference_id": reference_id,
        "verified_rotation_reference_method": method,
        "verified_rotation_reference_version": version,
    }


def _source_time_window(manifest: Mapping[str, Any]) -> tuple[float | None, float | None]:
    mapping = manifest.get("frame_index_mapping")
    if not isinstance(mapping, list) or not mapping:
        return None, None
    values = [
        _parse_epoch(row.get("source_timestamp"))
        for row in mapping
        if isinstance(row, Mapping)
    ]
    finite = [value for value in values if value is not None]
    return (min(finite), max(finite)) if finite else (None, None)


def _parse_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _assign_group_split(
    *, dataset_id: str, group_id: str, train_ratio: int, val_ratio: int
) -> str:
    digest = hashlib.sha256(f"{dataset_id}:{group_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % 100
    if bucket < train_ratio:
        return "train"
    if bucket < train_ratio + val_ratio:
        return "val"
    return "test"


def _split_summary(
    dataset: CanonicalSequenceDataset,
    *,
    train_ratio: int,
    val_ratio: int,
    test_ratio: int,
) -> SplitSummary:
    eligible = [item for item in dataset.items if item.exclusion_reason is None]
    return SplitSummary(
        strategy="deterministic_sha256_group_id",
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        test_ratio=test_ratio,
        train_count=sum(item.split == "train" for item in eligible),
        val_count=sum(item.split == "val" for item in eligible),
        test_count=sum(item.split == "test" for item in eligible),
    )


def _write_training_manifests(
    root: Path, dataset: CanonicalSequenceDataset
) -> None:
    for split in ("train", "val", "test"):
        items = [
            item
            for item in dataset.items
            if item.split == split and item.exclusion_reason is None
        ]
        _write_jsonl(
            root / f"{split}.jsonl",
            [item.model_dump(mode="json") for item in items],
        )
        _write_jsonl(
            root / f"{split}.human-labels.jsonl",
            [
                {
                    "sequence_id": item.sequence_id,
                    "group_id": item.group_id,
                    "human_labels": item.human_labels.model_dump(mode="json"),
                }
                for item in items
            ],
        )
        _write_jsonl(
            root / f"{split}.derived-targets.jsonl",
            [
                {
                    "sequence_id": item.sequence_id,
                    "group_id": item.group_id,
                    "label_quality": item.label_quality.model_dump(mode="json"),
                    "derived_targets": [
                        target.model_dump(mode="json") for target in item.derived_targets
                    ],
                }
                for item in items
            ],
        )


def _content_checksum(
    dataset: CanonicalSequenceDataset,
    *,
    annotation_schema_id: str,
    export_format: str,
) -> str:
    payload = {
        "schema_version": TEMPORAL_SNAPSHOT_SCHEMA,
        "materializer_version": TEMPORAL_SNAPSHOT_MATERIALIZER_VERSION,
        "annotation_schema_id": annotation_schema_id,
        "export_format": export_format.upper(),
        "dataset": dataset.model_dump(mode="json"),
    }
    digest = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return f"sha256:{digest}"


def _safe_sequence_directory(sequence_id: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", sequence_id).strip(".-")[:80]
    digest = hashlib.sha256(sequence_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix or 'sequence'}-{digest}"


def _task_id(task: Mapping[str, Any]) -> str:
    value = task.get("id") or task.get("task_id")
    if value is None:
        raise _SequenceIssue("label_studio_task_id_missing", "Label Studio task id is missing.")
    return str(value)


def _task_sort_key(task: Mapping[str, Any]) -> tuple[int, str]:
    value = task.get("id") or task.get("task_id")
    return (0, f"{int(value):020d}") if str(value).isdigit() else (1, str(value or ""))


def _task_sequence_id(task: Mapping[str, Any]) -> str | None:
    for container_name in ("meta", "data"):
        container = task.get(container_name)
        if isinstance(container, Mapping):
            value = container.get("sequence_id")
            if isinstance(value, str) and value:
                return value
    return None


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise _SequenceIssue("json_object_required", f"{path.name} must contain a JSON object.")
    return value


def _read_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _SequenceIssue(
                f"{label.replace(' ', '_')}_invalid",
                f"{label} line {line_number} is not valid JSON.",
            ) from exc
        if not isinstance(value, dict):
            raise _SequenceIssue(
                f"{label.replace(' ', '_')}_invalid",
                f"{label} line {line_number} must be a JSON object.",
            )
        rows.append(value)
    return rows


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_sha256(value: str) -> str:
    match = _SHA256_PATTERN.fullmatch(value.strip())
    if match is None:
        raise _SequenceIssue("checksum_invalid", "Artifact checksum is not a SHA-256 value.")
    return match.group(1).lower()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _same_number(value: object, expected: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and abs(float(value) - float(expected)) <= 1e-9
    )


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) else None


def _bounded_confidence(value: object) -> float | None:
    normalized = _finite_float(value)
    if normalized is None or not 0.0 <= normalized <= 1.0:
        return None
    return normalized


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
