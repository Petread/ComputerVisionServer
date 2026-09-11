from __future__ import annotations

import json
from pathlib import Path

import pytest
from cv_status import TaskType
from cv_training.backends import ultralytics_yolo as yolo_backend
from cv_training.trainer import TrainerConfig, resolve_trainer_architecture
from cv_training.trainer_types import TrainerDatasetArtifact


@pytest.mark.parametrize(
    "task_type",
    [TaskType.CLASSIFICATION, TaskType.DETECTION, TaskType.SEGMENTATION],
)
@pytest.mark.parametrize("version", ["v11", "v26"])
@pytest.mark.parametrize("size", ["n", "s", "m", "l", "x"])
def test_auto_pretraining_uses_transfer_learning_below_threshold_for_all_yolo_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_type: TaskType,
    version: str,
    size: str,
) -> None:
    config = _config(tmp_path, task_type, version, size)
    cache_root = tmp_path / "pretrained-cache"
    cache_root.mkdir()
    expected_checkpoint = cache_root / f"{yolo_backend._ultralytics_model_stem(config)}.pt"
    expected_checkpoint.write_bytes(b"pretrained-weights")
    monkeypatch.setenv("CV_PLATFORM_PRETRAINED_CHECKPOINT_ROOT", str(cache_root))

    source, pretraining = yolo_backend._resolve_model_source(
        config=config,
        dataset_artifact=_artifact_with_item_count(tmp_path, item_count=1999),
        resume_state=None,
    )

    assert source == str(expected_checkpoint)
    assert pretraining["enabled"] is True
    assert pretraining["strategy"] == "transfer_learning"
    assert pretraining["checkpoint_source"] == "local_cache"
    assert pretraining["dataset_items"] == 1999
    assert pretraining["scratch_training_min_items"] == 2000


@pytest.mark.parametrize(
    "task_type",
    [TaskType.CLASSIFICATION, TaskType.DETECTION, TaskType.SEGMENTATION],
)
@pytest.mark.parametrize("version", ["v11", "v26"])
@pytest.mark.parametrize("size", ["n", "s", "m", "l", "x"])
def test_auto_pretraining_switches_to_scratch_above_threshold_for_all_yolo_variants(
    tmp_path: Path,
    task_type: TaskType,
    version: str,
    size: str,
) -> None:
    config = _config(tmp_path, task_type, version, size)
    source, pretraining = yolo_backend._resolve_model_source(
        config=config,
        dataset_artifact=_artifact_with_item_count(tmp_path, item_count=2001),
        resume_state=None,
    )

    assert source.endswith(".yaml")
    assert pretraining["enabled"] is False
    assert pretraining["strategy"] == "scratch"
    assert pretraining["dataset_items"] == 2001
    assert pretraining["scratch_training_min_items"] == 2000


def test_ultralytics_model_names_match_task_variants(tmp_path: Path) -> None:
    assert _config(tmp_path, TaskType.DETECTION, "v26", "n").architecture.supported is True
    assert yolo_backend._ultralytics_model_stem(
        _config(tmp_path, TaskType.DETECTION, "v26", "n")
    ) == "yolo26n"
    assert yolo_backend._ultralytics_model_stem(
        _config(tmp_path, TaskType.ORIENTED_DETECTION, "v26", "n")
    ) == "yolo26n-obb"
    assert yolo_backend._ultralytics_model_stem(
        _config(tmp_path, TaskType.SEGMENTATION, "v11", "x")
    ) == "yolo11x-seg"
    assert yolo_backend._ultralytics_model_stem(
        _config(tmp_path, TaskType.SEGMENTATION, "v8", "n")
    ) == "yolov8n-seg"
    assert yolo_backend._ultralytics_model_stem(
        _config(tmp_path, TaskType.CLASSIFICATION, "v26", "s")
    ) == "yolo26s-cls"


def test_yolo_label_conversion_writes_detection_and_segmentation_formats(tmp_path: Path) -> None:
    detection = yolo_backend._annotation_to_yolo_line(
        annotation={"category_id": 2, "bbox": [10, 20, 30, 40]},
        task_type=TaskType.DETECTION,
        width=100,
        height=200,
        class_count=3,
    )
    segmentation = yolo_backend._annotation_to_yolo_line(
        annotation={"category_id": 1, "segmentation": [[10, 20, 30, 20, 30, 40]]},
        task_type=TaskType.SEGMENTATION,
        width=100,
        height=200,
        class_count=3,
    )

    assert detection == "1 0.25 0.2 0.3 0.2"
    assert segmentation == "0 0.1 0.1 0.3 0.1 0.3 0.2"


def test_prepare_yolo_dataset_converts_coco_to_ultralytics_yaml(tmp_path: Path) -> None:
    stage_root = tmp_path / "stage"
    image_root = stage_root / "train" / "images"
    image_root.mkdir(parents=True)
    (image_root / "sample.jpg").write_bytes(b"fake-image")
    (stage_root / "val" / "images").mkdir(parents=True)
    (stage_root / "test" / "images").mkdir(parents=True)
    annotations_root = stage_root / "annotations"
    annotations_root.mkdir(parents=True)
    _write_coco(annotations_root / "instances_train.json", include_annotation=True)
    _write_coco(annotations_root / "instances_val.json", include_annotation=False, images=[])
    _write_coco(annotations_root / "instances_test.json", include_annotation=False, images=[])
    class_map_path = stage_root / "class_map.json"
    class_map_path.write_text(json.dumps(["ok", "defect"]), encoding="utf-8")
    artifact = TrainerDatasetArtifact(
        task_type=TaskType.DETECTION,
        stage_root=str(stage_root),
        manifest_path=str(stage_root / "dataset_manifest.json"),
        conversion_summary_path=str(stage_root / "conversion_summary.json"),
        validation_report_path=str(stage_root / "validation_report.json"),
        split_manifest_path=str(stage_root / "split_manifest.json"),
        class_map_path=str(class_map_path),
        metadata={
            "images_root": {
                "train": str(stage_root / "train" / "images"),
                "val": str(stage_root / "val" / "images"),
                "test": str(stage_root / "test" / "images"),
            }
        },
    )

    spec = yolo_backend._prepare_yolo_label_dataset(
        config=_config(tmp_path, TaskType.DETECTION, "v26", "n"),
        dataset_artifact=artifact,
    )

    assert Path(spec.data_arg).name == "data.yaml"
    assert (spec.root / "images" / "train" / "sample.jpg").exists()
    assert (spec.root / "labels" / "train" / "sample.txt").read_text(encoding="utf-8")
    assert spec.metadata["splits"]["train"] == {"images": 1, "labels": 1}


def test_prepare_yolo_obb_dataset_uses_staged_labels_directly(tmp_path: Path) -> None:
    stage_root = tmp_path / "stage-obb"
    image_root = stage_root / "train" / "images"
    label_root = stage_root / "train" / "labels"
    image_root.mkdir(parents=True)
    label_root.mkdir(parents=True)
    (image_root / "sample.jpg").write_bytes(b"fake-image")
    (label_root / "sample.txt").write_text("0 0.1 0.1 0.4 0.1 0.4 0.3 0.1 0.3\n", encoding="utf-8")
    for split in ("val", "test"):
        (stage_root / split / "images").mkdir(parents=True)
        (stage_root / split / "labels").mkdir(parents=True)
    class_map_path = stage_root / "class_map.json"
    class_map_path.write_text(json.dumps(["scratch"]), encoding="utf-8")
    artifact = TrainerDatasetArtifact(
        task_type=TaskType.ORIENTED_DETECTION,
        stage_root=str(stage_root),
        manifest_path=str(stage_root / "dataset_manifest.json"),
        conversion_summary_path=str(stage_root / "conversion_summary.json"),
        validation_report_path=str(stage_root / "validation_report.json"),
        split_manifest_path=str(stage_root / "split_manifest.json"),
        class_map_path=str(class_map_path),
        metadata={
            "images_root": {
                "train": str(stage_root / "train" / "images"),
                "val": str(stage_root / "val" / "images"),
                "test": str(stage_root / "test" / "images"),
            },
            "labels_root": {
                "train": str(stage_root / "train" / "labels"),
                "val": str(stage_root / "val" / "labels"),
                "test": str(stage_root / "test" / "labels"),
            },
        },
    )

    spec = yolo_backend._prepare_yolo_obb_label_dataset(
        config=_config(tmp_path, TaskType.ORIENTED_DETECTION, "v26", "n"),
        dataset_artifact=artifact,
    )

    assert spec.metadata["format"] == "ultralytics_yolo_obb_labels"
    assert (spec.root / "images" / "train" / "sample.jpg").exists()
    assert (spec.root / "labels" / "train" / "sample.txt").read_text(encoding="utf-8").startswith("0 0.1")


def test_results_csv_metrics_are_normalized_for_tasks(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "results.csv").write_text(
        "epoch,metrics/mAP50(B),metrics/mAP50-95(B),metrics/recall(B)\n"
        "0,0.8,0.6,0.7\n",
        encoding="utf-8",
    )

    assert yolo_backend._read_results_metrics(
        run_dir=run_dir,
        task_type=TaskType.DETECTION,
    ) == {"map_50": 0.8, "map_50_95": 0.6, "recall_at_100": 0.7}


def test_results_csv_metrics_are_normalized_for_oriented_detection(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-obb"
    run_dir.mkdir()
    (run_dir / "results.csv").write_text(
        "epoch,metrics/mAP50(OBB),metrics/mAP50-95(OBB),metrics/recall(OBB)\n"
        "0,0.81,0.61,0.71\n",
        encoding="utf-8",
    )

    assert yolo_backend._read_results_metrics(
        run_dir=run_dir,
        task_type=TaskType.ORIENTED_DETECTION,
    ) == {"map_50": 0.81, "map_50_95": 0.61, "recall_at_100": 0.71}



def test_results_csv_metrics_snapshot_selects_best_and_latest_rows(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "results.csv").write_text(
        "epoch,metrics/accuracy_top1,metrics/accuracy_top5\n"
        "1,0.95,1.0\n"
        "2,0.40,0.8\n",
        encoding="utf-8",
    )

    snapshot = yolo_backend._read_results_metrics_snapshot(
        run_dir=run_dir,
        task_type=TaskType.CLASSIFICATION,
        best_metric_name="accuracy_top1",
        best_metric_mode="max",
    )

    assert snapshot.best["accuracy_top1"] == 0.95
    assert snapshot.latest["accuracy_top1"] == 0.40
    assert snapshot.best_epoch == 1
    assert snapshot.latest_epoch == 2


def test_publish_checkpoints_reports_best_and_latest_epochs_separately(tmp_path: Path) -> None:
    config = _config(tmp_path, TaskType.CLASSIFICATION, "v11", "s")
    run_dir = tmp_path / "run"
    weights_dir = run_dir / "weights"
    weights_dir.mkdir(parents=True)
    (weights_dir / "best.pt").write_bytes(b"best")
    (weights_dir / "last.pt").write_bytes(b"last")

    checkpoints = yolo_backend._publish_checkpoints(
        config=config,
        run_dir=run_dir,
        best_metrics={"accuracy_top1": 0.96},
        latest_metrics={"accuracy_top1": 0.88},
        best_epoch_no=34,
        latest_epoch_no=60,
        final_epoch_no=60,
        final_step_no=600,
    )

    best_checkpoint = next(item for item in checkpoints if item.is_best)
    latest_checkpoint = next(item for item in checkpoints if item.checkpoint_kind == "last")
    assert best_checkpoint.epoch_no == 34
    assert best_checkpoint.step_no == 340
    assert "epoch_0034_step_000340" in Path(best_checkpoint.path).name
    assert latest_checkpoint.epoch_no == 60
    assert latest_checkpoint.step_no == 600
    assert "epoch_0060_step_000600" in Path(latest_checkpoint.path).name


def test_select_checkpoint_for_export_prefers_best_and_falls_back_to_last(tmp_path: Path) -> None:
    config = _config(tmp_path, TaskType.SEGMENTATION, "v11", "s")
    run_dir = tmp_path / "run"
    weights_dir = run_dir / "weights"
    weights_dir.mkdir(parents=True)
    (weights_dir / "best.pt").write_bytes(b"best")
    (weights_dir / "last.pt").write_bytes(b"last")

    checkpoints = yolo_backend._publish_checkpoints(
        config=config,
        run_dir=run_dir,
        best_metrics={"mask_map_50_95": 0.81},
        latest_metrics={"mask_map_50_95": 0.74},
        best_epoch_no=12,
        latest_epoch_no=18,
        final_epoch_no=18,
        final_step_no=180,
    )

    best, latest, selected = yolo_backend._select_checkpoint_for_export(checkpoints)

    assert best is not None
    assert latest is not None
    assert selected == best
    assert Path(selected.path).read_bytes() == b"best"

    only_last = tuple(item for item in checkpoints if item.checkpoint_kind == "last")
    best, latest, selected = yolo_backend._select_checkpoint_for_export(only_last)

    assert best is None
    assert latest is not None
    assert selected == latest
    assert Path(selected.path).read_bytes() == b"last"


def test_build_train_kwargs_maps_early_stopping_patience_to_ultralytics(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, TaskType.CLASSIFICATION, "v11", "n")
    config = TrainerConfig(
        **{
            **config.__dict__,
            "resolved_recipe": {
                **config.resolved_recipe,
                "early_stopping_patience": 7,
            },
        }
    )
    dataset_spec = yolo_backend._UltralyticsDatasetSpec(
        data_arg=str(tmp_path / "data.yaml"),
        root=tmp_path,
        metadata={},
    )

    train_kwargs = yolo_backend._build_train_kwargs(
        config=config,
        dataset_spec=dataset_spec,
        resume_state=None,
    )

    assert train_kwargs["patience"] == 7


def test_disable_ultralytics_mlflow_callbacks_preserves_platform_callbacks() -> None:
    def platform_callback() -> None:
        pass

    def mlflow_callback() -> None:
        pass

    mlflow_callback.__module__ = "ultralytics.utils.callbacks.mlflow"

    class DummyModel:
        callbacks = {
            "on_train_epoch_end": [platform_callback, mlflow_callback],
            "on_fit_epoch_end": [mlflow_callback],
        }

    model = DummyModel()

    yolo_backend._disable_ultralytics_mlflow_callbacks(model=model)

    assert model.callbacks["on_train_epoch_end"] == [platform_callback]
    assert model.callbacks["on_fit_epoch_end"] == []


def _config(tmp_path: Path, task_type: TaskType, version: str, size: str) -> TrainerConfig:
    root = tmp_path / f"job-{task_type.value}-{version}-{size}"
    for directory in ("workspace", "configs", "checkpoints", "evaluation", "onnx", "tensorrt"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    best_metric = {
        TaskType.CLASSIFICATION: "accuracy_top1",
        TaskType.DETECTION: "map_50_95",
        TaskType.ORIENTED_DETECTION: "map_50_95",
        TaskType.SEGMENTATION: "mask_map_50_95",
    }[task_type]
    architecture = resolve_trainer_architecture(
        task_type=task_type,
        family="yolo",
        version=version,
        size=size,
    )
    return TrainerConfig(
        training_job_id=f"trn_{task_type.value}",
        dataset_snapshot_id=f"dss_{task_type.value}",
        task_type=task_type,
        trainer_mode="real",
        trainer_backend="ultralytics-yolo",
        architecture=architecture,
        snapshot_root=tmp_path / "snapshot-placeholder",
        workspace_dir=root / "workspace",
        configs_dir=root / "configs",
        checkpoint_dir=root / "checkpoints",
        evaluation_dir=root / "evaluation",
        onnx_dir=root / "onnx",
        tensorrt_dir=root / "tensorrt",
        max_epochs=1,
        steps_per_epoch=1,
        checkpoint_interval_steps=1,
        best_metric_name=best_metric,
        best_metric_mode="max",
        resolved_recipe={
            "recipe_name": f"baseline_{task_type.value}",
            "epochs": 1,
            "image_size": 64,
            "batch_size": 1,
            "learning_rate": 0.001,
            "optimizer": "sgd",
            "optimizer_settings": {"momentum": 0.9, "weight_decay": 0.0005},
        },
        runtime_metadata={},
        image_size=64,
        batch_size=1,
        learning_rate=0.001,
        optimizer="sgd",
        optimizer_settings={},
        gpu_device="cpu",
    )


def _write_coco(path: Path, *, include_annotation: bool, images: list[dict] | None = None) -> None:
    image_rows = images if images is not None else [
        {"id": 1, "file_name": "sample.jpg", "width": 100, "height": 100}
    ]
    annotations = (
        [{"id": 1, "image_id": 1, "category_id": 2, "bbox": [10, 20, 30, 40], "iscrowd": 0}]
        if include_annotation
        else []
    )
    path.write_text(
        json.dumps(
            {
                "images": image_rows,
                "annotations": annotations,
                "categories": [
                    {"id": 1, "name": "ok"},
                    {"id": 2, "name": "defect"},
                ],
            }
        ),
        encoding="utf-8",
    )


def _artifact_with_item_count(tmp_path: Path, *, item_count: int) -> TrainerDatasetArtifact:
    stage_root = tmp_path / f"artifact-{item_count}"
    stage_root.mkdir(parents=True, exist_ok=True)
    conversion_summary_path = stage_root / "conversion_summary.json"
    conversion_summary_path.write_text(
        json.dumps({"submitted_labeled_item_count": item_count}),
        encoding="utf-8",
    )
    return TrainerDatasetArtifact(
        task_type=TaskType.DETECTION,
        stage_root=str(stage_root),
        manifest_path=str(stage_root / "dataset_manifest.json"),
        conversion_summary_path=str(conversion_summary_path),
        validation_report_path=str(stage_root / "validation_report.json"),
        split_manifest_path=str(stage_root / "split_manifest.json"),
        class_map_path=str(stage_root / "class_map.json"),
    )
