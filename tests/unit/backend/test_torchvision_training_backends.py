from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cv_status import TaskType
from cv_training.backends import torchvision_trainers as tv_backends
from cv_training.trainer import TrainerConfig, resolve_trainer_architecture


class _Control:
    def __init__(self) -> None:
        self.progress = []
        self.checkpoints = []
        self.metadata = {}

    def should_cancel(self) -> bool:
        return False

    def on_progress(self, progress) -> None:  # noqa: ANN001
        self.progress.append(progress)

    def on_checkpoint(self, checkpoint) -> None:  # noqa: ANN001
        self.checkpoints.append(checkpoint)

    def on_runtime_metadata(self, metadata: dict) -> None:
        self.metadata.update(metadata)


def test_pretraining_policy_uses_transfer_through_threshold_and_scratch_above(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _dataset_artifact(tmp_path)
    cache_root = tmp_path / "pretrained-cache"
    cache_root.mkdir()
    expected_checkpoint = cache_root / "classification-resnet-v1-18.pth"
    expected_checkpoint.write_bytes(b"pretrained-weights")
    monkeypatch.setenv("CV_PLATFORM_PRETRAINED_CHECKPOINT_ROOT", str(cache_root))
    config = SimpleNamespace(
        task_type=TaskType.CLASSIFICATION,
        architecture=SimpleNamespace(
            family="resnet",
            version="v1",
            size="18",
            display_name="ResNet-18",
        ),
        resolved_recipe={
            "pretrained": "auto",
            "pretrained_checkpoint": "auto",
            "scratch_training_min_items": 2000,
        }
    )
    Path(artifact.conversion_summary_path).write_text(
        json.dumps({"submitted_labeled_item_count": 2000}),
        encoding="utf-8",
    )

    transfer = tv_backends._resolve_pretraining(
        config=config,
        dataset_artifact=artifact,
        resume_state=None,
    )
    assert transfer["enabled"] is True
    assert transfer["strategy"] == "transfer_learning"
    assert transfer["checkpoint"] == str(expected_checkpoint)
    assert transfer["checkpoint_source"] == "local_cache"

    Path(artifact.conversion_summary_path).write_text(
        json.dumps({"submitted_labeled_item_count": 2001}),
        encoding="utf-8",
    )
    scratch = tv_backends._resolve_pretraining(
        config=config,
        dataset_artifact=artifact,
        resume_state=None,
    )
    assert scratch["enabled"] is False
    assert scratch["strategy"] == "scratch"


def test_classification_balance_components_build_weighted_loss(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    data = pytest.importorskip("torch.utils.data")
    dataset = SimpleNamespace(rows=[(tmp_path / "a.png", 0), (tmp_path / "b.png", 0), (tmp_path / "c.png", 1)])
    config = SimpleNamespace(
        resolved_recipe={},
        optimizer_settings={"class_balance_strategy": "weighted_loss"},
    )

    sampler, loss_weights, metadata = tv_backends._classification_balance_components(
        torch=torch,
        data=data,
        train_dataset=dataset,
        class_count=2,
        config=config,
        device=torch.device("cpu"),
    )

    assert sampler is None
    assert loss_weights is not None
    assert loss_weights.tolist() == pytest.approx([0.75, 1.5])
    assert metadata["strategy"] == "weighted_loss"
    assert metadata["class_counts"] == [2, 1]


def test_classification_balance_components_build_balanced_sampler(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    data = pytest.importorskip("torch.utils.data")
    dataset = SimpleNamespace(rows=[(tmp_path / "a.png", 0), (tmp_path / "b.png", 0), (tmp_path / "c.png", 1)])
    config = SimpleNamespace(
        resolved_recipe={},
        optimizer_settings={"class_balance_strategy": "balanced_sampler"},
    )

    sampler, loss_weights, metadata = tv_backends._classification_balance_components(
        torch=torch,
        data=data,
        train_dataset=dataset,
        class_count=2,
        config=config,
        device=torch.device("cpu"),
    )

    assert sampler is not None
    assert loss_weights is None
    assert metadata["strategy"] == "balanced_sampler"
    assert metadata["class_weights"] == pytest.approx([0.75, 1.5])


def test_classification_trainer_persists_real_checkpoint_metrics_and_onnx(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    torch = pytest.importorskip("torch")
    nn = pytest.importorskip("torch.nn")
    snapshot_root = _write_classification_snapshot(tmp_path, sample_png_bytes)
    config = _trainer_config(
        tmp_path=tmp_path,
        task_type=TaskType.CLASSIFICATION,
        family="resnet",
        version="v1",
        size="18",
        recipe_name="baseline_cls",
        best_metric_name="accuracy_top1",
        image_size=8,
    )
    monkeypatch.setattr(
        tv_backends,
        "_runtime_dependency_metadata",
        lambda _config: {"backend_versions": {"torch": torch.__version__}, "gpu_device": "cpu"},
    )
    monkeypatch.setattr(
        tv_backends,
        "_build_classification_model",
        lambda *, config, class_count, pretrained: nn.Sequential(  # noqa: ARG005
            nn.Flatten(),
            nn.Linear(3 * 8 * 8, class_count),
        ),
    )
    _patch_export_and_validation(monkeypatch, tv_backends, "classification")
    config = _replace_snapshot_root(config, snapshot_root)
    control = _Control()

    result = tv_backends.TorchvisionClassificationTrainer().run(config=config, control=control)

    assert result.cancelled is False
    assert result.artifact_validation is not None
    assert result.artifact_validation.status == "passed"
    assert Path(result.latest_checkpoint_path or "").exists()
    assert result.export_artifacts[0].generated is True
    assert result.final_validation_metrics["accuracy_top1"] == pytest.approx(1.0)
    assert control.progress
    assert control.checkpoints
    assert control.metadata["trainer_backend"] == "torchvision-classification"
    assert control.metadata["pretraining"]["strategy"] == "scratch"


def test_classification_trainer_exports_best_checkpoint_and_stops_when_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    torch = pytest.importorskip("torch")
    nn = pytest.importorskip("torch.nn")
    snapshot_root = _write_classification_snapshot(tmp_path, sample_png_bytes)
    config = _trainer_config(
        tmp_path=tmp_path,
        task_type=TaskType.CLASSIFICATION,
        family="resnet",
        version="v1",
        size="18",
        recipe_name="baseline_cls",
        best_metric_name="accuracy_top1",
        image_size=8,
    )
    config = TrainerConfig(
        **{
            **config.__dict__,
            "max_epochs": 5,
            "resolved_recipe": {
                **config.resolved_recipe,
                "epochs": 5,
                "early_stopping_patience": 1,
            },
        }
    )
    monkeypatch.setattr(
        tv_backends,
        "_runtime_dependency_metadata",
        lambda _config: {"backend_versions": {"torch": torch.__version__}, "gpu_device": "cpu"},
    )
    monkeypatch.setattr(
        tv_backends,
        "_build_classification_model",
        lambda *, config, class_count, pretrained: nn.Sequential(  # noqa: ARG005
            nn.Flatten(),
            nn.Linear(3 * 8 * 8, class_count),
        ),
    )
    metric_sequence = iter(
        [
            {"accuracy_top1": 0.9, "f1_macro": 0.9},
            {"accuracy_top1": 0.1, "f1_macro": 0.1},
            {"accuracy_top1": 0.9, "f1_macro": 0.9},
        ]
    )
    monkeypatch.setattr(
        tv_backends,
        "_evaluate_classification",
        lambda **_kwargs: next(metric_sequence),
    )
    original_restore = tv_backends._restore_torch_checkpoint_for_export
    restored_paths: list[Path] = []

    def spy_restore(**kwargs) -> None:  # noqa: ANN003
        restored_paths.append(Path(kwargs["checkpoint_path"]))
        original_restore(**kwargs)

    monkeypatch.setattr(tv_backends, "_restore_torch_checkpoint_for_export", spy_restore)
    _patch_export_and_validation(monkeypatch, tv_backends, "classification")
    config = _replace_snapshot_root(config, snapshot_root)
    control = _Control()

    result = tv_backends.TorchvisionClassificationTrainer().run(config=config, control=control)

    assert result.final_epoch == 2
    assert result.best_metric_value == pytest.approx(0.9)
    assert result.final_validation_metrics["accuracy_top1"] == pytest.approx(0.9)
    assert result.runtime_metadata["best_checkpoint_restored_for_export"] is True
    assert result.runtime_metadata["early_stopping"]["stopped"] is True
    assert restored_paths
    assert "best_classification_epoch_0001" in restored_paths[0].name


def test_restore_torch_checkpoint_for_export_loads_saved_model_state(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    nn = pytest.importorskip("torch.nn")
    model = nn.Linear(1, 1)
    checkpoint_path = tmp_path / "best.pth"
    with torch.no_grad():
        model.weight.fill_(2.0)
        model.bias.fill_(0.5)
    torch.save({"model_state": model.state_dict()}, checkpoint_path)
    with torch.no_grad():
        model.weight.fill_(-3.0)
        model.bias.fill_(-1.0)

    tv_backends._restore_torch_checkpoint_for_export(
        torch=torch,
        model=model,
        checkpoint_path=checkpoint_path,
        device="cpu",
    )

    assert float(model.weight.detach().item()) == pytest.approx(2.0)
    assert float(model.bias.detach().item()) == pytest.approx(0.5)


def test_segmentation_trainer_persists_real_checkpoint_metrics_and_onnx(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    torch = pytest.importorskip("torch")
    nn = pytest.importorskip("torch.nn")
    snapshot_root = _write_segmentation_snapshot(tmp_path, sample_png_bytes)
    config = _trainer_config(
        tmp_path=tmp_path,
        task_type=TaskType.SEGMENTATION,
        family="deeplab",
        version="v3",
        size="resnet50",
        recipe_name="baseline_seg",
        best_metric_name="mask_map_50_95",
        image_size=8,
    )
    monkeypatch.setattr(
        tv_backends,
        "_runtime_dependency_metadata",
        lambda _config: {"backend_versions": {"torch": torch.__version__}, "gpu_device": "cpu"},
    )

    class TinySegmentation(nn.Module):
        def __init__(self, class_count: int) -> None:
            super().__init__()
            self.conv = nn.Conv2d(3, class_count, kernel_size=1)

        def forward(self, image):  # noqa: ANN001
            return {"out": self.conv(image)}

    monkeypatch.setattr(
        tv_backends,
        "_build_segmentation_model",
        lambda *, config, class_count, pretrained: TinySegmentation(class_count),  # noqa: ARG005
    )
    _patch_export_and_validation(monkeypatch, tv_backends, "segmentation")
    config = _replace_snapshot_root(config, snapshot_root)
    control = _Control()

    result = tv_backends.TorchvisionSegmentationTrainer().run(config=config, control=control)

    assert result.cancelled is False
    assert result.artifact_validation is not None
    assert result.artifact_validation.status == "passed"
    assert Path(result.latest_checkpoint_path or "").exists()
    assert result.export_artifacts[0].generated is True
    assert "mask_map_50_95" in result.final_validation_metrics
    assert control.progress
    assert control.checkpoints
    assert control.metadata["trainer_backend"] == "torchvision-segmentation"
    assert control.metadata["pretraining"]["strategy"] == "scratch"


def _patch_export_and_validation(monkeypatch: pytest.MonkeyPatch, module, task_name: str) -> None:  # noqa: ANN001
    def fake_export(*, torch, model, config, image_size):  # noqa: ANN001, ARG001
        output_path = config.onnx_dir / f"{task_name}.onnx"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"onnx-test-binary")
        return output_path

    monkeypatch.setattr(module, f"_export_{task_name}_onnx", fake_export)
    monkeypatch.setattr(
        module,
        "build_artifact_validation_summary",
        lambda **_kwargs: module.TrainerArtifactValidation(
            status="passed",
            checks={"checkpoint_valid": True, "onnx_valid": True},
            details={"tests": True},
        ),
    )


def _trainer_config(
    *,
    tmp_path: Path,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
    recipe_name: str,
    best_metric_name: str,
    image_size: int,
) -> TrainerConfig:
    root = tmp_path / f"job-{task_type.value}"
    for directory in ("workspace", "configs", "checkpoints", "evaluation", "onnx", "tensorrt"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    architecture = resolve_trainer_architecture(
        task_type=task_type,
        family=family,
        version=version,
        size=size,
    )
    assert architecture.supported is True
    return TrainerConfig(
        training_job_id=f"trn_{task_type.value}",
        dataset_snapshot_id=f"dss_{task_type.value}",
        task_type=task_type,
        trainer_mode="real",
        trainer_backend=f"torchvision-{task_type.value}",
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
        checkpoint_interval_steps=10,
        best_metric_name=best_metric_name,
        best_metric_mode="max",
        resolved_recipe={
            "recipe_name": recipe_name,
            "epochs": 1,
            "image_size": image_size,
            "batch_size": 1,
            "learning_rate": 0.001,
            "optimizer": "adamw",
            "pretrained": "scratch",
            "pretrained_checkpoint": "auto",
            "scratch_training_min_items": 2000,
        },
        runtime_metadata={},
        image_size=image_size,
        batch_size=1,
        learning_rate=0.001,
        optimizer="adamw",
        gpu_device="cpu",
    )


def _replace_snapshot_root(config: TrainerConfig, snapshot_root: Path) -> TrainerConfig:
    return TrainerConfig(**{**config.__dict__, "snapshot_root": snapshot_root})


def _write_classification_snapshot(tmp_path: Path, sample_png_bytes: bytes) -> Path:
    snapshot_root = tmp_path / "classification-snapshot"
    _snapshot_dirs(snapshot_root)
    for name in ("train.png", "val.png"):
        (snapshot_root / "images" / name).write_bytes(sample_png_bytes)
    _write_snapshot_payload(
        snapshot_root=snapshot_root,
        task_type="classification",
        items=[
            _canonical_item("train", "train.png", labels=["good"]),
            _canonical_item("val", "val.png", labels=["good"]),
        ],
        label_map=["good"],
    )
    return snapshot_root


def _write_segmentation_snapshot(tmp_path: Path, sample_png_bytes: bytes) -> Path:
    snapshot_root = tmp_path / "segmentation-snapshot"
    _snapshot_dirs(snapshot_root)
    for name in ("train.png", "val.png"):
        (snapshot_root / "images" / name).write_bytes(sample_png_bytes)
    polygon = {
        "class_name": "hand",
        "points": [
            {"x": 0.0, "y": 0.0},
            {"x": 1.0, "y": 0.0},
            {"x": 1.0, "y": 1.0},
            {"x": 0.0, "y": 1.0},
        ],
        "original_width": 1,
        "original_height": 1,
    }
    _write_snapshot_payload(
        snapshot_root=snapshot_root,
        task_type="segmentation",
        items=[
            _canonical_item("train", "train.png", polygons=[polygon]),
            _canonical_item("val", "val.png", polygons=[polygon]),
        ],
        label_map=["hand"],
    )
    return snapshot_root


def _snapshot_dirs(snapshot_root: Path) -> None:
    (snapshot_root / "annotations").mkdir(parents=True, exist_ok=True)
    (snapshot_root / "manifests").mkdir(parents=True, exist_ok=True)
    (snapshot_root / "images").mkdir(parents=True, exist_ok=True)


def _canonical_item(
    split: str,
    relative_path: str,
    *,
    labels: list[str] | None = None,
    polygons: list[dict] | None = None,
) -> dict:
    return {
        "item_id": f"itm_{split}_{relative_path}",
        "task_id": f"tsk_{split}_{relative_path}",
        "relative_path": relative_path,
        "source_path": f"/source/{relative_path}",
        "asset_sha256": f"sha-{relative_path}",
        "split": split,
        "eligibility_status": "eligible_labeled",
        "submitted": True,
        "background": False,
        "labels": labels or [],
        "boxes": [],
        "polygons": polygons or [],
        "raw_annotation_count": 1,
    }


def _write_snapshot_payload(
    *,
    snapshot_root: Path,
    task_type: str,
    items: list[dict],
    label_map: list[str],
) -> None:
    canonical_dataset = {
        "dataset_id": f"dst_{task_type}",
        "task_type": task_type,
        "source_task_count": len(items),
        "annotation_count": len(items),
        "eligible_item_count": len(items),
        "submitted_labeled_item_count": len(items),
        "submitted_background_item_count": 0,
        "draft_item_count": 0,
        "invalid_item_count": 0,
        "items": items,
        "class_distribution": {label: len(items) for label in label_map},
        "label_map": label_map,
    }
    (snapshot_root / "annotations" / "canonical_dataset.json").write_text(
        json.dumps(canonical_dataset, indent=2),
        encoding="utf-8",
    )
    (snapshot_root / "label_map.json").write_text(json.dumps(label_map), encoding="utf-8")
    (snapshot_root / "manifests" / "validation_report.json").write_text(
        json.dumps({"status": "passed", "checks": {"images_present": True}, "issues": []}),
        encoding="utf-8",
    )
    (snapshot_root / "manifests" / "split_metadata.json").write_text(
        json.dumps(
            {
                "strategy": "tests",
                "train_ratio": 50,
                "val_ratio": 50,
                "test_ratio": 0,
                "train_count": 1,
                "val_count": 1,
                "test_count": 0,
            }
        ),
        encoding="utf-8",
    )


def _dataset_artifact(tmp_path: Path):
    stage_root = tmp_path / "stage"
    stage_root.mkdir(parents=True, exist_ok=True)
    conversion_summary_path = stage_root / "conversion_summary.json"
    conversion_summary_path.write_text("{}", encoding="utf-8")
    return tv_backends.TrainerDatasetArtifact(
        task_type=TaskType.CLASSIFICATION,
        stage_root=str(stage_root),
        manifest_path=str(stage_root / "dataset_manifest.json"),
        conversion_summary_path=str(conversion_summary_path),
        validation_report_path=str(stage_root / "validation_report.json"),
        split_manifest_path=str(stage_root / "split_manifest.json"),
        class_map_path=str(stage_root / "class_map.json"),
    )
