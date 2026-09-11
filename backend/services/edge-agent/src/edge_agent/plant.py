from __future__ import annotations

import asyncio
import ipaddress
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from cv_contracts import (
    EdgeControlError,
    EdgePlantHealth,
    EdgePlantLastResultResponse,
    EdgePlantTriggerRequest,
    EdgePlantTriggerResponse,
)
from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError

from edge_agent.state import EdgeAgentConfig, EdgeAgentState, EdgeStateError, _utc_now

_PLANT_ERROR_RESPONSES = {
    status_code: {"model": EdgeControlError, "description": "Edge plant boundary error"}
    for status_code in (400, 403, 404, 409, 413, 422, 429, 503, 504)
}


def create_plant_app(
    config: EdgeAgentConfig | None = None,
    *,
    state: EdgeAgentState | None = None,
) -> FastAPI:
    edge_state = state or EdgeAgentState(config or EdgeAgentConfig.from_environment())
    owns_state = state is None
    limits = edge_state.config
    allowed = {ipaddress.ip_address(value) for value in limits.plant_allowed_ipv4s}
    semaphore = threading.BoundedSemaphore(limits.plant_max_concurrent_triggers)
    executor = ThreadPoolExecutor(
        max_workers=limits.plant_max_concurrent_triggers,
        thread_name_prefix="edge-plant-trigger",
    )
    request_times: dict[str, deque[float]] = defaultdict(deque)
    rate_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            if owns_state:
                edge_state.close()

    application = FastAPI(
        title="YBB Edge Plant Trigger API",
        version="1.0.0",
        responses=_PLANT_ERROR_RESPONSES,
        lifespan=lifespan,
    )
    application.state.edge_agent = edge_state

    @application.middleware("http")
    async def enforce_plant_boundary(request: Request, call_next: object) -> Response:
        host = request.client.host if request.client is not None else ""
        try:
            client_ip = ipaddress.ip_address(host)
        except ValueError:
            # Starlette's in-process TestClient is not reachable over a network socket.
            if host != "testclient":
                return _error_response(403, "plant_client_denied", "Client IP is invalid.")
            client_ip = ipaddress.ip_address("127.0.0.1")
        if not client_ip.is_loopback and client_ip not in allowed:
            return _error_response(
                403,
                "plant_client_denied",
                "Client IP is not admitted to the local plant trigger API.",
            )
        if request.method == "PUT":
            content_length = request.headers.get("content-length")
            try:
                declared_size = int(content_length) if content_length is not None else None
            except ValueError:
                return _error_response(
                    400, "plant_content_length_invalid", "Content-Length is invalid."
                )
            if declared_size is not None and (
                declared_size < 0 or declared_size > limits.plant_max_request_bytes
            ):
                return _error_response(
                    413, "plant_request_too_large", "Trigger body is too large."
                )
            body = await request.body()
            if len(body) > limits.plant_max_request_bytes:
                return _error_response(413, "plant_request_too_large", "Trigger body is too large.")
            now = time.monotonic()
            with rate_lock:
                timestamps = request_times[str(client_ip)]
                while timestamps and timestamps[0] <= now - 1.0:
                    timestamps.popleft()
                if len(timestamps) >= limits.plant_rate_limit_per_second:
                    return _error_response(
                        429, "plant_rate_limited", "Trigger rate limit exceeded."
                    )
                timestamps.append(now)
            return await call_next(request)  # type: ignore[operator]
        return await call_next(request)  # type: ignore[operator]

    @application.exception_handler(EdgeStateError)
    async def handle_state_error(_request: Request, exc: EdgeStateError) -> Response:
        return _error_response(
            exc.status_code,
            exc.code,
            str(exc),
            retryable=exc.retryable,
            detail=exc.detail,
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> Response:
        return _error_response(
            422,
            "request_validation_error",
            "The plant trigger request is invalid.",
            detail={
                "errors": [
                    {
                        "location": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"],
                        "type": error["type"],
                    }
                    for error in exc.errors()
                ]
            },
        )

    @application.get("/edge-plant/v1/health", response_model=EdgePlantHealth)
    def health() -> EdgePlantHealth:
        device_health = edge_state.health()
        return EdgePlantHealth(
            state="degraded" if device_health.state != "ok" else "ok",
            boot_id=edge_state.boot_id,
            active_channel_ids=edge_state.supervisor.active_channel_ids(),
            max_request_bytes=limits.plant_max_request_bytes,
            max_concurrent_triggers=limits.plant_max_concurrent_triggers,
            observed_at=_utc_now(),
        )

    @application.put(
        "/edge-plant/v1/channels/{channel_id}/trigger",
        response_model=EdgePlantTriggerResponse,
    )
    async def trigger(
        channel_id: str,
        request: EdgePlantTriggerRequest,
        x_idempotency_key: str = Header(min_length=3, max_length=160),
    ) -> EdgePlantTriggerResponse:
        timeout_ms = min(request.timeout_ms, limits.plant_trigger_timeout_ms)
        if not semaphore.acquire(blocking=False):
            raise EdgeStateError(
                "plant_concurrency_limited",
                "All trigger execution slots are busy.",
                status_code=429,
                retryable=True,
            )

        def execute_trigger() -> EdgePlantTriggerResponse:
            try:
                return edge_state.trigger_channel(
                    channel_id, request, idempotency_key=x_idempotency_key
                )
            finally:
                semaphore.release()

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            executor,
            execute_trigger,
        )
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout_ms / 1000)
        except TimeoutError as exc:
            raise EdgeStateError(
                "trigger_timeout",
                "Trigger execution exceeded the configured response deadline.",
                status_code=504,
                retryable=True,
                detail={"timeout_ms": timeout_ms},
            ) from exc

    @application.get(
        "/edge-plant/v1/channels/{channel_id}/last-result",
        response_model=EdgePlantLastResultResponse,
    )
    def last_result(channel_id: str) -> EdgePlantLastResultResponse:
        return edge_state.last_trigger_result(channel_id)

    return application


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    detail: dict[str, object] | None = None,
) -> Response:
    error = EdgeControlError(
        code=code,
        message=message,
        retryable=retryable,
        detail=detail or {},
    )
    return Response(
        content=error.model_dump_json(),
        status_code=status_code,
        media_type="application/json",
    )
