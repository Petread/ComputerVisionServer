from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cv_contracts import TrainingJobCreateRequest
from cv_db import Base, Dataset, DatasetSnapshot, create_training_job_request, record_deployment_approval, register_trained_model_version
from cv_db.model_catalog import architecture_summary
from cv_status import DatasetStatus, DeploymentApprovalScope, DeploymentApprovalStatus, SnapshotStatus, TaskType
from cv_training.backends.openmmlab_detection import (
    OpenMMLabDetectionTrainer,
    _build_platform_training_hook,
    _configure_early_stopping_hook,
    _configure_gradient_accumulation,
    _dataset_item_count,
    _is_albu_transform,
    _parse_version_triplet,
    _resolve_effective_train_batch_size,
    _rewrite_yolov8_training_schedule,
    _strip_unsupported_albu_transforms,
    _validate_supported_openmmlab_runtime,
)
from cv_training.backends.ultralytics_yolo import _classification_target_relative_path
from cv_training.dataset_staging import stage_framework_dataset
from cv_training.trainer import get_default_trainer, resolve_trainer_architecture, resolve_trainer_mode
from cv_training.trainer_types import (
    TrainerConfig,
    TrainerDatasetArtifact,
    TrainerProgress,
    TrainerResumeState,
)
from cv_training.validators import build_artifact_validation_summary
from cv_training.backends.simulation import SimulationTrainer


class _CollectingControl:
    def __init__(self) -> None:
        self.runtime_metadata: list[dict[str, object]] = []
        self.progress: list[TrainerProgress] = []
        self.checkpoints: list[object] = []

    def should_cancel(self) -> bool:
        return False

    def on_progress(self, progress: TrainerProgress) -> None:
        self.progress.append(progress)

    def on_checkpoint(self, checkpoint) -> None:  # noqa: ANN001
        self.checkpoints.append(checkpoint)

    def on_runtime_metadata(self, metadata: dict[str, object]) -> None:
        self.runtime_metadata.append(metadata)


def _sqlite_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _seed_training_job(session: Session, *, snapshot_id: str, trainer_mode: str) -> tuple[DatasetSnapshot, object]:
    dataset = Dataset(
        id=f"dst_{snapshot_id}",
        slug=f"dataset-{snapshot_id}",
        name=f"Dataset {snapshot_id}",
        task_type=TaskType.DETECTION.value,
        label_schema_version="1",
        status=DatasetStatus.SNAPSHOTTED.value,
        created_by="tester",
    )
    snapshot = DatasetSnapshot(
        id=snapshot_id,
        dataset_id=dataset.id,
        label_project_id=None,
        snapshot_no=1,
        status=SnapshotStatus.READY.value,
        item_count=3,
        annotation_count=2,
        manifest_uri=f"/snapshots/{snapshot_id}/manifest.json",
        content_sha256=f"checksum-{snapshot_id}",
        dvc_rev=f"dvc-{snapshot_id}",
        created_by="tester",
        export_format="JSON",
        label_schema_version="1",
        split_metadata={"train_ratio": 80, "val_ratio": 10, "test_ratio": 10},
        validation_report={},
        validation_status="passed",
        statistics={"item_count": 3},
        idempotency_key=f"key-{snapshot_id}",
    )
    session.add_all([dataset, snapshot])
    session.flush()

    payload = TrainingJobCreateRequest.model_validate(
        {
            "model_family_slug": "widget-inspection-det",
            "dataset_snapshot_id": snapshot.id,
            "task_type": "detection",
            "architecture": architecture_summary(
                task_type=TaskType.DETECTION,
                family="yolo",
                version="v8",
                size="n",
            ).model_dump(mode="json"),
            "model_template_key": "detection-yolov8-n",
            "recipe_name": "baseline_det",
            "recipe_version": "1.1.0",
            "max_epochs": 2,
            "checkpoint_interval_steps": 1,
            "parameter_overrides": {"trainer_mode": trainer_mode},
            "best_checkpoint_rule": {
                "metric_name": "map_50_95",
                "mode": "max",
                "phase": "evaluation",
                "minimum_epoch": 1,
            },
            "promotion_criteria": [
                {
                    "metric_name": "map_50_95",
                    "comparison": "gte",
                    "threshold": 0.5,
                    "phase": "evaluation",
                }
            ],
            "allow_duplicate_snapshot_training": False,
        }
    )
    job = create_training_job_request(
        session,
        payload=payload,
        requested_by="tester",
        source_service="tests",
        idempotency_key=f"training-{snapshot_id}",
    )
    return snapshot, job


def test_trainer_selection_supports_real_task_backends() -> None:
    assert resolve_trainer_mode("simulation") == "simulation"
    assert resolve_trainer_mode("real") == "real"

    detection = resolve_trainer_architecture(
        task_type=TaskType.DETECTION,
        family="yolo",
        version="v8",
        size="n",
    )
    assert detection.supported is True
    assert get_default_trainer(TaskType.DETECTION, trainer_mode="real", architecture=detection).trainer_backend == "openmmlab-mmyolo"

    classification = resolve_trainer_architecture(
        task_type=TaskType.CLASSIFICATION,
        family="resnet",
        version="v1",
        size="18",
    )
    assert classification.supported is True
    assert get_default_trainer(
        TaskType.CLASSIFICATION,
        trainer_mode="real",
        architecture=classification,
    ).trainer_backend == "torchvision-classification"
    assert (
        get_default_trainer(TaskType.CLASSIFICATION, trainer_mode="real").trainer_backend
        == "torchvision-classification"
    )

    segmentation = resolve_trainer_architecture(
        task_type=TaskType.SEGMENTATION,
        family="deeplab",
        version="v3",
        size="resnet50",
    )
    assert segmentation.supported is True
    assert get_default_trainer(
        TaskType.SEGMENTATION,
        trainer_mode="real",
        architecture=segmentation,
    ).trainer_backend == "torchvision-segmentation"
    assert (
        get_default_trainer(TaskType.SEGMENTATION, trainer_mode="real").trainer_backend
        == "torchvision-segmentation"
    )

    anomaly = resolve_trainer_architecture(
        task_type=TaskType.ANOMALY,
        family="patchcore",
        version="anomalib",
        size="wide_resnet50_2",
    )
    assert anomaly.supported is True
    assert anomaly.backend_model_key == "anomalib:patchcore:wide_resnet50_2"
    assert (
        get_default_trainer(TaskType.ANOMALY, trainer_mode="real", architecture=anomaly).trainer_backend
        == "anomalib-patchcore"
    )

    unsupported = resolve_trainer_architecture(
        task_type=TaskType.CLASSIFICATION,
        family="yolo",
        version="v8",
        size="n",
    )
    assert unsupported.supported is False
    with pytest.raises(ValueError, match="Classification real training supports"):
        get_default_trainer(TaskType.CLASSIFICATION, trainer_mode="real", architecture=unsupported)

    yolo11_classification = resolve_trainer_architecture(
        task_type=TaskType.CLASSIFICATION,
        family="yolo",
        version="v11",
        size="x",
    )
    assert yolo11_classification.supported is True
    assert (
        get_default_trainer(
            TaskType.CLASSIFICATION,
            trainer_mode="real",
            architecture=yolo11_classification,
        ).trainer_backend
        == "ultralytics-yolo"
    )

    yolo26_detection = resolve_trainer_architecture(
        task_type=TaskType.DETECTION,
        family="yolo",
        version="v26",
        size="m",
    )
    assert yolo26_detection.supported is True
    assert (
        get_default_trainer(
            TaskType.DETECTION,
            trainer_mode="real",
            architecture=yolo26_detection,
        ).trainer_backend
        == "ultralytics-yolo"
    )

    yolo26_segmentation = resolve_trainer_architecture(
        task_type=TaskType.SEGMENTATION,
        family="yolo",
        version="v26",
        size="l",
    )
    assert yolo26_segmentation.supported is True
    assert (
        get_default_trainer(
            TaskType.SEGMENTATION,
            trainer_mode="real",
            architecture=yolo26_segmentation,
        ).trainer_backend
        == "ultralytics-yolo"
    )


def test_ultralytics_classification_target_paths_stay_flat_inside_class_dir() -> None:
    assert _classification_target_relative_path(
        Path("train") / "Ring A" / "7c" / "7c04a010.jpg"
    ) == Path("7c__7c04a010.jpg")
    assert _classification_target_relative_path(Path("single.jpg")) == Path("single.jpg")


def test_detection_dataset_staging_keeps_background_images(
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    snapshot_root = tmp_path / "snapshot"
    (snapshot_root / "annotations").mkdir(parents=True, exist_ok=True)
    (snapshot_root / "manifests").mkdir(parents=True, exist_ok=True)
    (snapshot_root / "images").mkdir(parents=True, exist_ok=True)

    for name in ("labeled.png", "background.png"):
        (snapshot_root / "images" / name).write_bytes(sample_png_bytes)

    canonical_dataset = {
        "dataset_id": "dst_detection",
        "task_type": "detection",
        "source_task_count": 2,
        "annotation_count": 1,
        "eligible_item_count": 2,
        "submitted_labeled_item_count": 1,
        "submitted_background_item_count": 1,
        "draft_item_count": 0,
        "invalid_item_count": 0,
        "items": [
            {
                "item_id": "itm_labeled",
                "task_id": "tsk_labeled",
                "relative_path": "labeled.png",
                "source_path": "/source/labeled.png",
                "asset_sha256": "sha-labeled",
                "split": "train",
                "eligibility_status": "eligible_labeled",
                "submitted": True,
                "background": False,
                "labels": ["defect"],
                "boxes": [
                    {
                        "class_name": "defect",
                        "x": 0.1,
                        "y": 0.1,
                        "width": 0.4,
                        "height": 0.4,
                        "original_width": 1,
                        "original_height": 1,
                    }
                ],
                "polygons": [],
                "raw_annotation_count": 1,
            },
            {
                "item_id": "itm_background",
                "task_id": "tsk_background",
                "relative_path": "background.png",
                "source_path": "/source/background.png",
                "asset_sha256": "sha-background",
                "split": "train",
                "eligibility_status": "eligible_background",
                "submitted": True,
                "background": True,
                "labels": [],
                "boxes": [],
                "polygons": [],
                "raw_annotation_count": 0,
            },
        ],
        "class_distribution": {"defect": 1},
        "label_map": ["defect"],
    }
    (snapshot_root / "annotations" / "canonical_dataset.json").write_text(
        json.dumps(canonical_dataset, indent=2),
        encoding="utf-8",
    )
    (snapshot_root / "manifests" / "validation_report.json").write_text(
        json.dumps({"status": "passed", "checks": {"images_present": True}, "issues": []}, indent=2),
        encoding="utf-8",
    )
    (snapshot_root / "manifests" / "split_metadata.json").write_text(
        json.dumps(
            {
                "strategy": "deterministic",
                "train_ratio": 80,
                "val_ratio": 10,
                "test_ratio": 10,
                "train_count": 2,
                "val_count": 0,
                "test_count": 0,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (snapshot_root / "label_map.json").write_text(json.dumps(["defect"]), encoding="utf-8")

    artifact = stage_framework_dataset(
        training_job_id="trn_stage_detection",
        task_type=TaskType.DETECTION,
        snapshot_root=snapshot_root,
        stage_root=tmp_path / "stage",
    )

    manifest = json.loads(Path(artifact.manifest_path).read_text(encoding="utf-8"))
    train_annotations = json.loads(
        Path(manifest["annotations"]["train"]).read_text(encoding="utf-8")
    )
    summary = json.loads(Path(artifact.conversion_summary_path).read_text(encoding="utf-8"))

    assert len(train_annotations["images"]) == 2
    assert len(train_annotations["annotations"]) == 1
    assert summary["splits"]["train"]["background_images"] == 1


def test_detection_dataset_staging_accepts_legacy_snapshot_items_without_eligibility_status(
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    snapshot_root = tmp_path / "snapshot"
    (snapshot_root / "annotations").mkdir(parents=True, exist_ok=True)
    (snapshot_root / "manifests").mkdir(parents=True, exist_ok=True)
    (snapshot_root / "images").mkdir(parents=True, exist_ok=True)

    for name in ("labeled.png", "background.png"):
        (snapshot_root / "images" / name).write_bytes(sample_png_bytes)

    legacy_dataset = {
        "dataset_id": "dst_detection_legacy",
        "task_type": "detection",
        "source_task_count": 2,
        "annotation_count": 1,
        "items": [
            {
                "item_id": "itm_labeled",
                "task_id": "tsk_labeled",
                "relative_path": "labeled.png",
                "source_path": "/source/labeled.png",
                "asset_sha256": "sha-labeled",
                "split": "train",
                "labels": ["defect"],
                "boxes": [
                    {
                        "class_name": "defect",
                        "x": 0.1,
                        "y": 0.1,
                        "width": 0.4,
                        "height": 0.4,
                        "original_width": 1,
                        "original_height": 1,
                    }
                ],
                "raw_annotation_count": 1,
            },
            {
                "item_id": "itm_background",
                "task_id": "tsk_background",
                "relative_path": "background.png",
                "source_path": "/source/background.png",
                "asset_sha256": "sha-background",
                "split": "train",
                "labels": [],
                "boxes": [],
                "raw_annotation_count": 0,
            },
        ],
        "class_distribution": {"defect": 1},
        "label_map": ["defect"],
    }
    (snapshot_root / "annotations" / "canonical_dataset.json").write_text(
        json.dumps(legacy_dataset, indent=2),
        encoding="utf-8",
    )
    (snapshot_root / "manifests" / "validation_report.json").write_text(
        json.dumps({"status": "passed", "checks": {"images_present": True}, "issues": []}, indent=2),
        encoding="utf-8",
    )
    (snapshot_root / "manifests" / "split_metadata.json").write_text(
        json.dumps(
            {
                "strategy": "deterministic",
                "train_ratio": 80,
                "val_ratio": 10,
                "test_ratio": 10,
                "train_count": 2,
                "val_count": 0,
                "test_count": 0,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (snapshot_root / "label_map.json").write_text(json.dumps(["defect"]), encoding="utf-8")

    artifact = stage_framework_dataset(
        training_job_id="trn_stage_detection_legacy",
        task_type=TaskType.DETECTION,
        snapshot_root=snapshot_root,
        stage_root=tmp_path / "stage",
    )

    summary = json.loads(Path(artifact.conversion_summary_path).read_text(encoding="utf-8"))

    assert summary["eligible_item_count"] == 2
    assert summary["submitted_labeled_item_count"] == 1
    assert summary["submitted_background_item_count"] == 1
    assert summary["splits"]["train"]["background_images"] == 1


def test_segmentation_dataset_staging_projects_selected_model_classes(
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    snapshot_root = tmp_path / "snapshot-segmentation-classes"
    (snapshot_root / "annotations").mkdir(parents=True, exist_ok=True)
    (snapshot_root / "manifests").mkdir(parents=True, exist_ok=True)
    (snapshot_root / "images").mkdir(parents=True, exist_ok=True)
    for name in ("mixed.png", "edge-only.png"):
        (snapshot_root / "images" / name).write_bytes(sample_png_bytes)

    def polygon(class_name: str) -> dict[str, object]:
        return {
            "class_name": class_name,
            "points": [
                {"x": 0.0, "y": 0.0},
                {"x": 1.0, "y": 0.0},
                {"x": 1.0, "y": 1.0},
                {"x": 0.0, "y": 1.0},
            ],
            "original_width": 1,
            "original_height": 1,
        }

    canonical_dataset = {
        "dataset_id": "dst_segmentation_classes",
        "task_type": "segmentation",
        "source_task_count": 2,
        "annotation_count": 3,
        "eligible_item_count": 2,
        "submitted_labeled_item_count": 2,
        "submitted_background_item_count": 0,
        "draft_item_count": 0,
        "invalid_item_count": 0,
        "items": [
            {
                "item_id": "itm_mixed",
                "task_id": "tsk_mixed",
                "relative_path": "mixed.png",
                "source_path": "/source/mixed.png",
                "asset_sha256": "sha-mixed",
                "split": "train",
                "eligibility_status": "eligible_labeled",
                "submitted": True,
                "background": False,
                "labels": [],
                "boxes": [],
                "polygons": [polygon("defect"), polygon("edge")],
                "raw_annotation_count": 2,
            },
            {
                "item_id": "itm_edge",
                "task_id": "tsk_edge",
                "relative_path": "edge-only.png",
                "source_path": "/source/edge-only.png",
                "asset_sha256": "sha-edge",
                "split": "train",
                "eligibility_status": "eligible_labeled",
                "submitted": True,
                "background": False,
                "labels": [],
                "boxes": [],
                "polygons": [polygon("edge")],
                "raw_annotation_count": 1,
            },
        ],
        "class_distribution": {"defect": 1, "edge": 2},
        "label_map": ["defect", "edge"],
    }
    (snapshot_root / "annotations" / "canonical_dataset.json").write_text(
        json.dumps(canonical_dataset, indent=2),
        encoding="utf-8",
    )
    (snapshot_root / "manifests" / "validation_report.json").write_text(
        json.dumps({"status": "passed", "checks": {"images_present": True}, "issues": []}),
        encoding="utf-8",
    )
    (snapshot_root / "manifests" / "split_metadata.json").write_text(
        json.dumps(
            {
                "strategy": "deterministic",
                "train_ratio": 100,
                "val_ratio": 0,
                "test_ratio": 0,
                "train_count": 2,
                "val_count": 0,
                "test_count": 0,
            }
        ),
        encoding="utf-8",
    )
    (snapshot_root / "label_map.json").write_text(
        json.dumps(["defect", "edge"]),
        encoding="utf-8",
    )

    artifact = stage_framework_dataset(
        training_job_id="trn_stage_segmentation_classes",
        task_type=TaskType.SEGMENTATION,
        snapshot_root=snapshot_root,
        stage_root=tmp_path / "stage-segmentation-classes",
        selected_classes=["defect"],
    )

    manifest = json.loads(Path(artifact.manifest_path).read_text(encoding="utf-8"))
    annotations = json.loads(
        Path(manifest["annotations"]["train"]).read_text(encoding="utf-8")
    )
    summary = json.loads(Path(artifact.conversion_summary_path).read_text(encoding="utf-8"))
    validation = json.loads(Path(artifact.validation_report_path).read_text(encoding="utf-8"))

    assert json.loads(Path(artifact.class_map_path).read_text(encoding="utf-8")) == ["defect"]
    assert annotations["categories"] == [
        {"id": 1, "name": "defect", "supercategory": "defect"}
    ]
    assert len(annotations["images"]) == 2
    assert len(annotations["annotations"]) == 1
    assert summary["selected_classes"] == ["defect"]
    assert summary["submitted_background_item_count"] == 1
    assert summary["splits"]["train"]["background_images"] == 1
    assert validation["training_projection"]["excluded_classes"] == ["edge"]


def test_anomaly_dataset_staging_builds_anomalib_folder_layout(
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    snapshot_root = tmp_path / "snapshot-anomaly"
    (snapshot_root / "annotations").mkdir(parents=True, exist_ok=True)
    (snapshot_root / "manifests").mkdir(parents=True, exist_ok=True)
    (snapshot_root / "images").mkdir(parents=True, exist_ok=True)

    items = [
        ("normal-train.png", "train", "normal"),
        ("normal-val.png", "val", "normal"),
        ("bad-val.png", "val", "anomalous"),
    ]
    for name, _split, _label in items:
        (snapshot_root / "images" / name).write_bytes(sample_png_bytes)

    canonical_dataset = {
        "dataset_id": "dst_anomaly",
        "task_type": "anomaly",
        "source_task_count": 3,
        "annotation_count": 3,
        "eligible_item_count": 3,
        "submitted_labeled_item_count": 3,
        "submitted_background_item_count": 0,
        "draft_item_count": 0,
        "invalid_item_count": 0,
        "items": [
            {
                "item_id": f"itm_{index}",
                "task_id": f"tsk_{index}",
                "relative_path": name,
                "source_path": f"/source/{name}",
                "asset_sha256": f"sha-{index}",
                "split": split,
                "eligibility_status": "eligible_labeled",
                "submitted": True,
                "background": False,
                "labels": [label],
                "anomaly_label": label,
                "boxes": [],
                "polygons": [],
                "raw_annotation_count": 1,
            }
            for index, (name, split, label) in enumerate(items, start=1)
        ],
        "class_distribution": {"normal": 2, "anomalous": 1},
        "anomaly_distribution": {"normal": 2, "anomalous": 1},
        "label_map": ["normal", "anomalous"],
    }
    (snapshot_root / "annotations" / "canonical_dataset.json").write_text(
        json.dumps(canonical_dataset, indent=2),
        encoding="utf-8",
    )
    (snapshot_root / "manifests" / "validation_report.json").write_text(
        json.dumps({"status": "passed", "checks": {"images_present": True}, "issues": []}, indent=2),
        encoding="utf-8",
    )
    (snapshot_root / "manifests" / "split_metadata.json").write_text(
        json.dumps(
            {
                "strategy": "deterministic",
                "train_ratio": 34,
                "val_ratio": 66,
                "test_ratio": 0,
                "train_count": 1,
                "val_count": 2,
                "test_count": 0,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (snapshot_root / "label_map.json").write_text(json.dumps(["normal", "anomalous"]), encoding="utf-8")

    artifact = stage_framework_dataset(
        training_job_id="trn_stage_anomaly",
        task_type=TaskType.ANOMALY,
        snapshot_root=snapshot_root,
        stage_root=tmp_path / "stage-anomaly",
    )

    manifest = json.loads(Path(artifact.manifest_path).read_text(encoding="utf-8"))
    summary = json.loads(Path(artifact.conversion_summary_path).read_text(encoding="utf-8"))

    assert manifest["format"] == "anomalib_folder_image_level"
    assert manifest["masks_supported"] is False
    assert (Path(manifest["anomalib_root"]) / "normal" / "normal-train.png").exists()
    assert (Path(manifest["anomalib_root"]) / "normal_test" / "normal-val.png").exists()
    assert (Path(manifest["anomalib_root"]) / "abnormal" / "bad-val.png").exists()
    assert summary["splits"]["train"] == {"normal": 1, "anomalous": 0}
    assert summary["splits"]["val"] == {"normal": 1, "anomalous": 1}


def test_artifact_validation_rejects_json_placeholders_and_marks_simulation(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "fake-checkpoint.pth"
    onnx_path = tmp_path / "fake-model.onnx"
    checkpoint_path.write_text(json.dumps({"checkpoint": "placeholder"}), encoding="utf-8")
    onnx_path.write_text(json.dumps({"onnx": "placeholder"}), encoding="utf-8")

    validation = build_artifact_validation_summary(
        checkpoint_path=checkpoint_path,
        onnx_path=onnx_path,
        trainer_mode="real",
        tensorrt_deferred=True,
    )

    assert validation.status == "failed"
    assert validation.checks["checkpoint_valid"] is False
    assert validation.checks["onnx_valid"] is False
    assert validation.details["checkpoint"]["reason"] == "json_placeholder"
    assert validation.details["onnx"]["reason"] == "json_placeholder"

    control = _CollectingControl()
    trainer = SimulationTrainer()
    simulation_result = trainer.run(
        config=TrainerConfig(
            training_job_id="trn_simulation",
            dataset_snapshot_id="dss_simulation",
            task_type=TaskType.DETECTION,
            trainer_mode="simulation",
            trainer_backend=trainer.trainer_backend,
            architecture=resolve_trainer_architecture(
                task_type=TaskType.DETECTION,
                family="yolo",
                version="v8",
                size="n",
            ),
            snapshot_root=tmp_path / "snapshot",
            workspace_dir=tmp_path / "workspace",
            configs_dir=tmp_path / "configs",
            checkpoint_dir=tmp_path / "checkpoints",
            evaluation_dir=tmp_path / "evaluation",
            onnx_dir=tmp_path / "onnx",
            tensorrt_dir=tmp_path / "tensorrt",
            max_epochs=1,
            steps_per_epoch=1,
            checkpoint_interval_steps=1,
            best_metric_name="map_50_95",
            best_metric_mode="max",
            resolved_recipe={},
            runtime_metadata={"simulation_step_sleep_seconds": 0},
        ),
        control=control,
    )

    assert simulation_result.runtime_metadata["trainer_mode"] == "simulation"
    assert simulation_result.artifact_validation is not None
    assert simulation_result.artifact_validation.status == "simulation"
    assert control.runtime_metadata[0]["simulation_only"] is True


def test_openmmlab_runtime_validation_enforces_supported_worker_stack() -> None:
    assert _parse_version_triplet("2.0.1") == (2, 0, 1)
    assert _parse_version_triplet("2.0.0rc4") == (2, 0, 0)
    assert _parse_version_triplet("unknown") is None

    _validate_supported_openmmlab_runtime(
        python_version=(3, 11),
        platform_name="linux",
        mmcv_version="2.0.1",
        mmdet_version="3.0.0",
        numpy_version="1.26.4",
        setuptools_version="79.0.1",
    )

    with pytest.raises(RuntimeError, match="Python 3.11"):
        _validate_supported_openmmlab_runtime(
            python_version=(3, 12),
            platform_name="linux",
            mmcv_version="2.0.1",
            mmdet_version="3.0.0",
            numpy_version="1.26.4",
            setuptools_version="79.0.1",
        )

    with pytest.raises(RuntimeError, match="mmcv>=2.0.0,<2.1.0"):
        _validate_supported_openmmlab_runtime(
            python_version=(3, 11),
            platform_name="linux",
            mmcv_version="2.2.0",
            mmdet_version="3.0.0",
            numpy_version="1.26.4",
            setuptools_version="79.0.1",
        )

    with pytest.raises(RuntimeError, match="mmdet>=3.0.0,<3.1.0"):
        _validate_supported_openmmlab_runtime(
            python_version=(3, 11),
            platform_name="linux",
            mmcv_version="2.0.1",
            mmdet_version="3.3.0",
            numpy_version="1.26.4",
            setuptools_version="79.0.1",
        )

    with pytest.raises(RuntimeError, match="numpy<2"):
        _validate_supported_openmmlab_runtime(
            python_version=(3, 11),
            platform_name="linux",
            mmcv_version="2.0.1",
            mmdet_version="3.0.0",
            numpy_version="2.4.4",
            setuptools_version="79.0.1",
        )

    with pytest.raises(RuntimeError, match="setuptools<81"):
        _validate_supported_openmmlab_runtime(
            python_version=(3, 11),
            platform_name="linux",
            mmcv_version="2.0.1",
            mmdet_version="3.0.0",
            numpy_version="1.26.4",
            setuptools_version="82.0.1",
        )


def test_platform_training_hook_is_registered_as_mmengine_hook() -> None:
    class FakeHook:
        def __init__(self) -> None:
            self.hook_initialized = True

    hook = _build_platform_training_hook(
        Hook=FakeHook,
        control=_CollectingControl(),
        checkpoint_dir=Path("."),
        checkpoint_interval_steps=1,
        best_metric_name="map_50_95",
        best_metric_mode="max",
        normalize_metrics=lambda metrics: metrics,
    )

    assert isinstance(hook, FakeHook)
    assert hook.hook_initialized is True
    assert hook.priority == "LOWEST"


def test_platform_training_hook_publishes_local_checkpoints_to_persistent_storage(tmp_path: Path) -> None:
    source_dir = tmp_path / "local-checkpoints"
    published_dir = tmp_path / "published-checkpoints"
    source_dir.mkdir(parents=True, exist_ok=True)
    published_dir.mkdir(parents=True, exist_ok=True)
    source_checkpoint = source_dir / "iter_30.pth"
    source_checkpoint.write_bytes(b"real-checkpoint-binary")
    (source_dir / "last_checkpoint").write_text("iter_30.pth", encoding="utf-8")

    control = _CollectingControl()
    hook = _build_platform_training_hook(
        Hook=type("FakeHook", (), {"__init__": lambda self: None}),
        control=control,
        checkpoint_dir=published_dir,
        source_checkpoint_dir=source_dir,
        checkpoint_interval_steps=1,
        best_metric_name="map_50_95",
        best_metric_mode="max",
        normalize_metrics=lambda metrics: metrics,
    )
    hook.last_epoch = 1
    hook.last_step = 30
    hook.last_validation_metrics = {"map_50_95": 0.42}

    hook._record_new_checkpoints()

    published_checkpoint = published_dir / "iter_30.pth"
    assert published_checkpoint.exists() is True
    assert published_checkpoint.read_bytes() == b"real-checkpoint-binary"
    assert (published_dir / "last_checkpoint").read_text(encoding="utf-8") == "iter_30.pth"
    assert hook.latest_source_checkpoint_path == str(source_checkpoint)
    assert hook.latest_checkpoint_path == str(published_checkpoint)
    assert control.checkpoints[0].path == str(published_checkpoint)
    assert control.checkpoints[0].metadata["sha256"]


def test_unsupported_albu_transforms_are_removed_from_generated_config() -> None:
    cfg = {
        "train_pipeline": [
            {"type": "LoadImageFromFile"},
            {"type": "mmdet.Albu", "transforms": [{"type": "Blur"}]},
            {"type": "PackDetInputs"},
        ],
        "custom_hooks": [
            {
                "type": "mmdet.PipelineSwitchHook",
                "switch_pipeline": [
                    {"type": "LoadAnnotations"},
                    {"type": "Albu"},
                    {"type": "PackDetInputs"},
                ],
            }
        ],
    }

    _strip_unsupported_albu_transforms(cfg)

    assert [item["type"] for item in cfg["train_pipeline"]] == [
        "LoadImageFromFile",
        "PackDetInputs",
    ]
    assert [item["type"] for item in cfg["custom_hooks"][0]["switch_pipeline"]] == [
        "LoadAnnotations",
        "PackDetInputs",
    ]
    assert _is_albu_transform({"type": "mmdet.Albu"}) is True
    assert _is_albu_transform({"type": "Albu"}) is True
    assert _is_albu_transform({"type": "RandomFlip"}) is False


def test_best_checkpoint_evaluation_disables_checkpoint_hook(tmp_path: Path) -> None:
    class FakeConfig:
        @classmethod
        def fromfile(cls, _path: str):  # noqa: ANN206
            return SimpleNamespace(
                default_hooks={
                    "checkpoint": {"type": "CheckpointHook", "save_best": "coco/bbox_mAP"},
                    "logger": {"type": "LoggerHook"},
                },
                load_from=None,
                resume=True,
            )

    class FakeRunner:
        captured_cfg: SimpleNamespace | None = None

        @classmethod
        def from_cfg(cls, cfg: SimpleNamespace):  # noqa: ANN206
            cls.captured_cfg = cfg
            return cls()

        def val(self) -> dict[str, float]:
            return {"coco/bbox_mAP": 0.123, "coco/bbox_mAP_50": 0.456}

    metrics = OpenMMLabDetectionTrainer()._evaluate_best_checkpoint(
        Config=FakeConfig,
        Runner=FakeRunner,
        config_path=tmp_path / "resolved_openmmlab_detection.py",
        checkpoint_path="/tmp/best.pth",
    )

    assert metrics == {"map_50_95": 0.123, "map_50": 0.456}
    assert FakeRunner.captured_cfg is not None
    assert FakeRunner.captured_cfg.load_from == "/tmp/best.pth"
    assert FakeRunner.captured_cfg.resume is False
    assert "checkpoint" not in FakeRunner.captured_cfg.default_hooks


def test_completed_resume_result_skips_training_and_uses_checkpoint_for_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "best.pth"
    onnx_path = tmp_path / "model.onnx"
    checkpoint_path.write_bytes(b"real-checkpoint")
    onnx_path.write_bytes(b"real-onnx")
    trainer = OpenMMLabDetectionTrainer()
    control = _CollectingControl()
    config = TrainerConfig(
        training_job_id="trn_recovery",
        dataset_snapshot_id="dss_recovery",
        task_type=TaskType.DETECTION,
        trainer_mode="real",
        trainer_backend=trainer.trainer_backend,
        architecture=resolve_trainer_architecture(
            task_type=TaskType.DETECTION,
            family="yolo",
            version="v8",
            size="s",
        ),
        snapshot_root=tmp_path / "snapshot",
        workspace_dir=tmp_path / "workspace",
        configs_dir=tmp_path / "configs",
        checkpoint_dir=tmp_path / "checkpoints",
        evaluation_dir=tmp_path / "evaluation",
        onnx_dir=tmp_path / "onnx",
        tensorrt_dir=tmp_path / "tensorrt",
        max_epochs=55,
        steps_per_epoch=24,
        checkpoint_interval_steps=30,
        best_metric_name="map_50_95",
        best_metric_mode="max",
        resolved_recipe={},
        runtime_metadata={},
    )

    monkeypatch.setattr(
        trainer,
        "_evaluate_best_checkpoint",
        lambda **_kwargs: {"map_50_95": 0.42, "map_50": 0.61},
    )
    monkeypatch.setattr(
        trainer,
        "_export_onnx",
        lambda **_kwargs: (onnx_path, onnx_path),
    )

    result = trainer._build_completed_resume_result(
        Config=object,
        Runner=object,
        config=config,
        control=control,
        config_path=tmp_path / "resolved.py",
        localized_resume_state=TrainerResumeState(
            epoch_no=55,
            step_no=1320,
            best_metric=0.42,
            checkpoint_path=str(checkpoint_path),
            metadata={"post_training_recovery": True},
        ),
        persistent_checkpoint_path=str(checkpoint_path),
        dataset_artifact=None,
        local_onnx_dir=tmp_path / "local-onnx",
        runtime_dependencies={"backend_versions": {}},
    )

    assert result.final_step == 1320
    assert result.best_checkpoint_path == str(checkpoint_path)
    assert result.export_artifacts[0].path == str(onnx_path)
    assert result.runtime_metadata["post_training_recovery"] is True
    assert control.runtime_metadata[0]["post_training_recovery"] is True


def test_effective_train_batch_size_is_capped_for_small_gpus() -> None:
    assert _resolve_effective_train_batch_size(
        requested_batch_size=12,
        image_size=768,
        architecture_size="s",
        total_memory_gib=8.0,
    ) == 1
    assert _resolve_effective_train_batch_size(
        requested_batch_size=4,
        image_size=640,
        architecture_size="n",
        total_memory_gib=8.0,
    ) == 2
    assert _resolve_effective_train_batch_size(
        requested_batch_size=4,
        image_size=640,
        architecture_size="n",
        total_memory_gib=None,
    ) == 4

    cfg = {"optim_wrapper": {"type": "OptimWrapper"}}
    _configure_gradient_accumulation(
        cfg=cfg,
        requested_batch_size=12,
        effective_batch_size=1,
    )
    assert cfg["optim_wrapper"]["accumulative_counts"] == 12


class _AttrDict(dict):
    def __getattr__(self, item: str):  # noqa: ANN204
        return self[item]


@pytest.mark.parametrize("size", ["n", "s", "m", "l", "x"])
def test_auto_pretraining_uses_transfer_learning_through_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: str,
) -> None:
    cache_root = tmp_path / "pretrained-cache"
    cache_root.mkdir()
    monkeypatch.setenv("CV_PLATFORM_PRETRAINED_CHECKPOINT_ROOT", str(cache_root))
    summary_path = tmp_path / "conversion_summary.json"
    summary_path.write_text(
        json.dumps({"submitted_labeled_item_count": 2000}),
        encoding="utf-8",
    )
    artifact = TrainerDatasetArtifact(
        task_type=TaskType.DETECTION,
        stage_root=str(tmp_path / "stage"),
        manifest_path=str(tmp_path / "manifest.json"),
        conversion_summary_path=str(summary_path),
        validation_report_path=str(tmp_path / "validation.json"),
        split_manifest_path=str(tmp_path / "split.json"),
        class_map_path=str(tmp_path / "class_map.json"),
    )
    trainer = OpenMMLabDetectionTrainer()
    config = TrainerConfig(
        training_job_id="trn_pretrain",
        dataset_snapshot_id="dss_pretrain",
        task_type=TaskType.DETECTION,
        trainer_mode="real",
        trainer_backend=trainer.trainer_backend,
        architecture=resolve_trainer_architecture(
            task_type=TaskType.DETECTION,
            family="yolo",
            version="v8",
            size=size,
        ),
        snapshot_root=tmp_path / "snapshot",
        workspace_dir=tmp_path / "workspace",
        configs_dir=tmp_path / "configs",
        checkpoint_dir=tmp_path / "checkpoints",
        evaluation_dir=tmp_path / "evaluation",
        onnx_dir=tmp_path / "onnx",
        tensorrt_dir=tmp_path / "tensorrt",
        max_epochs=55,
        steps_per_epoch=24,
        checkpoint_interval_steps=30,
        best_metric_name="map_50_95",
        best_metric_mode="max",
        resolved_recipe={
            "pretrained": "auto",
            "pretrained_checkpoint": "auto",
            "scratch_training_min_items": 2000,
        },
        runtime_metadata={},
    )
    requested_checkpoint = trainer._pretrained_checkpoint_for_architecture(config)
    assert requested_checkpoint is not None
    expected_checkpoint = cache_root / requested_checkpoint.rsplit("/", 1)[-1]
    expected_checkpoint.write_bytes(b"pretrained-checkpoint")

    pretraining = trainer._resolve_pretraining(
        config=config,
        dataset_artifact=artifact,
        resume_state=None,
    )

    assert _dataset_item_count(artifact) == 2000
    assert pretraining["enabled"] is True
    assert pretraining["strategy"] == "transfer_learning"
    assert pretraining["checkpoint"] == str(expected_checkpoint)
    assert pretraining["checkpoint_source"] == "local_cache"


def test_auto_pretraining_requires_local_checkpoint_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_root = tmp_path / "empty-pretrained-cache"
    cache_root.mkdir()
    monkeypatch.setenv("CV_PLATFORM_PRETRAINED_CHECKPOINT_ROOT", str(cache_root))
    summary_path = tmp_path / "conversion_summary.json"
    summary_path.write_text(
        json.dumps({"submitted_labeled_item_count": 44}),
        encoding="utf-8",
    )
    artifact = TrainerDatasetArtifact(
        task_type=TaskType.DETECTION,
        stage_root=str(tmp_path / "stage"),
        manifest_path=str(tmp_path / "manifest.json"),
        conversion_summary_path=str(summary_path),
        validation_report_path=str(tmp_path / "validation.json"),
        split_manifest_path=str(tmp_path / "split.json"),
        class_map_path=str(tmp_path / "class_map.json"),
    )
    trainer = OpenMMLabDetectionTrainer()
    config = TrainerConfig(
        training_job_id="trn_offline",
        dataset_snapshot_id="dss_offline",
        task_type=TaskType.DETECTION,
        trainer_mode="real",
        trainer_backend=trainer.trainer_backend,
        architecture=resolve_trainer_architecture(
            task_type=TaskType.DETECTION,
            family="yolo",
            version="v8",
            size="s",
        ),
        snapshot_root=tmp_path / "snapshot",
        workspace_dir=tmp_path / "workspace",
        configs_dir=tmp_path / "configs",
        checkpoint_dir=tmp_path / "checkpoints",
        evaluation_dir=tmp_path / "evaluation",
        onnx_dir=tmp_path / "onnx",
        tensorrt_dir=tmp_path / "tensorrt",
        max_epochs=55,
        steps_per_epoch=24,
        checkpoint_interval_steps=30,
        best_metric_name="map_50_95",
        best_metric_mode="max",
        resolved_recipe={
            "pretrained": "auto",
            "pretrained_checkpoint": "auto",
            "scratch_training_min_items": 2000,
        },
        runtime_metadata={},
    )

    with pytest.raises(RuntimeError, match="Transfer learning.*requires a local pretrained checkpoint"):
        trainer._resolve_pretraining(
            config=config,
            dataset_artifact=artifact,
            resume_state=None,
        )


def test_auto_pretraining_switches_to_scratch_for_large_datasets(tmp_path: Path) -> None:
    summary_path = tmp_path / "conversion_summary.json"
    summary_path.write_text(
        json.dumps({"submitted_labeled_item_count": 2500}),
        encoding="utf-8",
    )
    artifact = TrainerDatasetArtifact(
        task_type=TaskType.DETECTION,
        stage_root=str(tmp_path / "stage"),
        manifest_path=str(tmp_path / "manifest.json"),
        conversion_summary_path=str(summary_path),
        validation_report_path=str(tmp_path / "validation.json"),
        split_manifest_path=str(tmp_path / "split.json"),
        class_map_path=str(tmp_path / "class_map.json"),
    )
    trainer = OpenMMLabDetectionTrainer()
    config = TrainerConfig(
        training_job_id="trn_scratch",
        dataset_snapshot_id="dss_scratch",
        task_type=TaskType.DETECTION,
        trainer_mode="real",
        trainer_backend=trainer.trainer_backend,
        architecture=resolve_trainer_architecture(
            task_type=TaskType.DETECTION,
            family="yolo",
            version="v8",
            size="n",
        ),
        snapshot_root=tmp_path / "snapshot",
        workspace_dir=tmp_path / "workspace",
        configs_dir=tmp_path / "configs",
        checkpoint_dir=tmp_path / "checkpoints",
        evaluation_dir=tmp_path / "evaluation",
        onnx_dir=tmp_path / "onnx",
        tensorrt_dir=tmp_path / "tensorrt",
        max_epochs=55,
        steps_per_epoch=24,
        checkpoint_interval_steps=30,
        best_metric_name="map_50_95",
        best_metric_mode="max",
        resolved_recipe={
            "pretrained": "auto",
            "pretrained_checkpoint": "auto",
            "scratch_training_min_items": 2000,
        },
        runtime_metadata={},
    )

    pretraining = trainer._resolve_pretraining(
        config=config,
        dataset_artifact=artifact,
        resume_state=None,
    )

    assert pretraining["enabled"] is False
    assert pretraining["strategy"] == "scratch"
    assert pretraining["checkpoint"] is None


def test_yolov8_schedule_is_rewritten_for_short_custom_training() -> None:
    cfg = _AttrDict(
        max_epochs=500,
        close_mosaic_epochs=10,
        default_hooks={
            "param_scheduler": {"max_epochs": 500, "warmup_mim_iter": 1000},
        },
        train_cfg={"dynamic_intervals": [(490, 1)]},
        custom_hooks=[
            {"type": "mmdet.PipelineSwitchHook", "switch_epoch": 490},
        ],
    )

    _rewrite_yolov8_training_schedule(
        cfg=cfg,
        max_epochs=55,
        steps_per_epoch=24,
        recipe={"warmup_mim_iter": "auto"},
    )

    assert cfg["max_epochs"] == 55
    assert cfg.default_hooks["param_scheduler"]["max_epochs"] == 55
    assert cfg.default_hooks["param_scheduler"]["warmup_mim_iter"] == 72
    assert cfg.train_cfg["dynamic_intervals"] == [(45, 1)]
    assert cfg["custom_hooks"][0]["switch_epoch"] == 45


def test_openmmlab_early_stopping_hook_uses_best_metric_alias() -> None:
    cfg = _AttrDict(custom_hooks=[{"type": "mmdet.PipelineSwitchHook"}])

    _configure_early_stopping_hook(
        cfg=cfg,
        best_metric_name="map_50_95",
        best_metric_mode="max",
        recipe={"early_stopping_patience": 10},
    )

    hook = cfg["custom_hooks"][-1]
    assert hook == {
        "type": "EarlyStoppingHook",
        "monitor": "coco/bbox_mAP",
        "rule": "greater",
        "patience": 10,
        "min_delta": 0.0,
        "strict": False,
    }


def test_deployment_gating_rejects_simulation_artifacts_by_default() -> None:
    engine = _sqlite_engine()
    with Session(engine, expire_on_commit=False) as session:
        snapshot, job = _seed_training_job(
            session,
            snapshot_id="dss_simulation_guard",
            trainer_mode="simulation",
        )
        version = register_trained_model_version(
            session,
            training_job_id=job.id,
            metrics_summary={"map_50_95": 0.62, "map_50": 0.75},
            evaluation_summary={
                "primary_metric_name": "map_50_95",
                "primary_metric_value": 0.62,
                "metrics": {"map_50_95": 0.62, "map_50": 0.75},
                "promotion_ready": False,
                "promotion_results": [],
                "trainer_mode": "simulation",
                "trainer_backend": "cv-platform-simulation",
            },
            artifact_manifest_uri=f"/artifacts/{job.id}/artifact_manifest.json",
            artifact_manifest={"metadata": {"simulation_only": True}},
            export_manifest_uri=f"/artifacts/{job.id}/export_manifest.json",
            export_manifest={
                "trainer_mode": "simulation",
                "trainer_backend": "cv-platform-simulation",
                "artifact_validation": {
                    "status": "simulation",
                    "checks": {"simulation_only": True},
                    "details": {"simulation_only": True},
                },
            },
            onnx_uri=None,
            tensorrt_uri=None,
            dvc_rev=snapshot.dvc_rev,
            dvc_references={"bundle_dvc_file": f"registry/{job.id}.dvc"},
            reproducibility_metadata={},
            code_revision="git-test-rev",
            mlflow_run_id=f"mlf-{job.id}",
            mlflow_registered_model=job.model_family_slug,
            mlflow_model_version="1",
            alias_names=["simulation-only"],
            registry_tags={"cv_platform.lifecycle": "trained", "cv_platform.trainer_mode": "simulation"},
            immutable_metadata={
                "trainer_mode": "simulation",
                "trainer_backend": "cv-platform-simulation",
                "artifact_validation": {
                    "status": "simulation",
                    "checks": {"simulation_only": True},
                    "details": {"simulation_only": True},
                },
            },
            immutable_metadata_sha256=f"sha-{job.id}",
        )

        with pytest.raises(
            ValueError,
            match="Simulation-trained model versions cannot be approved or promoted for deployment",
        ):
            record_deployment_approval(
                session,
                model_version_id=version.id,
                target_path="realtime",
                approval_scope=DeploymentApprovalScope.STAGING.value,
                status=DeploymentApprovalStatus.APPROVED.value,
                decided_by="tester",
                notes="should fail",
            )
