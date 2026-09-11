from __future__ import annotations

from datetime import datetime
from typing import Any

from cv_status import (
    BatchChunkStatus,
    BatchJobStatus,
    DatasetStatus,
    DeploymentApprovalScope,
    DeploymentApprovalStatus,
    DeploymentPackageStatus,
    DeploymentReleaseStatus,
    HealthState,
    LabelProjectStatus,
    ModelLifecycleStatus,
    PromotionStatus,
    RuntimeChannelDesiredState,
    RuntimeControlCommand,
    RuntimeControlCommandStatus,
    RuntimeExecutionStatus,
    RuntimeMode,
    RuntimeObservedState,
    RuntimePipelineStatus,
    RuntimeResourceStatus,
    RuntimeSourceType,
    SnapshotStatus,
    TargetPath,
    TaskType,
    TrainingJobStatus,
    UploadSessionStatus,
    UserRole,
    WebhookProcessingStatus,
)
from cv_utils import utc_now
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cv_db.base import Base, NamedPrimaryKeyMixin, TimestampMixin


class User(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text())
    password_hash: Mapped[str] = mapped_column(Text(), nullable=False)
    role: Mapped[str] = mapped_column(String(64), default=UserRole.OPERATOR.value)
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_version: Mapped[int] = mapped_column(Integer(), default=1, nullable=False)


class ApiKey(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "api_keys"

    record_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text())
    intended_usage: Mapped[str | None] = mapped_column(Text())
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(512), nullable=False)
    hashed_secret: Mapped[str] = mapped_column(Text(), nullable=False)
    secret_prefix: Mapped[str | None] = mapped_column(String(16))
    endpoint_mode_hint: Mapped[str | None] = mapped_column(String(32))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="managed", index=True)
    created_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked_by: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Dataset(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "datasets"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), default=TaskType.CLASSIFICATION.value)
    label_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=DatasetStatus.DRAFT.value)
    created_by: Mapped[str | None] = mapped_column(String(64))


class UploadSession(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "upload_sessions"

    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default=UploadSessionStatus.OPEN.value)
    expected_file_count: Mapped[int] = mapped_column(Integer(), default=0)
    received_file_count: Mapped[int] = mapped_column(Integer(), default=0)
    total_bytes: Mapped[int] = mapped_column(Integer(), default=0)
    content_manifest_sha256: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DatasetAsset(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "dataset_assets"

    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    upload_session_id: Mapped[str | None] = mapped_column(String(64))
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer(), default=0)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    width: Mapped[int | None] = mapped_column(Integer())
    height: Mapped[int | None] = mapped_column(Integer())


class LabelProject(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "label_projects"
    __table_args__ = (
        UniqueConstraint("label_studio_project_id", name="uq_label_project_external_id"),
    )

    dataset_id: Mapped[str | None] = mapped_column(String(64), index=True)
    label_studio_project_id: Mapped[int] = mapped_column(Integer(), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    label_config: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    label_schema_version: Mapped[str | None] = mapped_column(String(32))
    training_pipeline_slug: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default=LabelProjectStatus.DISCOVERED.value)
    auto_sync_enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    auto_snapshot_enabled: Mapped[bool] = mapped_column(Boolean(), default=False)
    auto_train_enabled: Mapped[bool] = mapped_column(Boolean(), default=False)
    auto_deploy_enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    default_model_template: Mapped[str | None] = mapped_column(String(120))
    default_model_family_id: Mapped[str | None] = mapped_column(String(64), index=True)
    default_training_profile: Mapped[str | None] = mapped_column(String(120))
    preferred_endpoint_mode: Mapped[str] = mapped_column(String(32), default="realtime")
    prediction_backend_enabled: Mapped[bool] = mapped_column(Boolean(), default=False)
    prediction_backend_id: Mapped[int | None] = mapped_column(Integer())
    prediction_backend_url: Mapped[str | None] = mapped_column(String(512))
    prediction_backend_status: Mapped[str] = mapped_column(String(32), default="not_configured")
    prediction_model_family_id: Mapped[str | None] = mapped_column(String(64))
    prediction_model_version_id: Mapped[str | None] = mapped_column(String(64))
    prediction_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prediction_last_error: Mapped[str | None] = mapped_column(Text())
    minimum_annotation_count: Mapped[int] = mapped_column(Integer(), default=1)
    required_classes: Mapped[list[str]] = mapped_column(JSON(), default=list)
    train_ratio: Mapped[int] = mapped_column(Integer(), default=80)
    val_ratio: Mapped[int] = mapped_column(Integer(), default=10)
    test_ratio: Mapped[int] = mapped_column(Integer(), default=10)
    class_balance_strategy: Mapped[str] = mapped_column(String(32), default="none")
    qualifying_actions: Mapped[list[str]] = mapped_column(JSON(), default=list)
    task_count: Mapped[int] = mapped_column(Integer(), default=0)
    annotated_task_count: Mapped[int] = mapped_column(Integer(), default=0)
    annotation_count: Mapped[int] = mapped_column(Integer(), default=0)
    skipped_annotation_count: Mapped[int] = mapped_column(Integer(), default=0)
    draft_task_count: Mapped[int] = mapped_column(Integer(), default=0)
    eligible_task_count: Mapped[int] = mapped_column(Integer(), default=0)
    submitted_labeled_task_count: Mapped[int] = mapped_column(Integer(), default=0)
    submitted_background_task_count: Mapped[int] = mapped_column(Integer(), default=0)
    invalid_task_count: Mapped[int] = mapped_column(Integer(), default=0)
    class_distribution: Mapped[dict[str, int]] = mapped_column(JSON(), default=dict)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str | None] = mapped_column(String(64))
    delete_reason: Mapped[str | None] = mapped_column(Text())


class LabelProjectWebhook(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "label_project_webhooks"

    label_project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    secret_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    target_url: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True)


class WebhookEvent(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("source", "source_event_key", name="uq_webhook_source_event"),)

    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    headers: Mapped[dict[str, str]] = mapped_column(JSON(), default=dict)
    remote_addr: Mapped[str | None] = mapped_column(String(128))
    payload_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_valid: Mapped[bool | None] = mapped_column(Boolean())
    label_studio_project_id: Mapped[int | None] = mapped_column(Integer())
    label_project_id: Mapped[str | None] = mapped_column(String(64))
    internal_event_type: Mapped[str | None] = mapped_column(String(160))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_status: Mapped[str] = mapped_column(
        String(32),
        default=WebhookProcessingStatus.ACCEPTED.value,
    )
    processing_error: Mapped[str | None] = mapped_column(Text())


class DatasetSnapshot(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "dataset_snapshots"
    __table_args__ = (
        UniqueConstraint("dataset_id", "snapshot_no", name="uq_dataset_snapshot_no"),
        UniqueConstraint("dataset_id", "idempotency_key", name="uq_dataset_snapshot_idempotency"),
    )

    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label_project_id: Mapped[str | None] = mapped_column(String(64))
    snapshot_no: Mapped[int] = mapped_column(Integer(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=SnapshotStatus.REQUESTED.value)
    item_count: Mapped[int] = mapped_column(Integer(), default=0)
    annotation_count: Mapped[int] = mapped_column(Integer(), default=0)
    eligible_item_count: Mapped[int] = mapped_column(Integer(), default=0)
    submitted_labeled_item_count: Mapped[int] = mapped_column(Integer(), default=0)
    submitted_background_item_count: Mapped[int] = mapped_column(Integer(), default=0)
    draft_item_count: Mapped[int] = mapped_column(Integer(), default=0)
    invalid_item_count: Mapped[int] = mapped_column(Integer(), default=0)
    manifest_uri: Mapped[str | None] = mapped_column(String(512))
    content_sha256: Mapped[str | None] = mapped_column(String(128))
    dvc_rev: Mapped[str | None] = mapped_column(String(128))
    created_by: Mapped[str | None] = mapped_column(String(64))
    export_format: Mapped[str] = mapped_column(String(64), default="JSON")
    label_schema_version: Mapped[str | None] = mapped_column(String(32))
    split_metadata: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    validation_status: Mapped[str | None] = mapped_column(String(32))
    statistics: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    source_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_label_studio_project_id: Mapped[int | None] = mapped_column(Integer())
    source_project_title: Mapped[str | None] = mapped_column(String(200))
    source_event_key: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    export_storage_uri: Mapped[str | None] = mapped_column(String(512))
    processing_error: Mapped[str | None] = mapped_column(Text())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str | None] = mapped_column(String(64))
    delete_reason: Mapped[str | None] = mapped_column(Text())


class TrainingJob(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "training_jobs"

    model_family_id: Mapped[str | None] = mapped_column(String(64))
    model_family_slug: Mapped[str] = mapped_column(String(120), nullable=False)
    dataset_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    architecture_family: Mapped[str] = mapped_column(String(64), nullable=False, default="yolo")
    architecture_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v8")
    architecture_size: Mapped[str] = mapped_column(String(16), nullable=False, default="n")
    architecture_display_name: Mapped[str] = mapped_column(
        String(160),
        nullable=False,
        default="YOLOv8 Nano",
    )
    model_template_key: Mapped[str | None] = mapped_column(String(120))
    model_template_version: Mapped[int | None] = mapped_column(Integer())
    status: Mapped[str] = mapped_column(String(32), default=TrainingJobStatus.QUEUED.value)
    queue_name: Mapped[str] = mapped_column(String(32), default="training")
    requested_by: Mapped[str | None] = mapped_column(String(64))
    recipe_name: Mapped[str] = mapped_column(String(120), nullable=False)
    recipe_version: Mapped[str] = mapped_column(String(32), nullable=False)
    learning_rate: Mapped[float] = mapped_column(default=0.001)
    image_size: Mapped[int | None] = mapped_column(Integer())
    batch_size: Mapped[int | None] = mapped_column(Integer())
    optimizer: Mapped[str | None] = mapped_column(String(120))
    optimizer_settings: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    resolved_training_defaults: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True, index=True)
    submission_hash: Mapped[str | None] = mapped_column(String(128))
    allow_duplicate_snapshot_training: Mapped[bool] = mapped_column(Boolean(), default=False)
    best_checkpoint_rule: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    promotion_criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    promotion_policy: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(128))
    latest_checkpoint_id: Mapped[str | None] = mapped_column(String(64))
    best_checkpoint_id: Mapped[str | None] = mapped_column(String(64))
    worker_instance_id: Mapped[str | None] = mapped_column(String(64))
    worker_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gpu_device: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean(), default=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_by: Mapped[str | None] = mapped_column(String(64))
    retry_count: Mapped[int] = mapped_column(Integer(), default=0)
    resume_count: Mapped[int] = mapped_column(Integer(), default=0)
    artifact_manifest_uri: Mapped[str | None] = mapped_column(String(512))
    evaluation_summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    metrics_summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    model_version_id: Mapped[str | None] = mapped_column(String(64))
    failure_class: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text())
    runtime_metadata: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str | None] = mapped_column(String(64))
    delete_reason: Mapped[str | None] = mapped_column(Text())


class ModelTemplate(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "model_templates"

    template_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text(), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    architecture_family: Mapped[str] = mapped_column(String(64), nullable=False)
    architecture_version: Mapped[str] = mapped_column(String(32), nullable=False)
    architecture_size: Mapped[str] = mapped_column(String(16), nullable=False)
    architecture_display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name_suffix: Mapped[str] = mapped_column(String(80), nullable=False)
    default_training_profile: Mapped[str] = mapped_column(String(120), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer(), default=1, nullable=False)
    training_defaults: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    default_promotion_policy: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    recommended: Mapped[bool] = mapped_column(Boolean(), default=False)
    active: Mapped[bool] = mapped_column(Boolean(), default=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))


class TrainingCheckpoint(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "training_checkpoints"

    training_job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    step_no: Mapped[int] = mapped_column(Integer(), default=0)
    epoch_no: Mapped[int] = mapped_column(Integer(), default=0)
    metric_primary: Mapped[float | None] = mapped_column()
    checkpoint_kind: Mapped[str] = mapped_column(String(32), default="interval")
    storage_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    resume_safe: Mapped[bool] = mapped_column(Boolean(), default=True)
    is_best: Mapped[bool] = mapped_column(Boolean(), default=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)


class TrainingMetric(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "training_metrics"

    training_job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(120), nullable=False)
    metric_value: Mapped[float] = mapped_column(nullable=False)
    epoch_no: Mapped[int] = mapped_column(Integer(), default=0)
    step_no: Mapped[int] = mapped_column(Integer(), default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean(), default=False)


class ModelFamily(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "model_families"

    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    label_project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    display_name: Mapped[str | None] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text())
    template_key: Mapped[str | None] = mapped_column(String(120))
    template_version: Mapped[int | None] = mapped_column(Integer())
    training_profile_key: Mapped[str | None] = mapped_column(String(120))
    training_overrides: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    promotion_policy: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    class_names: Mapped[list[str]] = mapped_column(JSON(), default=list)
    segmentation_mode: Mapped[str | None] = mapped_column(String(32))
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    architecture_family: Mapped[str] = mapped_column(String(64), nullable=False, default="yolo")
    architecture_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v8")
    architecture_size: Mapped[str] = mapped_column(String(16), nullable=False, default="n")
    architecture_display_name: Mapped[str] = mapped_column(
        String(160),
        nullable=False,
        default="YOLOv8 Nano",
    )
    contract_major: Mapped[int] = mapped_column(Integer(), default=1)
    input_schema_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    output_schema_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean(), default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str | None] = mapped_column(String(64))
    delete_reason: Mapped[str | None] = mapped_column(Text())


class ModelVersion(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("model_family_id", "version_no", name="uq_model_family_version"),
    )

    model_family_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer(), nullable=False)
    training_job_id: Mapped[str | None] = mapped_column(String(64))
    dataset_snapshot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default=ModelLifecycleStatus.DRAFT.value)
    architecture_family: Mapped[str] = mapped_column(String(64), nullable=False, default="yolo")
    architecture_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v8")
    architecture_size: Mapped[str] = mapped_column(String(16), nullable=False, default="n")
    architecture_display_name: Mapped[str] = mapped_column(
        String(160),
        nullable=False,
        default="YOLOv8 Nano",
    )
    model_template_key: Mapped[str | None] = mapped_column(String(120))
    model_template_version: Mapped[int | None] = mapped_column(Integer())
    resolved_training_defaults: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    promotion_policy: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_by: Mapped[str | None] = mapped_column(String(64))
    code_revision: Mapped[str | None] = mapped_column(String(128))
    metrics_summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    evaluation_summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    artifact_manifest_uri: Mapped[str | None] = mapped_column(String(512))
    artifact_manifest: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    export_manifest_uri: Mapped[str | None] = mapped_column(String(512))
    export_manifest: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    onnx_uri: Mapped[str | None] = mapped_column(String(512))
    tensorrt_uri: Mapped[str | None] = mapped_column(String(512))
    serving_package_status: Mapped[str] = mapped_column(
        String(32),
        default=DeploymentPackageStatus.MISSING.value,
    )
    serving_package_built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    serving_package_error: Mapped[str | None] = mapped_column(Text())
    serving_manifest_uri: Mapped[str | None] = mapped_column(String(512))
    serving_manifest: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    dvc_rev: Mapped[str | None] = mapped_column(String(128))
    dvc_references: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    reproducibility_metadata: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    immutable_metadata: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    immutable_metadata_sha256: Mapped[str | None] = mapped_column(String(128))
    alias_names: Mapped[list[str]] = mapped_column(JSON(), default=list)
    registry_tags: Mapped[dict[str, str]] = mapped_column(JSON(), default=dict)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(128))
    mlflow_registered_model: Mapped[str | None] = mapped_column(String(128))
    mlflow_model_version: Mapped[str | None] = mapped_column(String(64))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[str | None] = mapped_column(String(64))
    archive_reason: Mapped[str | None] = mapped_column(Text())
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[str | None] = mapped_column(String(64))
    rejection_reason: Mapped[str | None] = mapped_column(Text())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str | None] = mapped_column(String(64))
    delete_reason: Mapped[str | None] = mapped_column(Text())


class ModelPromotion(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "model_promotions"
    __table_args__ = (
        UniqueConstraint("model_version_id", "target_path", name="uq_model_promotion_target"),
    )

    model_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_path: Mapped[str] = mapped_column(String(32), default=TargetPath.REALTIME.value)
    requested_environment: Mapped[str] = mapped_column(String(32), default="dev")
    status: Mapped[str] = mapped_column(String(32), default=PromotionStatus.STAGING_REQUESTED.value)
    requested_by: Mapped[str | None] = mapped_column(String(64))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deployment_release_id: Mapped[str | None] = mapped_column(String(64))
    rollback_candidate: Mapped[bool] = mapped_column(Boolean(), default=False)
    rollback_marked_by: Mapped[str | None] = mapped_column(String(64))
    rollback_marked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeploymentApprovalRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "deployment_approval_records"

    model_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_path: Mapped[str] = mapped_column(String(32), default=TargetPath.REALTIME.value)
    approval_scope: Mapped[str] = mapped_column(String(32), default=DeploymentApprovalScope.STAGING.value)
    status: Mapped[str] = mapped_column(String(32), default=DeploymentApprovalStatus.APPROVED.value)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text())
    promotion_id: Mapped[str | None] = mapped_column(String(64))
    deployment_release_id: Mapped[str | None] = mapped_column(String(64))


class DeploymentRelease(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "deployment_releases"

    target_path: Mapped[str] = mapped_column(String(32), default=TargetPath.REALTIME.value)
    model_family_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    triton_model_name: Mapped[str] = mapped_column(String(160), nullable=False)
    triton_model_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=DeploymentReleaseStatus.REQUESTED.value)
    artifact_manifest_uri: Mapped[str | None] = mapped_column(String(512))
    compatibility_report: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    smoke_report_uri: Mapped[str | None] = mapped_column(String(512))
    smoke_report: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    rollback_source_release_id: Mapped[str | None] = mapped_column(String(64))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str | None] = mapped_column(String(64))
    delete_reason: Mapped[str | None] = mapped_column(Text())


class DeploymentTarget(Base):
    __tablename__ = "deployment_targets"

    target_path: Mapped[str] = mapped_column(String(32), primary_key=True)
    active_release_id: Mapped[str | None] = mapped_column(String(64))
    previous_release_id: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BatchJob(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "batch_jobs"

    deployment_release_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=BatchJobStatus.ACCEPTED.value)
    input_manifest_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    output_manifest_uri: Mapped[str | None] = mapped_column(String(512))
    total_items: Mapped[int] = mapped_column(Integer(), default=0)
    completed_items: Mapped[int] = mapped_column(Integer(), default=0)
    failed_items: Mapped[int] = mapped_column(Integer(), default=0)
    submitted_by: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchJobChunk(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "batch_job_chunks"
    __table_args__ = (UniqueConstraint("batch_job_id", "chunk_no", name="uq_batch_chunk_no"),)

    batch_job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    chunk_no: Mapped[int] = mapped_column(Integer(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=BatchChunkStatus.QUEUED.value)
    retry_count: Mapped[int] = mapped_column(Integer(), default=0)
    input_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    output_uri: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EdgeDevice(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "edge_devices"
    __table_args__ = (
        Index("ix_edge_devices_connectivity", "connectivity_state", "last_seen_at"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    technical_place: Mapped[str | None] = mapped_column(String(160))
    management_ip: Mapped[str] = mapped_column(String(15), unique=True, nullable=False)
    control_port: Mapped[int] = mapped_column(Integer(), default=9443, nullable=False)
    plant_api_port: Mapped[int] = mapped_column(Integer(), default=9444, nullable=False)
    rtsp_port: Mapped[int] = mapped_column(Integer(), default=8554, nullable=False)
    allowed_cidr: Mapped[str] = mapped_column(String(32), nullable=False)
    server_ip: Mapped[str] = mapped_column(String(15), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True, nullable=False)
    connectivity_state: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    reported_health_state: Mapped[str | None] = mapped_column(String(32))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_poll_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_connectivity_error: Mapped[str | None] = mapped_column(Text())
    current_boot_id: Mapped[str | None] = mapped_column(String(128))
    identity_generation: Mapped[int] = mapped_column(Integer(), default=1, nullable=False)
    deployment_revalidation_required: Mapped[bool] = mapped_column(
        Boolean(), default=True, nullable=False
    )
    identity_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON())
    health_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON())
    latest_capability_snapshot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    latest_capability_digest: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class EdgeCapabilitySnapshotRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "edge_capability_snapshots"
    __table_args__ = (
        Index(
            "ix_edge_capability_snapshots_device_observed",
            "edge_device_id",
            "observed_at",
        ),
    )

    edge_device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)


class EdgeFleetCommandRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "edge_fleet_commands"
    __table_args__ = (
        UniqueConstraint(
            "edge_device_id",
            "idempotency_key",
            name="uq_edge_fleet_command_idempotency",
        ),
        Index("ix_edge_fleet_commands_device_created", "edge_device_id", "created_at"),
    )

    edge_device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EdgeResultCursorRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "edge_result_cursors"
    __table_args__ = (
        UniqueConstraint(
            "edge_device_id",
            "boot_id",
            name="uq_edge_result_cursor_device_boot",
        ),
    )

    edge_device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    management_ip: Mapped[str] = mapped_column(String(15), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    last_persisted_sequence: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
    last_acknowledged_sequence: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class EdgeResultReceiptRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "edge_result_receipts"
    __table_args__ = (
        UniqueConstraint(
            "edge_device_id",
            "boot_id",
            "sequence",
            name="uq_edge_result_receipt_device_boot_sequence",
        ),
        Index("ix_edge_result_receipts_channel_created", "channel_id", "created_at"),
    )

    edge_device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    management_ip: Mapped[str] = mapped_column(String(15), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer(), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workload_release_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    result_record_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    envelope_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EdgeViewerLeaseRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "edge_viewer_leases"
    __table_args__ = (
        Index("ix_edge_viewer_leases_channel_expiry", "channel_id", "expires_at"),
        Index("ix_edge_viewer_leases_device_expiry", "edge_device_id", "expires_at"),
    )

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    edge_device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_fps: Mapped[float] = mapped_column(nullable=False)
    max_width: Mapped[int] = mapped_column(Integer(), nullable=False)
    include_overlay: Mapped[bool] = mapped_column(Boolean(), default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class EdgeDiagnosticReportRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "edge_diagnostic_reports"
    __table_args__ = (
        Index("ix_edge_diagnostic_reports_device_generated", "edge_device_id", "generated_at"),
        Index("ix_edge_diagnostic_reports_status_generated", "status", "generated_at"),
    )

    edge_device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128))
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list, nullable=False)
    channels: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list, nullable=False)


class EdgeWorkloadReleaseRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "edge_workload_releases"
    __table_args__ = (
        Index("ix_edge_workload_release_channel_created", "channel_id", "created_at"),
    )

    release_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    edge_device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    capability_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    artifact_sources: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON(), default=list, nullable=False
    )
    admission_report: Mapped[dict[str, Any]] = mapped_column(
        JSON(), default=dict, nullable=False
    )
    artifact_count: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
    total_artifact_bytes: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
    required_node_types: Mapped[list[str]] = mapped_column(JSON(), default=list, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128))


class EdgeDeploymentAttemptRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "edge_deployment_attempts"
    __table_args__ = (
        Index("ix_edge_deployment_channel_created", "channel_id", "created_at"),
        Index("ix_edge_deployment_status_updated", "status", "updated_at"),
    )

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    edge_device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workload_release_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    previous_edge_deployment_id: Mapped[str | None] = mapped_column(String(64))
    command_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    desired_state: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    remote_status: Mapped[str | None] = mapped_column(String(32))
    progress: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class RuntimeSource(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_sources"

    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(64),
        default=RuntimeSourceType.RTSP.value,
        nullable=False,
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    secret_refs: Mapped[dict[str, str]] = mapped_column(JSON(), default=dict)
    validation_status: Mapped[str] = mapped_column(
        String(32),
        default=RuntimeResourceStatus.DRAFT.value,
    )
    health_state: Mapped[str] = mapped_column(String(32), default=HealthState.DOWN.value)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class RuntimeArea(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_areas"
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    name_key: Mapped[str] = mapped_column(String(480), unique=True, nullable=False)


class RuntimeFacility(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_facilities"
    __table_args__ = (UniqueConstraint("area_id", "name_key", name="uq_runtime_facility_area_name"),)
    area_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_areas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    name_key: Mapped[str] = mapped_column(String(480), nullable=False)


class RuntimeChannel(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_channels"

    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    technical_place: Mapped[str] = mapped_column(String(160), nullable=False)
    facility_id: Mapped[str | None] = mapped_column(
        ForeignKey("runtime_facilities.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    description: Mapped[str | None] = mapped_column(Text())
    mode: Mapped[str] = mapped_column(String(32), default=RuntimeMode.REALTIME.value)
    assigned_pipeline: Mapped[str | None] = mapped_column(String(160))
    source_id: Mapped[str | None] = mapped_column(String(64), index=True)
    pipeline_version_id: Mapped[str | None] = mapped_column(String(64), index=True)
    authoring_label_project_ids: Mapped[list[str]] = mapped_column(JSON(), default=list)
    result_history_policy: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    stem_rotation_priority_control: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    execution_target: Mapped[str] = mapped_column(String(32), default="server", nullable=False)
    edge_device_id: Mapped[str | None] = mapped_column(String(64), index=True)
    edge_workload_release_id: Mapped[str | None] = mapped_column(String(64), index=True)
    edge_deployment_id: Mapped[str | None] = mapped_column(String(64), index=True)
    active_runtime_execution_id: Mapped[str | None] = mapped_column(String(64), index=True)
    output_summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    capture_summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    health_summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    performance_summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    desired_state: Mapped[str] = mapped_column(
        String(32),
        default=RuntimeChannelDesiredState.DRAFT.value,
    )
    observed_state: Mapped[str] = mapped_column(
        String(32),
        default=RuntimeObservedState.UNASSIGNED.value,
    )
    health_state: Mapped[str] = mapped_column(String(32), default=HealthState.DOWN.value)
    stream_desired_state: Mapped[str] = mapped_column(
        String(32),
        default="stopped",
        nullable=False,
    )
    stream_actual_state: Mapped[str] = mapped_column(
        String(32),
        default="stopped",
        nullable=False,
    )
    stream_worker_instance_id: Mapped[str | None] = mapped_column(String(128), index=True)
    stream_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stream_last_error: Mapped[str | None] = mapped_column(Text())
    stream_profile: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    stream_metrics: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text())
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str | None] = mapped_column(String(64))
    delete_reason: Mapped[str | None] = mapped_column(Text())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class PipelineDefinition(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "pipeline_definitions"

    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text())
    status: Mapped[str] = mapped_column(String(32), default=RuntimePipelineStatus.DRAFT.value)
    latest_version_id: Mapped[str | None] = mapped_column(String(64))
    published_version_id: Mapped[str | None] = mapped_column(String(64))
    template: Mapped[bool] = mapped_column(Boolean(), default=False)
    scope: Mapped[str] = mapped_column(String(32), default="template", index=True)
    owner_channel_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_template_definition_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class PipelineVersion(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "pipeline_versions"
    __table_args__ = (
        UniqueConstraint(
            "pipeline_definition_id",
            "version_no",
            name="uq_pipeline_definition_version",
        ),
    )

    pipeline_definition_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=RuntimePipelineStatus.DRAFT.value)
    mode: Mapped[str] = mapped_column(String(32), default=RuntimeMode.REALTIME.value)
    graph: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    model_capability_constraints: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    node_contract_versions: Mapped[dict[str, int]] = mapped_column(JSON(), default=dict)
    config_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    change_note: Mapped[str | None] = mapped_column(Text())
    created_by: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[str | None] = mapped_column(String(64))


class PipelineNode(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "pipeline_nodes"
    __table_args__ = (
        UniqueConstraint("pipeline_version_id", "node_key", name="uq_pipeline_node_key"),
    )

    pipeline_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(120), nullable=False)
    node_category: Mapped[str] = mapped_column(String(64), nullable=False)
    node_contract_version: Mapped[int] = mapped_column(Integer(), default=1)
    input_ports: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    output_ports: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    config: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    logical_model_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON())
    position: Mapped[dict[str, float]] = mapped_column(JSON(), default=dict)


class RuntimeExecution(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_executions"
    __table_args__ = (
        Index("ix_runtime_executions_channel_created", "channel_id", "created_at"),
    )

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), default=RuntimeMode.REALTIME.value)
    executor_service: Mapped[str] = mapped_column(String(64), nullable=False)
    executor_instance_id: Mapped[str | None] = mapped_column(String(64))
    assigned_runtime_worker: Mapped[str | None] = mapped_column(String(128), index=True)
    control_command_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(
        String(32),
        default=RuntimeExecutionStatus.REQUESTED.value,
    )
    actual_state: Mapped[str] = mapped_column(
        String(32),
        default=RuntimeObservedState.STARTING.value,
    )
    loading_state: Mapped[str] = mapped_column(String(64), default="not_started")
    desired_state: Mapped[str] = mapped_column(
        String(32),
        default=RuntimeChannelDesiredState.START_REQUESTED.value,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    frame_count: Mapped[int] = mapped_column(Integer(), default=0)
    drop_count: Mapped[int] = mapped_column(Integer(), default=0)
    trigger_count: Mapped[int] = mapped_column(Integer(), default=0)
    last_result_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text())
    health_summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    metrics_summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    runtime_plan: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    recovery_attempts: Mapped[int] = mapped_column(Integer(), default=0)


class RuntimeControlCommandRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_control_commands"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "idempotency_key",
            name="uq_runtime_control_channel_idempotency",
        ),
    )

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    command: Mapped[str] = mapped_column(
        String(32),
        default=RuntimeControlCommand.START.value,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default=RuntimeControlCommandStatus.QUEUED.value,
        nullable=False,
        index=True,
    )
    requested_by: Mapped[str | None] = mapped_column(String(128))
    requested_state: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text())
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    claimed_by: Mapped[str | None] = mapped_column(String(128), index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer(), default=0)
    runtime_execution_id: Mapped[str | None] = mapped_column(String(64), index=True)
    last_error: Mapped[str | None] = mapped_column(Text())


class TriggerEndpoint(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "trigger_endpoints"

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_definition_id: Mapped[str | None] = mapped_column(String(64), index=True)
    pipeline_version_id: Mapped[str | None] = mapped_column(String(64), index=True)
    trigger_node_key: Mapped[str | None] = mapped_column(String(64))
    derived_from_pipeline: Mapped[bool] = mapped_column(Boolean(), default=False)
    display_name: Mapped[str] = mapped_column(String(160), default="Trigger endpoint")
    description: Mapped[str | None] = mapped_column(Text())
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), default="http_put", nullable=False)
    method: Mapped[str] = mapped_column(String(16), default="PUT", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    auth_required: Mapped[bool] = mapped_column(Boolean(), default=False)
    auth_scope: Mapped[str] = mapped_column(
        String(128),
        default="runtime.trigger.invoke",
        nullable=False,
    )
    auth_policy: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    timeout_ms: Mapped[int] = mapped_column(Integer(), default=2500)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer(), default=60)
    burst_limit: Mapped[int] = mapped_column(Integer(), default=4)
    response_policy: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    request_schema: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    response_schema: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    status: Mapped[str] = mapped_column(String(32), default=RuntimeResourceStatus.DRAFT.value)
    last_invoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(32))
    last_latency_ms: Mapped[float | None] = mapped_column()
    last_error_code: Mapped[str | None] = mapped_column(String(120))
    last_error_message: Mapped[str | None] = mapped_column(Text())
    invocation_count: Mapped[int] = mapped_column(Integer(), default=0)
    failure_count: Mapped[int] = mapped_column(Integer(), default=0)
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class CaptureRule(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "capture_rules"
    __table_args__ = (
        UniqueConstraint("channel_id", "rule_key", name="uq_capture_rule_channel_key"),
    )

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_version_id: Mapped[str | None] = mapped_column(String(64), index=True)
    rule_key: Mapped[str] = mapped_column(String(64), nullable=False)
    capture_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    condition: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    target_label_project_ids: Mapped[list[str]] = mapped_column(JSON(), default=list)
    rate_limit: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    dedupe_policy: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    status: Mapped[str] = mapped_column(String(32), default=RuntimeResourceStatus.DRAFT.value)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class OutputConnector(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "output_connectors"
    __table_args__ = (
        Index("ix_output_connectors_channel_type", "channel_id", "connector_type"),
    )

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_version_id: Mapped[str | None] = mapped_column(String(64), index=True)
    connector_type: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_node_key: Mapped[str | None] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    secret_refs: Mapped[dict[str, str]] = mapped_column(JSON(), default=dict)
    delivery_policy: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    status: Mapped[str] = mapped_column(String(32), default=RuntimeResourceStatus.DRAFT.value)
    last_delivery_status: Mapped[str | None] = mapped_column(String(32))
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text())
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class ResultRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "result_records"
    __table_args__ = (
        Index("ix_result_records_channel_completed", "channel_id", "completed_at"),
    )

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_version_id: Mapped[str | None] = mapped_column(String(64), index=True)
    runtime_execution_id: Mapped[str | None] = mapped_column(String(64), index=True)
    trigger_endpoint_id: Mapped[str | None] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), default=RuntimeMode.REALTIME.value, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_type: Mapped[str] = mapped_column(String(64), default="pipeline_execution", index=True)
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    raw_inference_outputs: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    normalized_inference_outputs: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    derived_outputs: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    overlay_primitives: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    trigger_response: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    runtime_metadata: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    raw_outputs_ref: Mapped[str | None] = mapped_column(String(512))
    timing: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    model_routes: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    history_retained: Mapped[bool] = mapped_column(Boolean(), default=False)


class StemOrientationReview(Base, NamedPrimaryKeyMixin, TimestampMixin):
    """Append-only operator review revision for one immutable stem result."""

    __tablename__ = "stem_orientation_reviews"
    __table_args__ = (
        UniqueConstraint(
            "result_record_id",
            "revision",
            name="uq_stem_orientation_review_result_revision",
        ),
        Index(
            "ix_stem_orientation_reviews_result_created",
            "result_record_id",
            "created_at",
        ),
    )

    result_record_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer(), nullable=False)
    review_schema_version: Mapped[str] = mapped_column(
        String(80),
        default="stem.orientation.review.v1",
        nullable=False,
    )
    proposal_plausibility: Mapped[str] = mapped_column(String(16), nullable=False)
    direction_assessment: Mapped[str] = mapped_column(String(16), nullable=False)
    angle_assessment: Mapped[str] = mapped_column(String(24), nullable=False)
    bow_relevance: Mapped[str] = mapped_column(String(24), nullable=False)
    final_position: Mapped[str] = mapped_column(String(16), nullable=False)
    target_frame_start: Mapped[int | None] = mapped_column(Integer())
    target_frame_end: Mapped[int | None] = mapped_column(Integer())
    comment: Mapped[str] = mapped_column(Text(), default="", nullable=False)
    special_case: Mapped[str] = mapped_column(Text(), default="", nullable=False)
    supersedes_review_id: Mapped[str | None] = mapped_column(String(64), index=True)
    reviewer_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reviewer_username: Mapped[str] = mapped_column(String(128), nullable=False)


class RuntimeCaptureRequest(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_capture_requests"

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    capture_rule_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    result_record_id: Mapped[str | None] = mapped_column(String(64), index=True)
    manual_capture_session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    manual_capture_request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    target_label_project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label_studio_project_id: Mapped[int | None] = mapped_column(Integer(), index=True)
    target_project_title: Mapped[str | None] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(32), default="pipeline", index=True)
    capture_mode: Mapped[str] = mapped_column(String(64), default="full_frame", index=True)
    asset_uri: Mapped[str | None] = mapped_column(String(512))
    asset_payload: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    roi: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    digest: Mapped[str | None] = mapped_column(String(128), index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(256), index=True)
    duplicate_of_request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_frame_metadata: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    capture_intent: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    label_studio_task_ids: Mapped[list[int]] = mapped_column(JSON(), default=list)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    retry_count: Mapped[int] = mapped_column(Integer(), default=0)
    max_attempts: Mapped[int] = mapped_column(Integer(), default=3)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utc_now)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    rate_limited_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text())
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON(), default=dict)


class RuntimeManualCaptureSession(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_manual_capture_sessions"

    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_label_project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label_studio_project_id: Mapped[int] = mapped_column(Integer(), nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer(), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="running", index=True)
    operator_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    live_session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capture_count: Mapped[int] = mapped_column(Integer(), default=0)
    failure_count: Mapped[int] = mapped_column(Integer(), default=0)
    last_capture_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_capture_request_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text())
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON(), default=dict)


class RuntimeManualCaptureRequestRecord(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_manual_capture_requests"

    manual_capture_session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel_display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    target_label_project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_project_title: Mapped[str] = mapped_column(String(200), nullable=False)
    label_studio_project_id: Mapped[int] = mapped_column(Integer(), nullable=False)
    capture_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    interval_seconds: Mapped[int | None] = mapped_column(Integer())
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    operator_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    live_session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    runtime_execution_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_frame_metadata: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    asset_uri: Mapped[str | None] = mapped_column(String(512))
    digest: Mapped[str | None] = mapped_column(String(128), index=True)
    label_studio_task_ids: Mapped[list[int]] = mapped_column(JSON(), default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text())
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON(), default=dict)


class RuntimeConnectorDelivery(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_connector_deliveries"
    __table_args__ = (
        Index("ix_runtime_connector_delivery_due", "status", "next_attempt_at"),
        Index("ix_runtime_connector_delivery_channel_created", "channel_id", "created_at"),
    )

    output_connector_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    result_record_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    connector_type: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    source_node_key: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    retry_count: Mapped[int] = mapped_column(Integer(), default=0)
    max_attempts: Mapped[int] = mapped_column(Integer(), default=3)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utc_now)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text())


class RuntimeNodeTypeRegistry(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_node_type_registry"
    __table_args__ = (
        UniqueConstraint(
            "node_type",
            "contract_version",
            name="uq_runtime_node_type_contract",
        ),
    )

    node_type: Mapped[str] = mapped_column(String(120), nullable=False)
    node_category: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_version: Mapped[int] = mapped_column(Integer(), default=1)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text(), default="")
    supported_modes: Mapped[list[str]] = mapped_column(JSON(), default=list)
    input_ports: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    output_ports: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    config_schema_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    property_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list)
    default_config: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    runtime_handler_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    compatibility_constraints: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    active: Mapped[bool] = mapped_column(Boolean(), default=True)


class RuntimeModelCapabilityCache(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "runtime_model_capability_cache"
    __table_args__ = (
        UniqueConstraint(
            "deployment_release_id",
            name="uq_runtime_model_capability_release",
        ),
        Index(
            "ix_runtime_model_capability_ref",
            "model_family_slug",
            "target_path",
            "contract_major",
        ),
    )

    model_family_slug: Mapped[str] = mapped_column(String(120), nullable=False)
    target_path: Mapped[str] = mapped_column(String(32), default=TargetPath.REALTIME.value)
    contract_major: Mapped[int] = mapped_column(Integer(), default=1)
    deployment_release_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_events"

    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer(), nullable=False)
    source_service: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    causation_id: Mapped[str | None] = mapped_column(String(64))
    payload_schema_version: Mapped[int] = mapped_column(Integer(), default=1)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)


class IdempotencyKey(Base, NamedPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),)

    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    response_code: Mapped[int] = mapped_column(Integer(), nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"

    service_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict)
