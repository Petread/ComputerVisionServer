from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from cv_status import EnvironmentName
from pydantic import BaseModel, Field, model_validator


class ServiceRuntimeConfig(BaseModel):
    name: str
    host: str = "0.0.0.0"
    port: int = 8080
    public_base_url: str = "http://localhost:8080"


class DatabaseConfig(BaseModel):
    host: str = "postgres"
    port: int = 5432
    database: str = "cv_platform"
    user: str = "cv_platform"
    password_file: str = "/run/secrets/postgres_app_password"
    pool_size: int = 10

    def password(self) -> str:
        password_path = Path(self.password_file)
        return (
            password_path.read_text(encoding="utf-8").strip()
            if password_path.exists()
            else "cv-platform"
        )

    def sqlalchemy_dsn(self) -> str:
        return (
            f"postgresql+psycopg://{self.user}:{self.password()}@"
            f"{self.host}:{self.port}/{self.database}"
        )


class RedisConfig(BaseModel):
    host: str = "redis"
    port: int = 6379
    database: int = 0
    password_file: str | None = None

    def url(self) -> str:
        password = ""
        if self.password_file:
            secret_path = Path(self.password_file)
            if secret_path.exists():
                password = f":{secret_path.read_text(encoding='utf-8').strip()}@"
        return f"redis://{password}{self.host}:{self.port}/{self.database}"


class StorageRoots(BaseModel):
    state_root: str = ".local/state"
    training_root: str = ".local/state/training"
    serving_root: str = ".local/state/serving"
    audit_root: str = ".local/state/audit"
    logs_root: str = ".local/state/logs"

    def control_root(self) -> Path:
        return Path(self.state_root) / "control"

    def uploads_root(self) -> Path:
        return self.control_root() / "uploads"

    def labelstudio_root(self) -> Path:
        return self.control_root() / "labelstudio"

    def labelstudio_exports_root(self) -> Path:
        return self.labelstudio_root() / "exports"

    def snapshots_root(self) -> Path:
        return self.control_root() / "snapshots"

    def dvc_repo_root(self) -> Path:
        return self.control_root() / "dvc" / "repo"

    def serving_packages_root(self) -> Path:
        return Path(self.serving_root) / "packages"

    def serving_package_root(self, model_version_id: str) -> Path:
        return self.serving_packages_root() / f"mvr_{model_version_id}"

    def triton_root(self) -> Path:
        return Path(self.serving_root) / "triton"

    def triton_repository_root(self, target_path: str) -> Path:
        return self.triton_root() / target_path

    def triton_model_root(self, target_path: str, model_name: str) -> Path:
        return self.triton_repository_root(target_path) / model_name

    def inference_root(self) -> Path:
        return Path(self.state_root) / "inference"

    def batch_root(self) -> Path:
        return self.inference_root() / "batch"

    def runtime_root(self) -> Path:
        return Path(self.state_root) / "runtime"

    def runtime_capture_root(self) -> Path:
        return self.runtime_root() / "captures"

    def runtime_results_root(self) -> Path:
        return self.runtime_root() / "results"

    def runtime_overlays_root(self) -> Path:
        return self.runtime_root() / "overlays"

    def runtime_test_files_root(self) -> Path:
        return self.runtime_root() / "test-files"

    def runtime_channel_root(self, channel_id: str) -> Path:
        return self.runtime_root() / "channels" / f"chn_{channel_id}"

    def runtime_execution_root(self, runtime_execution_id: str) -> Path:
        return self.runtime_root() / "executions" / f"rex_{runtime_execution_id}"

    def edge_workload_root(self) -> Path:
        return self.runtime_root() / "edge-workloads"

    def edge_workload_blob_root(self) -> Path:
        return self.edge_workload_root() / "blobs" / "sha256"

    def quarantine_root(self) -> Path:
        return Path(self.state_root) / "quarantine"

    def quarantine_category_root(self, category: str) -> Path:
        return self.quarantine_root() / category

    def backups_root(self) -> Path:
        return Path(self.state_root) / "backups"

    def batch_job_root(self, batch_job_id: str) -> Path:
        return self.batch_root() / f"btj_{batch_job_id}"

    def batch_job_input_manifest(self, batch_job_id: str) -> Path:
        return self.batch_job_root(batch_job_id) / "input" / "manifest.json"

    def batch_job_chunk_input(self, batch_job_id: str, chunk_no: int) -> Path:
        return self.batch_job_root(batch_job_id) / "chunks" / f"chunk_{chunk_no:04d}.input.json"

    def batch_job_chunk_output(self, batch_job_id: str, chunk_no: int) -> Path:
        return self.batch_job_root(batch_job_id) / "chunks" / f"chunk_{chunk_no:04d}.output.json"

    def batch_job_result_manifest(self, batch_job_id: str) -> Path:
        return self.batch_job_root(batch_job_id) / "results" / "manifest.json"

    def batch_job_result_jsonl(self, batch_job_id: str) -> Path:
        return self.batch_job_root(batch_job_id) / "results" / "results.jsonl"

    def batch_job_result_bundle(self, batch_job_id: str) -> Path:
        return self.batch_job_root(batch_job_id) / "results" / "bundle.zip"

    def dataset_asset_root(self, dataset_id: str) -> Path:
        return self.uploads_root() / f"dts_{dataset_id}" / "assets"

    def upload_session_root(self, dataset_id: str, upload_session_id: str) -> Path:
        return self.uploads_root() / f"dts_{dataset_id}" / f"upl_{upload_session_id}"

    def upload_session_parts_root(self, dataset_id: str, upload_session_id: str) -> Path:
        return self.upload_session_root(dataset_id, upload_session_id) / "parts"

    def upload_session_assembled_root(self, dataset_id: str, upload_session_id: str) -> Path:
        return self.upload_session_root(dataset_id, upload_session_id) / "assembled"

    def upload_session_manifests_root(self, dataset_id: str, upload_session_id: str) -> Path:
        return self.upload_session_root(dataset_id, upload_session_id) / "manifests"

    def upload_session_manifest(self, dataset_id: str, upload_session_id: str) -> Path:
        return self.upload_session_manifests_root(dataset_id, upload_session_id) / "index.json"

    def labelstudio_project_export_root(self, label_project_id: str, snapshot_id: str) -> Path:
        return self.labelstudio_exports_root() / f"lpr_{label_project_id}" / f"dss_{snapshot_id}"

    def snapshot_root(self, snapshot_id: str) -> Path:
        return self.snapshots_root() / f"dss_{snapshot_id}"


class LabelStudioWebhookAuthConfig(BaseModel):
    enabled: bool = False
    header_name: str = "X-Label-Studio-Signature"
    secret_file: str | None = "/run/secrets/labelstudio_webhook_secret"
    allowed_cidrs: list[str] = Field(default_factory=list)

    def secret(self) -> str | None:
        if not self.secret_file:
            return None
        secret_path = Path(self.secret_file)
        if not secret_path.exists():
            return None
        return secret_path.read_text(encoding="utf-8").strip()


class DatasetWorkflowConfig(BaseModel):
    default_export_format: str = "JSON"
    automatic_snapshot_actions: list[str] = Field(
        default_factory=lambda: [
            "ANNOTATION_CREATED",
            "ANNOTATION_UPDATED",
            "ANNOTATIONS_CREATED",
            "REVIEW_CREATED",
            "REVIEW_UPDATED",
        ]
    )
    minimum_annotation_count: int = 1
    required_classes: list[str] = Field(default_factory=list)
    default_train_ratio: int = 80
    default_val_ratio: int = 10
    default_test_ratio: int = 10

    @model_validator(mode="after")
    def validate_ratios(self) -> DatasetWorkflowConfig:
        total = self.default_train_ratio + self.default_val_ratio + self.default_test_ratio
        if total != 100:
            raise ValueError("Dataset split ratios must sum to 100")
        return self


class LabelStudioConfig(BaseModel):
    base_url: str = "http://label-studio:8080"
    token_file: str = "/run/secrets/labelstudio_api_token"
    ml_backend_adapter_url: str = "http://label-studio-ml-adapter:8089"
    ml_backend_timeout_seconds: int = 60
    request_timeout_seconds: int = 30
    export_poll_interval_seconds: int = 3
    export_poll_attempts: int = 40
    local_files_root: str = "/srv/cv-platform"
    local_files_url_prefix: str = "/data/local-files/?d="
    webhook_auth: LabelStudioWebhookAuthConfig = Field(default_factory=LabelStudioWebhookAuthConfig)

    def token(self) -> str:
        token_path = Path(self.token_file)
        return token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""


class MlflowConfig(BaseModel):
    base_url: str = "http://mlflow:5000"
    tracking_uri: str = "http://mlflow:5000"


class DvcConfig(BaseModel):
    repo_root: str = "/workspace/.local/dvc"
    remote_name: str = "local"
    remote_url: str = "/workspace/.local/dvc-remote"


class GpuGuardrailsConfig(BaseModel):
    realtime_reserved_memory_gb: int = 8
    allow_training_start: bool = True
    pause_training_on_slo_pressure: bool = True


class TrainingRuntimeConfig(BaseModel):
    max_parallel_jobs: int = 1
    heartbeat_interval_seconds: int = 5
    stale_after_seconds: int = 30
    checkpoint_interval_steps: int = 25
    safe_retry_limit: int = 2
    job_timeout_seconds: int = 86400
    gpu_devices: list[str] = Field(default_factory=lambda: ["cuda:0"])
    trainer_mode: Literal["real", "simulation"] = "real"
    default_backend: str = "openmmlab-mmyolo"
    simulation_mode_enabled: bool = True
    allow_simulation_deployment: bool = False
    allow_onnx_deployment_fallback: bool = False
    engine_build_stage: Literal["deployment", "training"] = "deployment"
    export_opset_version: int = 13
    checkpoint_keep_count: int = 5
    registration_hook: str = "postgres_model_registry"
    log_tail_limit: int = 250
    default_step_duration_ms: int = 25
    cleanup_temp_on_failure: bool = True
    resume_from_latest_good_checkpoint: bool = True


class TritonEndpointConfig(BaseModel):
    grpc_url: str = "triton-rt:8001"
    http_url: str = "http://triton-rt:8000"
    timeout_seconds: int = 10
    gpu_profile: str = "nvidia-rtx-4500-ada"
    cuda_version: str = "12.4"
    tensorrt_version: str = "10.0"
    instance_group_count: int = Field(default=1, ge=1, le=8)


class TritonConfig(BaseModel):
    realtime: TritonEndpointConfig = Field(default_factory=TritonEndpointConfig)
    batch: TritonEndpointConfig = Field(
        default_factory=lambda: TritonEndpointConfig(
            grpc_url="triton-batch:8001",
            http_url="http://triton-batch:8000",
            instance_group_count=2,
        )
    )


class InferenceLimitsConfig(BaseModel):
    realtime_timeout_ms: int = 1_500
    batch_timeout_seconds: int = 300
    batch_chunk_size: int = 64
    realtime_max_concurrency: int = 4
    realtime_per_release_max_concurrency: int = 2
    batch_max_concurrency: int = 2
    batch_per_release_max_concurrency: int = 1
    realtime_requests_per_minute: int = 240
    batch_requests_per_minute: int = 60
    metrics_window_size: int = 200
    routing_cache_ttl_seconds: int = 5
    warm_ttl_seconds: int = 120


class RuntimePlaneConfig(BaseModel):
    realtime_frame_buffer_size: int = Field(default=2, ge=1, le=30)
    realtime_drop_policy: Literal["drop_oldest", "drop_newest"] = "drop_oldest"
    realtime_pipeline_max_inflight: int = Field(default=2, ge=1, le=16)
    realtime_target_latency_ms: int = Field(default=250, ge=20, le=5_000)
    realtime_max_channels_per_executor: int = Field(default=8, ge=1, le=128)
    trigger_timeout_ms: int = Field(default=2_500, ge=50, le=30_000)
    trigger_max_concurrency: int = Field(default=4, ge=1, le=128)
    interval_min_period_ms: int = Field(default=1_000, ge=100, le=3_600_000)
    executor_heartbeat_interval_seconds: int = Field(default=3, ge=1, le=60)
    executor_stale_after_seconds: int = Field(default=15, ge=2, le=600)
    executor_stop_grace_period_seconds: int = Field(default=15, ge=2, le=600)
    routing_cache_ttl_seconds: int = Field(default=5, ge=1, le=300)
    model_capability_cache_ttl_seconds: int = Field(default=10, ge=1, le=600)
    capture_queue_name: str = "runtime-capture"
    connector_queue_name: str = "runtime-connector"
    capture_max_image_bytes: int = Field(default=5_242_880, ge=1_024, le=104_857_600)
    capture_default_rate_per_minute: int = Field(default=60, ge=1, le=60_000)
    capture_storage_max_bytes: int = Field(
        default=107_374_182_400,
        ge=1_073_741_824,
        le=10_995_116_277_760,
    )
    capture_min_free_bytes: int = Field(
        default=21_474_836_480,
        ge=1_073_741_824,
        le=1_099_511_627_776,
    )
    capture_storage_usage_cache_seconds: int = Field(default=60, ge=5, le=3_600)
    temporal_sample_spool_queue_capacity: int = Field(default=512, ge=8, le=10_000)
    label_studio_submission_mode: Literal["api_upload", "local_file_url"] = "local_file_url"
    plugin_entrypoint_group: str = "cv_platform.runtime_nodes"
    plugin_allowlist: list[str] = Field(default_factory=list)
    inference_gateway_realtime_url: str = "http://inference-gateway-rt:8087"
    inference_gateway_bearer_token_file: str | None = None
    result_endpoint_auth_required: bool = False
    stream_auth_enabled: bool = False
    stream_access_policy: Literal["unauthenticated_lan", "session", "token", "internal_only"] = (
        "unauthenticated_lan"
    )
    stream_media_server_kind: Literal["mediamtx"] = "mediamtx"
    stream_mediamtx_api_url: str = "http://127.0.0.1:9997"
    stream_public_host: str | None = None
    stream_rtsp_port: int = Field(default=8554, ge=1, le=65535)
    stream_webrtc_port: int = Field(default=8889, ge=1, le=65535)
    stream_webrtc_ice_tcp_port: int = Field(default=8189, ge=1, le=65535)
    stream_rtsp_publish_host: str = "127.0.0.1"
    stream_gstreamer_python_bin: str = "/usr/bin/python3"
    stream_bitrate_override_kbps: int | None = Field(default=None, ge=128, le=100_000)
    stream_auto_bitrate_bits_per_pixel: float = Field(default=0.12, ge=0.01, le=2.0)
    stream_auto_bitrate_min_kbps: int = Field(default=2_000, ge=128, le=100_000)
    stream_auto_bitrate_max_kbps: int = Field(default=20_000, ge=128, le=100_000)
    stream_publisher_restart_backoff_initial_seconds: float = Field(
        default=0.25,
        ge=0.05,
        le=30.0,
    )
    stream_publisher_restart_backoff_max_seconds: float = Field(
        default=10.0,
        ge=0.1,
        le=300.0,
    )
    camera_media_runtime_dir: str = "/run/ybb-camera-media"
    camera_media_frame_slots: int = Field(default=4, ge=2, le=16)
    camera_media_reconcile_interval_seconds: float = Field(default=0.5, ge=0.1, le=10.0)
    camera_media_heartbeat_interval_seconds: float = Field(default=1.0, ge=0.25, le=30.0)
    camera_media_stale_after_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    camera_media_source_retry_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    plc_writes_allowed: bool = False

    def inference_gateway_bearer_token(self) -> str | None:
        if not self.inference_gateway_bearer_token_file:
            return None
        token_path = Path(self.inference_gateway_bearer_token_file)
        if not token_path.exists():
            return None
        return token_path.read_text(encoding="utf-8").strip()


class SessionConfig(BaseModel):
    cookie_name: str = "cv_session"
    csrf_cookie_name: str = "cv_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    secure_cookie: bool = False
    same_site: Literal["lax", "strict"] = "lax"
    max_age_seconds: int = 60 * 60 * 12
    secret_file: str = "/run/secrets/session_secret"
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_role: str = "platform_admin"
    bootstrap_admin_password_hash_file: str = "/run/secrets/bootstrap_admin_password_hash"

    def secret(self) -> str:
        secret_path = Path(self.secret_file)
        return (
            secret_path.read_text(encoding="utf-8").strip()
            if secret_path.exists()
            else "dev-insecure-secret"
        )

    def bootstrap_admin_password_hash(self) -> str:
        hash_path = Path(self.bootstrap_admin_password_hash_file)
        if hash_path.exists():
            return hash_path.read_text(encoding="utf-8").strip()
        return ""


class ApiKeyBootstrapConfig(BaseModel):
    key_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    label: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    intended_usage: str | None = Field(default=None, max_length=2000)
    subject_type: str = Field(default="inference_client", min_length=3, max_length=64)
    subject_id: str = Field(min_length=3, max_length=128)
    scopes: list[str] = Field(default_factory=list, min_length=1)
    secret_file: str
    expires_at: str | None = None
    endpoint_mode_hint: Literal["realtime", "batch", "both"] | None = None

    @model_validator(mode="after")
    def validate_scopes(self) -> ApiKeyBootstrapConfig:
        if not self.scopes:
            raise ValueError("api_keys.bootstrap_keys entries must include at least one scope")
        return self

    def secret(self) -> str:
        secret_path = Path(self.secret_file)
        return secret_path.read_text(encoding="utf-8").strip() if secret_path.exists() else ""


class ApiKeyConfig(BaseModel):
    enabled: bool = True
    authorization_header_name: str = "Authorization"
    legacy_header_name: str = "X-API-Key"
    bootstrap_keys: list[ApiKeyBootstrapConfig] = Field(default_factory=list)


class CorsConfig(BaseModel):
    allowed_origins: list[str] = Field(default_factory=list)
    allow_methods: list[str] = Field(
        default_factory=lambda: ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    )
    allow_headers: list[str] = Field(
        default_factory=lambda: [
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-CSRF-Token",
            "X-Request-ID",
            "X-Correlation-ID",
        ]
    )
    allow_credentials: bool = True


class RequestLimitsConfig(BaseModel):
    default_timeout_seconds: int = 30
    upload_timeout_seconds: int = 120
    edge_software_upload_timeout_seconds: int = 3600
    edge_software_upload_limit_bytes: int = 8 * 1024**3 + 1024**2
    default_body_limit_bytes: int = 1_048_576
    inference_body_limit_bytes: int = 3_145_728
    upload_body_limit_bytes: int = 33_554_432
    pipeline_graph_body_limit_bytes: int = 157_286_400
    camera_calibration_body_limit_bytes: int = 67_108_864
    batch_manifest_max_bytes: int = 10_485_760
    max_image_bytes: int = 5_242_880


class UploadSecurityConfig(BaseModel):
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".jpg", ".jpeg", ".png", ".bmp", ".webp"]
    )
    allowed_mime_types: list[str] = Field(
        default_factory=lambda: [
            "image/jpeg",
            "image/png",
            "image/bmp",
            "image/webp",
        ]
    )
    max_files_per_session: int = 50_000
    max_total_bytes: int = 268_435_456_000
    max_file_name_length: int = 160


class TlsConfig(BaseModel):
    enabled: bool = False
    cert_path: str = "/run/secrets/tls_cert.pem"
    key_path: str = "/run/secrets/tls_key.pem"


class LogRetentionConfig(BaseModel):
    operational_days: int = 14
    audit_days: int = 365


class ArtifactRetentionConfig(BaseModel):
    checkpoints_days: int = 30
    training_workspace_days: int = 7
    exports_days: int = 30
    batch_outputs_days: int = 14
    quarantine_days: int = 7
    runtime_operational_result_minutes: int = Field(default=60, ge=5, le=1440)
    runtime_result_prune_batch_size: int = Field(default=1_000, ge=100, le=100_000)
    runtime_delivery_metadata_days: int = Field(default=7, ge=1, le=365)
    runtime_capture_metadata_days: int = Field(default=30, ge=1, le=3650)
    runtime_temporal_sample_days: int = Field(default=30, ge=1, le=3650)


class BackupConfig(BaseModel):
    enabled: bool = True
    metadata_retention_days: int = 30


class ObservabilityThresholdsConfig(BaseModel):
    queue_depth_warning: int = 100
    queue_depth_critical: int = 500
    queue_oldest_age_warning_seconds: int = 300
    queue_oldest_age_critical_seconds: int = 1800
    worker_heartbeat_stale_seconds: int = 90
    disk_usage_warning_percent: int = 80
    disk_usage_critical_percent: int = 90
    gpu_memory_warning_percent: int = 85
    gpu_memory_critical_percent: int = 95


class PlatformSettings(BaseModel):
    env: EnvironmentName = EnvironmentName.DEV
    service: ServiceRuntimeConfig
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    storage: StorageRoots = Field(default_factory=StorageRoots)
    label_studio: LabelStudioConfig = Field(default_factory=LabelStudioConfig)
    dataset_workflow: DatasetWorkflowConfig = Field(default_factory=DatasetWorkflowConfig)
    mlflow: MlflowConfig = Field(default_factory=MlflowConfig)
    dvc: DvcConfig = Field(default_factory=DvcConfig)
    gpu: GpuGuardrailsConfig = Field(default_factory=GpuGuardrailsConfig)
    training: TrainingRuntimeConfig = Field(default_factory=TrainingRuntimeConfig)
    triton: TritonConfig = Field(default_factory=TritonConfig)
    inference_limits: InferenceLimitsConfig = Field(default_factory=InferenceLimitsConfig)
    runtime: RuntimePlaneConfig = Field(default_factory=RuntimePlaneConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    api_keys: ApiKeyConfig = Field(default_factory=ApiKeyConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)
    request_limits: RequestLimitsConfig = Field(default_factory=RequestLimitsConfig)
    uploads: UploadSecurityConfig = Field(default_factory=UploadSecurityConfig)
    tls: TlsConfig = Field(default_factory=TlsConfig)
    log_retention: LogRetentionConfig = Field(default_factory=LogRetentionConfig)
    artifact_retention: ArtifactRetentionConfig = Field(default_factory=ArtifactRetentionConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    observability: ObservabilityThresholdsConfig = Field(
        default_factory=ObservabilityThresholdsConfig
    )

    @model_validator(mode="after")
    def validate_runtime_constraints(self) -> PlatformSettings:
        if self.training.heartbeat_interval_seconds >= self.training.stale_after_seconds:
            raise ValueError(
                "training.stale_after_seconds must be greater than training.heartbeat_interval_seconds"
            )
        if (
            self.runtime.executor_heartbeat_interval_seconds
            >= self.runtime.executor_stale_after_seconds
        ):
            raise ValueError(
                "runtime.executor_stale_after_seconds must be greater than "
                "runtime.executor_heartbeat_interval_seconds"
            )
        if (
            self.runtime.stream_publisher_restart_backoff_initial_seconds
            > self.runtime.stream_publisher_restart_backoff_max_seconds
        ):
            raise ValueError(
                "runtime.stream_publisher_restart_backoff_max_seconds must be greater "
                "than or equal to stream_publisher_restart_backoff_initial_seconds"
            )
        if self.runtime.trigger_timeout_ms > self.request_limits.default_timeout_seconds * 1000:
            raise ValueError(
                "runtime.trigger_timeout_ms must fit inside request_limits.default_timeout_seconds"
            )
        if self.training.job_timeout_seconds <= 0:
            raise ValueError("training.job_timeout_seconds must be greater than zero")
        if self.request_limits.default_timeout_seconds <= 0:
            raise ValueError("request_limits.default_timeout_seconds must be greater than zero")
        if self.request_limits.upload_timeout_seconds < self.request_limits.default_timeout_seconds:
            raise ValueError(
                "request_limits.upload_timeout_seconds must be greater than or equal to request_limits.default_timeout_seconds"
            )
        if (
            self.request_limits.default_body_limit_bytes
            > self.request_limits.upload_body_limit_bytes
        ):
            raise ValueError(
                "request_limits.upload_body_limit_bytes must be greater than or equal to request_limits.default_body_limit_bytes"
            )
        if (
            self.request_limits.pipeline_graph_body_limit_bytes
            < self.request_limits.default_body_limit_bytes
        ):
            raise ValueError(
                "request_limits.pipeline_graph_body_limit_bytes must be greater than or equal to request_limits.default_body_limit_bytes"
            )
        if self.uploads.max_files_per_session <= 0:
            raise ValueError("uploads.max_files_per_session must be greater than zero")
        if self.uploads.max_total_bytes <= self.request_limits.upload_body_limit_bytes:
            raise ValueError(
                "uploads.max_total_bytes must be greater than request_limits.upload_body_limit_bytes"
            )
        if self.gpu.allow_training_start and not self.training.gpu_devices:
            raise ValueError(
                "training.gpu_devices must contain at least one device when training start is enabled"
            )
        if (
            self.observability.disk_usage_warning_percent
            >= self.observability.disk_usage_critical_percent
        ):
            raise ValueError(
                "observability disk usage warning threshold must be lower than the critical threshold"
            )
        if (
            self.observability.gpu_memory_warning_percent
            >= self.observability.gpu_memory_critical_percent
        ):
            raise ValueError(
                "observability GPU memory warning threshold must be lower than the critical threshold"
            )
        if self.observability.queue_depth_warning >= self.observability.queue_depth_critical:
            raise ValueError(
                "observability queue depth warning threshold must be lower than the critical threshold"
            )
        if (
            self.observability.queue_oldest_age_warning_seconds
            >= self.observability.queue_oldest_age_critical_seconds
        ):
            raise ValueError(
                "observability queue age warning threshold must be lower than the critical threshold"
            )
        if self.env == EnvironmentName.PROD:
            _validate_prod_runtime(self)
        return self


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _apply_env_overrides(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    prefix = "CV_PLATFORM__"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix) :].lower().split("__")
        cursor: dict[str, Any] = result
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return result


def load_platform_settings(service_name: str, *, root_dir: Path | None = None) -> PlatformSettings:
    workspace_root = root_dir or Path.cwd()
    env_name = EnvironmentName(os.getenv("ENV", EnvironmentName.DEV.value))
    base_payload = _load_yaml(workspace_root / "config" / "base" / "platform.yaml")
    env_payload = _load_yaml(workspace_root / "config" / env_name.value / "platform.yaml")
    merged = _deep_merge(base_payload, env_payload)
    merged = _apply_env_overrides(merged)
    service_payload = merged.setdefault("service", {})
    service_payload["name"] = service_name
    return PlatformSettings.model_validate({"env": env_name, **merged})


def prepare_runtime_paths(settings: PlatformSettings) -> list[str]:
    required_paths = [
        Path(settings.storage.state_root),
        Path(settings.storage.training_root),
        Path(settings.storage.serving_root),
        Path(settings.storage.audit_root),
        Path(settings.storage.logs_root),
        settings.storage.control_root(),
        settings.storage.uploads_root(),
        settings.storage.labelstudio_root(),
        settings.storage.labelstudio_exports_root(),
        settings.storage.snapshots_root(),
        settings.storage.serving_packages_root(),
        settings.storage.triton_root(),
        settings.storage.batch_root(),
        settings.storage.runtime_root(),
        settings.storage.runtime_capture_root(),
        settings.storage.runtime_results_root(),
        settings.storage.runtime_overlays_root(),
        settings.storage.runtime_test_files_root(),
        settings.storage.backups_root(),
        settings.storage.quarantine_root(),
        Path(settings.dvc.repo_root),
    ]
    created_or_verified: list[str] = []
    for path in required_paths:
        path.mkdir(parents=True, exist_ok=True)
        created_or_verified.append(str(path))
    return created_or_verified


def _validate_prod_runtime(settings: PlatformSettings) -> None:
    if not settings.tls.enabled:
        raise ValueError("tls.enabled must be true in production")
    if not settings.session.secure_cookie:
        raise ValueError("session.secure_cookie must be true in production")
    parsed_base_url = urlparse(settings.service.public_base_url)
    if parsed_base_url.scheme != "https":
        raise ValueError("service.public_base_url must use https in production")
    state_root = Path(settings.storage.state_root)
    if not state_root.is_absolute():
        raise ValueError("storage.state_root must be an absolute path in production")
    if state_root != Path("/srv/cv-platform"):
        raise ValueError("storage.state_root must resolve to /srv/cv-platform in production")
    if settings.training.trainer_mode != "real":
        raise ValueError("training.trainer_mode must be real in production")
    if settings.training.allow_simulation_deployment:
        raise ValueError("training.allow_simulation_deployment must be false in production")
    if settings.training.allow_onnx_deployment_fallback:
        raise ValueError("training.allow_onnx_deployment_fallback must be false in production")
    for path_value in (
        settings.storage.training_root,
        settings.storage.serving_root,
        settings.storage.audit_root,
        settings.storage.logs_root,
    ):
        path = Path(path_value)
        if not path.is_absolute():
            raise ValueError("All configured storage roots must be absolute paths in production")
        if not path.is_relative_to(state_root):
            raise ValueError(
                "All configured storage roots must remain under storage.state_root in production"
            )
    _validate_required_secret(settings.database.password_file, "database.password_file")
    _validate_required_secret(settings.session.secret_file, "session.secret_file")
    if settings.service.name == "admin-api":
        _validate_required_secret(
            settings.session.bootstrap_admin_password_hash_file,
            "session.bootstrap_admin_password_hash_file",
        )
    if (
        settings.label_studio.webhook_auth.enabled
        and settings.label_studio.webhook_auth.secret_file
    ):
        _validate_required_secret(
            settings.label_studio.webhook_auth.secret_file,
            "label_studio.webhook_auth.secret_file",
        )
    for index, bootstrap_key in enumerate(settings.api_keys.bootstrap_keys):
        _validate_required_secret(
            bootstrap_key.secret_file,
            f"api_keys.bootstrap_keys[{index}].secret_file",
        )


def _validate_required_secret(path_value: str, field_name: str) -> None:
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        raise ValueError(
            f"{field_name} must point to an existing mounted secret file in production"
        )
