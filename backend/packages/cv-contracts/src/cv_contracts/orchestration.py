from __future__ import annotations

from typing import Literal

from cv_status import (
    EndpointMode,
    LabelProjectStatus,
    ModelLifecycleStatus,
    TaskType,
)
from pydantic import BaseModel, Field

from cv_contracts.dataset import DatasetSnapshotSummary
from cv_contracts.modeling import (
    ModelArchitectureSummary,
    ModelTemplateSummary,
    SupportedModelArchitecture,
    TrainingDefaults,
)
from cv_contracts.promotion import PromotionCriteriaPolicy
from cv_contracts.registry import ModelVersionSummary
from cv_contracts.training import TrainingJobSummary


class ProjectAutomationPolicy(BaseModel):
    auto_sync_enabled: bool = True
    auto_snapshot_enabled: bool = True
    auto_train_enabled: bool = False
    auto_deploy_enabled: bool = True
    default_model_template: str | None = None
    default_model_family_id: str | None = None
    default_training_profile: str | None = None
    preferred_endpoint_mode: EndpointMode = EndpointMode.REALTIME


class ProjectModelCreateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    task_type: TaskType | None = None
    model_template_key: str = Field(min_length=3, max_length=120)
    architecture_family: str = Field(min_length=2, max_length=64)
    architecture_version: str = Field(min_length=1, max_length=32)
    architecture_size: str = Field(min_length=1, max_length=16)
    class_names: list[str] = Field(default_factory=list)
    segmentation_mode: Literal["instance", "semantic"] | None = None
    description: str | None = Field(default=None, max_length=500)
    training_overrides: TrainingDefaults | None = None
    promotion_policy: PromotionCriteriaPolicy | None = None


class ProjectModelUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    class_names: list[str] | None = None
    promotion_policy: PromotionCriteriaPolicy | None = None


class ProjectTrainRequest(BaseModel):
    model_family_id: str | None = Field(default=None, min_length=3, max_length=64)
    dataset_snapshot_id: str | None = Field(default=None, min_length=3, max_length=64)
    model_template_key: str | None = Field(default=None, max_length=120)
    training_profile_key: str | None = Field(default=None, max_length=120)
    allow_duplicate_snapshot_training: bool = False


class ProjectDeployRequest(BaseModel):
    model_family_id: str | None = Field(default=None, min_length=3, max_length=64)
    model_version_id: str | None = Field(default=None, min_length=3, max_length=64)
    preferred_endpoint_mode: EndpointMode | None = None
    allow_quality_gate_bypass: bool = False


class ProjectReadinessSummary(BaseModel):
    task_count: int = 0
    annotated_task_count: int = 0
    annotation_count: int = 0
    draft_task_count: int = 0
    eligible_task_count: int = 0
    submitted_labeled_task_count: int = 0
    submitted_background_task_count: int = 0
    invalid_task_count: int = 0
    minimum_annotation_count: int = 0
    required_classes: list[str] = Field(default_factory=list)
    class_distribution: dict[str, int] = Field(default_factory=dict)
    train_ratio: int = 80
    val_ratio: int = 10
    test_ratio: int = 10
    readiness_percent: float = 0
    annotations_ready: bool = False
    latest_snapshot_ready: bool = False
    latest_valid_snapshot_ready: bool = False
    readiness_reason: str


class ProjectModelSummary(BaseModel):
    model_family_id: str
    label_project_id: str | None = None
    display_name: str
    slug: str
    endpoint_name: str
    description: str | None = None
    task_type: TaskType
    architecture: ModelArchitectureSummary
    class_names: list[str] = Field(default_factory=list)
    uses_all_classes: bool = True
    segmentation_mode: Literal["instance", "semantic"] | None = None
    template_key: str | None = None
    template_version: int | None = None
    training_profile_key: str | None = None
    training_overrides: TrainingDefaults | None = None
    promotion_policy: PromotionCriteriaPolicy
    latest_model_version_id: str | None = None
    latest_model_version_no: int | None = None
    latest_model_status: ModelLifecycleStatus | None = None
    latest_training_job_id: str | None = None
    created_at: str


class EndpointRouteExample(BaseModel):
    method: str
    path: str
    description: str


class ProjectEndpointSummary(BaseModel):
    endpoint_name: str
    label_project_id: str
    model_family_id: str
    model_family_slug: str
    display_name: str
    task_type: TaskType
    architecture: ModelArchitectureSummary
    class_names: list[str] = Field(default_factory=list)
    uses_all_classes: bool = True
    segmentation_mode: Literal["instance", "semantic"] | None = None
    preferred_mode: EndpointMode
    realtime_available: bool = False
    batch_available: bool = False
    realtime_release_id: str | None = None
    batch_release_id: str | None = None
    current_model_version_id: str | None = None
    current_model_version_no: int | None = None
    last_deployed_at: str | None = None
    api_examples: list[EndpointRouteExample] = Field(default_factory=list)


class ProjectPredictionBackendSummary(BaseModel):
    enabled: bool = False
    backend_id: int | None = None
    backend_url: str | None = None
    status: str = "not_configured"
    model_family_id: str | None = None
    model_family_slug: str | None = None
    model_version_id: str | None = None
    model_version_no: int | None = None
    last_linked_at: str | None = None
    last_error: str | None = None
    auto_update: bool = True


class PipelineActivityItem(BaseModel):
    occurred_at: str
    title: str
    detail: str
    aggregate_type: str
    aggregate_id: str
    event_type: str


class ModelLifecycleStage(BaseModel):
    key: str
    label: str
    state: Literal[
        "pending",
        "running",
        "success",
        "warning",
        "failed",
        "blocked",
        "not_applicable",
    ]
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    started_at: str | None = None
    finished_at: str | None = None
    detail: str | None = None
    error: str | None = None
    activities: list[PipelineActivityItem] = Field(default_factory=list)


class ProjectModelWorkspaceDetail(BaseModel):
    label_project_id: str
    model: ProjectModelSummary
    versions: list[ModelVersionSummary] = Field(default_factory=list)
    training_history: list[TrainingJobSummary] = Field(default_factory=list)
    endpoints: list[ProjectEndpointSummary] = Field(default_factory=list)
    lifecycle: list[ModelLifecycleStage] = Field(default_factory=list)
    activity_timeline: list[PipelineActivityItem] = Field(default_factory=list)


class ProjectPipelineSummary(BaseModel):
    label_project_id: str
    label_studio_project_id: int
    title: str
    task_type: TaskType
    status: LabelProjectStatus
    dataset_id: str | None = None
    dataset_name: str | None = None
    training_pipeline_slug: str | None = None
    automation_policy: ProjectAutomationPolicy
    readiness: ProjectReadinessSummary
    available_classes: list[str] = Field(default_factory=list)
    latest_snapshot: DatasetSnapshotSummary | None = None
    latest_valid_snapshot: DatasetSnapshotSummary | None = None
    models: list[ProjectModelSummary] = Field(default_factory=list)
    last_training: TrainingJobSummary | None = None
    current_endpoints: list[ProjectEndpointSummary] = Field(default_factory=list)
    prediction_backend: ProjectPredictionBackendSummary = Field(
        default_factory=ProjectPredictionBackendSummary
    )
    next_recommended_action: str
    next_action_detail: str | None = None


class ProjectPipelineDetail(ProjectPipelineSummary):
    model_templates: list[ModelTemplateSummary] = Field(default_factory=list)
    supported_model_architectures: list[SupportedModelArchitecture] = Field(default_factory=list)
    training_history: list[TrainingJobSummary] = Field(default_factory=list)
    activity_timeline: list[PipelineActivityItem] = Field(default_factory=list)
