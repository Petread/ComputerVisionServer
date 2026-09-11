from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from cv_contracts.common import ResponseMeta
from cv_status import EndpointMode


ApiKeySourceType = Literal["bootstrap", "managed"]


class UserSummary(BaseModel):
    user_id: str
    username: str
    display_name: str
    description: str | None = None
    is_active: bool
    created_at: str
    updated_at: str
    created_by: str | None = None
    last_login_at: str | None = None


class UserDetail(UserSummary):
    password_changed_at: str | None = None
    session_version: int


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    display_name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    password: str = Field(min_length=8, max_length=256)


class UserUpdateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)


class UserPasswordChangeRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=256)


class SelfPasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class ApiKeyScopeSummary(BaseModel):
    scope: str
    label: str
    description: str
    endpoint_mode: EndpointMode | None = None
    assignable: bool = True


class ApiKeySummary(BaseModel):
    api_key_id: str
    key_id: str
    label: str
    description: str | None = None
    intended_usage: str | None = None
    subject_type: str
    subject_id: str
    scopes: list[str] = Field(default_factory=list)
    endpoint_mode_hint: EndpointMode | None = None
    source_type: ApiKeySourceType
    masked_key: str
    secret_prefix: str | None = None
    is_active: bool
    is_revoked: bool
    created_at: str
    updated_at: str
    created_by: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None
    revoked_by: str | None = None
    expires_at: str | None = None
    mutable: bool


class ApiKeyDetail(ApiKeySummary):
    status_reason: str


class ApiKeyCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    intended_usage: str | None = Field(default=None, max_length=2000)
    subject_name: str = Field(min_length=1, max_length=128)
    subject_type: str = Field(default="application", min_length=3, max_length=64)
    scopes: list[str] = Field(default_factory=list, min_length=1)
    expires_at: datetime | None = None
    endpoint_mode_hint: EndpointMode | None = None


class ApiKeyCreateResponse(BaseModel):
    api_key: ApiKeyDetail
    token: str
    token_preview: str
    one_time_reveal: bool = True


class ApiKeyRevokeResponse(BaseModel):
    api_key: ApiKeyDetail
    revoked: bool = True


PlcWriteConnectionStatus = Literal[
    "ready",
    "environment_blocked",
    "dependency_missing",
    "dry_run",
    "disabled",
    "invalid_config",
]
PlcWriteConnectionReusePolicy = Literal["per_execution", "per_runtime", "mixed"]
PlcWriteConnectionReconnectPolicy = Literal["disabled", "on_failure", "mixed"]
PlcWriteConnectionSafetyStatus = Literal["ephemeral", "bounded", "mixed_policy"]


class PlcWriteAccessPipelineRef(BaseModel):
    pipeline_definition_id: str
    pipeline_name: str
    pipeline_version_id: str
    version_no: int = Field(ge=1)
    version_scope: Literal["published", "latest"]
    mode: str
    node_key: str
    node_enabled: bool
    dry_run: bool
    reuse_policy: PlcWriteConnectionReusePolicy = "per_execution"
    reconnect_policy: PlcWriteConnectionReconnectPolicy = "on_failure"
    mapping_count: int = Field(ge=0)
    enabled_mapping_count: int = Field(ge=0)


class PlcWriteAccessConnectionSummary(BaseModel):
    connection_key: str
    display_name: str
    host: str
    port: int = Field(ge=1, le=65535)
    rack: int = Field(ge=0, le=7)
    slot: int = Field(ge=0, le=31)
    connection_ref: str | None = None
    status: PlcWriteConnectionStatus
    status_reason: str
    reuse_policy: PlcWriteConnectionReusePolicy = "per_execution"
    reconnect_policy: PlcWriteConnectionReconnectPolicy = "on_failure"
    connection_safety_status: PlcWriteConnectionSafetyStatus = "ephemeral"
    connection_safety_reason: str = (
        "Connections are opened for one execution and closed afterwards."
    )
    persistent_connection_limit: int = Field(default=0, ge=0)
    node_count: int = Field(ge=0)
    mapping_count: int = Field(ge=0)
    enabled_mapping_count: int = Field(ge=0)
    sample_targets: list[str] = Field(default_factory=list)
    pipelines: list[PlcWriteAccessPipelineRef] = Field(default_factory=list)


class PlcWriteAccessStatusResponse(BaseModel):
    schema_version: Literal["access.plc-write-access.v1"] = "access.plc-write-access.v1"
    allow_writes: bool
    desired_allow_writes: bool
    runtime_env_allow_writes: bool | None = None
    runtime_env_path: str | None = None
    runtime_env_writable: bool = False
    update_supported: bool = False
    restart_required: bool = False
    dependency_available: bool = False
    connections: list[PlcWriteAccessConnectionSummary] = Field(default_factory=list)
    meta: ResponseMeta


class PlcWriteAccessUpdateRequest(BaseModel):
    allow_writes: bool
