from __future__ import annotations

import re
from typing import Any, Literal

from cv_status import QueueName, TaskType, TrainingJobStatus
from pydantic import BaseModel, Field, model_validator

from cv_contracts.modeling import ModelArchitectureSummary, TrainingDefaults
from cv_contracts.promotion import (
    PromotionCriteriaPolicy,
    PromotionCriterion,
    PromotionCriterionResult,
)


class BestCheckpointRule(BaseModel):
    metric_name: str = Field(min_length=2, max_length=120)
    mode: Literal["max", "min"] = "max"
    phase: Literal["training", "validation", "evaluation"] = "evaluation"
    minimum_epoch: int = Field(default=1, ge=1, le=10_000)


class TrainingEvaluationSummary(BaseModel):
    primary_metric_name: str | None = None
    primary_metric_value: float | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    promotion_ready: bool = False
    promotion_results: list[PromotionCriterionResult] = Field(default_factory=list)
    best_checkpoint_id: str | None = None
    best_checkpoint_metric: float | None = None


class TrainingJobCreateRequest(BaseModel):
    model_family_slug: str = Field(min_length=3, max_length=120)
    dataset_snapshot_id: str = Field(min_length=10, max_length=64)
    task_type: TaskType
    architecture: ModelArchitectureSummary
    model_template_key: str | None = Field(default=None, max_length=120)
    model_template_version: int | None = Field(default=None, ge=1)
    recipe_name: str = Field(min_length=3, max_length=120)
    recipe_version: str = Field(min_length=1, max_length=32)
    max_epochs: int = Field(default=10, ge=1, le=1_000)
    learning_rate: float = Field(default=0.001, gt=0, le=10)
    image_size: int | None = Field(default=None, ge=32, le=4_096, multiple_of=32)
    batch_size: int | None = Field(default=None, ge=1, le=4_096)
    optimizer: str | None = Field(default=None, min_length=2, max_length=120)
    optimizer_settings: dict[str, Any] = Field(default_factory=dict)
    checkpoint_interval_steps: int = Field(default=25, ge=1, le=100_000)
    parameter_overrides: dict[str, Any] = Field(default_factory=dict)
    resolved_training_defaults: TrainingDefaults = Field(default_factory=TrainingDefaults)
    best_checkpoint_rule: BestCheckpointRule
    promotion_criteria: list[PromotionCriterion] = Field(default_factory=list, min_length=1)
    promotion_policy: PromotionCriteriaPolicy | None = None
    allow_duplicate_snapshot_training: bool = False
    idempotency_key: str | None = Field(default=None, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def populate_legacy_architecture(cls, raw_value: Any) -> Any:
        if not isinstance(raw_value, dict):
            return raw_value
        if raw_value.get("architecture") is not None:
            return raw_value

        task_type = raw_value.get("task_type")
        if task_type is None:
            return raw_value
        task_type_value = task_type.value if isinstance(task_type, TaskType) else str(task_type)
        family, version, size = _infer_architecture_components(
            model_template_key=raw_value.get("model_template_key"),
            task_type=task_type_value,
        )
        raw_value = dict(raw_value)
        raw_value["architecture"] = {
            "task_type": task_type_value,
            "family": family,
            "version": version,
            "size": size,
            "display_name": _architecture_display_name(family=family, version=version, size=size),
            "internal_key": f"{task_type_value}:{family}:{version}:{size}",
        }
        return raw_value


class TrainingJobCommandRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=200)
    force: bool = False


class TrainingJobSummary(BaseModel):
    training_job_id: str
    model_family_slug: str
    dataset_snapshot_id: str
    task_type: TaskType
    architecture: ModelArchitectureSummary
    model_template_key: str | None = None
    model_template_version: int | None = None
    recipe_name: str
    recipe_version: str
    queue_name: QueueName
    status: TrainingJobStatus
    progress_percent: float = Field(default=0, ge=0, le=100)
    current_epoch: int = 0
    current_step: int = 0
    total_epochs: int = 0
    latest_checkpoint_id: str | None = None
    worker_instance_id: str | None = None
    gpu_device: str | None = None
    trainer_mode: Literal["real", "simulation"] | None = None
    trainer_backend: str | None = None
    promotion_ready: bool = False
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class TrainingCheckpointSummary(BaseModel):
    checkpoint_id: str
    training_job_id: str
    step_no: int
    epoch_no: int
    metric_primary: float | None = None
    checkpoint_kind: str
    storage_uri: str
    sha256: str
    resume_safe: bool = True
    is_best: bool = False
    metrics: dict[str, float] = Field(default_factory=dict)
    created_at: str


class TrainingMetricRecord(BaseModel):
    metric_id: str
    training_job_id: str
    phase: str
    metric_name: str
    metric_value: float
    epoch_no: int = 0
    step_no: int = 0
    is_primary: bool = False
    checkpoint_id: str | None = None
    recorded_at: str


class TrainingJobLogEntry(BaseModel):
    sequence_no: int = Field(ge=1)
    level: str
    event_type: str
    message: str
    occurred_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


class TrainingJobEventSummary(BaseModel):
    event_id: str
    event_type: str
    sequence_no: int = Field(ge=1)
    source_service: str
    actor_type: str
    actor_id: str
    occurred_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


class TrainingJobDetail(TrainingJobSummary):
    requested_by: str | None = None
    idempotency_key: str | None = None
    allow_duplicate_snapshot_training: bool = False
    cancel_requested: bool = False
    cancel_requested_at: str | None = None
    best_checkpoint_id: str | None = None
    artifact_manifest_uri: str | None = None
    model_version_id: str | None = None
    retry_count: int = 0
    resume_count: int = 0
    failure_class: str | None = None
    last_error: str | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    parameter_overrides: dict[str, Any] = Field(default_factory=dict)
    resolved_training_defaults: TrainingDefaults = Field(default_factory=TrainingDefaults)
    best_checkpoint_rule: BestCheckpointRule
    promotion_criteria: list[PromotionCriterion] = Field(default_factory=list)
    promotion_policy: PromotionCriteriaPolicy | None = None
    evaluation_summary: TrainingEvaluationSummary = Field(default_factory=TrainingEvaluationSummary)
    metrics_summary: dict[str, float] = Field(default_factory=dict)
    runtime_metadata: dict[str, Any] = Field(default_factory=dict)
    artifact_validation: dict[str, Any] = Field(default_factory=dict)
    logs: list[TrainingJobLogEntry] = Field(default_factory=list)
    checkpoints: list[TrainingCheckpointSummary] = Field(default_factory=list)
    metrics: list[TrainingMetricRecord] = Field(default_factory=list)
    events: list[TrainingJobEventSummary] = Field(default_factory=list)


class TrainingMetricComparison(BaseModel):
    metric_name: str
    values: dict[str, float | None] = Field(default_factory=dict)


class TrainingRunComparison(BaseModel):
    jobs: list[TrainingJobSummary]
    metrics: list[TrainingMetricComparison]
    recommended_job_id: str | None = None


def _infer_architecture_components(
    *,
    model_template_key: Any,
    task_type: str,
) -> tuple[str, str, str]:
    if isinstance(model_template_key, str) and model_template_key:
        parts = model_template_key.split("-")
        key_parts = (
            parts[1:]
            if parts
            and parts[0]
            in {"classification", "detection", "oriented_detection", "segmentation", "anomaly"}
            else parts
        )
        if len(key_parts) >= 2:
            family_version = key_parts[0].lower()
            match = re.fullmatch(r"(?P<family>[a-z]+)(?P<version>v[0-9]+)", family_version)
            if match:
                return (match.group("family"), match.group("version"), key_parts[1].lower())
        if len(key_parts) >= 2 and key_parts[0].lower() == "resnet":
            return ("resnet", "v1", key_parts[1].lower())
        if len(key_parts) >= 3 and key_parts[0].lower() == "mobilenet":
            return ("mobilenet", key_parts[1].lower(), key_parts[2].lower())
        if len(key_parts) >= 2 and key_parts[0].lower() == "efficientnet":
            return ("efficientnet", "v1", key_parts[1].lower())
        if len(key_parts) >= 3 and key_parts[0].lower() in {"deeplab", "fcn", "lraspp"}:
            return (key_parts[0].lower(), key_parts[1].lower(), key_parts[2].lower())
    if task_type == TaskType.CLASSIFICATION.value:
        return ("resnet", "v1", "18")
    if task_type == TaskType.SEGMENTATION.value:
        return ("deeplab", "v3", "resnet50")
    if task_type == TaskType.ANOMALY.value:
        return ("patchcore", "anomalib", "wide_resnet50_2")
    return ("yolo", "v8", "n")


def _architecture_display_name(*, family: str, version: str, size: str) -> str:
    normalized_family = family.lower()
    if normalized_family == "resnet":
        return f"ResNet-{size.upper()}"
    if normalized_family == "mobilenet":
        return f"MobileNet{version.upper()} {size.title()}"
    if normalized_family == "efficientnet":
        return f"EfficientNet-{size.upper()}"
    if normalized_family == "deeplab":
        return f"DeepLab{version.upper()} ResNet-50"
    if normalized_family == "fcn":
        return "FCN ResNet-50"
    if normalized_family == "lraspp":
        return "LRASPP Large"
    if normalized_family == "patchcore":
        return f"PatchCore {size.replace('_', ' ').title()}"
    family_label = "YOLO" if normalized_family == "yolo" else family.upper()
    size_label = {
        "n": "Nano",
        "s": "Small",
        "m": "Medium",
        "l": "Large",
        "x": "XL",
    }.get(size.lower(), size.upper())
    return f"{family_label}{version.upper()} {size_label}"
