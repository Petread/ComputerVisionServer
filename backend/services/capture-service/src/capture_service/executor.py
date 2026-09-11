from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from cv_core import PlatformSettings
from cv_datasets.label_studio import LabelStudioClient, LabelStudioError
from cv_datasets.temporal_import import (
    TemporalLabelStudioTask,
    TemporalSampleImportError,
    build_temporal_label_studio_task,
    find_temporal_task_id,
    task_ids_from_import_response,
)
from cv_db import (
    claim_due_capture_requests,
    create_audit_event,
    enqueue_due_manual_interval_captures,
    get_runtime_live_view_state,
    record_service_heartbeat,
    session_scope,
    update_manual_capture_from_delivery,
)
from cv_db.models import (
    Dataset,
    LabelProject,
    ResultRecord,
    RuntimeCaptureRequest,
    RuntimeChannel,
    RuntimeSource,
)
from cv_runtime.payloads import FramePayload, RoiPayload, RuntimeImageContext
from cv_runtime.temporal_samples import (
    TemporalSampleSpoolNotReadyError,
    load_temporal_sample_materialization,
    materialize_temporal_sample_spool,
)
from cv_status import LabelProjectStatus, TaskType
from cv_utils import PlatformError, inspect_image_bytes, utc_now
from sqlalchemy import func, select
from sqlalchemy.orm import Session

LOGGER = logging.getLogger("capture-service")
_CAPTURE_STORAGE_CACHE_LOCK = threading.Lock()
_CAPTURE_STORAGE_USAGE_CACHE: dict[str, tuple[float, int]] = {}


class LabelStudioClientFactory(Protocol):
    def __call__(self, *, base_url: str, token: str, timeout_seconds: int) -> LabelStudioClient:
        ...


@dataclass(frozen=True)
class StoredCaptureAsset:
    path: Path
    uri: str
    digest: str
    mime_type: str
    width: int | None
    height: int | None
    source_frame_metadata: dict[str, Any]
    runtime_execution_id: str | None = None


@dataclass(frozen=True)
class LabelStudioSubmission:
    task_ids: list[int]
    predicted: bool
    prediction_status: str
    prediction_error: str | None = None
    prediction_backend_url: str | None = None
    prediction_model_version: str | None = None


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        LOGGER.debug("Could not adjust capture asset permissions for %s.", path, exc_info=True)


def _make_capture_path_web_readable(path: Path, *, root: Path) -> None:
    try:
        current = path.resolve()
    except OSError:
        current = path
    try:
        stop_at = root.resolve()
    except OSError:
        stop_at = root

    directories: list[Path] = []
    while True:
        directories.append(current)
        if current == stop_at or current.parent == current:
            break
        current = current.parent
    for directory in reversed(directories):
        _chmod_best_effort(directory, 0o755)


@dataclass(frozen=True)
class CaptureDrainStats:
    processed: int = 0
    delivered: int = 0
    failed_retryable: int = 0
    failed_terminal: int = 0
    skipped_duplicate: int = 0
    rate_limited: int = 0
    needs_attention: int = 0
    enqueued_interval_captures: int = 0

    def add(
        self,
        *,
        processed: int = 0,
        status: str | None = None,
        intervals: int = 0,
    ) -> CaptureDrainStats:
        return CaptureDrainStats(
            processed=self.processed + processed,
            delivered=self.delivered + (1 if status in {"delivered", "predicted"} else 0),
            failed_retryable=self.failed_retryable + (1 if status == "failed_retryable" else 0),
            failed_terminal=self.failed_terminal + (1 if status == "failed_terminal" else 0),
            skipped_duplicate=self.skipped_duplicate + (1 if status == "skipped_duplicate" else 0),
            rate_limited=self.rate_limited + (1 if status == "rate_limited" else 0),
            needs_attention=self.needs_attention + (1 if status == "needs_attention" else 0),
            enqueued_interval_captures=self.enqueued_interval_captures + intervals,
        )


async def capture_service_loop(
    *,
    settings: PlatformSettings,
    stop_event: asyncio.Event,
    client_factory: LabelStudioClientFactory = LabelStudioClient,
) -> None:
    while not stop_event.is_set():
        stats = CaptureDrainStats()
        try:
            stats = await asyncio.to_thread(
                drain_capture_work,
                settings=settings,
                limit=25,
                client_factory=client_factory,
        )
        except Exception:
            LOGGER.exception("Capture service loop failed while draining capture work.")
        await asyncio.to_thread(
            _record_capture_service_heartbeat,
            settings,
            status="running",
            stats=stats,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except TimeoutError:
            continue
    await asyncio.to_thread(
        _record_capture_service_heartbeat,
        settings,
        status="stopping",
        stats=CaptureDrainStats(),
    )


def drain_capture_work(
    *,
    settings: PlatformSettings,
    limit: int = 20,
    client_factory: LabelStudioClientFactory = LabelStudioClient,
) -> CaptureDrainStats:
    stats = CaptureDrainStats()
    with session_scope(settings) as session:
        enqueued = enqueue_due_manual_interval_captures(session)
        stats = stats.add(intervals=enqueued)
    with session_scope(settings) as session:
        rows = claim_due_capture_requests(
            session,
            limit=limit,
            processing_timeout_seconds=max(
                int(settings.label_studio.request_timeout_seconds) * 2,
                60,
            ),
        )
        row_ids = [row.id for row in rows]

    for row_id in row_ids:
        status = _process_capture_request(
            settings=settings,
            capture_request_id=row_id,
            client_factory=client_factory,
        )
        stats = stats.add(processed=1, status=status)
    return stats


def _record_capture_service_heartbeat(
    settings: PlatformSettings,
    *,
    status: str,
    stats: CaptureDrainStats,
) -> None:
    with session_scope(settings) as session:
        record_service_heartbeat(
            session,
            service_name="capture-service",
            instance_id="capture-service-main",
            status=status,
            details={
                "processed": stats.processed,
                "delivered": stats.delivered,
                "failed_retryable": stats.failed_retryable,
                "failed_terminal": stats.failed_terminal,
                "rate_limited": stats.rate_limited,
                "needs_attention": stats.needs_attention,
                "interval_captures_enqueued": stats.enqueued_interval_captures,
            },
        )


def _process_capture_request(
    *,
    settings: PlatformSettings,
    capture_request_id: str,
    client_factory: LabelStudioClientFactory,
) -> str:
    with session_scope(settings) as session:
        row = session.get(RuntimeCaptureRequest, capture_request_id)
        if row is None:
            return "failed_terminal"
        if row.status != "processing":
            return row.status
        try:
            if row.capture_mode == "temporal_sequence":
                _materialize_temporal_sequence_capture(
                    settings,
                    session,
                    row,
                    client_factory=client_factory,
                )
                return row.status
            target = _validate_target(session, row)
            if _rate_limited(settings, session, row):
                return row.status
            asset = _materialize_asset(settings, session, row)
            if _duplicate_suppressed(settings, session, row, asset):
                return row.status
            submission = _submit_to_label_studio(
                settings=settings,
                row=row,
                target=target,
                asset=asset,
                client_factory=client_factory,
            )
            now = utc_now()
            row.status = "delivered"
            row.submitted_at = now
            row.delivered_at = now
            row.asset_uri = asset.uri
            row.digest = asset.digest
            row.label_studio_task_ids = submission.task_ids
            row.source_frame_metadata = asset.source_frame_metadata
            _clear_terminal_capture_payload(row)
            row.last_error = (
                f"Prediction failed: {submission.prediction_error}"
                if submission.prediction_error
                else None
            )
            if submission.predicted:
                row.status = "predicted"
            row.metadata_json = {
                **(row.metadata_json or {}),
                "delivery_status": row.status,
                "submission_mode": settings.runtime.label_studio_submission_mode,
                "asset_width": asset.width,
                "asset_height": asset.height,
                "prediction_status": submission.prediction_status,
                "prediction_backend_url": submission.prediction_backend_url,
                "prediction_model_version": submission.prediction_model_version,
            }
            if submission.prediction_error:
                row.metadata_json["prediction_error"] = submission.prediction_error
            update_manual_capture_from_delivery(
                session,
                row,
                status="success",
                asset_uri=asset.uri,
                digest=asset.digest,
                label_studio_task_ids=submission.task_ids,
                source_frame_metadata=asset.source_frame_metadata,
                runtime_execution_id=asset.runtime_execution_id,
            )
            _write_capture_audit(
                session,
                row,
                event_type=(
                    "runtime.capture.predicted.v1"
                    if submission.predicted
                    else "runtime.capture.delivered.v1"
                ),
            )
        except _TerminalCaptureError as exc:
            LOGGER.error("Terminal capture delivery failure for request %s: %s", row.id, exc)
            _mark_failed(session, row, error=str(exc), retryable=False)
        except PlatformError as exc:
            _mark_failed(session, row, error=str(exc), retryable=exc.retryable)
        except LabelStudioError as exc:
            _mark_failed(session, row, error=str(exc), retryable=True)
            if row.capture_mode == "temporal_sequence":
                _update_temporal_result_asset_references(
                    session,
                    row,
                    session_id=str(
                        (row.source_frame_metadata or {}).get("session_id") or ""
                    ),
                    references={
                        **dict((row.metadata_json or {}).get("asset_references") or {}),
                        "label_studio_import": _temporal_import_reference(row),
                    },
                )
        except TemporalSampleSpoolNotReadyError as exc:
            _mark_failed(session, row, error=str(exc), retryable=True)
        except (TemporalSampleImportError, _TemporalImportNeedsAttention) as exc:
            _mark_temporal_needs_attention(
                session,
                row,
                error=str(exc),
                materialized_references=dict(
                    (row.metadata_json or {}).get("asset_references") or {}
                ),
            )
        except (OSError, ValueError) as exc:
            if row.capture_mode == "temporal_sequence":
                _mark_temporal_needs_attention(
                    session,
                    row,
                    error=str(exc),
                    materialized_references=dict(
                        (row.metadata_json or {}).get("asset_references") or {}
                    ),
                )
            else:
                _mark_failed(session, row, error=str(exc), retryable=False)
        except Exception as exc:
            LOGGER.exception("Unexpected capture delivery failure for request %s.", row.id)
            _mark_failed(
                session,
                row,
                error=f"Unexpected capture delivery failure: {exc}",
                retryable=True,
            )
        return row.status


def _materialize_temporal_sequence_capture(
    settings: PlatformSettings,
    session: Session,
    row: RuntimeCaptureRequest,
    *,
    client_factory: LabelStudioClientFactory,
) -> None:
    payload = dict(row.asset_payload or {})
    capture_root = settings.storage.runtime_capture_root()
    if row.asset_uri:
        materialized = load_temporal_sample_materialization(Path(row.asset_uri))
    else:
        if payload.get("payload_type") != "temporal_sample_spool":
            raise _TerminalCaptureError(
                "Temporal sequence capture did not contain a temporal sample spool reference."
            )
        spool_uri = payload.get("spool_uri")
        if not isinstance(spool_uri, str) or not spool_uri.strip():
            raise _TerminalCaptureError("Temporal sequence capture spool URI is missing.")
        spool_path = Path(spool_uri)
        if spool_path.is_dir():
            incoming_bytes = sum(
                path.stat().st_size for path in spool_path.rglob("*") if path.is_file()
            )
            _assert_capture_storage_available(
                settings=settings,
                capture_root=capture_root,
                incoming_bytes=max(incoming_bytes, 1),
            )
        materialized = materialize_temporal_sample_spool(
            spool_path,
            capture_root=capture_root,
            request_id=row.id,
        )
    row.asset_uri = str(materialized.manifest_path)
    row.digest = materialized.manifest_sha256
    previous_source_metadata = dict(row.source_frame_metadata or {})
    row.source_frame_metadata = {
        **previous_source_metadata,
        "source": "temporal_sample_spool",
        "sequence_id": materialized.sequence_id,
        "session_id": payload.get("session_id") or previous_source_metadata.get("session_id"),
        "track_id": payload.get("track_id") or previous_source_metadata.get("track_id"),
    }
    row.metadata_json = {
        **(row.metadata_json or {}),
        "sample_status": materialized.sample_status,
        "angle_label_eligible": materialized.angle_label_eligible,
        "asset_references": materialized.asset_references,
        "ground_truth_status": (
            "technical_review_only"
            if materialized.asset_references.get("observations_ref")
            else "pending_label_studio"
        ),
        "label_studio_submission": (
            "not_requested"
            if materialized.asset_references.get("observations_ref")
            else "pending"
        ),
    }
    _make_capture_path_web_readable(
        materialized.manifest_path.parent,
        root=capture_root,
    )
    for materialized_asset in materialized.manifest_path.parent.iterdir():
        if materialized_asset.is_file():
            _chmod_best_effort(materialized_asset, 0o644)
    _clear_terminal_capture_payload(row)
    _write_capture_audit(
        session,
        row,
        event_type="runtime.capture.temporal_sample_recorded.v1",
    )
    _prune_temporal_sample_assets(
        capture_root,
        retention_days=int(
            (row.metadata_json or {}).get("retention_days")
            or settings.artifact_retention.runtime_temporal_sample_days
        ),
        preserve=materialized.manifest_path.parent,
    )
    if materialized.sample_status != "recorded":
        _finish_temporal_without_import(
            session,
            row,
            status="skipped_not_released",
            reason="Temporal sample was not released for Label Studio import.",
            materialized_references=materialized.asset_references,
        )
        return
    if not bool((row.metadata_json or {}).get("auto_import_enabled", True)):
        _finish_temporal_without_import(
            session,
            row,
            status="recorded",
            reason="Automatic temporal import is disabled by pipeline configuration.",
            materialized_references=materialized.asset_references,
        )
        return
    try:
        target, dataset = _resolve_temporal_import_target(session, row)
        if not target.auto_sync_enabled:
            _finish_temporal_without_import(
                session,
                row,
                status="recorded",
                reason="Automatic synchronization is disabled for the mapped project.",
                materialized_references=materialized.asset_references,
            )
            return
        task = build_temporal_label_studio_task(
            materialized.manifest_path,
            dataset_id=dataset.id,
            label_project_id=target.id,
            local_files_root=Path(settings.label_studio.local_files_root),
            local_files_url_prefix=settings.label_studio.local_files_url_prefix,
            include_baseline_prediction=True,
        )
    except (_TemporalImportNeedsAttention, TemporalSampleImportError) as exc:
        _mark_temporal_needs_attention(
            session,
            row,
            error=str(exc),
            materialized_references=materialized.asset_references,
        )
        return
    row.target_label_project_id = target.id
    row.label_studio_project_id = target.label_studio_project_id
    row.target_project_title = target.title
    row.dedupe_key = task.idempotency_key
    row.metadata_json = {
        **(row.metadata_json or {}),
        "dataset_id": dataset.id,
        "annotation_schema_id": task.task["meta"]["annotation_schema"],
        "temporal_import_key": task.idempotency_key,
    }
    _acquire_temporal_import_lock(session, task.idempotency_key)
    reused = _previous_temporal_import(session, row, task)
    if reused is not None:
        reused_task_id, reused_request_id = reused
        row.duplicate_of_request_id = reused_request_id
        _complete_temporal_import(
            session,
            row,
            task=task,
            task_id=reused_task_id,
            references=materialized.asset_references,
            submission="reused_internal_ledger",
        )
        return
    if _temporal_sample_not_selected(row, task):
        _finish_temporal_without_import(
            session,
            row,
            status="skipped_sampling",
            reason="Temporal sample was excluded by deterministic import sampling.",
            materialized_references=materialized.asset_references,
        )
        return
    if _temporal_import_rate_limited(session, row):
        _update_temporal_result_asset_references(
            session,
            row,
            session_id=str((row.source_frame_metadata or {}).get("session_id") or ""),
            references={
                **materialized.asset_references,
                "label_studio_import": _temporal_import_reference(row),
            },
        )
        return
    client = client_factory(
        base_url=settings.label_studio.base_url,
        token=settings.label_studio.token(),
        timeout_seconds=settings.label_studio.request_timeout_seconds,
    )
    existing_task_id = find_temporal_task_id(
        client.list_project_tasks(target.label_studio_project_id),
        idempotency_key=task.idempotency_key,
    )
    submission = "recovered_from_label_studio"
    if existing_task_id is None:
        response = client.import_tasks_with_ids(
            target.label_studio_project_id,
            [task.task],
        )
        task_ids = _temporal_import_task_ids(
            settings,
            client,
            project_id=target.label_studio_project_id,
            response=response,
        )
        if len(task_ids) > 1:
            raise LabelStudioError(
                "Label Studio returned multiple task IDs for one temporal sample."
            )
        existing_task_id = task_ids[0] if task_ids else None
        submission = "created"
    if existing_task_id is None:
        existing_task_id = find_temporal_task_id(
            client.list_project_tasks(target.label_studio_project_id),
            idempotency_key=task.idempotency_key,
        )
    if existing_task_id is None:
        raise LabelStudioError(
            "Label Studio imported the temporal task but did not expose a recoverable task ID."
        )
    _complete_temporal_import(
        session,
        row,
        task=task,
        task_id=existing_task_id,
        references=materialized.asset_references,
        submission=submission,
    )


def _temporal_import_task_ids(
    settings: PlatformSettings,
    client: LabelStudioClient,
    *,
    project_id: int,
    response: dict[str, Any],
) -> list[int]:
    task_ids = task_ids_from_import_response(response)
    import_id = response.get("import")
    if task_ids or not isinstance(import_id, int):
        return task_ids
    attempts = max(int(settings.label_studio.export_poll_attempts), 1)
    interval = max(float(settings.label_studio.export_poll_interval_seconds), 0.0)
    for attempt in range(attempts):
        status = client.get_project_import(project_id, import_id)
        task_ids = task_ids_from_import_response(status)
        if task_ids:
            return task_ids
        state = str(status.get("status") or status.get("state") or "").lower()
        if state in {"failed", "error", "cancelled", "canceled"}:
            detail = status.get("error") or status.get("errors") or state
            raise TemporalSampleImportError(
                f"Label Studio temporal import {import_id} failed: {detail}."
            )
        if state in {"completed", "complete", "finished", "success", "succeeded"}:
            return []
        if attempt + 1 < attempts and interval > 0:
            time.sleep(interval)
    raise LabelStudioError(
        f"Label Studio temporal import {import_id} did not finish within the poll limit."
    )


def _resolve_temporal_import_target(
    session: Session,
    row: RuntimeCaptureRequest,
) -> tuple[LabelProject, Dataset]:
    metadata = row.metadata_json or {}
    configured_dataset = str(metadata.get("dataset_id") or "").strip()
    annotation_schema = str(
        metadata.get("annotation_schema_id") or "stem_orientation_angle.v1"
    )
    dataset: Dataset | None = None
    candidates: list[LabelProject]
    if configured_dataset:
        dataset = session.get(Dataset, configured_dataset)
        if dataset is None:
            dataset = session.scalar(
                select(Dataset).where(Dataset.slug == configured_dataset).limit(1)
            )
        if dataset is None:
            raise _TemporalImportNeedsAttention(
                f"Configured temporal dataset does not exist: {configured_dataset}."
            )
        candidates = list(
            session.scalars(
                select(LabelProject).where(LabelProject.dataset_id == dataset.id)
            ).all()
        )
    else:
        channel = session.get(RuntimeChannel, row.channel_id)
        authoring_ids = list(channel.authoring_label_project_ids or []) if channel else []
        candidates = (
            list(
                session.scalars(
                    select(LabelProject).where(LabelProject.id.in_(authoring_ids))
                ).all()
            )
            if authoring_ids
            else []
        )
    candidates = [
        candidate
        for candidate in candidates
        if candidate.deleted_at is None
        and candidate.status
        in {
            LabelProjectStatus.DISCOVERED.value,
            LabelProjectStatus.MAPPED.value,
            LabelProjectStatus.ACTIVE.value,
        }
        and candidate.task_type == TaskType.TEMPORAL_SEQUENCE.value
        and candidate.label_schema_version == annotation_schema
    ]
    if not candidates:
        raise _TemporalImportNeedsAttention(
            "No available temporal Label Studio project mapping matches this sample's "
            f"dataset and annotation schema ({annotation_schema})."
        )
    if len(candidates) != 1:
        raise _TemporalImportNeedsAttention(
            "Temporal import target is ambiguous; configure temporal_dataset_id so exactly "
            "one mapped Label Studio project is selected."
        )
    target = candidates[0]
    if dataset is None and target.dataset_id:
        dataset = session.get(Dataset, target.dataset_id)
    if dataset is None:
        raise _TemporalImportNeedsAttention(
            "The selected temporal Label Studio project is not assigned to a dataset."
        )
    if dataset.task_type != TaskType.TEMPORAL_SEQUENCE.value:
        raise _TemporalImportNeedsAttention(
            "The selected dataset is not a temporal_sequence dataset."
        )
    if dataset.label_schema_version != annotation_schema:
        raise _TemporalImportNeedsAttention(
            "Temporal sample and dataset use different annotation schema versions."
        )
    return target, dataset


def _acquire_temporal_import_lock(session: Session, idempotency_key: str) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    session.execute(select(func.pg_advisory_xact_lock(lock_key)))


def _previous_temporal_import(
    session: Session,
    row: RuntimeCaptureRequest,
    task: TemporalLabelStudioTask,
) -> tuple[int, str] | None:
    previous = session.scalars(
        select(RuntimeCaptureRequest)
        .where(
            RuntimeCaptureRequest.id != row.id,
            RuntimeCaptureRequest.capture_mode == "temporal_sequence",
            RuntimeCaptureRequest.target_label_project_id == task.label_project_id,
            RuntimeCaptureRequest.dedupe_key == task.idempotency_key,
            RuntimeCaptureRequest.status == "delivered",
        )
        .order_by(RuntimeCaptureRequest.delivered_at.asc())
    ).all()
    task_ids = {
        int(task_id)
        for previous_row in previous
        for task_id in (previous_row.label_studio_task_ids or [])
        if isinstance(task_id, int)
    }
    if len(task_ids) > 1:
        raise _TemporalImportNeedsAttention(
            "The internal capture ledger contains conflicting Label Studio task IDs for "
            "one temporal import key."
        )
    if not task_ids:
        return None
    task_id = next(iter(task_ids))
    source = next(
        previous_row
        for previous_row in previous
        if task_id in (previous_row.label_studio_task_ids or [])
    )
    return task_id, source.id


def _temporal_sample_not_selected(
    row: RuntimeCaptureRequest,
    task: TemporalLabelStudioTask,
) -> bool:
    raw = (row.metadata_json or {}).get("import_sample_every_n", 1)
    every_n = max(int(raw) if isinstance(raw, int | float | str) else 1, 1)
    ordinal = int(hashlib.sha256(task.idempotency_key.encode("utf-8")).hexdigest(), 16)
    return ordinal % every_n != 0


def _temporal_import_rate_limited(
    session: Session,
    row: RuntimeCaptureRequest,
) -> bool:
    raw = (row.metadata_json or {}).get("import_maximum_per_hour", 120)
    maximum = int(raw) if isinstance(raw, int | float | str) else 120
    if maximum <= 0:
        return False
    now = utc_now()
    window_start = now - timedelta(hours=1)
    delivered = session.scalars(
        select(RuntimeCaptureRequest.delivered_at).where(
            RuntimeCaptureRequest.id != row.id,
            RuntimeCaptureRequest.capture_mode == "temporal_sequence",
            RuntimeCaptureRequest.target_label_project_id == row.target_label_project_id,
            RuntimeCaptureRequest.status == "delivered",
            RuntimeCaptureRequest.delivered_at >= window_start,
        )
    ).all()
    delivered_at = sorted(value for value in delivered if value is not None)
    if len(delivered_at) < maximum:
        return False
    resume_at = delivered_at[0] + timedelta(hours=1, seconds=1)
    row.status = "rate_limited"
    row.rate_limited_until = resume_at
    row.next_attempt_at = resume_at
    row.last_error = (
        f"Temporal import is rate limited to {maximum} samples per hour for this project."
    )
    row.metadata_json = {
        **(row.metadata_json or {}),
        "delivery_status": row.status,
        "label_studio_submission": "rate_limited",
    }
    _write_capture_audit(session, row, event_type="runtime.capture.rate_limited.v1")
    return True


def _complete_temporal_import(
    session: Session,
    row: RuntimeCaptureRequest,
    *,
    task: TemporalLabelStudioTask,
    task_id: int,
    references: dict[str, Any],
    submission: str,
) -> None:
    now = utc_now()
    row.status = "delivered"
    row.submitted_at = now
    row.delivered_at = now
    row.next_attempt_at = None
    row.rate_limited_until = None
    row.last_error = None
    row.label_studio_task_ids = [task_id]
    row.metadata_json = {
        **(row.metadata_json or {}),
        "delivery_status": row.status,
        "label_studio_submission": submission,
        "label_studio_task_id": task_id,
        "temporal_import_key": task.idempotency_key,
        "primary_asset_sha256": task.primary_asset_sha256,
        "ground_truth_status": "pending_human_annotation",
        "baseline_prediction_is_ground_truth": False,
    }
    import_reference = _temporal_import_reference(row)
    _update_temporal_result_asset_references(
        session,
        row,
        session_id=str((row.source_frame_metadata or {}).get("session_id") or ""),
        references={**references, "label_studio_import": import_reference},
    )
    _write_capture_audit(
        session,
        row,
        event_type="runtime.capture.temporal_sample_imported.v1",
    )


def _finish_temporal_without_import(
    session: Session,
    row: RuntimeCaptureRequest,
    *,
    status: str,
    reason: str,
    materialized_references: dict[str, Any],
) -> None:
    now = utc_now()
    row.status = status
    row.delivered_at = now
    row.next_attempt_at = None
    row.last_error = reason
    row.metadata_json = {
        **(row.metadata_json or {}),
        "delivery_status": status,
        "label_studio_submission": status,
    }
    _update_temporal_result_asset_references(
        session,
        row,
        session_id=str((row.source_frame_metadata or {}).get("session_id") or ""),
        references={
            **materialized_references,
            "label_studio_import": _temporal_import_reference(row),
        },
    )
    _write_capture_audit(
        session,
        row,
        event_type=f"runtime.capture.temporal_sample_{status}.v1",
    )


def _mark_temporal_needs_attention(
    session: Session,
    row: RuntimeCaptureRequest,
    *,
    error: str,
    materialized_references: dict[str, Any],
) -> None:
    row.status = "needs_attention"
    row.next_attempt_at = None
    row.last_error = error
    row.metadata_json = {
        **(row.metadata_json or {}),
        "delivery_status": row.status,
        "label_studio_submission": "needs_attention",
    }
    _update_temporal_result_asset_references(
        session,
        row,
        session_id=str((row.source_frame_metadata or {}).get("session_id") or ""),
        references={
            **materialized_references,
            "label_studio_import": _temporal_import_reference(row),
        },
    )
    _write_capture_audit(
        session,
        row,
        event_type="runtime.capture.temporal_sample_needs_attention.v1",
    )


def _temporal_import_reference(row: RuntimeCaptureRequest) -> dict[str, Any]:
    return {
        "status": row.status,
        "dataset_id": (row.metadata_json or {}).get("dataset_id"),
        "label_project_id": (
            row.target_label_project_id
            if row.target_label_project_id != "temporal_sequence_dataset"
            else None
        ),
        "label_studio_project_id": row.label_studio_project_id,
        "label_studio_task_ids": list(row.label_studio_task_ids or []),
        "idempotency_key": (row.metadata_json or {}).get("temporal_import_key"),
        "ground_truth_status": (row.metadata_json or {}).get("ground_truth_status"),
        "last_error": row.last_error,
    }


def _update_temporal_result_asset_references(
    session: Session,
    row: RuntimeCaptureRequest,
    *,
    session_id: str,
    references: dict[str, Any],
) -> None:
    if not row.result_record_id or not session_id:
        return
    result_record = session.get(ResultRecord, row.result_record_id)
    if result_record is None:
        return
    result_payload = dict(result_record.result or {})
    result_payload["persisted_results"] = _replace_temporal_result_references(
        result_payload.get("persisted_results"),
        session_id=session_id,
        references=references,
    )
    result_record.result = result_payload
    result_record.derived_outputs = _replace_temporal_result_references(
        result_record.derived_outputs,
        session_id=session_id,
        references=references,
    )
    result_record.trigger_response = _replace_temporal_orientation_references(
        result_record.trigger_response,
        session_id=session_id,
        references=references,
    )


def _replace_temporal_result_references(
    values: object,
    *,
    session_id: str,
    references: dict[str, Any],
) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    for raw_entry in values if isinstance(values, list) else []:
        entry = dict(raw_entry) if isinstance(raw_entry, dict) else {}
        payload = dict(entry.get("payload") or {})
        if isinstance(payload.get("result"), dict):
            payload["result"] = _replace_temporal_orientation_references(
                payload["result"],
                session_id=session_id,
                references=references,
            )
        if isinstance(payload.get("orientation_result"), dict):
            payload["orientation_result"] = _replace_temporal_orientation_references(
                payload["orientation_result"],
                session_id=session_id,
                references=references,
            )
        entry["payload"] = payload
        updated.append(entry)
    return updated


def _replace_temporal_orientation_references(
    value: object,
    *,
    session_id: str,
    references: dict[str, Any],
) -> dict[str, Any]:
    result = dict(value) if isinstance(value, dict) else {}
    if result.get("session_id") == session_id:
        result["asset_references"] = dict(references)
    return result


def _prune_temporal_sample_assets(
    capture_root: Path,
    *,
    retention_days: int,
    preserve: Path,
) -> None:
    root = (capture_root / "temporal-samples").resolve()
    if not root.is_dir():
        return
    cutoff = utc_now().timestamp() - max(int(retention_days), 1) * 86_400
    preserve = preserve.resolve()
    for channel_root in root.iterdir():
        if not channel_root.is_dir():
            continue
        for sample_root in channel_root.iterdir():
            if not sample_root.is_dir():
                continue
            resolved = sample_root.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            if resolved == preserve or sample_root.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(sample_root)


def _validate_target(session: Session, row: RuntimeCaptureRequest) -> LabelProject:
    target = session.get(LabelProject, row.target_label_project_id)
    if target is None or target.deleted_at is not None:
        raise _TerminalCaptureError("The configured Label Studio capture project no longer exists.")
    if target.status not in {
        LabelProjectStatus.DISCOVERED.value,
        LabelProjectStatus.MAPPED.value,
        LabelProjectStatus.ACTIVE.value,
    }:
        raise _TerminalCaptureError(
            f"The configured Label Studio project is not available for capture: {target.status}."
        )
    row.label_studio_project_id = target.label_studio_project_id
    row.target_project_title = target.title
    return target


def _rate_limited(
    settings: PlatformSettings,
    session: Session,
    row: RuntimeCaptureRequest,
) -> bool:
    limit = _rate_limit_per_minute(settings, row)
    if limit <= 0:
        return False
    window_start = utc_now() - timedelta(minutes=1)
    count = int(
        session.scalar(
            select(func.count(RuntimeCaptureRequest.id)).where(
                RuntimeCaptureRequest.channel_id == row.channel_id,
                RuntimeCaptureRequest.target_label_project_id == row.target_label_project_id,
                RuntimeCaptureRequest.capture_rule_id == row.capture_rule_id,
                RuntimeCaptureRequest.status == "delivered",
                RuntimeCaptureRequest.delivered_at >= window_start,
            )
        )
        or 0
    )
    if count < limit:
        return False
    delay_seconds = max(int(60 / max(limit, 1)), 1)
    row.status = "rate_limited"
    row.rate_limited_until = utc_now() + timedelta(seconds=delay_seconds)
    row.next_attempt_at = row.rate_limited_until
    row.last_error = (
        f"Capture delivery is rate limited to {limit} per minute for this rule/project."
    )
    _write_capture_audit(session, row, event_type="runtime.capture.rate_limited.v1")
    return True


def _materialize_asset(
    settings: PlatformSettings,
    session: Session,
    row: RuntimeCaptureRequest,
) -> StoredCaptureAsset:
    frame, metadata, runtime_execution_id = _resolve_frame(settings, session, row)
    data = frame.image_bytes()
    if len(data) > settings.runtime.capture_max_image_bytes:
        raise PlatformError(
            code="runtime_capture_image_too_large",
            message="The capture image is too large for Label Studio delivery.",
            detail=(
                f"The image is {len(data)} bytes and the configured limit is "
                f"{settings.runtime.capture_max_image_bytes} bytes."
            ),
            status_code=413,
        )
    inspection = inspect_image_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    day = _timestamp_slug(utc_now())[:8]
    capture_root = settings.storage.runtime_capture_root()
    _assert_capture_storage_available(
        settings=settings,
        capture_root=capture_root,
        incoming_bytes=len(data),
    )
    root = capture_root / row.channel_id / row.target_label_project_id / row.source_type / day
    root.mkdir(parents=True, exist_ok=True)
    _make_capture_path_web_readable(root, root=capture_root)
    filename = f"{_timestamp_slug(utc_now())}_{digest[:16]}{inspection.extension}"
    final_path = root / filename
    temp_path = root / f".{filename}.tmp"
    temp_path.write_bytes(data)
    _chmod_best_effort(temp_path, 0o644)
    temp_path.replace(final_path)
    _adjust_cached_capture_storage_usage(capture_root, len(data))
    _chmod_best_effort(final_path, 0o644)
    row.asset_uri = str(final_path)
    row.digest = digest
    row.roi = row.roi or _roi_metadata(row.asset_payload or {})
    return StoredCaptureAsset(
        path=final_path,
        uri=str(final_path),
        digest=digest,
        mime_type=inspection.mime_type,
        width=inspection.width,
        height=inspection.height,
        source_frame_metadata=metadata,
        runtime_execution_id=runtime_execution_id,
    )


def _resolve_frame(
    settings: PlatformSettings,
    session: Session,
    row: RuntimeCaptureRequest,
) -> tuple[FramePayload, dict[str, Any], str | None]:
    if row.source_type == "pipeline":
        return _frame_from_pipeline_payload(row)
    capture_source = str(
        (row.metadata_json or {}).get("capture_source") or "LIVE_VIEW_FRAME"
    )
    expected_view_profile_id = (
        str((row.metadata_json or {}).get("view_profile_id"))
        if (row.metadata_json or {}).get("view_profile_id")
        else None
    )
    require_eligibility = bool(
        (row.metadata_json or {}).get("require_eligibility", False)
    )
    live_frame = _latest_live_frame(
        settings,
        session,
        row.channel_id,
        require_pipeline_image_output=capture_source == "PIPELINE_IMAGE_OUTPUT",
        expected_view_profile_id=expected_view_profile_id,
        require_eligibility=require_eligibility,
    )
    if live_frame is not None:
        return live_frame
    if capture_source == "PIPELINE_IMAGE_OUTPUT":
        raise _TerminalCaptureError(
            "No current canonical pipeline image output with the requested view profile is "
            "available. The capture was stopped fail closed; no full camera frame was used."
        )
    result_frame = _latest_result_frame(session, row.channel_id)
    if result_frame is not None:
        return result_frame
    source_frame = _source_file_frame(session, row.channel_id)
    if source_frame is not None:
        return source_frame
    raise _TerminalCaptureError(
        "No current live frame is available for capture. Start the channel runtime "
        "or configure a file-test source for development."
    )


def _frame_from_pipeline_payload(
    row: RuntimeCaptureRequest,
) -> tuple[FramePayload, dict[str, Any], str | None]:
    payload = dict(row.asset_payload or {})
    if payload.get("payload_type") == "frame":
        frame = FramePayload.model_validate(payload)
        return (
            frame,
            {
                **(row.source_frame_metadata or {}),
                "source": "pipeline_full_frame",
                **_frame_context_metadata(frame),
            },
            row.source_frame_metadata.get("runtime_execution_id"),
        )
    if payload.get("frame"):
        roi = RoiPayload.model_validate(payload)
        return (
            roi.frame,
            {
                **(row.source_frame_metadata or {}),
                "source": "pipeline_roi",
                "roi_key": roi.roi_key,
                "roi_label": roi.label,
                "roi_box": roi.box.model_dump(mode="json"),
                "source_region_key": roi.source_region_key,
                **_frame_context_metadata(roi.frame),
            },
            row.source_frame_metadata.get("runtime_execution_id"),
        )
    raise _TerminalCaptureError("The pipeline capture intent did not include a frame or ROI asset.")


def _latest_live_frame(
    settings: PlatformSettings,
    session: Session,
    channel_id: str,
    *,
    require_pipeline_image_output: bool = False,
    expected_view_profile_id: str | None = None,
    require_eligibility: bool = False,
) -> tuple[FramePayload, dict[str, Any], str | None] | None:
    try:
        state = get_runtime_live_view_state(session, settings, channel_id=channel_id)
    except ValueError:
        return None
    if not state.available or state.stale or state.frame is None or not state.frame.image_base64:
        return None
    frame_metadata = dict(state.frame.metadata or {})
    actual_view_profile_id = frame_metadata.get("view_profile_id")
    eligibility = next(
        (
            primitive
            for primitive in (getattr(state, "overlay", None) or [])
            if primitive.layer == "stem_capture_eligibility"
        ),
        None,
    )
    eligibility_metadata = dict(getattr(eligibility, "metadata", {}) or {})
    eligibility_status = str(eligibility_metadata.get("status") or "UNAVAILABLE")
    if require_pipeline_image_output:
        if not frame_metadata.get("canonical_model_view") or not actual_view_profile_id:
            return None
        if (
            expected_view_profile_id is not None
            and actual_view_profile_id != expected_view_profile_id
        ):
            return None
        if require_eligibility and eligibility_status != "ACCEPTED":
            return None
    image_context = _runtime_image_context(state.frame.image_context)
    frame = FramePayload(
        image_base64=state.frame.image_base64,
        mime_type=state.frame.mime_type,
        width=state.frame.width,
        height=state.frame.height,
        source_uri=state.frame.metadata.get("source_uri") if state.frame.metadata else None,
        timestamp=state.frame.source_timestamp or state.frame.captured_at,
        image_context=image_context,
        metadata=frame_metadata,
    )
    return (
        frame,
        {
            "source": (
                "pipeline_image_output"
                if require_pipeline_image_output
                else "latest_live_view_frame"
            ),
            "capture_source": (
                "PIPELINE_IMAGE_OUTPUT"
                if require_pipeline_image_output
                else "LIVE_VIEW_FRAME"
            ),
            "runtime_execution_id": state.runtime_instance_id,
            "sequence": state.frame.sequence,
            "source_timestamp": state.frame.source_timestamp,
            "captured_at": state.frame.captured_at,
            "width": state.frame.width,
            "height": state.frame.height,
            "eligibility_status": eligibility_status,
            "eligibility_verified": eligibility_status == "ACCEPTED",
            "eligibility_reason_codes": eligibility_metadata.get("reason_codes") or [],
            **_frame_context_metadata(frame),
        },
        state.runtime_instance_id,
    )


def _latest_result_frame(
    session: Session,
    channel_id: str,
) -> tuple[FramePayload, dict[str, Any], str | None] | None:
    rows = session.scalars(
        select(ResultRecord)
        .where(ResultRecord.channel_id == channel_id)
        .order_by(ResultRecord.completed_at.desc())
        .limit(10)
    ).all()
    for result in rows:
        payload = result.result or {}
        node_outputs = payload.get("node_outputs") if isinstance(payload, dict) else None
        if not isinstance(node_outputs, dict):
            continue
        for node_key, outputs in node_outputs.items():
            if not isinstance(outputs, dict):
                continue
            for output_port, candidate in outputs.items():
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("payload_type") != "frame" or not candidate.get("image_base64"):
                    continue
                frame = FramePayload.model_validate(candidate)
                return (
                    frame,
                    {
                        "source": "latest_runtime_result",
                        "result_record_id": result.id,
                        "runtime_execution_id": result.runtime_execution_id,
                        "node_key": str(node_key),
                        "output_port": str(output_port),
                        "source_timestamp": _iso(result.source_timestamp),
                        "completed_at": _iso(result.completed_at),
                        "frame_timestamp": frame.timestamp,
                        "source_uri": frame.source_uri,
                        "width": frame.width,
                        "height": frame.height,
                        **_frame_context_metadata(frame),
                    },
                    result.runtime_execution_id,
                )
    return None


def _runtime_image_context(value: object) -> RuntimeImageContext | None:
    if isinstance(value, RuntimeImageContext):
        return value
    if not isinstance(value, dict) or not value:
        return None
    try:
        return RuntimeImageContext.model_validate(value)
    except ValueError:
        return None


def _frame_context_metadata(frame: FramePayload) -> dict[str, Any]:
    image_context = frame.image_context
    metadata: dict[str, Any] = {
        **{
            key: frame.metadata.get(key)
            for key in (
                "view_profile_id",
                "view_profile_checksum",
                "view_profile_schema_version",
                "image_view_preprocessing_version",
                "undistort_preprocessing_version",
                "color_space",
                "camera_calibration_id",
                "process_calibration_id",
                "process_calibration_hash",
                "source_frame_context_id",
                "source_frame_timestamp",
            )
            if frame.metadata.get(key) is not None
        },
    }
    if image_context is None:
        return metadata
    context_payload = image_context.model_dump(mode="json")
    metadata["image_context"] = context_payload
    transform = image_context.transform
    if transform.transform_type != "crop":
        return metadata
    parameters = transform.parameters
    crop = {
        key: parameters[key]
        for key in ("x", "y", "width", "height", "source_width", "source_height")
        if key in parameters
    }
    if crop:
        metadata["crop"] = crop
    return metadata


def _source_file_frame(
    session: Session,
    channel_id: str,
) -> tuple[FramePayload, dict[str, Any], str | None] | None:
    channel = session.get(RuntimeChannel, channel_id)
    source = (
        session.get(RuntimeSource, channel.source_id)
        if channel is not None and channel.source_id
        else None
    )
    source_config = dict(source.config or {}) if source is not None else {}
    path_value = source_config.get("path")
    if not path_value:
        return None
    path = Path(str(path_value))
    if not path.exists() or not path.is_file():
        raise _TerminalCaptureError("The configured file-test source is not available for capture.")
    data = path.read_bytes()
    inspection = inspect_image_bytes(data)
    frame = FramePayload.from_bytes(
        data,
        mime_type=inspection.mime_type,
        width=inspection.width,
        height=inspection.height,
        source_uri=str(path),
    )
    return (
        frame,
        {
            "source": "channel_source_file",
            "source_type": source.source_type if source is not None else None,
            "source_uri": str(path),
            "width": inspection.width,
            "height": inspection.height,
        },
        None,
    )


def _duplicate_suppressed(
    settings: PlatformSettings,
    session: Session,
    row: RuntimeCaptureRequest,
    asset: StoredCaptureAsset,
) -> bool:
    duplicate_policy = str((row.metadata_json or {}).get("duplicate_policy") or "")
    if duplicate_policy in {"keep", "manual_captures_are_kept"}:
        return False
    dedupe_key = _dedupe_key(row, asset.digest)
    duplicate = session.scalar(
        select(RuntimeCaptureRequest)
        .where(
            RuntimeCaptureRequest.id != row.id,
            RuntimeCaptureRequest.dedupe_key == dedupe_key,
            RuntimeCaptureRequest.status.in_({"delivered", "predicted"}),
        )
        .order_by(RuntimeCaptureRequest.created_at.asc())
        .limit(1)
    )
    row.dedupe_key = dedupe_key
    if duplicate is None:
        return False
    _remove_redundant_capture_asset(
        asset=asset,
        capture_root=settings.storage.runtime_capture_root(),
    )
    row.status = "skipped_duplicate"
    row.duplicate_of_request_id = duplicate.id
    row.asset_uri = duplicate.asset_uri
    row.digest = asset.digest
    _clear_terminal_capture_payload(row)
    row.last_error = f"Duplicate capture suppressed; original request is {duplicate.id}."
    update_manual_capture_from_delivery(
        session,
        row,
        status="failed",
        asset_uri=duplicate.asset_uri,
        digest=asset.digest,
        source_frame_metadata=asset.source_frame_metadata,
        runtime_execution_id=asset.runtime_execution_id,
        error_summary=row.last_error,
    )
    _write_capture_audit(session, row, event_type="runtime.capture.skipped_duplicate.v1")
    return True


def _assert_capture_storage_available(
    *,
    settings: PlatformSettings,
    capture_root: Path,
    incoming_bytes: int,
) -> None:
    capture_root.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(capture_root)
    minimum_free = int(settings.runtime.capture_min_free_bytes)
    if disk.free - incoming_bytes < minimum_free:
        raise PlatformError(
            code="runtime_capture_disk_reserve_reached",
            message="Runtime capture was stopped to preserve free disk space.",
            detail=(
                f"Available bytes: {disk.free}; required reserve: {minimum_free}; "
                f"incoming capture bytes: {incoming_bytes}."
            ),
            status_code=507,
            retryable=False,
            metadata={
                "capture_root": str(capture_root),
                "free_bytes": disk.free,
                "minimum_free_bytes": minimum_free,
            },
        )

    used_bytes = _capture_storage_used_bytes(
        capture_root,
        cache_seconds=int(settings.runtime.capture_storage_usage_cache_seconds),
    )
    maximum_bytes = int(settings.runtime.capture_storage_max_bytes)
    if used_bytes + incoming_bytes > maximum_bytes:
        raise PlatformError(
            code="runtime_capture_storage_quota_reached",
            message="Runtime capture storage quota has been reached.",
            detail=(
                f"Capture storage uses approximately {used_bytes} bytes; "
                f"configured limit: {maximum_bytes} bytes."
            ),
            status_code=507,
            retryable=False,
            metadata={
                "capture_root": str(capture_root),
                "used_bytes": used_bytes,
                "maximum_bytes": maximum_bytes,
            },
        )


def _capture_storage_used_bytes(root: Path, *, cache_seconds: int) -> int:
    cache_key = str(root.resolve(strict=False))
    now = time.monotonic()
    with _CAPTURE_STORAGE_CACHE_LOCK:
        cached = _CAPTURE_STORAGE_USAGE_CACHE.get(cache_key)
        if cached is not None and now - cached[0] <= cache_seconds:
            return cached[1]

    total = _directory_size_bytes(root)
    with _CAPTURE_STORAGE_CACHE_LOCK:
        _CAPTURE_STORAGE_USAGE_CACHE[cache_key] = (time.monotonic(), total)
    return total


def _directory_size_bytes(root: Path) -> int:
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            LOGGER.warning("Could not scan capture storage directory %s.", directory, exc_info=True)
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                LOGGER.debug("Could not inspect capture storage entry %s.", entry.path, exc_info=True)
    return total


def _adjust_cached_capture_storage_usage(root: Path, delta_bytes: int) -> None:
    cache_key = str(root.resolve(strict=False))
    with _CAPTURE_STORAGE_CACHE_LOCK:
        cached = _CAPTURE_STORAGE_USAGE_CACHE.get(cache_key)
        if cached is None:
            return
        _CAPTURE_STORAGE_USAGE_CACHE[cache_key] = (
            time.monotonic(),
            max(cached[1] + delta_bytes, 0),
        )


def _remove_redundant_capture_asset(*, asset: StoredCaptureAsset, capture_root: Path) -> None:
    try:
        asset_path = asset.path.resolve(strict=False)
        asset_path.relative_to(capture_root.resolve(strict=False))
    except (OSError, ValueError):
        LOGGER.warning("Refusing to remove duplicate capture outside %s: %s", capture_root, asset.path)
        return
    try:
        size_bytes = asset_path.stat().st_size
    except OSError:
        size_bytes = 0
    try:
        asset_path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("Could not remove redundant duplicate capture %s.", asset_path, exc_info=True)
        return
    _adjust_cached_capture_storage_usage(capture_root, -size_bytes)


def _submit_to_label_studio(
    *,
    settings: PlatformSettings,
    row: RuntimeCaptureRequest,
    target: LabelProject,
    asset: StoredCaptureAsset,
    client_factory: LabelStudioClientFactory,
) -> LabelStudioSubmission:
    image_ref = _label_studio_image_ref(settings, asset=asset)
    task = {
        "data": {"image": image_ref},
        "meta": {
            "source": "runtime_capture",
            "capture_source_type": row.source_type,
            "capture_mode": row.capture_mode,
            "channel_id": row.channel_id,
            "capture_rule_id": row.capture_rule_id,
            "runtime_capture_request_id": row.id,
            "result_record_id": row.result_record_id,
            "manual_capture_session_id": row.manual_capture_session_id,
            "manual_capture_request_id": row.manual_capture_request_id,
            "target_project_title": target.title,
            "label_studio_project_id": target.label_studio_project_id,
            "asset_uri": asset.uri,
            "digest": asset.digest,
            "roi": row.roi or {},
            "source_frame": asset.source_frame_metadata,
            "metadata": row.metadata_json or {},
        },
    }
    client = client_factory(
        base_url=settings.label_studio.base_url,
        token=settings.label_studio.token(),
        timeout_seconds=settings.label_studio.request_timeout_seconds,
    )
    prediction_status = "disabled"
    prediction_error: str | None = None
    prediction_model_version: str | None = None
    prediction_backend_url = getattr(target, "prediction_backend_url", None)
    if _capture_predictions_requested(row):
        if getattr(target, "prediction_backend_enabled", False) and prediction_backend_url:
            try:
                prediction_payload = client.predict_with_ml_backend(
                    _prediction_request_url(prediction_backend_url),
                    [_prediction_task(task, asset=asset)],
                )
                predictions = _predictions_from_payload(prediction_payload)
                if predictions:
                    task["predictions"] = predictions
                    prediction_status = "predicted"
                    model_version = prediction_payload.get("model_version")
                    prediction_model_version = str(model_version) if model_version else None
                else:
                    prediction_status = "empty"
            except LabelStudioError as exc:
                prediction_status = "failed"
                prediction_error = str(exc)
        else:
            prediction_status = "not_configured"
    response = client.import_tasks(target.label_studio_project_id, [task])
    return LabelStudioSubmission(
        task_ids=_extract_task_ids(response),
        predicted=prediction_status == "predicted",
        prediction_status=prediction_status,
        prediction_error=prediction_error,
        prediction_backend_url=prediction_backend_url,
        prediction_model_version=prediction_model_version,
    )


def _capture_predictions_requested(row: RuntimeCaptureRequest) -> bool:
    metadata = row.metadata_json or {}
    value = metadata.get("request_predictions")
    return True if value is None else bool(value)


def _prediction_request_url(backend_url: str) -> str:
    normalized = backend_url.rstrip("/")
    if normalized.endswith("/predict"):
        return normalized
    return f"{normalized}/predict"


def _predictions_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("predictions")
    if raw is None:
        raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _prediction_task(task: dict[str, Any], *, asset: StoredCaptureAsset) -> dict[str, Any]:
    raw_data = task.get("data")
    data = raw_data if isinstance(raw_data, dict) else {}
    return {
        **task,
        "data": {
            **data,
            "image": _asset_data_uri(asset),
        },
    }


def _asset_data_uri(asset: StoredCaptureAsset) -> str:
    data = asset.path.read_bytes()
    return f"data:{asset.mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def _mark_failed(
    session: Session,
    row: RuntimeCaptureRequest,
    *,
    error: str,
    retryable: bool,
) -> None:
    now = utc_now()
    if retryable and int(row.retry_count or 0) + 1 < int(row.max_attempts or 3):
        row.retry_count = int(row.retry_count or 0) + 1
        row.status = "failed_retryable"
        row.next_attempt_at = now + timedelta(seconds=_retry_delay_seconds(row.retry_count))
    else:
        row.status = "failed_terminal"
        row.next_attempt_at = None
        _clear_terminal_capture_payload(row)
    row.last_error = error
    row.metadata_json = {
        **(row.metadata_json or {}),
        "delivery_status": row.status,
        "failure_recorded_at": _iso(now),
    }
    update_manual_capture_from_delivery(
        session,
        row,
        status="failed",
        asset_uri=row.asset_uri,
        digest=row.digest,
        label_studio_task_ids=row.label_studio_task_ids or [],
        source_frame_metadata=row.source_frame_metadata or {},
        runtime_execution_id=(row.source_frame_metadata or {}).get("runtime_execution_id"),
        error_summary=error,
    )
    _write_capture_audit(session, row, event_type=f"runtime.capture.{row.status}.v1")


def _clear_terminal_capture_payload(row: RuntimeCaptureRequest) -> None:
    capture_intent = dict(row.capture_intent or {})
    raw_intent = capture_intent.get("intent")
    if isinstance(raw_intent, dict):
        compact_intent = {
            key: value
            for key, value in raw_intent.items()
            if key not in {"asset", "frame", "image_base64"}
        }
        capture_intent["intent"] = compact_intent
    row.asset_payload = {}
    row.capture_intent = capture_intent


def _write_capture_audit(
    session: Session,
    row: RuntimeCaptureRequest,
    *,
    event_type: str,
) -> None:
    create_audit_event(
        session,
        event_type=event_type,
        aggregate_type="runtime_capture_request",
        aggregate_id=row.id,
        source_service="capture-service",
        actor_type="service",
        actor_id="capture-service",
        correlation_id=(row.source_frame_metadata or {}).get("correlation_id"),
        causation_id=row.result_record_id or row.manual_capture_request_id,
        payload={
            "channel_id": row.channel_id,
            "capture_rule_id": row.capture_rule_id,
            "source_type": row.source_type,
            "capture_mode": row.capture_mode,
            "target_label_project_id": row.target_label_project_id,
            "label_studio_project_id": row.label_studio_project_id,
            "label_studio_task_ids": list(row.label_studio_task_ids or []),
            "status": row.status,
            "retry_count": row.retry_count,
            "asset_uri": row.asset_uri,
            "digest": row.digest,
            "duplicate_of_request_id": row.duplicate_of_request_id,
            "dataset_id": (row.metadata_json or {}).get("dataset_id"),
            "temporal_import_key": (row.metadata_json or {}).get("temporal_import_key"),
            "label_studio_submission": (row.metadata_json or {}).get(
                "label_studio_submission"
            ),
            "last_error": row.last_error,
        },
    )


def _rate_limit_per_minute(settings: PlatformSettings, row: RuntimeCaptureRequest) -> int:
    metadata = row.metadata_json or {}
    raw = metadata.get("rate_limit") if isinstance(metadata.get("rate_limit"), dict) else {}
    value = raw.get("per_minute") or raw.get("rate_per_minute")
    if isinstance(value, int):
        return value
    return settings.runtime.capture_default_rate_per_minute


def _dedupe_key(row: RuntimeCaptureRequest, digest: str) -> str:
    raw = f"{row.channel_id}|{row.capture_rule_id}|{row.target_label_project_id}|{digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _label_studio_image_ref(settings: PlatformSettings, *, asset: StoredCaptureAsset) -> str:
    if settings.runtime.label_studio_submission_mode == "api_upload":
        return _asset_data_uri(asset)
    local_root = Path(settings.label_studio.local_files_root).resolve()
    asset_path = asset.path.resolve()
    try:
        relative = asset_path.relative_to(local_root)
    except ValueError as exc:
        raise ValueError(
            "Runtime capture storage must be below label_studio.local_files_root "
            "for local-file submission."
        ) from exc
    prefix = settings.label_studio.local_files_url_prefix
    separator = "" if prefix.endswith("=") or prefix.endswith("/") else "/"
    return f"{prefix}{separator}{relative.as_posix()}"


def _extract_task_ids(response: dict[str, Any]) -> list[int]:
    for key in ("task_ids", "ids", "created"):
        raw = response.get(key)
        if isinstance(raw, list):
            return [
                int(item)
                for item in raw
                if isinstance(item, int | str) and str(item).isdigit()
            ]
    tasks = response.get("tasks")
    if isinstance(tasks, list):
        ids: list[int] = []
        for task in tasks:
            if isinstance(task, dict) and isinstance(task.get("id"), int | str):
                value = str(task["id"])
                if value.isdigit():
                    ids.append(int(value))
        return ids
    return []


def _roi_metadata(asset_payload: dict[str, Any]) -> dict[str, Any]:
    if "box" not in asset_payload:
        return {}
    return {
        "roi_key": asset_payload.get("roi_key"),
        "box": asset_payload.get("box"),
        "label": asset_payload.get("label"),
        "source_region_key": asset_payload.get("source_region_key"),
        "metadata": asset_payload.get("metadata") or {},
    }


def _retry_delay_seconds(retry_count: int) -> int:
    intervals = (5, 30, 120, 300)
    return intervals[min(max(retry_count - 1, 0), len(intervals) - 1)]


def _timestamp_slug(value: object) -> str:
    raw = _iso(value) or ""
    return re.sub(r"[^0-9A-Za-z]+", "", raw)[:20] or "capture"


def _iso(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)


class _TerminalCaptureError(RuntimeError):
    pass


class _TemporalImportNeedsAttention(RuntimeError):
    pass
