from __future__ import annotations

import hashlib

from cv_contracts import (
    AcceptedJob,
    AnnotationSchemaSummary,
    AnnotationStatistics,
    DatasetCreateRequest,
    DatasetSnapshotCreateRequest,
    DatasetSnapshotDetail,
    DatasetSnapshotSummary,
    DatasetSummary,
    LabelProjectMappingRequest,
    LabelProjectProvisionRequest,
    LabelProjectSummary,
    ListResponse,
    ResponseMeta,
    ValidationReport,
)
from cv_datasets import list_annotation_schemas
from cv_db import (
    build_annotation_statistics,
    create_dataset,
    get_dataset_snapshot_detail,
    list_dataset_snapshots,
    list_datasets,
    list_label_projects,
    queued_job_payload,
    request_dataset_snapshot,
    session_scope,
    upsert_label_project_mapping,
)
from cv_status import QueueName
from cv_utils import NotFoundError, PlatformError, get_correlation_id, get_request_id, utc_now_iso
from fastapi import APIRouter, Depends, Request
from redis import Redis
from rq import Queue, Retry

router = APIRouter()


def get_settings(request: Request):
    return request.app.state.settings


def build_meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )


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


@router.get("/datasets", response_model=ListResponse[DatasetSummary], tags=["datasets"])
def get_dataset_list(settings=Depends(get_settings)) -> ListResponse[DatasetSummary]:
    with session_scope(settings) as session:
        items = list_datasets(session)
    return ListResponse(items=items, meta=build_meta())


@router.post("/datasets", response_model=DatasetSummary, tags=["datasets"])
def create_dataset_record(
    payload: DatasetCreateRequest,
    settings=Depends(get_settings),
) -> DatasetSummary:
    with session_scope(settings) as session:
        return create_dataset(session, payload, created_by="bootstrap-admin")


@router.get("/label-projects", response_model=ListResponse[LabelProjectSummary], tags=["label-projects"])
def get_label_projects(settings=Depends(get_settings)) -> ListResponse[LabelProjectSummary]:
    with session_scope(settings) as session:
        items = list_label_projects(session)
    return ListResponse(items=items, meta=build_meta())


@router.get(
    "/annotation-schemas",
    response_model=ListResponse[AnnotationSchemaSummary],
    tags=["label-projects"],
)
def get_annotation_schemas() -> ListResponse[AnnotationSchemaSummary]:
    return ListResponse(items=_annotation_schema_summaries(), meta=build_meta())


@router.post(
    "/label-projects:provision",
    response_model=AcceptedJob,
    status_code=202,
    tags=["label-projects"],
)
def provision_label_project(
    payload: LabelProjectProvisionRequest,
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
        requested_by="bootstrap-admin",
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    request_key = payload.idempotency_key or (
        f"{payload.dataset_id}:{payload.annotation_schema_id}"
    )
    return AcceptedJob(
        **queued_job_payload(
            f"label-project-provision-{hashlib.sha256(request_key.encode('utf-8')).hexdigest()[:20]}",
            QueueName.DATASET,
        )
    )


@router.post("/label-projects", response_model=LabelProjectSummary, tags=["label-projects"])
def map_label_project(
    payload: LabelProjectMappingRequest,
    settings=Depends(get_settings),
) -> LabelProjectSummary:
    ratio_total = payload.train_ratio + payload.val_ratio + payload.test_ratio
    if ratio_total != 100:
        raise PlatformError(
            code="invalid_split_ratios",
            message="Train, validation, and test split ratios must add up to 100.",
            detail="Please adjust the project mapping split settings and try again.",
            status_code=422,
        )
    with session_scope(settings) as session:
        try:
            return upsert_label_project_mapping(session, payload)
        except ValueError as exc:
            raise PlatformError(
                code="mapping_failed",
                message="The Label Studio project could not be mapped to this dataset.",
                detail=str(exc),
                status_code=422,
            ) from exc


@router.post("/label-projects/sync", response_model=AcceptedJob, tags=["label-projects"])
def sync_label_projects(settings=Depends(get_settings)) -> AcceptedJob:
    queue = _queue(QueueName.DATASET, settings)
    queue.enqueue(
        "control_worker.jobs.dataset.sync_labelstudio_projects",
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    return AcceptedJob(**queued_job_payload("label-project-sync", QueueName.DATASET))


@router.get(
    "/datasets/{dataset_id}/snapshots",
    response_model=ListResponse[DatasetSnapshotSummary],
    tags=["dataset-snapshots"],
)
def get_dataset_snapshots(dataset_id: str, settings=Depends(get_settings)) -> ListResponse[DatasetSnapshotSummary]:
    with session_scope(settings) as session:
        items = list_dataset_snapshots(session, dataset_id=dataset_id)
    return ListResponse(items=items, meta=build_meta())


@router.post("/dataset-snapshots", response_model=AcceptedJob, tags=["dataset-snapshots"])
def create_snapshot_request(
    payload: DatasetSnapshotCreateRequest,
    settings=Depends(get_settings),
) -> AcceptedJob:
    if payload.export_format.upper() != "JSON":
        raise PlatformError(
            code="unsupported_export_format",
            message="Only Label Studio JSON exports are currently supported for immutable snapshots.",
            detail="Use the default JSON snapshot export and rely on the platform adapters for training-ready conversions.",
            status_code=422,
        )
    with session_scope(settings) as session:
        try:
            snapshot = request_dataset_snapshot(session, payload, created_by="bootstrap-admin")
        except ValueError as exc:
            raise PlatformError(
                code="snapshot_request_invalid",
                message="The dataset snapshot could not be requested.",
                detail=str(exc),
                status_code=422,
            ) from exc
    queue = _queue(QueueName.DATASET, settings)
    queue.enqueue(
        "control_worker.jobs.dataset.create_labelstudio_snapshot_export",
        snapshot_id=snapshot.snapshot_id,
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    return AcceptedJob(**queued_job_payload(snapshot.snapshot_id, QueueName.DATASET))


@router.get(
    "/dataset-snapshots/{snapshot_id}",
    response_model=DatasetSnapshotDetail,
    tags=["dataset-snapshots"],
)
def get_snapshot_detail(snapshot_id: str, settings=Depends(get_settings)) -> DatasetSnapshotDetail:
    with session_scope(settings) as session:
        item = get_dataset_snapshot_detail(session, snapshot_id)
    if item is None:
        raise NotFoundError(
            message="Dataset snapshot not found",
            detail=f"No snapshot exists for id {snapshot_id}.",
        )
    return item


@router.get(
    "/dataset-snapshots/{snapshot_id}/validation-report",
    response_model=ValidationReport,
    tags=["dataset-snapshots"],
)
def get_snapshot_validation_report(snapshot_id: str, settings=Depends(get_settings)) -> ValidationReport:
    with session_scope(settings) as session:
        item = get_dataset_snapshot_detail(session, snapshot_id)
    if item is None:
        raise NotFoundError(
            message="Dataset snapshot not found",
            detail=f"No snapshot exists for id {snapshot_id}.",
        )
    return item.validation_report


@router.get(
    "/datasets/{dataset_id}/annotation-statistics",
    response_model=AnnotationStatistics,
    tags=["statistics"],
)
def get_dataset_annotation_statistics(
    dataset_id: str,
    settings=Depends(get_settings),
) -> AnnotationStatistics:
    with session_scope(settings) as session:
        return build_annotation_statistics(session, dataset_id=dataset_id)


@router.get(
    "/label-projects/{label_project_id}/annotation-statistics",
    response_model=AnnotationStatistics,
    tags=["statistics"],
)
def get_project_annotation_statistics(
    label_project_id: str,
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
