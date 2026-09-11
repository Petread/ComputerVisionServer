from __future__ import annotations

import hashlib
from typing import Annotated

from admin_api.application.audit import write_admin_audit_event
from admin_api.dependencies import build_meta, get_settings, require_permission
from cv_contracts import (
    AcceptedJob,
    AnnotationSchemaSummary,
    AnnotationStatistics,
    CleanupExecutionRequest,
    CleanupExecutionResult,
    CleanupImpactPreview,
    DatasetCreateRequest,
    DatasetSnapshotCreateRequest,
    DatasetSnapshotDetail,
    DatasetSnapshotSummary,
    DatasetSummary,
    LabelProjectMappingRequest,
    LabelProjectProvisionRequest,
    LabelProjectSummary,
    ListResponse,
    SessionUser,
    ValidationReport,
)
from cv_datasets import list_annotation_schemas
from cv_db import (
    build_annotation_statistics,
    create_dataset,
    execute_dataset_snapshot_cleanup,
    get_dataset_snapshot_detail,
    list_dataset_snapshots,
    list_datasets,
    list_label_projects,
    preview_dataset_snapshot_cleanup,
    queued_job_payload,
    request_dataset_snapshot,
    session_scope,
    upsert_label_project_mapping,
)
from cv_status import QueueName, UserRole
from cv_utils import NotFoundError, PlatformError
from fastapi import APIRouter, Depends, Request
from redis import Redis
from rq import Queue, Retry

router = APIRouter(tags=["datasets"])

DatasetReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "datasets.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.LABEL_MANAGER,
            UserRole.VIEWER,
        )
    ),
]
DatasetWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "datasets.write",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.LABEL_MANAGER,
        )
    ),
]
SnapshotWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "dataset_snapshots.create",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.LABEL_MANAGER,
        )
    ),
]


def _queue(queue_name: QueueName, settings) -> Queue:
    return Queue(queue_name.value, connection=Redis.from_url(settings.redis.url()))


def _annotation_schema_summaries() -> list[AnnotationSchemaSummary]:
    return [
        AnnotationSchemaSummary(
            annotation_schema_id=schema.definition.schema_id,
            task_type=schema.definition.task_type,
            title=schema.definition.title,
            description=schema.definition.description,
            target_builder_id=schema.definition.target_builder_id,
            target_builder_version=schema.definition.target_builder_version,
            label_config_sha256=hashlib.sha256(
                schema.label_studio_config().encode("utf-8")
            ).hexdigest(),
            field_names=[field.name for field in schema.definition.fields],
        )
        for schema in list_annotation_schemas()
    ]


@router.get("/datasets", response_model=ListResponse[DatasetSummary])
def get_dataset_list(
    _current_user: DatasetReader,
    settings=Depends(get_settings),
) -> ListResponse[DatasetSummary]:
    with session_scope(settings) as session:
        items = list_datasets(session)
    return ListResponse(items=items, meta=build_meta())


@router.post("/datasets", response_model=DatasetSummary)
def create_dataset_record(
    payload: DatasetCreateRequest,
    request: Request,
    current_user: DatasetWriter,
    settings=Depends(get_settings),
) -> DatasetSummary:
    with session_scope(settings) as session:
        item = create_dataset(session, payload, created_by=current_user.username)
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="dataset.created.v1",
        aggregate_type="dataset",
        aggregate_id=item.dataset_id,
        payload={"slug": item.slug, "task_type": item.task_type.value},
    )
    return item


@router.get("/label-projects", response_model=ListResponse[LabelProjectSummary])
def get_label_projects(
    _current_user: DatasetReader,
    settings=Depends(get_settings),
) -> ListResponse[LabelProjectSummary]:
    with session_scope(settings) as session:
        items = list_label_projects(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/annotation-schemas", response_model=ListResponse[AnnotationSchemaSummary])
def get_annotation_schemas(
    _current_user: DatasetReader,
) -> ListResponse[AnnotationSchemaSummary]:
    return ListResponse(items=_annotation_schema_summaries(), meta=build_meta())


@router.post("/label-projects:provision", response_model=AcceptedJob, status_code=202)
def provision_label_project(
    payload: LabelProjectProvisionRequest,
    request: Request,
    current_user: DatasetWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    if payload.train_ratio + payload.val_ratio + payload.test_ratio != 100:
        raise PlatformError(
            code="invalid_split_ratios",
            message="Train, validation, and test split ratios must add up to 100.",
            detail="Adjust the project provisioning split settings so the total equals 100.",
            status_code=422,
        )
    _queue(QueueName.DATASET, settings).enqueue(
        "control_worker.jobs.dataset.provision_labelstudio_project",
        request_payload=payload.model_dump(mode="json"),
        requested_by=current_user.username,
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    request_key = payload.idempotency_key or (
        f"{payload.dataset_id}:{payload.annotation_schema_id}"
    )
    job = AcceptedJob(
        **queued_job_payload(
            f"label-project-provision-{hashlib.sha256(request_key.encode('utf-8')).hexdigest()[:20]}",
            QueueName.DATASET,
        )
    )
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="label_project.provisioning.requested.v1",
        aggregate_type="dataset",
        aggregate_id=payload.dataset_id,
        payload={
            "job_id": job.job_id,
            "annotation_schema_id": payload.annotation_schema_id,
            "auto_sync_enabled": payload.auto_sync_enabled,
            "auto_snapshot_enabled": payload.auto_snapshot_enabled,
        },
    )
    return job


@router.post("/label-projects", response_model=LabelProjectSummary)
def map_label_project(
    payload: LabelProjectMappingRequest,
    request: Request,
    current_user: DatasetWriter,
    settings=Depends(get_settings),
) -> LabelProjectSummary:
    ratio_total = payload.train_ratio + payload.val_ratio + payload.test_ratio
    if ratio_total != 100:
        raise PlatformError(
            code="invalid_split_ratios",
            message="Train, validation, and test split ratios must add up to 100.",
            detail="Adjust the project mapping split settings so the total equals 100.",
            status_code=422,
        )
    with session_scope(settings) as session:
        try:
            item = upsert_label_project_mapping(session, payload)
        except ValueError as exc:
            raise PlatformError(
                code="mapping_failed",
                message="The Label Studio project could not be mapped to this dataset.",
                detail=str(exc),
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="label_project.mapped.v1",
        aggregate_type="label_project",
        aggregate_id=item.label_project_id,
        payload={
            "dataset_id": item.dataset_id,
            "label_studio_project_id": item.label_studio_project_id,
            "task_type": item.task_type.value,
        },
    )
    return item


@router.post("/label-projects/sync", response_model=AcceptedJob, status_code=202)
def sync_label_projects(
    request: Request,
    current_user: DatasetWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    _queue(QueueName.DATASET, settings).enqueue(
        "control_worker.jobs.dataset.sync_labelstudio_projects",
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    job = AcceptedJob(**queued_job_payload("label-project-sync", QueueName.DATASET))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="label_project.sync.requested.v1",
        aggregate_type="system",
        aggregate_id="labelstudio",
        payload={"job_id": job.job_id},
    )
    return job


@router.get("/dataset-snapshots", response_model=ListResponse[DatasetSnapshotSummary])
def get_all_dataset_snapshots(
    _current_user: DatasetReader,
    dataset_id: str | None = None,
    settings=Depends(get_settings),
) -> ListResponse[DatasetSnapshotSummary]:
    with session_scope(settings) as session:
        items = list_dataset_snapshots(session, dataset_id=dataset_id)
    return ListResponse(items=items, meta=build_meta())


@router.get("/datasets/{dataset_id}/snapshots", response_model=ListResponse[DatasetSnapshotSummary])
def get_dataset_snapshots(
    dataset_id: str,
    _current_user: DatasetReader,
    settings=Depends(get_settings),
) -> ListResponse[DatasetSnapshotSummary]:
    with session_scope(settings) as session:
        items = list_dataset_snapshots(session, dataset_id=dataset_id)
    return ListResponse(items=items, meta=build_meta())


@router.post("/dataset-snapshots", response_model=AcceptedJob, status_code=202)
def create_snapshot_request(
    payload: DatasetSnapshotCreateRequest,
    request: Request,
    current_user: SnapshotWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    if payload.export_format.upper() != "JSON":
        raise PlatformError(
            code="unsupported_export_format",
            message="Only Label Studio JSON exports are currently supported for immutable snapshots.",
            detail=(
                "Use the JSON snapshot export so the control worker can materialize, validate, "
                "and DVC-track the snapshot deterministically."
            ),
            status_code=422,
        )
    with session_scope(settings) as session:
        try:
            snapshot = request_dataset_snapshot(
                session,
                payload,
                created_by=current_user.username,
            )
        except ValueError as exc:
            raise PlatformError(
                code="snapshot_request_invalid",
                message="The dataset snapshot could not be requested.",
                detail=str(exc),
                status_code=422,
            ) from exc
    _queue(QueueName.DATASET, settings).enqueue(
        "control_worker.jobs.dataset.create_labelstudio_snapshot_export",
        snapshot_id=snapshot.snapshot_id,
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    accepted = AcceptedJob(**queued_job_payload(snapshot.snapshot_id, QueueName.DATASET))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="dataset.snapshot.requested.v1",
        aggregate_type="dataset_snapshot",
        aggregate_id=snapshot.snapshot_id,
        payload={
            "dataset_id": snapshot.dataset_id,
            "label_project_id": snapshot.label_project_id,
            "job_id": accepted.job_id,
        },
    )
    return accepted


@router.get("/dataset-snapshots/{snapshot_id}", response_model=DatasetSnapshotDetail)
def get_snapshot_detail(
    snapshot_id: str,
    _current_user: DatasetReader,
    settings=Depends(get_settings),
) -> DatasetSnapshotDetail:
    with session_scope(settings) as session:
        item = get_dataset_snapshot_detail(session, snapshot_id)
    if item is None:
        raise NotFoundError(
            message="Dataset snapshot not found",
            detail=f"No snapshot exists for id {snapshot_id}.",
    )
    return item


@router.get("/dataset-snapshots/{snapshot_id}/cleanup-preview", response_model=CleanupImpactPreview)
def preview_snapshot_cleanup(
    snapshot_id: str,
    _current_user: DatasetWriter,
    settings=Depends(get_settings),
) -> CleanupImpactPreview:
    with session_scope(settings) as session:
        try:
            return preview_dataset_snapshot_cleanup(
                session,
                settings=settings,
                snapshot_id=snapshot_id,
            )
        except ValueError as exc:
            raise NotFoundError(
                message="Dataset snapshot not found",
                detail=str(exc),
            ) from exc


@router.post("/dataset-snapshots/{snapshot_id}:cleanup", response_model=CleanupExecutionResult)
def cleanup_snapshot(
    snapshot_id: str,
    payload: CleanupExecutionRequest,
    request: Request,
    current_user: DatasetWriter,
    settings=Depends(get_settings),
) -> CleanupExecutionResult:
    with session_scope(settings) as session:
        try:
            result = execute_dataset_snapshot_cleanup(
                session,
                settings=settings,
                snapshot_id=snapshot_id,
                payload=payload,
                deleted_by=current_user.username,
            )
        except ValueError as exc:
            detail = str(exc)
            if "does not exist" in detail:
                raise NotFoundError(
                    message="Dataset snapshot not found",
                    detail=detail,
                ) from exc
            raise PlatformError(
                code="snapshot_cleanup_invalid",
                message="The dataset snapshot cleanup request could not be completed.",
                detail=detail,
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="dataset.snapshot.cleanup.executed.v1",
        aggregate_type="dataset_snapshot",
        aggregate_id=snapshot_id,
        payload=result.model_dump(mode="json"),
    )
    return result


@router.get("/dataset-snapshots/{snapshot_id}/validation-report", response_model=ValidationReport)
def get_snapshot_validation_report(
    snapshot_id: str,
    _current_user: DatasetReader,
    settings=Depends(get_settings),
) -> ValidationReport:
    with session_scope(settings) as session:
        item = get_dataset_snapshot_detail(session, snapshot_id)
    if item is None:
        raise NotFoundError(
            message="Dataset snapshot not found",
            detail=f"No snapshot exists for id {snapshot_id}.",
        )
    return item.validation_report


@router.get("/datasets/{dataset_id}/annotation-statistics", response_model=AnnotationStatistics)
def get_dataset_annotation_statistics(
    dataset_id: str,
    _current_user: DatasetReader,
    settings=Depends(get_settings),
) -> AnnotationStatistics:
    with session_scope(settings) as session:
        return build_annotation_statistics(session, dataset_id=dataset_id)


@router.get("/label-projects/{label_project_id}/annotation-statistics", response_model=AnnotationStatistics)
def get_project_annotation_statistics(
    label_project_id: str,
    _current_user: DatasetReader,
    settings=Depends(get_settings),
) -> AnnotationStatistics:
    with session_scope(settings) as session:
        try:
            return build_annotation_statistics(session, label_project_id=label_project_id)
        except ValueError as exc:
            raise NotFoundError(
                message="Label Studio project mapping not found",
                detail=str(exc),
            ) from exc
