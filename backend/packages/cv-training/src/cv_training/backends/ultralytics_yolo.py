from __future__ import annotations

import csv
import importlib.metadata
import importlib.util
import json
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml
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

ULTRALYTICS_YOLO_VERSIONS = {"v11", "v26"}
ULTRALYTICS_YOLO_OBB_VERSIONS = {"v26"}
ULTRALYTICS_YOLO_SEGMENTATION_VERSIONS = {"v8", *ULTRALYTICS_YOLO_VERSIONS}
ULTRALYTICS_YOLO_SIZES = {"n", "s", "m", "l", "x"}


class _YoloModel(Protocol):
    def train(self, **kwargs: object) -> object: ...

    def export(self, **kwargs: object) -> object: ...


class _YoloFactory(Protocol):
    def __call__(self, model: str) -> _YoloModel: ...


@dataclass(frozen=True)
class _UltralyticsDatasetSpec:
    data_arg: str
    root: Path
    metadata: dict[str, Any]


@dataclass(frozen=True)
class _UltralyticsMetricsSnapshot:
    best: dict[str, float]
    latest: dict[str, float]
    best_epoch: int | None = None
    latest_epoch: int | None = None


class UltralyticsYoloTrainer(BaseTrainer):
    @property
    def trainer_mode(self) -> str:
        return "real"

    @property
    def trainer_backend(self) -> str:
        return "ultralytics-yolo"

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
        dataset_spec = _prepare_ultralytics_dataset(
            config=config,
            dataset_artifact=dataset_artifact,
        )
        model_source, pretraining = _resolve_model_source(
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
                "ultralytics_dataset": dataset_spec.metadata,
                "pretraining": pretraining,
                **runtime_dependencies,
            }
        )

        yolo_cls = self._load_yolo_symbol()
        model = yolo_cls(model_source)
        _disable_ultralytics_mlflow_callbacks(model=model)
        bridge = _UltralyticsProgressBridge(
            config=config,
            control=control,
            dataset_spec=dataset_spec,
        )
        _register_progress_callbacks(model=model, bridge=bridge)
        train_kwargs = _build_train_kwargs(
            config=config,
            dataset_spec=dataset_spec,
            resume_state=resume_state,
        )
        with _WithoutEnv("MLFLOW_TRACKING_URI"):
            model.train(**train_kwargs)

        run_dir = _resolve_run_dir(
            model=model,
            fallback_root=config.workspace_dir / "ultralytics-runs",
        )
        metric_snapshot = _read_results_metrics_snapshot(
            run_dir=run_dir,
            task_type=config.task_type,
            best_metric_name=config.best_metric_name,
            best_metric_mode=config.best_metric_mode,
        )
        final_epoch = bridge.last_epoch or metric_snapshot.latest_epoch or int(config.max_epochs)
        final_step = bridge.last_step or (
            int(final_epoch) * max(int(config.steps_per_epoch), 1)
        )
        latest_metrics = metric_snapshot.latest or dict(bridge.last_validation_metrics)
        best_metrics = metric_snapshot.best or latest_metrics
        checkpoints = _publish_checkpoints(
            config=config,
            run_dir=run_dir,
            best_metrics=best_metrics,
            latest_metrics=latest_metrics,
            best_epoch_no=metric_snapshot.best_epoch or final_epoch,
            latest_epoch_no=metric_snapshot.latest_epoch or final_epoch,
            final_epoch_no=final_epoch,
            final_step_no=final_step,
        )
        best_checkpoint, latest_checkpoint, checkpoint_for_export = _select_checkpoint_for_export(
            checkpoints
        )
        if checkpoint_for_export is None:
            raise RuntimeError("Ultralytics training did not produce a usable checkpoint.")
        for checkpoint in checkpoints:
            control.on_checkpoint(checkpoint)

        onnx_path = _export_onnx(
            yolo_cls=yolo_cls,
            checkpoint_path=Path(checkpoint_for_export.path),
            config=config,
        )
        export_metrics = best_metrics if best_checkpoint is not None else latest_metrics
        artifact_validation = build_artifact_validation_summary(
            checkpoint_path=Path(checkpoint_for_export.path),
            onnx_path=onnx_path,
            trainer_mode="real",
            tensorrt_deferred=True,
        )
        export_artifacts = _export_artifacts(onnx_path, artifact_validation)

        if bridge.cancelled:
            return TrainerResult(
                final_epoch=bridge.last_epoch,
                final_step=bridge.last_step,
                best_metric_name=config.best_metric_name,
                best_metric_value=float(export_metrics.get(config.best_metric_name, 0.0)),
                best_metrics=best_metrics,
                final_validation_metrics=export_metrics,
                checkpoints=checkpoints,
                dataset_artifact=dataset_artifact,
                export_artifacts=export_artifacts,
                artifact_validation=artifact_validation,
                runtime_metadata={
                    "trainer_mode": "real",
                    "trainer_backend": self.trainer_backend,
                    "runtime_dependencies": runtime_dependencies,
                    "ultralytics_run_dir": str(run_dir),
                    "cancelled": True,
                    "deployable": True,
                    "exported_checkpoint_kind": checkpoint_for_export.checkpoint_kind,
                    "best_checkpoint_used_for_export": best_checkpoint is not None,
                },
                best_checkpoint_path=(
                    best_checkpoint.path if best_checkpoint else latest_checkpoint.path
                ),
                latest_checkpoint_path=latest_checkpoint.path if latest_checkpoint else None,
                cancelled=True,
            )

        final_metric = float(export_metrics.get(config.best_metric_name, 0.0))
        return TrainerResult(
            final_epoch=final_epoch,
            final_step=final_step,
            best_metric_name=config.best_metric_name,
            best_metric_value=final_metric,
            best_metrics=best_metrics,
            final_validation_metrics=export_metrics,
            checkpoints=checkpoints,
            dataset_artifact=dataset_artifact,
            export_artifacts=export_artifacts,
            artifact_validation=artifact_validation,
            runtime_metadata={
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "runtime_dependencies": runtime_dependencies,
                "ultralytics_run_dir": str(run_dir),
                "ultralytics_model_source": model_source,
                "ultralytics_dataset": dataset_spec.metadata,
                "exported_checkpoint_kind": checkpoint_for_export.checkpoint_kind,
                "best_checkpoint_used_for_export": best_checkpoint is not None,
                "checkpoint_reporting": {
                    "best_epoch": metric_snapshot.best_epoch,
                    "latest_epoch": metric_snapshot.latest_epoch,
                },
            },
            best_checkpoint_path=(
                best_checkpoint.path if best_checkpoint else latest_checkpoint.path
            ),
            latest_checkpoint_path=latest_checkpoint.path if latest_checkpoint else None,
        )

    def _validate_config(self, config: TrainerConfig) -> None:
        if config.task_type not in {
            TaskType.CLASSIFICATION,
            TaskType.DETECTION,
            TaskType.ORIENTED_DETECTION,
            TaskType.SEGMENTATION,
        }:
            raise RuntimeError(
                f"{self.trainer_backend} supports classification, detection, oriented detection, and segmentation, "
                f"not {config.task_type.value}."
            )
        key = (
            config.architecture.family.lower(),
            config.architecture.version.lower(),
            config.architecture.size.lower(),
        )
        supported_versions = (
            ULTRALYTICS_YOLO_OBB_VERSIONS
            if config.task_type == TaskType.ORIENTED_DETECTION
            else
            ULTRALYTICS_YOLO_SEGMENTATION_VERSIONS
            if config.task_type == TaskType.SEGMENTATION
            else ULTRALYTICS_YOLO_VERSIONS
        )
        if (
            key[0] != "yolo"
            or key[1] not in supported_versions
            or key[2] not in ULTRALYTICS_YOLO_SIZES
        ):
            raise RuntimeError(
                f"Architecture {config.architecture.backend_model_key} is not supported by "
                f"{self.trainer_backend}. Supported YOLO versions are v11/v26 for "
                "classification and detection, v26 for oriented detection, "
                "plus v8/v11/v26 for segmentation, "
                "in sizes n/s/m/l/x."
            )

    def _runtime_dependency_metadata(self, config: TrainerConfig) -> dict[str, Any]:
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("PyTorch is required for Ultralytics real training.") from exc
        if config.gpu_device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(
                f"GPU device {config.gpu_device} was requested, but PyTorch CUDA is unavailable."
            )

        required_modules = ("ultralytics", "torchvision", "onnx", "onnxruntime", "numpy", "PIL")
        versions: dict[str, str] = {"torch": getattr(torch, "__version__", "unknown")}
        for module_name in required_modules:
            if importlib.util.find_spec(module_name) is None:
                raise RuntimeError(
                    f"Required Ultralytics real-training dependency {module_name!r} is missing."
                )
            package_name = "Pillow" if module_name == "PIL" else module_name
            try:
                versions[module_name] = importlib.metadata.version(package_name)
            except importlib.metadata.PackageNotFoundError:
                versions[module_name] = "unknown"
        return {
            "backend_versions": versions,
            "gpu_device": config.gpu_device,
        }

    def _load_yolo_symbol(self) -> _YoloFactory:
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError(
                "The ultralytics package is required for YOLOv8 segmentation and "
                "YOLO11/YOLO26 training."
            ) from exc
        return YOLO


class _UltralyticsProgressBridge:
    def __init__(
        self,
        *,
        config: TrainerConfig,
        control: TrainerControl,
        dataset_spec: _UltralyticsDatasetSpec,
    ) -> None:
        self.config = config
        self.control = control
        self.dataset_spec = dataset_spec
        self.last_epoch = 0
        self.last_step = 0
        self.last_validation_metrics: dict[str, float] = {}
        self.cancelled = False

    def on_train_batch_end(self, trainer: object) -> None:
        if self.control.should_cancel():
            self.cancelled = True
            try:
                trainer.stop = True
            except Exception:
                return

    def on_train_epoch_end(self, trainer: object) -> None:
        epoch_no = int(getattr(trainer, "epoch", 0) or 0) + 1
        steps_per_epoch = max(int(getattr(trainer, "nb", 0) or self.config.steps_per_epoch), 1)
        step_no = max(epoch_no * steps_per_epoch, self.last_step)
        self.last_epoch = epoch_no
        self.last_step = step_no
        training_metrics = _normalize_loss_metrics(
            losses=getattr(trainer, "tloss", None),
            loss_names=getattr(trainer, "loss_names", None),
        )
        self.last_validation_metrics = _normalize_metrics(getattr(trainer, "metrics", {}) or {})
        self.control.on_progress(
            TrainerProgress(
                epoch_no=epoch_no,
                step_no=step_no,
                total_epochs=max(int(getattr(trainer, "epochs", 0) or self.config.max_epochs), 1),
                total_steps=max(
                    int(getattr(trainer, "epochs", 0) or self.config.max_epochs),
                    1,
                )
                * steps_per_epoch,
                training_metrics=training_metrics,
                validation_metrics=self.last_validation_metrics,
            )
        )
        if self.control.should_cancel():
            self.cancelled = True
            try:
                trainer.stop = True
            except Exception:
                return


def _register_progress_callbacks(*, model: object, bridge: _UltralyticsProgressBridge) -> None:
    add_callback = getattr(model, "add_callback", None)
    if add_callback is None:
        return
    add_callback("on_train_batch_end", bridge.on_train_batch_end)
    add_callback("on_train_epoch_end", bridge.on_train_epoch_end)


def _disable_ultralytics_mlflow_callbacks(*, model: object | None = None) -> None:
    # The platform owns MLflow run lifecycle and metric logging. Ultralytics'
    # optional callback starts its own active run, which can conflict with that.
    try:
        from ultralytics.utils.callbacks import mlflow as ultralytics_mlflow

        ultralytics_mlflow.callbacks = {}
        ultralytics_mlflow.mlflow = None
    except Exception:
        pass

    callbacks = getattr(model, "callbacks", None)
    if not isinstance(callbacks, dict):
        return
    for event, handlers in list(callbacks.items()):
        if not isinstance(handlers, list):
            continue
        callbacks[event] = [
            handler
            for handler in handlers
            if not _is_ultralytics_mlflow_callback(handler)
        ]


def _is_ultralytics_mlflow_callback(handler: object) -> bool:
    return str(getattr(handler, "__module__", "")).startswith(
        "ultralytics.utils.callbacks.mlflow"
    )


def _prepare_ultralytics_dataset(
    *,
    config: TrainerConfig,
    dataset_artifact: TrainerDatasetArtifact,
) -> _UltralyticsDatasetSpec:
    if config.task_type == TaskType.CLASSIFICATION:
        return _prepare_classification_dataset(config=config, dataset_artifact=dataset_artifact)
    if config.task_type == TaskType.ORIENTED_DETECTION:
        return _prepare_yolo_obb_label_dataset(config=config, dataset_artifact=dataset_artifact)
    return _prepare_yolo_label_dataset(config=config, dataset_artifact=dataset_artifact)


def _prepare_classification_dataset(
    *,
    config: TrainerConfig,
    dataset_artifact: TrainerDatasetArtifact,
) -> _UltralyticsDatasetSpec:
    class_names = _load_class_names(dataset_artifact)
    class_to_dir = {
        label: f"{index:03d}_{_safe_path_component(label)}"
        for index, label in enumerate(class_names)
    }
    output_root = config.workspace_dir / "ultralytics-dataset" / "classification"
    rows_by_split: dict[str, list[dict[str, str]]] = {
        split: [] for split in ("train", "val", "test")
    }
    stage_root = Path(dataset_artifact.stage_root)
    for split in ("train", "val", "test"):
        manifest_path = stage_root / "annotations" / f"{split}.jsonl"
        if not manifest_path.exists():
            continue
        for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            row = json.loads(raw_line)
            label = str(row.get("label") or "")
            if label not in class_to_dir:
                continue
            image = str(row.get("image") or "")
            if not image:
                continue
            rows_by_split[split].append({"label": label, "image": image})

    if not rows_by_split["train"]:
        raise DatasetPreparationError("Classification training requires labeled train images.")

    for split in ("train", "val", "test"):
        source_rows = rows_by_split[split] or rows_by_split["train"]
        for row in source_rows:
            source_relative = Path(row["image"])
            source_path = stage_root / source_relative
            target_relative = _classification_target_relative_path(source_relative)
            target_path = output_root / split / class_to_dir[row["label"]] / target_relative
            _link_or_copy(source_path, target_path)

    class_map_path = output_root / "class_map.json"
    class_map_path.parent.mkdir(parents=True, exist_ok=True)
    class_map_path.write_text(
        json.dumps(
            {
                "class_names": class_names,
                "ultralytics_class_dirs": class_to_dir,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return _UltralyticsDatasetSpec(
        data_arg=str(output_root),
        root=output_root,
        metadata={
            "format": "ultralytics_classification_imagefolder",
            "data": str(output_root),
            "class_names": class_names,
            "class_map_path": str(class_map_path),
            "splits": {
                split: len(rows_by_split[split] or rows_by_split["train"])
                for split in rows_by_split
            },
        },
    )


def _prepare_yolo_label_dataset(
    *,
    config: TrainerConfig,
    dataset_artifact: TrainerDatasetArtifact,
) -> _UltralyticsDatasetSpec:
    class_names = _load_class_names(dataset_artifact)
    stage_root = Path(dataset_artifact.stage_root)
    output_root = config.workspace_dir / "ultralytics-dataset" / config.task_type.value
    split_counts: dict[str, dict[str, int]] = {}
    non_empty_splits: list[str] = []

    for split in ("train", "val", "test"):
        annotation_path = stage_root / "annotations" / f"instances_{split}.json"
        payload = json.loads(annotation_path.read_text(encoding="utf-8"))
        images = payload.get("images") or []
        annotations = payload.get("annotations") or []
        images_by_id = {int(image["id"]): image for image in images if image.get("id") is not None}
        annotations_by_image: dict[int, list[dict[str, Any]]] = {
            image_id: [] for image_id in images_by_id
        }
        for annotation in annotations:
            image_id = int(annotation.get("image_id") or 0)
            if image_id in annotations_by_image:
                annotations_by_image[image_id].append(annotation)

        image_count = 0
        label_count = 0
        for image_id, image in images_by_id.items():
            file_name = str(image.get("file_name") or "")
            if not file_name:
                continue
            source_path = Path(dataset_artifact.metadata["images_root"][split]) / file_name
            target_image_path = output_root / "images" / split / file_name
            _link_or_copy(source_path, target_image_path)
            label_path = (output_root / "labels" / split / file_name).with_suffix(".txt")
            label_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                _annotation_to_yolo_line(
                    annotation=annotation,
                    task_type=config.task_type,
                    width=float(image.get("width") or 1),
                    height=float(image.get("height") or 1),
                    class_count=len(class_names),
                )
                for annotation in annotations_by_image.get(image_id, [])
            ]
            clean_lines = [line for line in lines if line]
            label_path.write_text("\n".join(clean_lines), encoding="utf-8")
            image_count += 1
            label_count += len(clean_lines)

        if image_count:
            non_empty_splits.append(split)
        split_counts[split] = {"images": image_count, "labels": label_count}

    if not split_counts["train"]["images"]:
        raise DatasetPreparationError(f"{config.task_type.value} training requires train images.")

    fallback_split = non_empty_splits[0]
    yaml_path = output_root / "data.yaml"
    yaml_payload = {
        "path": str(output_root),
        "train": "images/train",
        "val": "images/val" if split_counts["val"]["images"] else f"images/{fallback_split}",
        "test": "images/test" if split_counts["test"]["images"] else f"images/{fallback_split}",
        "names": class_names,
    }
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(yaml.safe_dump(yaml_payload, sort_keys=False), encoding="utf-8")
    return _UltralyticsDatasetSpec(
        data_arg=str(yaml_path),
        root=output_root,
        metadata={
            "format": "ultralytics_yolo_labels",
            "data_yaml": str(yaml_path),
            "class_names": class_names,
            "splits": split_counts,
        },
    )


def _prepare_yolo_obb_label_dataset(
    *,
    config: TrainerConfig,
    dataset_artifact: TrainerDatasetArtifact,
) -> _UltralyticsDatasetSpec:
    class_names = _load_class_names(dataset_artifact)
    output_root = config.workspace_dir / "ultralytics-dataset" / config.task_type.value
    images_root = dict(dataset_artifact.metadata.get("images_root") or {})
    labels_root = dict(dataset_artifact.metadata.get("labels_root") or {})
    split_counts: dict[str, dict[str, int]] = {}
    non_empty_splits: list[str] = []

    for split in ("train", "val", "test"):
        source_images_root = Path(str(images_root.get(split) or ""))
        source_labels_root = Path(str(labels_root.get(split) or ""))
        image_count = 0
        label_count = 0
        if source_images_root.exists():
            for source_path in source_images_root.rglob("*"):
                if not source_path.is_file():
                    continue
                relative_path = source_path.relative_to(source_images_root)
                _link_or_copy(source_path, output_root / "images" / split / relative_path)
                image_count += 1
        if source_labels_root.exists():
            for source_path in source_labels_root.rglob("*.txt"):
                relative_path = source_path.relative_to(source_labels_root)
                target_path = output_root / "labels" / split / relative_path
                _copy_file(source_path, target_path)
                lines = [
                    line
                    for line in source_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                label_count += len(lines)
        if image_count:
            non_empty_splits.append(split)
        split_counts[split] = {"images": image_count, "labels": label_count}

    if not split_counts["train"]["images"]:
        raise DatasetPreparationError("Oriented detection training requires train images.")

    fallback_split = non_empty_splits[0]
    yaml_path = output_root / "data.yaml"
    yaml_payload = {
        "path": str(output_root),
        "train": "images/train",
        "val": "images/val" if split_counts["val"]["images"] else f"images/{fallback_split}",
        "test": "images/test" if split_counts["test"]["images"] else f"images/{fallback_split}",
        "names": class_names,
    }
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(yaml.safe_dump(yaml_payload, sort_keys=False), encoding="utf-8")
    return _UltralyticsDatasetSpec(
        data_arg=str(yaml_path),
        root=output_root,
        metadata={
            "format": "ultralytics_yolo_obb_labels",
            "data_yaml": str(yaml_path),
            "class_names": class_names,
            "splits": split_counts,
        },
    )


def _annotation_to_yolo_line(
    *,
    annotation: dict[str, Any],
    task_type: TaskType,
    width: float,
    height: float,
    class_count: int,
) -> str | None:
    class_index = int(annotation.get("category_id") or 0) - 1
    if class_index < 0 or class_index >= class_count:
        return None
    if task_type == TaskType.SEGMENTATION:
        polygons = annotation.get("segmentation") or []
        if not polygons:
            return None
        polygon = next(
            (
                candidate
                for candidate in polygons
                if isinstance(candidate, list) and len(candidate) >= 6
            ),
            None,
        )
        if polygon is None:
            return None
        points = []
        for index in range(0, len(polygon) - 1, 2):
            points.append(_format_float(_clip(float(polygon[index]) / width)))
            points.append(_format_float(_clip(float(polygon[index + 1]) / height)))
        return " ".join([str(class_index), *points])

    bbox = annotation.get("bbox") or []
    if len(bbox) < 4:
        return None
    x, y, box_width, box_height = [float(value) for value in bbox[:4]]
    if box_width <= 0 or box_height <= 0:
        return None
    x_center = (x + box_width / 2.0) / width
    y_center = (y + box_height / 2.0) / height
    return " ".join(
        [
            str(class_index),
            _format_float(_clip(x_center)),
            _format_float(_clip(y_center)),
            _format_float(_clip(box_width / width)),
            _format_float(_clip(box_height / height)),
        ]
    )


def _build_train_kwargs(
    *,
    config: TrainerConfig,
    dataset_spec: _UltralyticsDatasetSpec,
    resume_state: TrainerResumeState | None,
) -> dict[str, Any]:
    recipe = config.resolved_recipe
    optimizer_settings = {
        **dict(recipe.get("optimizer_settings") or {}),
        **dict(config.optimizer_settings),
    }
    image_size = int(
        config.image_size or recipe.get("image_size") or _default_image_size(config.task_type)
    )
    save_period = max(
        math.ceil(int(config.checkpoint_interval_steps) / max(int(config.steps_per_epoch), 1)),
        1,
    )
    train_kwargs: dict[str, Any] = {
        "data": dataset_spec.data_arg,
        "epochs": max(int(config.max_epochs), 1),
        "imgsz": image_size,
        "batch": max(int(config.batch_size or recipe.get("batch_size") or 1), 1),
        "project": str(config.workspace_dir / "ultralytics-runs"),
        "name": config.training_job_id,
        "exist_ok": True,
        "device": _ultralytics_device(config.gpu_device),
        "save": True,
        "save_period": save_period,
        "workers": max(int(recipe.get("num_workers") or 0), 0),
        "verbose": False,
        "plots": False,
        "val": True,
    }
    if config.learning_rate is not None:
        train_kwargs["lr0"] = float(config.learning_rate)
    optimizer = str(config.optimizer or recipe.get("optimizer") or "auto")
    train_kwargs["optimizer"] = _ultralytics_optimizer_name(optimizer)
    if optimizer_settings.get("momentum") is not None:
        train_kwargs["momentum"] = float(optimizer_settings["momentum"])
    if optimizer_settings.get("weight_decay") is not None:
        train_kwargs["weight_decay"] = float(optimizer_settings["weight_decay"])
    early_stopping_patience = recipe.get("early_stopping_patience")
    if early_stopping_patience is None:
        early_stopping_patience = recipe.get("patience")
    if early_stopping_patience is not None:
        train_kwargs["patience"] = int(early_stopping_patience)
    if resume_state is not None and resume_state.checkpoint_path:
        train_kwargs["resume"] = True
    return train_kwargs


def _resolve_model_source(
    *,
    config: TrainerConfig,
    dataset_artifact: TrainerDatasetArtifact,
    resume_state: TrainerResumeState | None,
) -> tuple[str, dict[str, Any]]:
    dataset_items = _dataset_item_count(dataset_artifact)
    if resume_state is not None and resume_state.checkpoint_path:
        return (
            resume_state.checkpoint_path,
            {
                "enabled": False,
                "strategy": "resume_checkpoint",
                "checkpoint": resume_state.checkpoint_path,
                "dataset_items": dataset_items,
            },
        )
    base = _ultralytics_model_stem(config)
    recipe = config.resolved_recipe
    raw_pretrained = str(recipe.get("pretrained", "auto")).strip().lower()
    checkpoint = str(recipe.get("pretrained_checkpoint", "auto")).strip()
    scratch_min_items = max(int(recipe.get("scratch_training_min_items") or 2000), 1)
    if checkpoint and checkpoint.lower() not in {"auto", "none"}:
        checkpoint_source, checkpoint_metadata = _resolve_ultralytics_checkpoint_source(
            checkpoint,
            aliases=_ultralytics_checkpoint_aliases(config),
            context=f"{config.task_type.value} {base}",
        )
        return (
            checkpoint_source,
            {
                "enabled": True,
                "strategy": "custom_checkpoint",
                "reason": "Recipe provided a pretrained checkpoint path.",
                "checkpoint": checkpoint_source,
                "dataset_items": dataset_items,
                "scratch_training_min_items": scratch_min_items,
                "source": checkpoint_source,
                **checkpoint_metadata,
            },
        )
    if raw_pretrained in {"false", "scratch", "none", "0"}:
        return (
            f"{base}.yaml",
            {
                "enabled": False,
                "strategy": "scratch",
                "reason": "Recipe requested scratch training.",
                "checkpoint": None,
                "dataset_items": dataset_items,
                "scratch_training_min_items": scratch_min_items,
                "source": None,
            },
        )
    if raw_pretrained == "auto" and dataset_items > scratch_min_items:
        return (
            f"{base}.yaml",
            {
                "enabled": False,
                "strategy": "scratch",
                "reason": (
                    f"Dataset has {dataset_items} items; using scratch training above "
                    f"{scratch_min_items}."
                ),
                "checkpoint": None,
                "dataset_items": dataset_items,
                "scratch_training_min_items": scratch_min_items,
                "source": None,
            },
        )
    model_source, source_metadata = _resolve_ultralytics_checkpoint_source(
        f"{base}.pt",
        aliases=_ultralytics_checkpoint_aliases(config),
        context=f"{config.task_type.value} {base}",
    )
    return (
        model_source,
        {
            "enabled": True,
            "strategy": "transfer_learning" if raw_pretrained == "auto" else "pretrained",
            "reason": (
                f"Dataset has {dataset_items} items; using transfer learning at or below "
                f"{scratch_min_items}."
            )
            if raw_pretrained == "auto"
            else "Recipe requested pretrained initialization.",
            "checkpoint": model_source,
            "dataset_items": dataset_items,
            "scratch_training_min_items": scratch_min_items,
            "source": model_source,
            **source_metadata,
        },
    )


def _resolve_ultralytics_checkpoint_source(
    checkpoint: str,
    *,
    aliases: tuple[str, ...] = (),
    context: str = "Ultralytics model",
) -> tuple[str, dict[str, Any]]:
    return require_local_pretrained_checkpoint(
        checkpoint,
        aliases=aliases,
        context=context,
    )


def _pretrained_checkpoint_roots() -> tuple[Path, ...]:
    return pretrained_checkpoint_roots()


def _ultralytics_checkpoint_aliases(config: TrainerConfig) -> tuple[str, ...]:
    task_type = config.task_type.value
    version = config.architecture.version.lower()
    size = config.architecture.size.lower()
    return (
        f"{task_type}-yolo{version}-{size}.pt",
        f"{task_type}-yolo-{version}-{size}.pt",
    )


def _ultralytics_model_stem(config: TrainerConfig) -> str:
    raw_version = config.architecture.version.lower()
    version = raw_version if raw_version == "v8" else raw_version.lstrip("v")
    size = config.architecture.size.lower()
    stem = f"yolo{version}{size}"
    if config.task_type == TaskType.CLASSIFICATION:
        return f"{stem}-cls"
    if config.task_type == TaskType.ORIENTED_DETECTION:
        return f"{stem}-obb"
    if config.task_type == TaskType.SEGMENTATION:
        return f"{stem}-seg"
    return stem


def _publish_checkpoints(
    *,
    config: TrainerConfig,
    run_dir: Path,
    best_metrics: dict[str, float],
    latest_metrics: dict[str, float],
    best_epoch_no: int,
    latest_epoch_no: int,
    final_epoch_no: int,
    final_step_no: int,
) -> tuple[TrainerCheckpointArtifact, ...]:
    weights_dir = run_dir / "weights"
    best_step_no = _step_for_epoch(
        epoch_no=best_epoch_no,
        final_epoch_no=final_epoch_no,
        final_step_no=final_step_no,
        fallback_steps_per_epoch=config.steps_per_epoch,
    )
    latest_step_no = _step_for_epoch(
        epoch_no=latest_epoch_no,
        final_epoch_no=final_epoch_no,
        final_step_no=final_step_no,
        fallback_steps_per_epoch=config.steps_per_epoch,
    )
    candidates = [
        ("best", weights_dir / "best.pt", True, best_metrics, best_epoch_no, best_step_no),
        ("last", weights_dir / "last.pt", False, latest_metrics, latest_epoch_no, latest_step_no),
    ]
    artifacts: list[TrainerCheckpointArtifact] = []
    for kind, source_path, is_best, metrics, epoch_no, step_no in candidates:
        if not source_path.exists():
            continue
        checkpoint_name = (
            f"{kind}_{config.task_type.value}_epoch_{max(epoch_no, 0):04d}_"
            f"step_{max(step_no, 0):06d}.pt"
        )
        target_path = config.checkpoint_dir / checkpoint_name
        _copy_file(source_path, target_path)
        artifacts.append(
            TrainerCheckpointArtifact(
                path=str(target_path),
                epoch_no=epoch_no,
                step_no=step_no,
                metrics=metrics,
                checkpoint_kind=kind,
                is_best=is_best,
                resume_safe=True,
                metadata={
                    "real_training": True,
                    "size_bytes": target_path.stat().st_size,
                    "sha256": sha256_file(target_path),
                    "source_checkpoint_path": str(source_path),
                    "trainer_backend": "ultralytics-yolo",
                    "reported_epoch": epoch_no,
                    "reported_step": step_no,
                },
            )
        )
    return tuple(artifacts)


def _select_checkpoint_for_export(
    checkpoints: tuple[TrainerCheckpointArtifact, ...],
) -> tuple[
    TrainerCheckpointArtifact | None,
    TrainerCheckpointArtifact | None,
    TrainerCheckpointArtifact | None,
]:
    best_checkpoint = next((item for item in checkpoints if item.is_best), None)
    latest_checkpoint = next(
        (item for item in reversed(checkpoints) if item.checkpoint_kind == "last"),
        checkpoints[-1] if checkpoints else None,
    )
    return best_checkpoint, latest_checkpoint, best_checkpoint or latest_checkpoint


def _step_for_epoch(
    *,
    epoch_no: int,
    final_epoch_no: int,
    final_step_no: int,
    fallback_steps_per_epoch: int,
) -> int:
    epoch = max(int(epoch_no), 0)
    if epoch == 0:
        return 0
    final_epoch = max(int(final_epoch_no), 0)
    if final_epoch > 0 and final_step_no > 0:
        return max(round((int(final_step_no) / final_epoch) * epoch), 1)
    return epoch * max(int(fallback_steps_per_epoch), 1)


def _export_onnx(
    *,
    yolo_cls: _YoloFactory,
    checkpoint_path: Path,
    config: TrainerConfig,
) -> Path:
    output_path = config.onnx_dir / "model.onnx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    model = yolo_cls(str(checkpoint_path))
    image_size = int(
        config.image_size
        or config.resolved_recipe.get("image_size")
        or _default_image_size(config.task_type)
    )
    exported = model.export(
        format="onnx",
        imgsz=image_size,
        opset=int(config.resolved_recipe.get("onnx_opset") or 13),
        dynamic=True,
        simplify=False,
    )
    exported_path = Path(str(exported)) if exported else checkpoint_path.with_suffix(".onnx")
    if exported_path != output_path:
        _copy_file(exported_path, output_path)
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


def _read_results_metrics(*, run_dir: Path, task_type: TaskType) -> dict[str, float]:
    return _read_results_metrics_snapshot(
        run_dir=run_dir,
        task_type=task_type,
        best_metric_name=None,
        best_metric_mode="max",
    ).latest


def _read_results_metrics_snapshot(
    *,
    run_dir: Path,
    task_type: TaskType,
    best_metric_name: str | None,
    best_metric_mode: str,
) -> _UltralyticsMetricsSnapshot:
    results_path = run_dir / "results.csv"
    if not results_path.exists():
        return _UltralyticsMetricsSnapshot(best={}, latest={})
    with results_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return _UltralyticsMetricsSnapshot(best={}, latest={})
    latest = _metrics_for_task(_normalize_metrics(rows[-1]), task_type=task_type)
    best_row = _select_best_results_row(
        rows=rows,
        task_type=task_type,
        best_metric_name=best_metric_name,
        best_metric_mode=best_metric_mode,
    )
    best = _metrics_for_task(_normalize_metrics(best_row), task_type=task_type)
    return _UltralyticsMetricsSnapshot(
        best=best,
        latest=latest,
        best_epoch=_epoch_no_from_results_row(best_row),
        latest_epoch=_epoch_no_from_results_row(rows[-1]),
    )


def _select_best_results_row(
    *,
    rows: list[dict[str, str]],
    task_type: TaskType,
    best_metric_name: str | None,
    best_metric_mode: str,
) -> dict[str, str]:
    if not best_metric_name:
        return rows[-1]
    selected = rows[-1]
    selected_value: float | None = None
    for row in rows:
        metrics = _metrics_for_task(_normalize_metrics(row), task_type=task_type)
        candidate = metrics.get(best_metric_name)
        if candidate is None:
            continue
        if selected_value is None or _metric_is_better(
            candidate,
            selected_value,
            best_metric_mode,
        ):
            selected = row
            selected_value = candidate
    return selected


def _metrics_for_task(normalized: dict[str, float], *, task_type: TaskType) -> dict[str, float]:
    if task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
        return {
            "map_50": normalized.get("map_50", 0.0),
            "map_50_95": normalized.get("map_50_95", 0.0),
            "recall_at_100": normalized.get("recall_at_100", normalized.get("recall", 0.0)),
        }
    if task_type == TaskType.SEGMENTATION:
        return {
            "mask_map_50": normalized.get("mask_map_50", 0.0),
            "mask_map_50_95": normalized.get("mask_map_50_95", 0.0),
            "recall_at_100": normalized.get("recall_at_100", normalized.get("recall", 0.0)),
        }
    return {
        "accuracy_top1": normalized.get("accuracy_top1", 0.0),
        "accuracy_top5": normalized.get("accuracy_top5", 0.0),
        "precision_macro": normalized.get("precision_macro", normalized.get("accuracy_top1", 0.0)),
        "recall_macro": normalized.get("recall_macro", normalized.get("accuracy_top1", 0.0)),
        "f1_macro": normalized.get("f1_macro", normalized.get("accuracy_top1", 0.0)),
    }


def _epoch_no_from_results_row(row: dict[str, str]) -> int | None:
    raw_value = row.get("epoch")
    if raw_value is None:
        return None
    try:
        epoch = int(float(str(raw_value).strip()))
    except (TypeError, ValueError):
        return None
    return epoch if epoch > 0 else epoch + 1


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
                        total += int(
                            split_summary.get("images")
                            or split_summary.get("items")
                            or 0
                        )
                if total:
                    return total
    return 0


def _normalize_metrics(raw_metrics: object) -> dict[str, float]:
    if not isinstance(raw_metrics, dict):
        return {}
    aliases = {
        "metrics/mAP50(B)": "map_50",
        "metrics/mAP50-95(B)": "map_50_95",
        "metrics/recall(B)": "recall_at_100",
        "metrics/mAP50(OBB)": "map_50",
        "metrics/mAP50-95(OBB)": "map_50_95",
        "metrics/recall(OBB)": "recall_at_100",
        "metrics/mAP50(M)": "mask_map_50",
        "metrics/mAP50-95(M)": "mask_map_50_95",
        "metrics/recall(M)": "recall_at_100",
        "metrics/accuracy_top1": "accuracy_top1",
        "metrics/accuracy_top5": "accuracy_top5",
        "fitness": "fitness",
    }
    metrics: dict[str, float] = {}
    for raw_key, raw_value in raw_metrics.items():
        key = aliases.get(str(raw_key).strip(), str(raw_key).strip())
        value = _float_or_none(raw_value)
        if value is not None:
            metrics[key] = value
    return metrics


def _normalize_loss_metrics(*, losses: object, loss_names: object) -> dict[str, float]:
    values = _as_float_list(losses)
    names = [str(item) for item in loss_names] if isinstance(loss_names, list | tuple) else []
    if not values:
        return {}
    if not names or len(names) != len(values):
        return {"loss": sum(values)}
    return {
        name if name.startswith("loss") else f"loss_{name}": value
        for name, value in zip(names, values, strict=False)
    }


def _resolve_run_dir(*, model: object, fallback_root: Path) -> Path:
    trainer = getattr(model, "trainer", None)
    save_dir = getattr(trainer, "save_dir", None)
    if save_dir is not None:
        return Path(str(save_dir))
    if fallback_root.exists():
        candidates = [path for path in fallback_root.iterdir() if path.is_dir()]
        if candidates:
            return max(candidates, key=lambda item: item.stat().st_mtime)
    return fallback_root


def _load_class_names(dataset_artifact: TrainerDatasetArtifact) -> list[str]:
    payload = json.loads(Path(dataset_artifact.class_map_path).read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise DatasetPreparationError("Ultralytics training requires a non-empty class map.")
    return [str(item) for item in payload]


def _link_or_copy(source_path: Path, target_path: Path) -> None:
    if not source_path.exists():
        raise DatasetPreparationError(f"Staged image {source_path} is missing.")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        return
    try:
        target_path.hardlink_to(source_path)
    except OSError:
        shutil.copy2(source_path, target_path)


def _copy_file(source_path: Path, target_path: Path) -> Path:
    if not source_path.exists():
        raise RuntimeError(f"Expected artifact {source_path} does not exist.")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.resolve(strict=False) == target_path.resolve(strict=False):
        return target_path
    shutil.copy2(source_path, target_path)
    return target_path


def _classification_target_relative_path(source_relative: Path) -> Path:
    parts = source_relative.parts
    candidate_parts = parts[2:] if len(parts) >= 3 else (source_relative.name,)
    if not candidate_parts:
        return Path(_safe_path_component(source_relative.name))
    safe_parts = [_safe_path_component(part) for part in candidate_parts[:-1]]
    file_path = Path(candidate_parts[-1])
    safe_name = f"{_safe_path_component(file_path.stem)}{file_path.suffix.lower()}"
    return Path("__".join([*safe_parts, safe_name]))


def _safe_path_component(value: str) -> str:
    sanitized = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_" for char in value.strip()
    )
    return sanitized or "class"


def _ultralytics_device(gpu_device: str) -> int | str:
    value = str(gpu_device or "cuda:0").lower()
    if value.startswith("cuda"):
        if ":" in value:
            try:
                return int(value.split(":", maxsplit=1)[1])
            except ValueError:
                return 0
        return 0
    return value


def _ultralytics_optimizer_name(value: str) -> str:
    return {
        "adam": "Adam",
        "adamw": "AdamW",
        "sgd": "SGD",
        "musgd": "MuSGD",
        "auto": "auto",
    }.get(value.lower(), value)


def _default_image_size(task_type: TaskType) -> int:
    if task_type == TaskType.CLASSIFICATION:
        return 224
    if task_type == TaskType.ORIENTED_DETECTION:
        return 1024
    return 640


def _clip(value: float) -> float:
    return max(min(value, 1.0), 0.0)


def _format_float(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _float_or_none(value: object) -> float | None:
    try:
        if hasattr(value, "item"):
            value = value.item()
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_is_better(candidate: float, current: float, mode: str) -> bool:
    if mode == "min":
        return candidate < current
    return candidate > current


def _as_float_list(value: object) -> list[float]:
    if value is None:
        return []
    if hasattr(value, "detach"):
        try:
            value = value.detach().cpu().tolist()
        except Exception:
            return []
    if isinstance(value, list | tuple):
        output: list[float] = []
        for item in value:
            parsed = _float_or_none(item)
            if parsed is not None:
                output.append(parsed)
        return output
    parsed = _float_or_none(value)
    return [] if parsed is None else [parsed]


class _WithoutEnv:
    def __init__(self, key: str) -> None:
        self.key = key
        self.previous: str | None = None

    def __enter__(self) -> None:
        self.previous = os.environ.pop(self.key, None)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.previous is not None:
            os.environ[self.key] = self.previous
