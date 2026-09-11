from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
import urllib.error
import urllib.request
from redis import Redis
from rq import Queue
from sqlalchemy import text

from cv_contracts import (
    DiskUsageSummary,
    GpuDeviceSummary,
    IntegrationStatusSummary,
    QueueStatus,
    RuntimeOperationalSummary,
    ServiceSummary,
    SystemAlert,
    WorkerStatusSummary,
)
from cv_core import PlatformSettings
from cv_db import list_service_heartbeats, session_scope
from cv_db.models import DeploymentRelease, DeploymentTarget, ModelVersion
from cv_deploy import TritonRepositoryManager
from cv_status import HealthState, QueueName, TargetPath
from cv_utils import utc_now_iso

SERVICE_HEALTH_TARGETS: dict[str, str] = {
    "dataset-service": "http://dataset-service:8082/health/ready",
    "training-orchestrator": "http://training-orchestrator:8083/health/ready",
    "model-registry-service": "http://model-registry-service:8084/health/ready",
    "deployment-controller": "http://deployment-controller:8085/health/ready",
    "webhook-receiver": "http://webhook-receiver:8086/health/ready",
    "inference-gateway-rt": "http://inference-gateway-rt:8087/health/ready",
    "inference-gateway-batch": "http://inference-gateway-batch:8088/health/ready",
    "runtime-admin-api": "http://runtime-admin-api:8090/health/ready",
    "channel-service": "http://channel-service:8091/health/ready",
    "pipeline-definition-service": "http://pipeline-definition-service:8092/health/ready",
    "runtime-orchestrator": "http://runtime-orchestrator:8093/health/ready",
    "realtime-executor": "http://realtime-executor:8094/health/ready",
    "trigger-executor": "http://trigger-executor:8095/health/ready",
    "overlay-service": "http://overlay-service:8096/health/ready",
    "capture-service": "http://capture-service:8097/health/ready",
    "result-service": "http://result-service:8098/health/ready",
    "connector-service": "http://connector-service:8099/health/ready",
    "operations-agent": "http://operations-agent:8101/health/ready",
}

SERVICE_HEALTH_PORTS: dict[str, int] = {
    "dataset-service": 8082,
    "training-orchestrator": 8083,
    "model-registry-service": 8084,
    "deployment-controller": 8085,
    "webhook-receiver": 8086,
    "inference-gateway-rt": 8087,
    "inference-gateway-batch": 8088,
    "runtime-admin-api": 8090,
    "channel-service": 8091,
    "pipeline-definition-service": 8092,
    "runtime-orchestrator": 8093,
    "realtime-executor": 8094,
    "trigger-executor": 8095,
    "overlay-service": 8096,
    "capture-service": 8097,
    "result-service": 8098,
    "connector-service": 8099,
    "operations-agent": 8101,
}

EXPECTED_WORKERS = (
    "control-worker",
    "training-worker",
    "batch-worker",
    "maintenance-worker",
    "runtime-orchestrator",
    "capture-service",
    "connector-service",
)


async def probe_services(settings: PlatformSettings) -> list[ServiceSummary]:
    service_health_targets = _configured_service_health_targets()
    service_summaries = await asyncio.gather(
        *[_probe_service(name, url) for name, url in service_health_targets.items()]
    )
    return [
        ServiceSummary(
            name="admin-api",
            state=HealthState.OK,
            detail=f"Aggregator online at {settings.service.public_base_url}.",
        ),
        *service_summaries,
    ]


def _configured_service_health_targets() -> dict[str, str]:
    targets = dict(SERVICE_HEALTH_TARGETS)
    health_host = os.getenv("CV_PLATFORM_SERVICE_HEALTH_HOST", "").strip()
    if health_host:
        targets = {
            name: f"http://{health_host}:{port}/health/ready"
            for name, port in SERVICE_HEALTH_PORTS.items()
        }

    raw_overrides = os.getenv("CV_PLATFORM_SERVICE_HEALTH_TARGETS_JSON", "").strip()
    if raw_overrides:
        try:
            parsed = json.loads(raw_overrides)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            for name, endpoint in parsed.items():
                if isinstance(name, str) and isinstance(endpoint, str) and endpoint.strip():
                    targets[name] = endpoint.strip()

    excluded = {
        item.strip()
        for item in os.getenv("CV_PLATFORM_SERVICE_HEALTH_EXCLUDE", "").split(",")
        if item.strip()
    }
    for name in excluded:
        targets.pop(name, None)
    return targets


async def probe_dependencies(settings: PlatformSettings) -> list[IntegrationStatusSummary]:
    return await asyncio.gather(
        _probe_http(
            name="label_studio",
            category="annotation",
            endpoint=f"{settings.label_studio.base_url.rstrip('/')}/api/version",
            remediation_hint="Check the Label Studio service, reverse proxy route, and API token.",
        ),
        _probe_http(
            name="mlflow",
            category="tracking",
            endpoint=f"{settings.mlflow.base_url.rstrip('/')}/api/2.0/mlflow/experiments/search",
            remediation_hint="Verify the MLflow service is running and reachable from the control plane.",
            method="POST",
            json_payload={"max_results": 1},
        ),
        asyncio.to_thread(_probe_redis, settings.redis.url()),
        asyncio.to_thread(_probe_postgres, settings),
        asyncio.to_thread(_probe_dvc_repo, settings.dvc.repo_root),
    )


def read_queue_metrics(settings: PlatformSettings) -> list[QueueStatus]:
    redis = Redis.from_url(settings.redis.url())
    results: list[QueueStatus] = []
    for queue_name in QueueName:
        queue = Queue(queue_name.value, connection=redis)
        oldest_age = 0
        job_ids = queue.get_job_ids(offset=0, length=1)
        if job_ids:
            job = queue.fetch_job(job_ids[0])
            if job is not None and job.enqueued_at is not None:
                oldest_age = int((datetime.now(tz=UTC) - job.enqueued_at).total_seconds())
        results.append(
            QueueStatus(
                queue_name=queue_name.value,
                depth=len(queue),
                oldest_job_age_seconds=oldest_age,
            )
        )
    return results


def read_worker_statuses(settings: PlatformSettings) -> list[WorkerStatusSummary]:
    observed_at = datetime.now(tz=UTC)
    heartbeat_threshold = settings.observability.worker_heartbeat_stale_seconds
    with session_scope(settings) as session:
        rows = list_service_heartbeats(session)
    by_service: dict[str, list[WorkerStatusSummary]] = {}
    for row in rows:
        heartbeat_age_seconds = int((observed_at - row.observed_at).total_seconds())
        state = HealthState.OK
        detail = f"Heartbeat age {heartbeat_age_seconds}s."
        if row.status == "stopping":
            state = HealthState.DEGRADED
            detail = "Graceful shutdown is in progress."
        elif row.status == "stopped":
            state = HealthState.DOWN
            detail = "Worker reported a stopped state."
        elif heartbeat_age_seconds > heartbeat_threshold:
            state = HealthState.DOWN
            detail = f"Heartbeat is stale at {heartbeat_age_seconds}s."
        by_service.setdefault(row.service_name, []).append(
            WorkerStatusSummary(
                service_name=row.service_name,
                instance_id=row.instance_id,
                state=state,
                status=row.status,
                detail=detail,
                last_heartbeat_at=row.observed_at.isoformat().replace("+00:00", "Z"),
                details=dict(row.details or {}),
            )
        )
    for service_name in EXPECTED_WORKERS:
        by_service.setdefault(
            service_name,
            [
                WorkerStatusSummary(
                    service_name=service_name,
                    instance_id="missing",
                    state=HealthState.DOWN,
                    status="missing",
                    detail="No worker heartbeat has been observed for this service.",
                    last_heartbeat_at=None,
                    details={},
                )
            ],
        )
    flattened: list[WorkerStatusSummary] = []
    for service_name in EXPECTED_WORKERS:
        flattened.extend(by_service[service_name])
    return flattened


def read_disk_usage(settings: PlatformSettings) -> list[DiskUsageSummary]:
    observed_at = utc_now_iso()
    disk_targets = [
        ("state_root", "Platform State Root", Path(settings.storage.state_root)),
        ("training_root", "Training Root", Path(settings.storage.training_root)),
        ("serving_root", "Serving Root", Path(settings.storage.serving_root)),
        ("audit_root", "Audit Root", Path(settings.storage.audit_root)),
        ("logs_root", "Logs Root", Path(settings.storage.logs_root)),
    ]
    items: list[DiskUsageSummary] = []
    for key, label, path in disk_targets:
        if not path.exists():
            items.append(
                DiskUsageSummary(
                    key=key,
                    label=label,
                    path=str(path),
                    state=HealthState.DOWN,
                    detail="Configured storage path is missing.",
                    observed_at=observed_at,
                )
            )
            continue
        usage = shutil.disk_usage(path)
        used_bytes = usage.total - usage.free
        used_percent = round((used_bytes / usage.total) * 100, 2) if usage.total else 0.0
        state = HealthState.OK
        if used_percent >= settings.observability.disk_usage_critical_percent:
            state = HealthState.DOWN
        elif used_percent >= settings.observability.disk_usage_warning_percent:
            state = HealthState.DEGRADED
        items.append(
            DiskUsageSummary(
                key=key,
                label=label,
                path=str(path),
                state=state,
                detail="Disk usage sampled successfully.",
                total_bytes=usage.total,
                used_bytes=used_bytes,
                free_bytes=usage.free,
                used_percent=used_percent,
                observed_at=observed_at,
            )
        )
    return items


def read_gpu_status(settings: PlatformSettings) -> list[GpuDeviceSummary]:
    observed_at = utc_now_iso()
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        fallback_state = HealthState.DOWN if settings.env.value == "prod" else HealthState.DEGRADED
        return [
            GpuDeviceSummary(
                device_index=0,
                state=fallback_state,
                detail=f"GPU telemetry unavailable: {exc}",
                observed_at=observed_at,
            )
        ]
    items: list[GpuDeviceSummary] = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        device_index = int(parts[0])
        name = parts[1]
        utilization_percent = float(parts[2])
        memory_used_mb = int(parts[3])
        memory_total_mb = int(parts[4])
        temperature_c = float(parts[5])
        memory_used_percent = (
            round((memory_used_mb / memory_total_mb) * 100, 2) if memory_total_mb else 0.0
        )
        state = HealthState.OK
        if memory_used_percent >= settings.observability.gpu_memory_critical_percent:
            state = HealthState.DOWN
        elif memory_used_percent >= settings.observability.gpu_memory_warning_percent:
            state = HealthState.DEGRADED
        items.append(
            GpuDeviceSummary(
                device_index=device_index,
                name=name,
                state=state,
                detail="GPU telemetry collected successfully.",
                utilization_percent=utilization_percent,
                memory_used_mb=memory_used_mb,
                memory_total_mb=memory_total_mb,
                memory_used_percent=memory_used_percent,
                temperature_c=temperature_c,
                observed_at=observed_at,
            )
        )
    if not items:
        items.append(
            GpuDeviceSummary(
                device_index=0,
                state=HealthState.DEGRADED,
                detail="nvidia-smi returned no GPU rows.",
                observed_at=observed_at,
            )
        )
    return items


def repository_integrity_alerts(settings: PlatformSettings) -> list[SystemAlert]:
    manager = TritonRepositoryManager(settings)
    alerts: list[SystemAlert] = []
    with session_scope(settings) as session:
        active_release_ids = [
            item.active_release_id
            for item in session.query(DeploymentTarget).all()
            if item.active_release_id is not None
        ]
        targets = (
            session.query(DeploymentRelease)
            .filter(DeploymentRelease.id.in_(active_release_ids))
            .all()
            if active_release_ids
            else []
        )
        versions = {item.id: item for item in session.query(ModelVersion).all()}
    for release in targets:
        version = versions.get(release.model_version_id)
        if version is None or not version.serving_manifest:
            continue
        report = manager.repository_integrity_report(
            target_path=TargetPath(release.target_path),
            stable_model_name=release.triton_model_name,
            triton_model_version=release.triton_model_version,
            package_manifest=version.serving_manifest,
        )
        if bool(report.get("healthy")):
            continue
        for issue in report.get("checks", []):
            if issue.get("passed", False):
                continue
            alerts.append(
                SystemAlert(
                    code=f"repository_integrity_{issue['name']}",
                    severity="critical",
                    source=f"{release.target_path}:{release.id}",
                    title="Model repository integrity check failed",
                    detail=str(
                        issue.get("detail")
                        or "The active Triton repository contents do not match the immutable package."
                    ),
                    remediation_hint="Quarantine the broken repository contents and roll back to the previous healthy release before promoting again.",
                    observed_at=utc_now_iso(),
                )
            )
    return alerts


def build_operational_alerts(
    *,
    settings: PlatformSettings,
    services: list[ServiceSummary],
    dependencies: list[IntegrationStatusSummary],
    workers: list[WorkerStatusSummary],
    queues: list[QueueStatus],
    disks: list[DiskUsageSummary],
    gpus: list[GpuDeviceSummary],
    realtime_release_id: str | None,
    batch_release_id: str | None,
    runtime: RuntimeOperationalSummary | None = None,
) -> list[SystemAlert]:
    alerts: list[SystemAlert] = []
    observed_at = utc_now_iso()
    for service in services:
        if service.state == HealthState.DOWN:
            alerts.append(
                SystemAlert(
                    code=f"service_down_{service.name}",
                    severity="critical",
                    source=service.name,
                    title="Service is down",
                    detail=service.detail,
                    remediation_hint="Review the service logs and restart the affected service before retrying dependent workflows.",
                    observed_at=observed_at,
                )
            )
    for dependency in dependencies:
        if dependency.state == HealthState.OK:
            continue
        alerts.append(
            SystemAlert(
                code=f"dependency_{dependency.state}_{dependency.name}",
                severity="critical" if dependency.state == HealthState.DOWN else "warning",
                source=dependency.name,
                title="Dependency health needs attention",
                detail=dependency.detail,
                remediation_hint=dependency.remediation_hint,
                observed_at=observed_at,
            )
        )
    for worker in workers:
        if worker.state == HealthState.OK:
            continue
        alerts.append(
            SystemAlert(
                code=f"worker_{worker.state}_{worker.service_name}",
                severity="critical" if worker.state == HealthState.DOWN else "warning",
                source=worker.service_name,
                title="Worker heartbeat needs attention",
                detail=worker.detail,
                remediation_hint="Restart the worker and confirm its queue backlog starts draining again.",
                observed_at=observed_at,
            )
        )
    for queue in queues:
        severity = _queue_severity(settings, queue)
        if severity is None:
            continue
        alerts.append(
            SystemAlert(
                code=f"queue_pressure_{queue.queue_name}",
                severity=severity,
                source=queue.queue_name,
                title="Queue pressure is elevated",
                detail=f"Depth={queue.depth}, oldest_job_age_seconds={queue.oldest_job_age_seconds}.",
                remediation_hint="Inspect failing jobs, verify worker heartbeats, and consider pausing new submissions until the backlog clears.",
                observed_at=observed_at,
            )
        )
    for disk in disks:
        if disk.state == HealthState.OK:
            continue
        alerts.append(
            SystemAlert(
                code=f"disk_{disk.state}_{disk.key}",
                severity="critical" if disk.state == HealthState.DOWN else "warning",
                source=disk.key,
                title="Disk usage is elevated",
                detail=f"{disk.path} is at {disk.used_percent:.2f}% utilization.",
                remediation_hint="Apply retention policies, inspect quarantine growth, and move large artifacts off the node before training or batch work continues.",
                observed_at=observed_at,
            )
        )
    for gpu in gpus:
        if gpu.state == HealthState.OK:
            continue
        alerts.append(
            SystemAlert(
                code=f"gpu_{gpu.state}_{gpu.device_index}",
                severity="critical" if gpu.state == HealthState.DOWN else "warning",
                source=f"gpu:{gpu.device_index}",
                title="GPU pressure is elevated",
                detail=gpu.detail
                if gpu.memory_used_percent is None
                else f"GPU memory usage is {gpu.memory_used_percent:.2f}% and utilization is {gpu.utilization_percent or 0:.2f}%.",
                remediation_hint="Check active training and Triton workloads, then pause or reschedule the non-critical workload first.",
                observed_at=observed_at,
            )
        )
    if runtime is not None:
        for item in runtime.attention:
            severity = (
                "critical"
                if item.health_state == HealthState.DOWN
                or item.observed_state.value == "failed"
                else "warning"
            )
            remediation_hint = item.recovery_hint or (
                "Open the channel detail page, review the runtime card, and restart "
                "only after correcting the source or pipeline issue."
            )
            alerts.append(
                SystemAlert(
                    code=f"runtime_channel_{item.observed_state.value}_{item.channel_id}",
                    severity=severity,
                    source=f"runtime:{item.channel_id}",
                    title="Runtime channel needs attention",
                    detail=f"{item.name} at {item.technical_place}: {item.health_text}.",
                    remediation_hint=remediation_hint,
                    observed_at=observed_at,
                )
            )
        drop_rate = runtime.performance.realtime_drop_rate_percent
        if drop_rate is not None and drop_rate >= 10:
            alerts.append(
                SystemAlert(
                    code="runtime_realtime_drop_rate_elevated",
                    severity="warning",
                    source="runtime:realtime",
                    title="Realtime drop rate is elevated",
                    detail=(
                        "Aggregate realtime drop rate is "
                        f"{drop_rate:.1f}%."
                    ),
                    remediation_hint=(
                        "Reduce sampling rate, inspect source FPS, and verify "
                        "inference latency before adding channels."
                    ),
                    observed_at=observed_at,
                )
            )
        realtime_latency = runtime.performance.realtime_latency_p95_ms
        if (
            realtime_latency is not None
            and realtime_latency >= settings.runtime.realtime_target_latency_ms
        ):
            alerts.append(
                SystemAlert(
                    code="runtime_realtime_latency_elevated",
                    severity="warning",
                    source="runtime:realtime",
                    title="Realtime latency is elevated",
                    detail=(
                        f"Observed p95 latency is {realtime_latency:.0f} ms; "
                        f"target is {settings.runtime.realtime_target_latency_ms} ms."
                    ),
                    remediation_hint=(
                        "Inspect active channels, model routes, GPU pressure, and "
                        "drop policy before enabling more runtime work."
                    ),
                    observed_at=observed_at,
                )
            )
    if not realtime_release_id:
        alerts.append(
            SystemAlert(
                code="realtime_release_missing",
                severity="critical",
                source="deployment:realtime",
                title="No active realtime release",
                detail="Realtime inference currently has no active deployment target.",
                remediation_hint="Promote a previously validated realtime release or roll back to the last healthy production version.",
                observed_at=observed_at,
            )
        )
    if not batch_release_id:
        alerts.append(
            SystemAlert(
                code="batch_release_missing",
                severity="warning",
                source="deployment:batch",
                title="No active batch release",
                detail="Batch inference currently has no active deployment target.",
                remediation_hint="Stage and activate a validated batch release before submitting new batch jobs.",
                observed_at=observed_at,
            )
        )
    try:
        alerts.extend(repository_integrity_alerts(settings))
    except Exception as exc:  # noqa: BLE001
        alerts.append(
            SystemAlert(
                code="repository_integrity_probe_failed",
                severity="warning",
                source="deployment-repository",
                title="Repository integrity probe failed",
                detail=str(exc),
                remediation_hint="Review the deployment-controller logs and rerun repository integrity checks.",
                observed_at=observed_at,
            )
        )
    return alerts


async def _probe_service(name: str, url: str) -> ServiceSummary:
    try:
        status_code, payload = await asyncio.to_thread(_request_json, url)
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}")
        checks = payload.get("checks", [])
        failing = [
            f"{item.get('name')}: {item.get('detail')}"
            for item in checks
            if item.get("state") != HealthState.OK.value
        ]
        return ServiceSummary(
            name=name,
            state=payload.get("state", HealthState.OK.value),
            detail="; ".join(failing) if failing else "Ready.",
        )
    except Exception as exc:
        return ServiceSummary(
            name=name,
            state=HealthState.DOWN,
            detail=f"Probe failed: {exc}",
        )


async def _probe_http(
    *,
    name: str,
    category: str,
    endpoint: str,
    remediation_hint: str,
    method: str = "GET",
    json_payload: dict[str, object] | None = None,
) -> IntegrationStatusSummary:
    observed_at = utc_now_iso()
    try:
        status_code, _payload = await asyncio.to_thread(
            _request_json,
            endpoint,
            method,
            json_payload,
        )
        reachable = 200 <= status_code < 300
        state = HealthState.OK if reachable else HealthState.DEGRADED
        detail = f"HTTP {status_code}"
    except Exception as exc:
        reachable = False
        state = HealthState.DOWN
        detail = f"Probe failed: {exc}"
    return IntegrationStatusSummary(
        name=name,
        category=category,
        state=state,
        configured=True,
        reachable=reachable,
        endpoint=endpoint,
        detail=detail,
        remediation_hint=None if reachable else remediation_hint,
        observed_at=observed_at,
    )


def _probe_redis(redis_url: str) -> IntegrationStatusSummary:
    observed_at = utc_now_iso()
    try:
        redis = Redis.from_url(redis_url)
        reachable = bool(redis.ping())
        detail = "PING successful" if reachable else "PING did not succeed"
        state = HealthState.OK if reachable else HealthState.DOWN
    except Exception as exc:
        reachable = False
        detail = f"Probe failed: {exc}"
        state = HealthState.DOWN
    return IntegrationStatusSummary(
        name="redis",
        category="queueing",
        state=state,
        configured=True,
        reachable=reachable,
        endpoint=redis_url,
        detail=detail,
        remediation_hint=None
        if reachable
        else "Verify Redis is running and the configured password is correct.",
        observed_at=observed_at,
    )


def _probe_postgres(settings: PlatformSettings) -> IntegrationStatusSummary:
    observed_at = utc_now_iso()
    try:
        with session_scope(settings) as session:
            session.execute(text("SELECT 1"))
        reachable = True
        detail = "SELECT 1 successful"
        state = HealthState.OK
    except Exception as exc:
        reachable = False
        detail = f"Probe failed: {exc}"
        state = HealthState.DOWN
    return IntegrationStatusSummary(
        name="postgres",
        category="persistence",
        state=state,
        configured=True,
        reachable=reachable,
        endpoint=settings.database.sqlalchemy_dsn(),
        detail=detail,
        remediation_hint=None
        if reachable
        else "Check PostgreSQL availability, credentials, and network routing.",
        observed_at=observed_at,
    )


def _probe_dvc_repo(repo_root: str) -> IntegrationStatusSummary:
    observed_at = utc_now_iso()
    path = Path(repo_root)
    exists = path.exists()
    reachable = exists and path.is_dir()
    return IntegrationStatusSummary(
        name="dvc_repo",
        category="artifact_versioning",
        state=HealthState.OK if reachable else HealthState.DEGRADED,
        configured=True,
        reachable=reachable,
        endpoint=str(path),
        detail="Repository path exists"
        if reachable
        else "Configured DVC repository path is missing.",
        remediation_hint=(
            None
            if reachable
            else "Initialize or mount the DVC repository root before tracking snapshots or artifacts."
        ),
        observed_at=observed_at,
    )


def _queue_severity(settings: PlatformSettings, queue: QueueStatus) -> str | None:
    if (
        queue.depth >= settings.observability.queue_depth_critical
        or queue.oldest_job_age_seconds >= settings.observability.queue_oldest_age_critical_seconds
    ):
        return "critical"
    if (
        queue.depth >= settings.observability.queue_depth_warning
        or queue.oldest_job_age_seconds >= settings.observability.queue_oldest_age_warning_seconds
    ):
        return "warning"
    return None


def _request_json(
    endpoint: str,
    method: str = "GET",
    json_payload: dict[str, object] | None = None,
    timeout_seconds: float = 2.5,
) -> tuple[int, dict[str, object]]:
    payload = None
    headers: dict[str, str] = {}
    if json_payload is not None:
        payload = json.dumps(json_payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(endpoint, data=payload, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="ignore")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        if not body:
            return exc.code, {}
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {}
