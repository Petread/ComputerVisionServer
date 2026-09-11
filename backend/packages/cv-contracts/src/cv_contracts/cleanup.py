from __future__ import annotations

from pydantic import BaseModel, Field


class CleanupEntityImpact(BaseModel):
    scope_type: str
    scope_id: str
    display_name: str
    count: int = Field(default=0, ge=0)


class CleanupArtifactImpact(BaseModel):
    path: str
    category: str


class CleanupImpactPreview(BaseModel):
    scope_type: str
    scope_id: str
    display_name: str
    confirmation_token: str
    blocked: bool = False
    blocked_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    will_remove: list[CleanupEntityImpact] = Field(default_factory=list)
    will_remain: list[str] = Field(default_factory=list)
    active_deployment_targets: list[str] = Field(default_factory=list)
    artifact_paths: list[CleanupArtifactImpact] = Field(default_factory=list)


class CleanupExecutionRequest(BaseModel):
    confirmation_token: str = Field(min_length=8, max_length=256)
    reason: str | None = Field(default=None, max_length=500)
    delete_artifacts: bool = False
    delete_entire_pipeline: bool = False


class CleanupExecutionResult(BaseModel):
    scope_type: str
    scope_id: str
    already_deleted: bool = False
    deleted_entities: list[CleanupEntityImpact] = Field(default_factory=list)
    retained_entities: list[str] = Field(default_factory=list)
    deleted_artifacts: list[str] = Field(default_factory=list)
    artifact_errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
