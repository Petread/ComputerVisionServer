from __future__ import annotations

import math
from typing import Any, Literal

from cv_status import (
    HealthState,
    RuntimeChannelDesiredState,
    RuntimeControlCommand,
    RuntimeControlCommandStatus,
    RuntimeExecutionStatus,
    RuntimeMode,
    RuntimeNodeCategory,
    RuntimeObservedState,
    RuntimePipelineStatus,
    RuntimeResourceStatus,
    RuntimeSourceType,
    TargetPath,
    TaskType,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cv_contracts.common import ResponseMeta


class StrictRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeLogicalModelRef(StrictRuntimeModel):
    model_family_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    target_path: TargetPath = TargetPath.REALTIME
    contract_major: int = Field(default=1, ge=1)
    selection_policy: Literal["active_production", "staged_test", "explicit_release"] = (
        "active_production"
    )
    required_task_type: TaskType
    deployment_release_id: str | None = Field(default=None, min_length=3, max_length=64)

    @model_validator(mode="after")
    def validate_explicit_release(self) -> RuntimeLogicalModelRef:
        if self.selection_policy == "explicit_release" and not self.deployment_release_id:
            raise ValueError("deployment_release_id is required for explicit_release policy")
        return self


class RuntimeSourceCreateRequest(StrictRuntimeModel):
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    name: str = Field(min_length=3, max_length=160)
    source_type: RuntimeSourceType
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)


class RuntimeSourceSummary(StrictRuntimeModel):
    source_id: str
    slug: str
    name: str
    source_type: RuntimeSourceType
    validation_status: RuntimeResourceStatus
    health_state: HealthState
    last_validated_at: str | None = None
    last_error: str | None = None


class RuntimeChannelSourceConfig(StrictRuntimeModel):
    source_type: RuntimeSourceType
    config: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(default="Not configured", max_length=240)


class RuntimeChannelCaptureSummary(StrictRuntimeModel):
    summary: str = Field(default="No capture rules", max_length=240)
    rule_count: int = Field(default=0, ge=0)
    target_project_count: int = Field(default=0, ge=0)
    modes: list[str] = Field(default_factory=list)


class RuntimeChannelOutputSummary(StrictRuntimeModel):
    summary: str = Field(default="No outputs configured", max_length=240)
    connector_count: int = Field(default=0, ge=0)
    primary_outputs: list[str] = Field(default_factory=list)


class RuntimeChannelHealthSummary(StrictRuntimeModel):
    state: HealthState = HealthState.DOWN
    status_text: str = "Not running"
    last_error: str | None = None
    last_heartbeat_at: str | None = None
    recovery_hint: str | None = None


class RuntimeChannelPerformanceSummary(StrictRuntimeModel):
    fps: float | None = Field(default=None, ge=0)
    source_fps: float | None = Field(default=None, ge=0)
    processed_fps: float | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    latency_p50_ms: float | None = Field(default=None, ge=0)
    latency_p95_ms: float | None = Field(default=None, ge=0)
    latency_p99_ms: float | None = Field(default=None, ge=0)
    drop_rate_percent: float | None = Field(default=None, ge=0, le=100)
    frame_count: int = Field(default=0, ge=0)
    drop_count: int = Field(default=0, ge=0)
    trigger_count: int = Field(default=0, ge=0)
    reconnect_count: int = Field(default=0, ge=0)
    source_error_count: int = Field(default=0, ge=0)
    last_frame_at: str | None = None
    last_result_status: str | None = None


class RuntimeChannelRuntimeState(StrictRuntimeModel):
    channel_id: str
    runtime_instance_id: str | None = None
    execution_mode: RuntimeMode
    desired_state: RuntimeChannelDesiredState
    actual_state: RuntimeObservedState
    loading_state: str = "not_started"
    last_heartbeat_at: str | None = None
    last_error: str | None = None
    last_start_at: str | None = None
    last_stop_at: str | None = None
    assigned_pipeline_version_id: str | None = None
    assigned_runtime_worker: str | None = None
    heartbeat_age_seconds: int | None = Field(default=None, ge=0)
    recovery_attempts: int = Field(default=0, ge=0)
    recovery_hint: str | None = None
    health_summary: dict[str, Any] = Field(default_factory=dict)
    metrics_summary: dict[str, Any] = Field(default_factory=dict)


class RuntimeChannelTriggerablePipelineLink(StrictRuntimeModel):
    trigger_endpoint_id: str
    name: str
    description: str | None = None
    slug: str
    trigger_type: Literal["http_put"] = "http_put"
    method: Literal["PUT"] = "PUT"
    path: str
    enabled: bool
    auth_required: bool = False
    auth_scope: str | None = None
    status: RuntimeResourceStatus
    pipeline_version_id: str | None = None
    pipeline_label: str | None = None
    last_invoked_at: str | None = None
    last_status: str | None = None
    last_latency_ms: float | None = Field(default=None, ge=0)


class RuntimeChannelRequestEndpoint(StrictRuntimeModel):
    kind: Literal["trigger_execution", "latest_result"]
    label: str
    description: str | None = None
    method: Literal["GET", "PUT"]
    path: str
    available: bool
    unavailable_reason: str | None = None
    auth_required: bool = False
    auth_label: str
    trigger_endpoint_id: str | None = None
    pipeline_version_id: str | None = None
    pipeline_label: str | None = None
    status: RuntimeResourceStatus | None = None
    last_activity_at: str | None = None


class RuntimeSourceCredentialInput(StrictRuntimeModel):
    username: str | None = Field(default=None, min_length=1, max_length=512)
    password: str | None = Field(default=None, min_length=1, max_length=4096)


class RuntimeResultHistoryPolicy(StrictRuntimeModel):
    enabled: bool = False
    retention_hours: int = Field(default=24, ge=1, le=8760)
    sample_interval_seconds: int = Field(default=5, ge=1, le=3600)
    max_records: int = Field(default=10_000, ge=1, le=1_000_000)


class RuntimeChannelPlacementRequest(StrictRuntimeModel):
    target: Literal["server", "edge_device"] = "server"
    edge_device_id: str | None = Field(default=None, min_length=3, max_length=64)

    @model_validator(mode="after")
    def validate_target(self) -> RuntimeChannelPlacementRequest:
        if self.target == "edge_device" and not self.edge_device_id:
            raise ValueError("edge_device_id is required for edge_device placement")
        if self.target == "server" and self.edge_device_id is not None:
            raise ValueError("edge_device_id must be omitted for server placement")
        return self


class RuntimeChannelPlacementSummary(RuntimeChannelPlacementRequest):
    edge_device_name: str | None = None
    edge_management_ip: str | None = None
    edge_connectivity_state: str | None = None
    workload_release_id: str | None = None
    edge_deployment_id: str | None = None
    edge_deployment_status: str | None = None
    edge_admission_status: str | None = None


StemRotationPrioritySource = Literal["FRONTEND", "PLC"]
StemRotationPriorityMode = Literal[
    "NONE",
    "VORSCHNITT_KRITISCH",
    "NACHSCHNITT_KRITISCH",
]


class RuntimeStemRotationPriorityControl(StrictRuntimeModel):
    """One revisioned source-of-truth for commissioning priority selection."""

    schema_version: Literal["runtime.stem-rotation-priority-control.v1"] = (
        "runtime.stem-rotation-priority-control.v1"
    )
    priority_source: StemRotationPrioritySource = "FRONTEND"
    priority_mode: StemRotationPriorityMode = "NONE"
    optimization_enable: bool = True
    revision: int = Field(default=0, ge=0)
    updated_at: str | None = None
    updated_by: str | None = None


class RuntimeChannelCreateRequest(StrictRuntimeModel):
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    name: str = Field(min_length=3, max_length=160)
    technical_place: str = Field(min_length=1, max_length=160)
    facility_id: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    source_type: RuntimeSourceType = RuntimeSourceType.RTSP
    source_config: dict[str, Any] = Field(default_factory=dict)
    source_credentials: RuntimeSourceCredentialInput | None = None
    mode: RuntimeMode = RuntimeMode.REALTIME
    assigned_pipeline: str | None = Field(default=None, min_length=2, max_length=160)
    output_summary: RuntimeChannelOutputSummary = Field(default_factory=RuntimeChannelOutputSummary)
    capture_summary: RuntimeChannelCaptureSummary = Field(
        default_factory=RuntimeChannelCaptureSummary
    )
    source_id: str | None = Field(default=None, min_length=3, max_length=64)
    pipeline_version_id: str | None = Field(default=None, min_length=3, max_length=64)
    authoring_label_project_ids: list[str] = Field(default_factory=list)
    result_history_policy: RuntimeResultHistoryPolicy = Field(
        default_factory=RuntimeResultHistoryPolicy
    )
    stem_rotation_priority_source: StemRotationPrioritySource = "FRONTEND"
    stem_rotation_priority_mode: StemRotationPriorityMode = "NONE"
    stem_rotation_optimization_enable: bool = True
    placement: RuntimeChannelPlacementRequest = Field(
        default_factory=RuntimeChannelPlacementRequest
    )


class RuntimeChannelUpdateRequest(StrictRuntimeModel):
    facility_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, min_length=3, max_length=160)
    technical_place: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    source_type: RuntimeSourceType | None = None
    source_config: dict[str, Any] | None = None
    source_credentials: RuntimeSourceCredentialInput | None = None
    mode: RuntimeMode | None = None
    assigned_pipeline: str | None = Field(default=None, min_length=2, max_length=160)
    output_summary: RuntimeChannelOutputSummary | None = None
    capture_summary: RuntimeChannelCaptureSummary | None = None
    source_id: str | None = Field(default=None, min_length=3, max_length=64)
    pipeline_version_id: str | None = Field(default=None, min_length=3, max_length=64)
    authoring_label_project_ids: list[str] | None = None
    result_history_policy: RuntimeResultHistoryPolicy | None = None
    stem_rotation_priority_source: StemRotationPrioritySource | None = None
    stem_rotation_priority_mode: StemRotationPriorityMode | None = None
    stem_rotation_optimization_enable: bool | None = None
    desired_state: RuntimeChannelDesiredState | None = None
    placement: RuntimeChannelPlacementRequest | None = None


class RuntimeChannelSummary(StrictRuntimeModel):
    facility_id: str | None = None
    channel_id: str
    slug: str
    name: str
    technical_place: str
    mode: RuntimeMode
    source_type: RuntimeSourceType
    source_summary: str
    assigned_pipeline: str | None = None
    output_summary: RuntimeChannelOutputSummary = Field(default_factory=RuntimeChannelOutputSummary)
    capture_summary: RuntimeChannelCaptureSummary = Field(
        default_factory=RuntimeChannelCaptureSummary
    )
    health: RuntimeChannelHealthSummary = Field(default_factory=RuntimeChannelHealthSummary)
    performance: RuntimeChannelPerformanceSummary = Field(
        default_factory=RuntimeChannelPerformanceSummary
    )
    runtime_state: RuntimeChannelRuntimeState | None = None
    source_id: str | None = None
    pipeline_version_id: str | None = None
    authoring_label_project_ids: list[str] = Field(default_factory=list)
    result_history_policy: RuntimeResultHistoryPolicy = Field(
        default_factory=RuntimeResultHistoryPolicy
    )
    stem_rotation_priority_control: RuntimeStemRotationPriorityControl = Field(
        default_factory=RuntimeStemRotationPriorityControl
    )
    placement: RuntimeChannelPlacementSummary = Field(
        default_factory=RuntimeChannelPlacementSummary
    )
    desired_state: RuntimeChannelDesiredState
    observed_state: RuntimeObservedState
    status: RuntimeObservedState
    health_state: HealthState
    last_activity_at: str | None = None
    last_error: str | None = None


class RuntimeChannelDetail(RuntimeChannelSummary):
    description: str | None = None
    source_config: dict[str, Any] = Field(default_factory=dict)
    triggerable_pipelines: list[RuntimeChannelTriggerablePipelineLink] = Field(default_factory=list)
    request_endpoints: list[RuntimeChannelRequestEndpoint] = Field(default_factory=list)
    created_at: str
    updated_at: str | None = None


class RuntimeChannelAuthoringLabelProject(StrictRuntimeModel):
    label_project_id: str
    label_studio_project_id: int
    title: str
    task_type: TaskType
    dataset_name: str | None = None
    labels: list[str] = Field(default_factory=list)


class RuntimeChannelAuthoringContext(StrictRuntimeModel):
    channel: RuntimeChannelDetail
    source_config: dict[str, Any] = Field(default_factory=dict)
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    label_projects: list[RuntimeChannelAuthoringLabelProject] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    pipeline_definition_id: str | None = None
    pipeline_version_id: str | None = None


class RuntimeChannelAuthoringContextResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.channel-authoring.context.v1"] = (
        "runtime.channel-authoring.context.v1"
    )
    context: RuntimeChannelAuthoringContext
    meta: ResponseMeta


class RuntimeChannelPipelineInstanceRequest(StrictRuntimeModel):
    template_definition_id: str | None = Field(default=None, min_length=3, max_length=64)
    template_version_id: str | None = Field(default=None, min_length=3, max_length=64)
    name: str | None = Field(default=None, min_length=3, max_length=160)
    replace_existing: bool = False


class RuntimeChannelListResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.channels.list.v1"] = "runtime.channels.list.v1"
    items: list[RuntimeChannelSummary] = Field(default_factory=list)
    meta: ResponseMeta


class RuntimeChannelStatusCounts(StrictRuntimeModel):
    total: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    stopped: int = Field(default=0, ge=0)
    degraded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    draft: int = Field(default=0, ge=0)
    start_requested: int = Field(default=0, ge=0)
    stop_requested: int = Field(default=0, ge=0)


class RuntimeChannelStatusOverview(StrictRuntimeModel):
    schema_version: Literal["runtime.channels.status-overview.v1"] = (
        "runtime.channels.status-overview.v1"
    )
    counts: RuntimeChannelStatusCounts
    health: list[RuntimeChannelHealthSummary] = Field(default_factory=list)
    technical_places: list[str] = Field(default_factory=list)
    source_types: list[RuntimeSourceType] = Field(default_factory=list)
    modes: list[RuntimeMode] = Field(default_factory=list)
    meta: ResponseMeta


class RuntimeChannelTestFileUploadResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.channel-test-file.upload.v1"] = (
        "runtime.channel-test-file.upload.v1"
    )
    file_name: str
    content_type: str
    size_bytes: int = Field(ge=0)
    path: str
    source_config: dict[str, Any] = Field(default_factory=dict)
    meta: ResponseMeta


class RuntimeChannelCommandRequest(StrictRuntimeModel):
    reason: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=160)
    force: bool = False


class RuntimeControlCommandSummary(StrictRuntimeModel):
    job_id: str
    command_id: str
    channel_id: str
    command: RuntimeControlCommand
    queue: Literal["runtime-control"] = "runtime-control"
    status: RuntimeControlCommandStatus
    accepted_at: str
    claimed_at: str | None = None
    consumed_at: str | None = None
    finished_at: str | None = None
    runtime_instance_id: str | None = None
    last_error: str | None = None


class RuntimeChannelCommandResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.channel.command.v1"] = "runtime.channel.command.v1"
    channel: RuntimeChannelSummary
    accepted_job: RuntimeControlCommandSummary
    meta: ResponseMeta


RuntimeManualCaptureMode = Literal["single", "interval"]
RuntimeManualCaptureSource = Literal["LIVE_VIEW_FRAME", "PIPELINE_IMAGE_OUTPUT"]
RuntimeManualCaptureSessionState = Literal["idle", "running", "stopped", "failed"]
RuntimeManualCaptureResultStatus = Literal["queued", "success", "failed"]
RuntimeCaptureDeliveryStatus = Literal[
    "queued",
    "processing",
    "rate_limited",
    "delivered",
    "predicted",
    "failed_retryable",
    "failed_terminal",
    "skipped_duplicate",
    "recorded",
    "skipped_not_released",
    "skipped_sampling",
    "needs_attention",
]
RuntimeConnectorDeliveryStatus = Literal[
    "queued",
    "processing",
    "delivered",
    "failed_retryable",
    "failed_terminal",
    "skipped_no_payload",
]


class RuntimeManualCaptureTarget(StrictRuntimeModel):
    capture_target_id: str
    display_name: str
    detail: str | None = None
    task_type: TaskType
    status: str
    dataset_name: str | None = None
    label_studio_project_id: int | None = None
    selectable: bool = True


class RuntimeManualCaptureTargetListResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.manual-capture.targets.v1"] = (
        "runtime.manual-capture.targets.v1"
    )
    items: list[RuntimeManualCaptureTarget] = Field(default_factory=list)
    meta: ResponseMeta


class RuntimeManualCaptureRequest(StrictRuntimeModel):
    capture_target_id: str = Field(min_length=3, max_length=64)
    capture_source: RuntimeManualCaptureSource = "LIVE_VIEW_FRAME"
    view_profile_id: str | None = Field(default=None, min_length=3, max_length=128)
    require_eligibility: bool = False
    request_predictions: bool = True
    live_session_id: str | None = Field(default=None, min_length=3, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=160)


class RuntimeManualIntervalCaptureStartRequest(RuntimeManualCaptureRequest):
    interval_seconds: int = Field(default=10, ge=1, le=3600)


class RuntimeManualIntervalCaptureStopRequest(StrictRuntimeModel):
    reason: str | None = Field(default=None, max_length=1000)


class RuntimeManualCaptureResult(StrictRuntimeModel):
    capture_request_id: str | None = None
    manual_capture_session_id: str | None = None
    channel_id: str
    channel_display_name: str
    capture_target_id: str
    target_display_name: str
    label_studio_project_id: int
    mode: RuntimeManualCaptureMode
    interval_seconds: int | None = Field(default=None, ge=1, le=3600)
    captured_at: str
    operator_user_id: str
    live_session_id: str | None = None
    source_frame_metadata: dict[str, Any] = Field(default_factory=dict)
    status: RuntimeManualCaptureResultStatus
    asset_uri: str | None = None
    label_studio_task_ids: list[int] = Field(default_factory=list)
    error_summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeManualCaptureResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.manual-capture.result.v1"] = "runtime.manual-capture.result.v1"
    result: RuntimeManualCaptureResult
    meta: ResponseMeta


class RuntimeManualCaptureSessionStatus(StrictRuntimeModel):
    manual_capture_session_id: str | None = None
    channel_id: str
    state: RuntimeManualCaptureSessionState
    capture_target_id: str | None = None
    target_display_name: str | None = None
    label_studio_project_id: int | None = None
    interval_seconds: int | None = Field(default=None, ge=1, le=3600)
    operator_user_id: str | None = None
    live_session_id: str | None = None
    started_at: str | None = None
    stopped_at: str | None = None
    last_capture_at: str | None = None
    capture_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_result: RuntimeManualCaptureResult | None = None
    error_summary: str | None = None


class RuntimeManualCaptureSessionResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.manual-capture.session.v1"] = (
        "runtime.manual-capture.session.v1"
    )
    session: RuntimeManualCaptureSessionStatus
    meta: ResponseMeta


class RuntimeManualCaptureHistoryResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.manual-capture.history.v1"] = (
        "runtime.manual-capture.history.v1"
    )
    items: list[RuntimeManualCaptureResult] = Field(default_factory=list)
    meta: ResponseMeta


class RuntimeCaptureRequestSummary(StrictRuntimeModel):
    capture_request_id: str
    channel_id: str
    capture_rule_id: str
    result_record_id: str | None = None
    manual_capture_session_id: str | None = None
    manual_capture_request_id: str | None = None
    target_label_project_id: str
    target_display_name: str | None = None
    label_studio_project_id: int | None = None
    source_type: Literal["manual", "pipeline", "temporal_sample"]
    capture_mode: Literal[
        "full_frame",
        "roi",
        "conditional",
        "multi_project",
        "full_image",
        "full_image_and_roi",
        "temporal_sequence",
    ]
    status: RuntimeCaptureDeliveryStatus
    retry_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    queued_at: str | None = None
    claimed_at: str | None = None
    submitted_at: str | None = None
    delivered_at: str | None = None
    next_attempt_at: str | None = None
    rate_limited_until: str | None = None
    asset_uri: str | None = None
    digest: str | None = None
    roi: dict[str, Any] = Field(default_factory=dict)
    label_studio_task_ids: list[int] = Field(default_factory=list)
    duplicate_of_request_id: str | None = None
    last_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


class RuntimeCaptureHistoryResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.capture.history.v1"] = "runtime.capture.history.v1"
    items: list[RuntimeCaptureRequestSummary] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    meta: ResponseMeta


class RuntimeCaptureDrainRequest(StrictRuntimeModel):
    limit: int = Field(default=20, ge=1, le=500)


class RuntimeCaptureDrainResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.capture.drain.v1"] = "runtime.capture.drain.v1"
    processed: int = Field(default=0, ge=0)
    delivered: int = Field(default=0, ge=0)
    failed_retryable: int = Field(default=0, ge=0)
    failed_terminal: int = Field(default=0, ge=0)
    skipped_duplicate: int = Field(default=0, ge=0)
    rate_limited: int = Field(default=0, ge=0)
    needs_attention: int = Field(default=0, ge=0)
    enqueued_interval_captures: int = Field(default=0, ge=0)
    meta: ResponseMeta


class RuntimeNodePort(StrictRuntimeModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    data_type: str = Field(min_length=2, max_length=120)
    required: bool = True


class RuntimeNodePropertyOption(StrictRuntimeModel):
    value: str
    label: str


class RuntimeNodePropertySpec(StrictRuntimeModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=160)
    field_type: Literal[
        "text",
        "number",
        "integer",
        "slider",
        "boolean",
        "select",
        "channel_ref",
        "image_upload",
        "image_upload_list",
        "model_ref",
        "zone_list",
        "roi",
        "label_project_list",
        "response_mapping",
        "result_projection_table",
        "expression",
        "edge_role_mapping",
        "measurement_rule_table",
        "clearance_rule_table",
        "cv_line_selection_rules",
        "metric_apply_to",
        "quality_rule_table",
        "spatial_rule_table",
        "color_profile_table",
        "direction_reference",
        "board_surface_calibration",
        "camera_intrinsic_calibration",
        "image_view_profile",
        "stem_curvature_process_calibration",
        "stem_surface_phase_calibration",
        "stem_minimum_principle_validation",
        "board_cut_class_rules",
        "board_surface_class_mapping",
        "plc_connection",
        "plc_write_mapping_table",
    ]
    required: bool = False
    default_value: Any | None = None
    options: list[RuntimeNodePropertyOption] = Field(default_factory=list)
    min_value: float | None = None
    max_value: float | None = None
    step_value: float | None = Field(default=None, gt=0)
    section: str | None = Field(default=None, max_length=80)
    help_text: str | None = Field(default=None, max_length=500)


class RuntimeNodeSpec(StrictRuntimeModel):
    node_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    node_type: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    node_category: RuntimeNodeCategory
    node_contract_version: int = Field(default=1, ge=1)
    input_ports: list[RuntimeNodePort] = Field(default_factory=list)
    output_ports: list[RuntimeNodePort] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    logical_model_ref: RuntimeLogicalModelRef | None = None
    position: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_inference_model_ref(self) -> RuntimeNodeSpec:
        template_model_unbound = bool(self.config.get("template_model_unbound", False))
        if (
            self.node_category == RuntimeNodeCategory.INFERENCE
            and self.logical_model_ref is None
            and not template_model_unbound
        ):
            raise ValueError("inference nodes require logical_model_ref")
        if self.node_category != RuntimeNodeCategory.INFERENCE and self.logical_model_ref:
            raise ValueError("logical_model_ref is only valid for inference nodes")
        return self


class RuntimeAuthoringNodeSpec(StrictRuntimeModel):
    node_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    node_type: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    config: dict[str, Any] = Field(default_factory=dict)
    logical_model_ref: RuntimeLogicalModelRef | None = None
    position: dict[str, float] = Field(default_factory=dict)


class RuntimeGraphEdge(StrictRuntimeModel):
    from_node_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    from_port: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    to_node_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    to_port: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class RuntimePipelineGraph(StrictRuntimeModel):
    nodes: list[RuntimeNodeSpec] = Field(default_factory=list)
    edges: list[RuntimeGraphEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_node_keys(self) -> RuntimePipelineGraph:
        node_keys = [node.node_key for node in self.nodes]
        if len(node_keys) != len(set(node_keys)):
            raise ValueError("pipeline node_key values must be unique")
        known_node_keys = set(node_keys)
        for edge in self.edges:
            if edge.from_node_key not in known_node_keys:
                raise ValueError(f"unknown from_node_key: {edge.from_node_key}")
            if edge.to_node_key not in known_node_keys:
                raise ValueError(f"unknown to_node_key: {edge.to_node_key}")
        return self


class RuntimePipelineAuthoringGraph(StrictRuntimeModel):
    nodes: list[RuntimeAuthoringNodeSpec] = Field(default_factory=list)
    edges: list[RuntimeGraphEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_node_keys(self) -> RuntimePipelineAuthoringGraph:
        node_keys = [node.node_key for node in self.nodes]
        if len(node_keys) != len(set(node_keys)):
            raise ValueError("pipeline authoring node_key values must be unique")
        known_node_keys = set(node_keys)
        for edge in self.edges:
            if edge.from_node_key not in known_node_keys:
                raise ValueError(f"unknown from_node_key: {edge.from_node_key}")
            if edge.to_node_key not in known_node_keys:
                raise ValueError(f"unknown to_node_key: {edge.to_node_key}")
        return self


RuntimeProjectionDestination = Literal[
    "response",
    "history",
    "live_view",
    "overlay",
    "plc",
    "connector",
]

RuntimeProjectionOperator = Literal[
    "exists",
    "not_exists",
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "in",
    "not_in",
    "truthy",
    "falsy",
]


class RuntimeValueSelector(StrictRuntimeModel):
    node_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    output_port: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    path: str = Field(default="", max_length=240)
    value_type: Literal[
        "auto",
        "scalar",
        "number",
        "boolean",
        "text",
        "object",
        "object_list",
        "list",
        "map",
    ] = "auto"


class RuntimeProjectionFilter(StrictRuntimeModel):
    path: str = Field(default="", max_length=240)
    op: RuntimeProjectionOperator
    value: Any | None = None


class RuntimeProjectionAggregate(StrictRuntimeModel):
    op: Literal["none", "count", "list", "first", "any", "all", "sum", "avg", "min", "max"] = "none"
    path: str | None = Field(default=None, max_length=240)


class RuntimeProjectionField(StrictRuntimeModel):
    source_path: str = Field(min_length=1, max_length=240)
    output_key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    required: bool = False
    default_value: Any | None = None


class RuntimeDecisionRule(StrictRuntimeModel):
    op: RuntimeProjectionOperator = "gt"
    value: Any | None = 0
    when_true: Any = "FAIL"
    when_false: Any = "PASS"


class RuntimeResultProjection(StrictRuntimeModel):
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    label: str | None = Field(default=None, max_length=160)
    source: RuntimeValueSelector = Field(default_factory=RuntimeValueSelector)
    filters: list[RuntimeProjectionFilter] = Field(default_factory=list)
    aggregate: RuntimeProjectionAggregate = Field(default_factory=RuntimeProjectionAggregate)
    fields: list[RuntimeProjectionField] = Field(default_factory=list)
    decision: RuntimeDecisionRule | None = None
    destinations: list[RuntimeProjectionDestination] = Field(
        default_factory=lambda: ["response", "history"]
    )
    required: bool = False
    default_value: Any | None = None

    @model_validator(mode="after")
    def validate_destinations(self) -> RuntimeResultProjection:
        if not self.destinations:
            raise ValueError("result projection requires at least one destination")
        if len(self.destinations) != len(set(self.destinations)):
            raise ValueError("result projection destinations must be unique")
        return self


class RuntimeOutputPolicy(StrictRuntimeModel):
    projections: list[RuntimeResultProjection] = Field(default_factory=list)
    default_destinations: list[RuntimeProjectionDestination] = Field(
        default_factory=lambda: ["response", "history"]
    )
    include_runtime_metadata: bool = True

    @model_validator(mode="after")
    def validate_projection_keys(self) -> RuntimeOutputPolicy:
        keys = [projection.key for projection in self.projections]
        if len(keys) != len(set(keys)):
            raise ValueError("result projection keys must be unique")
        if len(self.default_destinations) != len(set(self.default_destinations)):
            raise ValueError("default projection destinations must be unique")
        return self


class RuntimePipelineOutputCatalogField(StrictRuntimeModel):
    key: str = Field(min_length=1, max_length=240)
    label: str = Field(min_length=1, max_length=160)
    value_type: Literal[
        "auto",
        "scalar",
        "number",
        "boolean",
        "text",
        "object",
        "object_list",
        "list",
        "map",
    ] = "auto"
    aggregate_ops: list[str] = Field(default_factory=list)
    filterable_fields: list[str] = Field(default_factory=list)
    list_item_fields: list[str] = Field(default_factory=list)


class RuntimePipelineOutputCatalogEntry(StrictRuntimeModel):
    node_key: str
    node_type: str
    output_port: str | None = None
    payload_type: str | None = None
    fields: list[str] = Field(default_factory=list)
    field_options: list[RuntimePipelineOutputCatalogField] = Field(default_factory=list)


class RuntimePipelineOutputCatalog(StrictRuntimeModel):
    entries: list[RuntimePipelineOutputCatalogEntry] = Field(default_factory=list)
    destinations: list[RuntimeProjectionDestination] = Field(
        default_factory=lambda: ["response", "history", "live_view", "overlay", "plc", "connector"]
    )
    aggregate_ops: list[str] = Field(
        default_factory=lambda: [
            "none",
            "count",
            "list",
            "first",
            "any",
            "all",
            "sum",
            "avg",
            "min",
            "max",
        ]
    )


class RuntimePipelineDefinitionCreateRequest(StrictRuntimeModel):
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    name: str = Field(min_length=3, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    template: bool = False


class RuntimePipelineVersionCreateRequest(StrictRuntimeModel):
    mode: RuntimeMode
    graph: RuntimePipelineGraph | None = None
    authoring_graph: RuntimePipelineAuthoringGraph | None = None
    change_note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_graph_input(self) -> RuntimePipelineVersionCreateRequest:
        if self.graph is None and self.authoring_graph is None:
            raise ValueError("graph or authoring_graph is required")
        return self


class RuntimePipelineDefinitionUpdateRequest(StrictRuntimeModel):
    name: str | None = Field(default=None, min_length=3, max_length=160)
    description: str | None = Field(default=None, max_length=2000)


class RuntimePipelineValidationIssue(StrictRuntimeModel):
    severity: Literal["error", "warning"]
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,119}$")
    message: str = Field(min_length=3, max_length=500)
    node_key: str | None = None
    edge_index: int | None = Field(default=None, ge=0)
    field: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimePipelineValidationReport(StrictRuntimeModel):
    status: Literal["valid", "invalid"]
    checked_at: str
    error_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    issues: list[RuntimePipelineValidationIssue] = Field(default_factory=list)


class RuntimePipelineValidationRequest(StrictRuntimeModel):
    mode: RuntimeMode
    graph: RuntimePipelineGraph | None = None
    authoring_graph: RuntimePipelineAuthoringGraph | None = None

    @model_validator(mode="after")
    def validate_graph_input(self) -> RuntimePipelineValidationRequest:
        if self.graph is None and self.authoring_graph is None:
            raise ValueError("graph or authoring_graph is required")
        return self


class RuntimePipelinePreviewRequest(StrictRuntimeModel):
    mode: RuntimeMode = RuntimeMode.REALTIME
    graph: RuntimePipelineGraph | None = None
    authoring_graph: RuntimePipelineAuthoringGraph | None = None
    preview_frame: RuntimeLiveFrame | None = None
    selected_node_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    )
    include_capture_outputs: bool = False

    @model_validator(mode="after")
    def validate_graph_input(self) -> RuntimePipelinePreviewRequest:
        if self.graph is None and self.authoring_graph is None:
            raise ValueError("graph or authoring_graph is required")
        return self


class RuntimePipelineValidationResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.pipeline.validation.v1"] = "runtime.pipeline.validation.v1"
    report: RuntimePipelineValidationReport
    normalized_graph: RuntimePipelineGraph | None = None
    output_catalog: RuntimePipelineOutputCatalog | None = None
    meta: ResponseMeta


class RuntimePipelineVersionSummary(StrictRuntimeModel):
    pipeline_version_id: str
    pipeline_definition_id: str
    version_no: int = Field(ge=1)
    status: RuntimePipelineStatus
    mode: RuntimeMode
    config_digest: str
    validation_status: Literal["valid", "invalid"] = "invalid"
    error_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    change_note: str | None = None
    created_at: str | None = None
    published_at: str | None = None


class RuntimePipelineVersionDetail(RuntimePipelineVersionSummary):
    graph: RuntimePipelineGraph
    validation_report: RuntimePipelineValidationReport
    model_capability_constraints: dict[str, Any] = Field(default_factory=dict)
    node_contract_versions: dict[str, int] = Field(default_factory=dict)


class RuntimePipelineDefinitionSummary(StrictRuntimeModel):
    pipeline_definition_id: str
    slug: str
    name: str
    description: str | None = None
    status: RuntimePipelineStatus
    latest_version_id: str | None = None
    latest_version_mode: RuntimeMode | None = None
    published_version_id: str | None = None
    published_version_mode: RuntimeMode | None = None
    template: bool = False
    scope: Literal["template", "channel_instance"] = "template"
    owner_channel_id: str | None = None
    source_template_definition_id: str | None = None
    version_count: int = Field(default=0, ge=0)


class RuntimePipelineDefinitionDetail(RuntimePipelineDefinitionSummary):
    versions: list[RuntimePipelineVersionSummary] = Field(default_factory=list)
    latest_version: RuntimePipelineVersionDetail | None = None


class RuntimePipelineDefinitionListResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.pipeline-definitions.list.v1"] = (
        "runtime.pipeline-definitions.list.v1"
    )
    items: list[RuntimePipelineDefinitionSummary] = Field(default_factory=list)
    meta: ResponseMeta


class RuntimePipelineTemplateInstantiateRequest(StrictRuntimeModel):
    name: str = Field(min_length=3, max_length=160)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    description: str | None = Field(default=None, max_length=2000)
    change_note: str | None = Field(default="Created from template", max_length=2000)


class RuntimeModelCapabilityOption(StrictRuntimeModel):
    deployment_release_id: str
    model_family_slug: str
    display_name: str
    target_path: TargetPath
    task_type: TaskType
    model_version_id: str
    version_label: str
    deployment_status: str
    architecture_family: str | None = None
    architecture_version: str | None = None
    architecture_size: str | None = None
    architecture_display_name: str | None = None
    contract_major: int = Field(ge=1)
    active: bool = True
    capability: dict[str, Any] = Field(default_factory=dict)


class RuntimeModelCapabilityListResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.model-capabilities.list.v1"] = (
        "runtime.model-capabilities.list.v1"
    )
    items: list[RuntimeModelCapabilityOption] = Field(default_factory=list)
    meta: ResponseMeta


RuntimeTriggerExecutionStatus = Literal["completed", "failed", "skipped", "timeout", "rejected"]


class RuntimeTriggerCameraImage(StrictRuntimeModel):
    image_base64: str = Field(min_length=16)
    mime_type: Literal["image/jpeg", "image/png"] = "image/jpeg"
    captured_at: str | None = None


class RuntimeBoardFormat(StrictRuntimeModel):
    format_id: str = Field(min_length=1, max_length=120)
    format_version: str = Field(min_length=1, max_length=120)
    width_mm: float = Field(gt=0, le=10_000)
    thickness_mm: float = Field(gt=0, le=2_000)


class RuntimeTriggerExecutionRequest(StrictRuntimeModel):
    trigger_id: str | None = Field(default=None, min_length=1, max_length=120)
    image_base64: str | None = Field(default=None, min_length=16)
    mime_type: str = "image/jpeg"
    images_by_role: dict[Literal["upper", "lower", "end_face"], RuntimeTriggerCameraImage] = Field(
        default_factory=dict
    )
    timeout_ms: int | None = Field(default=None, ge=50, le=30_000)
    board_format: RuntimeBoardFormat | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RuntimeTriggerExecutionResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.trigger.execution-response.v1"] = (
        "runtime.trigger.execution-response.v1"
    )
    status: RuntimeTriggerExecutionStatus
    trigger_endpoint_id: str | None = None
    endpoint_slug: str | None = None
    channel_id: str | None = None
    pipeline_version_id: str | None = None
    runtime_execution_id: str | None = None
    trigger_id: str | None = None
    correlation_id: str | None = None
    final_response: dict[str, Any] = Field(default_factory=dict)
    execution_result: dict[str, Any] = Field(default_factory=dict)
    overlay_outputs: list[dict[str, Any]] = Field(default_factory=list)
    capture_intents: list[dict[str, Any]] = Field(default_factory=list)
    http_trigger_responses: list[dict[str, Any]] = Field(default_factory=list)
    persisted_results: list[dict[str, Any]] = Field(default_factory=list)
    plc_write_outputs: list[dict[str, Any]] = Field(default_factory=list)
    projected_results: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    timing: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    meta: ResponseMeta


RuntimeResultStatus = Literal["completed", "failed", "timeout"]


class RuntimeResultRecordSummary(StrictRuntimeModel):
    result_record_id: str
    channel_id: str
    channel_name: str | None = None
    technical_place: str | None = None
    pipeline_version_id: str | None = None
    runtime_execution_id: str | None = None
    trigger_endpoint_id: str | None = None
    correlation_id: str
    source_timestamp: str | None = None
    completed_at: str
    mode: RuntimeMode = RuntimeMode.REALTIME
    status: RuntimeResultStatus
    result_type: str = Field(default="pipeline_execution", max_length=64)
    schema_version: str
    result: dict[str, Any] = Field(default_factory=dict)
    trigger_response: dict[str, Any] = Field(default_factory=dict)
    derived_preview: dict[str, Any] = Field(default_factory=dict)
    inference_output_count: int = Field(default=0, ge=0)
    derived_output_count: int = Field(default=0, ge=0)
    overlay_primitive_count: int = Field(default=0, ge=0)
    timing: dict[str, Any] = Field(default_factory=dict)
    model_routes: list[dict[str, Any]] = Field(default_factory=list)


class RuntimeResultRecordDetail(RuntimeResultRecordSummary):
    raw_inference_outputs: list[dict[str, Any]] = Field(default_factory=list)
    normalized_inference_outputs: list[dict[str, Any]] = Field(default_factory=list)
    derived_outputs: list[dict[str, Any]] = Field(default_factory=list)
    overlay_primitives: list[dict[str, Any]] = Field(default_factory=list)
    runtime_metadata: dict[str, Any] = Field(default_factory=dict)
    execution_result: dict[str, Any] = Field(default_factory=dict)


class RuntimeResultListResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.results.list.v1"] = "runtime.results.list.v1"
    items: list[RuntimeResultRecordSummary] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)
    meta: ResponseMeta


class RuntimeResultDetailResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.results.detail.v1"] = "runtime.results.detail.v1"
    item: RuntimeResultRecordDetail
    meta: ResponseMeta


StemReviewPlausibility = Literal["yes", "no", "unclear"]
StemReviewDirection = Literal["correct", "incorrect", "unclear"]
StemReviewAngle = Literal["too_small", "appropriate", "too_large", "not_assessable"]
StemReviewBowRelevance = Literal["relevant", "nearly_straight", "unclear"]
StemReviewFinalPosition = Literal["good", "acceptable", "bad", "not_assessed"]


class RuntimeStemOrientationReviewCreateRequest(StrictRuntimeModel):
    proposal_plausibility: StemReviewPlausibility
    direction_assessment: StemReviewDirection
    angle_assessment: StemReviewAngle
    bow_relevance: StemReviewBowRelevance
    final_position: StemReviewFinalPosition
    target_frame_start: int | None = Field(default=None, ge=0)
    target_frame_end: int | None = Field(default=None, ge=0)
    comment: str = Field(default="", max_length=4_000)
    special_case: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_target_frame_interval(self) -> RuntimeStemOrientationReviewCreateRequest:
        if self.target_frame_end is not None and self.target_frame_start is None:
            raise ValueError("target_frame_start is required when target_frame_end is set")
        if self.target_frame_start is not None and self.target_frame_end is None:
            self.target_frame_end = self.target_frame_start
        if (
            self.target_frame_start is not None
            and self.target_frame_end is not None
            and self.target_frame_end < self.target_frame_start
        ):
            raise ValueError("target_frame_end must be greater than or equal to target_frame_start")
        return self


class RuntimeStemOrientationReview(StrictRuntimeModel):
    schema_version: Literal["stem.orientation.review.v1"] = "stem.orientation.review.v1"
    review_id: str
    result_record_id: str
    revision: int = Field(ge=1)
    proposal_plausibility: StemReviewPlausibility
    direction_assessment: StemReviewDirection
    angle_assessment: StemReviewAngle
    bow_relevance: StemReviewBowRelevance
    final_position: StemReviewFinalPosition
    target_frame_start: int | None = Field(default=None, ge=0)
    target_frame_end: int | None = Field(default=None, ge=0)
    comment: str = ""
    special_case: str = ""
    supersedes_review_id: str | None = None
    reviewer_user_id: str
    reviewer_username: str
    created_at: str


class RuntimeStemOrientationReviewListResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.stem-orientation-reviews.list.v1"] = (
        "runtime.stem-orientation-reviews.list.v1"
    )
    result_record_id: str
    items: list[RuntimeStemOrientationReview] = Field(default_factory=list)
    latest: RuntimeStemOrientationReview | None = None
    meta: ResponseMeta


class RuntimeStemOrientationReviewResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.stem-orientation-reviews.detail.v1"] = (
        "runtime.stem-orientation-reviews.detail.v1"
    )
    item: RuntimeStemOrientationReview
    meta: ResponseMeta


class RuntimeStemShadowMetricCount(StrictRuntimeModel):
    key: str
    count: int = Field(ge=0)
    share: float = Field(default=0, ge=0, le=1)


class RuntimeStemShadowHistogramBin(StrictRuntimeModel):
    lower_bound: float
    upper_bound: float
    count: int = Field(ge=0)


class RuntimeStemShadowLatencySummary(StrictRuntimeModel):
    measured_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    minimum_ms: float | None = Field(default=None, ge=0)
    median_ms: float | None = Field(default=None, ge=0)
    p90_ms: float | None = Field(default=None, ge=0)
    p95_ms: float | None = Field(default=None, ge=0)
    maximum_ms: float | None = Field(default=None, ge=0)


class RuntimeStemShadowDriftSummary(StrictRuntimeModel):
    status: Literal["NO_DRIFT_DETECTED", "DRIFT_SIGNAL", "INSUFFICIENT_DATA"]
    baseline_count: int = Field(default=0, ge=0)
    recent_count: int = Field(default=0, ge=0)
    baseline_mean_confidence: float | None = Field(default=None, ge=0, le=1)
    recent_mean_confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_delta: float | None = None
    baseline_uncertain_share: float | None = Field(default=None, ge=0, le=1)
    recent_uncertain_share: float | None = Field(default=None, ge=0, le=1)
    uncertain_share_delta: float | None = None
    process_calibration_hashes: list[RuntimeStemShadowMetricCount] = Field(
        default_factory=list
    )
    rotation_calibration_hashes: list[RuntimeStemShadowMetricCount] = Field(
        default_factory=list
    )
    process_calibration_change_count: int = Field(default=0, ge=0)
    rotation_calibration_change_count: int = Field(default=0, ge=0)
    drift_reason_codes: list[RuntimeStemShadowMetricCount] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)


class RuntimeStemShadowFeedbackSummary(StrictRuntimeModel):
    reviewed_session_count: int = Field(default=0, ge=0)
    ratings: list[RuntimeStemShadowMetricCount] = Field(default_factory=list)
    technical_error_reasons: list[RuntimeStemShadowMetricCount] = Field(
        default_factory=list
    )


class RuntimeStemShadowEvaluationResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.stem-shadow-evaluation.v1"] = (
        "runtime.stem-shadow-evaluation.v1"
    )
    pipeline_version_id: str
    shadow_mode_enabled: bool
    evaluation_status: Literal["ACTIVE", "INACTIVE", "NO_SESSIONS"]
    sessions_available: int = Field(default=0, ge=0)
    sessions_analyzed: int = Field(default=0, ge=0)
    excluded_session_count: int = Field(default=0, ge=0)
    maximum_sessions: int = Field(ge=1)
    limit_reached: bool = False
    completed_from: str | None = None
    completed_to: str | None = None
    decision_distribution: list[RuntimeStemShadowMetricCount] = Field(default_factory=list)
    no_rotation_share: float = Field(default=0, ge=0, le=1)
    uncertain_share: float = Field(default=0, ge=0, le=1)
    deadline_miss_share: float = Field(default=0, ge=0, le=1)
    angle_histogram: list[RuntimeStemShadowHistogramBin] = Field(default_factory=list)
    angle_missing_count: int = Field(default=0, ge=0)
    confidence_histogram: list[RuntimeStemShadowHistogramBin] = Field(default_factory=list)
    confidence_percentiles: dict[str, float | None] = Field(default_factory=dict)
    reason_code_distribution: list[RuntimeStemShadowMetricCount] = Field(
        default_factory=list
    )
    decision_latency: RuntimeStemShadowLatencySummary
    drift: RuntimeStemShadowDriftSummary
    feedback: RuntimeStemShadowFeedbackSummary
    report_json: dict[str, Any] = Field(default_factory=dict)
    report_markdown: str = ""
    meta: ResponseMeta


class RuntimeLiveFrame(StrictRuntimeModel):
    image_base64: str | None = None
    mime_type: str = "image/jpeg"
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    source_timestamp: str | None = None
    captured_at: str | None = None
    sequence: int = Field(default=0, ge=0)
    image_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeCameraCalibrationRoi(StrictRuntimeModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(ge=0)
    height: int = Field(ge=0)


class RuntimeCameraCalibrationBoardDefinition(StrictRuntimeModel):
    board_type: Literal["checkerboard", "charuco"] = "checkerboard"
    columns: int = Field(default=9, ge=3, le=64)
    rows: int = Field(default=6, ge=3, le=64)
    square_length: float = Field(default=1.0, gt=0)
    marker_length: float | None = Field(default=None, gt=0)
    dictionary_name: str = Field(default="DICT_4X4_50", min_length=3, max_length=80)

    @model_validator(mode="after")
    def validate_charuco_definition(self) -> RuntimeCameraCalibrationBoardDefinition:
        if self.board_type == "charuco":
            if self.marker_length is None:
                raise ValueError("marker_length is required for a ChArUco board")
            if self.marker_length >= self.square_length:
                raise ValueError("marker_length must be smaller than square_length")
        return self


class RuntimeCameraIntrinsicCalibrationProfile(StrictRuntimeModel):
    schema_version: Literal["camera.intrinsic-calibration.v1"] = (
        "camera.intrinsic-calibration.v1"
    )
    status: Literal["valid", "invalid"]
    source_channel_id: str = Field(min_length=3, max_length=64)
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    distortion_model: Literal["opencv_standard"] = "opencv_standard"
    calibration_method: Literal[
        "CHECKERBOARD_OR_CHARUCO",
        "IDENTITY_PASSTHROUGH",
    ] = "CHECKERBOARD_OR_CHARUCO"
    camera_matrix: list[float]
    output_camera_matrix: list[float]
    distortion_coefficients: list[float] = Field(min_length=4, max_length=14)
    rms_reprojection_error_px: float = Field(ge=0)
    per_observation_reprojection_error_px: dict[str, float] = Field(default_factory=dict)
    accepted_observation_count: int = Field(ge=0)
    board_definition: RuntimeCameraCalibrationBoardDefinition
    valid_roi: RuntimeCameraCalibrationRoi
    generated_at: str = Field(min_length=10, max_length=64)
    calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    perspective_rectified: Literal[False] = False

    @model_validator(mode="after")
    def validate_matrix_shapes(self) -> RuntimeCameraIntrinsicCalibrationProfile:
        if len(self.camera_matrix) != 9:
            raise ValueError("camera_matrix must contain exactly 9 values")
        if len(self.output_camera_matrix) != 9:
            raise ValueError("output_camera_matrix must contain exactly 9 values")
        if any(value < 0 for value in self.per_observation_reprojection_error_px.values()):
            raise ValueError("per-observation reprojection errors must be non-negative")
        return self


class RuntimeNormalizedImagePoint(StrictRuntimeModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class RuntimeImageViewProfileProvenance(StrictRuntimeModel):
    method: Literal["MANUAL_QUADRILATERAL"] = "MANUAL_QUADRILATERAL"
    created_at: str = Field(min_length=10, max_length=64)
    operator_user_id: str | None = Field(default=None, max_length=120)
    source_frame_timestamp: str | None = Field(default=None, max_length=64)
    source_frame_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    pipeline_version_id: str | None = Field(default=None, max_length=120)
    notes: str = Field(default="", max_length=1000)


class RuntimeImageViewProfile(StrictRuntimeModel):
    """Versioned, reusable image-space contract after intrinsic undistortion."""

    schema_version: Literal["image.view-profile.v1"] = "image.view-profile.v1"
    view_profile_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,119}$")
    status: Literal["valid", "invalid"]
    source_space: Literal["UNDISTORTED_IMAGE"] = "UNDISTORTED_IMAGE"
    source_channel_id: str = Field(min_length=3, max_length=64)
    source_image_width: int = Field(ge=32, le=16384)
    source_image_height: int = Field(ge=32, le=16384)
    intrinsic_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_quadrilateral: list[RuntimeNormalizedImagePoint] = Field(min_length=4, max_length=4)
    output_width: int = Field(ge=32, le=8192)
    output_height: int = Field(ge=32, le=8192)
    transform_type: Literal[
        "PERSPECTIVE_RECTIFY",
        "POLYGON_MASKED_CROP",
    ] = "PERSPECTIVE_RECTIFY"
    interpolation: Literal["NEAREST", "LINEAR", "CUBIC"] = "LINEAR"
    padding_mode: Literal["CONSTANT"] = "CONSTANT"
    padding_value: int = Field(default=0, ge=0, le=255)
    enabled: bool = True
    source_to_view_homography: list[float]
    view_to_source_homography: list[float]
    profile_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    validation_status: Literal["VALID", "INVALID"]
    validated_at: str = Field(min_length=10, max_length=64)
    provenance: RuntimeImageViewProfileProvenance
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_matrices_and_status(self) -> RuntimeImageViewProfile:
        if len(self.source_to_view_homography) != 9:
            raise ValueError("source_to_view_homography must contain exactly 9 values")
        if len(self.view_to_source_homography) != 9:
            raise ValueError("view_to_source_homography must contain exactly 9 values")
        if (self.status == "valid") != (self.validation_status == "VALID"):
            raise ValueError("image view validation status is inconsistent")
        if self.output_width * self.output_height > 16_777_216:
            raise ValueError("image view output exceeds the supported pixel count")
        return self


class RuntimeImageViewProfileCalculateRequest(StrictRuntimeModel):
    view_profile_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,119}$")
    source_image_width: int = Field(ge=32, le=16384)
    source_image_height: int = Field(ge=32, le=16384)
    intrinsic_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_quadrilateral: list[RuntimeNormalizedImagePoint] = Field(min_length=4, max_length=4)
    output_width: int = Field(ge=32, le=8192)
    output_height: int = Field(ge=32, le=8192)
    transform_type: Literal[
        "PERSPECTIVE_RECTIFY",
        "POLYGON_MASKED_CROP",
    ] = "POLYGON_MASKED_CROP"
    interpolation: Literal["NEAREST", "LINEAR", "CUBIC"] = "LINEAR"
    padding_mode: Literal["CONSTANT"] = "CONSTANT"
    padding_value: int = Field(default=0, ge=0, le=255)
    enabled: bool = True
    source_frame_timestamp: str | None = Field(default=None, max_length=64)
    source_frame_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    pipeline_version_id: str | None = Field(default=None, max_length=120)
    notes: str = Field(default="", max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_output_pixel_count(self) -> RuntimeImageViewProfileCalculateRequest:
        if self.output_width * self.output_height > 16_777_216:
            raise ValueError("image view output exceeds the supported pixel count")
        return self


class RuntimeImageViewProfileCalculateResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.image-view-profile.calculate.v1"] = (
        "runtime.image-view-profile.calculate.v1"
    )
    status: Literal["valid", "invalid"]
    profile: RuntimeImageViewProfile | None = None
    failure_code: str | None = Field(default=None, max_length=120)
    failure_message: str | None = Field(default=None, max_length=500)
    meta: ResponseMeta


class RuntimeStemCurvatureImagePoint(StrictRuntimeModel):
    x_px: float = Field(ge=0)
    y_px: float = Field(ge=0)


class RuntimeStemCurvatureProcessPoint(StrictRuntimeModel):
    x_mm: float
    y_mm: float


class RuntimeStemCurvatureProcessReferencePoint(StrictRuntimeModel):
    point_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
    image_point: RuntimeStemCurvatureImagePoint
    process_point: RuntimeStemCurvatureProcessPoint


class RuntimeStemCurvatureImagePolygon(StrictRuntimeModel):
    points: list[RuntimeStemCurvatureImagePoint] = Field(min_length=3, max_length=64)


class RuntimeStemCurvatureProcessPolygon(StrictRuntimeModel):
    image_points: list[RuntimeStemCurvatureImagePoint] = Field(min_length=3, max_length=64)
    process_points: list[RuntimeStemCurvatureProcessPoint] = Field(min_length=3, max_length=64)

    @model_validator(mode="after")
    def validate_corresponding_points(self) -> RuntimeStemCurvatureProcessPolygon:
        if len(self.image_points) != len(self.process_points):
            raise ValueError("image_points and process_points must have equal length")
        return self


class RuntimeStemCurvatureImageGate(StrictRuntimeModel):
    start: RuntimeStemCurvatureImagePoint
    end: RuntimeStemCurvatureImagePoint


class RuntimeStemCurvatureProcessGate(StrictRuntimeModel):
    image_start: RuntimeStemCurvatureImagePoint
    image_end: RuntimeStemCurvatureImagePoint
    process_start: RuntimeStemCurvatureProcessPoint
    process_end: RuntimeStemCurvatureProcessPoint


class RuntimeStemCurvatureProcessBounds(StrictRuntimeModel):
    x_min_mm: float
    x_max_mm: float
    y_min_mm: float
    y_max_mm: float

    @model_validator(mode="after")
    def validate_non_empty_bounds(self) -> RuntimeStemCurvatureProcessBounds:
        if self.x_max_mm <= self.x_min_mm or self.y_max_mm <= self.y_min_mm:
            raise ValueError("process bounds must span positive X and Y ranges")
        return self


class RuntimeStemCurvatureCoordinateFrameDefinition(StrictRuntimeModel):
    method: Literal["ORTHOGONAL_XY_AXIS_ASSISTANT"] = "ORTHOGONAL_XY_AXIS_ASSISTANT"
    origin_image_point: RuntimeStemCurvatureImagePoint
    x_axis_image_endpoint: RuntimeStemCurvatureImagePoint
    x_length_mm: float = Field(gt=0)
    y_axis_image_endpoint: RuntimeStemCurvatureImagePoint
    y_length_mm: float = Field(gt=0)
    xy_corner_image_point: RuntimeStemCurvatureImagePoint
    z_axis_image_endpoint: RuntimeStemCurvatureImagePoint
    z_direction_semantics: Literal[
        "UP_FROM_REFERENCE_PLANE",
        "DOWN_TOWARD_MACHINE_BASE",
    ] = "UP_FROM_REFERENCE_PLANE"
    z_reference_length_mm: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_distinct_axis_points(
        self,
    ) -> RuntimeStemCurvatureCoordinateFrameDefinition:
        origin = self.origin_image_point
        for label, point in (
            ("X endpoint", self.x_axis_image_endpoint),
            ("Y endpoint", self.y_axis_image_endpoint),
            ("XY corner", self.xy_corner_image_point),
            ("Z direction", self.z_axis_image_endpoint),
        ):
            if math.hypot(point.x_px - origin.x_px, point.y_px - origin.y_px) <= 1e-6:
                raise ValueError(f"{label} must differ from the coordinate origin")
        return self


def _validate_coordinate_frame_reference_points(
    reference_points: list[RuntimeStemCurvatureProcessReferencePoint],
    definition: RuntimeStemCurvatureCoordinateFrameDefinition | None,
) -> None:
    if definition is None:
        return
    by_id = {point.point_id: point for point in reference_points}
    expected = {
        "axis-origin": (definition.origin_image_point, 0.0, 0.0),
        "axis-x-end": (definition.x_axis_image_endpoint, definition.x_length_mm, 0.0),
        "axis-y-end": (definition.y_axis_image_endpoint, 0.0, definition.y_length_mm),
        "axis-xy-corner": (
            definition.xy_corner_image_point,
            definition.x_length_mm,
            definition.y_length_mm,
        ),
    }
    for point_id, (image_point, x_mm, y_mm) in expected.items():
        point = by_id.get(point_id)
        if point is None:
            raise ValueError(f"coordinate frame requires reference point {point_id}")
        values = (
            (point.image_point.x_px, image_point.x_px),
            (point.image_point.y_px, image_point.y_px),
            (point.process_point.x_mm, x_mm),
            (point.process_point.y_mm, y_mm),
        )
        if any(not math.isclose(actual, target, abs_tol=1e-6) for actual, target in values):
            raise ValueError(f"reference point {point_id} conflicts with coordinate frame")


def _validate_stem_measurement_geometry_binding(
    mode: Literal["EXPLICIT_GEOMETRY_V1", "CANONICAL_VIEW_CORRIDOR_V1"],
    view_profile_id: str | None,
    view_profile_checksum: str | None,
) -> None:
    if mode == "CANONICAL_VIEW_CORRIDOR_V1":
        if view_profile_id is None or view_profile_checksum is None:
            raise ValueError(
                "canonical-view corridor geometry requires its view profile ID and checksum"
            )
        return
    if view_profile_id is not None or view_profile_checksum is not None:
        raise ValueError(
            "canonical view binding is only valid for CANONICAL_VIEW_CORRIDOR_V1"
        )


class RuntimeStemCurvatureReferencePlane(StrictRuntimeModel):
    plane_id: str = Field(min_length=1, max_length=120)
    z_mm: float = 0.0
    z_positive_direction: Literal["TOWARD_CAMERA", "AWAY_FROM_CAMERA"]
    x_axis_description: str = Field(min_length=1, max_length=300)
    y_axis_description: str = Field(min_length=1, max_length=300)
    z_axis_description: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=500)


class RuntimeStemCurvatureProcessCalibrationProvenance(StrictRuntimeModel):
    method: Literal["MANUAL_REFERENCE_POINT_HOMOGRAPHY"] = (
        "MANUAL_REFERENCE_POINT_HOMOGRAPHY"
    )
    created_at: str = Field(min_length=10, max_length=64)
    operator_user_id: str | None = Field(default=None, max_length=120)
    reference_frame_timestamp: str | None = Field(default=None, max_length=64)
    reference_frame_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    notes: str = Field(default="", max_length=1000)


class RuntimeStemCurvatureProcessCalibrationProfile(StrictRuntimeModel):
    schema_version: Literal["stem.curvature-process-calibration.v1"] = (
        "stem.curvature-process-calibration.v1"
    )
    calibration_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,119}$")
    status: Literal["valid", "invalid"]
    source_channel_id: str = Field(min_length=3, max_length=64)
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    intrinsic_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    coordinate_unit: Literal["mm"] = "mm"
    reference_points: list[RuntimeStemCurvatureProcessReferencePoint] = Field(
        min_length=4,
        max_length=64,
    )
    coordinate_frame_definition: RuntimeStemCurvatureCoordinateFrameDefinition | None = None
    measurement_geometry_mode: Literal[
        "EXPLICIT_GEOMETRY_V1",
        "CANONICAL_VIEW_CORRIDOR_V1",
    ] = "EXPLICIT_GEOMETRY_V1"
    canonical_view_profile_id: str | None = Field(default=None, min_length=3, max_length=128)
    canonical_view_profile_checksum: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    homography_image_to_process: list[float]
    homography_process_to_image: list[float]
    process_bounds: RuntimeStemCurvatureProcessBounds
    reference_plane: RuntimeStemCurvatureReferencePlane
    candidate_roi: RuntimeStemCurvatureProcessPolygon
    curvature_observation_roi: RuntimeStemCurvatureProcessPolygon
    surface_tracking_roi: RuntimeStemCurvatureProcessPolygon
    session_start_gate: RuntimeStemCurvatureProcessGate
    curvature_freeze_gate: RuntimeStemCurvatureProcessGate
    decision_gate: RuntimeStemCurvatureProcessGate
    decision_margin_mm: float = Field(ge=0)
    conveyor_direction: Literal["POSITIVE_X", "NEGATIVE_X"]
    rotation_reference_view: Literal[
        "LOOKING_IN_CONVEYOR_DIRECTION",
        "LOOKING_AGAINST_CONVEYOR_DIRECTION",
    ]
    positive_rotation: Literal["CLOCKWISE", "COUNTER_CLOCKWISE"]
    diameter_class: str | None = Field(default=None, max_length=120)
    height_class: str | None = Field(default=None, max_length=120)
    rms_reprojection_error_px: float = Field(ge=0)
    maximum_reprojection_error_px: float = Field(ge=0)
    maximum_allowed_reprojection_error_px: float = Field(gt=0)
    provenance: RuntimeStemCurvatureProcessCalibrationProvenance
    calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    perspective_rectified: Literal[True] = True

    @model_validator(mode="after")
    def validate_matrix_shapes_and_references(
        self,
    ) -> RuntimeStemCurvatureProcessCalibrationProfile:
        if len(self.homography_image_to_process) != 9:
            raise ValueError("homography_image_to_process must contain exactly 9 values")
        if len(self.homography_process_to_image) != 9:
            raise ValueError("homography_process_to_image must contain exactly 9 values")
        point_ids = [item.point_id for item in self.reference_points]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("reference point IDs must be unique")
        _validate_coordinate_frame_reference_points(
            self.reference_points,
            self.coordinate_frame_definition,
        )
        _validate_stem_measurement_geometry_binding(
            self.measurement_geometry_mode,
            self.canonical_view_profile_id,
            self.canonical_view_profile_checksum,
        )
        return self


class RuntimeStemCurvatureProcessCalibrationCalculateRequest(StrictRuntimeModel):
    calibration_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,119}$")
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    intrinsic_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reference_points: list[RuntimeStemCurvatureProcessReferencePoint] = Field(
        min_length=4,
        max_length=64,
    )
    coordinate_frame_definition: RuntimeStemCurvatureCoordinateFrameDefinition | None = None
    measurement_geometry_mode: Literal[
        "EXPLICIT_GEOMETRY_V1",
        "CANONICAL_VIEW_CORRIDOR_V1",
    ] = "EXPLICIT_GEOMETRY_V1"
    canonical_view_profile_id: str | None = Field(default=None, min_length=3, max_length=128)
    canonical_view_profile_checksum: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    reference_plane: RuntimeStemCurvatureReferencePlane
    candidate_roi: RuntimeStemCurvatureImagePolygon
    curvature_observation_roi: RuntimeStemCurvatureImagePolygon
    surface_tracking_roi: RuntimeStemCurvatureImagePolygon
    session_start_gate: RuntimeStemCurvatureImageGate
    curvature_freeze_gate: RuntimeStemCurvatureImageGate
    decision_gate: RuntimeStemCurvatureImageGate
    decision_margin_mm: float = Field(default=100.0, ge=0)
    conveyor_direction: Literal["POSITIVE_X", "NEGATIVE_X"]
    rotation_reference_view: Literal[
        "LOOKING_IN_CONVEYOR_DIRECTION",
        "LOOKING_AGAINST_CONVEYOR_DIRECTION",
    ]
    positive_rotation: Literal["CLOCKWISE", "COUNTER_CLOCKWISE"]
    diameter_class: str | None = Field(default=None, max_length=120)
    height_class: str | None = Field(default=None, max_length=120)
    maximum_rms_reprojection_error_px: float = Field(default=2.0, gt=0, le=50)
    reference_frame_timestamp: str | None = Field(default=None, max_length=64)
    reference_frame_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    notes: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_reference_ids(
        self,
    ) -> RuntimeStemCurvatureProcessCalibrationCalculateRequest:
        point_ids = [item.point_id for item in self.reference_points]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("reference point IDs must be unique")
        _validate_coordinate_frame_reference_points(
            self.reference_points,
            self.coordinate_frame_definition,
        )
        _validate_stem_measurement_geometry_binding(
            self.measurement_geometry_mode,
            self.canonical_view_profile_id,
            self.canonical_view_profile_checksum,
        )
        return self


class RuntimeStemCurvatureProcessCalibrationCalculateResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.stem-process-calibration.calculate.v1"] = (
        "runtime.stem-process-calibration.calculate.v1"
    )
    status: Literal["valid", "invalid"]
    profile: RuntimeStemCurvatureProcessCalibrationProfile | None = None
    rms_reprojection_error_px: float | None = Field(default=None, ge=0)
    maximum_reprojection_error_px: float | None = Field(default=None, ge=0)
    failure_code: str | None = Field(default=None, max_length=120)
    failure_message: str | None = Field(default=None, max_length=500)
    meta: ResponseMeta


class RuntimeStemSurfacePhaseCalibrationObservation(StrictRuntimeModel):
    observation_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,79}$")
    raw_phase: float
    known_angle_deg: float
    actual_marking: str = Field(default="", max_length=160)
    diameter_mm: float = Field(gt=0)
    image_center_x_px: float = Field(ge=0)
    image_center_y_px: float = Field(ge=0)
    weight: float = Field(default=1.0, gt=0, le=100)
    calculated_phase_deg: float | None = None
    error_deg: float | None = Field(default=None, ge=0)
    accepted: bool | None = None


class RuntimeStemSurfacePhaseCalibrationProvenance(StrictRuntimeModel):
    method: Literal["MARKED_TEST_STEM", "REFERENCE_CYLINDER"]
    created_at: str = Field(min_length=10, max_length=64)
    operator_user_id: str | None = Field(default=None, max_length=120)
    reference_asset_id: str | None = Field(default=None, max_length=120)
    notes: str = Field(default="", max_length=1000)


class RuntimeStemSurfacePhaseCalibrationProfile(StrictRuntimeModel):
    schema_version: Literal["stem.surface-phase-calibration.v1"] = (
        "stem.surface-phase-calibration.v1"
    )
    calibration_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,119}$")
    calibration_version: str = Field(min_length=1, max_length=64)
    status: Literal["valid", "invalid"]
    source_channel_id: str = Field(min_length=3, max_length=64)
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    intrinsic_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    process_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    rotation_reference_view: Literal[
        "LOOKING_IN_CONVEYOR_DIRECTION",
        "LOOKING_AGAINST_CONVEYOR_DIRECTION",
    ]
    positive_rotation: Literal["CLOCKWISE", "COUNTER_CLOCKWISE"]
    phase_estimator_version: Literal[
        "LK_KEYFRAME_RELOCK_V1",
        "CYLINDRICAL_SURFACE_V2",
    ] = "LK_KEYFRAME_RELOCK_V1"
    fit_model: Literal["LINEAR_THROUGH_ORIGIN"] = "LINEAR_THROUGH_ORIGIN"
    degrees_per_raw_phase: float | None = None
    observations: list[RuntimeStemSurfacePhaseCalibrationObservation] = Field(
        min_length=1,
        max_length=200,
    )
    accepted_observation_count: int = Field(ge=0)
    mean_abs_error_deg: float | None = Field(default=None, ge=0)
    rms_error_deg: float | None = Field(default=None, ge=0)
    maximum_abs_error_deg: float | None = Field(default=None, ge=0)
    maximum_mean_abs_error_deg: float = Field(gt=0)
    maximum_abs_error_limit_deg: float = Field(gt=0)
    requested_valid_diameter_min_mm: float | None = Field(default=None, gt=0)
    requested_valid_diameter_max_mm: float | None = Field(default=None, gt=0)
    diameter_tolerance_ratio: float = Field(default=0.25, ge=0, le=2)
    image_center_tolerance_x_ratio: float = Field(default=0.20, ge=0, le=1)
    image_center_tolerance_y_ratio: float = Field(default=0.20, ge=0, le=1)
    valid_diameter_min_mm: float = Field(gt=0)
    valid_diameter_max_mm: float = Field(gt=0)
    valid_image_center_x_min_px: float = Field(ge=0)
    valid_image_center_x_max_px: float = Field(ge=0)
    valid_image_center_y_min_px: float = Field(ge=0)
    valid_image_center_y_max_px: float = Field(ge=0)
    provenance: RuntimeStemSurfacePhaseCalibrationProvenance
    calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_ranges(self) -> RuntimeStemSurfacePhaseCalibrationProfile:
        if (self.requested_valid_diameter_min_mm is None) != (
            self.requested_valid_diameter_max_mm is None
        ):
            raise ValueError("requested valid diameter range must be complete")
        if (
            self.requested_valid_diameter_min_mm is not None
            and self.requested_valid_diameter_max_mm is not None
            and self.requested_valid_diameter_max_mm
            < self.requested_valid_diameter_min_mm
        ):
            raise ValueError("requested valid diameter range is reversed")
        if self.valid_diameter_max_mm < self.valid_diameter_min_mm:
            raise ValueError("valid diameter range is reversed")
        if self.valid_image_center_x_max_px < self.valid_image_center_x_min_px:
            raise ValueError("valid image-center X range is reversed")
        if self.valid_image_center_y_max_px < self.valid_image_center_y_min_px:
            raise ValueError("valid image-center Y range is reversed")
        return self


class RuntimeStemSurfacePhaseCalibrationCalculateRequest(StrictRuntimeModel):
    calibration_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,119}$")
    calibration_version: str = Field(min_length=1, max_length=64)
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    intrinsic_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    process_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    rotation_reference_view: Literal[
        "LOOKING_IN_CONVEYOR_DIRECTION",
        "LOOKING_AGAINST_CONVEYOR_DIRECTION",
    ]
    positive_rotation: Literal["CLOCKWISE", "COUNTER_CLOCKWISE"]
    phase_estimator_version: Literal[
        "LK_KEYFRAME_RELOCK_V1",
        "CYLINDRICAL_SURFACE_V2",
    ] = "LK_KEYFRAME_RELOCK_V1"
    method: Literal["MARKED_TEST_STEM", "REFERENCE_CYLINDER"]
    observations: list[RuntimeStemSurfacePhaseCalibrationObservation] = Field(
        min_length=4,
        max_length=200,
    )
    minimum_angle_span_deg: float = Field(default=30.0, gt=0, le=720)
    maximum_mean_abs_error_deg: float = Field(default=3.0, gt=0, le=90)
    maximum_abs_error_deg: float = Field(default=7.5, gt=0, le=180)
    valid_diameter_min_mm: float | None = Field(default=None, gt=0)
    valid_diameter_max_mm: float | None = Field(default=None, gt=0)
    diameter_tolerance_ratio: float = Field(default=0.25, ge=0, le=2)
    image_center_tolerance_x_ratio: float = Field(default=0.20, ge=0, le=1)
    image_center_tolerance_y_ratio: float = Field(default=0.20, ge=0, le=1)
    operator_user_id: str | None = Field(default=None, max_length=120)
    reference_asset_id: str | None = Field(default=None, max_length=120)
    notes: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_observations(self) -> RuntimeStemSurfacePhaseCalibrationCalculateRequest:
        observation_ids = [item.observation_id for item in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("surface-phase observation IDs must be unique")
        for item in self.observations:
            if item.image_center_x_px >= self.image_width:
                raise ValueError("observation image_center_x_px lies outside image")
            if item.image_center_y_px >= self.image_height:
                raise ValueError("observation image_center_y_px lies outside image")
        if (self.valid_diameter_min_mm is None) != (
            self.valid_diameter_max_mm is None
        ):
            raise ValueError("both valid diameter limits must be provided together")
        if (
            self.valid_diameter_min_mm is not None
            and self.valid_diameter_max_mm is not None
        ):
            if self.valid_diameter_max_mm < self.valid_diameter_min_mm:
                raise ValueError("valid diameter range is reversed")
            observed_min = min(item.diameter_mm for item in self.observations)
            observed_max = max(item.diameter_mm for item in self.observations)
            if (
                observed_min < self.valid_diameter_min_mm
                or observed_max > self.valid_diameter_max_mm
            ):
                raise ValueError(
                    "configured valid diameter range must contain all calibration observations"
                )
        return self


class RuntimeStemSurfacePhaseCalibrationCalculateResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.stem-surface-phase-calibration.calculate.v1"] = (
        "runtime.stem-surface-phase-calibration.calculate.v1"
    )
    status: Literal["valid", "invalid"]
    profile: RuntimeStemSurfacePhaseCalibrationProfile | None = None
    mean_abs_error_deg: float | None = Field(default=None, ge=0)
    rms_error_deg: float | None = Field(default=None, ge=0)
    maximum_abs_error_deg: float | None = Field(default=None, ge=0)
    failure_code: str | None = Field(default=None, max_length=120)
    failure_message: str | None = Field(default=None, max_length=500)
    meta: ResponseMeta


class RuntimeStemMinimumPrincipleValidationObservation(StrictRuntimeModel):
    observation_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,79}$")
    reference_setup: Literal[
        "STRAIGHT_REFERENCE_CYLINDER",
        "CURVED_REFERENCE_BODY",
        "MARKED_TEST_STEM",
    ]
    reference_body_id: str = Field(min_length=1, max_length=120)
    run_id: str = Field(min_length=1, max_length=120)
    actual_marking: str = Field(min_length=1, max_length=160)
    known_angle_deg: float | None = None
    calculated_phase_deg: float | None = None
    expected_target_phase_deg: float | None = None
    calculated_target_phase_deg: float | None = None
    expected_correction_deg: float | None = Field(default=None, ge=-90, le=90)
    calculated_correction_deg: float | None = Field(default=None, ge=-90, le=90)
    decision: Literal["ROTATE", "NO_ROTATION", "UNCERTAIN"]
    observed_phase_span_deg: float = Field(default=0, ge=0, le=720)
    diameter_mm: float = Field(gt=0)
    diameter_class: str = Field(min_length=1, max_length=120)
    image_center_x_px: float = Field(ge=0)
    image_center_y_px: float = Field(ge=0)
    known_x_mm: float | None = None
    known_y_mm: float | None = None
    measured_x_mm: float | None = None
    measured_y_mm: float | None = None
    expected_curvature_min_mm: float | None = Field(default=None, ge=0)
    expected_curvature_max_mm: float | None = Field(default=None, ge=0)
    measured_curvature_min_mm: float | None = Field(default=None, ge=0)
    measured_curvature_max_mm: float | None = Field(default=None, ge=0)
    translation_distance_mm: float = Field(default=0, ge=0)
    relock_attempted: bool = False
    relock_succeeded: bool = False
    confidence: float = Field(default=0, ge=0, le=1)
    reason_codes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_paired_measurements(
        self,
    ) -> RuntimeStemMinimumPrincipleValidationObservation:
        pairs = (
            (self.known_angle_deg, self.calculated_phase_deg, "phase angles"),
            (
                self.expected_target_phase_deg,
                self.calculated_target_phase_deg,
                "target phases",
            ),
            (
                self.expected_correction_deg,
                self.calculated_correction_deg,
                "correction angles",
            ),
            (self.known_x_mm, self.measured_x_mm, "X coordinates"),
            (self.known_y_mm, self.measured_y_mm, "Y coordinates"),
            (
                self.expected_curvature_min_mm,
                self.measured_curvature_min_mm,
                "minimum curvature",
            ),
            (
                self.expected_curvature_max_mm,
                self.measured_curvature_max_mm,
                "maximum curvature",
            ),
        )
        for expected, measured, label in pairs:
            if (expected is None) != (measured is None):
                raise ValueError(f"{label} must be supplied as a complete pair")
        if self.relock_succeeded and not self.relock_attempted:
            raise ValueError("a successful Re-Lock requires relock_attempted=true")
        if self.decision == "ROTATE" and self.calculated_correction_deg is None:
            raise ValueError("ROTATE observations require a calculated correction angle")
        if self.decision == "UNCERTAIN" and self.calculated_correction_deg is not None:
            raise ValueError("UNCERTAIN observations must not publish a correction angle")
        if (
            self.expected_curvature_min_mm is not None
            and self.expected_curvature_max_mm is not None
            and self.expected_curvature_max_mm < self.expected_curvature_min_mm
        ):
            raise ValueError("expected maximum curvature must not be below the minimum")
        if (
            self.measured_curvature_min_mm is not None
            and self.measured_curvature_max_mm is not None
            and self.measured_curvature_max_mm < self.measured_curvature_min_mm
        ):
            raise ValueError("measured maximum curvature must not be below the minimum")
        return self


class RuntimeStemMinimumPrincipleValidationThresholds(StrictRuntimeModel):
    minimum_total_observations: int = Field(ge=3, le=10000)
    minimum_observations_per_setup: int = Field(ge=1, le=1000)
    minimum_valid_angle_observations: int = Field(ge=1, le=10000)
    maximum_median_angle_error_deg: float = Field(gt=0, le=90)
    maximum_p95_angle_error_deg: float = Field(gt=0, le=180)
    maximum_sign_error_count: int = Field(ge=0, le=10000)
    maximum_target_phase_repeatability_deg: float = Field(gt=0, le=90)
    maximum_homography_error_mm: float = Field(gt=0)
    maximum_straight_curvature_mm: float = Field(gt=0)
    maximum_translation_phase_error_deg: float = Field(gt=0, le=180)
    maximum_curvature_abs_error_mm: float = Field(gt=0)
    maximum_curvature_repeatability_mm: float = Field(gt=0)
    minimum_relock_success_rate: float = Field(ge=0, le=1)
    maximum_uncertainty_rate: float = Field(ge=0, le=1)


class RuntimeStemMinimumPrincipleValidationEvaluation(
    RuntimeStemMinimumPrincipleValidationObservation
):
    angle_error_deg: float | None = Field(default=None, ge=0)
    phase_error_deg_signed: float | None = None
    target_phase_error_deg_signed: float | None = None
    correction_error_deg_signed: float | None = None
    sign_error: bool = False
    homography_error_mm: float | None = Field(default=None, ge=0)
    curvature_min_error_mm: float | None = Field(default=None, ge=0)
    curvature_max_error_mm: float | None = Field(default=None, ge=0)
    image_position_bucket: str
    phase_span_bucket: str


class RuntimeStemMinimumPrincipleValidationMetricGroup(StrictRuntimeModel):
    group_key: str
    observation_count: int = Field(ge=0)
    valid_angle_count: int = Field(ge=0)
    median_angle_error_deg: float | None = Field(default=None, ge=0)
    p90_angle_error_deg: float | None = Field(default=None, ge=0)
    p95_angle_error_deg: float | None = Field(default=None, ge=0)
    uncertainty_rate: float = Field(ge=0, le=1)


class RuntimeStemMinimumPrincipleValidationMetrics(StrictRuntimeModel):
    total_observation_count: int = Field(ge=0)
    valid_angle_observation_count: int = Field(ge=0)
    median_angle_error_deg: float | None = Field(default=None, ge=0)
    p90_angle_error_deg: float | None = Field(default=None, ge=0)
    p95_angle_error_deg: float | None = Field(default=None, ge=0)
    sign_error_count: int = Field(ge=0)
    sign_error_rate: float = Field(ge=0, le=1)
    homography_error_median_mm: float | None = Field(default=None, ge=0)
    homography_error_p95_mm: float | None = Field(default=None, ge=0)
    straight_curvature_p95_mm: float | None = Field(default=None, ge=0)
    translation_phase_error_p95_deg: float | None = Field(default=None, ge=0)
    curvature_min_abs_error_p95_mm: float | None = Field(default=None, ge=0)
    curvature_max_abs_error_p95_mm: float | None = Field(default=None, ge=0)
    curvature_min_repeatability_p95_mm: float | None = Field(default=None, ge=0)
    curvature_max_repeatability_p95_mm: float | None = Field(default=None, ge=0)
    target_phase_repeatability_p95_deg: float | None = Field(default=None, ge=0)
    relock_attempt_count: int = Field(ge=0)
    relock_success_count: int = Field(ge=0)
    relock_success_rate: float | None = Field(default=None, ge=0, le=1)
    uncertainty_count: int = Field(ge=0)
    uncertainty_rate: float = Field(ge=0, le=1)
    by_phase_span: list[RuntimeStemMinimumPrincipleValidationMetricGroup] = Field(
        default_factory=list
    )
    by_diameter: list[RuntimeStemMinimumPrincipleValidationMetricGroup] = Field(
        default_factory=list
    )
    by_image_position: list[RuntimeStemMinimumPrincipleValidationMetricGroup] = Field(
        default_factory=list
    )


class RuntimeStemMinimumPrincipleValidationCheck(StrictRuntimeModel):
    check_id: str
    status: Literal["PASS", "FAIL", "INSUFFICIENT_DATA"]
    sample_count: int = Field(default=0, ge=0)
    value: float | int | str | None = None
    comparison: Literal["<=", ">=", "==", "present"]
    limit: float | int | str | None = None
    message: str


class RuntimeStemMinimumPrincipleValidationProvenance(StrictRuntimeModel):
    method: Literal["OBJECTIVE_REFERENCE_BODY_VALIDATION"] = (
        "OBJECTIVE_REFERENCE_BODY_VALIDATION"
    )
    created_at: str = Field(min_length=10, max_length=64)
    operator_user_id: str | None = Field(default=None, max_length=120)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=200)
    notes: str = Field(default="", max_length=2000)


class RuntimeStemMinimumPrincipleValidationArtifact(StrictRuntimeModel):
    schema_version: Literal["stem.minimum-principle-validation.v1"] = (
        "stem.minimum-principle-validation.v1"
    )
    artifact_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,119}$")
    artifact_version: str = Field(min_length=1, max_length=64)
    status: Literal["VALIDATION_PASSED", "VALIDATION_FAILED", "INSUFFICIENT_DATA"]
    source_channel_id: str = Field(min_length=3, max_length=64)
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    process_calibration_id: str
    process_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    phase_calibration_id: str
    phase_calibration_version: str
    phase_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    algorithm: Literal["CURVATURE_PHASE_MINIMUM"] = "CURVATURE_PHASE_MINIMUM"
    algorithm_version: str
    thresholds: RuntimeStemMinimumPrincipleValidationThresholds
    observations: list[RuntimeStemMinimumPrincipleValidationEvaluation] = Field(
        min_length=1, max_length=10000
    )
    metrics: RuntimeStemMinimumPrincipleValidationMetrics
    checks: list[RuntimeStemMinimumPrincipleValidationCheck] = Field(
        min_length=1, max_length=100
    )
    reason_codes: list[str] = Field(default_factory=list, max_length=200)
    provenance: RuntimeStemMinimumPrincipleValidationProvenance
    report_markdown: str
    report_html: str
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class RuntimeStemMinimumPrincipleValidationCalculateRequest(StrictRuntimeModel):
    artifact_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,119}$")
    artifact_version: str = Field(min_length=1, max_length=64)
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    process_calibration_id: str
    process_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    phase_calibration_id: str
    phase_calibration_version: str
    phase_calibration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    thresholds_confirmed: Literal[True]
    thresholds: RuntimeStemMinimumPrincipleValidationThresholds
    observations: list[RuntimeStemMinimumPrincipleValidationObservation] = Field(
        min_length=1, max_length=10000
    )
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=200)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_observations(
        self,
    ) -> RuntimeStemMinimumPrincipleValidationCalculateRequest:
        observation_ids = [item.observation_id for item in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("minimum-principle validation observation IDs must be unique")
        for item in self.observations:
            if item.image_center_x_px >= self.image_width:
                raise ValueError("validation image_center_x_px lies outside image")
            if item.image_center_y_px >= self.image_height:
                raise ValueError("validation image_center_y_px lies outside image")
        return self


class RuntimeStemMinimumPrincipleValidationCalculateResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.stem-minimum-principle-validation.calculate.v1"] = (
        "runtime.stem-minimum-principle-validation.calculate.v1"
    )
    status: Literal["VALIDATION_PASSED", "VALIDATION_FAILED", "INSUFFICIENT_DATA"]
    artifact: RuntimeStemMinimumPrincipleValidationArtifact
    report_json: dict[str, Any]
    report_markdown: str
    report_html: str
    meta: ResponseMeta


class RuntimeCameraCalibrationObservation(StrictRuntimeModel):
    observation_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,79}$")
    frame: RuntimeLiveFrame


class RuntimeCameraCalibrationObservationResult(StrictRuntimeModel):
    observation_id: str
    accepted: bool
    detected_point_count: int = Field(default=0, ge=0)
    reprojection_error_px: float | None = Field(default=None, ge=0)
    reason_code: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=500)


class RuntimeCameraCalibrationCalculateRequest(StrictRuntimeModel):
    board_definition: RuntimeCameraCalibrationBoardDefinition
    observations: list[RuntimeCameraCalibrationObservation] = Field(
        default_factory=list,
        max_length=50,
    )
    calibration_method: Literal[
        "CHECKERBOARD_OR_CHARUCO",
        "IDENTITY_PASSTHROUGH",
    ] = "CHECKERBOARD_OR_CHARUCO"
    image_width: int | None = Field(default=None, ge=1)
    image_height: int | None = Field(default=None, ge=1)
    minimum_observation_count: int = Field(default=5, ge=3, le=50)
    maximum_rms_reprojection_error_px: float = Field(default=2.0, gt=0, le=20)
    preview_observation_id: str | None = None

    @model_validator(mode="after")
    def validate_observation_set(self) -> RuntimeCameraCalibrationCalculateRequest:
        if self.calibration_method == "IDENTITY_PASSTHROUGH":
            if self.image_width is None or self.image_height is None:
                raise ValueError(
                    "image_width and image_height are required for identity passthrough"
                )
            if self.observations:
                raise ValueError("identity passthrough does not accept board observations")
            if self.preview_observation_id is not None:
                raise ValueError(
                    "identity passthrough does not accept preview_observation_id"
                )
            return self
        if not self.observations:
            raise ValueError("board calibration requires at least one observation")
        observation_ids = [item.observation_id for item in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("observation_id values must be unique")
        if self.minimum_observation_count > len(self.observations):
            raise ValueError("minimum_observation_count exceeds the supplied observations")
        if (
            self.preview_observation_id is not None
            and self.preview_observation_id not in observation_ids
        ):
            raise ValueError("preview_observation_id must reference a supplied observation")
        return self


class RuntimeCameraCalibrationCalculateResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.camera-calibration.calculate.v1"] = (
        "runtime.camera-calibration.calculate.v1"
    )
    status: Literal["valid", "invalid"]
    profile: RuntimeCameraIntrinsicCalibrationProfile | None = None
    observations: list[RuntimeCameraCalibrationObservationResult] = Field(default_factory=list)
    rms_reprojection_error_px: float | None = Field(default=None, ge=0)
    failure_code: str | None = Field(default=None, max_length=120)
    failure_message: str | None = Field(default=None, max_length=500)
    preview_before: RuntimeLiveFrame | None = None
    preview_after: RuntimeLiveFrame | None = None
    meta: ResponseMeta


class RuntimeOverlayPrimitiveState(StrictRuntimeModel):
    primitive_type: Literal[
        "box",
        "polygon",
        "point",
        "label",
        "measurement",
        "roi",
        "zone",
        "motion",
        "heatmap",
        "mask",
        "line",
        "axis",
        "arrow",
    ]
    layer: str = Field(default="overlay", max_length=64)
    label: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    geometry: dict[str, Any] = Field(default_factory=dict)
    image_context_id: str | None = None
    display_context_id: str | None = None
    frame_id: str | None = Field(default=None, max_length=128)
    source_timestamp: str | None = None
    coordinate_system: str = Field(default="image_pixels", max_length=64)
    resolution: dict[str, Any] = Field(default_factory=dict)
    object_id: str | None = None
    source_object_id: str | None = None
    projected_from_context_id: str | None = None
    style: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeLiveMetrics(StrictRuntimeModel):
    fps: float | None = Field(default=None, ge=0)
    source_fps: float | None = Field(default=None, ge=0)
    processed_fps: float | None = Field(default=None, ge=0)
    drop_rate_percent: float | None = Field(default=None, ge=0, le=100)
    latency_ms: float | None = Field(default=None, ge=0)
    latency_p50_ms: float | None = Field(default=None, ge=0)
    latency_p95_ms: float | None = Field(default=None, ge=0)
    latency_p99_ms: float | None = Field(default=None, ge=0)
    frame_count: int = Field(default=0, ge=0)
    acquired_frame_count: int = Field(default=0, ge=0)
    dropped_frame_count: int = Field(default=0, ge=0)
    reconnect_count: int = Field(default=0, ge=0)
    source_error_count: int = Field(default=0, ge=0)
    buffer_depth: int = Field(default=0, ge=0)
    output_stream_fps: float | None = Field(default=None, ge=0)
    stream_dropped_frame_count: int = Field(default=0, ge=0)
    encode_latency_ms: float | None = Field(default=None, ge=0)
    stream_publication_latency_ms: float | None = Field(default=None, ge=0)
    connected_clients: int = Field(default=0, ge=0)
    gpu_encoder_available: bool = False
    overlay_fps: float | None = Field(default=None, ge=0)
    dropped_overlay_frame_count: int = Field(default=0, ge=0)
    stale_overlay_count: int = Field(default=0, ge=0)
    last_frame_at: str | None = None
    stage_timings_ms: dict[str, float] = Field(default_factory=dict)


class RuntimeLiveSourceHealth(StrictRuntimeModel):
    state: HealthState = HealthState.DOWN
    status_text: str = "No live source"
    connected: bool = False
    last_error: str | None = None
    reconnect_count: int = Field(default=0, ge=0)
    source_error_count: int = Field(default=0, ge=0)


class RuntimeLiveDiagnostics(StrictRuntimeModel):
    source_type: str | None = None
    source_max_fps: float | None = Field(default=None, ge=0)
    source_sample_every_n: int = Field(default=1, ge=1)
    effective_source_limit_fps: float | None = Field(default=None, ge=0)
    pipeline_max_inflight: int = Field(default=1, ge=1)
    pipeline_configured_max_inflight: int = Field(default=1, ge=1)
    pipeline_global_max_concurrency: int = Field(default=1, ge=1)
    pipeline_per_release_max_concurrency: int = Field(default=1, ge=1)
    pipeline_inflight_limit_reason: str = "channel_configuration"
    pipeline_stateful_nodes: list[str] = Field(default_factory=list)
    pipeline_frame_samplers: list[dict[str, Any]] = Field(default_factory=list)
    pipeline_node_timings: list[dict[str, Any]] = Field(default_factory=list)
    pipeline_inference_timings: list[dict[str, Any]] = Field(default_factory=list)
    plc_write_outputs: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    stream_transport: str | None = None


class RuntimeStemRotationViewPoint(StrictRuntimeModel):
    y_mm: float
    z_mm: float


class RuntimeStemRotationChordView(StrictRuntimeModel):
    start: RuntimeStemRotationViewPoint
    end: RuntimeStemRotationViewPoint
    span_mm: float = Field(ge=0)
    valid: bool


class RuntimeStemRotationLongitudinalSliceView(StrictRuntimeModel):
    x_mm: float
    in_evaluation_range: bool = True
    current_center_axis_mm: float
    current_outline_negative_mm: float
    current_outline_positive_mm: float
    optimal_center_axis_mm: float | None = None
    optimal_outline_negative_mm: float | None = None
    optimal_outline_positive_mm: float | None = None
    mean_diameter_mm: float = Field(gt=0)
    ovality_ratio: float = Field(ge=0, le=1)
    quality: float = Field(ge=0, le=1)
    interpolated: bool = False
    limiting_current: bool = False
    limiting_optimal: bool = False


class RuntimeStemRotationCrossSectionView(StrictRuntimeModel):
    x_mm: float
    current_center: RuntimeStemRotationViewPoint
    optimal_center: RuntimeStemRotationViewPoint | None = None
    current_ellipse: list[RuntimeStemRotationViewPoint] = Field(default_factory=list)
    optimal_ellipse: list[RuntimeStemRotationViewPoint] = Field(default_factory=list)
    current_symmetric_chord: RuntimeStemRotationChordView
    optimal_symmetric_chord: RuntimeStemRotationChordView | None = None
    current_phi_deg: float
    optimal_phi_deg: float | None = None
    a_mm: float = Field(gt=0)
    b_mm: float = Field(gt=0)
    quality: float = Field(ge=0, le=1)


class RuntimeStemRotationDeltaGeometryView(StrictRuntimeModel):
    x_mm: float
    in_evaluation_range: bool = True
    current_geometry_mm: float | None = Field(default=None, ge=0)
    optimal_geometry_mm: float | None = Field(default=None, ge=0)
    delta_geometry_mm: float | None = None
    valid: bool
    reason_code: str
    limiting_current: bool = False
    limiting_optimal: bool = False


class RuntimeStemRotationAngleScoreView(StrictRuntimeModel):
    angle_deg: float
    score_mm: float | None = Field(default=None, ge=0)
    valid: bool
    selected: bool = False
    current: bool = False


class RuntimeStemRotationDiagnosticsView(StrictRuntimeModel):
    score_current_mm: float | None = Field(default=None, ge=0)
    score_optimal_mm: float | None = Field(default=None, ge=0)
    optimal_candidate_angle_deg: float | None = None
    plateau_width_deg: float | None = Field(default=None, ge=0)
    score_tolerance_mm: float | None = Field(default=None, ge=0)
    valid_slice_count: int = Field(default=0, ge=0)
    evaluated_slice_count: int = Field(default=0, ge=0)
    mean_fit_quality: float | None = Field(default=None, ge=0, le=1)
    mean_observation_quality: float | None = Field(default=None, ge=0, le=1)
    mean_center_residual_rms_mm: float | None = Field(default=None, ge=0)
    mean_width_residual_rms_mm: float | None = Field(default=None, ge=0)
    minimum_phase_coverage_ratio: float | None = Field(default=None, ge=0, le=1)
    fit_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    active_configuration: dict[str, Any] = Field(default_factory=dict)
    angle_score_curve: list[RuntimeStemRotationAngleScoreView] = Field(
        default_factory=list
    )


class RuntimeStemRotationViewModel(StrictRuntimeModel):
    """Read-only HMI model; all domain geometry is prepared in the backend."""

    schema_version: Literal["stem.rotation-hmi.v1"] = "stem.rotation-hmi.v1"
    coordinate_system: Literal["MACHINE_X_Y_Z_RIGHT_HANDED_V1"] = (
        "MACHINE_X_Y_Z_RIGHT_HANDED_V1"
    )
    rotation_axis: Literal["POSITIVE_MACHINE_X"] = "POSITIVE_MACHINE_X"
    rotation_reference_view: str | None = None
    positive_rotation: str | None = None
    timestamp: str
    state: Literal[
        "IDLE",
        "MEASURING",
        "MODEL_BUILDING",
        "OPTIMIZING",
        "RESULT_READY",
        "DONE",
        "APPLIED",
        "INVALID",
        "ERROR",
    ]
    result_ready: bool = False
    result_valid: bool = False
    priority_source: Literal["FRONTEND", "PLC"]
    priority_mode: Literal[
        "NONE",
        "VORSCHNITT_KRITISCH",
        "NACHSCHNITT_KRITISCH",
    ]
    priority_axis: Literal["NONE", "VERTICAL_Z", "HORIZONTAL_Y"]
    priority_revision: int = Field(ge=0)
    optimization_enable: bool
    should_rotate: bool = False
    rotation_delta_deg: float | None = None
    rotation_direction: Literal["POSITIVE", "NEGATIVE", "NONE", "UNAVAILABLE"]
    gain_mm: float | None = Field(default=None, ge=0)
    model_quality: float = Field(default=0, ge=0, le=1)
    stem_id: str | None = None
    session_id: str | None = None
    model_hash: str | None = None
    reason_code: str
    reason_codes: list[str] = Field(default_factory=list)
    limiting_x_current_mm: float | None = None
    limiting_x_optimal_mm: float | None = None
    comparison_angle_deg: float | None = None
    stem_longitudinal_geometry: list[RuntimeStemRotationLongitudinalSliceView] = Field(
        default_factory=list
    )
    current_critical_cross_section: RuntimeStemRotationCrossSectionView | None = None
    optimal_critical_cross_section: RuntimeStemRotationCrossSectionView | None = None
    delta_geometry_by_x: list[RuntimeStemRotationDeltaGeometryView] = Field(
        default_factory=list
    )
    diagnostics: RuntimeStemRotationDiagnosticsView


class RuntimeLiveViewState(StrictRuntimeModel):
    channel_id: str
    runtime_instance_id: str | None = None
    actual_state: RuntimeObservedState
    available: bool = False
    stale: bool = False
    display_context_id: str | None = None
    display_context: dict[str, Any] = Field(default_factory=dict)
    frame: RuntimeLiveFrame | None = None
    source_frame: RuntimeLiveFrame | None = None
    undistorted_frame: RuntimeLiveFrame | None = None
    overlay: list[RuntimeOverlayPrimitiveState] = Field(default_factory=list)
    source_overlay: list[RuntimeOverlayPrimitiveState] = Field(default_factory=list)
    raw_overlay: list[RuntimeOverlayPrimitiveState] = Field(default_factory=list)
    layers_available: list[str] = Field(default_factory=list)
    metrics: RuntimeLiveMetrics = Field(default_factory=RuntimeLiveMetrics)
    source_health: RuntimeLiveSourceHealth = Field(default_factory=RuntimeLiveSourceHealth)
    diagnostics: RuntimeLiveDiagnostics = Field(default_factory=RuntimeLiveDiagnostics)
    stem_rotation_view: RuntimeStemRotationViewModel | None = None
    recovery_hint: str | None = None
    updated_at: str | None = None


class RuntimeLiveViewResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.live-view.state.v1"] = "runtime.live-view.state.v1"
    state: RuntimeLiveViewState
    meta: ResponseMeta


RuntimeStreamVariantKey = Literal["raw", "undistorted_full", "canonical_model"]
RuntimeStreamProtocol = Literal["webrtc"]
RuntimeStreamStatus = Literal["disabled", "starting", "ready", "degraded", "unavailable"]
RuntimeStreamAccessPolicy = Literal[
    "unauthenticated_lan",
    "session",
    "token",
    "internal_only",
]
RuntimeStreamEncoderMode = Literal["gpu", "unavailable"]
RuntimeStreamCompositorMode = Literal["none"]
RuntimeStreamDesiredState = Literal["stopped", "running"]
RuntimeStreamActualState = Literal["stopped", "starting", "ready", "recovering", "failed"]
RuntimeStreamComponentState = Literal[
    "stopped",
    "starting",
    "ready",
    "recovering",
    "failed",
    "idle",
]


class RuntimeStreamEndpoint(StrictRuntimeModel):
    protocol: RuntimeStreamProtocol
    url: str
    display_url: str | None = None
    external_client_example: str | None = None
    browser_compatible: bool = False
    vlc_compatible: bool = False


class RuntimeStreamEncoderStatus(StrictRuntimeModel):
    mode: RuntimeStreamEncoderMode = "unavailable"
    encoder: str | None = None
    codec: str = "h264"
    hardware_accelerated: bool = False
    pixel_format: str | None = None
    preset: str | None = None
    warning: str | None = None


class RuntimeStreamPublicationMetrics(StrictRuntimeModel):
    input_fps: float | None = Field(default=None, ge=0)
    output_fps: float | None = Field(default=None, ge=0)
    submitted_frames: int = Field(default=0, ge=0)
    published_frames: int = Field(default=0, ge=0)
    encoder_dropped_frames: int = Field(default=0, ge=0)
    transform_fps: float | None = Field(default=None, ge=0)
    transform_dropped_frames: int = Field(default=0, ge=0)
    processing_latency_ms: float | None = Field(default=None, ge=0)
    transform_source_frame_age_ms: float | None = Field(default=None, ge=0)
    dropped_frames: int = Field(default=0, ge=0)
    repeated_frames: int = Field(default=0, ge=0)
    encode_latency_ms: float | None = Field(default=None, ge=0)
    publication_latency_ms: float | None = Field(default=None, ge=0)
    source_frame_age_ms: float | None = Field(default=None, ge=0)
    connected_clients: int = Field(default=0, ge=0)
    reconnect_count: int = Field(default=0, ge=0)
    last_frame_sequence: int = Field(default=0, ge=0)
    updated_at: str | None = None


class RuntimeStreamVariantStatus(StrictRuntimeModel):
    variant: RuntimeStreamVariantKey
    status: RuntimeStreamStatus = "unavailable"
    intended_use: str
    overlay_included: bool = False
    protocol: RuntimeStreamProtocol
    endpoints: list[RuntimeStreamEndpoint] = Field(default_factory=list)
    codec: str = "h264"
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    fps: float | None = Field(default=None, ge=0)
    latency_estimate_ms: float | None = Field(default=None, ge=0)
    encoder: RuntimeStreamEncoderStatus = Field(default_factory=RuntimeStreamEncoderStatus)
    compositor_mode: RuntimeStreamCompositorMode = "none"
    fanout: Literal["server", "none"] = "server"
    multicast: bool = False
    access_policy: RuntimeStreamAccessPolicy = "unauthenticated_lan"
    authentication_required: bool = False
    ready: bool = False
    warning: str | None = None
    metrics: RuntimeStreamPublicationMetrics = Field(
        default_factory=RuntimeStreamPublicationMetrics
    )


class RuntimeStreamComponentStatus(StrictRuntimeModel):
    state: RuntimeStreamComponentState
    ready: bool = False
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RuntimeStreamComponents(StrictRuntimeModel):
    camera: RuntimeStreamComponentStatus
    encoder: RuntimeStreamComponentStatus
    publication: RuntimeStreamComponentStatus
    browser_readers: RuntimeStreamComponentStatus


class RuntimeChannelStreamListResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.channel-streams.list.v2"] = "runtime.channel-streams.list.v2"
    channel_id: str
    desired_state: RuntimeStreamDesiredState = "stopped"
    actual_state: RuntimeStreamActualState = "stopped"
    worker_instance_id: str | None = None
    last_heartbeat_at: str | None = None
    last_error: str | None = None
    negotiated_profile: dict[str, Any] = Field(default_factory=dict)
    components: RuntimeStreamComponents
    stream_auth_enabled: bool = False
    access_policy_default: RuntimeStreamAccessPolicy = "unauthenticated_lan"
    items: list[RuntimeStreamVariantStatus] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    meta: ResponseMeta


class RuntimeChannelStreamControlResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.channel-stream.control.v1"] = (
        "runtime.channel-stream.control.v1"
    )
    channel_id: str
    desired_state: RuntimeStreamDesiredState
    actual_state: RuntimeStreamActualState
    accepted: bool = True
    meta: ResponseMeta


class RuntimeChannelBindResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.channel-bind.response.v1"] = "runtime.channel-bind.response.v1"
    channel_id: str
    pipeline_version_id: str
    runtime_execution_id: str
    executor_service: str
    status: RuntimeExecutionStatus
    meta: ResponseMeta


class RuntimeExecutionSummary(StrictRuntimeModel):
    runtime_execution_id: str
    channel_id: str
    pipeline_version_id: str
    mode: RuntimeMode
    executor_service: str
    executor_instance_id: str | None = None
    status: RuntimeExecutionStatus
    started_at: str | None = None
    stopped_at: str | None = None
    heartbeat_at: str | None = None
    frame_count: int = Field(default=0, ge=0)
    drop_count: int = Field(default=0, ge=0)
    trigger_count: int = Field(default=0, ge=0)
    last_result_id: str | None = None
    last_error: str | None = None
    recovery_attempts: int = Field(default=0, ge=0)


class RuntimeTriggerResponseFieldMapping(StrictRuntimeModel):
    source_field: str = Field(min_length=1, max_length=160)
    response_key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    required: bool = False
    default_value: Any | None = None


class RuntimeTriggerResponsePolicy(StrictRuntimeModel):
    mode: Literal["configured_fields", "all_final_response"] = "all_final_response"
    fields: list[RuntimeTriggerResponseFieldMapping] = Field(default_factory=list)
    include_unmapped: bool = False

    @model_validator(mode="after")
    def validate_fields(self) -> RuntimeTriggerResponsePolicy:
        if self.mode == "configured_fields" and not self.fields:
            raise ValueError("configured_fields response policy requires at least one field")
        keys = [field.response_key for field in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError("response field keys must be unique")
        return self


class RuntimeTriggerEndpointReadiness(StrictRuntimeModel):
    ready: bool
    reason: str
    runtime_instance_id: str | None = None
    actual_state: RuntimeObservedState | None = None
    channel_id: str | None = None
    pipeline_version_id: str | None = None
    endpoint_pipeline_version_id: str | None = None
    last_heartbeat_at: str | None = None


class RuntimeTriggerEndpointCreateRequest(StrictRuntimeModel):
    name: str = Field(min_length=3, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    channel_id: str = Field(min_length=3, max_length=64)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    pipeline_version_id: str = Field(min_length=3, max_length=64)
    trigger_type: Literal["http_put"] = "http_put"
    enabled: bool = True
    auth_required: bool = False
    auth_scope: str = Field(default="runtime.trigger.invoke", min_length=3, max_length=128)
    auth_policy: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=2500, ge=50, le=30_000)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=60_000)
    burst_limit: int = Field(default=4, ge=1, le=1_000)
    response_policy: RuntimeTriggerResponsePolicy = Field(
        default_factory=RuntimeTriggerResponsePolicy
    )
    request_schema: dict[str, Any] = Field(default_factory=dict)
    response_schema: dict[str, Any] = Field(default_factory=dict)


class RuntimeTriggerEndpointUpdateRequest(StrictRuntimeModel):
    name: str | None = Field(default=None, min_length=3, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    channel_id: str | None = Field(default=None, min_length=3, max_length=64)
    pipeline_version_id: str | None = Field(default=None, min_length=3, max_length=64)
    trigger_type: Literal["http_put"] | None = None
    enabled: bool | None = None
    auth_required: bool | None = None
    auth_scope: str | None = Field(default=None, min_length=3, max_length=128)
    auth_policy: dict[str, Any] | None = None
    timeout_ms: int | None = Field(default=None, ge=50, le=30_000)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=60_000)
    burst_limit: int | None = Field(default=None, ge=1, le=1_000)
    response_policy: RuntimeTriggerResponsePolicy | None = None
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None


class RuntimeTriggerEndpointSummary(StrictRuntimeModel):
    trigger_endpoint_id: str
    channel_id: str
    channel_name: str | None = None
    name: str
    description: str | None = None
    slug: str
    trigger_type: Literal["http_put"] = "http_put"
    method: Literal["PUT"] = "PUT"
    path: str
    enabled: bool
    auth_required: bool
    auth_scope: str
    auth_policy: dict[str, Any] = Field(default_factory=dict)
    pipeline_definition_id: str | None = None
    pipeline_version_id: str | None = None
    trigger_node_key: str | None = None
    derived_from_pipeline: bool = False
    pipeline_label: str | None = None
    timeout_ms: int = Field(ge=50, le=30_000)
    rate_limit_per_minute: int = Field(ge=1, le=60_000)
    burst_limit: int = Field(ge=1, le=1_000)
    status: RuntimeResourceStatus
    readiness: RuntimeTriggerEndpointReadiness
    last_invoked_at: str | None = None
    last_status: RuntimeTriggerExecutionStatus | None = None
    last_latency_ms: float | None = Field(default=None, ge=0)
    last_error_code: str | None = None
    last_error_message: str | None = None
    invocation_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)


class RuntimeTriggerEndpointDetail(RuntimeTriggerEndpointSummary):
    response_policy: RuntimeTriggerResponsePolicy = Field(
        default_factory=RuntimeTriggerResponsePolicy
    )
    request_schema: dict[str, Any] = Field(default_factory=dict)
    response_schema: dict[str, Any] = Field(default_factory=dict)
    last_result: RuntimeResultRecordSummary | None = None
    created_at: str
    updated_at: str | None = None
    created_by: str | None = None
    updated_by: str | None = None


class RuntimeTriggerEndpointListResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.trigger-endpoints.list.v1"] = (
        "runtime.trigger-endpoints.list.v1"
    )
    items: list[RuntimeTriggerEndpointSummary] = Field(default_factory=list)
    meta: ResponseMeta


class RuntimeCaptureRuleCreateRequest(StrictRuntimeModel):
    channel_id: str = Field(min_length=3, max_length=64)
    pipeline_version_id: str | None = Field(default=None, min_length=3, max_length=64)
    rule_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    capture_mode: Literal["full_image", "roi", "full_image_and_roi"]
    condition: dict[str, Any] = Field(default_factory=dict)
    target_label_project_ids: list[str] = Field(default_factory=list, min_length=1)
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    dedupe_policy: dict[str, Any] = Field(default_factory=dict)


class RuntimeCaptureRuleSummary(RuntimeCaptureRuleCreateRequest):
    capture_rule_id: str
    status: RuntimeResourceStatus


class RuntimeOutputConnectorDeliveryPolicy(StrictRuntimeModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_backoff_seconds: list[int] = Field(default_factory=lambda: [5, 30, 120])

    @model_validator(mode="after")
    def validate_retry_backoff(self) -> RuntimeOutputConnectorDeliveryPolicy:
        if any(value < 1 or value > 86_400 for value in self.retry_backoff_seconds):
            raise ValueError("retry_backoff_seconds values must be between 1 and 86400")
        return self


class RuntimeOutputConnectorCreateRequest(StrictRuntimeModel):
    channel_id: str = Field(min_length=3, max_length=64)
    connector_type: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    name: str = Field(min_length=3, max_length=160)
    source_node_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    delivery_policy: RuntimeOutputConnectorDeliveryPolicy = Field(
        default_factory=RuntimeOutputConnectorDeliveryPolicy
    )


class RuntimeOutputConnectorSummary(RuntimeOutputConnectorCreateRequest):
    output_connector_id: str
    status: RuntimeResourceStatus
    pipeline_version_id: str | None = None
    last_delivery_status: RuntimeConnectorDeliveryStatus | None = None
    last_delivery_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class RuntimeOutputConnectorDetail(RuntimeOutputConnectorSummary):
    created_by: str | None = None
    updated_by: str | None = None


class RuntimeOutputConnectorUpdateRequest(StrictRuntimeModel):
    name: str | None = Field(default=None, min_length=3, max_length=160)
    source_node_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    secret_refs: dict[str, str] | None = None
    delivery_policy: RuntimeOutputConnectorDeliveryPolicy | None = None


class RuntimeOutputConnectorListResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.output-connectors.list.v1"] = (
        "runtime.output-connectors.list.v1"
    )
    items: list[RuntimeOutputConnectorSummary] = Field(default_factory=list)
    meta: ResponseMeta


class RuntimeOutputConnectorTypeSummary(StrictRuntimeModel):
    connector_type: str
    display_name: str
    description: str
    config_schema: dict[str, Any] = Field(default_factory=dict)
    default_config: dict[str, Any] = Field(default_factory=dict)
    default_delivery_policy: RuntimeOutputConnectorDeliveryPolicy = Field(
        default_factory=RuntimeOutputConnectorDeliveryPolicy
    )
    supports_secrets: bool = False
    retryable: bool = True


class RuntimeOutputConnectorTypeCatalogResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.output-connector-types.catalog.v1"] = (
        "runtime.output-connector-types.catalog.v1"
    )
    items: list[RuntimeOutputConnectorTypeSummary] = Field(default_factory=list)
    meta: ResponseMeta


class RuntimeConnectorDeliverySummary(StrictRuntimeModel):
    connector_delivery_id: str
    output_connector_id: str
    channel_id: str
    result_record_id: str
    connector_type: str
    connector_name: str | None = None
    source_node_key: str | None = None
    status: RuntimeConnectorDeliveryStatus
    retry_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    queued_at: str | None = None
    claimed_at: str | None = None
    delivered_at: str | None = None
    completed_at: str | None = None
    next_attempt_at: str | None = None
    request_payload: dict[str, Any] = Field(default_factory=dict)
    response_payload: dict[str, Any] = Field(default_factory=dict)
    last_error: str | None = None
    created_at: str | None = None


class RuntimeConnectorDeliveryHistoryResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.connector-deliveries.history.v1"] = (
        "runtime.connector-deliveries.history.v1"
    )
    items: list[RuntimeConnectorDeliverySummary] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    meta: ResponseMeta


class RuntimeConnectorDrainRequest(StrictRuntimeModel):
    limit: int = Field(default=20, ge=1, le=500)


class RuntimeConnectorDrainResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.connector-deliveries.drain.v1"] = (
        "runtime.connector-deliveries.drain.v1"
    )
    processed: int = Field(default=0, ge=0)
    delivered: int = Field(default=0, ge=0)
    failed_retryable: int = Field(default=0, ge=0)
    failed_terminal: int = Field(default=0, ge=0)
    skipped_no_payload: int = Field(default=0, ge=0)
    meta: ResponseMeta


class RuntimeNodeTypeSummary(StrictRuntimeModel):
    node_type: str
    node_category: RuntimeNodeCategory
    contract_version: int = Field(ge=1)
    display_name: str
    description: str = ""
    supported_modes: list[RuntimeMode] = Field(default_factory=list)
    input_ports: list[RuntimeNodePort] = Field(default_factory=list)
    output_ports: list[RuntimeNodePort] = Field(default_factory=list)
    config_schema_ref: str
    property_schema: list[RuntimeNodePropertySpec] = Field(default_factory=list)
    default_config: dict[str, Any] = Field(default_factory=dict)
    runtime_handler_ref: str
    compatibility_constraints: dict[str, Any] = Field(default_factory=dict)


class RuntimeNodeTypeCatalogResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.node-types.catalog.v1"] = "runtime.node-types.catalog.v1"
    items: list[RuntimeNodeTypeSummary] = Field(default_factory=list)
    meta: ResponseMeta


class RuntimeBoundaryResponse(StrictRuntimeModel):
    schema_version: Literal["runtime.boundary.v1"] = "runtime.boundary.v1"
    service_name: str
    responsibilities: list[str] = Field(default_factory=list)
    non_responsibilities: list[str] = Field(default_factory=list)
