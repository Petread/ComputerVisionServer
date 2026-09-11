from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from cv_core.settings import PlatformSettings, ServiceRuntimeConfig, StorageRoots
from cv_status import TaskType
from cv_training.recipes import load_training_recipe
from cv_training.trainer import TrainerArtifactValidation, TrainerExportArtifact, TrainerResult
from training_worker import jobs, runtime
from training_worker.runtime import TerminalTrainingError


def test_training_worker_runtime_imports_without_bytecode_loader() -> None:
    source_path = Path(runtime.__file__)
    source = source_path.read_text(encoding="utf-8")

    assert hasattr(runtime, "run_training_workflow")
    assert hasattr(jobs, "run_training_job")
    assert "__pycache__" not in source
    assert "runtime.legacy" not in source
    assert "SourcelessFileLoader" not in source


def test_cancelled_ultralytics_result_with_onnx_is_deployable() -> None:
    result = TrainerResult(
        final_epoch=8,
        final_step=80,
        best_metric_name="mask_map_50_95",
        best_metric_value=0.72,
        best_metrics={"mask_map_50_95": 0.72},
        final_validation_metrics={"mask_map_50_95": 0.72},
        export_artifacts=(
            TrainerExportArtifact(
                kind="onnx",
                path="/tmp/model.onnx",
                generated=True,
                status="passed",
            ),
        ),
        best_checkpoint_path="/tmp/best.pt",
        latest_checkpoint_path="/tmp/last.pt",
        cancelled=True,
    )

    assert runtime._cancelled_result_is_deployable(
        result=result,
        trainer=SimpleNamespace(trainer_backend="ultralytics-yolo"),
    )
    assert not runtime._cancelled_result_is_deployable(
        result=result,
        trainer=SimpleNamespace(trainer_backend="torchvision-segmentation"),
    )


def test_detection_baseline_recipe_loads_from_packaged_resource(tmp_path: Path) -> None:
    recipe = load_training_recipe(
        workspace_root=tmp_path,
        task_type=TaskType.DETECTION,
        recipe_name="baseline_det",
    )

    assert recipe["recipe_name"] == "baseline_det"
    assert recipe["recipe_version"] == "1.2.0"
    assert recipe["trainer_backend"] == "openmmlab-mmyolo"
    assert recipe["pretrained"] == "auto"
    assert recipe["pretrained_checkpoint"] == "auto"
    assert recipe["scratch_training_min_items"] == 2000
    assert recipe["learning_rate"] == 0.01
    assert recipe["early_stopping_patience"] == 40
    assert recipe["early_stopping_min_delta"] == 0.0
    assert recipe["_recipe_source"].startswith("package://cv_training/")


def test_mmyolo_detection_recipe_loads_from_packaged_resource(tmp_path: Path) -> None:
    recipe = load_training_recipe(
        workspace_root=tmp_path,
        task_type=TaskType.DETECTION,
        recipe_name="baseline_det_mmyolo",
    )

    assert recipe["recipe_name"] == "baseline_det_mmyolo"
    assert recipe["recipe_version"] == "1.2.0"
    assert recipe["trainer_backend"] == "openmmlab-mmyolo"
    assert recipe["framework"] == "openmmlab-mmyolo"
    assert recipe["learning_rate"] == 0.01
    assert recipe["early_stopping_patience"] == 40
    assert recipe["early_stopping_min_delta"] == 0.0
    assert recipe["_recipe_source"].startswith("package://cv_training/")


@pytest.mark.parametrize(
    ("task_type", "recipe_name", "trainer_backend", "best_metric"),
    [
        (
            TaskType.CLASSIFICATION,
            "baseline_cls_torchvision",
            "torchvision-classification",
            "f1_macro",
        ),
        (
            TaskType.SEGMENTATION,
            "baseline_seg_torchvision",
            "torchvision-segmentation",
            "mask_map_50_95",
        ),
    ],
)
def test_torchvision_baseline_recipes_load_from_packaged_resources(
    tmp_path: Path,
    task_type: TaskType,
    recipe_name: str,
    trainer_backend: str,
    best_metric: str,
) -> None:
    recipe = load_training_recipe(
        workspace_root=tmp_path,
        task_type=task_type,
        recipe_name=recipe_name,
    )

    assert recipe["recipe_name"] == recipe_name
    assert recipe["recipe_version"] == "1.2.0"
    assert recipe["framework"] == "torchvision"
    assert recipe["trainer_backend"] == trainer_backend
    assert recipe["best_metric_name"] == best_metric
    assert recipe["pretrained"] == "auto"
    assert recipe["scratch_training_min_items"] == 2000
    assert recipe["early_stopping_patience"] == 30
    assert recipe["early_stopping_min_delta"] == 0.0
    assert recipe["_recipe_source"].startswith("package://cv_training/")


@pytest.mark.parametrize(
    ("task_type", "recipe_name", "trainer_backend", "patience"),
    [
        (TaskType.CLASSIFICATION, "baseline_cls_yolo", "ultralytics-yolo", 100),
        (TaskType.DETECTION, "baseline_det_yolo", "ultralytics-yolo", 50),
        (TaskType.SEGMENTATION, "baseline_seg_yolo", "ultralytics-yolo", 60),
    ],
)
def test_ultralytics_baseline_recipes_load_from_packaged_resources(
    tmp_path: Path,
    task_type: TaskType,
    recipe_name: str,
    trainer_backend: str,
    patience: int,
) -> None:
    recipe = load_training_recipe(
        workspace_root=tmp_path,
        task_type=task_type,
        recipe_name=recipe_name,
    )

    assert recipe["recipe_name"] == recipe_name
    assert recipe["recipe_version"] == "1.2.0"
    assert recipe["framework"] == "ultralytics"
    assert recipe["trainer_backend"] == trainer_backend
    assert recipe["pretrained"] == "auto"
    assert recipe["scratch_training_min_items"] == 2000
    if recipe_name in {"baseline_det_yolo", "baseline_seg_yolo"}:
        assert recipe["learning_rate"] == 0.01
    assert recipe["early_stopping_patience"] == patience
    assert recipe["early_stopping_min_delta"] == 0.0
    assert recipe["_recipe_source"].startswith("package://cv_training/")


def test_segmentation_export_manifest_enables_nms() -> None:
    nms = runtime._build_nms(
        {
            "nms_enabled": True,
            "nms_iou_threshold": 0.65,
            "max_detections": 200,
            "score_threshold": 0.25,
        },
        task_type=TaskType.SEGMENTATION,
    )

    assert nms == {
        "enabled": True,
        "iou_threshold": 0.65,
        "max_detections": 200,
        "score_threshold": 0.25,
    }


def test_anomaly_baseline_recipe_loads_from_packaged_resource(tmp_path: Path) -> None:
    recipe = load_training_recipe(
        workspace_root=tmp_path,
        task_type=TaskType.ANOMALY,
        recipe_name="baseline_anomaly",
    )

    assert recipe["recipe_name"] == "baseline_anomaly"
    assert recipe["recipe_version"] == "1.0.0"
    assert recipe["framework"] == "anomalib"
    assert recipe["trainer_backend"] == "anomalib-patchcore"
    assert recipe["method"] == "patchcore"
    assert recipe["backbone"] == "wide_resnet50_2"
    assert recipe["best_metric_name"] == "image_auroc"
    assert recipe["postprocessing"]["heatmap_support"] is True
    assert recipe["postprocessing"]["mask_support"] is False
    assert recipe["_recipe_source"].startswith("package://cv_training/")


def test_missing_recipe_error_lists_actionable_search_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Searched:"):
        load_training_recipe(
            workspace_root=tmp_path,
            task_type=TaskType.DETECTION,
            recipe_name="does_not_exist",
        )


def test_runtime_blocks_unsupported_architecture() -> None:
    context = SimpleNamespace(
        task_type=TaskType.CLASSIFICATION,
        architecture_family="yolo",
        architecture_version="v8",
        architecture_size="n",
        architecture_display_name="YOLOv8 Nano Classifier",
    )

    with pytest.raises(TerminalTrainingError) as exc_info:
        runtime._resolve_supported_architecture(context)

    assert exc_info.value.failure_class == "unsupported_architecture"
    assert "Classification real training supports" in str(exc_info.value)


def test_openmmlab_dependency_preflight_reports_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = PlatformSettings(
        service=ServiceRuntimeConfig(name="training-worker"),
        storage=StorageRoots(
            state_root=str(tmp_path / "state"),
            training_root=str(tmp_path / "state" / "training"),
            serving_root=str(tmp_path / "state" / "serving"),
            audit_root=str(tmp_path / "state" / "audit"),
            logs_root=str(tmp_path / "state" / "logs"),
        ),
    )

    def fake_package_status(module_name: str) -> dict[str, object]:
        return {
            "available": module_name != "mmcv",
            "version": "2.0.1" if module_name == "mmcv" else "1.0.0",
        }

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.sys, "version_info", (3, 11, 0))
    monkeypatch.setattr(runtime, "_package_status", fake_package_status)
    monkeypatch.setattr(runtime, "_torch_cuda_status", lambda _available: {"cuda_available": True})
    monkeypatch.setattr(runtime, "_available_mmyolo_config_files", lambda: ["configs/yolov8/test.py"])

    with pytest.raises(TerminalTrainingError) as exc_info:
        runtime.collect_dependency_preflight(
            settings=settings,
            workspace_root=tmp_path,
            task_type=TaskType.DETECTION,
            recipe_name="baseline_det",
            snapshot_root=tmp_path / "snapshot",
            require_openmmlab=True,
        )

    assert exc_info.value.failure_class == "dependency_missing"
    assert "mmcv" in str(exc_info.value)


def test_torchvision_dependency_preflight_reports_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = PlatformSettings(
        service=ServiceRuntimeConfig(name="training-worker"),
        storage=StorageRoots(
            state_root=str(tmp_path / "state"),
            training_root=str(tmp_path / "state" / "training"),
            serving_root=str(tmp_path / "state" / "serving"),
            audit_root=str(tmp_path / "state" / "audit"),
            logs_root=str(tmp_path / "state" / "logs"),
        ),
    )

    def fake_package_status(module_name: str) -> dict[str, object]:
        return {
            "available": module_name != "torchvision",
            "version": "1.0.0",
        }

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.sys, "version_info", (3, 11, 0))
    monkeypatch.setattr(runtime, "_package_status", fake_package_status)
    monkeypatch.setattr(runtime, "_torch_cuda_status", lambda _available: {"cuda_available": True})

    with pytest.raises(TerminalTrainingError) as exc_info:
        runtime.collect_dependency_preflight(
            settings=settings,
            workspace_root=tmp_path,
            task_type=TaskType.CLASSIFICATION,
            recipe_name="baseline_cls",
            snapshot_root=tmp_path / "snapshot",
            require_torchvision=True,
        )

    assert exc_info.value.failure_class == "dependency_missing"
    assert "torchvision" in str(exc_info.value)


def test_ultralytics_dependency_preflight_reports_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = PlatformSettings(
        service=ServiceRuntimeConfig(name="training-worker"),
        storage=StorageRoots(
            state_root=str(tmp_path / "state"),
            training_root=str(tmp_path / "state" / "training"),
            serving_root=str(tmp_path / "state" / "serving"),
            audit_root=str(tmp_path / "state" / "audit"),
            logs_root=str(tmp_path / "state" / "logs"),
        ),
    )

    def fake_package_status(module_name: str) -> dict[str, object]:
        return {
            "available": module_name != "ultralytics",
            "version": "1.0.0",
        }

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.sys, "version_info", (3, 11, 0))
    monkeypatch.setattr(runtime, "_package_status", fake_package_status)
    monkeypatch.setattr(runtime, "_torch_cuda_status", lambda _available: {"cuda_available": True})

    with pytest.raises(TerminalTrainingError) as exc_info:
        runtime.collect_dependency_preflight(
            settings=settings,
            workspace_root=tmp_path,
            task_type=TaskType.DETECTION,
            recipe_name="baseline_det",
            snapshot_root=tmp_path / "snapshot",
            require_ultralytics=True,
        )

    assert exc_info.value.failure_class == "dependency_missing"
    assert "ultralytics" in str(exc_info.value)


def test_real_runtime_refuses_simulation_result() -> None:
    result = TrainerResult(
        final_epoch=1,
        final_step=1,
        best_metric_name="map_50_95",
        best_metric_value=0.7,
        best_metrics={"map_50_95": 0.7},
        final_validation_metrics={"map_50_95": 0.7},
        artifact_validation=TrainerArtifactValidation(
            status="simulation",
            checks={"simulation_only": True},
            details={"simulation_only": True},
        ),
        runtime_metadata={"trainer_mode": "simulation", "simulation_only": True},
    )
    trainer = SimpleNamespace(trainer_mode="real", trainer_backend="openmmlab-mmyolo")

    with pytest.raises(TerminalTrainingError) as exc_info:
        runtime._guard_against_simulation_result(result=result, trainer=trainer)

    assert exc_info.value.failure_class == "simulation_not_allowed"


def test_io_schema_uses_exported_onnx_tensor_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime,
        "_onnx_graph_io_schema",
        lambda _path: {
            "input_names": ["images"],
            "outputs": [{"name": "output0", "shape": ["batch", 4], "data_type": "FP32"}],
        },
    )

    schema = runtime._build_io_schema(
        TaskType.CLASSIFICATION,
        image_size=960,
        class_count=4,
        onnx_path=Path("model.onnx"),
    )

    assert schema["input_tensors"][0]["name"] == "images"
    assert schema["output_tensors"][0]["name"] == "output0"
    assert schema["output_tensors"][0]["dims"] == [4]


def test_ultralytics_preprocessing_uses_zero_to_one_scaling() -> None:
    preprocessing = runtime._build_preprocessing(
        {
            "image_size": 960,
            "normalize": {
                "mean": [0.485, 0.456, 0.406],
                "std": [0.229, 0.224, 0.225],
            },
        },
        trainer_backend="ultralytics-yolo",
    )

    assert preprocessing["resize"] == 960
    assert preprocessing["normalize"] == {"mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]}
    assert preprocessing["scale"] == pytest.approx(1 / 255)


def test_ultralytics_segmentation_preprocessing_records_training_letterbox() -> None:
    preprocessing = runtime._build_preprocessing(
        {
            "image_size": 960,
            "task_type": TaskType.SEGMENTATION.value,
        },
        trainer_backend="ultralytics-yolo",
    )

    assert preprocessing["resize_mode"] == "letterbox"
    assert preprocessing["letterbox"] == {
        "enabled": True,
        "padding_value": 114,
        "center": True,
    }


def test_snapshot_image_view_preprocessing_loads_versioned_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "snapshot_manifest.json"
    manifest_path.write_text(
        '{"image_view":{"view_profile_id":"stem-view-v1","input_width":1600}}',
        encoding="utf-8",
    )

    assert runtime._load_snapshot_image_view_preprocessing(manifest_path) == {
        "image_view": {
            "view_profile_id": "stem-view-v1",
            "input_width": 1600,
        }
    }


def test_snapshot_image_view_preprocessing_rejects_non_object_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "snapshot_manifest.json"
    manifest_path.write_text("[]", encoding="utf-8")

    with pytest.raises(TerminalTrainingError) as exc_info:
        runtime._load_snapshot_image_view_preprocessing(manifest_path)

    assert exc_info.value.failure_class == "dataset_invalid"
    assert "must contain a JSON object" in str(exc_info.value)


def test_openmmlab_mmyolo_preprocessing_matches_data_preprocessor_scaling() -> None:
    preprocessing = runtime._build_preprocessing(
        {
            "image_size": 960,
            "normalize": {
                "mean": [123.675, 116.28, 103.53],
                "std": [58.395, 57.12, 57.375],
            },
        },
        trainer_backend="openmmlab-mmyolo",
    )

    assert preprocessing["resize"] == 960
    assert preprocessing["color_format"] == "rgb"
    assert preprocessing["normalize"] == {
        "mean": [0.0, 0.0, 0.0],
        "std": [255.0, 255.0, 255.0],
    }


def test_runtime_classifies_registry_alias_errors() -> None:
    assert (
        runtime._classify_exception(RuntimeError("'latest' alias name is reserved."))
        == "registry_failed"
    )


def test_runtime_detects_completed_progress_for_registration_recovery() -> None:
    assert runtime._progress_indicates_training_complete({"progress_percent": 100.0}) is True
    assert (
        runtime._progress_indicates_training_complete(
            {"current_step": 1320, "total_steps": 1320}
        )
        is True
    )
    assert (
        runtime._progress_indicates_training_complete(
            {"progress_percent": 42.0, "current_step": 10, "total_steps": 1320}
        )
        is False
    )
