export type HealthState = 'ok' | 'degraded' | 'down';
export type QueueName =
  | 'webhook'
  | 'dataset'
  | 'training'
  | 'deploy'
  | 'batch'
  | 'runtime-control'
  | 'runtime-capture'
  | 'runtime-connector'
  | 'maintenance';
export type UserRole =
  | 'platform_admin'
  | 'ml_engineer'
  | 'operator'
  | 'label_manager'
  | 'viewer'
  | 'inference_client';
export type DatasetStatus = 'draft' | 'uploading' | 'ready' | 'snapshotting' | 'snapshotted' | 'failed';
export type LabelProjectStatus =
  | 'discovered'
  | 'mapped'
  | 'active'
  | 'needs_attention'
  | 'archived';
export type SnapshotStatus =
  | 'requested'
  | 'exporting'
  | 'materializing'
  | 'validating'
  | 'ready'
  | 'failed'
  | 'superseded';
export type TaskType =
  | 'classification'
  | 'detection'
  | 'oriented_detection'
  | 'segmentation'
  | 'anomaly'
  | 'temporal_sequence';
export type TrainingJobStatus =
  | 'queued'
  | 'waiting_for_gpu'
  | 'preparing_data'
  | 'validating_snapshot'
  | 'initializing'
  | 'training'
  | 'checkpointing'
  | 'evaluating'
  | 'exporting'
  | 'registering'
  | 'resuming'
  | 'cancel_requested'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'rolled_back_if_needed';
export type ModelLifecycleStatus =
  | 'draft'
  | 'trained'
  | 'validated'
  | 'staged'
  | 'production'
  | 'archived'
  | 'rejected';
export type PromotionStatus =
  | 'staging_requested'
  | 'staged'
  | 'production_requested'
  | 'production'
  | 'rejected'
  | 'retired';
export type DeploymentApprovalScope = 'staging' | 'production';
export type DeploymentApprovalStatus = 'approved' | 'rejected';
export type DeploymentPackageStatus = 'missing' | 'building' | 'ready' | 'failed';
export type DeploymentReleaseStatus =
  | 'requested'
  | 'packaging'
  | 'repository_sync'
  | 'loading'
  | 'smoke_testing'
  | 'activated'
  | 'deactivated'
  | 'activation_failed'
  | 'rollback_requested'
  | 'rolled_back'
  | 'retired';
export type BatchJobStatus = 'accepted' | 'queued' | 'running' | 'completed' | 'failed' | 'canceled';
export type BatchChunkStatus = 'queued' | 'running' | 'completed' | 'failed';
export type ValidationStatus = 'pending' | 'passed' | 'failed';
export type WebhookProcessingStatus =
  | 'accepted'
  | 'processing'
  | 'processed'
  | 'failed'
  | 'duplicate'
  | 'ignored';
export type TargetPath = 'realtime' | 'batch';
export type EndpointMode = 'realtime' | 'batch' | 'both';
export type RuntimeMode = 'realtime' | 'trigger' | 'interval';
export type RuntimeSourceType =
  | 'rtsp'
  | 'usb_opencv'
  | 'aravis_gige'
  | 'flir_blackfly'
  | 'basler_pylon'
  | 'snapshot'
  | 'trigger_capture'
  | 'file_test';
export type RuntimeNodeCategory =
  | 'trigger'
  | 'source'
  | 'preprocessing'
  | 'computer_vision'
  | 'inference'
  | 'analysis'
  | 'capture'
  | 'output';
export type RuntimePipelineStatus = 'draft' | 'validated' | 'published' | 'superseded' | 'archived';
export type RuntimeChannelDesiredState =
  | 'draft'
  | 'stopped'
  | 'start_requested'
  | 'running'
  | 'stop_requested'
  | 'disabled';
export type RuntimeObservedState =
  | 'unassigned'
  | 'starting'
  | 'validating'
  | 'loading_pipeline'
  | 'running'
  | 'degraded'
  | 'stopping'
  | 'stopped'
  | 'failed'
  | 'recovering';
export type RuntimeControlCommand = 'start' | 'stop' | 'restart' | 'reconcile';
export type RuntimeControlCommandStatus = 'queued' | 'processing' | 'succeeded' | 'failed' | 'cancelled';
export type RuntimeResourceStatus = 'draft' | 'active' | 'degraded' | 'disabled' | 'failed';
export type RuntimeTriggerExecutionStatus =
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'timeout'
  | 'rejected';

export interface ResponseMeta {
  request_id?: string | null;
  correlation_id?: string | null;
  generated_at: string;
}

export interface TrainingDefaults {
  epochs: number;
  learning_rate: number;
  image_size?: number | null;
  batch_size?: number | null;
  optimizer?: string | null;
  optimizer_settings: Record<string, unknown>;
  checkpoint_interval_steps: number;
}

export interface ModelArchitectureSummary {
  task_type: TaskType;
  family: string;
  version: string;
  size: string;
  display_name: string;
  internal_key: string;
}

export interface SupportedModelArchitecture {
  architecture: ModelArchitectureSummary;
  supported: boolean;
  support_reason?: string | null;
  trainer_backend: string;
  concrete_architecture: string;
  export_support: boolean;
  deployment_support: boolean;
  inference_support: boolean;
  transfer_learning_policy: string;
  status: 'supported' | 'experimental' | 'hidden';
  status_flags: string[];
  capability_metadata: Record<string, unknown>;
}

export interface ModelTemplateDefaults {
  training: TrainingDefaults;
  promotion?: PromotionCriteriaPolicy | null;
}

export interface CleanupEntityImpact {
  scope_type: string;
  scope_id: string;
  display_name: string;
  count: number;
}

export interface CleanupArtifactImpact {
  path: string;
  category: string;
}

export interface CleanupImpactPreview {
  scope_type: string;
  scope_id: string;
  display_name: string;
  confirmation_token: string;
  blocked: boolean;
  blocked_reasons: string[];
  warnings: string[];
  will_remove: CleanupEntityImpact[];
  will_remain: string[];
  active_deployment_targets: string[];
  artifact_paths: CleanupArtifactImpact[];
}

export interface CleanupExecutionRequest {
  confirmation_token: string;
  reason?: string | null;
  delete_artifacts?: boolean;
  delete_entire_pipeline?: boolean;
}

export interface CleanupExecutionResult {
  scope_type: string;
  scope_id: string;
  already_deleted: boolean;
  deleted_entities: CleanupEntityImpact[];
  retained_entities: string[];
  deleted_artifacts: string[];
  artifact_errors: string[];
  warnings: string[];
}

export interface SessionUser {
  user_id: string;
  username: string;
  display_name?: string | null;
  role: UserRole;
}

export interface SessionResponse {
  authenticated: boolean;
  user?: SessionUser | null;
  csrf_token?: string | null;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export type ApiKeySourceType = 'bootstrap' | 'managed';

export interface UserSummary {
  user_id: string;
  username: string;
  display_name: string;
  description?: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  created_by?: string | null;
  last_login_at?: string | null;
}

export interface UserDetail extends UserSummary {
  password_changed_at?: string | null;
  session_version: number;
}

export interface UserCreateRequest {
  username: string;
  display_name: string;
  description?: string | null;
  password: string;
}

export interface UserUpdateRequest {
  display_name: string;
  description?: string | null;
}

export interface UserPasswordChangeRequest {
  new_password: string;
}

export interface SelfPasswordChangeRequest {
  current_password: string;
  new_password: string;
}

export interface ApiKeyScopeSummary {
  scope: string;
  label: string;
  description: string;
  endpoint_mode?: EndpointMode | null;
  assignable: boolean;
}

export interface ApiKeySummary {
  api_key_id: string;
  key_id: string;
  label: string;
  description?: string | null;
  intended_usage?: string | null;
  subject_type: string;
  subject_id: string;
  scopes: string[];
  endpoint_mode_hint?: EndpointMode | null;
  source_type: ApiKeySourceType;
  masked_key: string;
  secret_prefix?: string | null;
  is_active: boolean;
  is_revoked: boolean;
  created_at: string;
  updated_at: string;
  created_by?: string | null;
  last_used_at?: string | null;
  revoked_at?: string | null;
  revoked_by?: string | null;
  expires_at?: string | null;
  mutable: boolean;
}

export interface ApiKeyDetail extends ApiKeySummary {
  status_reason: string;
}

export interface ApiKeyCreateRequest {
  label: string;
  description?: string | null;
  intended_usage?: string | null;
  subject_name: string;
  subject_type: string;
  scopes: string[];
  expires_at?: string | null;
  endpoint_mode_hint?: EndpointMode | null;
}

export interface ApiKeyCreateResponse {
  api_key: ApiKeyDetail;
  token: string;
  token_preview: string;
  one_time_reveal: boolean;
}

export interface ApiKeyRevokeResponse {
  api_key: ApiKeyDetail;
  revoked: boolean;
}

export type PlcWriteConnectionStatus =
  | 'ready'
  | 'environment_blocked'
  | 'dependency_missing'
  | 'dry_run'
  | 'disabled'
  | 'invalid_config';

export type PlcWriteConnectionReusePolicy = 'per_execution' | 'per_runtime' | 'mixed';
export type PlcWriteConnectionReconnectPolicy = 'disabled' | 'on_failure' | 'mixed';
export type PlcWriteConnectionSafetyStatus = 'ephemeral' | 'bounded' | 'mixed_policy';

export interface PlcWriteAccessPipelineRef {
  pipeline_definition_id: string;
  pipeline_name: string;
  pipeline_version_id: string;
  version_no: number;
  version_scope: 'published' | 'latest';
  mode: string;
  node_key: string;
  node_enabled: boolean;
  dry_run: boolean;
  reuse_policy: PlcWriteConnectionReusePolicy;
  reconnect_policy: PlcWriteConnectionReconnectPolicy;
  mapping_count: number;
  enabled_mapping_count: number;
}

export interface PlcWriteAccessConnectionSummary {
  connection_key: string;
  display_name: string;
  host: string;
  port: number;
  rack: number;
  slot: number;
  connection_ref?: string | null;
  status: PlcWriteConnectionStatus;
  status_reason: string;
  reuse_policy: PlcWriteConnectionReusePolicy;
  reconnect_policy: PlcWriteConnectionReconnectPolicy;
  connection_safety_status: PlcWriteConnectionSafetyStatus;
  connection_safety_reason: string;
  persistent_connection_limit: number;
  node_count: number;
  mapping_count: number;
  enabled_mapping_count: number;
  sample_targets: string[];
  pipelines: PlcWriteAccessPipelineRef[];
}

export interface PlcWriteAccessStatusResponse {
  schema_version: 'access.plc-write-access.v1';
  allow_writes: boolean;
  desired_allow_writes: boolean;
  runtime_env_allow_writes?: boolean | null;
  runtime_env_path?: string | null;
  runtime_env_writable: boolean;
  update_supported: boolean;
  restart_required: boolean;
  dependency_available: boolean;
  connections: PlcWriteAccessConnectionSummary[];
  meta: ResponseMeta;
}

export interface PlcWriteAccessUpdateRequest {
  allow_writes: boolean;
}

export interface ServiceSummary {
  name: string;
  state: HealthState;
  detail: string;
  last_heartbeat_at?: string | null;
}

export interface QueueStatus {
  queue_name: QueueName | string;
  depth: number;
  oldest_job_age_seconds: number;
}

export interface WorkerStatusSummary {
  service_name: string;
  instance_id: string;
  state: HealthState;
  status: string;
  detail: string;
  last_heartbeat_at?: string | null;
  details: Record<string, unknown>;
}

export interface OperationWorkerSummary {
  service_name: string;
  unit_name: string;
  display_name: string;
  state: HealthState;
  status: string;
  detail: string;
  active_state?: string | null;
  sub_state?: string | null;
  main_pid?: number | null;
  restart_count: number;
  last_error?: string | null;
  observed_at: string;
  restartable: boolean;
}

export interface OperationWorkersResponse {
  items: OperationWorkerSummary[];
  meta: ResponseMeta;
}

export interface DiskUsageSummary {
  key: string;
  label: string;
  path: string;
  state: HealthState;
  detail: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  used_percent: number;
  observed_at: string;
}

export interface GpuDeviceSummary {
  device_index: number;
  name?: string | null;
  state: HealthState;
  detail: string;
  utilization_percent?: number | null;
  memory_used_mb?: number | null;
  memory_total_mb?: number | null;
  memory_used_percent?: number | null;
  temperature_c?: number | null;
  observed_at: string;
}

export interface SystemAlert {
  code: string;
  severity: 'info' | 'warning' | 'critical';
  source: string;
  title: string;
  detail: string;
  remediation_hint?: string | null;
  observed_at: string;
}

export interface RuntimePerformanceSnapshot {
  realtime_fps?: number | null;
  realtime_latency_p95_ms?: number | null;
  realtime_drop_rate_percent?: number | null;
  trigger_latency_p95_ms?: number | null;
  trigger_timeout_count: number;
  frame_count: number;
  drop_count: number;
  trigger_count: number;
}

export interface RuntimeChannelOperationalSummary {
  channel_id: string;
  name: string;
  technical_place: string;
  mode: RuntimeMode;
  observed_state: RuntimeObservedState;
  health_state: HealthState;
  health_text: string;
  runtime_instance_id?: string | null;
  executor_service?: string | null;
  assigned_runtime_worker?: string | null;
  last_heartbeat_at?: string | null;
  heartbeat_age_seconds?: number | null;
  recovery_attempts: number;
  recovery_hint?: string | null;
  fps?: number | null;
  latency_p95_ms?: number | null;
  drop_rate_percent?: number | null;
  trigger_latency_ms?: number | null;
  frame_count: number;
  drop_count: number;
  trigger_count: number;
  last_error?: string | null;
}

export interface RuntimeOperationalSummary {
  state: HealthState;
  total_channels: number;
  running_channels: number;
  degraded_channels: number;
  failed_channels: number;
  stopped_channels: number;
  realtime_active_channels: number;
  trigger_active_channels: number;
  active_runtime_instances: number;
  stale_runtime_instances: number;
  failed_runtime_instances: number;
  recovering_channels: number;
  performance: RuntimePerformanceSnapshot;
  attention: RuntimeChannelOperationalSummary[];
  executors: RuntimeChannelOperationalSummary[];
  recovery_hint?: string | null;
  observed_at: string;
}

export interface StatusCard {
  key: string;
  label: string;
  value: string;
  state: HealthState;
}

export interface SystemStatusResponse {
  services: ServiceSummary[];
  dependencies: IntegrationStatusSummary[];
  workers: WorkerStatusSummary[];
  queues: QueueStatus[];
  disks: DiskUsageSummary[];
  gpus: GpuDeviceSummary[];
  runtime: RuntimeOperationalSummary;
  alerts: SystemAlert[];
  cards: StatusCard[];
  meta: ResponseMeta;
}

export type UpdateChangeType = 'added' | 'modified' | 'deleted' | 'renamed' | 'unknown';
export type UpdateRiskLevel = 'low' | 'medium' | 'high' | 'blocked';
export type UpdateActionType = 'none' | 'restart' | 'manual';
export type UpdateApplyStatus = 'accepted' | 'running' | 'succeeded' | 'failed' | 'blocked';

export interface OfflineUpdateChange {
  path: string;
  change_type: UpdateChangeType;
  category: string;
  affected_services: string[];
  restart_required: boolean;
  rebuild_required: boolean;
  migration_required: boolean;
  hot_apply_supported: boolean;
  reason: string;
}

export interface OfflineUpdateServicePlan {
  service_name: string;
  action: UpdateActionType;
  risk: UpdateRiskLevel;
  changed_files: string[];
  safe_to_restart_independently: boolean;
  expected_impact: string;
  required_follow_up: string;
  blockers: string[];
}

export interface OfflineUpdateBundleSummary {
  file_name: string;
  path: string;
  size_bytes: number;
  modified_at: string;
  bundle_type: string;
  app_name?: string | null;
  app_version?: string | null;
  distro_name?: string | null;
  app_host_name?: string | null;
  build_timestamp?: string | null;
  apply_token: string;
  affected_services: string[];
  affected_workers: string[];
  blockers: string[];
  warnings: string[];
}

export interface OfflineUpdatePlanResponse {
  enabled: boolean;
  apply_available: boolean;
  repo_root?: string | null;
  current_git_ref?: string | null;
  current_git_sha?: string | null;
  last_applied_git_sha?: string | null;
  last_applied_at?: string | null;
  pending_change_count: number;
  changed_files: OfflineUpdateChange[];
  service_plans: OfflineUpdateServicePlan[];
  blocked: boolean;
  blockers: string[];
  warnings: string[];
  apply_token?: string | null;
  incoming_dir?: string | null;
  incoming_bundles: OfflineUpdateBundleSummary[];
  selected_bundle?: OfflineUpdateBundleSummary | null;
  meta: ResponseMeta;
}

export interface OfflineUpdateApplyRequest {
  apply_token: string;
  service_names?: string[] | null;
  reason?: string | null;
  force?: boolean;
}

export interface OfflineUpdateApplyResult {
  operation_id: string;
  status: UpdateApplyStatus;
  accepted_at: string;
  service_names: string[];
  changed_files: string[];
  message: string;
  blockers: string[];
  warnings: string[];
  meta: ResponseMeta;
}

export interface OfflineUpdateScheduleRequest {
  apply_token: string;
  scheduled_for: string;
  reason?: string | null;
}

export interface OfflineUpdateScheduleResult {
  operation_id: string;
  status: UpdateApplyStatus;
  scheduled_for: string;
  message: string;
  blockers: string[];
  warnings: string[];
  meta: ResponseMeta;
}

export interface AuditEventSummary {
  event_id: string;
  event_type: string;
  aggregate_type: string;
  aggregate_id: string;
  sequence_no: number;
  source_service: string;
  actor_type: string;
  actor_id: string;
  request_id?: string | null;
  correlation_id?: string | null;
  occurred_at: string;
}

export interface AuditEventDetail extends AuditEventSummary {
  causation_id?: string | null;
  payload_schema_version: number;
  payload: Record<string, unknown>;
}

export interface ListResponse<T> {
  items: T[];
  meta: ResponseMeta;
}

export type EdgeConnectivityState = 'unknown' | 'online' | 'degraded' | 'offline' | 'stale';
export type EdgeHealthState = 'ok' | 'degraded' | 'down';

export interface EdgeDeviceAddress {
  /** Fixed IPv4 address of the Edge device itself. */
  management_ip: string;
  control_port: number;
  plant_api_port: number;
  rtsp_port: number;
}

export interface EdgeDeviceRegistrationRequest {
  name: string;
  technical_place?: string | null;
  address: Pick<EdgeDeviceAddress, 'management_ip'> &
    Partial<Pick<EdgeDeviceAddress, 'control_port' | 'plant_api_port' | 'rtsp_port'>>;
  allowed_cidr: string;
  /** Fixed LAN/VLAN IPv4 address of the central orchestration server. */
  server_ip: string;
  enabled?: boolean;
}

export interface EdgeDeviceUpdateRequest {
  name?: string | null;
  technical_place?: string | null;
  control_port?: number | null;
  plant_api_port?: number | null;
  rtsp_port?: number | null;
  allowed_cidr?: string | null;
  server_ip?: string | null;
  enabled?: boolean | null;
}

export interface EdgeFleetCommandSummary {
  command_id: string;
  edge_device_id: string;
  command_type:
    | 'reconcile'
    | 'blob_upload'
    | 'deployment_prepare'
    | 'deployment_commit'
    | 'deployment_rollback'
    | 'deployment_stop'
    | 'result_ack';
  idempotency_key: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  attempt_count: number;
  request_payload: Record<string, unknown>;
  response_payload: Record<string, unknown>;
  last_error?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface EdgeCapabilitySnapshot {
  schema_version: 'edge.device.capabilities.v1';
  management_ip: string;
  platform: 'jetson_orin_nano' | 'jetson_agx_orin' | 'windows_11' | 'simulated';
  architecture: string;
  os_version: string;
  jetpack_version?: string | null;
  l4t_version?: string | null;
  cuda_version?: string | null;
  tensorrt_version?: string | null;
  deepstream_version?: string | null;
  gpu_model?: string | null;
  gpu_memory_bytes: number;
  ram_total_bytes: number;
  storage_total_bytes: number;
  storage_free_bytes: number;
  agent_version: string;
  runtime_version: string;
  supported_node_types: string[];
  runtime_code_digest?: string | null;
  node_contract_versions?: Record<string, number>;
  software_update_protocol?: number;
  camera_adapters: string[];
  observed_at: string;
}

export interface EdgeTimingSummary {
  key: string;
  node_type?: string | null;
  status?: string | null;
  sample_count: number;
  mean_ms: number;
  p95_ms: number;
  max_ms: number;
  last_ms: number;
}

export interface EdgeRuntimeMetrics {
  observed_at: string;
  window_seconds: number;
  acquired_frames: number;
  processed_frames: number;
  sampled_out_frames: number;
  execution_errors: number;
  source_fps: number;
  processed_fps: number;
  last_frame_at?: string | null;
  last_execution_at?: string | null;
  stages: EdgeTimingSummary[];
  nodes: EdgeTimingSummary[];
}

export interface EdgeRuntimeStatus {
  schema_version: 'edge.runtime.status.v1';
  boot_id: string;
  supervisor_state: 'idle' | 'running' | 'degraded';
  active_slot?: 'A' | 'B' | null;
  previous_slot?: 'A' | 'B' | null;
  slots: Array<{
    name: 'A' | 'B';
    state: 'empty' | 'prepared' | 'active' | 'standby' | 'failed';
    deployment_id?: string | null;
    workload_release_id?: string | null;
    updated_at: string;
  }>;
  processes: Array<{
    deployment_id: string;
    state: 'stopped' | 'starting' | 'running' | 'backoff' | 'crash_loop';
    restart_count: number;
    started_at?: string | null;
    last_heartbeat_at?: string | null;
    next_restart_at?: string | null;
    last_error?: string | null;
    channel_id?: string | null;
    workload_release_id?: string | null;
    metrics?: EdgeRuntimeMetrics | null;
    engines?: Array<{
      node_key: string;
      cache_hit: boolean;
      cache_key?: string | null;
      reason?: string | null;
      duration_ms: number;
      engine_size_bytes: number;
    }>;
  }>;
  storage: {
    artifact_bytes: number;
    artifact_quota_bytes: number;
    outbox_bytes: number;
    outbox_quota_bytes: number;
    outbox_items: number;
    outbox_max_items: number;
    disk_free_bytes: number;
    minimum_disk_free_bytes: number;
    pressure: boolean;
  };
  database_journal_mode: 'wal';
  recovered_deployment_ids: string[];
  observed_at: string;
}

export interface EdgeFleetReconcilerStatus {
  schema_version: 'edge.fleet.reconciler.v1';
  running: boolean;
  interval_seconds: number;
  cycle_count: number;
  last_cycle_started_at?: string | null;
  last_cycle_finished_at?: string | null;
  last_cycle_device_count: number;
  last_cycle_success_count: number;
  last_cycle_failure_count: number;
  last_error?: string | null;
}

export interface EdgeDeviceSummary {
  schema_version: 'edge.fleet.device.v1';
  edge_device_id: string;
  name: string;
  technical_place?: string | null;
  address: EdgeDeviceAddress;
  allowed_cidr: string;
  server_ip: string;
  enabled: boolean;
  connectivity_state: EdgeConnectivityState;
  reported_health_state?: EdgeHealthState | null;
  last_seen_at?: string | null;
  last_poll_attempt_at?: string | null;
  last_connectivity_error?: string | null;
  current_boot_id?: string | null;
  identity_generation: number;
  deployment_revalidation_required: boolean;
  assigned_channel_count: number;
  created_at: string;
  updated_at: string;
}

export interface EdgeSoftwareStatus {
  transfer_bytes?: number;
  transfer_total_bytes?: number;
  protocol_version: number;
  request_id?: string | null;
  phase: 'idle' | 'queued' | 'verifying' | 'installing' | 'activating' | 'checking' | 'succeeded' | 'failed' | 'rolled_back' | 'transferring';
  active_release?: string;
  previous_release?: string | null;
  candidate_release?: string | null;
  runtime_code_digest?: string | null;
  last_error?: string | null;
  updated_at?: string | null;
}

export interface EdgeSoftwarePackage {
  package_sha256: string;
  size_bytes: number;
  release: {
    release_id: string;
    platform: string;
    architecture: string;
    python_version: string;
    cuda_version?: string | null;
    tensorrt_version?: string | null;
    runtime_code_digest: string;
    node_contract_versions: Record<string, number>;
    system_packages?: Record<string, string>;
  };
}

export interface EdgeSoftwareDevice {
  edge_device_id: string;
  runtime_code_digest?: string | null;
  update_protocol: number;
  platform?: string | null;
  runtime_version?: string | null;
  node_count: number;
  requires_update: boolean;
  active_package_sha256?: string | null;
  recommended_package_sha256?: string | null;
  recommendation_reasons?: string[];
  software_observed_at?: string | null;
  releases: Array<{ package_sha256: string; reasons: string[] }>;
  commands: Array<{
    command_id: string;
    status: 'queued' | 'running' | 'succeeded' | 'failed';
    action: 'update' | 'rollback';
    package_sha256?: string | null;
    last_error?: string | null;
    software: EdgeSoftwareStatus;
    created_at: string;
  }>;
}

export interface EdgeSoftwareOverview {
  server_runtime_digest: string;
  central_release_id?: string | null;
  targets_managed?: boolean;
  target_error?: string | null;
  releases: EdgeSoftwarePackage[];
  devices: EdgeSoftwareDevice[];
}

export interface EdgeSoftwareRolloutRequest {
  package_sha256?: string | null;
  action: 'update' | 'rollback';
  idempotency_key: string;
}

export interface EdgeDeviceDetail extends EdgeDeviceSummary {
  identity?: {
    schema_version: 'edge.device.identity.v1';
    address: EdgeDeviceAddress;
    hostname: string;
    platform: EdgeCapabilitySnapshot['platform'];
    architecture: string;
    agent_version: string;
    runtime_version: string;
    boot_id: string;
  } | null;
  health?: {
    schema_version: 'edge.device.health.v1';
    state: EdgeHealthState;
    detail: string;
    boot_id: string;
    observed_at: string;
    active_deployment_ids: string[];
    result_backlog_count: number;
    disk_free_bytes: number;
    runtime?: EdgeRuntimeStatus | null;
  } | null;
  latest_capability_snapshot?: {
    snapshot_id: string;
    digest: string;
    observed_at: string;
    capabilities: EdgeCapabilitySnapshot;
  } | null;
  recent_commands: EdgeFleetCommandSummary[];
  result_cursors: Array<{
    edge_device_id: string;
    management_ip: string;
    boot_id: string;
    last_persisted_sequence: number;
    last_acknowledged_sequence: number;
    updated_at: string;
  }>;
}

export interface EdgeDeviceListResponse {
  schema_version: 'edge.fleet.devices.list.v1';
  items: EdgeDeviceSummary[];
  meta: ResponseMeta;
}

export interface EdgeDeviceReconcileResponse {
  schema_version: 'edge.fleet.reconcile.v1';
  device: EdgeDeviceDetail;
  command: EdgeFleetCommandSummary;
  meta: ResponseMeta;
}

export type EdgeArtifactKind =
  | 'onnx_model'
  | 'label_map'
  | 'pipeline_graph'
  | 'runtime_plugin'
  | 'configuration'
  | 'golden_input'
  | 'other';

export interface EdgeArtifactDescriptor {
  sha256: string;
  size_bytes: number;
  kind: EdgeArtifactKind;
  file_name: string;
  media_type: string;
}

export interface EdgeAdmissionReport {
  schema_version: 'edge.workload.admission.v1';
  status: 'admitted' | 'rejected';
  edge_device_id: string;
  capability_digest: string;
  checked_at: string;
  checks: Array<{
    key: string;
    outcome: 'pass' | 'warning' | 'fail';
    message: string;
    detail: Record<string, unknown>;
  }>;
}

export interface EdgeWorkloadReleaseSummary {
  schema_version: 'edge.workload.release.v1';
  workload_release_id: string;
  release_digest: string;
  channel_id: string;
  pipeline_version_id: string;
  edge_device_id: string;
  capability_digest: string;
  platform: EdgeCapabilitySnapshot['platform'];
  status: 'built' | 'admitted' | 'rejected';
  artifact_count: number;
  total_artifact_bytes: number;
  required_node_types: string[];
  created_at: string;
  created_by?: string | null;
}

export interface EdgeWorkloadReleaseDetail extends EdgeWorkloadReleaseSummary {
  manifest: Record<string, unknown>;
  artifacts: EdgeArtifactDescriptor[];
  admission_report: EdgeAdmissionReport;
}

export interface EdgeWorkloadReleaseListResponse {
  schema_version: 'edge.workload.releases.list.v1';
  items: EdgeWorkloadReleaseSummary[];
  meta: ResponseMeta;
}

export interface EdgeDeploymentAttemptSummary {
  schema_version: 'edge.deployment.attempt.v1';
  edge_deployment_id: string;
  channel_id: string;
  edge_device_id: string;
  workload_release_id: string;
  previous_edge_deployment_id?: string | null;
  command_id?: string | null;
  desired_state: 'running' | 'stopped';
  status:
    | 'queued'
    | 'building'
    | 'admission_failed'
    | 'transferring'
    | 'preparing'
    | 'activating'
    | 'running'
    | 'standby'
    | 'stopping'
    | 'stopped'
    | 'rolling_back'
    | 'rolled_back'
    | 'failed';
  remote_status?: string | null;
  progress: Record<string, unknown>;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
  activated_at?: string | null;
  finished_at?: string | null;
}

export interface EdgeDeploymentAttemptListResponse {
  schema_version: 'edge.deployment.attempts.list.v1';
  items: EdgeDeploymentAttemptSummary[];
  meta: ResponseMeta;
}

export interface EdgeDeploymentControllerStatus {
  schema_version: 'edge.deployment.controller.v1';
  running: boolean;
  worker_id: string;
  interval_seconds: number;
  processed_commands: number;
  failed_commands: number;
  active_command_id?: string | null;
  last_error?: string | null;
}

export type EdgeOperatorCheckOutcome = 'pass' | 'warning' | 'fail';
export type EdgeDiagnosticStatus = 'healthy' | 'degraded' | 'critical';

export interface EdgeOperatorCheck {
  key: string;
  category:
    | 'placement'
    | 'connectivity'
    | 'capability'
    | 'deployment'
    | 'runtime'
    | 'storage'
    | 'results'
    | 'slo';
  outcome: EdgeOperatorCheckOutcome;
  message: string;
  remediation_hint?: string | null;
  detail: Record<string, unknown>;
}

export interface EdgeDeploymentPreflightRequest {
  channel_id: string;
  precision?: 'fp32' | 'fp16' | 'int8';
}

export interface EdgeDeploymentPreflightReport {
  schema_version: 'edge.operator.deployment-preflight.v1';
  ready: boolean;
  channel_id: string;
  channel_name: string;
  edge_device_id: string;
  edge_device_name: string;
  management_ip: string;
  workload_release_id?: string | null;
  release_digest?: string | null;
  precision: 'fp32' | 'fp16' | 'int8';
  artifact_count: number;
  total_artifact_bytes: number;
  current_deployment_id?: string | null;
  rollback_deployment_id?: string | null;
  checks: EdgeOperatorCheck[];
  generated_at: string;
}

export interface EdgeDeploymentExecuteRequest {
  channel_id: string;
  workload_release_id: string;
  idempotency_key: string;
  reason?: string | null;
}

export interface EdgeDeploymentWorkflowResponse {
  schema_version: 'edge.operator.deployment-workflow.v1';
  channel_id: string;
  edge_device_id: string;
  workload_release_id: string;
  command_id: string;
  command: 'start' | 'restart';
  status: 'queued' | 'processing' | 'succeeded' | 'failed';
  accepted_at: string;
  preflight: EdgeDeploymentPreflightReport;
}

export interface EdgeRollbackRequest {
  idempotency_key: string;
  reason: string;
}

export interface EdgeRollbackResponse {
  schema_version: 'edge.operator.rollback.v1';
  command: EdgeFleetCommandSummary;
  rolled_back_deployment: EdgeDeploymentAttemptSummary;
  restored_deployment: EdgeDeploymentAttemptSummary;
  completed_at: string;
}

export interface EdgeChannelDiagnostic {
  channel_id: string;
  channel_name: string;
  mode: string;
  desired_state: string;
  observed_state: string;
  deployment_id?: string | null;
  workload_release_id?: string | null;
  result_lag: number;
  latency_p95_ms?: number | null;
  latency_slo_ms: number;
  status: EdgeDiagnosticStatus;
}

export interface EdgeDiagnosticReport {
  schema_version: 'edge.operator.diagnostic-report.v1';
  diagnostic_report_id: string;
  edge_device_id: string;
  edge_device_name: string;
  status: EdgeDiagnosticStatus;
  generated_at: string;
  created_by?: string | null;
  checks: EdgeOperatorCheck[];
  channels: EdgeChannelDiagnostic[];
}

export interface EdgeFleetAlert {
  alert_id: string;
  severity: 'info' | 'warning' | 'critical';
  edge_device_id: string;
  channel_id?: string | null;
  check_key: string;
  title: string;
  detail: string;
  remediation_hint?: string | null;
  observed_at: string;
}

export interface EdgeFleetHealthOverview {
  schema_version: 'edge.operator.fleet-health.v1';
  generated_at: string;
  total_devices: number;
  healthy_devices: number;
  degraded_devices: number;
  critical_devices: number;
  evaluated_channels: number;
  alerts: EdgeFleetAlert[];
  reports: EdgeDiagnosticReport[];
}

export interface EdgeViewerLeaseRequest {
  lease_id?: string | null;
  ttl_seconds?: number;
  max_fps?: number;
  max_width?: number;
  include_overlay?: boolean;
}

export interface EdgeViewerLeaseSummary {
  schema_version: 'edge.viewer.lease.v1';
  lease_id: string;
  channel_id: string;
  edge_device_id: string;
  status: 'active' | 'released' | 'expired';
  expires_at: string;
  max_fps: number;
  max_width: number;
  include_overlay: boolean;
  created_at: string;
  updated_at: string;
}

export interface EdgeResultSyncDeviceStatus {
  edge_device_id: string;
  last_cycle_at?: string | null;
  last_success_at?: string | null;
  persisted_results: number;
  acknowledged_results: number;
  preview_frames: number;
  last_error?: string | null;
}

export interface EdgeResultSyncStatus {
  schema_version: 'edge.result-sync.status.v1';
  running: boolean;
  interval_seconds: number;
  batch_limit: number;
  cycle_count: number;
  persisted_results: number;
  acknowledged_results: number;
  preview_frames: number;
  last_cycle_started_at?: string | null;
  last_cycle_finished_at?: string | null;
  last_error?: string | null;
  devices: EdgeResultSyncDeviceStatus[];
}

export interface RuntimeChannelCaptureSummary {
  summary: string;
  rule_count: number;
  target_project_count: number;
  modes: string[];
}

export interface RuntimeChannelOutputSummary {
  summary: string;
  connector_count: number;
  primary_outputs: string[];
}

export interface RuntimeResultHistoryPolicy {
  enabled: boolean;
  retention_hours: number;
  sample_interval_seconds: number;
  max_records: number;
}

export interface RuntimeChannelPlacement {
  target: 'server' | 'edge_device';
  edge_device_id?: string | null;
  edge_device_name?: string | null;
  edge_management_ip?: string | null;
  edge_connectivity_state?: EdgeConnectivityState | null;
  workload_release_id?: string | null;
  edge_deployment_id?: string | null;
  edge_deployment_status?: EdgeDeploymentAttemptSummary['status'] | null;
  edge_admission_status?: 'admitted' | 'rejected' | null;
}

export interface RuntimeChannelHealthSummary {
  state: HealthState;
  status_text: string;
  last_error?: string | null;
  last_heartbeat_at?: string | null;
  recovery_hint?: string | null;
}

export interface RuntimeChannelPerformanceSummary {
  fps?: number | null;
  source_fps?: number | null;
  processed_fps?: number | null;
  latency_ms?: number | null;
  latency_p50_ms?: number | null;
  latency_p95_ms?: number | null;
  latency_p99_ms?: number | null;
  drop_rate_percent?: number | null;
  frame_count: number;
  drop_count: number;
  trigger_count: number;
  reconnect_count: number;
  source_error_count: number;
  last_frame_at?: string | null;
  last_result_status?: string | null;
}

export interface RuntimeChannelRuntimeState {
  channel_id: string;
  runtime_instance_id?: string | null;
  execution_mode: RuntimeMode;
  desired_state: RuntimeChannelDesiredState;
  actual_state: RuntimeObservedState;
  loading_state: string;
  last_heartbeat_at?: string | null;
  last_error?: string | null;
  last_start_at?: string | null;
  last_stop_at?: string | null;
  assigned_pipeline_version_id?: string | null;
  assigned_runtime_worker?: string | null;
  heartbeat_age_seconds?: number | null;
  recovery_attempts: number;
  recovery_hint?: string | null;
  health_summary: Record<string, unknown>;
  metrics_summary: Record<string, unknown>;
}

export interface RuntimeChannelTriggerablePipelineLink {
  trigger_endpoint_id: string;
  name: string;
  description?: string | null;
  slug: string;
  trigger_type: 'http_put';
  method: 'PUT';
  path: string;
  enabled: boolean;
  auth_required: boolean;
  auth_scope?: string | null;
  status: RuntimeResourceStatus;
  pipeline_version_id?: string | null;
  pipeline_label?: string | null;
  last_invoked_at?: string | null;
  last_status?: string | null;
  last_latency_ms?: number | null;
}

export interface RuntimeChannelRequestEndpoint {
  kind: 'trigger_execution' | 'latest_result';
  label: string;
  description?: string | null;
  method: 'GET' | 'PUT';
  path: string;
  available: boolean;
  unavailable_reason?: string | null;
  auth_required: boolean;
  auth_label: string;
  trigger_endpoint_id?: string | null;
  pipeline_version_id?: string | null;
  pipeline_label?: string | null;
  status?: RuntimeResourceStatus | null;
  last_activity_at?: string | null;
}

export type StemRotationPrioritySource = 'FRONTEND' | 'PLC';
export type StemRotationPriorityMode =
  | 'NONE'
  | 'VORSCHNITT_KRITISCH'
  | 'NACHSCHNITT_KRITISCH';

export interface RuntimeStemRotationPriorityControl {
  schema_version: 'runtime.stem-rotation-priority-control.v1';
  priority_source: StemRotationPrioritySource;
  priority_mode: StemRotationPriorityMode;
  optimization_enable: boolean;
  revision: number;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface RuntimeChannelSummary {
  channel_id: string;
  slug: string;
  name: string;
  facility_id?: string | null;
  technical_place: string;
  mode: RuntimeMode;
  source_type: RuntimeSourceType;
  source_summary: string;
  assigned_pipeline?: string | null;
  output_summary: RuntimeChannelOutputSummary;
  capture_summary: RuntimeChannelCaptureSummary;
  health: RuntimeChannelHealthSummary;
  performance: RuntimeChannelPerformanceSummary;
  runtime_state?: RuntimeChannelRuntimeState | null;
  source_id?: string | null;
  pipeline_version_id?: string | null;
  authoring_label_project_ids: string[];
  result_history_policy: RuntimeResultHistoryPolicy;
  stem_rotation_priority_control: RuntimeStemRotationPriorityControl;
  placement: RuntimeChannelPlacement;
  desired_state: RuntimeChannelDesiredState;
  observed_state: RuntimeObservedState;
  status: RuntimeObservedState;
  health_state: HealthState;
  last_activity_at?: string | null;
  last_error?: string | null;
}

export interface RuntimeChannelDetail extends RuntimeChannelSummary {
  description?: string | null;
  source_config: Record<string, unknown>;
  triggerable_pipelines: RuntimeChannelTriggerablePipelineLink[];
  request_endpoints: RuntimeChannelRequestEndpoint[];
  created_at: string;
  updated_at?: string | null;
}

export interface RuntimeChannelAuthoringLabelProject {
  label_project_id: string;
  label_studio_project_id: number;
  title: string;
  task_type: TaskType;
  dataset_name?: string | null;
  labels: string[];
}

export interface RuntimeChannelAuthoringContext {
  channel: RuntimeChannelDetail;
  source_config: Record<string, unknown>;
  source_metadata: Record<string, unknown>;
  label_projects: RuntimeChannelAuthoringLabelProject[];
  labels: string[];
  pipeline_definition_id?: string | null;
  pipeline_version_id?: string | null;
}

export interface RuntimeChannelAuthoringContextResponse {
  schema_version: 'runtime.channel-authoring.context.v1';
  context: RuntimeChannelAuthoringContext;
  meta: ResponseMeta;
}

export interface RuntimeChannelPipelineInstanceRequest {
  template_definition_id?: string | null;
  template_version_id?: string | null;
  name?: string | null;
  replace_existing?: boolean;
}

export interface RuntimeChannelCreateRequest {
  slug?: string | null;
  name: string;
  facility_id?: string | null;
  technical_place: string;
  description?: string | null;
  source_type: RuntimeSourceType;
  source_config: Record<string, unknown>;
  source_credentials?: RuntimeSourceCredentialInput | null;
  mode?: RuntimeMode;
  assigned_pipeline?: string | null;
  output_summary?: RuntimeChannelOutputSummary;
  capture_summary?: RuntimeChannelCaptureSummary;
  source_id?: string | null;
  pipeline_version_id?: string | null;
  authoring_label_project_ids?: string[];
  result_history_policy?: RuntimeResultHistoryPolicy;
  stem_rotation_priority_source?: StemRotationPrioritySource;
  stem_rotation_priority_mode?: StemRotationPriorityMode;
  stem_rotation_optimization_enable?: boolean;
  placement?: Pick<RuntimeChannelPlacement, 'target' | 'edge_device_id'>;
}

export interface RuntimeChannelUpdateRequest {
  name?: string | null;
  facility_id?: string | null;
  technical_place?: string | null;
  description?: string | null;
  source_type?: RuntimeSourceType | null;
  source_config?: Record<string, unknown> | null;
  source_credentials?: RuntimeSourceCredentialInput | null;
  mode?: RuntimeMode | null;
  assigned_pipeline?: string | null;
  output_summary?: RuntimeChannelOutputSummary | null;
  capture_summary?: RuntimeChannelCaptureSummary | null;
  source_id?: string | null;
  pipeline_version_id?: string | null;
  authoring_label_project_ids?: string[] | null;
  result_history_policy?: RuntimeResultHistoryPolicy | null;
  stem_rotation_priority_source?: StemRotationPrioritySource | null;
  stem_rotation_priority_mode?: StemRotationPriorityMode | null;
  stem_rotation_optimization_enable?: boolean | null;
  desired_state?: RuntimeChannelDesiredState | null;
  placement?: Pick<RuntimeChannelPlacement, 'target' | 'edge_device_id'> | null;
}

export interface RuntimeSourceCredentialInput {
  username?: string | null;
  password?: string | null;
}

export interface RuntimeChannelListResponse {
  schema_version: 'runtime.channels.list.v1';
  items: RuntimeChannelSummary[];
  meta: ResponseMeta;
}

export interface RuntimeChannelStatusCounts {
  total: number;
  running: number;
  stopped: number;
  degraded: number;
  failed: number;
  draft: number;
  start_requested: number;
  stop_requested: number;
}

export interface RuntimeChannelStatusOverview {
  schema_version: 'runtime.channels.status-overview.v1';
  counts: RuntimeChannelStatusCounts;
  health: RuntimeChannelHealthSummary[];
  technical_places: string[];
  source_types: RuntimeSourceType[];
  modes: RuntimeMode[];
  meta: ResponseMeta;
}

export interface RuntimeChannelTestFileUploadResponse {
  schema_version: 'runtime.channel-test-file.upload.v1';
  file_name: string;
  content_type: string;
  size_bytes: number;
  path: string;
  source_config: Record<string, unknown>;
  meta: ResponseMeta;
}

export interface RuntimeChannelCommandRequest {
  reason?: string | null;
  idempotency_key?: string | null;
  force?: boolean;
}

export interface RuntimeControlCommandSummary {
  job_id: string;
  command_id: string;
  channel_id: string;
  command: RuntimeControlCommand;
  queue: 'runtime-control';
  status: RuntimeControlCommandStatus;
  accepted_at: string;
  claimed_at?: string | null;
  consumed_at?: string | null;
  finished_at?: string | null;
  runtime_instance_id?: string | null;
  last_error?: string | null;
}

export interface RuntimeChannelCommandResponse {
  schema_version: 'runtime.channel.command.v1';
  channel: RuntimeChannelSummary;
  accepted_job: RuntimeControlCommandSummary;
  meta: ResponseMeta;
}

export interface RuntimeLiveFrame {
  image_base64?: string | null;
  mime_type: string;
  width?: number | null;
  height?: number | null;
  source_timestamp?: string | null;
  captured_at?: string | null;
  sequence: number;
  image_context?: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface RuntimeCameraCalibrationRoi {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface RuntimeCameraCalibrationBoardDefinition {
  board_type: 'checkerboard' | 'charuco';
  columns: number;
  rows: number;
  square_length: number;
  marker_length?: number | null;
  dictionary_name: string;
}

export interface RuntimeCameraIntrinsicCalibrationProfile {
  schema_version: 'camera.intrinsic-calibration.v1';
  status: 'valid' | 'invalid';
  source_channel_id: string;
  image_width: number;
  image_height: number;
  distortion_model: 'opencv_standard';
  calibration_method?: 'CHECKERBOARD_OR_CHARUCO' | 'IDENTITY_PASSTHROUGH';
  camera_matrix: number[];
  output_camera_matrix: number[];
  distortion_coefficients: number[];
  rms_reprojection_error_px: number;
  per_observation_reprojection_error_px: Record<string, number>;
  accepted_observation_count: number;
  board_definition: RuntimeCameraCalibrationBoardDefinition;
  valid_roi: RuntimeCameraCalibrationRoi;
  generated_at: string;
  calibration_hash: string;
  perspective_rectified: false;
}

export interface RuntimeNormalizedImagePoint {
  x: number;
  y: number;
}

export interface RuntimeImageViewProfileProvenance {
  method: 'MANUAL_QUADRILATERAL';
  created_at: string;
  operator_user_id?: string | null;
  source_frame_timestamp?: string | null;
  source_frame_sha256?: string | null;
  pipeline_version_id?: string | null;
  notes: string;
}

export interface RuntimeImageViewProfile {
  schema_version: 'image.view-profile.v1';
  view_profile_id: string;
  status: 'valid' | 'invalid';
  source_space: 'UNDISTORTED_IMAGE';
  source_channel_id: string;
  source_image_width: number;
  source_image_height: number;
  intrinsic_calibration_hash: string;
  source_quadrilateral: RuntimeNormalizedImagePoint[];
  output_width: number;
  output_height: number;
  transform_type: 'PERSPECTIVE_RECTIFY' | 'POLYGON_MASKED_CROP';
  interpolation: 'NEAREST' | 'LINEAR' | 'CUBIC';
  padding_mode: 'CONSTANT';
  padding_value: number;
  enabled: boolean;
  source_to_view_homography: number[];
  view_to_source_homography: number[];
  profile_checksum: string;
  validation_status: 'VALID' | 'INVALID';
  validated_at: string;
  provenance: RuntimeImageViewProfileProvenance;
  metadata: Record<string, unknown>;
}

export interface RuntimeImageViewProfileCalculateRequest {
  view_profile_id: string;
  source_image_width: number;
  source_image_height: number;
  intrinsic_calibration_hash: string;
  source_quadrilateral: RuntimeNormalizedImagePoint[];
  output_width: number;
  output_height: number;
  transform_type?: 'PERSPECTIVE_RECTIFY' | 'POLYGON_MASKED_CROP';
  interpolation?: 'NEAREST' | 'LINEAR' | 'CUBIC';
  padding_mode?: 'CONSTANT';
  padding_value?: number;
  enabled?: boolean;
  source_frame_timestamp?: string | null;
  source_frame_sha256?: string | null;
  pipeline_version_id?: string | null;
  notes?: string;
  metadata?: Record<string, unknown>;
}

export interface RuntimeImageViewProfileCalculateResponse {
  schema_version: 'runtime.image-view-profile.calculate.v1';
  status: 'valid' | 'invalid';
  profile?: RuntimeImageViewProfile | null;
  failure_code?: string | null;
  failure_message?: string | null;
  meta: ResponseMeta;
}

export interface RuntimeStemCurvatureImagePoint {
  x_px: number;
  y_px: number;
}

export interface RuntimeStemCurvatureProcessPoint {
  x_mm: number;
  y_mm: number;
}

export interface RuntimeStemCurvatureProcessReferencePoint {
  point_id: string;
  image_point: RuntimeStemCurvatureImagePoint;
  process_point: RuntimeStemCurvatureProcessPoint;
}

export interface RuntimeStemCurvatureImagePolygon {
  points: RuntimeStemCurvatureImagePoint[];
}

export interface RuntimeStemCurvatureProcessPolygon {
  image_points: RuntimeStemCurvatureImagePoint[];
  process_points: RuntimeStemCurvatureProcessPoint[];
}

export interface RuntimeStemCurvatureImageGate {
  start: RuntimeStemCurvatureImagePoint;
  end: RuntimeStemCurvatureImagePoint;
}

export interface RuntimeStemCurvatureProcessGate {
  image_start: RuntimeStemCurvatureImagePoint;
  image_end: RuntimeStemCurvatureImagePoint;
  process_start: RuntimeStemCurvatureProcessPoint;
  process_end: RuntimeStemCurvatureProcessPoint;
}

export interface RuntimeStemCurvatureProcessBounds {
  x_min_mm: number;
  x_max_mm: number;
  y_min_mm: number;
  y_max_mm: number;
}

export interface RuntimeStemCurvatureCoordinateFrameDefinition {
  method: 'ORTHOGONAL_XY_AXIS_ASSISTANT';
  origin_image_point: RuntimeStemCurvatureImagePoint;
  x_axis_image_endpoint: RuntimeStemCurvatureImagePoint;
  x_length_mm: number;
  y_axis_image_endpoint: RuntimeStemCurvatureImagePoint;
  y_length_mm: number;
  xy_corner_image_point: RuntimeStemCurvatureImagePoint;
  z_axis_image_endpoint: RuntimeStemCurvatureImagePoint;
  z_direction_semantics: 'UP_FROM_REFERENCE_PLANE' | 'DOWN_TOWARD_MACHINE_BASE';
  z_reference_length_mm?: number | null;
}

export interface RuntimeStemCurvatureReferencePlane {
  plane_id: string;
  z_mm: number;
  z_positive_direction: 'TOWARD_CAMERA' | 'AWAY_FROM_CAMERA';
  x_axis_description: string;
  y_axis_description: string;
  z_axis_description: string;
  description: string;
}

export interface RuntimeStemCurvatureProcessCalibrationProvenance {
  method: 'MANUAL_REFERENCE_POINT_HOMOGRAPHY' | 'CALIBRATED_XY_Z_PNP_V1';
  created_at: string;
  operator_user_id?: string | null;
  reference_frame_timestamp?: string | null;
  reference_frame_sha256?: string | null;
  notes: string;
}

export interface RuntimeStemSpatialCalibration {
  method: 'CALIBRATED_XY_Z_PNP_V1';
  camera_matrix: number[];
  rotation_world_to_camera: number[];
  translation_world_to_camera_mm: number[];
  camera_center_mm: number[];
  projection_matrix: number[];
  reference_point_count: number;
  rms_reprojection_error_px: number;
  maximum_reprojection_error_px: number;
  z_reprojection_error_px: number;
  z_reference_displacement_px: number;
  pose_condition_number: number;
}

export interface RuntimeStemCurvatureProcessCalibrationProfile {
  schema_version: 'stem.curvature-process-calibration.v1';
  calibration_id: string;
  status: 'valid' | 'invalid';
  source_channel_id: string;
  image_width: number;
  image_height: number;
  intrinsic_calibration_hash: string;
  coordinate_unit: 'mm';
  reference_points: RuntimeStemCurvatureProcessReferencePoint[];
  coordinate_frame_definition?: RuntimeStemCurvatureCoordinateFrameDefinition | null;
  spatial_calibration?: RuntimeStemSpatialCalibration | null;
  measurement_geometry_mode?: 'EXPLICIT_GEOMETRY_V1' | 'CANONICAL_VIEW_CORRIDOR_V1';
  canonical_view_profile_id?: string | null;
  canonical_view_profile_checksum?: string | null;
  homography_image_to_process: number[];
  homography_process_to_image: number[];
  process_bounds: RuntimeStemCurvatureProcessBounds;
  reference_plane: RuntimeStemCurvatureReferencePlane;
  candidate_roi: RuntimeStemCurvatureProcessPolygon;
  curvature_observation_roi: RuntimeStemCurvatureProcessPolygon;
  surface_tracking_roi: RuntimeStemCurvatureProcessPolygon;
  session_start_gate: RuntimeStemCurvatureProcessGate;
  curvature_freeze_gate: RuntimeStemCurvatureProcessGate;
  decision_gate: RuntimeStemCurvatureProcessGate;
  decision_margin_mm: number;
  conveyor_direction: 'POSITIVE_X' | 'NEGATIVE_X';
  rotation_reference_view:
    | 'LOOKING_IN_CONVEYOR_DIRECTION'
    | 'LOOKING_AGAINST_CONVEYOR_DIRECTION';
  positive_rotation: 'CLOCKWISE' | 'COUNTER_CLOCKWISE';
  diameter_class?: string | null;
  height_class?: string | null;
  rms_reprojection_error_px: number;
  maximum_reprojection_error_px: number;
  maximum_allowed_reprojection_error_px: number;
  provenance: RuntimeStemCurvatureProcessCalibrationProvenance;
  calibration_hash: string;
  perspective_rectified: true;
}

export interface RuntimeStemCurvatureProcessCalibrationCalculateRequest {
  calibration_id: string;
  image_width: number;
  image_height: number;
  intrinsic_calibration_hash: string;
  reference_points: RuntimeStemCurvatureProcessReferencePoint[];
  coordinate_frame_definition?: RuntimeStemCurvatureCoordinateFrameDefinition | null;
  spatial_calibration_enabled?: boolean;
  intrinsic_calibration?: RuntimeCameraIntrinsicCalibrationProfile | null;
  measurement_geometry_mode?: 'EXPLICIT_GEOMETRY_V1' | 'CANONICAL_VIEW_CORRIDOR_V1';
  canonical_view_profile_id?: string | null;
  canonical_view_profile_checksum?: string | null;
  reference_plane: RuntimeStemCurvatureReferencePlane;
  candidate_roi: RuntimeStemCurvatureImagePolygon;
  curvature_observation_roi: RuntimeStemCurvatureImagePolygon;
  surface_tracking_roi: RuntimeStemCurvatureImagePolygon;
  session_start_gate: RuntimeStemCurvatureImageGate;
  curvature_freeze_gate: RuntimeStemCurvatureImageGate;
  decision_gate: RuntimeStemCurvatureImageGate;
  decision_margin_mm?: number;
  conveyor_direction: 'POSITIVE_X' | 'NEGATIVE_X';
  rotation_reference_view:
    | 'LOOKING_IN_CONVEYOR_DIRECTION'
    | 'LOOKING_AGAINST_CONVEYOR_DIRECTION';
  positive_rotation: 'CLOCKWISE' | 'COUNTER_CLOCKWISE';
  diameter_class?: string | null;
  height_class?: string | null;
  maximum_rms_reprojection_error_px?: number;
  reference_frame_timestamp?: string | null;
  reference_frame_sha256?: string | null;
  notes?: string;
}

export interface RuntimeStemCurvatureProcessCalibrationCalculateResponse {
  schema_version: 'runtime.stem-process-calibration.calculate.v1';
  status: 'valid' | 'invalid';
  profile?: RuntimeStemCurvatureProcessCalibrationProfile | null;
  rms_reprojection_error_px?: number | null;
  maximum_reprojection_error_px?: number | null;
  failure_code?: string | null;
  failure_message?: string | null;
  meta: ResponseMeta;
}

export interface RuntimeStemSurfacePhaseCalibrationObservation {
  observation_id: string;
  raw_phase: number;
  known_angle_deg: number;
  actual_marking?: string;
  diameter_mm: number;
  image_center_x_px: number;
  image_center_y_px: number;
  weight: number;
  calculated_phase_deg?: number | null;
  error_deg?: number | null;
  accepted?: boolean | null;
}

export interface RuntimeStemSurfacePhaseCalibrationProvenance {
  method: 'MARKED_TEST_STEM' | 'REFERENCE_CYLINDER';
  created_at: string;
  operator_user_id?: string | null;
  reference_asset_id?: string | null;
  notes: string;
}

export interface RuntimeStemSurfacePhaseCalibrationProfile {
  schema_version: 'stem.surface-phase-calibration.v1';
  calibration_id: string;
  calibration_version: string;
  status: 'valid' | 'invalid';
  source_channel_id: string;
  image_width: number;
  image_height: number;
  intrinsic_calibration_hash: string;
  process_calibration_hash: string;
  rotation_reference_view:
    | 'LOOKING_IN_CONVEYOR_DIRECTION'
    | 'LOOKING_AGAINST_CONVEYOR_DIRECTION';
  positive_rotation: 'CLOCKWISE' | 'COUNTER_CLOCKWISE';
  phase_estimator_version?: 'LK_KEYFRAME_RELOCK_V1' | 'CYLINDRICAL_SURFACE_V2';
  fit_model: 'LINEAR_THROUGH_ORIGIN';
  degrees_per_raw_phase?: number | null;
  observations: RuntimeStemSurfacePhaseCalibrationObservation[];
  accepted_observation_count: number;
  mean_abs_error_deg?: number | null;
  rms_error_deg?: number | null;
  maximum_abs_error_deg?: number | null;
  maximum_mean_abs_error_deg: number;
  maximum_abs_error_limit_deg: number;
  requested_valid_diameter_min_mm?: number | null;
  requested_valid_diameter_max_mm?: number | null;
  diameter_tolerance_ratio?: number;
  image_center_tolerance_x_ratio?: number;
  image_center_tolerance_y_ratio?: number;
  valid_diameter_min_mm: number;
  valid_diameter_max_mm: number;
  valid_image_center_x_min_px: number;
  valid_image_center_x_max_px: number;
  valid_image_center_y_min_px: number;
  valid_image_center_y_max_px: number;
  provenance: RuntimeStemSurfacePhaseCalibrationProvenance;
  calibration_hash: string;
}

export interface RuntimeStemSurfacePhaseCalibrationCalculateRequest {
  calibration_id: string;
  calibration_version: string;
  image_width: number;
  image_height: number;
  intrinsic_calibration_hash: string;
  process_calibration_hash: string;
  rotation_reference_view:
    | 'LOOKING_IN_CONVEYOR_DIRECTION'
    | 'LOOKING_AGAINST_CONVEYOR_DIRECTION';
  positive_rotation: 'CLOCKWISE' | 'COUNTER_CLOCKWISE';
  phase_estimator_version?: 'LK_KEYFRAME_RELOCK_V1' | 'CYLINDRICAL_SURFACE_V2';
  method: 'MARKED_TEST_STEM' | 'REFERENCE_CYLINDER';
  observations: RuntimeStemSurfacePhaseCalibrationObservation[];
  minimum_angle_span_deg?: number;
  maximum_mean_abs_error_deg?: number;
  maximum_abs_error_deg?: number;
  valid_diameter_min_mm?: number | null;
  valid_diameter_max_mm?: number | null;
  diameter_tolerance_ratio?: number;
  image_center_tolerance_x_ratio?: number;
  image_center_tolerance_y_ratio?: number;
  operator_user_id?: string | null;
  reference_asset_id?: string | null;
  notes?: string;
}

export interface RuntimeStemSurfacePhaseCalibrationCalculateResponse {
  schema_version: 'runtime.stem-surface-phase-calibration.calculate.v1';
  status: 'valid' | 'invalid';
  profile?: RuntimeStemSurfacePhaseCalibrationProfile | null;
  mean_abs_error_deg?: number | null;
  rms_error_deg?: number | null;
  maximum_abs_error_deg?: number | null;
  failure_code?: string | null;
  failure_message?: string | null;
  meta: ResponseMeta;
}

export type RuntimeStemMinimumPrincipleReferenceSetup =
  | 'STRAIGHT_REFERENCE_CYLINDER'
  | 'CURVED_REFERENCE_BODY'
  | 'MARKED_TEST_STEM';

export interface RuntimeStemMinimumPrincipleValidationObservation {
  observation_id: string;
  reference_setup: RuntimeStemMinimumPrincipleReferenceSetup;
  reference_body_id: string;
  run_id: string;
  actual_marking: string;
  known_angle_deg?: number | null;
  calculated_phase_deg?: number | null;
  expected_target_phase_deg?: number | null;
  calculated_target_phase_deg?: number | null;
  expected_correction_deg?: number | null;
  calculated_correction_deg?: number | null;
  decision: 'ROTATE' | 'NO_ROTATION' | 'UNCERTAIN';
  observed_phase_span_deg: number;
  diameter_mm: number;
  diameter_class: string;
  image_center_x_px: number;
  image_center_y_px: number;
  known_x_mm?: number | null;
  known_y_mm?: number | null;
  measured_x_mm?: number | null;
  measured_y_mm?: number | null;
  expected_curvature_min_mm?: number | null;
  expected_curvature_max_mm?: number | null;
  measured_curvature_min_mm?: number | null;
  measured_curvature_max_mm?: number | null;
  translation_distance_mm: number;
  relock_attempted: boolean;
  relock_succeeded: boolean;
  confidence: number;
  reason_codes: string[];
}

export interface RuntimeStemMinimumPrincipleValidationThresholds {
  minimum_total_observations: number;
  minimum_observations_per_setup: number;
  minimum_valid_angle_observations: number;
  maximum_median_angle_error_deg: number;
  maximum_p95_angle_error_deg: number;
  maximum_sign_error_count: number;
  maximum_target_phase_repeatability_deg: number;
  maximum_homography_error_mm: number;
  maximum_straight_curvature_mm: number;
  maximum_translation_phase_error_deg: number;
  maximum_curvature_abs_error_mm: number;
  maximum_curvature_repeatability_mm: number;
  minimum_relock_success_rate: number;
  maximum_uncertainty_rate: number;
}

export interface RuntimeStemMinimumPrincipleValidationEvaluation
  extends RuntimeStemMinimumPrincipleValidationObservation {
  angle_error_deg?: number | null;
  phase_error_deg_signed?: number | null;
  target_phase_error_deg_signed?: number | null;
  correction_error_deg_signed?: number | null;
  sign_error: boolean;
  homography_error_mm?: number | null;
  curvature_min_error_mm?: number | null;
  curvature_max_error_mm?: number | null;
  image_position_bucket: string;
  phase_span_bucket: string;
}

export interface RuntimeStemMinimumPrincipleValidationMetricGroup {
  group_key: string;
  observation_count: number;
  valid_angle_count: number;
  median_angle_error_deg?: number | null;
  p90_angle_error_deg?: number | null;
  p95_angle_error_deg?: number | null;
  uncertainty_rate: number;
}

export interface RuntimeStemMinimumPrincipleValidationMetrics {
  total_observation_count: number;
  valid_angle_observation_count: number;
  median_angle_error_deg?: number | null;
  p90_angle_error_deg?: number | null;
  p95_angle_error_deg?: number | null;
  sign_error_count: number;
  sign_error_rate: number;
  homography_error_median_mm?: number | null;
  homography_error_p95_mm?: number | null;
  straight_curvature_p95_mm?: number | null;
  translation_phase_error_p95_deg?: number | null;
  curvature_min_abs_error_p95_mm?: number | null;
  curvature_max_abs_error_p95_mm?: number | null;
  curvature_min_repeatability_p95_mm?: number | null;
  curvature_max_repeatability_p95_mm?: number | null;
  target_phase_repeatability_p95_deg?: number | null;
  relock_attempt_count: number;
  relock_success_count: number;
  relock_success_rate?: number | null;
  uncertainty_count: number;
  uncertainty_rate: number;
  by_phase_span: RuntimeStemMinimumPrincipleValidationMetricGroup[];
  by_diameter: RuntimeStemMinimumPrincipleValidationMetricGroup[];
  by_image_position: RuntimeStemMinimumPrincipleValidationMetricGroup[];
}

export interface RuntimeStemMinimumPrincipleValidationCheck {
  check_id: string;
  status: 'PASS' | 'FAIL' | 'INSUFFICIENT_DATA';
  sample_count: number;
  value?: number | string | null;
  comparison: '<=' | '>=' | '==' | 'present';
  limit?: number | string | null;
  message: string;
}

export interface RuntimeStemMinimumPrincipleValidationArtifact {
  schema_version: 'stem.minimum-principle-validation.v1';
  artifact_id: string;
  artifact_version: string;
  status: 'VALIDATION_PASSED' | 'VALIDATION_FAILED' | 'INSUFFICIENT_DATA';
  source_channel_id: string;
  image_width: number;
  image_height: number;
  process_calibration_id: string;
  process_calibration_hash: string;
  phase_calibration_id: string;
  phase_calibration_version: string;
  phase_calibration_hash: string;
  algorithm: 'CURVATURE_PHASE_MINIMUM';
  algorithm_version: string;
  thresholds: RuntimeStemMinimumPrincipleValidationThresholds;
  observations: RuntimeStemMinimumPrincipleValidationEvaluation[];
  metrics: RuntimeStemMinimumPrincipleValidationMetrics;
  checks: RuntimeStemMinimumPrincipleValidationCheck[];
  reason_codes: string[];
  provenance: {
    method: 'OBJECTIVE_REFERENCE_BODY_VALIDATION';
    created_at: string;
    operator_user_id?: string | null;
    reference_asset_ids: string[];
    notes: string;
  };
  report_markdown: string;
  report_html: string;
  artifact_hash: string;
}

export interface RuntimeStemMinimumPrincipleValidationCalculateRequest {
  artifact_id: string;
  artifact_version: string;
  image_width: number;
  image_height: number;
  process_calibration_id: string;
  process_calibration_hash: string;
  phase_calibration_id: string;
  phase_calibration_version: string;
  phase_calibration_hash: string;
  thresholds_confirmed: true;
  thresholds: RuntimeStemMinimumPrincipleValidationThresholds;
  observations: RuntimeStemMinimumPrincipleValidationObservation[];
  reference_asset_ids?: string[];
  notes?: string;
}

export interface RuntimeStemMinimumPrincipleValidationCalculateResponse {
  schema_version: 'runtime.stem-minimum-principle-validation.calculate.v1';
  status: 'VALIDATION_PASSED' | 'VALIDATION_FAILED' | 'INSUFFICIENT_DATA';
  artifact: RuntimeStemMinimumPrincipleValidationArtifact;
  report_json: Record<string, unknown>;
  report_markdown: string;
  report_html: string;
  meta: ResponseMeta;
}

export interface RuntimeCameraCalibrationObservation {
  observation_id: string;
  frame: RuntimeLiveFrame;
}

export interface RuntimeCameraCalibrationObservationResult {
  observation_id: string;
  accepted: boolean;
  detected_point_count: number;
  reprojection_error_px?: number | null;
  reason_code?: string | null;
  message?: string | null;
}

export interface RuntimeCameraCalibrationCalculateRequest {
  board_definition: RuntimeCameraCalibrationBoardDefinition;
  observations?: RuntimeCameraCalibrationObservation[];
  calibration_method?: 'CHECKERBOARD_OR_CHARUCO' | 'IDENTITY_PASSTHROUGH';
  image_width?: number | null;
  image_height?: number | null;
  minimum_observation_count?: number;
  maximum_rms_reprojection_error_px?: number;
  preview_observation_id?: string | null;
}

export interface RuntimeCameraCalibrationCalculateResponse {
  schema_version: 'runtime.camera-calibration.calculate.v1';
  status: 'valid' | 'invalid';
  profile?: RuntimeCameraIntrinsicCalibrationProfile | null;
  observations: RuntimeCameraCalibrationObservationResult[];
  rms_reprojection_error_px?: number | null;
  failure_code?: string | null;
  failure_message?: string | null;
  preview_before?: RuntimeLiveFrame | null;
  preview_after?: RuntimeLiveFrame | null;
  meta: ResponseMeta;
}

export interface RuntimeOverlayPrimitiveState {
  primitive_type:
    | 'box'
    | 'polygon'
    | 'point'
    | 'label'
    | 'measurement'
    | 'roi'
    | 'zone'
    | 'line'
    | 'axis'
    | 'arrow'
    | 'motion'
    | 'heatmap'
    | 'mask';
  layer: string;
  label?: string | null;
  confidence?: number | null;
  geometry: Record<string, unknown>;
  image_context_id?: string | null;
  display_context_id?: string | null;
  frame_id?: string | null;
  source_timestamp?: string | null;
  coordinate_system?: string;
  resolution?: Record<string, unknown>;
  object_id?: string | null;
  source_object_id?: string | null;
  projected_from_context_id?: string | null;
  style: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface RuntimeLiveMetrics {
  fps?: number | null;
  source_fps?: number | null;
  processed_fps?: number | null;
  drop_rate_percent?: number | null;
  latency_ms?: number | null;
  latency_p50_ms?: number | null;
  latency_p95_ms?: number | null;
  latency_p99_ms?: number | null;
  frame_count: number;
  acquired_frame_count: number;
  dropped_frame_count: number;
  reconnect_count: number;
  source_error_count: number;
  buffer_depth: number;
  output_stream_fps?: number | null;
  stream_dropped_frame_count?: number;
  encode_latency_ms?: number | null;
  stream_publication_latency_ms?: number | null;
  connected_clients?: number;
  gpu_encoder_available?: boolean;
  overlay_fps?: number | null;
  dropped_overlay_frame_count?: number;
  stale_overlay_count?: number;
  last_frame_at?: string | null;
  stage_timings_ms?: Record<string, number>;
}

export interface RuntimeLiveSourceHealth {
  state: HealthState;
  status_text: string;
  connected: boolean;
  last_error?: string | null;
  reconnect_count: number;
  source_error_count: number;
}

export interface RuntimeLiveDiagnostics {
  source_type?: string | null;
  source_max_fps?: number | null;
  source_sample_every_n: number;
  effective_source_limit_fps?: number | null;
  pipeline_max_inflight: number;
  pipeline_configured_max_inflight?: number;
  pipeline_global_max_concurrency?: number;
  pipeline_per_release_max_concurrency?: number;
  pipeline_inflight_limit_reason?: string;
  pipeline_stateful_nodes: string[];
  pipeline_frame_samplers: Array<Record<string, unknown>>;
  pipeline_node_timings: Array<Record<string, unknown>>;
  pipeline_inference_timings: Array<Record<string, unknown>>;
  plc_write_outputs: Array<Record<string, unknown>>;
  warnings: string[];
  stream_transport?: string | null;
}

export interface RuntimeStemRotationViewPoint {
  y_mm: number;
  z_mm: number;
}

export interface RuntimeStemRotationChordView {
  start: RuntimeStemRotationViewPoint;
  end: RuntimeStemRotationViewPoint;
  span_mm: number;
  valid: boolean;
}

export interface RuntimeStemRotationLongitudinalSliceView {
  x_mm: number;
  in_evaluation_range: boolean;
  current_center_axis_mm: number;
  current_outline_negative_mm: number;
  current_outline_positive_mm: number;
  optimal_center_axis_mm?: number | null;
  optimal_outline_negative_mm?: number | null;
  optimal_outline_positive_mm?: number | null;
  mean_diameter_mm: number;
  ovality_ratio: number;
  quality: number;
  interpolated: boolean;
  limiting_current: boolean;
  limiting_optimal: boolean;
}

export interface RuntimeStemRotationCrossSectionView {
  x_mm: number;
  current_center: RuntimeStemRotationViewPoint;
  optimal_center?: RuntimeStemRotationViewPoint | null;
  current_ellipse: RuntimeStemRotationViewPoint[];
  optimal_ellipse: RuntimeStemRotationViewPoint[];
  current_symmetric_chord: RuntimeStemRotationChordView;
  optimal_symmetric_chord?: RuntimeStemRotationChordView | null;
  current_phi_deg: number;
  optimal_phi_deg?: number | null;
  a_mm: number;
  b_mm: number;
  quality: number;
}

export interface RuntimeStemRotationDeltaGeometryView {
  x_mm: number;
  in_evaluation_range: boolean;
  current_geometry_mm?: number | null;
  optimal_geometry_mm?: number | null;
  delta_geometry_mm?: number | null;
  valid: boolean;
  reason_code: string;
  limiting_current: boolean;
  limiting_optimal: boolean;
}

export interface RuntimeStemRotationAngleScoreView {
  angle_deg: number;
  score_mm?: number | null;
  valid: boolean;
  selected: boolean;
  current: boolean;
}

export interface RuntimeStemRotationDiagnosticsView {
  score_current_mm?: number | null;
  score_optimal_mm?: number | null;
  optimal_candidate_angle_deg?: number | null;
  plateau_width_deg?: number | null;
  score_tolerance_mm?: number | null;
  valid_slice_count: number;
  evaluated_slice_count: number;
  mean_fit_quality?: number | null;
  mean_observation_quality?: number | null;
  mean_center_residual_rms_mm?: number | null;
  mean_width_residual_rms_mm?: number | null;
  minimum_phase_coverage_ratio?: number | null;
  fit_diagnostics: Array<Record<string, unknown>>;
  active_configuration: Record<string, unknown>;
  angle_score_curve: RuntimeStemRotationAngleScoreView[];
}

export type RuntimeStemRotationOptimizationState =
  | 'IDLE'
  | 'MEASURING'
  | 'MODEL_BUILDING'
  | 'OPTIMIZING'
  | 'RESULT_READY'
  | 'DONE'
  | 'APPLIED'
  | 'INVALID'
  | 'ERROR';

export interface RuntimeStemRotationViewModel {
  schema_version: 'stem.rotation-hmi.v1';
  coordinate_system: 'MACHINE_X_Y_Z_RIGHT_HANDED_V1';
  rotation_axis: 'POSITIVE_MACHINE_X';
  rotation_reference_view?: string | null;
  positive_rotation?: string | null;
  timestamp: string;
  state: RuntimeStemRotationOptimizationState;
  result_ready: boolean;
  result_valid: boolean;
  priority_source: StemRotationPrioritySource;
  priority_mode: StemRotationPriorityMode;
  priority_axis: 'NONE' | 'VERTICAL_Z' | 'HORIZONTAL_Y';
  priority_revision: number;
  optimization_enable: boolean;
  should_rotate: boolean;
  rotation_delta_deg?: number | null;
  rotation_direction: 'POSITIVE' | 'NEGATIVE' | 'NONE' | 'UNAVAILABLE';
  gain_mm?: number | null;
  model_quality: number;
  stem_id?: string | null;
  session_id?: string | null;
  model_hash?: string | null;
  reason_code: string;
  reason_codes: string[];
  limiting_x_current_mm?: number | null;
  limiting_x_optimal_mm?: number | null;
  comparison_angle_deg?: number | null;
  stem_longitudinal_geometry: RuntimeStemRotationLongitudinalSliceView[];
  current_critical_cross_section?: RuntimeStemRotationCrossSectionView | null;
  optimal_critical_cross_section?: RuntimeStemRotationCrossSectionView | null;
  delta_geometry_by_x: RuntimeStemRotationDeltaGeometryView[];
  diagnostics: RuntimeStemRotationDiagnosticsView;
}

export interface RuntimeLiveViewState {
  channel_id: string;
  runtime_instance_id?: string | null;
  actual_state: RuntimeObservedState;
  available: boolean;
  stale: boolean;
  display_context_id?: string | null;
  display_context?: Record<string, unknown>;
  frame?: RuntimeLiveFrame | null;
  source_frame?: RuntimeLiveFrame | null;
  undistorted_frame?: RuntimeLiveFrame | null;
  overlay: RuntimeOverlayPrimitiveState[];
  source_overlay?: RuntimeOverlayPrimitiveState[];
  raw_overlay?: RuntimeOverlayPrimitiveState[];
  layers_available: string[];
  metrics: RuntimeLiveMetrics;
  source_health: RuntimeLiveSourceHealth;
  diagnostics?: RuntimeLiveDiagnostics;
  stem_rotation_view?: RuntimeStemRotationViewModel | null;
  recovery_hint?: string | null;
  updated_at?: string | null;
}

export interface RuntimeLiveViewResponse {
  schema_version: 'runtime.live-view.state.v1';
  state: RuntimeLiveViewState;
  execution_result?: Record<string, unknown> | null;
  meta: ResponseMeta;
}

export type RuntimeStreamVariantKey = 'raw' | 'undistorted_full' | 'canonical_model';
export type RuntimeStreamProtocol = 'webrtc';
export type RuntimeStreamStatus = 'disabled' | 'starting' | 'ready' | 'degraded' | 'unavailable';
export type RuntimeStreamAccessPolicy =
  | 'unauthenticated_lan'
  | 'session'
  | 'token'
  | 'internal_only';
export type RuntimeStreamEncoderMode = 'gpu' | 'unavailable';
export type RuntimeStreamCompositorMode = 'none';
export type RuntimeStreamDesiredState = 'stopped' | 'running';
export type RuntimeStreamActualState = 'stopped' | 'starting' | 'ready' | 'recovering' | 'failed';
export type RuntimeStreamComponentState = RuntimeStreamActualState | 'idle';

export interface RuntimeStreamEndpoint {
  protocol: RuntimeStreamProtocol;
  url: string;
  display_url?: string | null;
  external_client_example?: string | null;
  browser_compatible: boolean;
  vlc_compatible: boolean;
}

export interface RuntimeStreamEncoderStatus {
  mode: RuntimeStreamEncoderMode;
  encoder?: string | null;
  codec: string;
  hardware_accelerated: boolean;
  pixel_format?: string | null;
  preset?: string | null;
  warning?: string | null;
}

export interface RuntimeStreamPublicationMetrics {
  input_fps?: number | null;
  output_fps?: number | null;
  submitted_frames?: number;
  published_frames?: number;
  encoder_dropped_frames?: number;
  transform_fps?: number | null;
  transform_dropped_frames?: number;
  processing_latency_ms?: number | null;
  transform_source_frame_age_ms?: number | null;
  dropped_frames: number;
  repeated_frames: number;
  encode_latency_ms?: number | null;
  publication_latency_ms?: number | null;
  source_frame_age_ms?: number | null;
  connected_clients: number;
  reconnect_count: number;
  last_frame_sequence: number;
  updated_at?: string | null;
}

export interface RuntimeStreamVariantStatus {
  variant: RuntimeStreamVariantKey;
  status: RuntimeStreamStatus;
  intended_use: string;
  overlay_included: boolean;
  protocol: RuntimeStreamProtocol;
  endpoints: RuntimeStreamEndpoint[];
  codec: string;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  latency_estimate_ms?: number | null;
  encoder: RuntimeStreamEncoderStatus;
  compositor_mode: RuntimeStreamCompositorMode;
  fanout: 'server' | 'none';
  multicast: boolean;
  access_policy: RuntimeStreamAccessPolicy;
  authentication_required: boolean;
  ready: boolean;
  warning?: string | null;
  metrics: RuntimeStreamPublicationMetrics;
}

export interface RuntimeChannelStreamListResponse {
  schema_version: 'runtime.channel-streams.list.v2';
  channel_id: string;
  desired_state: RuntimeStreamDesiredState;
  actual_state: RuntimeStreamActualState;
  worker_instance_id?: string | null;
  last_heartbeat_at?: string | null;
  last_error?: string | null;
  negotiated_profile: Record<string, unknown>;
  components: {
    camera: RuntimeStreamComponentStatus;
    encoder: RuntimeStreamComponentStatus;
    publication: RuntimeStreamComponentStatus;
    browser_readers: RuntimeStreamComponentStatus;
  };
  stream_auth_enabled: boolean;
  access_policy_default: RuntimeStreamAccessPolicy;
  items: RuntimeStreamVariantStatus[];
  warnings: string[];
  meta: ResponseMeta;
}

export interface RuntimeStreamComponentStatus {
  state: RuntimeStreamComponentState;
  ready: boolean;
  error?: string | null;
  details: Record<string, unknown>;
}

export interface RuntimeChannelStreamControlResponse {
  schema_version: 'runtime.channel-stream.control.v1';
  channel_id: string;
  desired_state: RuntimeStreamDesiredState;
  actual_state: RuntimeStreamActualState;
  accepted: boolean;
  meta: ResponseMeta;
}

export type RuntimeManualCaptureMode = 'single' | 'interval';
export type RuntimeManualCaptureSessionState = 'idle' | 'running' | 'stopped' | 'failed';
export type RuntimeManualCaptureResultStatus = 'queued' | 'success' | 'failed';
export type RuntimeCaptureDeliveryStatus =
  | 'queued'
  | 'processing'
  | 'rate_limited'
  | 'delivered'
  | 'predicted'
  | 'failed_retryable'
  | 'failed_terminal'
  | 'skipped_duplicate'
  | 'recorded'
  | 'skipped_not_released'
  | 'skipped_sampling'
  | 'needs_attention';
export type RuntimeConnectorDeliveryStatus =
  | 'queued'
  | 'processing'
  | 'delivered'
  | 'failed_retryable'
  | 'failed_terminal'
  | 'skipped_no_payload';

export interface RuntimeManualCaptureTarget {
  capture_target_id: string;
  display_name: string;
  detail?: string | null;
  task_type: TaskType;
  status: string;
  dataset_name?: string | null;
  label_studio_project_id?: number | null;
  selectable: boolean;
}

export interface RuntimeManualCaptureTargetListResponse {
  schema_version: 'runtime.manual-capture.targets.v1';
  items: RuntimeManualCaptureTarget[];
  meta: ResponseMeta;
}

export interface RuntimePipelineCaptureImage {
  node_key: string;
  output_port: string;
  pipeline_version_id?: string | null;
  frame: RuntimeLiveFrame;
  eligibility?: Record<string, unknown> | null;
}

export interface RuntimeManualCaptureRequest {
  capture_target_id: string;
  capture_source?: 'LIVE_VIEW_FRAME' | 'PIPELINE_IMAGE_OUTPUT';
  view_profile_id?: string | null;
  require_eligibility?: boolean;
  request_predictions?: boolean;
  live_session_id?: string | null;
  idempotency_key?: string | null;
  pipeline_image?: RuntimePipelineCaptureImage | null;
}

export interface RuntimeManualIntervalCaptureStartRequest extends RuntimeManualCaptureRequest {
  interval_seconds: number;
}

export interface RuntimeManualIntervalCaptureStopRequest {
  reason?: string | null;
}

export interface RuntimeManualCaptureResult {
  capture_request_id?: string | null;
  manual_capture_session_id?: string | null;
  channel_id: string;
  channel_display_name: string;
  capture_target_id: string;
  target_display_name: string;
  label_studio_project_id: number;
  mode: RuntimeManualCaptureMode;
  interval_seconds?: number | null;
  captured_at: string;
  operator_user_id: string;
  live_session_id?: string | null;
  source_frame_metadata: Record<string, unknown>;
  status: RuntimeManualCaptureResultStatus;
  asset_uri?: string | null;
  label_studio_task_ids: number[];
  error_summary?: string | null;
  metadata: Record<string, unknown>;
}

export interface RuntimeManualCaptureResponse {
  schema_version: 'runtime.manual-capture.result.v1';
  result: RuntimeManualCaptureResult;
  meta: ResponseMeta;
}

export interface RuntimeManualCaptureSessionStatus {
  manual_capture_session_id?: string | null;
  channel_id: string;
  state: RuntimeManualCaptureSessionState;
  capture_target_id?: string | null;
  target_display_name?: string | null;
  label_studio_project_id?: number | null;
  interval_seconds?: number | null;
  operator_user_id?: string | null;
  live_session_id?: string | null;
  started_at?: string | null;
  stopped_at?: string | null;
  last_capture_at?: string | null;
  capture_count: number;
  failure_count: number;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  last_result?: RuntimeManualCaptureResult | null;
  error_summary?: string | null;
}

export interface RuntimeManualCaptureSessionResponse {
  schema_version: 'runtime.manual-capture.session.v1';
  session: RuntimeManualCaptureSessionStatus;
  meta: ResponseMeta;
}

export interface RuntimeManualCaptureHistoryResponse {
  schema_version: 'runtime.manual-capture.history.v1';
  items: RuntimeManualCaptureResult[];
  meta: ResponseMeta;
}

export interface RuntimeCaptureRequestSummary {
  capture_request_id: string;
  channel_id: string;
  capture_rule_id: string;
  result_record_id?: string | null;
  manual_capture_session_id?: string | null;
  manual_capture_request_id?: string | null;
  target_label_project_id: string;
  target_display_name?: string | null;
  label_studio_project_id?: number | null;
  source_type: 'manual' | 'pipeline' | 'temporal_sample';
  capture_mode:
    | 'full_frame'
    | 'roi'
    | 'conditional'
    | 'multi_project'
    | 'full_image'
    | 'full_image_and_roi'
    | 'temporal_sequence';
  status: RuntimeCaptureDeliveryStatus;
  retry_count: number;
  max_attempts: number;
  queued_at?: string | null;
  claimed_at?: string | null;
  submitted_at?: string | null;
  delivered_at?: string | null;
  next_attempt_at?: string | null;
  rate_limited_until?: string | null;
  asset_uri?: string | null;
  digest?: string | null;
  roi: Record<string, unknown>;
  label_studio_task_ids: number[];
  duplicate_of_request_id?: string | null;
  last_error?: string | null;
  metadata: Record<string, unknown>;
  created_at?: string | null;
}

export interface RuntimeCaptureHistoryResponse {
  schema_version: 'runtime.capture.history.v1';
  items: RuntimeCaptureRequestSummary[];
  total: number;
  meta: ResponseMeta;
}

export interface RuntimeOutputConnectorDeliveryPolicy {
  max_attempts: number;
  retry_backoff_seconds: number[];
}

export interface RuntimeOutputConnectorCreateRequest {
  channel_id: string;
  connector_type: string;
  name: string;
  source_node_key?: string | null;
  enabled?: boolean;
  config?: Record<string, unknown>;
  secret_refs?: Record<string, string>;
  delivery_policy?: RuntimeOutputConnectorDeliveryPolicy;
}

export interface RuntimeOutputConnectorSummary extends RuntimeOutputConnectorCreateRequest {
  output_connector_id: string;
  status: RuntimeResourceStatus;
  pipeline_version_id?: string | null;
  last_delivery_status?: RuntimeConnectorDeliveryStatus | null;
  last_delivery_at?: string | null;
  last_success_at?: string | null;
  last_error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface RuntimeOutputConnectorDetail extends RuntimeOutputConnectorSummary {
  created_by?: string | null;
  updated_by?: string | null;
}

export interface RuntimeOutputConnectorUpdateRequest {
  name?: string | null;
  source_node_key?: string | null;
  enabled?: boolean | null;
  config?: Record<string, unknown> | null;
  secret_refs?: Record<string, string> | null;
  delivery_policy?: RuntimeOutputConnectorDeliveryPolicy | null;
}

export interface RuntimeOutputConnectorListResponse {
  schema_version: 'runtime.output-connectors.list.v1';
  items: RuntimeOutputConnectorSummary[];
  meta: ResponseMeta;
}

export interface RuntimeOutputConnectorTypeSummary {
  connector_type: string;
  display_name: string;
  description: string;
  config_schema: Record<string, unknown>;
  default_config: Record<string, unknown>;
  default_delivery_policy: RuntimeOutputConnectorDeliveryPolicy;
  supports_secrets: boolean;
  retryable: boolean;
}

export interface RuntimeOutputConnectorTypeCatalogResponse {
  schema_version: 'runtime.output-connector-types.catalog.v1';
  items: RuntimeOutputConnectorTypeSummary[];
  meta: ResponseMeta;
}

export interface RuntimeConnectorDeliverySummary {
  connector_delivery_id: string;
  output_connector_id: string;
  channel_id: string;
  result_record_id: string;
  connector_type: string;
  connector_name?: string | null;
  source_node_key?: string | null;
  status: RuntimeConnectorDeliveryStatus;
  retry_count: number;
  max_attempts: number;
  queued_at?: string | null;
  claimed_at?: string | null;
  delivered_at?: string | null;
  completed_at?: string | null;
  next_attempt_at?: string | null;
  request_payload: Record<string, unknown>;
  response_payload: Record<string, unknown>;
  last_error?: string | null;
  created_at?: string | null;
}

export interface RuntimeConnectorDeliveryHistoryResponse {
  schema_version: 'runtime.connector-deliveries.history.v1';
  items: RuntimeConnectorDeliverySummary[];
  total: number;
  meta: ResponseMeta;
}

export interface RuntimeLogicalModelRef {
  model_family_slug: string;
  label_project_id?: string | null;
  target_path: TargetPath;
  contract_major: number;
  selection_policy: 'active_production' | 'staged_test' | 'explicit_release';
  required_task_type: TaskType;
  deployment_release_id?: string | null;
}

export interface RuntimeNodePort {
  key: string;
  data_type: string;
  required: boolean;
}

export interface RuntimeNodePropertyOption {
  value: string;
  label: string;
}

export interface RuntimeNodePropertySpec {
  key: string;
  label: string;
  field_type:
    | 'text'
    | 'number'
    | 'integer'
    | 'slider'
    | 'boolean'
    | 'select'
    | 'multi_select'
    | 'channel_ref'
    | 'image_upload'
    | 'image_upload_list'
    | 'model_ref'
    | 'zone_list'
    | 'roi'
    | 'label_project_list'
    | 'response_mapping'
    | 'result_projection_table'
    | 'edge_role_mapping'
    | 'measurement_rule_table'
    | 'clearance_rule_table'
    | 'cv_line_selection_rules'
    | 'metric_apply_to'
    | 'quality_rule_table'
    | 'spatial_rule_table'
    | 'color_profile_table'
    | 'direction_reference'
    | 'board_surface_calibration'
    | 'camera_intrinsic_calibration'
    | 'image_view_profile'
    | 'stem_curvature_process_calibration'
    | 'stem_surface_phase_calibration'
    | 'stem_minimum_principle_validation'
    | 'board_cut_class_rules'
    | 'board_surface_class_mapping'
    | 'plc_connection'
    | 'plc_write_mapping_table'
    | 'expression';
  required: boolean;
  default_value?: unknown | null;
  options: RuntimeNodePropertyOption[];
  min_value?: number | null;
  max_value?: number | null;
  step_value?: number | null;
  section?: string | null;
  help_text?: string | null;
}

export interface RuntimeTriggerExecutionRequest {
  trigger_id?: string | null;
  image_base64?: string | null;
  mime_type?: string;
  images_by_role?: Partial<Record<'upper' | 'lower' | 'end_face', {
    image_base64: string;
    mime_type?: 'image/jpeg' | 'image/png';
    captured_at?: string | null;
  }>>;
  timeout_ms?: number | null;
  board_format?: {
    format_id: string;
    format_version: string;
    width_mm: number;
    thickness_mm: number;
  } | null;
  payload?: Record<string, unknown>;
}

export interface RuntimeTriggerExecutionResponse {
  schema_version: 'runtime.trigger.execution-response.v1';
  status: RuntimeTriggerExecutionStatus;
  trigger_endpoint_id?: string | null;
  endpoint_slug?: string | null;
  channel_id?: string | null;
  pipeline_version_id?: string | null;
  runtime_execution_id?: string | null;
  trigger_id?: string | null;
  correlation_id?: string | null;
  final_response: Record<string, unknown>;
  execution_result: Record<string, unknown>;
  overlay_outputs: Record<string, unknown>[];
  capture_intents: Record<string, unknown>[];
  http_trigger_responses: Record<string, unknown>[];
  persisted_results: Record<string, unknown>[];
  plc_write_outputs: Record<string, unknown>[];
  projected_results: Record<string, unknown>[];
  errors: Record<string, unknown>[];
  timing: Record<string, unknown>;
  error_code?: string | null;
  error_message?: string | null;
  retryable: boolean;
  meta: ResponseMeta;
}

export interface RuntimeTriggerResponseFieldMapping {
  source_field: string;
  response_key: string;
  required: boolean;
  default_value?: unknown | null;
}

export interface RuntimeTriggerResponsePolicy {
  mode: 'configured_fields' | 'all_final_response';
  fields: RuntimeTriggerResponseFieldMapping[];
  include_unmapped: boolean;
}

export interface RuntimeTriggerEndpointReadiness {
  ready: boolean;
  reason: string;
  runtime_instance_id?: string | null;
  actual_state?: RuntimeObservedState | null;
  channel_id?: string | null;
  pipeline_version_id?: string | null;
  endpoint_pipeline_version_id?: string | null;
  last_heartbeat_at?: string | null;
}

export interface RuntimeTriggerEndpointCreateRequest {
  name: string;
  description?: string | null;
  channel_id: string;
  slug?: string | null;
  pipeline_version_id: string;
  trigger_type?: 'http_put';
  enabled?: boolean;
  auth_required?: boolean;
  auth_scope?: string;
  auth_policy?: Record<string, unknown>;
  timeout_ms?: number;
  rate_limit_per_minute?: number;
  burst_limit?: number;
  response_policy?: RuntimeTriggerResponsePolicy;
  request_schema?: Record<string, unknown>;
  response_schema?: Record<string, unknown>;
}

export interface RuntimeTriggerEndpointUpdateRequest {
  name?: string | null;
  description?: string | null;
  slug?: string | null;
  channel_id?: string | null;
  pipeline_version_id?: string | null;
  trigger_type?: 'http_put' | null;
  enabled?: boolean | null;
  auth_required?: boolean | null;
  auth_scope?: string | null;
  auth_policy?: Record<string, unknown> | null;
  timeout_ms?: number | null;
  rate_limit_per_minute?: number | null;
  burst_limit?: number | null;
  response_policy?: RuntimeTriggerResponsePolicy | null;
  request_schema?: Record<string, unknown> | null;
  response_schema?: Record<string, unknown> | null;
}

export interface RuntimeResultRecordSummary {
  result_record_id: string;
  channel_id: string;
  channel_name?: string | null;
  technical_place?: string | null;
  pipeline_version_id?: string | null;
  runtime_execution_id?: string | null;
  trigger_endpoint_id?: string | null;
  correlation_id: string;
  source_timestamp?: string | null;
  completed_at: string;
  mode: RuntimeMode;
  status: 'completed' | 'failed' | 'timeout';
  result_type: string;
  schema_version: string;
  result: Record<string, unknown>;
  trigger_response: Record<string, unknown>;
  derived_preview: Record<string, unknown>;
  inference_output_count: number;
  derived_output_count: number;
  overlay_primitive_count: number;
  timing: Record<string, unknown>;
  model_routes: Record<string, unknown>[];
}

export interface RuntimeResultRecordDetail extends RuntimeResultRecordSummary {
  raw_inference_outputs: Record<string, unknown>[];
  normalized_inference_outputs: Record<string, unknown>[];
  derived_outputs: Record<string, unknown>[];
  overlay_primitives: Record<string, unknown>[];
  runtime_metadata: Record<string, unknown>;
  execution_result: Record<string, unknown>;
}

export interface RuntimeResultListResponse {
  schema_version: 'runtime.results.list.v1';
  items: RuntimeResultRecordSummary[];
  total: number;
  limit: number;
  offset: number;
  meta: ResponseMeta;
}

export interface RuntimeResultDetailResponse {
  schema_version: 'runtime.results.detail.v1';
  item: RuntimeResultRecordDetail;
  meta: ResponseMeta;
}

export type StemReviewPlausibility = 'yes' | 'no' | 'unclear';
export type StemReviewDirection = 'correct' | 'incorrect' | 'unclear';
export type StemReviewAngle = 'too_small' | 'appropriate' | 'too_large' | 'not_assessable';
export type StemReviewBowRelevance = 'relevant' | 'nearly_straight' | 'unclear';
export type StemReviewFinalPosition = 'good' | 'acceptable' | 'bad' | 'not_assessed';

export interface RuntimeStemOrientationReviewCreateRequest {
  proposal_plausibility: StemReviewPlausibility;
  direction_assessment: StemReviewDirection;
  angle_assessment: StemReviewAngle;
  bow_relevance: StemReviewBowRelevance;
  final_position: StemReviewFinalPosition;
  target_frame_start?: number | null;
  target_frame_end?: number | null;
  comment?: string;
  special_case?: string;
}

export interface RuntimeStemOrientationReview {
  schema_version: 'stem.orientation.review.v1';
  review_id: string;
  result_record_id: string;
  revision: number;
  proposal_plausibility: StemReviewPlausibility;
  direction_assessment: StemReviewDirection;
  angle_assessment: StemReviewAngle;
  bow_relevance: StemReviewBowRelevance;
  final_position: StemReviewFinalPosition;
  target_frame_start?: number | null;
  target_frame_end?: number | null;
  comment: string;
  special_case: string;
  supersedes_review_id?: string | null;
  reviewer_user_id: string;
  reviewer_username: string;
  created_at: string;
}

export interface RuntimeStemOrientationReviewListResponse {
  schema_version: 'runtime.stem-orientation-reviews.list.v1';
  result_record_id: string;
  items: RuntimeStemOrientationReview[];
  latest?: RuntimeStemOrientationReview | null;
  meta: ResponseMeta;
}

export interface RuntimeStemOrientationReviewResponse {
  schema_version: 'runtime.stem-orientation-reviews.detail.v1';
  item: RuntimeStemOrientationReview;
  meta: ResponseMeta;
}

export interface RuntimeStemShadowMetricCount {
  key: string;
  count: number;
  share: number;
}

export interface RuntimeStemShadowHistogramBin {
  lower_bound: number;
  upper_bound: number;
  count: number;
}

export interface RuntimeStemShadowLatencySummary {
  measured_count: number;
  missing_count: number;
  minimum_ms?: number | null;
  median_ms?: number | null;
  p90_ms?: number | null;
  p95_ms?: number | null;
  maximum_ms?: number | null;
}

export interface RuntimeStemShadowDriftSummary {
  status: 'NO_DRIFT_DETECTED' | 'DRIFT_SIGNAL' | 'INSUFFICIENT_DATA';
  baseline_count: number;
  recent_count: number;
  baseline_mean_confidence?: number | null;
  recent_mean_confidence?: number | null;
  confidence_delta?: number | null;
  baseline_uncertain_share?: number | null;
  recent_uncertain_share?: number | null;
  uncertain_share_delta?: number | null;
  process_calibration_hashes: RuntimeStemShadowMetricCount[];
  rotation_calibration_hashes: RuntimeStemShadowMetricCount[];
  process_calibration_change_count: number;
  rotation_calibration_change_count: number;
  drift_reason_codes: RuntimeStemShadowMetricCount[];
  signals: string[];
}

export interface RuntimeStemShadowFeedbackSummary {
  reviewed_session_count: number;
  ratings: RuntimeStemShadowMetricCount[];
  technical_error_reasons: RuntimeStemShadowMetricCount[];
}

export interface RuntimeStemShadowEvaluationResponse {
  schema_version: 'runtime.stem-shadow-evaluation.v1';
  pipeline_version_id: string;
  shadow_mode_enabled: boolean;
  evaluation_status: 'ACTIVE' | 'INACTIVE' | 'NO_SESSIONS';
  sessions_available: number;
  sessions_analyzed: number;
  excluded_session_count: number;
  maximum_sessions: number;
  limit_reached: boolean;
  completed_from?: string | null;
  completed_to?: string | null;
  decision_distribution: RuntimeStemShadowMetricCount[];
  no_rotation_share: number;
  uncertain_share: number;
  deadline_miss_share: number;
  angle_histogram: RuntimeStemShadowHistogramBin[];
  angle_missing_count: number;
  confidence_histogram: RuntimeStemShadowHistogramBin[];
  confidence_percentiles: Record<string, number | null>;
  reason_code_distribution: RuntimeStemShadowMetricCount[];
  decision_latency: RuntimeStemShadowLatencySummary;
  drift: RuntimeStemShadowDriftSummary;
  feedback: RuntimeStemShadowFeedbackSummary;
  report_json: Record<string, unknown>;
  report_markdown: string;
  meta: ResponseMeta;
}

export interface RuntimeTriggerEndpointSummary {
  trigger_endpoint_id: string;
  channel_id: string;
  channel_name?: string | null;
  name: string;
  description?: string | null;
  slug: string;
  trigger_type: 'http_put';
  method: 'PUT';
  path: string;
  enabled: boolean;
  auth_required: boolean;
  auth_scope: string;
  auth_policy: Record<string, unknown>;
  pipeline_definition_id?: string | null;
  pipeline_version_id?: string | null;
  trigger_node_key?: string | null;
  derived_from_pipeline: boolean;
  pipeline_label?: string | null;
  timeout_ms: number;
  rate_limit_per_minute: number;
  burst_limit: number;
  status: RuntimeResourceStatus;
  readiness: RuntimeTriggerEndpointReadiness;
  last_invoked_at?: string | null;
  last_status?: RuntimeTriggerExecutionStatus | null;
  last_latency_ms?: number | null;
  last_error_code?: string | null;
  last_error_message?: string | null;
  invocation_count: number;
  failure_count: number;
}

export interface RuntimeTriggerEndpointDetail extends RuntimeTriggerEndpointSummary {
  response_policy: RuntimeTriggerResponsePolicy;
  request_schema: Record<string, unknown>;
  response_schema: Record<string, unknown>;
  last_result?: RuntimeResultRecordSummary | null;
  created_at: string;
  updated_at?: string | null;
  created_by?: string | null;
  updated_by?: string | null;
}

export interface RuntimeTriggerEndpointListResponse {
  schema_version: 'runtime.trigger-endpoints.list.v1';
  items: RuntimeTriggerEndpointSummary[];
  meta: ResponseMeta;
}

export interface RuntimeNodeTypeSummary {
  node_type: string;
  node_category: RuntimeNodeCategory;
  contract_version: number;
  display_name: string;
  description: string;
  supported_modes: RuntimeMode[];
  input_ports: RuntimeNodePort[];
  output_ports: RuntimeNodePort[];
  config_schema_ref: string;
  property_schema: RuntimeNodePropertySpec[];
  default_config: Record<string, unknown>;
  runtime_handler_ref: string;
  compatibility_constraints: Record<string, unknown>;
}

export interface RuntimeNodeTypeCatalogResponse {
  schema_version: 'runtime.node-types.catalog.v1';
  items: RuntimeNodeTypeSummary[];
  meta: ResponseMeta;
}

export interface RuntimeNodeSpec {
  node_key: string;
  node_type: string;
  node_category: RuntimeNodeCategory;
  node_contract_version: number;
  input_ports: RuntimeNodePort[];
  output_ports: RuntimeNodePort[];
  config: Record<string, unknown>;
  logical_model_ref?: RuntimeLogicalModelRef | null;
  position: Record<string, number>;
}

export interface RuntimeAuthoringNodeSpec {
  node_key: string;
  node_type: string;
  config: Record<string, unknown>;
  logical_model_ref?: RuntimeLogicalModelRef | null;
  position: Record<string, number>;
}

export interface RuntimeGraphEdge {
  from_node_key: string;
  from_port: string;
  to_node_key: string;
  to_port: string;
}

export interface RuntimePipelineGraph {
  nodes: RuntimeNodeSpec[];
  edges: RuntimeGraphEdge[];
}

export interface RuntimePipelineAuthoringGraph {
  nodes: RuntimeAuthoringNodeSpec[];
  edges: RuntimeGraphEdge[];
}

export type RuntimeProjectionDestination =
  | 'response'
  | 'history'
  | 'live_view'
  | 'overlay'
  | 'plc'
  | 'connector';

export type RuntimeProjectionOperator =
  | 'exists'
  | 'not_exists'
  | 'eq'
  | 'ne'
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'
  | 'contains'
  | 'in'
  | 'not_in'
  | 'truthy'
  | 'falsy';

export interface RuntimeValueSelector {
  node_key?: string | null;
  output_port?: string | null;
  path: string;
  value_type:
    | 'auto'
    | 'scalar'
    | 'number'
    | 'boolean'
    | 'text'
    | 'object'
    | 'object_list'
    | 'list'
    | 'map';
}

export interface RuntimeProjectionFilter {
  path: string;
  op: RuntimeProjectionOperator;
  value?: unknown | null;
}

export interface RuntimeProjectionAggregate {
  op: 'none' | 'count' | 'list' | 'first' | 'any' | 'all' | 'sum' | 'avg' | 'min' | 'max';
  path?: string | null;
}

export interface RuntimeProjectionField {
  source_path: string;
  output_key: string;
  required: boolean;
  default_value?: unknown | null;
}

export interface RuntimeDecisionRule {
  op: RuntimeProjectionOperator;
  value?: unknown | null;
  when_true: unknown;
  when_false: unknown;
}

export interface RuntimeResultProjection {
  key: string;
  label?: string | null;
  source: RuntimeValueSelector;
  filters: RuntimeProjectionFilter[];
  aggregate: RuntimeProjectionAggregate;
  fields: RuntimeProjectionField[];
  decision?: RuntimeDecisionRule | null;
  destinations: RuntimeProjectionDestination[];
  required: boolean;
  default_value?: unknown | null;
}

export interface RuntimeOutputPolicy {
  projections: RuntimeResultProjection[];
  default_destinations: RuntimeProjectionDestination[];
  include_runtime_metadata: boolean;
}

export interface RuntimePipelineOutputCatalogField {
  key: string;
  label: string;
  value_type:
    | 'auto'
    | 'scalar'
    | 'number'
    | 'boolean'
    | 'text'
    | 'object'
    | 'object_list'
    | 'list'
    | 'map';
  aggregate_ops: string[];
  filterable_fields: string[];
  list_item_fields: string[];
}

export interface RuntimePipelineOutputCatalogEntry {
  node_key: string;
  node_type: string;
  output_port?: string | null;
  payload_type?: string | null;
  fields: string[];
  field_options: RuntimePipelineOutputCatalogField[];
}

export interface RuntimePipelineOutputCatalog {
  entries: RuntimePipelineOutputCatalogEntry[];
  destinations: RuntimeProjectionDestination[];
  aggregate_ops: string[];
}

export interface RuntimePipelineDefinitionCreateRequest {
  slug?: string | null;
  name: string;
  description?: string | null;
  template?: boolean;
}

export interface RuntimePipelineVersionCreateRequest {
  mode: RuntimeMode;
  graph?: RuntimePipelineGraph | null;
  authoring_graph?: RuntimePipelineAuthoringGraph | null;
  change_note?: string | null;
}

export interface RuntimePipelineValidationIssue {
  severity: 'error' | 'warning';
  code: string;
  message: string;
  node_key?: string | null;
  edge_index?: number | null;
  field?: string | null;
  metadata: Record<string, unknown>;
}

export interface RuntimePipelineValidationReport {
  status: 'valid' | 'invalid';
  checked_at: string;
  error_count: number;
  warning_count: number;
  issues: RuntimePipelineValidationIssue[];
}

export interface RuntimePipelineValidationRequest {
  mode: RuntimeMode;
  graph?: RuntimePipelineGraph | null;
  authoring_graph?: RuntimePipelineAuthoringGraph | null;
}

export interface RuntimePipelinePreviewRequest {
  mode?: RuntimeMode;
  graph?: RuntimePipelineGraph | null;
  authoring_graph?: RuntimePipelineAuthoringGraph | null;
  preview_frame?: RuntimeLiveFrame | null;
  bark_job?: StemBarkJob | null;
  selected_node_key?: string | null;
  include_capture_outputs?: boolean;
  include_execution_result?: boolean;
}

export interface RuntimePipelineValidationResponse {
  schema_version: 'runtime.pipeline.validation.v1';
  report: RuntimePipelineValidationReport;
  normalized_graph?: RuntimePipelineGraph | null;
  output_catalog?: RuntimePipelineOutputCatalog | null;
  meta: ResponseMeta;
}

export interface RuntimePipelineVersionSummary {
  pipeline_version_id: string;
  pipeline_definition_id: string;
  version_no: number;
  status: RuntimePipelineStatus;
  mode: RuntimeMode;
  config_digest: string;
  validation_status: 'valid' | 'invalid';
  error_count: number;
  warning_count: number;
  change_note?: string | null;
  created_at?: string | null;
  published_at?: string | null;
}

export interface RuntimePipelineVersionDetail extends RuntimePipelineVersionSummary {
  graph: RuntimePipelineGraph;
  validation_report: RuntimePipelineValidationReport;
  model_capability_constraints: Record<string, unknown>;
  node_contract_versions: Record<string, number>;
}

export interface RuntimePipelineDefinitionSummary {
  pipeline_definition_id: string;
  slug: string;
  name: string;
  description?: string | null;
  status: RuntimePipelineStatus;
  latest_version_id?: string | null;
  latest_version_mode?: RuntimeMode | null;
  published_version_id?: string | null;
  published_version_mode?: RuntimeMode | null;
  template: boolean;
  scope: 'template' | 'channel_instance';
  owner_channel_id?: string | null;
  source_template_definition_id?: string | null;
  version_count: number;
}

export interface RuntimePipelineDefinitionDetail extends RuntimePipelineDefinitionSummary {
  versions: RuntimePipelineVersionSummary[];
  latest_version?: RuntimePipelineVersionDetail | null;
}

export interface RuntimePipelineDefinitionListResponse {
  schema_version: 'runtime.pipeline-definitions.list.v1';
  items: RuntimePipelineDefinitionSummary[];
  meta: ResponseMeta;
}

export interface RuntimePipelineTemplateInstantiateRequest {
  name: string;
  slug?: string | null;
  description?: string | null;
  change_note?: string | null;
}

export interface RuntimeModelCapabilityOption {
  deployment_release_id: string;
  model_family_slug: string;
  display_name: string;
  target_path: TargetPath;
  task_type: TaskType;
  model_version_id: string;
  version_label: string;
  deployment_status: string;
  architecture_family?: string | null;
  architecture_version?: string | null;
  architecture_size?: string | null;
  architecture_display_name?: string | null;
  contract_major: number;
  active: boolean;
  capability: Record<string, unknown>;
}

export interface RuntimeModelCapabilityListResponse {
  schema_version: 'runtime.model-capabilities.list.v1';
  items: RuntimeModelCapabilityOption[];
  meta: ResponseMeta;
}

export interface DatasetSummary {
  dataset_id: string;
  name: string;
  slug: string;
  task_type: TaskType;
  status: DatasetStatus;
  label_schema_version: string;
  project_count: number;
  snapshot_count: number;
  latest_snapshot_id?: string | null;
  latest_snapshot_status?: SnapshotStatus | null;
  created_at: string;
}

export interface DatasetCreateRequest {
  name: string;
  slug: string;
  task_type: TaskType;
  label_schema_version: string;
}

export interface UploadSessionCreateRequest {
  expected_file_count: number;
  target_label_project_id?: string | null;
  classification_label?: string | null;
  source_folder_name?: string | null;
}

export interface UploadPartReceipt {
  upload_session_id: string;
  part_no: number;
  file_name: string;
  stored_name: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  width?: number | null;
  height?: number | null;
}

export interface UploadSessionSummary {
  upload_session_id: string;
  dataset_id: string;
  status: 'open' | 'finalizing' | 'completed' | 'failed';
  expected_file_count: number;
  received_file_count: number;
  total_bytes: number;
  content_manifest_sha256?: string | null;
  created_at: string;
  completed_at?: string | null;
}

export interface UploadSessionCompleteRequest {
  content_manifest_sha256?: string | null;
}

export type ClassBalanceStrategy = 'none' | 'weighted_loss' | 'balanced_sampler';

export interface LabelProjectMappingRequest {
  dataset_id: string;
  label_studio_project_id: number;
  title: string;
  task_type: TaskType;
  training_pipeline_slug: string;
  auto_snapshot_enabled: boolean;
  minimum_annotation_count: number;
  required_classes: string[];
  qualifying_actions: string[];
  label_config: Record<string, unknown>;
  train_ratio: number;
  val_ratio: number;
  test_ratio: number;
  class_balance_strategy: ClassBalanceStrategy;
}

export interface AnnotationSchemaSummary {
  annotation_schema_id: string;
  task_type: TaskType;
  title: string;
  description: string;
  target_builder_id: string;
  target_builder_version: string;
  label_config_sha256: string;
  field_names: string[];
}

export interface LabelProjectProvisionRequest {
  dataset_id: string;
  title: string;
  annotation_schema_id: string;
  training_pipeline_slug: string;
  auto_sync_enabled: boolean;
  auto_snapshot_enabled: boolean;
  minimum_annotation_count: number;
  train_ratio: number;
  val_ratio: number;
  test_ratio: number;
  class_balance_strategy: ClassBalanceStrategy;
  idempotency_key?: string | null;
}

export interface LabelProjectSummary {
  label_project_id: string;
  label_studio_project_id: number;
  title: string;
  dataset_id?: string | null;
  dataset_name?: string | null;
  task_type: TaskType;
  status: LabelProjectStatus;
  label_schema_version?: string | null;
  label_config_sha256?: string | null;
  training_pipeline_slug?: string | null;
  auto_sync_enabled: boolean;
  auto_snapshot_enabled: boolean;
  minimum_annotation_count: number;
  required_classes: string[];
  qualifying_actions: string[];
  label_config: Record<string, unknown>;
  train_ratio: number;
  val_ratio: number;
  test_ratio: number;
  class_balance_strategy: ClassBalanceStrategy;
  task_count: number;
  annotated_task_count: number;
  annotation_count: number;
  skipped_annotation_count: number;
  class_distribution: Record<string, number>;
  last_activity_at?: string | null;
  created_at: string;
}

export interface DatasetSnapshotCreateRequest {
  dataset_id: string;
  label_project_id: string;
  export_format: string;
  trigger: string;
  reason?: string | null;
  idempotency_key?: string | null;
}

export interface SplitMetadata {
  strategy: string;
  train_ratio: number;
  val_ratio: number;
  test_ratio: number;
  train_count: number;
  val_count: number;
  test_count: number;
}

export interface ValidationIssue {
  severity: string;
  code: string;
  message: string;
  item_ref?: string | null;
  metadata: Record<string, unknown>;
}

export interface ValidationReport {
  status: ValidationStatus;
  checked_at?: string | null;
  checks: Record<string, boolean>;
  issues: ValidationIssue[];
  warning_count: number;
  error_count: number;
}

export interface SnapshotComparison {
  previous_snapshot_id?: string | null;
  annotation_delta: number;
  item_delta: number;
  checksum_changed: boolean;
  schema_changed: boolean;
  split_changed: boolean;
}

export interface DatasetSnapshotSummary {
  snapshot_id: string;
  dataset_id: string;
  label_project_id?: string | null;
  source_label_studio_project_id?: number | null;
  source_project_title?: string | null;
  snapshot_no: number;
  status: SnapshotStatus;
  export_format: string;
  label_schema_version: string;
  item_count: number;
  annotation_count: number;
  eligible_item_count: number;
  submitted_labeled_item_count: number;
  submitted_background_item_count: number;
  draft_item_count: number;
  invalid_item_count: number;
  content_checksum?: string | null;
  validation_status?: ValidationStatus | null;
  created_at: string;
}

export interface DatasetSnapshotDetail extends DatasetSnapshotSummary {
  manifest_uri?: string | null;
  export_storage_uri?: string | null;
  dvc_rev?: string | null;
  created_by?: string | null;
  source_captured_at?: string | null;
  statistics: Record<string, unknown>;
  split_metadata: SplitMetadata;
  validation_report: ValidationReport;
  comparison?: SnapshotComparison | null;
  processing_error?: string | null;
}

export interface AnnotationStatistics {
  dataset_id?: string | null;
  label_project_id?: string | null;
  label_studio_project_id?: number | null;
  task_count: number;
  annotated_task_count: number;
  annotation_count: number;
  skipped_annotation_count: number;
  unlabeled_task_count: number;
  draft_task_count: number;
  eligible_task_count: number;
  submitted_labeled_task_count: number;
  submitted_background_task_count: number;
  invalid_task_count: number;
  class_distribution: Record<string, number>;
  snapshot_count: number;
  last_activity_at?: string | null;
}

export interface IntegrationStatusSummary {
  name: string;
  category: string;
  state: HealthState;
  configured: boolean;
  reachable: boolean;
  endpoint?: string | null;
  detail: string;
  remediation_hint?: string | null;
  observed_at: string;
}

export interface StoragePathSummary {
  key: string;
  label: string;
  path: string;
  exists: boolean;
  writable: boolean;
}

export interface AuthSupportSummary {
  cookie_name: string;
  csrf_cookie_name: string;
  csrf_header_name: string;
  secure_cookie: boolean;
  session_max_age_seconds: number;
  current_user?: SessionUser | null;
  supported_roles: UserRole[];
  api_key_auth_enabled: boolean;
  api_key_bootstrap_count: number;
}

export interface LabelStudioCredentialStatus {
  configured: boolean;
  token_file_path: string;
  write_target_path?: string | null;
  writable: boolean;
  last_verified?: boolean | null;
  project_count?: number | null;
  message: string;
  updated_at: string;
}

export interface LabelStudioTokenUpdateRequest {
  token: string;
}

export interface LabelStudioTokenUpdateResponse {
  credentials: LabelStudioCredentialStatus;
  meta: ResponseMeta;
}

export interface SettingsStatusResponse {
  environment: string;
  public_base_url: string;
  storage_paths: StoragePathSummary[];
  integrations: IntegrationStatusSummary[];
  deployment_targets: DeploymentTargetSummary[];
  auth: AuthSupportSummary;
  label_studio_credentials: LabelStudioCredentialStatus;
  feature_flags: Record<string, boolean>;
  meta: ResponseMeta;
}

export interface TrainingJobSummary {
  training_job_id: string;
  model_family_slug: string;
  dataset_snapshot_id: string;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  model_template_key?: string | null;
  model_template_version?: number | null;
  recipe_name: string;
  recipe_version: string;
  queue_name: QueueName | string;
  status: TrainingJobStatus;
  progress_percent: number;
  current_epoch: number;
  current_step: number;
  total_epochs: number;
  latest_checkpoint_id?: string | null;
  worker_instance_id?: string | null;
  gpu_device?: string | null;
  trainer_mode?: 'real' | 'simulation' | null;
  trainer_backend?: string | null;
  promotion_ready: boolean;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface TrainingJobCreateRequest {
  model_family_slug: string;
  dataset_snapshot_id: string;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  model_template_key?: string | null;
  model_template_version?: number | null;
  recipe_name: string;
  recipe_version: string;
  max_epochs: number;
  learning_rate: number;
  image_size?: number | null;
  batch_size?: number | null;
  optimizer?: string | null;
  optimizer_settings: Record<string, unknown>;
  checkpoint_interval_steps: number;
  parameter_overrides: Record<string, unknown>;
  resolved_training_defaults: TrainingDefaults;
  best_checkpoint_rule: BestCheckpointRule;
  promotion_criteria: PromotionCriterion[];
  promotion_policy?: PromotionCriteriaPolicy | null;
  allow_duplicate_snapshot_training: boolean;
  idempotency_key?: string | null;
}

export interface BestCheckpointRule {
  metric_name: string;
  mode: 'max' | 'min';
  phase: 'training' | 'validation' | 'evaluation';
  minimum_epoch: number;
}

export interface PromotionCriterion {
  metric_name: string;
  comparison: 'gte' | 'lte';
  threshold: number;
  phase: 'validation' | 'evaluation';
}

export interface PromotionCriterionResult {
  metric_name: string;
  comparison: 'gte' | 'lte';
  threshold: number;
  phase: 'validation' | 'evaluation';
  actual_value?: number | null;
  passed: boolean;
}

export interface PromotionCriteriaPolicy {
  criteria: PromotionCriterion[];
  allow_model_override: boolean;
}

export interface TrainingEvaluationSummary {
  primary_metric_name?: string | null;
  primary_metric_value?: number | null;
  metrics: Record<string, number>;
  promotion_ready: boolean;
  promotion_results: PromotionCriterionResult[];
  best_checkpoint_id?: string | null;
  best_checkpoint_metric?: number | null;
}

export interface TrainingJobCommandRequest {
  reason?: string | null;
  idempotency_key?: string | null;
  force?: boolean;
}

export interface TrainingCheckpointSummary {
  checkpoint_id: string;
  training_job_id: string;
  step_no: number;
  epoch_no: number;
  metric_primary?: number | null;
  checkpoint_kind: string;
  storage_uri: string;
  sha256: string;
  resume_safe: boolean;
  is_best: boolean;
  metrics: Record<string, number>;
  created_at: string;
}

export interface TrainingMetricRecord {
  metric_id: string;
  training_job_id: string;
  phase: string;
  metric_name: string;
  metric_value: number;
  epoch_no: number;
  step_no: number;
  is_primary: boolean;
  checkpoint_id?: string | null;
  recorded_at: string;
}

export interface TrainingJobLogEntry {
  sequence_no: number;
  level: string;
  event_type: string;
  message: string;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export interface TrainingJobEventSummary {
  event_id: string;
  event_type: string;
  sequence_no: number;
  source_service: string;
  actor_type: string;
  actor_id: string;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export interface TrainingJobDetail extends TrainingJobSummary {
  requested_by?: string | null;
  idempotency_key?: string | null;
  allow_duplicate_snapshot_training: boolean;
  cancel_requested: boolean;
  cancel_requested_at?: string | null;
  best_checkpoint_id?: string | null;
  artifact_manifest_uri?: string | null;
  model_version_id?: string | null;
  retry_count: number;
  resume_count: number;
  failure_class?: string | null;
  last_error?: string | null;
  progress: Record<string, unknown>;
  parameter_overrides: Record<string, unknown>;
  resolved_training_defaults: TrainingDefaults;
  best_checkpoint_rule: BestCheckpointRule;
  promotion_criteria: PromotionCriterion[];
  promotion_policy?: PromotionCriteriaPolicy | null;
  evaluation_summary: TrainingEvaluationSummary;
  metrics_summary: Record<string, number>;
  runtime_metadata: Record<string, unknown>;
  artifact_validation: Record<string, unknown>;
  logs: TrainingJobLogEntry[];
  checkpoints: TrainingCheckpointSummary[];
  metrics: TrainingMetricRecord[];
  events: TrainingJobEventSummary[];
}

export interface TrainingMetricComparison {
  metric_name: string;
  values: Record<string, number | null>;
}

export interface TrainingRunComparison {
  jobs: TrainingJobSummary[];
  metrics: TrainingMetricComparison[];
  recommended_job_id?: string | null;
}

export interface ModelFamilySummary {
  model_family_id: string;
  slug: string;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  template_key?: string | null;
  template_version?: number | null;
  resolved_training_defaults: TrainingDefaults;
  promotion_policy?: PromotionCriteriaPolicy | null;
  contract_major: number;
  version_count: number;
  latest_model_version_id?: string | null;
  latest_status?: ModelLifecycleStatus | null;
}

export interface ModelLineageSummary {
  dataset_snapshot_id?: string | null;
  code_revision?: string | null;
  training_job_id?: string | null;
  mlflow_run_id?: string | null;
  dvc_references: Record<string, unknown>;
  reproducibility_metadata: Record<string, unknown>;
}

export interface ModelPromotionSummary {
  promotion_id: string;
  target_path: TargetPath;
  status: PromotionStatus;
  requested_by?: string | null;
  deployment_release_id?: string | null;
  rollback_candidate: boolean;
  rollback_marked_at?: string | null;
  validated_at?: string | null;
  notes?: string | null;
}

export interface DeploymentApprovalRecordSummary {
  approval_record_id: string;
  model_version_id: string;
  target_path: TargetPath;
  approval_scope: DeploymentApprovalScope;
  status: DeploymentApprovalStatus;
  decided_by?: string | null;
  decided_at: string;
  notes?: string | null;
  promotion_id?: string | null;
  deployment_release_id?: string | null;
}

export interface ModelVersionSummary {
  model_version_id: string;
  model_family_id: string;
  model_family_slug: string;
  version_no: number;
  status: ModelLifecycleStatus;
  training_job_id?: string | null;
  dataset_snapshot_id?: string | null;
  architecture: ModelArchitectureSummary;
  model_template_key?: string | null;
  model_template_version?: number | null;
  resolved_training_defaults: TrainingDefaults;
  promotion_policy?: PromotionCriteriaPolicy | null;
  code_revision?: string | null;
  primary_metric_name?: string | null;
  primary_metric_value?: number | null;
  trainer_mode?: 'real' | 'simulation' | null;
  trainer_backend?: string | null;
  alias_names: string[];
  registry_tags: Record<string, string>;
  target_statuses: Record<string, string>;
  rollback_candidate_targets: TargetPath[];
  deployable_for: TargetPath[];
  created_at: string;
}

export interface ModelVersionDetail extends ModelVersionSummary {
  metrics_summary: Record<string, number>;
  evaluation_summary: Record<string, unknown>;
  artifact_manifest_uri?: string | null;
  artifact_manifest: Record<string, unknown>;
  export_manifest_uri?: string | null;
  export_manifest: Record<string, unknown>;
  mlflow_registered_model?: string | null;
  mlflow_model_version?: string | null;
  lineage: ModelLineageSummary;
  immutable_metadata: Record<string, unknown>;
  immutable_metadata_sha256?: string | null;
  artifact_validation: Record<string, unknown>;
  promotions: ModelPromotionSummary[];
  approvals: DeploymentApprovalRecordSummary[];
  archived_at?: string | null;
  archived_by?: string | null;
  archive_reason?: string | null;
  rejected_at?: string | null;
  rejected_by?: string | null;
  rejection_reason?: string | null;
}

export interface ModelVersionComparisonMetric {
  metric_name: string;
  values: Record<string, number | null>;
}

export interface ModelVersionComparisonField {
  field_name: string;
  values: Record<string, string | null>;
}

export interface ModelVersionComparison {
  versions: ModelVersionSummary[];
  metrics: ModelVersionComparisonMetric[];
  lineage_fields: ModelVersionComparisonField[];
}

export interface DeploymentReleaseSummary {
  deployment_release_id: string;
  target_path: TargetPath;
  model_version_id: string;
  architecture: ModelArchitectureSummary;
  triton_model_name: string;
  triton_model_version: number;
  status: DeploymentReleaseStatus;
  serving_enabled: boolean;
  serving_release_id?: string | null;
  activated_at?: string | null;
  rollback_source_release_id?: string | null;
}

export interface DeploymentReleaseDetail extends DeploymentReleaseSummary {
  artifact_manifest_uri?: string | null;
  artifact_manifest: Record<string, unknown>;
  smoke_report_uri?: string | null;
  smoke_report: Record<string, unknown>;
  compatibility_report: Record<string, unknown>;
}

export interface DeploymentModelServingCommandRequest {
  reason?: string | null;
}

export interface DeploymentTargetSummary {
  target_path: TargetPath;
  active_release_id?: string | null;
  previous_release_id?: string | null;
  active_model_version_id?: string | null;
  previous_model_version_id?: string | null;
  active_release_status?: DeploymentReleaseStatus | null;
  updated_at?: string | null;
}

export interface DeploymentPackageSummary {
  model_version_id: string;
  model_family_id: string;
  model_family_slug: string;
  version_no: number;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  status: DeploymentPackageStatus;
  built_at?: string | null;
  serving_manifest_uri?: string | null;
  compatible_targets: TargetPath[];
  error?: string | null;
}

export interface DeploymentPackageDetail extends DeploymentPackageSummary {
  export_manifest_uri?: string | null;
  artifact_manifest_uri?: string | null;
  serving_manifest: Record<string, unknown>;
  export_manifest: Record<string, unknown>;
  artifact_manifest: Record<string, unknown>;
}

export interface DeploymentPackageBuildRequest {
  model_version_id: string;
}

export interface DeploymentCommandRequest {
  model_version_id: string;
  target_path: TargetPath;
  requested_state: 'staged' | 'production';
}

export interface DeploymentSmokeTestRequest {
  reason?: string | null;
}

export interface RollbackCommandRequest {
  reason?: string | null;
}

export interface ModelApprovalRequest {
  target_path: TargetPath;
  approval_scope: DeploymentApprovalScope;
  notes?: string | null;
}

export interface ModelLifecycleCommandRequest {
  reason?: string | null;
}

export interface RollbackTargetRequest {
  target_path: TargetPath;
  enabled: boolean;
  notes?: string | null;
}

export interface ProjectAutomationPolicy {
  auto_sync_enabled: boolean;
  auto_snapshot_enabled: boolean;
  auto_train_enabled: boolean;
  auto_deploy_enabled: boolean;
  default_model_template?: string | null;
  default_model_family_id?: string | null;
  default_training_profile?: string | null;
  preferred_endpoint_mode: EndpointMode;
}

export interface ModelTemplateSummary {
  template_id?: string | null;
  template_key: string;
  label: string;
  description: string;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  display_name_suffix: string;
  default_training_profile: string;
  defaults: ModelTemplateDefaults;
  default_promotion_policy: PromotionCriteriaPolicy;
  template_version: number;
  recommended: boolean;
  active: boolean;
}

export interface ModelTemplateCreateRequest {
  template_key: string;
  label: string;
  description: string;
  task_type: TaskType;
  architecture_family: string;
  architecture_version: string;
  architecture_size: string;
  display_name_suffix: string;
  default_training_profile: string;
  defaults: ModelTemplateDefaults;
  default_promotion_policy?: PromotionCriteriaPolicy | null;
  recommended: boolean;
  active: boolean;
}

export interface ModelTemplateUpdateRequest {
  label?: string | null;
  description?: string | null;
  display_name_suffix?: string | null;
  default_training_profile?: string | null;
  defaults?: ModelTemplateDefaults | null;
  default_promotion_policy?: PromotionCriteriaPolicy | null;
  recommended?: boolean | null;
  active?: boolean | null;
}

export interface ProjectModelCreateRequest {
  display_name?: string | null;
  task_type?: TaskType | null;
  model_template_key: string;
  architecture_family: string;
  architecture_version: string;
  architecture_size: string;
  class_names?: string[];
  segmentation_mode?: 'instance' | 'semantic' | null;
  description?: string | null;
  training_overrides?: TrainingDefaults | null;
  promotion_policy?: PromotionCriteriaPolicy | null;
}

export interface ProjectModelUpdateRequest {
  display_name?: string | null;
  description?: string | null;
  class_names?: string[] | null;
  promotion_policy?: PromotionCriteriaPolicy | null;
}

export interface ProjectTrainRequest {
  model_family_id?: string | null;
  dataset_snapshot_id?: string | null;
  model_template_key?: string | null;
  training_profile_key?: string | null;
  allow_duplicate_snapshot_training: boolean;
}

export interface ProjectDeployRequest {
  model_family_id?: string | null;
  model_version_id?: string | null;
  preferred_endpoint_mode?: EndpointMode | null;
  allow_quality_gate_bypass?: boolean;
}

export interface ProjectReadinessSummary {
  task_count: number;
  annotated_task_count: number;
  annotation_count: number;
  draft_task_count: number;
  eligible_task_count: number;
  submitted_labeled_task_count: number;
  submitted_background_task_count: number;
  invalid_task_count: number;
  minimum_annotation_count: number;
  required_classes: string[];
  class_distribution: Record<string, number>;
  train_ratio: number;
  val_ratio: number;
  test_ratio: number;
  readiness_percent: number;
  annotations_ready: boolean;
  latest_snapshot_ready: boolean;
  latest_valid_snapshot_ready: boolean;
  readiness_reason: string;
}

export interface ProjectModelSummary {
  model_family_id: string;
  label_project_id?: string | null;
  display_name: string;
  slug: string;
  endpoint_name: string;
  description?: string | null;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  class_names?: string[];
  uses_all_classes?: boolean;
  segmentation_mode?: 'instance' | 'semantic' | null;
  template_key?: string | null;
  template_version?: number | null;
  training_profile_key?: string | null;
  training_overrides?: TrainingDefaults | null;
  promotion_policy: PromotionCriteriaPolicy;
  latest_model_version_id?: string | null;
  latest_model_version_no?: number | null;
  latest_model_status?: ModelLifecycleStatus | null;
  latest_training_job_id?: string | null;
  created_at: string;
}

export interface EndpointRouteExample {
  method: string;
  path: string;
  description: string;
}

export interface ProjectEndpointSummary {
  endpoint_name: string;
  label_project_id: string;
  model_family_id: string;
  model_family_slug: string;
  display_name: string;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  class_names?: string[];
  uses_all_classes?: boolean;
  segmentation_mode?: 'instance' | 'semantic' | null;
  preferred_mode: EndpointMode;
  realtime_available: boolean;
  batch_available: boolean;
  realtime_release_id?: string | null;
  batch_release_id?: string | null;
  current_model_version_id?: string | null;
  current_model_version_no?: number | null;
  last_deployed_at?: string | null;
  api_examples: EndpointRouteExample[];
}

export interface ProjectPredictionBackendSummary {
  enabled: boolean;
  backend_id?: number | null;
  backend_url?: string | null;
  status: string;
  model_family_id?: string | null;
  model_family_slug?: string | null;
  model_version_id?: string | null;
  model_version_no?: number | null;
  last_linked_at?: string | null;
  last_error?: string | null;
  auto_update: boolean;
}

export interface PipelineActivityItem {
  occurred_at: string;
  title: string;
  detail: string;
  aggregate_type: string;
  aggregate_id: string;
  event_type: string;
}

export type ModelLifecycleStageState =
  | 'pending'
  | 'running'
  | 'success'
  | 'warning'
  | 'failed'
  | 'blocked'
  | 'not_applicable';

export interface ModelLifecycleStage {
  key: string;
  label: string;
  state: ModelLifecycleStageState;
  progress_percent?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  detail?: string | null;
  error?: string | null;
  activities: PipelineActivityItem[];
}

export interface ProjectModelWorkspaceDetail {
  label_project_id: string;
  model: ProjectModelSummary;
  versions: ModelVersionSummary[];
  training_history: TrainingJobSummary[];
  endpoints: ProjectEndpointSummary[];
  lifecycle: ModelLifecycleStage[];
  activity_timeline: PipelineActivityItem[];
}

export interface ProjectPipelineSummary {
  label_project_id: string;
  label_studio_project_id: number;
  title: string;
  task_type: TaskType;
  status: LabelProjectStatus;
  dataset_id?: string | null;
  dataset_name?: string | null;
  training_pipeline_slug?: string | null;
  automation_policy: ProjectAutomationPolicy;
  readiness: ProjectReadinessSummary;
  available_classes?: string[];
  latest_snapshot?: DatasetSnapshotSummary | null;
  latest_valid_snapshot?: DatasetSnapshotSummary | null;
  models: ProjectModelSummary[];
  last_training?: TrainingJobSummary | null;
  current_endpoints: ProjectEndpointSummary[];
  prediction_backend: ProjectPredictionBackendSummary;
  next_recommended_action: string;
  next_action_detail?: string | null;
}

export interface ProjectPipelineDetail extends ProjectPipelineSummary {
  model_templates: ModelTemplateSummary[];
  supported_model_architectures: SupportedModelArchitecture[];
  training_history: TrainingJobSummary[];
  activity_timeline: PipelineActivityItem[];
}

export interface AcceptedJob {
  job_id: string;
  queue: QueueName | string;
  status: string;
  accepted_at: string;
}

export interface WebhookEventSummary {
  event_id: string;
  source: string;
  source_event_key: string;
  action: string;
  label_studio_project_id?: number | null;
  label_project_id?: string | null;
  signature_valid?: boolean | null;
  internal_event_type?: string | null;
  processing_status: WebhookProcessingStatus;
  received_at: string;
  processed_at?: string | null;
}

export interface WebhookEventDetail extends WebhookEventSummary {
  remote_addr?: string | null;
  headers: Record<string, string>;
  payload: Record<string, unknown>;
  processing_error?: string | null;
}

export interface PublicInferenceWarning {
  code: string;
  message: string;
}

export interface PublicImageMetadata {
  file_name?: string | null;
  content_type: string;
  size_bytes: number;
  width?: number | null;
  height?: number | null;
  sha256: string;
}

export interface PublicInferenceTiming {
  received_at: string;
  completed_at: string;
  validation_ms: number;
  routing_ms: number;
  preprocessing_ms: number;
  inference_ms: number;
  postprocessing_ms: number;
  total_ms: number;
}

export interface PublicParameterSpec {
  name: string;
  parameter_type: 'integer' | 'number' | 'string' | 'boolean';
  required: boolean;
  default?: unknown | null;
  minimum?: number | null;
  maximum?: number | null;
  description: string;
}

export interface PublicInputConstraints {
  canonical_form: 'multipart/form-data';
  supported_forms: string[];
  supported_content_types: string[];
  max_images_per_request: number;
  max_image_bytes: number;
  max_request_body_bytes: number;
}

export interface PublicResponseSchema {
  schema_id: string;
  content_type: 'application/json';
}

export interface PublicRealtimeAvailability {
  available: boolean;
  timeout_default_ms?: number | null;
  timeout_max_ms?: number | null;
  queueing: 'none';
  overload_behavior: 'reject';
}

export interface PublicBatchAvailability {
  available: boolean;
  timeout_default_seconds?: number | null;
  timeout_max_seconds?: number | null;
  idempotency_required: boolean;
  resumable: boolean;
}

export interface PublicModelAvailability {
  realtime: PublicRealtimeAvailability;
  batch: PublicBatchAvailability;
}

export interface PublicModelSummary {
  model_id: string;
  display_name: string;
  description?: string | null;
  model_family: string;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  model_version?: number | null;
  availability: PublicModelAvailability;
}

export interface PublicModelDetail extends PublicModelSummary {
  input_constraints: PublicInputConstraints;
  response_schema: PublicResponseSchema;
  supported_parameters: PublicParameterSpec[];
  class_labels: string[];
}

export interface PublicModelListResponse {
  api_version: 'v1';
  schema_version: 'inference.public.models.v1';
  request_id: string;
  correlation_id?: string | null;
  items: PublicModelSummary[];
}

export interface PublicModelDetailResponse {
  api_version: 'v1';
  schema_version: 'inference.public.model-detail.v1';
  request_id: string;
  correlation_id?: string | null;
  item: PublicModelDetail;
}

export interface PublicClassificationPrediction {
  label: string;
  score: number;
}

export interface PublicBoundingBox {
  x_min: number;
  y_min: number;
  x_max: number;
  y_max: number;
}

export interface PublicOrientedPoint {
  x: number;
  y: number;
}

export interface PublicOrientedBoundingBox {
  points: PublicOrientedPoint[];
  rotation_degrees?: number | null;
}

export interface PublicCoordinateSystem {
  name: 'normalized_xyxy';
  origin: 'top_left';
  normalized: boolean;
  description: string;
}

export interface PublicDetection {
  label: string;
  class_id?: number | null;
  confidence: number;
  bounding_box: PublicBoundingBox;
  oriented_box?: PublicOrientedBoundingBox | null;
}

export interface PublicSegmentationPoint {
  x: number;
  y: number;
}

export interface PublicSegmentation {
  label: string;
  class_id?: number | null;
  confidence: number;
  polygon: PublicSegmentationPoint[];
}

export interface PublicAnomalyHeatmap {
  format: 'png_base64';
  data: string;
  width: number;
  height: number;
  min_value?: number | null;
  max_value?: number | null;
  colormap: string;
}

export interface PublicAnomalyMask {
  format: 'png_base64';
  data: string;
  width: number;
  height: number;
  threshold?: number | null;
}

export interface PublicClassificationResponse {
  api_version: 'v1';
  schema_version: 'inference.public.classification-response.v1';
  request_id: string;
  correlation_id?: string | null;
  model_id: string;
  model_family: string;
  model_version: number;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  input: PublicImageMetadata;
  timing: PublicInferenceTiming;
  warnings: PublicInferenceWarning[];
  predictions: PublicClassificationPrediction[];
}

export interface PublicDetectionResponse {
  api_version: 'v1';
  schema_version: 'inference.public.detection-response.v1';
  request_id: string;
  correlation_id?: string | null;
  model_id: string;
  model_family: string;
  model_version: number;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  input: PublicImageMetadata;
  timing: PublicInferenceTiming;
  warnings: PublicInferenceWarning[];
  coordinate_system: PublicCoordinateSystem;
  detections: PublicDetection[];
}

export interface PublicSegmentationResponse {
  api_version: 'v1';
  schema_version: 'inference.public.segmentation-response.v1';
  request_id: string;
  correlation_id?: string | null;
  model_id: string;
  model_family: string;
  model_version: number;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  input: PublicImageMetadata;
  timing: PublicInferenceTiming;
  warnings: PublicInferenceWarning[];
  image_width: number;
  image_height: number;
  segments: PublicSegmentation[];
}

export interface PublicAnomalyResponse {
  api_version: 'v1';
  schema_version: 'inference.public.anomaly-response.v1';
  request_id: string;
  correlation_id?: string | null;
  model_id: string;
  model_family: string;
  model_version: number;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  input: PublicImageMetadata;
  timing: PublicInferenceTiming;
  warnings: PublicInferenceWarning[];
  anomaly_score: number;
  predicted_label: 'normal' | 'anomalous';
  is_anomalous: boolean;
  threshold?: number | null;
  heatmap: PublicAnomalyHeatmap;
  mask?: PublicAnomalyMask | null;
  image_width: number;
  image_height: number;
}

export interface InferenceTiming {
  received_at: string;
  validation_ms: number;
  routing_ms: number;
  preprocessing_ms: number;
  inference_ms: number;
  postprocessing_ms: number;
  total_ms: number;
  shared_memory_transport: boolean;
}

export interface LatencyQuantiles {
  sample_count: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  throughput_per_second: number;
}

export interface ModelRouteRef {
  deployment_release_id: string;
  target_path: TargetPath;
  release_status: DeploymentReleaseStatus;
  model_family_slug: string;
  model_version_id: string;
  task_type: TaskType;
  triton_model_name: string;
  triton_model_version: number;
  serving_artifact_format?: 'tensorrt_plan' | 'onnx' | null;
  contract_major: number;
}

export interface RealtimeInferenceRequest {
  deployment_target: string;
  image_base64: string;
  deployment_release_id?: string | null;
  timeout_ms?: number | null;
}

export interface RealtimeClassifyRequest extends RealtimeInferenceRequest {
  top_k?: number;
}

export interface RealtimeDetectRequest extends RealtimeInferenceRequest {
  max_detections?: number;
  score_threshold?: number | null;
}

export interface RealtimeSegmentRequest extends RealtimeInferenceRequest {
  max_segments?: number;
  score_threshold?: number | null;
  return_masks?: boolean;
}

export interface RealtimeAnomalyRequest extends RealtimeInferenceRequest {
  threshold?: number | null;
  return_mask?: boolean;
}

export interface ClassificationLabelScore {
  label: string;
  score: number;
  rank: number;
}

export interface DetectionBox {
  x_min: number;
  y_min: number;
  x_max: number;
  y_max: number;
}

export interface DetectionPoint {
  x: number;
  y: number;
}

export interface OrientedDetectionBox {
  points: DetectionPoint[];
  rotation_degrees?: number | null;
}

export interface DetectionPrediction {
  label: string;
  score: number;
  box: DetectionBox;
  oriented_box?: OrientedDetectionBox | null;
}

export interface SegmentationPoint {
  x: number;
  y: number;
}

export interface SegmentationPrediction {
  label: string;
  score: number;
  polygon: SegmentationPoint[];
  mask?: SegmentationMask | null;
}

export interface SegmentationMask {
  format: 'png_base64';
  data: string;
  width: number;
  height: number;
  native_width?: number | null;
  native_height?: number | null;
}

export type BarkSideMapping = 'left_first' | 'left_last';

export interface StemBarkJob {
  schema_version: 'stem.bark-job.v1';
  csv_text: string;
  csv_filename: string;
  side_mapping?: BarkSideMapping | null;
}

export interface BarkSideMeasurement {
  reference: {
    side: 'left' | 'right'; slice_role: 'first' | 'last'; side_mapping: BarkSideMapping;
    mapping_source: 'pipeline' | 'job'; workpiece_id: string; csv_sha256: string;
    leading_end: 'S' | 'Z';
    slice: { csv_line: number; position_mm: number; points_mm: [number, number][] };
  };
  valid: boolean; mean_mm: number | null; min_mm: number | null; max_mm: number | null;
  valid_fraction: number; maximum_invalid_gap_deg: number; reason_codes: string[];
  samples: { angle_deg: number; valid: boolean; thickness_mm: number | null; reason?: string | null }[];
  registration_error_mm?: number | null; registration_stability_mm?: number | null;
  effective_source_mm_per_pixel?: number | null;
}

export interface BarkMeasurementResult {
  schema_version: 'stem.bark-measurement.v1'; workpiece_id: string; valid: boolean;
  mean_mm: number | null; metrology_status: 'not_validated'; metrology_label: string;
  left: BarkSideMeasurement; right: BarkSideMeasurement;
}

export interface AnomalyHeatmap {
  format: 'png_base64';
  data: string;
  width: number;
  height: number;
  min_value?: number | null;
  max_value?: number | null;
  colormap: string;
}

export interface AnomalyMask {
  format: 'png_base64';
  data: string;
  width: number;
  height: number;
  threshold: number;
}

export interface RealtimeInferenceResponse {
  schema_version: 'inference.response.v1';
  request_id: string;
  correlation_id?: string | null;
  deployment_target: string;
  route: ModelRouteRef;
  architecture: ModelArchitectureSummary;
  warm_model: boolean;
  timing: InferenceTiming;
  task_type: TaskType;
  labels: ClassificationLabelScore[];
  detections: DetectionPrediction[];
  image_width?: number | null;
  image_height?: number | null;
  segments: SegmentationPrediction[];
  anomaly_score?: number | null;
  predicted_label?: 'normal' | 'anomalous' | null;
  is_anomalous?: boolean | null;
  threshold?: number | null;
  heatmap?: AnomalyHeatmap | null;
  mask?: AnomalyMask | null;
}

export interface ClassificationInferenceResponse {
  schema_version: 'inference.classification.response.v1';
  request_id: string;
  correlation_id?: string | null;
  deployment_target: string;
  route: ModelRouteRef;
  architecture: ModelArchitectureSummary;
  warm_model: boolean;
  timing: InferenceTiming;
  labels: ClassificationLabelScore[];
}

export interface DetectionInferenceResponse {
  schema_version: 'inference.detection.response.v1';
  request_id: string;
  correlation_id?: string | null;
  deployment_target: string;
  route: ModelRouteRef;
  architecture: ModelArchitectureSummary;
  warm_model: boolean;
  timing: InferenceTiming;
  detections: DetectionPrediction[];
}

export interface SegmentationInferenceResponse {
  schema_version: 'inference.segmentation.response.v1';
  request_id: string;
  correlation_id?: string | null;
  deployment_target: string;
  route: ModelRouteRef;
  architecture: ModelArchitectureSummary;
  warm_model: boolean;
  timing: InferenceTiming;
  image_width: number;
  image_height: number;
  segments: SegmentationPrediction[];
}

export interface AnomalyInferenceResponse {
  schema_version: 'inference.anomaly.response.v1';
  request_id: string;
  correlation_id?: string | null;
  deployment_target: string;
  route: ModelRouteRef;
  architecture: ModelArchitectureSummary;
  warm_model: boolean;
  timing: InferenceTiming;
  anomaly_score: number;
  predicted_label: 'normal' | 'anomalous';
  is_anomalous: boolean;
  threshold: number;
  heatmap: AnomalyHeatmap;
  mask?: AnomalyMask | null;
  image_width: number;
  image_height: number;
  model_metadata: Record<string, unknown>;
}

export interface BatchInputItem {
  item_id: string;
  image_base64: string;
  external_ref?: string | null;
  metadata: Record<string, unknown>;
}

export interface BatchJobCreateRequest {
  deployment_target: string;
  deployment_release_id?: string | null;
  input_manifest_uri?: string | null;
  inputs?: BatchInputItem[];
  chunk_size?: number | null;
  timeout_seconds?: number | null;
  idempotency_key?: string | null;
}

export interface BatchJobProgress {
  total_items: number;
  completed_items: number;
  failed_items: number;
  total_chunks: number;
  queued_chunks: number;
  running_chunks: number;
  completed_chunks: number;
  failed_chunks: number;
  progress_percent: number;
}

export interface BatchChunkSummary {
  chunk_id: string;
  chunk_no: number;
  status: BatchChunkStatus;
  retry_count: number;
  input_uri: string;
  output_uri?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface BatchArtifactSummary {
  manifest_uri?: string | null;
  results_uri?: string | null;
  jsonl_uri?: string | null;
  bundle_uri?: string | null;
  download_path?: string | null;
  recoverable: boolean;
  incomplete_chunk_nos: number[];
}

export interface BatchJobSummary {
  schema_version: 'inference.batch.job-summary.v1';
  batch_job_id: string;
  deployment_target: string;
  deployment_release_id: string;
  status: BatchJobStatus;
  created_at: string;
  progress: BatchJobProgress;
}

export interface BatchJobDetail extends BatchJobSummary {
  route: ModelRouteRef;
  input_manifest_uri: string;
  output_manifest_uri?: string | null;
  submitted_by?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  resumable: boolean;
  chunks: BatchChunkSummary[];
  artifacts: BatchArtifactSummary;
  metrics: LatencyQuantiles;
}

export interface BatchResultRecord {
  item_id: string;
  external_ref?: string | null;
  status: 'completed' | 'failed';
  labels: ClassificationLabelScore[];
  detections: DetectionPrediction[];
  image_width?: number | null;
  image_height?: number | null;
  segments: SegmentationPrediction[];
  anomaly_score?: number | null;
  predicted_label?: 'normal' | 'anomalous' | null;
  is_anomalous?: boolean | null;
  threshold?: number | null;
  heatmap?: AnomalyHeatmap | null;
  mask?: AnomalyMask | null;
  timing?: InferenceTiming | null;
  error?: string | null;
}

export interface BatchResultResponse {
  schema_version: 'inference.batch.results.v1';
  batch_job_id: string;
  route: ModelRouteRef;
  status: BatchJobStatus;
  items: BatchResultRecord[];
  artifacts: BatchArtifactSummary;
  metrics: LatencyQuantiles;
  meta: ResponseMeta;
}

export interface ModelCapabilitySummary {
  deployment_release_id: string;
  target_path: TargetPath;
  active: boolean;
  rollback_candidate: boolean;
  model_family_slug: string;
  model_version_id: string;
  task_type: TaskType;
  architecture: ModelArchitectureSummary;
  contract_major: number;
  input_schema_ref?: string | null;
  output_schema_ref?: string | null;
  class_labels: string[];
  compatible_targets: TargetPath[];
  max_batch_size: number;
  confidence_threshold_default: number;
  preprocessing: Record<string, unknown>;
  postprocessing: Record<string, unknown>;
  capabilities: Record<string, unknown>;
  anomaly: Record<string, unknown>;
}

export interface ModelCapabilitiesResponse {
  schema_version: 'inference.model-capabilities.v1';
  items: ModelCapabilitySummary[];
  meta: ResponseMeta;
}

export interface ModelStatusSummary {
  route: ModelRouteRef;
  active: boolean;
  rollback_candidate: boolean;
  warm_model: boolean;
  overloaded: boolean;
  current_inflight: number;
  concurrency_limit: number;
  last_request_at?: string | null;
  latency: LatencyQuantiles;
  backend_statistics: Record<string, number>;
}

export interface ModelStatusResponse {
  schema_version: 'inference.model-status.v1';
  items: ModelStatusSummary[];
  meta: ResponseMeta;
}

export interface ApiError {
  code: string;
  message: string;
  detail?: string | null;
  status_code: number;
  correlation_id?: string | null;
  metadata: Record<string, unknown>;
}

export type RuntimeArea = { id: string; name: string };
export type RuntimeFacility = RuntimeArea & { area_id: string };
export type RuntimePlaces = { areas: RuntimeArea[]; facilities: RuntimeFacility[] };
export interface EdgeSettingsCommand {
  request_id: string;
  expected_revision: string;
  values: Record<string, string>;
}

export interface EdgeSettingsStatus {
  supported: boolean;
  writable: boolean;
  message?: string | null;
  boot_id?: string | null;
  revision?: string | null;
  values: Record<string, string>;
  request_id?: string | null;
  phase: 'idle' | 'queued' | 'applying' | 'succeeded' | 'failed';
  last_error?: string | null;
}
