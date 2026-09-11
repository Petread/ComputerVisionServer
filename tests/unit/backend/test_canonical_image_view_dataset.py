from __future__ import annotations

import json
from pathlib import Path

from cv_datasets import DatasetAssetRecord, convert_label_studio_export, materialize_snapshot


def _asset(tmp_path: Path, name: str = "corridor.jpg") -> DatasetAssetRecord:
    image = tmp_path / "assets" / name
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"canonical-image-pixels")
    return DatasetAssetRecord(
        asset_id=f"ast_{name}",
        relative_path=name,
        sha256=f"sha256-{name}",
        source_path=str(image),
    )


def _task(
    task_id: int,
    image: str,
    *,
    include_view: bool = True,
    view_profile_id: str = "stem-measurement-view-v1",
    view_profile_checksum: str = "c" * 64,
) -> dict[str, object]:
    meta: dict[str, object] = {}
    if include_view:
        meta = {
            "source_frame": {
                "width": 1600,
                "height": 640,
                "view_profile_id": view_profile_id,
                "view_profile_checksum": view_profile_checksum,
                "color_space": "BGR",
                "undistort_preprocessing_version": "camera.undistort.v1",
                "image_view_preprocessing_version": "image.view-runtime.v1",
                "source_frame_context_id": "ctx_full",
                "source_frame_timestamp": "2026-08-05T12:00:00Z",
                "image_context": {
                    "width": 1600,
                    "height": 640,
                    "transform": {
                        "transform_type": "perspective_rectify",
                        "parameters": {
                            "view_profile_id": view_profile_id,
                            "view_profile_checksum": view_profile_checksum,
                            "intrinsic_calibration_hash": "d" * 64,
                            "source_to_view_homography": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                            "view_to_source_homography": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                        },
                    },
                },
            },
            "metadata": {
                "capture_source": "PIPELINE_IMAGE_OUTPUT",
                "view_profile_id": view_profile_id,
                "view_profile_checksum": view_profile_checksum,
                "frame_eligibility": {
                    "eligible": True,
                    "task_role": "POSITIVE",
                    "reason_codes": [],
                    "output_width": 1600,
                    "output_height": 640,
                },
            },
        }
    return {
        "id": task_id,
        "data": {"image": image},
        "meta": meta,
        "annotations": [
            {
                "id": task_id * 10,
                "result": [
                    {
                        "type": "polygonlabels",
                        "value": {
                            "points": [[10, 30], [90, 30], [90, 70], [10, 70]],
                            "polygonlabels": ["stem"],
                            "original_width": 1600,
                            "original_height": 640,
                        },
                    }
                ],
            }
        ],
    }


def test_label_studio_conversion_keeps_canonical_view_provenance() -> None:
    # The Label Studio task image is the canonical asset; the full camera image exists only
    # as provenance and must never become the training item.
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        root = Path(directory)
        asset = _asset(root)
        dataset, _split, issues = convert_label_studio_export(
            dataset_id="dst_stem_corridor_v1",
            task_type="segmentation",
            export_payload=[_task(1, "corridor.jpg")],
            assets=[asset],
            label_config={"labels": ["stem"]},
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
        )

    assert issues == []
    assert dataset.items[0].relative_path == "corridor.jpg"
    assert dataset.items[0].metadata["capture_source"] == "PIPELINE_IMAGE_OUTPUT"
    assert dataset.items[0].metadata["view_profile_id"] == "stem-measurement-view-v1"
    assert dataset.items[0].metadata["frame_eligibility"]["task_role"] == "POSITIVE"
    assert dataset.metadata["image_view"]["profile_checksum"] == "c" * 64
    assert dataset.metadata["image_view"]["input_width"] == 1600


def test_snapshot_manifest_exports_model_view_compatibility(tmp_path: Path) -> None:
    asset = _asset(tmp_path)
    artifacts = materialize_snapshot(
        snapshot_id="dss_stem_corridor_v1",
        dataset_id="dst_stem_corridor_v1",
        task_type="segmentation",
        export_format="JSON",
        label_schema_version="1",
        project_label_schema_version="1",
        label_config={"labels": ["stem"]},
        required_classes=["stem"],
        minimum_annotation_count=1,
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
        export_payload=[_task(1, "corridor.jpg")],
        assets=[asset],
        target_root=tmp_path / "snapshot",
    )
    manifest = json.loads(
        (tmp_path / "snapshot" / "snapshot_manifest.json").read_text(encoding="utf-8")
    )

    assert artifacts.validation.status == "passed"
    assert manifest["image_view"]["view_profile_id"] == "stem-measurement-view-v1"
    assert manifest["image_view"]["input_height"] == 640
    assert manifest["image_view"]["color_space"] == "BGR"


def test_canonical_and_legacy_items_cannot_be_mixed_in_one_training_snapshot(
    tmp_path: Path,
) -> None:
    first = _asset(tmp_path, "canonical.jpg")
    second = _asset(tmp_path, "legacy-full.jpg")
    dataset, _split, issues = convert_label_studio_export(
        dataset_id="dst_mixed",
        task_type="segmentation",
        export_payload=[
            _task(1, "canonical.jpg", include_view=True),
            _task(2, "legacy-full.jpg", include_view=False),
        ],
        assets=[first, second],
        label_config={"labels": ["stem"]},
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
    )

    assert len(dataset.items) == 2
    assert "image_view_provenance_incomplete" in {issue.code for issue in issues}


def test_multiple_canonical_view_profiles_are_allowed_and_recorded(tmp_path: Path) -> None:
    first = _asset(tmp_path, "corridor-v1.jpg")
    second = _asset(tmp_path, "corridor-v2.jpg")
    artifacts = materialize_snapshot(
        snapshot_id="dss_stem_multi_view",
        dataset_id="dst_stem_multi_view",
        task_type="segmentation",
        export_format="JSON",
        label_schema_version="1",
        project_label_schema_version="1",
        label_config={"labels": ["stem"]},
        required_classes=["stem"],
        minimum_annotation_count=1,
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
        export_payload=[
            _task(1, "corridor-v1.jpg"),
            _task(
                2,
                "corridor-v2.jpg",
                view_profile_id="stem-measurement-view-v2",
                view_profile_checksum="d" * 64,
            ),
        ],
        assets=[first, second],
        target_root=tmp_path / "multi-view-snapshot",
    )
    manifest = json.loads(
        (tmp_path / "multi-view-snapshot" / "snapshot_manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert artifacts.validation.status == "passed"
    mixed_issue = next(
        issue
        for issue in artifacts.validation.issues
        if issue.code == "image_view_profile_mixed"
    )
    assert mixed_issue.severity == "warning"
    assert manifest["image_view"]["contract_mode"] == "MULTIPLE_CANONICAL_VIEWS"
    assert manifest["image_view"]["profile_count"] == 2
    assert {
        profile["view_profile_id"] for profile in manifest["image_view"]["profiles"]
    } == {"stem-measurement-view-v1", "stem-measurement-view-v2"}
    assert "view_profile_id" not in manifest["image_view"]
