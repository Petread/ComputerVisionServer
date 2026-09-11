from __future__ import annotations

import json
import time
from pathlib import Path

from cv_status import TaskType

from cv_training.trainer_types import (
    BaseTrainer,
    TrainerArtifactValidation,
    TrainerCheckpointArtifact,
    TrainerConfig,
    TrainerControl,
    TrainerProgress,
    TrainerResult,
    TrainerResumeState,
)


class SimulationTrainer(BaseTrainer):
    @property
    def trainer_mode(self) -> str:
        return "simulation"

    @property
    def trainer_backend(self) -> str:
        return "cv-platform-simulation"

    def run(
        self,
        *,
        config: TrainerConfig,
        control: TrainerControl,
        resume_state: TrainerResumeState | None = None,
    ) -> TrainerResult:
        start_epoch = resume_state.epoch_no if resume_state else 0
        best_metric = resume_state.best_metric if resume_state else None
        best_metrics: dict[str, float] = {}
        final_validation_metrics: dict[str, float] = {}
        total_steps = config.max_epochs * config.steps_per_epoch
        simulation_delay = float(config.runtime_metadata.get("simulation_step_sleep_seconds") or 0.0)
        checkpoints: list[TrainerCheckpointArtifact] = []

        control.on_runtime_metadata(
            {
                "trainer_mode": "simulation",
                "trainer_backend": self.trainer_backend,
                "simulation_only": True,
                "real_training": False,
            }
        )

        for epoch_no in range(start_epoch + 1, config.max_epochs + 1):
            for step_index in range(1, config.steps_per_epoch + 1):
                step_no = ((epoch_no - 1) * config.steps_per_epoch) + step_index
                if simulation_delay:
                    time.sleep(simulation_delay)
                training_metrics = _build_training_metrics(
                    task_type=config.task_type,
                    epoch_no=epoch_no,
                    step_index=step_index,
                    steps_per_epoch=config.steps_per_epoch,
                )
                validation_metrics = _build_validation_metrics(
                    task_type=config.task_type,
                    epoch_no=epoch_no,
                    max_epochs=config.max_epochs,
                )
                control.on_progress(
                    TrainerProgress(
                        epoch_no=epoch_no,
                        step_no=step_no,
                        total_epochs=config.max_epochs,
                        total_steps=total_steps,
                        training_metrics=training_metrics,
                        validation_metrics=validation_metrics,
                    )
                )

                if step_no % config.checkpoint_interval_steps == 0:
                    checkpoint = self._write_simulation_checkpoint(
                        config=config,
                        epoch_no=epoch_no,
                        step_no=step_no,
                        metrics=validation_metrics,
                        checkpoint_kind="interval",
                        best_metric_name=config.best_metric_name,
                        best_metric_mode=config.best_metric_mode,
                        current_best=best_metric,
                    )
                    candidate = validation_metrics[config.best_metric_name]
                    if _is_better(candidate, best_metric, config.best_metric_mode):
                        best_metric = candidate
                        best_metrics = dict(validation_metrics)
                        checkpoint = TrainerCheckpointArtifact(
                            **{
                                **checkpoint.__dict__,
                                "is_best": True,
                            }
                        )
                    checkpoints.append(checkpoint)
                    control.on_checkpoint(checkpoint)
                if control.should_cancel():
                    cancel_metrics = dict(validation_metrics)
                    checkpoint = self._write_simulation_checkpoint(
                        config=config,
                        epoch_no=epoch_no,
                        step_no=step_no,
                        metrics=cancel_metrics,
                        checkpoint_kind="cancel",
                        best_metric_name=config.best_metric_name,
                        best_metric_mode=config.best_metric_mode,
                        current_best=best_metric,
                    )
                    candidate = cancel_metrics[config.best_metric_name]
                    if _is_better(candidate, best_metric, config.best_metric_mode):
                        best_metric = candidate
                        best_metrics = dict(cancel_metrics)
                        checkpoint = TrainerCheckpointArtifact(
                            **{
                                **checkpoint.__dict__,
                                "is_best": True,
                            }
                        )
                    checkpoints.append(checkpoint)
                    control.on_checkpoint(checkpoint)
                    return TrainerResult(
                        final_epoch=epoch_no,
                        final_step=step_no,
                        best_metric_name=config.best_metric_name,
                        best_metric_value=best_metric or candidate,
                        best_metrics=best_metrics or cancel_metrics,
                        final_validation_metrics=cancel_metrics,
                        checkpoints=tuple(checkpoints),
                        best_checkpoint_path=_best_checkpoint_path(checkpoints),
                        latest_checkpoint_path=checkpoints[-1].path if checkpoints else None,
                        artifact_validation=TrainerArtifactValidation(
                            status="simulation",
                            checks={"simulation_only": True},
                            details={"simulation_only": True},
                        ),
                        runtime_metadata={
                            "trainer_mode": "simulation",
                            "trainer_backend": self.trainer_backend,
                            "simulation_only": True,
                        },
                        cancelled=True,
                    )

            final_validation_metrics = _build_validation_metrics(
                task_type=config.task_type,
                epoch_no=epoch_no,
                max_epochs=config.max_epochs,
            )
            candidate = final_validation_metrics[config.best_metric_name]
            checkpoint = self._write_simulation_checkpoint(
                config=config,
                epoch_no=epoch_no,
                step_no=epoch_no * config.steps_per_epoch,
                metrics=final_validation_metrics,
                checkpoint_kind="epoch",
                best_metric_name=config.best_metric_name,
                best_metric_mode=config.best_metric_mode,
                current_best=best_metric,
            )
            if _is_better(candidate, best_metric, config.best_metric_mode):
                best_metric = candidate
                best_metrics = dict(final_validation_metrics)
                checkpoint = TrainerCheckpointArtifact(
                    **{
                        **checkpoint.__dict__,
                        "is_best": True,
                    }
                )
            checkpoints.append(checkpoint)
            control.on_checkpoint(checkpoint)

        return TrainerResult(
            final_epoch=config.max_epochs,
            final_step=total_steps,
            best_metric_name=config.best_metric_name,
            best_metric_value=best_metric or final_validation_metrics[config.best_metric_name],
            best_metrics=best_metrics or final_validation_metrics,
            final_validation_metrics=final_validation_metrics,
            checkpoints=tuple(checkpoints),
            best_checkpoint_path=_best_checkpoint_path(checkpoints),
            latest_checkpoint_path=checkpoints[-1].path if checkpoints else None,
            artifact_validation=TrainerArtifactValidation(
                status="simulation",
                checks={"simulation_only": True},
                details={"simulation_only": True},
            ),
            runtime_metadata={
                "trainer_mode": "simulation",
                "trainer_backend": self.trainer_backend,
                "simulation_only": True,
            },
        )

    def _write_simulation_checkpoint(
        self,
        *,
        config: TrainerConfig,
        epoch_no: int,
        step_no: int,
        metrics: dict[str, float],
        checkpoint_kind: str,
        best_metric_name: str,
        best_metric_mode: str,
        current_best: float | None,
    ) -> TrainerCheckpointArtifact:
        checkpoint_path = (
            config.checkpoint_dir / f"simulation_epoch_{epoch_no:04d}_step_{step_no:06d}.checkpoint.json"
        )
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text(
            json.dumps(
                {
                    "trainer_mode": "simulation",
                    "trainer_backend": self.trainer_backend,
                    "epoch_no": epoch_no,
                    "step_no": step_no,
                    "checkpoint_kind": checkpoint_kind,
                    "metrics": metrics,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        candidate = metrics[best_metric_name]
        is_best = _is_better(candidate, current_best, best_metric_mode)
        return TrainerCheckpointArtifact(
            path=str(checkpoint_path),
            epoch_no=epoch_no,
            step_no=step_no,
            metrics=metrics,
            checkpoint_kind=checkpoint_kind,
            is_best=is_best,
            resume_safe=True,
            metadata={"simulation_only": True},
        )


def _build_training_metrics(
    *,
    task_type: TaskType,
    epoch_no: int,
    step_index: int,
    steps_per_epoch: int,
) -> dict[str, float]:
    progress = epoch_no + (step_index / max(steps_per_epoch, 1))
    if task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
        return {
            "loss_cls": round(max(0.04, 1.6 / progress), 4),
            "loss_bbox": round(max(0.03, 1.1 / progress), 4),
        }
    if task_type == TaskType.SEGMENTATION:
        return {
            "loss_cls": round(max(0.05, 1.5 / progress), 4),
            "loss_mask": round(max(0.04, 1.25 / progress), 4),
        }
    return {"loss": round(max(0.02, 1.4 / progress), 4)}


def _build_validation_metrics(
    *,
    task_type: TaskType,
    epoch_no: int,
    max_epochs: int,
) -> dict[str, float]:
    completion = min(max(epoch_no / max(max_epochs, 1), 0), 1)
    if task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
        map_50_95 = round(0.35 + (completion * 0.42), 4)
        return {
            "map_50": round(min(0.94, map_50_95 + 0.12), 4),
            "map_50_95": map_50_95,
            "recall_at_100": round(0.52 + (completion * 0.27), 4),
        }
    if task_type == TaskType.SEGMENTATION:
        mask_map_50_95 = round(0.32 + (completion * 0.4), 4)
        return {
            "mask_map_50": round(min(0.92, mask_map_50_95 + 0.13), 4),
            "mask_map_50_95": mask_map_50_95,
            "recall_at_100": round(0.5 + (completion * 0.28), 4),
        }
    accuracy = round(0.62 + (completion * 0.35), 4)
    precision = round(0.6 + (completion * 0.33), 4)
    recall = round(0.59 + (completion * 0.31), 4)
    return {
        "accuracy_top1": accuracy,
        "precision_macro": precision,
        "recall_macro": recall,
        "f1_macro": round((2 * precision * recall) / max(precision + recall, 1e-9), 4),
    }


def _best_checkpoint_path(checkpoints: list[TrainerCheckpointArtifact]) -> str | None:
    for checkpoint in reversed(checkpoints):
        if checkpoint.is_best:
            return checkpoint.path
    return checkpoints[-1].path if checkpoints else None


def _is_better(candidate: float, current: float | None, mode: str) -> bool:
    if current is None:
        return True
    if mode == "min":
        return candidate < current
    return candidate > current
