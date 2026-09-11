from __future__ import annotations

import importlib.metadata
import importlib.util
import inspect
import shutil
from pathlib import Path
from typing import Any

from cv_status import TaskType

from cv_training.dataset_staging import (
    DatasetPreparationError,
    selected_classes_from_runtime_metadata,
    stage_framework_dataset,
)
from cv_training.paths import sha256_file
from cv_training.pretrained import require_local_pretrained_checkpoint, stage_torch_hub_checkpoint
from cv_training.trainer_types import (
    BaseTrainer,
    TrainerArtifactValidation,
    TrainerCheckpointArtifact,
    TrainerConfig,
    TrainerControl,
    TrainerExportArtifact,
    TrainerProgress,
    TrainerResult,
    TrainerResumeState,
)
from cv_training.validators import build_artifact_validation_summary

ANOMALIB_PATCHCORE_SUPPORTED_ARCHITECTURES: dict[tuple[str, str, str], str] = {
    ("patchcore", "anomalib", "wide_resnet50_2"): "patchcore:wide_resnet50_2",
}

_PATCHCORE_BACKBONE_WEIGHT_FILES: dict[str, tuple[str, ...]] = {
    "wide_resnet50_2": (
        "anomaly-patchcore-anomalib-wide_resnet50_2.pth",
        "anomaly-patchcore-wide_resnet50_2.pth",
        "patchcore-wide_resnet50_2.pth",
        "wide_resnet50_2.pth",
        "wide_resnet50_2-95faca4d.pth",
        "wide_resnet50_2-9ba9bcbe.pth",
    )
}


class AnomalibPatchCoreTrainer(BaseTrainer):
    @property
    def trainer_mode(self) -> str:
        return "real"

    @property
    def trainer_backend(self) -> str:
        return "anomalib-patchcore"

    def run(
        self,
        *,
        config: TrainerConfig,
        control: TrainerControl,
        resume_state: TrainerResumeState | None = None,
    ) -> TrainerResult:
        self._validate_config(config)
        symbols = _load_anomalib_symbols()
        runtime_dependencies = _runtime_dependency_metadata(config)
        dataset_artifact = stage_framework_dataset(
            training_job_id=config.training_job_id,
            task_type=TaskType.ANOMALY,
            snapshot_root=config.snapshot_root,
            stage_root=config.workspace_dir / "dataset-stage" / "anomaly",
            selected_classes=selected_classes_from_runtime_metadata(config.runtime_metadata),
        )
        image_size = int(config.image_size or config.resolved_recipe.get("image_size") or 256)
        batch_size = max(int(config.batch_size or config.resolved_recipe.get("batch_size") or 8), 1)
        num_workers = max(int(config.resolved_recipe.get("num_workers") or 0), 0)
        datamodule = _build_folder_datamodule(
            folder_cls=symbols["Folder"],
            dataset_root=Path(str(dataset_artifact.metadata["anomalib_root"])),
            image_size=image_size,
            batch_size=batch_size,
            num_workers=num_workers,
        )
        backbone = str(
            config.optimizer_settings.get("backbone")
            or config.resolved_recipe.get("backbone")
            or "wide_resnet50_2"
        )
        pretraining = _resolve_patchcore_pretraining(config=config, backbone=backbone)
        model = _build_patchcore_model(
            model_cls=symbols["Patchcore"],
            backbone=backbone,
            pre_trained=bool(pretraining["enabled"]),
        )
        engine = _build_engine(
            engine_cls=symbols["Engine"],
            config=config,
            image_size=image_size,
        )
        control.on_runtime_metadata(
            {
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "real_training": True,
                "simulation_only": False,
                "dataset_manifest_path": dataset_artifact.manifest_path,
                "anomaly": _anomaly_runtime_metadata(
                    dataset_artifact=dataset_artifact,
                    method="patchcore",
                    backbone=backbone,
                ),
                "pretraining": pretraining,
                **runtime_dependencies,
            }
        )
        if resume_state is not None and resume_state.checkpoint_path:
            raise RuntimeError("PatchCore anomaly training does not support resume checkpoints yet.")

        engine.fit(model=model, datamodule=datamodule)
        control.on_progress(
            TrainerProgress(
                epoch_no=max(int(config.max_epochs), 1),
                step_no=max(int(config.max_epochs), 1),
                total_epochs=max(int(config.max_epochs), 1),
                total_steps=max(int(config.max_epochs), 1),
                training_metrics={"memory_bank_size": float(_memory_bank_size(model))},
                validation_metrics={},
            )
        )
        raw_metrics = engine.test(model=model, datamodule=datamodule)
        metrics = _normalize_anomaly_metrics(raw_metrics)
        if config.best_metric_name not in metrics:
            raise RuntimeError(
                f"Anomalib evaluation did not produce required metric {config.best_metric_name!r}."
            )

        checkpoint = _publish_checkpoint(config=config, model=model, metrics=metrics)
        control.on_checkpoint(checkpoint)
        onnx_path = _export_onnx(
            engine=engine,
            model=model,
            export_type=symbols["ExportType"],
            output_root=config.onnx_dir,
        )
        artifact_validation = build_artifact_validation_summary(
            checkpoint_path=Path(checkpoint.path),
            onnx_path=onnx_path,
            trainer_mode="real",
            tensorrt_deferred=True,
        )
        threshold = _resolve_threshold(model=model, metrics=metrics, config=config)
        final_metrics = dict(metrics)
        final_metrics.setdefault("threshold", threshold)
        return TrainerResult(
            final_epoch=max(int(config.max_epochs), 1),
            final_step=max(int(config.max_epochs), 1),
            best_metric_name=config.best_metric_name,
            best_metric_value=float(final_metrics[config.best_metric_name]),
            best_metrics=final_metrics,
            final_validation_metrics=final_metrics,
            checkpoints=(checkpoint,),
            dataset_artifact=dataset_artifact,
            export_artifacts=_export_artifacts(onnx_path, artifact_validation),
            artifact_validation=artifact_validation,
            runtime_metadata={
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "runtime_dependencies": runtime_dependencies,
                "anomaly": {
                    **_anomaly_runtime_metadata(
                        dataset_artifact=dataset_artifact,
                        method="patchcore",
                        backbone=backbone,
                    ),
                    "threshold": threshold,
                    "evaluation_metrics": final_metrics,
                },
                "pretraining": pretraining,
            },
            best_checkpoint_path=checkpoint.path,
            latest_checkpoint_path=checkpoint.path,
        )

    def _validate_config(self, config: TrainerConfig) -> None:
        if config.task_type != TaskType.ANOMALY:
            raise RuntimeError(
                f"{self.trainer_backend} supports anomaly training only, not {config.task_type.value}."
            )
        key = (
            config.architecture.family.lower(),
            config.architecture.version.lower(),
            config.architecture.size.lower(),
        )
        if key not in ANOMALIB_PATCHCORE_SUPPORTED_ARCHITECTURES:
            raise RuntimeError(
                "Only PatchCore with WideResNet-50-2 is currently supported for anomaly "
                "training, export, deployment, and runtime heatmaps."
            )


def _load_anomalib_symbols() -> dict[str, Any]:
    try:
        from anomalib.data import Folder
        from anomalib.engine import Engine
        from anomalib.models import Patchcore
    except ImportError as exc:
        raise RuntimeError(
            "The anomalib package is required for real PatchCore anomaly training."
        ) from exc
    try:
        from anomalib.deploy import ExportType
    except ImportError:
        ExportType = None
    return {
        "Folder": Folder,
        "Engine": Engine,
        "Patchcore": Patchcore,
        "ExportType": ExportType,
    }


def _build_folder_datamodule(
    *,
    folder_cls: Any,
    dataset_root: Path,
    image_size: int,
    batch_size: int,
    num_workers: int,
) -> Any:
    kwargs = {
        "name": "cv-platform-anomaly",
        "root": str(dataset_root),
        "normal_dir": "normal",
        "abnormal_dir": "abnormal",
        "normal_test_dir": "normal_test",
        "image_size": (image_size, image_size),
        "train_batch_size": batch_size,
        "eval_batch_size": batch_size,
        "num_workers": num_workers,
    }
    return folder_cls(**_supported_kwargs(folder_cls, kwargs))


def _resolve_patchcore_pretraining(*, config: TrainerConfig, backbone: str) -> dict[str, Any]:
    raw_pretrained = str(config.resolved_recipe.get("pretrained", "true")).strip().lower()
    if raw_pretrained in {"false", "0", "no", "off", "scratch", "none"}:
        return {
            "enabled": False,
            "strategy": "scratch",
            "reason": "Recipe requested PatchCore without a pretrained backbone.",
            "checkpoint": None,
            "backbone": backbone,
        }
    candidates = _PATCHCORE_BACKBONE_WEIGHT_FILES.get(backbone, ())
    requested = candidates[0] if candidates else f"anomaly-patchcore-{backbone}.pth"
    checkpoint, metadata = require_local_pretrained_checkpoint(
        requested,
        aliases=candidates[1:],
        context=f"anomaly patchcore {backbone}",
    )
    staged_metadata = stage_torch_hub_checkpoint(Path(checkpoint))
    return {
        "enabled": True,
        "strategy": "pretrained_backbone",
        "reason": "PatchCore uses a local pretrained feature backbone.",
        "checkpoint": checkpoint,
        "backbone": backbone,
        **metadata,
        **staged_metadata,
    }


def _build_patchcore_model(*, model_cls: Any, backbone: object, pre_trained: bool) -> Any:
    kwargs = {
        "backbone": str(backbone or "wide_resnet50_2"),
        "pre_trained": pre_trained,
        "layers": ["layer2", "layer3"],
    }
    return model_cls(**_supported_kwargs(model_cls, kwargs))


def _build_engine(*, engine_cls: Any, config: TrainerConfig, image_size: int) -> Any:
    accelerator = "gpu" if str(config.gpu_device).startswith("cuda") else "cpu"
    kwargs = {
        "max_epochs": max(int(config.max_epochs), 1),
        "accelerator": accelerator,
        "devices": 1,
        "default_root_dir": str(config.workspace_dir / "anomalib-runs"),
        "image_size": image_size,
    }
    return engine_cls(**_supported_kwargs(engine_cls, kwargs))


def _supported_kwargs(callable_obj: Any, values: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return values
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return values
    return {key: value for key, value in values.items() if key in signature.parameters}


def _normalize_anomaly_metrics(raw_metrics: object) -> dict[str, float]:
    if isinstance(raw_metrics, list) and raw_metrics:
        raw_metrics = raw_metrics[0]
    if not isinstance(raw_metrics, dict):
        return {}
    aliases = {
        "image_AUROC": "image_auroc",
        "image/auroc": "image_auroc",
        "AUROC": "image_auroc",
        "image_F1Score": "image_f1",
        "image/f1score": "image_f1",
        "F1Score": "image_f1",
        "image_Precision": "precision",
        "Precision": "precision",
        "image_Recall": "recall",
        "Recall": "recall",
        "pixel_AUROC": "pixel_auroc",
        "threshold": "threshold",
    }
    metrics: dict[str, float] = {}
    for raw_key, raw_value in raw_metrics.items():
        key = aliases.get(str(raw_key), str(raw_key).strip().lower())
        parsed = _float_or_none(raw_value)
        if parsed is not None:
            metrics[key] = parsed
    if "image_f1" not in metrics and "precision" in metrics and "recall" in metrics:
        precision = metrics["precision"]
        recall = metrics["recall"]
        metrics["image_f1"] = (2 * precision * recall) / max(precision + recall, 1e-9)
    return metrics


def _publish_checkpoint(
    *,
    config: TrainerConfig,
    model: Any,
    metrics: dict[str, float],
) -> TrainerCheckpointArtifact:
    run_root = config.workspace_dir / "anomalib-runs"
    candidates = sorted(run_root.rglob("*.ckpt"), key=lambda item: item.stat().st_mtime)
    checkpoint_path = config.checkpoint_dir / "best_anomaly_patchcore.ckpt"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    if candidates:
        shutil.copy2(candidates[-1], checkpoint_path)
    else:
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("PyTorch is required to persist the PatchCore checkpoint.") from exc
        state_dict = model.state_dict() if hasattr(model, "state_dict") else {}
        torch.save(
            {
                "trainer_mode": "real",
                "trainer_backend": "anomalib-patchcore",
                "task_type": TaskType.ANOMALY.value,
                "metrics": metrics,
                "model_state": state_dict,
            },
            str(checkpoint_path),
        )
    return TrainerCheckpointArtifact(
        path=str(checkpoint_path),
        epoch_no=max(int(config.max_epochs), 1),
        step_no=max(int(config.max_epochs), 1),
        metrics=metrics,
        checkpoint_kind="best",
        is_best=True,
        resume_safe=False,
        metadata={
            "real_training": True,
            "size_bytes": checkpoint_path.stat().st_size,
            "sha256": sha256_file(checkpoint_path),
            "trainer_backend": "anomalib-patchcore",
        },
    )


def _export_onnx(*, engine: Any, model: Any, export_type: Any, output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    export_kwargs: dict[str, Any] = {"model": model, "export_root": str(output_root)}
    if export_type is not None:
        export_kwargs["export_type"] = getattr(export_type, "ONNX", "onnx")
    else:
        export_kwargs["export_type"] = "onnx"
    exported = engine.export(**_supported_kwargs(engine.export, export_kwargs))
    candidates = []
    if exported:
        candidates.append(Path(str(exported)))
    candidates.extend(sorted(output_root.rglob("*.onnx"), key=lambda item: item.stat().st_mtime))
    source_path = next((candidate for candidate in reversed(candidates) if candidate.exists()), None)
    if source_path is None:
        raise RuntimeError("Anomalib PatchCore export did not produce an ONNX artifact.")
    target_path = output_root / "model.onnx"
    if source_path.resolve(strict=False) != target_path.resolve(strict=False):
        shutil.copy2(source_path, target_path)
    if target_path.stat().st_size <= 0:
        raise RuntimeError("Anomalib PatchCore ONNX export is empty.")
    return target_path


def _export_artifacts(
    onnx_path: Path,
    artifact_validation: TrainerArtifactValidation,
) -> tuple[TrainerExportArtifact, ...]:
    return (
        TrainerExportArtifact(
            kind="onnx",
            path=str(onnx_path),
            generated=True,
            status="ready",
            metadata={"validation_status": artifact_validation.status},
        ),
        TrainerExportArtifact(
            kind="tensorrt",
            path=None,
            generated=False,
            status="deferred_to_deployment",
            metadata={"reason": "TensorRT engine builds run in deployment packaging."},
        ),
    )


def _runtime_dependency_metadata(config: TrainerConfig) -> dict[str, Any]:
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("PyTorch is required for Anomalib real training.") from exc
    if str(config.gpu_device).startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(
            f"GPU device {config.gpu_device} was requested, but PyTorch CUDA is unavailable."
        )
    required_modules = ("anomalib", "torchvision", "onnx", "onnxruntime", "numpy", "PIL")
    versions: dict[str, str] = {"torch": getattr(torch, "__version__", "unknown")}
    for module_name in required_modules:
        if importlib.util.find_spec(module_name) is None:
            raise RuntimeError(
                f"Required Anomalib real-training dependency {module_name!r} is missing."
            )
        package_name = "Pillow" if module_name == "PIL" else module_name
        try:
            versions[module_name] = importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            versions[module_name] = "unknown"
    return {"backend_versions": versions, "gpu_device": config.gpu_device}


def _anomaly_runtime_metadata(
    *,
    dataset_artifact: Any,
    method: str,
    backbone: str,
) -> dict[str, Any]:
    return {
        "task_type": TaskType.ANOMALY.value,
        "method": method,
        "backend": "anomalib",
        "backbone": backbone,
        "dataset_format": dataset_artifact.metadata.get("format"),
        "heatmap_support": True,
        "score_output_support": True,
        "thresholding_support": True,
        "mask_support": False,
        "localization_masks_from_label_studio": False,
        "normal_label": "normal",
        "abnormal_label": "anomalous",
    }


def _resolve_threshold(*, model: Any, metrics: dict[str, float], config: TrainerConfig) -> float:
    if "threshold" in metrics:
        return _clip01(metrics["threshold"])
    for attribute_name in ("threshold", "image_threshold"):
        value = getattr(model, attribute_name, None)
        parsed = _float_or_none(value)
        if parsed is not None:
            return _clip01(parsed)
    configured = config.optimizer_settings.get("threshold") or config.resolved_recipe.get("threshold")
    parsed_configured = _float_or_none(configured)
    return _clip01(parsed_configured if parsed_configured is not None else 0.5)


def _memory_bank_size(model: Any) -> int:
    memory_bank = getattr(model, "memory_bank", None)
    if memory_bank is None:
        return 0
    if hasattr(memory_bank, "shape") and memory_bank.shape:
        return int(memory_bank.shape[0])
    try:
        return len(memory_bank)
    except TypeError:
        return 0


def _float_or_none(value: object) -> float | None:
    try:
        if hasattr(value, "detach"):
            value = value.detach().cpu()
        if hasattr(value, "item"):
            value = value.item()
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clip01(value: float) -> float:
    return max(0.0, min(float(value), 1.0))
