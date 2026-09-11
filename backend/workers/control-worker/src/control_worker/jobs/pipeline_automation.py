from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_contracts import ProjectTrainRequest
from cv_db import (
    build_project_training_request,
    create_audit_event,
    create_or_reuse_dataset_snapshot_request,
    create_training_job_request,
)
from cv_db.models import DatasetSnapshot, LabelProject, ModelFamily, TrainingJob
from cv_status import SnapshotStatus, TrainingJobStatus


def request_snapshot_after_project_sync(
    session: Session,
    *,
    label_project,
    trigger: str,
    reason: str,
    created_by: str,
    actor_id: str,
    causation_id: str | None = None,
    force_new_snapshot: bool = False,
) -> str | None:
    if not label_project.dataset_id or not label_project.auto_snapshot_enabled:
        return None

    minimum_annotation_count = max(int(label_project.minimum_annotation_count or 0), 1)
    annotations_ready = (
        int(getattr(label_project, "eligible_task_count", 0) or 0) >= minimum_annotation_count
        and (
            int(getattr(label_project, "submitted_labeled_task_count", 0) or 0) > 0
            or int(getattr(label_project, "submitted_background_task_count", 0) or 0) > 0
        )
    )
    if not annotations_ready:
        return None

    latest_snapshot = session.scalar(
        select(DatasetSnapshot)
        .where(
            DatasetSnapshot.dataset_id == label_project.dataset_id,
            DatasetSnapshot.label_project_id == label_project.id,
        )
        .order_by(DatasetSnapshot.snapshot_no.desc())
    )
    active_statuses = {
        SnapshotStatus.REQUESTED.value,
        SnapshotStatus.EXPORTING.value,
        SnapshotStatus.MATERIALIZING.value,
        SnapshotStatus.VALIDATING.value,
    }
    idempotency_key = None
    if force_new_snapshot and latest_snapshot is not None:
        if latest_snapshot.status in active_statuses:
            return latest_snapshot.id
        idempotency_key = f"manual-sync:{label_project.id}:{latest_snapshot.id}"

    snapshot = create_or_reuse_dataset_snapshot_request(
        session,
        dataset_id=label_project.dataset_id,
        label_project_id=label_project.id,
        export_format="JSON",
        trigger=trigger,
        reason=reason,
        created_by=created_by,
        idempotency_key=idempotency_key,
    )
    create_audit_event(
        session,
        event_type="dataset.snapshot.requested.v1",
        aggregate_type="dataset_snapshot",
        aggregate_id=snapshot.snapshot_id,
        actor_type="system",
        actor_id=actor_id,
        causation_id=causation_id,
        payload={
            "trigger": trigger,
            "reason": reason,
            "label_project_id": label_project.id,
        },
    )
    return snapshot.snapshot_id


def request_training_after_snapshot_ready(
    session: Session,
    *,
    snapshot_id: str,
) -> str | None:
    """Create at most one automatic run for a snapshot and its configured project model."""
    snapshot = session.get(DatasetSnapshot, snapshot_id)
    if snapshot is None or snapshot.status != SnapshotStatus.READY.value:
        return None
    project = (
        session.get(LabelProject, snapshot.label_project_id)
        if snapshot.label_project_id
        else None
    )
    if (
        project is None
        or project.deleted_at is not None
        or not bool(project.auto_train_enabled)
        or not project.default_model_family_id
    ):
        return None
    family = session.get(ModelFamily, project.default_model_family_id)
    if (
        family is None
        or family.deleted_at is not None
        or not family.active
        or family.label_project_id != project.id
        or family.task_type != project.task_type
    ):
        create_audit_event(
            session,
            event_type="project_pipeline.auto_training.skipped.v1",
            aggregate_type="label_project",
            aggregate_id=project.id,
            actor_type="system",
            actor_id="project-auto-training",
            payload={
                "snapshot_id": snapshot.id,
                "model_family_id": project.default_model_family_id,
                "reason": "The configured default model is no longer valid for this project.",
            },
        )
        return None

    existing = session.scalar(
        select(TrainingJob)
        .where(
            TrainingJob.dataset_snapshot_id == snapshot.id,
            TrainingJob.model_family_id == family.id,
        )
        .limit(1)
    )
    if existing:
        return existing.id if existing.status == TrainingJobStatus.QUEUED.value else None

    training_request = build_project_training_request(
        session,
        label_project_id=project.id,
        payload=ProjectTrainRequest(
            model_family_id=family.id,
            dataset_snapshot_id=snapshot.id,
            allow_duplicate_snapshot_training=False,
        ),
    )
    idempotency_key = f"auto-train:{snapshot.id}:{family.id}"
    job = create_training_job_request(
        session,
        training_request,
        requested_by="project-auto-training",
        source_service="control-worker",
        idempotency_key=idempotency_key,
    )
    create_audit_event(
        session,
        event_type="project_pipeline.auto_training.requested.v1",
        aggregate_type="label_project",
        aggregate_id=project.id,
        actor_type="system",
        actor_id="project-auto-training",
        payload={
            "snapshot_id": snapshot.id,
            "model_family_id": family.id,
            "training_job_id": job.id,
            "idempotency_key": idempotency_key,
        },
    )
    return job.id
