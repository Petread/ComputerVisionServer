from typing import Annotated

from cv_contracts.runtime import RuntimeBoundaryResponse, RuntimeChannelBindResponse
from cv_core import parse_machine_token
from cv_db import (
    authenticate_machine_identity,
    create_audit_event,
    get_ready_runtime_execution_binding,
    runtime_control_command_to_api,
    session_scope,
)
from cv_runtime import RuntimeGraphBuildError, runtime_service_boundary
from cv_status import RuntimeExecutionStatus
from cv_utils import AuthenticationError, PlatformError, get_correlation_id, get_request_id, utc_now_iso
from fastapi import APIRouter, Depends, Request

from runtime_orchestrator import SERVICE_NAME

router = APIRouter(tags=["runtime-boundary"])


def get_settings(request: Request):
    return request.app.state.settings


def require_runtime_control_access(request: Request, settings=Depends(get_settings)) -> None:
    if not settings.api_keys.enabled:
        return None
    machine_token = parse_machine_token(
        authorization_header=request.headers.get(settings.api_keys.authorization_header_name),
        legacy_api_key_header=request.headers.get(settings.api_keys.legacy_header_name),
    )
    if machine_token is None:
        raise AuthenticationError("Machine authentication is required for runtime-control APIs.")
    key_id, secret = machine_token
    authenticate_machine_identity(
        settings,
        key_id=key_id,
        secret=secret,
        required_scopes={"runtime.control"},
        allowed_subject_types={"service"},
    )
    return None


RuntimeControlAccess = Annotated[None, Depends(require_runtime_control_access)]


@router.get("/runtime/boundary", response_model=RuntimeBoundaryResponse)
def get_runtime_boundary() -> RuntimeBoundaryResponse:
    boundary = runtime_service_boundary(SERVICE_NAME)
    return RuntimeBoundaryResponse(
        service_name=boundary.service_name,
        responsibilities=list(boundary.responsibilities),
        non_responsibilities=list(boundary.non_responsibilities),
    )


@router.post(
    "/runtime/channels/{channel_id}:bind",
    response_model=RuntimeChannelBindResponse,
)
def bind_channel_runtime(
    channel_id: str,
    _identity: RuntimeControlAccess,
    settings=Depends(get_settings),
) -> RuntimeChannelBindResponse:
    with session_scope(settings) as session:
        try:
            execution, loaded = get_ready_runtime_execution_binding(
                session,
                channel_id=channel_id,
            )
        except (ValueError, RuntimeGraphBuildError) as exc:
            raise PlatformError(
                code="runtime_channel_bind_failed",
                message="The channel does not have a ready runtime instance.",
                detail=str(exc),
                status_code=422,
            ) from exc
        executor_service = execution.executor_service
        pipeline_version_id = loaded.version.id
        runtime_execution_id = execution.id
        status = RuntimeExecutionStatus(execution.status)
    return RuntimeChannelBindResponse(
        channel_id=channel_id,
        pipeline_version_id=pipeline_version_id,
        runtime_execution_id=runtime_execution_id,
        executor_service=executor_service,
        status=status,
        meta={
            "request_id": get_request_id(),
            "correlation_id": get_correlation_id(),
            "generated_at": utc_now_iso(),
        },
    )


@router.post("/runtime-control:process-one")
def process_one_runtime_control_command(
    request: Request,
    _identity: RuntimeControlAccess,
) -> dict[str, object]:
    orchestrator = getattr(request.app.state, "runtime_control_orchestrator", None)
    if orchestrator is None:
        raise PlatformError(
            code="runtime_control_unavailable",
            message="Runtime-control orchestrator is not available in this service instance.",
            status_code=503,
        )
    command = orchestrator.process_one()
    if command is None:
        return {
            "processed": False,
            "meta": {
                "request_id": get_request_id(),
                "correlation_id": get_correlation_id(),
                "generated_at": utc_now_iso(),
            },
        }
    _write_runtime_control_audit(
        request=request,
        event_type="runtime.control_command.processed.v1",
        aggregate_id=command.id,
        payload={
            "channel_id": command.channel_id,
            "command": command.command,
            "status": command.status,
            "runtime_execution_id": command.runtime_execution_id,
        },
    )
    return {
        "processed": True,
        "command": runtime_control_command_to_api(command).model_dump(mode="json"),
        "meta": {
            "request_id": get_request_id(),
            "correlation_id": get_correlation_id(),
            "generated_at": utc_now_iso(),
        },
    }


@router.post("/runtime-control:reconcile")
def reconcile_runtime_control(
    request: Request,
    _identity: RuntimeControlAccess,
) -> dict[str, object]:
    orchestrator = getattr(request.app.state, "runtime_control_orchestrator", None)
    if orchestrator is None:
        raise PlatformError(
            code="runtime_control_unavailable",
            message="Runtime-control orchestrator is not available in this service instance.",
            status_code=503,
        )
    changed = orchestrator.reconcile()
    _write_runtime_control_audit(
        request=request,
        event_type="runtime.control_command.reconciled.v1",
        aggregate_id="runtime-control",
        payload={"changed": changed},
    )
    return {
        "changed": changed,
        "meta": {
            "request_id": get_request_id(),
            "correlation_id": get_correlation_id(),
            "generated_at": utc_now_iso(),
        },
    }


def _write_runtime_control_audit(
    *,
    request: Request,
    event_type: str,
    aggregate_id: str,
    payload: dict[str, object],
) -> None:
    settings = request.app.state.settings
    with session_scope(settings) as session:
        create_audit_event(
            session,
            event_type=event_type,
            aggregate_type="runtime_control",
            aggregate_id=aggregate_id,
            source_service=SERVICE_NAME,
            actor_type="service",
            actor_id=getattr(request.app.state, "instance_id", SERVICE_NAME),
            request_id=get_request_id(),
            correlation_id=get_correlation_id(),
            payload=payload,
        )
