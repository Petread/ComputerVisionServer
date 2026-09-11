from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cv_contracts.runtime import RuntimeResultHistoryPolicy
from cv_core import load_platform_settings
from cv_db import (
    create_audit_event,
    get_training_job_detail,
    get_training_job_record,
    mark_stale_training_jobs,
    prepare_training_job_resume,
    prune_training_checkpoints,
    session_scope,
    update_training_job_state,
)
from cv_db.models import (
    ResultRecord,
    RuntimeCaptureRequest,
    RuntimeChannel,
    RuntimeConnectorDelivery,
    RuntimeExecution,
    StemOrientationReview,
    TrainingJob,
)
from cv_status import QueueName, TrainingJobStatus
from cv_utils import remove_path_within_root, utc_now, utc_now_iso
from redis import Redis
from rq import Queue, Retry
from sqlalchemy import delete, exists, or_, select, text, update

GPU_LOCK_KEY_PATTERNS = (
    "cv-platform:training:gpu:*",
    "training-worker:gpu:*",
)
TERMINAL_TRAINING_STATUSES = {
    TrainingJobStatus.COMPLETED.value,
    TrainingJobStatus.CANCELLED.value,
    TrainingJobStatus.FAILED.value,
    TrainingJobStatus.ROLLED_BACK_IF_NEEDED.value,
}
ACTIVE_CAPTURE_STATUSES = {
    "queued",
    "processing",
    "rate_limited",
    "failed_retryable",
}
TERMINAL_CAPTURE_STATUSES = {
    "delivered",
    "predicted",
    "failed_terminal",
    "skipped_duplicate",
}
ACTIVE_CONNECTOR_STATUSES = {
    "queued",
    "processing",
    "failed_retryable",
}
TERMINAL_CONNECTOR_STATUSES = {
    "delivered",
    "failed_terminal",
    "skipped_no_payload",
}


def backup_metadata() -> dict[str, object]:
    settings = load_platform_settings("maintenance-worker")
    backup_root = (
        settings.storage.backups_root() / f"metadata_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    backup_root.mkdir(parents=True, exist_ok=True)

    copied_paths = {
        "audit_events": _copy_tree(
            Path(settings.storage.audit_root) / "events", backup_root / "audit" / "events"
        ),
        "snapshot_manifests": _copy_snapshot_manifests(settings, backup_root),
        "serving_history": _copy_tree(
            Path(settings.storage.serving_root) / "history",
            backup_root / "serving" / "history",
        ),
        "dvc_manifests": _copy_tree(
            Path(settings.dvc.repo_root) / "manifests", backup_root / "dvc" / "manifests"
        ),
    }
    database_backup = _dump_postgres(settings, backup_root)
    manifest = {
        "created_at": utc_now_iso(),
        "backup_root": str(backup_root),
        "copied_paths": copied_paths,
        "database_backup": database_backup,
    }
    (backup_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    with session_scope(settings) as session:
        create_audit_event(
            session,
            event_type="maintenance.backup.completed.v1",
            aggregate_type="system",
            aggregate_id="backup",
            source_service="maintenance-worker",
            actor_type="system",
            actor_id="maintenance-worker",
            payload=manifest,
        )
    return {"status": "completed", "backup_root": str(backup_root), "finished_at": utc_now_iso()}


def checkpoint_pruning() -> dict[str, object]:
    settings = load_platform_settings("maintenance-worker")
    cutoff = utc_now() - timedelta(days=settings.artifact_retention.checkpoints_days)
    training_root = Path(settings.storage.training_root)
    pruned: list[str] = []
    errors: list[str] = []
    if not training_root.exists():
        return {"status": "completed", "pruned_count": 0, "finished_at": utc_now_iso()}

    with session_scope(settings) as session:
        jobs = session.scalars(
            select(TrainingJob).where(TrainingJob.status.in_(TERMINAL_TRAINING_STATUSES))
        ).all()
        stale_jobs = [
            job
            for job in jobs
            if _as_utc(job.finished_at or job.updated_at or job.created_at) < cutoff
        ]
        pruned_by_job = {
            job.id: prune_training_checkpoints(
                session,
                training_job_id=job.id,
                keep_count=settings.training.checkpoint_keep_count,
            )
            for job in stale_jobs
        }

    for job_id, paths in pruned_by_job.items():
        job_root = training_root / f"trn_{job_id}"
        for raw_path in paths:
            candidate = Path(raw_path)
            try:
                remove_path_within_root(root=job_root, target=candidate)
                pruned.append(str(candidate))
            except (OSError, ValueError) as exc:
                errors.append(f"{candidate}: {exc}")
    return {
        "status": "completed" if not errors else "completed_with_errors",
        "pruned_count": len(pruned),
        "errors": errors,
        "finished_at": utc_now_iso(),
    }


def training_workspace_retention() -> dict[str, object]:
    settings = load_platform_settings("maintenance-worker")
    cutoff = utc_now() - timedelta(days=settings.artifact_retention.training_workspace_days)
    training_root = Path(settings.storage.training_root)
    removed: list[str] = []
    errors: list[str] = []
    if not training_root.exists():
        return {"status": "completed", "removed_count": 0, "finished_at": utc_now_iso()}

    with session_scope(settings) as session:
        jobs = session.scalars(
            select(TrainingJob).where(TrainingJob.status.in_(TERMINAL_TRAINING_STATUSES))
        ).all()
        stale_job_ids = [
            job.id
            for job in jobs
            if _as_utc(job.finished_at or job.updated_at or job.created_at) < cutoff
        ]

    for job_id in stale_job_ids:
        job_root = training_root / f"trn_{job_id}"
        for directory_name in ("workspace", ".tmp"):
            candidate = job_root / directory_name
            if not candidate.exists():
                continue
            try:
                remove_path_within_root(root=job_root, target=candidate)
                removed.append(str(candidate))
            except (OSError, ValueError) as exc:
                errors.append(f"{candidate}: {exc}")
    return {
        "status": "completed" if not errors else "completed_with_errors",
        "removed_count": len(removed),
        "errors": errors,
        "finished_at": utc_now_iso(),
    }


def artifact_retention() -> dict[str, object]:
    settings = load_platform_settings("maintenance-worker")
    removals = []
    removals.extend(
        _prune_by_age(
            root=Path(settings.storage.logs_root),
            cutoff=utc_now() - timedelta(days=settings.log_retention.operational_days),
        )
    )
    removals.extend(
        _prune_by_age(
            root=Path(settings.storage.audit_root) / "events",
            cutoff=utc_now() - timedelta(days=settings.log_retention.audit_days),
        )
    )
    removals.extend(
        _prune_by_age(
            root=settings.storage.labelstudio_exports_root(),
            cutoff=utc_now() - timedelta(days=settings.artifact_retention.exports_days),
        )
    )
    removals.extend(
        _prune_by_age(
            root=settings.storage.batch_root(),
            cutoff=utc_now() - timedelta(days=settings.artifact_retention.batch_outputs_days),
        )
    )
    removals.extend(
        _prune_by_age(
            root=settings.storage.quarantine_root(),
            cutoff=utc_now() - timedelta(days=settings.artifact_retention.quarantine_days),
        )
    )
    removals.extend(
        _prune_backup_sets_by_age(
            root=settings.storage.backups_root(),
            cutoff=utc_now() - timedelta(days=settings.backup.metadata_retention_days),
        )
    )
    return {"status": "completed", "removed_count": len(removals), "finished_at": utc_now_iso()}


def runtime_result_retention() -> dict[str, object]:
    """Prune operational rows and bound all retained history, including shadow results."""
    settings = load_platform_settings("maintenance-worker")
    now = utc_now()
    operational_cutoff = now - timedelta(
        minutes=settings.artifact_retention.runtime_operational_result_minutes
    )
    batch_size = settings.artifact_retention.runtime_result_prune_batch_size
    deleted_result_count = 0

    with session_scope(settings) as session:
        result_table_size_bytes = _result_table_size_bytes(session)
        manual_cleanup_required = result_table_size_bytes > 50 * 1024**3
        channels = list(session.scalars(select(RuntimeChannel)).all())
        remaining = 0 if manual_cleanup_required else batch_size
        for channel in channels:
            if remaining <= 0:
                break
            policy = _history_policy(channel)
            retention_cutoff = now - timedelta(hours=policy.retention_hours)
            stale_filter = or_(
                (
                    ResultRecord.history_retained.is_(False)
                    & (ResultRecord.completed_at < operational_cutoff)
                ),
                (
                    ResultRecord.history_retained.is_(True)
                    & (ResultRecord.completed_at < retention_cutoff)
                ),
            )
            stale_ids = list(
                session.scalars(
                    select(ResultRecord.id)
                    .where(
                        ResultRecord.channel_id == channel.id,
                        stale_filter,
                        _result_has_no_active_dependents(),
                    )
                    .order_by(ResultRecord.completed_at.asc(), ResultRecord.id.asc())
                    .limit(remaining)
                ).all()
            )
            deleted = _delete_result_ids(session, stale_ids)
            deleted_result_count += deleted
            remaining -= deleted

            if remaining <= 0:
                continue
            excess_ids = list(
                session.scalars(
                    select(ResultRecord.id)
                    .where(
                        ResultRecord.channel_id == channel.id,
                        ResultRecord.history_retained.is_(True),
                        _result_has_no_active_dependents(),
                    )
                    .order_by(ResultRecord.completed_at.desc(), ResultRecord.id.desc())
                    .offset(policy.max_records)
                    .limit(remaining)
                ).all()
            )
            deleted = _delete_result_ids(session, excess_ids)
            deleted_result_count += deleted
            remaining -= deleted

        if remaining > 0:
            channel_ids = [channel.id for channel in channels]
            orphan_filters = [ResultRecord.completed_at < operational_cutoff]
            if channel_ids:
                orphan_filters.append(ResultRecord.channel_id.not_in(channel_ids))
            orphan_ids = list(
                session.scalars(
                    select(ResultRecord.id)
                    .where(
                        *orphan_filters,
                        _result_has_no_active_dependents(),
                    )
                    .order_by(ResultRecord.completed_at.asc(), ResultRecord.id.asc())
                    .limit(remaining)
                ).all()
            )
            deleted_result_count += _delete_result_ids(session, orphan_ids)

        deleted_delivery_count = _delete_terminal_metadata(
            session,
            model=RuntimeConnectorDelivery,
            terminal_statuses=TERMINAL_CONNECTOR_STATUSES,
            cutoff=now
            - timedelta(days=settings.artifact_retention.runtime_delivery_metadata_days),
            limit=batch_size,
        )
        deleted_capture_count = _delete_terminal_metadata(
            session,
            model=RuntimeCaptureRequest,
            terminal_statuses=TERMINAL_CAPTURE_STATUSES,
            cutoff=now
            - timedelta(days=settings.artifact_retention.runtime_capture_metadata_days),
            limit=batch_size,
        )

    return {
        "status": "manual_cleanup_required" if manual_cleanup_required else "completed",
        "deleted_result_count": deleted_result_count,
        "deleted_delivery_count": deleted_delivery_count,
        "deleted_capture_count": deleted_capture_count,
        "batch_size": batch_size,
        "result_table_size_bytes": result_table_size_bytes,
        "manual_cleanup_required": manual_cleanup_required,
        "finished_at": utc_now_iso(),
    }


def reconcile_training_jobs() -> dict[str, object]:
    settings = load_platform_settings("maintenance-worker")
    training_queue = Queue(
        QueueName.TRAINING.value, connection=Redis.from_url(settings.redis.url())
    )
    with session_scope(settings) as session:
        stale_ids = mark_stale_training_jobs(
            session,
            stale_after_seconds=settings.training.stale_after_seconds,
            source_service="maintenance-worker",
        )

    resumed_ids: list[str] = []
    restarted_ids: list[str] = []
    for training_job_id in stale_ids:
        with session_scope(settings) as session:
            detail = get_training_job_detail(session, training_job_id)
            row = get_training_job_record(session, training_job_id)
            if detail is None or row is None:
                continue
            if detail.latest_checkpoint_id and settings.training.resume_from_latest_good_checkpoint:
                prepare_training_job_resume(
                    session,
                    training_job_id=training_job_id,
                    requested_by="maintenance-worker",
                    source_service="maintenance-worker",
                    reason="Worker heartbeat went stale; resuming from the latest checkpoint.",
                )
                resumed_ids.append(training_job_id)
                job_name = "training_worker.jobs.resume_training_job"
            else:
                update_training_job_state(
                    session,
                    training_job_id=training_job_id,
                    status=TrainingJobStatus.QUEUED,
                    source_service="maintenance-worker",
                    actor_type="system",
                    actor_id="maintenance-worker",
                    reason="Worker heartbeat went stale; restarting from the initial state.",
                    last_error="Previous worker heartbeat expired before the run completed.",
                    failure_class="stale_worker_recovered",
                )
                restarted_ids.append(training_job_id)
                job_name = "training_worker.jobs.run_training_job"
        training_queue.enqueue(
            job_name,
            training_job_id=training_job_id,
            job_timeout=settings.training.job_timeout_seconds,
            retry=Retry(max=settings.training.safe_retry_limit, interval=[5, 30, 120]),
        )
    return {
        "status": "completed",
        "stale_training_job_ids": stale_ids,
        "resumed_training_job_ids": resumed_ids,
        "restarted_training_job_ids": restarted_ids,
        "finished_at": utc_now_iso(),
    }


def cleanup_stale_locks() -> dict[str, object]:
    settings = load_platform_settings("maintenance-worker")
    redis = Redis.from_url(settings.redis.url())
    removed_keys: list[str] = []
    with session_scope(settings) as session:
        for key in _iter_gpu_lock_keys(redis):
            raw_value = redis.get(key)
            if not raw_value:
                continue
            training_job_id = _training_job_id_from_gpu_lock(raw_value)
            if not training_job_id:
                redis.delete(key)
                removed_keys.append(_redis_key_to_str(key))
                continue
            job = get_training_job_record(session, training_job_id)
            if job is None or job.status in {
                TrainingJobStatus.COMPLETED.value,
                TrainingJobStatus.CANCELLED.value,
                TrainingJobStatus.FAILED.value,
                TrainingJobStatus.ROLLED_BACK_IF_NEEDED.value,
            }:
                redis.delete(key)
                removed_keys.append(_redis_key_to_str(key))
                continue
            if job.worker_heartbeat_at is None:
                continue
            age_seconds = int((utc_now() - job.worker_heartbeat_at).total_seconds())
            if age_seconds > settings.training.stale_after_seconds:
                redis.delete(key)
                removed_keys.append(_redis_key_to_str(key))
        if removed_keys:
            create_audit_event(
                session,
                event_type="maintenance.stale_locks.cleaned.v1",
                aggregate_type="system",
                aggregate_id="locks",
                source_service="maintenance-worker",
                actor_type="system",
                actor_id="maintenance-worker",
                payload={"removed_keys": removed_keys},
            )
    return {"status": "completed", "removed_lock_keys": removed_keys, "finished_at": utc_now_iso()}


def _iter_gpu_lock_keys(redis: Redis):
    seen: set[bytes | str] = set()
    for pattern in GPU_LOCK_KEY_PATTERNS:
        for key in redis.scan_iter(match=pattern):
            if key in seen:
                continue
            seen.add(key)
            yield key


def _training_job_id_from_gpu_lock(raw_value: bytes | str) -> str | None:
    payload = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else str(raw_value)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        training_job_id, _, _instance_id = payload.partition(":")
        return training_job_id or None
    if isinstance(parsed, dict):
        value = parsed.get("training_job_id")
        return str(value) if value else None
    return None


def _redis_key_to_str(key: bytes | str) -> str:
    return key.decode("utf-8") if isinstance(key, bytes) else str(key)


def _active_training_job_ids(settings) -> set[str]:
    with session_scope(settings) as session:
        rows = session.scalars(
            select(TrainingJob).where(
                TrainingJob.status.in_(
                    [
                        TrainingJobStatus.QUEUED.value,
                        TrainingJobStatus.WAITING_FOR_GPU.value,
                        TrainingJobStatus.PREPARING_DATA.value,
                        TrainingJobStatus.VALIDATING_SNAPSHOT.value,
                        TrainingJobStatus.INITIALIZING.value,
                        TrainingJobStatus.TRAINING.value,
                        TrainingJobStatus.CHECKPOINTING.value,
                        TrainingJobStatus.EVALUATING.value,
                        TrainingJobStatus.EXPORTING.value,
                        TrainingJobStatus.REGISTERING.value,
                        TrainingJobStatus.RESUMING.value,
                        TrainingJobStatus.CANCEL_REQUESTED.value,
                    ]
                )
            )
        ).all()
    return {row.id for row in rows}


def _copy_tree(source: Path, destination: Path) -> str | None:
    if not source.exists():
        return None
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return str(destination)


def _copy_snapshot_manifests(settings, backup_root: Path) -> list[str]:
    copied: list[str] = []
    destination_root = backup_root / "snapshots"
    for snapshot_root in settings.storage.snapshots_root().glob("dss_*"):
        if not snapshot_root.is_dir():
            continue
        target_dir = destination_root / snapshot_root.name
        target_dir.mkdir(parents=True, exist_ok=True)
        for relative in (
            "snapshot_manifest.json",
            "manifests/validation_report.json",
            "manifests/split_metadata.json",
        ):
            source = snapshot_root / relative
            if not source.exists():
                continue
            destination = target_dir / Path(relative).name
            destination.write_bytes(source.read_bytes())
            copied.append(str(destination))
    return copied


def _dump_postgres(settings, backup_root: Path) -> dict[str, str]:
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        return {"status": "skipped_missing_pg_dump"}
    dump_path = backup_root / "postgres" / f"{settings.database.database}.sql"
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PGPASSWORD"] = settings.database.password()
    command = [
        pg_dump,
        "--host",
        settings.database.host,
        "--port",
        str(settings.database.port),
        "--username",
        settings.database.user,
        "--dbname",
        settings.database.database,
        "--file",
        str(dump_path),
    ]
    subprocess.run(command, check=True, env=env, capture_output=True, text=True, timeout=60)
    return {"status": "completed", "path": str(dump_path)}


def _prune_by_age(*, root: Path, cutoff: datetime) -> list[str]:
    if not root.exists():
        return []
    removed: list[str] = []
    for candidate in sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if not candidate.exists():
            continue
        if datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC) >= cutoff:
            continue
        if candidate.is_dir() and any(candidate.iterdir()):
            continue
        remove_path_within_root(root=root, target=candidate)
        removed.append(str(candidate))
    return removed


def _prune_backup_sets_by_age(*, root: Path, cutoff: datetime) -> list[str]:
    if not root.exists():
        return []
    removed: list[str] = []
    for candidate in root.iterdir():
        if not candidate.name.startswith("metadata_"):
            continue
        try:
            modified_at = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if modified_at >= cutoff:
            continue
        remove_path_within_root(root=root, target=candidate)
        removed.append(str(candidate))
    return removed


def _history_policy(channel: RuntimeChannel) -> RuntimeResultHistoryPolicy:
    try:
        return RuntimeResultHistoryPolicy.model_validate(channel.result_history_policy or {})
    except ValueError:
        return RuntimeResultHistoryPolicy()


def _result_table_size_bytes(session) -> int:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return 0
    value = session.scalar(
        text("SELECT pg_total_relation_size('public.result_records'::regclass)")
    )
    return int(value or 0)


def _result_has_no_active_dependents():
    active_capture = exists().where(
        RuntimeCaptureRequest.result_record_id == ResultRecord.id,
        RuntimeCaptureRequest.status.in_(ACTIVE_CAPTURE_STATUSES),
    )
    active_delivery = exists().where(
        RuntimeConnectorDelivery.result_record_id == ResultRecord.id,
        RuntimeConnectorDelivery.status.in_(ACTIVE_CONNECTOR_STATUSES),
    )
    durable_stem_review = exists().where(
        StemOrientationReview.result_record_id == ResultRecord.id
    )
    return ~active_capture & ~active_delivery & ~durable_stem_review


def _delete_result_ids(session, result_ids: list[str]) -> int:
    if not result_ids:
        return 0
    session.execute(
        update(RuntimeExecution)
        .where(RuntimeExecution.last_result_id.in_(result_ids))
        .values(last_result_id=None)
    )
    session.execute(
        update(RuntimeCaptureRequest)
        .where(RuntimeCaptureRequest.result_record_id.in_(result_ids))
        .values(result_record_id=None)
    )
    result = session.execute(delete(ResultRecord).where(ResultRecord.id.in_(result_ids)))
    return int(result.rowcount or 0)


def _delete_terminal_metadata(
    session,
    *,
    model,
    terminal_statuses: set[str],
    cutoff: datetime,
    limit: int,
) -> int:
    row_ids = list(
        session.scalars(
            select(model.id)
            .where(
                model.status.in_(terminal_statuses),
                model.created_at < cutoff,
            )
            .order_by(model.created_at.asc(), model.id.asc())
            .limit(limit)
        ).all()
    )
    if not row_ids:
        return 0
    result = session.execute(delete(model).where(model.id.in_(row_ids)))
    return int(result.rowcount or 0)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
