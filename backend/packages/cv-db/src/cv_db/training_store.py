from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from cv_contracts import (
    BestCheckpointRule,
    PromotionCriteriaPolicy,
    PromotionCriterion,
    TrainingCheckpointSummary,
    TrainingEvaluationSummary,
    TrainingJobCreateRequest,
    TrainingJobDetail,
    TrainingJobEventSummary,
    TrainingJobSummary,
    TrainingMetricRecord,
)
from cv_contracts.modeling import training_defaults_from_persisted
from cv_core import generate_prefixed_id
from cv_status import QueueName, SnapshotStatus, TaskType, TrainingJobStatus
from cv_utils import utc_now, utc_now_iso
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cv_db.model_catalog import architecture_summary, is_trainable_architecture
from cv_db.models import (
    AuditEvent,
    DatasetSnapshot,
    IdempotencyKey,
    ModelFamily,
    ModelVersion,
    ServiceHeartbeat,
    TrainingCheckpoint,
    TrainingJob,
    TrainingMetric,
)
from cv_db.repositories import create_audit_event

TERMINAL_TRAINING_STATES = {
    TrainingJobStatus.COMPLETED.value,
    TrainingJobStatus.FAILED.value,
    TrainingJobStatus.CANCELLED.value,
    TrainingJobStatus.ROLLED_BACK_IF_NEEDED.value,
}

ACTIVE_TRAINING_STATES = {
    status.value for status in TrainingJobStatus if status.value not in TERMINAL_TRAINING_STATES
}


def build_training_request_hash(payload: TrainingJobCreateRequest) -> str:
    return hashlib.sha256(
        json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def get_training_job_record(session: Session, training_job_id: str) -> TrainingJob | None:
    return session.get(TrainingJob, training_job_id)


def get_training_job_by_idempotency_key(
    session: Session,
    *,
    idempotency_key: str,
) -> TrainingJob | None:
    return session.scalar(
        select(TrainingJob)
        .where(TrainingJob.idempotency_key == idempotency_key)
        .order_by(TrainingJob.created_at.desc())
    )


def list_training_jobs(session: Session) -> list[TrainingJobSummary]:
    rows = session.scalars(
        select(TrainingJob)
        .where(TrainingJob.deleted_at.is_(None))
        .order_by(TrainingJob.created_at.desc())
    ).all()
    return [_job_summary(row) for row in rows]


def get_training_job_detail(session: Session, training_job_id: str) -> TrainingJobDetail | None:
    row = get_training_job_record(session, training_job_id)
    if row is None:
        return None

    checkpoints = session.scalars(
        select(TrainingCheckpoint)
        .where(TrainingCheckpoint.training_job_id == training_job_id)
        .order_by(TrainingCheckpoint.created_at.desc())
    ).all()
    metrics = session.scalars(
        select(TrainingMetric)
        .where(TrainingMetric.training_job_id == training_job_id)
        .order_by(TrainingMetric.created_at.asc())
    ).all()
    events = session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.aggregate_type == "training_job",
            AuditEvent.aggregate_id == training_job_id,
        )
        .order_by(AuditEvent.sequence_no.asc())
    ).all()

    runtime_metadata = row.runtime_metadata or {}
    detail = TrainingJobDetail(
        **_job_summary(row).model_dump(),
        requested_by=row.requested_by,
        idempotency_key=row.idempotency_key,
        allow_duplicate_snapshot_training=row.allow_duplicate_snapshot_training,
        cancel_requested=row.cancel_requested,
        cancel_requested_at=_iso(row.cancel_requested_at),
        best_checkpoint_id=row.best_checkpoint_id,
        artifact_manifest_uri=row.artifact_manifest_uri,
        model_version_id=row.model_version_id,
        retry_count=row.retry_count,
        resume_count=row.resume_count,
        failure_class=row.failure_class,
        last_error=row.last_error,
        progress=row.progress or {},
        parameter_overrides=(row.config_snapshot or {}).get("parameter_overrides", {}),
        resolved_training_defaults=training_defaults_from_persisted(
            row.resolved_training_defaults or {}
        ),
        best_checkpoint_rule=BestCheckpointRule.model_validate(
            row.best_checkpoint_rule
            or {
                "metric_name": "metric_primary",
                "mode": "max",
                "phase": "evaluation",
                "minimum_epoch": 1,
            }
        ),
        promotion_criteria=[
            PromotionCriterion.model_validate(item) for item in (row.promotion_criteria or [])
        ],
        promotion_policy=(
            PromotionCriteriaPolicy.model_validate(row.promotion_policy)
            if row.promotion_policy
            else None
        ),
        evaluation_summary=TrainingEvaluationSummary.model_validate(row.evaluation_summary or {}),
        metrics_summary={k: float(v) for k, v in (row.metrics_summary or {}).items()},
        runtime_metadata=runtime_metadata,
        artifact_validation=runtime_metadata.get("artifact_validation") or {},
        logs=[],
        checkpoints=[_checkpoint_summary(item) for item in checkpoints],
        metrics=[_metric_record(item) for item in metrics],
        events=[_training_event_summary(item) for item in events],
    )
    return detail


def create_training_job_request(
    session: Session,
    payload: TrainingJobCreateRequest,
    *,
    requested_by: str | None,
    source_service: str,
    idempotency_key: str | None = None,
) -> TrainingJob:
    resolved_idempotency_key = idempotency_key or payload.idempotency_key
    if resolved_idempotency_key:
        existing = get_training_job_by_idempotency_key(
            session,
            idempotency_key=resolved_idempotency_key,
        )
        if existing is not None:
            return existing
    snapshot = session.get(DatasetSnapshot, payload.dataset_snapshot_id)
    if snapshot is None:
        raise ValueError("The referenced dataset snapshot does not exist.")
    if snapshot.deleted_at is not None:
        raise ValueError("The referenced dataset snapshot has been deleted.")
    if snapshot.status != SnapshotStatus.READY.value:
        raise ValueError("Only ready dataset snapshots can be used for training.")

    if not payload.allow_duplicate_snapshot_training:
        duplicate = session.scalar(
            select(TrainingJob).where(
                TrainingJob.dataset_snapshot_id == payload.dataset_snapshot_id,
                TrainingJob.model_family_slug == payload.model_family_slug,
                TrainingJob.status.in_(ACTIVE_TRAINING_STATES),
                TrainingJob.allow_duplicate_snapshot_training.is_(False),
            )
        )
        if duplicate is not None:
            raise ValueError(
                f"Model {payload.model_family_slug} already has active training job "
                f"{duplicate.id} for dataset snapshot {payload.dataset_snapshot_id}."
            )

    if not is_trainable_architecture(
        task_type=payload.task_type,
        family=payload.architecture.family,
        version=payload.architecture.version,
        size=payload.architecture.size,
    ):
        raise ValueError(
            "The selected architecture is not supported by the existing training and export path."
        )

    promotion_policy = payload.promotion_policy or PromotionCriteriaPolicy(
        criteria=payload.promotion_criteria,
        allow_model_override=True,
    )
    resolved_promotion_criteria = list(promotion_policy.criteria)
    family = _ensure_model_family(
        session,
        slug=payload.model_family_slug,
        task_type=payload.task_type,
        architecture=payload.architecture,
        promotion_policy=promotion_policy,
    )
    request_hash = build_training_request_hash(payload)
    progress = _build_progress(
        status=TrainingJobStatus.QUEUED,
        current_epoch=0,
        current_step=0,
        total_epochs=payload.max_epochs,
        total_steps=max(payload.max_epochs, 1),
    )
    row = TrainingJob(
        id=generate_prefixed_id("trn"),
        model_family_id=family.id,
        model_family_slug=family.slug,
        dataset_snapshot_id=payload.dataset_snapshot_id,
        task_type=payload.task_type.value,
        architecture_family=payload.architecture.family,
        architecture_version=payload.architecture.version,
        architecture_size=payload.architecture.size,
        architecture_display_name=payload.architecture.display_name,
        model_template_key=payload.model_template_key,
        model_template_version=payload.model_template_version,
        status=TrainingJobStatus.QUEUED.value,
        queue_name=QueueName.TRAINING.value,
        requested_by=requested_by,
        recipe_name=payload.recipe_name,
        recipe_version=payload.recipe_version,
        learning_rate=payload.learning_rate,
        image_size=payload.image_size,
        batch_size=payload.batch_size,
        optimizer=payload.optimizer,
        optimizer_settings=payload.optimizer_settings,
        resolved_training_defaults=payload.resolved_training_defaults.model_dump(mode="json"),
        idempotency_key=resolved_idempotency_key,
        submission_hash=request_hash,
        allow_duplicate_snapshot_training=payload.allow_duplicate_snapshot_training,
        best_checkpoint_rule=payload.best_checkpoint_rule.model_dump(),
        promotion_criteria=[item.model_dump() for item in resolved_promotion_criteria],
        promotion_policy=promotion_policy.model_dump(mode="json"),
        config_snapshot=payload.model_dump(mode="json"),
        progress=progress,
        evaluation_summary={},
        metrics_summary={},
        runtime_metadata={},
    )
    session.add(row)
    session.flush()
    create_audit_event(
        session,
        event_type="training.job.state_changed.v1",
        aggregate_type="training_job",
        aggregate_id=row.id,
        source_service=source_service,
        actor_type="user" if requested_by else "system",
        actor_id=requested_by or source_service,
        payload={
            "previous_state": None,
            "current_state": TrainingJobStatus.QUEUED.value,
            "reason": "Training job created and queued.",
            "dataset_snapshot_id": payload.dataset_snapshot_id,
            "model_family_slug": payload.model_family_slug,
        },
    )
    return row


def claim_training_job(
    session: Session,
    *,
    training_job_id: str,
    worker_instance_id: str,
    source_service: str,
) -> TrainingJob:
    row = get_training_job_record(session, training_job_id)
    if row is None:
        raise ValueError(f"Training job {training_job_id} does not exist.")
    if row.status in TERMINAL_TRAINING_STATES:
        return row
    now = utc_now()
    row.worker_instance_id = worker_instance_id
    row.worker_claimed_at = now
    row.worker_heartbeat_at = now
    row.started_at = row.started_at or now
    row.progress = row.progress or {}
    session.flush()
    create_audit_event(
        session,
        event_type="training.job.claimed.v1",
        aggregate_type="training_job",
        aggregate_id=row.id,
        source_service=source_service,
        actor_type="system",
        actor_id=worker_instance_id,
        payload={"worker_instance_id": worker_instance_id},
    )
    return row


def update_training_job_heartbeat(
    session: Session,
    *,
    training_job_id: str,
    worker_instance_id: str,
    details: dict[str, Any],
) -> TrainingJob:
    row = get_training_job_record(session, training_job_id)
    if row is None:
        raise ValueError(f"Training job {training_job_id} does not exist.")
    row.worker_instance_id = worker_instance_id
    row.worker_heartbeat_at = utc_now()
    metadata = dict(row.runtime_metadata or {})
    metadata["last_heartbeat_details"] = details
    row.runtime_metadata = metadata
    session.flush()
    return row


def release_training_job_claim(session: Session, *, training_job_id: str) -> TrainingJob:
    row = get_training_job_record(session, training_job_id)
    if row is None:
        raise ValueError(f"Training job {training_job_id} does not exist.")
    row.worker_instance_id = None
    row.worker_claimed_at = None
    row.worker_heartbeat_at = None
    row.gpu_device = None
    session.flush()
    return row


def update_training_job_state(
    session: Session,
    *,
    training_job_id: str,
    status: TrainingJobStatus,
    source_service: str,
    actor_type: str,
    actor_id: str,
    reason: str | None = None,
    progress: dict[str, Any] | None = None,
    latest_checkpoint_id: str | None = None,
    best_checkpoint_id: str | None = None,
    gpu_device: str | None = None,
    evaluation_summary: dict[str, Any] | None = None,
    metrics_summary: dict[str, Any] | None = None,
    artifact_manifest_uri: str | None = None,
    model_version_id: str | None = None,
    mlflow_run_id: str | None = None,
    failure_class: str | None = None,
    last_error: str | None = None,
    runtime_metadata: dict[str, Any] | None = None,
) -> TrainingJob:
    row = get_training_job_record(session, training_job_id)
    if row is None:
        raise ValueError(f"Training job {training_job_id} does not exist.")
    previous_state = row.status
    row.status = status.value
    if progress is not None:
        row.progress = progress
    if latest_checkpoint_id is not None:
        row.latest_checkpoint_id = latest_checkpoint_id
    if best_checkpoint_id is not None:
        row.best_checkpoint_id = best_checkpoint_id
    if gpu_device is not None:
        row.gpu_device = gpu_device
    if evaluation_summary is not None:
        row.evaluation_summary = evaluation_summary
    if metrics_summary is not None:
        row.metrics_summary = metrics_summary
    if artifact_manifest_uri is not None:
        row.artifact_manifest_uri = artifact_manifest_uri
    if model_version_id is not None:
        row.model_version_id = model_version_id
    if mlflow_run_id is not None:
        row.mlflow_run_id = mlflow_run_id
    if failure_class is not None:
        row.failure_class = failure_class
    if last_error is not None:
        row.last_error = last_error
    if status == TrainingJobStatus.COMPLETED:
        row.failure_class = None
        row.last_error = None
    if runtime_metadata:
        merged_metadata = dict(row.runtime_metadata or {})
        merged_metadata.update(runtime_metadata)
        row.runtime_metadata = merged_metadata
    if row.started_at is None and status not in {
        TrainingJobStatus.QUEUED,
        TrainingJobStatus.CANCELLED,
    }:
        row.started_at = utc_now()
    if status in {
        TrainingJobStatus.COMPLETED,
        TrainingJobStatus.FAILED,
        TrainingJobStatus.CANCELLED,
        TrainingJobStatus.ROLLED_BACK_IF_NEEDED,
    }:
        row.finished_at = utc_now()
    session.flush()
    if previous_state != row.status or reason:
        create_audit_event(
            session,
            event_type="training.job.state_changed.v1",
            aggregate_type="training_job",
            aggregate_id=row.id,
            source_service=source_service,
            actor_type=actor_type,
            actor_id=actor_id,
            payload={
                "previous_state": previous_state,
                "current_state": row.status,
                "reason": reason,
                "progress": row.progress or {},
                "latest_checkpoint_id": row.latest_checkpoint_id,
                "best_checkpoint_id": row.best_checkpoint_id,
                "gpu_device": row.gpu_device,
            },
        )
    return row


def request_training_job_cancel(
    session: Session,
    *,
    training_job_id: str,
    requested_by: str,
    source_service: str,
    reason: str | None,
    force: bool = False,
    stale_after_seconds: int | None = None,
) -> TrainingJob:
    row = get_training_job_record(session, training_job_id)
    if row is None:
        raise ValueError(f"Training job {training_job_id} does not exist.")
    now = utc_now()
    previous_state = row.status
    stale = _training_job_heartbeat_is_stale(
        row,
        stale_after_seconds=stale_after_seconds,
        now=now,
    )
    finish_immediately = force or stale
    row.cancel_requested = True
    row.cancel_requested_at = now
    row.cancel_requested_by = requested_by
    if row.status in {TrainingJobStatus.QUEUED.value, TrainingJobStatus.WAITING_FOR_GPU.value}:
        _finish_cancelled_training_job(row, now=now, reason=reason, force=force, stale=stale)
    elif row.status not in TERMINAL_TRAINING_STATES and finish_immediately:
        _finish_cancelled_training_job(row, now=now, reason=reason, force=force, stale=stale)
    elif row.status not in TERMINAL_TRAINING_STATES:
        row.status = TrainingJobStatus.CANCEL_REQUESTED.value
        row.progress = _progress_with_status(row.progress, TrainingJobStatus.CANCEL_REQUESTED)
        metadata = dict(row.runtime_metadata or {})
        metadata["cancellation"] = {
            "mode": "graceful",
            "requested_at": _iso(now),
            "requested_by": requested_by,
            "reason": reason,
        }
        row.runtime_metadata = metadata
    session.flush()
    create_audit_event(
        session,
        event_type="training.job.cancel_requested.v1",
        aggregate_type="training_job",
        aggregate_id=row.id,
        source_service=source_service,
        actor_type="user",
        actor_id=requested_by,
        payload={
            "reason": reason,
            "current_state": row.status,
            "cancel_requested_at": _iso(now),
            "force": force,
            "stale": stale,
            "finished_immediately": finish_immediately
            or row.status == TrainingJobStatus.CANCELLED.value,
        },
    )
    if previous_state != row.status:
        create_audit_event(
            session,
            event_type="training.job.state_changed.v1",
            aggregate_type="training_job",
            aggregate_id=row.id,
            source_service=source_service,
            actor_type="user",
            actor_id=requested_by,
            payload={
                "previous_state": previous_state,
                "current_state": row.status,
                "reason": reason or "Operator requested cancellation.",
                "force": force,
                "stale": stale,
            },
        )
    return row


def prepare_training_job_resume(
    session: Session,
    *,
    training_job_id: str,
    requested_by: str,
    source_service: str,
    reason: str | None,
) -> TrainingJob:
    row = get_training_job_record(session, training_job_id)
    if row is None:
        raise ValueError(f"Training job {training_job_id} does not exist.")
    if row.latest_checkpoint_id is None:
        raise ValueError("The training job has no resume-safe checkpoint to resume from.")
    previous_state = row.status
    row.cancel_requested = False
    row.cancel_requested_at = None
    row.cancel_requested_by = None
    row.resume_count += 1
    row.finished_at = None
    row.failure_class = None
    row.last_error = None
    row.status = TrainingJobStatus.RESUMING.value
    session.flush()
    create_audit_event(
        session,
        event_type="training.job.resume_requested.v1",
        aggregate_type="training_job",
        aggregate_id=row.id,
        source_service=source_service,
        actor_type="user",
        actor_id=requested_by,
        payload={"reason": reason, "latest_checkpoint_id": row.latest_checkpoint_id},
    )
    if previous_state != row.status:
        create_audit_event(
            session,
            event_type="training.job.state_changed.v1",
            aggregate_type="training_job",
            aggregate_id=row.id,
            source_service=source_service,
            actor_type="user",
            actor_id=requested_by,
            payload={
                "previous_state": previous_state,
                "current_state": row.status,
                "reason": reason or "Operator requested resume from latest checkpoint.",
            },
        )
    return row


def record_training_checkpoint(
    session: Session,
    *,
    training_job_id: str,
    epoch_no: int,
    step_no: int,
    storage_uri: str,
    sha256: str,
    checkpoint_kind: str,
    metric_primary: float | None,
    resume_safe: bool,
    is_best: bool,
    metrics: dict[str, Any],
) -> TrainingCheckpoint:
    if is_best:
        for existing in session.scalars(
            select(TrainingCheckpoint).where(TrainingCheckpoint.training_job_id == training_job_id)
        ).all():
            existing.is_best = False

    checkpoint = TrainingCheckpoint(
        id=generate_prefixed_id("chk"),
        training_job_id=training_job_id,
        step_no=step_no,
        epoch_no=epoch_no,
        metric_primary=metric_primary,
        checkpoint_kind=checkpoint_kind,
        storage_uri=storage_uri,
        sha256=sha256,
        resume_safe=resume_safe,
        is_best=is_best,
        metrics=metrics,
    )
    session.add(checkpoint)
    row = get_training_job_record(session, training_job_id)
    if row is not None:
        row.latest_checkpoint_id = checkpoint.id
        if is_best:
            row.best_checkpoint_id = checkpoint.id
    session.flush()
    return checkpoint


def prune_training_checkpoints(
    session: Session,
    *,
    training_job_id: str,
    keep_count: int,
) -> list[str]:
    effective_keep_count = max(int(keep_count), 1)
    job = get_training_job_record(session, training_job_id)
    checkpoints = session.scalars(
        select(TrainingCheckpoint)
        .where(TrainingCheckpoint.training_job_id == training_job_id)
        .order_by(
            TrainingCheckpoint.created_at.desc(),
            TrainingCheckpoint.step_no.desc(),
            TrainingCheckpoint.epoch_no.desc(),
        )
    ).all()
    if len(checkpoints) <= effective_keep_count:
        return []

    protected_ids = {
        checkpoint_id
        for checkpoint_id in (
            getattr(job, "latest_checkpoint_id", None),
            getattr(job, "best_checkpoint_id", None),
        )
        if checkpoint_id
    }
    protected_ids.update(checkpoint.id for checkpoint in checkpoints if checkpoint.is_best)

    retained_ids: set[str] = set(protected_ids)
    for checkpoint in checkpoints:
        if len(retained_ids) >= effective_keep_count and checkpoint.id not in retained_ids:
            continue
        retained_ids.add(checkpoint.id)

    stale_checkpoints = [
        checkpoint for checkpoint in checkpoints if checkpoint.id not in retained_ids
    ]
    if not stale_checkpoints:
        return []

    stale_ids = [checkpoint.id for checkpoint in stale_checkpoints]
    for metric in session.scalars(
        select(TrainingMetric).where(TrainingMetric.checkpoint_id.in_(stale_ids))
    ).all():
        metric.checkpoint_id = None

    retained_storage_uris = {
        checkpoint.storage_uri for checkpoint in checkpoints if checkpoint.id in retained_ids
    }
    storage_uris = [
        checkpoint.storage_uri
        for checkpoint in stale_checkpoints
        if checkpoint.storage_uri not in retained_storage_uris
    ]
    for checkpoint in stale_checkpoints:
        session.delete(checkpoint)
    session.flush()
    return storage_uris


def record_training_metric(
    session: Session,
    *,
    training_job_id: str,
    phase: str,
    metric_name: str,
    metric_value: float,
    epoch_no: int,
    step_no: int,
    checkpoint_id: str | None = None,
    is_primary: bool = False,
) -> TrainingMetric:
    row = TrainingMetric(
        id=generate_prefixed_id("met"),
        training_job_id=training_job_id,
        checkpoint_id=checkpoint_id,
        phase=phase,
        metric_name=metric_name,
        metric_value=metric_value,
        epoch_no=epoch_no,
        step_no=step_no,
        is_primary=is_primary,
    )
    session.add(row)
    session.flush()
    return row


def register_trained_model_version(
    session: Session,
    *,
    training_job_id: str,
    metrics_summary: dict[str, Any],
    evaluation_summary: dict[str, Any],
    artifact_manifest_uri: str | None,
    artifact_manifest: dict[str, Any],
    export_manifest_uri: str | None,
    export_manifest: dict[str, Any],
    onnx_uri: str | None,
    tensorrt_uri: str | None,
    dvc_rev: str | None,
    dvc_references: dict[str, Any],
    reproducibility_metadata: dict[str, Any],
    code_revision: str | None,
    mlflow_run_id: str | None,
    mlflow_registered_model: str | None,
    mlflow_model_version: str | None,
    alias_names: list[str],
    registry_tags: dict[str, str],
    immutable_metadata: dict[str, Any],
    immutable_metadata_sha256: str,
) -> ModelVersion:
    job = get_training_job_record(session, training_job_id)
    if job is None:
        raise ValueError(f"Training job {training_job_id} does not exist.")
    if job.model_family_id is None:
        family = _ensure_model_family(
            session,
            slug=job.model_family_slug,
            task_type=TaskType(job.task_type),
            architecture=architecture_summary(
                task_type=TaskType(job.task_type),
                family=job.architecture_family,
                version=job.architecture_version,
                size=job.architecture_size,
            ),
            promotion_policy=(
                PromotionCriteriaPolicy.model_validate(job.promotion_policy)
                if job.promotion_policy
                else None
            ),
        )
        job.model_family_id = family.id

    next_version = (
        session.scalar(
            select(func.max(ModelVersion.version_no)).where(
                ModelVersion.model_family_id == job.model_family_id
            )
        )
        or 0
    ) + 1
    model_version = ModelVersion(
        id=generate_prefixed_id("mvr"),
        model_family_id=job.model_family_id,
        version_no=next_version,
        training_job_id=job.id,
        dataset_snapshot_id=job.dataset_snapshot_id,
        status="trained",
        architecture_family=job.architecture_family,
        architecture_version=job.architecture_version,
        architecture_size=job.architecture_size,
        architecture_display_name=job.architecture_display_name,
        model_template_key=job.model_template_key,
        model_template_version=job.model_template_version,
        resolved_training_defaults=job.resolved_training_defaults or {},
        promotion_policy=job.promotion_policy or {},
        code_revision=code_revision,
        metrics_summary=metrics_summary,
        evaluation_summary=evaluation_summary,
        artifact_manifest_uri=artifact_manifest_uri,
        artifact_manifest=artifact_manifest,
        export_manifest_uri=export_manifest_uri,
        export_manifest=export_manifest,
        onnx_uri=onnx_uri,
        tensorrt_uri=tensorrt_uri,
        dvc_rev=dvc_rev,
        dvc_references=dvc_references,
        reproducibility_metadata=reproducibility_metadata,
        immutable_metadata=immutable_metadata,
        immutable_metadata_sha256=immutable_metadata_sha256,
        alias_names=alias_names,
        registry_tags=registry_tags,
        mlflow_run_id=mlflow_run_id,
        mlflow_registered_model=mlflow_registered_model or f"{job.model_family_slug}",
        mlflow_model_version=mlflow_model_version or str(next_version),
    )
    session.add(model_version)
    job.model_version_id = model_version.id
    job.mlflow_run_id = mlflow_run_id or job.mlflow_run_id
    session.flush()
    return model_version


def read_idempotency_response(
    session: Session,
    *,
    scope: str,
    key: str,
    request_hash: str,
) -> dict[str, Any] | None:
    row = session.scalar(
        select(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
    )
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise ValueError(
            "The supplied Idempotency-Key has already been used with a different request payload."
        )
    return row.response_body


def write_idempotency_response(
    session: Session,
    *,
    scope: str,
    key: str,
    request_hash: str,
    response_code: int,
    response_body: dict[str, Any],
    ttl_hours: int = 24,
) -> IdempotencyKey:
    row = session.scalar(
        select(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
    )
    expires_at = utc_now() + timedelta(hours=ttl_hours)
    if row is None:
        row = IdempotencyKey(
            id=generate_prefixed_id("idk"),
            scope=scope,
            key=key,
            request_hash=request_hash,
            response_code=response_code,
            response_body=response_body,
            expires_at=expires_at,
        )
        session.add(row)
    else:
        row.request_hash = request_hash
        row.response_code = response_code
        row.response_body = response_body
        row.expires_at = expires_at
    session.flush()
    return row


def mark_stale_training_jobs(
    session: Session,
    *,
    stale_after_seconds: int,
    source_service: str,
) -> list[str]:
    cutoff = utc_now() - timedelta(seconds=stale_after_seconds)
    rows = session.scalars(
        select(TrainingJob).where(
            TrainingJob.status.in_(ACTIVE_TRAINING_STATES),
            TrainingJob.worker_heartbeat_at.is_not(None),
            TrainingJob.worker_heartbeat_at < cutoff,
        )
    ).all()
    stale_ids: list[str] = []
    for row in rows:
        metadata = dict(row.runtime_metadata or {})
        metadata["stale_detected_at"] = utc_now_iso()
        row.runtime_metadata = metadata
        stale_ids.append(row.id)
        create_audit_event(
            session,
            event_type="training.job.stale_detected.v1",
            aggregate_type="training_job",
            aggregate_id=row.id,
            source_service=source_service,
            actor_type="system",
            actor_id=source_service,
            payload={
                "worker_instance_id": row.worker_instance_id,
                "last_heartbeat_at": _iso(row.worker_heartbeat_at),
            },
        )
    session.flush()
    return stale_ids


def record_service_heartbeat(
    session: Session,
    *,
    service_name: str,
    instance_id: str,
    status: str,
    details: dict[str, Any],
) -> ServiceHeartbeat:
    row = session.get(ServiceHeartbeat, {"service_name": service_name, "instance_id": instance_id})
    if row is None:
        row = ServiceHeartbeat(
            service_name=service_name,
            instance_id=instance_id,
            status=status,
            observed_at=utc_now(),
            details=details,
        )
        session.add(row)
    else:
        row.status = status
        row.observed_at = utc_now()
        row.details = details
    session.flush()
    return row


def list_service_heartbeats(session: Session) -> list[ServiceHeartbeat]:
    return session.scalars(
        select(ServiceHeartbeat).order_by(
            ServiceHeartbeat.service_name.asc(), ServiceHeartbeat.instance_id.asc()
        )
    ).all()


def _ensure_model_family(
    session: Session,
    *,
    slug: str,
    task_type: TaskType,
    architecture: Any | None = None,
    promotion_policy: PromotionCriteriaPolicy | None = None,
) -> ModelFamily:
    row = session.scalar(select(ModelFamily).where(ModelFamily.slug == slug, ModelFamily.deleted_at.is_(None)))
    if row is not None:
        try:
            existing_task_type = TaskType(row.task_type)
        except ValueError:
            existing_task_type = None
        if existing_task_type is not None and existing_task_type != task_type:
            raise ValueError(
                f"Model family {slug} already exists with task type {existing_task_type.value} "
                f"but the training request is {task_type.value}."
            )
        if architecture is not None:
            requested_architecture = (
                getattr(architecture, "family", "").lower(),
                getattr(architecture, "version", "").lower(),
                getattr(architecture, "size", "").lower(),
            )
            existing_architecture = (
                row.architecture_family.lower(),
                row.architecture_version.lower(),
                row.architecture_size.lower(),
            )
            if requested_architecture != existing_architecture:
                raise ValueError(
                    f"Model family {slug} uses architecture {row.architecture_display_name}, "
                    "but the training request selected a different architecture."
                )
        if promotion_policy is not None and not row.promotion_policy:
            row.promotion_policy = promotion_policy.model_dump(mode="json")
        return row
    architecture_family = getattr(architecture, "family", "yolo")
    architecture_version = getattr(architecture, "version", "v8")
    architecture_size = getattr(architecture, "size", "n")
    architecture_display_name = getattr(architecture, "display_name", "YOLOv8 Nano")
    row = ModelFamily(
        id=generate_prefixed_id("mfy"),
        slug=slug,
        task_type=task_type.value,
        architecture_family=architecture_family,
        architecture_version=architecture_version,
        architecture_size=architecture_size,
        architecture_display_name=architecture_display_name,
        promotion_policy=(
            promotion_policy.model_dump(mode="json") if promotion_policy is not None else {}
        ),
        contract_major=1,
        input_schema_ref=f"contracts/{task_type.value}/input.v1.json",
        output_schema_ref=f"contracts/{task_type.value}/output.v1.json",
        active=True,
    )
    session.add(row)
    session.flush()
    return row


def _build_progress(
    *,
    status: TrainingJobStatus,
    current_epoch: int,
    current_step: int,
    total_epochs: int,
    total_steps: int,
) -> dict[str, Any]:
    effective_total_steps = max(total_steps, 1)
    completed_steps = min(max(current_step, 0), effective_total_steps)
    return {
        "status": status.value,
        "current_epoch": current_epoch,
        "current_step": current_step,
        "total_epochs": total_epochs,
        "total_steps": total_steps,
        "progress_percent": round((completed_steps / effective_total_steps) * 100, 2),
        "updated_at": utc_now_iso(),
    }


def _progress_with_status(
    progress: dict[str, Any] | None,
    status: TrainingJobStatus,
) -> dict[str, Any]:
    next_progress = dict(progress or {})
    next_progress["status"] = status.value
    next_progress["updated_at"] = utc_now_iso()
    return next_progress


def _training_job_heartbeat_is_stale(
    row: TrainingJob,
    *,
    stale_after_seconds: int | None,
    now: datetime,
) -> bool:
    if stale_after_seconds is None or row.worker_heartbeat_at is None:
        return False
    heartbeat_at = row.worker_heartbeat_at
    cutoff = now - timedelta(seconds=stale_after_seconds)
    if heartbeat_at.tzinfo is None and cutoff.tzinfo is not None:
        cutoff = cutoff.replace(tzinfo=None)
    elif heartbeat_at.tzinfo is not None and cutoff.tzinfo is None:
        heartbeat_at = heartbeat_at.replace(tzinfo=None)
    return heartbeat_at < cutoff


def _finish_cancelled_training_job(
    row: TrainingJob,
    *,
    now: datetime,
    reason: str | None,
    force: bool,
    stale: bool,
) -> None:
    row.status = TrainingJobStatus.CANCELLED.value
    row.finished_at = now
    row.worker_instance_id = None
    row.worker_claimed_at = None
    row.worker_heartbeat_at = None
    row.gpu_device = None
    row.progress = _progress_with_status(row.progress, TrainingJobStatus.CANCELLED)
    metadata = dict(row.runtime_metadata or {})
    metadata["cancellation"] = {
        "mode": "forced" if force else "immediate",
        "cancelled_at": _iso(now),
        "reason": reason,
        "force": force,
        "stale": stale,
    }
    row.runtime_metadata = metadata


def _job_summary(row: TrainingJob) -> TrainingJobSummary:
    progress = row.progress or {}
    evaluation_summary = TrainingEvaluationSummary.model_validate(row.evaluation_summary or {})
    runtime_metadata = row.runtime_metadata or {}
    return TrainingJobSummary(
        training_job_id=row.id,
        model_family_slug=row.model_family_slug,
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
        progress_percent=float(progress.get("progress_percent", 0)),
        current_epoch=int(progress.get("current_epoch", 0)),
        current_step=int(progress.get("current_step", 0)),
        total_epochs=int(progress.get("total_epochs", 0)),
        latest_checkpoint_id=row.latest_checkpoint_id,
        worker_instance_id=row.worker_instance_id,
        gpu_device=row.gpu_device,
        trainer_mode=runtime_metadata.get("trainer_mode"),
        trainer_backend=runtime_metadata.get("trainer_backend"),
        promotion_ready=evaluation_summary.promotion_ready,
        created_at=_iso(row.created_at) or utc_now_iso(),
        started_at=_iso(row.started_at),
        finished_at=_iso(row.finished_at),
    )


def _checkpoint_summary(row: TrainingCheckpoint) -> TrainingCheckpointSummary:
    return TrainingCheckpointSummary(
        checkpoint_id=row.id,
        training_job_id=row.training_job_id,
        step_no=row.step_no,
        epoch_no=row.epoch_no,
        metric_primary=row.metric_primary,
        checkpoint_kind=row.checkpoint_kind,
        storage_uri=row.storage_uri,
        sha256=row.sha256,
        resume_safe=row.resume_safe,
        is_best=row.is_best,
        metrics={key: float(value) for key, value in (row.metrics or {}).items()},
        created_at=_iso(row.created_at) or utc_now_iso(),
    )


def _metric_record(row: TrainingMetric) -> TrainingMetricRecord:
    return TrainingMetricRecord(
        metric_id=row.id,
        training_job_id=row.training_job_id,
        phase=row.phase,
        metric_name=row.metric_name,
        metric_value=row.metric_value,
        epoch_no=row.epoch_no,
        step_no=row.step_no,
        is_primary=row.is_primary,
        checkpoint_id=row.checkpoint_id,
        recorded_at=_iso(row.created_at) or utc_now_iso(),
    )


def _training_event_summary(row: AuditEvent) -> TrainingJobEventSummary:
    return TrainingJobEventSummary(
        event_id=row.id,
        event_type=row.event_type,
        sequence_no=row.sequence_no,
        source_service=row.source_service,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        occurred_at=_iso(row.occurred_at) or utc_now_iso(),
        payload=row.payload or {},
    )


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)
