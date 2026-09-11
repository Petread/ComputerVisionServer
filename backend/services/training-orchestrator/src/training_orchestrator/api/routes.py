from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from redis import Redis
from rq import Queue, Retry

from cv_contracts import (
    AcceptedJob,
    ListResponse,
    ResponseMeta,
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
    get_training_job_detail,
    list_training_jobs,
    prepare_training_job_resume,
    queued_job_payload,
    read_idempotency_response,
    request_training_job_cancel,
    session_scope,
    write_idempotency_response,
)
from cv_status import QueueName
from cv_training import build_training_job_paths, read_jsonl
from cv_utils import NotFoundError, PlatformError, get_correlation_id, get_request_id, utc_now_iso

router = APIRouter(prefix="/training-jobs", tags=["training"])


def get_settings(request: Request):
    return request.app.state.settings


def build_meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )


def training_queue(settings) -> Queue:
    return Queue(QueueName.TRAINING.value, connection=Redis.from_url(settings.redis.url()))


@router.get("", response_model=ListResponse[TrainingJobSummary])
def get_training_jobs(settings=Depends(get_settings)) -> ListResponse[TrainingJobSummary]:
    with session_scope(settings) as session:
        items = list_training_jobs(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/compare", response_model=TrainingRunComparison)
def compare_training_jobs(
    job_ids: Annotated[Sequence[str], Query()],
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


@router.get("/{training_job_id}", response_model=TrainingJobDetail)
def get_training_job(training_job_id: str, settings=Depends(get_settings)) -> TrainingJobDetail:
    with session_scope(settings) as session:
        item = get_training_job_detail(session, training_job_id)
    if item is None:
        raise NotFoundError(
            message="Training job not found",
            detail=f"No training job exists for id {training_job_id}.",
        )
    paths = build_training_job_paths(settings, training_job_id)
    logs = [
        TrainingJobLogEntry.model_validate(entry)
        for entry in read_jsonl(paths.log_file, limit=settings.training.log_tail_limit)
    ]
    return item.model_copy(update={"logs": logs})


@router.post("", response_model=AcceptedJob, status_code=202)
def request_training_job(
    payload: TrainingJobCreateRequest,
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
                    message="The supplied Idempotency-Key has already been used for a different training request.",
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
                requested_by="bootstrap-admin",
                source_service="training-orchestrator",
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

    training_queue(settings).enqueue(
        "training_worker.jobs.run_training_job",
        training_job_id=job.id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=Retry(max=settings.training.safe_retry_limit, interval=[5, 30, 120]),
    )
    return accepted


@router.post("/{training_job_id}:cancel", response_model=AcceptedJob, status_code=202)
def cancel_training_job(
    training_job_id: str,
    payload: TrainingJobCommandRequest,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            request_training_job_cancel(
                session,
                training_job_id=training_job_id,
                requested_by="bootstrap-admin",
                source_service="training-orchestrator",
                reason=payload.reason,
                force=payload.force,
                stale_after_seconds=settings.training.stale_after_seconds,
            )
        except ValueError as exc:
            raise NotFoundError(
                message="Training job not found",
                detail=str(exc),
            ) from exc
    return AcceptedJob(**queued_job_payload(training_job_id, QueueName.TRAINING))


@router.post("/{training_job_id}:resume", response_model=AcceptedJob, status_code=202)
def resume_training_job(
    training_job_id: str,
    payload: TrainingJobCommandRequest,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            prepare_training_job_resume(
                session,
                training_job_id=training_job_id,
                requested_by="bootstrap-admin",
                source_service="training-orchestrator",
                reason=payload.reason,
            )
        except ValueError as exc:
            raise PlatformError(
                code="training_resume_invalid",
                message="The training job could not be resumed.",
                detail=str(exc),
                status_code=422,
            ) from exc
    training_queue(settings).enqueue(
        "training_worker.jobs.resume_training_job",
        training_job_id=training_job_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=Retry(max=settings.training.safe_retry_limit, interval=[5, 30, 120]),
    )
    return AcceptedJob(**queued_job_payload(training_job_id, QueueName.TRAINING))


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
