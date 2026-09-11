from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from cv_status import TaskType

from cv_training.dataset_staging import (
    DatasetPreparationError,
    selected_classes_from_runtime_metadata,
    stage_framework_dataset,
)
from cv_training.paths import sha256_file
from cv_training.pretrained import require_local_pretrained_checkpoint
from cv_training.trainer_types import (
    BaseTrainer,
    TrainerArtifactValidation,
    TrainerCheckpointArtifact,
    TrainerConfig,
    TrainerControl,
    TrainerDatasetArtifact,
    TrainerExportArtifact,
    TrainerProgress,
    TrainerResult,
    TrainerResumeState,
)
from cv_training.validators import build_artifact_validation_summary

CLASSIFICATION_SUPPORTED_ARCHITECTURES: dict[tuple[str, str, str], str] = {
    ("resnet", "v1", "18"): "resnet18",
    ("resnet", "v1", "50"): "resnet50",
    ("mobilenet", "v3", "small"): "mobilenet_v3_small",
    ("mobilenet", "v3", "large"): "mobilenet_v3_large",
    ("efficientnet", "v1", "b0"): "efficientnet_b0",
}

SEGMENTATION_SUPPORTED_ARCHITECTURES: dict[tuple[str, str, str], str] = {
    ("deeplab", "v3", "resnet50"): "deeplabv3_resnet50",
    ("fcn", "v1", "resnet50"): "fcn_resnet50",
    ("lraspp", "v3", "large"): "lraspp_mobilenet_v3_large",
}

_TORCHVISION_CLASSIFICATION_WEIGHT_FILES: dict[str, tuple[str, ...]] = {
    "resnet18": ("resnet18-f37072fd.pth",),
    "resnet50": ("resnet50-0676ba61.pth", "resnet50-11ad3fa6.pth"),
    "mobilenet_v3_small": ("mobilenet_v3_small-047dcff4.pth",),
    "mobilenet_v3_large": ("mobilenet_v3_large-8738ca79.pth",),
    "efficientnet_b0": ("efficientnet_b0_rwightman-7f5810bc.pth",),
}

_TORCHVISION_SEGMENTATION_WEIGHT_FILES: dict[str, tuple[str, ...]] = {
    "deeplabv3_resnet50": ("deeplabv3_resnet50_coco-cd0a2569.pth",),
    "fcn_resnet50": ("fcn_resnet50_coco-1167a1af.pth",),
    "lraspp_mobilenet_v3_large": ("lraspp_mobilenet_v3_large-d234d4ea.pth",),
}


class TorchvisionClassificationTrainer(BaseTrainer):
    @property
    def trainer_mode(self) -> str:
        return "real"

    @property
    def trainer_backend(self) -> str:
        return "torchvision-classification"

    def run(
        self,
        *,
        config: TrainerConfig,
        control: TrainerControl,
        resume_state: TrainerResumeState | None = None,
    ) -> TrainerResult:
        if config.task_type != TaskType.CLASSIFICATION:
            raise RuntimeError(
                f"{self.trainer_backend} supports classification only, not {config.task_type.value}."
            )
        key = _architecture_key(config)
        if key not in CLASSIFICATION_SUPPORTED_ARCHITECTURES:
            raise RuntimeError(
                f"Architecture {config.architecture.backend_model_key} is not supported by {self.trainer_backend}."
            )

        torch, nn, optim, data = _load_torch_symbols()
        runtime_dependencies = _runtime_dependency_metadata(config)
        dataset_artifact = _stage_dataset(config)
        class_names = _load_class_map(dataset_artifact)
        if not class_names:
            raise DatasetPreparationError("Classification training requires at least one declared class.")
        pretraining = _resolve_pretraining(
            config=config,
            dataset_artifact=dataset_artifact,
            resume_state=resume_state,
        )
        control.on_runtime_metadata(
            {
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "real_training": True,
                "simulation_only": False,
                "dataset_manifest_path": dataset_artifact.manifest_path,
                "pretraining": pretraining,
                **runtime_dependencies,
            }
        )

        device = _resolve_device(torch, config.gpu_device)
        image_size = int(config.image_size or config.resolved_recipe.get("image_size") or 224)
        batch_size = max(int(config.batch_size or config.resolved_recipe.get("batch_size") or 16), 1)
        num_workers = max(int(config.resolved_recipe.get("num_workers") or 0), 0)
        train_dataset = _ClassificationJsonlDataset(
            stage_root=Path(dataset_artifact.stage_root),
            split="train",
            class_names=class_names,
            image_size=image_size,
        )
        eval_dataset = _classification_eval_dataset(
            dataset_artifact=dataset_artifact,
            class_names=class_names,
            image_size=image_size,
        )
        balance_sampler, loss_weights, balance_metadata = _classification_balance_components(
            torch=torch,
            data=data,
            train_dataset=train_dataset,
            class_count=len(class_names),
            config=config,
            device=device,
        )
        train_loader = data.DataLoader(
            train_dataset,
            batch_size=batch_size,
            shuffle=balance_sampler is None,
            sampler=balance_sampler,
            num_workers=num_workers,
        )
        eval_loader = data.DataLoader(
            eval_dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=num_workers,
        )
        model = _build_classification_model(
            config=config,
            class_count=len(class_names),
            pretrained=bool(pretraining["enabled"]) and pretraining.get("checkpoint") is None,
        ).to(device)
        optimizer = _build_optimizer(optim, config=config, model=model)
        criterion = nn.CrossEntropyLoss(weight=loss_weights)
        control.on_runtime_metadata({"class_balance": balance_metadata})
        start_epoch, start_step, best_metric = _load_resume_checkpoint(
            torch=torch,
            model=model,
            optimizer=optimizer,
            resume_state=resume_state,
            device=device,
        )
        if pretraining.get("checkpoint"):
            _load_partial_checkpoint(
                torch=torch,
                model=model,
                checkpoint_path=Path(str(pretraining["checkpoint"])),
                device=device,
            )

        checkpoints: list[TrainerCheckpointArtifact] = []
        total_epochs = max(int(config.max_epochs), 1)
        steps_per_epoch = max(len(train_loader), 1)
        total_steps = total_epochs * steps_per_epoch
        latest_metrics: dict[str, float] = {}
        best_metrics: dict[str, float] = {}
        best_checkpoint_path: str | None = resume_state.checkpoint_path if resume_state else None
        latest_checkpoint_path: str | None = resume_state.checkpoint_path if resume_state else None
        step_no = start_step
        final_epoch = start_epoch
        early_stopping_patience = _early_stopping_patience(config)
        early_stopping_min_delta = _early_stopping_min_delta(config)
        epochs_without_improvement = 0
        stopped_early = False

        for epoch_no in range(start_epoch + 1, total_epochs + 1):
            epoch_improved = False
            model.train()
            for images, labels in train_loader:
                if control.should_cancel():
                    return _cancel_result(
                        config=config,
                        control=control,
                        checkpoint_args={
                            "torch": torch,
                            "model": model,
                            "optimizer": optimizer,
                            "epoch_no": epoch_no,
                            "step_no": step_no,
                            "metrics": latest_metrics or _evaluate_classification(
                                torch=torch,
                                model=model,
                                loader=eval_loader,
                                device=device,
                                class_count=len(class_names),
                            ),
                            "checkpoint_kind": "cancel",
                            "is_best": False,
                        },
                        dataset_artifact=dataset_artifact,
                        checkpoints=checkpoints,
                        best_metric_name=config.best_metric_name,
                        best_metric_value=best_metric,
                        best_metrics=best_metrics or latest_metrics,
                    )
                images = images.to(device)
                labels = labels.to(device)
                optimizer.zero_grad(set_to_none=True)
                logits = model(images)
                loss = criterion(logits, labels)
                loss.backward()
                optimizer.step()
                step_no += 1
                training_metrics = {"loss": float(loss.detach().cpu())}
                control.on_progress(
                    TrainerProgress(
                        epoch_no=epoch_no,
                        step_no=step_no,
                        total_epochs=total_epochs,
                        total_steps=total_steps,
                        training_metrics=training_metrics,
                        validation_metrics=latest_metrics,
                    )
                )
                if step_no % max(int(config.checkpoint_interval_steps), 1) == 0:
                    latest_metrics = _evaluate_classification(
                        torch=torch,
                        model=model,
                        loader=eval_loader,
                        device=device,
                        class_count=len(class_names),
                    )
                    is_best = _metric_is_better(
                        latest_metrics.get(config.best_metric_name, 0.0),
                        best_metric,
                        config.best_metric_mode,
                        min_delta=early_stopping_min_delta,
                    )
                    checkpoint = _write_torch_checkpoint(
                        config=config,
                        torch=torch,
                        model=model,
                        optimizer=optimizer,
                        epoch_no=epoch_no,
                        step_no=step_no,
                        metrics=latest_metrics,
                        checkpoint_kind="interval",
                        is_best=is_best,
                    )
                    checkpoints.append(checkpoint)
                    control.on_checkpoint(checkpoint)
                    latest_checkpoint_path = checkpoint.path
                    if is_best:
                        best_metric = latest_metrics.get(config.best_metric_name, 0.0)
                        best_metrics = dict(latest_metrics)
                        best_checkpoint_path = checkpoint.path
                        epoch_improved = True

            latest_metrics = _evaluate_classification(
                torch=torch,
                model=model,
                loader=eval_loader,
                device=device,
                class_count=len(class_names),
            )
            is_best = _metric_is_better(
                latest_metrics.get(config.best_metric_name, 0.0),
                best_metric,
                config.best_metric_mode,
                min_delta=early_stopping_min_delta,
            )
            checkpoint = _write_torch_checkpoint(
                config=config,
                torch=torch,
                model=model,
                optimizer=optimizer,
                epoch_no=epoch_no,
                step_no=step_no,
                metrics=latest_metrics,
                checkpoint_kind="epoch",
                is_best=is_best,
            )
            checkpoints.append(checkpoint)
            control.on_checkpoint(checkpoint)
            latest_checkpoint_path = checkpoint.path
            if is_best:
                best_metric = latest_metrics.get(config.best_metric_name, 0.0)
                best_metrics = dict(latest_metrics)
                best_checkpoint_path = checkpoint.path
                epoch_improved = True
            final_epoch = epoch_no
            if epoch_improved:
                epochs_without_improvement = 0
            else:
                epochs_without_improvement += 1
            if (
                early_stopping_patience is not None
                and epochs_without_improvement >= early_stopping_patience
            ):
                stopped_early = True
                break

        restored_checkpoint_path = best_checkpoint_path or latest_checkpoint_path
        if restored_checkpoint_path:
            _restore_torch_checkpoint_for_export(
                torch=torch,
                model=model,
                checkpoint_path=Path(restored_checkpoint_path),
                device=device,
            )
            latest_metrics = _evaluate_classification(
                torch=torch,
                model=model,
                loader=eval_loader,
                device=device,
                class_count=len(class_names),
            )
            best_metrics = dict(latest_metrics)

        onnx_path = _export_classification_onnx(
            torch=torch,
            model=model,
            config=config,
            image_size=image_size,
        )
        artifact_validation = build_artifact_validation_summary(
            checkpoint_path=Path(best_checkpoint_path or latest_checkpoint_path or ""),
            onnx_path=onnx_path,
            trainer_mode="real",
            tensorrt_deferred=True,
        )
        final_metric = float(
            (best_metrics or latest_metrics).get(config.best_metric_name, best_metric or 0.0)
        )
        return TrainerResult(
            final_epoch=final_epoch,
            final_step=step_no,
            best_metric_name=config.best_metric_name,
            best_metric_value=final_metric,
            best_metrics=best_metrics or latest_metrics,
            final_validation_metrics=latest_metrics,
            checkpoints=tuple(checkpoints),
            dataset_artifact=dataset_artifact,
            export_artifacts=_export_artifacts(onnx_path, artifact_validation),
            artifact_validation=artifact_validation,
            runtime_metadata={
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "runtime_dependencies": runtime_dependencies,
                "pretraining": pretraining,
                "class_count": len(class_names),
                "ignored_background_items": train_dataset.ignored_items + eval_dataset.ignored_items,
                "class_balance": balance_metadata,
                "best_checkpoint_restored_for_export": bool(restored_checkpoint_path),
                "early_stopping": {
                    "enabled": early_stopping_patience is not None,
                    "patience_epochs": early_stopping_patience,
                    "min_delta": early_stopping_min_delta,
                    "stopped": stopped_early,
                    "stopped_epoch": final_epoch if stopped_early else None,
                },
            },
            best_checkpoint_path=best_checkpoint_path or latest_checkpoint_path,
            latest_checkpoint_path=latest_checkpoint_path,
        )


class TorchvisionSegmentationTrainer(BaseTrainer):
    @property
    def trainer_mode(self) -> str:
        return "real"

    @property
    def trainer_backend(self) -> str:
        return "torchvision-segmentation"

    def run(
        self,
        *,
        config: TrainerConfig,
        control: TrainerControl,
        resume_state: TrainerResumeState | None = None,
    ) -> TrainerResult:
        if config.task_type != TaskType.SEGMENTATION:
            raise RuntimeError(
                f"{self.trainer_backend} supports segmentation only, not {config.task_type.value}."
            )
        key = _architecture_key(config)
        if key not in SEGMENTATION_SUPPORTED_ARCHITECTURES:
            raise RuntimeError(
                f"Architecture {config.architecture.backend_model_key} is not supported by {self.trainer_backend}."
            )

        torch, nn, optim, data = _load_torch_symbols()
        runtime_dependencies = _runtime_dependency_metadata(config)
        dataset_artifact = _stage_dataset(config)
        class_names = _load_class_map(dataset_artifact)
        if not class_names:
            raise DatasetPreparationError("Segmentation training requires at least one declared class.")
        pretraining = _resolve_pretraining(
            config=config,
            dataset_artifact=dataset_artifact,
            resume_state=resume_state,
        )
        control.on_runtime_metadata(
            {
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "real_training": True,
                "simulation_only": False,
                "dataset_manifest_path": dataset_artifact.manifest_path,
                "pretraining": pretraining,
                **runtime_dependencies,
            }
        )

        device = _resolve_device(torch, config.gpu_device)
        image_size = int(config.image_size or config.resolved_recipe.get("image_size") or 512)
        batch_size = max(int(config.batch_size or config.resolved_recipe.get("batch_size") or 4), 1)
        num_workers = max(int(config.resolved_recipe.get("num_workers") or 0), 0)
        train_dataset = _SegmentationCocoDataset(
            stage_root=Path(dataset_artifact.stage_root),
            split="train",
            class_count=len(class_names),
            image_size=image_size,
        )
        eval_dataset = _segmentation_eval_dataset(
            dataset_artifact=dataset_artifact,
            class_count=len(class_names),
            image_size=image_size,
        )
        train_loader = data.DataLoader(
            train_dataset,
            batch_size=batch_size,
            shuffle=True,
            num_workers=num_workers,
        )
        eval_loader = data.DataLoader(
            eval_dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=num_workers,
        )
        model = _build_segmentation_model(
            config=config,
            class_count=len(class_names) + 1,
            pretrained=bool(pretraining["enabled"]) and pretraining.get("checkpoint") is None,
        ).to(device)
        optimizer = _build_optimizer(optim, config=config, model=model)
        criterion = nn.CrossEntropyLoss()
        start_epoch, start_step, best_metric = _load_resume_checkpoint(
            torch=torch,
            model=model,
            optimizer=optimizer,
            resume_state=resume_state,
            device=device,
        )
        if pretraining.get("checkpoint"):
            _load_partial_checkpoint(
                torch=torch,
                model=model,
                checkpoint_path=Path(str(pretraining["checkpoint"])),
                device=device,
            )

        checkpoints: list[TrainerCheckpointArtifact] = []
        total_epochs = max(int(config.max_epochs), 1)
        steps_per_epoch = max(len(train_loader), 1)
        total_steps = total_epochs * steps_per_epoch
        latest_metrics: dict[str, float] = {}
        best_metrics: dict[str, float] = {}
        best_checkpoint_path: str | None = resume_state.checkpoint_path if resume_state else None
        latest_checkpoint_path: str | None = resume_state.checkpoint_path if resume_state else None
        step_no = start_step
        final_epoch = start_epoch
        early_stopping_patience = _early_stopping_patience(config)
        early_stopping_min_delta = _early_stopping_min_delta(config)
        epochs_without_improvement = 0
        stopped_early = False

        for epoch_no in range(start_epoch + 1, total_epochs + 1):
            epoch_improved = False
            model.train()
            for images, masks in train_loader:
                if control.should_cancel():
                    return _cancel_result(
                        config=config,
                        control=control,
                        checkpoint_args={
                            "torch": torch,
                            "model": model,
                            "optimizer": optimizer,
                            "epoch_no": epoch_no,
                            "step_no": step_no,
                            "metrics": latest_metrics or _evaluate_segmentation(
                                torch=torch,
                                model=model,
                                loader=eval_loader,
                                device=device,
                                class_count=len(class_names) + 1,
                            ),
                            "checkpoint_kind": "cancel",
                            "is_best": False,
                        },
                        dataset_artifact=dataset_artifact,
                        checkpoints=checkpoints,
                        best_metric_name=config.best_metric_name,
                        best_metric_value=best_metric,
                        best_metrics=best_metrics or latest_metrics,
                    )
                images = images.to(device)
                masks = masks.to(device)
                optimizer.zero_grad(set_to_none=True)
                logits = _segmentation_logits(model(images))
                loss = criterion(logits, masks)
                loss.backward()
                optimizer.step()
                step_no += 1
                training_metrics = {
                    "loss_cls": 0.0,
                    "loss_mask": float(loss.detach().cpu()),
                }
                control.on_progress(
                    TrainerProgress(
                        epoch_no=epoch_no,
                        step_no=step_no,
                        total_epochs=total_epochs,
                        total_steps=total_steps,
                        training_metrics=training_metrics,
                        validation_metrics=latest_metrics,
                    )
                )
                if step_no % max(int(config.checkpoint_interval_steps), 1) == 0:
                    latest_metrics = _evaluate_segmentation(
                        torch=torch,
                        model=model,
                        loader=eval_loader,
                        device=device,
                        class_count=len(class_names) + 1,
                    )
                    is_best = _metric_is_better(
                        latest_metrics.get(config.best_metric_name, 0.0),
                        best_metric,
                        config.best_metric_mode,
                        min_delta=early_stopping_min_delta,
                    )
                    checkpoint = _write_torch_checkpoint(
                        config=config,
                        torch=torch,
                        model=model,
                        optimizer=optimizer,
                        epoch_no=epoch_no,
                        step_no=step_no,
                        metrics=latest_metrics,
                        checkpoint_kind="interval",
                        is_best=is_best,
                    )
                    checkpoints.append(checkpoint)
                    control.on_checkpoint(checkpoint)
                    latest_checkpoint_path = checkpoint.path
                    if is_best:
                        best_metric = latest_metrics.get(config.best_metric_name, 0.0)
                        best_metrics = dict(latest_metrics)
                        best_checkpoint_path = checkpoint.path
                        epoch_improved = True

            latest_metrics = _evaluate_segmentation(
                torch=torch,
                model=model,
                loader=eval_loader,
                device=device,
                class_count=len(class_names) + 1,
            )
            is_best = _metric_is_better(
                latest_metrics.get(config.best_metric_name, 0.0),
                best_metric,
                config.best_metric_mode,
                min_delta=early_stopping_min_delta,
            )
            checkpoint = _write_torch_checkpoint(
                config=config,
                torch=torch,
                model=model,
                optimizer=optimizer,
                epoch_no=epoch_no,
                step_no=step_no,
                metrics=latest_metrics,
                checkpoint_kind="epoch",
                is_best=is_best,
            )
            checkpoints.append(checkpoint)
            control.on_checkpoint(checkpoint)
            latest_checkpoint_path = checkpoint.path
            if is_best:
                best_metric = latest_metrics.get(config.best_metric_name, 0.0)
                best_metrics = dict(latest_metrics)
                best_checkpoint_path = checkpoint.path
                epoch_improved = True
            final_epoch = epoch_no
            if epoch_improved:
                epochs_without_improvement = 0
            else:
                epochs_without_improvement += 1
            if (
                early_stopping_patience is not None
                and epochs_without_improvement >= early_stopping_patience
            ):
                stopped_early = True
                break

        restored_checkpoint_path = best_checkpoint_path or latest_checkpoint_path
        if restored_checkpoint_path:
            _restore_torch_checkpoint_for_export(
                torch=torch,
                model=model,
                checkpoint_path=Path(restored_checkpoint_path),
                device=device,
            )
            latest_metrics = _evaluate_segmentation(
                torch=torch,
                model=model,
                loader=eval_loader,
                device=device,
                class_count=len(class_names) + 1,
            )
            best_metrics = dict(latest_metrics)

        onnx_path = _export_segmentation_onnx(
            torch=torch,
            model=model,
            config=config,
            image_size=image_size,
        )
        artifact_validation = build_artifact_validation_summary(
            checkpoint_path=Path(best_checkpoint_path or latest_checkpoint_path or ""),
            onnx_path=onnx_path,
            trainer_mode="real",
            tensorrt_deferred=True,
        )
        final_metric = float(
            (best_metrics or latest_metrics).get(config.best_metric_name, best_metric or 0.0)
        )
        return TrainerResult(
            final_epoch=final_epoch,
            final_step=step_no,
            best_metric_name=config.best_metric_name,
            best_metric_value=final_metric,
            best_metrics=best_metrics or latest_metrics,
            final_validation_metrics=latest_metrics,
            checkpoints=tuple(checkpoints),
            dataset_artifact=dataset_artifact,
            export_artifacts=_export_artifacts(onnx_path, artifact_validation),
            artifact_validation=artifact_validation,
            runtime_metadata={
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "runtime_dependencies": runtime_dependencies,
                "pretraining": pretraining,
                "class_count": len(class_names),
                "output_class_count": len(class_names) + 1,
                "background_class_index": 0,
                "best_checkpoint_restored_for_export": bool(restored_checkpoint_path),
                "early_stopping": {
                    "enabled": early_stopping_patience is not None,
                    "patience_epochs": early_stopping_patience,
                    "min_delta": early_stopping_min_delta,
                    "stopped": stopped_early,
                    "stopped_epoch": final_epoch if stopped_early else None,
                },
            },
            best_checkpoint_path=best_checkpoint_path or latest_checkpoint_path,
            latest_checkpoint_path=latest_checkpoint_path,
        )


def _stage_dataset(config: TrainerConfig) -> TrainerDatasetArtifact:
    return stage_framework_dataset(
        training_job_id=config.training_job_id,
        task_type=config.task_type,
        snapshot_root=config.snapshot_root,
        stage_root=config.workspace_dir / "dataset-stage" / config.task_type.value,
        selected_classes=selected_classes_from_runtime_metadata(config.runtime_metadata),
    )


def _load_torch_symbols() -> tuple[Any, Any, Any, Any]:
    try:
        import torch
        from torch import nn, optim
        from torch.utils import data
    except ImportError as exc:
        raise RuntimeError("PyTorch and torchvision are required for real torchvision training.") from exc
    return torch, nn, optim, data


def _runtime_dependency_metadata(config: TrainerConfig) -> dict[str, Any]:
    torch, _, _, _ = _load_torch_symbols()
    if importlib.util.find_spec("torchvision") is None:
        raise RuntimeError("Required real-training dependency 'torchvision' is missing.")
    if config.gpu_device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(
            f"GPU device {config.gpu_device} was requested, but PyTorch CUDA is unavailable."
        )
    required_modules = ("torchvision", "PIL", "onnx", "onnxruntime", "numpy", "setuptools")
    versions: dict[str, str] = {"torch": getattr(torch, "__version__", "unknown")}
    for module_name in required_modules:
        if importlib.util.find_spec(module_name) is None:
            raise RuntimeError(f"Required real-training dependency {module_name!r} is missing.")
        distribution_name = "Pillow" if module_name == "PIL" else module_name
        try:
            versions[module_name] = importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            versions[module_name] = "unknown"
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError(
            "Real torchvision training currently requires the dedicated Python 3.11 training-worker image."
        )
    return {
        "backend_versions": versions,
        "gpu_device": config.gpu_device,
    }


def _resolve_device(torch: Any, gpu_device: str) -> Any:
    if gpu_device.startswith("cuda") and torch.cuda.is_available():
        return torch.device(gpu_device)
    if gpu_device.startswith("cuda"):
        raise RuntimeError(f"GPU device {gpu_device} was requested, but PyTorch CUDA is unavailable.")
    return torch.device("cpu")


def _load_class_map(dataset_artifact: TrainerDatasetArtifact) -> list[str]:
    payload = json.loads(Path(dataset_artifact.class_map_path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise DatasetPreparationError("Staged class_map.json must contain a JSON list.")
    return [str(item) for item in payload]


def _resolve_pretraining(
    *,
    config: TrainerConfig,
    dataset_artifact: TrainerDatasetArtifact,
    resume_state: TrainerResumeState | None,
) -> dict[str, Any]:
    dataset_items = _dataset_item_count(dataset_artifact)
    if resume_state is not None and resume_state.checkpoint_path:
        return {
            "enabled": False,
            "strategy": "resume_checkpoint",
            "reason": "A resume checkpoint is being loaded.",
            "checkpoint": None,
            "dataset_items": dataset_items,
        }
    recipe = config.resolved_recipe
    raw_strategy = str(recipe.get("pretrained", "auto")).strip().lower()
    checkpoint_setting = str(recipe.get("pretrained_checkpoint", "auto")).strip()
    scratch_min_items = max(int(recipe.get("scratch_training_min_items") or 2000), 1)
    if raw_strategy in {"false", "0", "no", "off", "scratch", "none"}:
        enabled = False
        strategy = "scratch"
        reason = "Recipe requested scratch training."
    elif raw_strategy in {"true", "1", "yes", "on", "pretrained", "transfer"}:
        enabled = True
        strategy = "transfer_learning"
        reason = "Recipe requested transfer learning."
    else:
        enabled = dataset_items <= scratch_min_items
        strategy = "transfer_learning" if enabled else "scratch"
        reason = (
            f"Dataset has {dataset_items} items; using transfer learning at or below {scratch_min_items}."
            if enabled
            else f"Dataset has {dataset_items} items; using scratch training above {scratch_min_items}."
        )
    checkpoint = None
    if checkpoint_setting and checkpoint_setting.lower() not in {"auto", "none", "false", "0", "off"}:
        checkpoint, checkpoint_metadata = require_local_pretrained_checkpoint(
            checkpoint_setting,
            context=f"{config.task_type.value} {config.architecture.display_name}",
        )
        enabled = True
        strategy = "custom_checkpoint"
        reason = "Recipe provided a pretrained checkpoint path."
    elif enabled:
        requested_checkpoint, aliases = _torchvision_checkpoint_candidates(config)
        checkpoint, checkpoint_metadata = require_local_pretrained_checkpoint(
            requested_checkpoint,
            aliases=aliases,
            context=f"{config.task_type.value} {config.architecture.display_name}",
        )
    else:
        checkpoint_metadata = {}
    return {
        "enabled": enabled,
        "strategy": strategy,
        "reason": reason,
        "checkpoint": checkpoint,
        "dataset_items": dataset_items,
        "scratch_training_min_items": scratch_min_items,
        "source": checkpoint,
        **checkpoint_metadata,
    }


def _torchvision_checkpoint_candidates(config: TrainerConfig) -> tuple[str, tuple[str, ...]]:
    task_type = config.task_type
    key = _architecture_key(config)
    if task_type == TaskType.CLASSIFICATION:
        factory_name = CLASSIFICATION_SUPPORTED_ARCHITECTURES[key]
        official_names = _TORCHVISION_CLASSIFICATION_WEIGHT_FILES.get(factory_name, ())
    elif task_type == TaskType.SEGMENTATION:
        factory_name = SEGMENTATION_SUPPORTED_ARCHITECTURES[key]
        official_names = _TORCHVISION_SEGMENTATION_WEIGHT_FILES.get(factory_name, ())
    else:
        raise RuntimeError(f"Torchvision pretraining is not available for {task_type.value}.")
    family = config.architecture.family.lower()
    version = config.architecture.version.lower()
    size = config.architecture.size.lower()
    primary = f"{task_type.value}-{family}-{version}-{size}.pth"
    aliases = (
        f"{task_type.value}-{factory_name}.pth",
        f"{factory_name}.pth",
        *official_names,
    )
    return primary, aliases


class _ClassificationJsonlDataset:
    def __init__(
        self,
        *,
        stage_root: Path,
        split: str,
        class_names: list[str],
        image_size: int,
    ) -> None:
        self.stage_root = stage_root
        self.split = split
        self.class_to_index = {name: index for index, name in enumerate(class_names)}
        self.image_size = image_size
        self.rows: list[tuple[Path, int]] = []
        self.ignored_items = 0
        manifest_path = stage_root / "annotations" / f"{split}.jsonl"
        if not manifest_path.exists():
            raise DatasetPreparationError(f"Classification split manifest {manifest_path} is missing.")
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            label = str(payload.get("label") or "")
            class_index = self.class_to_index.get(label)
            if class_index is None:
                self.ignored_items += 1
                continue
            image_path = stage_root / str(payload.get("image") or "")
            if not image_path.exists():
                raise DatasetPreparationError(f"Classification image {image_path} is missing.")
            self.rows.append((image_path, class_index))
        if not self.rows:
            raise DatasetPreparationError(
                f"Classification split {split} has no usable labeled images after filtering background rows."
            )

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> tuple[Any, int]:
        from PIL import Image
        from torchvision.transforms import functional as transform

        image_path, label = self.rows[index]
        with Image.open(image_path) as image:
            image = image.convert("RGB")
            image = transform.resize(image, [self.image_size, self.image_size])
            tensor = transform.to_tensor(image)
            tensor = transform.normalize(
                tensor,
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225],
            )
        return tensor, label


class _SegmentationCocoDataset:
    def __init__(
        self,
        *,
        stage_root: Path,
        split: str,
        class_count: int,
        image_size: int,
    ) -> None:
        self.stage_root = stage_root
        self.split = split
        self.class_count = class_count
        self.image_size = image_size
        annotation_path = stage_root / "annotations" / f"instances_{split}.json"
        if not annotation_path.exists():
            raise DatasetPreparationError(f"Segmentation annotations {annotation_path} are missing.")
        payload = json.loads(annotation_path.read_text(encoding="utf-8"))
        images = payload.get("images")
        annotations = payload.get("annotations")
        if not isinstance(images, list) or not isinstance(annotations, list):
            raise DatasetPreparationError(f"Segmentation annotations {annotation_path} are invalid.")
        grouped: dict[int, list[dict[str, Any]]] = {}
        for annotation in annotations:
            if isinstance(annotation, dict):
                grouped.setdefault(int(annotation.get("image_id") or 0), []).append(annotation)
        self.items: list[tuple[Path, int, int, list[dict[str, Any]]]] = []
        image_root = stage_root / split / "images"
        for image in images:
            if not isinstance(image, dict):
                continue
            image_id = int(image.get("id") or 0)
            file_name = str(image.get("file_name") or "")
            image_path = image_root / file_name
            if not image_path.exists():
                raise DatasetPreparationError(f"Segmentation image {image_path} is missing.")
            width = int(image.get("width") or 0)
            height = int(image.get("height") or 0)
            self.items.append((image_path, width, height, grouped.get(image_id, [])))
        if not self.items:
            raise DatasetPreparationError(f"Segmentation split {split} has no usable images.")

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> tuple[Any, Any]:
        import torch
        from PIL import Image, ImageDraw
        from torchvision.transforms import functional as transform

        image_path, width, height, annotations = self.items[index]
        with Image.open(image_path) as image:
            image = image.convert("RGB")
            if width <= 0 or height <= 0:
                width, height = int(image.width), int(image.height)
            mask = Image.new("L", (width, height), 0)
            draw = ImageDraw.Draw(mask)
            for annotation in annotations:
                class_index = int(annotation.get("category_id") or 0)
                if class_index <= 0 or class_index > self.class_count:
                    continue
                segmentation = annotation.get("segmentation") or []
                for polygon in segmentation:
                    if not isinstance(polygon, list) or len(polygon) < 6:
                        continue
                    points = [
                        (float(polygon[item]), float(polygon[item + 1]))
                        for item in range(0, len(polygon) - 1, 2)
                    ]
                    draw.polygon(points, fill=class_index)
            image = transform.resize(image, [self.image_size, self.image_size])
            mask = transform.resize(mask, [self.image_size, self.image_size], interpolation=Image.Resampling.NEAREST)
            image_tensor = transform.to_tensor(image)
            image_tensor = transform.normalize(
                image_tensor,
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225],
            )
            mask_tensor = torch.as_tensor(list(mask.tobytes()), dtype=torch.long).view(
                self.image_size,
                self.image_size,
            )
        return image_tensor, mask_tensor


def _classification_eval_dataset(
    *,
    dataset_artifact: TrainerDatasetArtifact,
    class_names: list[str],
    image_size: int,
) -> _ClassificationJsonlDataset:
    stage_root = Path(dataset_artifact.stage_root)
    for split in ("val", "test", "train"):
        try:
            return _ClassificationJsonlDataset(
                stage_root=stage_root,
                split=split,
                class_names=class_names,
                image_size=image_size,
            )
        except DatasetPreparationError:
            continue
    raise DatasetPreparationError("No usable classification evaluation split is available.")


def _segmentation_eval_dataset(
    *,
    dataset_artifact: TrainerDatasetArtifact,
    class_count: int,
    image_size: int,
) -> _SegmentationCocoDataset:
    stage_root = Path(dataset_artifact.stage_root)
    for split in ("val", "test", "train"):
        try:
            return _SegmentationCocoDataset(
                stage_root=stage_root,
                split=split,
                class_count=class_count,
                image_size=image_size,
            )
        except DatasetPreparationError:
            continue
    raise DatasetPreparationError("No usable segmentation evaluation split is available.")


def _build_classification_model(*, config: TrainerConfig, class_count: int, pretrained: bool) -> Any:
    import torchvision.models as models
    from torch import nn

    key = _architecture_key(config)
    factory_name = CLASSIFICATION_SUPPORTED_ARCHITECTURES[key]
    weights = _classification_weights(models, factory_name) if pretrained else None
    model = getattr(models, factory_name)(weights=weights)
    if factory_name.startswith("resnet"):
        model.fc = nn.Linear(model.fc.in_features, class_count)
    elif factory_name.startswith("mobilenet") or factory_name.startswith("efficientnet"):
        last = model.classifier[-1]
        model.classifier[-1] = nn.Linear(last.in_features, class_count)
    else:  # pragma: no cover - guarded by supported architecture map
        raise RuntimeError(f"Unsupported torchvision classification model {factory_name}.")
    return model


def _classification_weights(models: Any, factory_name: str) -> Any:
    enum_name = {
        "resnet18": "ResNet18_Weights",
        "resnet50": "ResNet50_Weights",
        "mobilenet_v3_small": "MobileNet_V3_Small_Weights",
        "mobilenet_v3_large": "MobileNet_V3_Large_Weights",
        "efficientnet_b0": "EfficientNet_B0_Weights",
    }[factory_name]
    return getattr(models, enum_name).DEFAULT


def _build_segmentation_model(*, config: TrainerConfig, class_count: int, pretrained: bool) -> Any:
    import torchvision.models.segmentation as models

    key = _architecture_key(config)
    factory_name = SEGMENTATION_SUPPORTED_ARCHITECTURES[key]
    weights = _segmentation_weights(models, factory_name) if pretrained else None
    factory = getattr(models, factory_name)
    factory_kwargs: dict[str, Any] = {"weights": weights}
    if not pretrained:
        factory_kwargs["weights_backbone"] = None
    try:
        model = factory(**factory_kwargs)
    except TypeError:
        factory_kwargs.pop("weights_backbone", None)
        model = factory(**factory_kwargs)
    _replace_segmentation_head(model.classifier, class_count)
    aux_classifier = getattr(model, "aux_classifier", None)
    if aux_classifier is not None:
        _replace_segmentation_head(aux_classifier, class_count)
    return model


def _segmentation_weights(models: Any, factory_name: str) -> Any:
    enum_name = {
        "deeplabv3_resnet50": "DeepLabV3_ResNet50_Weights",
        "fcn_resnet50": "FCN_ResNet50_Weights",
        "lraspp_mobilenet_v3_large": "LRASPP_MobileNet_V3_Large_Weights",
    }[factory_name]
    return getattr(models, enum_name).DEFAULT


def _replace_segmentation_head(head: Any, class_count: int) -> None:
    from torch import nn

    if hasattr(head, "low_classifier") and hasattr(head, "high_classifier"):
        low = head.low_classifier
        high = head.high_classifier
        head.low_classifier = nn.Conv2d(low.in_channels, class_count, kernel_size=1)
        head.high_classifier = nn.Conv2d(high.in_channels, class_count, kernel_size=1)
        return
    for name, child in reversed(list(head.named_children())):
        if isinstance(child, nn.Conv2d):
            setattr(
                head,
                name,
                nn.Conv2d(
                    child.in_channels,
                    class_count,
                    kernel_size=child.kernel_size,
                    stride=child.stride,
                    padding=child.padding,
                    dilation=child.dilation,
                    groups=child.groups,
                    bias=child.bias is not None,
                    padding_mode=child.padding_mode,
                ),
            )
            return
        _replace_segmentation_head(child, class_count)
        return
    raise RuntimeError("Could not replace torchvision segmentation classifier head.")


def _classification_balance_components(
    *,
    torch: Any,
    data: Any,
    train_dataset: _ClassificationJsonlDataset,
    class_count: int,
    config: TrainerConfig,
    device: Any,
) -> tuple[Any | None, Any | None, dict[str, Any]]:
    strategy = _class_balance_strategy(config)
    class_counts = _classification_class_counts(train_dataset, class_count=class_count)
    metadata: dict[str, Any] = {
        "strategy": strategy,
        "class_counts": class_counts,
    }
    if strategy == "none":
        return None, None, metadata

    total = sum(class_counts)
    if total <= 0:
        return None, None, {**metadata, "strategy": "none", "disabled_reason": "empty_train_split"}

    class_weights = [
        (total / max(class_count * count, 1)) if count > 0 else 0.0
        for count in class_counts
    ]
    metadata["class_weights"] = [round(float(value), 6) for value in class_weights]

    if strategy == "weighted_loss":
        loss_weights = torch.tensor(class_weights, dtype=torch.float32, device=device)
        return None, loss_weights, metadata

    sample_weights = [
        (1.0 / class_counts[label]) if class_counts[label] > 0 else 0.0
        for _image_path, label in train_dataset.rows
    ]
    sampler = data.WeightedRandomSampler(
        sample_weights,
        num_samples=len(sample_weights),
        replacement=True,
    )
    return sampler, None, metadata


def _class_balance_strategy(config: TrainerConfig) -> str:
    recipe_settings = dict(config.resolved_recipe.get("optimizer_settings") or {})
    settings = {**recipe_settings, **dict(config.optimizer_settings or {})}
    raw_value = settings.get("class_balance_strategy") or config.resolved_recipe.get(
        "class_balance_strategy"
    ) or "none"
    normalized = str(raw_value).strip().lower().replace("-", "_")
    if normalized in {"none", "weighted_loss", "balanced_sampler"}:
        return normalized
    raise DatasetPreparationError(
        "class_balance_strategy must be one of none, weighted_loss, or balanced_sampler."
    )


def _classification_class_counts(
    train_dataset: _ClassificationJsonlDataset,
    *,
    class_count: int,
) -> list[int]:
    counts = [0 for _ in range(class_count)]
    for _image_path, label in train_dataset.rows:
        if 0 <= label < class_count:
            counts[label] += 1
    return counts


def _build_optimizer(optim: Any, *, config: TrainerConfig, model: Any) -> Any:
    recipe_settings = dict(config.resolved_recipe.get("optimizer_settings") or {})
    settings = {**recipe_settings, **dict(config.optimizer_settings or {})}
    lr = float(config.learning_rate or config.resolved_recipe.get("learning_rate") or 0.001)
    optimizer_name = str(config.optimizer or config.resolved_recipe.get("optimizer") or "adamw").lower()
    weight_decay = float(settings.get("weight_decay") or 0.0)
    if optimizer_name == "sgd":
        return optim.SGD(
            model.parameters(),
            lr=lr,
            momentum=float(settings.get("momentum") or 0.9),
            weight_decay=weight_decay,
        )
    if optimizer_name == "adam":
        return optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
    return optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)


def _torch_load_checkpoint(*, torch: Any, checkpoint_path: Path, device: Any) -> Any:
    try:
        return torch.load(str(checkpoint_path), map_location=device, weights_only=False)
    except TypeError:  # pragma: no cover - older torch versions do not expose weights_only.
        return torch.load(str(checkpoint_path), map_location=device)


def _load_resume_checkpoint(
    *,
    torch: Any,
    model: Any,
    optimizer: Any,
    resume_state: TrainerResumeState | None,
    device: Any,
) -> tuple[int, int, float | None]:
    if resume_state is None or not resume_state.checkpoint_path:
        return 0, 0, None
    checkpoint_path = Path(resume_state.checkpoint_path)
    if not checkpoint_path.exists():
        raise RuntimeError(f"Requested resume checkpoint {checkpoint_path} does not exist.")
    payload = _torch_load_checkpoint(torch=torch, checkpoint_path=checkpoint_path, device=device)
    if isinstance(payload, dict) and "model_state" in payload:
        model.load_state_dict(payload["model_state"])
        if "optimizer_state" in payload:
            optimizer.load_state_dict(payload["optimizer_state"])
        return (
            int(payload.get("epoch_no") or resume_state.epoch_no),
            int(payload.get("step_no") or resume_state.step_no),
            _float_or_none(payload.get("best_metric")) or resume_state.best_metric,
        )
    model.load_state_dict(payload)
    return resume_state.epoch_no, resume_state.step_no, resume_state.best_metric


def _load_partial_checkpoint(*, torch: Any, model: Any, checkpoint_path: Path, device: Any) -> None:
    if not checkpoint_path.exists():
        raise RuntimeError(f"Configured pretrained checkpoint {checkpoint_path} does not exist.")
    payload = _torch_load_checkpoint(torch=torch, checkpoint_path=checkpoint_path, device=device)
    if isinstance(payload, dict):
        payload = payload.get("model_state") or payload.get("state_dict") or payload
    if not isinstance(payload, dict):
        raise RuntimeError(f"Configured pretrained checkpoint {checkpoint_path} does not contain a state dict.")
    current_state = model.state_dict()
    compatible_state = {}
    for key, value in payload.items():
        normalized_key = str(key).removeprefix("module.")
        current_value = current_state.get(normalized_key)
        if current_value is None:
            continue
        if hasattr(current_value, "shape") and hasattr(value, "shape") and current_value.shape != value.shape:
            continue
        compatible_state[normalized_key] = value
    if not compatible_state:
        raise RuntimeError(
            f"Configured pretrained checkpoint {checkpoint_path} did not contain any compatible tensors."
        )
    model.load_state_dict(compatible_state, strict=False)


def _restore_torch_checkpoint_for_export(
    *,
    torch: Any,
    model: Any,
    checkpoint_path: Path,
    device: Any,
) -> None:
    if not checkpoint_path.exists():
        raise RuntimeError(f"Best checkpoint selected for export {checkpoint_path} does not exist.")
    payload = _torch_load_checkpoint(torch=torch, checkpoint_path=checkpoint_path, device=device)
    model_state = payload
    if isinstance(payload, dict):
        model_state = payload.get("model_state") or payload.get("state_dict") or payload
    model.load_state_dict(model_state)


def _early_stopping_patience(config: TrainerConfig) -> int | None:
    raw_value = config.resolved_recipe.get("early_stopping_patience")
    if raw_value is None:
        raw_value = config.resolved_recipe.get("patience")
    if raw_value is None:
        return None
    try:
        patience = int(raw_value)
    except (TypeError, ValueError):
        return None
    return patience if patience > 0 else None


def _early_stopping_min_delta(config: TrainerConfig) -> float:
    raw_value = config.resolved_recipe.get("early_stopping_min_delta")
    if raw_value is None:
        raw_value = config.resolved_recipe.get("min_delta")
    if raw_value is None:
        return 0.0
    try:
        return max(float(raw_value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _write_torch_checkpoint(
    *,
    config: TrainerConfig,
    torch: Any,
    model: Any,
    optimizer: Any,
    epoch_no: int,
    step_no: int,
    metrics: dict[str, float],
    checkpoint_kind: str,
    is_best: bool,
) -> TrainerCheckpointArtifact:
    prefix = "best" if is_best else checkpoint_kind
    checkpoint_path = config.checkpoint_dir / f"{prefix}_{config.task_type.value}_epoch_{epoch_no:04d}_step_{step_no:06d}.pth"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "trainer_mode": "real",
            "trainer_backend": config.trainer_backend,
            "task_type": config.task_type.value,
            "architecture": _architecture_metadata(config),
            "epoch_no": epoch_no,
            "step_no": step_no,
            "best_metric": metrics.get(config.best_metric_name),
            "metrics": metrics,
            "model_state": model.state_dict(),
            "optimizer_state": optimizer.state_dict(),
        },
        str(checkpoint_path),
    )
    return TrainerCheckpointArtifact(
        path=str(checkpoint_path),
        epoch_no=epoch_no,
        step_no=step_no,
        metrics=metrics,
        checkpoint_kind=checkpoint_kind,
        is_best=is_best,
        resume_safe=True,
        metadata={
            "real_training": True,
            "size_bytes": checkpoint_path.stat().st_size,
            "sha256": sha256_file(checkpoint_path),
        },
    )


def _evaluate_classification(
    *,
    torch: Any,
    model: Any,
    loader: Any,
    device: Any,
    class_count: int,
) -> dict[str, float]:
    model.eval()
    confusion = [[0 for _ in range(class_count)] for _ in range(class_count)]
    correct = 0
    total = 0
    with torch.no_grad():
        for images, labels in loader:
            images = images.to(device)
            labels = labels.to(device)
            predictions = model(images).argmax(dim=1)
            for target, prediction in zip(labels.cpu().tolist(), predictions.cpu().tolist(), strict=False):
                target_index = int(target)
                prediction_index = int(prediction)
                if 0 <= target_index < class_count and 0 <= prediction_index < class_count:
                    confusion[target_index][prediction_index] += 1
                correct += int(target_index == prediction_index)
                total += 1
    precision_values: list[float] = []
    recall_values: list[float] = []
    for class_index in range(class_count):
        tp = confusion[class_index][class_index]
        fp = sum(confusion[row][class_index] for row in range(class_count) if row != class_index)
        fn = sum(confusion[class_index][col] for col in range(class_count) if col != class_index)
        precision_values.append(tp / max(tp + fp, 1))
        recall_values.append(tp / max(tp + fn, 1))
    precision = sum(precision_values) / max(len(precision_values), 1)
    recall = sum(recall_values) / max(len(recall_values), 1)
    return {
        "accuracy_top1": round(correct / max(total, 1), 6),
        "precision_macro": round(precision, 6),
        "recall_macro": round(recall, 6),
        "f1_macro": round((2 * precision * recall) / max(precision + recall, 1e-9), 6),
    }


def _evaluate_segmentation(
    *,
    torch: Any,
    model: Any,
    loader: Any,
    device: Any,
    class_count: int,
) -> dict[str, float]:
    model.eval()
    intersections = [0.0 for _ in range(class_count)]
    unions = [0.0 for _ in range(class_count)]
    true_foreground = 0.0
    predicted_true_foreground = 0.0
    with torch.no_grad():
        for images, masks in loader:
            images = images.to(device)
            masks = masks.to(device)
            predictions = _segmentation_logits(model(images)).argmax(dim=1)
            for class_index in range(1, class_count):
                predicted = predictions == class_index
                target = masks == class_index
                intersections[class_index] += float((predicted & target).sum().item())
                unions[class_index] += float((predicted | target).sum().item())
            true_foreground += float((masks > 0).sum().item())
            predicted_true_foreground += float(((predictions > 0) & (masks > 0)).sum().item())
    foreground_ious = [
        intersections[class_index] / unions[class_index]
        for class_index in range(1, class_count)
        if unions[class_index] > 0
    ]
    mean_iou = sum(foreground_ious) / max(len(foreground_ious), 1)
    recall = predicted_true_foreground / max(true_foreground, 1.0)
    return {
        "mask_map_50": round(mean_iou, 6),
        "mask_map_50_95": round(mean_iou, 6),
        "recall_at_100": round(recall, 6),
        "semantic_mean_iou": round(mean_iou, 6),
    }


def _export_classification_onnx(*, torch: Any, model: Any, config: TrainerConfig, image_size: int) -> Path:
    model.eval()
    output_path = config.onnx_dir / "model.onnx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    device = next(model.parameters()).device
    dummy = torch.randn(1, 3, image_size, image_size, device=device)
    torch.onnx.export(
        model,
        dummy,
        str(output_path),
        input_names=["input__image"],
        output_names=["output__predictions"],
        dynamic_axes={"input__image": {0: "batch"}, "output__predictions": {0: "batch"}},
        opset_version=int(config.resolved_recipe.get("onnx_opset") or 13),
    )
    return output_path


def _export_segmentation_onnx(*, torch: Any, model: Any, config: TrainerConfig, image_size: int) -> Path:
    from torch import nn

    class _SegmentationOnnxWrapper(nn.Module):
        def __init__(self, wrapped: Any) -> None:
            super().__init__()
            self.wrapped = wrapped

        def forward(self, image: Any) -> Any:
            return _segmentation_logits(self.wrapped(image))

    model.eval()
    output_path = config.onnx_dir / "model.onnx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = _SegmentationOnnxWrapper(model)
    device = next(model.parameters()).device
    dummy = torch.randn(1, 3, image_size, image_size, device=device)
    torch.onnx.export(
        wrapper,
        dummy,
        str(output_path),
        input_names=["input__image"],
        output_names=["output__masks"],
        dynamic_axes={
            "input__image": {0: "batch"},
            "output__masks": {0: "batch", 2: "height", 3: "width"},
        },
        opset_version=int(config.resolved_recipe.get("onnx_opset") or 13),
    )
    return output_path


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
            metadata={"reason": "Real TensorRT engine builds run in deployment packaging."},
        ),
    )


def _cancel_result(
    *,
    config: TrainerConfig,
    control: TrainerControl,
    checkpoint_args: dict[str, Any],
    dataset_artifact: TrainerDatasetArtifact,
    checkpoints: list[TrainerCheckpointArtifact],
    best_metric_name: str,
    best_metric_value: float | None,
    best_metrics: dict[str, float],
) -> TrainerResult:
    checkpoint = _write_torch_checkpoint(config=config, **checkpoint_args)
    checkpoints.append(checkpoint)
    control.on_checkpoint(checkpoint)
    metrics = checkpoint.metrics
    return TrainerResult(
        final_epoch=checkpoint.epoch_no,
        final_step=checkpoint.step_no,
        best_metric_name=best_metric_name,
        best_metric_value=float(best_metric_value or metrics.get(best_metric_name, 0.0)),
        best_metrics=best_metrics or metrics,
        final_validation_metrics=metrics,
        checkpoints=tuple(checkpoints),
        dataset_artifact=dataset_artifact,
        artifact_validation=TrainerArtifactValidation(
            status="cancelled",
            checks={"checkpoint_valid": True, "onnx_valid": False},
            details={"cancelled": True},
        ),
        runtime_metadata={
            "trainer_mode": "real",
            "trainer_backend": config.trainer_backend,
            "cancelled": True,
        },
        best_checkpoint_path=checkpoint.path if checkpoint.is_best else None,
        latest_checkpoint_path=checkpoint.path,
        cancelled=True,
    )


def _segmentation_logits(output: Any) -> Any:
    if isinstance(output, dict):
        return output["out"]
    return output


def _dataset_item_count(dataset_artifact: TrainerDatasetArtifact) -> int:
    summary_path = Path(dataset_artifact.conversion_summary_path)
    if summary_path.exists():
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            for key in ("submitted_labeled_item_count", "eligible_item_count"):
                if payload.get(key) is not None:
                    return max(int(payload[key]), 0)
            splits = payload.get("splits")
            if isinstance(splits, dict):
                total = 0
                for split_summary in splits.values():
                    if isinstance(split_summary, dict):
                        total += int(split_summary.get("images") or split_summary.get("items") or 0)
                if total:
                    return total
    return 0


def _architecture_key(config: TrainerConfig) -> tuple[str, str, str]:
    return (
        config.architecture.family.lower(),
        config.architecture.version.lower(),
        config.architecture.size.lower(),
    )


def _architecture_metadata(config: TrainerConfig) -> dict[str, str | bool | None]:
    return {
        "task_type": config.architecture.task_type.value,
        "family": config.architecture.family,
        "version": config.architecture.version,
        "size": config.architecture.size,
        "display_name": config.architecture.display_name,
        "backend_model_key": config.architecture.backend_model_key,
        "supported": config.architecture.supported,
        "support_reason": config.architecture.support_reason,
    }


def _metric_is_better(
    candidate: float,
    current: float | None,
    mode: str,
    *,
    min_delta: float = 0.0,
) -> bool:
    if current is None:
        return True
    delta = max(float(min_delta), 0.0)
    if mode == "min":
        return candidate < current - delta
    return candidate > current + delta


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
