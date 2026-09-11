from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from cv_contracts import (
    AnnotationStatistics,
    AuditEventDetail,
    AuditEventSummary,
    DatasetCreateRequest,
    DatasetSnapshotCreateRequest,
    DatasetSnapshotDetail,
    DatasetSnapshotSummary,
    DatasetSummary,
    DeploymentReleaseSummary,
    LabelProjectMappingRequest,
    LabelProjectProvisionRequest,
    LabelProjectSummary,
    ModelFamilySummary,
    ModelVersionSummary,
    SnapshotComparison,
    SplitMetadata,
    TrainingJobCreateRequest,
    TrainingJobSummary,
    ValidationReport,
    WebhookEventDetail,
    WebhookEventSummary,
)
from cv_core import generate_prefixed_id
from cv_datasets import DatasetAssetRecord
from cv_status import (
    DatasetStatus,
    DeploymentReleaseStatus,
    LabelProjectStatus,
    ModelLifecycleStatus,
    QueueName,
    SnapshotStatus,
    TaskType,
    TrainingJobStatus,
    ValidationStatus,
    WebhookProcessingStatus,
)
from cv_utils import get_correlation_id, get_request_id, utc_now, utc_now_iso
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cv_db.model_catalog import architecture_summary
from cv_db.models import (
    AuditEvent,
    Dataset,
    DatasetAsset,
    DatasetSnapshot,
    DeploymentRelease,
    LabelProject,
    ModelFamily,
    ModelPromotion,
    ModelVersion,
    TrainingJob,
    WebhookEvent,
)


def create_dataset(
    session: Session,
    payload: DatasetCreateRequest,
    *,
    created_by: str | None = None,
) -> DatasetSummary:
    dataset = Dataset(
        id=generate_prefixed_id("dst"),
        slug=payload.slug,
        name=payload.name,
        task_type=payload.task_type.value,
        label_schema_version=payload.label_schema_version,
        status=DatasetStatus.DRAFT.value,
        created_by=created_by,
    )
    session.add(dataset)
    session.flush()
    return _dataset_summary(dataset)


def list_datasets(session: Session) -> list[DatasetSummary]:
    rows = session.scalars(select(Dataset).order_by(Dataset.created_at.desc())).all()
    project_counts = Counter(
        project.dataset_id
        for project in session.scalars(select(LabelProject).where(LabelProject.deleted_at.is_(None))).all()
        if project.dataset_id
    )
    snapshots = session.scalars(
        select(DatasetSnapshot)
        .where(DatasetSnapshot.deleted_at.is_(None))
        .order_by(DatasetSnapshot.created_at.desc())
    ).all()
    snapshot_counts = Counter(snapshot.dataset_id for snapshot in snapshots)
    latest_snapshot_by_dataset: dict[str, DatasetSnapshot] = {}
    for snapshot in snapshots:
        latest_snapshot_by_dataset.setdefault(snapshot.dataset_id, snapshot)
    return [
        _dataset_summary(
            row,
            project_count=project_counts.get(row.id, 0),
            snapshot_count=snapshot_counts.get(row.id, 0),
            latest_snapshot=latest_snapshot_by_dataset.get(row.id),
        )
        for row in rows
    ]


def get_dataset_record(session: Session, dataset_id: str) -> Dataset | None:
    return session.get(Dataset, dataset_id)


def list_label_projects(session: Session) -> list[LabelProjectSummary]:
    rows = session.scalars(
        select(LabelProject)
        .where(LabelProject.deleted_at.is_(None))
        .order_by(LabelProject.created_at.desc())
    ).all()
    dataset_names = {dataset.id: dataset.name for dataset in session.scalars(select(Dataset)).all()}
    return [_label_project_summary(row, dataset_names.get(row.dataset_id)) for row in rows]


def get_label_project_record(session: Session, label_project_id: str) -> LabelProject | None:
    return session.get(LabelProject, label_project_id)


def get_label_project_by_external_id(
    session: Session,
    label_studio_project_id: int,
    *,
    include_deleted: bool = False,
) -> LabelProject | None:
    statement = select(LabelProject).where(
        LabelProject.label_studio_project_id == label_studio_project_id,
    )
    if not include_deleted:
        statement = statement.where(LabelProject.deleted_at.is_(None))
    return session.scalar(statement)


def get_label_project_for_dataset_schema(
    session: Session,
    *,
    dataset_id: str,
    annotation_schema_id: str,
    include_deleted: bool = False,
) -> LabelProject | None:
    statement = select(LabelProject).where(
        LabelProject.dataset_id == dataset_id,
        LabelProject.label_schema_version == annotation_schema_id,
    )
    if not include_deleted:
        statement = statement.where(LabelProject.deleted_at.is_(None))
    rows = session.scalars(
        statement.order_by(LabelProject.created_at.asc()).limit(2)
    ).all()
    if len(rows) > 1:
        raise ValueError(
            "More than one Label Studio project mapping exists for the dataset and schema."
        )
    return rows[0] if rows else None


def upsert_discovered_label_project(
    session: Session,
    *,
    project_payload: dict[str, Any],
    automatic_snapshot_actions: list[str],
    minimum_annotation_count: int,
    required_classes: list[str],
    split_ratios: tuple[int, int, int],
) -> LabelProject:
    label_studio_project_id = int(project_payload["id"])
    row = get_label_project_by_external_id(
        session,
        label_studio_project_id,
        include_deleted=True,
    )
    label_config = _coerce_label_config(project_payload)
    previous_label_config = row.label_config if row is not None else None
    provisioned_schema_id = _provisioned_annotation_schema_id(previous_label_config)
    if provisioned_schema_id is not None:
        label_config = _merge_provisioned_label_config(previous_label_config, label_config)
    inferred_task_type = _infer_task_type(project_payload)
    label_schema_version = provisioned_schema_id or _label_schema_version_from_config(label_config)
    previous_task_type: TaskType | None = None
    if row is not None and getattr(row, "task_type", None):
        try:
            previous_task_type = TaskType(row.task_type)
        except ValueError:
            previous_task_type = None
    resolved_task_type = _resolve_discovered_task_type(
        inferred_task_type,
        previous_task_type=previous_task_type,
    )
    if row is None:
        row = LabelProject(
            id=generate_prefixed_id("lpr"),
            label_studio_project_id=label_studio_project_id,
            dataset_id=None,
            title=str(project_payload.get("title") or f"Project {label_studio_project_id}"),
            task_type=resolved_task_type.value,
            label_config=label_config,
            label_schema_version=label_schema_version,
            training_pipeline_slug=_slugify(str(project_payload.get("title") or f"project-{label_studio_project_id}")),
            auto_sync_enabled=True,
            auto_snapshot_enabled=True,
            auto_train_enabled=False,
            auto_deploy_enabled=True,
            default_model_template=_default_model_template_for_task(resolved_task_type),
            default_training_profile="safe-default",
            preferred_endpoint_mode="realtime",
            qualifying_actions=automatic_snapshot_actions,
            minimum_annotation_count=minimum_annotation_count,
            required_classes=required_classes,
            train_ratio=split_ratios[0],
            val_ratio=split_ratios[1],
            test_ratio=split_ratios[2],
            class_balance_strategy="none",
            status=LabelProjectStatus.DISCOVERED.value,
        )
        session.add(row)

    row.title = str(project_payload.get("title") or row.title)
    row.task_type = resolved_task_type.value
    row.label_config = label_config
    row.label_schema_version = label_schema_version
    row.task_count = int(
        project_payload.get("task_number")
        or project_payload.get("task_count")
        or project_payload.get("total_tasks")
        or 0
    )
    row.annotated_task_count = int(
        project_payload.get("num_tasks_with_annotations")
        or project_payload.get("finished_task_number")
        or 0
    )
    row.annotation_count = int(project_payload.get("useful_annotation_number") or 0)
    row.skipped_annotation_count = int(project_payload.get("skipped_annotations_number") or 0)
    submitted_task_hint = int(
        project_payload.get("finished_task_number")
        or project_payload.get("num_tasks_with_annotations")
        or 0
    )
    row.eligible_task_count = max(
        int(getattr(row, "eligible_task_count", 0) or 0),
        submitted_task_hint,
    )
    row.submitted_labeled_task_count = max(
        int(getattr(row, "submitted_labeled_task_count", 0) or 0),
        row.annotated_task_count,
    )
    row.submitted_background_task_count = int(
        getattr(row, "submitted_background_task_count", 0) or 0
    )
    row.draft_task_count = max(int(row.task_count or 0) - int(row.eligible_task_count or 0), 0)
    row.invalid_task_count = int(getattr(row, "invalid_task_count", 0) or 0)
    row.training_pipeline_slug = row.training_pipeline_slug or _slugify(row.title)
    row.auto_sync_enabled = bool(getattr(row, "auto_sync_enabled", True))
    row.auto_snapshot_enabled = bool(row.auto_snapshot_enabled if row.auto_snapshot_enabled is not None else True)
    row.auto_train_enabled = bool(getattr(row, "auto_train_enabled", False))
    row.auto_deploy_enabled = bool(getattr(row, "auto_deploy_enabled", True))
    row.default_model_template = _coerce_project_default_template(
        row.default_model_template,
        resolved_task_type,
        previous_task_type=previous_task_type,
    )
    row.default_training_profile = row.default_training_profile or "safe-default"
    row.preferred_endpoint_mode = row.preferred_endpoint_mode or "realtime"
    row.class_balance_strategy = _class_balance_strategy(
        getattr(row, "class_balance_strategy", None)
    )
    row.class_distribution = row.class_distribution or {}
    row.deleted_at = None
    row.deleted_by = None
    row.delete_reason = None
    dataset = _ensure_project_dataset(
        session,
        row=row,
        label_schema_version=label_schema_version,
    )
    row.status = LabelProjectStatus.ACTIVE.value if dataset is not None else LabelProjectStatus.DISCOVERED.value
    session.flush()
    return row


def upsert_label_project_mapping(
    session: Session,
    payload: LabelProjectMappingRequest,
) -> LabelProjectSummary:
    dataset = get_dataset_record(session, payload.dataset_id)
    row = get_label_project_by_external_id(
        session,
        payload.label_studio_project_id,
        include_deleted=True,
    )
    if row is None:
        row = LabelProject(
            id=generate_prefixed_id("lpr"),
            label_studio_project_id=payload.label_studio_project_id,
            title=payload.title,
            task_type=payload.task_type.value,
        )
        session.add(row)

    row.dataset_id = payload.dataset_id
    row.title = payload.title
    row.task_type = payload.task_type.value
    row.label_config = payload.label_config
    row.label_schema_version = dataset.label_schema_version if dataset else None
    row.training_pipeline_slug = payload.training_pipeline_slug
    row.status = LabelProjectStatus.ACTIVE.value
    row.auto_sync_enabled = bool(getattr(row, "auto_sync_enabled", True))
    row.auto_snapshot_enabled = payload.auto_snapshot_enabled
    row.auto_train_enabled = bool(getattr(row, "auto_train_enabled", False))
    row.auto_deploy_enabled = bool(getattr(row, "auto_deploy_enabled", True))
    row.default_model_template = _coerce_project_default_template(
        row.default_model_template,
        payload.task_type,
        previous_task_type=None,
    )
    row.default_training_profile = row.default_training_profile or "safe-default"
    row.preferred_endpoint_mode = row.preferred_endpoint_mode or "realtime"
    row.minimum_annotation_count = payload.minimum_annotation_count
    row.required_classes = payload.required_classes
    row.train_ratio = payload.train_ratio
    row.val_ratio = payload.val_ratio
    row.test_ratio = payload.test_ratio
    row.class_balance_strategy = payload.class_balance_strategy
    row.qualifying_actions = payload.qualifying_actions
    row.deleted_at = None
    row.deleted_by = None
    row.delete_reason = None
    session.flush()
    return _label_project_summary(row, dataset.name if dataset else None)


def upsert_provisioned_label_project(
    session: Session,
    *,
    payload: LabelProjectProvisionRequest,
    label_studio_project_id: int,
    label_config: dict[str, Any],
    qualifying_actions: list[str],
) -> LabelProjectSummary:
    dataset = get_dataset_record(session, payload.dataset_id)
    if dataset is None:
        raise ValueError(f"Dataset {payload.dataset_id} does not exist.")

    row = get_label_project_for_dataset_schema(
        session,
        dataset_id=payload.dataset_id,
        annotation_schema_id=payload.annotation_schema_id,
        include_deleted=True,
    )
    external_row = get_label_project_by_external_id(
        session,
        label_studio_project_id,
        include_deleted=True,
    )
    if row is not None and external_row is not None and row.id != external_row.id:
        raise ValueError(
            "The provisioned Label Studio project is already mapped to a different record."
        )
    if row is None:
        row = external_row
    if row is None:
        row = LabelProject(
            id=generate_prefixed_id("lpr"),
            label_studio_project_id=label_studio_project_id,
            title=payload.title,
            task_type=dataset.task_type,
        )
        session.add(row)

    row.dataset_id = dataset.id
    row.label_studio_project_id = label_studio_project_id
    row.title = payload.title
    row.task_type = dataset.task_type
    row.label_config = label_config
    row.label_schema_version = payload.annotation_schema_id
    row.training_pipeline_slug = payload.training_pipeline_slug
    row.status = LabelProjectStatus.ACTIVE.value
    row.auto_sync_enabled = payload.auto_sync_enabled
    row.auto_snapshot_enabled = payload.auto_snapshot_enabled
    row.auto_train_enabled = bool(getattr(row, "auto_train_enabled", False))
    row.auto_deploy_enabled = bool(getattr(row, "auto_deploy_enabled", True))
    row.default_model_template = _coerce_project_default_template(
        row.default_model_template,
        TaskType(dataset.task_type),
        previous_task_type=None,
    )
    row.default_training_profile = row.default_training_profile or "safe-default"
    row.preferred_endpoint_mode = row.preferred_endpoint_mode or "realtime"
    row.minimum_annotation_count = payload.minimum_annotation_count
    row.required_classes = []
    row.train_ratio = payload.train_ratio
    row.val_ratio = payload.val_ratio
    row.test_ratio = payload.test_ratio
    row.class_balance_strategy = payload.class_balance_strategy
    row.qualifying_actions = qualifying_actions
    row.deleted_at = None
    row.deleted_by = None
    row.delete_reason = None
    session.flush()
    return _label_project_summary(row, dataset.name)


def request_dataset_snapshot(
    session: Session,
    payload: DatasetSnapshotCreateRequest,
    *,
    created_by: str | None = None,
) -> DatasetSnapshotSummary:
    return create_or_reuse_dataset_snapshot_request(
        session,
        dataset_id=payload.dataset_id,
        label_project_id=payload.label_project_id,
        export_format=payload.export_format,
        trigger=payload.trigger,
        reason=payload.reason,
        created_by=created_by,
        idempotency_key=payload.idempotency_key,
    )


def create_or_reuse_dataset_snapshot_request(
    session: Session,
    *,
    dataset_id: str,
    label_project_id: str,
    export_format: str,
    trigger: str,
    reason: str | None,
    created_by: str | None,
    source_event_key: str | None = None,
    idempotency_key: str | None = None,
) -> DatasetSnapshotSummary:
    dataset = get_dataset_record(session, dataset_id)
    label_project = get_label_project_record(session, label_project_id)
    if dataset is None or label_project is None:
        raise ValueError("Dataset or Label Studio project mapping does not exist.")

    snapshot_key = idempotency_key or _build_snapshot_idempotency_key(
        dataset=dataset,
        label_project=label_project,
        export_format=export_format,
        trigger=trigger,
        reason=reason,
        source_event_key=source_event_key,
    )
    existing = session.scalar(
        select(DatasetSnapshot).where(
            DatasetSnapshot.dataset_id == dataset_id,
            DatasetSnapshot.idempotency_key == snapshot_key,
        )
    )
    if existing is not None:
        return _snapshot_summary(existing)

    next_snapshot_no = (
        session.scalar(
            select(func.max(DatasetSnapshot.snapshot_no)).where(
                DatasetSnapshot.dataset_id == dataset_id
            )
        )
        or 0
    ) + 1
    snapshot = DatasetSnapshot(
        id=generate_prefixed_id("dss"),
        dataset_id=dataset_id,
        label_project_id=label_project_id,
        snapshot_no=next_snapshot_no,
        status=SnapshotStatus.REQUESTED.value,
        item_count=0,
        annotation_count=label_project.annotation_count,
        eligible_item_count=int(getattr(label_project, "eligible_task_count", 0) or 0),
        submitted_labeled_item_count=int(
            getattr(label_project, "submitted_labeled_task_count", 0) or 0
        ),
        submitted_background_item_count=int(
            getattr(label_project, "submitted_background_task_count", 0) or 0
        ),
        draft_item_count=int(getattr(label_project, "draft_task_count", 0) or 0),
        invalid_item_count=int(getattr(label_project, "invalid_task_count", 0) or 0),
        created_by=created_by,
        export_format=export_format,
        label_schema_version=dataset.label_schema_version,
        split_metadata={
            "strategy": "deterministic_sha256_mod_100",
            "train_ratio": label_project.train_ratio,
            "val_ratio": label_project.val_ratio,
            "test_ratio": label_project.test_ratio,
            "train_count": 0,
            "val_count": 0,
            "test_count": 0,
        },
        validation_report={"status": ValidationStatus.PENDING.value, "checks": {}, "issues": []},
        validation_status=ValidationStatus.PENDING.value,
        statistics=_label_project_statistics(row=label_project),
        source_captured_at=utc_now(),
        source_label_studio_project_id=label_project.label_studio_project_id,
        source_project_title=label_project.title,
        source_event_key=source_event_key,
        idempotency_key=snapshot_key,
    )
    dataset.status = DatasetStatus.SNAPSHOTTING.value
    session.add(snapshot)
    session.flush()
    return _snapshot_summary(snapshot)


def list_dataset_snapshots(
    session: Session, *, dataset_id: str | None = None
) -> list[DatasetSnapshotSummary]:
    stmt = (
        select(DatasetSnapshot)
        .where(DatasetSnapshot.deleted_at.is_(None))
        .order_by(DatasetSnapshot.created_at.desc())
    )
    if dataset_id:
        stmt = stmt.where(DatasetSnapshot.dataset_id == dataset_id)
    rows = session.scalars(stmt).all()
    return [_snapshot_summary(row) for row in rows]


def get_dataset_snapshot_record(session: Session, snapshot_id: str) -> DatasetSnapshot | None:
    return session.get(DatasetSnapshot, snapshot_id)


def get_dataset_snapshot_detail(
    session: Session,
    snapshot_id: str,
) -> DatasetSnapshotDetail | None:
    row = get_dataset_snapshot_record(session, snapshot_id)
    if row is None:
        return None
    previous = session.scalar(
        select(DatasetSnapshot)
        .where(
            DatasetSnapshot.dataset_id == row.dataset_id,
            DatasetSnapshot.snapshot_no < row.snapshot_no,
        )
        .order_by(DatasetSnapshot.snapshot_no.desc())
    )
    comparison = _snapshot_comparison(row, previous) if previous else None
    split_metadata = SplitMetadata.model_validate(
        row.split_metadata
        or {
            "strategy": "deterministic_sha256_mod_100",
            "train_ratio": 0,
            "val_ratio": 0,
            "test_ratio": 0,
            "train_count": 0,
            "val_count": 0,
            "test_count": 0,
        }
    )
    validation_report = ValidationReport.model_validate(
        row.validation_report
        or {
            "status": row.validation_status or ValidationStatus.PENDING.value,
            "checks": {},
            "issues": [],
            "warning_count": 0,
            "error_count": 0,
        }
    )
    return DatasetSnapshotDetail(
        **_snapshot_summary(row).model_dump(),
        manifest_uri=row.manifest_uri,
        export_storage_uri=row.export_storage_uri,
        dvc_rev=row.dvc_rev,
        created_by=row.created_by,
        source_captured_at=_iso(row.source_captured_at),
        statistics=row.statistics or {},
        split_metadata=split_metadata,
        validation_report=validation_report,
        comparison=comparison,
        processing_error=row.processing_error,
    )


def update_dataset_snapshot(
    session: Session,
    snapshot_id: str,
    *,
    status: SnapshotStatus | None = None,
    item_count: int | None = None,
    annotation_count: int | None = None,
    eligible_item_count: int | None = None,
    submitted_labeled_item_count: int | None = None,
    submitted_background_item_count: int | None = None,
    draft_item_count: int | None = None,
    invalid_item_count: int | None = None,
    manifest_uri: str | None = None,
    content_sha256: str | None = None,
    dvc_rev: str | None = None,
    split_metadata: dict[str, Any] | None = None,
    validation_report: dict[str, Any] | None = None,
    validation_status: ValidationStatus | None = None,
    statistics: dict[str, Any] | None = None,
    export_storage_uri: str | None = None,
    processing_error: str | None = None,
) -> DatasetSnapshot:
    row = get_dataset_snapshot_record(session, snapshot_id)
    if row is None:
        raise ValueError("Dataset snapshot does not exist.")
    if status is not None:
        row.status = status.value
    if item_count is not None:
        row.item_count = item_count
    if annotation_count is not None:
        row.annotation_count = annotation_count
    if eligible_item_count is not None:
        row.eligible_item_count = eligible_item_count
    if submitted_labeled_item_count is not None:
        row.submitted_labeled_item_count = submitted_labeled_item_count
    if submitted_background_item_count is not None:
        row.submitted_background_item_count = submitted_background_item_count
    if draft_item_count is not None:
        row.draft_item_count = draft_item_count
    if invalid_item_count is not None:
        row.invalid_item_count = invalid_item_count
    if manifest_uri is not None:
        row.manifest_uri = manifest_uri
    if content_sha256 is not None:
        row.content_sha256 = content_sha256
    if dvc_rev is not None:
        row.dvc_rev = dvc_rev
    if split_metadata is not None:
        row.split_metadata = split_metadata
    if validation_report is not None:
        row.validation_report = validation_report
    if validation_status is not None:
        row.validation_status = validation_status.value
    if statistics is not None:
        row.statistics = statistics
    if export_storage_uri is not None:
        row.export_storage_uri = export_storage_uri
    row.processing_error = processing_error
    if status == SnapshotStatus.READY:
        dataset = get_dataset_record(session, row.dataset_id)
        if dataset is not None:
            dataset.status = DatasetStatus.SNAPSHOTTED.value
        for older in session.scalars(
            select(DatasetSnapshot).where(
                DatasetSnapshot.dataset_id == row.dataset_id,
                DatasetSnapshot.id != row.id,
                DatasetSnapshot.status == SnapshotStatus.READY.value,
            )
        ).all():
            older.status = SnapshotStatus.SUPERSEDED.value
    elif status == SnapshotStatus.FAILED:
        dataset = get_dataset_record(session, row.dataset_id)
        if dataset is not None:
            dataset.status = DatasetStatus.FAILED.value
    session.flush()
    return row


def find_ready_snapshot_by_checksum(
    session: Session,
    *,
    dataset_id: str,
    content_sha256: str,
    exclude_snapshot_id: str | None = None,
) -> DatasetSnapshot | None:
    stmt = select(DatasetSnapshot).where(
        DatasetSnapshot.dataset_id == dataset_id,
        DatasetSnapshot.content_sha256 == content_sha256,
        DatasetSnapshot.status == SnapshotStatus.READY.value,
        DatasetSnapshot.deleted_at.is_(None),
    )
    if exclude_snapshot_id:
        stmt = stmt.where(DatasetSnapshot.id != exclude_snapshot_id)
    return session.scalar(stmt)


def list_dataset_assets_for_dataset(session: Session, dataset_id: str) -> list[DatasetAssetRecord]:
    rows = session.scalars(
        select(DatasetAsset)
        .where(DatasetAsset.dataset_id == dataset_id)
        .order_by(DatasetAsset.created_at.asc())
    ).all()
    return [
        DatasetAssetRecord(
            asset_id=row.id,
            relative_path=row.relative_path,
            sha256=row.sha256,
            source_path=row.relative_path,
            width=row.width,
            height=row.height,
        )
        for row in rows
    ]


def build_annotation_statistics(
    session: Session,
    *,
    dataset_id: str | None = None,
    label_project_id: str | None = None,
) -> AnnotationStatistics:
    if label_project_id:
        row = get_label_project_record(session, label_project_id)
        if row is None:
            raise ValueError("Label Studio project mapping does not exist.")
        snapshot_count = (
            session.scalar(
                select(func.count(DatasetSnapshot.id)).where(
                    DatasetSnapshot.label_project_id == label_project_id,
                    DatasetSnapshot.deleted_at.is_(None),
                )
            )
            or 0
        )
        return AnnotationStatistics(
            dataset_id=row.dataset_id,
            label_project_id=row.id,
            label_studio_project_id=row.label_studio_project_id,
            task_count=row.task_count,
            annotated_task_count=row.annotated_task_count,
            annotation_count=row.annotation_count,
            skipped_annotation_count=row.skipped_annotation_count,
            unlabeled_task_count=max(row.task_count - row.annotated_task_count, 0),
            draft_task_count=int(getattr(row, "draft_task_count", 0) or 0),
            eligible_task_count=int(getattr(row, "eligible_task_count", 0) or 0),
            submitted_labeled_task_count=int(
                getattr(row, "submitted_labeled_task_count", 0) or 0
            ),
            submitted_background_task_count=int(
                getattr(row, "submitted_background_task_count", 0) or 0
            ),
            invalid_task_count=int(getattr(row, "invalid_task_count", 0) or 0),
            class_distribution=row.class_distribution or {},
            snapshot_count=snapshot_count,
            last_activity_at=_iso(row.last_activity_at),
        )

    rows = session.scalars(
        select(LabelProject)
        .where(LabelProject.dataset_id == dataset_id, LabelProject.deleted_at.is_(None))
        .order_by(LabelProject.created_at.desc())
    ).all()
    combined_distribution: Counter[str] = Counter()
    for row in rows:
        combined_distribution.update(row.class_distribution or {})
    snapshot_count = (
        session.scalar(
            select(func.count(DatasetSnapshot.id)).where(
                DatasetSnapshot.dataset_id == dataset_id,
                DatasetSnapshot.deleted_at.is_(None),
            )
        )
        or 0
    )
    latest_activity = max(
        (row.last_activity_at for row in rows if row.last_activity_at), default=None
    )
    return AnnotationStatistics(
        dataset_id=dataset_id,
        task_count=sum(row.task_count for row in rows),
        annotated_task_count=sum(row.annotated_task_count for row in rows),
        annotation_count=sum(row.annotation_count for row in rows),
        skipped_annotation_count=sum(row.skipped_annotation_count for row in rows),
        unlabeled_task_count=max(
            sum(row.task_count for row in rows) - sum(row.annotated_task_count for row in rows), 0
        ),
        draft_task_count=sum(int(getattr(row, "draft_task_count", 0) or 0) for row in rows),
        eligible_task_count=sum(int(getattr(row, "eligible_task_count", 0) or 0) for row in rows),
        submitted_labeled_task_count=sum(
            int(getattr(row, "submitted_labeled_task_count", 0) or 0) for row in rows
        ),
        submitted_background_task_count=sum(
            int(getattr(row, "submitted_background_task_count", 0) or 0) for row in rows
        ),
        invalid_task_count=sum(int(getattr(row, "invalid_task_count", 0) or 0) for row in rows),
        class_distribution=dict(sorted(combined_distribution.items())),
        snapshot_count=snapshot_count,
        last_activity_at=_iso(latest_activity),
    )


def create_training_job(
    session: Session,
    payload: TrainingJobCreateRequest,
    *,
    requested_by: str | None = None,
) -> TrainingJobSummary:
    job = TrainingJob(
        id=generate_prefixed_id("trn"),
        model_family_id=payload.model_family_slug,
        dataset_snapshot_id=payload.dataset_snapshot_id,
        status=TrainingJobStatus.ACCEPTED.value,
        queue_name=QueueName.TRAINING.value,
        requested_by=requested_by,
        recipe_name=payload.recipe_name,
        recipe_version=payload.recipe_version,
        config_snapshot=payload.model_dump(),
    )
    session.add(job)
    session.flush()
    return TrainingJobSummary(
        training_job_id=job.id,
        model_family_slug=payload.model_family_slug,
        dataset_snapshot_id=payload.dataset_snapshot_id,
        task_type=payload.task_type,
        architecture=payload.architecture,
        model_template_key=payload.model_template_key,
        model_template_version=payload.model_template_version,
        recipe_name=payload.recipe_name,
        recipe_version=payload.recipe_version,
        queue_name=QueueName(job.queue_name),
        status=TrainingJobStatus(job.status),
        total_epochs=payload.max_epochs,
        created_at=_iso(job.created_at) or utc_now_iso(),
    )


def list_training_jobs(session: Session) -> list[TrainingJobSummary]:
    rows = session.scalars(
        select(TrainingJob)
        .where(TrainingJob.deleted_at.is_(None))
        .order_by(TrainingJob.created_at.desc())
    ).all()
    return [
        TrainingJobSummary(
            training_job_id=row.id,
            model_family_slug=row.model_family_id or "unassigned",
            dataset_snapshot_id=row.dataset_snapshot_id,
            task_type=TaskType(row.task_type),
            architecture=architecture_summary(
                task_type=TaskType(row.task_type),
                family=row.architecture_family,
                version=row.architecture_version,
                size=row.architecture_size,
            ),
            model_template_key=row.model_template_key,
            model_template_version=row.model_template_version,
            recipe_name=row.recipe_name,
            recipe_version=row.recipe_version,
            queue_name=QueueName(row.queue_name),
            status=TrainingJobStatus(row.status),
            total_epochs=int((row.progress or {}).get("total_epochs", 0)),
            created_at=_iso(row.created_at) or utc_now_iso(),
        )
        for row in rows
    ]


def list_model_families(session: Session) -> list[ModelFamilySummary]:
    rows = session.scalars(
        select(ModelFamily)
        .where(ModelFamily.deleted_at.is_(None))
        .order_by(ModelFamily.slug.asc())
    ).all()
    return [
        ModelFamilySummary(
            model_family_id=row.id,
            slug=row.slug,
            task_type=TaskType(row.task_type),
            architecture=architecture_summary(
                task_type=TaskType(row.task_type),
                family=row.architecture_family,
                version=row.architecture_version,
                size=row.architecture_size,
            ),
            template_key=row.template_key,
            template_version=row.template_version,
            contract_major=row.contract_major,
        )
        for row in rows
    ]


def list_model_versions(session: Session) -> list[ModelVersionSummary]:
    rows = session.scalars(
        select(ModelVersion)
        .where(ModelVersion.deleted_at.is_(None))
        .order_by(ModelVersion.created_at.desc())
    ).all()
    family_lookup = {
        row.id: row.slug
        for row in session.scalars(select(ModelFamily).where(ModelFamily.deleted_at.is_(None))).all()
    }
    return [
        ModelVersionSummary(
            model_version_id=row.id,
            model_family_id=row.model_family_id,
            model_family_slug=family_lookup.get(row.model_family_id, "unknown"),
            version_no=row.version_no,
            status=ModelLifecycleStatus(row.status),
            architecture=architecture_summary(
                task_type=TaskType(row.task_type),
                family=row.architecture_family,
                version=row.architecture_version,
                size=row.architecture_size,
            ),
            model_template_key=row.model_template_key,
            model_template_version=row.model_template_version,
            resolved_training_defaults=row.resolved_training_defaults or {},
            created_at=_iso(row.created_at) or utc_now_iso(),
        )
        for row in rows
    ]


def list_deployment_releases(session: Session) -> list[DeploymentReleaseSummary]:
    rows = session.scalars(
        select(DeploymentRelease)
        .where(DeploymentRelease.deleted_at.is_(None))
        .order_by(DeploymentRelease.created_at.desc())
    ).all()
    versions = {
        row.id: row
        for row in session.scalars(
            select(ModelVersion).where(ModelVersion.deleted_at.is_(None))
        ).all()
    }
    return [
        DeploymentReleaseSummary(
            deployment_release_id=row.id,
            target_path=row.target_path,
            model_version_id=row.model_version_id,
            architecture=architecture_summary(
                task_type=TaskType(
                    versions[row.model_version_id].task_type
                    if row.model_version_id in versions
                    else TaskType.CLASSIFICATION.value
                ),
                family=(
                    versions[row.model_version_id].architecture_family
                    if row.model_version_id in versions
                    else "yolo"
                ),
                version=(
                    versions[row.model_version_id].architecture_version
                    if row.model_version_id in versions
                    else "v8"
                ),
                size=(
                    versions[row.model_version_id].architecture_size
                    if row.model_version_id in versions
                    else "n"
                ),
            ),
            triton_model_name=row.triton_model_name,
            triton_model_version=row.triton_model_version,
            status=DeploymentReleaseStatus(row.status),
            activated_at=_iso(row.activated_at),
        )
        for row in rows
    ]


def create_promotion_request(
    session: Session,
    *,
    model_version_id: str,
    target_path: str,
    requested_by: str | None,
) -> str:
    promotion_id = generate_prefixed_id("prm")
    session.add(
        ModelPromotion(
            id=promotion_id,
            model_version_id=model_version_id,
            target_path=target_path,
            requested_by=requested_by,
            status="requested",
            requested_environment="dev",
        )
    )
    session.flush()
    return promotion_id


def create_webhook_event(
    session: Session,
    *,
    source: str,
    source_event_key: str,
    action: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    remote_addr: str | None,
    signature_valid: bool | None,
    label_studio_project_id: int | None,
) -> tuple[str, bool]:
    existing = find_webhook_event_by_source_key(
        session, source=source, source_event_key=source_event_key
    )
    if existing is not None:
        return existing.id, False
    event = WebhookEvent(
        id=generate_prefixed_id("evt"),
        source=source,
        source_event_key=source_event_key,
        action=action,
        payload=payload,
        headers=headers,
        remote_addr=remote_addr,
        payload_sha256=_payload_sha256(payload),
        signature_valid=signature_valid,
        label_studio_project_id=label_studio_project_id,
        received_at=utc_now(),
        processing_status=WebhookProcessingStatus.ACCEPTED.value,
    )
    session.add(event)
    session.flush()
    return event.id, True


def find_webhook_event_by_source_key(
    session: Session,
    *,
    source: str,
    source_event_key: str,
) -> WebhookEvent | None:
    return session.scalar(
        select(WebhookEvent).where(
            WebhookEvent.source == source,
            WebhookEvent.source_event_key == source_event_key,
        )
    )


def get_webhook_event_record(session: Session, event_id: str) -> WebhookEvent | None:
    return session.get(WebhookEvent, event_id)


def list_webhook_events(session: Session) -> list[WebhookEventSummary]:
    rows = session.scalars(select(WebhookEvent).order_by(WebhookEvent.received_at.desc())).all()
    return [_webhook_summary(row) for row in rows]


def get_webhook_event_detail(session: Session, event_id: str) -> WebhookEventDetail | None:
    row = get_webhook_event_record(session, event_id)
    if row is None:
        return None
    return WebhookEventDetail(
        **_webhook_summary(row).model_dump(),
        remote_addr=row.remote_addr,
        headers=row.headers or {},
        payload=row.payload or {},
        processing_error=row.processing_error,
    )


def list_audit_events(
    session: Session,
    *,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    event_type: str | None = None,
    source_service: str | None = None,
    limit: int = 200,
) -> list[AuditEventSummary]:
    stmt = select(AuditEvent).order_by(AuditEvent.occurred_at.desc())
    if aggregate_type:
        stmt = stmt.where(AuditEvent.aggregate_type == aggregate_type)
    if aggregate_id:
        stmt = stmt.where(AuditEvent.aggregate_id == aggregate_id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if source_service:
        stmt = stmt.where(AuditEvent.source_service == source_service)
    rows = session.scalars(stmt.limit(max(min(limit, 500), 1))).all()
    return [_audit_event_summary(row) for row in rows]


def get_audit_event_detail(session: Session, event_id: str) -> AuditEventDetail | None:
    row = session.get(AuditEvent, event_id)
    if row is None:
        return None
    return AuditEventDetail(
        **_audit_event_summary(row).model_dump(),
        causation_id=row.causation_id,
        payload_schema_version=row.payload_schema_version,
        payload=row.payload or {},
    )


def update_webhook_event(
    session: Session,
    event_id: str,
    *,
    processing_status: WebhookProcessingStatus | None = None,
    internal_event_type: str | None = None,
    label_project_id: str | None = None,
    processing_error: str | None = None,
    mark_processed: bool = False,
) -> WebhookEvent:
    row = get_webhook_event_record(session, event_id)
    if row is None:
        raise ValueError("Webhook event does not exist.")
    if processing_status is not None:
        row.processing_status = processing_status.value
    if internal_event_type is not None:
        row.internal_event_type = internal_event_type
    if label_project_id is not None:
        row.label_project_id = label_project_id
    row.processing_error = processing_error
    if mark_processed:
        row.processed_at = utc_now()
    session.flush()
    return row


def create_audit_event(
    session: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    source_service: str = "unknown",
    actor_type: str,
    actor_id: str,
    payload: dict[str, Any],
    request_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    occurred_at: Any | None = None,
) -> str:
    sequence_no = (
        session.scalar(
            select(func.max(AuditEvent.sequence_no)).where(
                AuditEvent.aggregate_type == aggregate_type,
                AuditEvent.aggregate_id == aggregate_id,
            )
        )
        or 0
    ) + 1
    event_id = generate_prefixed_id("aud")
    occurred_at_value = occurred_at or utc_now()
    event_payload = {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "sequence_no": sequence_no,
        "source_service": source_service,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "request_id": request_id or get_request_id(),
        "correlation_id": correlation_id or get_correlation_id(),
        "causation_id": causation_id,
        "payload_schema_version": 1,
        "occurred_at": _iso(occurred_at_value) or utc_now_iso(),
        "payload": payload,
    }
    session.add(
        AuditEvent(
            id=event_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            sequence_no=sequence_no,
            source_service=source_service,
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=event_payload["request_id"],
            correlation_id=event_payload["correlation_id"],
            causation_id=causation_id,
            payload_schema_version=1,
            occurred_at=occurred_at_value,
            payload=payload,
        )
    )
    session.flush()
    audit_root = session.info.get("audit_root")
    if audit_root:
        try:
            _append_audit_export(Path(str(audit_root)), event_payload)
        except OSError as exc:
            export_errors = session.info.setdefault("audit_export_errors", [])
            export_errors.append({"event_id": event_id, "error": str(exc)})
    return event_id


def queued_job_payload(job_id: str, queue_name: QueueName) -> dict[str, str]:
    return {
        "job_id": job_id,
        "queue": queue_name.value,
        "status": "accepted",
        "accepted_at": utc_now_iso(),
    }


def _append_audit_export(audit_root: Path, payload: dict[str, Any]) -> None:
    events_root = audit_root / "events"
    events_root.mkdir(parents=True, exist_ok=True)
    file_name = f"{payload['occurred_at'][:10]}.jsonl"
    path = events_root / file_name
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def _dataset_summary(
    row: Dataset,
    *,
    project_count: int = 0,
    snapshot_count: int = 0,
    latest_snapshot: DatasetSnapshot | None = None,
) -> DatasetSummary:
    return DatasetSummary(
        dataset_id=row.id,
        name=row.name,
        slug=row.slug,
        task_type=TaskType(row.task_type),
        status=DatasetStatus(row.status),
        label_schema_version=row.label_schema_version,
        project_count=project_count,
        snapshot_count=snapshot_count,
        latest_snapshot_id=latest_snapshot.id if latest_snapshot else None,
        latest_snapshot_status=(
            SnapshotStatus(latest_snapshot.status)
            if latest_snapshot and latest_snapshot.status
            else None
        ),
        created_at=_iso(row.created_at) or utc_now_iso(),
    )


def _label_project_summary(row: LabelProject, dataset_name: str | None) -> LabelProjectSummary:
    label_config = row.label_config or {}
    config_sha256 = label_config.get("config_sha256")
    return LabelProjectSummary(
        label_project_id=row.id,
        label_studio_project_id=row.label_studio_project_id,
        title=row.title,
        dataset_id=row.dataset_id,
        dataset_name=dataset_name,
        task_type=TaskType(row.task_type),
        status=LabelProjectStatus(row.status),
        label_schema_version=row.label_schema_version,
        label_config_sha256=(
            str(config_sha256) if isinstance(config_sha256, str) else None
        ),
        training_pipeline_slug=row.training_pipeline_slug,
        auto_sync_enabled=row.auto_sync_enabled,
        auto_snapshot_enabled=row.auto_snapshot_enabled,
        minimum_annotation_count=row.minimum_annotation_count,
        required_classes=row.required_classes or [],
        qualifying_actions=row.qualifying_actions or [],
        label_config=label_config,
        train_ratio=row.train_ratio,
        val_ratio=row.val_ratio,
        test_ratio=row.test_ratio,
        class_balance_strategy=_class_balance_strategy(
            getattr(row, "class_balance_strategy", None)
        ),
        task_count=row.task_count,
        annotated_task_count=row.annotated_task_count,
        annotation_count=row.annotation_count,
        skipped_annotation_count=row.skipped_annotation_count,
        draft_task_count=int(getattr(row, "draft_task_count", 0) or 0),
        eligible_task_count=int(getattr(row, "eligible_task_count", 0) or 0),
        submitted_labeled_task_count=int(getattr(row, "submitted_labeled_task_count", 0) or 0),
        submitted_background_task_count=int(
            getattr(row, "submitted_background_task_count", 0) or 0
        ),
        invalid_task_count=int(getattr(row, "invalid_task_count", 0) or 0),
        class_distribution=row.class_distribution or {},
        last_activity_at=_iso(row.last_activity_at),
        created_at=_iso(row.created_at) or utc_now_iso(),
    )


def _snapshot_summary(row: DatasetSnapshot) -> DatasetSnapshotSummary:
    return DatasetSnapshotSummary(
        snapshot_id=row.id,
        dataset_id=row.dataset_id,
        label_project_id=row.label_project_id,
        source_label_studio_project_id=row.source_label_studio_project_id,
        source_project_title=row.source_project_title,
        snapshot_no=row.snapshot_no,
        status=SnapshotStatus(row.status),
        export_format=row.export_format,
        label_schema_version=row.label_schema_version or "",
        item_count=row.item_count,
        annotation_count=row.annotation_count,
        eligible_item_count=row.eligible_item_count,
        submitted_labeled_item_count=row.submitted_labeled_item_count,
        submitted_background_item_count=row.submitted_background_item_count,
        draft_item_count=row.draft_item_count,
        invalid_item_count=row.invalid_item_count,
        content_checksum=row.content_sha256,
        validation_status=ValidationStatus(row.validation_status)
        if row.validation_status
        else None,
        created_at=_iso(row.created_at) or utc_now_iso(),
    )


def _class_balance_strategy(value: object) -> str:
    normalized = str(value or "none").lower()
    return normalized if normalized in {"none", "weighted_loss", "balanced_sampler"} else "none"


def _snapshot_comparison(
    current: DatasetSnapshot,
    previous: DatasetSnapshot | None,
) -> SnapshotComparison:
    if previous is None:
        return SnapshotComparison()
    return SnapshotComparison(
        previous_snapshot_id=previous.id,
        annotation_delta=current.annotation_count - previous.annotation_count,
        item_delta=current.item_count - previous.item_count,
        checksum_changed=current.content_sha256 != previous.content_sha256,
        schema_changed=current.label_schema_version != previous.label_schema_version,
        split_changed=(current.split_metadata or {}) != (previous.split_metadata or {}),
    )


def _webhook_summary(row: WebhookEvent) -> WebhookEventSummary:
    return WebhookEventSummary(
        event_id=row.id,
        source=row.source,
        source_event_key=row.source_event_key,
        action=row.action,
        label_studio_project_id=row.label_studio_project_id,
        label_project_id=row.label_project_id,
        signature_valid=row.signature_valid,
        internal_event_type=row.internal_event_type,
        processing_status=WebhookProcessingStatus(row.processing_status),
        received_at=_iso(row.received_at) or utc_now_iso(),
        processed_at=_iso(row.processed_at),
    )


def _audit_event_summary(row: AuditEvent) -> AuditEventSummary:
    return AuditEventSummary(
        event_id=row.id,
        event_type=row.event_type,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        sequence_no=row.sequence_no,
        source_service=row.source_service,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        request_id=row.request_id,
        correlation_id=row.correlation_id,
        occurred_at=_iso(row.occurred_at) or utc_now_iso(),
    )


def _label_project_statistics(*, row: LabelProject) -> dict[str, Any]:
    return {
        "task_count": row.task_count,
        "annotated_task_count": row.annotated_task_count,
        "annotation_count": row.annotation_count,
        "skipped_annotation_count": row.skipped_annotation_count,
        "draft_task_count": int(getattr(row, "draft_task_count", 0) or 0),
        "eligible_task_count": int(getattr(row, "eligible_task_count", 0) or 0),
        "submitted_labeled_task_count": int(
            getattr(row, "submitted_labeled_task_count", 0) or 0
        ),
        "submitted_background_task_count": int(
            getattr(row, "submitted_background_task_count", 0) or 0
        ),
        "invalid_task_count": int(getattr(row, "invalid_task_count", 0) or 0),
        "class_distribution": row.class_distribution or {},
    }


def _ensure_project_dataset(
    session: Session,
    *,
    row: LabelProject,
    label_schema_version: str,
) -> Dataset | None:
    if row.dataset_id:
        dataset = get_dataset_record(session, row.dataset_id)
        if dataset is not None:
            dataset_changed = (
                dataset.task_type != row.task_type
                or dataset.label_schema_version != label_schema_version
            )
            dataset.task_type = row.task_type
            dataset.label_schema_version = label_schema_version
            if dataset_changed and dataset.status == DatasetStatus.SNAPSHOTTED.value:
                dataset.status = DatasetStatus.READY.value
        return dataset

    title = row.title.strip() or f"Project {row.label_studio_project_id}"
    base_slug = _slugify(f"{title}-{row.task_type}")
    slug = _unique_dataset_slug(session, base_slug)
    dataset = Dataset(
        id=generate_prefixed_id("dst"),
        slug=slug,
        name=title,
        task_type=row.task_type,
        label_schema_version=label_schema_version,
        status=DatasetStatus.READY.value,
        created_by="label-studio-sync",
    )
    session.add(dataset)
    session.flush()
    row.dataset_id = dataset.id
    row.training_pipeline_slug = row.training_pipeline_slug or slug
    return dataset


def _unique_dataset_slug(session: Session, base_slug: str) -> str:
    existing = set(session.scalars(select(Dataset.slug)).all())
    candidate = base_slug
    suffix = 2
    while candidate in existing:
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


def _default_model_template_for_task(task_type: TaskType) -> str | None:
    if task_type == TaskType.CLASSIFICATION:
        return "classification-resnet-18"
    if task_type == TaskType.DETECTION:
        return "detection-yolov8-n"
    if task_type == TaskType.ORIENTED_DETECTION:
        return "oriented_detection-yolov26-n"
    if task_type == TaskType.SEGMENTATION:
        return "segmentation-deeplab-v3-resnet50"
    if task_type == TaskType.ANOMALY:
        return "anomaly-patchcore-wide-resnet50-2"
    return None


def _coerce_project_default_template(
    current: str | None,
    task_type: TaskType,
    *,
    previous_task_type: TaskType | None,
) -> str | None:
    if not current:
        return _default_model_template_for_task(task_type)

    if previous_task_type is not None and previous_task_type != task_type:
        return _default_model_template_for_task(task_type)

    return current


def _slugify(value: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return candidate or "pipeline"


def _label_schema_version_from_config(label_config: dict[str, Any]) -> str:
    payload = json.dumps(label_config or {}, sort_keys=True)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"ls-{digest}"


def _build_snapshot_idempotency_key(
    *,
    dataset: Dataset,
    label_project: LabelProject,
    export_format: str,
    trigger: str,
    reason: str | None,
    source_event_key: str | None,
) -> str:
    fingerprint = {
        "dataset_id": dataset.id,
        "label_project_id": label_project.id,
        "label_studio_project_id": label_project.label_studio_project_id,
        "export_format": export_format,
        "trigger": trigger,
        "reason": reason,
        "source_event_key": source_event_key,
        "task_count": label_project.task_count,
        "annotated_task_count": label_project.annotated_task_count,
        "annotation_count": label_project.annotation_count,
        "dataset_label_schema_version": dataset.label_schema_version,
        "project_label_schema_version": label_project.label_schema_version,
        "last_activity_at": _iso(label_project.last_activity_at),
    }
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode("utf-8")).hexdigest()
    return f"snapshot:{digest}"


def _payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _infer_task_type(project_payload: dict[str, Any]) -> TaskType:
    label_config = _coerce_label_config(project_payload)
    text_config = (
        json.dumps(label_config, sort_keys=True)
        if isinstance(label_config, dict)
        else str(label_config)
    )
    lowered = text_config.lower()
    if (
        "polygonlabels" in lowered
        or "polygonlabels" in str(project_payload.get("label_config", "")).lower()
    ):
        return TaskType.SEGMENTATION
    if (
        "rectanglelabels" in lowered
        or "rectanglelabels" in str(project_payload.get("label_config", "")).lower()
    ):
        return TaskType.DETECTION
    return TaskType.CLASSIFICATION


def _resolve_discovered_task_type(
    inferred_task_type: TaskType,
    *,
    previous_task_type: TaskType | None,
) -> TaskType:
    if previous_task_type == TaskType.TEMPORAL_SEQUENCE:
        return TaskType.TEMPORAL_SEQUENCE
    if previous_task_type == TaskType.ANOMALY and inferred_task_type == TaskType.CLASSIFICATION:
        return TaskType.ANOMALY
    if (
        previous_task_type == TaskType.ORIENTED_DETECTION
        and inferred_task_type == TaskType.DETECTION
    ):
        return TaskType.ORIENTED_DETECTION
    return inferred_task_type


def _coerce_label_config(project_payload: dict[str, Any]) -> dict[str, Any]:
    parsed = project_payload.get("parsed_label_config")
    if isinstance(parsed, dict):
        return parsed
    raw = project_payload.get("label_config")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        return {"raw": raw}
    return {}


def _provisioned_annotation_schema_id(label_config: object) -> str | None:
    if not isinstance(label_config, dict):
        return None
    schema_id = label_config.get("annotation_schema_id")
    provisioning = label_config.get("provisioning")
    if not isinstance(schema_id, str) or not schema_id:
        return None
    if not isinstance(provisioning, dict) or provisioning.get("marker_version") != "v1":
        return None
    return schema_id


def _merge_provisioned_label_config(
    previous: dict[str, Any],
    discovered: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(discovered)
    for key in (
        "raw",
        "annotation_schema_id",
        "annotation_schema_title",
        "target_builder_id",
        "target_builder_version",
        "config_sha256",
        "provisioning",
    ):
        if key in previous:
            merged[key] = previous[key]
    return merged


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)
