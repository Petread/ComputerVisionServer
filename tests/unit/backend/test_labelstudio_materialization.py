from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cv_datasets import (
    DatasetAssetRecord,
    convert_label_studio_export,
    extract_declared_classes,
    materialize_snapshot,
)


def test_materialize_detection_snapshot_creates_training_ready_outputs(tmp_path: Path) -> None:
    image_path = tmp_path / "assets" / "widgets" / "image-1.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"image-bytes")

    export_payload = [
        {
            "id": 1,
            "data": {"image": "widgets/image-1.jpg"},
            "annotations": [
                {
                    "id": 11,
                    "result": [
                        {
                            "value": {
                                "x": 10,
                                "y": 20,
                                "width": 30,
                                "height": 40,
                                "rectanglelabels": ["defect"],
                                "original_width": 640,
                                "original_height": 480,
                            }
                        }
                    ],
                }
            ],
        }
    ]

    artifacts = materialize_snapshot(
        snapshot_id="dss_test",
        dataset_id="dst_test",
        task_type="detection",
        export_format="JSON",
        label_schema_version="2",
        project_label_schema_version="2",
        label_config={"labels": ["defect"]},
        required_classes=["defect"],
        minimum_annotation_count=1,
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
        export_payload=export_payload,
        assets=[
            DatasetAssetRecord(
                asset_id="ast_1",
                relative_path="widgets/image-1.jpg",
                sha256="sha256-image-1",
                source_path=str(image_path),
            )
        ],
        target_root=tmp_path / "snapshot",
    )

    split = artifacts.canonical_dataset.items[0].split
    labels_path = (
        tmp_path
        / "snapshot"
        / "annotations"
        / "training-ready"
        / "detection"
        / "labels"
        / split
        / "image-1.txt"
    )

    assert artifacts.validation.status == "passed"
    assert artifacts.content_sha256.startswith("sha256:")
    assert (tmp_path / "snapshot" / "snapshot_manifest.json").exists()
    assert labels_path.exists()
    assert json.loads((tmp_path / "snapshot" / "label_map.json").read_text(encoding="utf-8")) == [
        "defect"
    ]


def test_materialize_oriented_detection_snapshot_writes_yolo_obb_labels(tmp_path: Path) -> None:
    image_path = tmp_path / "assets" / "widgets" / "rotated.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"image-bytes")

    artifacts = materialize_snapshot(
        snapshot_id="dss_obb",
        dataset_id="dst_obb",
        task_type="oriented_detection",
        export_format="JSON",
        label_schema_version="2",
        project_label_schema_version="2",
        label_config={"labels": ["scratch"]},
        required_classes=["scratch"],
        minimum_annotation_count=1,
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
        export_payload=[
            {
                "id": 1,
                "data": {"image": "widgets/rotated.jpg"},
                "annotations": [
                    {
                        "id": 11,
                        "result": [
                            {
                                "value": {
                                    "x": 10,
                                    "y": 20,
                                    "width": 30,
                                    "height": 10,
                                    "rotation": 30,
                                    "rectanglelabels": ["scratch"],
                                    "original_width": 640,
                                    "original_height": 480,
                                }
                            }
                        ],
                    }
                ],
            }
        ],
        assets=[
            DatasetAssetRecord(
                asset_id="ast_obb",
                relative_path="widgets/rotated.jpg",
                sha256="sha256-obb",
                source_path=str(image_path),
            )
        ],
        target_root=tmp_path / "snapshot",
    )

    split = artifacts.canonical_dataset.items[0].split
    label_path = (
        tmp_path
        / "snapshot"
        / "annotations"
        / "training-ready"
        / "oriented_detection"
        / "labels"
        / split
        / "rotated.txt"
    )
    values = label_path.read_text(encoding="utf-8").split()

    assert artifacts.validation.status == "passed"
    assert artifacts.canonical_dataset.rotated_rectangle_count == 1
    assert artifacts.canonical_dataset.items[0].oriented_boxes[0].rotation == 30
    assert values[0] == "0"
    assert len(values) == 9


def test_detection_snapshot_rejects_rotated_rectangle_labels(tmp_path: Path) -> None:
    image_path = tmp_path / "assets" / "widgets" / "rotated.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"image-bytes")

    artifacts = materialize_snapshot(
        snapshot_id="dss_rotated_detection",
        dataset_id="dst_rotated_detection",
        task_type="detection",
        export_format="JSON",
        label_schema_version="2",
        project_label_schema_version="2",
        label_config={"labels": ["scratch"]},
        required_classes=["scratch"],
        minimum_annotation_count=1,
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
        export_payload=[
            {
                "id": 1,
                "data": {"image": "widgets/rotated.jpg"},
                "annotations": [
                    {
                        "id": 11,
                        "result": [
                            {
                                "value": {
                                    "x": 10,
                                    "y": 20,
                                    "width": 30,
                                    "height": 10,
                                    "rotation": 15,
                                    "rectanglelabels": ["scratch"],
                                }
                            }
                        ],
                    }
                ],
            }
        ],
        assets=[
            DatasetAssetRecord(
                asset_id="ast_rotated",
                relative_path="widgets/rotated.jpg",
                sha256="sha256-rotated",
                source_path=str(image_path),
            )
        ],
        target_root=tmp_path / "snapshot",
    )

    assert artifacts.validation.status == "failed"
    assert artifacts.canonical_dataset.rotated_rectangle_count == 1
    assert any("Oriented Detection" in issue.message for issue in artifacts.validation.issues)


def test_materialize_snapshot_reports_missing_files_and_required_classes(tmp_path: Path) -> None:
    export_payload = [
        {
            "id": 7,
            "data": {"image": "widgets/missing.jpg"},
            "annotations": [
                {
                    "id": 77,
                    "result": [
                        {
                            "value": {
                                "choices": ["ok"],
                            }
                        }
                    ],
                }
            ],
        }
    ]

    artifacts = materialize_snapshot(
        snapshot_id="dss_missing",
        dataset_id="dst_missing",
        task_type="classification",
        export_format="JSON",
        label_schema_version="1",
        project_label_schema_version="1",
        label_config={"labels": ["ok", "defect"]},
        required_classes=["defect"],
        minimum_annotation_count=1,
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
        export_payload=export_payload,
        assets=[
            DatasetAssetRecord(
                asset_id="ast_missing",
                relative_path="widgets/missing.jpg",
                sha256="missing",
                source_path=str(tmp_path / "assets" / "widgets" / "missing.jpg"),
            )
        ],
        target_root=tmp_path / "snapshot-missing",
    )

    issue_codes = {issue.code for issue in artifacts.validation.issues}

    assert artifacts.validation.status == "failed"
    assert "missing_file" in issue_codes
    assert "required_classes_missing" in issue_codes
    assert (tmp_path / "snapshot-missing" / "manifests" / "validation_report.json").exists()


def test_convert_export_resolves_labelstudio_local_file_urls(tmp_path: Path) -> None:
    image_path = tmp_path / "assets" / "widgets" / "sample.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"png-bytes")

    dataset, split_summary, issues = convert_label_studio_export(
        dataset_id="dst_local_files",
        task_type="classification",
        export_payload=[
            {
                "id": 1,
                "data": {
                    "image": "/data/local-files/?d=assets/widgets/sample.png",
                },
                "annotations": [
                    {
                        "id": 11,
                        "result": [
                            {
                                "value": {
                                    "choices": ["accepted"],
                                }
                            }
                        ],
                    }
                ],
            }
        ],
        assets=[
            DatasetAssetRecord(
                asset_id="ast_local_file",
                relative_path="assets/widgets/sample.png",
                sha256="sample-sha",
                source_path=str(image_path),
            )
        ],
        label_config={"labels": ["accepted"]},
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
    )

    assert len(dataset.items) == 1
    assert dataset.items[0].relative_path == "assets/widgets/sample.png"
    assert split_summary.train_count + split_summary.val_count + split_summary.test_count == 1
    assert issues == []


def test_materialize_anomaly_snapshot_maps_good_bad_labels_to_training_ready_dataset(
    tmp_path: Path,
) -> None:
    candidates = [
        (task_id, f"widgets/anomaly-{task_id}.jpg")
        for task_id in range(1, 5)
    ]
    ordered = sorted(
        candidates,
        key=lambda item: hashlib.sha256(f"dst_anomaly:{item[0]}:{item[1]}".encode("utf-8")).hexdigest(),
    )
    labels_by_task = {
        ordered[0][0]: "good",
        ordered[1][0]: "normal",
        ordered[2][0]: "good",
        ordered[3][0]: "bad",
    }
    assets: list[DatasetAssetRecord] = []
    export_payload = []
    for task_id, relative_path in candidates:
        image_path = tmp_path / "assets" / relative_path
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(f"image-{task_id}".encode("ascii"))
        assets.append(
            DatasetAssetRecord(
                asset_id=f"ast_anomaly_{task_id}",
                relative_path=relative_path,
                sha256=f"sha256-anomaly-{task_id}",
                source_path=str(image_path),
            )
        )
        export_payload.append(
            {
                "id": task_id,
                "data": {"image": relative_path},
                "annotations": [
                    {
                        "id": task_id + 100,
                        "result": [
                            {
                                "value": {
                                    "choices": [labels_by_task[task_id]],
                                }
                            }
                        ],
                    }
                ],
            }
        )

    artifacts = materialize_snapshot(
        snapshot_id="dss_anomaly",
        dataset_id="dst_anomaly",
        task_type="anomaly",
        export_format="JSON",
        label_schema_version="1",
        project_label_schema_version="1",
        label_config={"labels": ["good", "bad"]},
        required_classes=["good", "bad"],
        minimum_annotation_count=1,
        train_ratio=50,
        val_ratio=50,
        test_ratio=0,
        export_payload=export_payload,
        assets=assets,
        target_root=tmp_path / "snapshot-anomaly",
    )

    training_ready = tmp_path / "snapshot-anomaly" / "annotations" / "training-ready" / "anomaly"
    dataset_manifest = json.loads((training_ready / "dataset.json").read_text(encoding="utf-8"))
    val_items = [
        json.loads(line)
        for line in (training_ready / "val.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert artifacts.validation.status == "passed"
    assert artifacts.canonical_dataset.label_map == ["normal", "anomalous"]
    assert artifacts.canonical_dataset.anomaly_distribution == {"normal": 3, "anomalous": 1}
    assert dataset_manifest["format"] == "anomaly_image_level_jsonl"
    assert dataset_manifest["masks_supported"] is False
    assert {item["label"] for item in val_items} == {"normal", "anomalous"}


def test_anomaly_conversion_rejects_conflicting_good_bad_labels(tmp_path: Path) -> None:
    image_path = tmp_path / "assets" / "widgets" / "conflict.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"image-bytes")

    dataset, _split_summary, issues = convert_label_studio_export(
        dataset_id="dst_anomaly_conflict",
        task_type="anomaly",
        export_payload=[
            {
                "id": 1,
                "data": {"image": "widgets/conflict.jpg"},
                "annotations": [
                    {
                        "id": 11,
                        "result": [
                            {"value": {"choices": ["good", "bad"]}},
                        ],
                    }
                ],
            }
        ],
        assets=[
            DatasetAssetRecord(
                asset_id="ast_conflict",
                relative_path="widgets/conflict.jpg",
                sha256="conflict-sha",
                source_path=str(image_path),
            )
        ],
        label_config={"labels": ["good", "bad"]},
        train_ratio=80,
        val_ratio=20,
        test_ratio=0,
    )

    assert dataset.items[0].anomaly_label is None
    assert {issue.code for issue in issues} == {"invalid_anomaly_label"}


def test_convert_export_treats_completed_polygon_annotations_with_draft_origin_as_submitted(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "assets" / "hands" / "sample.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"jpg-bytes")

    dataset, split_summary, issues = convert_label_studio_export(
        dataset_id="dst_segmentation",
        task_type="segmentation",
        export_payload=[
            {
                "id": 57,
                "data": {
                    "image": "/label-studio/data/upload/13/103efccd-Image_48.jpg",
                },
                "annotations": [
                    {
                        "id": 73,
                        "completed_by": 1,
                        "created_at": "2026-04-23T16:27:00.740382Z",
                        "updated_at": "2026-04-23T16:27:00.740396Z",
                        "draft_created_at": "2026-04-23T16:25:43.401826Z",
                        "was_cancelled": False,
                        "ground_truth": False,
                        "result": [
                            {
                                "type": "polygonlabels",
                                "value": {
                                    "points": [[10, 20], [30, 20], [30, 40], [10, 40]],
                                    "polygonlabels": ["Hand"],
                                    "original_width": 3024,
                                    "original_height": 4032,
                                },
                            }
                        ],
                    }
                ],
            }
        ],
        assets=[
            DatasetAssetRecord(
                asset_id="ast_segmentation",
                relative_path="13/103efccd-Image_48.jpg",
                sha256="segmentation-sha",
                source_path=str(image_path),
            )
        ],
        label_config={"labels": ["Hand"]},
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
    )

    assert issues == []
    assert dataset.eligible_item_count == 1
    assert dataset.submitted_labeled_item_count == 1
    assert dataset.draft_item_count == 0
    assert dataset.items[0].submitted is True
    assert dataset.items[0].eligibility_status == "eligible_labeled"
    assert dataset.items[0].polygons[0].class_name == "Hand"
    assert split_summary.train_count + split_summary.val_count + split_summary.test_count == 1


def test_labelstudio_control_names_are_not_treated_as_detection_classes() -> None:
    label_config = {
        "label": {
            "type": "RectangleLabels",
            "inputs": [{"type": "Image", "value": "image"}],
            "labels": ["Hand"],
            "to_name": ["image"],
            "labels_attrs": {"Hand": {"value": "Hand", "background": "#FFA39E"}},
        }
    }

    assert extract_declared_classes(label_config) == ["Hand"]


def test_detection_conversion_counts_boxes_once_and_balances_small_splits(tmp_path: Path) -> None:
    assets: list[DatasetAssetRecord] = []
    export_payload = []
    for index in range(30):
        relative_path = f"hands/image-{index}.jpg"
        image_path = tmp_path / relative_path
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"jpg-bytes")
        assets.append(
            DatasetAssetRecord(
                asset_id=f"ast_{index}",
                relative_path=relative_path,
                sha256=f"sha-{index}",
                source_path=str(image_path),
            )
        )
        export_payload.append(
            {
                "id": index + 1,
                "data": {"image": relative_path},
                "annotations": [
                    {
                        "id": 100 + index,
                        "completed_by": 1,
                        "result": [
                            {
                                "value": {
                                    "x": 10,
                                    "y": 20,
                                    "width": 30,
                                    "height": 40,
                                    "rectanglelabels": ["Hand"],
                                    "original_width": 640,
                                    "original_height": 480,
                                }
                            }
                        ],
                    }
                ],
            }
        )

    dataset, split_summary, issues = convert_label_studio_export(
        dataset_id="dst_hands",
        task_type="detection",
        export_payload=export_payload,
        assets=assets,
        label_config={
            "label": {
                "type": "RectangleLabels",
                "inputs": [{"type": "Image", "value": "image"}],
                "labels": ["Hand"],
                "to_name": ["image"],
            }
        },
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
    )

    assert issues == []
    assert dataset.label_map == ["Hand"]
    assert dataset.class_distribution == {"Hand": 30}
    assert split_summary.train_count == 24
    assert split_summary.val_count == 3
    assert split_summary.test_count == 3
