from cv_contracts.runtime import (
    RuntimeBoundaryResponse,
    RuntimeTriggerExecutionRequest,
    RuntimeTriggerExecutionResponse,
)
from cv_core import parse_machine_token
from cv_db import (
    authenticate_machine_identity,
    build_runtime_model_resolver,
    get_ready_runtime_execution_binding,
    record_runtime_execution_result,
    session_scope,
)
from cv_runtime import (
    HttpInferenceGatewayClient,
    RuntimeExecutionContext,
    RuntimeGraphBuildError,
    runtime_service_boundary,
)
from cv_runtime.payloads import FramePayload
from cv_runtime.siemens_sps import close_siemens_sps_connection_cache
from cv_status import RuntimeMode
from cv_utils import AuthenticationError, PlatformError, get_correlation_id, get_request_id, utc_now_iso
from fastapi import APIRouter, Depends, Request

from realtime_executor import SERVICE_NAME

router = APIRouter(tags=["runtime-boundary"])


def get_settings(request: Request):
    return request.app.state.settings


def require_realtime_executor_access(request: Request, settings=Depends(get_settings)) -> None:
    if not settings.api_keys.enabled:
        return None
    machine_token = parse_machine_token(
        authorization_header=request.headers.get(settings.api_keys.authorization_header_name),
        legacy_api_key_header=request.headers.get(settings.api_keys.legacy_header_name),
    )
    if machine_token is None:
        raise AuthenticationError("Machine authentication is required for realtime executor APIs.")
    key_id, secret = machine_token
    authenticate_machine_identity(
        settings,
        key_id=key_id,
        secret=secret,
        required_scopes={"runtime.executor.invoke"},
        allowed_subject_types={"service"},
    )
    return None


@router.get("/runtime/boundary", response_model=RuntimeBoundaryResponse)
def get_runtime_boundary() -> RuntimeBoundaryResponse:
    boundary = runtime_service_boundary(SERVICE_NAME)
    return RuntimeBoundaryResponse(
        service_name=boundary.service_name,
        responsibilities=list(boundary.responsibilities),
        non_responsibilities=list(boundary.non_responsibilities),
    )


@router.post(
    "/runtime/channels/{channel_id}:execute-frame",
    response_model=RuntimeTriggerExecutionResponse,
)
def execute_single_frame(
    channel_id: str,
    payload: RuntimeTriggerExecutionRequest,
    _identity=Depends(require_realtime_executor_access),
    settings=Depends(get_settings),
) -> RuntimeTriggerExecutionResponse:
    correlation_id = get_correlation_id() or get_request_id() or payload.trigger_id or f"frm_{channel_id}"
    frame = _frame_from_payload(payload)
    with session_scope(settings) as session:
        try:
            execution, loaded = get_ready_runtime_execution_binding(
                session,
                channel_id=channel_id,
                expected_mode=RuntimeMode.REALTIME,
            )
        except (ValueError, RuntimeGraphBuildError) as exc:
            raise PlatformError(
                code="runtime_pipeline_not_executable",
                message="The realtime channel runtime is not ready.",
                detail=str(exc),
                status_code=422,
            ) from exc
        context = RuntimeExecutionContext(
            correlation_id=correlation_id,
            channel_id=loaded.channel.id,
            pipeline_version_id=loaded.version.id,
            runtime_execution_id=execution.id,
            mode=RuntimeMode(loaded.channel.mode),
            source_config=dict(loaded.source.config or {}) if loaded.source is not None else {},
            input_frame=frame,
            inference_client=HttpInferenceGatewayClient(
                base_url=settings.runtime.inference_gateway_realtime_url,
                bearer_token=settings.runtime.inference_gateway_bearer_token(),
                timeout_seconds=settings.inference_limits.realtime_timeout_ms / 1000,
            ),
            model_resolver=build_runtime_model_resolver(session),
            metadata={
                "executor_service": SERVICE_NAME,
                "single_frame": True,
                "default_inference_timeout_ms": int(
                    settings.inference_limits.realtime_timeout_ms
                ),
                "plc_writes_allowed": bool(settings.runtime.plc_writes_allowed),
            },
        )
        try:
            result = loaded.executable.execute(context)
        finally:
            close_siemens_sps_connection_cache(context.state)
        record_runtime_execution_result(
            session,
            result,
            channel_id=loaded.channel.id,
            runtime_execution_id=execution.id,
        )
    return RuntimeTriggerExecutionResponse(
        status=result.status,
        final_response=result.final_response,
        execution_result=result.model_dump(mode="json"),
        overlay_outputs=result.overlay_outputs,
        capture_intents=result.capture_intents,
        persisted_results=result.persisted_results,
        plc_write_outputs=result.plc_write_outputs,
        errors=result.errors,
        meta={
            "request_id": get_request_id(),
            "correlation_id": correlation_id,
            "generated_at": utc_now_iso(),
        },
    )


def _frame_from_payload(payload: RuntimeTriggerExecutionRequest) -> FramePayload | None:
    if payload.image_base64:
        return FramePayload(
            image_base64=payload.image_base64,
            mime_type=payload.mime_type,
            timestamp=utc_now_iso(),
        )
    if payload.image_path:
        raise PlatformError(
            code="runtime_realtime_image_path_disabled",
            message="Realtime executor payloads must not reference server-local image paths.",
            detail="Send image_base64 or use a channel-owned file/test source configured by an operator.",
            status_code=422,
        )
    return None
