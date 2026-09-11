from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cv_contracts.runtime import (
    RuntimeControlCommandSummary,
    RuntimeLogicalModelRef,
    RuntimePipelineGraph,
    RuntimeResultHistoryPolicy,
)
from cv_core import generate_prefixed_id
from cv_runtime import ExecutablePipelineGraph, RuntimeGraphBuildError, RuntimeModelBinding
from cv_runtime.payloads import RuntimeExecutionResult
from cv_status import (
    DeploymentReleaseStatus,
    HealthState,
    PromotionStatus,
    RuntimeChannelDesiredState,
    RuntimeControlCommand,
    RuntimeControlCommandStatus,
    RuntimeExecutionStatus,
    RuntimeMode,
    RuntimeNodeCategory,
    RuntimeObservedState,
    RuntimePipelineStatus,
    RuntimeResourceStatus,
    RuntimeSourceType,
    TargetPath,
    TaskType,
)
from cv_utils import utc_now, utc_now_iso
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cv_db.models import (
    DeploymentRelease,
    DeploymentTarget,
    ModelFamily,
    ModelPromotion,
    ModelVersion,
    PipelineDefinition,
    PipelineVersion,
    ResultRecord,
    RuntimeChannel,
    RuntimeControlCommandRecord,
    RuntimeExecution,
    RuntimeModelCapabilityCache,
    RuntimeNodeTypeRegistry,
    RuntimeSource,
    TriggerEndpoint,
)

_BASLER_IDENTIFIER_KEYS = (
    "camera_name",
    "camera",
    "serial_number",
    "user_defined_name",
    "device_id",
    "id_",
    "ip_address",
    "model",
)


@dataclass(frozen=True)
class LoadedRuntimePipeline:
    channel: RuntimeChannel
    source: RuntimeSource | None
    version: PipelineVersion
    definition: PipelineDefinition
    graph: RuntimePipelineGraph
    executable: ExecutablePipelineGraph


@dataclass(frozen=True)
class RuntimeModelCapabilityOption:
    deployment_release_id: str
    model_family_slug: str
    display_name: str
    target_path: TargetPath
    task_type: TaskType
    model_version_id: str
    version_label: str
    deployment_status: str
    architecture_family: str | None
    architecture_version: str | None
    architecture_size: str | None
    architecture_display_name: str | None
    contract_major: int
    active: bool
    capability: dict[str, Any]

    def to_api(self) -> dict[str, Any]:
        return {
            "deployment_release_id": self.deployment_release_id,
            "model_family_slug": self.model_family_slug,
            "display_name": self.display_name,
            "target_path": self.target_path.value,
            "task_type": self.task_type.value,
            "model_version_id": self.model_version_id,
            "version_label": self.version_label,
            "deployment_status": self.deployment_status,
            "architecture_family": self.architecture_family,
            "architecture_version": self.architecture_version,
            "architecture_size": self.architecture_size,
            "architecture_display_name": self.architecture_display_name,
            "contract_major": self.contract_major,
            "active": self.active,
            "capability": self.capability,
        }


TERMINAL_RUNTIME_EXECUTION_STATUSES = {
    RuntimeExecutionStatus.STOPPED.value,
    RuntimeExecutionStatus.FAILED_TERMINAL.value,
    RuntimeExecutionStatus.RECOVERED.value,
}

ACTIVE_RUNTIME_EXECUTION_STATUSES = {
    RuntimeExecutionStatus.REQUESTED.value,
    RuntimeExecutionStatus.ADMITTED.value,
    RuntimeExecutionStatus.VALIDATING.value,
    RuntimeExecutionStatus.LOADING_PIPELINE.value,
    RuntimeExecutionStatus.STARTING.value,
    RuntimeExecutionStatus.RUNNING.value,
    RuntimeExecutionStatus.DRAINING.value,
    RuntimeExecutionStatus.FAILED_RETRYABLE.value,
}


def refresh_runtime_model_capability_cache(
    session: Session,
    *,
    ttl_seconds: int = 10,
) -> int:
    """Project deployment-table truth into the runtime model capability cache."""
    now = utc_now()
    expires_at = now + timedelta(seconds=max(ttl_seconds, 1))
    rows = session.execute(
        select(DeploymentRelease, ModelVersion, ModelFamily)
        .join(ModelVersion, ModelVersion.id == DeploymentRelease.model_version_id)
        .join(ModelFamily, ModelFamily.id == DeploymentRelease.model_family_id)
        .where(
            DeploymentRelease.deleted_at.is_(None),
            ModelVersion.deleted_at.is_(None),
            ModelFamily.deleted_at.is_(None),
            ModelFamily.active.is_(True),
        )
    ).all()
    existing_rows = list(session.scalars(select(RuntimeModelCapabilityCache)).all())

    targets = {
        row.target_path: row
        for row in session.scalars(select(DeploymentTarget)).all()
    }
    promotions = {
        row.deployment_release_id: row
        for row in session.scalars(
            select(ModelPromotion).where(ModelPromotion.deployment_release_id.is_not(None))
        ).all()
        if row.deployment_release_id
    }
    existing = {
        row.deployment_release_id: row
        for row in existing_rows
    }

    synced_release_ids: set[str] = set()
    changed = 0
    for release, version, family in rows:
        synced_release_ids.add(release.id)
        deployment_status, selectable = _runtime_deployment_status(
            release=release,
            target=targets.get(release.target_path),
            promotion=promotions.get(release.id),
        )
        capability = _deployment_capability_payload(
            release=release,
            version=version,
            family=family,
            deployment_status=deployment_status,
            selectable=selectable,
        )
        cache_row = existing.get(release.id)
        if cache_row is None:
            cache_row = RuntimeModelCapabilityCache(
                id=generate_prefixed_id("rmc"),
                model_family_slug=family.slug,
                target_path=release.target_path,
                contract_major=int(family.contract_major or 1),
                deployment_release_id=release.id,
                model_version_id=version.id,
                task_type=family.task_type,
                capability=capability,
                refreshed_at=now,
                expires_at=expires_at,
            )
            session.add(cache_row)
            changed += 1
            continue
        if (
            cache_row.model_family_slug != family.slug
            or cache_row.target_path != release.target_path
            or int(cache_row.contract_major or 1) != int(family.contract_major or 1)
            or cache_row.model_version_id != version.id
            or cache_row.task_type != family.task_type
            or cache_row.capability != capability
            or (_aware_datetime(cache_row.expires_at) or now) <= now
        ):
            changed += 1
        cache_row.model_family_slug = family.slug
        cache_row.target_path = release.target_path
        cache_row.contract_major = int(family.contract_major or 1)
        cache_row.model_version_id = version.id
        cache_row.task_type = family.task_type
        cache_row.capability = capability
        cache_row.refreshed_at = now
        cache_row.expires_at = expires_at

    for cache_row in existing_rows:
        if cache_row.deployment_release_id in synced_release_ids:
            continue
        capability = dict(cache_row.capability or {})
        if (
            capability.get("source_of_truth") != "deployment_releases"
            or (_aware_datetime(cache_row.expires_at) or now) > now
        ):
            cache_row.expires_at = now
            cache_row.capability = {
                **capability,
                "active": False,
                "deployment_status": "unavailable",
                "unavailable_reason": (
                    "deployment_release_missing"
                    if capability.get("source_of_truth") == "deployment_releases"
                    else "legacy_runtime_cache_source"
                ),
            }
            changed += 1
    session.flush()
    return changed


def list_runtime_model_capability_options(
    session: Session,
    *,
    task_type: TaskType | None = None,
    target_path: TargetPath | None = None,
    cache_ttl_seconds: int = 10,
) -> list[RuntimeModelCapabilityOption]:
    refresh_runtime_model_capability_cache(session, ttl_seconds=cache_ttl_seconds)
    now = utc_now()
    rows = session.scalars(
        select(RuntimeModelCapabilityCache).where(RuntimeModelCapabilityCache.expires_at > now)
    ).all()
    options: list[RuntimeModelCapabilityOption] = []
    for row in rows:
        capability = dict(row.capability or {})
        if not runtime_model_capability_is_selectable(capability):
            continue
        row_task = TaskType(row.task_type)
        row_target = TargetPath(row.target_path)
        if task_type and row_task != task_type:
            continue
        if target_path and row_target != target_path:
            continue
        architecture = capability.get("architecture") or {}
        options.append(
            RuntimeModelCapabilityOption(
                deployment_release_id=row.deployment_release_id,
                model_family_slug=row.model_family_slug,
                display_name=str(
                    capability.get("display_name")
                    or capability.get("model_family_display_name")
                    or row.model_family_slug
                ),
                target_path=row_target,
                task_type=row_task,
                model_version_id=row.model_version_id,
                version_label=str(capability.get("version_label") or row.model_version_id),
                deployment_status=str(capability.get("deployment_status") or "available"),
                architecture_family=architecture.get("family"),
                architecture_version=architecture.get("version"),
                architecture_size=architecture.get("size"),
                architecture_display_name=architecture.get("display_name"),
                contract_major=row.contract_major,
                active=bool(capability.get("active", True)),
                capability=capability,
            )
        )
    return sorted(options, key=lambda item: (item.display_name.casefold(), item.version_label))


def create_runtime_control_command(
    session: Session,
    *,
    channel_id: str,
    command: RuntimeControlCommand,
    requested_by: str | None,
    reason: str | None = None,
    idempotency_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[RuntimeControlCommandRecord, RuntimeChannel]:
    channel = _get_channel(session, channel_id)
    existing = _get_idempotent_runtime_control_command(
        session,
        channel_id=channel_id,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        if existing.command != command.value or dict(existing.payload or {}) != dict(payload or {}):
            raise ValueError("Runtime control idempotency key was reused with another request.")
        # A retry returns the original command without resetting a completed
        # channel (or overwriting the state of a newer command).
        return existing, channel
    pending = _get_pending_runtime_control_command(
        session,
        channel_id=channel_id,
        command=command,
    )
    if pending is not None:
        pending_payload = dict(pending.payload or {})
        requested_payload = dict(payload or {})
        force_escalation = bool(requested_payload.get("force")) and not bool(
            pending_payload.get("force")
        )
        if not force_escalation:
            if payload and pending_payload != requested_payload:
                raise ValueError("A pending runtime command has different pinned workflow data.")
            return pending, channel

    if command in {RuntimeControlCommand.START, RuntimeControlCommand.RESTART}:
        if RuntimeChannelDesiredState(channel.desired_state) == RuntimeChannelDesiredState.DISABLED:
            raise ValueError("Disabled channels must be re-enabled before they can be started.")
        validate_runtime_channel_start_requirements(session, channel_id=channel_id)
    _apply_runtime_control_command_request_state(channel, command)

    row = RuntimeControlCommandRecord(
        id=generate_prefixed_id("rcc"),
        channel_id=channel_id,
        command=command.value,
        status=RuntimeControlCommandStatus.QUEUED.value,
        requested_by=requested_by,
        requested_state=(
            RuntimeChannelDesiredState.RUNNING.value
            if command in {RuntimeControlCommand.START, RuntimeControlCommand.RESTART}
            else RuntimeChannelDesiredState.STOPPED.value
            if command == RuntimeControlCommand.STOP
            else None
        ),
        reason=reason,
        idempotency_key=idempotency_key,
        payload=dict(payload or {}),
    )
    channel.last_activity_at = utc_now()
    session.add(row)
    session.flush()
    return row, channel


def _apply_runtime_control_command_request_state(
    channel: RuntimeChannel,
    command: RuntimeControlCommand,
) -> None:
    if command in {RuntimeControlCommand.START, RuntimeControlCommand.RESTART}:
        channel.desired_state = RuntimeChannelDesiredState.START_REQUESTED.value
        channel.observed_state = RuntimeObservedState.STARTING.value
        channel.health_state = HealthState.DEGRADED.value
        channel.health_summary = {
            "state": HealthState.DEGRADED.value,
            "status_text": "Start requested",
            "last_error": None,
            "recovery_hint": "Waiting for the runtime-control worker to start the executor.",
        }
        channel.last_activity_at = utc_now()
        return
    if command == RuntimeControlCommand.STOP:
        channel.desired_state = RuntimeChannelDesiredState.STOP_REQUESTED.value
        channel.observed_state = RuntimeObservedState.STOPPING.value
        channel.health_state = HealthState.DEGRADED.value
        channel.health_summary = {
            "state": HealthState.DEGRADED.value,
            "status_text": "Stop requested",
            "last_error": None,
            "recovery_hint": "Waiting for the runtime-control worker to stop the executor.",
        }
        channel.last_activity_at = utc_now()
        return
    channel.observed_state = RuntimeObservedState.RECOVERING.value
    channel.health_state = HealthState.DEGRADED.value
    channel.health_summary = {
        "state": HealthState.DEGRADED.value,
        "status_text": "Runtime reconciliation requested",
        "recovery_hint": "The orchestrator will compare persisted runtime state with executor handles.",
    }
    channel.last_activity_at = utc_now()


def runtime_control_command_to_api(row: RuntimeControlCommandRecord) -> RuntimeControlCommandSummary:
    return RuntimeControlCommandSummary(
        job_id=row.id,
        command_id=row.id,
        channel_id=row.channel_id,
        command=RuntimeControlCommand(row.command),
        status=RuntimeControlCommandStatus(row.status),
        accepted_at=_iso(row.created_at) or utc_now_iso(),
        claimed_at=_iso(row.claimed_at),
        consumed_at=_iso(row.consumed_at),
        finished_at=_iso(row.finished_at),
        runtime_instance_id=row.runtime_execution_id,
        last_error=row.last_error,
    )


def claim_next_runtime_control_command(
    session: Session,
    *,
    worker_id: str,
) -> RuntimeControlCommandRecord | None:
    row = session.scalar(
        select(RuntimeControlCommandRecord)
        .join(RuntimeChannel, RuntimeChannel.id == RuntimeControlCommandRecord.channel_id)
        .where(RuntimeControlCommandRecord.status == RuntimeControlCommandStatus.QUEUED.value)
        .where(RuntimeChannel.execution_target == "server")
        .order_by(RuntimeControlCommandRecord.created_at.asc())
    )
    if row is None:
        return None
    row.status = RuntimeControlCommandStatus.PROCESSING.value
    row.claimed_by = worker_id
    row.claimed_at = utc_now()
    row.consumed_at = row.claimed_at
    row.attempt_count = int(row.attempt_count or 0) + 1
    session.flush()
    return row


def mark_runtime_control_command_succeeded(
    session: Session,
    command_id: str,
    *,
    runtime_execution_id: str | None = None,
) -> RuntimeControlCommandRecord:
    row = _get_runtime_control_command(session, command_id)
    row.status = RuntimeControlCommandStatus.SUCCEEDED.value
    row.runtime_execution_id = runtime_execution_id or row.runtime_execution_id
    row.finished_at = utc_now()
    row.last_error = None
    session.flush()
    return row


def mark_runtime_control_command_failed(
    session: Session,
    command_id: str,
    *,
    error: str,
    runtime_execution_id: str | None = None,
) -> RuntimeControlCommandRecord:
    row = _get_runtime_control_command(session, command_id)
    row.status = RuntimeControlCommandStatus.FAILED.value
    row.runtime_execution_id = runtime_execution_id or row.runtime_execution_id
    row.finished_at = utc_now()
    row.last_error = error
    channel = session.get(RuntimeChannel, row.channel_id)
    if channel is not None:
        channel.observed_state = RuntimeObservedState.FAILED.value
        channel.health_state = HealthState.DOWN.value
        channel.last_error = error
        channel.health_summary = {
            "state": HealthState.DOWN.value,
            "status_text": "Runtime command failed",
            "last_error": error,
        }
        channel.last_activity_at = utc_now()
    session.flush()
    return row


def validate_runtime_channel_start_requirements(
    session: Session,
    *,
    channel_id: str,
) -> LoadedRuntimePipeline:
    loaded = load_published_runtime_pipeline(session, channel_id=channel_id)
    channel_mode = RuntimeMode(loaded.channel.mode)
    if RuntimeMode(loaded.version.mode) != channel_mode:
        raise ValueError(
            "Live channel pipelines must match the channel execution mode "
            f"({loaded.version.mode} != {channel_mode.value}). "
            "Assign a realtime pipeline for Start runtime; triggerable pipelines run "
            "through trigger endpoints."
        )
    uses_test_photo = any(
        node.node_type == "source.test_photo" for node in loaded.graph.nodes
    )
    if loaded.source is None and not uses_test_photo:
        raise ValueError("Channel source is missing.")
    if loaded.source is not None and not uses_test_photo:
        _validate_runtime_source_config(
            RuntimeSourceType(loaded.source.source_type),
            dict(loaded.source.config or {}),
        )
    resolver = build_runtime_model_resolver(session)
    for node in loaded.graph.nodes:
        if node.node_category == RuntimeNodeCategory.INFERENCE:
            if node.logical_model_ref is None:
                raise ValueError(f"Inference node {node.node_key} has no logical model reference.")
            try:
                resolver(node.logical_model_ref)
            except ValueError as exc:
                model_ref = node.logical_model_ref
                raise ValueError(
                    f"Inference node {node.node_key} cannot resolve model "
                    f"{model_ref.model_family_slug}/{model_ref.target_path.value} "
                    f"with policy {model_ref.selection_policy}: {exc}"
                ) from exc
    return loaded


def create_runtime_execution_instance(
    session: Session,
    *,
    loaded: LoadedRuntimePipeline,
    command_id: str | None,
    executor_instance_id: str,
    assigned_runtime_worker: str,
) -> RuntimeExecution:
    executor_service = _executor_for_mode(RuntimeMode(loaded.channel.mode))
    execution = RuntimeExecution(
        id=generate_prefixed_id("rex"),
        channel_id=loaded.channel.id,
        pipeline_version_id=loaded.version.id,
        mode=loaded.channel.mode,
        executor_service=executor_service,
        executor_instance_id=executor_instance_id,
        assigned_runtime_worker=assigned_runtime_worker,
        control_command_id=command_id,
        status=RuntimeExecutionStatus.VALIDATING.value,
        actual_state=RuntimeObservedState.VALIDATING.value,
        loading_state="validating",
        desired_state=RuntimeChannelDesiredState.RUNNING.value,
        started_at=utc_now(),
        heartbeat_at=utc_now(),
        runtime_plan=_runtime_plan_summary(session, loaded),
        health_summary={
            "state": HealthState.DEGRADED.value,
            "status_text": "Validating runtime plan",
            "recovery_hint": "If validation stalls, check the assigned pipeline and model capability references.",
        },
        metrics_summary={},
    )
    session.add(execution)
    loaded.channel.active_runtime_execution_id = execution.id
    loaded.channel.pipeline_version_id = loaded.version.id
    loaded.channel.desired_state = RuntimeChannelDesiredState.RUNNING.value
    loaded.channel.observed_state = RuntimeObservedState.VALIDATING.value
    loaded.channel.health_state = HealthState.DEGRADED.value
    loaded.channel.health_summary = {
        "state": HealthState.DEGRADED.value,
        "status_text": "Validating runtime plan",
        "runtime_instance_id": execution.id,
        "recovery_hint": "If validation stalls, check the assigned pipeline and model capability references.",
    }
    loaded.channel.last_error = None
    loaded.channel.last_activity_at = utc_now()
    session.flush()
    return execution


def transition_runtime_execution_loading(
    session: Session,
    *,
    runtime_execution_id: str,
    status: RuntimeExecutionStatus,
    actual_state: RuntimeObservedState,
    loading_state: str,
    health_text: str,
) -> RuntimeExecution:
    execution = _get_runtime_execution(session, runtime_execution_id)
    execution.status = status.value
    execution.actual_state = actual_state.value
    execution.loading_state = loading_state
    execution.heartbeat_at = utc_now()
    execution.health_summary = {
        "state": HealthState.DEGRADED.value,
        "status_text": health_text,
        "recovery_hint": "If this state does not advance, reconcile the runtime-control service.",
    }
    channel = session.get(RuntimeChannel, execution.channel_id)
    if channel is not None:
        channel.observed_state = actual_state.value
        channel.health_state = HealthState.DEGRADED.value
        channel.health_summary = {
            "state": HealthState.DEGRADED.value,
            "status_text": health_text,
            "runtime_instance_id": execution.id,
            "recovery_hint": "If this state does not advance, reconcile the runtime-control service.",
        }
        channel.last_activity_at = utc_now()
    session.flush()
    return execution


def mark_runtime_execution_running(
    session: Session,
    *,
    runtime_execution_id: str,
    executor_instance_id: str | None = None,
    assigned_runtime_worker: str | None = None,
) -> RuntimeExecution:
    execution = _get_runtime_execution(session, runtime_execution_id)
    now = utc_now()
    execution.status = RuntimeExecutionStatus.RUNNING.value
    execution.actual_state = RuntimeObservedState.RUNNING.value
    execution.loading_state = "ready"
    execution.executor_instance_id = executor_instance_id or execution.executor_instance_id
    execution.assigned_runtime_worker = assigned_runtime_worker or execution.assigned_runtime_worker
    execution.heartbeat_at = now
    execution.last_error = None
    execution.health_summary = {
        "state": HealthState.OK.value,
        "status_text": "Runtime instance is running",
        "last_heartbeat_at": _iso(now),
        "recovery_hint": None,
    }
    channel = session.get(RuntimeChannel, execution.channel_id)
    if channel is not None:
        channel.active_runtime_execution_id = execution.id
        channel.desired_state = RuntimeChannelDesiredState.RUNNING.value
        channel.observed_state = RuntimeObservedState.RUNNING.value
        channel.health_state = HealthState.OK.value
        channel.health_summary = {
            "state": HealthState.OK.value,
            "status_text": "Running",
            "runtime_instance_id": execution.id,
            "last_heartbeat_at": _iso(now),
            "recovery_hint": None,
        }
        channel.performance_summary = _channel_performance_from_metrics(execution.metrics_summary or {})
        channel.last_error = None
        channel.last_activity_at = now
        _set_trigger_endpoint_status(
            session,
            channel_id=channel.id,
            status=RuntimeResourceStatus.ACTIVE
            if RuntimeMode(execution.mode) == RuntimeMode.TRIGGER
            else None,
        )
    session.flush()
    return execution


def mark_runtime_execution_failed_retryable(
    session: Session,
    *,
    runtime_execution_id: str,
    error: str,
    executor_instance_id: str | None = None,
    health_summary: dict[str, Any] | None = None,
    metrics_summary: dict[str, Any] | None = None,
) -> RuntimeExecution:
    execution = _get_runtime_execution(session, runtime_execution_id)
    now = utc_now()
    execution.heartbeat_at = now
    execution.executor_instance_id = executor_instance_id or execution.executor_instance_id
    execution_stopping = (
        execution.desired_state == RuntimeChannelDesiredState.STOPPED.value
        or execution.status
        in {
            RuntimeExecutionStatus.DRAINING.value,
            RuntimeExecutionStatus.STOPPED.value,
            RuntimeExecutionStatus.FAILED_TERMINAL.value,
            RuntimeExecutionStatus.RECOVERED.value,
        }
    )
    if execution_stopping:
        session.flush()
        return execution

    summary = {
        "state": HealthState.DEGRADED.value,
        "status_text": "Runtime executor exited unexpectedly",
        "last_error": error,
        "recovery_hint": "Runtime-control will relaunch this executor if the channel remains desired running.",
    }
    if health_summary is not None:
        summary.update(health_summary)
        summary["state"] = summary.get("state") or HealthState.DEGRADED.value
        summary["status_text"] = summary.get("status_text") or "Runtime executor exited unexpectedly"
        summary["last_error"] = summary.get("last_error") or error
        summary["recovery_hint"] = summary.get("recovery_hint") or (
            "Runtime-control will relaunch this executor if the channel remains desired running."
        )

    execution.status = RuntimeExecutionStatus.FAILED_RETRYABLE.value
    execution.actual_state = RuntimeObservedState.DEGRADED.value
    execution.loading_state = "executor_failed"
    execution.last_error = error
    execution.health_summary = summary
    execution.recovery_attempts = int(execution.recovery_attempts or 0) + 1
    if metrics_summary is not None:
        execution.metrics_summary = metrics_summary
        execution.frame_count = int(metrics_summary.get("frame_count", execution.frame_count or 0) or 0)
        execution.drop_count = int(metrics_summary.get("drop_count", execution.drop_count or 0) or 0)
        execution.trigger_count = int(
            metrics_summary.get("trigger_count", execution.trigger_count or 0) or 0
        )

    channel = session.get(RuntimeChannel, execution.channel_id)
    if (
        channel is not None
        and channel.active_runtime_execution_id == execution.id
        and channel.desired_state == RuntimeChannelDesiredState.RUNNING.value
    ):
        channel.observed_state = RuntimeObservedState.DEGRADED.value
        channel.health_state = HealthState.DEGRADED.value
        channel.health_summary = {
            **summary,
            "last_heartbeat_at": _iso(now),
            "runtime_instance_id": execution.id,
        }
        channel.performance_summary = _channel_performance_from_metrics(execution.metrics_summary or {})
        channel.last_error = error
        channel.last_activity_at = now
        _set_trigger_endpoint_status(
            session,
            channel_id=channel.id,
            status=RuntimeResourceStatus.DEGRADED
            if RuntimeMode(execution.mode) == RuntimeMode.TRIGGER
            else None,
        )
    session.flush()
    return execution


def get_active_runtime_execution(
    session: Session,
    *,
    channel_id: str,
) -> RuntimeExecution | None:
    channel = session.get(RuntimeChannel, channel_id)
    if channel is not None and channel.active_runtime_execution_id:
        execution = session.get(RuntimeExecution, channel.active_runtime_execution_id)
        if execution is not None and execution.status not in TERMINAL_RUNTIME_EXECUTION_STATUSES:
            return execution
    return session.scalar(
        select(RuntimeExecution)
        .where(
            RuntimeExecution.channel_id == channel_id,
            RuntimeExecution.status.in_(ACTIVE_RUNTIME_EXECUTION_STATUSES),
        )
        .order_by(RuntimeExecution.created_at.desc())
    )


def get_ready_runtime_execution_binding(
    session: Session,
    *,
    channel_id: str,
    expected_mode: RuntimeMode | None = None,
) -> tuple[RuntimeExecution, LoadedRuntimePipeline]:
    execution = get_active_runtime_execution(session, channel_id=channel_id)
    if execution is None or RuntimeObservedState(execution.actual_state) != RuntimeObservedState.RUNNING:
        raise ValueError("Channel runtime is not running.")
    if expected_mode is not None and RuntimeMode(execution.mode) != expected_mode:
        raise ValueError(
            f"Runtime instance is bound to {execution.mode}, not {expected_mode.value}."
        )
    loaded = load_published_runtime_pipeline(
        session,
        channel_id=channel_id,
        pipeline_version_id=execution.pipeline_version_id,
        allow_superseded=True,
    )
    if loaded.version.id != execution.pipeline_version_id:
        raise ValueError("Running runtime instance is bound to a different pipeline version.")
    return execution, loaded


def runtime_execution_matches_channel_binding(
    session: Session,
    execution: RuntimeExecution,
) -> bool:
    try:
        loaded = load_published_runtime_pipeline(session, channel_id=execution.channel_id)
        if RuntimeMode(loaded.version.mode) != RuntimeMode(execution.mode):
            return False
        return runtime_execution_matches_loaded_binding(session, execution, loaded)
    except (ValueError, RuntimeGraphBuildError):
        return False


def runtime_execution_matches_loaded_binding(
    session: Session,
    execution: RuntimeExecution,
    loaded: LoadedRuntimePipeline,
) -> bool:
    runtime_plan = dict(execution.runtime_plan or {})
    actual_signature = dict(runtime_plan.get("binding_signature") or {})
    expected_signature = _runtime_binding_signature(session, loaded)
    if "model_bindings" not in actual_signature and not expected_signature.get("model_bindings"):
        actual_signature["model_bindings"] = []
    return (
        execution.pipeline_version_id == loaded.version.id
        and actual_signature == expected_signature
    )


def record_runtime_execution_heartbeat(
    session: Session,
    *,
    runtime_execution_id: str,
    executor_instance_id: str | None = None,
    health_summary: dict[str, Any] | None = None,
    metrics_summary: dict[str, Any] | None = None,
) -> RuntimeExecution:
    execution = _get_runtime_execution(session, runtime_execution_id)
    now = utc_now()
    execution.heartbeat_at = now
    execution.executor_instance_id = executor_instance_id or execution.executor_instance_id
    execution_stopping = (
        execution.desired_state == RuntimeChannelDesiredState.STOPPED.value
        or execution.status
        in {
            RuntimeExecutionStatus.DRAINING.value,
            RuntimeExecutionStatus.STOPPED.value,
            RuntimeExecutionStatus.FAILED_TERMINAL.value,
            RuntimeExecutionStatus.RECOVERED.value,
        }
    )
    if execution_stopping:
        session.flush()
        return execution
    reported_health = _health_state_from_summary(health_summary or execution.health_summary)
    if (
        execution.status == RuntimeExecutionStatus.FAILED_RETRYABLE.value
        and reported_health == HealthState.OK
    ):
        execution.status = RuntimeExecutionStatus.RUNNING.value
        execution.loading_state = "ready"
    if health_summary is not None:
        execution.health_summary = {
            **health_summary,
            "recovery_hint": health_summary.get("recovery_hint"),
        }
        last_error = health_summary.get("last_error")
        execution.last_error = str(last_error) if last_error else None
    if metrics_summary is not None:
        execution.metrics_summary = metrics_summary
        execution.frame_count = int(metrics_summary.get("frame_count", execution.frame_count or 0) or 0)
        execution.drop_count = int(metrics_summary.get("drop_count", execution.drop_count or 0) or 0)
        execution.trigger_count = int(
            metrics_summary.get("trigger_count", execution.trigger_count or 0) or 0
        )
    if execution.status == RuntimeExecutionStatus.RUNNING.value:
        execution.actual_state = (
            RuntimeObservedState.RUNNING.value
            if reported_health == HealthState.OK
            else RuntimeObservedState.DEGRADED.value
        )
    channel = session.get(RuntimeChannel, execution.channel_id)
    running_execution_confirms_pending_start = (
        channel is not None
        and channel.desired_state == RuntimeChannelDesiredState.START_REQUESTED.value
        and execution.status == RuntimeExecutionStatus.RUNNING.value
        and execution.desired_state == RuntimeChannelDesiredState.RUNNING.value
    )
    channel_accepts_heartbeat = (
        channel is not None
        and channel.active_runtime_execution_id == execution.id
        and (
            channel.desired_state == RuntimeChannelDesiredState.RUNNING.value
            or running_execution_confirms_pending_start
        )
    )
    if channel_accepts_heartbeat:
        if running_execution_confirms_pending_start:
            channel.desired_state = RuntimeChannelDesiredState.RUNNING.value
        channel.observed_state = execution.actual_state
        channel.health_state = _health_state_from_summary(
            health_summary or execution.health_summary
        ).value
        channel.health_summary = {
            **(health_summary or execution.health_summary or {}),
            "last_heartbeat_at": _iso(now),
            "runtime_instance_id": execution.id,
            "recovery_hint": (health_summary or execution.health_summary or {}).get(
                "recovery_hint"
            ),
        }
        channel.performance_summary = _channel_performance_from_metrics(
            execution.metrics_summary or {}
        )
        heartbeat_error = (health_summary or execution.health_summary or {}).get("last_error")
        if heartbeat_error:
            channel.last_error = str(heartbeat_error)
        elif channel.health_state == HealthState.OK.value:
            channel.last_error = None
        channel.last_activity_at = now
        is_trigger = RuntimeMode(execution.mode) == RuntimeMode.TRIGGER
        _set_trigger_endpoint_status(
            session,
            channel_id=channel.id,
            status=RuntimeResourceStatus.ACTIVE
            if is_trigger and execution.actual_state == RuntimeObservedState.RUNNING.value
            else RuntimeResourceStatus.DEGRADED
            if is_trigger
            else None,
        )
    session.flush()
    return execution


def stop_runtime_execution(
    session: Session,
    *,
    runtime_execution_id: str,
    error: str | None = None,
    failed: bool = False,
) -> RuntimeExecution:
    execution = _get_runtime_execution(session, runtime_execution_id)
    now = utc_now()
    execution.desired_state = RuntimeChannelDesiredState.STOPPED.value
    execution.status = (
        RuntimeExecutionStatus.FAILED_TERMINAL.value if failed else RuntimeExecutionStatus.STOPPED.value
    )
    execution.actual_state = RuntimeObservedState.FAILED.value if failed else RuntimeObservedState.STOPPED.value
    execution.loading_state = "failed" if failed else "stopped"
    execution.stopped_at = now
    execution.heartbeat_at = now
    execution.last_error = error
    execution.health_summary = {
        "state": HealthState.DOWN.value,
        "status_text": "Runtime failed" if failed else "Runtime stopped",
        "last_error": error,
        "recovery_hint": (
            "Inspect the last error and restart after correcting the source or pipeline issue."
            if failed
            else "Start the channel when runtime execution is required."
        ),
    }
    channel = session.get(RuntimeChannel, execution.channel_id)
    if channel is not None and channel.active_runtime_execution_id == execution.id:
        channel.desired_state = RuntimeChannelDesiredState.STOPPED.value
        channel.observed_state = execution.actual_state
        channel.health_state = HealthState.DOWN.value
        channel.active_runtime_execution_id = None if not failed else execution.id
        channel.last_error = error
        channel.health_summary = {
            "state": HealthState.DOWN.value,
            "status_text": "Failed" if failed else "Stopped",
            "last_error": error,
            "runtime_instance_id": execution.id,
            "recovery_hint": (
                "Inspect the last error and restart after correcting the source or pipeline issue."
                if failed
                else "Start the channel when runtime execution is required."
            ),
        }
        channel.last_activity_at = now
        _set_trigger_endpoint_status(
            session,
            channel_id=channel.id,
            status=RuntimeResourceStatus.FAILED
            if failed and RuntimeMode(execution.mode) == RuntimeMode.TRIGGER
            else RuntimeResourceStatus.DISABLED
            if RuntimeMode(execution.mode) == RuntimeMode.TRIGGER
            else None,
        )
    session.flush()
    return execution


def request_runtime_execution_stop(
    session: Session,
    *,
    runtime_execution_id: str,
    update_channel: bool = True,
) -> RuntimeExecution:
    execution = _get_runtime_execution(session, runtime_execution_id)
    execution.status = RuntimeExecutionStatus.DRAINING.value
    execution.actual_state = RuntimeObservedState.STOPPING.value
    execution.loading_state = "stopping"
    execution.desired_state = RuntimeChannelDesiredState.STOPPED.value
    execution.heartbeat_at = utc_now()
    channel = session.get(RuntimeChannel, execution.channel_id)
    if update_channel and channel is not None:
        channel.desired_state = RuntimeChannelDesiredState.STOP_REQUESTED.value
        channel.observed_state = RuntimeObservedState.STOPPING.value
        channel.health_state = HealthState.DEGRADED.value
        channel.health_summary = {
            "state": HealthState.DEGRADED.value,
            "status_text": "Stopping",
            "runtime_instance_id": execution.id,
            "recovery_hint": "If stopping stalls, reconcile the runtime orchestrator.",
        }
        channel.last_activity_at = utc_now()
    session.flush()
    return execution


def mark_stale_runtime_executions(
    session: Session,
    *,
    stale_after_seconds: int,
    fail_after_seconds: int | None = None,
    stop_after_seconds: int | None = None,
) -> list[RuntimeExecution]:
    now = utc_now()
    stale_before = now - timedelta(seconds=stale_after_seconds)
    fail_before = now - timedelta(seconds=fail_after_seconds or stale_after_seconds * 2)
    stop_before = now - timedelta(seconds=stop_after_seconds or stale_after_seconds)
    stale_rows = session.scalars(
        select(RuntimeExecution)
        .where(
            or_(
                and_(
                    RuntimeExecution.status.in_(
                        {
                            RuntimeExecutionStatus.RUNNING.value,
                            RuntimeExecutionStatus.FAILED_RETRYABLE.value,
                        }
                    ),
                    RuntimeExecution.heartbeat_at.is_not(None),
                    RuntimeExecution.heartbeat_at < stale_before,
                ),
                and_(
                    RuntimeExecution.status == RuntimeExecutionStatus.DRAINING.value,
                    RuntimeExecution.heartbeat_at.is_not(None),
                    RuntimeExecution.heartbeat_at < stop_before,
                ),
            ),
        )
        .order_by(RuntimeExecution.heartbeat_at.asc())
    ).all()
    changed: list[RuntimeExecution] = []
    for execution in stale_rows:
        if execution.status == RuntimeExecutionStatus.DRAINING.value:
            stopped = stop_runtime_execution(
                session,
                runtime_execution_id=execution.id,
            )
            stopped.health_summary = {
                **(stopped.health_summary or {}),
                "status_text": "Runtime stopped after stale stop recovery",
                "forced_stop_recovery": True,
                "recovery_hint": "Start the channel when live execution is required.",
            }
            channel = session.get(RuntimeChannel, stopped.channel_id)
            if channel is not None:
                channel.health_summary = {
                    **(channel.health_summary or {}),
                    "status_text": "Stopped after stale stop recovery",
                    "forced_stop_recovery": True,
                }
            changed.append(stopped)
            continue
        heartbeat_at = _aware_datetime(execution.heartbeat_at)
        if heartbeat_at and heartbeat_at < fail_before:
            execution.status = RuntimeExecutionStatus.FAILED_TERMINAL.value
            execution.actual_state = RuntimeObservedState.FAILED.value
            execution.loading_state = "heartbeat_failed"
            execution.last_error = "Runtime executor heartbeat is stale; the instance is treated as failed."
        else:
            execution.status = RuntimeExecutionStatus.FAILED_RETRYABLE.value
            execution.actual_state = RuntimeObservedState.DEGRADED.value
            execution.loading_state = "heartbeat_stale"
            execution.last_error = "Runtime executor heartbeat is stale."
        execution.recovery_attempts = int(execution.recovery_attempts or 0) + 1
        execution.health_summary = {
            "state": HealthState.DEGRADED.value
            if execution.actual_state == RuntimeObservedState.DEGRADED.value
            else HealthState.DOWN.value,
            "status_text": "Heartbeat stale",
            "last_error": execution.last_error,
            "recovery_hint": (
                "Heartbeat exceeded the failure window. Restart after checking the source and executor logs."
                if execution.actual_state == RuntimeObservedState.FAILED.value
                else "Heartbeat is stale. Reconcile or restart if the executor does not recover."
            ),
        }
        channel = session.get(RuntimeChannel, execution.channel_id)
        if channel is not None and channel.active_runtime_execution_id == execution.id:
            channel.observed_state = execution.actual_state
            channel.health_state = (
                HealthState.DEGRADED.value
                if execution.actual_state == RuntimeObservedState.DEGRADED.value
                else HealthState.DOWN.value
            )
            channel.last_error = execution.last_error
            channel.health_summary = {
                **execution.health_summary,
                "runtime_instance_id": execution.id,
                "last_heartbeat_at": _iso(execution.heartbeat_at),
            }
            channel.last_activity_at = now
            _set_trigger_endpoint_status(
                session,
                channel_id=channel.id,
                status=RuntimeResourceStatus.FAILED
                if execution.actual_state == RuntimeObservedState.FAILED.value
                else RuntimeResourceStatus.DEGRADED,
            )
        changed.append(execution)
    session.flush()
    return changed


def list_recoverable_runtime_executions(session: Session) -> list[RuntimeExecution]:
    return session.scalars(
        select(RuntimeExecution)
        .where(
            RuntimeExecution.desired_state == RuntimeChannelDesiredState.RUNNING.value,
            RuntimeExecution.status.in_(
                {
                    RuntimeExecutionStatus.RUNNING.value,
                    RuntimeExecutionStatus.FAILED_RETRYABLE.value,
                    RuntimeExecutionStatus.REQUESTED.value,
                    RuntimeExecutionStatus.ADMITTED.value,
                    RuntimeExecutionStatus.STARTING.value,
                }
            ),
        )
        .order_by(RuntimeExecution.created_at.asc())
    ).all()


def build_runtime_model_resolver(session: Session):
    # Refresh once when a resolver is created. A resolver may be reused for all
    # inference nodes in a runtime; refreshing inside _resolve made every frame
    # scan and update the complete deployment capability projection.
    refresh_runtime_model_capability_cache(session)

    def _resolve(model_ref: RuntimeLogicalModelRef) -> RuntimeModelBinding:
        now = utc_now()
        query = select(RuntimeModelCapabilityCache).where(
            RuntimeModelCapabilityCache.model_family_slug == model_ref.model_family_slug,
            RuntimeModelCapabilityCache.target_path == model_ref.target_path.value,
            RuntimeModelCapabilityCache.contract_major == model_ref.contract_major,
            RuntimeModelCapabilityCache.expires_at > now,
        )
        candidate_rows = session.scalars(query).all()
        rows = candidate_rows
        if model_ref.selection_policy == "explicit_release":
            rows = [row for row in rows if row.deployment_release_id == model_ref.deployment_release_id]
        rows = [
            row
            for row in rows
            if runtime_model_capability_is_selectable_for_policy(
                dict(row.capability or {}),
                model_ref.selection_policy,
            )
        ]
        if not rows:
            raise ValueError(
                _runtime_model_capability_unavailable_detail(
                    session,
                    model_ref=model_ref,
                    now=now,
                    candidate_rows=candidate_rows,
                )
            )
        rows.sort(
            key=lambda row: runtime_model_capability_rank(
                dict(row.capability or {}),
                model_ref.selection_policy,
            )
        )
        row = rows[0]
        task_type = TaskType(row.task_type)
        if task_type != model_ref.required_task_type:
            raise ValueError(
                f"Model capability task {task_type.value} does not satisfy "
                f"{model_ref.required_task_type.value}."
            )
        capability = dict(row.capability or {})
        return RuntimeModelBinding(
            deployment_release_id=row.deployment_release_id,
            deployment_target=row.target_path,
            target_path=TargetPath(row.target_path),
            model_family_slug=row.model_family_slug,
            model_version_id=row.model_version_id,
            task_type=task_type,
            contract_major=row.contract_major,
            display_name=str(capability.get("display_name") or row.model_family_slug),
            capability=capability,
        )

    return _resolve


def load_published_runtime_pipeline(
    session: Session,
    *,
    channel_id: str,
    pipeline_version_id: str | None = None,
    allow_superseded: bool = False,
) -> LoadedRuntimePipeline:
    channel = _get_channel(session, channel_id)
    version = (
        _resolve_explicit_pipeline_version(session, pipeline_version_id)
        if pipeline_version_id
        else _resolve_channel_pipeline_version(session, channel)
    )
    allowed_statuses = {RuntimePipelineStatus.PUBLISHED}
    if allow_superseded:
        allowed_statuses.add(RuntimePipelineStatus.SUPERSEDED)
    if RuntimePipelineStatus(version.status) not in allowed_statuses:
        raise ValueError("Assigned pipeline version must be published before execution.")
    definition = session.get(PipelineDefinition, version.pipeline_definition_id)
    if definition is None:
        raise ValueError("Pipeline definition is missing for the assigned version.")
    if definition.scope != "channel_instance" or definition.owner_channel_id != channel.id:
        raise ValueError("Channel execution requires a published channel-scoped pipeline instance.")
    graph = RuntimePipelineGraph.model_validate(version.graph or {})
    handler_refs = _handler_refs(session)
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id=version.id,
        graph=graph,
        handler_refs=handler_refs,
    )
    source = session.get(RuntimeSource, channel.source_id) if channel.source_id else None
    return LoadedRuntimePipeline(
        channel=channel,
        source=source,
        version=version,
        definition=definition,
        graph=graph,
        executable=executable,
    )


def bind_runtime_channel_execution(
    session: Session,
    *,
    channel_id: str,
    executor_service: str | None = None,
    executor_instance_id: str | None = None,
) -> tuple[RuntimeExecution, LoadedRuntimePipeline]:
    channel = _get_channel(session, channel_id)
    try:
        loaded = load_published_runtime_pipeline(session, channel_id=channel_id)
    except (ValueError, RuntimeGraphBuildError) as exc:
        channel.observed_state = RuntimeObservedState.FAILED.value
        channel.health_state = HealthState.DOWN.value
        channel.last_error = str(exc)
        channel.last_activity_at = utc_now()
        session.flush()
        raise
    execution = RuntimeExecution(
        id=generate_prefixed_id("rex"),
        channel_id=channel.id,
        pipeline_version_id=loaded.version.id,
        mode=channel.mode,
        executor_service=executor_service
        or ("trigger-executor" if RuntimeMode(channel.mode) == RuntimeMode.TRIGGER else "realtime-executor"),
        executor_instance_id=executor_instance_id,
        assigned_runtime_worker=executor_instance_id,
        status=RuntimeExecutionStatus.RUNNING.value,
        actual_state=RuntimeObservedState.RUNNING.value,
        loading_state="ready",
        desired_state=RuntimeChannelDesiredState.RUNNING.value,
        started_at=utc_now(),
        heartbeat_at=utc_now(),
        runtime_plan=_runtime_plan_summary(session, loaded),
        health_summary={
            "state": HealthState.OK.value,
            "status_text": "Runtime instance is running",
        },
        metrics_summary={},
    )
    session.add(execution)
    channel.desired_state = RuntimeChannelDesiredState.RUNNING.value
    channel.observed_state = RuntimeObservedState.RUNNING.value
    channel.health_state = HealthState.OK.value
    channel.active_runtime_execution_id = execution.id
    channel.last_error = None
    channel.last_activity_at = utc_now()
    session.flush()
    return execution, loaded


def record_runtime_execution_result(
    session: Session,
    result: RuntimeExecutionResult,
    *,
    channel_id: str,
    runtime_execution_id: str | None = None,
    trigger_endpoint_id: str | None = None,
    retain_history: bool | None = None,
) -> ResultRecord:
    execution = session.get(RuntimeExecution, runtime_execution_id) if runtime_execution_id else None
    channel = session.get(RuntimeChannel, channel_id)
    history_policy = _result_history_policy(channel)
    history_retained = bool(
        result.persisted_results
        and (
            retain_history
            if retain_history is not None
            else history_policy.enabled
            and _runtime_result_history_is_due(
                session,
                channel_id=channel_id,
                policy=history_policy,
            )
        )
    )
    mode = (
        RuntimeMode(execution.mode)
        if execution is not None
        else RuntimeMode.TRIGGER
        if trigger_endpoint_id
        else RuntimeMode.REALTIME
    )
    history_outputs = (
        _without_inline_image_data(result.persisted_results) if history_retained else []
    )
    runtime_metadata = {
        "runtime_execution_id": runtime_execution_id,
        "trigger_endpoint_id": trigger_endpoint_id,
        "executor_service": execution.executor_service if execution is not None else None,
        "executor_instance_id": execution.executor_instance_id if execution is not None else None,
        "assigned_runtime_worker": execution.assigned_runtime_worker if execution is not None else None,
        "correlation_id": result.correlation_id,
        "result_created_by": "runtime_execution_store",
        "shadow_mode": _stem_shadow_mode_from_outputs(history_outputs),
    }
    row = ResultRecord(
        id=generate_prefixed_id("rrc"),
        channel_id=channel_id,
        pipeline_version_id=result.pipeline_version_id,
        runtime_execution_id=runtime_execution_id,
        trigger_endpoint_id=trigger_endpoint_id,
        correlation_id=result.correlation_id,
        source_timestamp=_source_timestamp_from_execution_result(result),
        completed_at=utc_now(),
        mode=mode.value,
        status=result.status if result.status in {"completed", "failed"} else "completed",
        result_type=_result_type(
            mode=mode,
            status=result.status,
            trigger_endpoint_id=trigger_endpoint_id,
            history_outputs=history_outputs,
        ),
        schema_version=result.schema_version,
        result=_compact_result_record_payload(
            result,
            history_outputs=history_outputs,
        ),
        # History stores only fields explicitly projected to the history
        # destination. Raw masks, frames, and complete inference payloads are
        # runtime data and must not silently become durable database history.
        raw_inference_outputs=[],
        normalized_inference_outputs=[],
        derived_outputs=history_outputs,
        overlay_primitives=[],
        trigger_response=_without_inline_image_data(result.final_response),
        runtime_metadata=runtime_metadata,
        raw_outputs_ref=None,
        timing=result.timing,
        model_routes=[
            item["payload"].get("route")
            for item in result.normalized_inference_outputs
            if isinstance(item, dict)
            and isinstance(item.get("payload"), dict)
            and item["payload"].get("route")
        ],
        history_retained=history_retained,
    )
    session.add(row)
    if result.capture_intents:
        from cv_db.capture_store import enqueue_capture_requests_for_result

        enqueue_capture_requests_for_result(
            session,
            result_record=row,
            execution_result=result,
        )
    if (
        result.overlay_outputs
        or result.http_trigger_responses
        or result.persisted_results
        or result.internal_events
    ):
        from cv_db.connector_store import enqueue_connector_deliveries_for_result

        enqueue_connector_deliveries_for_result(
            session,
            result_record=row,
            execution_result=result,
        )
    if execution is not None:
        if RuntimeMode(execution.mode) == RuntimeMode.TRIGGER:
            execution.trigger_count = int(execution.trigger_count or 0) + 1
            trigger_latency = result.timing.get("total_ms") or result.timing.get("latency_ms")
            execution.metrics_summary = {
                **(execution.metrics_summary or {}),
                "metrics_source": "trigger_executor",
                "execution_mode": RuntimeMode.TRIGGER.value,
                "trigger_count": execution.trigger_count,
                "last_trigger_latency_ms": trigger_latency,
                "trigger_latency_ms": trigger_latency,
                "last_trigger_at": utc_now_iso(),
                "last_trigger_status": result.status,
            }
        else:
            execution.frame_count = int(execution.frame_count or 0) + 1
        execution.last_result_id = row.id
        execution.heartbeat_at = utc_now()
        if result.status == "failed":
            execution.last_error = "; ".join(error.get("message", "") for error in result.errors)
    if channel is not None:
        channel.last_activity_at = utc_now()
        if execution is not None:
            channel.performance_summary = _channel_performance_from_metrics(
                execution.metrics_summary or {}
            )
        if trigger_endpoint_id is not None:
            if result.status == "failed":
                channel.last_error = "; ".join(
                    error.get("message", "") for error in result.errors
                )
            else:
                channel.last_error = None
        elif result.status == "failed":
            channel.observed_state = RuntimeObservedState.DEGRADED.value
            channel.health_state = HealthState.DEGRADED.value
            channel.last_error = "; ".join(error.get("message", "") for error in result.errors)
        else:
            channel.observed_state = RuntimeObservedState.RUNNING.value
            channel.health_state = HealthState.OK.value
            channel.last_error = None
    session.flush()
    return row


def _stem_shadow_mode_from_outputs(outputs: list[dict[str, Any]]) -> bool:
    for entry in outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        result = payload.get("result") if isinstance(payload, dict) else None
        if (
            isinstance(result, dict)
            and result.get("schema_version") in {
                "stem.orientation.v3",
                "stem.rotation-optimization.v1",
            }
            and result.get("shadow_mode") is True
        ):
            return True
    return False


def _compact_result_record_payload(
    result: RuntimeExecutionResult,
    *,
    history_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    # These sections have dedicated ResultRecord columns. Keeping the same large
    # inference payloads in the legacy JSON document doubled PostgreSQL storage.
    for key in (
        "raw_inference_outputs",
        "normalized_inference_outputs",
        "derived_outputs",
        "overlay_primitives",
        "overlay_outputs",
        "display_outputs",
        "capture_intents",
    ):
        payload[key] = []
    payload["persisted_results"] = history_outputs
    # node_outputs can contain full frame base64 data and duplicates of every
    # normalized/derived payload. Historical result APIs use the structured
    # columns above; live capture uses the dedicated latest-frame store.
    payload["node_outputs"] = {}
    return _without_inline_image_data(payload)


def _result_history_policy(
    channel: RuntimeChannel | None,
) -> RuntimeResultHistoryPolicy:
    if channel is None:
        return RuntimeResultHistoryPolicy()
    try:
        return RuntimeResultHistoryPolicy.model_validate(channel.result_history_policy or {})
    except ValueError:
        return RuntimeResultHistoryPolicy()


def _runtime_result_history_is_due(
    session: Session,
    *,
    channel_id: str,
    policy: RuntimeResultHistoryPolicy,
) -> bool:
    latest_completed_at = session.scalar(
        select(ResultRecord.completed_at)
        .where(
            ResultRecord.channel_id == channel_id,
            ResultRecord.history_retained.is_(True),
        )
        .order_by(ResultRecord.completed_at.desc(), ResultRecord.id.desc())
        .limit(1)
    )
    if latest_completed_at is None:
        return True
    if latest_completed_at.tzinfo is None:
        latest_completed_at = latest_completed_at.replace(tzinfo=UTC)
    return utc_now() - latest_completed_at >= timedelta(
        seconds=policy.sample_interval_seconds
    )


def _without_inline_image_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_inline_image_data(item)
            for key, item in value.items()
            if key not in {"image_base64", "data_uri"}
        }
    if isinstance(value, list):
        return [_without_inline_image_data(item) for item in value]
    return value


def _result_type(
    *,
    mode: RuntimeMode,
    status: str,
    trigger_endpoint_id: str | None,
    history_outputs: list[dict[str, Any]],
) -> str:
    if status == "failed":
        return "failed_execution"
    if _contains_ready_stem_rotation_optimization(history_outputs):
        return "stem_rotation_optimization"
    if _contains_decision_ready_stem_orientation(history_outputs):
        return "stem_orientation"
    if trigger_endpoint_id is not None:
        return "trigger_execution"
    if mode == RuntimeMode.REALTIME:
        return "realtime_frame"
    return "pipeline_execution"


def _contains_decision_ready_stem_orientation(
    history_outputs: list[dict[str, Any]],
) -> bool:
    for entry in history_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        value = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(value, dict) or value.get("schema_version") not in {
            "stem.orientation.v2",
            "stem.orientation.v3",
        }:
            continue
        decision = value.get("decision")
        if bool(value.get("decision_ready")) or (
            isinstance(decision, dict) and bool(decision.get("ready"))
        ):
            return True
    return False


def _contains_ready_stem_rotation_optimization(
    history_outputs: list[dict[str, Any]],
) -> bool:
    for entry in history_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        value = payload.get("result") if isinstance(payload, dict) else None
        if (
            isinstance(value, dict)
            and value.get("schema_version") == "stem.rotation-optimization.v1"
            and bool(value.get("result_ready"))
        ):
            return True
    return False


def _source_timestamp_from_execution_result(result: RuntimeExecutionResult) -> datetime | None:
    for node_outputs in result.node_outputs.values():
        for payload in node_outputs.values():
            if not isinstance(payload, dict):
                continue
            timestamp = payload.get("timestamp")
            if not timestamp and isinstance(payload.get("frame"), dict):
                timestamp = payload["frame"].get("timestamp")
            parsed = _parse_iso_datetime(timestamp)
            if parsed is not None:
                return parsed
    return None


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _overlay_primitives_from_execution_result(result: RuntimeExecutionResult) -> list[dict[str, Any]]:
    primitives: list[dict[str, Any]] = [
        primitive for primitive in result.overlay_primitives if isinstance(primitive, dict)
    ]
    if primitives:
        return primitives
    for entry in result.overlay_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        for primitive in payload.get("primitives") or []:
            if isinstance(primitive, dict):
                primitives.append(
                    {
                        **primitive,
                        "node_key": entry.get("node_key"),
                        "node_type": entry.get("node_type"),
                        "output_port": entry.get("output_port"),
                    }
                )
    return primitives


def _get_channel(session: Session, channel_id: str) -> RuntimeChannel:
    row = session.get(RuntimeChannel, channel_id)
    if row is None or row.deleted_at is not None:
        raise ValueError(f"Runtime channel {channel_id} does not exist.")
    return row


def _get_runtime_execution(session: Session, runtime_execution_id: str) -> RuntimeExecution:
    row = session.get(RuntimeExecution, runtime_execution_id)
    if row is None:
        raise ValueError(f"Runtime instance {runtime_execution_id} does not exist.")
    return row


def _get_runtime_control_command(
    session: Session,
    command_id: str,
) -> RuntimeControlCommandRecord:
    row = session.get(RuntimeControlCommandRecord, command_id)
    if row is None:
        raise ValueError(f"Runtime control command {command_id} does not exist.")
    return row


def _get_idempotent_runtime_control_command(
    session: Session,
    *,
    channel_id: str,
    idempotency_key: str | None,
) -> RuntimeControlCommandRecord | None:
    if not idempotency_key:
        return None
    return session.scalar(
        select(RuntimeControlCommandRecord).where(
            RuntimeControlCommandRecord.channel_id == channel_id,
            RuntimeControlCommandRecord.idempotency_key == idempotency_key,
        )
    )


def _get_pending_runtime_control_command(
    session: Session,
    *,
    channel_id: str,
    command: RuntimeControlCommand,
) -> RuntimeControlCommandRecord | None:
    return session.scalar(
        select(RuntimeControlCommandRecord)
        .where(
            RuntimeControlCommandRecord.channel_id == channel_id,
            RuntimeControlCommandRecord.command == command.value,
            RuntimeControlCommandRecord.status.in_(
                {
                    RuntimeControlCommandStatus.QUEUED.value,
                    RuntimeControlCommandStatus.PROCESSING.value,
                }
            ),
        )
        .order_by(RuntimeControlCommandRecord.created_at.desc())
    )


def _resolve_channel_pipeline_version(session: Session, channel: RuntimeChannel) -> PipelineVersion:
    if channel.pipeline_version_id:
        version = session.get(PipelineVersion, channel.pipeline_version_id)
        if version is not None:
            return version
    if channel.assigned_pipeline:
        definition = session.scalar(
            select(PipelineDefinition).where(PipelineDefinition.slug == channel.assigned_pipeline)
        )
        if definition is not None and definition.published_version_id:
            version = session.get(PipelineVersion, definition.published_version_id)
            if version is not None:
                return version
    raise ValueError("Channel has no resolvable published pipeline version.")


def _resolve_explicit_pipeline_version(
    session: Session,
    pipeline_version_id: str,
) -> PipelineVersion:
    version = session.get(PipelineVersion, pipeline_version_id)
    if version is None:
        raise ValueError("Pipeline version does not exist.")
    return version


def _executor_for_mode(mode: RuntimeMode) -> str:
    if mode == RuntimeMode.TRIGGER:
        return "trigger-executor"
    return "realtime-executor"


def _runtime_plan_summary(session: Session, loaded: LoadedRuntimePipeline) -> dict[str, Any]:
    return {
        "pipeline_version_id": loaded.version.id,
        "pipeline_definition_id": loaded.definition.id,
        "mode": loaded.channel.mode,
        "executor_service": _executor_for_mode(RuntimeMode(loaded.channel.mode)),
        "binding_signature": _runtime_binding_signature(session, loaded),
        "source_id": loaded.source.id if loaded.source is not None else None,
        "source_type": loaded.source.source_type if loaded.source is not None else None,
        "source_config": dict(loaded.source.config or {}) if loaded.source is not None else {},
        "source_secret_refs": dict(loaded.source.secret_refs or {})
        if loaded.source is not None
        else {},
        "node_count": len(loaded.graph.nodes),
        "edge_count": len(loaded.graph.edges),
        "nodes": [
            {
                "node_key": node.node_key,
                "node_type": node.node_type,
                "node_category": node.node_category.value,
            }
            for node in loaded.graph.nodes
        ],
    }


def _runtime_binding_signature(session: Session, loaded: LoadedRuntimePipeline) -> dict[str, Any]:
    return {
        "mode": loaded.channel.mode,
        "pipeline_version_id": loaded.version.id,
        "source_id": loaded.source.id if loaded.source is not None else None,
        "source_type": loaded.source.source_type if loaded.source is not None else None,
        "source_config": dict(loaded.source.config or {}) if loaded.source is not None else {},
        "source_secret_refs": dict(loaded.source.secret_refs or {})
        if loaded.source is not None
        else {},
        "model_bindings": _runtime_model_binding_signature(session, loaded),
    }


def _runtime_model_binding_signature(
    session: Session,
    loaded: LoadedRuntimePipeline,
) -> list[dict[str, Any]]:
    resolver = build_runtime_model_resolver(session)
    bindings: list[dict[str, Any]] = []
    for node in sorted(loaded.graph.nodes, key=lambda item: item.node_key):
        if node.node_category != RuntimeNodeCategory.INFERENCE:
            continue
        if node.logical_model_ref is None:
            bindings.append(
                {
                    "node_key": node.node_key,
                    "node_type": node.node_type,
                    "model_ref_missing": True,
                }
            )
            continue
        model_ref = node.logical_model_ref
        binding = resolver(model_ref)
        capability = dict(binding.capability or {})
        bindings.append(
            {
                "node_key": node.node_key,
                "node_type": node.node_type,
                "model_family_slug": model_ref.model_family_slug,
                "target_path": model_ref.target_path.value,
                "required_task_type": model_ref.required_task_type.value,
                "selection_policy": model_ref.selection_policy,
                "requested_deployment_release_id": model_ref.deployment_release_id,
                "deployment_release_id": binding.deployment_release_id,
                "deployment_target": binding.deployment_target,
                "model_version_id": binding.model_version_id,
                "task_type": binding.task_type.value,
                "contract_major": binding.contract_major,
                "deployment_status": capability.get("deployment_status"),
                "release_status": capability.get("release_status"),
                "release_activated_at": capability.get("release_activated_at"),
                "triton_model_name": capability.get("triton_model_name"),
                "triton_model_version": capability.get("triton_model_version"),
            }
        )
    return bindings


def _validate_runtime_source_config(
    source_type: RuntimeSourceType,
    source_config: dict[str, Any],
) -> None:
    forbidden_keys = {
        key
        for key in source_config
        if any(secret_hint in key.lower() for secret_hint in ("password", "secret", "token"))
    }
    if forbidden_keys:
        raise ValueError("Source config must not contain inline secrets.")
    if source_type == RuntimeSourceType.RTSP:
        raw_url = str(source_config.get("url") or "")
        if raw_url and not raw_url.lower().startswith(("rtsp://", "rtsps://")):
            raise ValueError("RTSP channels require an rtsp:// or rtsps:// URL.")
        if "://" in raw_url and "@" in raw_url.split("://", 1)[1]:
            raise ValueError("RTSP URLs must use secret_refs instead of inline credentials.")
        if not (
            source_config.get("url")
            or source_config.get("host")
            or source_config.get("secret_ref")
        ):
            raise ValueError("RTSP channels require a URL, host, or secret reference.")
    if source_type == RuntimeSourceType.USB_OPENCV:
        if (
            source_config.get("device_index") is None
            and not source_config.get("device_path")
        ):
            raise ValueError("USB/OpenCV channels require a device_index or device_path.")
        backend = str(source_config.get("backend") or "auto").strip().lower()
        if backend not in {"auto", "v4l2", "dshow", "msmf"}:
            raise ValueError("USB/OpenCV backend must be auto, v4l2, dshow, or msmf.")
    if source_type == RuntimeSourceType.FLIR_BLACKFLY:
        identifiers = (
            source_config.get("serial_number"),
            source_config.get("user_defined_name"),
            source_config.get("device_id"),
            source_config.get("id_"),
            source_config.get("ip_address"),
        )
        if not any(identifiers):
            raise ValueError(
                "FLIR Blackfly channels require a serial_number, user_defined_name, "
                "device_id, id_, or ip_address."
            )
    if source_type == RuntimeSourceType.BASLER_PYLON:
        identifiers = tuple(source_config.get(key) for key in _BASLER_IDENTIFIER_KEYS)
        if not any(identifiers):
            raise ValueError(
                "Basler Pylon channels require a camera_name, serial_number, "
                "user_defined_name, device_id, id_, ip_address, or model."
            )
        trigger_mode = str(source_config.get("trigger_mode") or "Off").strip().lower()
        if trigger_mode not in {"off", "false", "0"} and not source_config.get("trigger_source"):
            raise ValueError("Basler Pylon triggered acquisition requires a trigger_source.")
        _validate_basler_runtime_link_budget(source_config)
    if source_type == RuntimeSourceType.ARAVIS_GIGE:
        if not source_config.get("camera_name"):
            raise ValueError(
                "Aravis GigE channels require a camera_name, serial_number, "
                "user_defined_name, device_id, id_, or ip_address."
            )
        if not source_config.get("bridge_name"):
            raise ValueError("Aravis GigE channels require an internal bridge_name.")
        if not source_config.get("rtsp_port") or not source_config.get("rtsp_mount"):
            raise ValueError("Aravis GigE channels require an internal RTSP bridge endpoint.")
    if source_type == RuntimeSourceType.SNAPSHOT:
        raw_url = str(source_config.get("url") or source_config.get("snapshot_url") or "")
        if source_config.get("path"):
            raise ValueError("Snapshot channels must use HTTP(S) snapshot URLs, not server-local paths.")
        if raw_url and not raw_url.lower().startswith(("http://", "https://")):
            raise ValueError("Snapshot channels require an http:// or https:// URL.")
        if "://" in raw_url and "@" in raw_url.split("://", 1)[1]:
            raise ValueError("Snapshot URLs must not contain inline credentials.")
        if not raw_url:
            raise ValueError("Snapshot channels require an HTTP(S) snapshot URL.")
    if source_type == RuntimeSourceType.FILE_TEST and not source_config.get("path"):
        raise ValueError("File/test channels require a path.")


def _validate_basler_runtime_link_budget(source_config: dict[str, Any]) -> None:
    link_speed_mbps = source_config.get("link_speed_mbps")
    if link_speed_mbps in (None, ""):
        return
    try:
        width = int(source_config.get("width") or 0)
        height = int(source_config.get("height") or 0)
        fps = float(source_config.get("fps") or 0)
        link_speed = float(link_speed_mbps)
    except (TypeError, ValueError):
        return
    bits_per_pixel = {
        "mono8": 8,
        "bayerrg8": 8,
        "bayerbg8": 8,
        "bayergb8": 8,
        "bayergr8": 8,
        "yuv422packed": 16,
        "rgb8": 24,
        "bgr8": 24,
    }.get(str(source_config.get("pixel_format") or "").strip().casefold())
    if width <= 0 or height <= 0 or fps <= 0 or link_speed <= 0 or bits_per_pixel is None:
        return
    payload_mbps = width * height * fps * bits_per_pixel / 1_000_000
    if payload_mbps > link_speed * 0.8:
        raise ValueError(
            "Basler Pylon image payload exceeds 80% of the configured link_speed_mbps "
            f"({payload_mbps:.0f} Mbit/s payload on {link_speed:.0f} Mbit/s)."
        )


def _health_state_from_summary(payload: dict[str, Any] | None) -> HealthState:
    raw = (payload or {}).get("state")
    try:
        return HealthState(raw)
    except (TypeError, ValueError):
        return HealthState.OK


def _channel_performance_from_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "fps": payload.get("fps") or payload.get("processed_fps"),
        "source_fps": payload.get("source_fps"),
        "processed_fps": payload.get("processed_fps") or payload.get("fps"),
        "latency_ms": payload.get("latency_ms") or payload.get("last_trigger_latency_ms"),
        "latency_p50_ms": payload.get("latency_p50_ms"),
        "latency_p95_ms": payload.get("latency_p95_ms"),
        "latency_p99_ms": payload.get("latency_p99_ms"),
        "drop_rate_percent": payload.get("drop_rate_percent"),
        "frame_count": int(payload.get("frame_count") or 0),
        "drop_count": int(payload.get("drop_count") or payload.get("dropped_frame_count") or 0),
        "trigger_count": int(payload.get("trigger_count") or 0),
        "reconnect_count": int(payload.get("reconnect_count") or 0),
        "source_error_count": int(payload.get("source_error_count") or 0),
        "last_frame_at": payload.get("last_frame_at"),
    }


def _runtime_deployment_status(
    *,
    release: DeploymentRelease,
    target: DeploymentTarget | None,
    promotion: ModelPromotion | None,
) -> tuple[str, bool]:
    release_status = DeploymentReleaseStatus(release.status)
    if release_status == DeploymentReleaseStatus.RETIRED or release.retired_at is not None:
        return "retired", False
    if release_status == DeploymentReleaseStatus.ROLLED_BACK:
        return "rolled_back", False
    if release_status == DeploymentReleaseStatus.ACTIVATION_FAILED:
        return "failed", False
    if target is not None and target.active_release_id == release.id:
        return "production", release_status == DeploymentReleaseStatus.ACTIVATED
    if promotion is not None and promotion.status == PromotionStatus.STAGED.value:
        return "staged", release_status == DeploymentReleaseStatus.ACTIVATED
    if release_status == DeploymentReleaseStatus.ACTIVATED:
        return "activated", True
    return release_status.value, False


def _deployment_capability_payload(
    *,
    release: DeploymentRelease,
    version: ModelVersion,
    family: ModelFamily,
    deployment_status: str,
    selectable: bool,
) -> dict[str, Any]:
    serving_manifest = dict(version.serving_manifest or {})
    compatibility_report = dict(release.compatibility_report or {})
    smoke_report = dict(release.smoke_report or {})
    class_labels = serving_manifest.get("class_labels") or serving_manifest.get("class_map") or []
    if not class_labels:
        postprocessing = serving_manifest.get("postprocessing")
        if isinstance(postprocessing, dict):
            class_labels = postprocessing.get("class_map") or []
    max_batch_size = _target_max_batch_size(
        serving_manifest.get("hardware_compatibility"),
        release.target_path,
    )
    architecture = {
        "family": version.architecture_family or family.architecture_family,
        "version": version.architecture_version or family.architecture_version,
        "size": version.architecture_size or family.architecture_size,
        "display_name": (
            version.architecture_display_name
            or family.architecture_display_name
            or family.slug
        ),
    }
    display_name = family.display_name or family.slug
    anomaly_payload = {}
    if family.task_type == TaskType.ANOMALY.value:
        anomaly_payload = dict(serving_manifest.get("anomaly") or {})
        postprocessing = serving_manifest.get("postprocessing") or {}
        anomaly_payload.setdefault("backend", "anomalib")
        anomaly_payload.setdefault("method", postprocessing.get("anomaly_method") or "patchcore")
        anomaly_payload.setdefault("heatmap_support", bool(postprocessing.get("heatmap_support", True)))
        anomaly_payload.setdefault("score_output_support", bool(postprocessing.get("score_output_support", True)))
        anomaly_payload.setdefault("thresholding_support", bool(postprocessing.get("thresholding_support", True)))
        anomaly_payload.setdefault("mask_support", bool(postprocessing.get("mask_support", False)))
        anomaly_payload.setdefault(
            "threshold",
            postprocessing.get("threshold")
            or postprocessing.get("confidence_threshold_default")
            or serving_manifest.get("confidence_threshold_default")
            or 0.5,
        )
    return {
        "source_of_truth": "deployment_releases",
        "display_name": display_name,
        "model_family_display_name": display_name,
        "version_label": f"v{version.version_no}",
        "deployment_status": deployment_status,
        "active": selectable,
        "model_lifecycle_status": version.status,
        "release_status": release.status,
        "release_activated_at": _iso(release.activated_at),
        "release_retired_at": _iso(release.retired_at),
        "triton_model_name": release.triton_model_name,
        "triton_model_version": release.triton_model_version,
        "architecture": architecture,
        "input_schema_ref": family.input_schema_ref,
        "output_schema_ref": family.output_schema_ref,
        "class_labels": class_labels if isinstance(class_labels, list) else [],
        "configured_class_names": list(getattr(family, "class_names", None) or []),
        "uses_all_project_classes": not bool(getattr(family, "class_names", None)),
        "segmentation_mode": getattr(family, "segmentation_mode", None),
        "class_map_version": serving_manifest.get("class_map_version"),
        "compatible_targets": serving_manifest.get("compatible_targets") or [release.target_path],
        "max_batch_size": max_batch_size,
        "confidence_threshold_default": serving_manifest.get(
            "confidence_threshold_default",
            0.35,
        ),
        "trainer_backend": serving_manifest.get("trainer_backend"),
        "preprocessing": serving_manifest.get("preprocessing") or {},
        "postprocessing": serving_manifest.get("postprocessing") or {},
        "capabilities": {
            "score_output": family.task_type == TaskType.ANOMALY.value,
            "heatmap_overlay": bool(anomaly_payload.get("heatmap_support", False)),
            "thresholding": bool(anomaly_payload.get("thresholding_support", False)),
            "mask_output": bool(anomaly_payload.get("mask_support", False)),
        }
        if family.task_type == TaskType.ANOMALY.value
        else {},
        "anomaly": anomaly_payload,
        "smoke_test_passed": bool(smoke_report.get("passed", False)) if smoke_report else None,
        "compatible": bool(compatibility_report.get("compatible", True)),
    }


def _target_max_batch_size(raw: object, target_path: str) -> int:
    if isinstance(raw, dict):
        value = raw.get("max_batch_size")
        if isinstance(value, dict):
            value = value.get(target_path)
        if isinstance(value, int) and value > 0:
            return value
    return 1 if target_path == TargetPath.REALTIME.value else 64


def _set_trigger_endpoint_status(
    session: Session,
    *,
    channel_id: str,
    status: RuntimeResourceStatus | None,
) -> None:
    if status is None:
        return
    for endpoint in session.scalars(
        select(TriggerEndpoint).where(TriggerEndpoint.channel_id == channel_id)
    ).all():
        endpoint.status = (
            RuntimeResourceStatus.DISABLED.value
            if not bool(getattr(endpoint, "enabled", True))
            else status.value
        )
        endpoint.updated_at = utc_now()


def _ensure_trigger_endpoint_for_channel(session: Session, channel: RuntimeChannel) -> None:
    existing = session.scalar(
        select(TriggerEndpoint).where(TriggerEndpoint.channel_id == channel.id)
    )
    if existing is not None:
        return
    endpoint = TriggerEndpoint(
        id=generate_prefixed_id("tep"),
        channel_id=channel.id,
        pipeline_version_id=channel.pipeline_version_id,
        display_name=f"{channel.name} trigger",
        slug=f"{channel.slug}-trigger",
        method="PUT",
        enabled=True,
        auth_required=False,
        auth_scope="runtime.trigger.invoke",
        timeout_ms=2500,
        rate_limit_per_minute=60,
        burst_limit=4,
        response_policy={},
        request_schema={},
        response_schema={},
        status=RuntimeResourceStatus.DEGRADED.value,
        created_by="runtime-orchestrator",
        updated_by="runtime-orchestrator",
    )
    session.add(endpoint)


def _handler_refs(session: Session) -> dict[str, str]:
    rows = session.scalars(
        select(RuntimeNodeTypeRegistry).where(RuntimeNodeTypeRegistry.active.is_(True))
    ).all()
    return {row.node_type: row.runtime_handler_ref for row in rows}


def runtime_model_capability_is_selectable(capability: dict[str, Any]) -> bool:
    if capability.get("active") is False:
        return False
    status = str(capability.get("deployment_status") or "available").casefold()
    return status in {"production", "staged", "activated", "active", "testing", "test"}


def runtime_model_capability_is_selectable_for_policy(
    capability: dict[str, Any],
    selection_policy: str,
) -> bool:
    if not runtime_model_capability_is_selectable(capability):
        return False
    status = str(capability.get("deployment_status") or "").casefold()
    if selection_policy == "active_production":
        # Only the release explicitly selected by the deployment target is
        # production. Other activated releases remain selectable when pinned,
        # but must not be followed by an active-production binding.
        return status == "production"
    if selection_policy == "staged_test":
        return status in {"staged", "testing", "test", "production", "activated", "active"}
    return status in {"production", "staged", "activated", "active", "testing", "test"}


def runtime_model_capability_rank(
    capability: dict[str, Any],
    selection_policy: str,
) -> tuple[int, int, int, float, str]:
    status = str(capability.get("deployment_status") or "available").casefold()
    if selection_policy == "staged_test":
        preferred = {"staged", "testing", "test"}
        status_order = {
            "staged": 0,
            "testing": 0,
            "test": 0,
            "production": 1,
            "active": 1,
            "activated": 1,
        }
    else:
        preferred = {"production", "activated", "active"}
        status_order = {
            "production": 0,
            "active": 0,
            "activated": 1,
            "staged": 2,
            "testing": 2,
            "test": 2,
        }
    return (
        0 if status in preferred else 1,
        status_order.get(status, 9),
        _rank_int_desc(capability.get("triton_model_version")),
        _rank_datetime_desc(capability.get("release_activated_at")),
        status,
    )


def _rank_int_desc(value: object) -> int:
    try:
        return -int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _rank_datetime_desc(value: object) -> float:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return 0.0
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return -parsed.timestamp()


def _runtime_model_capability_unavailable_detail(
    session: Session,
    *,
    model_ref: RuntimeLogicalModelRef,
    now: datetime,
    candidate_rows: list[RuntimeModelCapabilityCache],
) -> str:
    parts = [
        "No available runtime model capability matches "
        f"{model_ref.model_family_slug}/{model_ref.target_path.value} "
        f"for {model_ref.selection_policy}."
    ]
    if candidate_rows:
        parts.append(
            "Candidates on requested target: "
            f"{_runtime_model_capability_rows_summary(candidate_rows)}."
        )
        if model_ref.selection_policy == "explicit_release" and model_ref.deployment_release_id:
            parts.append(
                f"Requested deployment_release_id={model_ref.deployment_release_id}."
            )
        return " ".join(parts)

    alternate_rows = session.scalars(
        select(RuntimeModelCapabilityCache).where(
            RuntimeModelCapabilityCache.model_family_slug == model_ref.model_family_slug,
            RuntimeModelCapabilityCache.contract_major == model_ref.contract_major,
            RuntimeModelCapabilityCache.expires_at > now,
        )
    ).all()
    if alternate_rows:
        parts.append(
            "Candidates on other targets: "
            f"{_runtime_model_capability_rows_summary(alternate_rows)}."
        )
        return " ".join(parts)

    family = session.scalar(
        select(ModelFamily)
        .where(ModelFamily.slug == model_ref.model_family_slug)
        .order_by(ModelFamily.created_at.desc())
    )
    if family is None:
        parts.append("No model family with this slug exists in the registry.")
        return " ".join(parts)

    parts.append(
        "Model family exists "
        f"(id={family.id}, task_type={family.task_type}, "
        f"contract_major={family.contract_major}, active={bool(family.active)}, "
        f"deleted={family.deleted_at is not None})."
    )
    releases = session.scalars(
        select(DeploymentRelease)
        .where(DeploymentRelease.model_family_id == family.id)
        .order_by(DeploymentRelease.created_at.desc())
    ).all()
    if releases:
        parts.append(f"Deployment releases: {_deployment_release_rows_summary(releases)}.")
    else:
        parts.append("No deployment release exists for this model family.")
    return " ".join(parts)


def _runtime_model_capability_rows_summary(
    rows: list[RuntimeModelCapabilityCache],
    *,
    limit: int = 5,
) -> str:
    items: list[str] = []
    for row in rows[:limit]:
        capability = dict(row.capability or {})
        items.append(
            (
                f"{row.deployment_release_id}"
                f"(target={row.target_path}, "
                f"deployment_status={capability.get('deployment_status') or 'unknown'}, "
                f"active={capability.get('active', True)}, "
                f"release_status={capability.get('release_status') or 'unknown'}, "
                f"task_type={row.task_type}, "
                f"contract_major={row.contract_major})"
            )
        )
    if len(rows) > limit:
        items.append(f"... +{len(rows) - limit} more")
    return ", ".join(items)


def _deployment_release_rows_summary(
    rows: list[DeploymentRelease],
    *,
    limit: int = 5,
) -> str:
    items = [
        (
            f"{row.id}"
            f"(target={row.target_path}, status={row.status}, "
            f"retired={row.retired_at is not None}, deleted={row.deleted_at is not None})"
        )
        for row in rows[:limit]
    ]
    if len(rows) > limit:
        items.append(f"... +{len(rows) - limit} more")
    return ", ".join(items)


def _iso(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)


def _aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
