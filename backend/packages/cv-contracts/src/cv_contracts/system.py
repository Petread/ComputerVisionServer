from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from cv_contracts.common import ResponseMeta, ServiceSummary, StatusCard
from cv_contracts.settings import IntegrationStatusSummary
from cv_status import HealthState, RuntimeMode, RuntimeObservedState


class QueueStatus(BaseModel):
    queue_name: str
    depth: int
    oldest_job_age_seconds: int


class WorkerStatusSummary(BaseModel):
    service_name: str
    instance_id: str
    state: HealthState
    status: str
    detail: str
    last_heartbeat_at: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class OperationWorkerSummary(BaseModel):
    service_name: str
    unit_name: str
    display_name: str
    state: HealthState
    status: str
    detail: str
    active_state: str | None = None
    sub_state: str | None = None
    main_pid: int | None = None
    restart_count: int = 0
    last_error: str | None = None
    observed_at: str
    restartable: bool = True


class OperationWorkersResponse(BaseModel):
    items: list[OperationWorkerSummary] = Field(default_factory=list)
    meta: ResponseMeta


class DiskUsageSummary(BaseModel):
    key: str
    label: str
    path: str
    state: HealthState
    detail: str
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    used_percent: float = 0
    observed_at: str


class GpuDeviceSummary(BaseModel):
    device_index: int
    name: str | None = None
    state: HealthState
    detail: str
    utilization_percent: float | None = None
    memory_used_mb: int | None = None
    memory_total_mb: int | None = None
    memory_used_percent: float | None = None
    temperature_c: float | None = None
    observed_at: str


class RuntimePerformanceSnapshot(BaseModel):
    realtime_fps: float | None = None
    realtime_latency_p95_ms: float | None = None
    realtime_drop_rate_percent: float | None = None
    trigger_latency_p95_ms: float | None = None
    trigger_timeout_count: int = 0
    frame_count: int = 0
    drop_count: int = 0
    trigger_count: int = 0


class RuntimeChannelOperationalSummary(BaseModel):
    channel_id: str
    name: str
    technical_place: str
    mode: RuntimeMode
    observed_state: RuntimeObservedState
    health_state: HealthState
    health_text: str
    runtime_instance_id: str | None = None
    executor_service: str | None = None
    assigned_runtime_worker: str | None = None
    last_heartbeat_at: str | None = None
    heartbeat_age_seconds: int | None = None
    recovery_attempts: int = 0
    recovery_hint: str | None = None
    fps: float | None = None
    latency_p95_ms: float | None = None
    drop_rate_percent: float | None = None
    trigger_latency_ms: float | None = None
    frame_count: int = 0
    drop_count: int = 0
    trigger_count: int = 0
    last_error: str | None = None


class RuntimeOperationalSummary(BaseModel):
    state: HealthState = HealthState.OK
    total_channels: int = 0
    running_channels: int = 0
    degraded_channels: int = 0
    failed_channels: int = 0
    stopped_channels: int = 0
    realtime_active_channels: int = 0
    trigger_active_channels: int = 0
    active_runtime_instances: int = 0
    stale_runtime_instances: int = 0
    failed_runtime_instances: int = 0
    recovering_channels: int = 0
    performance: RuntimePerformanceSnapshot = Field(default_factory=RuntimePerformanceSnapshot)
    attention: list[RuntimeChannelOperationalSummary] = Field(default_factory=list)
    executors: list[RuntimeChannelOperationalSummary] = Field(default_factory=list)
    recovery_hint: str | None = None
    observed_at: str


class SystemAlert(BaseModel):
    code: str
    severity: Literal["info", "warning", "critical"]
    source: str
    title: str
    detail: str
    remediation_hint: str | None = None
    observed_at: str


class SystemStatusResponse(BaseModel):
    services: list[ServiceSummary]
    dependencies: list[IntegrationStatusSummary]
    workers: list[WorkerStatusSummary]
    queues: list[QueueStatus]
    disks: list[DiskUsageSummary]
    gpus: list[GpuDeviceSummary]
    runtime: RuntimeOperationalSummary
    alerts: list[SystemAlert]
    cards: list[StatusCard]
    meta: ResponseMeta


UpdateChangeType = Literal["added", "modified", "deleted", "renamed", "unknown"]
UpdateRiskLevel = Literal["low", "medium", "high", "blocked"]
UpdateActionType = Literal["none", "restart", "manual"]
UpdateApplyStatus = Literal["accepted", "running", "succeeded", "failed", "blocked"]


class OfflineUpdateChange(BaseModel):
    path: str
    change_type: UpdateChangeType = "unknown"
    category: str
    affected_services: list[str] = Field(default_factory=list)
    restart_required: bool = False
    rebuild_required: bool = False
    migration_required: bool = False
    hot_apply_supported: bool = True
    reason: str


class OfflineUpdateServicePlan(BaseModel):
    service_name: str
    action: UpdateActionType
    risk: UpdateRiskLevel = "low"
    changed_files: list[str] = Field(default_factory=list)
    safe_to_restart_independently: bool = True
    expected_impact: str = ""
    required_follow_up: str = ""
    blockers: list[str] = Field(default_factory=list)


class OfflineUpdateBundleSummary(BaseModel):
    file_name: str
    path: str
    size_bytes: int
    modified_at: str
    bundle_type: str = "unknown"
    app_name: str | None = None
    app_version: str | None = None
    distro_name: str | None = None
    app_host_name: str | None = None
    build_timestamp: str | None = None
    apply_token: str
    affected_services: list[str] = Field(default_factory=list)
    affected_workers: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OfflineUpdatePlanResponse(BaseModel):
    enabled: bool
    apply_available: bool
    repo_root: str | None = None
    current_git_ref: str | None = None
    current_git_sha: str | None = None
    last_applied_git_sha: str | None = None
    last_applied_at: str | None = None
    pending_change_count: int = 0
    changed_files: list[OfflineUpdateChange] = Field(default_factory=list)
    service_plans: list[OfflineUpdateServicePlan] = Field(default_factory=list)
    blocked: bool = False
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    apply_token: str | None = None
    incoming_dir: str | None = None
    incoming_bundles: list[OfflineUpdateBundleSummary] = Field(default_factory=list)
    selected_bundle: OfflineUpdateBundleSummary | None = None
    meta: ResponseMeta


class OfflineUpdateApplyRequest(BaseModel):
    apply_token: str
    service_names: list[str] | None = None
    reason: str | None = None
    force: bool = False


class OfflineUpdateApplyResult(BaseModel):
    operation_id: str
    status: UpdateApplyStatus
    accepted_at: str
    service_names: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    message: str
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    meta: ResponseMeta


class OfflineUpdateScheduleRequest(BaseModel):
    apply_token: str
    scheduled_for: str
    reason: str | None = None


class OfflineUpdateScheduleResult(BaseModel):
    operation_id: str
    status: UpdateApplyStatus
    scheduled_for: str
    message: str
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    meta: ResponseMeta
