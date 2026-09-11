from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv_datasets.models import (
    BoundingBox,
    CanonicalDataset,
    CanonicalItem,
    OrientedBoundingBox,
    SegmentationPolygon,
)
from cv_status import TaskType

from cv_training.paths import read_json, write_json
from cv_training.trainer_types import TrainerDatasetArtifact


class DatasetPreparationError(ValueError):
    pass


@dataclass(frozen=True)
class SnapshotTrainingSource:
    snapshot_root: Path
    canonical_dataset: CanonicalDataset
    validation_report: dict[str, Any]
    split_metadata: dict[str, Any]
    label_map: list[str]


def load_snapshot_training_source(snapshot_root: Path) -> SnapshotTrainingSource:
    canonical_path = snapshot_root / "annotations" / "canonical_dataset.json"
    validation_path = snapshot_root / "manifests" / "validation_report.json"
    split_path = snapshot_root / "manifests" / "split_metadata.json"
    label_map_path = snapshot_root / "label_map.json"
    if not canonical_path.exists():
        raise DatasetPreparationError(
            f"Snapshot {snapshot_root} is missing annotations/canonical_dataset.json."
        )
    if not validation_path.exists():
        raise DatasetPreparationError(
            f"Snapshot {snapshot_root} is missing manifests/validation_report.json."
        )
    if not split_path.exists():
        raise DatasetPreparationError(
            f"Snapshot {snapshot_root} is missing manifests/split_metadata.json."
        )
    if not label_map_path.exists():
        raise DatasetPreparationError(f"Snapshot {snapshot_root} is missing label_map.json.")

    canonical_payload = _normalize_canonical_payload(read_json(canonical_path))
    validation_report = read_json(validation_path)
    split_metadata = read_json(split_path)
    label_map_payload = read_json(label_map_path)
    if not isinstance(label_map_payload, list):
        raise DatasetPreparationError("Snapshot label_map.json must contain a JSON list.")

    source = SnapshotTrainingSource(
        snapshot_root=snapshot_root,
        canonical_dataset=CanonicalDataset.model_validate(canonical_payload),
        validation_report=validation_report if isinstance(validation_report, dict) else {},
        split_metadata=split_metadata if isinstance(split_metadata, dict) else {},
        label_map=[str(item) for item in label_map_payload],
    )
    _validate_source(source)
    return source


def _normalize_canonical_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)
    normalized_items: list[dict[str, Any]] = []
    for raw_item in payload.get("items") or []:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        labels = list(item.get("labels") or [])
        boxes = list(item.get("boxes") or [])
        oriented_boxes = list(item.get("oriented_boxes") or [])
        polygons = list(item.get("polygons") or [])

        eligibility_status = item.get("eligibility_status")
        submitted = bool(item.get("submitted", eligibility_status != "draft"))
        background = bool(
            item.get("background", not labels and not boxes and not oriented_boxes and not polygons)
        )
        if not eligibility_status:
            if not submitted:
                eligibility_status = "draft"
            elif background:
                eligibility_status = "eligible_background"
            else:
                eligibility_status = "eligible_labeled"

        item["labels"] = labels
        item["anomaly_label"] = item.get("anomaly_label")
        item["boxes"] = boxes
        item["oriented_boxes"] = oriented_boxes
        item["polygons"] = polygons
        item["submitted"] = submitted
        item["background"] = background
        item["eligibility_status"] = eligibility_status
        item["exclusion_reason"] = item.get("exclusion_reason")
        item["raw_annotation_count"] = int(item.get("raw_annotation_count") or 0)
        normalized_items.append(item)

    normalized["items"] = normalized_items
    normalized["eligible_item_count"] = sum(
        1 for item in normalized_items if str(item.get("eligibility_status") or "").startswith("eligible")
    )
    normalized["submitted_labeled_item_count"] = sum(
        1 for item in normalized_items if item.get("eligibility_status") == "eligible_labeled"
    )
    normalized["submitted_background_item_count"] = sum(
        1 for item in normalized_items if item.get("eligibility_status") == "eligible_background"
    )
    normalized["draft_item_count"] = sum(
        1 for item in normalized_items if item.get("eligibility_status") == "draft"
    )
    normalized["invalid_item_count"] = sum(
        1 for item in normalized_items if item.get("eligibility_status") == "invalid"
    )
    return normalized


def stage_framework_dataset(
    *,
    training_job_id: str,
    task_type: TaskType,
    snapshot_root: Path,
    stage_root: Path,
    selected_classes: list[str] | None = None,
) -> TrainerDatasetArtifact:
    source = load_snapshot_training_source(snapshot_root)
    if TaskType(source.canonical_dataset.task_type) != task_type:
        raise DatasetPreparationError(
            f"Snapshot task type {source.canonical_dataset.task_type} does not match {task_type.value}."
        )
    source = _project_training_classes(source, selected_classes)

    stage_root.mkdir(parents=True, exist_ok=True)
    class_map_path = stage_root / "class_map.json"
    split_manifest_path = stage_root / "split_manifest.json"
    validation_report_path = stage_root / "validation_report.json"
    conversion_summary_path = stage_root / "conversion_summary.json"
    manifest_path = stage_root / "dataset_manifest.json"

    write_json(class_map_path, source.label_map)
    write_json(split_manifest_path, source.split_metadata)
    write_json(validation_report_path, source.validation_report)

    if task_type == TaskType.ANOMALY:
        manifest_metadata = _stage_anomaly_dataset(
            source=source,
            stage_root=stage_root,
        )
    elif task_type == TaskType.DETECTION:
        manifest_metadata = _stage_detection_dataset(
            source=source,
            stage_root=stage_root,
        )
    elif task_type == TaskType.ORIENTED_DETECTION:
        manifest_metadata = _stage_oriented_detection_dataset(
            source=source,
            stage_root=stage_root,
        )
    elif task_type == TaskType.SEGMENTATION:
        manifest_metadata = _stage_segmentation_dataset(
            source=source,
            stage_root=stage_root,
        )
    else:
        manifest_metadata = _stage_classification_dataset(
            source=source,
            stage_root=stage_root,
        )

    conversion_summary = {
        "training_job_id": training_job_id,
        "task_type": task_type.value,
        "stage_root": str(stage_root),
        "snapshot_root": str(snapshot_root),
        "class_count": len(source.label_map),
        "selected_classes": list(source.label_map),
        "uses_all_snapshot_classes": not bool(selected_classes),
        "eligible_item_count": source.canonical_dataset.eligible_item_count,
        "submitted_labeled_item_count": source.canonical_dataset.submitted_labeled_item_count,
        "submitted_background_item_count": source.canonical_dataset.submitted_background_item_count,
        "draft_item_count": source.canonical_dataset.draft_item_count,
        "invalid_item_count": source.canonical_dataset.invalid_item_count,
        **manifest_metadata["summary"],
    }
    write_json(conversion_summary_path, conversion_summary)

    manifest_payload = {
        "training_job_id": training_job_id,
        "task_type": task_type.value,
        "stage_root": str(stage_root),
        "class_map_path": str(class_map_path),
        "validation_report_path": str(validation_report_path),
        "split_manifest_path": str(split_manifest_path),
        "conversion_summary_path": str(conversion_summary_path),
        **manifest_metadata["manifest"],
    }
    write_json(manifest_path, manifest_payload)

    return TrainerDatasetArtifact(
        task_type=task_type,
        stage_root=str(stage_root),
        manifest_path=str(manifest_path),
        conversion_summary_path=str(conversion_summary_path),
        validation_report_path=str(validation_report_path),
        split_manifest_path=str(split_manifest_path),
        class_map_path=str(class_map_path),
        metadata={
            **manifest_metadata["manifest"],
            "selected_classes": list(source.label_map),
            "uses_all_snapshot_classes": not bool(selected_classes),
        },
    )


def selected_classes_from_runtime_metadata(runtime_metadata: dict[str, Any]) -> list[str] | None:
    parameter_overrides = runtime_metadata.get("parameter_overrides")
    if not isinstance(parameter_overrides, dict):
        return None
    raw_classes = parameter_overrides.get("class_names")
    if not isinstance(raw_classes, list):
        return None
    normalized = [str(item).strip() for item in raw_classes if str(item).strip()]
    return normalized or None


def _project_training_classes(
    source: SnapshotTrainingSource,
    selected_classes: list[str] | None,
) -> SnapshotTrainingSource:
    if not selected_classes:
        return source

    requested = list(
        dict.fromkeys(str(item).strip() for item in selected_classes if str(item).strip())
    )
    available = set(source.label_map)
    unknown = [item for item in requested if item not in available]
    if unknown:
        raise DatasetPreparationError(
            "The model class selection is not present in the immutable snapshot: "
            + ", ".join(unknown)
            + "."
        )

    selected = set(requested)
    label_map = [item for item in source.label_map if item in selected]
    projected_items: list[CanonicalItem] = []
    class_distribution: dict[str, int] = {item: 0 for item in label_map}
    annotation_count = 0
    for item in source.canonical_dataset.items:
        labels = [label for label in item.labels if label in selected]
        boxes = [box for box in item.boxes if box.class_name in selected]
        oriented_boxes = [
            box for box in item.oriented_boxes if box.class_name in selected
        ]
        polygons = [polygon for polygon in item.polygons if polygon.class_name in selected]
        anomaly_label = (
            item.anomaly_label
            if item.anomaly_label is None or item.anomaly_label in selected
            else None
        )
        has_foreground = bool(labels or boxes or oriented_boxes or polygons or anomaly_label)
        eligibility_status = item.eligibility_status
        if eligibility_status.startswith("eligible"):
            eligibility_status = (
                "eligible_labeled" if has_foreground else "eligible_background"
            )
        projected_items.append(
            item.model_copy(
                update={
                    "labels": labels,
                    "boxes": boxes,
                    "oriented_boxes": oriented_boxes,
                    "polygons": polygons,
                    "anomaly_label": anomaly_label,
                    "background": not has_foreground,
                    "eligibility_status": eligibility_status,
                }
            )
        )

        for label in labels:
            class_distribution[label] = class_distribution.get(label, 0) + 1
        for region in [*boxes, *oriented_boxes, *polygons]:
            class_distribution[region.class_name] = (
                class_distribution.get(region.class_name, 0) + 1
            )
        if anomaly_label:
            class_distribution[anomaly_label] = (
                class_distribution.get(anomaly_label, 0) + 1
            )
        annotation_count += len(boxes) + len(oriented_boxes) + len(polygons)
        if not (boxes or oriented_boxes or polygons):
            annotation_count += len(labels) or int(anomaly_label is not None)

    if not any(class_distribution.values()):
        raise DatasetPreparationError(
            "None of the model's selected classes has an eligible annotation in this snapshot."
        )

    projected_dataset = source.canonical_dataset.model_copy(
        update={
            "annotation_count": annotation_count,
            "eligible_item_count": sum(
                item.eligibility_status.startswith("eligible")
                for item in projected_items
            ),
            "submitted_labeled_item_count": sum(
                item.eligibility_status == "eligible_labeled"
                for item in projected_items
            ),
            "submitted_background_item_count": sum(
                item.eligibility_status == "eligible_background"
                for item in projected_items
            ),
            "items": projected_items,
            "class_distribution": class_distribution,
            "anomaly_distribution": (
                class_distribution
                if source.canonical_dataset.task_type == TaskType.ANOMALY.value
                else {}
            ),
            "label_map": label_map,
        }
    )
    return SnapshotTrainingSource(
        snapshot_root=source.snapshot_root,
        canonical_dataset=projected_dataset,
        validation_report={
            **source.validation_report,
            "training_projection": {
                "selected_classes": label_map,
                "excluded_classes": [
                    item for item in source.label_map if item not in selected
                ],
                "source_label_map": list(source.label_map),
            },
        },
        split_metadata=source.split_metadata,
        label_map=label_map,
    )


def _validate_source(source: SnapshotTrainingSource) -> None:
    validation_status = str(source.validation_report.get("status") or "").lower()
    if validation_status == "failed":
        raise DatasetPreparationError(
            "The dataset snapshot failed validation and cannot be staged for real training."
        )
    eligible_items = [
        item for item in source.canonical_dataset.items if item.eligibility_status.startswith("eligible")
    ]
    if not eligible_items:
        raise DatasetPreparationError("No eligible items are available for training.")
    if not source.label_map:
        raise DatasetPreparationError("The snapshot does not define a usable class map.")
    if TaskType(source.canonical_dataset.task_type) == TaskType.ANOMALY:
        _validate_anomaly_source(source)
    for item in eligible_items:
        source_path = source.snapshot_root / "images" / item.relative_path
        if not source_path.exists():
            raise DatasetPreparationError(
                f"Snapshot image {item.relative_path} is missing from immutable storage."
            )


def _validate_anomaly_source(source: SnapshotTrainingSource) -> None:
    eligible_items = [
        item for item in source.canonical_dataset.items if item.eligibility_status == "eligible_labeled"
    ]
    normal_items = [
        item for item in eligible_items if (item.anomaly_label or (item.labels[0] if item.labels else None)) == "normal"
    ]
    anomalous_items = [
        item
        for item in eligible_items
        if (item.anomaly_label or (item.labels[0] if item.labels else None)) == "anomalous"
    ]
    train_normal = [item for item in normal_items if item.split == "train"]
    eval_normal = [item for item in normal_items if item.split in {"val", "test"}]
    eval_anomalous = [item for item in anomalous_items if item.split in {"val", "test"}]
    if not normal_items:
        raise DatasetPreparationError("Anomaly training requires at least one good/normal image.")
    if not anomalous_items:
        raise DatasetPreparationError("Anomaly training requires at least one bad/anomalous image.")
    if not train_normal:
        raise DatasetPreparationError("PatchCore anomaly training requires normal images in the train split.")
    if not eval_normal or not eval_anomalous:
        raise DatasetPreparationError(
            "Anomaly evaluation requires both normal and anomalous images in val or test splits."
        )


def _stage_anomaly_dataset(
    *,
    source: SnapshotTrainingSource,
    stage_root: Path,
) -> dict[str, Any]:
    output_root = stage_root / "anomalib-folder"
    split_counts: dict[str, dict[str, int]] = {
        "train": {"normal": 0, "anomalous": 0},
        "val": {"normal": 0, "anomalous": 0},
        "test": {"normal": 0, "anomalous": 0},
    }
    manifest_rows: dict[str, list[dict[str, Any]]] = {split: [] for split in ("train", "val", "test")}

    for item in source.canonical_dataset.items:
        if item.eligibility_status != "eligible_labeled":
            continue
        label = item.anomaly_label or (item.labels[0] if item.labels else None)
        if label not in {"normal", "anomalous"}:
            continue
        source_path = source.snapshot_root / "images" / item.relative_path
        if item.split == "train" and label == "normal":
            target_path = output_root / "normal" / item.relative_path
        elif item.split in {"val", "test"} and label == "normal":
            target_path = output_root / "normal_test" / item.relative_path
        else:
            target_path = output_root / "abnormal" / item.relative_path
        _link_or_copy(source_path, target_path)
        split_counts[item.split][label] += 1
        manifest_rows[item.split].append(
            {
                "image": str(target_path.relative_to(output_root)).replace("\\", "/"),
                "label": label,
                "task_id": item.task_id,
                "split": item.split,
                "mask": None,
            }
        )

    annotations_root = stage_root / "annotations"
    for split, rows in manifest_rows.items():
        manifest_path = annotations_root / f"anomaly_{split}.jsonl"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows),
            encoding="utf-8",
        )

    return {
        "summary": {"splits": split_counts, "masks_supported": False},
        "manifest": {
            "format": "anomalib_folder_image_level",
            "anomalib_root": str(output_root),
            "normal_dir": "normal",
            "abnormal_dir": "abnormal",
            "normal_test_dir": "normal_test",
            "mask_dir": None,
            "masks_supported": False,
            "normal_label": "normal",
            "abnormal_label": "anomalous",
            "manifests": {
                split: str(stage_root / "annotations" / f"anomaly_{split}.jsonl")
                for split in ("train", "val", "test")
            },
        },
    }


def _stage_detection_dataset(
    *,
    source: SnapshotTrainingSource,
    stage_root: Path,
) -> dict[str, Any]:
    categories = [
        {"id": index + 1, "name": label, "supercategory": label}
        for index, label in enumerate(source.label_map)
    ]
    category_index = {item["name"]: item["id"] for item in categories}
    split_counts: dict[str, dict[str, int]] = {}

    for split in ("train", "val", "test"):
        split_items = [
            item
            for item in source.canonical_dataset.items
            if item.split == split and item.eligibility_status.startswith("eligible")
        ]
        split_root = stage_root / split / "images"
        split_root.mkdir(parents=True, exist_ok=True)
        images: list[dict[str, Any]] = []
        annotations: list[dict[str, Any]] = []
        image_count = 0
        annotation_count = 0
        background_count = 0
        next_annotation_id = 1

        for image_id, item in enumerate(split_items, start=1):
            source_path = source.snapshot_root / "images" / item.relative_path
            staged_path = split_root / item.relative_path
            _link_or_copy(source_path, staged_path)
            width, height = _image_size(item=item, source_path=source_path)
            images.append(
                {
                    "id": image_id,
                    "file_name": item.relative_path.replace("\\", "/"),
                    "width": width,
                    "height": height,
                }
            )
            image_count += 1
            if item.background or not item.boxes:
                background_count += 1
                continue
            for box in item.boxes:
                x, y, box_width, box_height = _absolute_box(box=box, width=width, height=height)
                annotations.append(
                    {
                        "id": next_annotation_id,
                        "image_id": image_id,
                        "category_id": category_index[box.class_name],
                        "bbox": [x, y, box_width, box_height],
                        "area": round(box_width * box_height, 4),
                        "iscrowd": 0,
                    }
                )
                next_annotation_id += 1
                annotation_count += 1

        annotation_path = stage_root / "annotations" / f"instances_{split}.json"
        write_json(
            annotation_path,
            {
                "info": {"description": "CV platform staged detection dataset"},
                "licenses": [],
                "images": images,
                "annotations": annotations,
                "categories": categories,
            },
        )
        split_counts[split] = {
            "images": image_count,
            "annotations": annotation_count,
            "background_images": background_count,
        }

    return {
        "summary": {"splits": split_counts},
        "manifest": {
            "format": "coco_detection",
            "annotations": {
                split: str(stage_root / "annotations" / f"instances_{split}.json")
                for split in ("train", "val", "test")
            },
            "images_root": {
                split: str(stage_root / split / "images")
                for split in ("train", "val", "test")
            },
        },
    }


def _stage_oriented_detection_dataset(
    *,
    source: SnapshotTrainingSource,
    stage_root: Path,
) -> dict[str, Any]:
    label_index = {label: index for index, label in enumerate(source.label_map)}
    split_counts: dict[str, dict[str, int]] = {}

    for split in ("train", "val", "test"):
        split_items = [
            item
            for item in source.canonical_dataset.items
            if item.split == split and item.eligibility_status.startswith("eligible")
        ]
        split_root = stage_root / split / "images"
        labels_root = stage_root / split / "labels"
        split_root.mkdir(parents=True, exist_ok=True)
        labels_root.mkdir(parents=True, exist_ok=True)
        image_count = 0
        label_count = 0
        background_count = 0
        manifest_rows: list[dict[str, Any]] = []

        for item in split_items:
            source_path = source.snapshot_root / "images" / item.relative_path
            staged_path = split_root / item.relative_path
            _link_or_copy(source_path, staged_path)
            label_path = (labels_root / item.relative_path).with_suffix(".txt")
            label_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                _oriented_box_to_yolo_line(box=box, label_index=label_index)
                for box in item.oriented_boxes
            ]
            clean_lines = [line for line in lines if line]
            label_path.write_text("\n".join(clean_lines), encoding="utf-8")
            if item.background or not clean_lines:
                background_count += 1
            image_count += 1
            label_count += len(clean_lines)
            manifest_rows.append(
                {
                    "image": str(staged_path.relative_to(stage_root)).replace("\\", "/"),
                    "label": str(label_path.relative_to(stage_root)).replace("\\", "/"),
                    "task_id": item.task_id,
                    "background": item.background or not clean_lines,
                }
            )

        manifest_path = stage_root / "annotations" / f"oriented_instances_{split}.jsonl"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in manifest_rows),
            encoding="utf-8",
        )
        split_counts[split] = {
            "images": image_count,
            "labels": label_count,
            "background_images": background_count,
        }

    return {
        "summary": {"splits": split_counts},
        "manifest": {
            "format": "ultralytics_yolo_obb_labels",
            "annotations": {
                split: str(stage_root / "annotations" / f"oriented_instances_{split}.jsonl")
                for split in ("train", "val", "test")
            },
            "images_root": {
                split: str(stage_root / split / "images")
                for split in ("train", "val", "test")
            },
            "labels_root": {
                split: str(stage_root / split / "labels")
                for split in ("train", "val", "test")
            },
        },
    }


def _stage_segmentation_dataset(
    *,
    source: SnapshotTrainingSource,
    stage_root: Path,
) -> dict[str, Any]:
    categories = [
        {"id": index + 1, "name": label, "supercategory": label}
        for index, label in enumerate(source.label_map)
    ]
    category_index = {item["name"]: item["id"] for item in categories}
    split_counts: dict[str, dict[str, int]] = {}

    for split in ("train", "val", "test"):
        split_items = [
            item
            for item in source.canonical_dataset.items
            if item.split == split and item.eligibility_status.startswith("eligible")
        ]
        split_root = stage_root / split / "images"
        split_root.mkdir(parents=True, exist_ok=True)
        images: list[dict[str, Any]] = []
        annotations: list[dict[str, Any]] = []
        image_count = 0
        annotation_count = 0
        background_count = 0
        next_annotation_id = 1

        for image_id, item in enumerate(split_items, start=1):
            source_path = source.snapshot_root / "images" / item.relative_path
            staged_path = split_root / item.relative_path
            _link_or_copy(source_path, staged_path)
            width, height = _image_size(item=item, source_path=source_path)
            images.append(
                {
                    "id": image_id,
                    "file_name": item.relative_path.replace("\\", "/"),
                    "width": width,
                    "height": height,
                }
            )
            image_count += 1
            if item.background or not item.polygons:
                background_count += 1
                continue
            for polygon in item.polygons:
                segmentation = _absolute_polygon(polygon=polygon, width=width, height=height)
                bbox = _bbox_from_segmentation(segmentation)
                annotations.append(
                    {
                        "id": next_annotation_id,
                        "image_id": image_id,
                        "category_id": category_index[polygon.class_name],
                        "segmentation": [segmentation],
                        "bbox": bbox,
                        "area": round(bbox[2] * bbox[3], 4),
                        "iscrowd": 0,
                    }
                )
                next_annotation_id += 1
                annotation_count += 1

        annotation_path = stage_root / "annotations" / f"instances_{split}.json"
        write_json(
            annotation_path,
            {
                "info": {"description": "CV platform staged segmentation dataset"},
                "licenses": [],
                "images": images,
                "annotations": annotations,
                "categories": categories,
            },
        )
        split_counts[split] = {
            "images": image_count,
            "annotations": annotation_count,
            "background_images": background_count,
        }

    return {
        "summary": {"splits": split_counts},
        "manifest": {
            "format": "coco_instance_segmentation",
            "annotations": {
                split: str(stage_root / "annotations" / f"instances_{split}.json")
                for split in ("train", "val", "test")
            },
            "images_root": {
                split: str(stage_root / split / "images")
                for split in ("train", "val", "test")
            },
        },
    }


def _stage_classification_dataset(
    *,
    source: SnapshotTrainingSource,
    stage_root: Path,
) -> dict[str, Any]:
    background_label = "__background__"
    split_counts: dict[str, dict[str, int]] = {}

    for split in ("train", "val", "test"):
        split_items = [
            item
            for item in source.canonical_dataset.items
            if item.split == split and item.eligibility_status.startswith("eligible")
        ]
        manifest_rows: list[dict[str, Any]] = []
        class_counts: dict[str, int] = {}
        for item in split_items:
            source_path = source.snapshot_root / "images" / item.relative_path
            label = item.labels[0] if item.labels else background_label
            staged_path = stage_root / split / label / item.relative_path
            _link_or_copy(source_path, staged_path)
            manifest_rows.append(
                {
                    "image": str(staged_path.relative_to(stage_root)).replace("\\", "/"),
                    "label": label,
                    "background": item.background,
                    "task_id": item.task_id,
                }
            )
            class_counts[label] = class_counts.get(label, 0) + 1
        manifest_path = stage_root / "annotations" / f"{split}.jsonl"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in manifest_rows),
            encoding="utf-8",
        )
        split_counts[split] = {"items": len(manifest_rows), "classes": len(class_counts)}

    return {
        "summary": {"splits": split_counts},
        "manifest": {
            "format": "classification_imagefolder_jsonl",
            "manifests": {
                split: str(stage_root / "annotations" / f"{split}.jsonl")
                for split in ("train", "val", "test")
            },
            "images_root": {
                split: str(stage_root / split)
                for split in ("train", "val", "test")
            },
            "background_label": background_label,
        },
    }


def _link_or_copy(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        return
    try:
        target_path.hardlink_to(source_path)
    except OSError:
        shutil.copy2(source_path, target_path)


def _image_size(*, item: CanonicalItem, source_path: Path) -> tuple[int, int]:
    width = next((box.original_width for box in item.boxes if box.original_width), None)
    height = next((box.original_height for box in item.boxes if box.original_height), None)
    if width and height:
        return width, height
    seg_width = next((polygon.original_width for polygon in item.polygons if polygon.original_width), None)
    seg_height = next((polygon.original_height for polygon in item.polygons if polygon.original_height), None)
    if seg_width and seg_height:
        return seg_width, seg_height
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - exercised in runtime validation
        raise DatasetPreparationError(
            "Pillow is required to resolve image dimensions for staged datasets."
        ) from exc
    with Image.open(source_path) as image:
        return int(image.width), int(image.height)


def _absolute_box(*, box: BoundingBox, width: int, height: int) -> tuple[float, float, float, float]:
    x = round(box.x * width, 4)
    y = round(box.y * height, 4)
    box_width = round(box.width * width, 4)
    box_height = round(box.height * height, 4)
    return x, y, box_width, box_height


def _absolute_polygon(*, polygon: SegmentationPolygon, width: int, height: int) -> list[float]:
    values: list[float] = []
    for point in polygon.points:
        values.append(round(point.x * width, 4))
        values.append(round(point.y * height, 4))
    return values


def _oriented_box_to_yolo_line(
    *,
    box: OrientedBoundingBox,
    label_index: dict[str, int],
) -> str | None:
    class_index = label_index.get(box.class_name)
    if class_index is None or len(box.points) < 4:
        return None
    encoded_points = " ".join(
        f"{_clip01(point.x):.6f} {_clip01(point.y):.6f}" for point in box.points[:4]
    )
    return f"{class_index} {encoded_points}".strip()


def _clip01(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _bbox_from_segmentation(segmentation: list[float]) -> list[float]:
    xs = segmentation[::2]
    ys = segmentation[1::2]
    min_x = min(xs)
    min_y = min(ys)
    max_x = max(xs)
    max_y = max(ys)
    return [round(min_x, 4), round(min_y, 4), round(max_x - min_x, 4), round(max_y - min_y, 4)]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()
