from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from cv_status import TaskType

TrainerMode = Literal["real", "simulation"]


@dataclass(frozen=True)
class ResolvedTrainerArchitecture:
    task_type: TaskType
    family: str
    version: str
    size: str
    display_name: str
    backend_model_key: str
    supported: bool = True
    support_reason: str | None = None


@dataclass(frozen=True)
class TrainerResumeState:
    epoch_no: int
    step_no: int
    best_metric: float | None = None
    checkpoint_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrainerDatasetArtifact:
    task_type: TaskType
    stage_root: str
    manifest_path: str
    conversion_summary_path: str
    validation_report_path: str
    split_manifest_path: str
    class_map_path: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrainerCheckpointArtifact:
    path: str
    epoch_no: int
    step_no: int
    metrics: dict[str, float]
    checkpoint_kind: str
    is_best: bool = False
    resume_safe: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrainerExportArtifact:
    kind: str
    path: str | None
    generated: bool
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrainerArtifactValidation:
    status: str
    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrainerConfig:
    training_job_id: str
    dataset_snapshot_id: str
    task_type: TaskType
    trainer_mode: TrainerMode
    trainer_backend: str
    architecture: ResolvedTrainerArchitecture
    snapshot_root: Path
    workspace_dir: Path
    configs_dir: Path
    checkpoint_dir: Path
    evaluation_dir: Path
    onnx_dir: Path
    tensorrt_dir: Path
    max_epochs: int
    steps_per_epoch: int
    checkpoint_interval_steps: int
    best_metric_name: str
    best_metric_mode: str
    resolved_recipe: dict[str, Any]
    runtime_metadata: dict[str, Any]
    image_size: int | None = None
    batch_size: int | None = None
    learning_rate: float | None = None
    optimizer: str | None = None
    optimizer_settings: dict[str, Any] = field(default_factory=dict)
    gpu_device: str = "cuda:0"


@dataclass(frozen=True)
class TrainerProgress:
    epoch_no: int
    step_no: int
    total_epochs: int
    total_steps: int
    training_metrics: dict[str, float]
    validation_metrics: dict[str, float]


@dataclass(frozen=True)
class TrainerResult:
    final_epoch: int
    final_step: int
    best_metric_name: str
    best_metric_value: float
    best_metrics: dict[str, float]
    final_validation_metrics: dict[str, float]
    checkpoints: tuple[TrainerCheckpointArtifact, ...] = ()
    dataset_artifact: TrainerDatasetArtifact | None = None
    export_artifacts: tuple[TrainerExportArtifact, ...] = ()
    artifact_validation: TrainerArtifactValidation | None = None
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    best_checkpoint_path: str | None = None
    latest_checkpoint_path: str | None = None
    cancelled: bool = False


class TrainerControl(Protocol):
    def should_cancel(self) -> bool: ...

    def on_progress(self, progress: TrainerProgress) -> None: ...

    def on_checkpoint(self, checkpoint: TrainerCheckpointArtifact) -> None: ...

    def on_runtime_metadata(self, metadata: dict[str, Any]) -> None: ...


class BaseTrainer(ABC):
    @property
    @abstractmethod
    def trainer_mode(self) -> TrainerMode:
        raise NotImplementedError

    @property
    @abstractmethod
    def trainer_backend(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def run(
        self,
        *,
        config: TrainerConfig,
        control: TrainerControl,
        resume_state: TrainerResumeState | None = None,
    ) -> TrainerResult:
        raise NotImplementedError
