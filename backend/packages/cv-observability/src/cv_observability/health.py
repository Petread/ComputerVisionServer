from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest

from cv_contracts import HealthCheck, HealthResponse, ResponseMeta
from cv_status import HealthState
from cv_utils import get_correlation_id, get_request_id, utc_now_iso

HealthCheckFn = Callable[[], Awaitable[tuple[str, HealthState, str]]]


def create_health_router(
    *,
    service_name: str,
    live_checks: list[HealthCheckFn] | None = None,
    readiness_checks: list[HealthCheckFn] | None = None,
    startup_checks: list[HealthCheckFn] | None = None,
    registry: CollectorRegistry | None = None,
) -> APIRouter:
    router = APIRouter(tags=["health"])
    live_probe_checks = live_checks or []
    ready_probe_checks = readiness_checks or []
    startup_probe_checks = startup_checks or []
    metrics_registry = registry or REGISTRY

    async def build_response(checks: list[HealthCheckFn]) -> HealthResponse:
        check_results: list[HealthCheck] = []
        overall_state = HealthState.OK
        for check in checks:
            check_name, state, detail = await check()
            if state == HealthState.DOWN:
                overall_state = HealthState.DOWN
            elif state == HealthState.DEGRADED and overall_state == HealthState.OK:
                overall_state = HealthState.DEGRADED
            check_results.append(
                HealthCheck(
                    name=check_name,
                    state=state,
                    detail=detail,
                    observed_at=utc_now_iso(),
                )
            )
        return HealthResponse(
            service=service_name,
            state=overall_state,
            checks=check_results,
            meta=ResponseMeta(
                request_id=get_request_id(),
                correlation_id=get_correlation_id(),
                generated_at=utc_now_iso(),
            ),
        )

    @router.get("/health/live", response_model=HealthResponse)
    async def live(response: Response) -> HealthResponse:
        payload = await build_response(live_probe_checks)
        if payload.state == HealthState.DOWN:
            response.status_code = 503
        return payload

    @router.get("/health/ready", response_model=HealthResponse)
    async def ready(response: Response) -> HealthResponse:
        payload = await build_response(ready_probe_checks)
        if payload.state != HealthState.OK:
            response.status_code = 503
        return payload

    @router.get("/health/startup", response_model=HealthResponse)
    async def startup(response: Response) -> HealthResponse:
        payload = await build_response(startup_probe_checks)
        if payload.state != HealthState.OK:
            response.status_code = 503
        return payload

    @router.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            generate_latest(metrics_registry).decode("utf-8"), media_type=CONTENT_TYPE_LATEST
        )

    return router
