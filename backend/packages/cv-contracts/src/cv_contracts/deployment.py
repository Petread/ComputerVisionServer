from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from cv_contracts.modeling import ModelArchitectureSummary
from cv_status import DeploymentPackageStatus, DeploymentReleaseStatus, TargetPath, TaskType


class DeploymentPackageSummary(BaseModel):
    model_version_id: str
    model_family_id: str
    model_family_slug: str
    version_no: int
    task_type: TaskType
    architecture: ModelArchitectureSummary
    status: DeploymentPackageStatus
    built_at: str | None = None
    serving_manifest_uri: str | None = None
    compatible_targets: list[TargetPath] = Field(default_factory=list)
    error: str | None = None


class DeploymentPackageDetail(DeploymentPackageSummary):
    export_manifest_uri: str | None = None
    artifact_manifest_uri: str | None = None
    serving_manifest: dict[str, Any] = Field(default_factory=dict)
    export_manifest: dict[str, Any] = Field(default_factory=dict)
    artifact_manifest: dict[str, Any] = Field(default_factory=dict)


class DeploymentPackageBuildRequest(BaseModel):
    model_version_id: str


class DeploymentReleaseSummary(BaseModel):
    deployment_release_id: str
    target_path: TargetPath
    model_version_id: str
    architecture: ModelArchitectureSummary
    triton_model_name: str
    triton_model_version: int
    status: DeploymentReleaseStatus
    serving_enabled: bool = True
    serving_release_id: str | None = None
    activated_at: str | None = None
    rollback_source_release_id: str | None = None


class DeploymentReleaseDetail(DeploymentReleaseSummary):
    artifact_manifest_uri: str | None = None
    artifact_manifest: dict[str, Any] = Field(default_factory=dict)
    smoke_report_uri: str | None = None
    smoke_report: dict[str, Any] = Field(default_factory=dict)
    compatibility_report: dict[str, Any] = Field(default_factory=dict)


class DeploymentTargetSummary(BaseModel):
    target_path: TargetPath
    active_release_id: str | None = None
    previous_release_id: str | None = None
    active_model_version_id: str | None = None
    previous_model_version_id: str | None = None
    active_release_status: DeploymentReleaseStatus | None = None
    updated_at: str | None = None


class DeploymentCommandRequest(BaseModel):
    model_version_id: str
    target_path: TargetPath
    requested_state: Literal["staged", "production"] = "staged"


class DeploymentSmokeTestRequest(BaseModel):
    reason: str | None = None


class DeploymentModelServingCommandRequest(BaseModel):
    reason: str | None = None


class RollbackCommandRequest(BaseModel):
    reason: str | None = None
