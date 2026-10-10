from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cv_contracts.runtime import (
    RuntimeBoundaryResponse,
    RuntimeLiveDiagnostics,
    RuntimeLiveFrame,
    RuntimeLiveMetrics,
    RuntimeLiveSourceHealth,
    RuntimeLiveViewState,
    RuntimeOverlayPrimitiveState,
    RuntimePipelineGraph,
    RuntimeTriggerEndpointReadiness,
    RuntimeTriggerExecutionRequest,
    RuntimeTriggerExecutionResponse,
    RuntimeTriggerResponsePolicy,
)
from cv_core import MachineIdentity, PlatformSettings, parse_machine_token
from cv_db import (
    authenticate_machine_identity,
    get_runtime_live_view_state,
    get_trigger_endpoint_for_execution,
    load_published_runtime_pipeline,
    mark_trigger_endpoint_invoked,
    record_runtime_execution_result,
    record_trigger_timeout_result,
    session_scope,
    trigger_endpoint_readiness,
    trigger_live_view_runtime_id,
    write_runtime_live_view_state,
)
from cv_db.models import RuntimeChannel, RuntimeSource, TriggerEndpoint
from cv_db.runtime_model_store import (
    build_execution_model_resolver, lock_runtime_use, require_runtime_pipeline,
)
from cv_runtime import (
    HttpInferenceGatewayClient,
    RuntimeExecutionContext,
    RuntimeExecutionResult,
    RuntimeGraphBuildError,
    runtime_service_boundary,
)
from cv_runtime.payloads import (
    FramePayload,
    SynchronizedCameraFrame,
    SynchronizedFrameSetPayload,
    TriggerPayload,
)
from cv_runtime.siemens_sps import close_siemens_sps_connection_cache
from cv_status import HealthState, RuntimeMode, RuntimeObservedState, RuntimeSourceType
from cv_utils import (
    AuthenticationError,
    NotFoundError,
    PlatformError,
    ensure_path_within_root,
    get_correlation_id,
    get_request_id,
    inspect_image_bytes,
    utc_now_iso,
)
from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy.orm import Session

from trigger_executor import SERVICE_NAME

try:
    from realtime_executor.sources import RtspSourceConfigurationError, build_frame_source
except Exception:
    RtspSourceConfigurationError = RuntimeError  # type: ignore[assignment]
    build_frame_source = None  # type: ignore[assignment]

router = APIRouter(tags=["runtime-boundary"])

_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="TriggerBearer")
_legacy_key_scheme = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    scheme_name="TriggerApiKey",
)
_UNSAFE_TRIGGER_PAYLOAD_KEYS = {
    "image_path",
    "path",
    "file_path",
    "local_path",
    "server_path",
}
_TEST_FILE_IMAGE_EXTENSIONS = {".bmp", ".jpg", ".jpeg", ".png", ".webp"}
_TEST_FILE_VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}


@dataclass(frozen=True)
class TriggerEndpointSnapshot:
    endpoint_id: str
    slug: str
    channel_id: str
    pipeline_version_id: str | None
    timeout_ms: int
    rate_limit_per_minute: int
    burst_limit: int
    auth_required: bool
    auth_scope: str
    enabled: bool
    readiness: RuntimeTriggerEndpointReadiness


@dataclass(frozen=True)
class CameraCaptureBinding:
    role: str
    channel_id: str
    source_type: RuntimeSourceType | None
    source_config: dict[str, Any]
    source_secret_refs: dict[str, str]


class TriggerAdmissionController:
    def __init__(self, *, max_concurrency: int) -> None:
        self.max_concurrency = max_concurrency
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="trigger-execution",
        )

    def admit_rate(
        self,
        *,
        endpoint_slug: str,
        rate_limit_per_minute: int,
        burst_limit: int,
    ) -> bool:
        now = time.monotonic()
        window_start = now - 60.0
        with self._lock:
            hits = self._hits[endpoint_slug]
            while hits and hits[0] < window_start:
                hits.popleft()
            if len(hits) >= max(rate_limit_per_minute, 1):
                return False
            recent_burst = [item for item in hits if item >= now - 1.0]
            if len(recent_burst) >= max(burst_limit, 1):
                return False
            hits.append(now)
        return True

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> Future[Any]:
        acquired = (
            self._semaphore.acquire(blocking=True, timeout=max(timeout_seconds, 0.0))
            if timeout_seconds is not None
            else self._semaphore.acquire(blocking=False)
        )
        if not acquired:
            raise PlatformError(
                code="runtime_trigger_timeout",
                message="Trigger execution could not start before the endpoint timeout.",
                status_code=504,
                retryable=True,
                metadata={"max_concurrency": self.max_concurrency},
            )
        try:
            future = self._executor.submit(fn, *args, **kwargs)
        except Exception:
            self._semaphore.release()
            raise
        future.add_done_callback(lambda _future: self._semaphore.release())
        return future


def get_settings(request: Request):
    return request.app.state.settings


def _admission_controller(request: Request) -> TriggerAdmissionController:
    settings = request.app.state.settings
    controller = getattr(request.app.state, "trigger_admission_controller", None)
    if (
        controller is None
        or controller.max_concurrency != settings.runtime.trigger_max_concurrency
    ):
        controller = TriggerAdmissionController(
            max_concurrency=settings.runtime.trigger_max_concurrency
        )
        request.app.state.trigger_admission_controller = controller
    return controller


@router.get("/runtime/boundary", response_model=RuntimeBoundaryResponse)
def get_runtime_boundary() -> RuntimeBoundaryResponse:
    boundary = runtime_service_boundary(SERVICE_NAME)
    return RuntimeBoundaryResponse(
        service_name=boundary.service_name,
        responsibilities=list(boundary.responsibilities),
        non_responsibilities=list(boundary.non_responsibilities),
    )


async def _read_trigger_execution_payload(request: Request) -> RuntimeTriggerExecutionRequest:
    raw_body = (await request.body()).strip()
    payload_json: dict[str, Any] = {}
    if raw_body:
        try:
            decoded = json.loads(raw_body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlatformError(
                code="runtime_trigger_payload_invalid",
                message=(
                    "Trigger request body must be empty or contain a JSON object. "
                    "Content-Type application/json is accepted but not required."
                ),
                status_code=422,
                retryable=False,
            ) from exc
        if decoded is None:
            payload_json = {}
        elif isinstance(decoded, dict):
            payload_json = decoded
        elif isinstance(decoded, str):
            payload_json = {"trigger_id": decoded}
        else:
            raise PlatformError(
                code="runtime_trigger_payload_invalid",
                message="Trigger request body must be a JSON object, JSON string trigger id, or empty.",
                status_code=422,
                retryable=False,
            )

    query = request.query_params
    for key in ("trigger_id", "timeout_ms", "image_base64", "mime_type"):
        if key in query and key not in payload_json:
            payload_json[key] = query[key]

    try:
        return RuntimeTriggerExecutionRequest.model_validate(payload_json)
    except ValidationError as exc:
        raise PlatformError(
            code="runtime_trigger_payload_invalid",
            message="Trigger request payload is invalid.",
            detail=str(exc),
            status_code=422,
            retryable=False,
        ) from exc


@router.put(
    "/runtime/triggers/{endpoint_slug}",
    response_model=RuntimeTriggerExecutionResponse,
    responses={
        401: {"description": "Trigger authentication failed."},
        404: {"description": "Trigger endpoint was not found."},
        409: {"description": "Trigger endpoint is disabled."},
        422: {"description": "Trigger endpoint or request is invalid."},
        429: {"description": "Trigger endpoint is rate-limited or busy."},
        503: {"description": "Runtime instance is not ready."},
        504: {"model": RuntimeTriggerExecutionResponse, "description": "Trigger timed out."},
    },
    openapi_extra={
        "security": [{}, {"TriggerBearer": []}, {"TriggerApiKey": []}],
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "trigger_id": {"type": "string"},
                            "timeout_ms": {"type": "integer"},
                            "payload": {"type": "object"},
                            "image_base64": {"type": "string"},
                            "mime_type": {"type": "string"},
                            "board_format": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "format_id",
                                    "format_version",
                                    "width_mm",
                                    "thickness_mm",
                                ],
                                "properties": {
                                    "format_id": {"type": "string"},
                                    "format_version": {"type": "string"},
                                    "width_mm": {
                                        "type": "number",
                                        "exclusiveMinimum": 0,
                                        "maximum": 10000,
                                    },
                                    "thickness_mm": {
                                        "type": "number",
                                        "exclusiveMinimum": 0,
                                        "maximum": 2000,
                                    },
                                },
                            },
                            "images_by_role": {
                                "type": "object",
                                "properties": {
                                    "upper": {"type": "object"},
                                    "lower": {"type": "object"},
                                    "end_face": {"type": "object"},
                                },
                            },
                        },
                    }
                }
            },
        },
    },
)
def execute_trigger(
    endpoint_slug: str,
    response: Response,
    request: Request,
    payload: RuntimeTriggerExecutionRequest = Depends(_read_trigger_execution_payload),
    _bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    _legacy_key: str | None = Depends(_legacy_key_scheme),
    settings=Depends(get_settings),
) -> RuntimeTriggerExecutionResponse:
    endpoint = _load_endpoint_snapshot(settings, endpoint_slug)
    _authenticate_trigger_request(request, settings, endpoint)
    _validate_trigger_request_payload(payload)
    if not endpoint.enabled:
        raise PlatformError(
            code="runtime_trigger_endpoint_disabled",
            message="The pipeline HTTP trigger endpoint is disabled.",
            status_code=409,
            retryable=False,
            metadata={"trigger_endpoint_id": endpoint.endpoint_id, "endpoint_slug": endpoint.slug},
        )
    if not endpoint.readiness.ready:
        raise PlatformError(
            code="runtime_trigger_runtime_unavailable",
            message="The pipeline HTTP trigger endpoint is not ready.",
            detail=endpoint.readiness.reason,
            status_code=503,
            retryable=True,
            metadata=endpoint.readiness.model_dump(mode="json"),
        )

    controller = _admission_controller(request)
    if not controller.admit_rate(
        endpoint_slug=endpoint.slug,
        rate_limit_per_minute=endpoint.rate_limit_per_minute,
        burst_limit=endpoint.burst_limit,
    ):
        raise PlatformError(
            code="runtime_trigger_rate_limited",
            message="The pipeline HTTP trigger endpoint rate limit has been reached.",
            status_code=429,
            retryable=True,
            metadata={
                "trigger_endpoint_id": endpoint.endpoint_id,
                "endpoint_slug": endpoint.slug,
                "rate_limit_per_minute": endpoint.rate_limit_per_minute,
                "burst_limit": endpoint.burst_limit,
            },
        )

    request_id = get_request_id()
    correlation_id = (
        get_correlation_id()
        or request_id
        or payload.trigger_id
        or f"trg_{endpoint.slug}"
    )
    effective_timeout_ms = min(endpoint.timeout_ms, payload.timeout_ms or endpoint.timeout_ms)
    effective_timeout_seconds = effective_timeout_ms / 1000
    started = time.perf_counter()
    remaining_timeout_seconds = max(
        effective_timeout_seconds - (time.perf_counter() - started),
        0.001,
    )
    future = controller.submit(
        _execute_trigger_job,
        settings,
        endpoint.endpoint_id,
        payload.model_dump(mode="json"),
        request_id,
        correlation_id,
        effective_timeout_ms,
        timeout_seconds=remaining_timeout_seconds,
    )
    try:
        remaining_timeout_seconds = max(
            effective_timeout_seconds - (time.perf_counter() - started),
            0.001,
        )
        result = future.result(timeout=remaining_timeout_seconds)
    except FutureTimeoutError:
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        with session_scope(settings) as session:
            record_trigger_timeout_result(
                session,
                endpoint_id=endpoint.endpoint_id,
                correlation_id=correlation_id,
                timeout_ms=effective_timeout_ms,
                latency_ms=latency_ms,
            )
        response.status_code = 504
        return _timeout_response(
            endpoint=endpoint,
            payload=payload,
            request_id=request_id,
            correlation_id=correlation_id,
            timeout_ms=effective_timeout_ms,
            latency_ms=latency_ms,
        )
    except PlatformError as exc:
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        if exc.code == "runtime_trigger_timeout":
            with session_scope(settings) as session:
                record_trigger_timeout_result(
                    session,
                    endpoint_id=endpoint.endpoint_id,
                    correlation_id=correlation_id,
                    timeout_ms=effective_timeout_ms,
                    latency_ms=latency_ms,
                )
            response.status_code = 504
            return _timeout_response(
                endpoint=endpoint,
                payload=payload,
                request_id=request_id,
                correlation_id=correlation_id,
                timeout_ms=effective_timeout_ms,
                latency_ms=latency_ms,
            )
        with session_scope(settings) as session:
            mark_trigger_endpoint_invoked(
                session,
                endpoint_id=endpoint.endpoint_id,
                status="rejected" if exc.status_code < 500 else "failed",
                latency_ms=latency_ms,
                error_code=exc.code,
                error_message=exc.message,
            )
        raise
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        with session_scope(settings) as session:
            mark_trigger_endpoint_invoked(
                session,
                endpoint_id=endpoint.endpoint_id,
                status="failed",
                latency_ms=latency_ms,
                error_code="runtime_trigger_execution_failed",
                error_message="Trigger execution failed unexpectedly.",
            )
        raise PlatformError(
            code="runtime_trigger_execution_failed",
            message="Trigger execution failed unexpectedly.",
            status_code=500,
            retryable=True,
        ) from exc
    if result.status == "failed":
        response.status_code = 502
    return result


def _load_endpoint_snapshot(settings, endpoint_slug: str) -> TriggerEndpointSnapshot:
    with session_scope(settings) as session:
        row = get_trigger_endpoint_for_execution(session, endpoint_slug)
        if row is None:
            raise NotFoundError(
                message="Pipeline HTTP trigger endpoint not found",
                detail=f"No pipeline HTTP trigger endpoint exists for {endpoint_slug}.",
            )
        readiness = trigger_endpoint_readiness(session, row)
        return TriggerEndpointSnapshot(
            endpoint_id=row.id,
            slug=row.slug,
            channel_id=row.channel_id,
            pipeline_version_id=row.pipeline_version_id,
            timeout_ms=int(row.timeout_ms or settings.runtime.trigger_timeout_ms),
            rate_limit_per_minute=int(row.rate_limit_per_minute or 60),
            burst_limit=int(row.burst_limit or 4),
            auth_required=bool(row.auth_required),
            auth_scope=row.auth_scope,
            enabled=bool(row.enabled),
            readiness=readiness,
        )


def _authenticate_trigger_request(
    request: Request,
    settings,
    endpoint: TriggerEndpointSnapshot,
) -> MachineIdentity | None:
    machine_token = parse_machine_token(
        authorization_header=request.headers.get(settings.api_keys.authorization_header_name),
        legacy_api_key_header=request.headers.get(settings.api_keys.legacy_header_name),
    )
    if not endpoint.auth_required and machine_token is None:
        return None
    if not settings.api_keys.enabled:
        raise PlatformError(
            code="runtime_trigger_auth_unavailable",
            message=(
                "This pipeline HTTP trigger endpoint requires API-key authentication, "
                "but API keys are disabled."
            ),
            status_code=503,
            retryable=False,
        )
    if machine_token is None:
        raise AuthenticationError("Machine authentication is required for trigger execution.")
    key_id, secret = machine_token
    return authenticate_machine_identity(
        settings,
        key_id=key_id,
        secret=secret,
        required_scopes={endpoint.auth_scope},
    )


def _execute_trigger_job(
    settings,
    endpoint_id: str,
    payload_json: dict[str, Any],
    request_id: str | None,
    correlation_id: str,
    effective_timeout_ms: int,
) -> RuntimeTriggerExecutionResponse:
    started = time.perf_counter()
    payload = RuntimeTriggerExecutionRequest.model_validate(payload_json)
    with session_scope(settings) as session:
        endpoint = session.get(TriggerEndpoint, endpoint_id)
        if endpoint is None:
            raise NotFoundError("Trigger endpoint not found", detail=endpoint_id)
        if not bool(endpoint.enabled):
            raise PlatformError(
                code="runtime_trigger_endpoint_disabled",
                message="The pipeline HTTP trigger endpoint is disabled.",
                status_code=409,
                retryable=False,
            )
        channel = session.get(RuntimeChannel, endpoint.channel_id)
        execution_id = channel.active_runtime_execution_id if channel else ""
        pipeline_id = endpoint.pipeline_version_id or (channel.pipeline_version_id if channel else "")
        lock_runtime_use(session, execution_id or "")
        execution = require_runtime_pipeline(
            session, execution_id=execution_id or "", pipeline_version_id=pipeline_id or "",
        )
        try:
            loaded = load_published_runtime_pipeline(
                session,
                channel_id=endpoint.channel_id,
                pipeline_version_id=endpoint.pipeline_version_id,
            )
        except (ValueError, RuntimeGraphBuildError) as exc:
            raise PlatformError(
                code="runtime_trigger_runtime_unavailable",
                message="The triggerable pipeline is not ready.",
                detail=str(exc),
                status_code=422,
                retryable=True,
            ) from exc
        if RuntimeMode(loaded.version.mode) != RuntimeMode.TRIGGER:
            raise PlatformError(
                code="runtime_trigger_invalid_endpoint_config",
                message="HTTP trigger endpoints require a published triggerable pipeline.",
                status_code=422,
                retryable=False,
                metadata={"pipeline_version_id": loaded.version.id},
            )

        source_type = RuntimeSourceType(loaded.source.source_type) if loaded.source else None
        source_config = dict(loaded.source.config or {}) if loaded.source is not None else {}
        source_secret_refs = (
            dict(loaded.source.secret_refs or {}) if loaded.source is not None else {}
        )
        synchronized_config = _synchronized_camera_source_config(loaded.graph)
        single_camera_test_config = _single_board_camera_source_config(loaded.graph)
        test_photo_config = _test_photo_source_config(loaded.graph)
        input_frames: dict[str, FramePayload] = {}
        synchronized_frame_set: SynchronizedFrameSetPayload | None = None
        if synchronized_config is not None:
            bindings = _camera_capture_bindings(session, synchronized_config)
            input_frames, synchronized_frame_set = _capture_synchronized_camera_frames(
                settings=settings,
                bindings=bindings,
                payload=payload,
                timeout_ms=min(
                    int(synchronized_config.get("capture_timeout_ms", effective_timeout_ms)),
                    effective_timeout_ms,
                ),
                max_capture_skew_ms=max(
                    float(synchronized_config.get("max_capture_skew_ms", 50.0)), 0.0
                ),
                correlation_id=correlation_id,
            )
            frame = input_frames.get("upper") or next(iter(input_frames.values()), None)
        elif single_camera_test_config is not None:
            binding = _single_board_camera_capture_binding(
                session, single_camera_test_config
            )
            input_frames, synchronized_frame_set = _capture_single_board_camera_frame(
                settings=settings,
                binding=binding,
                camera_role=str(
                    single_camera_test_config.get("camera_role") or "upper"
                ),
                payload=payload,
                timeout_ms=effective_timeout_ms,
                correlation_id=correlation_id,
            )
            frame = next(iter(input_frames.values()), None)
        elif test_photo_config is not None:
            # The graph source materializes its embedded photo. Avoid touching the
            # channel camera so tests stay deterministic and work while it is offline.
            frame = None
        else:
            frame = _capture_trigger_frame(
                session=session,
                settings=settings,
                channel_id=loaded.channel.id,
                payload=payload,
                source_type=source_type,
                source_config=source_config,
                source_secret_refs=source_secret_refs,
                timeout_ms=effective_timeout_ms,
            )
        trigger = TriggerPayload(
            trigger_id=payload.trigger_id or correlation_id,
            body={
                **payload.payload,
                **(
                    {"board_format": payload.board_format.model_dump(mode="json")}
                    if payload.board_format is not None
                    else {}
                ),
                "trigger_endpoint_id": endpoint.id,
                "endpoint_slug": endpoint.slug,
            },
        )
        context = RuntimeExecutionContext(
            correlation_id=correlation_id,
            channel_id=loaded.channel.id,
            pipeline_version_id=loaded.version.id,
            runtime_execution_id=execution.id,
            trigger_endpoint_id=endpoint.id,
            mode=RuntimeMode.TRIGGER,
            source_config=source_config,
            input_frame=frame,
            input_frames=input_frames,
            synchronized_frame_set=synchronized_frame_set,
            trigger=trigger,
            inference_client=HttpInferenceGatewayClient(
                base_url=settings.runtime.inference_gateway_realtime_url,
                bearer_token=settings.runtime.inference_gateway_bearer_token(),
                timeout_seconds=min(
                    settings.inference_limits.realtime_timeout_ms,
                    effective_timeout_ms,
                )
                / 1000,
                overload_policy="wait",
                runtime_execution_id=execution.id,
                pipeline_version_id=loaded.version.id,
            ),
            model_resolver=build_execution_model_resolver(session, execution, loaded.version.id),
            metadata={
                "executor_service": SERVICE_NAME,
                "trigger_endpoint_id": endpoint.id,
                "endpoint_slug": endpoint.slug,
                "request_id": request_id,
                "execution_lifecycle": "synchronous_pipeline_http_trigger_invocation",
                "default_inference_timeout_ms": int(
                    min(settings.inference_limits.realtime_timeout_ms, effective_timeout_ms)
                ),
                "plc_writes_allowed": bool(settings.runtime.plc_writes_allowed),
                "capture_skew_ms": synchronized_frame_set.capture_skew_ms
                if synchronized_frame_set is not None
                else 0.0,
            },
        )
        try:
            if synchronized_frame_set is not None and not synchronized_frame_set.valid:
                result = _invalid_synchronized_capture_result(
                    synchronized_frame_set,
                    channel_id=loaded.channel.id,
                    pipeline_version_id=loaded.version.id,
                    correlation_id=correlation_id,
                    started_at=context.now_iso(),
                    inspection_length_mm=_board_inspection_length(loaded.graph),
                )
            else:
                result = loaded.executable.execute(context)
        finally:
            close_siemens_sps_connection_cache(context.state)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        if elapsed_ms > effective_timeout_ms:
            raise PlatformError(
                code="runtime_trigger_timeout",
                message="Trigger execution exceeded the endpoint timeout.",
                status_code=504,
                retryable=True,
                metadata={"timeout_ms": effective_timeout_ms, "latency_ms": elapsed_ms},
            )
        mapping_error = _apply_endpoint_response_mapping(endpoint, result)
        status = result.status
        error_code = None
        error_message = None
        if mapping_error is not None:
            status = "failed"
            error_code = "runtime_trigger_response_mapping_failed"
            error_message = mapping_error
            result.errors.append(
                {
                    "code": error_code,
                    "message": mapping_error,
                    "retryable": False,
                }
            )
        elif result.errors:
            first_error = result.errors[0]
            error_code = str(first_error.get("code") or "runtime_trigger_pipeline_failed")
            error_message = str(first_error.get("message") or "Pipeline execution failed.")
        result.status = status
        bark_job_data = payload.payload.get("bark_job")
        is_bark_job = bark_job_data is not None and any(
            node.node_type == "source.stem_csv_measurements" for node in loaded.graph.nodes
        )
        result_record = record_runtime_execution_result(
            session,
            result,
            channel_id=loaded.channel.id,
            runtime_execution_id=execution.id,
            trigger_endpoint_id=endpoint.id,
            retain_history=True if is_bark_job else None,
        )
        if is_bark_job and frame is not None:
            from cv_contracts.bark import StemBarkJob
            from cv_db.bark_artifacts import store_bark_artifacts

            try:
                bark_job = StemBarkJob.model_validate(bark_job_data)
            except ValueError:
                # The CSV source already reports malformed job data as a node error.
                bark_job = None
            if bark_job is not None:
                # Keep explicitly submitted inspection failures for review as well.
                result_record.history_retained = True
                store_bark_artifacts(settings, result_record, result, frame, bark_job)
                result.final_response["result_record_id"] = result_record.id
                result.final_response["artifacts"] = result_record.runtime_metadata[
                    "bark_artifacts"
                ]
                result_record.trigger_response = dict(result.final_response)
        latency_ms = float(
            result.timing.get("total_ms")
            or round((time.perf_counter() - started) * 1000, 3)
        )
        mark_trigger_endpoint_invoked(
            session,
            endpoint_id=endpoint.id,
            status=status,  # type: ignore[arg-type]
            latency_ms=latency_ms,
            error_code=error_code,
            error_message=error_message,
        )
        _write_trigger_live_view_state(
            settings=settings,
            endpoint=endpoint,
            result=result,
            fallback_frame=frame,
        )
        return RuntimeTriggerExecutionResponse(
            status=status,  # type: ignore[arg-type]
            trigger_endpoint_id=endpoint.id,
            endpoint_slug=endpoint.slug,
            channel_id=loaded.channel.id,
            pipeline_version_id=loaded.version.id,
            runtime_execution_id=execution.id,
            trigger_id=payload.trigger_id or correlation_id,
            correlation_id=correlation_id,
            final_response=result.final_response,
            execution_result=result.model_dump(mode="json"),
            overlay_outputs=result.overlay_outputs,
            capture_intents=result.capture_intents,
            http_trigger_responses=result.http_trigger_responses,
            persisted_results=result.persisted_results,
            plc_write_outputs=result.plc_write_outputs,
            projected_results=result.projected_results,
            errors=result.errors,
            timing=result.timing,
            error_code=error_code,
            error_message=error_message,
            retryable=bool(
                result.errors and any(error.get("retryable") for error in result.errors)
            ),
            meta={
                "request_id": request_id,
                "correlation_id": correlation_id,
                "generated_at": utc_now_iso(),
            },
        )


def _write_trigger_live_view_state(
    *,
    settings,
    endpoint: TriggerEndpoint,
    result,
    fallback_frame: FramePayload,
) -> None:
    try:
        display = _display_selection_from_result(result)
        display_context_id = _string_or_none(display.get("display_context_id"))
        display_context = (
            dict(display.get("display_context"))
            if isinstance(display.get("display_context"), dict)
            else {}
        )
        display_frame = _display_frame_from_result(
            result,
            display_context_id=display_context_id,
            fallback_frame=fallback_frame,
        )
        overlay = _overlay_from_trigger_result(result, display_context_id=display_context_id)
        layers = sorted({primitive.layer for primitive in overlay})
        state = RuntimeLiveViewState(
            channel_id=endpoint.channel_id,
            runtime_instance_id=trigger_live_view_runtime_id(endpoint.channel_id),
            actual_state=RuntimeObservedState.STOPPED,
            available=True,
            stale=False,
            display_context_id=display_context_id,
            display_context=display_context,
            frame=RuntimeLiveFrame(
                image_base64=display_frame.image_base64,
                mime_type=display_frame.mime_type,
                width=display_frame.width,
                height=display_frame.height,
                source_timestamp=display_frame.timestamp,
                captured_at=utc_now_iso(),
                sequence=int(endpoint.invocation_count or 0),
                image_context=(
                    display_frame.image_context.model_dump(mode="json")
                    if display_frame.image_context is not None
                    else {}
                ),
                metadata={
                    "trigger_endpoint_id": endpoint.id,
                    "endpoint_slug": endpoint.slug,
                    "source_uri": display_frame.source_uri,
                },
            ),
            source_frame=RuntimeLiveFrame(
                image_base64=fallback_frame.image_base64_value(),
                mime_type=fallback_frame.mime_type,
                width=fallback_frame.width,
                height=fallback_frame.height,
                source_timestamp=fallback_frame.timestamp,
                captured_at=utc_now_iso(),
                sequence=int(endpoint.invocation_count or 0),
                image_context=(
                    fallback_frame.image_context.model_dump(mode="json")
                    if fallback_frame.image_context is not None
                    else {}
                ),
                metadata={
                    **dict(fallback_frame.metadata or {}),
                    "trigger_endpoint_id": endpoint.id,
                    "endpoint_slug": endpoint.slug,
                    "source_uri": fallback_frame.source_uri,
                    "frame_role": "pipeline_input",
                },
            ),
            overlay=overlay,
            layers_available=layers,
            metrics=RuntimeLiveMetrics(
                frame_count=int(endpoint.invocation_count or 0),
                latency_ms=float(result.timing.get("total_ms") or 0),
                last_frame_at=utc_now_iso(),
            ),
            source_health=RuntimeLiveSourceHealth(
                state=HealthState.OK,
                status_text="Latest trigger frame",
                connected=True,
            ),
            diagnostics=RuntimeLiveDiagnostics(
                plc_write_outputs=result.plc_write_outputs,
                stream_transport="trigger-live-view",
            ),
            updated_at=utc_now_iso(),
        )
        write_runtime_live_view_state(settings, state, split_frame_payload=True)
    except Exception:
        return


def _display_selection_from_result(result) -> dict[str, Any]:
    if result.display_outputs:
        payload = result.display_outputs[-1].get("payload")
        return dict(payload) if isinstance(payload, dict) else {}
    frame = _default_trigger_display_frame_payload(result)
    if frame and isinstance(frame.get("image_context"), dict):
        context = dict(frame["image_context"])
        return {
            "display_context_id": context.get("context_id"),
            "display_context": context,
            "frame": frame,
            "selection": "default_latest_trigger_visual_context",
        }
    return {}


def _display_frame_from_result(
    result,
    *,
    display_context_id: str | None,
    fallback_frame: FramePayload,
) -> FramePayload:
    frames = _frame_payloads_from_result(result)
    if not display_context_id:
        frame_payload = _default_trigger_display_frame_payload(result)
        if frame_payload is not None:
            try:
                return FramePayload.model_validate(frame_payload)
            except Exception:
                pass
        return fallback_frame
    for frame_payload in reversed(frames):
        context = frame_payload.get("image_context")
        context_id = context.get("context_id") if isinstance(context, dict) else None
        if display_context_id and context_id != display_context_id:
            continue
        try:
            return FramePayload.model_validate(frame_payload)
        except Exception:
            continue
    return fallback_frame


def _default_trigger_display_frame_payload(result) -> dict[str, Any] | None:
    frames = _frame_payloads_from_result(result)
    for frame in frames:
        context = frame.get("image_context")
        if isinstance(context, dict) and context.get("kind") == "original":
            return frame
    for frame in frames:
        if not _frame_payload_is_mask_artifact(frame):
            return frame
    return None


def _frame_payload_is_mask_artifact(frame: dict[str, Any]) -> bool:
    context = frame.get("image_context")
    if not isinstance(context, dict):
        return False
    transform = context.get("transform")
    if isinstance(transform, dict) and transform.get("transform_type") == "mask":
        return True
    metadata = context.get("metadata")
    if isinstance(metadata, dict) and metadata.get("operation") in {
        "color_object_segmentation_mask",
        "canny_edge_detection",
    }:
        return True
    return context.get("kind") in {"masked", "processed"} and str(
        frame.get("mime_type") or ""
    ).startswith("image/")


def _frame_payloads_from_result(result) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for outputs in result.node_outputs.values():
        if not isinstance(outputs, dict):
            continue
        for payload in outputs.values():
            if isinstance(payload, dict) and payload.get("payload_type") == "frame":
                frames.append(payload)
    for entry in result.display_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        frame = payload.get("frame") if isinstance(payload, dict) else None
        if isinstance(frame, dict) and frame.get("payload_type") == "frame":
            frames.append(frame)
    return frames


def _overlay_from_trigger_result(
    result,
    *,
    display_context_id: str | None,
) -> list[RuntimeOverlayPrimitiveState]:
    primitives: list[RuntimeOverlayPrimitiveState] = []
    for entry in result.overlay_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        for primitive in payload.get("primitives") or []:
            if isinstance(primitive, dict) and _primitive_matches_display(primitive, display_context_id):
                primitives.append(
                    _primitive_from_payload(
                        primitive,
                        default_layer="overlay",
                        display_context_id=display_context_id,
                    )
                )
    owned_inference_payload_types = _explicit_overlay_owned_payload_types(
        result.overlay_outputs
    )
    for entry in result.normalized_inference_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("payload_type")
        if payload_type in owned_inference_payload_types:
            continue
        image_context_id = _string_or_none(payload.get("image_context_id"))
        if not _context_matches_display(image_context_id, display_context_id):
            continue
        if payload_type == "classifications":
            top = next((item for item in payload.get("labels") or [] if isinstance(item, dict)), None)
            if top is not None:
                primitives.append(
                    RuntimeOverlayPrimitiveState(
                        primitive_type="label",
                        layer="labels",
                        label=_string_or_none(top.get("label")),
                        confidence=_float_or_none(top.get("score")),
                        geometry={},
                        image_context_id=image_context_id,
                        display_context_id=display_context_id,
                        metadata={"source": "classification"},
                    )
                )
        if payload_type == "detections":
            for item in payload.get("detections") or []:
                if isinstance(item, dict):
                    primitives.append(
                        RuntimeOverlayPrimitiveState(
                            primitive_type="box",
                            layer="detections",
                            label=_string_or_none(item.get("label")),
                            confidence=_float_or_none(item.get("score")),
                            geometry=dict(item.get("box") or {}),
                            image_context_id=image_context_id,
                            display_context_id=display_context_id,
                        )
                    )
        if payload_type == "segments":
            for item in payload.get("segments") or []:
                if isinstance(item, dict):
                    primitives.append(
                        RuntimeOverlayPrimitiveState(
                            primitive_type="polygon",
                            layer="segmentation",
                            label=_string_or_none(item.get("label")),
                            confidence=_float_or_none(item.get("score")),
                            geometry={"points": item.get("polygon") or []},
                            coordinate_system="normalized_xy",
                            image_context_id=image_context_id,
                            display_context_id=display_context_id,
                        )
                    )
    return primitives


def _explicit_overlay_owned_payload_types(
    overlay_outputs: list[dict[str, Any]],
) -> set[str]:
    supported_payload_types = {
        "detections",
        "segments",
        "classifications",
        "anomaly",
    }
    owned_payload_types: set[str] = set()
    for entry in overlay_outputs:
        if not isinstance(entry, dict) or entry.get("node_type") != "output.overlay":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            continue
        source_payload_type = str(metadata.get("source_payload_type") or "")
        if source_payload_type in supported_payload_types:
            owned_payload_types.add(source_payload_type)
    return owned_payload_types


def _primitive_from_payload(
    primitive: dict[str, Any],
    *,
    default_layer: str,
    display_context_id: str | None,
) -> RuntimeOverlayPrimitiveState:
    return RuntimeOverlayPrimitiveState(
        primitive_type=str(primitive.get("primitive_type") or "label"),  # type: ignore[arg-type]
        layer=str(primitive.get("layer") or primitive.get("metadata", {}).get("layer") or default_layer),
        label=_string_or_none(primitive.get("label")),
        confidence=_float_or_none(primitive.get("confidence")),
        geometry=dict(primitive.get("geometry") or {}),
        image_context_id=_string_or_none(primitive.get("image_context_id")),
        display_context_id=display_context_id,
        object_id=_string_or_none(primitive.get("object_id")),
        source_object_id=_string_or_none(primitive.get("source_object_id")),
        projected_from_context_id=_string_or_none(primitive.get("projected_from_context_id")),
        style=dict(primitive.get("style") or {}),
        metadata=dict(primitive.get("metadata") or {}),
    )


def _primitive_matches_display(
    primitive: dict[str, Any],
    display_context_id: str | None,
) -> bool:
    primitive_context_id = _string_or_none(
        primitive.get("display_context_id") or primitive.get("image_context_id")
    )
    return _context_matches_display(primitive_context_id, display_context_id)


def _context_matches_display(
    image_context_id: str | None,
    display_context_id: str | None,
) -> bool:
    return display_context_id is None or image_context_id in {None, display_context_id}


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _float_or_none(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _synchronized_camera_source_config(
    graph: RuntimePipelineGraph,
) -> dict[str, Any] | None:
    for node in graph.nodes:
        if node.node_type == "source.synchronized_camera_frames":
            return dict(node.config or {})
    return None


def _single_board_camera_source_config(
    graph: RuntimePipelineGraph,
) -> dict[str, Any] | None:
    for node in graph.nodes:
        if node.node_type == "source.single_board_camera_test":
            return dict(node.config or {})
    return None


def _test_photo_source_config(
    graph: RuntimePipelineGraph,
) -> dict[str, Any] | None:
    for node in graph.nodes:
        if node.node_type == "source.test_photo":
            return dict(node.config or {})
    return None


def _board_inspection_length(graph: RuntimePipelineGraph) -> float:
    for node in graph.nodes:
        if node.node_type in {
            "analysis.multi_view_board_cut_fusion",
            "analysis.single_view_board_cut_decision",
        }:
            try:
                value = float((node.config or {}).get("inspection_length_mm", 300.0))
            except (TypeError, ValueError):
                return 300.0
            return value if value > 0 else 300.0
    return 300.0


def _camera_capture_bindings(
    session: Session,
    config: dict[str, Any],
) -> dict[str, CameraCaptureBinding]:
    bindings: dict[str, CameraCaptureBinding] = {}
    for role in ("upper", "lower", "end_face"):
        channel_id = str(config.get(f"{role}_channel_id") or "").strip()
        if not channel_id:
            continue
        channel = session.get(RuntimeChannel, channel_id)
        source = (
            session.get(RuntimeSource, channel.source_id)
            if channel is not None and channel.deleted_at is None and channel.source_id
            else None
        )
        if source is None:
            continue
        try:
            source_type = RuntimeSourceType(source.source_type)
        except ValueError:
            source_type = None
        bindings[role] = CameraCaptureBinding(
            role=role,
            channel_id=channel_id,
            source_type=source_type,
            source_config=dict(source.config or {}),
            source_secret_refs=dict(source.secret_refs or {}),
        )
    return bindings


def _single_board_camera_capture_binding(
    session: Session,
    config: dict[str, Any],
) -> CameraCaptureBinding | None:
    role = str(config.get("camera_role") or "upper")
    if role not in {"upper", "lower", "end_face"}:
        return None
    channel_id = str(config.get("camera_channel_id") or "").strip()
    if not channel_id:
        return None
    return _camera_capture_bindings(
        session,
        {f"{role}_channel_id": channel_id},
    ).get(role)


def _capture_single_board_camera_frame(
    *,
    settings: PlatformSettings,
    binding: CameraCaptureBinding | None,
    camera_role: str,
    payload: RuntimeTriggerExecutionRequest,
    timeout_ms: int,
    correlation_id: str,
) -> tuple[dict[str, FramePayload], SynchronizedFrameSetPayload]:
    role = camera_role if camera_role in {"upper", "lower", "end_face"} else "upper"
    started = time.monotonic()
    frames: dict[str, FramePayload] = {}
    error: str | None = None
    if binding is None or binding.role != role:
        error = "camera_binding_missing"
    else:
        inline = payload.images_by_role.get(role)  # type: ignore[arg-type]
        role_payload = payload.model_copy(
            update={
                "image_base64": (
                    inline.image_base64 if inline is not None else payload.image_base64
                ),
                "mime_type": inline.mime_type if inline is not None else payload.mime_type,
                "images_by_role": {},
            }
        )
        try:
            with session_scope(settings) as capture_session:
                frame = _capture_trigger_frame(
                    session=capture_session,
                    settings=settings,
                    channel_id=binding.channel_id,
                    payload=role_payload,
                    source_type=binding.source_type,
                    source_config=binding.source_config,
                    source_secret_refs=binding.source_secret_refs,
                    timeout_ms=timeout_ms,
                )
            captured_at = inline.captured_at if inline is not None else frame.timestamp
            frame.timestamp = captured_at or frame.timestamp or utc_now_iso()
            frame.metadata = {
                **frame.metadata,
                "camera_role": role,
                "channel_id": binding.channel_id,
                "captured_at": frame.timestamp,
                "trigger_id": payload.trigger_id or correlation_id,
                "single_camera_test": True,
            }
            if _parse_capture_timestamp(frame.timestamp) is None:
                error = "capture_timestamp_invalid"
            else:
                frames[role] = frame
        except PlatformError as exc:
            error = exc.code
        except Exception:
            error = "camera_capture_failed"

    if error is None:
        status, valid, reason = "captured", True, None
    elif "timeout" in error:
        status, valid, reason = "capture_timeout", False, f"{role}:{error}"
    elif error == "capture_timestamp_invalid":
        status, valid, reason = "capture_not_synchronized", False, f"{role}:{error}"
    else:
        status, valid, reason = "camera_missing", False, f"{role}:{error}"
    synchronized_frames: list[SynchronizedCameraFrame] = []
    if role in frames and binding is not None:
        synchronized_frames.append(
            SynchronizedCameraFrame(
                role=role,  # type: ignore[arg-type]
                channel_id=binding.channel_id,
                frame=frames[role],
                captured_at=frames[role].timestamp,
                capture_offset_ms=0.0,
            )
        )
    frame_set = SynchronizedFrameSetPayload(
        workpiece_id=str(
            payload.payload.get("workpiece_id")
            or payload.payload.get("sequence_id")
            or correlation_id
        ),
        trigger_id=payload.trigger_id or correlation_id,
        frames=synchronized_frames,
        capture_skew_ms=0.0,
        max_capture_skew_ms=0.0,
        valid=valid,
        status=status,  # type: ignore[arg-type]
        reason_code=reason,
        metadata={
            "single_camera_test": True,
            "camera_role": role,
            "capture_elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        },
    )
    return frames, frame_set


def _capture_synchronized_camera_frames(
    *,
    settings: PlatformSettings,
    bindings: dict[str, CameraCaptureBinding],
    payload: RuntimeTriggerExecutionRequest,
    timeout_ms: int,
    max_capture_skew_ms: float,
    correlation_id: str,
) -> tuple[dict[str, FramePayload], SynchronizedFrameSetPayload]:
    roles = ("upper", "lower", "end_face")
    started = time.monotonic()
    deadline = started + max(timeout_ms / 1000.0, 0.05)
    frames: dict[str, FramePayload] = {}
    errors: dict[str, str] = {}

    def capture(binding: CameraCaptureBinding) -> FramePayload:
        inline = payload.images_by_role.get(binding.role)  # type: ignore[arg-type]
        role_payload = payload.model_copy(
            update={
                "image_base64": inline.image_base64 if inline is not None else None,
                "mime_type": inline.mime_type if inline is not None else payload.mime_type,
                "images_by_role": {},
            }
        )
        with session_scope(settings) as capture_session:
            frame = _capture_trigger_frame(
                session=capture_session,
                settings=settings,
                channel_id=binding.channel_id,
                payload=role_payload,
                source_type=binding.source_type,
                source_config=binding.source_config,
                source_secret_refs=binding.source_secret_refs,
                timeout_ms=timeout_ms,
            )
        captured_at = inline.captured_at if inline is not None else frame.timestamp
        frame.timestamp = captured_at or frame.timestamp or utc_now_iso()
        frame.metadata = {
            **frame.metadata,
            "camera_role": binding.role,
            "channel_id": binding.channel_id,
            "captured_at": frame.timestamp,
            "trigger_id": payload.trigger_id or correlation_id,
        }
        return frame

    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="board-camera-capture")
    try:
        futures = {
            role: pool.submit(capture, bindings[role])
            for role in roles
            if role in bindings
        }
        for role in roles:
            future = futures.get(role)
            if future is None:
                errors[role] = "camera_binding_missing"
                continue
            remaining = max(deadline - time.monotonic(), 0.0)
            try:
                frames[role] = future.result(timeout=remaining)
            except FutureTimeoutError:
                future.cancel()
                errors[role] = "capture_timeout"
            except PlatformError as exc:
                errors[role] = exc.code
            except Exception:
                errors[role] = "camera_capture_failed"
    finally:
        # A context-manager shutdown waits for timed-out capture threads and would
        # silently defeat the configured fail-closed deadline.
        pool.shutdown(wait=False, cancel_futures=True)

    timestamps_by_role = {
        role: _parse_capture_timestamp(frame.timestamp) for role, frame in frames.items()
    }
    for role, captured_at in timestamps_by_role.items():
        if captured_at is None:
            errors.setdefault(role, "capture_timestamp_invalid")
    timestamps = [captured_at for captured_at in timestamps_by_role.values() if captured_at]
    capture_skew_ms = (
        max((max(timestamps) - min(timestamps)).total_seconds() * 1000.0, 0.0)
        if len(timestamps) == len(roles)
        else 0.0
    )
    synchronized_frames: list[SynchronizedCameraFrame] = []
    earliest = min(timestamps) if len(timestamps) == len(roles) else None
    for role in roles:
        frame = frames.get(role)
        binding = bindings.get(role)
        if frame is None or binding is None:
            continue
        captured_at = _parse_capture_timestamp(frame.timestamp)
        offset = (
            max((captured_at - earliest).total_seconds() * 1000.0, 0.0)
            if captured_at is not None and earliest is not None
            else 0.0
        )
        synchronized_frames.append(
            SynchronizedCameraFrame(
                role=role,  # type: ignore[arg-type]
                channel_id=binding.channel_id,
                frame=frame,
                captured_at=frame.timestamp,
                capture_offset_ms=round(offset, 3),
            )
        )

    if errors:
        if any(error == "capture_timeout" for error in errors.values()):
            status = "capture_timeout"
        elif any(error == "capture_timestamp_invalid" for error in errors.values()):
            status = "capture_not_synchronized"
        else:
            status = "camera_missing"
        valid = False
        reason = ";".join(f"{role}:{errors[role]}" for role in roles if role in errors)
    elif capture_skew_ms > max_capture_skew_ms:
        status = "capture_not_synchronized"
        valid = False
        reason = "capture_skew_exceeded"
    else:
        status = "captured"
        valid = True
        reason = None

    frame_set = SynchronizedFrameSetPayload(
        workpiece_id=str(
            payload.payload.get("workpiece_id")
            or payload.payload.get("sequence_id")
            or correlation_id
        ),
        trigger_id=payload.trigger_id or correlation_id,
        frames=synchronized_frames,
        capture_skew_ms=round(capture_skew_ms, 3),
        max_capture_skew_ms=max_capture_skew_ms,
        valid=valid,
        status=status,  # type: ignore[arg-type]
        reason_code=reason,
        metadata={"capture_elapsed_ms": round((time.monotonic() - started) * 1000.0, 3)},
    )
    return frames, frame_set


def _parse_capture_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _invalid_synchronized_capture_result(
    frame_set: SynchronizedFrameSetPayload,
    *,
    channel_id: str,
    pipeline_version_id: str,
    correlation_id: str,
    started_at: str,
    inspection_length_mm: float,
) -> RuntimeExecutionResult:
    completed_at = utc_now_iso()
    decision = {
        "schema_version": "board.cut-decision.v1",
        "workpiece_id": frame_set.workpiece_id,
        "valid": False,
        "status": frame_set.status,
        "cut_position_mm": None,
        "longitudinal_shift_mm": None,
        "inspection_length_mm": inspection_length_mm,
        "capture_skew_ms": frame_set.capture_skew_ms,
        "views_used": [entry.role for entry in frame_set.frames],
        "uncertainty_mm": 0.0,
        "dominant_constraint": None,
        "reason_code": frame_set.reason_code,
    }
    guard_node_type = (
        "analysis.single_view_board_cut_decision"
        if bool(frame_set.metadata.get("single_camera_test"))
        else "analysis.multi_view_board_cut_fusion"
    )
    derived_entry = {
        "node_key": "synchronized_capture_guard",
        "node_type": guard_node_type,
        "node_category": "analysis",
        "output_port": "decision",
        "payload_type": "board_cut_decision",
        "payload": {"payload_type": "board_cut_decision", "metadata": {}, "decision": decision},
    }
    return RuntimeExecutionResult(
        status="completed",
        channel_id=channel_id,
        pipeline_version_id=pipeline_version_id,
        correlation_id=correlation_id,
        started_at=started_at,
        completed_at=completed_at,
        derived_outputs=[derived_entry],
        synchronized_frame_sets=[
            {
                "node_key": "test_camera"
                if bool(frame_set.metadata.get("single_camera_test"))
                else "board_cameras",
                "node_type": "source.single_board_camera_test"
                if bool(frame_set.metadata.get("single_camera_test"))
                else "source.synchronized_camera_frames",
                "node_category": "source",
                "output_port": "frame_set",
                "payload_type": "synchronized_frame_set",
                "payload": frame_set.model_dump(mode="json"),
            }
        ],
        board_cut_decisions=[derived_entry],
        final_response=decision,
        timing={"total_ms": 0.0, "capture_skew_ms": frame_set.capture_skew_ms},
    )


def _capture_trigger_frame(
    *,
    session,
    settings,
    channel_id: str,
    payload: RuntimeTriggerExecutionRequest,
    source_type: RuntimeSourceType | None,
    source_config: dict[str, Any],
    source_secret_refs: dict[str, str],
    timeout_ms: int,
) -> FramePayload:
    if payload.image_base64:
        try:
            image_bytes = base64.b64decode(payload.image_base64.encode("ascii"), validate=True)
        except ValueError as exc:
            raise PlatformError(
                code="runtime_trigger_image_invalid",
                message="The trigger image_base64 field is not valid base64.",
                status_code=422,
                retryable=False,
            ) from exc
        return _frame_from_image_bytes(
            image_bytes,
            requested_mime_type=payload.mime_type,
            source_uri=None,
            metadata={"source": "request_body"},
        )
    snapshot_url = str(
        source_config.get("snapshot_url")
        or source_config.get("url")
        or source_config.get("http_url")
        or ""
    ).strip()
    test_file_path = str(source_config.get("path") or "").strip()
    if source_type == RuntimeSourceType.FILE_TEST and test_file_path:
        return _frame_from_managed_test_file(
            test_file_path,
            settings=settings,
            source_config=source_config,
        )
    live_source_types = {
        RuntimeSourceType.RTSP,
        RuntimeSourceType.USB_OPENCV,
        RuntimeSourceType.ARAVIS_GIGE,
        RuntimeSourceType.FLIR_BLACKFLY,
        RuntimeSourceType.BASLER_PYLON,
    }
    if source_type in live_source_types:
        try:
            return _frame_from_live_channel_source(
                source_type=source_type,
                source_config=source_config,
                source_secret_refs=source_secret_refs,
                timeout_ms=timeout_ms,
                max_bytes=settings.request_limits.max_image_bytes,
            )
        except PlatformError:
            live_frame = _latest_live_frame_payload(
                session=session,
                settings=settings,
                channel_id=channel_id,
            )
            if live_frame is not None:
                return live_frame
            raise
    if source_config.get("path"):
        raise PlatformError(
            code="runtime_trigger_unsafe_snapshot_source",
            message="Trigger frame sources must not use server-local paths.",
            status_code=422,
            retryable=False,
        )
    if not snapshot_url:
        live_frame = _latest_live_frame_payload(
            session=session,
            settings=settings,
            channel_id=channel_id,
        )
        if live_frame is not None:
            return live_frame
        raise PlatformError(
            code="runtime_trigger_snapshot_source_missing",
            message=(
                "The trigger did not include an image and the channel has "
                "no HTTP(S) frame source or live frame fallback."
            ),
            detail="Configure snapshot_url/url on the channel source or keep a live runtime running.",
            status_code=422,
            retryable=False,
            metadata={"source_type": source_type.value if source_type else None},
        )
    return _fetch_snapshot_url(
        snapshot_url,
        timeout_seconds=max(min(timeout_ms / 1000, 10.0), 0.05),
        max_bytes=settings.request_limits.max_image_bytes,
    )


def _frame_from_live_channel_source(
    *,
    source_type: RuntimeSourceType,
    source_config: dict[str, Any],
    source_secret_refs: dict[str, str],
    timeout_ms: int,
    max_bytes: int,
) -> FramePayload:
    if build_frame_source is None:
        raise PlatformError(
            code="runtime_trigger_live_source_unavailable",
            message="The trigger executor cannot acquire frames from live channel sources.",
            detail="The realtime source adapter package is not available in this runtime.",
            status_code=503,
            retryable=True,
            metadata={"source_type": source_type.value},
        )
    timeout_seconds = max(min(timeout_ms / 1000, 10.0), 0.05)
    capture_config = _live_source_capture_config(source_config, timeout_seconds=timeout_seconds)
    stop_event = threading.Event()
    try:
        source = build_frame_source(
            source_type=source_type.value,
            config=capture_config,
            secret_refs=source_secret_refs,
        )
    except RtspSourceConfigurationError as exc:
        raise PlatformError(
            code="runtime_trigger_live_source_invalid",
            message="The channel camera source is not configured for trigger capture.",
            detail=str(exc),
            status_code=422,
            retryable=False,
            metadata={"source_type": source_type.value},
        ) from exc

    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            packet = source.read(stop_event)
            if packet is None:
                continue
            packet = packet.ensure_encoded()
            if not packet.image_bytes:
                continue
            if len(packet.image_bytes) > max_bytes:
                raise PlatformError(
                    code="runtime_trigger_live_frame_too_large",
                    message="The channel camera source returned an image that is too large.",
                    status_code=413,
                    retryable=False,
                    metadata={"max_bytes": max_bytes, "source_type": source_type.value},
                )
            return _frame_from_image_bytes(
                packet.image_bytes,
                requested_mime_type=None,
                source_uri=packet.source_uri,
                metadata={
                    **dict(packet.metadata or {}),
                    "source": "channel_live_source_on_demand",
                    "source_type": source_type.value,
                    "source_timestamp": packet.source_timestamp,
                    "sequence": packet.sequence,
                },
            )
        health = getattr(source, "health", None)
        detail = (
            getattr(health, "last_error", None)
            or getattr(health, "status_text", None)
            or "The channel camera source did not produce a frame before the trigger timeout."
        )
        raise PlatformError(
            code="runtime_trigger_live_source_unavailable",
            message="The channel camera source did not produce a trigger frame.",
            detail=str(detail),
            status_code=502,
            retryable=True,
            metadata={"source_type": source_type.value},
        )
    except PlatformError:
        raise
    except Exception as exc:
        raise PlatformError(
            code="runtime_trigger_live_source_unavailable",
            message="The channel camera source could not be read for trigger capture.",
            detail=str(exc),
            status_code=502,
            retryable=True,
            metadata={"source_type": source_type.value},
        ) from exc
    finally:
        stop_event.set()
        try:
            source.close()
        except Exception:
            pass


def _live_source_capture_config(
    source_config: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    capture_config = dict(source_config or {})
    for key in ("read_timeout_seconds", "fetch_timeout_seconds"):
        configured = capture_config.get(key)
        try:
            configured_seconds = float(configured) if configured is not None else timeout_seconds
        except (TypeError, ValueError):
            configured_seconds = timeout_seconds
        capture_config[key] = max(min(configured_seconds, timeout_seconds), 0.05)
    try:
        reconnect_seconds = float(capture_config.get("reconnect_seconds") or 0.05)
    except (TypeError, ValueError):
        reconnect_seconds = 0.05
    capture_config["reconnect_seconds"] = min(
        max(reconnect_seconds, 0.05),
        timeout_seconds,
    )
    return capture_config


def _latest_live_frame_payload(
    *,
    session,
    settings,
    channel_id: str,
) -> FramePayload | None:
    try:
        state = get_runtime_live_view_state(session, settings, channel_id=channel_id)
    except ValueError:
        return None
    if not state.available or state.stale or state.frame is None:
        return None
    frame = state.frame
    if not frame.image_base64:
        return None
    try:
        image_bytes = base64.b64decode(frame.image_base64.encode("ascii"), validate=True)
    except ValueError as exc:
        raise PlatformError(
            code="runtime_trigger_live_frame_invalid",
            message="The latest live frame could not be decoded for trigger capture.",
            status_code=502,
            retryable=True,
        ) from exc
    return _frame_from_image_bytes(
        image_bytes,
        requested_mime_type=frame.mime_type,
        source_uri=None,
        metadata={
            "source": "live_view_latest_frame",
            "runtime_instance_id": state.runtime_instance_id,
            "source_timestamp": frame.source_timestamp,
            "sequence": frame.sequence,
        },
    )


def _frame_from_managed_test_file(
    path_value: str,
    *,
    settings,
    source_config: dict[str, Any],
) -> FramePayload:
    try:
        path = ensure_path_within_root(
            root=settings.storage.runtime_test_files_root(),
            target=Path(path_value),
        )
    except ValueError as exc:
        raise PlatformError(
            code="runtime_trigger_unsafe_test_file_source",
            message="Trigger test file sources must use managed uploaded channel test files.",
            detail=str(exc),
            status_code=422,
            retryable=False,
        ) from exc
    if not path.exists() or not path.is_file():
        raise PlatformError(
            code="runtime_trigger_test_file_missing",
            message="The configured channel test file is not readable.",
            detail=str(path),
            status_code=422,
            retryable=False,
        )
    suffix = path.suffix.lower()
    if suffix in _TEST_FILE_VIDEO_EXTENSIONS or source_config.get("kind") == "video":
        return _first_video_frame(path)
    if suffix not in _TEST_FILE_IMAGE_EXTENSIONS and source_config.get("kind") != "image":
        raise PlatformError(
            code="runtime_trigger_test_file_invalid",
            message="The configured channel test file type is not supported.",
            detail=str(path),
            status_code=415,
            retryable=False,
        )
    if path.stat().st_size > settings.request_limits.max_image_bytes:
        raise PlatformError(
            code="runtime_trigger_test_file_too_large",
            message="The configured channel test image is too large.",
            detail=f"Maximum image size is {settings.request_limits.max_image_bytes} bytes.",
            status_code=413,
            retryable=False,
        )
    return _frame_from_image_bytes(
        path.read_bytes(),
        requested_mime_type=None,
        source_uri=str(path),
        metadata={
            "source": "managed_test_file",
            "file_name": source_config.get("file_name") or path.name,
        },
    )


def _first_video_frame(path: Path) -> FramePayload:
    try:
        import cv2  # type: ignore[import-not-found]
    except Exception as exc:
        raise PlatformError(
            code="runtime_trigger_test_video_unavailable",
            message="OpenCV is required to read channel test videos.",
            detail=str(exc),
            status_code=422,
            retryable=False,
        ) from exc
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise PlatformError(
                code="runtime_trigger_test_video_unavailable",
                message="The configured channel test video could not be opened.",
                detail=str(path),
                status_code=422,
                retryable=False,
            )
        ok, frame = capture.read()
        if not ok or frame is None:
            raise PlatformError(
                code="runtime_trigger_test_video_unavailable",
                message="The configured channel test video did not produce a frame.",
                detail=str(path),
                status_code=422,
                retryable=False,
            )
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            raise PlatformError(
                code="runtime_trigger_test_video_unavailable",
                message="The configured channel test video frame could not be encoded.",
                detail=str(path),
                status_code=422,
                retryable=False,
            )
    finally:
        capture.release()
    return _frame_from_image_bytes(
        bytes(encoded),
        requested_mime_type=None,
        source_uri=str(path),
        metadata={"source": "managed_test_video", "file_name": path.name},
    )


def _fetch_snapshot_url(
    snapshot_url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> FramePayload:
    parsed = urlparse(snapshot_url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise PlatformError(
            code="runtime_trigger_snapshot_source_invalid",
            message="Trigger frame sources must use http:// or https:// URLs.",
            status_code=422,
            retryable=False,
        )
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise PlatformError(
            code="runtime_trigger_snapshot_source_invalid",
            message="Trigger frame URLs must not contain inline credentials.",
            status_code=422,
            retryable=False,
        )
    request = urllib.request.Request(
        snapshot_url,
        headers={"Accept": "image/*"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            content_length = response.headers.get("content-length")
            parsed_content_length = (
                int(content_length)
                if content_length and content_length.isdigit()
                else None
            )
            if parsed_content_length is not None and parsed_content_length > max_bytes:
                raise PlatformError(
                    code="runtime_trigger_snapshot_too_large",
                    message="The configured frame source returned an image that is too large.",
                    status_code=413,
                    retryable=False,
                    metadata={"max_bytes": max_bytes},
                )
            image_bytes = response.read(max_bytes + 1)
            if len(image_bytes) > max_bytes:
                raise PlatformError(
                code="runtime_trigger_snapshot_too_large",
                message="The configured frame source returned an image that is too large.",
                    status_code=413,
                    retryable=False,
                    metadata={"max_bytes": max_bytes},
                )
    except PlatformError:
        raise
    except urllib.error.HTTPError as exc:
        raise PlatformError(
            code="runtime_trigger_snapshot_unavailable",
            message=f"Frame source returned HTTP {exc.code}.",
            status_code=502,
            retryable=exc.code >= 500,
            metadata={"status_code": exc.code},
        ) from exc
    except OSError as exc:
        raise PlatformError(
            code="runtime_trigger_snapshot_unavailable",
            message="Frame source could not be reached.",
            detail=str(exc),
            status_code=502,
            retryable=True,
        ) from exc
    return _frame_from_image_bytes(
        image_bytes,
        requested_mime_type=None,
        source_uri=snapshot_url,
        metadata={"source": "snapshot_url"},
    )


def _frame_from_image_bytes(
    image_bytes: bytes,
    *,
    requested_mime_type: str | None,
    source_uri: str | None,
    metadata: dict[str, Any],
) -> FramePayload:
    inspection = inspect_image_bytes(image_bytes)
    if requested_mime_type is not None and requested_mime_type not in {"image/jpeg", "image/png"}:
        raise PlatformError(
            code="runtime_trigger_image_invalid",
            message="Trigger image mime_type must be image/jpeg or image/png.",
            status_code=422,
            retryable=False,
        )
    if requested_mime_type is not None and requested_mime_type != inspection.mime_type:
        raise PlatformError(
            code="runtime_trigger_image_invalid",
            message="Trigger image bytes do not match the declared mime_type.",
            status_code=422,
            retryable=False,
        )
    mime_type = inspection.mime_type
    return FramePayload.from_bytes(
        image_bytes,
        mime_type=mime_type,
        width=inspection.width,
        height=inspection.height,
        source_uri=source_uri,
        timestamp=utc_now_iso(),
        metadata=metadata,
    )


def _apply_endpoint_response_mapping(
    endpoint: TriggerEndpoint,
    result,
) -> str | None:
    try:
        policy = RuntimeTriggerResponsePolicy.model_validate(endpoint.response_policy or {})
    except ValueError as exc:
        return str(exc)
    if policy.mode == "all_final_response":
        return None
    mapped: dict[str, Any] = {}
    if policy.include_unmapped:
        mapped.update(result.final_response)
    missing: list[str] = []
    for field in policy.fields:
        value = _lookup_field(result.final_response, field.source_field)
        if value is None:
            if field.default_value is not None:
                value = field.default_value
            elif field.required:
                missing.append(field.source_field)
                continue
            else:
                continue
        mapped[field.response_key] = value
    if missing:
        return f"Required response fields were missing: {', '.join(missing)}."
    result.final_response = mapped
    return None


def _lookup_field(payload: dict[str, Any], path: str) -> Any:
    cursor: Any = payload
    for part in path.split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        else:
            return None
    return cursor


def _timeout_response(
    *,
    endpoint: TriggerEndpointSnapshot,
    payload: RuntimeTriggerExecutionRequest,
    request_id: str | None,
    correlation_id: str,
    timeout_ms: int,
    latency_ms: float,
) -> RuntimeTriggerExecutionResponse:
    return RuntimeTriggerExecutionResponse(
        status="timeout",
        trigger_endpoint_id=endpoint.endpoint_id,
        endpoint_slug=endpoint.slug,
        channel_id=endpoint.channel_id,
        pipeline_version_id=endpoint.pipeline_version_id,
        runtime_execution_id=endpoint.readiness.runtime_instance_id,
        trigger_id=payload.trigger_id or correlation_id,
        correlation_id=correlation_id,
        final_response={},
        errors=[
            {
                "code": "runtime_trigger_timeout",
                "message": "Trigger execution exceeded the endpoint timeout.",
                "retryable": True,
                "timeout_ms": timeout_ms,
            }
        ],
        timing={"total_ms": latency_ms, "timeout_ms": timeout_ms},
        error_code="runtime_trigger_timeout",
        error_message="Trigger execution exceeded the endpoint timeout.",
        retryable=True,
        meta={
            "request_id": request_id,
            "correlation_id": correlation_id,
            "generated_at": utc_now_iso(),
        },
    )


def _validate_trigger_request_payload(payload: RuntimeTriggerExecutionRequest) -> None:
    unsafe_keys = sorted(_unsafe_payload_paths(payload.payload))
    if unsafe_keys:
        raise PlatformError(
            code="runtime_trigger_payload_unsafe",
            message="Trigger payloads must not reference server-local paths.",
            detail=", ".join(unsafe_keys),
            status_code=422,
            retryable=False,
        )
    try:
        json.dumps(payload.payload)
    except (TypeError, ValueError) as exc:
        raise PlatformError(
            code="runtime_trigger_payload_invalid",
            message="Trigger payload must be JSON serializable.",
            status_code=422,
            retryable=False,
        ) from exc


def _unsafe_payload_paths(value: Any, *, prefix: str = "payload") -> set[str]:
    unsafe: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{prefix}.{key_text}"
            if key_text in _UNSAFE_TRIGGER_PAYLOAD_KEYS:
                unsafe.add(child_path)
            unsafe.update(_unsafe_payload_paths(item, prefix=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            unsafe.update(_unsafe_payload_paths(item, prefix=f"{prefix}[{index}]"))
    return unsafe
