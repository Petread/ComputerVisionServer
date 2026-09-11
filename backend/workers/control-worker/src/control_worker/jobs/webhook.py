from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from redis import Redis
from rq import Queue, Retry

from cv_core import load_platform_settings
from cv_datasets import LabelStudioClient, LabelStudioError
from cv_db import (
    create_audit_event,
    create_or_reuse_dataset_snapshot_request,
    get_label_project_by_external_id,
    get_label_project_record,
    get_webhook_event_record,
    session_scope,
    update_webhook_event,
    upsert_discovered_label_project,
)
from cv_status import QueueName, SnapshotStatus, WebhookProcessingStatus
from cv_utils import utc_now, utc_now_iso
from control_worker.jobs.pipeline_automation import request_snapshot_after_project_sync


def process_labelstudio_event(event_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    snapshot_id: str | None = None
    try:
        with session_scope(settings) as session:
            event = get_webhook_event_record(session, event_id)
            if event is None:
                raise ValueError(f"Webhook event {event_id} does not exist.")
            if event.processing_status == WebhookProcessingStatus.PROCESSED.value:
                return {"event_id": event_id, "status": "processed", "finished_at": utc_now_iso()}

            update_webhook_event(
                session,
                event_id,
                processing_status=WebhookProcessingStatus.PROCESSING,
            )
            label_project = None
            if event.label_studio_project_id is not None:
                label_project = get_label_project_by_external_id(session, event.label_studio_project_id)
                if label_project is None and _embedded_project_payload(event.payload):
                    label_project = upsert_discovered_label_project(
                        session,
                        project_payload=_embedded_project_payload(event.payload),
                        automatic_snapshot_actions=settings.dataset_workflow.automatic_snapshot_actions,
                        minimum_annotation_count=settings.dataset_workflow.minimum_annotation_count,
                        required_classes=settings.dataset_workflow.required_classes,
                        split_ratios=(
                            settings.dataset_workflow.default_train_ratio,
                            settings.dataset_workflow.default_val_ratio,
                            settings.dataset_workflow.default_test_ratio,
                        ),
                    )

            internal_event_type = _internal_event_type(event.action, label_project is not None)
            create_audit_event(
                session,
                event_type="webhook.label_studio.received.v1",
                aggregate_type="webhook_event",
                aggregate_id=event.id,
                actor_type="service",
                actor_id="label-studio",
                causation_id=event.id,
                payload={
                    "action": event.action,
                    "source_event_key": event.source_event_key,
                    "label_studio_project_id": event.label_studio_project_id,
                },
            )

            if label_project is not None:
                _apply_event_to_project(label_project, event.payload)
                create_audit_event(
                    session,
                    event_type=internal_event_type,
                    aggregate_type="label_project",
                    aggregate_id=label_project.id,
                    actor_type="service",
                    actor_id="label-studio",
                    causation_id=event.id,
                    payload={
                        "action": event.action,
                        "source_event_key": event.source_event_key,
                        "annotation_count": label_project.annotation_count,
                        "annotated_task_count": label_project.annotated_task_count,
                    },
                )

                if (
                    label_project.dataset_id
                    and label_project.auto_snapshot_enabled
                    and event.action in (label_project.qualifying_actions or [])
                ):
                    snapshot = create_or_reuse_dataset_snapshot_request(
                        session,
                        dataset_id=label_project.dataset_id,
                        label_project_id=label_project.id,
                        export_format="JSON",
                        trigger="webhook",
                        reason=f"Automatic snapshot after {event.action}",
                        created_by="label-studio-webhook",
                        source_event_key=event.source_event_key,
                        idempotency_key=f"webhook:{event.source_event_key}",
                    )
                    snapshot_id = snapshot.snapshot_id
                    create_audit_event(
                        session,
                        event_type="dataset.snapshot.requested.v1",
                        aggregate_type="dataset_snapshot",
                        aggregate_id=snapshot.snapshot_id,
                        actor_type="service",
                        actor_id="label-studio",
                        causation_id=event.id,
                        payload={
                            "trigger": "webhook",
                            "action": event.action,
                            "label_project_id": label_project.id,
                        },
                    )

            update_webhook_event(
                session,
                event_id,
                processing_status=WebhookProcessingStatus.PROCESSED,
                internal_event_type=internal_event_type,
                label_project_id=label_project.id if label_project is not None else None,
                processing_error=None,
                mark_processed=True,
            )

        if snapshot_id:
            _dataset_queue(settings).enqueue(
                "control_worker.jobs.dataset.create_labelstudio_snapshot_export",
                snapshot_id=snapshot_id,
                retry=Retry(max=3, interval=[5, 30, 120]),
            )
        return {"event_id": event_id, "status": "processed", "finished_at": utc_now_iso()}
    except Exception as exc:
        with session_scope(settings) as session:
            if get_webhook_event_record(session, event_id) is not None:
                update_webhook_event(
                    session,
                    event_id,
                    processing_status=WebhookProcessingStatus.FAILED,
                    processing_error=str(exc),
                    mark_processed=True,
                )
        raise


def reconcile_project_counters(
    label_project_id: str,
    *,
    force_new_snapshot: bool = False,
) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    snapshot_id: str | None = None
    with session_scope(settings) as session:
        label_project = get_label_project_record(session, label_project_id)
        if label_project is None:
            raise ValueError(f"Label Studio project {label_project_id} does not exist.")
        external_project_id = label_project.label_studio_project_id

    client = _label_studio_client(settings)
    project_payload = next(
        (
            project
            for project in client.list_projects()
            if int(project.get("id", -1)) == external_project_id
        ),
        None,
    )
    if project_payload is None:
        raise ValueError(
            f"Label Studio project {external_project_id} could not be found during reconciliation."
        )

    with session_scope(settings) as session:
        row = upsert_discovered_label_project(
            session,
            project_payload=project_payload,
            automatic_snapshot_actions=settings.dataset_workflow.automatic_snapshot_actions,
            minimum_annotation_count=settings.dataset_workflow.minimum_annotation_count,
            required_classes=settings.dataset_workflow.required_classes,
            split_ratios=(
                settings.dataset_workflow.default_train_ratio,
                settings.dataset_workflow.default_val_ratio,
                settings.dataset_workflow.default_test_ratio,
            ),
        )
        create_audit_event(
            session,
            event_type="label_project.reconciled.v1",
            aggregate_type="label_project",
            aggregate_id=row.id,
            actor_type="system",
            actor_id="control-worker",
            payload={
                "task_count": row.task_count,
                "annotated_task_count": row.annotated_task_count,
                "annotation_count": row.annotation_count,
            },
        )
        snapshot_id = request_snapshot_after_project_sync(
            session,
            label_project=row,
            trigger="reconcile",
            reason="Automatic snapshot after project reconciliation",
            created_by="control-worker-reconcile",
            actor_id="control-worker",
            force_new_snapshot=force_new_snapshot,
        )

    if snapshot_id:
        _dataset_queue(settings).enqueue(
            "control_worker.jobs.dataset.create_labelstudio_snapshot_export",
            snapshot_id=snapshot_id,
            retry=Retry(max=3, interval=[5, 30, 120]),
        )
    return {
        "label_project_id": label_project_id,
        "status": "reconciled",
        "snapshot_id": snapshot_id,
        "finished_at": utc_now_iso(),
    }


def _dataset_queue(settings) -> Queue:
    return Queue(QueueName.DATASET.value, connection=Redis.from_url(settings.redis.url()))


def _label_studio_client(settings) -> LabelStudioClient:
    token = settings.label_studio.token()
    if not token:
        raise LabelStudioError("The Label Studio API token is not configured.")
    return LabelStudioClient(
        base_url=settings.label_studio.base_url,
        token=token,
        timeout_seconds=settings.label_studio.request_timeout_seconds,
    )


def _embedded_project_payload(payload: dict[str, Any]) -> dict[str, Any]:
    project = payload.get("project")
    if isinstance(project, dict):
        return project
    project_id = payload.get("project_id")
    if project_id is not None:
        return {
            "id": project_id,
            "title": payload.get("project_title") or f"Project {project_id}",
            "label_config": payload.get("label_config") or "",
        }
    return {}


def _internal_event_type(action: str, has_project_mapping: bool) -> str:
    action_upper = action.upper()
    if "ANNOTATION" in action_upper or "REVIEW" in action_upper:
        return "label_project.annotation_state_changed.v1" if has_project_mapping else "label_project.annotation_activity_observed.v1"
    if "PROJECT" in action_upper:
        return "label_project.state_changed.v1"
    return "label_project.activity_observed.v1"


def _apply_event_to_project(label_project, payload: dict[str, Any]) -> None:
    project_payload = _embedded_project_payload(payload)
    label_project.last_activity_at = utc_now()
    if project_payload:
        label_project.title = str(project_payload.get("title") or label_project.title)
        label_project.task_count = int(
            project_payload.get("task_number")
            or project_payload.get("task_count")
            or label_project.task_count
        )
        label_project.annotated_task_count = int(
            project_payload.get("num_tasks_with_annotations")
            or project_payload.get("finished_task_number")
            or label_project.annotated_task_count
        )
        label_project.annotation_count = int(
            project_payload.get("useful_annotation_number") or label_project.annotation_count
        )
        label_project.skipped_annotation_count = int(
            project_payload.get("skipped_annotations_number") or label_project.skipped_annotation_count
        )

    observed_labels = _extract_payload_labels(payload)
    if observed_labels:
        distribution = Counter(label_project.class_distribution or {})
        distribution.update(observed_labels)
        label_project.class_distribution = dict(sorted(distribution.items()))
        if not project_payload:
            label_project.annotation_count += sum(observed_labels.values())
            label_project.annotated_task_count = max(label_project.annotated_task_count, 1)


def _extract_payload_labels(payload: dict[str, Any]) -> Counter[str]:
    labels: Counter[str] = Counter()
    annotation_candidates: list[dict[str, Any]] = []
    for key in ("annotation", "review"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            annotation_candidates.append(candidate)
    annotations = payload.get("annotations")
    if isinstance(annotations, list):
        annotation_candidates.extend([candidate for candidate in annotations if isinstance(candidate, dict)])

    for annotation in annotation_candidates:
        results = annotation.get("result")
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            value = result.get("value")
            if not isinstance(value, dict):
                continue
            for key in ("choices", "labels", "rectanglelabels"):
                candidate = value.get(key)
                if isinstance(candidate, list):
                    labels.update(entry for entry in candidate if isinstance(entry, str))
    return labels
