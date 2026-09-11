from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from cv_observability import (
    CorrelationIdMiddleware,
    configure_logging,
    create_health_router,
    install_exception_handlers,
)
from cv_status import HealthState
from cv_utils import utc_now_iso
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cv_core.middleware import ContentLengthLimitMiddleware, RequestTimeoutMiddleware
from cv_core.settings import PlatformSettings, load_platform_settings, prepare_runtime_paths

ProbeFn = Callable[[PlatformSettings], Awaitable[tuple[str, HealthState, str]]]
LifecycleHook = Callable[[FastAPI], Awaitable[None] | None]

_POSTGRES_SERVICES = {
    "admin-api",
    "webhook-receiver",
    "dataset-service",
    "training-orchestrator",
    "model-registry-service",
    "deployment-controller",
    "inference-gateway-rt",
    "inference-gateway-batch",
    "label-studio-ml-adapter",
    "runtime-admin-api",
    "channel-service",
    "pipeline-definition-service",
    "runtime-orchestrator",
    "realtime-executor",
    "camera-media",
    "trigger-executor",
    "overlay-service",
    "capture-service",
    "result-service",
    "connector-service",
}

_REDIS_SERVICES = {
    "admin-api",
    "webhook-receiver",
    "dataset-service",
    "training-orchestrator",
    "deployment-controller",
    "inference-gateway-batch",
    "runtime-admin-api",
    "runtime-orchestrator",
    "capture-service",
    "connector-service",
}


def create_service_app(
    *,
    title: str,
    service_name: str,
    api_router: APIRouter | None = None,
    probe_factories: list[ProbeFn] | None = None,
    configure_app: Callable[[FastAPI], None] | None = None,
    startup_hooks: list[LifecycleHook] | None = None,
    shutdown_hooks: list[LifecycleHook] | None = None,
) -> FastAPI:
    configured_settings = load_platform_settings(service_name)
    instance_id = _build_instance_id(service_name)
    configure_logging(service_name, configured_settings.env.value, instance_id=instance_id)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = getattr(app.state, "settings", configured_settings)
        prepared_paths = prepare_runtime_paths(settings)
        app.state.settings = settings
        app.state.instance_id = instance_id
        app.state.startup_completed = True
        app.state.shutting_down = False
        app.state.started_at = utc_now_iso()
        app.state.prepared_paths = prepared_paths
        for hook in startup_hooks or []:
            result = hook(app)
            if result is not None:
                await result
        try:
            yield
        finally:
            app.state.shutting_down = True
            for hook in shutdown_hooks or []:
                result = hook(app)
                if result is not None:
                    await result

    app = FastAPI(title=title, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(configured_settings),
        allow_credentials=configured_settings.cors.allow_credentials,
        allow_methods=configured_settings.cors.allow_methods,
        allow_headers=configured_settings.cors.allow_headers,
    )
    app.add_middleware(
        RequestTimeoutMiddleware,
        timeout_resolver=lambda request: _request_timeout_seconds(configured_settings, request),
    )
    app.add_middleware(
        ContentLengthLimitMiddleware,
        limit_resolver=lambda request: _request_body_limit_bytes(configured_settings, request),
    )
    app.add_middleware(CorrelationIdMiddleware)
    install_exception_handlers(app)

    async def live_probe() -> tuple[str, HealthState, str]:
        if getattr(app.state, "shutting_down", False):
            return (
                "process",
                HealthState.DEGRADED,
                "Shutdown is in progress; liveness remains true until exit.",
            )
        return ("process", HealthState.OK, "Service process is running.")

    async def startup_probe() -> tuple[str, HealthState, str]:
        if getattr(app.state, "startup_completed", False):
            prepared_paths = getattr(app.state, "prepared_paths", [])
            return (
                "startup",
                HealthState.OK,
                f"Startup validation completed and {len(prepared_paths)} runtime paths are ready.",
            )
        return ("startup", HealthState.DOWN, "Startup validation has not completed yet.")

    async def shutdown_probe() -> tuple[str, HealthState, str]:
        if getattr(app.state, "shutting_down", False):
            return (
                "shutdown",
                HealthState.DOWN,
                "Shutdown requested; service is no longer ready to accept work.",
            )
        return ("shutdown", HealthState.OK, "Service is accepting work.")

    async def storage_probe() -> tuple[str, HealthState, str]:
        settings: PlatformSettings = app.state.settings
        critical_paths = [
            Path(settings.storage.state_root),
            Path(settings.storage.logs_root),
            Path(settings.storage.audit_root),
        ]
        missing = [str(path) for path in critical_paths if not path.exists()]
        if missing:
            return ("storage", HealthState.DOWN, f"Missing runtime paths: {', '.join(missing)}")
        return ("storage", HealthState.OK, "Runtime paths are present.")

    async def postgres_probe() -> tuple[str, HealthState, str]:
        settings: PlatformSettings = app.state.settings
        if service_name not in _POSTGRES_SERVICES:
            return (
                "postgres",
                HealthState.OK,
                "PostgreSQL is not required for this service readiness check.",
            )
        return await _tcp_probe(
            name="postgres",
            host=settings.database.host,
            port=settings.database.port,
            success_detail="TCP connectivity to PostgreSQL succeeded.",
        )

    async def redis_probe() -> tuple[str, HealthState, str]:
        settings: PlatformSettings = app.state.settings
        if service_name not in _REDIS_SERVICES:
            return (
                "redis",
                HealthState.OK,
                "Redis is not required for this service readiness check.",
            )
        return await _tcp_probe(
            name="redis",
            host=settings.redis.host,
            port=settings.redis.port,
            success_detail="TCP connectivity to Redis succeeded.",
        )

    async def triton_probe() -> tuple[str, HealthState, str]:
        settings: PlatformSettings = app.state.settings
        if service_name == "inference-gateway-rt":
            return await _http_ready_probe("triton_rt", settings.triton.realtime.http_url)
        if service_name == "inference-gateway-batch":
            return await _http_ready_probe("triton_batch", settings.triton.batch.http_url)
        return ("triton", HealthState.OK, "Triton readiness is not required for this service.")

    async def custom_probe(factory: ProbeFn) -> tuple[str, HealthState, str]:
        return await factory(app.state.settings)

    live_checks = [live_probe]
    startup_checks = [startup_probe, storage_probe]
    readiness_checks = [
        shutdown_probe,
        startup_probe,
        storage_probe,
        postgres_probe,
        redis_probe,
        triton_probe,
    ]
    if probe_factories:
        readiness_checks.extend(
            [lambda factory=factory: custom_probe(factory) for factory in probe_factories]
        )
    app.include_router(
        create_health_router(
            service_name=service_name,
            live_checks=live_checks,
            readiness_checks=readiness_checks,
            startup_checks=startup_checks,
        )
    )
    if api_router is not None:
        app.include_router(api_router, prefix="/api/v1")
    if configure_app is not None:
        configure_app(app)
    return app


async def _tcp_probe(
    *,
    name: str,
    host: str,
    port: int,
    success_detail: str,
) -> tuple[str, HealthState, str]:
    def _attempt() -> None:
        with socket.create_connection((host, port), timeout=2.0):
            return None

    try:
        await asyncio.to_thread(_attempt)
    except OSError as exc:
        return (name, HealthState.DOWN, f"Connection failed: {exc}")
    return (name, HealthState.OK, success_detail)


async def _http_ready_probe(name: str, endpoint_url: str) -> tuple[str, HealthState, str]:
    parsed = urlparse(endpoint_url.rstrip("/"))
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = f"{parsed.path or ''}/v2/health/ready"

    def _attempt() -> tuple[int, str]:
        import http.client

        connection_cls = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_cls(host, port, timeout=2.0)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="ignore")
            return response.status, body
        finally:
            connection.close()

    try:
        status_code, _body = await asyncio.to_thread(_attempt)
    except OSError as exc:
        return (name, HealthState.DOWN, f"HTTP readiness probe failed: {exc}")
    if status_code != 200:
        return (name, HealthState.DOWN, f"HTTP readiness probe returned {status_code}.")
    return (name, HealthState.OK, "HTTP readiness probe succeeded.")


def _build_instance_id(service_name: str) -> str:
    return f"{socket.gethostname()}-{service_name}-{os.getpid()}"


def _cors_origins(settings: PlatformSettings) -> list[str]:
    if settings.cors.allowed_origins:
        return settings.cors.allowed_origins
    parsed = urlparse(settings.service.public_base_url)
    if parsed.scheme and parsed.netloc:
        return [f"{parsed.scheme}://{parsed.netloc}"]
    return ["http://localhost"]


def _request_timeout_seconds(settings: PlatformSettings, request) -> float:
    if (
        request.url.path == "/api/v1/runtime/edge-software/packages"
        and request.method.upper() == "POST"
    ):
        return float(settings.request_limits.edge_software_upload_timeout_seconds)
    if _is_pipeline_graph_request(request):
        return float(settings.request_limits.upload_timeout_seconds)
    if request.url.path.startswith("/api/v1/upload-sessions/") and request.method.upper() == "PUT":
        return float(settings.request_limits.upload_timeout_seconds)
    if request.url.path == "/api/v1/runtime/channel-test-files" and request.method.upper() == "POST":
        return float(settings.request_limits.upload_timeout_seconds)
    if _is_camera_calibration_calculation(request):
        return float(settings.request_limits.upload_timeout_seconds)
    if "/api/v1/infer/" in request.url.path:
        return max(float(settings.request_limits.default_timeout_seconds), 31.0)
    if "/api/v1/batch/" in request.url.path:
        return float(settings.inference_limits.batch_timeout_seconds)
    return float(settings.request_limits.default_timeout_seconds)


def _request_body_limit_bytes(settings: PlatformSettings, request) -> int:
    if (
        request.url.path == "/api/v1/runtime/edge-software/packages"
        and request.method.upper() == "POST"
    ):
        return settings.request_limits.edge_software_upload_limit_bytes
    if _is_pipeline_graph_request(request):
        return settings.request_limits.pipeline_graph_body_limit_bytes
    if request.url.path.startswith("/api/v1/upload-sessions/") and request.method.upper() == "PUT":
        return settings.request_limits.upload_body_limit_bytes
    if request.url.path == "/api/v1/runtime/channel-test-files" and request.method.upper() == "POST":
        return settings.request_limits.upload_body_limit_bytes
    if _is_camera_calibration_calculation(request):
        return settings.request_limits.camera_calibration_body_limit_bytes
    if (
        "/api/v1/realtime/" in request.url.path
        or "/api/v1/infer/" in request.url.path
        or "/api/v1/batch/" in request.url.path
    ):
        return settings.request_limits.inference_body_limit_bytes
    return settings.request_limits.default_body_limit_bytes


def _is_pipeline_graph_request(request) -> bool:
    if request.method.upper() != "POST":
        return False
    path = request.url.path
    return (
        (
            path.startswith("/api/v1/runtime/pipeline-definitions/")
            and path.endswith("/versions")
        )
        or path == "/api/v1/runtime/pipeline-versions:validate"
        or (
            path.startswith("/api/v1/runtime/channels/")
            and path.endswith("/pipeline-preview")
        )
    )


def _is_camera_calibration_calculation(request) -> bool:
    return (
        request.method.upper() == "POST"
        and request.url.path.startswith("/api/v1/runtime/channels/")
        and request.url.path.endswith("/camera-calibration/calculate")
    )
