from __future__ import annotations

import importlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import MutableMapping, MutableSequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from cv_status import TaskType

from cv_training.dataset_staging import (
    DatasetPreparationError,
    selected_classes_from_runtime_metadata,
    stage_framework_dataset,
)
from cv_training.paths import sha256_file
from cv_training.pretrained import (
    pretrained_checkpoint_roots,
    require_local_pretrained_checkpoint,
)
from cv_training.trainer_types import (
    BaseTrainer,
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


class OpenMMLabDetectionTrainer(BaseTrainer):
    _SUPPORTED_MODELS = {
        ("yolo", "v8", "n"): "configs/yolov8/yolov8_n_syncbn_fast_8xb16-500e_coco.py",
        ("yolo", "v8", "s"): "configs/yolov8/yolov8_s_syncbn_fast_8xb16-500e_coco.py",
        ("yolo", "v8", "m"): "configs/yolov8/yolov8_m_syncbn_fast_8xb16-500e_coco.py",
        ("yolo", "v8", "l"): "configs/yolov8/yolov8_l_syncbn_fast_8xb16-500e_coco.py",
        ("yolo", "v8", "x"): "configs/yolov8/yolov8_x_syncbn_fast_8xb16-500e_coco.py",
    }
    _PRETRAINED_CHECKPOINTS = {
        ("yolo", "v8", "n"): (
            "https://download.openmmlab.com/mmyolo/v0/yolov8/"
            "yolov8_n_syncbn_fast_8xb16-500e_coco/"
            "yolov8_n_syncbn_fast_8xb16-500e_coco_20230114_131804-88c11cdb.pth"
        ),
        ("yolo", "v8", "s"): (
            "https://download.openmmlab.com/mmyolo/v0/yolov8/"
            "yolov8_s_syncbn_fast_8xb16-500e_coco/"
            "yolov8_s_syncbn_fast_8xb16-500e_coco_20230117_180101-5aa5f0f1.pth"
        ),
        ("yolo", "v8", "m"): (
            "https://download.openmmlab.com/mmyolo/v0/yolov8/"
            "yolov8_m_syncbn_fast_8xb16-500e_coco/"
            "yolov8_m_syncbn_fast_8xb16-500e_coco_20230115_192200-c22e560a.pth"
        ),
        ("yolo", "v8", "l"): (
            "https://download.openmmlab.com/mmyolo/v0/yolov8/"
            "yolov8_l_syncbn_fast_8xb16-500e_coco/"
            "yolov8_l_syncbn_fast_8xb16-500e_coco_20230217_182526-189611b6.pth"
        ),
        ("yolo", "v8", "x"): (
            "https://download.openmmlab.com/mmyolo/v0/yolov8/"
            "yolov8_x_syncbn_fast_8xb16-500e_coco/"
            "yolov8_x_syncbn_fast_8xb16-500e_coco_20230218_023338-5674673c.pth"
        ),
    }

    @property
    def trainer_mode(self) -> str:
        return "real"

    @property
    def trainer_backend(self) -> str:
        return "openmmlab-mmyolo"

    def run(
        self,
        *,
        config: TrainerConfig,
        control: TrainerControl,
        resume_state: TrainerResumeState | None = None,
    ) -> TrainerResult:
        self._validate_config(config)
        runtime_dependencies = self._runtime_dependency_metadata(config)
        dataset_artifact = stage_framework_dataset(
            training_job_id=config.training_job_id,
            task_type=config.task_type,
            snapshot_root=config.snapshot_root,
            stage_root=config.workspace_dir / "dataset-stage" / config.task_type.value,
            selected_classes=selected_classes_from_runtime_metadata(config.runtime_metadata),
        )
        control.on_runtime_metadata(
            {
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "real_training": True,
                "simulation_only": False,
                "dataset_manifest_path": dataset_artifact.manifest_path,
                **runtime_dependencies,
            }
        )

        Config, Runner, Hook = self._load_mmengine_symbols()
        local_runtime_root = Path(tempfile.mkdtemp(prefix=f"cv-platform-training-{config.training_job_id}-"))
        local_checkpoint_dir = local_runtime_root / "checkpoints"
        local_onnx_dir = local_runtime_root / "onnx"
        local_checkpoint_dir.mkdir(parents=True, exist_ok=True)
        local_onnx_dir.mkdir(parents=True, exist_ok=True)
        localized_resume_state = _localize_resume_state(
            resume_state=resume_state,
            local_checkpoint_dir=local_checkpoint_dir,
        )
        pretraining = self._resolve_pretraining(
            config=config,
            dataset_artifact=dataset_artifact,
            resume_state=localized_resume_state,
        )
        control.on_runtime_metadata({"pretraining": pretraining})

        try:
            config_path = self._build_backend_config(
                Config=Config,
                config=config,
                dataset_artifact=dataset_artifact,
                resume_state=localized_resume_state,
                pretraining=pretraining,
                runtime_work_dir=local_checkpoint_dir,
            )
            cfg = Config.fromfile(str(config_path))
            if (
                localized_resume_state is not None
                and localized_resume_state.checkpoint_path
                and localized_resume_state.metadata.get("post_training_recovery")
            ):
                return self._build_completed_resume_result(
                    Config=Config,
                    Runner=Runner,
                    config=config,
                    control=control,
                    config_path=config_path,
                    localized_resume_state=localized_resume_state,
                    persistent_checkpoint_path=resume_state.checkpoint_path if resume_state else None,
                    dataset_artifact=dataset_artifact,
                    local_onnx_dir=local_onnx_dir,
                    runtime_dependencies=runtime_dependencies,
                )
            progress_hook = _build_platform_training_hook(
                Hook=Hook,
                control=control,
                checkpoint_dir=config.checkpoint_dir,
                source_checkpoint_dir=local_checkpoint_dir,
                checkpoint_interval_steps=config.checkpoint_interval_steps,
                best_metric_name=config.best_metric_name,
                best_metric_mode=config.best_metric_mode,
                normalize_metrics=self._normalize_metrics,
            )
            runner = Runner.from_cfg(cfg)
            runner.register_hook(progress_hook, priority="LOWEST")
            runner.train()

            if progress_hook.cancelled:
                best_checkpoint_path = progress_hook.best_checkpoint_path
                latest_checkpoint_path = progress_hook.latest_checkpoint_path
                return TrainerResult(
                    final_epoch=progress_hook.last_epoch,
                    final_step=progress_hook.last_step,
                    best_metric_name=config.best_metric_name,
                    best_metric_value=float(
                        (progress_hook.best_metrics or progress_hook.last_validation_metrics).get(
                            config.best_metric_name,
                            0.0,
                        )
                    ),
                    best_metrics=progress_hook.best_metrics or progress_hook.last_validation_metrics,
                    final_validation_metrics=progress_hook.last_validation_metrics,
                    checkpoints=tuple(progress_hook.checkpoints),
                    dataset_artifact=dataset_artifact,
                    runtime_metadata={
                        "trainer_mode": "real",
                        "trainer_backend": self.trainer_backend,
                        "runtime_dependencies": runtime_dependencies,
                        "cancelled": True,
                    },
                    best_checkpoint_path=best_checkpoint_path,
                    latest_checkpoint_path=latest_checkpoint_path,
                    cancelled=True,
                )

            resume_local_checkpoint_path = (
                localized_resume_state.checkpoint_path if localized_resume_state else None
            )
            resume_checkpoint_path = resume_state.checkpoint_path if resume_state else None
            best_local_checkpoint_path = (
                progress_hook.best_source_checkpoint_path
                or progress_hook.latest_source_checkpoint_path
                or resume_local_checkpoint_path
            )
            best_checkpoint_path = (
                progress_hook.best_checkpoint_path
                or progress_hook.latest_checkpoint_path
                or resume_checkpoint_path
            )
            evaluation_metrics = self._evaluate_best_checkpoint(
                Config=Config,
                Runner=Runner,
                config_path=config_path,
                checkpoint_path=best_local_checkpoint_path,
            )
            onnx_local_path, onnx_path = self._export_onnx(
                config=config,
                config_path=config_path,
                checkpoint_path=best_local_checkpoint_path,
                dataset_artifact=dataset_artifact,
                local_output_dir=local_onnx_dir,
            )
            artifact_validation = build_artifact_validation_summary(
                checkpoint_path=Path(best_local_checkpoint_path) if best_local_checkpoint_path else None,
                onnx_path=onnx_local_path,
                trainer_mode="real",
                tensorrt_deferred=True,
            )

            return TrainerResult(
                final_epoch=max(
                    progress_hook.last_epoch,
                    localized_resume_state.epoch_no if localized_resume_state else 0,
                ),
                final_step=max(
                    progress_hook.last_step,
                    localized_resume_state.step_no if localized_resume_state else 0,
                ),
                best_metric_name=config.best_metric_name,
                best_metric_value=float(
                    evaluation_metrics.get(
                        config.best_metric_name,
                        progress_hook.best_metrics.get(config.best_metric_name, 0.0),
                    )
                ),
                best_metrics=evaluation_metrics or progress_hook.best_metrics,
                final_validation_metrics=evaluation_metrics or progress_hook.last_validation_metrics,
                checkpoints=tuple(progress_hook.checkpoints),
                dataset_artifact=dataset_artifact,
                export_artifacts=(
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
                ),
                artifact_validation=artifact_validation,
                runtime_metadata={
                    "trainer_mode": "real",
                    "trainer_backend": self.trainer_backend,
                    "runtime_dependencies": runtime_dependencies,
                    "resolved_backend_config_path": str(config_path),
                },
                best_checkpoint_path=best_checkpoint_path,
                latest_checkpoint_path=progress_hook.latest_checkpoint_path,
            )
        finally:
            shutil.rmtree(local_runtime_root, ignore_errors=True)

    def _validate_config(self, config: TrainerConfig) -> None:
        if config.task_type != TaskType.DETECTION:
            raise RuntimeError(
                f"Real {self.trainer_backend} training currently supports detection only, not {config.task_type.value}."
            )
        key = (
            config.architecture.family.lower(),
            config.architecture.version.lower(),
            config.architecture.size.lower(),
        )
        if key not in self._SUPPORTED_MODELS:
            raise RuntimeError(
                f"Architecture {config.architecture.family} {config.architecture.version} {config.architecture.size} "
                f"is not supported by {self.trainer_backend}."
            )

    def _runtime_dependency_metadata(self, config: TrainerConfig) -> dict[str, Any]:
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("PyTorch is required for real training.") from exc
        if config.gpu_device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(
                f"GPU device {config.gpu_device} was requested, but PyTorch CUDA is unavailable."
            )
        required_modules = (
            "albumentations",
            "mmcv",
            "mmengine",
            "mmdet",
            "mmyolo",
            "mmdeploy",
            "onnx",
            "onnxruntime",
            "numpy",
            "setuptools",
        )
        versions: dict[str, str] = {"torch": getattr(torch, "__version__", "unknown")}
        for module_name in required_modules:
            if importlib.util.find_spec(module_name) is None:
                raise RuntimeError(
                    f"Required real-training dependency {module_name!r} is missing."
                )
            try:
                versions[module_name] = importlib.metadata.version(module_name)
            except importlib.metadata.PackageNotFoundError:
                versions[module_name] = "unknown"
        _validate_supported_openmmlab_runtime(
            python_version=(sys.version_info.major, sys.version_info.minor),
            platform_name=sys.platform,
            mmcv_version=versions.get("mmcv", "unknown"),
            mmdet_version=versions.get("mmdet", "unknown"),
            numpy_version=versions.get("numpy", "unknown"),
            setuptools_version=versions.get("setuptools", "unknown"),
        )
        return {
            "backend_versions": versions,
            "gpu_device": config.gpu_device,
        }

    def _load_mmengine_symbols(self):  # noqa: ANN202
        from mmengine.config import Config
        from mmengine.hooks import Hook
        from mmengine.runner import Runner

        return Config, Runner, Hook

    def _build_backend_config(
        self,
        *,
        Config,  # noqa: ANN001
        config: TrainerConfig,
        dataset_artifact,
        resume_state: TrainerResumeState | None,
        pretraining: dict[str, Any] | None = None,
        runtime_work_dir: Path | None = None,
    ) -> Path:
        base_config = self._resolve_mmyolo_config(config)
        cfg = Config.fromfile(str(base_config))
        class_map = json.loads(Path(dataset_artifact.class_map_path).read_text(encoding="utf-8"))
        num_classes = len(class_map)
        metainfo = {
            "classes": tuple(class_map),
            "palette": _build_palette(num_classes),
        }
        cfg.default_scope = "mmyolo"
        cfg.work_dir = str(runtime_work_dir or config.checkpoint_dir)
        cfg.launcher = "none"
        cfg.resume = bool(resume_state and resume_state.checkpoint_path)
        cfg.load_from = (
            resume_state.checkpoint_path
            if resume_state and resume_state.checkpoint_path
            else (pretraining or {}).get("checkpoint")
        )

        if hasattr(cfg, "train_cfg") and isinstance(cfg.train_cfg, dict):
            cfg.train_cfg["max_epochs"] = config.max_epochs
            cfg.train_cfg["val_begin"] = 1
            cfg.train_cfg["val_interval"] = 1
        _rewrite_yolov8_training_schedule(
            cfg=cfg,
            max_epochs=config.max_epochs,
            steps_per_epoch=config.steps_per_epoch,
            recipe=config.resolved_recipe,
        )
        effective_batch_size = _resolve_effective_train_batch_size(
            requested_batch_size=config.batch_size,
            image_size=config.image_size,
            architecture_size=config.architecture.size,
            total_memory_gib=_gpu_total_memory_gib(config.gpu_device),
        )
        if effective_batch_size is not None:
            cfg.train_dataloader["batch_size"] = effective_batch_size
        cfg.val_dataloader["batch_size"] = 1
        cfg.test_dataloader["batch_size"] = 1
        _rewrite_dataset_config(
            cfg.train_dataloader["dataset"],
            data_root=dataset_artifact.stage_root,
            ann_file="annotations/instances_train.json",
            img_prefix="train/images/",
            metainfo=metainfo,
        )
        _rewrite_dataset_config(
            cfg.val_dataloader["dataset"],
            data_root=dataset_artifact.stage_root,
            ann_file="annotations/instances_val.json",
            img_prefix="val/images/",
            metainfo=metainfo,
        )
        _rewrite_dataset_config(
            cfg.test_dataloader["dataset"],
            data_root=dataset_artifact.stage_root,
            ann_file="annotations/instances_test.json",
            img_prefix="test/images/",
            metainfo=metainfo,
        )
        _rewrite_evaluator(cfg.val_evaluator, data_root=dataset_artifact.stage_root, ann_file="annotations/instances_val.json")
        _rewrite_evaluator(cfg.test_evaluator, data_root=dataset_artifact.stage_root, ann_file="annotations/instances_test.json")
        _rewrite_num_classes(cfg.model, num_classes)
        _rewrite_optimizer(
            cfg=cfg,
            learning_rate=config.learning_rate,
            optimizer_name=config.optimizer,
            batch_size_per_gpu=config.batch_size or effective_batch_size,
        )
        _configure_gradient_accumulation(
            cfg=cfg,
            requested_batch_size=config.batch_size,
            effective_batch_size=effective_batch_size,
        )
        if config.image_size:
            _rewrite_image_scale(cfg, int(config.image_size))
        _configure_early_stopping_hook(
            cfg=cfg,
            best_metric_name=config.best_metric_name,
            best_metric_mode=config.best_metric_mode,
            recipe=config.resolved_recipe,
        )
        # MMDetection 3.0.0's Albu wrapper forwards unsupported metadata keys such as
        # img_path into albumentations for this MMYOLO pipeline, so strip those
        # transforms from the generated config to keep the supported runtime stable.
        _strip_unsupported_albu_transforms(getattr(cfg, "_cfg_dict", cfg))

        checkpoint_hook = dict(cfg.default_hooks.get("checkpoint") or {})
        checkpoint_hook.update(
            {
                "type": "CheckpointHook",
                "by_epoch": False,
                "interval": max(int(config.checkpoint_interval_steps), 1),
                "save_last": True,
                "save_best": _backend_metric_name(config.best_metric_name),
                "rule": "greater" if config.best_metric_mode == "max" else "less",
                "filename_tmpl": "iter_{}.pth",
                "max_keep_ckpts": int(config.resolved_recipe.get("max_keep_ckpts") or 5),
            }
        )
        cfg.default_hooks["checkpoint"] = checkpoint_hook
        logger_hook = dict(cfg.default_hooks.get("logger") or {})
        logger_hook["interval"] = 1
        cfg.default_hooks["logger"] = logger_hook

        config_path = config.configs_dir / "resolved_openmmlab_detection.py"
        cfg.dump(str(config_path))
        return config_path

    def _resolve_pretraining(
        self,
        *,
        config: TrainerConfig,
        dataset_artifact: TrainerDatasetArtifact,
        resume_state: TrainerResumeState | None,
    ) -> dict[str, Any]:
        if resume_state is not None and resume_state.checkpoint_path:
            return {
                "enabled": False,
                "strategy": "resume_checkpoint",
                "reason": "A resume checkpoint is being loaded.",
                "checkpoint": None,
                "dataset_items": _dataset_item_count(dataset_artifact),
            }

        recipe = config.resolved_recipe
        raw_strategy = str(recipe.get("pretrained", "auto")).strip().lower()
        checkpoint_setting = str(recipe.get("pretrained_checkpoint", "auto")).strip()
        dataset_items = _dataset_item_count(dataset_artifact)
        scratch_min_items = max(int(recipe.get("scratch_training_min_items") or 2000), 1)

        if raw_strategy in {"false", "0", "no", "off", "scratch", "none"}:
            return {
                "enabled": False,
                "strategy": "scratch",
                "reason": "Recipe requested scratch training.",
                "checkpoint": None,
                "dataset_items": dataset_items,
                "scratch_training_min_items": scratch_min_items,
            }
        if raw_strategy == "auto" and dataset_items > scratch_min_items:
            return {
                "enabled": False,
                "strategy": "scratch",
                "reason": "Dataset size is above the scratch-training threshold.",
                "checkpoint": None,
                "dataset_items": dataset_items,
                "scratch_training_min_items": scratch_min_items,
            }

        requested_checkpoint = (
            self._pretrained_checkpoint_for_architecture(config)
            if checkpoint_setting.lower() == "auto"
            else checkpoint_setting
        )
        if not requested_checkpoint:
            return {
                "enabled": False,
                "strategy": "scratch",
                "reason": "No pretrained checkpoint is configured for this architecture.",
                "checkpoint": None,
                "dataset_items": dataset_items,
                "scratch_training_min_items": scratch_min_items,
            }
        checkpoint, resolution_metadata = _resolve_pretrained_checkpoint_source(
            requested_checkpoint,
            aliases=_openmmlab_checkpoint_aliases(config),
            context=(
                f"{config.task_type.value} "
                f"{config.architecture.family}{config.architecture.version}-{config.architecture.size}"
            ),
        )
        return {
            "enabled": True,
            "strategy": "transfer_learning" if raw_strategy == "auto" else "pretrained",
            "reason": "Dataset size is at or below the scratch-training threshold."
            if raw_strategy == "auto"
            else "Recipe requested pretrained initialization.",
            "checkpoint": checkpoint,
            "dataset_items": dataset_items,
            "scratch_training_min_items": scratch_min_items,
            **resolution_metadata,
        }

    def _pretrained_checkpoint_for_architecture(self, config: TrainerConfig) -> str | None:
        key = (
            config.architecture.family.lower(),
            config.architecture.version.lower(),
            config.architecture.size.lower(),
        )
        return self._PRETRAINED_CHECKPOINTS.get(key)

    def _resolve_mmyolo_config(self, config: TrainerConfig) -> Path:
        key = (
            config.architecture.family.lower(),
            config.architecture.version.lower(),
            config.architecture.size.lower(),
        )
        relative_config = self._SUPPORTED_MODELS[key]
        module = importlib.import_module("mmyolo")
        package_root = Path(module.__file__).resolve()
        candidates = [
            package_root.parent / ".mim" / relative_config,
            package_root.parent.parent / relative_config,
            package_root.parent.parent.parent / relative_config,
            package_root.parent / relative_config,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise RuntimeError(
            f"Could not locate the MMYOLO config {relative_config}. "
            "Install MMYOLO from source or a package that includes configs."
        )

    def _evaluate_best_checkpoint(
        self,
        *,
        Config,  # noqa: ANN001
        Runner,  # noqa: ANN001
        config_path: Path,
        checkpoint_path: str | None,
    ) -> dict[str, float]:
        if checkpoint_path is None:
            return {}
        cfg = Config.fromfile(str(config_path))
        cfg.load_from = checkpoint_path
        cfg.resume = False
        # The standalone validation runner must not try to save "best" checkpoints.
        # MMEngine initializes the CheckpointHook's file backend in train flows, but
        # runner.val() can still call after_val_epoch and hit an uninitialized hook.
        default_hooks = getattr(cfg, "default_hooks", None)
        if isinstance(default_hooks, MutableMapping):
            default_hooks.pop("checkpoint", None)
        runner = Runner.from_cfg(cfg)
        metrics = runner.val()
        return self._normalize_metrics(metrics or {})

    def _build_completed_resume_result(
        self,
        *,
        Config,  # noqa: ANN001
        Runner,  # noqa: ANN001
        config: TrainerConfig,
        control: TrainerControl,
        config_path: Path,
        localized_resume_state: TrainerResumeState,
        persistent_checkpoint_path: str | None,
        dataset_artifact,
        local_onnx_dir: Path,
        runtime_dependencies: dict[str, Any],
    ) -> TrainerResult:
        checkpoint_path = localized_resume_state.checkpoint_path
        if checkpoint_path is None:
            raise RuntimeError("Post-training recovery requires a checkpoint path.")
        published_checkpoint_path = persistent_checkpoint_path or checkpoint_path
        control.on_runtime_metadata(
            {
                "post_training_recovery": True,
                "recovery_checkpoint_path": published_checkpoint_path,
            }
        )
        evaluation_metrics = self._evaluate_best_checkpoint(
            Config=Config,
            Runner=Runner,
            config_path=config_path,
            checkpoint_path=checkpoint_path,
        )
        onnx_local_path, onnx_path = self._export_onnx(
            config=config,
            config_path=config_path,
            checkpoint_path=checkpoint_path,
            dataset_artifact=dataset_artifact,
            local_output_dir=local_onnx_dir,
        )
        artifact_validation = build_artifact_validation_summary(
            checkpoint_path=Path(checkpoint_path),
            onnx_path=onnx_local_path,
            trainer_mode="real",
            tensorrt_deferred=True,
        )
        best_metric_value = float(
            evaluation_metrics.get(
                config.best_metric_name,
                localized_resume_state.best_metric or 0.0,
            )
        )
        return TrainerResult(
            final_epoch=max(int(localized_resume_state.epoch_no or 0), 1),
            final_step=max(int(localized_resume_state.step_no or 0), 1),
            best_metric_name=config.best_metric_name,
            best_metric_value=best_metric_value,
            best_metrics=evaluation_metrics or {config.best_metric_name: best_metric_value},
            final_validation_metrics=evaluation_metrics or {config.best_metric_name: best_metric_value},
            checkpoints=(),
            dataset_artifact=dataset_artifact,
            export_artifacts=(
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
            ),
            artifact_validation=artifact_validation,
            runtime_metadata={
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "runtime_dependencies": runtime_dependencies,
                "resolved_backend_config_path": str(config_path),
                "post_training_recovery": True,
            },
            best_checkpoint_path=published_checkpoint_path,
            latest_checkpoint_path=published_checkpoint_path,
        )

    def _export_onnx(
        self,
        *,
        config: TrainerConfig,
        config_path: Path,
        checkpoint_path: str | None,
        dataset_artifact,
        local_output_dir: Path | None = None,
    ) -> tuple[Path, Path]:
        if checkpoint_path is None:
            raise RuntimeError("Real training completed without a best checkpoint to export.")
        try:
            from mmdeploy.apis import torch2onnx
        except ImportError as exc:
            raise RuntimeError("MMDeploy is required for real ONNX export.") from exc
        deploy_cfg = _resolve_mmdeploy_detection_onnxruntime_config()
        sample_image = _first_sample_image(dataset_artifact.stage_root)
        output_root = local_output_dir or config.onnx_dir
        output_path = output_root / "model.onnx"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        torch2onnx(
            sample_image,
            str(output_root),
            output_path.name,
            str(deploy_cfg),
            str(config_path),
            checkpoint_path,
            config.gpu_device,
        )
        published_output_path = _publish_file_to_persistent_path(
            output_path,
            config.onnx_dir / output_path.name,
            artifact_kind="ONNX export",
        )
        return output_path, published_output_path

    def _normalize_metrics(self, metrics: dict[str, Any]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        key_map = {
            "coco/bbox_mAP": "map_50_95",
            "bbox_mAP": "map_50_95",
            "coco/bbox_mAP_50": "map_50",
            "bbox_mAP_50": "map_50",
            "coco/bbox_mAR_100": "recall_at_100",
            "bbox_mAR_100": "recall_at_100",
        }
        for key, value in metrics.items():
            mapped_key = key_map.get(str(key))
            if mapped_key is None:
                continue
            normalized[mapped_key] = float(value)
        return normalized


class _PlatformTrainingHookMixin:
    priority = "LOWEST"

    def __init__(
        self,
        *,
        control: TrainerControl,
        checkpoint_dir: Path,
        source_checkpoint_dir: Path | None = None,
        checkpoint_interval_steps: int,
        best_metric_name: str,
        best_metric_mode: str,
        normalize_metrics,
    ) -> None:
        self.control = control
        self.checkpoint_dir = checkpoint_dir
        self.source_checkpoint_dir = source_checkpoint_dir or checkpoint_dir
        self.checkpoint_interval_steps = checkpoint_interval_steps
        self.best_metric_name = best_metric_name
        self.best_metric_mode = best_metric_mode
        self.normalize_metrics = normalize_metrics
        self.last_validation_metrics: dict[str, float] = {}
        self.best_metrics: dict[str, float] = {}
        self.best_metric_value: float | None = None
        self.last_epoch = 0
        self.last_step = 0
        self.cancelled = False
        self._seen_checkpoints: set[str] = set()
        self.checkpoints: list[TrainerCheckpointArtifact] = []
        self.best_checkpoint_path: str | None = None
        self.latest_checkpoint_path: str | None = None
        self.best_source_checkpoint_path: str | None = None
        self.latest_source_checkpoint_path: str | None = None

    def after_train_iter(self, runner, batch_idx: int, data_batch=None, outputs=None) -> None:  # noqa: ANN001
        step_no = int(getattr(runner, "iter", 0)) + 1
        epoch_no = int(getattr(runner, "epoch", 0)) + 1
        total_steps = int(getattr(runner, "max_iters", 0) or step_no)
        total_epochs = int(getattr(runner, "max_epochs", 0) or epoch_no)
        training_metrics = _extract_training_metrics(outputs)
        self.last_epoch = epoch_no
        self.last_step = step_no
        self.control.on_progress(
            TrainerProgress(
                epoch_no=epoch_no,
                step_no=step_no,
                total_epochs=total_epochs,
                total_steps=total_steps,
                training_metrics=training_metrics,
                validation_metrics=self.last_validation_metrics,
            )
        )
        self._record_new_checkpoints()
        if self.control.should_cancel() and not self.cancelled:
            checkpoint_name = f"cancel_iter_{step_no}.pth"
            runner.save_checkpoint(
                out_dir=str(self.source_checkpoint_dir),
                filename=checkpoint_name,
                save_optimizer=True,
                save_param_scheduler=True,
                by_epoch=False,
                meta={"cancel_requested": True},
            )
            self.cancelled = True
            setattr(runner.train_loop, "stop_training", True)
            self._record_new_checkpoints(force_kind="cancel")

    def after_val_epoch(self, runner, metrics: dict[str, Any] | None = None) -> None:  # noqa: ANN001
        normalized = self.normalize_metrics(metrics or {})
        self.last_validation_metrics = normalized
        candidate = normalized.get(self.best_metric_name)
        if candidate is not None and _metric_is_better(
            candidate,
            self.best_metric_value,
            self.best_metric_mode,
        ):
            self.best_metric_value = candidate
            self.best_metrics = dict(normalized)
        self._record_new_checkpoints()

    def _record_new_checkpoints(self, *, force_kind: str | None = None) -> None:
        if not self.source_checkpoint_dir.exists():
            return
        for path in sorted(self.source_checkpoint_dir.glob("*.pth"), key=lambda item: item.stat().st_mtime):
            source_resolved = str(path)
            if source_resolved in self._seen_checkpoints:
                continue
            published_path = _publish_file_to_persistent_path(
                path,
                self.checkpoint_dir / path.name,
                artifact_kind="training checkpoint",
            )
            _sync_last_checkpoint_sentinel(
                source_dir=self.source_checkpoint_dir,
                destination_dir=self.checkpoint_dir,
            )
            self._seen_checkpoints.add(source_resolved)
            name = path.name
            step_no = _step_from_checkpoint_name(name) or self.last_step
            checkpoint_kind = force_kind or ("best" if name.startswith("best_") else "interval")
            is_best = name.startswith("best_")
            checkpoint = TrainerCheckpointArtifact(
                path=str(published_path),
                epoch_no=self.last_epoch,
                step_no=step_no,
                metrics=dict(self.last_validation_metrics),
                checkpoint_kind=checkpoint_kind,
                is_best=is_best,
                resume_safe=True,
                metadata={
                    "real_training": True,
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "source_checkpoint_path": source_resolved,
                },
            )
            self.checkpoints.append(checkpoint)
            self.latest_checkpoint_path = str(published_path)
            self.latest_source_checkpoint_path = source_resolved
            if is_best:
                self.best_checkpoint_path = str(published_path)
                self.best_source_checkpoint_path = source_resolved
            self.control.on_checkpoint(checkpoint)


def _build_platform_training_hook(*, Hook, **kwargs):  # noqa: ANN202, N803
    class _PlatformTrainingHook(_PlatformTrainingHookMixin, Hook):
        def __init__(self, **hook_kwargs: Any) -> None:
            Hook.__init__(self)
            _PlatformTrainingHookMixin.__init__(self, **hook_kwargs)

    return _PlatformTrainingHook(**kwargs)


def _rewrite_dataset_config(
    dataset_cfg: dict[str, Any],
    *,
    data_root: str,
    ann_file: str,
    img_prefix: str,
    metainfo: dict[str, Any],
) -> None:
    target = dataset_cfg
    while isinstance(target, dict) and isinstance(target.get("dataset"), dict):
        target = target["dataset"]
    target["data_root"] = data_root
    target["ann_file"] = ann_file
    target["metainfo"] = metainfo
    target["data_prefix"] = {"img": img_prefix}
    target["filter_cfg"] = {"filter_empty_gt": False}


def _rewrite_evaluator(evaluator_cfg: Any, *, data_root: str, ann_file: str) -> None:
    if isinstance(evaluator_cfg, list):
        for item in evaluator_cfg:
            _rewrite_evaluator(item, data_root=data_root, ann_file=ann_file)
        return
    if not isinstance(evaluator_cfg, dict):
        return
    evaluator_cfg["ann_file"] = str(Path(data_root) / ann_file)


def _rewrite_num_classes(node: Any, num_classes: int) -> None:
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key == "num_classes":
                node[key] = num_classes
            else:
                _rewrite_num_classes(value, num_classes)
    elif isinstance(node, list):
        for value in node:
            _rewrite_num_classes(value, num_classes)


def _rewrite_optimizer(
    *,
    cfg,  # noqa: ANN001
    learning_rate: float | None,
    optimizer_name: str | None,
    batch_size_per_gpu: int | None = None,
) -> None:
    optim_wrapper = cfg.get("optim_wrapper")
    if not isinstance(optim_wrapper, dict):
        return
    optimizer_cfg = optim_wrapper.get("optimizer")
    if not isinstance(optimizer_cfg, dict):
        return
    if learning_rate is not None:
        optimizer_cfg["lr"] = float(learning_rate)
    if optimizer_name:
        optimizer_cfg["type"] = {
            "adam": "Adam",
            "adamw": "AdamW",
            "sgd": "SGD",
        }.get(str(optimizer_name).lower(), str(optimizer_name))
    if batch_size_per_gpu is not None:
        optimizer_cfg["batch_size_per_gpu"] = max(int(batch_size_per_gpu), 1)


def _rewrite_yolov8_training_schedule(
    *,
    cfg,  # noqa: ANN001
    max_epochs: int,
    steps_per_epoch: int,
    recipe: dict[str, Any],
) -> None:
    if "max_epochs" in cfg:
        cfg["max_epochs"] = int(max_epochs)
    close_mosaic_epochs = int(recipe.get("close_mosaic_epochs") or cfg.get("close_mosaic_epochs") or 10)
    if "close_mosaic_epochs" in cfg:
        cfg["close_mosaic_epochs"] = close_mosaic_epochs
    if isinstance(cfg.get("default_hooks"), dict):
        param_scheduler = cfg.default_hooks.get("param_scheduler")
        if isinstance(param_scheduler, dict):
            param_scheduler["max_epochs"] = int(max_epochs)
            if str(recipe.get("warmup_mim_iter", "auto")).lower() == "auto":
                param_scheduler["warmup_mim_iter"] = _resolve_warmup_iterations(
                    max_epochs=max_epochs,
                    steps_per_epoch=steps_per_epoch,
                )
            elif recipe.get("warmup_mim_iter") is not None:
                param_scheduler["warmup_mim_iter"] = max(int(recipe["warmup_mim_iter"]), 0)
    custom_hooks = cfg.get("custom_hooks")
    switch_epoch = max(int(max_epochs) - close_mosaic_epochs, 0)
    train_cfg = cfg.get("train_cfg")
    if isinstance(train_cfg, dict) and isinstance(train_cfg.get("dynamic_intervals"), list):
        rewritten_intervals = []
        for item in train_cfg["dynamic_intervals"]:
            interval = 1
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                interval = int(item[1])
            rewritten_intervals.append((switch_epoch, max(interval, 1)))
        if rewritten_intervals:
            train_cfg["dynamic_intervals"] = rewritten_intervals
    if isinstance(custom_hooks, list):
        for hook in custom_hooks:
            if isinstance(hook, dict) and str(hook.get("type") or "").endswith("PipelineSwitchHook"):
                hook["switch_epoch"] = switch_epoch


def _configure_early_stopping_hook(
    *,
    cfg,  # noqa: ANN001
    best_metric_name: str,
    best_metric_mode: str,
    recipe: dict[str, Any],
) -> None:
    patience = _early_stopping_patience(recipe)
    if patience is None:
        return
    min_delta = _early_stopping_min_delta(recipe)
    custom_hooks = list(cfg.get("custom_hooks") or [])
    custom_hooks.append(
        {
            "type": "EarlyStoppingHook",
            "monitor": _backend_metric_name(best_metric_name),
            "rule": "greater" if best_metric_mode == "max" else "less",
            "patience": patience,
            "min_delta": min_delta,
            "strict": False,
        }
    )
    cfg["custom_hooks"] = custom_hooks


def _early_stopping_patience(recipe: dict[str, Any]) -> int | None:
    raw_value = recipe.get("early_stopping_patience")
    if raw_value is None:
        raw_value = recipe.get("patience")
    if raw_value is None:
        return None
    try:
        patience = int(raw_value)
    except (TypeError, ValueError):
        return None
    return patience if patience > 0 else None


def _early_stopping_min_delta(recipe: dict[str, Any]) -> float:
    raw_value = recipe.get("early_stopping_min_delta")
    if raw_value is None:
        raw_value = recipe.get("min_delta")
    if raw_value is None:
        return 0.0
    try:
        return max(float(raw_value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _resolve_warmup_iterations(*, max_epochs: int, steps_per_epoch: int) -> int:
    total_steps = max(int(max_epochs), 1) * max(int(steps_per_epoch), 1)
    if total_steps <= 30:
        return max(total_steps // 5, 1)
    return max(min(max(int(steps_per_epoch) * 3, 10), total_steps // 3, 1000), 1)


def _configure_gradient_accumulation(*, cfg, requested_batch_size: int | None, effective_batch_size: int | None) -> None:  # noqa: ANN001
    if requested_batch_size is None or effective_batch_size is None:
        return
    requested = max(int(requested_batch_size), 1)
    effective = max(int(effective_batch_size), 1)
    accumulation_steps = max(math.ceil(requested / effective), 1)
    if accumulation_steps <= 1:
        return
    optim_wrapper = cfg.get("optim_wrapper")
    if not isinstance(optim_wrapper, dict):
        return
    optim_wrapper["accumulative_counts"] = accumulation_steps


def _resolve_effective_train_batch_size(
    *,
    requested_batch_size: int | None,
    image_size: int | None,
    architecture_size: str,
    total_memory_gib: float | None,
) -> int | None:
    if requested_batch_size is None:
        return None
    requested = max(int(requested_batch_size), 1)
    if total_memory_gib is None:
        return requested
    size_factor = {
        "n": 1.0,
        "s": 1.75,
        "m": 2.5,
        "l": 3.5,
        "x": 4.5,
    }.get(str(architecture_size).lower(), 2.0)
    pixel_factor = max((int(image_size or 640) / 640) ** 2, 1.0)
    memory_budget = (
        2.5
        if total_memory_gib <= 8.5
        else 4.5
        if total_memory_gib <= 12
        else 7.5
        if total_memory_gib <= 16
        else float(requested)
    )
    cap = max(int(memory_budget // (size_factor * pixel_factor)), 1)
    return min(requested, cap)


def _gpu_total_memory_gib(gpu_device: str) -> float | None:
    if not gpu_device.startswith("cuda"):
        return None
    try:
        import torch
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    try:
        device_index = int(gpu_device.split(":", maxsplit=1)[1]) if ":" in gpu_device else 0
    except ValueError:
        device_index = 0
    properties = torch.cuda.get_device_properties(device_index)
    return float(properties.total_memory) / float(1024**3)


def _rewrite_image_scale(cfg, image_size: int) -> None:  # noqa: ANN001
    for key in ("img_scale", "image_size", "input_size"):
        if key in cfg:
            cfg[key] = (image_size, image_size) if key == "img_scale" else image_size
    for pipeline_name in ("train_pipeline", "test_pipeline"):
        pipeline = cfg.get(pipeline_name)
        if not isinstance(pipeline, list):
            continue
        for item in pipeline:
            if not isinstance(item, dict):
                continue
            if "scale" in item:
                item["scale"] = (image_size, image_size)
            if "img_scale" in item:
                item["img_scale"] = (image_size, image_size)


def _strip_unsupported_albu_transforms(node: Any) -> Any:
    if hasattr(node, "_cfg_dict"):
        return _strip_unsupported_albu_transforms(node._cfg_dict)
    if isinstance(node, MutableSequence):
        filtered: list[Any] = []
        for item in node:
            if _is_albu_transform(item):
                continue
            filtered.append(_strip_unsupported_albu_transforms(item))
        node[:] = filtered
        return node
    if isinstance(node, MutableMapping):
        for key, value in list(node.items()):
            node[key] = _strip_unsupported_albu_transforms(value)
    return node


def _is_albu_transform(node: Any) -> bool:
    return isinstance(node, dict) and str(node.get("type") or "") in {"Albu", "mmdet.Albu"}


def _build_palette(num_classes: int) -> list[tuple[int, int, int]]:
    palette: list[tuple[int, int, int]] = []
    for index in range(num_classes):
        palette.append(
            (
                (37 * (index + 1)) % 255,
                (67 * (index + 3)) % 255,
                (97 * (index + 5)) % 255,
            )
        )
    return palette


def _backend_metric_name(metric_name: str) -> str:
    if metric_name == "map_50_95":
        return "coco/bbox_mAP"
    if metric_name == "map_50":
        return "coco/bbox_mAP_50"
    if metric_name == "recall_at_100":
        return "coco/bbox_mAR_100"
    return metric_name


def _dataset_item_count(dataset_artifact: TrainerDatasetArtifact) -> int:
    summary: dict[str, Any] = {}
    summary_path = Path(dataset_artifact.conversion_summary_path)
    if summary_path.exists():
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                summary = payload
        except (OSError, json.JSONDecodeError):
            summary = {}
    for key in ("submitted_labeled_item_count", "eligible_item_count"):
        value = summary.get(key)
        if value is not None:
            return max(int(value), 0)
    splits = summary.get("splits")
    if isinstance(splits, dict):
        total = 0
        for split_summary in splits.values():
            if isinstance(split_summary, dict):
                total += int(split_summary.get("images") or split_summary.get("items") or 0)
        if total:
            return total
    return 0


def _metric_is_better(candidate: float, current: float | None, mode: str) -> bool:
    if current is None:
        return True
    if mode == "min":
        return candidate < current
    return candidate > current


def _extract_training_metrics(outputs: Any) -> dict[str, float]:
    if not isinstance(outputs, dict):
        return {}
    metrics: dict[str, float] = {}
    log_vars = outputs.get("log_vars")
    if isinstance(log_vars, dict):
        for key, value in log_vars.items():
            if key.startswith("loss"):
                metrics[str(key)] = float(value)
    if not metrics and outputs.get("loss") is not None:
        metrics["loss"] = float(outputs["loss"])
    return metrics


def _step_from_checkpoint_name(name: str) -> int | None:
    stem = Path(name).stem
    if "iter_" not in stem:
        return None
    try:
        return int(stem.rsplit("iter_", maxsplit=1)[1])
    except ValueError:
        return None


def _resolve_mmdeploy_detection_onnxruntime_config() -> Path:
    search_plan = (
        (
            "mmyolo",
            (
                Path(".mim") / "configs" / "deploy" / "detection_onnxruntime_dynamic.py",
                Path("configs") / "deploy" / "detection_onnxruntime_dynamic.py",
            ),
        ),
        (
            "mmdeploy",
            (
                Path(".mim") / "configs" / "deploy" / "detection_onnxruntime_dynamic.py",
                Path("configs") / "deploy" / "detection_onnxruntime_dynamic.py",
                Path(".mim") / "configs" / "mmdet" / "detection" / "detection_onnxruntime_dynamic.py",
                Path("configs") / "mmdet" / "detection" / "detection_onnxruntime_dynamic.py",
            ),
        ),
    )
    for module_name, relative_paths in search_plan:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        package_root = Path(module.__file__).resolve()
        roots = (
            package_root.parent,
            package_root.parent.parent,
            package_root.parent.parent.parent,
        )
        for relative_path in relative_paths:
            for root in roots:
                candidate = root / relative_path
                if candidate.exists():
                    return candidate
    raise RuntimeError(
        "Could not locate MMDeploy detection_onnxruntime_dynamic.py. "
        "Install MMYOLO/MMDeploy from a package or source tree that includes deploy configs."
    )


def _first_sample_image(stage_root: str) -> str:
    for split in ("val", "train", "test"):
        split_root = Path(stage_root) / split / "images"
        if not split_root.exists():
            continue
        for candidate in split_root.rglob("*"):
            if candidate.is_file():
                return str(candidate)
    raise DatasetPreparationError("No staged image is available for ONNX export.")


def _localize_resume_state(
    *,
    resume_state: TrainerResumeState | None,
    local_checkpoint_dir: Path,
) -> TrainerResumeState | None:
    if resume_state is None or not resume_state.checkpoint_path:
        return resume_state
    source_checkpoint_path = Path(resume_state.checkpoint_path)
    if not source_checkpoint_path.exists():
        raise RuntimeError(
            f"Requested resume checkpoint {source_checkpoint_path} does not exist."
        )
    localized_checkpoint_path = _copy_file_with_retries(
        source_checkpoint_path,
        local_checkpoint_dir / source_checkpoint_path.name,
        artifact_kind="resume checkpoint",
        attempts=3,
    )
    return replace(resume_state, checkpoint_path=str(localized_checkpoint_path))


def _sync_last_checkpoint_sentinel(*, source_dir: Path, destination_dir: Path) -> None:
    if _paths_equivalent(source_dir, destination_dir):
        return
    source_path = source_dir / "last_checkpoint"
    if not source_path.exists():
        return
    _publish_file_to_persistent_path(
        source_path,
        destination_dir / source_path.name,
        artifact_kind="checkpoint pointer",
    )


def _publish_file_to_persistent_path(
    source_path: Path,
    destination_path: Path,
    *,
    artifact_kind: str,
) -> Path:
    if _paths_equivalent(source_path, destination_path):
        return destination_path
    return _copy_file_with_retries(
        source_path,
        destination_path,
        artifact_kind=artifact_kind,
        attempts=4,
        failure_hint=(
            " This usually indicates Docker bind-mount I/O instability on the persistent training directory. "
            "On Windows development machines keep the repository and training artifacts on WSL2 ext4, "
            "or publish the training state through a Docker-managed volume instead of a C: bind mount."
        ),
    )


def _copy_file_with_retries(
    source_path: Path,
    destination_path: Path,
    *,
    artifact_kind: str,
    attempts: int = 4,
    retry_delay_seconds: float = 0.75,
    failure_hint: str | None = None,
) -> Path:
    source_path = source_path.resolve()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source_size = source_path.stat().st_size
    last_error: OSError | None = None

    for attempt_no in range(1, max(int(attempts), 1) + 1):
        temp_path = destination_path.with_name(
            f".{destination_path.name}.{os.getpid()}.{attempt_no}.tmp"
        )
        try:
            with source_path.open("rb") as source_handle, temp_path.open("wb") as destination_handle:
                shutil.copyfileobj(source_handle, destination_handle, length=4 * 1024 * 1024)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
            copied_size = temp_path.stat().st_size
            if copied_size != source_size:
                raise OSError(
                    f"Copied {artifact_kind} size mismatch for {destination_path}: "
                    f"expected {source_size} bytes, wrote {copied_size} bytes."
                )
            os.replace(temp_path, destination_path)
            return destination_path
        except OSError as exc:
            last_error = exc
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            if attempt_no >= attempts:
                break
            time.sleep(retry_delay_seconds * attempt_no)

    hint = failure_hint or ""
    raise RuntimeError(
        f"Failed to persist {artifact_kind} from {source_path} to {destination_path} after {attempts} attempts.{hint}"
    ) from last_error


def _paths_equivalent(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return left.absolute() == right.absolute()


def _validate_supported_openmmlab_runtime(
    *,
    python_version: tuple[int, int],
    platform_name: str,
    mmcv_version: str,
    mmdet_version: str,
    numpy_version: str,
    setuptools_version: str,
) -> None:
    if platform_name != "linux":
        raise RuntimeError(
            "Real openmmlab-mmyolo training currently supports Linux worker runtimes only."
        )
    if python_version != (3, 11):
        raise RuntimeError(
            "Real openmmlab-mmyolo training currently requires the dedicated Python 3.11 "
            "training-worker image. Rebuild the worker from infra/docker/training-worker.Dockerfile."
        )
    parsed_mmcv = _parse_version_triplet(mmcv_version)
    if parsed_mmcv is None or not ((2, 0, 0) <= parsed_mmcv < (2, 1, 0)):
        raise RuntimeError(
            f"MMCV {mmcv_version} is not supported by the current MMYOLO runtime. "
            "Install mmcv>=2.0.0,<2.1.0."
        )
    parsed_mmdet = _parse_version_triplet(mmdet_version)
    if parsed_mmdet is None or not ((3, 0, 0) <= parsed_mmdet < (3, 1, 0)):
        raise RuntimeError(
            f"MMDetection {mmdet_version} is not supported by the current MMYOLO runtime. "
            "Install mmdet>=3.0.0,<3.1.0."
        )
    parsed_numpy = _parse_version_triplet(numpy_version)
    if parsed_numpy is None or parsed_numpy >= (2, 0, 0):
        raise RuntimeError(
            f"NumPy {numpy_version} is not supported by the current PyTorch/MMCV worker stack. "
            "Install numpy<2 and rebuild the training-worker image."
        )
    parsed_setuptools = _parse_version_triplet(setuptools_version)
    if parsed_setuptools is None or parsed_setuptools >= (81, 0, 0):
        raise RuntimeError(
            f"setuptools {setuptools_version} is not supported by torch 2.0.0 because it removes pkg_resources. "
            "Install setuptools<81 and rebuild the training-worker image."
        )


def _resolve_pretrained_checkpoint_source(
    checkpoint: str,
    *,
    aliases: tuple[str, ...] = (),
    context: str = "OpenMMLab detection",
) -> tuple[str, dict[str, Any]]:
    return require_local_pretrained_checkpoint(
        checkpoint,
        aliases=aliases,
        context=context,
    )


def _openmmlab_checkpoint_aliases(config: TrainerConfig) -> tuple[str, ...]:
    version = config.architecture.version.lower()
    size = config.architecture.size.lower()
    return (
        f"{config.task_type.value}-yolo{version}-{size}.pth",
        f"{config.task_type.value}-yolo-{version}-{size}.pth",
        f"yolo{version}-{size}.pth",
        f"yolov8-{size}.pth" if version == "v8" else f"yolo{version}-{size}.pth",
    )


def _pretrained_checkpoint_roots() -> tuple[Path, ...]:
    return pretrained_checkpoint_roots()


def _parse_version_triplet(version: str) -> tuple[int, int, int] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())
