from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from redis import Redis
from rq import Queue, Retry

from admin_api.application.audit import write_admin_audit_event
from admin_api.dependencies import build_meta, get_settings, require_permission
from cv_contracts import (
    AcceptedJob,
    CleanupExecutionRequest,
    CleanupExecutionResult,
    CleanupImpactPreview,
    ListResponse,
    SessionUser,
    TrainingJobCommandRequest,
    TrainingJobCreateRequest,
    TrainingJobDetail,
    TrainingJobLogEntry,
    TrainingJobSummary,
    TrainingRunComparison,
)
from cv_db import (
    build_training_request_hash,
    create_training_job_request,
    execute_training_job_cleanup,
    get_training_job_detail,
    list_training_jobs,
    prepare_training_job_resume,
    preview_training_job_cleanup,
    queued_job_payload,
    read_idempotency_response,
    request_training_job_cancel,
    session_scope,
    write_idempotency_response,
)
from cv_status import QueueName
from cv_status import UserRole
from cv_training import build_training_job_paths, read_jsonl
from cv_utils import NotFoundError, PlatformError

router = APIRouter(tags=["training"])

TrainingReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "training.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.VIEWER,
        )
    ),
]
TrainingWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "training.write",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
        )
    ),
]


def _training_queue(settings) -> Queue:
    return Queue(QueueName.TRAINING.value, connection=Redis.from_url(settings.redis.url()))


@router.get("/training-jobs", response_model=ListResponse[TrainingJobSummary])
def get_training_jobs(
    _current_user: TrainingReader,
    settings=Depends(get_settings),
) -> ListResponse[TrainingJobSummary]:
    with session_scope(settings) as session:
        items = list_training_jobs(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/training-jobs/compare", response_model=TrainingRunComparison)
def compare_training_jobs(
    job_ids: Annotated[Sequence[str], Query()],
    _current_user: TrainingReader,
    settings=Depends(get_settings),
) -> TrainingRunComparison:
    unique_ids = list(dict.fromkeys(job_ids))
    if len(unique_ids) < 2:
        raise PlatformError(
            code="comparison_requires_multiple_jobs",
            message="Select at least two completed training runs to compare.",
            status_code=422,
        )
    with session_scope(settings) as session:
        details = [get_training_job_detail(session, training_job_id) for training_job_id in unique_ids]
    items = [item for item in details if item is not None]
    if len(items) != len(unique_ids):
        missing = [job_id for job_id, item in zip(unique_ids, details, strict=False) if item is None]
        raise NotFoundError(
            message="One or more training jobs were not found.",
            detail=f"Missing training jobs: {', '.join(missing)}",
        )
    metric_names = sorted(
        {
            metric_name
            for item in items
            for metric_name in (item.metrics_summary or item.evaluation_summary.metrics).keys()
        }
    )
    metrics = [
        {
            "metric_name": metric_name,
            "values": {
                item.training_job_id: (
                    item.metrics_summary.get(metric_name)
                    if metric_name in item.metrics_summary
                    else item.evaluation_summary.metrics.get(metric_name)
                )
                for item in items
            },
        }
        for metric_name in metric_names
    ]
    recommended = _recommend_job(items)
    return TrainingRunComparison(
        jobs=[TrainingJobSummary.model_validate(item.model_dump()) for item in items],
        metrics=metrics,
        recommended_job_id=recommended.training_job_id if recommended else None,
    )


@router.get("/training-jobs/{training_job_id}", response_model=TrainingJobDetail)
def get_training_job(
    training_job_id: str,
    _current_user: TrainingReader,
    settings=Depends(get_settings),
) -> TrainingJobDetail:
    with session_scope(settings) as session:
        item = get_training_job_detail(session, training_job_id)
    if item is None:
        raise NotFoundError(
            message="Training job not found",
            detail=f"No training job exists for id {training_job_id}.",
        )
    paths = build_training_job_paths(settings, training_job_id)
    logs = [
        TrainingJobLogEntry.model_validate(
            entry
            if entry.get("sequence_no") is not None
            else {"sequence_no": index, **entry}
        )
        for index, entry in enumerate(
            read_jsonl(paths.log_file, limit=settings.training.log_tail_limit),
            start=1,
        )
    ]
    return item.model_copy(update={"logs": logs})


@router.get("/training-jobs/{training_job_id}/cleanup-preview", response_model=CleanupImpactPreview)
def preview_training_cleanup(
    training_job_id: str,
    _current_user: TrainingWriter,
    settings=Depends(get_settings),
) -> CleanupImpactPreview:
    with session_scope(settings) as session:
        try:
            return preview_training_job_cleanup(
                session,
                settings=settings,
                training_job_id=training_job_id,
            )
        except ValueError as exc:
            raise NotFoundError(
                message="Training job not found",
                detail=str(exc),
            ) from exc


@router.post("/training-jobs/{training_job_id}:cleanup", response_model=CleanupExecutionResult)
def cleanup_training_job(
    training_job_id: str,
    payload: CleanupExecutionRequest,
    request: Request,
    current_user: TrainingWriter,
    settings=Depends(get_settings),
) -> CleanupExecutionResult:
    with session_scope(settings) as session:
        try:
            result = execute_training_job_cleanup(
                session,
                settings=settings,
                training_job_id=training_job_id,
                payload=payload,
                deleted_by=current_user.username,
            )
        except ValueError as exc:
            detail = str(exc)
            if "does not exist" in detail:
                raise NotFoundError(
                    message="Training job not found",
                    detail=detail,
                ) from exc
            raise PlatformError(
                code="training_cleanup_invalid",
                message="The training cleanup request could not be completed.",
                detail=detail,
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="training.cleanup.executed.v1",
        aggregate_type="training_job",
        aggregate_id=training_job_id,
        payload=result.model_dump(mode="json"),
    )
    return result


@router.post("/training-jobs", response_model=AcceptedJob, status_code=202)
def request_training_job(
    payload: TrainingJobCreateRequest,
    request: Request,
    current_user: TrainingWriter,
    settings=Depends(get_settings),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AcceptedJob:
    effective_idempotency_key = idempotency_key or payload.idempotency_key
    request_hash = build_training_request_hash(payload)
    if effective_idempotency_key:
        with session_scope(settings) as session:
            try:
                response_body = read_idempotency_response(
                    session,
                    scope="training.create",
                    key=effective_idempotency_key,
                    request_hash=request_hash,
                )
            except ValueError as exc:
                raise PlatformError(
                    code="idempotency_key_conflict",
                    message="The supplied Idempotency-Key was already used for a different training request.",
                    detail=str(exc),
                    status_code=409,
                ) from exc
        if response_body is not None:
            return AcceptedJob.model_validate(response_body)

    with session_scope(settings) as session:
        try:
            job = create_training_job_request(
                session,
                payload,
                requested_by=current_user.username,
                source_service="admin-api",
                idempotency_key=effective_idempotency_key,
            )
        except ValueError as exc:
            raise PlatformError(
                code="training_request_invalid",
                message="The training job could not be created.",
                detail=str(exc),
                status_code=422,
            ) from exc

        accepted = AcceptedJob(**queued_job_payload(job.id, QueueName.TRAINING))
        if effective_idempotency_key:
            write_idempotency_response(
                session,
                scope="training.create",
                key=effective_idempotency_key,
                request_hash=request_hash,
                response_code=202,
                response_body=accepted.model_dump(mode="json"),
            )

    _training_queue(settings).enqueue(
        "training_worker.jobs.run_training_job",
        training_job_id=job.id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=Retry(max=settings.training.safe_retry_limit, interval=[5, 30, 120]),
    )
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="training.requested.v1",
        aggregate_type="training_job",
        aggregate_id=job.id,
        payload={
            "dataset_snapshot_id": payload.dataset_snapshot_id,
            "model_family_slug": payload.model_family_slug,
            "task_type": payload.task_type.value,
        },
    )
    return accepted


@router.post("/training-jobs/{training_job_id}:cancel", response_model=AcceptedJob, status_code=202)
def cancel_training_job(
    training_job_id: str,
    payload: TrainingJobCommandRequest,
    request: Request,
    current_user: TrainingWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            request_training_job_cancel(
                session,
                training_job_id=training_job_id,
                requested_by=current_user.username,
                source_service="admin-api",
                reason=payload.reason,
                force=payload.force,
                stale_after_seconds=settings.training.stale_after_seconds,
            )
        except ValueError as exc:
            raise NotFoundError(
                message="Training job not found",
                detail=str(exc),
            ) from exc
    accepted = AcceptedJob(**queued_job_payload(training_job_id, QueueName.TRAINING))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="training.cancel.requested.v1",
        aggregate_type="training_job",
        aggregate_id=training_job_id,
        payload={"reason": payload.reason, "job_id": accepted.job_id},
    )
    return accepted


@router.post("/training-jobs/{training_job_id}:resume", response_model=AcceptedJob, status_code=202)
def resume_training_job(
    training_job_id: str,
    payload: TrainingJobCommandRequest,
    request: Request,
    current_user: TrainingWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            prepare_training_job_resume(
                session,
                training_job_id=training_job_id,
                requested_by=current_user.username,
                source_service="admin-api",
                reason=payload.reason,
            )
        except ValueError as exc:
            raise PlatformError(
                code="training_resume_invalid",
                message="The training job could not be resumed.",
                detail=str(exc),
                status_code=422,
            ) from exc
    _training_queue(settings).enqueue(
        "training_worker.jobs.resume_training_job",
        training_job_id=training_job_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=Retry(max=settings.training.safe_retry_limit, interval=[5, 30, 120]),
    )
    accepted = AcceptedJob(**queued_job_payload(training_job_id, QueueName.TRAINING))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="training.resume.requested.v1",
        aggregate_type="training_job",
        aggregate_id=training_job_id,
        payload={"reason": payload.reason, "job_id": accepted.job_id},
    )
    return accepted


def _recommend_job(items: list[TrainingJobDetail]) -> TrainingJobDetail | None:
    if not items:
        return None
    promotable = [item for item in items if item.evaluation_summary.promotion_ready]
    candidates = promotable or items

    def score(item: TrainingJobDetail) -> float:
        primary = item.evaluation_summary.primary_metric_name
        if primary is None:
            return 0.0
        if primary in item.metrics_summary:
            return float(item.metrics_summary[primary])
        return float(item.evaluation_summary.metrics.get(primary, 0.0))

    return max(candidates, key=score, default=None)
