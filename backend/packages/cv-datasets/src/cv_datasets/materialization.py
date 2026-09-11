from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from cv_datasets.conversion import (
    convert_label_studio_export,
    extract_declared_classes,
    generate_content_checksum,
)
from cv_datasets.models import DatasetAssetRecord, SnapshotArtifacts
from cv_datasets.validation import validate_canonical_dataset


def materialize_snapshot(
    *,
    snapshot_id: str,
    dataset_id: str,
    task_type: str,
    export_format: str,
    label_schema_version: str,
    project_label_schema_version: str | None,
    label_config: dict[str, Any] | str | None,
    required_classes: list[str],
    minimum_annotation_count: int,
    train_ratio: int,
    val_ratio: int,
    test_ratio: int,
    export_payload: list[dict[str, Any]],
    assets: list[DatasetAssetRecord],
    target_root: Path,
) -> SnapshotArtifacts:
    target_root.mkdir(parents=True, exist_ok=True)
    images_root = target_root / "images"
    annotations_root = target_root / "annotations"
    manifests_root = target_root / "manifests"
    training_root = annotations_root / "training-ready"
    images_root.mkdir(parents=True, exist_ok=True)
    annotations_root.mkdir(parents=True, exist_ok=True)
    manifests_root.mkdir(parents=True, exist_ok=True)
    training_root.mkdir(parents=True, exist_ok=True)

    canonical_dataset, split_summary, conversion_issues = convert_label_studio_export(
        dataset_id=dataset_id,
        task_type=task_type,
        export_payload=export_payload,
        assets=assets,
        label_config=label_config,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        test_ratio=test_ratio,
    )
    declared_classes = extract_declared_classes(label_config)
    validation = validate_canonical_dataset(
        dataset=canonical_dataset,
        split_summary=split_summary,
        issues=conversion_issues,
        declared_classes=declared_classes,
        required_classes=required_classes,
        minimum_annotation_count=minimum_annotation_count,
        task_type=task_type,
        dataset_label_schema_version=label_schema_version,
        project_label_schema_version=project_label_schema_version,
    )

    for item in canonical_dataset.items:
        source_path = Path(item.source_path)
        if not source_path.exists():
            continue
        destination = images_root / item.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)

    _write_json(annotations_root / "labelstudio_export.json", export_payload)
    _write_json(annotations_root / "canonical_dataset.json", canonical_dataset.model_dump())
    _write_json(manifests_root / "validation_report.json", validation.model_dump())
    _write_json(manifests_root / "split_metadata.json", split_summary.model_dump())
    _write_json(target_root / "label_map.json", canonical_dataset.label_map)

    if task_type == "classification":
        _write_classification_training_ready(training_root, canonical_dataset.items)
    elif task_type == "anomaly":
        _write_anomaly_training_ready(training_root, canonical_dataset.items)
    elif task_type == "detection" and validation.checks.get("no_duplicate_file_collisions", False):
        _write_detection_training_ready(training_root, canonical_dataset.items, canonical_dataset.label_map)
    elif task_type == "oriented_detection" and validation.checks.get("no_duplicate_file_collisions", False):
        _write_oriented_detection_training_ready(
            training_root,
            canonical_dataset.items,
            canonical_dataset.label_map,
        )
    elif task_type == "segmentation" and validation.checks.get("no_duplicate_file_collisions", False):
        _write_segmentation_training_ready(training_root, canonical_dataset.items, canonical_dataset.label_map)

    content_sha256 = generate_content_checksum(
        dataset=canonical_dataset,
        split_summary=split_summary,
        label_schema_version=label_schema_version,
        export_format=export_format,
    )
    snapshot_manifest = {
        "snapshot_id": snapshot_id,
        "dataset_id": dataset_id,
        "task_type": task_type,
        "label_schema_version": label_schema_version,
        "export_format": export_format,
        "item_count": len(canonical_dataset.items),
        "annotation_count": canonical_dataset.annotation_count,
        "eligible_item_count": canonical_dataset.eligible_item_count,
        "submitted_labeled_item_count": canonical_dataset.submitted_labeled_item_count,
        "submitted_background_item_count": canonical_dataset.submitted_background_item_count,
        "draft_item_count": canonical_dataset.draft_item_count,
        "invalid_item_count": canonical_dataset.invalid_item_count,
        "rotated_rectangle_count": canonical_dataset.rotated_rectangle_count,
        "content_checksum": content_sha256,
        "split_metadata": split_summary.model_dump(),
        "validation_status": validation.status,
        "class_distribution": canonical_dataset.class_distribution,
        "anomaly_distribution": canonical_dataset.anomaly_distribution,
        "image_view": canonical_dataset.metadata.get("image_view"),
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
            "label_map": "label_map.json",
            "snapshot_manifest": "snapshot_manifest.json",
        },
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_classification_training_ready(target_root: Path, items: list[Any]) -> None:
    for split in ("train", "val", "test"):
        target_path = target_root / "classification" / f"{split}.jsonl"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for item in items:
            if item.split != split or not item.eligibility_status.startswith("eligible"):
                continue
            rows.append(
                json.dumps(
                    {
                        "image": f"images/{item.relative_path}",
                        "labels": item.labels,
                        "background": item.background,
                        "task_id": item.task_id,
                    },
                    sort_keys=True,
                )
            )
        target_path.write_text("\n".join(rows), encoding="utf-8")


def _write_anomaly_training_ready(target_root: Path, items: list[Any]) -> None:
    anomaly_root = target_root / "anomaly"
    rows_by_split: dict[str, list[str]] = {"train": [], "val": [], "test": []}
    split_counts: dict[str, dict[str, int]] = {
        "train": {"normal": 0, "anomalous": 0},
        "val": {"normal": 0, "anomalous": 0},
        "test": {"normal": 0, "anomalous": 0},
    }
    for item in items:
        if item.eligibility_status != "eligible_labeled":
            continue
        label = item.anomaly_label or (item.labels[0] if item.labels else None)
        if label not in {"normal", "anomalous"}:
            continue
        split_counts[item.split][label] += 1
        rows_by_split[item.split].append(
            json.dumps(
                {
                    "image": f"images/{item.relative_path}",
                    "label": label,
                    "task_id": item.task_id,
                    "mask": None,
                },
                sort_keys=True,
            )
        )
    for split, rows in rows_by_split.items():
        target_path = anomaly_root / f"{split}.jsonl"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("\n".join(rows), encoding="utf-8")
    _write_json(
        anomaly_root / "dataset.json",
        {
            "format": "anomaly_image_level_jsonl",
            "normal_label": "normal",
            "abnormal_label": "anomalous",
            "masks_supported": False,
            "splits": split_counts,
        },
    )


def _write_detection_training_ready(target_root: Path, items: list[Any], label_map: list[str]) -> None:
    label_index = {label: index for index, label in enumerate(label_map)}
    data_yaml = {
        "train": "manifests/train.txt",
        "val": "manifests/val.txt",
        "test": "manifests/test.txt",
        "names": label_map,
    }
    _write_json(target_root / "detection" / "data.json", data_yaml)

    manifests_root = target_root.parent.parent / "manifests"
    split_manifests: dict[str, list[str]] = {"train": [], "val": [], "test": []}
    for item in items:
        if not item.eligibility_status.startswith("eligible"):
            continue
        split_manifests[item.split].append(f"images/{item.relative_path}")
        labels_dir = target_root / "detection" / "labels" / item.split
        labels_dir.mkdir(parents=True, exist_ok=True)
        label_path = labels_dir / f"{Path(item.relative_path).stem}.txt"
        lines = []
        for box in item.boxes:
            center_x = box.x + (box.width / 2.0)
            center_y = box.y + (box.height / 2.0)
            lines.append(
                f"{label_index[box.class_name]} {center_x:.6f} {center_y:.6f} {box.width:.6f} {box.height:.6f}"
            )
        label_path.write_text("\n".join(lines), encoding="utf-8")

    for split, values in split_manifests.items():
        (manifests_root / f"{split}.txt").write_text("\n".join(values), encoding="utf-8")


def _write_oriented_detection_training_ready(target_root: Path, items: list[Any], label_map: list[str]) -> None:
    label_index = {label: index for index, label in enumerate(label_map)}
    data_yaml = {
        "train": "manifests/train.txt",
        "val": "manifests/val.txt",
        "test": "manifests/test.txt",
        "names": label_map,
        "format": "yolo_obb",
    }
    _write_json(target_root / "oriented_detection" / "data.json", data_yaml)

    manifests_root = target_root.parent.parent / "manifests"
    split_manifests: dict[str, list[str]] = {"train": [], "val": [], "test": []}
    for item in items:
        if not item.eligibility_status.startswith("eligible"):
            continue
        split_manifests[item.split].append(f"images/{item.relative_path}")
        labels_dir = target_root / "oriented_detection" / "labels" / item.split
        labels_dir.mkdir(parents=True, exist_ok=True)
        label_path = labels_dir / f"{Path(item.relative_path).stem}.txt"
        lines = []
        for box in item.oriented_boxes:
            encoded_points = " ".join(
                f"{point.x:.6f} {point.y:.6f}" for point in box.points[:4]
            )
            lines.append(f"{label_index[box.class_name]} {encoded_points}".strip())
        label_path.write_text("\n".join(lines), encoding="utf-8")

    for split, values in split_manifests.items():
        (manifests_root / f"{split}.txt").write_text("\n".join(values), encoding="utf-8")


def _write_segmentation_training_ready(target_root: Path, items: list[Any], label_map: list[str]) -> None:
    label_index = {label: index for index, label in enumerate(label_map)}
    data_yaml = {
        "train": "manifests/train.txt",
        "val": "manifests/val.txt",
        "test": "manifests/test.txt",
        "names": label_map,
    }
    _write_json(target_root / "segmentation" / "data.json", data_yaml)

    manifests_root = target_root.parent.parent / "manifests"
    split_manifests: dict[str, list[str]] = {"train": [], "val": [], "test": []}
    for item in items:
        if not item.eligibility_status.startswith("eligible"):
            continue
        split_manifests[item.split].append(f"images/{item.relative_path}")
        labels_dir = target_root / "segmentation" / "labels" / item.split
        labels_dir.mkdir(parents=True, exist_ok=True)
        label_path = labels_dir / f"{Path(item.relative_path).stem}.txt"
        lines = []
        for polygon in item.polygons:
            encoded_points = " ".join(
                f"{point.x:.6f} {point.y:.6f}" for point in polygon.points
            )
            lines.append(f"{label_index[polygon.class_name]} {encoded_points}".strip())
        label_path.write_text("\n".join(lines), encoding="utf-8")

    for split, values in split_manifests.items():
        (manifests_root / f"{split}.txt").write_text("\n".join(values), encoding="utf-8")
