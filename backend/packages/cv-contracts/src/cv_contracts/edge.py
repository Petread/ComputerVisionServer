from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from cv_contracts.common import ResponseMeta


class StrictEdgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


EdgePlatform = Literal[
    "jetson_orin_nano",
    "jetson_agx_orin",
    "windows_11",
    "simulated",
]
EdgeHealthState = Literal["ok", "degraded", "down"]
EdgeArtifactKind = Literal[
    "onnx_model",
    "label_map",
    "pipeline_graph",
    "runtime_plugin",
    "configuration",
    "golden_input",
    "other",
]
EdgeDeploymentStatus = Literal[
    "requested",
    "validating",
    "transferring",
    "prepared",
    "activating",
    "running",
    "standby",
    "activation_failed",
    "rolling_back",
    "rolled_back",
    "stopping",
    "stopped",
]
EdgeRuntimeSlotName = Literal["A", "B"]
EdgeRuntimeSlotState = Literal["empty", "prepared", "active", "standby", "failed"]
EdgeRuntimeProcessState = Literal[
    "stopped",
    "starting",
    "running",
    "backoff",
    "crash_loop",
]
EdgeRuntimeSupervisorState = Literal["idle", "running", "degraded"]
EdgeConnectivityState = Literal["unknown", "online", "degraded", "offline", "stale"]
EdgeFleetCommandStatus = Literal["queued", "running", "succeeded", "failed"]
EdgeFleetCommandType = Literal[
    "software_update",
    "software_rollback",
    "reconcile",
    "blob_upload",
    "deployment_prepare",
    "deployment_commit",
    "deployment_rollback",
    "deployment_stop",
    "result_ack",
]
EdgeWorkloadReleaseStatus = Literal["built", "admitted", "rejected"]
EdgeAdmissionOutcome = Literal["pass", "warning", "fail"]
EdgeDeploymentAttemptStatus = Literal[
    "queued",
    "building",
    "admission_failed",
    "transferring",
    "preparing",
    "activating",
    "running",
    "standby",
    "stopping",
    "stopped",
    "rolling_back",
    "rolled_back",
    "failed",
]
EdgeOperatorCheckOutcome = Literal["pass", "warning", "fail"]
EdgeDiagnosticStatus = Literal["healthy", "degraded", "critical"]
EdgeAlertSeverity = Literal["info", "warning", "critical"]


class EdgeDeviceAddress(StrictEdgeModel):
    management_ip: IPv4Address = Field(description="Fixed IPv4 address of the Edge device itself.")
    control_port: int = Field(
        default=9443,
        ge=1,
        le=65535,
        description="Edge-local control port reached from the orchestration server.",
    )
    plant_api_port: int = Field(
        default=9444,
        ge=1,
        le=65535,
        description="Edge-local HTTP trigger port reached from admitted PLC clients.",
    )
    rtsp_port: int = Field(default=8554, ge=1, le=65535)


class EdgeDeviceIdentity(StrictEdgeModel):
    schema_version: Literal["edge.device.identity.v1"] = "edge.device.identity.v1"
    address: EdgeDeviceAddress
    hostname: str = Field(min_length=1, max_length=253)
    platform: EdgePlatform
    architecture: str = Field(min_length=1, max_length=64)
    agent_version: str = Field(min_length=1, max_length=64)
    runtime_version: str = Field(min_length=1, max_length=64)
    boot_id: str = Field(min_length=1, max_length=128)


class EdgeRuntimeSlot(StrictEdgeModel):
    name: EdgeRuntimeSlotName
    state: EdgeRuntimeSlotState
    deployment_id: str | None = Field(default=None, max_length=128)
    workload_release_id: str | None = Field(default=None, max_length=128)
    updated_at: str


class EdgeRuntimeProcess(StrictEdgeModel):
    deployment_id: str = Field(min_length=3, max_length=128)
    state: EdgeRuntimeProcessState
    restart_count: int = Field(default=0, ge=0)
    started_at: str | None = None
    last_heartbeat_at: str | None = None
    next_restart_at: str | None = None
    last_error: str | None = Field(default=None, max_length=2000)


class EdgeStorageQuotaStatus(StrictEdgeModel):
    artifact_bytes: int = Field(ge=0)
    artifact_quota_bytes: int = Field(ge=1)
    outbox_bytes: int = Field(ge=0)
    outbox_quota_bytes: int = Field(ge=1)
    outbox_items: int = Field(ge=0)
    outbox_max_items: int = Field(ge=1)
    disk_free_bytes: int = Field(ge=0)
    minimum_disk_free_bytes: int = Field(ge=0)
    pressure: bool = False


class EdgeRuntimeStatus(StrictEdgeModel):
    schema_version: Literal["edge.runtime.status.v1"] = "edge.runtime.status.v1"
    boot_id: str = Field(min_length=1, max_length=128)
    supervisor_state: EdgeRuntimeSupervisorState
    active_slot: EdgeRuntimeSlotName | None = None
    previous_slot: EdgeRuntimeSlotName | None = None
    slots: list[EdgeRuntimeSlot] = Field(min_length=2, max_length=2)
    processes: list[EdgeRuntimeProcess] = Field(default_factory=list)
    storage: EdgeStorageQuotaStatus
    database_journal_mode: Literal["wal"] = "wal"
    recovered_deployment_ids: list[str] = Field(default_factory=list)
    observed_at: str


class EdgeDeviceHealth(StrictEdgeModel):
    schema_version: Literal["edge.device.health.v1"] = "edge.device.health.v1"
    state: EdgeHealthState
    detail: str = Field(min_length=1, max_length=1000)
    boot_id: str = Field(min_length=1, max_length=128)
    observed_at: str
    active_deployment_ids: list[str] = Field(default_factory=list)
    result_backlog_count: int = Field(default=0, ge=0)
    disk_free_bytes: int = Field(default=0, ge=0)
    runtime: EdgeRuntimeStatus | None = None


class EdgeCameraAdapterCapability(StrictEdgeModel):
    adapter: str = Field(min_length=1, max_length=64)
    available: bool
    provider: str = Field(min_length=1, max_length=96)
    version: str | None = Field(default=None, max_length=64)
    device_count: int | None = Field(default=None, ge=0)
    detail: str | None = Field(default=None, max_length=500)


class EdgeCapabilitySnapshot(StrictEdgeModel):
    schema_version: Literal["edge.device.capabilities.v1"] = "edge.device.capabilities.v1"
    management_ip: IPv4Address = Field(description="Fixed IPv4 address of the Edge device itself.")
    platform: EdgePlatform
    architecture: str = Field(min_length=1, max_length=64)
    os_version: str = Field(min_length=1, max_length=160)
    jetpack_version: str | None = Field(default=None, max_length=64)
    l4t_version: str | None = Field(default=None, max_length=64)
    cuda_version: str | None = Field(default=None, max_length=64)
    tensorrt_version: str | None = Field(default=None, max_length=64)
    deepstream_version: str | None = Field(default=None, max_length=64)
    gpu_model: str | None = Field(default=None, max_length=160)
    gpu_driver_version: str | None = Field(default=None, max_length=64)
    gpu_memory_bytes: int = Field(default=0, ge=0)
    python_version: str | None = Field(default=None, max_length=64)
    ram_total_bytes: int = Field(ge=0)
    storage_total_bytes: int = Field(ge=0)
    storage_free_bytes: int = Field(ge=0)
    agent_version: str = Field(min_length=1, max_length=64)
    runtime_version: str = Field(min_length=1, max_length=64)
    supported_node_types: list[str] = Field(default_factory=list)
    runtime_code_digest: str | None = None
    node_contract_versions: dict[str, int] = Field(default_factory=dict)
    software_update_protocol: int = 0
    camera_adapters: list[str] = Field(default_factory=list)
    camera_inventory: list[EdgeCameraAdapterCapability] = Field(default_factory=list)
    inventory_source: Literal["probed", "partial", "configured_fallback"] = "configured_fallback"
    inventory_warnings: list[str] = Field(default_factory=list)
    observed_at: str


class EdgeArtifactDescriptor(StrictEdgeModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    kind: EdgeArtifactKind
    file_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=160)

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("file_name must be a base name without path separators")
        return value


class EdgeSoftwareRelease(StrictEdgeModel):
    schema_version: Literal["edge.software.release.v1"] = "edge.software.release.v1"
    release_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,79}$")
    platform: EdgePlatform
    architecture: str
    python_version: str
    cuda_version: str | None = None
    tensorrt_version: str | None = None
    runtime_code_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_contract_versions: dict[str, int] = Field(default_factory=dict)
    wheels: dict[str, str] = Field(min_length=1)


class EdgeSoftwareCommand(StrictEdgeModel):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,127}$")
    action: Literal["update", "rollback"] = "update"
    package_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class EdgeSoftwareStatus(StrictEdgeModel):
    protocol_version: int = 0
    request_id: str | None = None
    phase: Literal[
        "idle",
        "queued",
        "transferring",
        "verifying",
        "installing",
        "activating",
        "checking",
        "succeeded",
        "failed",
        "rolled_back",
    ] = "idle"
    active_release: str = "bootstrap"
    previous_release: str | None = None
    candidate_release: str | None = None
    runtime_code_digest: str | None = None
    last_error: str | None = None
    updated_at: str | None = None
    transfer_bytes: int = Field(default=0, ge=0)
    transfer_total_bytes: int = Field(default=0, ge=0)


class EdgeSoftwareRolloutRequest(StrictEdgeModel):
    package_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    action: Literal["update", "rollback"] = "update"
    idempotency_key: str = Field(min_length=3, max_length=120)


class EdgeSoftwarePackageSummary(StrictEdgeModel):
    package_sha256: str
    size_bytes: int = Field(ge=0)
    release: EdgeSoftwareRelease


class EdgeSoftwareCompatibility(StrictEdgeModel):
    package_sha256: str
    reasons: list[str] = Field(default_factory=list)


class EdgeSoftwareCommandSummary(StrictEdgeModel):
    command_id: str
    status: EdgeFleetCommandStatus
    action: Literal["update", "rollback"]
    package_sha256: str | None = None
    last_error: str | None = None
    software: EdgeSoftwareStatus = Field(default_factory=EdgeSoftwareStatus)
    created_at: str


class EdgeSoftwareDeviceSummary(StrictEdgeModel):
    edge_device_id: str
    runtime_code_digest: str | None = None
    update_protocol: int = 0
    platform: EdgePlatform | None = None
    runtime_version: str | None = None
    node_count: int = 0
    requires_update: bool = False
    releases: list[EdgeSoftwareCompatibility] = Field(default_factory=list)
    commands: list[EdgeSoftwareCommandSummary] = Field(default_factory=list)


class EdgeSoftwareOverview(StrictEdgeModel):
    server_runtime_digest: str
    releases: list[EdgeSoftwarePackageSummary] = Field(default_factory=list)
    devices: list[EdgeSoftwareDeviceSummary] = Field(default_factory=list)


class EdgeDeploymentManifest(StrictEdgeModel):
    schema_version: Literal["edge.deployment.manifest.v1"] = "edge.deployment.manifest.v1"
    deployment_id: str = Field(min_length=3, max_length=128)
    workload_release_id: str = Field(min_length=3, max_length=128)
    channel_id: str = Field(min_length=3, max_length=128)
    pipeline_version_id: str = Field(min_length=3, max_length=128)
    idempotency_key: str = Field(min_length=3, max_length=160)
    artifacts: list[EdgeArtifactDescriptor] = Field(default_factory=list)
    required_platforms: list[EdgePlatform] = Field(default_factory=list)
    required_node_types: list[str] = Field(default_factory=list)
    runtime_requirements: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    simulate_activation_failure: bool = False


class EdgeDeploymentCommandRequest(StrictEdgeModel):
    idempotency_key: str = Field(min_length=3, max_length=160)


class EdgeDeploymentState(StrictEdgeModel):
    schema_version: Literal["edge.deployment.state.v1"] = "edge.deployment.state.v1"
    deployment_id: str
    workload_release_id: str
    channel_id: str
    status: EdgeDeploymentStatus
    idempotency_key: str
    prepared_at: str | None = None
    activated_at: str | None = None
    rolled_back_at: str | None = None
    last_error: str | None = None
    missing_artifacts: list[str] = Field(default_factory=list)
    runtime_slot: EdgeRuntimeSlotName | None = None
    recovery_count: int = Field(default=0, ge=0)
    admission_report: dict[str, Any] = Field(default_factory=dict)
    engine_builds: list[dict[str, Any]] = Field(default_factory=list)


class EdgeBlobUploadStatus(StrictEdgeModel):
    schema_version: Literal["edge.blob.status.v1"] = "edge.blob.status.v1"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    received_bytes: int = Field(ge=0)
    complete: bool
    idempotency_key: str | None = Field(default=None, max_length=160)


class EdgeResultEnvelope(StrictEdgeModel):
    schema_version: Literal["edge.result.envelope.v1"] = "edge.result.envelope.v1"
    management_ip: IPv4Address = Field(description="Fixed IPv4 address of the Edge device itself.")
    boot_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    channel_id: str = Field(min_length=1, max_length=128)
    workload_release_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=160)
    occurred_at: str
    result_type: str = Field(default="pipeline_execution", min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class EdgeResultBatch(StrictEdgeModel):
    schema_version: Literal["edge.result.batch.v1"] = "edge.result.batch.v1"
    management_ip: IPv4Address = Field(description="Fixed IPv4 address of the Edge device itself.")
    boot_id: str
    after_sequence: int = Field(ge=0)
    items: list[EdgeResultEnvelope] = Field(default_factory=list)
    highest_sequence: int = Field(ge=0)
    has_more: bool = False

    @field_validator("items")
    @classmethod
    def validate_item_identity(
        cls,
        items: list[EdgeResultEnvelope],
        info: ValidationInfo,
    ) -> list[EdgeResultEnvelope]:
        management_ip = info.data.get("management_ip")
        boot_id = info.data.get("boot_id")
        if any(item.management_ip != management_ip or item.boot_id != boot_id for item in items):
            raise ValueError("all result items must belong to the batch device and boot")
        sequences = [item.sequence for item in items]
        if sequences != sorted(set(sequences)):
            raise ValueError("result item sequences must be unique and ascending")
        return items


class EdgeResultAck(StrictEdgeModel):
    schema_version: Literal["edge.result.ack.v1"] = "edge.result.ack.v1"
    management_ip: IPv4Address = Field(description="Fixed IPv4 address of the Edge device itself.")
    boot_id: str = Field(min_length=1, max_length=128)
    acknowledged_through_sequence: int = Field(ge=0)
    idempotency_key: str = Field(min_length=3, max_length=160)


class EdgeResultStreamSummary(StrictEdgeModel):
    boot_id: str = Field(min_length=1, max_length=128)
    earliest_sequence: int = Field(ge=0)
    highest_sequence: int = Field(ge=0)
    acknowledged_sequence: int = Field(ge=0)
    pending_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_watermarks(self) -> EdgeResultStreamSummary:
        if self.acknowledged_sequence > self.highest_sequence:
            raise ValueError("acknowledged_sequence must not exceed highest_sequence")
        if self.pending_count == 0 and self.earliest_sequence != 0:
            raise ValueError("earliest_sequence must be zero when no results are pending")
        if self.pending_count > 0 and not (
            self.acknowledged_sequence < self.earliest_sequence <= self.highest_sequence
        ):
            raise ValueError(
                "pending streams require an earliest sequence after the acknowledgement"
            )
        return self


class EdgeResultStreamList(StrictEdgeModel):
    schema_version: Literal["edge.result.streams.v1"] = "edge.result.streams.v1"
    management_ip: IPv4Address = Field(description="Fixed IPv4 address of the Edge device itself.")
    current_boot_id: str = Field(min_length=1, max_length=128)
    items: list[EdgeResultStreamSummary] = Field(default_factory=list)


class EdgeMediaLeaseRequest(StrictEdgeModel):
    lease_id: str = Field(min_length=3, max_length=160)
    channel_id: str = Field(min_length=3, max_length=128)
    ttl_seconds: int = Field(default=30, ge=5, le=120)
    max_fps: float = Field(default=2.0, ge=0.2, le=10.0)
    max_width: int = Field(default=640, ge=64, le=1920)
    include_overlay: bool = True


class EdgeMediaLeaseState(StrictEdgeModel):
    schema_version: Literal["edge.media.lease.v1"] = "edge.media.lease.v1"
    lease_id: str
    channel_id: str
    expires_at: str
    max_fps: float
    max_width: int
    include_overlay: bool


class EdgePreviewFrame(StrictEdgeModel):
    schema_version: Literal["edge.media.preview.v1"] = "edge.media.preview.v1"
    channel_id: str
    workload_release_id: str | None = None
    boot_id: str
    available: bool = False
    sequence: int = Field(default=0, ge=0)
    image_base64: str | None = None
    mime_type: Literal["image/jpeg"] = "image/jpeg"
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    captured_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EdgePlantHealth(StrictEdgeModel):
    """Minimal health contract exposed to the machine/PLC network on port 9444."""

    schema_version: Literal["edge.plant.health.v1"] = "edge.plant.health.v1"
    state: Literal["ok", "degraded"]
    boot_id: str = Field(min_length=1, max_length=128)
    active_channel_ids: list[str] = Field(default_factory=list)
    max_request_bytes: int = Field(ge=1)
    max_concurrent_triggers: int = Field(ge=1)
    observed_at: str


class EdgePlantTriggerRequest(StrictEdgeModel):
    """Bounded, task-neutral trigger input. Images are deliberately not accepted here."""

    schema_version: Literal["edge.plant.trigger.request.v1"] = "edge.plant.trigger.request.v1"
    correlation_id: str | None = Field(default=None, min_length=1, max_length=160)
    payload: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=2500, ge=50, le=10_000)
    include_overlay: bool = False


class EdgePlantTriggerResponse(StrictEdgeModel):
    schema_version: Literal["edge.plant.trigger.response.v1"] = "edge.plant.trigger.response.v1"
    channel_id: str = Field(min_length=3, max_length=128)
    workload_release_id: str = Field(min_length=3, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=160)
    boot_id: str = Field(min_length=1, max_length=128)
    result_sequence: int = Field(ge=1)
    duplicate: bool = False
    execution_result: dict[str, Any]
    completed_at: str


class EdgePlantLastResultResponse(StrictEdgeModel):
    schema_version: Literal["edge.plant.last-result.v1"] = "edge.plant.last-result.v1"
    channel_id: str = Field(min_length=3, max_length=128)
    available: bool = False
    result: EdgePlantTriggerResponse | None = None


class EdgeViewerLeaseRequest(StrictEdgeModel):
    lease_id: str | None = Field(default=None, min_length=3, max_length=160)
    ttl_seconds: int = Field(default=30, ge=5, le=120)
    max_fps: float = Field(default=2.0, ge=0.2, le=10.0)
    max_width: int = Field(default=640, ge=64, le=1920)
    include_overlay: bool = True


class EdgeViewerLeaseSummary(StrictEdgeModel):
    schema_version: Literal["edge.viewer.lease.v1"] = "edge.viewer.lease.v1"
    lease_id: str
    channel_id: str
    edge_device_id: str
    status: Literal["active", "released", "expired"]
    expires_at: str
    max_fps: float
    max_width: int
    include_overlay: bool
    created_at: str
    updated_at: str


class EdgeResultSyncDeviceStatus(StrictEdgeModel):
    edge_device_id: str
    last_cycle_at: str | None = None
    last_success_at: str | None = None
    persisted_results: int = Field(default=0, ge=0)
    acknowledged_results: int = Field(default=0, ge=0)
    preview_frames: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=2000)


class EdgeResultSyncStatus(StrictEdgeModel):
    schema_version: Literal["edge.result-sync.status.v1"] = "edge.result-sync.status.v1"
    running: bool
    interval_seconds: float = Field(gt=0)
    batch_limit: int = Field(ge=1, le=1000)
    cycle_count: int = Field(default=0, ge=0)
    persisted_results: int = Field(default=0, ge=0)
    acknowledged_results: int = Field(default=0, ge=0)
    preview_frames: int = Field(default=0, ge=0)
    last_cycle_started_at: str | None = None
    last_cycle_finished_at: str | None = None
    last_error: str | None = Field(default=None, max_length=2000)
    devices: list[EdgeResultSyncDeviceStatus] = Field(default_factory=list)


class EdgeControlError(StrictEdgeModel):
    schema_version: Literal["edge.control.error.v1"] = "edge.control.error.v1"
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class EdgeDeviceRegistrationRequest(StrictEdgeModel):
    name: str = Field(min_length=3, max_length=160)
    technical_place: str | None = Field(default=None, max_length=160)
    address: EdgeDeviceAddress
    allowed_cidr: IPv4Network = Field(
        description="Isolated machine VLAN/CIDR containing Edge device and orchestration server."
    )
    server_ip: IPv4Address = Field(
        description="Fixed LAN/VLAN IPv4 address of the central orchestration server."
    )
    enabled: bool = True

    @model_validator(mode="after")
    def validate_network_scope(self) -> EdgeDeviceRegistrationRequest:
        if self.address.management_ip not in self.allowed_cidr:
            raise ValueError("management_ip must belong to allowed_cidr")
        if self.server_ip not in self.allowed_cidr:
            raise ValueError("server_ip must belong to allowed_cidr")
        if self.server_ip == self.address.management_ip:
            raise ValueError("server_ip and management_ip must differ")
        return self


class EdgeDeviceUpdateRequest(StrictEdgeModel):
    name: str | None = Field(default=None, min_length=3, max_length=160)
    technical_place: str | None = Field(default=None, max_length=160)
    control_port: int | None = Field(default=None, ge=1, le=65535)
    plant_api_port: int | None = Field(default=None, ge=1, le=65535)
    rtsp_port: int | None = Field(default=None, ge=1, le=65535)
    allowed_cidr: IPv4Network | None = Field(
        default=None,
        description="Isolated machine VLAN/CIDR containing Edge device and orchestration server.",
    )
    server_ip: IPv4Address | None = Field(
        default=None,
        description="Fixed LAN/VLAN IPv4 address of the central orchestration server.",
    )
    enabled: bool | None = None


class EdgeFleetCommandSummary(StrictEdgeModel):
    command_id: str
    edge_device_id: str
    command_type: EdgeFleetCommandType
    idempotency_key: str
    status: EdgeFleetCommandStatus
    attempt_count: int = Field(ge=0)
    request_payload: dict[str, Any] = Field(default_factory=dict)
    response_payload: dict[str, Any] = Field(default_factory=dict)
    last_error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class EdgeCapabilitySnapshotRecord(StrictEdgeModel):
    snapshot_id: str
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: str
    capabilities: EdgeCapabilitySnapshot


class EdgeResultCursorSummary(StrictEdgeModel):
    edge_device_id: str
    management_ip: IPv4Address = Field(description="Fixed IPv4 address of the Edge device itself.")
    boot_id: str = Field(min_length=1, max_length=128)
    last_persisted_sequence: int = Field(ge=0)
    last_acknowledged_sequence: int = Field(ge=0)
    updated_at: str


class EdgeDeviceSummary(StrictEdgeModel):
    schema_version: Literal["edge.fleet.device.v1"] = "edge.fleet.device.v1"
    edge_device_id: str
    name: str
    technical_place: str | None = None
    address: EdgeDeviceAddress
    allowed_cidr: IPv4Network = Field(
        description="Isolated machine VLAN/CIDR containing Edge device and orchestration server."
    )
    server_ip: IPv4Address = Field(
        description="Fixed LAN/VLAN IPv4 address of the central orchestration server."
    )
    enabled: bool
    connectivity_state: EdgeConnectivityState
    reported_health_state: EdgeHealthState | None = None
    last_seen_at: str | None = None
    last_poll_attempt_at: str | None = None
    last_connectivity_error: str | None = None
    current_boot_id: str | None = None
    identity_generation: int = Field(ge=1)
    deployment_revalidation_required: bool
    assigned_channel_count: int = Field(ge=0)
    created_at: str
    updated_at: str


class EdgeDeviceDetail(EdgeDeviceSummary):
    identity: EdgeDeviceIdentity | None = None
    health: EdgeDeviceHealth | None = None
    latest_capability_snapshot: EdgeCapabilitySnapshotRecord | None = None
    recent_commands: list[EdgeFleetCommandSummary] = Field(default_factory=list)
    result_cursors: list[EdgeResultCursorSummary] = Field(default_factory=list)


class EdgeDeviceListResponse(StrictEdgeModel):
    schema_version: Literal["edge.fleet.devices.list.v1"] = "edge.fleet.devices.list.v1"
    items: list[EdgeDeviceSummary]
    meta: ResponseMeta


class EdgeReconcileRequest(StrictEdgeModel):
    idempotency_key: str = Field(min_length=3, max_length=160)


class EdgeDeviceReconcileResponse(StrictEdgeModel):
    schema_version: Literal["edge.fleet.reconcile.v1"] = "edge.fleet.reconcile.v1"
    device: EdgeDeviceDetail
    command: EdgeFleetCommandSummary
    meta: ResponseMeta


class EdgeFleetReconcilerStatus(StrictEdgeModel):
    schema_version: Literal["edge.fleet.reconciler.v1"] = "edge.fleet.reconciler.v1"
    running: bool
    interval_seconds: float = Field(gt=0)
    cycle_count: int = Field(default=0, ge=0)
    last_cycle_started_at: str | None = None
    last_cycle_finished_at: str | None = None
    last_cycle_device_count: int = Field(default=0, ge=0)
    last_cycle_success_count: int = Field(default=0, ge=0)
    last_cycle_failure_count: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=2000)


class EdgeAdmissionCheck(StrictEdgeModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    outcome: EdgeAdmissionOutcome
    message: str = Field(min_length=1, max_length=1000)
    detail: dict[str, Any] = Field(default_factory=dict)


class EdgeAdmissionReport(StrictEdgeModel):
    schema_version: Literal["edge.workload.admission.v1"] = "edge.workload.admission.v1"
    status: Literal["admitted", "rejected"]
    edge_device_id: str = Field(min_length=3, max_length=64)
    capability_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_at: str
    checks: list[EdgeAdmissionCheck] = Field(default_factory=list)


class EdgeWorkloadReleaseBuildRequest(StrictEdgeModel):
    channel_id: str = Field(min_length=3, max_length=64)
    idempotency_key: str = Field(min_length=3, max_length=160)
    precision: Literal["fp32", "fp16", "int8"] = "fp16"


class EdgeWorkloadReleaseSummary(StrictEdgeModel):
    schema_version: Literal["edge.workload.release.v1"] = "edge.workload.release.v1"
    workload_release_id: str = Field(min_length=3, max_length=64)
    release_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    channel_id: str
    pipeline_version_id: str
    edge_device_id: str
    capability_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    platform: EdgePlatform
    status: EdgeWorkloadReleaseStatus
    artifact_count: int = Field(ge=0)
    total_artifact_bytes: int = Field(ge=0)
    required_node_types: list[str] = Field(default_factory=list)
    created_at: str
    created_by: str | None = None


class EdgeWorkloadReleaseDetail(EdgeWorkloadReleaseSummary):
    manifest: EdgeDeploymentManifest
    artifacts: list[EdgeArtifactDescriptor] = Field(default_factory=list)
    admission_report: EdgeAdmissionReport


class EdgeWorkloadReleaseListResponse(StrictEdgeModel):
    schema_version: Literal["edge.workload.releases.list.v1"] = "edge.workload.releases.list.v1"
    items: list[EdgeWorkloadReleaseSummary] = Field(default_factory=list)
    meta: ResponseMeta


class EdgeDeploymentAttemptSummary(StrictEdgeModel):
    schema_version: Literal["edge.deployment.attempt.v1"] = "edge.deployment.attempt.v1"
    edge_deployment_id: str = Field(min_length=3, max_length=64)
    channel_id: str
    edge_device_id: str
    workload_release_id: str
    previous_edge_deployment_id: str | None = None
    command_id: str | None = None
    desired_state: Literal["running", "stopped"]
    status: EdgeDeploymentAttemptStatus
    remote_status: EdgeDeploymentStatus | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    last_error: str | None = Field(default=None, max_length=2000)
    created_at: str
    updated_at: str
    activated_at: str | None = None
    finished_at: str | None = None


class EdgeDeploymentAttemptListResponse(StrictEdgeModel):
    schema_version: Literal["edge.deployment.attempts.list.v1"] = "edge.deployment.attempts.list.v1"
    items: list[EdgeDeploymentAttemptSummary] = Field(default_factory=list)
    meta: ResponseMeta


class EdgeDeploymentControllerStatus(StrictEdgeModel):
    schema_version: Literal["edge.deployment.controller.v1"] = "edge.deployment.controller.v1"
    running: bool
    worker_id: str
    interval_seconds: float = Field(gt=0)
    processed_commands: int = Field(ge=0)
    failed_commands: int = Field(ge=0)
    active_command_id: str | None = None
    last_error: str | None = Field(default=None, max_length=2000)


class EdgeOperatorCheck(StrictEdgeModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    category: Literal[
        "placement",
        "connectivity",
        "capability",
        "deployment",
        "runtime",
        "storage",
        "results",
        "slo",
    ]
    outcome: EdgeOperatorCheckOutcome
    message: str = Field(min_length=1, max_length=1000)
    remediation_hint: str | None = Field(default=None, max_length=1000)
    detail: dict[str, Any] = Field(default_factory=dict)


class EdgeDeploymentPreflightRequest(StrictEdgeModel):
    channel_id: str = Field(min_length=3, max_length=64)
    precision: Literal["fp32", "fp16", "int8"] = "fp16"


class EdgeDeploymentPreflightReport(StrictEdgeModel):
    schema_version: Literal["edge.operator.deployment-preflight.v1"] = (
        "edge.operator.deployment-preflight.v1"
    )
    ready: bool
    channel_id: str
    channel_name: str
    edge_device_id: str
    edge_device_name: str
    management_ip: IPv4Address = Field(description="Fixed IPv4 address of the Edge device itself.")
    workload_release_id: str | None = None
    release_digest: str | None = None
    precision: Literal["fp32", "fp16", "int8"]
    artifact_count: int = Field(default=0, ge=0)
    total_artifact_bytes: int = Field(default=0, ge=0)
    current_deployment_id: str | None = None
    rollback_deployment_id: str | None = None
    checks: list[EdgeOperatorCheck] = Field(default_factory=list)
    generated_at: str


class EdgeDeploymentExecuteRequest(StrictEdgeModel):
    channel_id: str = Field(min_length=3, max_length=64)
    workload_release_id: str = Field(min_length=3, max_length=64)
    idempotency_key: str = Field(min_length=3, max_length=160)
    reason: str | None = Field(default=None, max_length=1000)


class EdgeDeploymentWorkflowResponse(StrictEdgeModel):
    schema_version: Literal["edge.operator.deployment-workflow.v1"] = (
        "edge.operator.deployment-workflow.v1"
    )
    channel_id: str
    edge_device_id: str
    workload_release_id: str
    command_id: str
    command: Literal["start", "restart"]
    status: Literal["queued", "processing", "succeeded", "failed"]
    accepted_at: str
    preflight: EdgeDeploymentPreflightReport


class EdgeRollbackRequest(StrictEdgeModel):
    idempotency_key: str = Field(min_length=3, max_length=160)
    reason: str = Field(min_length=3, max_length=1000)


class EdgeRollbackResponse(StrictEdgeModel):
    schema_version: Literal["edge.operator.rollback.v1"] = "edge.operator.rollback.v1"
    command: EdgeFleetCommandSummary
    rolled_back_deployment: EdgeDeploymentAttemptSummary
    restored_deployment: EdgeDeploymentAttemptSummary
    completed_at: str


class EdgeDiagnosticRunRequest(StrictEdgeModel):
    refresh_device: bool = True


class EdgeChannelDiagnostic(StrictEdgeModel):
    channel_id: str
    channel_name: str
    mode: str
    desired_state: str
    observed_state: str
    deployment_id: str | None = None
    workload_release_id: str | None = None
    result_lag: int = Field(default=0, ge=0)
    latency_p95_ms: float | None = Field(default=None, ge=0)
    latency_slo_ms: float = Field(gt=0)
    status: EdgeDiagnosticStatus


class EdgeDiagnosticReport(StrictEdgeModel):
    schema_version: Literal["edge.operator.diagnostic-report.v1"] = (
        "edge.operator.diagnostic-report.v1"
    )
    diagnostic_report_id: str
    edge_device_id: str
    edge_device_name: str
    status: EdgeDiagnosticStatus
    generated_at: str
    created_by: str | None = None
    checks: list[EdgeOperatorCheck] = Field(default_factory=list)
    channels: list[EdgeChannelDiagnostic] = Field(default_factory=list)


class EdgeFleetAlert(StrictEdgeModel):
    alert_id: str
    severity: EdgeAlertSeverity
    edge_device_id: str
    channel_id: str | None = None
    check_key: str
    title: str
    detail: str
    remediation_hint: str | None = None
    observed_at: str


class EdgeFleetHealthOverview(StrictEdgeModel):
    schema_version: Literal["edge.operator.fleet-health.v1"] = "edge.operator.fleet-health.v1"
    generated_at: str
    total_devices: int = Field(ge=0)
    healthy_devices: int = Field(ge=0)
    degraded_devices: int = Field(ge=0)
    critical_devices: int = Field(ge=0)
    evaluated_channels: int = Field(ge=0)
    alerts: list[EdgeFleetAlert] = Field(default_factory=list)
    reports: list[EdgeDiagnosticReport] = Field(default_factory=list)
