from __future__ import annotations

from typing import Any, Literal

from cv_status import (
    DeploymentApprovalScope,
    DeploymentApprovalStatus,
    ModelLifecycleStatus,
    PromotionStatus,
    TargetPath,
    TaskType,
)
from pydantic import BaseModel, Field

from cv_contracts.modeling import ModelArchitectureSummary, TrainingDefaults
from cv_contracts.promotion import PromotionCriteriaPolicy


class ModelFamilySummary(BaseModel):
    model_family_id: str
    slug: str
    task_type: TaskType
    architecture: ModelArchitectureSummary
    template_key: str | None = None
    template_version: int | None = None
    promotion_policy: PromotionCriteriaPolicy | None = None
    contract_major: int
    version_count: int = 0
    latest_model_version_id: str | None = None
    latest_status: ModelLifecycleStatus | None = None


class ModelLineageSummary(BaseModel):
    dataset_snapshot_id: str | None = None
    code_revision: str | None = None
    training_job_id: str | None = None
    mlflow_run_id: str | None = None
    dvc_references: dict[str, Any] = Field(default_factory=dict)
    reproducibility_metadata: dict[str, Any] = Field(default_factory=dict)


class ModelPromotionSummary(BaseModel):
    promotion_id: str
    target_path: TargetPath
    status: PromotionStatus
    requested_by: str | None = None
    deployment_release_id: str | None = None
    rollback_candidate: bool = False
    rollback_marked_at: str | None = None
    validated_at: str | None = None
    notes: str | None = None


class DeploymentApprovalRecordSummary(BaseModel):
    approval_record_id: str
    model_version_id: str
    target_path: TargetPath
    approval_scope: DeploymentApprovalScope
    status: DeploymentApprovalStatus
    decided_by: str | None = None
    decided_at: str
    notes: str | None = None
    promotion_id: str | None = None
    deployment_release_id: str | None = None


class ModelVersionSummary(BaseModel):
    model_version_id: str
    model_family_id: str
    model_family_slug: str
    version_no: int
    status: ModelLifecycleStatus
    architecture: ModelArchitectureSummary
    model_template_key: str | None = None
    model_template_version: int | None = None
    resolved_training_defaults: TrainingDefaults = Field(default_factory=TrainingDefaults)
    promotion_policy: PromotionCriteriaPolicy | None = None
    training_job_id: str | None = None
    dataset_snapshot_id: str | None = None
    code_revision: str | None = None
    primary_metric_name: str | None = None
    primary_metric_value: float | None = None
    trainer_mode: Literal["real", "simulation"] | None = None
    trainer_backend: str | None = None
    alias_names: list[str] = Field(default_factory=list)
    registry_tags: dict[str, str] = Field(default_factory=dict)
    target_statuses: dict[str, str] = Field(default_factory=dict)
    rollback_candidate_targets: list[TargetPath] = Field(default_factory=list)
    deployable_for: list[TargetPath] = Field(default_factory=list)
    created_at: str


class ModelVersionDetail(ModelVersionSummary):
    metrics_summary: dict[str, float] = Field(default_factory=dict)
    evaluation_summary: dict[str, Any] = Field(default_factory=dict)
    artifact_manifest_uri: str | None = None
    artifact_manifest: dict[str, Any] = Field(default_factory=dict)
    export_manifest_uri: str | None = None
    export_manifest: dict[str, Any] = Field(default_factory=dict)
    mlflow_registered_model: str | None = None
    mlflow_model_version: str | None = None
    lineage: ModelLineageSummary = Field(default_factory=ModelLineageSummary)
    immutable_metadata: dict[str, Any] = Field(default_factory=dict)
    immutable_metadata_sha256: str | None = None
    artifact_validation: dict[str, Any] = Field(default_factory=dict)
    promotions: list[ModelPromotionSummary] = Field(default_factory=list)
    approvals: list[DeploymentApprovalRecordSummary] = Field(default_factory=list)
    archived_at: str | None = None
    archived_by: str | None = None
    archive_reason: str | None = None
    rejected_at: str | None = None
    rejected_by: str | None = None
    rejection_reason: str | None = None


class ModelVersionComparisonMetric(BaseModel):
    metric_name: str
    values: dict[str, float | None] = Field(default_factory=dict)


class ModelVersionComparisonField(BaseModel):
    field_name: str
    values: dict[str, str | None] = Field(default_factory=dict)


class ModelVersionComparison(BaseModel):
    versions: list[ModelVersionSummary]
    metrics: list[ModelVersionComparisonMetric]
    lineage_fields: list[ModelVersionComparisonField]


class ModelApprovalRequest(BaseModel):
    target_path: TargetPath
    approval_scope: DeploymentApprovalScope
    notes: str | None = Field(default=None, max_length=500)


class ModelLifecycleCommandRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class RollbackTargetRequest(BaseModel):
    target_path: TargetPath
    enabled: bool = True
    notes: str | None = Field(default=None, max_length=500)
