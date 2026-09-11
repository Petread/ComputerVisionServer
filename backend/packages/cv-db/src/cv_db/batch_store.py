from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_contracts import (
    BatchArtifactSummary,
    BatchChunkSummary,
    BatchJobCreateRequest,
    BatchJobDetail,
    BatchJobProgress,
    BatchJobSummary,
    BatchResultResponse,
    LatencyQuantiles,
    ModelRouteRef,
)
from cv_core import PlatformSettings, generate_prefixed_id
from cv_status import BatchChunkStatus, BatchJobStatus, DeploymentReleaseStatus, TargetPath, TaskType
from cv_utils import utc_now, utc_now_iso

from cv_db.models import BatchJob, BatchJobChunk, DeploymentRelease, ModelFamily, ModelVersion
from cv_db.repositories import create_audit_event


TERMINAL_BATCH_STATES = {
    BatchJobStatus.COMPLETED.value,
    BatchJobStatus.FAILED.value,
    BatchJobStatus.CANCELED.value,
}


def build_batch_request_hash(payload: BatchJobCreateRequest) -> str:
    return hashlib.sha256(
        json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def create_batch_job_request(
    session: Session,
    *,
    payload: BatchJobCreateRequest,
    deployment_release_id: str,
    input_manifest_uri: str,
    submitted_by: str | None,
    source_service: str,
) -> BatchJob:
    row = BatchJob(
        id=generate_prefixed_id("btj"),
        deployment_release_id=deployment_release_id,
        status=BatchJobStatus.ACCEPTED.value,
        input_manifest_uri=input_manifest_uri,
        output_manifest_uri=None,
        total_items=len(payload.inputs),
        completed_items=0,
        failed_items=0,
        submitted_by=submitted_by,
        started_at=None,
        finished_at=None,
    )
    session.add(row)
    session.flush()
    create_audit_event(
        session,
        event_type="batch.job.state_changed.v1",
        aggregate_type="batch_job",
        aggregate_id=row.id,
        source_service=source_service,
        actor_type="user" if submitted_by else "system",
        actor_id=submitted_by or source_service,
        payload={
            "previous_state": None,
            "current_state": BatchJobStatus.ACCEPTED.value,
            "deployment_release_id": deployment_release_id,
            "input_manifest_uri": input_manifest_uri,
        },
    )
    return row


def get_batch_job_record(session: Session, batch_job_id: str) -> BatchJob | None:
    return session.get(BatchJob, batch_job_id)


def get_batch_chunk_record(session: Session, chunk_id: str) -> BatchJobChunk | None:
    return session.get(BatchJobChunk, chunk_id)


def list_batch_jobs(
    session: Session,
    *,
    settings: PlatformSettings,
) -> list[BatchJobSummary]:
    rows = session.scalars(select(BatchJob).order_by(BatchJob.created_at.desc())).all()
    return [_batch_job_summary(session, settings=settings, row=row) for row in rows]


def get_batch_job_detail(
    session: Session,
    *,
    settings: PlatformSettings,
    batch_job_id: str,
) -> BatchJobDetail | None:
    row = get_batch_job_record(session, batch_job_id)
    if row is None:
        return None
    summary = _batch_job_summary(session, settings=settings, row=row)
    chunks = session.scalars(
        select(BatchJobChunk)
        .where(BatchJobChunk.batch_job_id == batch_job_id)
        .order_by(BatchJobChunk.chunk_no.asc())
    ).all()
    artifacts, metrics = _artifacts_and_metrics(row)
    return BatchJobDetail(
        **summary.model_dump(),
        route=_route_ref(session, deployment_release_id=row.deployment_release_id),
        input_manifest_uri=row.input_manifest_uri,
        output_manifest_uri=row.output_manifest_uri,
        submitted_by=row.submitted_by,
        started_at=_iso(row.started_at),
        finished_at=_iso(row.finished_at),
        resumable=row.status != BatchJobStatus.COMPLETED.value,
        chunks=[
            BatchChunkSummary(
                chunk_id=item.id,
                chunk_no=item.chunk_no,
                status=BatchChunkStatus(item.status),
                retry_count=item.retry_count,
                input_uri=item.input_uri,
                output_uri=item.output_uri,
                started_at=_iso(item.started_at),
                finished_at=_iso(item.finished_at),
            )
            for item in chunks
        ],
        artifacts=artifacts,
        metrics=metrics,
    )


def get_batch_result_response(
    session: Session,
    *,
    batch_job_id: str,
) -> BatchResultResponse | None:
    row = get_batch_job_record(session, batch_job_id)
    if row is None or row.output_manifest_uri is None:
        return None
    path = Path(row.output_manifest_uri)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return BatchResultResponse.model_validate(payload)


def replace_batch_chunks(
    session: Session,
    *,
    batch_job_id: str,
    chunks: list[dict[str, Any]],
) -> list[BatchJobChunk]:
    for existing in session.scalars(
        select(BatchJobChunk).where(BatchJobChunk.batch_job_id == batch_job_id)
    ).all():
        session.delete(existing)
    session.flush()

    created: list[BatchJobChunk] = []
    for chunk in chunks:
        row = BatchJobChunk(
            id=generate_prefixed_id("bch"),
            batch_job_id=batch_job_id,
            chunk_no=int(chunk["chunk_no"]),
            status=BatchChunkStatus.QUEUED.value,
            retry_count=0,
            input_uri=str(chunk["input_uri"]),
            output_uri=None,
            started_at=None,
            finished_at=None,
        )
        session.add(row)
        created.append(row)
    session.flush()
    return created


def update_batch_chunk_state(
    session: Session,
    *,
    chunk_id: str,
    status: BatchChunkStatus,
    output_uri: str | None = None,
    increment_retry: bool = False,
) -> BatchJobChunk:
    row = get_batch_chunk_record(session, chunk_id)
    if row is None:
        raise ValueError(f"Batch chunk {chunk_id} does not exist.")
    row.status = status.value
    if increment_retry:
        row.retry_count += 1
    if status == BatchChunkStatus.RUNNING:
        row.started_at = row.started_at or utc_now()
    if status in {BatchChunkStatus.COMPLETED, BatchChunkStatus.FAILED}:
        row.finished_at = utc_now()
    if output_uri is not None:
        row.output_uri = output_uri
    session.flush()
    return row


def update_batch_job_state(
    session: Session,
    *,
    batch_job_id: str,
    status: BatchJobStatus,
    source_service: str,
    actor_type: str,
    actor_id: str,
    output_manifest_uri: str | None = None,
    total_items: int | None = None,
    completed_items: int | None = None,
    failed_items: int | None = None,
    reason: str | None = None,
) -> BatchJob:
    row = get_batch_job_record(session, batch_job_id)
    if row is None:
        raise ValueError(f"Batch job {batch_job_id} does not exist.")
    previous_state = row.status
    row.status = status.value
    if total_items is not None:
        row.total_items = total_items
    if completed_items is not None:
        row.completed_items = completed_items
    if failed_items is not None:
        row.failed_items = failed_items
    if output_manifest_uri is not None:
        row.output_manifest_uri = output_manifest_uri
    if status == BatchJobStatus.RUNNING:
        row.started_at = row.started_at or utc_now()
    if status in {
        BatchJobStatus.COMPLETED,
        BatchJobStatus.FAILED,
        BatchJobStatus.CANCELED,
    }:
        row.finished_at = utc_now()
    session.flush()
    if previous_state != row.status or reason:
        create_audit_event(
            session,
            event_type="batch.job.state_changed.v1",
            aggregate_type="batch_job",
            aggregate_id=batch_job_id,
            source_service=source_service,
            actor_type=actor_type,
            actor_id=actor_id,
            payload={
                "previous_state": previous_state,
                "current_state": row.status,
                "reason": reason,
                "total_items": row.total_items,
                "completed_items": row.completed_items,
                "failed_items": row.failed_items,
                "output_manifest_uri": row.output_manifest_uri,
            },
        )
    return row


def refresh_batch_job_progress(session: Session, *, batch_job_id: str) -> BatchJobProgress:
    row = get_batch_job_record(session, batch_job_id)
    if row is None:
        raise ValueError(f"Batch job {batch_job_id} does not exist.")
    chunks = session.scalars(
        select(BatchJobChunk).where(BatchJobChunk.batch_job_id == batch_job_id)
    ).all()
    counts = {status.value: 0 for status in BatchChunkStatus}
    for chunk in chunks:
        counts[chunk.status] = counts.get(chunk.status, 0) + 1
    total_chunks = len(chunks)
    total_items = max(row.total_items, 0)
    completed_items = min(row.completed_items, total_items)
    failed_items = min(row.failed_items, max(total_items - completed_items, 0))
    progress_percent = 0.0
    if total_items > 0:
        progress_percent = round(((completed_items + failed_items) / total_items) * 100, 2)
    return BatchJobProgress(
        total_items=total_items,
        completed_items=completed_items,
        failed_items=failed_items,
        total_chunks=total_chunks,
        queued_chunks=counts.get(BatchChunkStatus.QUEUED.value, 0),
        running_chunks=counts.get(BatchChunkStatus.RUNNING.value, 0),
        completed_chunks=counts.get(BatchChunkStatus.COMPLETED.value, 0),
        failed_chunks=counts.get(BatchChunkStatus.FAILED.value, 0),
        progress_percent=progress_percent,
    )


def _batch_job_summary(
    session: Session,
    *,
    settings: PlatformSettings,
    row: BatchJob,
) -> BatchJobSummary:
    progress = refresh_batch_job_progress(session, batch_job_id=row.id)
    deployment_target = TargetPath.BATCH.value
    input_path = Path(row.input_manifest_uri)
    if input_path.exists():
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                deployment_target = str(payload.get("deployment_target") or deployment_target)
        except json.JSONDecodeError:
            deployment_target = TargetPath.BATCH.value
    return BatchJobSummary(
        batch_job_id=row.id,
        deployment_target=deployment_target,
        deployment_release_id=row.deployment_release_id,
        status=BatchJobStatus(row.status),
        created_at=_iso(row.created_at) or utc_now_iso(),
        progress=progress,
    )


def _route_ref(session: Session, *, deployment_release_id: str) -> ModelRouteRef:
    release = session.get(DeploymentRelease, deployment_release_id)
    if release is None:
        raise ValueError(f"Deployment release {deployment_release_id} does not exist.")
    version = session.get(ModelVersion, release.model_version_id)
    family = session.get(ModelFamily, release.model_family_id)
    if version is None or family is None:
        raise ValueError(
            f"Deployment release {deployment_release_id} does not reference a valid model version."
        )
    return ModelRouteRef(
        deployment_release_id=release.id,
        target_path=TargetPath(release.target_path),
        release_status=DeploymentReleaseStatus(release.status),
        model_family_slug=family.slug,
        model_version_id=version.id,
        task_type=TaskType(family.task_type),
        triton_model_name=release.triton_model_name,
        triton_model_version=release.triton_model_version,
        contract_major=family.contract_major,
    )


def _artifacts_and_metrics(row: BatchJob) -> tuple[BatchArtifactSummary, LatencyQuantiles]:
    if row.output_manifest_uri:
        path = Path(row.output_manifest_uri)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            artifacts = BatchArtifactSummary.model_validate(payload.get("artifacts") or {})
            metrics = LatencyQuantiles.model_validate(payload.get("metrics") or {})
            return artifacts, metrics
    return BatchArtifactSummary(manifest_uri=row.output_manifest_uri), LatencyQuantiles()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)
