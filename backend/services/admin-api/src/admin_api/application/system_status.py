from __future__ import annotations

import asyncio

from cv_contracts import ResponseMeta, StatusCard, SystemStatusResponse
from cv_core import PlatformSettings
from cv_db import build_runtime_operational_summary, list_deployment_targets, session_scope
from cv_status import HealthState
from cv_utils import get_correlation_id, get_request_id, utc_now_iso

from admin_api.application.operational_probes import (
    build_operational_alerts,
    probe_dependencies,
    probe_services,
    read_disk_usage,
    read_gpu_status,
    read_queue_metrics,
    read_worker_statuses,
)


async def build_system_status(settings: PlatformSettings) -> SystemStatusResponse:
    services, dependencies = await asyncio.gather(
        probe_services(settings),
        probe_dependencies(settings),
    )
    try:
        queues = await asyncio.to_thread(read_queue_metrics, settings)
    except Exception:
        queues = []
    try:
        workers = await asyncio.to_thread(read_worker_statuses, settings)
    except Exception:
        workers = []
    disks = await asyncio.to_thread(read_disk_usage, settings)
    gpus = await asyncio.to_thread(read_gpu_status, settings)
    try:
        deployment_targets = await asyncio.to_thread(_read_deployment_targets, settings)
    except Exception:
        deployment_targets = []
    try:
        runtime = await asyncio.to_thread(_read_runtime_status, settings)
    except Exception:
        runtime = None
    active_realtime = next(
        (
            target.active_release_id
            for target in deployment_targets
            if getattr(target.target_path, "value", target.target_path) == "realtime"
        ),
        None,
    )
    active_batch = next(
        (
            target.active_release_id
            for target in deployment_targets
            if getattr(target.target_path, "value", target.target_path) == "batch"
        ),
        None,
    )
    alerts = await asyncio.to_thread(
        build_operational_alerts,
        settings=settings,
        services=services,
        dependencies=dependencies,
        workers=workers,
        queues=queues,
        disks=disks,
        gpus=gpus,
        realtime_release_id=active_realtime,
        batch_release_id=active_batch,
        runtime=runtime,
    )
    cards = [
        StatusCard(key="env", label="Environment", value=settings.env.value),
        StatusCard(key="storage_root", label="State Root", value=settings.storage.state_root),
        StatusCard(
            key="active_rt_release",
            label="Realtime Release",
            value=active_realtime or "none",
            state=HealthState.OK if active_realtime else HealthState.DOWN,
        ),
        StatusCard(
            key="active_batch_release",
            label="Batch Release",
            value=active_batch or "none",
            state=HealthState.OK if active_batch else HealthState.DEGRADED,
        ),
        StatusCard(
            key="alerts_total",
            label="Open Alerts",
            value=str(len(alerts)),
            state=HealthState.OK if not alerts else HealthState.DEGRADED,
        ),
        StatusCard(
            key="services_down",
            label="Services Down",
            value=str(sum(1 for item in services if item.state == HealthState.DOWN)),
            state=HealthState.OK
            if all(item.state != HealthState.DOWN for item in services)
            else HealthState.DOWN,
        ),
        StatusCard(
            key="workers_down",
            label="Workers Down",
            value=str(sum(1 for item in workers if item.state == HealthState.DOWN)),
            state=HealthState.OK
            if all(item.state != HealthState.DOWN for item in workers)
            else HealthState.DOWN,
        ),
        StatusCard(
            key="queue_backlog",
            label="Queued Items",
            value=str(sum(item.depth for item in queues)),
            state=HealthState.OK
            if not any(item.depth for item in queues)
            else HealthState.DEGRADED,
        ),
        StatusCard(
            key="runtime_channels",
            label="Runtime Channels",
            value=str(runtime.total_channels if runtime is not None else 0),
            state=runtime.state if runtime is not None else HealthState.DEGRADED,
        ),
        StatusCard(
            key="runtime_attention",
            label="Runtime Attention",
            value=str(
                (
                    runtime.degraded_channels
                    + runtime.failed_channels
                    + runtime.stale_runtime_instances
                )
                if runtime is not None
                else 0
            ),
            state=HealthState.OK
            if runtime is not None
            and runtime.degraded_channels == 0
            and runtime.failed_channels == 0
            and runtime.stale_runtime_instances == 0
            else HealthState.DEGRADED,
        ),
        StatusCard(
            key="runtime_fps",
            label="Runtime FPS",
            value=(
                f"{runtime.performance.realtime_fps:.1f}"
                if runtime is not None and runtime.performance.realtime_fps is not None
                else "n/a"
            ),
            state=HealthState.OK if runtime is not None else HealthState.DEGRADED,
        ),
        StatusCard(
            key="runtime_trigger_p95",
            label="Trigger p95",
            value=(
                f"{runtime.performance.trigger_latency_p95_ms:.0f} ms"
                if runtime is not None
                and runtime.performance.trigger_latency_p95_ms is not None
                else "n/a"
            ),
            state=HealthState.OK if runtime is not None else HealthState.DEGRADED,
        ),
    ]
    return SystemStatusResponse(
        services=services,
        dependencies=dependencies,
        workers=workers,
        queues=queues,
        disks=disks,
        gpus=gpus,
        runtime=runtime or _empty_runtime_status(),
        alerts=alerts,
        cards=cards,
        meta=ResponseMeta(
            request_id=get_request_id(),
            correlation_id=get_correlation_id(),
            generated_at=utc_now_iso(),
        ),
    )


def _read_deployment_targets(settings: PlatformSettings):
    with session_scope(settings) as session:
        return list_deployment_targets(session)


def _read_runtime_status(settings: PlatformSettings):
    with session_scope(settings) as session:
        return build_runtime_operational_summary(session, settings)


def _empty_runtime_status():
    from cv_contracts import RuntimeOperationalSummary

    return RuntimeOperationalSummary(
        state=HealthState.DEGRADED,
        observed_at=utc_now_iso(),
        recovery_hint="Runtime status aggregation failed; check admin-api database access.",
    )
