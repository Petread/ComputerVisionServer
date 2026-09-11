from __future__ import annotations

from typing import Any, Literal

from cv_status import (
    DatasetStatus,
    LabelProjectStatus,
    SnapshotStatus,
    TaskType,
    UploadSessionStatus,
    ValidationStatus,
)
from pydantic import BaseModel, Field

ClassBalanceStrategy = Literal["none", "weighted_loss", "balanced_sampler"]


class DatasetCreateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    task_type: TaskType
    label_schema_version: str = Field(min_length=1, max_length=32)


class UploadSessionCreateRequest(BaseModel):
    expected_file_count: int = Field(ge=1, le=50_000)
    target_label_project_id: str | None = Field(default=None, min_length=1, max_length=64)
    classification_label: str | None = Field(default=None, min_length=1, max_length=200)
    source_folder_name: str | None = Field(default=None, min_length=1, max_length=200)


class UploadSessionCompleteRequest(BaseModel):
    content_manifest_sha256: str | None = Field(default=None, min_length=32, max_length=128)


class UploadPartReceipt(BaseModel):
    upload_session_id: str
    part_no: int = Field(ge=1)
    file_name: str
    stored_name: str
    mime_type: str
    size_bytes: int = Field(ge=1)
    sha256: str
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    status: str = "stored"


class UploadSessionSummary(BaseModel):
    upload_session_id: str
    dataset_id: str
    status: UploadSessionStatus
    expected_file_count: int = Field(ge=0)
    received_file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    content_manifest_sha256: str | None = None
    created_at: str
    completed_at: str | None = None


class DatasetSummary(BaseModel):
    dataset_id: str
    name: str
    slug: str
    task_type: TaskType
    status: DatasetStatus
    label_schema_version: str
    project_count: int = 0
    snapshot_count: int = 0
    latest_snapshot_id: str | None = None
    latest_snapshot_status: SnapshotStatus | None = None
    created_at: str


class LabelProjectMappingRequest(BaseModel):
    dataset_id: str
    label_studio_project_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    task_type: TaskType
    training_pipeline_slug: str = Field(min_length=1, max_length=120)
    auto_snapshot_enabled: bool = False
    minimum_annotation_count: int = Field(default=1, ge=0)
    required_classes: list[str] = Field(default_factory=list)
    train_ratio: int = Field(default=80, ge=0, le=100)
    val_ratio: int = Field(default=10, ge=0, le=100)
    test_ratio: int = Field(default=10, ge=0, le=100)
    class_balance_strategy: ClassBalanceStrategy = "none"
    qualifying_actions: list[str] = Field(default_factory=list)
    label_config: dict[str, Any] = Field(default_factory=dict)


class AnnotationSchemaSummary(BaseModel):
    annotation_schema_id: str
    task_type: TaskType
    title: str
    description: str
    target_builder_id: str
    target_builder_version: str
    label_config_sha256: str = Field(min_length=64, max_length=64)
    field_names: list[str] = Field(default_factory=list)


class LabelProjectProvisionRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=3, max_length=50)
    annotation_schema_id: str = Field(min_length=1, max_length=64)
    training_pipeline_slug: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9-]{0,119}$",
    )
    auto_sync_enabled: bool = True
    auto_snapshot_enabled: bool = False
    minimum_annotation_count: int = Field(default=1, ge=0)
    train_ratio: int = Field(default=80, ge=0, le=100)
    val_ratio: int = Field(default=10, ge=0, le=100)
    test_ratio: int = Field(default=10, ge=0, le=100)
    class_balance_strategy: ClassBalanceStrategy = "none"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class LabelProjectSummary(BaseModel):
    label_project_id: str
    label_studio_project_id: int
    title: str
    dataset_id: str | None = None
    dataset_name: str | None = None
    task_type: TaskType
    status: LabelProjectStatus
    label_schema_version: str | None = None
    label_config_sha256: str | None = None
    training_pipeline_slug: str | None = None
    auto_sync_enabled: bool = True
    auto_snapshot_enabled: bool
    minimum_annotation_count: int
    required_classes: list[str] = Field(default_factory=list)
    qualifying_actions: list[str] = Field(default_factory=list)
    label_config: dict[str, Any] = Field(default_factory=dict)
    train_ratio: int
    val_ratio: int
    test_ratio: int
    class_balance_strategy: ClassBalanceStrategy = "none"
    task_count: int = 0
    annotated_task_count: int = 0
    annotation_count: int = 0
    skipped_annotation_count: int = 0
    class_distribution: dict[str, int] = Field(default_factory=dict)
    last_activity_at: str | None = None
    created_at: str


class DatasetSnapshotCreateRequest(BaseModel):
    dataset_id: str
    label_project_id: str
    export_format: str = Field(default="JSON", min_length=1, max_length=64)
    trigger: str = Field(default="manual", min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=280)
    idempotency_key: str | None = Field(default=None, max_length=200)


class SplitMetadata(BaseModel):
    strategy: str
    train_ratio: int
    val_ratio: int
    test_ratio: int
    train_count: int
    val_count: int
    test_count: int


class ValidationIssue(BaseModel):
    severity: str
    code: str
    message: str
    item_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    status: ValidationStatus
    checked_at: str | None = None
    checks: dict[str, bool] = Field(default_factory=dict)
    issues: list[ValidationIssue] = Field(default_factory=list)
    warning_count: int = 0
    error_count: int = 0


class SnapshotComparison(BaseModel):
    previous_snapshot_id: str | None = None
    annotation_delta: int = 0
    item_delta: int = 0
    checksum_changed: bool = False
    schema_changed: bool = False
    split_changed: bool = False


class DatasetSnapshotSummary(BaseModel):
    snapshot_id: str
    dataset_id: str
    label_project_id: str | None = None
    source_label_studio_project_id: int | None = None
    source_project_title: str | None = None
    snapshot_no: int
    status: SnapshotStatus
    export_format: str
    label_schema_version: str
    item_count: int
    annotation_count: int
    eligible_item_count: int = 0
    submitted_labeled_item_count: int = 0
    submitted_background_item_count: int = 0
    draft_item_count: int = 0
    invalid_item_count: int = 0
    content_checksum: str | None = None
    validation_status: ValidationStatus | None = None
    created_at: str


class DatasetSnapshotDetail(DatasetSnapshotSummary):
    manifest_uri: str | None = None
    export_storage_uri: str | None = None
    dvc_rev: str | None = None
    created_by: str | None = None
    source_captured_at: str | None = None
    statistics: dict[str, Any] = Field(default_factory=dict)
    split_metadata: SplitMetadata
    validation_report: ValidationReport
    comparison: SnapshotComparison | None = None
    processing_error: str | None = None


class AnnotationStatistics(BaseModel):
    dataset_id: str | None = None
    label_project_id: str | None = None
    label_studio_project_id: int | None = None
    task_count: int = 0
    annotated_task_count: int = 0
    annotation_count: int = 0
    skipped_annotation_count: int = 0
    unlabeled_task_count: int = 0
    draft_task_count: int = 0
    eligible_task_count: int = 0
    submitted_labeled_task_count: int = 0
    submitted_background_task_count: int = 0
    invalid_task_count: int = 0
    class_distribution: dict[str, int] = Field(default_factory=dict)
    snapshot_count: int = 0
    last_activity_at: str | None = None
