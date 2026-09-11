from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from cv_contracts import (
    RuntimeChannelOperationalSummary,
    RuntimeOperationalSummary,
    RuntimePerformanceSnapshot,
)
from cv_core import PlatformSettings
from cv_status import (
    HealthState,
    RuntimeChannelDesiredState,
    RuntimeExecutionStatus,
    RuntimeMode,
    RuntimeObservedState,
)
from cv_utils import utc_now, utc_now_iso
from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_db.models import ResultRecord, RuntimeChannel, RuntimeExecution, TriggerEndpoint

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

TERMINAL_RUNTIME_EXECUTION_STATUSES = {
    RuntimeExecutionStatus.STOPPED.value,
    RuntimeExecutionStatus.FAILED_TERMINAL.value,
    RuntimeExecutionStatus.RECOVERED.value,
}


def build_runtime_operational_summary(
    session: Session,
    settings: PlatformSettings,
) -> RuntimeOperationalSummary:
    now = utc_now()
    channels = session.scalars(
        select(RuntimeChannel)
        .where(RuntimeChannel.deleted_at.is_(None))
        .order_by(RuntimeChannel.technical_place.asc(), RuntimeChannel.name.asc())
    ).all()
    channel_ids = {channel.id for channel in channels}
    executions = (
        session.scalars(
            select(RuntimeExecution)
            .where(RuntimeExecution.channel_id.in_(channel_ids))
            .order_by(RuntimeExecution.created_at.desc())
        ).all()
        if channel_ids
        else []
    )
    endpoints_by_channel: dict[str, TriggerEndpoint] = {}
    for endpoint in session.scalars(
        select(TriggerEndpoint).order_by(TriggerEndpoint.updated_at.desc())
    ).all():
        endpoints_by_channel.setdefault(endpoint.channel_id, endpoint)
    executions_by_id = {execution.id: execution for execution in executions}
    latest_execution_by_channel: dict[str, RuntimeExecution] = {}
    for execution in executions:
        latest_execution_by_channel.setdefault(execution.channel_id, execution)
    active_executions = [
        execution
        for execution in executions
        if execution.status in ACTIVE_RUNTIME_EXECUTION_STATUSES
    ]
    trigger_latencies = _recent_trigger_latencies(session)
    executor_summaries = [
        _channel_operational_summary(
            channel,
            execution=_current_execution(
                channel,
                executions_by_id=executions_by_id,
                latest_execution_by_channel=latest_execution_by_channel,
            ),
            endpoint=endpoints_by_channel.get(channel.id),
            settings=settings,
            now=now,
        )
        for channel in channels
    ]
    attention = [
        item
        for item in executor_summaries
        if _needs_attention(item, settings=settings)
    ]
    state = _runtime_state(attention)
    status_counts = Counter(channel.observed_state for channel in channels)
    mode_active_counts = Counter(
        execution.mode
        for execution in active_executions
        if execution.actual_state == RuntimeObservedState.RUNNING.value
    )
    failed_instances = [item for item in executor_summaries if _is_failed_instance(item)]
    stale_instances = [
        item
        for item in executor_summaries
        if (item.heartbeat_age_seconds or 0) >= settings.runtime.executor_stale_after_seconds
        and item.observed_state != RuntimeObservedState.STOPPED
    ]
    return RuntimeOperationalSummary(
        state=state,
        total_channels=len(channels),
        running_channels=status_counts[RuntimeObservedState.RUNNING.value],
        degraded_channels=status_counts[RuntimeObservedState.DEGRADED.value],
        failed_channels=status_counts[RuntimeObservedState.FAILED.value],
        stopped_channels=status_counts[RuntimeObservedState.STOPPED.value],
        realtime_active_channels=mode_active_counts[RuntimeMode.REALTIME.value],
        trigger_active_channels=mode_active_counts[RuntimeMode.TRIGGER.value],
        active_runtime_instances=len(active_executions),
        stale_runtime_instances=len(stale_instances),
        failed_runtime_instances=len(failed_instances),
        recovering_channels=status_counts[RuntimeObservedState.RECOVERING.value],
        performance=_runtime_performance(executor_summaries, trigger_latencies),
        attention=attention[:12],
        executors=executor_summaries,
        recovery_hint=_runtime_recovery_hint(attention, stale_instances),
        observed_at=utc_now_iso(),
    )


def cleanup_stale_runtime_references(session: Session) -> int:
    """Clear dangling active-runtime pointers that no longer represent active work."""
    changed = 0
    for channel in session.scalars(
        select(RuntimeChannel).where(RuntimeChannel.active_runtime_execution_id.is_not(None))
    ).all():
        execution = session.get(RuntimeExecution, channel.active_runtime_execution_id)
        if execution is None:
            channel.active_runtime_execution_id = None
            channel.health_summary = {
                **(channel.health_summary or {}),
                "state": HealthState.DEGRADED.value,
                "status_text": "Runtime reference was cleaned up",
                "recovery_hint": "Start the channel again to create a new runtime instance.",
            }
            channel.health_state = HealthState.DEGRADED.value
            changed += 1
            continue
        if execution.status in {
            RuntimeExecutionStatus.STOPPED.value,
            RuntimeExecutionStatus.FAILED_TERMINAL.value,
            RuntimeExecutionStatus.RECOVERED.value,
        }:
            channel.active_runtime_execution_id = None
            if channel.desired_state != RuntimeChannelDesiredState.RUNNING.value:
                channel.observed_state = RuntimeObservedState.STOPPED.value
                channel.health_state = HealthState.DOWN.value
                channel.health_summary = {
                    "state": HealthState.DOWN.value,
                    "status_text": "Stopped",
                    "recovery_hint": "Start the channel when live execution is required.",
                }
            changed += 1
    if changed:
        session.flush()
    return changed


def runtime_prometheus_metrics_text(session: Session, settings: PlatformSettings) -> str:
    summary = build_runtime_operational_summary(session, settings)
    lines = [
        "# HELP cv_runtime_channels_total Runtime channels by mode, observed state, and health.",
        "# TYPE cv_runtime_channels_total gauge",
    ]
    channel_counts: Counter[tuple[str, str, str]] = Counter(
        (item.mode.value, item.observed_state.value, item.health_state.value)
        for item in summary.executors
    )
    for (mode, observed_state, health_state), count in sorted(channel_counts.items()):
        lines.append(
            "cv_runtime_channels_total"
            f'{{mode="{_label(mode)}",observed_state="{_label(observed_state)}",'
            f'health_state="{_label(health_state)}"}} {count}'
        )
    lines.extend(
        [
            "# HELP cv_runtime_active_instances Runtime execution instances that are not terminal.",
            "# TYPE cv_runtime_active_instances gauge",
            f"cv_runtime_active_instances {summary.active_runtime_instances}",
            "# HELP cv_runtime_stale_instances Runtime execution instances with stale heartbeats.",
            "# TYPE cv_runtime_stale_instances gauge",
            f"cv_runtime_stale_instances {summary.stale_runtime_instances}",
            "# HELP cv_runtime_failed_instances Runtime execution instances marked failed.",
            "# TYPE cv_runtime_failed_instances gauge",
            f"cv_runtime_failed_instances {summary.failed_runtime_instances}",
            "# HELP cv_runtime_trigger_timeouts Trigger timeout indicators in current summary.",
            "# TYPE cv_runtime_trigger_timeouts gauge",
            f"cv_runtime_trigger_timeouts {summary.performance.trigger_timeout_count}",
        ]
    )
    if summary.performance.trigger_latency_p95_ms is not None:
        lines.extend(
            [
                "# HELP cv_runtime_trigger_latency_p95_ms Recent trigger execution p95 latency.",
                "# TYPE cv_runtime_trigger_latency_p95_ms gauge",
                (
                    "cv_runtime_trigger_latency_p95_ms "
                    f"{summary.performance.trigger_latency_p95_ms}"
                ),
            ]
        )
    lines.extend(
        [
            "# HELP cv_runtime_realtime_fps Current processed FPS per realtime channel.",
            "# TYPE cv_runtime_realtime_fps gauge",
        ]
    )
    for item in summary.executors:
        labels = _channel_labels(item)
        if item.fps is not None:
            lines.append(f"cv_runtime_realtime_fps{{{labels}}} {item.fps}")
        if item.latency_p95_ms is not None:
            lines.append(f"cv_runtime_latency_p95_ms{{{labels}}} {item.latency_p95_ms}")
        if item.drop_rate_percent is not None:
            lines.append(f"cv_runtime_drop_rate_percent{{{labels}}} {item.drop_rate_percent}")
        if item.heartbeat_age_seconds is not None:
            lines.append(
                "cv_runtime_executor_heartbeat_age_seconds"
                f"{{{labels}}} {item.heartbeat_age_seconds}"
            )
        lines.append(f"cv_runtime_frames_total{{{labels}}} {item.frame_count}")
        lines.append(f"cv_runtime_drops_total{{{labels}}} {item.drop_count}")
        lines.append(f"cv_runtime_triggers_total{{{labels}}} {item.trigger_count}")
        lines.append(f"cv_runtime_recovery_attempts_total{{{labels}}} {item.recovery_attempts}")
        if item.trigger_latency_ms is not None:
            lines.append(
                f"cv_runtime_trigger_last_latency_ms{{{labels}}} {item.trigger_latency_ms}"
            )
    lines.append("")
    return "\n".join(lines)


def _channel_operational_summary(
    channel: RuntimeChannel,
    *,
    execution: RuntimeExecution | None,
    endpoint: TriggerEndpoint | None,
    settings: PlatformSettings,
    now: datetime,
) -> RuntimeChannelOperationalSummary:
    metrics = dict(channel.performance_summary or {})
    health = dict(channel.health_summary or {})
    runtime_instance_id = None
    executor_service = None
    assigned_runtime_worker = None
    heartbeat_at: datetime | None = None
    heartbeat_age_seconds = None
    recovery_attempts = 0
    observed_state = RuntimeObservedState(channel.observed_state)
    health_state = HealthState(channel.health_state)
    last_error = channel.last_error
    if execution is not None:
        metrics = {**metrics, **dict(execution.metrics_summary or {})}
        health = {**health, **dict(execution.health_summary or {})}
        runtime_instance_id = execution.id
        executor_service = execution.executor_service
        assigned_runtime_worker = (
            execution.assigned_runtime_worker or execution.executor_instance_id
        )
        heartbeat_at = _aware(execution.heartbeat_at)
        if heartbeat_at is not None:
            heartbeat_age_seconds = max(int((now - heartbeat_at).total_seconds()), 0)
        recovery_attempts = int(execution.recovery_attempts or 0)
        observed_state = RuntimeObservedState(execution.actual_state or channel.observed_state)
        reported_health = _health_state(health.get("state"))
        if reported_health is not None:
            health_state = reported_health
        last_error = execution.last_error or last_error
    recovery_hint = _recovery_hint(
        channel=channel,
        execution=execution,
        health=health,
        heartbeat_age_seconds=heartbeat_age_seconds,
        settings=settings,
    )
    health_text = str(health.get("status_text") or _status_text(observed_state))
    trigger_latency_ms = _trigger_latency_for_channel(channel.id, metrics)
    if trigger_latency_ms is None and endpoint is not None:
        trigger_latency_ms = endpoint.last_latency_ms
    trigger_count = int(
        metrics.get("trigger_count") or getattr(execution, "trigger_count", 0) or 0
    )
    if endpoint is not None:
        trigger_count = max(trigger_count, int(endpoint.invocation_count or 0))
    return RuntimeChannelOperationalSummary(
        channel_id=channel.id,
        name=channel.name,
        technical_place=channel.technical_place,
        mode=RuntimeMode(channel.mode),
        observed_state=observed_state,
        health_state=health_state,
        health_text=health_text,
        runtime_instance_id=runtime_instance_id,
        executor_service=executor_service,
        assigned_runtime_worker=assigned_runtime_worker,
        last_heartbeat_at=_iso(heartbeat_at),
        heartbeat_age_seconds=heartbeat_age_seconds,
        recovery_attempts=recovery_attempts,
        recovery_hint=recovery_hint,
        fps=_float(metrics.get("processed_fps") or metrics.get("fps")),
        latency_p95_ms=_float(metrics.get("latency_p95_ms") or metrics.get("latency_ms")),
        drop_rate_percent=_float(metrics.get("drop_rate_percent")),
        trigger_latency_ms=_float(trigger_latency_ms),
        frame_count=int(metrics.get("frame_count") or getattr(execution, "frame_count", 0) or 0),
        drop_count=int(metrics.get("drop_count") or getattr(execution, "drop_count", 0) or 0),
        trigger_count=trigger_count,
        last_error=last_error
        or (endpoint.last_error_message if endpoint is not None else None),
    )


def _current_execution(
    channel: RuntimeChannel,
    *,
    executions_by_id: dict[str, RuntimeExecution],
    latest_execution_by_channel: dict[str, RuntimeExecution],
) -> RuntimeExecution | None:
    if channel.active_runtime_execution_id:
        active_execution = executions_by_id.get(channel.active_runtime_execution_id)
        if active_execution is not None:
            return active_execution
    return latest_execution_by_channel.get(channel.id)


def _runtime_performance(
    executors: list[RuntimeChannelOperationalSummary],
    trigger_latencies: list[float],
) -> RuntimePerformanceSnapshot:
    total_frames = sum(item.frame_count for item in executors)
    total_drops = sum(item.drop_count for item in executors)
    realtime_latencies = [
        item.latency_p95_ms
        for item in executors
        if item.mode == RuntimeMode.REALTIME and item.latency_p95_ms is not None
    ]
    drop_rate = None
    if total_frames + total_drops:
        drop_rate = round((total_drops / (total_frames + total_drops)) * 100, 3)
    return RuntimePerformanceSnapshot(
        realtime_fps=round(sum(item.fps or 0 for item in executors), 3),
        realtime_latency_p95_ms=max(realtime_latencies) if realtime_latencies else None,
        realtime_drop_rate_percent=drop_rate,
        trigger_latency_p95_ms=_percentile(trigger_latencies, 95),
        trigger_timeout_count=sum(
            1
            for item in executors
            if item.mode == RuntimeMode.TRIGGER
            and item.last_error
            and "timeout" in item.last_error.lower()
        ),
        frame_count=total_frames,
        drop_count=total_drops,
        trigger_count=sum(item.trigger_count for item in executors),
    )


def _needs_attention(
    item: RuntimeChannelOperationalSummary,
    *,
    settings: PlatformSettings,
) -> bool:
    if item.observed_state in {
        RuntimeObservedState.DEGRADED,
        RuntimeObservedState.FAILED,
        RuntimeObservedState.RECOVERING,
    }:
        return True
    if item.observed_state == RuntimeObservedState.STOPPED:
        return item.last_error is not None
    if (
        item.heartbeat_age_seconds is not None
        and item.heartbeat_age_seconds >= settings.runtime.executor_stale_after_seconds
    ):
        return True
    return item.health_state != HealthState.OK


def _is_failed_instance(item: RuntimeChannelOperationalSummary) -> bool:
    if item.observed_state == RuntimeObservedState.FAILED:
        return True
    return (
        item.health_state == HealthState.DOWN
        and item.observed_state != RuntimeObservedState.STOPPED
    )


def _runtime_state(attention: list[RuntimeChannelOperationalSummary]) -> HealthState:
    has_failed_runtime = any(
        item.health_state == HealthState.DOWN
        or item.observed_state == RuntimeObservedState.FAILED
        for item in attention
    )
    if has_failed_runtime:
        return HealthState.DOWN
    if attention:
        return HealthState.DEGRADED
    return HealthState.OK


def _runtime_recovery_hint(
    attention: list[RuntimeChannelOperationalSummary],
    stale_instances: list[RuntimeChannelOperationalSummary],
) -> str | None:
    if stale_instances:
        return "Restart or reconcile stale channels, then confirm executor heartbeats refresh."
    if any(item.observed_state == RuntimeObservedState.FAILED for item in attention):
        return (
            "Review the channel error summary and restart only after the source and "
            "pipeline issue is corrected."
        )
    if attention:
        return (
            "Open the affected channel detail and follow the remediation hint shown "
            "on the runtime card."
        )
    return None


def _recovery_hint(
    *,
    channel: RuntimeChannel,
    execution: RuntimeExecution | None,
    health: dict[str, Any],
    heartbeat_age_seconds: int | None,
    settings: PlatformSettings,
) -> str | None:
    stored = health.get("recovery_hint")
    if isinstance(stored, str) and stored:
        return stored
    if (
        heartbeat_age_seconds is not None
        and heartbeat_age_seconds >= settings.runtime.executor_stale_after_seconds
    ):
        return (
            "Executor heartbeat is stale. Use Restart if the source is healthy, "
            "or Stop to clear the runtime before reconfiguration."
        )
    if execution is not None and execution.actual_state == RuntimeObservedState.FAILED.value:
        return (
            "Inspect the last error, verify the source and model route, then request "
            "Restart from the channel detail page."
        )
    if channel.observed_state == RuntimeObservedState.DEGRADED.value:
        return (
            "Check source connectivity, drop rate, and latest runtime result errors "
            "before restarting."
        )
    if channel.observed_state == RuntimeObservedState.STOPPED.value:
        return "Start the channel when runtime execution is required."
    return None


def _recent_trigger_latencies(session: Session, *, limit: int = 200) -> list[float]:
    rows = session.scalars(
        select(ResultRecord)
        .where(ResultRecord.mode == RuntimeMode.TRIGGER.value)
        .order_by(ResultRecord.completed_at.desc())
        .limit(limit)
    ).all()
    values: list[float] = []
    for row in rows:
        timing = row.timing or {}
        value = timing.get("total_ms") or timing.get("latency_ms")
        parsed = _float(value)
        if parsed is not None:
            values.append(parsed)
    return values


def _trigger_latency_for_channel(channel_id: str, metrics: dict[str, Any]) -> object:
    if metrics.get("trigger_latency_ms") is not None:
        return metrics.get("trigger_latency_ms")
    if metrics.get("last_trigger_latency_ms") is not None:
        return metrics.get("last_trigger_latency_ms")
    endpoint_latency = metrics.get("last_latency_ms")
    if endpoint_latency is not None:
        return endpoint_latency
    return None


def _status_text(state: RuntimeObservedState) -> str:
    return {
        RuntimeObservedState.UNASSIGNED: "Needs setup",
        RuntimeObservedState.STARTING: "Starting",
        RuntimeObservedState.VALIDATING: "Validating",
        RuntimeObservedState.LOADING_PIPELINE: "Loading pipeline",
        RuntimeObservedState.RUNNING: "Running",
        RuntimeObservedState.DEGRADED: "Needs attention",
        RuntimeObservedState.STOPPING: "Stopping",
        RuntimeObservedState.STOPPED: "Stopped",
        RuntimeObservedState.FAILED: "Failed",
        RuntimeObservedState.RECOVERING: "Recovering",
    }.get(state, "Unknown")


def _health_state(value: object) -> HealthState | None:
    try:
        return HealthState(value)
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = min(
        max(round((percentile / 100) * (len(ordered) - 1)), 0),
        len(ordered) - 1,
    )
    return round(ordered[index], 3)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _iso(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)


def _channel_labels(item: RuntimeChannelOperationalSummary) -> str:
    return ",".join(
        [
            f'channel_id="{_label(item.channel_id)}"',
            f'mode="{_label(item.mode.value)}"',
            f'observed_state="{_label(item.observed_state.value)}"',
            f'health_state="{_label(item.health_state.value)}"',
            f'executor_service="{_label(item.executor_service or "none")}"',
        ]
    )


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
