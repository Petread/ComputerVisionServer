from __future__ import annotations

from cv_status import HealthState, UserRole
from pydantic import BaseModel, Field

from cv_contracts.auth import SessionUser
from cv_contracts.common import ResponseMeta
from cv_contracts.deployment import DeploymentTargetSummary


class IntegrationStatusSummary(BaseModel):
    name: str
    category: str
    state: HealthState
    configured: bool = True
    reachable: bool = False
    endpoint: str | None = None
    detail: str
    remediation_hint: str | None = None
    observed_at: str


class StoragePathSummary(BaseModel):
    key: str
    label: str
    path: str
    exists: bool
    writable: bool


class AuthSupportSummary(BaseModel):
    cookie_name: str
    csrf_cookie_name: str
    csrf_header_name: str
    secure_cookie: bool
    session_max_age_seconds: int
    current_user: SessionUser | None = None
    supported_roles: list[UserRole] = Field(default_factory=list)
    api_key_auth_enabled: bool = True
    api_key_bootstrap_count: int = 0


class LabelStudioCredentialStatus(BaseModel):
    configured: bool
    token_file_path: str
    write_target_path: str | None = None
    writable: bool
    last_verified: bool | None = None
    project_count: int | None = None
    message: str
    updated_at: str


class LabelStudioTokenUpdateRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class LabelStudioTokenUpdateResponse(BaseModel):
    credentials: LabelStudioCredentialStatus
    meta: ResponseMeta


class SettingsStatusResponse(BaseModel):
    environment: str
    public_base_url: str
    storage_paths: list[StoragePathSummary] = Field(default_factory=list)
    integrations: list[IntegrationStatusSummary] = Field(default_factory=list)
    deployment_targets: list[DeploymentTargetSummary] = Field(default_factory=list)
    auth: AuthSupportSummary
    label_studio_credentials: LabelStudioCredentialStatus
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    meta: ResponseMeta
