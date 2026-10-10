from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import socket
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv_core import PlatformSettings, load_platform_settings, prepare_runtime_paths
from cv_core.mlops import build_dvc_adapter, build_mlflow_adapter
from cv_db import (
    DatasetSnapshot,
    TrainingCheckpoint,
    claim_training_job,
    get_training_job_record,
    prune_training_checkpoints,
    record_training_checkpoint,
    record_training_metric,
    register_trained_model_version,
    release_training_job_claim,
    session_scope,
    update_training_job_heartbeat,
    update_training_job_state,
)
from cv_deploy import build_export_manifest_payload, build_training_artifact_manifest_payload
from cv_deploy.packaging import normalize_classification_export_manifest
from cv_status import QueueName, SnapshotStatus, TaskType, TrainingJobStatus
from cv_training.paths import (
    TrainingJobPaths,
    append_jsonl,
    build_training_job_paths,
    sha256_file,
    write_json,
)
from cv_training.recipes import load_training_recipe, merge_recipe_overrides
from cv_training.trainer import (
    BaseTrainer,
    ResolvedTrainerArchitecture,
    TrainerArtifactValidation,
    TrainerCheckpointArtifact,
    TrainerConfig,
    TrainerExportArtifact,
    TrainerProgress,
    TrainerResult,
    TrainerResumeState,
    get_default_trainer,
    resolve_trainer_architecture,
    resolve_trainer_mode,
)
from cv_utils import utc_now_iso

from training_worker.gpu_guard import gpu_admission_allowed

try:  # pragma: no cover - exercised by installed worker images.
    from redis import Redis
except ImportError:  # pragma: no cover - keeps module importable during lightweight checks.

    class Redis:  # type: ignore[no-redef]
        @classmethod
        def from_url(cls, _url: str) -> "Redis":
            raise TerminalTrainingError(
                "Redis is required by training-worker but the `redis` package is not installed.",
                failure_class="dependency_missing",
            )

        def set(self, *_args: Any, **_kwargs: Any) -> bool:
            return False

        def delete(self, *_args: Any, **_kwargs: Any) -> None:
            return None


try:  # pragma: no cover - RQ is absent in some unit-test import paths.
    from rq import Queue, Retry, get_current_job
except ImportError:  # pragma: no cover
    Queue = None  # type: ignore[assignment]
    Retry = None  # type: ignore[assignment]

    def get_current_job() -> Any | None:
        return None


SOURCE_SERVICE = "training-worker"
GPU_LOCK_KEY_PREFIX = "cv-platform:training:gpu"


class TrainingWorkflowError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure_class: str = "training_failed",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.metadata = metadata or {}


class TerminalTrainingError(TrainingWorkflowError):
    pass


class RetryableTrainingError(TrainingWorkflowError):
    def __init__(
        self,
        message: str,
        *,
        failure_class: str = "retryable_worker_error",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, failure_class=failure_class, metadata=metadata)


@dataclass(frozen=True)
class _GpuReservation:
    device_id: str
    lock_key: str


@dataclass(frozen=True)
class _TrainingJobContext:
    training_job_id: str
    dataset_snapshot_id: str
    model_family_slug: str
    task_type: TaskType
    architecture_family: str
    architecture_version: str
    architecture_size: str
    architecture_display_name: str
    recipe_name: str
    recipe_version: str
    max_epochs: int
    checkpoint_interval_steps: int
    learning_rate: float | None
    image_size: int | None
    batch_size: int | None
    optimizer: str | None
    optimizer_settings: dict[str, Any]
    best_checkpoint_rule: dict[str, Any]
    promotion_criteria: list[dict[str, Any]]
    parameter_overrides: dict[str, Any]
    config_snapshot: dict[str, Any]
    snapshot: DatasetSnapshot


def _load_worker_settings() -> PlatformSettings:
    return load_platform_settings(SOURCE_SERVICE)


def _training_runtime_setting(settings: Any, name: str, default: Any | None = None) -> Any:
    defaults = {
        "trainer_mode": "real",
        "gpu_devices": ["cuda:0"],
        "checkpoint_interval_steps": 25,
        "safe_retry_limit": 2,
        "heartbeat_interval_seconds": 5,
        "resume_from_latest_good_checkpoint": True,
        "simulation_mode_enabled": True,
        "engine_build_stage": "deployment",
        "export_opset_version": 13,
        "checkpoint_keep_count": 5,
        "default_step_duration_ms": 25,
    }
    fallback = default if default is not None else defaults.get(name)
    training = getattr(settings, "training", None)
    return getattr(training, name, fallback)


def run_training_workflow(
    training_job_id: str,
    *,
    resume_requested: bool = False,
) -> dict[str, str]:
    settings = _load_worker_settings()
    prepare_runtime_paths(settings)
    worker_instance_id = _worker_instance_id()
    paths = build_training_job_paths(settings, training_job_id).ensure()
    redis_client: Redis | None = None
    gpu_reservation: _GpuReservation | None = None
    mlflow_adapter: Any | None = None
    mlflow_run_id: str | None = None
    post_training_recovery_hint = False

    try:
        redis_client = Redis.from_url(settings.redis.url())
        workspace_root = _workspace_root()

        with session_scope(settings) as session:
            claimed = claim_training_job(
                session,
                training_job_id=training_job_id,
                worker_instance_id=worker_instance_id,
                source_service=SOURCE_SERVICE,
            )
            if claimed.status in {
                TrainingJobStatus.COMPLETED.value,
                TrainingJobStatus.CANCELLED.value,
                TrainingJobStatus.FAILED.value,
            }:
                return {"training_job_id": training_job_id, "status": claimed.status}
            post_training_recovery_hint = resume_requested and _progress_indicates_training_complete(
                claimed.progress
            )

        gpu_reservation = _reserve_gpu(
            settings=settings,
            redis_client=redis_client,
            training_job_id=training_job_id,
            worker_instance_id=worker_instance_id,
        )

        with session_scope(settings) as session:
            update_training_job_state(
                session,
                training_job_id=training_job_id,
                status=TrainingJobStatus.INITIALIZING,
                source_service=SOURCE_SERVICE,
                actor_type="system",
                actor_id=worker_instance_id,
                reason="Training worker claimed the job and is initializing runtime.",
                progress=_progress_payload(
                    status=TrainingJobStatus.INITIALIZING,
                    current_epoch=0,
                    current_step=0,
                    total_epochs=1,
                    total_steps=1,
                ),
                gpu_device=gpu_reservation.device_id,
                runtime_metadata={"worker_instance_id": worker_instance_id},
            )

        context = _load_job_context(settings, training_job_id)
        snapshot_root, snapshot_manifest_path = _validate_snapshot_context(
            settings=settings,
            context=context,
            worker_instance_id=worker_instance_id,
        )
        recipe = _load_and_merge_recipe(
            workspace_root=workspace_root,
            context=context,
            worker_instance_id=worker_instance_id,
            settings=settings,
        )
        architecture = _resolve_supported_architecture(context)
        trainer_mode = resolve_trainer_mode(
            str(
                context.parameter_overrides.get("trainer_mode")
                or _training_runtime_setting(settings, "trainer_mode")
                or "real"
            )
        )
        trainer = _select_trainer(
            task_type=context.task_type,
            trainer_mode=trainer_mode,
            architecture=architecture,
        )
        recipe = _recipe_with_trainer_backend(recipe, trainer=trainer)
        runtime_preflight = collect_dependency_preflight(
            settings=settings,
            workspace_root=workspace_root,
            task_type=context.task_type,
            recipe_name=context.recipe_name,
            snapshot_root=snapshot_root,
            require_openmmlab=_requires_openmmlab_preflight(
                trainer=trainer,
                trainer_mode=trainer_mode,
            ),
            require_torchvision=_requires_torchvision_preflight(
                trainer=trainer,
                trainer_mode=trainer_mode,
            ),
            require_ultralytics=_requires_ultralytics_preflight(
                trainer=trainer,
                trainer_mode=trainer_mode,
            ),
            require_anomalib=_requires_anomalib_preflight(
                trainer=trainer,
                trainer_mode=trainer_mode,
            ),
            require_temporal=_requires_temporal_preflight(
                trainer=trainer,
                trainer_mode=trainer_mode,
            ),
        )
        _merge_runtime_metadata(
            settings=settings,
            training_job_id=training_job_id,
            status=TrainingJobStatus.INITIALIZING,
            worker_instance_id=worker_instance_id,
            metadata={
                "preflight": runtime_preflight,
                "recipe_path": runtime_preflight.get("recipe_path"),
                "snapshot_root": str(snapshot_root),
                "snapshot_manifest_path": str(snapshot_manifest_path),
            },
        )

        dvc_adapter = build_dvc_adapter(settings)
        try:
            dvc_adapter.verify_runtime_prerequisites()
        except Exception as exc:
            raise TerminalTrainingError(
                f"Training runtime prerequisites are missing: {exc}",
                failure_class="dependency_missing",
            ) from exc

        mlflow_adapter = build_mlflow_adapter(settings)
        mlflow_run = mlflow_adapter.start_training_run(
            model_family_slug=context.model_family_slug,
            training_job_id=training_job_id,
            dataset_snapshot_id=context.dataset_snapshot_id,
            parameters=_mlflow_parameters(context=context, recipe=recipe, trainer_mode=trainer_mode),
            tags={
                "cv_platform.trainer_mode": trainer_mode,
                "cv_platform.trainer_backend": _trainer_backend_value(trainer),
            },
        )
        mlflow_run_id = mlflow_run.run_id

        config = _build_trainer_config(
            context=context,
            settings=settings,
            paths=paths,
            snapshot_root=snapshot_root,
            recipe=recipe,
            architecture=architecture,
            trainer=trainer,
            trainer_mode=trainer_mode,
            gpu_device=gpu_reservation.device_id,
            runtime_preflight=runtime_preflight,
        )
        resume_state = _build_resume_state(
            settings=settings,
            context=context,
            resume_requested=resume_requested,
            post_training_recovery_hint=post_training_recovery_hint,
        )
        controller = TrainingRunController(
            settings=settings,
            training_job_id=training_job_id,
            worker_instance_id=worker_instance_id,
            paths=paths,
            gpu_reservation=gpu_reservation,
            total_epochs=config.max_epochs,
            total_steps=config.max_epochs * config.steps_per_epoch,
            best_metric_name=config.best_metric_name,
            mlflow_adapter=mlflow_adapter,
            mlflow_run_id=mlflow_run_id,
        )

        with session_scope(settings) as session:
            update_training_job_state(
                session,
                training_job_id=training_job_id,
                status=TrainingJobStatus.PREPARING_DATA,
                source_service=SOURCE_SERVICE,
                actor_type="system",
                actor_id=worker_instance_id,
                reason="Preparing training workspace and dataset staging.",
                progress=_progress_payload(
                    status=TrainingJobStatus.PREPARING_DATA,
                    current_epoch=resume_state.epoch_no if resume_state else 0,
                    current_step=resume_state.step_no if resume_state else 0,
                    total_epochs=config.max_epochs,
                    total_steps=config.max_epochs * config.steps_per_epoch,
                ),
                gpu_device=gpu_reservation.device_id,
                mlflow_run_id=mlflow_run_id,
            )

        result = trainer.run(config=config, control=controller, resume_state=resume_state)
        cancelled = result.cancelled or controller.should_cancel()
        if cancelled and _cancelled_result_is_deployable(result=result, trainer=trainer):
            if trainer_mode == "real":
                _guard_against_simulation_result(result=result, trainer=trainer)
            final_artifacts = _validate_deployable_artifacts(
                result=result,
                trainer_mode=trainer_mode,
            )
            _persist_final_metrics(
                settings=settings,
                training_job_id=training_job_id,
                result=result,
            )
            registration = _register_successful_training(
                settings=settings,
                context=context,
                paths=paths,
                snapshot_root=snapshot_root,
                snapshot_manifest_path=snapshot_manifest_path,
                recipe=recipe,
                trainer=trainer,
                trainer_mode=trainer_mode,
                result=result,
                final_artifacts=final_artifacts,
                mlflow_adapter=mlflow_adapter,
                mlflow_run_id=mlflow_run_id,
                dvc_adapter=dvc_adapter,
                workspace_root=workspace_root,
                worker_instance_id=worker_instance_id,
                cancelled=True,
            )
            mlflow_adapter.finish_run(
                run_id=mlflow_run_id,
                status="KILLED",
                metrics=result.final_validation_metrics,
                tags={"cv_platform.lifecycle": "cancelled-deployable"},
            )
            return _complete_cancelled_job(
                settings=settings,
                training_job_id=training_job_id,
                worker_instance_id=worker_instance_id,
                controller=controller,
                result=result,
                model_version_id=registration["model_version_id"],
            )
        if cancelled:
            return _complete_cancelled_job(
                settings=settings,
                training_job_id=training_job_id,
                worker_instance_id=worker_instance_id,
                controller=controller,
                result=result,
            )

        if trainer_mode == "real":
            _guard_against_simulation_result(result=result, trainer=trainer)

        final_artifacts = _validate_deployable_artifacts(result=result, trainer_mode=trainer_mode)
        _persist_final_metrics(
            settings=settings,
            training_job_id=training_job_id,
            result=result,
        )
        registration = _register_successful_training(
            settings=settings,
            context=context,
            paths=paths,
            snapshot_root=snapshot_root,
            snapshot_manifest_path=snapshot_manifest_path,
            recipe=recipe,
            trainer=trainer,
            trainer_mode=trainer_mode,
            result=result,
            final_artifacts=final_artifacts,
            mlflow_adapter=mlflow_adapter,
            mlflow_run_id=mlflow_run_id,
            dvc_adapter=dvc_adapter,
            workspace_root=workspace_root,
            worker_instance_id=worker_instance_id,
        )
        mlflow_adapter.finish_run(
            run_id=mlflow_run_id,
            status="FINISHED",
            metrics=result.final_validation_metrics,
            tags={"cv_platform.lifecycle": "trained"},
        )
        return {
            "training_job_id": training_job_id,
            "status": TrainingJobStatus.COMPLETED.value,
            "model_version_id": registration["model_version_id"],
        }
    except RetryableTrainingError as exc:
        _handle_retryable_error(
            settings=settings,
            training_job_id=training_job_id,
            worker_instance_id=worker_instance_id,
            paths=paths,
            error=exc,
        )
        raise
    except TerminalTrainingError as exc:
        _mark_failed(
            settings=settings,
            training_job_id=training_job_id,
            worker_instance_id=worker_instance_id,
            paths=paths,
            failure_class=exc.failure_class,
            message=str(exc),
            metadata=exc.metadata,
        )
        _finish_mlflow_run_on_failure(
            mlflow_adapter=mlflow_adapter,
            mlflow_run_id=mlflow_run_id,
            failure_class=exc.failure_class,
        )
        raise
    except Exception as exc:
        failure_class = _classify_exception(exc)
        message = str(exc) or exc.__class__.__name__
        _mark_failed(
            settings=settings,
            training_job_id=training_job_id,
            worker_instance_id=worker_instance_id,
            paths=paths,
            failure_class=failure_class,
            message=message,
            metadata={"traceback": traceback.format_exc(limit=8)},
        )
        _finish_mlflow_run_on_failure(
            mlflow_adapter=mlflow_adapter,
            mlflow_run_id=mlflow_run_id,
            failure_class=failure_class,
        )
        raise TerminalTrainingError(message, failure_class=failure_class) from exc
    finally:
        if redis_client is not None and gpu_reservation is not None:
            _release_gpu(redis_client, gpu_reservation)
        try:
            with session_scope(settings) as session:
                release_training_job_claim(session, training_job_id=training_job_id)
        except Exception:
            pass


class TrainingRunController:
    def __init__(
        self,
        *,
        settings: PlatformSettings,
        training_job_id: str,
        worker_instance_id: str,
        paths: TrainingJobPaths,
        gpu_reservation: _GpuReservation,
        total_epochs: int,
        total_steps: int,
        best_metric_name: str,
        mlflow_adapter: Any | None = None,
        mlflow_run_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.training_job_id = training_job_id
        self.worker_instance_id = worker_instance_id
        self.paths = paths
        self.gpu_reservation = gpu_reservation
        self.total_epochs = total_epochs
        self.total_steps = total_steps
        self.best_metric_name = best_metric_name
        self.mlflow_adapter = mlflow_adapter
        self.mlflow_run_id = mlflow_run_id
        self.runtime_metadata: dict[str, Any] = {}
        self._last_mlflow_validation_metrics: dict[str, float] = {}
        self._mlflow_metric_logging_failed = False

    def should_cancel(self) -> bool:
        with session_scope(self.settings) as session:
            row = get_training_job_record(session, self.training_job_id)
            if row is None:
                return True
            return bool(row.cancel_requested) or row.status in {
                TrainingJobStatus.CANCEL_REQUESTED.value,
                TrainingJobStatus.CANCELLED.value,
            }

    def on_progress(self, progress: TrainerProgress) -> None:
        progress_payload = _progress_payload(
            status=TrainingJobStatus.TRAINING,
            current_epoch=progress.epoch_no,
            current_step=progress.step_no,
            total_epochs=progress.total_epochs,
            total_steps=progress.total_steps,
        )
        heartbeat_details = {
            "status": TrainingJobStatus.TRAINING.value,
            "epoch_no": progress.epoch_no,
            "step_no": progress.step_no,
            "total_epochs": progress.total_epochs,
            "total_steps": progress.total_steps,
            "training_metrics": dict(progress.training_metrics),
            "validation_metrics": dict(progress.validation_metrics),
        }
        _safe_append_jsonl(
            self.paths.metrics_file,
            {
                "recorded_at": utc_now_iso(),
                "epoch_no": progress.epoch_no,
                "step_no": progress.step_no,
                "training": progress.training_metrics,
                "validation": progress.validation_metrics,
            },
        )
        self._log_progress_metrics_to_mlflow(progress)
        with session_scope(self.settings) as session:
            update_training_job_heartbeat(
                session,
                training_job_id=self.training_job_id,
                worker_instance_id=self.worker_instance_id,
                details=heartbeat_details,
            )
            for metric_name, metric_value in progress.training_metrics.items():
                record_training_metric(
                    session,
                    training_job_id=self.training_job_id,
                    phase="training",
                    metric_name=str(metric_name),
                    metric_value=float(metric_value),
                    epoch_no=progress.epoch_no,
                    step_no=progress.step_no,
                )
            for metric_name, metric_value in progress.validation_metrics.items():
                record_training_metric(
                    session,
                    training_job_id=self.training_job_id,
                    phase="validation",
                    metric_name=str(metric_name),
                    metric_value=float(metric_value),
                    epoch_no=progress.epoch_no,
                    step_no=progress.step_no,
                    is_primary=str(metric_name) == self.best_metric_name,
                )
            update_training_job_state(
                session,
                training_job_id=self.training_job_id,
                status=TrainingJobStatus.TRAINING,
                source_service=SOURCE_SERVICE,
                actor_type="system",
                actor_id=self.worker_instance_id,
                progress=progress_payload,
                gpu_device=self.gpu_reservation.device_id,
            )

    def _log_progress_metrics_to_mlflow(self, progress: TrainerProgress) -> None:
        if self.mlflow_adapter is None or not self.mlflow_run_id:
            return
        log_metrics = getattr(self.mlflow_adapter, "log_metrics", None)
        if log_metrics is None:
            return
        try:
            metrics = {
                f"training/{metric_name}": float(metric_value)
                for metric_name, metric_value in progress.training_metrics.items()
            }
            validation_metrics = {
                f"validation/{metric_name}": float(metric_value)
                for metric_name, metric_value in progress.validation_metrics.items()
            }
            if validation_metrics and validation_metrics != self._last_mlflow_validation_metrics:
                metrics.update(validation_metrics)
                self._last_mlflow_validation_metrics = dict(validation_metrics)
            metrics["training/epoch"] = float(progress.epoch_no)
            log_metrics(run_id=self.mlflow_run_id, metrics=metrics, step=progress.step_no)
        except Exception as exc:
            if self._mlflow_metric_logging_failed:
                return
            self._mlflow_metric_logging_failed = True
            self.log(
                level="warning",
                event_type="training.mlflow_metrics_failed",
                message="Live MLflow metric logging failed; continuing with local metrics.",
                payload={"error": str(exc) or exc.__class__.__name__},
            )

    def on_checkpoint(self, checkpoint: TrainerCheckpointArtifact | None = None, **kwargs: Any) -> None:
        if checkpoint is None:
            checkpoint = TrainerCheckpointArtifact(
                path=str(kwargs.pop("path")),
                epoch_no=int(kwargs.pop("epoch_no")),
                step_no=int(kwargs.pop("step_no")),
                metrics={str(key): float(value) for key, value in dict(kwargs.pop("metrics", {})).items()},
                checkpoint_kind=str(kwargs.pop("checkpoint_kind")),
                is_best=bool(kwargs.pop("is_best", False)),
                resume_safe=bool(kwargs.pop("resume_safe", True)),
                metadata=dict(kwargs.pop("metadata", {})),
            )
        progress_payload = _progress_payload(
            status=TrainingJobStatus.CHECKPOINTING,
            current_epoch=checkpoint.epoch_no,
            current_step=checkpoint.step_no,
            total_epochs=self.total_epochs,
            total_steps=self.total_steps,
        )
        with session_scope(self.settings) as session:
            update_training_job_state(
                session,
                training_job_id=self.training_job_id,
                status=TrainingJobStatus.CHECKPOINTING,
                source_service=SOURCE_SERVICE,
                actor_type="system",
                actor_id=self.worker_instance_id,
                progress=progress_payload,
                gpu_device=self.gpu_reservation.device_id,
                reason=f"Persisting {checkpoint.checkpoint_kind} checkpoint.",
            )

        checkpoint_path = Path(checkpoint.path)
        if not checkpoint_path.exists():
            raise TerminalTrainingError(
                f"Trainer reported checkpoint {checkpoint_path}, but the file does not exist.",
                failure_class="checkpoint_missing",
            )
        checkpoint_sha256 = str(checkpoint.metadata.get("sha256") or "").strip() or sha256_file(
            checkpoint_path
        )

        with session_scope(self.settings) as session:
            checkpoint_row = record_training_checkpoint(
                session,
                training_job_id=self.training_job_id,
                epoch_no=checkpoint.epoch_no,
                step_no=checkpoint.step_no,
                storage_uri=str(checkpoint_path),
                sha256=checkpoint_sha256,
                checkpoint_kind=checkpoint.checkpoint_kind,
                metric_primary=float(checkpoint.metrics.get(self.best_metric_name, 0.0)),
                resume_safe=checkpoint.resume_safe,
                is_best=checkpoint.is_best,
                metrics=checkpoint.metrics,
            )
            update_training_job_state(
                session,
                training_job_id=self.training_job_id,
                status=TrainingJobStatus.TRAINING,
                source_service=SOURCE_SERVICE,
                actor_type="system",
                actor_id=self.worker_instance_id,
                progress=_progress_payload(
                    status=TrainingJobStatus.TRAINING,
                    current_epoch=checkpoint.epoch_no,
                    current_step=checkpoint.step_no,
                    total_epochs=self.total_epochs,
                    total_steps=self.total_steps,
                ),
                latest_checkpoint_id=checkpoint_row.id,
                best_checkpoint_id=checkpoint_row.id if checkpoint.is_best else None,
                gpu_device=self.gpu_reservation.device_id,
            )
            pruned_checkpoint_paths = prune_training_checkpoints(
                session,
                training_job_id=self.training_job_id,
                keep_count=_checkpoint_keep_count(self.settings),
            )
        deleted_checkpoint_paths, checkpoint_delete_errors = _delete_pruned_checkpoint_files(
            paths=pruned_checkpoint_paths,
            job_root=self.paths.job_root,
        )
        self.log(
            level="info",
            event_type="training.checkpoint",
            message="Checkpoint persisted.",
            payload={
                "checkpoint_kind": checkpoint.checkpoint_kind,
                "epoch_no": checkpoint.epoch_no,
                "step_no": checkpoint.step_no,
                "storage_uri": str(checkpoint_path),
                "is_best": checkpoint.is_best,
                "metrics": checkpoint.metrics,
                "pruned_checkpoints": deleted_checkpoint_paths,
            },
        )
        if checkpoint_delete_errors:
            self.log(
                level="warning",
                event_type="training.checkpoint_prune_failed",
                message="One or more stale checkpoint files could not be deleted.",
                payload={"errors": checkpoint_delete_errors},
            )

    def on_runtime_metadata(self, metadata: dict[str, Any]) -> None:
        self.runtime_metadata.update(_json_safe(metadata))
        with session_scope(self.settings) as session:
            row = get_training_job_record(session, self.training_job_id)
            status = TrainingJobStatus(row.status) if row is not None else TrainingJobStatus.TRAINING
            update_training_job_state(
                session,
                training_job_id=self.training_job_id,
                status=status,
                source_service=SOURCE_SERVICE,
                actor_type="system",
                actor_id=self.worker_instance_id,
                runtime_metadata=self.runtime_metadata,
            )

    def log(
        self,
        *,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        _safe_append_jsonl(
            self.paths.log_file,
            {
                "occurred_at": utc_now_iso(),
                "level": level,
                "event_type": event_type,
                "message": message,
                "payload": _json_safe(payload or {}),
            },
        )


def collect_dependency_preflight(
    *,
    settings: PlatformSettings,
    workspace_root: Path,
    task_type: TaskType | None = None,
    recipe_name: str | None = None,
    snapshot_root: Path | None = None,
    require_openmmlab: bool = False,
    require_torchvision: bool = False,
    require_ultralytics: bool = False,
    require_anomalib: bool = False,
    require_temporal: bool = False,
) -> dict[str, Any]:
    package_names = (
        "torch",
        "torchvision",
        "ultralytics",
        "anomalib",
        "openvino",
        "mmcv",
        "mmengine",
        "mmdet",
        "mmyolo",
        "mmdeploy",
        "onnx",
        "onnxruntime",
        "numpy",
        "setuptools",
        "PIL",
    )
    packages = {name: _package_status(name) for name in package_names}
    torch_cuda = _torch_cuda_status(packages["torch"]["available"])
    recipe_path = None
    if task_type is not None and recipe_name:
        recipe_path = _expected_recipe_path(workspace_root, task_type, recipe_name)
    metadata: dict[str, Any] = {
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
        },
        "os": {
            "platform": sys.platform,
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "packages": packages,
        "torch_cuda": torch_cuda,
        "mmyolo_config_files": _available_mmyolo_config_files(),
        "recipe_path": str(recipe_path) if recipe_path is not None else None,
        "recipe_exists": bool(recipe_path and recipe_path.exists()),
        "snapshot_root": str(snapshot_root) if snapshot_root is not None else None,
        "snapshot_exists": bool(snapshot_root and snapshot_root.exists()),
        "training_root": settings.storage.training_root,
    }
    if (
        not require_openmmlab
        and not require_torchvision
        and not require_ultralytics
        and not require_anomalib
        and not require_temporal
    ):
        return _json_safe(metadata)

    if (
        require_openmmlab
        or require_torchvision
        or require_ultralytics
        or require_anomalib
        or require_temporal
    ) and sys.platform != "linux":
        raise TerminalTrainingError(
            "Real training currently supports Linux worker runtimes only.",
            failure_class="unsupported_runtime",
            metadata=metadata,
        )
    if (
        require_openmmlab
        or require_torchvision
        or require_ultralytics
        or require_anomalib
        or require_temporal
    ) and sys.version_info[:2] != (3, 11):
        raise TerminalTrainingError(
            "Real training requires the dedicated Python 3.11 training-worker image.",
            failure_class="unsupported_runtime",
            metadata=metadata,
        )
    if require_temporal:
        required = ("torch", "onnx", "onnxruntime", "numpy")
        missing = [name for name in required if not packages[name]["available"]]
        if missing:
            raise TerminalTrainingError(
                "Required temporal-sequence real-training dependencies are missing: "
                f"{', '.join(missing)}.",
                failure_class="dependency_missing",
                metadata=metadata,
            )
        if not torch_cuda.get("cuda_available"):
            raise TerminalTrainingError(
                "PyTorch CUDA is unavailable in the temporal training-worker runtime.",
                failure_class="dependency_missing",
                metadata=metadata,
            )
        return _json_safe(metadata)
    if require_ultralytics:
        required = (
            "torch",
            "torchvision",
            "ultralytics",
            "PIL",
            "onnx",
            "onnxruntime",
            "numpy",
        )
        missing = [name for name in required if not packages[name]["available"]]
        if missing:
            raise TerminalTrainingError(
                "Required Ultralytics real-training dependencies are missing: "
                f"{', '.join(missing)}.",
                failure_class="dependency_missing",
                metadata=metadata,
            )
        if not torch_cuda.get("cuda_available"):
            raise TerminalTrainingError(
                "PyTorch CUDA is unavailable in the real training worker runtime.",
                failure_class="dependency_missing",
                metadata=metadata,
            )
        return _json_safe(metadata)
    if require_anomalib:
        required = (
            "torch",
            "torchvision",
            "anomalib",
            "openvino",
            "PIL",
            "onnx",
            "onnxruntime",
            "numpy",
        )
        missing = [name for name in required if not packages[name]["available"]]
        if missing:
            raise TerminalTrainingError(
                "Required Anomalib real-training dependencies are missing: "
                f"{', '.join(missing)}.",
                failure_class="dependency_missing",
                metadata=metadata,
            )
        if not torch_cuda.get("cuda_available"):
            raise TerminalTrainingError(
                "PyTorch CUDA is unavailable in the real training worker runtime.",
                failure_class="dependency_missing",
                metadata=metadata,
            )
        return _json_safe(metadata)
    if require_torchvision:
        required = ("torch", "torchvision", "PIL", "onnx", "onnxruntime", "numpy", "setuptools")
        missing = [name for name in required if not packages[name]["available"]]
        if missing:
            raise TerminalTrainingError(
                f"Required real-training dependencies are missing: {', '.join(missing)}.",
                failure_class="dependency_missing",
                metadata=metadata,
            )
        if not torch_cuda.get("cuda_available"):
            raise TerminalTrainingError(
                "PyTorch CUDA is unavailable in the real training worker runtime.",
                failure_class="dependency_missing",
                metadata=metadata,
            )
        return _json_safe(metadata)

    required = ("torch", "mmcv", "mmengine", "mmdet", "mmyolo", "mmdeploy", "onnx", "onnxruntime", "numpy", "setuptools")
    missing = [name for name in required if not packages[name]["available"]]
    if missing:
        raise TerminalTrainingError(
            f"Required real-training dependencies are missing: {', '.join(missing)}.",
            failure_class="dependency_missing",
            metadata=metadata,
        )
    if not torch_cuda.get("cuda_available"):
        raise TerminalTrainingError(
            "PyTorch CUDA is unavailable in the real training worker runtime.",
            failure_class="dependency_missing",
            metadata=metadata,
        )
    from cv_training.backends.openmmlab_detection import _validate_supported_openmmlab_runtime

    try:
        _validate_supported_openmmlab_runtime(
            python_version=sys.version_info[:2],
            platform_name=sys.platform,
            mmcv_version=str(packages["mmcv"]["version"]),
            mmdet_version=str(packages["mmdet"]["version"]),
            numpy_version=str(packages["numpy"]["version"]),
            setuptools_version=str(packages["setuptools"]["version"]),
        )
    except RuntimeError as exc:
        raise TerminalTrainingError(
            str(exc),
            failure_class="unsupported_runtime",
            metadata=metadata,
        ) from exc
    if not metadata["mmyolo_config_files"]:
        raise TerminalTrainingError(
            "MMYOLO is installed but no YOLOv8 configuration files were found. "
            "Install MMYOLO from source or a wheel that includes configs.",
            failure_class="dependency_missing",
            metadata=metadata,
        )
    return _json_safe(metadata)


def _load_job_context(settings: PlatformSettings, training_job_id: str) -> _TrainingJobContext:
    with session_scope(settings) as session:
        row = get_training_job_record(session, training_job_id)
        if row is None:
            raise TerminalTrainingError(
                f"Training job {training_job_id} does not exist.",
                failure_class="job_missing",
            )
        snapshot = session.get(DatasetSnapshot, row.dataset_snapshot_id)
        if snapshot is None:
            raise TerminalTrainingError(
                f"Dataset snapshot {row.dataset_snapshot_id} does not exist.",
                failure_class="dataset_invalid",
            )
        config_snapshot = dict(row.config_snapshot or {})
        return _TrainingJobContext(
            training_job_id=row.id,
            dataset_snapshot_id=row.dataset_snapshot_id,
            model_family_slug=row.model_family_slug,
            task_type=TaskType(row.task_type),
            architecture_family=row.architecture_family,
            architecture_version=row.architecture_version,
            architecture_size=row.architecture_size,
            architecture_display_name=row.architecture_display_name,
            recipe_name=row.recipe_name,
            recipe_version=row.recipe_version,
            max_epochs=int(config_snapshot.get("max_epochs") or 0) or 1,
            checkpoint_interval_steps=max(
                int(
                    config_snapshot.get("checkpoint_interval_steps")
                    or (config_snapshot.get("resolved_training_defaults") or {}).get(
                        "checkpoint_interval_steps"
                    )
                    or 1
                ),
                1,
            ),
            learning_rate=float(row.learning_rate) if row.learning_rate is not None else None,
            image_size=row.image_size,
            batch_size=row.batch_size,
            optimizer=row.optimizer,
            optimizer_settings=dict(row.optimizer_settings or {}),
            best_checkpoint_rule=dict(row.best_checkpoint_rule or {}),
            promotion_criteria=[dict(item) for item in (row.promotion_criteria or [])],
            parameter_overrides=dict(config_snapshot.get("parameter_overrides") or {}),
            config_snapshot=config_snapshot,
            snapshot=snapshot,
        )


def _validate_snapshot_context(
    *,
    settings: PlatformSettings,
    context: _TrainingJobContext,
    worker_instance_id: str,
) -> tuple[Path, Path]:
    snapshot = context.snapshot
    if snapshot.status != SnapshotStatus.READY.value:
        raise TerminalTrainingError(
            f"Dataset snapshot {snapshot.id} is {snapshot.status}; only ready snapshots can be trained.",
            failure_class="dataset_invalid",
        )
    snapshot_root = settings.storage.snapshot_root(snapshot.id)
    manifest_path = _resolve_snapshot_manifest_path(snapshot_root, snapshot.manifest_uri)
    if not snapshot_root.exists():
        raise TerminalTrainingError(
            f"Dataset snapshot root does not exist: {snapshot_root}",
            failure_class="dataset_invalid",
            metadata={"snapshot_root": str(snapshot_root)},
        )
    if not manifest_path.exists():
        raise TerminalTrainingError(
            f"Dataset snapshot manifest does not exist: {manifest_path}",
            failure_class="dataset_invalid",
            metadata={"snapshot_root": str(snapshot_root), "manifest_path": str(manifest_path)},
        )
    with session_scope(settings) as session:
        update_training_job_state(
            session,
            training_job_id=context.training_job_id,
            status=TrainingJobStatus.VALIDATING_SNAPSHOT,
            source_service=SOURCE_SERVICE,
            actor_type="system",
            actor_id=worker_instance_id,
            reason="Validated dataset snapshot root and manifest before training.",
            runtime_metadata={
                "snapshot_root": str(snapshot_root),
                "snapshot_manifest_path": str(manifest_path),
            },
        )
    return snapshot_root, manifest_path


def _load_and_merge_recipe(
    *,
    workspace_root: Path,
    context: _TrainingJobContext,
    worker_instance_id: str,
    settings: PlatformSettings,
) -> dict[str, Any]:
    try:
        recipe = load_training_recipe(
            workspace_root=workspace_root,
            task_type=context.task_type,
            recipe_name=context.recipe_name,
        )
    except ValueError as exc:
        raise TerminalTrainingError(str(exc), failure_class="recipe_missing") from exc
    merged = merge_recipe_overrides(recipe, context.parameter_overrides)
    if context.recipe_version and str(merged.get("recipe_version") or context.recipe_version) != context.recipe_version:
        merged["requested_recipe_version"] = context.recipe_version
    with session_scope(settings) as session:
        update_training_job_state(
            session,
            training_job_id=context.training_job_id,
            status=TrainingJobStatus.INITIALIZING,
            source_service=SOURCE_SERVICE,
            actor_type="system",
            actor_id=worker_instance_id,
            reason=f"Loaded training recipe {context.recipe_name}.",
            runtime_metadata={
                "recipe_name": context.recipe_name,
                "recipe_version": context.recipe_version,
                "resolved_recipe": _json_safe(merged),
            },
        )
    return merged


def _resolve_supported_architecture(context: _TrainingJobContext) -> ResolvedTrainerArchitecture:
    architecture = resolve_trainer_architecture(
        task_type=context.task_type,
        family=context.architecture_family,
        version=context.architecture_version,
        size=context.architecture_size,
        display_name=context.architecture_display_name,
    )
    if not architecture.supported:
        failure_class = (
            "unsupported_architecture"
            if context.task_type in {
                TaskType.CLASSIFICATION,
                TaskType.DETECTION,
                TaskType.ORIENTED_DETECTION,
                TaskType.SEGMENTATION,
                TaskType.ANOMALY,
                TaskType.TEMPORAL_SEQUENCE,
            }
            else "unsupported_task"
        )
        raise TerminalTrainingError(
            architecture.support_reason
            or f"Architecture {architecture.backend_model_key} is not supported.",
            failure_class=failure_class,
            metadata={"architecture": architecture.__dict__},
        )
    return architecture


def _select_trainer(
    *,
    task_type: TaskType,
    trainer_mode: str,
    architecture: ResolvedTrainerArchitecture,
) -> BaseTrainer:
    trainer = get_default_trainer(
        task_type,
        trainer_mode=trainer_mode,  # type: ignore[arg-type]
        architecture=architecture,
    )
    if trainer_mode != "simulation" and _trainer_mode_value(trainer) == "simulation":
        raise TerminalTrainingError(
            "Real training requested but the selected trainer is SimulationTrainer.",
            failure_class="simulation_not_allowed",
        )
    return trainer


def _build_trainer_config(
    *,
    context: _TrainingJobContext,
    settings: PlatformSettings,
    paths: TrainingJobPaths,
    snapshot_root: Path,
    recipe: dict[str, Any],
    architecture: ResolvedTrainerArchitecture,
    trainer: BaseTrainer,
    trainer_mode: str,
    gpu_device: str,
    runtime_preflight: dict[str, Any],
) -> TrainerConfig:
    batch_size = context.batch_size or _int_or_none(recipe.get("batch_size"))
    image_size = context.image_size or _int_or_none(recipe.get("image_size"))
    if context.task_type != TaskType.TEMPORAL_SEQUENCE:
        image_size = image_size or 640
    steps_per_epoch = _resolve_steps_per_epoch(
        recipe=recipe,
        item_count=int(context.snapshot.item_count or 1),
        batch_size=batch_size,
    )
    best_metric_name = str(
        context.best_checkpoint_rule.get("metric_name")
        or recipe.get("best_metric_name")
        or "map_50_95"
    )
    best_metric_mode = str(
        context.best_checkpoint_rule.get("mode") or recipe.get("best_metric_mode") or "max"
    )
    return TrainerConfig(
        training_job_id=context.training_job_id,
        dataset_snapshot_id=context.dataset_snapshot_id,
        task_type=context.task_type,
        trainer_mode=trainer_mode,  # type: ignore[arg-type]
        trainer_backend=_trainer_backend_value(trainer),
        architecture=architecture,
        snapshot_root=snapshot_root,
        workspace_dir=paths.workspace_dir,
        configs_dir=paths.configs_dir,
        checkpoint_dir=paths.checkpoints_dir,
        evaluation_dir=paths.evaluation_dir,
        onnx_dir=paths.onnx_dir,
        tensorrt_dir=paths.tensorrt_dir,
        max_epochs=int(context.max_epochs or recipe.get("epochs") or 1),
        steps_per_epoch=steps_per_epoch,
        checkpoint_interval_steps=int(
            context.checkpoint_interval_steps
            or recipe.get("checkpoint_interval_steps")
            or _training_runtime_setting(settings, "checkpoint_interval_steps")
            or 1
        ),
        best_metric_name=best_metric_name,
        best_metric_mode=best_metric_mode,
        resolved_recipe=recipe,
        runtime_metadata={
            "preflight": runtime_preflight,
            "requested_recipe_version": context.recipe_version,
            "parameter_overrides": context.parameter_overrides,
        },
        image_size=image_size,
        batch_size=batch_size,
        learning_rate=context.learning_rate or _float_or_none(recipe.get("learning_rate")),
        optimizer=context.optimizer or (str(recipe.get("optimizer")) if recipe.get("optimizer") else None),
        optimizer_settings=context.optimizer_settings,
        gpu_device=gpu_device,
    )


def _build_resume_state(
    *,
    settings: PlatformSettings,
    context: _TrainingJobContext,
    resume_requested: bool,
    post_training_recovery_hint: bool = False,
) -> TrainerResumeState | None:
    if not resume_requested and not _training_runtime_setting(
        settings,
        "resume_from_latest_good_checkpoint",
        True,
    ):
        return None
    with session_scope(settings) as session:
        row = get_training_job_record(session, context.training_job_id)
        if row is None or row.latest_checkpoint_id is None:
            return None
        post_training_recovery = post_training_recovery_hint or _progress_indicates_training_complete(
            row.progress
        )
        checkpoint_id = (
            row.best_checkpoint_id
            if resume_requested and post_training_recovery and row.best_checkpoint_id
            else row.latest_checkpoint_id
        )
        checkpoint = session.get(TrainingCheckpoint, checkpoint_id)
        if checkpoint is None or not checkpoint.resume_safe:
            return None
        progress = dict(row.progress or {})
        return TrainerResumeState(
            epoch_no=(
                int(progress.get("current_epoch") or 0)
                if post_training_recovery
                else int(checkpoint.epoch_no or 0)
            ),
            step_no=(
                int(progress.get("current_step") or 0)
                if post_training_recovery
                else int(checkpoint.step_no or 0)
            ),
            best_metric=float(checkpoint.metric_primary) if checkpoint.metric_primary is not None else None,
            checkpoint_path=checkpoint.storage_uri,
            metadata={
                "post_training_recovery": post_training_recovery,
                "checkpoint_id": checkpoint.id,
                "checkpoint_kind": checkpoint.checkpoint_kind,
                "is_best_checkpoint": bool(checkpoint.is_best),
            },
        )


def _progress_indicates_training_complete(progress: dict[str, Any] | None) -> bool:
    payload = dict(progress or {})
    try:
        if float(payload.get("progress_percent") or 0.0) >= 100.0:
            return True
    except (TypeError, ValueError):
        pass
    try:
        current_step = int(payload.get("current_step") or 0)
        total_steps = int(payload.get("total_steps") or 0)
    except (TypeError, ValueError):
        return False
    return total_steps > 0 and current_step >= total_steps


def _validate_deployable_artifacts(
    *,
    result: TrainerResult,
    trainer_mode: str,
) -> dict[str, Path | TrainerArtifactValidation]:
    onnx_artifact = _find_export_artifact(result.export_artifacts, "onnx")
    onnx_path = Path(onnx_artifact.path) if onnx_artifact and onnx_artifact.path else None
    checkpoint_path = Path(result.best_checkpoint_path or result.latest_checkpoint_path or "")
    if not checkpoint_path.exists():
        raise TerminalTrainingError(
            "Training completed without a persisted checkpoint artifact.",
            failure_class="export_failed",
            metadata={"checkpoint_path": str(checkpoint_path)},
        )
    if onnx_path is None or not onnx_path.exists() or onnx_path.stat().st_size <= 0:
        raise TerminalTrainingError(
            "Training completed but ONNX export is missing or empty.",
            failure_class="export_failed",
            metadata={"onnx_path": str(onnx_path) if onnx_path is not None else None},
        )
    if _looks_like_json_placeholder(onnx_path):
        raise TerminalTrainingError(
            "ONNX export is a JSON placeholder, not a deployable model artifact.",
            failure_class="export_failed",
            metadata={"onnx_path": str(onnx_path)},
        )
    if _looks_like_json_placeholder(checkpoint_path):
        raise TerminalTrainingError(
            "Checkpoint artifact is a JSON placeholder, not a real training checkpoint.",
            failure_class="export_failed",
            metadata={"checkpoint_path": str(checkpoint_path)},
        )
    artifact_validation = result.artifact_validation
    if artifact_validation is None:
        artifact_validation = TrainerArtifactValidation(
            status="passed" if trainer_mode == "real" else "simulation",
            checks={"checkpoint_valid": True, "onnx_valid": True},
            details={
                "checkpoint": {"size_bytes": checkpoint_path.stat().st_size},
                "onnx": {"size_bytes": onnx_path.stat().st_size},
            },
        )
    if trainer_mode == "real" and artifact_validation.status != "passed":
        raise TerminalTrainingError(
            "Training completed but deployable artifact validation failed.",
            failure_class="export_failed",
            metadata={"artifact_validation": artifact_validation.__dict__},
        )
    return {
        "onnx_path": onnx_path,
        "checkpoint_path": checkpoint_path,
        "artifact_validation": artifact_validation,
    }


def _cancelled_result_is_deployable(*, result: TrainerResult, trainer: BaseTrainer) -> bool:
    return (
        _trainer_backend_value(trainer) == "ultralytics-yolo"
        and bool(result.best_checkpoint_path or result.latest_checkpoint_path)
        and _find_export_artifact(result.export_artifacts, "onnx") is not None
    )


def _register_successful_training(
    *,
    settings: PlatformSettings,
    context: _TrainingJobContext,
    paths: TrainingJobPaths,
    snapshot_root: Path,
    snapshot_manifest_path: Path,
    recipe: dict[str, Any],
    trainer: BaseTrainer,
    trainer_mode: str,
    result: TrainerResult,
    final_artifacts: dict[str, Path | TrainerArtifactValidation],
    mlflow_adapter: Any,
    mlflow_run_id: str,
    dvc_adapter: Any,
    workspace_root: Path,
    worker_instance_id: str,
    cancelled: bool = False,
) -> dict[str, str]:
    onnx_path = final_artifacts["onnx_path"]
    checkpoint_path = final_artifacts["checkpoint_path"]
    artifact_validation = final_artifacts["artifact_validation"]
    assert isinstance(onnx_path, Path)
    assert isinstance(checkpoint_path, Path)
    assert isinstance(artifact_validation, TrainerArtifactValidation)

    temporal_training = context.task_type == TaskType.TEMPORAL_SEQUENCE
    if temporal_training:
        class_map, class_map_path = _load_temporal_head_order(result)
    else:
        class_map, class_map_path = _load_training_class_map(
            snapshot_root=snapshot_root,
            result=result,
        )
    class_map_sha256 = sha256_file(class_map_path)
    temporal_artifacts = _temporal_export_artifacts(result) if temporal_training else {}
    code_revision = dvc_adapter.current_code_revision(workspace_root)
    temporal_contract = (
        dict(json.loads(temporal_artifacts["temporal_contract"].read_text(encoding="utf-8")))
        if temporal_training
        else {}
    )
    angle_contract = (
        dict(json.loads(temporal_artifacts["angle_contract"].read_text(encoding="utf-8")))
        if temporal_training
        else {}
    )
    temporal_manifest = (
        {
            "schema_version": "temporal-model-artifacts.v1",
            "shadow_only": True,
            "automatic_production_promotion": False,
            "artifacts": {
                name: {
                    "uri": str(path),
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
                for name, path in temporal_artifacts.items()
            },
            "runtime_contract_implemented": True,
            "code_revision": code_revision,
            "contract": {
                "schema_version": temporal_contract.get("schema_version"),
                "schema_digest": temporal_contract.get("schema_digest"),
                "feature_set_id": temporal_contract.get("feature_set_id"),
                "feature_order": temporal_contract.get("feature_order"),
                "source_feature_schemas": temporal_contract.get("source_feature_schemas"),
                "rotation_schemas": temporal_contract.get("rotation_schemas"),
                "calibration_schemas": temporal_contract.get("calibration_schemas"),
                "annotation_schema": temporal_contract.get("annotation_schema"),
                "target_builders": temporal_contract.get("target_builders"),
                "minimum_window_steps": temporal_contract.get("minimum_window_steps"),
                "maximum_window_steps": temporal_contract.get("maximum_window_steps"),
                "padding": temporal_contract.get("padding"),
                "causal": temporal_contract.get("causal"),
                "input_end": temporal_contract.get("input_end"),
                "post_decision_frames_allowed": temporal_contract.get(
                    "post_decision_frames_allowed"
                ),
                "rotation_viewpoint_convention": temporal_contract.get(
                    "rotation_viewpoint_convention"
                ),
                "positive_rotation_direction": temporal_contract.get(
                    "positive_rotation_direction"
                ),
            },
            "angle_contract": angle_contract,
        }
        if temporal_training
        else {}
    )
    evaluation_summary = _build_evaluation_summary(
        context=context,
        result=result,
        artifact_validation=artifact_validation,
        trainer=trainer,
        trainer_mode=trainer_mode,
    )
    metrics_summary = {key: float(value) for key, value in result.final_validation_metrics.items()}
    if result.best_metric_name not in metrics_summary:
        metrics_summary[result.best_metric_name] = float(result.best_metric_value)
    write_json(paths.evaluation_summary_file, evaluation_summary)

    engine_build_manifest = _build_engine_manifest(settings)
    write_json(paths.engine_build_manifest_file, engine_build_manifest)
    export_manifest = build_export_manifest_payload(
        training_job_id=context.training_job_id,
        model_family_slug=context.model_family_slug,
        task_type=context.task_type.value,
        recipe_name=context.recipe_name,
        recipe_version=context.recipe_version,
        image_size=(
            None
            if temporal_training
            else int(recipe.get("image_size") or context.image_size or 640)
        ),
        snapshot_id=context.dataset_snapshot_id,
        snapshot_manifest_uri=str(snapshot_manifest_path),
        class_map=[] if temporal_training else class_map,
        class_map_sha256=None if temporal_training else class_map_sha256,
        io_schema=_build_io_schema(
            context.task_type,
            image_size=(
                None
                if temporal_training
                else int(recipe.get("image_size") or context.image_size or 640)
            ),
            class_count=len(class_map),
            onnx_path=onnx_path,
        ),
        preprocessing={
            **_build_preprocessing(
                recipe,
                trainer_backend=_trainer_backend_value(trainer),
                temporal_contract=temporal_contract,
            ),
            **_load_snapshot_image_view_preprocessing(snapshot_manifest_path),
        },
        postprocessing=_build_postprocessing(recipe, class_map),
        nms=_build_nms(recipe, task_type=context.task_type),
        hardware_compatibility=_build_hardware_compatibility(settings),
        build_manifest=engine_build_manifest,
        onnx_path=onnx_path,
        tensorrt_path=_find_existing_tensorrt(result),
        artifact_validation=artifact_validation.__dict__,
        temporal=temporal_manifest,
    )
    export_manifest.update(
        {
            "trainer_mode": trainer_mode,
            "trainer_backend": _trainer_backend_value(trainer),
            "cancelled": cancelled,
        }
    )
    export_manifest = normalize_classification_export_manifest(export_manifest, onnx_path)
    if context.task_type == TaskType.ANOMALY:
        anomaly_metadata = dict((result.runtime_metadata or {}).get("anomaly") or {})
        threshold = float(
            anomaly_metadata.get("threshold")
            or metrics_summary.get("threshold")
            or (export_manifest.get("postprocessing") or {}).get("confidence_threshold_default")
            or 0.5
        )
        export_manifest["anomaly"] = {
            "task_type": TaskType.ANOMALY.value,
            "backend": "anomalib",
            "method": anomaly_metadata.get("method") or "patchcore",
            "backbone": anomaly_metadata.get("backbone") or context.architecture_size,
            "heatmap_support": True,
            "score_output_support": True,
            "thresholding_support": True,
            "mask_support": False,
            "threshold": threshold,
            "dataset_format": anomaly_metadata.get("dataset_format"),
        }
        export_manifest.setdefault("postprocessing", {}).update(
            {
                "anomaly_method": export_manifest["anomaly"]["method"],
                "confidence_threshold_default": threshold,
                "threshold": threshold,
                "heatmap_colormap": "turbo",
                "heatmap_opacity_default": 0.55,
                "heatmap_support": True,
                "score_output_support": True,
                "thresholding_support": True,
                "mask_support": False,
            }
        )
    write_json(paths.export_manifest_file, export_manifest)

    artifact_manifest = build_training_artifact_manifest_payload(
        training_job_id=context.training_job_id,
        task_type=context.task_type.value,
        training_root=paths.job_root,
        evaluation_summary_path=paths.evaluation_summary_file,
        onnx_path=onnx_path,
        tensorrt_path=_find_existing_tensorrt(result),
        export_manifest_path=paths.export_manifest_file,
        export_manifest_payload=export_manifest,
        additional_artifacts=temporal_artifacts,
    )
    artifact_manifest.setdefault("metadata", {}).update(
        {
            "trainer_mode": trainer_mode,
            "trainer_backend": _trainer_backend_value(trainer),
            "simulation_only": trainer_mode == "simulation",
            "cancelled": cancelled,
        }
    )
    if context.task_type == TaskType.ANOMALY:
        artifact_manifest.setdefault("metadata", {})["anomaly"] = export_manifest.get("anomaly")
    if temporal_training:
        artifact_manifest.setdefault("metadata", {})["temporal"] = temporal_manifest
    write_json(paths.artifact_manifest_file, artifact_manifest)

    with session_scope(settings) as session:
        update_training_job_state(
            session,
            training_job_id=context.training_job_id,
            status=TrainingJobStatus.REGISTERING,
            source_service=SOURCE_SERVICE,
            actor_type="system",
            actor_id=worker_instance_id,
            reason="Registering trained model version.",
            evaluation_summary=evaluation_summary,
            metrics_summary=metrics_summary,
            artifact_manifest_uri=str(paths.artifact_manifest_file),
            runtime_metadata={
                "artifact_validation": artifact_validation.__dict__,
                "trainer_mode": trainer_mode,
                "trainer_backend": _trainer_backend_value(trainer),
            },
        )

    reproducibility_metadata = dvc_adapter.build_reproducibility_metadata(
        workspace_root=workspace_root,
        training_job_id=context.training_job_id,
    )
    registration_files = {
        "checkpoint": checkpoint_path,
        "onnx": onnx_path,
        "export_manifest": paths.export_manifest_file,
        "artifact_manifest": paths.artifact_manifest_file,
        "evaluation_summary": paths.evaluation_summary_file,
        **temporal_artifacts,
    }
    dvc_refs = dvc_adapter.capture_model_artifact_bundle(
        training_job_id=context.training_job_id,
        files=registration_files,
        metadata=reproducibility_metadata,
    )
    registration_artifact_path = mlflow_adapter.log_artifacts(
        run_id=mlflow_run_id,
        artifact_path="registration-bundle",
        file_paths=[
            checkpoint_path,
            onnx_path,
            paths.export_manifest_file,
            paths.artifact_manifest_file,
            paths.evaluation_summary_file,
            paths.engine_build_manifest_file,
            *temporal_artifacts.values(),
        ],
    )
    aliases = ["cancelled-checkpoint"] if cancelled else ["latest-trained", "trained"]
    lifecycle = "cancelled-deployable" if cancelled else "trained"
    if temporal_training and not cancelled:
        lifecycle = "trained-shadow"
    registry_tags = {
        "cv_platform.lifecycle": lifecycle,
        "cv_platform.trainer_mode": trainer_mode,
        "cv_platform.trainer_backend": _trainer_backend_value(trainer),
        "cv_platform.training_job_id": context.training_job_id,
        "cv_platform.shadow_only": str(temporal_training).lower(),
    }
    mlflow_registration = mlflow_adapter.register_model_version(
        registered_model_name=context.model_family_slug,
        source_uri=f"runs:/{mlflow_run_id}/{registration_artifact_path}",
        run_id=mlflow_run_id,
        aliases=aliases,
        tags=registry_tags,
    )
    immutable_metadata = {
        "training_job_id": context.training_job_id,
        "dataset_snapshot_id": context.dataset_snapshot_id,
        "trainer_mode": trainer_mode,
        "trainer_backend": _trainer_backend_value(trainer),
        "artifact_validation": artifact_validation.__dict__,
        "onnx_uri": str(onnx_path),
        "checkpoint_uri": str(checkpoint_path),
        "class_map_sha256": export_manifest.get("class_map_sha256"),
        "head_class_order_sha256": class_map_sha256 if temporal_training else None,
        "temporal_artifact_hashes": {
            name: sha256_file(path) for name, path in temporal_artifacts.items()
        },
        "cancelled": cancelled,
    }
    immutable_metadata_sha256 = hashlib.sha256(
        json.dumps(immutable_metadata, sort_keys=True).encode("utf-8")
    ).hexdigest()
    with session_scope(settings) as session:
        model_version = register_trained_model_version(
            session,
            training_job_id=context.training_job_id,
            metrics_summary=metrics_summary,
            evaluation_summary=evaluation_summary,
            artifact_manifest_uri=str(paths.artifact_manifest_file),
            artifact_manifest=artifact_manifest,
            export_manifest_uri=str(paths.export_manifest_file),
            export_manifest=export_manifest,
            onnx_uri=str(onnx_path),
            tensorrt_uri=None,
            dvc_rev=context.snapshot.dvc_rev,
            dvc_references=dvc_refs,
            reproducibility_metadata=reproducibility_metadata,
            code_revision=code_revision,
            mlflow_run_id=mlflow_run_id,
            mlflow_registered_model=mlflow_registration.registered_model_name,
            mlflow_model_version=mlflow_registration.model_version,
            alias_names=aliases,
            registry_tags=registry_tags,
            immutable_metadata=immutable_metadata,
            immutable_metadata_sha256=immutable_metadata_sha256,
        )
        if not cancelled:
            update_training_job_state(
                session,
                training_job_id=context.training_job_id,
                status=TrainingJobStatus.COMPLETED,
                source_service=SOURCE_SERVICE,
                actor_type="system",
                actor_id=worker_instance_id,
                reason=(
                    "Temporal training completed and shadow-only model version registered."
                    if temporal_training
                    else "Training completed and deployable model version registered."
                ),
                progress=_progress_payload(
                    status=TrainingJobStatus.COMPLETED,
                    current_epoch=result.final_epoch,
                    current_step=result.final_step,
                    total_epochs=max(result.final_epoch, 1),
                    total_steps=max(result.final_step, 1),
                ),
                evaluation_summary=evaluation_summary,
                metrics_summary=metrics_summary,
                artifact_manifest_uri=str(paths.artifact_manifest_file),
                model_version_id=model_version.id,
                mlflow_run_id=mlflow_run_id,
                runtime_metadata={
                    "trainer_mode": trainer_mode,
                    "trainer_backend": _trainer_backend_value(trainer),
                    "artifact_validation": artifact_validation.__dict__,
                    "export_manifest_uri": str(paths.export_manifest_file),
                    "onnx_uri": str(onnx_path),
                },
            )
    mlflow_adapter.sync_registered_model_metadata(
        registered_model_name=mlflow_registration.registered_model_name,
        model_version=mlflow_registration.model_version,
        aliases=aliases,
        tags=registry_tags,
    )
    if not cancelled and not temporal_training:
        _queue_post_training_automation(
            settings=settings,
            training_job_id=context.training_job_id,
            paths=paths,
            worker_instance_id=worker_instance_id,
        )
    elif not cancelled and temporal_training:
        _safe_append_jsonl(
            paths.log_file,
            {
                "occurred_at": utc_now_iso(),
                "level": "info",
                "event_type": "deployment.automation_skipped",
                "message": (
                    "Temporal-sequence artifact remains shadow-only; automatic deployment "
                    "promotion is disabled."
                ),
                "payload": {"training_job_id": context.training_job_id},
            },
        )
    return {"model_version_id": model_version.id}


def _queue_post_training_automation(
    *,
    settings: PlatformSettings,
    training_job_id: str,
    paths: TrainingJobPaths,
    worker_instance_id: str,
) -> None:
    if Queue is None:
        _safe_append_jsonl(
            paths.log_file,
            {
                "occurred_at": utc_now_iso(),
                "level": "warning",
                "event_type": "deployment.automation_not_queued",
                "message": "Post-training deployment automation could not be queued because RQ is unavailable.",
                "payload": {"training_job_id": training_job_id, "worker_instance_id": worker_instance_id},
            },
        )
        return
    try:
        retry = (
            Retry(max=2, interval=[10, 60])
            if Retry is not None
            else None
        )
        kwargs: dict[str, Any] = {
            "training_job_id": training_job_id,
            "job_timeout": settings.training.job_timeout_seconds,
        }
        if retry is not None:
            kwargs["retry"] = retry
        queued = Queue(
            QueueName.DEPLOY.value,
            connection=Redis.from_url(settings.redis.url()),
        ).enqueue(
            "control_worker.jobs.orchestration.run_post_training_automation",
            **kwargs,
        )
        _safe_append_jsonl(
            paths.log_file,
            {
                "occurred_at": utc_now_iso(),
                "level": "info",
                "event_type": "deployment.automation_queued",
                "message": "Post-training deployment automation was queued.",
                "payload": {
                    "training_job_id": training_job_id,
                    "queue_name": QueueName.DEPLOY.value,
                    "rq_job_id": getattr(queued, "id", None),
                    "worker_instance_id": worker_instance_id,
                },
            },
        )
    except Exception as exc:
        _safe_append_jsonl(
            paths.log_file,
            {
                "occurred_at": utc_now_iso(),
                "level": "warning",
                "event_type": "deployment.automation_not_queued",
                "message": "Post-training deployment automation could not be queued.",
                "payload": {
                    "training_job_id": training_job_id,
                    "worker_instance_id": worker_instance_id,
                    "error": str(exc),
                },
            },
        )


def _complete_cancelled_job(
    *,
    settings: PlatformSettings,
    training_job_id: str,
    worker_instance_id: str,
    controller: TrainingRunController,
    result: TrainerResult,
    model_version_id: str | None = None,
) -> dict[str, str]:
    with session_scope(settings) as session:
        update_training_job_state(
            session,
            training_job_id=training_job_id,
            status=TrainingJobStatus.CANCELLED,
            source_service=SOURCE_SERVICE,
            actor_type="system",
            actor_id=worker_instance_id,
            reason=(
                "Training job was cancelled; its best available checkpoint was registered "
                "as a deployable model version."
                if model_version_id
                else "Training job was cancelled by operator request."
            ),
            progress=_progress_payload(
                status=TrainingJobStatus.CANCELLED,
                current_epoch=result.final_epoch,
                current_step=result.final_step,
                total_epochs=controller.total_epochs,
                total_steps=controller.total_steps,
            ),
            model_version_id=model_version_id,
            runtime_metadata={
                "cancelled": True,
                "deployable": model_version_id is not None,
                "cancelled_model_version_id": model_version_id,
            },
        )
    response = {
        "training_job_id": training_job_id,
        "status": TrainingJobStatus.CANCELLED.value,
    }
    if model_version_id is not None:
        response["model_version_id"] = model_version_id
    return response


def _persist_final_metrics(
    *,
    settings: PlatformSettings,
    training_job_id: str,
    result: TrainerResult,
) -> None:
    with session_scope(settings) as session:
        for metric_name, metric_value in result.final_validation_metrics.items():
            record_training_metric(
                session,
                training_job_id=training_job_id,
                phase="evaluation",
                metric_name=str(metric_name),
                metric_value=float(metric_value),
                epoch_no=result.final_epoch,
                step_no=result.final_step,
                is_primary=str(metric_name) == result.best_metric_name,
            )


def _handle_retryable_error(
    *,
    settings: PlatformSettings,
    training_job_id: str,
    worker_instance_id: str,
    paths: TrainingJobPaths,
    error: RetryableTrainingError,
) -> None:
    job = get_current_job()
    retries_left = int(getattr(job, "retries_left", 0) or 0)
    retry_payload = {
        "failure_class": error.failure_class,
        "retries_left": retries_left,
        "metadata": _json_safe(error.metadata),
    }
    if retries_left > 0:
        _safe_append_jsonl(
            paths.log_file,
            {
                "occurred_at": utc_now_iso(),
                "level": "warning",
                "event_type": "training.retryable_failure",
                "message": str(error),
                "payload": retry_payload,
            },
        )
        with session_scope(settings) as session:
            update_training_job_state(
                session,
                training_job_id=training_job_id,
                status=TrainingJobStatus.QUEUED,
                source_service=SOURCE_SERVICE,
                actor_type="system",
                actor_id=worker_instance_id,
                reason=f"Retryable training failure; RQ retries left: {retries_left}.",
                failure_class=error.failure_class,
                last_error=str(error),
                runtime_metadata={
                    "retryable_failure": {"message": str(error), **retry_payload}
                },
            )
        return
    _mark_failed(
        settings=settings,
        training_job_id=training_job_id,
        worker_instance_id=worker_instance_id,
        paths=paths,
        failure_class=error.failure_class,
        message=str(error),
        metadata=error.metadata,
    )


def _mark_failed(
    *,
    settings: PlatformSettings,
    training_job_id: str,
    worker_instance_id: str,
    paths: TrainingJobPaths,
    failure_class: str,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    payload = {
        "failure_class": failure_class,
        "last_error": message,
        "metadata": _json_safe(metadata or {}),
    }
    _safe_append_jsonl(
        paths.log_file,
        {
            "occurred_at": utc_now_iso(),
            "level": "error",
            "event_type": "training.failed",
            "message": message,
            "payload": payload,
        },
    )
    with session_scope(settings) as session:
        update_training_job_state(
            session,
            training_job_id=training_job_id,
            status=TrainingJobStatus.FAILED,
            source_service=SOURCE_SERVICE,
            actor_type="system",
            actor_id=worker_instance_id,
            reason=message,
            failure_class=failure_class,
            last_error=message,
            runtime_metadata=payload,
        )


def _finish_mlflow_run_on_failure(
    *,
    mlflow_adapter: Any | None,
    mlflow_run_id: str | None,
    failure_class: str,
) -> None:
    if mlflow_adapter is None or not mlflow_run_id:
        return
    try:
        mlflow_adapter.finish_run(
            run_id=mlflow_run_id,
            status="FAILED",
            metrics={},
            tags={
                "cv_platform.lifecycle": "failed",
                "cv_platform.failure_class": failure_class,
            },
        )
    except Exception:
        pass


def _safe_append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    try:
        append_jsonl(path, payload)
    except OSError as exc:
        print(
            json.dumps(
                {
                    "level": "warning",
                    "event_type": "training.local_log_write_failed",
                    "path": str(path),
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )


def _checkpoint_keep_count(settings: PlatformSettings) -> int:
    return max(int(_training_runtime_setting(settings, "checkpoint_keep_count") or 5), 1)


def _delete_pruned_checkpoint_files(
    *,
    paths: list[str],
    job_root: Path,
) -> tuple[list[str], list[str]]:
    deleted: list[str] = []
    errors: list[str] = []
    job_root_resolved = job_root.resolve(strict=False)
    for raw_path in paths:
        path = Path(raw_path)
        resolved = path.resolve(strict=False)
        try:
            if not resolved.is_relative_to(job_root_resolved):
                errors.append(f"Skipped checkpoint outside job root: {path}")
                continue
            if not path.exists():
                continue
            if not path.is_file():
                errors.append(f"Skipped non-file checkpoint path: {path}")
                continue
            path.unlink()
            deleted.append(str(path))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: {exc}")
    return deleted, errors


def _reserve_gpu(
    *,
    settings: PlatformSettings,
    redis_client: Redis,
    training_job_id: str,
    worker_instance_id: str,
) -> _GpuReservation:
    if not gpu_admission_allowed(settings):
        raise RetryableTrainingError(
            "Training start is currently paused by GPU guardrails.",
            failure_class="gpu_unavailable",
        )
    devices = list(_training_runtime_setting(settings, "gpu_devices") or [])
    if not devices:
        raise TerminalTrainingError(
            "No GPU devices are configured for the training worker.",
            failure_class="unsupported_runtime",
        )
    ttl = max(int(_training_runtime_setting(settings, "heartbeat_interval_seconds") or 5) * 12, 60)
    unavailable_devices: list[dict[str, object]] = []
    for device_id in devices:
        lock_key = f"{GPU_LOCK_KEY_PREFIX}:{device_id}"
        acquired = redis_client.set(
            lock_key,
            json.dumps(
                {
                    "training_job_id": training_job_id,
                    "worker_instance_id": worker_instance_id,
                    "acquired_at": utc_now_iso(),
                },
                sort_keys=True,
            ),
            nx=True,
            ex=ttl,
        )
        if acquired:
            return _GpuReservation(device_id=str(device_id), lock_key=lock_key)
        unavailable_devices.append(
            {
                "device_id": str(device_id),
                "lock_key": lock_key,
                "ttl_seconds": redis_client.ttl(lock_key),
            }
        )
    raise RetryableTrainingError(
        "No configured GPU device is currently available for training.",
        failure_class="gpu_unavailable",
        metadata={"unavailable_devices": unavailable_devices},
    )


def _release_gpu(redis_client: Redis, reservation: _GpuReservation) -> None:
    try:
        redis_client.delete(reservation.lock_key)
    except Exception:
        pass


def _merge_runtime_metadata(
    *,
    settings: PlatformSettings,
    training_job_id: str,
    status: TrainingJobStatus,
    worker_instance_id: str,
    metadata: dict[str, Any],
) -> None:
    with session_scope(settings) as session:
        update_training_job_state(
            session,
            training_job_id=training_job_id,
            status=status,
            source_service=SOURCE_SERVICE,
            actor_type="system",
            actor_id=worker_instance_id,
            runtime_metadata=_json_safe(metadata),
        )


def _worker_instance_id() -> str:
    return os.getenv("WORKER_INSTANCE_ID") or f"{socket.gethostname()}-{os.getpid()}"


def _workspace_root() -> Path:
    configured = os.getenv("CV_PLATFORM_WORKSPACE_ROOT")
    if configured:
        return Path(configured).resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "ml" / "configs").exists():
        return cwd
    for parent in Path(__file__).resolve().parents:
        if (parent / "ml" / "configs").exists():
            return parent
    return cwd


def _progress_payload(
    *,
    status: TrainingJobStatus,
    current_epoch: int,
    current_step: int,
    total_epochs: int,
    total_steps: int,
) -> dict[str, Any]:
    effective_total_steps = max(int(total_steps), 1)
    completed_steps = min(max(int(current_step), 0), effective_total_steps)
    return {
        "status": status.value,
        "current_epoch": int(current_epoch),
        "current_step": int(current_step),
        "total_epochs": int(total_epochs),
        "total_steps": int(total_steps),
        "progress_percent": round((completed_steps / effective_total_steps) * 100, 2),
        "updated_at": utc_now_iso(),
    }


def _resolve_snapshot_manifest_path(snapshot_root: Path, manifest_uri: str | None) -> Path:
    candidates: list[Path] = []
    if manifest_uri:
        manifest_path = Path(manifest_uri)
        candidates.append(manifest_path if manifest_path.is_absolute() else snapshot_root / manifest_path)
    candidates.extend(
        [
            snapshot_root / "snapshot_manifest.json",
            snapshot_root / "manifest.json",
            snapshot_root / "manifests" / "snapshot_manifest.json",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _expected_recipe_path(workspace_root: Path, task_type: TaskType, recipe_name: str) -> Path:
    task_dir = {
        TaskType.CLASSIFICATION: "classification",
        TaskType.DETECTION: "detection",
        TaskType.ORIENTED_DETECTION: "oriented_detection",
        TaskType.SEGMENTATION: "segmentation",
        TaskType.ANOMALY: "anomaly",
        TaskType.TEMPORAL_SEQUENCE: "temporal_sequence",
    }[task_type]
    return workspace_root / "ml" / "configs" / "task-types" / task_dir / f"{recipe_name}.yaml"


def _package_status(module_name: str) -> dict[str, Any]:
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError) as exc:
        return {"available": False, "version": None, "error": str(exc)}
    if spec is None:
        return {"available": False, "version": None, "error": "module_not_found"}
    try:
        distribution_name = "Pillow" if module_name == "PIL" else module_name
        version = importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return {
        "available": True,
        "version": version,
        "origin": spec.origin,
    }


def _torch_cuda_status(torch_available: bool) -> dict[str, Any]:
    if not torch_available:
        return {"cuda_available": False, "device_count": 0, "error": "torch_not_available"}
    try:
        import torch

        return {
            "cuda_available": bool(torch.cuda.is_available()),
            "device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
            "torch_version": getattr(torch, "__version__", "unknown"),
        }
    except Exception as exc:
        return {"cuda_available": False, "device_count": 0, "error": str(exc)}


def _available_mmyolo_config_files() -> list[str]:
    try:
        module_spec = importlib.util.find_spec("mmyolo")
    except (ImportError, ValueError):
        return []
    if module_spec is None or module_spec.origin is None:
        return []
    package_root = Path(module_spec.origin).resolve().parent
    roots = [package_root, package_root.parent, package_root.parent.parent]
    patterns = [
        Path(".mim") / "configs" / "yolov8" / "*.py",
        Path("configs") / "yolov8" / "*.py",
    ]
    found: list[str] = []
    for root in roots:
        for pattern in patterns:
            found.extend(str(path) for path in sorted(root.glob(str(pattern))))
    return sorted(set(found))


def _requires_openmmlab_preflight(*, trainer: BaseTrainer, trainer_mode: str) -> bool:
    return trainer_mode == "real" and _trainer_backend_value(trainer) == "openmmlab-mmyolo"


def _requires_torchvision_preflight(*, trainer: BaseTrainer, trainer_mode: str) -> bool:
    return trainer_mode == "real" and _trainer_backend_value(trainer).startswith("torchvision-")


def _requires_ultralytics_preflight(*, trainer: BaseTrainer, trainer_mode: str) -> bool:
    return trainer_mode == "real" and _trainer_backend_value(trainer) == "ultralytics-yolo"


def _requires_anomalib_preflight(*, trainer: BaseTrainer, trainer_mode: str) -> bool:
    return trainer_mode == "real" and _trainer_backend_value(trainer) == "anomalib-patchcore"


def _requires_temporal_preflight(*, trainer: BaseTrainer, trainer_mode: str) -> bool:
    return (
        trainer_mode == "real"
        and _trainer_backend_value(trainer) == "pytorch-temporal-sequence"
    )


def _recipe_with_trainer_backend(recipe: dict[str, Any], *, trainer: BaseTrainer) -> dict[str, Any]:
    resolved = dict(recipe)
    trainer_backend = _trainer_backend_value(trainer)
    resolved["trainer_backend"] = trainer_backend
    if trainer_backend == "ultralytics-yolo":
        resolved["framework"] = "ultralytics"
    elif trainer_backend == "anomalib-patchcore":
        resolved["framework"] = "anomalib"
    elif trainer_backend == "openmmlab-mmyolo":
        resolved["framework"] = "openmmlab-mmyolo"
    elif trainer_backend.startswith("torchvision-"):
        resolved["framework"] = "torchvision"
    elif trainer_backend == "pytorch-temporal-sequence":
        resolved["framework"] = "pytorch"
    return resolved


def _trainer_mode_value(trainer: Any) -> str:
    return str(getattr(trainer, "trainer_mode", "real"))


def _trainer_backend_value(trainer: Any) -> str:
    return str(getattr(trainer, "trainer_backend", trainer.__class__.__name__))


def _resolve_steps_per_epoch(
    *,
    recipe: dict[str, Any],
    item_count: int,
    batch_size: int | None,
) -> int:
    if recipe.get("steps_per_epoch"):
        return max(int(recipe["steps_per_epoch"]), 1)
    effective_batch_size = max(int(batch_size or recipe.get("batch_size") or 1), 1)
    return max((max(item_count, 1) + effective_batch_size - 1) // effective_batch_size, 1)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mlflow_parameters(
    *,
    context: _TrainingJobContext,
    recipe: dict[str, Any],
    trainer_mode: str,
) -> dict[str, Any]:
    return {
        "task_type": context.task_type.value,
        "architecture": f"{context.architecture_family}:{context.architecture_version}:{context.architecture_size}",
        "recipe_name": context.recipe_name,
        "recipe_version": context.recipe_version,
        "trainer_mode": trainer_mode,
        "max_epochs": context.max_epochs,
        "image_size": context.image_size or recipe.get("image_size"),
        "batch_size": context.batch_size or recipe.get("batch_size"),
        "learning_rate": context.learning_rate or recipe.get("learning_rate"),
    }


def _guard_against_simulation_result(*, result: TrainerResult, trainer: BaseTrainer) -> None:
    metadata = result.runtime_metadata or {}
    validation = result.artifact_validation
    if (
        _trainer_mode_value(trainer) == "simulation"
        or metadata.get("simulation_only") is True
        or metadata.get("trainer_mode") == "simulation"
        or (validation is not None and validation.status == "simulation")
    ):
        raise TerminalTrainingError(
            "Real training path received simulation artifacts; refusing to register deployable model.",
            failure_class="simulation_not_allowed",
            metadata={"runtime_metadata": metadata},
        )


def _find_export_artifact(
    export_artifacts: tuple[TrainerExportArtifact, ...],
    kind: str,
) -> TrainerExportArtifact | None:
    for artifact in export_artifacts:
        if artifact.kind == kind:
            return artifact
    return None


def _find_existing_tensorrt(result: TrainerResult) -> Path | None:
    artifact = _find_export_artifact(result.export_artifacts, "tensorrt")
    if artifact is None or not artifact.path:
        return None
    path = Path(artifact.path)
    return path if path.exists() else None


def _looks_like_json_placeholder(path: Path) -> bool:
    try:
        prefix = path.read_bytes()[:256].lstrip()
    except OSError:
        return False
    if prefix[:1] not in {b"{", b"["}:
        return False
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return True


def _load_class_map(snapshot_root: Path) -> tuple[list[str], Path]:
    candidates = [
        snapshot_root / "label_map.json",
        snapshot_root / "manifests" / "label_map.json",
        snapshot_root / "annotations" / "label_map.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [str(item) for item in payload], path
        if isinstance(payload, dict):
            labels = payload.get("labels") or payload.get("classes")
            if isinstance(labels, list):
                return [str(item) for item in labels], path
    raise TerminalTrainingError(
        f"Dataset snapshot is missing label_map.json under {snapshot_root}.",
        failure_class="dataset_invalid",
    )


def _load_training_class_map(
    *,
    snapshot_root: Path,
    result: TrainerResult,
) -> tuple[list[str], Path]:
    if result.dataset_artifact is not None:
        projected_path = Path(result.dataset_artifact.class_map_path)
        if projected_path.exists():
            payload = json.loads(projected_path.read_text(encoding="utf-8"))
            if isinstance(payload, list) and payload:
                return [str(item) for item in payload], projected_path
    return _load_class_map(snapshot_root)


def _load_snapshot_image_view_preprocessing(
    snapshot_manifest_path: Path,
) -> dict[str, Any]:
    try:
        snapshot_manifest = json.loads(snapshot_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TerminalTrainingError(
            f"Dataset snapshot manifest could not be read: {snapshot_manifest_path}",
            failure_class="dataset_invalid",
            metadata={"snapshot_manifest_path": str(snapshot_manifest_path)},
        ) from exc
    if not isinstance(snapshot_manifest, dict):
        raise TerminalTrainingError(
            f"Dataset snapshot manifest must contain a JSON object: {snapshot_manifest_path}",
            failure_class="dataset_invalid",
            metadata={"snapshot_manifest_path": str(snapshot_manifest_path)},
        )
    image_view = snapshot_manifest.get("image_view")
    if not isinstance(image_view, dict) or not image_view:
        return {}
    return {"image_view": dict(image_view)}


def _load_temporal_head_order(result: TrainerResult) -> tuple[list[str], Path]:
    artifact = _find_export_artifact(result.export_artifacts, "head_class_order")
    if artifact is None or not artifact.path:
        raise TerminalTrainingError(
            "Temporal training completed without the versioned head-class-order artifact.",
            failure_class="export_failed",
        )
    path = Path(artifact.path)
    if not path.exists():
        raise TerminalTrainingError(
            f"Temporal head-class-order artifact does not exist: {path}.",
            failure_class="export_failed",
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    labels: list[str] = []
    for head_name in (
        "bow_relevance",
        "target_observed",
        "final_orientation_quality",
        "angle_vector",
    ):
        values = payload.get(head_name) if isinstance(payload, dict) else None
        if isinstance(values, list):
            labels.extend(f"{head_name}:{value}" for value in values)
    if not labels:
        raise TerminalTrainingError(
            f"Temporal head-class-order artifact is invalid: {path}.",
            failure_class="export_failed",
        )
    return labels, path


def _temporal_export_artifacts(result: TrainerResult) -> dict[str, Path]:
    allowed = {
        "temporal_contract",
        "scaler",
        "angle_contract",
        "head_class_order",
        "confidence_calibration",
        "temporal_evaluation",
    }
    paths: dict[str, Path] = {}
    for artifact in result.export_artifacts:
        if artifact.kind not in allowed or not artifact.generated or not artifact.path:
            continue
        path = Path(artifact.path)
        if not path.exists():
            raise TerminalTrainingError(
                f"Temporal export artifact {artifact.kind} does not exist: {path}.",
                failure_class="export_failed",
            )
        paths[artifact.kind] = path
    missing = sorted(allowed - paths.keys())
    if missing:
        raise TerminalTrainingError(
            "Temporal training completed without required contract artifacts: "
            f"{', '.join(missing)}.",
            failure_class="export_failed",
        )
    return paths


def _build_evaluation_summary(
    *,
    context: _TrainingJobContext,
    result: TrainerResult,
    artifact_validation: TrainerArtifactValidation,
    trainer: BaseTrainer,
    trainer_mode: str,
) -> dict[str, Any]:
    metrics = {key: float(value) for key, value in result.final_validation_metrics.items()}
    if result.best_metric_name not in metrics:
        metrics[result.best_metric_name] = float(result.best_metric_value)
    promotion_results: list[dict[str, Any]] = []
    for criterion in context.promotion_criteria:
        metric_name = str(criterion.get("metric_name"))
        actual = metrics.get(metric_name)
        comparison = str(criterion.get("comparison") or "gte")
        threshold = float(criterion.get("threshold") or 0)
        passed = actual is not None and (
            actual >= threshold if comparison == "gte" else actual <= threshold
        )
        promotion_results.append(
            {
                "metric_name": metric_name,
                "comparison": comparison,
                "threshold": threshold,
                "phase": str(criterion.get("phase") or "evaluation"),
                "actual_value": actual,
                "passed": passed,
            }
        )
    temporal_shadow_only = context.task_type == TaskType.TEMPORAL_SEQUENCE
    return {
        "primary_metric_name": result.best_metric_name,
        "primary_metric_value": float(result.best_metric_value),
        "metrics": metrics,
        "promotion_ready": (
            False
            if temporal_shadow_only
            else bool(promotion_results) and all(item["passed"] for item in promotion_results)
        ),
        "promotion_results": promotion_results,
        "promotion_block_reason": (
            "temporal_sequence_shadow_only_production_gate"
            if temporal_shadow_only
            else None
        ),
        "gold_silver_separate": temporal_shadow_only,
        "trainer_mode": trainer_mode,
        "trainer_backend": _trainer_backend_value(trainer),
        "artifact_validation": artifact_validation.__dict__,
    }


def _build_engine_manifest(settings: PlatformSettings) -> dict[str, Any]:
    return {
        "stage": _training_runtime_setting(settings, "engine_build_stage", "deployment"),
        "precision": "fp16",
        "export_opset_version": int(_training_runtime_setting(settings, "export_opset_version", 13)),
        "generated_at": utc_now_iso(),
    }


def _build_io_schema(
    task_type: TaskType,
    *,
    image_size: int | None,
    class_count: int | None = None,
    onnx_path: Path | None = None,
) -> dict[str, Any]:
    graph_schema = _onnx_graph_io_schema(onnx_path)
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        output_tensors = _graph_output_tensors(graph_schema, fallback_dims=[1]) or [
            {"name": name, "data_type": "FP32", "dims": dims}
            for name, dims in (
                ("output__bow_relevance_probability", [1]),
                ("output__target_observed_probability", [1]),
                ("output__angle_sin_2e", [1]),
                ("output__angle_cos_2e", [1]),
                ("output__angle_confidence_raw", [1]),
                ("output__final_quality_ordinal_logits", [2]),
            )
        ]
    elif task_type == TaskType.DETECTION:
        output_tensors = _graph_output_tensors(graph_schema, fallback_dims=["N", 6]) or [
            {"name": "output__detections", "data_type": "FP32", "dims": ["N", 6]}
        ]
    elif task_type == TaskType.ORIENTED_DETECTION:
        output_tensors = _graph_output_tensors(graph_schema, fallback_dims=["N", 7]) or [
            {"name": "output__oriented_detections", "data_type": "FP32", "dims": ["N", 7]}
        ]
    elif task_type == TaskType.ANOMALY:
        output_tensors = _graph_output_tensors(graph_schema, fallback_dims=["N"]) or [
            {
                "name": "output__anomaly_score",
                "data_type": "FP32",
                "dims": ["N"],
            },
            {
                "name": "output__anomaly_map",
                "data_type": "FP32",
                "dims": ["N", 1, image_size, image_size],
            },
        ]
    elif task_type == TaskType.SEGMENTATION:
        output_tensors = _graph_output_tensors(
            graph_schema,
            fallback_dims=["C", image_size, image_size],
        ) or [
            {"name": "output__masks", "data_type": "FP32", "dims": ["C", image_size, image_size]}
        ]
    else:
        output_tensors = _graph_output_tensors(
            graph_schema,
            fallback_dims=[class_count or "C"],
        ) or [
            {"name": "output__predictions", "data_type": "FP32", "dims": [class_count or "C"]}
        ]
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        graph_input_shape = list((graph_schema or {}).get("input_shape") or [])
        temporal_dims = _manifest_dims_from_onnx_shape(graph_input_shape) or ["T", "F"]
        input_tensor = {
            "name": _input_tensor_name(graph_schema, fallback="input__sequence"),
            "data_type": "FP32",
            "dims": temporal_dims,
            "layout": "TF",
            "description": "Left-padded causal feature sequence ending at the decision frame.",
        }
    else:
        if image_size is None:
            raise ValueError(f"image_size is required for {task_type.value} model IO schemas.")
        input_tensor = {
            "name": _input_tensor_name(graph_schema, fallback="input__image"),
            "data_type": "FP32",
            "dims": [3, image_size, image_size],
            "layout": "CHW",
        }
    schema = {
        "input_tensors": [
            input_tensor
        ],
        "output_tensors": output_tensors,
        "contract_major": 1,
    }
    schema["schema_digest"] = hashlib.sha256(
        json.dumps(schema, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return schema


def _onnx_graph_io_schema(onnx_path: Path | None) -> dict[str, Any] | None:
    if onnx_path is None or not onnx_path.exists():
        return None
    try:
        import onnx

        model = onnx.load(str(onnx_path), load_external_data=False)
    except Exception:
        return None
    inputs = list(model.graph.input)
    outputs = list(model.graph.output)
    if not inputs or not outputs:
        return None
    first_input = inputs[0]
    return {
        "input_names": [str(item.name) for item in inputs],
        "outputs": [
            {
                "name": str(item.name),
                "shape": _onnx_value_shape(item),
                "data_type": _onnx_elem_type_to_manifest_dtype(item),
            }
            for item in outputs
        ],
        "input_shape": _onnx_value_shape(first_input),
        "input_data_type": _onnx_elem_type_to_manifest_dtype(first_input),
    }


def _onnx_value_shape(value_info: Any) -> list[Any]:
    tensor_type = value_info.type.tensor_type
    if not tensor_type.HasField("shape"):
        return []
    shape: list[Any] = []
    for dim in tensor_type.shape.dim:
        if dim.HasField("dim_value"):
            shape.append(int(dim.dim_value))
        elif dim.HasField("dim_param"):
            shape.append(str(dim.dim_param))
        else:
            shape.append(-1)
    return shape


def _input_tensor_name(graph_schema: dict[str, Any] | None, *, fallback: str) -> str:
    if graph_schema and graph_schema["input_names"]:
        return graph_schema["input_names"][0]
    return fallback


def _graph_output_tensors(
    graph_schema: dict[str, Any] | None,
    *,
    fallback_dims: list[Any],
) -> list[dict[str, Any]]:
    if not graph_schema:
        return []
    outputs = graph_schema.get("outputs") or []
    tensors: list[dict[str, Any]] = []
    for output in outputs:
        if not isinstance(output, dict) or not output.get("name"):
            continue
        dims = _manifest_dims_from_onnx_shape(output.get("shape") or [])
        tensors.append(
            {
                "name": str(output["name"]),
                "data_type": str(output.get("data_type") or "FP32"),
                "dims": dims or fallback_dims,
            }
        )
    return tensors


def _manifest_dims_from_onnx_shape(shape: list[Any]) -> list[Any]:
    dims = list(shape)
    if len(dims) > 1:
        dims = dims[1:]
    normalized: list[Any] = []
    for item in dims:
        if isinstance(item, int) and item > 0:
            normalized.append(item)
        else:
            normalized.append(-1)
    return normalized


def _onnx_type_to_manifest_dtype(value: str) -> str:
    value = value.lower()
    if "int64" in value:
        return "INT64"
    if "int32" in value:
        return "INT32"
    if "float16" in value or "fp16" in value:
        return "FP16"
    return "FP32"


def _onnx_elem_type_to_manifest_dtype(value_info: Any) -> str:
    elem_type = int(value_info.type.tensor_type.elem_type)
    mapping = {
        1: "FP32",
        10: "FP16",
        6: "INT32",
        7: "INT64",
    }
    return mapping.get(elem_type, "FP32")


def _build_preprocessing(
    recipe: dict[str, Any],
    *,
    trainer_backend: str | None = None,
    temporal_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    backend = str(trainer_backend or "").lower()
    if backend == "pytorch-temporal-sequence":
        temporal = dict(recipe.get("temporal") or {})
        contract = dict(temporal_contract or {})
        return {
            "input_kind": "causal_feature_sequence",
            "feature_set_id": contract.get("feature_set_id") or temporal.get("feature_set_id"),
            "input_end": "decision_frame_or_earlier",
            "post_decision_frames_allowed": False,
            "padding": {"direction": "left", "value": 0.0},
            "minimum_window_steps": contract.get("minimum_window_steps"),
            "maximum_window_steps": contract.get("maximum_window_steps"),
            "scaler_scope": "train_split_only",
            "masking": "feature validity plus angle-target loss mask",
        }
    geometry_task = str(recipe.get("task_type") or "").lower() in {
        TaskType.DETECTION.value,
        TaskType.ORIENTED_DETECTION.value,
        TaskType.SEGMENTATION.value,
    }
    if backend == "ultralytics-yolo":
        return {
            "resize": int(recipe.get("image_size") or 640),
            "resize_mode": "letterbox" if geometry_task else "stretch",
            "letterbox": {
                "enabled": geometry_task,
                "padding_value": 114,
                "center": True,
            },
            "normalize": {"mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]},
            "color_format": "rgb",
            "scale": 0.00392156862745098,
        }
    if backend == "openmmlab-mmyolo":
        return {
            "resize": int(recipe.get("image_size") or 640),
            "resize_mode": "letterbox" if geometry_task else "stretch",
            "letterbox": {
                "enabled": geometry_task,
                "padding_value": 114,
                "center": True,
            },
            "normalize": {"mean": [0.0, 0.0, 0.0], "std": [255.0, 255.0, 255.0]},
            "color_format": "rgb",
        }
    return {
        "resize": int(recipe.get("image_size") or 640),
        "normalize": recipe.get("normalize")
        or {"mean": [123.675, 116.28, 103.53], "std": [58.395, 57.12, 57.375]},
        "color_format": "rgb",
    }


def _build_postprocessing(recipe: dict[str, Any], class_map: list[str]) -> dict[str, Any]:
    postprocessing = dict(recipe.get("postprocessing") or {})
    if str(recipe.get("task_type") or "").lower() == TaskType.TEMPORAL_SEQUENCE.value:
        temporal = dict(recipe.get("temporal") or {})
        return {
            "angle_decode": "0.5 * atan2(sin_2e, cos_2e)",
            "angle_wrap_interval_deg": [-90.0, 90.0],
            "angle_wrap_upper_bound_exclusive": True,
            "direction_deadband_deg": float(temporal.get("direction_deadband_deg") or 5.0),
            "confidence_calibration": "versioned_isotonic_validation_split",
            "head_class_order": class_map,
            "shadow_only": True,
        }
    postprocessing.setdefault("confidence_threshold_default", 0.5)
    if str(recipe.get("task_type") or "").lower() == TaskType.ORIENTED_DETECTION.value:
        postprocessing.setdefault("output_geometry", "oriented_box")
    postprocessing["class_map"] = class_map
    return postprocessing


def _build_nms(recipe: dict[str, Any], *, task_type: TaskType) -> dict[str, Any]:
    if task_type not in {
        TaskType.DETECTION,
        TaskType.ORIENTED_DETECTION,
        TaskType.SEGMENTATION,
    } or recipe.get("nms_enabled") is False:
        return {
            "enabled": False,
            "iou_threshold": None,
            "max_detections": None,
            "score_threshold": None,
        }
    return {
        "enabled": True,
        "iou_threshold": float(recipe.get("nms_iou_threshold") or 0.65),
        "max_detections": int(recipe.get("max_detections") or 300),
        "score_threshold": float(recipe.get("score_threshold") or 0.25),
    }


def _build_hardware_compatibility(settings: PlatformSettings) -> dict[str, Any]:
    return {
        "accelerator": "nvidia_gpu",
        "target_gpu_profile": settings.triton.realtime.gpu_profile,
        "min_compute_capability": "7.5",
        "cuda_version": settings.triton.realtime.cuda_version,
        "tensorrt_version": settings.triton.realtime.tensorrt_version,
        "precision": "fp16",
        "max_batch_size": {"realtime": 1, "batch": 16},
        "validated_devices": [settings.triton.realtime.gpu_profile],
    }


def _classify_exception(exc: BaseException) -> str:
    if isinstance(exc, TrainingWorkflowError):
        return exc.failure_class
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "dependency_missing"
    message = str(exc).lower()
    if "recipe" in message:
        return "recipe_missing"
    if "snapshot" in message or "dataset" in message:
        return "dataset_invalid"
    if "onnx" in message or "export" in message:
        return "export_failed"
    if "mlflow" in message or "registered model" in message or "model version" in message or "alias" in message:
        return "registry_failed"
    if "python 3.11" in message or "linux" in message or "mmcv" in message or "numpy" in message or "setuptools" in message:
        return "unsupported_runtime"
    if "architecture" in message or "not supported" in message:
        return "unsupported_architecture"
    if "classification" in message or "segmentation" in message or "task" in message:
        return "unsupported_task"
    return "training_failed"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and value.__class__.__module__ == "cv_status.enums":
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = [
    "RetryableTrainingError",
    "TerminalTrainingError",
    "TrainingRunController",
    "TrainingWorkflowError",
    "collect_dependency_preflight",
    "run_training_workflow",
    "_progress_payload",
    "_training_runtime_setting",
]
