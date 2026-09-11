from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlparse

from cv_contracts import SessionUser
from cv_contracts.runtime import (
    RuntimeBoundaryResponse,
    RuntimeCameraCalibrationCalculateRequest,
    RuntimeCameraCalibrationCalculateResponse,
    RuntimeImageViewProfileCalculateRequest,
    RuntimeImageViewProfileCalculateResponse,
    RuntimeStemCurvatureProcessCalibrationCalculateRequest,
    RuntimeStemCurvatureProcessCalibrationCalculateResponse,
    RuntimeStemSurfacePhaseCalibrationCalculateRequest,
    RuntimeStemSurfacePhaseCalibrationCalculateResponse,
    RuntimeStemMinimumPrincipleValidationCalculateRequest,
    RuntimeStemMinimumPrincipleValidationCalculateResponse,
    RuntimeCaptureHistoryResponse,
    RuntimeChannelAuthoringContextResponse,
    RuntimeChannelCommandRequest,
    RuntimeChannelCommandResponse,
    RuntimeChannelCreateRequest,
    RuntimeChannelDetail,
    RuntimeChannelListResponse,
    RuntimeChannelPipelineInstanceRequest,
    RuntimeChannelStatusOverview,
    RuntimeChannelStreamControlResponse,
    RuntimeChannelStreamListResponse,
    RuntimeChannelSummary,
    RuntimeChannelTestFileUploadResponse,
    RuntimeChannelUpdateRequest,
    RuntimeConnectorDeliveryHistoryResponse,
    RuntimeControlCommandSummary,
    RuntimeLiveDiagnostics,
    RuntimeLiveFrame,
    RuntimeLiveMetrics,
    RuntimeLiveSourceHealth,
    RuntimeLiveViewResponse,
    RuntimeLiveViewState,
    RuntimeManualCaptureHistoryResponse,
    RuntimeManualCaptureRequest,
    RuntimeManualCaptureResponse,
    RuntimeManualCaptureSessionResponse,
    RuntimeManualCaptureTargetListResponse,
    RuntimeManualIntervalCaptureStartRequest,
    RuntimeManualIntervalCaptureStopRequest,
    RuntimeModelCapabilityListResponse,
    RuntimeNodeTypeCatalogResponse,
    RuntimeOutputConnectorCreateRequest,
    RuntimeOutputConnectorDetail,
    RuntimeOutputConnectorListResponse,
    RuntimeOutputConnectorTypeCatalogResponse,
    RuntimeOutputConnectorUpdateRequest,
    RuntimeOverlayPrimitiveState,
    RuntimePipelineDefinitionCreateRequest,
    RuntimePipelineDefinitionDetail,
    RuntimePipelineDefinitionListResponse,
    RuntimePipelineDefinitionSummary,
    RuntimePipelineGraph,
    RuntimePipelinePreviewRequest,
    RuntimePipelineTemplateInstantiateRequest,
    RuntimePipelineValidationRequest,
    RuntimePipelineValidationResponse,
    RuntimePipelineVersionCreateRequest,
    RuntimePipelineVersionDetail,
    RuntimeStreamComponents,
    RuntimeStreamComponentState,
    RuntimeStreamComponentStatus,
    RuntimeStreamEncoderStatus,
    RuntimeStreamEndpoint,
    RuntimeStreamPublicationMetrics,
    RuntimeStreamStatus,
    RuntimeStreamVariantStatus,
    RuntimeTriggerEndpointCreateRequest,
    RuntimeTriggerEndpointDetail,
    RuntimeTriggerEndpointListResponse,
    RuntimeTriggerEndpointUpdateRequest,
)
from cv_core import PlatformSettings, generate_prefixed_id
from cv_db import (
    archive_pipeline_definition,
    build_runtime_channel_status_overview,
    build_runtime_model_resolver,
    connector_delivery_history_response,
    create_manual_capture_session,
    create_output_connector,
    create_pipeline_definition,
    create_pipeline_version,
    create_runtime_channel,
    create_trigger_endpoint,
    delete_runtime_channel,
    enqueue_manual_capture_request,
    ensure_channel_pipeline_instance,
    get_manual_capture_session_status,
    get_output_connector_detail,
    get_pipeline_definition_detail,
    get_runtime_channel_authoring_context,
    get_runtime_channel_detail,
    get_runtime_channel_for_manual_capture,
    get_runtime_live_view_response,
    get_runtime_live_view_state,
    get_trigger_endpoint_detail,
    instantiate_pipeline_template,
    list_manual_capture_history,
    list_manual_capture_targets,
    list_output_connectors_response,
    list_pipeline_definitions,
    list_runtime_capture_history,
    list_runtime_channels,
    list_runtime_model_capability_options,
    list_runtime_node_types,
    list_trigger_endpoints_response,
    normalize_pipeline_request_graph,
    publish_pipeline_version,
    read_runtime_overlay_event,
    request_channel_stream_restart,
    request_channel_stream_start,
    request_channel_stream_stop,
    request_runtime_channel_restart,
    request_runtime_channel_start,
    request_runtime_channel_stop,
    runtime_overlay_event_path,
    runtime_pipeline_output_catalog,
    runtime_prometheus_metrics_text,
    session_scope,
    stop_manual_capture_session,
    update_output_connector,
    update_runtime_channel,
    update_trigger_endpoint,
    validate_pipeline_request,
    validate_pipeline_version,
)
from cv_db.models import RuntimeChannel, RuntimeSource
from cv_runtime import (
    ExecutablePipelineGraph,
    HttpInferenceGatewayClient,
    LatestFrameRingReader,
    RuntimeExecutionContext,
    RuntimeGraphBuildError,
    RuntimeImageContext,
    RuntimeImageTransform,
    calculate_camera_calibration,
    calculate_image_view_profile,
    calculate_stem_curvature_process_calibration,
    calculate_stem_surface_phase_calibration,
    calculate_stem_minimum_principle_validation,
    create_identity_camera_calibration,
    output_connector_type_catalog,
    runtime_service_boundary,
)
from cv_runtime.payloads import FramePayload, RuntimeExecutionResult
from cv_runtime.siemens_sps import close_siemens_sps_connection_cache
from cv_status import (
    HealthState,
    RuntimeChannelDesiredState,
    RuntimeMode,
    RuntimeNodeCategory,
    RuntimeObservedState,
    RuntimeSourceType,
    TargetPath,
    TaskType,
    UserRole,
)
from cv_utils import (
    NotFoundError,
    PlatformError,
    inspect_image_bytes,
    sanitize_filename,
    utc_now,
    utc_now_iso,
)
from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse

from runtime_admin_api import SERVICE_NAME
from runtime_admin_api.audit import write_runtime_audit_event
from runtime_admin_api.dependencies import (
    RuntimeAdminPrincipal,
    build_meta,
    get_settings,
    require_permission,
)

try:
    from realtime_executor.sources import RtspSourceConfigurationError, build_frame_source
except Exception:
    RtspSourceConfigurationError = RuntimeError  # type: ignore[assignment]
    build_frame_source = None  # type: ignore[assignment]

router = APIRouter(tags=["runtime"])

_TEST_FILE_CHUNK_BYTES = 1024 * 1024
_TEST_FILE_IMAGE_EXTENSIONS = {".bmp", ".jpg", ".jpeg", ".png", ".webp"}
_TEST_FILE_VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
_TEST_FILE_VIDEO_MIME_TYPES = {
    "application/octet-stream",
    "video/avi",
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-m4v",
    "video/x-matroska",
    "video/x-msvideo",
}
_CALIBRATION_CAPTURE_LOCK = threading.Lock()
_CALIBRATION_CAPTURE_ACTIVE_DESIRED_STATES = {
    RuntimeChannelDesiredState.START_REQUESTED.value,
    RuntimeChannelDesiredState.RUNNING.value,
    RuntimeChannelDesiredState.STOP_REQUESTED.value,
}
_CALIBRATION_CAPTURE_ACTIVE_OBSERVED_STATES = {
    RuntimeObservedState.STARTING.value,
    RuntimeObservedState.VALIDATING.value,
    RuntimeObservedState.LOADING_PIPELINE.value,
    RuntimeObservedState.RUNNING.value,
    RuntimeObservedState.DEGRADED.value,
    RuntimeObservedState.STOPPING.value,
    RuntimeObservedState.RECOVERING.value,
}

ChannelReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "runtime.channels.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.VIEWER,
        )
    ),
]
ChannelWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "runtime.channels.write",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
        )
    ),
]
RuntimeControlWriter = Annotated[
    RuntimeAdminPrincipal,
    Depends(
        require_permission(
            "runtime.channels.control",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
        )
    ),
]
ManualCaptureReader = ChannelReader
ManualCaptureWriter = ChannelWriter
PipelineReader = ChannelReader
PipelineWriter = ChannelWriter
SettingsDep = Annotated[PlatformSettings, Depends(get_settings)]


@router.get("/runtime/boundary", response_model=RuntimeBoundaryResponse)
def get_runtime_boundary() -> RuntimeBoundaryResponse:
    boundary = runtime_service_boundary(SERVICE_NAME)
    return RuntimeBoundaryResponse(
        service_name=boundary.service_name,
        responsibilities=list(boundary.responsibilities),
        non_responsibilities=list(boundary.non_responsibilities),
    )


@router.get("/runtime/node-types", response_model=RuntimeNodeTypeCatalogResponse)
def get_node_types(
    _current_user: PipelineReader,
    settings: SettingsDep,
    category: RuntimeNodeCategory | None = None,
    mode: RuntimeMode | None = None,
) -> RuntimeNodeTypeCatalogResponse:
    with session_scope(settings) as session:
        items = list_runtime_node_types(session, category=category, mode=mode)
    return RuntimeNodeTypeCatalogResponse(items=items, meta=build_meta())


@router.get("/runtime/model-capabilities", response_model=RuntimeModelCapabilityListResponse)
def get_model_capabilities(
    _current_user: PipelineReader,
    settings: SettingsDep,
    task_type: TaskType | None = None,
    target_path: TargetPath | None = None,
) -> RuntimeModelCapabilityListResponse:
    with session_scope(settings) as session:
        items = [
            option.to_api()
            for option in list_runtime_model_capability_options(
                session,
                task_type=task_type,
                target_path=target_path,
                cache_ttl_seconds=settings.runtime.model_capability_cache_ttl_seconds,
            )
        ]
    return RuntimeModelCapabilityListResponse(items=items, meta=build_meta())


@router.get(
    "/runtime/output-connectors/types",
    response_model=RuntimeOutputConnectorTypeCatalogResponse,
)
def get_output_connector_types(
    _current_user: ChannelReader,
) -> RuntimeOutputConnectorTypeCatalogResponse:
    return RuntimeOutputConnectorTypeCatalogResponse(
        items=output_connector_type_catalog(),
        meta=build_meta(),
    )


@router.get("/runtime/output-connectors", response_model=RuntimeOutputConnectorListResponse)
def get_output_connectors(
    _current_user: ChannelReader,
    settings: SettingsDep,
    channel_id: str | None = Query(default=None, min_length=3, max_length=64),
    connector_type: str | None = Query(default=None, min_length=3, max_length=120),
) -> RuntimeOutputConnectorListResponse:
    with session_scope(settings) as session:
        return list_output_connectors_response(
            session,
            channel_id=channel_id,
            connector_type=connector_type,
            meta=build_meta(),
        )


@router.post(
    "/runtime/output-connectors",
    response_model=RuntimeOutputConnectorDetail,
    status_code=201,
)
def create_output_connector_route(
    payload: RuntimeOutputConnectorCreateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeOutputConnectorDetail:
    with session_scope(settings) as session:
        try:
            item = create_output_connector(
                session,
                payload,
                created_by=current_user.username,
            )
        except ValueError as exc:
            raise _connector_error(exc) from exc
    _write_connector_audit(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.output_connector.created.v1",
        item=item,
    )
    return item


@router.get(
    "/runtime/output-connectors/{output_connector_id}",
    response_model=RuntimeOutputConnectorDetail,
)
def get_output_connector_route(
    output_connector_id: str,
    _current_user: ChannelReader,
    settings: SettingsDep,
) -> RuntimeOutputConnectorDetail:
    with session_scope(settings) as session:
        item = get_output_connector_detail(session, output_connector_id)
    if item is None:
        raise NotFoundError(
            message="Output connector not found",
            detail=f"No output connector exists for id {output_connector_id}.",
        )
    return item


@router.put(
    "/runtime/output-connectors/{output_connector_id}",
    response_model=RuntimeOutputConnectorDetail,
)
def update_output_connector_route(
    output_connector_id: str,
    payload: RuntimeOutputConnectorUpdateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeOutputConnectorDetail:
    with session_scope(settings) as session:
        try:
            item = update_output_connector(
                session,
                output_connector_id,
                payload,
                updated_by=current_user.username,
            )
        except ValueError as exc:
            raise _connector_error(exc) from exc
    _write_connector_audit(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.output_connector.updated.v1",
        item=item,
    )
    return item


@router.get(
    "/runtime/channels/{channel_id}/output-connectors",
    response_model=RuntimeOutputConnectorListResponse,
)
def get_channel_output_connectors(
    channel_id: str,
    _current_user: ChannelReader,
    settings: SettingsDep,
) -> RuntimeOutputConnectorListResponse:
    with session_scope(settings) as session:
        if get_runtime_channel_detail(session, channel_id) is None:
            raise NotFoundError(
                message="Channel not found",
                detail=f"No runtime channel exists for id {channel_id}.",
            )
        return list_output_connectors_response(
            session,
            channel_id=channel_id,
            meta=build_meta(),
        )


@router.get(
    "/runtime/channels/{channel_id}/connector-deliveries",
    response_model=RuntimeConnectorDeliveryHistoryResponse,
)
def get_channel_connector_delivery_history(
    channel_id: str,
    _current_user: ChannelReader,
    settings: SettingsDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> RuntimeConnectorDeliveryHistoryResponse:
    with session_scope(settings) as session:
        if get_runtime_channel_detail(session, channel_id) is None:
            raise NotFoundError(
                message="Channel not found",
                detail=f"No runtime channel exists for id {channel_id}.",
            )
        return connector_delivery_history_response(
            session,
            channel_id=channel_id,
            limit=limit,
            meta=build_meta(),
        )


@router.get("/runtime/pipeline-definitions", response_model=RuntimePipelineDefinitionListResponse)
def get_pipeline_definitions(
    _current_user: PipelineReader,
    settings: SettingsDep,
    include_templates: bool = Query(default=True),
    templates_only: bool = Query(default=False),
    scope: Literal["template", "channel_instance"] | None = Query(default=None),
    owner_channel_id: str | None = Query(default=None, min_length=3, max_length=64),
) -> RuntimePipelineDefinitionListResponse:
    with session_scope(settings) as session:
        items = list_pipeline_definitions(
            session,
            include_templates=include_templates,
            templates_only=templates_only,
            scope=scope,
            owner_channel_id=owner_channel_id,
        )
    return RuntimePipelineDefinitionListResponse(items=items, meta=build_meta())


@router.post(
    "/runtime/pipeline-definitions",
    response_model=RuntimePipelineDefinitionDetail,
    status_code=201,
)
def create_pipeline_definition_route(
    payload: RuntimePipelineDefinitionCreateRequest,
    request: Request,
    current_user: PipelineWriter,
    settings: SettingsDep,
) -> RuntimePipelineDefinitionDetail:
    with session_scope(settings) as session:
        item = create_pipeline_definition(
            session,
            payload,
            created_by=current_user.username,
        )
    _write_pipeline_audit(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.pipeline_definition.created.v1",
        aggregate_id=item.pipeline_definition_id,
        payload={"name": item.name, "slug": item.slug, "template": item.template},
    )
    return item


@router.get(
    "/runtime/pipeline-definitions/{pipeline_definition_id}",
    response_model=RuntimePipelineDefinitionDetail,
)
def get_pipeline_definition_route(
    pipeline_definition_id: str,
    _current_user: PipelineReader,
    settings: SettingsDep,
) -> RuntimePipelineDefinitionDetail:
    with session_scope(settings) as session:
        item = get_pipeline_definition_detail(session, pipeline_definition_id)
    if item is None:
        raise NotFoundError(
            message="Pipeline not found",
            detail=f"No runtime pipeline exists for id {pipeline_definition_id}.",
        )
    return item


@router.delete(
    "/runtime/pipeline-definitions/{pipeline_definition_id}",
    response_model=RuntimePipelineDefinitionSummary,
)
def delete_pipeline_definition_route(
    pipeline_definition_id: str,
    request: Request,
    current_user: PipelineWriter,
    settings: SettingsDep,
    reason: str | None = Query(default=None, max_length=500),
) -> RuntimePipelineDefinitionSummary:
    with session_scope(settings) as session:
        try:
            item = archive_pipeline_definition(
                session,
                pipeline_definition_id,
                archived_by=current_user.username,
                reason=reason,
            )
        except ValueError as exc:
            raise _pipeline_error(exc) from exc
    _write_pipeline_audit(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.pipeline_definition.deleted.v1",
        aggregate_id=pipeline_definition_id,
        payload={"reason": reason, "name": item.name},
    )
    return item


@router.post(
    "/runtime/pipeline-definitions/{pipeline_definition_id}/versions",
    response_model=RuntimePipelineVersionDetail,
    status_code=201,
)
def create_pipeline_version_route(
    pipeline_definition_id: str,
    payload: RuntimePipelineVersionCreateRequest,
    request: Request,
    current_user: PipelineWriter,
    settings: SettingsDep,
) -> RuntimePipelineVersionDetail:
    with session_scope(settings) as session:
        try:
            item = create_pipeline_version(
                session,
                pipeline_definition_id,
                payload,
                created_by=current_user.username,
            )
        except ValueError as exc:
            raise _pipeline_error(exc) from exc
    _write_pipeline_audit(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.pipeline_version.saved.v1",
        aggregate_id=pipeline_definition_id,
        payload={
            "pipeline_version_id": item.pipeline_version_id,
            "version_no": item.version_no,
            "status": item.status.value,
            "error_count": item.error_count,
        },
    )
    return item


@router.post(
    "/runtime/pipeline-templates/{template_id}:instantiate",
    response_model=RuntimePipelineDefinitionDetail,
    status_code=201,
)
def instantiate_pipeline_template_route(
    template_id: str,
    payload: RuntimePipelineTemplateInstantiateRequest,
    request: Request,
    current_user: PipelineWriter,
    settings: SettingsDep,
) -> RuntimePipelineDefinitionDetail:
    with session_scope(settings) as session:
        try:
            item = instantiate_pipeline_template(
                session,
                template_id,
                payload,
                created_by=current_user.username,
            )
        except ValueError as exc:
            raise _pipeline_error(exc) from exc
    _write_pipeline_audit(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.pipeline_template.instantiated.v1",
        aggregate_id=item.pipeline_definition_id,
        payload={"template_id": template_id, "name": item.name},
    )
    return item


@router.post(
    "/runtime/pipeline-versions:validate",
    response_model=RuntimePipelineValidationResponse,
)
def validate_pipeline_graph_route(
    payload: RuntimePipelineValidationRequest,
    _current_user: PipelineReader,
    settings: SettingsDep,
) -> RuntimePipelineValidationResponse:
    with session_scope(settings) as session:
        report = validate_pipeline_request(session, payload)
        normalized_graph = normalize_pipeline_request_graph(session, payload)
        output_catalog = runtime_pipeline_output_catalog(session, normalized_graph)
    return RuntimePipelineValidationResponse(
        report=report,
        normalized_graph=normalized_graph,
        output_catalog=output_catalog,
        meta=build_meta(),
    )


@router.post(
    "/runtime/pipeline-versions/{pipeline_version_id}:validate",
    response_model=RuntimePipelineVersionDetail,
)
def validate_pipeline_version_route(
    pipeline_version_id: str,
    request: Request,
    current_user: PipelineWriter,
    settings: SettingsDep,
) -> RuntimePipelineVersionDetail:
    with session_scope(settings) as session:
        try:
            item = validate_pipeline_version(session, pipeline_version_id)
        except ValueError as exc:
            raise _pipeline_error(exc) from exc
    _write_pipeline_audit(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.pipeline_version.validated.v1",
        aggregate_id=item.pipeline_definition_id,
        payload={
            "pipeline_version_id": item.pipeline_version_id,
            "status": item.validation_status,
            "error_count": item.error_count,
        },
    )
    return item


@router.post(
    "/runtime/pipeline-versions/{pipeline_version_id}:publish",
    response_model=RuntimePipelineVersionDetail,
)
def publish_pipeline_version_route(
    pipeline_version_id: str,
    request: Request,
    current_user: PipelineWriter,
    settings: SettingsDep,
) -> RuntimePipelineVersionDetail:
    with session_scope(settings) as session:
        try:
            item = publish_pipeline_version(
                session,
                pipeline_version_id,
                published_by=current_user.username,
            )
        except ValueError as exc:
            raise _pipeline_error(exc) from exc
    _write_pipeline_audit(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.pipeline_version.published.v1",
        aggregate_id=item.pipeline_definition_id,
        payload={"pipeline_version_id": item.pipeline_version_id, "version_no": item.version_no},
    )
    return item


@router.get("/runtime/channels", response_model=RuntimeChannelListResponse)
def get_channels(
    _current_user: ChannelReader,
    settings: SettingsDep,
    search: str | None = Query(default=None, min_length=1, max_length=160),
    source_type: RuntimeSourceType | None = None,
    mode: RuntimeMode | None = None,
    status: str | None = Query(default=None, max_length=64),
    technical_place: str | None = Query(default=None, max_length=160),
    execution_target: Literal["server", "edge_device"] | None = None,
) -> RuntimeChannelListResponse:
    with session_scope(settings) as session:
        items = list_runtime_channels(
            session,
            search=search,
            source_type=source_type,
            mode=mode,
            status=status,
            technical_place=technical_place,
            execution_target=execution_target,
        )
    return RuntimeChannelListResponse(items=items, meta=build_meta())


@router.post(
    "/runtime/channel-test-files",
    response_model=RuntimeChannelTestFileUploadResponse,
    status_code=201,
)
async def upload_channel_test_file(
    file: Annotated[UploadFile, File()],
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeChannelTestFileUploadResponse:
    upload = await _persist_channel_test_file(file, settings=settings)
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel_test_file.uploaded.v1",
        aggregate_type="runtime_test_file",
        aggregate_id=str(upload["source_config"]["managed_upload_id"]),
        payload={
            "file_name": upload["file_name"],
            "content_type": upload["content_type"],
            "size_bytes": upload["size_bytes"],
            "path": upload["path"],
        },
    )
    return RuntimeChannelTestFileUploadResponse(
        file_name=str(upload["file_name"]),
        content_type=str(upload["content_type"]),
        size_bytes=int(upload["size_bytes"]),
        path=str(upload["path"]),
        source_config=dict(upload["source_config"]),
        meta=build_meta(),
    )


@router.post("/runtime/channels", response_model=RuntimeChannelDetail, status_code=201)
def create_channel(
    payload: RuntimeChannelCreateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeChannelDetail:
    with session_scope(settings) as session:
        try:
            item = create_runtime_channel(
                session,
                payload,
                created_by=current_user.username,
                source_secret_root=settings.storage.runtime_root() / "source-secrets",
            )
        except ValueError as exc:
            raise PlatformError(
                code="runtime_channel_invalid",
                message="The channel could not be created.",
                detail=str(exc),
                status_code=422,
            ) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.created.v1",
        aggregate_type="runtime_channel",
        aggregate_id=item.channel_id,
        payload={
            "name": item.name,
            "technical_place": item.technical_place,
            "source_type": item.source_type.value,
            "mode": item.mode.value,
            "assigned_pipeline": item.assigned_pipeline,
            "placement": item.placement.model_dump(mode="json"),
            "stem_rotation_priority_control": (
                item.stem_rotation_priority_control.model_dump(mode="json")
            ),
        },
    )
    return item


@router.get("/runtime/channels/status", response_model=RuntimeChannelStatusOverview)
def get_channel_status_overview(
    _current_user: ChannelReader,
    settings: SettingsDep,
) -> RuntimeChannelStatusOverview:
    with session_scope(settings) as session:
        return build_runtime_channel_status_overview(session, meta=build_meta())


@router.get("/runtime/metrics", response_class=PlainTextResponse)
def get_runtime_prometheus_metrics(
    settings: SettingsDep,
) -> PlainTextResponse:
    with session_scope(settings) as session:
        payload = runtime_prometheus_metrics_text(session, settings)
    return PlainTextResponse(
        payload,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/runtime/channels/{channel_id}", response_model=RuntimeChannelDetail)
def get_channel(
    channel_id: str,
    _current_user: ChannelReader,
    settings: SettingsDep,
) -> RuntimeChannelDetail:
    with session_scope(settings) as session:
        item = get_runtime_channel_detail(session, channel_id)
    if item is None:
        raise NotFoundError(
            message="Channel not found",
            detail=f"No runtime channel exists for id {channel_id}.",
        )
    return item


@router.get(
    "/runtime/channels/{channel_id}/authoring-context",
    response_model=RuntimeChannelAuthoringContextResponse,
)
def get_channel_authoring_context(
    channel_id: str,
    _current_user: ChannelReader,
    settings: SettingsDep,
) -> RuntimeChannelAuthoringContextResponse:
    with session_scope(settings) as session:
        try:
            context = get_runtime_channel_authoring_context(session, channel_id)
        except ValueError as exc:
            raise _channel_error(exc) from exc
    return RuntimeChannelAuthoringContextResponse(context=context, meta=build_meta())


@router.post(
    "/runtime/channels/{channel_id}/pipeline-instance",
    response_model=RuntimePipelineDefinitionDetail,
    status_code=201,
)
def create_channel_pipeline_instance_route(
    channel_id: str,
    payload: RuntimeChannelPipelineInstanceRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimePipelineDefinitionDetail:
    with session_scope(settings) as session:
        try:
            item = ensure_channel_pipeline_instance(
                session,
                channel_id=channel_id,
                payload=payload,
                created_by=current_user.username,
            )
        except ValueError as exc:
            raise _channel_error(exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.pipeline_instance.created.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "pipeline_definition_id": item.pipeline_definition_id,
            "pipeline_version_id": item.latest_version_id,
            "source_template_definition_id": item.source_template_definition_id,
            "replace_existing": payload.replace_existing,
        },
    )
    return item


@router.put("/runtime/channels/{channel_id}", response_model=RuntimeChannelDetail)
def update_channel(
    channel_id: str,
    payload: RuntimeChannelUpdateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeChannelDetail:
    with session_scope(settings) as session:
        try:
            item = update_runtime_channel(
                session,
                channel_id,
                payload,
                updated_by=current_user.username,
                source_secret_root=settings.storage.runtime_root() / "source-secrets",
            )
        except ValueError as exc:
            raise _channel_error(exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.updated.v1",
        aggregate_type="runtime_channel",
        aggregate_id=item.channel_id,
        payload={
            "name": item.name,
            "technical_place": item.technical_place,
            "source_type": item.source_type.value,
            "mode": item.mode.value,
            "assigned_pipeline": item.assigned_pipeline,
            "placement": item.placement.model_dump(mode="json"),
            "stem_rotation_priority_control": (
                item.stem_rotation_priority_control.model_dump(mode="json")
            ),
        },
    )
    return item


@router.delete("/runtime/channels/{channel_id}", response_model=RuntimeChannelSummary)
def delete_channel(
    channel_id: str,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
    reason: str | None = Query(default=None, max_length=1000),
) -> RuntimeChannelSummary:
    with session_scope(settings) as session:
        try:
            item = delete_runtime_channel(
                session,
                channel_id,
                deleted_by=current_user.username,
                reason=reason,
            )
        except ValueError as exc:
            raise _channel_error(exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.deleted.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={"name": item.name, "reason": reason},
    )
    return item


@router.get("/runtime/channels/{channel_id}/status", response_model=RuntimeChannelSummary)
def get_channel_status(
    channel_id: str,
    _current_user: ChannelReader,
    settings: SettingsDep,
) -> RuntimeChannelSummary:
    with session_scope(settings) as session:
        detail = get_runtime_channel_detail(session, channel_id)
    if detail is None:
        raise NotFoundError(
            message="Channel not found",
            detail=f"No runtime channel exists for id {channel_id}.",
        )
    return _runtime_channel_summary_from_detail(detail)


@router.get(
    "/runtime/channels/{channel_id}/streams",
    response_model=RuntimeChannelStreamListResponse,
)
def get_channel_streams(
    channel_id: str,
    _current_user: ChannelReader,
    settings: SettingsDep,
) -> RuntimeChannelStreamListResponse:
    with session_scope(settings) as session:
        detail = get_runtime_channel_detail(session, channel_id)
        row = session.get(RuntimeChannel, channel_id)
        if detail is None or row is None or row.deleted_at is not None:
            raise NotFoundError(
                message="Channel not found",
                detail=f"No runtime channel exists for id {channel_id}.",
            )
        return _runtime_channel_streams_response(detail, row=row, settings=settings)


@router.post(
    "/runtime/channels/{channel_id}/stream/start",
    response_model=RuntimeChannelStreamControlResponse,
    status_code=202,
)
def start_channel_stream(
    channel_id: str,
    request: Request,
    current_user: RuntimeControlWriter,
    settings: SettingsDep,
) -> RuntimeChannelStreamControlResponse:
    return _mutate_channel_stream(
        settings=settings,
        request=request,
        current_user=current_user,
        channel_id=channel_id,
        command="start",
    )


@router.post(
    "/runtime/channels/{channel_id}/stream/stop",
    response_model=RuntimeChannelStreamControlResponse,
    status_code=202,
)
def stop_channel_stream(
    channel_id: str,
    request: Request,
    current_user: RuntimeControlWriter,
    settings: SettingsDep,
) -> RuntimeChannelStreamControlResponse:
    return _mutate_channel_stream(
        settings=settings,
        request=request,
        current_user=current_user,
        channel_id=channel_id,
        command="stop",
    )


@router.post(
    "/runtime/channels/{channel_id}/stream/restart",
    response_model=RuntimeChannelStreamControlResponse,
    status_code=202,
)
def restart_channel_stream(
    channel_id: str,
    request: Request,
    current_user: RuntimeControlWriter,
    settings: SettingsDep,
) -> RuntimeChannelStreamControlResponse:
    return _mutate_channel_stream(
        settings=settings,
        request=request,
        current_user=current_user,
        channel_id=channel_id,
        command="restart",
    )


@router.get(
    "/runtime/trigger-endpoints",
    response_model=RuntimeTriggerEndpointListResponse,
)
def list_trigger_endpoints(
    _current_user: ChannelReader,
    settings: SettingsDep,
    channel_id: str | None = Query(default=None, min_length=3, max_length=64),
) -> RuntimeTriggerEndpointListResponse:
    with session_scope(settings) as session:
        return list_trigger_endpoints_response(
            session,
            channel_id=channel_id,
            meta=build_meta(),
        )


@router.post(
    "/runtime/trigger-endpoints",
    response_model=RuntimeTriggerEndpointDetail,
    status_code=201,
    include_in_schema=False,
)
def create_trigger_endpoint_route(
    payload: RuntimeTriggerEndpointCreateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeTriggerEndpointDetail:
    with session_scope(settings) as session:
        try:
            item = create_trigger_endpoint(
                session,
                payload,
                created_by=current_user.username,
            )
        except ValueError as exc:
            raise _trigger_endpoint_error(exc) from exc
    _write_trigger_endpoint_audit(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.trigger_endpoint.created.v1",
        item=item,
    )
    return item


@router.get(
    "/runtime/trigger-endpoints/{trigger_endpoint_id}",
    response_model=RuntimeTriggerEndpointDetail,
)
def get_trigger_endpoint_route(
    trigger_endpoint_id: str,
    _current_user: ChannelReader,
    settings: SettingsDep,
) -> RuntimeTriggerEndpointDetail:
    with session_scope(settings) as session:
        item = get_trigger_endpoint_detail(session, trigger_endpoint_id)
    if item is None:
        raise NotFoundError(
            message="Trigger endpoint metadata not found",
            detail=f"No runtime trigger endpoint metadata exists for id {trigger_endpoint_id}.",
        )
    return item


@router.put(
    "/runtime/trigger-endpoints/{trigger_endpoint_id}",
    response_model=RuntimeTriggerEndpointDetail,
    include_in_schema=False,
)
def update_trigger_endpoint_route(
    trigger_endpoint_id: str,
    payload: RuntimeTriggerEndpointUpdateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeTriggerEndpointDetail:
    with session_scope(settings) as session:
        try:
            item = update_trigger_endpoint(
                session,
                trigger_endpoint_id,
                payload,
                updated_by=current_user.username,
            )
        except ValueError as exc:
            raise _trigger_endpoint_error(exc) from exc
    _write_trigger_endpoint_audit(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.trigger_endpoint.updated.v1",
        item=item,
    )
    return item


@router.get(
    "/runtime/channels/{channel_id}/live-view/latest",
    response_model=RuntimeLiveViewResponse,
)
def get_channel_live_view_latest(
    channel_id: str,
    _current_user: ChannelReader,
    settings: SettingsDep,
    max_frame_width: int | None = Query(default=None, ge=32, le=1024),
    include_frame: bool = Query(default=True),
    prefer_camera_stream_frame: bool = Query(default=False),
) -> RuntimeLiveViewResponse:
    with session_scope(settings) as session:
        try:
            response = get_runtime_live_view_response(
                session,
                settings,
                channel_id=channel_id,
                max_frame_width=max_frame_width,
                include_frame=include_frame,
            )
        except ValueError as exc:
            raise _channel_error(exc) from exc
        channel = session.get(RuntimeChannel, channel_id)
        stream_active = bool(channel and _camera_stream_is_active(channel))
        source = (
            session.get(RuntimeSource, channel.source_id)
            if channel is not None and channel.source_id
            else None
        )
        source_type = str(source.source_type) if source is not None else "camera"
    if not prefer_camera_stream_frame or not include_frame or not stream_active:
        return response
    try:
        frame = _capture_calibration_frame_from_stream_ring(
            channel_id=channel_id,
            settings=settings,
            timeout_ms=500,
            max_bytes=settings.request_limits.max_image_bytes,
        )
    except PlatformError as exc:
        return RuntimeLiveViewResponse(
            state=response.state.model_copy(
                update={
                    "recovery_hint": (
                        exc.detail
                        or "The camera stream is running; waiting for its raw source frame."
                    )
                }
            ),
            meta=response.meta,
        )
    return RuntimeLiveViewResponse(
        state=_calibration_live_state(
            channel_id=channel_id,
            actual_state=response.state.actual_state,
            frame=frame,
            source_type=source_type,
            capture_mode="camera_media_stream",
        ),
        meta=response.meta,
    )


@router.get(
    "/runtime/channels/{channel_id}/overlay/events",
    response_class=StreamingResponse,
)
def stream_channel_overlay_events(
    channel_id: str,
    _current_user: ChannelReader,
    settings: SettingsDep,
) -> StreamingResponse:
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        if channel is None or channel.deleted_at is not None:
            raise NotFoundError(message="Channel not found", detail=channel_id)
    return StreamingResponse(
        _iter_channel_overlay_events(channel_id=channel_id, settings=settings),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _iter_channel_overlay_events(
    *,
    channel_id: str,
    settings: PlatformSettings,
) -> Iterator[str]:
    path = runtime_overlay_event_path(settings, channel_id)
    last_revision: tuple[int, int] | None | object = object()
    last_keepalive = time.monotonic()
    while True:
        try:
            stat = path.stat()
            revision: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            revision = None
        if revision != last_revision:
            last_revision = revision
            state = read_runtime_overlay_event(settings, channel_id) if revision else None
            if state is None:
                yield "event: clear\ndata: {}\n\n"
            else:
                frame_sequence = state.frame.sequence if state.frame is not None else 0
                payload = RuntimeLiveViewResponse(
                    state=state,
                    meta=build_meta(),
                ).model_dump(mode="json")
                yield (
                    f"id: {frame_sequence}\n"
                    "event: overlay\n"
                    f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                )
        now = time.monotonic()
        if now - last_keepalive >= 15.0:
            last_keepalive = now
            yield ": keepalive\n\n"
        time.sleep(0.02)


@router.post(
    "/runtime/channels/{channel_id}/calibration-frame",
    response_model=RuntimeLiveViewResponse,
)
def capture_channel_calibration_frame(
    channel_id: str,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
    timeout_ms: int = Query(default=8000, ge=500, le=30000),
) -> RuntimeLiveViewResponse:
    """Acquire one frame without requiring a valid or running inference pipeline."""
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        if channel is None or channel.deleted_at is not None:
            raise NotFoundError(message="Channel not found", detail=channel_id)
        if not channel.source_id:
            raise PlatformError(
                code="runtime_calibration_source_missing",
                message="The selected camera channel has no source assigned.",
                status_code=422,
                retryable=False,
                metadata={"channel_id": channel_id},
            )
        source = session.get(RuntimeSource, channel.source_id)
        if source is None:
            raise PlatformError(
                code="runtime_calibration_source_missing",
                message="The source assigned to the selected camera channel does not exist.",
                status_code=422,
                retryable=False,
                metadata={"channel_id": channel_id, "source_id": channel.source_id},
            )
        try:
            source_type = RuntimeSourceType(source.source_type)
            actual_state = RuntimeObservedState(channel.observed_state)
        except ValueError as exc:
            raise PlatformError(
                code="runtime_calibration_source_invalid",
                message="The selected channel cannot be used for camera calibration.",
                detail=str(exc),
                status_code=422,
                retryable=False,
                metadata={"channel_id": channel_id, "source_type": source.source_type},
            ) from exc
        source_config = dict(source.config or {})
        source_secret_refs = dict(source.secret_refs or {})
        stream_active = _camera_stream_is_active(channel)
        runtime_active = (
            channel.desired_state in _CALIBRATION_CAPTURE_ACTIVE_DESIRED_STATES
            or channel.observed_state in _CALIBRATION_CAPTURE_ACTIVE_OBSERVED_STATES
        )

    if stream_active:
        frame = _capture_calibration_frame_from_stream_ring(
            channel_id=channel_id,
            settings=settings,
            timeout_ms=timeout_ms,
            max_bytes=settings.request_limits.max_image_bytes,
        )
        state = _calibration_live_state(
            channel_id=channel_id,
            actual_state=actual_state,
            frame=frame,
            source_type=source_type.value,
            capture_mode="camera_media_stream",
        )
        write_runtime_audit_event(
            settings=settings,
            request=request,
            current_user=current_user,
            event_type="runtime.channel.calibration_frame.captured.v1",
            aggregate_type="runtime_channel",
            aggregate_id=channel_id,
            payload={
                "source_type": source_type.value,
                "width": frame.width,
                "height": frame.height,
                "sequence": frame.sequence,
                "exclusive_single_frame": False,
                "camera_media_stream_snapshot": True,
            },
        )
        return RuntimeLiveViewResponse(state=state, meta=build_meta())

    if runtime_active:
        raise PlatformError(
            code="runtime_calibration_camera_in_use",
            message="The selected camera channel is currently active.",
            detail=(
                "Use its current live source frame, or stop the inference runtime before "
                "requesting an exclusive calibration frame."
            ),
            status_code=409,
            retryable=True,
            metadata={
                "channel_id": channel_id,
                "desired_state": channel.desired_state,
                "observed_state": channel.observed_state,
                "stream_desired_state": channel.stream_desired_state,
                "stream_actual_state": channel.stream_actual_state,
            },
        )

    if not _CALIBRATION_CAPTURE_LOCK.acquire(blocking=False):
        raise PlatformError(
            code="runtime_calibration_capture_busy",
            message="Another exclusive calibration capture is already in progress.",
            detail="Wait for that single-frame capture to finish and try again.",
            status_code=409,
            retryable=True,
        )
    try:
        frame = _capture_calibration_frame_from_source(
            source_type=source_type,
            source_config=source_config,
            source_secret_refs=source_secret_refs,
            timeout_ms=timeout_ms,
            max_bytes=settings.request_limits.max_image_bytes,
        )
    finally:
        _CALIBRATION_CAPTURE_LOCK.release()

    state = _calibration_live_state(
        channel_id=channel_id,
        actual_state=actual_state,
        frame=frame,
        source_type=source_type.value,
        capture_mode="exclusive_single_frame",
    )
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.calibration_frame.captured.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "source_type": source_type.value,
            "width": frame.width,
            "height": frame.height,
            "sequence": frame.sequence,
            "exclusive_single_frame": True,
        },
    )
    return RuntimeLiveViewResponse(state=state, meta=build_meta())


@router.post(
    "/runtime/channels/{channel_id}/camera-calibration/calculate",
    response_model=RuntimeCameraCalibrationCalculateResponse,
)
def calculate_channel_camera_calibration(
    channel_id: str,
    payload: RuntimeCameraCalibrationCalculateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeCameraCalibrationCalculateResponse:
    """Calculate one intrinsic profile from temporary captured board observations."""
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        if channel is None or channel.deleted_at is not None:
            raise NotFoundError(message="Channel not found", detail=channel_id)
    try:
        if payload.calibration_method == "IDENTITY_PASSTHROUGH":
            computation = create_identity_camera_calibration(
                source_channel_id=channel_id,
                image_width=payload.image_width or 0,
                image_height=payload.image_height or 0,
                board_definition=payload.board_definition,
            )
        else:
            computation = calculate_camera_calibration(
                source_channel_id=channel_id,
                board_definition=payload.board_definition,
                observations=payload.observations,
                minimum_observation_count=payload.minimum_observation_count,
                maximum_rms_reprojection_error_px=(
                    payload.maximum_rms_reprojection_error_px
                ),
                preview_observation_id=payload.preview_observation_id,
            )
    except RuntimeError as exc:
        raise PlatformError(
            code="runtime_camera_calibration_unavailable",
            message="Intrinsic camera calibration is unavailable in this runtime.",
            detail=str(exc),
            status_code=503,
            retryable=False,
            metadata={"channel_id": channel_id},
        ) from exc

    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.camera_calibration.calculated.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "status": computation.status,
            "calibration_method": payload.calibration_method,
            "calibration_hash": (
                computation.profile.calibration_hash if computation.profile is not None else None
            ),
            "board_type": payload.board_definition.board_type,
            "supplied_observation_count": len(payload.observations),
            "accepted_observation_count": sum(
                1 for item in computation.observations if item.accepted
            ),
            "rms_reprojection_error_px": computation.rms_reprojection_error_px,
            "failure_code": computation.failure_code,
            "persisted": False,
        },
    )
    return RuntimeCameraCalibrationCalculateResponse(
        status=computation.status,
        profile=computation.profile,
        observations=computation.observations,
        rms_reprojection_error_px=computation.rms_reprojection_error_px,
        failure_code=computation.failure_code,
        failure_message=computation.failure_message,
        preview_before=computation.preview_before,
        preview_after=computation.preview_after,
        meta=build_meta(),
    )


@router.post(
    "/runtime/channels/{channel_id}/image-view-profile/calculate",
    response_model=RuntimeImageViewProfileCalculateResponse,
)
def calculate_channel_image_view_profile(
    channel_id: str,
    payload: RuntimeImageViewProfileCalculateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeImageViewProfileCalculateResponse:
    """Validate one versioned canonical view on the undistorted camera space."""
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        if channel is None or channel.deleted_at is not None:
            raise NotFoundError(message="Channel not found", detail=channel_id)
    try:
        computation = calculate_image_view_profile(
            source_channel_id=channel_id,
            request=payload,
            operator_user_id=current_user.user_id,
        )
    except RuntimeError as exc:
        raise PlatformError(
            code="runtime_image_view_profile_unavailable",
            message="Canonical image-view calculation is unavailable in this runtime.",
            detail=str(exc),
            status_code=503,
            retryable=False,
            metadata={"channel_id": channel_id},
        ) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.image_view_profile.calculated.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "status": computation.status,
            "view_profile_id": payload.view_profile_id,
            "profile_checksum": (
                computation.profile.profile_checksum if computation.profile else None
            ),
            "source_resolution": [payload.source_image_width, payload.source_image_height],
            "output_resolution": [payload.output_width, payload.output_height],
            "intrinsic_calibration_hash": payload.intrinsic_calibration_hash,
            "failure_code": computation.failure_code,
            "persisted": False,
        },
    )
    return RuntimeImageViewProfileCalculateResponse(
        status=computation.status,
        profile=computation.profile,
        failure_code=computation.failure_code,
        failure_message=computation.failure_message,
        meta=build_meta(),
    )


@router.post(
    "/runtime/channels/{channel_id}/stem-process-calibration/calculate",
    response_model=RuntimeStemCurvatureProcessCalibrationCalculateResponse,
)
def calculate_channel_stem_process_calibration(
    channel_id: str,
    payload: RuntimeStemCurvatureProcessCalibrationCalculateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeStemCurvatureProcessCalibrationCalculateResponse:
    """Calculate a versioned planar X/Y process calibration for stem measurement."""
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        if channel is None or channel.deleted_at is not None:
            raise NotFoundError(message="Channel not found", detail=channel_id)
    try:
        computation = calculate_stem_curvature_process_calibration(
            source_channel_id=channel_id,
            request=payload,
            operator_user_id=current_user.user_id,
        )
    except RuntimeError as exc:
        raise PlatformError(
            code="runtime_stem_process_calibration_unavailable",
            message="Stem process calibration is unavailable in this runtime.",
            detail=str(exc),
            status_code=503,
            retryable=False,
            metadata={"channel_id": channel_id},
        ) from exc

    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.stem_process_calibration.calculated.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "status": computation.status,
            "calibration_id": payload.calibration_id,
            "calibration_hash": (
                computation.profile.calibration_hash
                if computation.profile is not None
                else None
            ),
            "intrinsic_calibration_hash": payload.intrinsic_calibration_hash,
            "reference_point_count": len(payload.reference_points),
            "rms_reprojection_error_px": computation.rms_reprojection_error_px,
            "maximum_reprojection_error_px": (
                computation.maximum_reprojection_error_px
            ),
            "failure_code": computation.failure_code,
            "persisted": False,
        },
    )
    return RuntimeStemCurvatureProcessCalibrationCalculateResponse(
        status=computation.status,
        profile=computation.profile,
        rms_reprojection_error_px=computation.rms_reprojection_error_px,
        maximum_reprojection_error_px=computation.maximum_reprojection_error_px,
        failure_code=computation.failure_code,
        failure_message=computation.failure_message,
        meta=build_meta(),
    )


@router.post(
    "/runtime/channels/{channel_id}/stem-surface-phase-calibration/calculate",
    response_model=RuntimeStemSurfacePhaseCalibrationCalculateResponse,
)
def calculate_channel_stem_surface_phase_calibration(
    channel_id: str,
    payload: RuntimeStemSurfacePhaseCalibrationCalculateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeStemSurfacePhaseCalibrationCalculateResponse:
    """Fit a versioned raw surface-phase to known-angle commissioning profile."""
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        if channel is None or channel.deleted_at is not None:
            raise NotFoundError(message="Channel not found", detail=channel_id)
    computation = calculate_stem_surface_phase_calibration(
        source_channel_id=channel_id,
        request=payload,
        operator_user_id=current_user.user_id,
    )
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.stem_surface_phase_calibration.calculated.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "status": computation.status,
            "calibration_id": payload.calibration_id,
            "calibration_version": payload.calibration_version,
            "calibration_hash": (
                computation.profile.calibration_hash if computation.profile else None
            ),
            "observation_count": len(payload.observations),
            "mean_abs_error_deg": computation.mean_abs_error_deg,
            "rms_error_deg": computation.rms_error_deg,
            "maximum_abs_error_deg": computation.maximum_abs_error_deg,
            "failure_code": computation.failure_code,
            "persisted": False,
        },
    )
    return RuntimeStemSurfacePhaseCalibrationCalculateResponse(
        status=computation.status,
        profile=computation.profile,
        mean_abs_error_deg=computation.mean_abs_error_deg,
        rms_error_deg=computation.rms_error_deg,
        maximum_abs_error_deg=computation.maximum_abs_error_deg,
        failure_code=computation.failure_code,
        failure_message=computation.failure_message,
        meta=build_meta(),
    )


@router.post(
    "/runtime/channels/{channel_id}/stem-minimum-principle-validation/calculate",
    response_model=RuntimeStemMinimumPrincipleValidationCalculateResponse,
)
def calculate_channel_stem_minimum_principle_validation(
    channel_id: str,
    payload: RuntimeStemMinimumPrincipleValidationCalculateRequest,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> RuntimeStemMinimumPrincipleValidationCalculateResponse:
    """Calculate a versioned objective single-camera minimum-principle report."""
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        if channel is None or channel.deleted_at is not None:
            raise NotFoundError(message="Channel not found", detail=channel_id)
    computation = calculate_stem_minimum_principle_validation(
        source_channel_id=channel_id,
        request=payload,
        operator_user_id=current_user.user_id,
    )
    artifact = computation.artifact
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.stem_minimum_principle_validation.calculated.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "status": computation.status,
            "artifact_id": artifact.artifact_id,
            "artifact_version": artifact.artifact_version,
            "artifact_hash": artifact.artifact_hash,
            "process_calibration_hash": artifact.process_calibration_hash,
            "phase_calibration_hash": artifact.phase_calibration_hash,
            "observation_count": artifact.metrics.total_observation_count,
            "median_angle_error_deg": artifact.metrics.median_angle_error_deg,
            "p90_angle_error_deg": artifact.metrics.p90_angle_error_deg,
            "p95_angle_error_deg": artifact.metrics.p95_angle_error_deg,
            "sign_error_count": artifact.metrics.sign_error_count,
            "uncertainty_rate": artifact.metrics.uncertainty_rate,
            "reason_codes": artifact.reason_codes,
            "persisted": False,
        },
    )
    report_json = artifact.model_dump(mode="json")
    return RuntimeStemMinimumPrincipleValidationCalculateResponse(
        status=computation.status,
        artifact=artifact,
        report_json=report_json,
        report_markdown=artifact.report_markdown,
        report_html=artifact.report_html,
        meta=build_meta(),
    )


@router.post(
    "/runtime/channels/{channel_id}/pipeline-preview",
    response_model=RuntimeLiveViewResponse,
)
def preview_channel_pipeline_graph(
    channel_id: str,
    payload: RuntimePipelinePreviewRequest,
    _current_user: PipelineReader,
    settings: SettingsDep,
) -> RuntimeLiveViewResponse:
    with session_scope(settings) as session:
        try:
            live_state = get_runtime_live_view_state(
                session,
                settings,
                channel_id=channel_id,
                include_frame=True,
            )
        except ValueError as exc:
            raise _channel_error(exc) from exc
        preview_input_state = (
            live_state.model_copy(update={"frame": payload.preview_frame}, deep=True)
            if payload.preview_frame is not None
            else live_state
        )
        frame = _frame_payload_from_live_state(channel_id, preview_input_state)
        if frame is None:
            return RuntimeLiveViewResponse(
                state=live_state.model_copy(
                    update={
                        "available": False,
                        "recovery_hint": (
                            "No frame is available for pipeline preview. Start the runtime, "
                            "invoke a trigger pipeline, or configure a channel test image."
                        ),
                    }
                ),
                meta=build_meta(),
            )
        mode = payload.mode
        handler_refs = {
            item.node_type: item.runtime_handler_ref
            for item in list_runtime_node_types(session, mode=mode)
        }
        graph = normalize_pipeline_request_graph(
            session,
            RuntimePipelineValidationRequest(
                mode=payload.mode,
                graph=payload.graph,
                authoring_graph=payload.authoring_graph,
            ),
        )
        graph = _preview_graph_through_selected_node(graph, payload.selected_node_key)
        try:
            executable = ExecutablePipelineGraph.build(
                pipeline_version_id="preview",
                graph=graph,
                handler_refs=handler_refs,
            )
        except RuntimeGraphBuildError as exc:
            raise PlatformError(
                code="runtime_pipeline_preview_invalid",
                message="The draft pipeline preview graph is not executable.",
                detail=str(exc),
                status_code=422,
            ) from exc
        preview_channel = session.get(RuntimeChannel, channel_id)
        context = RuntimeExecutionContext(
            correlation_id=f"preview_{channel_id}_{int(time.time() * 1000)}",
            channel_id=channel_id,
            pipeline_version_id="preview",
            runtime_execution_id=f"preview-{channel_id}",
            mode=mode,
            input_frame=frame,
            inference_client=HttpInferenceGatewayClient(
                base_url=settings.runtime.inference_gateway_realtime_url,
                bearer_token=settings.runtime.inference_gateway_bearer_token(),
                timeout_seconds=settings.inference_limits.realtime_timeout_ms / 1000,
                overload_policy="wait",
            ),
            model_resolver=build_runtime_model_resolver(session),
            metadata={
                "executor_service": SERVICE_NAME,
                "authoring_preview": True,
                "default_inference_timeout_ms": int(
                    settings.inference_limits.realtime_timeout_ms
                ),
                "materialize_frame_node_outputs": True,
                "materialize_live_display_frame": True,
                "include_capture_outputs": payload.include_capture_outputs,
                "selected_node_key": payload.selected_node_key,
                "frozen_preview_frame": payload.preview_frame is not None,
                "plc_writes_allowed": False,
                "stem_rotation_priority_control": dict(
                    preview_channel.stem_rotation_priority_control
                    if preview_channel is not None
                    else {}
                ),
            },
        )
        try:
            result = executable.execute(context)
        finally:
            close_siemens_sps_connection_cache(context.state)
    return RuntimeLiveViewResponse(
        state=_preview_live_state_from_result(
            channel_id=channel_id,
            fallback_state=preview_input_state,
            fallback_frame=frame,
            result=result,
            graph_nodes=[node.model_dump(mode="json") for node in graph.nodes],
        ),
        meta=build_meta(),
    )


def _preview_graph_through_selected_node(
    graph: RuntimePipelineGraph,
    selected_node_key: str | None,
) -> RuntimePipelineGraph:
    if not selected_node_key or all(
        node.node_key != selected_node_key for node in graph.nodes
    ):
        return graph

    incoming_by_node: dict[str, set[str]] = {}
    for edge in graph.edges:
        incoming_by_node.setdefault(edge.to_node_key, set()).add(edge.from_node_key)
    retained = {selected_node_key}
    pending = [selected_node_key]
    while pending:
        current = pending.pop()
        for upstream in incoming_by_node.get(current, set()):
            if upstream not in retained:
                retained.add(upstream)
                pending.append(upstream)
    return graph.model_copy(
        update={
            "nodes": [node for node in graph.nodes if node.node_key in retained],
            "edges": [
                edge
                for edge in graph.edges
                if edge.from_node_key in retained and edge.to_node_key in retained
            ],
        },
        deep=True,
    )


def _runtime_channel_summary_from_detail(
    detail: RuntimeChannelDetail,
) -> RuntimeChannelSummary:
    summary_fields = set(RuntimeChannelSummary.model_fields)
    return RuntimeChannelSummary.model_validate(detail.model_dump(include=summary_fields))


def _runtime_channel_streams_response(
    channel: RuntimeChannelDetail,
    *,
    row: RuntimeChannel,
    settings: PlatformSettings,
) -> RuntimeChannelStreamListResponse:
    profile = dict(row.stream_profile or {})
    metrics_payload = dict(row.stream_metrics or {})
    actual_state = str(row.stream_actual_state or "stopped")
    desired_state = str(row.stream_desired_state or "stopped")
    stream_error = row.stream_last_error
    if _channel_stream_heartbeat_is_stale(row, settings=settings):
        actual_state = "recovering"
        stream_error = (
            "Camera-media heartbeat is stale; the last reported stream state is no longer "
            "trusted."
        )
        metrics_payload.update(
            {
                "camera_state": "recovering",
                "encoder_state": "recovering",
                "publication_state": "recovering",
                "browser_reader_state": "idle",
                "connected_clients": 0,
            }
        )
        for derived_key in ("undistorted_full", "canonical_model"):
            derived_stale = dict(metrics_payload.get(derived_key) or {})
            if derived_stale.get("configured"):
                derived_stale.update(
                    {
                        "status": "degraded",
                        "ready": False,
                        "transform_state": "recovering",
                        "encoder_state": "recovering",
                        "publication_state": "recovering",
                        "browser_reader_state": "idle",
                        "connected_clients": 0,
                        "warning": stream_error,
                    }
                )
                metrics_payload[derived_key] = derived_stale
    status_by_state = {
        "stopped": "disabled",
        "starting": "starting",
        "ready": "ready",
        "recovering": "degraded",
        "failed": "unavailable",
    }
    status = status_by_state.get(actual_state, "unavailable")
    encoder_name = str(metrics_payload.get("encoder") or "gstreamer:nvcudah264enc")
    encoder_ready = (
        _stream_component_state(metrics_payload.get("encoder_state"), actual_state) == "ready"
    )
    publication_metrics = RuntimeStreamPublicationMetrics.model_validate(
        {
            key: value
            for key, value in metrics_payload.items()
            if key in RuntimeStreamPublicationMetrics.model_fields
        }
    )
    item = RuntimeStreamVariantStatus(
        variant="raw",
        status=status,
        intended_use="Single production browser stream",
        overlay_included=False,
        protocol="webrtc",
        endpoints=_media_stream_endpoints(settings, channel.slug, "live"),
        codec="h264",
        width=_optional_positive_int(profile.get("width")),
        height=_optional_positive_int(profile.get("height")),
        fps=_optional_positive_float(profile.get("fps")),
        latency_estimate_ms=settings.runtime.realtime_target_latency_ms,
        encoder=RuntimeStreamEncoderStatus(
            mode="gpu" if encoder_ready else "unavailable",
            encoder=encoder_name if encoder_ready else None,
            hardware_accelerated=encoder_ready,
            pixel_format=str(profile.get("encoder_pixel_format") or "NV12")
            if encoder_ready
            else None,
            preset="nvenc-zero-latency" if encoder_ready else None,
            warning=stream_error,
        ),
        compositor_mode="none",
        fanout="server" if desired_state == "running" else "none",
        access_policy=_stream_access_policy(settings),
        authentication_required=bool(settings.runtime.stream_auth_enabled),
        ready=actual_state == "ready",
        warning=stream_error,
        metrics=publication_metrics,
    )
    canonical_item = _derived_stream_variant_status(
        settings=settings,
        channel_slug=channel.slug,
        desired_state=desired_state,
        profile=profile,
        metrics_payload=metrics_payload,
        variant="canonical_model",
        path_variant="model",
        intended_use="Continuous canonical model and Label Studio image view",
    )
    warnings = [stream_error] if stream_error else []
    camera_state = _stream_component_state(metrics_payload.get("camera_state"), actual_state)
    encoder_state = _stream_component_state(metrics_payload.get("encoder_state"), actual_state)
    publication_state = _stream_component_state(
        metrics_payload.get("publication_state"), actual_state
    )
    reader_state = _stream_component_state(
        metrics_payload.get("browser_reader_state"), "idle"
    )
    components = RuntimeStreamComponents(
        camera=RuntimeStreamComponentStatus(
            state=camera_state,
            ready=camera_state == "ready",
            error=stream_error if camera_state in {"recovering", "failed"} else None,
            details={"source_fps": metrics_payload.get("source_fps")},
        ),
        encoder=RuntimeStreamComponentStatus(
            state=encoder_state,
            ready=encoder_state == "ready",
            error=stream_error if encoder_state in {"recovering", "failed"} else None,
            details={"encoder": encoder_name},
        ),
        publication=RuntimeStreamComponentStatus(
            state=publication_state,
            ready=publication_state == "ready",
            error=(
                stream_error
                if publication_state in {"recovering", "failed"}
                else None
            ),
            details={"path": f"channels/{channel.slug}/live"},
        ),
        browser_readers=RuntimeStreamComponentStatus(
            state=reader_state,
            ready=reader_state == "ready",
            details={"connected_clients": publication_metrics.connected_clients},
        ),
    )
    return RuntimeChannelStreamListResponse(
        channel_id=channel.channel_id,
        desired_state=desired_state,
        actual_state=actual_state,
        worker_instance_id=row.stream_worker_instance_id,
        last_heartbeat_at=_iso(row.stream_heartbeat_at),
        last_error=stream_error,
        negotiated_profile=profile,
        components=components,
        stream_auth_enabled=bool(settings.runtime.stream_auth_enabled),
        access_policy_default="token"
        if settings.runtime.stream_auth_enabled
        else "unauthenticated_lan",
        items=[item, canonical_item],
        warnings=warnings,
        meta=build_meta(),
    )


def _derived_stream_variant_status(
    *,
    settings: PlatformSettings,
    channel_slug: str,
    desired_state: str,
    profile: dict[str, Any],
    metrics_payload: dict[str, Any],
    variant: Literal["undistorted_full", "canonical_model"],
    path_variant: str,
    intended_use: str,
) -> RuntimeStreamVariantStatus:
    variant_profile = dict(profile.get(variant) or {})
    variant_metrics = dict(metrics_payload.get(variant) or {})
    configured = bool(variant_profile.get("configured"))
    status_value = str(
        variant_metrics.get("status") or ("starting" if configured else "unavailable")
    )
    status = (
        status_value
        if status_value in {"disabled", "starting", "ready", "degraded", "unavailable"}
        else "unavailable"
    )
    warning = str(variant_metrics["warning"]) if variant_metrics.get("warning") else None
    encoder_ready = (
        _stream_component_state(
            variant_metrics.get("encoder_state"),
            "stopped" if not configured else "starting",
        )
        == "ready"
    )
    publication_metrics = RuntimeStreamPublicationMetrics.model_validate(
        {
            key: value
            for key, value in variant_metrics.items()
            if key in RuntimeStreamPublicationMetrics.model_fields
        }
    )
    return RuntimeStreamVariantStatus(
        variant=variant,
        status=cast("RuntimeStreamStatus", status),
        intended_use=intended_use,
        overlay_included=False,
        protocol="webrtc",
        endpoints=_media_stream_endpoints(settings, channel_slug, path_variant),
        codec="h264",
        width=_optional_positive_int(variant_profile.get("width")),
        height=_optional_positive_int(variant_profile.get("height")),
        fps=_optional_positive_float(variant_profile.get("fps")),
        latency_estimate_ms=settings.runtime.realtime_target_latency_ms,
        encoder=RuntimeStreamEncoderStatus(
            mode="gpu" if encoder_ready else "unavailable",
            encoder=(
                str(variant_metrics.get("encoder") or "gstreamer:nvcudah264enc")
                if encoder_ready
                else None
            ),
            hardware_accelerated=encoder_ready,
            pixel_format=(
                str(variant_profile.get("encoder_pixel_format") or "NV12")
                if encoder_ready
                else None
            ),
            preset="nvenc-zero-latency" if encoder_ready else None,
            warning=warning,
        ),
        compositor_mode="none",
        fanout="server" if desired_state == "running" and configured else "none",
        access_policy=_stream_access_policy(settings),
        authentication_required=bool(settings.runtime.stream_auth_enabled),
        ready=bool(variant_metrics.get("ready")),
        warning=warning,
        metrics=publication_metrics,
    )


def _channel_stream_heartbeat_is_stale(
    row: RuntimeChannel,
    *,
    settings: PlatformSettings,
) -> bool:
    if row.stream_desired_state != "running" or row.stream_actual_state == "stopped":
        return False
    reference = row.stream_heartbeat_at or row.updated_at
    if reference is None:
        return False
    now = utc_now()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=now.tzinfo)
    return (now - reference).total_seconds() > float(
        settings.runtime.camera_media_stale_after_seconds
    )


def _optional_positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_positive_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _iso(value: object) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else None


def _stream_component_state(
    value: object,
    fallback: RuntimeStreamComponentState,
) -> RuntimeStreamComponentState:
    candidate = str(value or fallback)
    if candidate in {"stopped", "starting", "ready", "recovering", "failed", "idle"}:
        return cast("RuntimeStreamComponentState", candidate)
    return "recovering"


def _media_stream_endpoints(
    settings: PlatformSettings,
    channel_slug: str,
    variant: str,
) -> list[RuntimeStreamEndpoint]:
    path_slug = _stream_path_component(channel_slug)
    path = f"channels/{path_slug}/{variant}"
    host = _public_stream_host(settings)
    webrtc_url = f"/stream-webrtc/{path}/whep"
    webrtc_display_url = f"https://{host}/stream-webrtc/{path}/whep"
    return [
        RuntimeStreamEndpoint(
            protocol="webrtc",
            url=webrtc_url,
            display_url=webrtc_display_url,
            browser_compatible=True,
            vlc_compatible=False,
        ),
    ]


def _public_stream_host(settings: PlatformSettings) -> str:
    if settings.runtime.stream_public_host:
        return settings.runtime.stream_public_host
    parsed = urlparse(settings.service.public_base_url)
    return parsed.hostname or "127.0.0.1"


def _stream_access_policy(settings: PlatformSettings) -> str:
    if settings.runtime.stream_auth_enabled:
        return settings.runtime.stream_access_policy
    return "unauthenticated_lan"


def _stream_path_component(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return normalized.casefold() or "channel"


def _frame_payload_from_live_state(
    channel_id: str,
    state: RuntimeLiveViewState,
) -> FramePayload | None:
    frame = state.source_frame or state.frame
    if frame is None or not frame.image_base64:
        return None
    image_context = None
    if isinstance(frame.image_context, dict) and frame.image_context:
        try:
            image_context = RuntimeImageContext.model_validate(frame.image_context)
        except ValueError:
            image_context = None
    if image_context is None:
        image_context = RuntimeImageContext(
            context_id=f"{channel_id}:authoring-input",
            kind="original",
            width=frame.width,
            height=frame.height,
            source_uri=f"runtime-channel:{channel_id}",
            transform=RuntimeImageTransform(),
            metadata={"authoring_source": "runtime_live_view"},
        )
    return FramePayload(
        image_base64=frame.image_base64,
        mime_type=frame.mime_type,
        width=frame.width,
        height=frame.height,
        source_uri=f"runtime-channel:{channel_id}",
        timestamp=frame.source_timestamp or frame.captured_at,
        image_context=image_context,
        metadata={
            **frame.metadata,
            "authoring_source": "runtime_live_view",
            "live_frame_sequence": frame.sequence,
        },
    )


def _preview_live_state_from_result(
    *,
    channel_id: str,
    fallback_state: RuntimeLiveViewState,
    fallback_frame: FramePayload,
    result: RuntimeExecutionResult,
    graph_nodes: list[dict[str, Any]],
) -> RuntimeLiveViewState:
    display_payload = _display_selection_from_result(result)
    display_frame = _display_frame_from_result(display_payload) or _last_frame_payload(result) or fallback_frame
    display_context = (
        display_payload.get("display_context")
        if isinstance(display_payload, dict) and isinstance(display_payload.get("display_context"), dict)
        else None
    )
    image_context = display_frame.image_context
    if display_context is None and image_context is not None:
        display_context = image_context.model_dump(mode="json")
    display_context_id = (
        _string_or_none(display_payload.get("display_context_id"))
        if isinstance(display_payload, dict)
        else None
    ) or (image_context.context_id if image_context is not None else None)
    original_context_id = (
        fallback_frame.image_context.context_id
        if fallback_frame.image_context is not None
        else None
    )
    overlay = _overlay_primitives_from_result(result, display_context_id=display_context_id)
    if _context_matches_display(original_context_id, display_context_id):
        overlay.extend(
            _configured_visual_primitives(
                graph_nodes,
                fallback_frame,
                image_context_id=original_context_id,
            )
        )
    layers_available = sorted({primitive.layer for primitive in overlay})
    error_messages = [
        str(error.get("message") or error.get("code") or "Pipeline preview failed")
        for error in result.errors
        if isinstance(error, dict)
    ]
    warnings = error_messages[:]
    if result.status != "completed" and not warnings:
        warnings.append(f"Pipeline preview ended with status {result.status}.")
    total_ms = _timing_number(result.timing.get("total_ms"))
    frame_sequence = fallback_state.frame.sequence if fallback_state.frame is not None else 0
    display_context_dict = display_context if isinstance(display_context, dict) else {}
    return RuntimeLiveViewState(
        channel_id=channel_id,
        runtime_instance_id=fallback_state.runtime_instance_id,
        actual_state=fallback_state.actual_state,
        available=display_frame.image_base64 != "",
        stale=fallback_state.stale,
        display_context_id=display_context_id,
        display_context=display_context_dict,
        frame=RuntimeLiveFrame(
            image_base64=display_frame.image_base64,
            mime_type=display_frame.mime_type,
            width=display_frame.width or (image_context.width if image_context is not None else None),
            height=display_frame.height or (image_context.height if image_context is not None else None),
            source_timestamp=display_frame.timestamp,
            captured_at=fallback_state.frame.captured_at if fallback_state.frame is not None else None,
            sequence=frame_sequence,
            image_context=display_context_dict,
            metadata={
                **display_frame.metadata,
                "authoring_preview": True,
                "pipeline_preview_status": result.status,
            },
        ),
        source_frame=RuntimeLiveFrame(
            image_base64=fallback_frame.image_base64_value(),
            mime_type=fallback_frame.mime_type,
            width=fallback_frame.width,
            height=fallback_frame.height,
            source_timestamp=fallback_frame.timestamp,
            captured_at=(
                fallback_state.source_frame.captured_at
                if fallback_state.source_frame is not None
                else fallback_state.frame.captured_at
                if fallback_state.frame is not None
                else None
            ),
            sequence=frame_sequence,
            image_context=(
                fallback_frame.image_context.model_dump(mode="json")
                if fallback_frame.image_context is not None
                else {}
            ),
            metadata={
                **fallback_frame.metadata,
                "authoring_preview": True,
                "frame_role": "pipeline_input",
            },
        ),
        overlay=overlay,
        layers_available=layers_available,
        metrics=RuntimeLiveMetrics(
            latency_ms=total_ms,
            frame_count=max(1, fallback_state.metrics.frame_count),
            acquired_frame_count=fallback_state.metrics.acquired_frame_count,
            dropped_frame_count=fallback_state.metrics.dropped_frame_count,
            reconnect_count=fallback_state.metrics.reconnect_count,
            source_error_count=fallback_state.metrics.source_error_count,
            buffer_depth=fallback_state.metrics.buffer_depth,
            last_frame_at=fallback_state.metrics.last_frame_at,
            stage_timings_ms={"pipeline_preview_ms": total_ms} if total_ms is not None else {},
        ),
        source_health=RuntimeLiveSourceHealth(
            state=HealthState.OK if result.status == "completed" else HealthState.DEGRADED,
            status_text="Draft preview completed" if result.status == "completed" else "Draft preview has errors",
            connected=True,
            last_error="; ".join(error_messages) if error_messages else None,
            reconnect_count=fallback_state.source_health.reconnect_count,
            source_error_count=fallback_state.source_health.source_error_count,
        ),
        diagnostics=RuntimeLiveDiagnostics(
            source_type=fallback_state.diagnostics.source_type,
            source_max_fps=fallback_state.diagnostics.source_max_fps,
            source_sample_every_n=fallback_state.diagnostics.source_sample_every_n,
            effective_source_limit_fps=fallback_state.diagnostics.effective_source_limit_fps,
            pipeline_max_inflight=1,
            pipeline_stateful_nodes=fallback_state.diagnostics.pipeline_stateful_nodes,
            pipeline_frame_samplers=fallback_state.diagnostics.pipeline_frame_samplers,
            pipeline_node_timings=(
                result.timing.get("node_timings_ms")
                if isinstance(result.timing.get("node_timings_ms"), list)
                else []
            ),
            pipeline_inference_timings=(
                result.timing.get("inference_timings_ms")
                if isinstance(result.timing.get("inference_timings_ms"), list)
                else []
            ),
            plc_write_outputs=result.plc_write_outputs,
            warnings=warnings,
            stream_transport="authoring-preview",
        ),
        recovery_hint=None if result.status == "completed" else "Open Validation Results and inspect node timings.",
        updated_at=utc_now_iso(),
    )


def _display_selection_from_result(result: RuntimeExecutionResult) -> dict[str, Any] | None:
    for entry in reversed(result.display_outputs):
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        if isinstance(payload, dict):
            return payload
    return None


def _display_frame_from_result(display_payload: dict[str, Any] | None) -> FramePayload | None:
    if not isinstance(display_payload, dict):
        return None
    return _frame_payload_from_mapping(display_payload.get("frame"))


def _last_frame_payload(result: RuntimeExecutionResult) -> FramePayload | None:
    latest_frame = None
    for node_outputs in result.node_outputs.values():
        if not isinstance(node_outputs, dict):
            continue
        for payload in node_outputs.values():
            frame = _frame_payload_from_mapping(payload)
            if frame is not None:
                latest_frame = frame
    return latest_frame


def _frame_payload_from_mapping(payload: object) -> FramePayload | None:
    if not isinstance(payload, dict) or payload.get("payload_type") != "frame" or not payload.get("image_base64"):
        return None
    try:
        return FramePayload.model_validate(payload)
    except ValueError:
        return None


def _overlay_primitives_from_result(
    result: RuntimeExecutionResult,
    *,
    display_context_id: str | None,
) -> list[RuntimeOverlayPrimitiveState]:
    primitives: list[RuntimeOverlayPrimitiveState] = []
    for raw in result.overlay_primitives:
        primitive = _runtime_overlay_primitive(raw, display_context_id=display_context_id)
        if primitive is not None:
            primitives.append(primitive)
    primitives.extend(_cv_edge_primitives_from_result(result, display_context_id=display_context_id))
    primitives.extend(_reference_edge_primitives_from_result(result, display_context_id=display_context_id))
    primitives.extend(_coordinate_frame_primitives_from_result(result, display_context_id=display_context_id))
    return primitives


def _runtime_overlay_primitive(
    raw: object,
    *,
    display_context_id: str | None,
) -> RuntimeOverlayPrimitiveState | None:
    if not isinstance(raw, dict):
        return None
    image_context_id = _string_or_none(raw.get("image_context_id"))
    if not _context_matches_display(image_context_id, display_context_id):
        return None
    allowed = set(RuntimeOverlayPrimitiveState.model_fields)
    payload = {key: value for key, value in raw.items() if key in allowed}
    payload.setdefault("style", {})
    payload.setdefault("metadata", {})
    metadata = payload["metadata"] if isinstance(payload["metadata"], dict) else {}
    payload["metadata"] = {
        **metadata,
        **{
            key: raw[key]
            for key in ("node_key", "node_type", "output_port")
            if key in raw
        },
    }
    try:
        return RuntimeOverlayPrimitiveState.model_validate(payload)
    except ValueError:
        return None


def _cv_edge_primitives_from_result(
    result: RuntimeExecutionResult,
    *,
    display_context_id: str | None,
) -> list[RuntimeOverlayPrimitiveState]:
    primitives: list[RuntimeOverlayPrimitiveState] = []
    for entry in result.cv_edges:
        if not isinstance(entry, dict) or not isinstance(entry.get("payload"), dict):
            continue
        payload = entry["payload"]
        image_context_id = _string_or_none(payload.get("source_image_context_id")) or _string_or_none(
            payload.get("image_context_id")
        )
        if not _context_matches_display(image_context_id, display_context_id):
            continue
        mask_frame = payload.get("edge_mask_frame")
        if isinstance(mask_frame, dict) and mask_frame.get("image_base64"):
            primitives.append(
                RuntimeOverlayPrimitiveState(
                    primitive_type="mask",
                    layer="cv_edge_mask",
                    label="Canny edge mask",
                    confidence=1.0 if payload.get("edge_pixel_count") else 0.0,
                    geometry={
                        "format": "png",
                        "data": str(mask_frame["image_base64"]),
                        "width": payload.get("width"),
                        "height": payload.get("height"),
                        "edge_pixel_count": payload.get("edge_pixel_count"),
                    },
                    image_context_id=image_context_id,
                    style={"opacity": 0.42, "color": "#ffffff"},
                    metadata={
                        "node_key": entry.get("node_key"),
                        "node_type": entry.get("node_type"),
                    },
                )
            )
        for candidate in list(payload.get("line_candidates") or [])[:50]:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("line"), dict):
                continue
            line = candidate["line"]
            if not isinstance(line.get("start"), dict) or not isinstance(line.get("end"), dict):
                continue
            primitives.append(
                RuntimeOverlayPrimitiveState(
                    primitive_type="line",
                    layer="cv_candidate_lines",
                    label="candidate",
                    confidence=_timing_number(candidate.get("score")),
                    geometry={
                        "start": line["start"],
                        "end": line["end"],
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=image_context_id,
                    style={"stroke": "#9aa5ad", "stroke_width": 1.2},
                    metadata={
                        **candidate,
                        "node_key": entry.get("node_key"),
                        "node_type": entry.get("node_type"),
                    },
                )
            )
        for contour in list(payload.get("contours") or [])[:40]:
            if not isinstance(contour, dict) or not isinstance(contour.get("points"), list):
                continue
            primitives.append(
                RuntimeOverlayPrimitiveState(
                    primitive_type="polygon",
                    layer="cv_edge_contours",
                    label="edge contour",
                    confidence=_timing_number(contour.get("score")),
                    geometry={
                        "points": contour["points"],
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=image_context_id,
                    style={"stroke": "#5db7ff", "fill": "rgba(93,183,255,0.05)"},
                    metadata={
                        **contour,
                        "node_key": entry.get("node_key"),
                        "node_type": entry.get("node_type"),
                    },
                )
            )
    return primitives


def _reference_edge_primitives_from_result(
    result: RuntimeExecutionResult,
    *,
    display_context_id: str | None,
) -> list[RuntimeOverlayPrimitiveState]:
    primitives: list[RuntimeOverlayPrimitiveState] = []
    for entry in result.reference_edges:
        if not isinstance(entry, dict) or not isinstance(entry.get("payload"), dict):
            continue
        payload = entry["payload"]
        image_context_id = _string_or_none(payload.get("image_context_id"))
        if not _context_matches_display(image_context_id, display_context_id):
            continue
        for edge in payload.get("edges") or []:
            if not isinstance(edge, dict) or not isinstance(edge.get("line"), dict):
                continue
            line = edge["line"]
            if not isinstance(line.get("start"), dict) or not isinstance(line.get("end"), dict):
                continue
            primitives.append(
                RuntimeOverlayPrimitiveState(
                    primitive_type="line",
                    layer="reference_edges",
                    label=_string_or_none(edge.get("role")) or "edge",
                    confidence=_timing_number(edge.get("confidence")),
                    geometry={
                        "start": line["start"],
                        "end": line["end"],
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=image_context_id,
                    style={"stroke": "#5db7ff", "stroke_width": 2.5},
                    metadata={
                        **edge,
                        "node_key": entry.get("node_key"),
                        "node_type": entry.get("node_type"),
                    },
                )
            )
    return primitives


def _coordinate_frame_primitives_from_result(
    result: RuntimeExecutionResult,
    *,
    display_context_id: str | None,
) -> list[RuntimeOverlayPrimitiveState]:
    primitives: list[RuntimeOverlayPrimitiveState] = []
    for entry in result.coordinate_frames:
        if not isinstance(entry, dict) or not isinstance(entry.get("payload"), dict):
            continue
        payload = entry["payload"]
        frame = payload.get("frame")
        if not isinstance(frame, dict):
            continue
        image_context_id = _string_or_none(frame.get("image_context_id"))
        if not _context_matches_display(image_context_id, display_context_id):
            continue
        origin = frame.get("origin") if isinstance(frame.get("origin"), dict) else None
        longitudinal = frame.get("longitudinal_axis") if isinstance(frame.get("longitudinal_axis"), dict) else None
        transverse = frame.get("transverse_axis") if isinstance(frame.get("transverse_axis"), dict) else None
        if origin is None or longitudinal is None or transverse is None:
            continue
        metadata = frame.get("metadata") if isinstance(frame.get("metadata"), dict) else {}
        length = _timing_number(metadata.get("axis_display_length_px")) or max(
            _timing_number(metadata.get("image_width")) or 0,
            _timing_number(metadata.get("image_height")) or 0,
            80,
        ) * 0.25
        origin_x = _timing_number(origin.get("x"))
        origin_y = _timing_number(origin.get("y"))
        long_x = _timing_number(longitudinal.get("x"))
        long_y = _timing_number(longitudinal.get("y"))
        trans_x = _timing_number(transverse.get("x"))
        trans_y = _timing_number(transverse.get("y"))
        if None in {origin_x, origin_y, long_x, long_y, trans_x, trans_y}:
            continue
        axes = [
            ("longitudinal", long_x, long_y, "#49c47a", 2.5),
            ("transverse", trans_x, trans_y, "#f4b740", 2.0),
        ]
        for label, axis_x, axis_y, stroke, stroke_width in axes:
            primitives.append(
                RuntimeOverlayPrimitiveState(
                    primitive_type="axis",
                    layer="coordinate_frame",
                    label=label,
                    confidence=_timing_number(frame.get("confidence")),
                    geometry={
                        "start": {"x": origin_x, "y": origin_y},
                        "end": {
                            "x": origin_x + axis_x * length,
                            "y": origin_y + axis_y * length,
                        },
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=image_context_id,
                    style={"stroke": stroke, "stroke_width": stroke_width},
                    metadata={
                        **frame,
                        "node_key": entry.get("node_key"),
                        "node_type": entry.get("node_type"),
                    },
                )
            )
    return primitives


def _configured_visual_primitives(
    graph_nodes: list[dict[str, Any]],
    frame: FramePayload,
    *,
    image_context_id: str | None,
) -> list[RuntimeOverlayPrimitiveState]:
    primitives: list[RuntimeOverlayPrimitiveState] = []
    frame_width = frame.width or (frame.image_context.width if frame.image_context is not None else None)
    frame_height = frame.height or (frame.image_context.height if frame.image_context is not None else None)
    for node in graph_nodes:
        if not isinstance(node, dict):
            continue
        node_type = _string_or_none(node.get("node_type"))
        node_key = _string_or_none(node.get("node_key"))
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if node_type == "preprocessing.crop":
            geometry = _crop_geometry(config, frame_width=frame_width, frame_height=frame_height)
            if geometry is None:
                continue
            primitives.append(
                RuntimeOverlayPrimitiveState(
                    primitive_type="box",
                    layer="authoring_crop",
                    label=node_key or "Crop",
                    geometry=geometry,
                    image_context_id=image_context_id,
                    style={"stroke": "#f4b740", "stroke_width": 2.0},
                    metadata={"node_key": node_key, "node_type": node_type, "authoring_config": True},
                )
            )
        if node_type == "preprocessing.roi_extract":
            for index, box in enumerate(config.get("regions") or []):
                geometry = _box_geometry(box, frame_width=frame_width, frame_height=frame_height)
                if geometry is None:
                    continue
                primitives.append(
                    RuntimeOverlayPrimitiveState(
                        primitive_type="roi",
                        layer="authoring_rois",
                        label=_string_or_none(box.get("label")) if isinstance(box, dict) else f"roi_{index + 1}",
                        geometry=geometry,
                        image_context_id=image_context_id,
                        style={"stroke": "#49c47a", "stroke_width": 2.0},
                        metadata={"node_key": node_key, "node_type": node_type, "authoring_config": True},
                    )
                )
        if node_type == "preprocessing.zone_filter":
            for index, box in enumerate(config.get("zones") or []):
                geometry = _box_geometry(box, frame_width=frame_width, frame_height=frame_height)
                if geometry is None:
                    continue
                primitives.append(
                    RuntimeOverlayPrimitiveState(
                        primitive_type="zone",
                        layer="authoring_zones",
                        label=_string_or_none(box.get("label")) if isinstance(box, dict) else f"zone_{index + 1}",
                        geometry=geometry,
                        image_context_id=image_context_id,
                        style={"stroke": "#5db7ff", "stroke_width": 2.0},
                        metadata={"node_key": node_key, "node_type": node_type, "authoring_config": True},
                    )
                )
    return primitives


def _crop_geometry(
    config: dict[str, Any],
    *,
    frame_width: int | None,
    frame_height: int | None,
) -> dict[str, Any] | None:
    x = _timing_number(config.get("x"))
    y = _timing_number(config.get("y"))
    width = _timing_number(config.get("width"))
    height = _timing_number(config.get("height"))
    if x is None or y is None or width is None or height is None or width <= 0 or height <= 0:
        return None
    if frame_width is not None:
        width = min(width, max(frame_width - x, 1))
    if frame_height is not None:
        height = min(height, max(frame_height - y, 1))
    return {
        "x": max(x, 0),
        "y": max(y, 0),
        "width": width,
        "height": height,
        "coordinate_system": "pixel_xy",
    }


def _box_geometry(
    box: object,
    *,
    frame_width: int | None,
    frame_height: int | None,
) -> dict[str, Any] | None:
    if not isinstance(box, dict):
        return None
    x = _timing_number(box.get("x"))
    y = _timing_number(box.get("y"))
    width = _timing_number(box.get("width"))
    height = _timing_number(box.get("height"))
    if x is None or y is None or width is None or height is None or width <= 0 or height <= 0:
        return None
    if x <= 1 and y <= 1 and width <= 1 and height <= 1:
        return {
            "x": max(0.0, min(x, 1.0)),
            "y": max(0.0, min(y, 1.0)),
            "width": max(0.0, min(width, 1.0 - max(0.0, min(x, 1.0)))),
            "height": max(0.0, min(height, 1.0 - max(0.0, min(y, 1.0)))),
        }
    if frame_width is None or frame_height is None:
        return None
    return {
        "x": max(x, 0),
        "y": max(y, 0),
        "width": min(width, max(frame_width - x, 1)),
        "height": min(height, max(frame_height - y, 1)),
        "coordinate_system": "pixel_xy",
    }


def _context_matches_display(
    image_context_id: str | None,
    display_context_id: str | None,
) -> bool:
    return image_context_id is None or display_context_id is None or image_context_id == display_context_id


def _timing_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@router.get(
    "/runtime/channels/{channel_id}/manual-capture/targets",
    response_model=RuntimeManualCaptureTargetListResponse,
)
def get_manual_capture_targets(
    channel_id: str,
    _current_user: ManualCaptureReader,
    settings: SettingsDep,
) -> RuntimeManualCaptureTargetListResponse:
    with session_scope(settings) as session:
        try:
            get_runtime_channel_for_manual_capture(session, channel_id)
            items = list_manual_capture_targets(session)
        except ValueError as exc:
            raise _manual_capture_error(exc) from exc
    return RuntimeManualCaptureTargetListResponse(items=items, meta=build_meta())


@router.post(
    "/runtime/channels/{channel_id}/manual-capture/single",
    response_model=RuntimeManualCaptureResponse,
)
def create_manual_single_capture(
    channel_id: str,
    payload: RuntimeManualCaptureRequest,
    request: Request,
    current_user: ManualCaptureWriter,
    settings: SettingsDep,
) -> RuntimeManualCaptureResponse:
    with session_scope(settings) as session:
        try:
            result = enqueue_manual_capture_request(
                session,
                channel_id=channel_id,
                capture_target_id=payload.capture_target_id,
                mode="single",
                operator_user_id=current_user.user_id,
                live_session_id=payload.live_session_id,
                request_predictions=payload.request_predictions,
                capture_source=payload.capture_source,
                view_profile_id=payload.view_profile_id,
                require_eligibility=payload.require_eligibility,
            )
        except ValueError as exc:
            raise _manual_capture_error(exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.manual_capture.single.requested.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "capture_target_id": payload.capture_target_id,
            "capture_request_id": result.capture_request_id,
            "status": result.status,
            "error_summary": result.error_summary,
            "live_session_id": payload.live_session_id,
            "request_predictions": payload.request_predictions,
            "capture_source": payload.capture_source,
            "view_profile_id": payload.view_profile_id,
            "require_eligibility": payload.require_eligibility,
        },
    )
    return RuntimeManualCaptureResponse(result=result, meta=build_meta())


@router.post(
    "/runtime/channels/{channel_id}/manual-capture/interval-session",
    response_model=RuntimeManualCaptureSessionResponse,
    status_code=202,
)
async def start_manual_interval_capture(
    channel_id: str,
    payload: RuntimeManualIntervalCaptureStartRequest,
    request: Request,
    current_user: ManualCaptureWriter,
    settings: SettingsDep,
) -> RuntimeManualCaptureSessionResponse:
    _validate_manual_interval(payload.interval_seconds, settings)
    with session_scope(settings) as session:
        try:
            session_status = create_manual_capture_session(
                session,
                channel_id=channel_id,
                capture_target_id=payload.capture_target_id,
                interval_seconds=payload.interval_seconds,
                operator_user_id=current_user.user_id,
                live_session_id=payload.live_session_id,
                request_predictions=payload.request_predictions,
                capture_source=payload.capture_source,
                view_profile_id=payload.view_profile_id,
                require_eligibility=payload.require_eligibility,
            )
        except ValueError as exc:
            raise _manual_capture_error(exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.manual_capture.interval.started.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "manual_capture_session_id": session_status.manual_capture_session_id,
            "capture_target_id": payload.capture_target_id,
            "interval_seconds": payload.interval_seconds,
            "live_session_id": payload.live_session_id,
            "request_predictions": payload.request_predictions,
            "capture_source": payload.capture_source,
            "view_profile_id": payload.view_profile_id,
            "require_eligibility": payload.require_eligibility,
        },
    )
    return RuntimeManualCaptureSessionResponse(session=session_status, meta=build_meta())


@router.post(
    "/runtime/channels/{channel_id}/manual-capture/interval-session:stop",
    response_model=RuntimeManualCaptureSessionResponse,
)
async def stop_manual_interval_capture(
    channel_id: str,
    payload: RuntimeManualIntervalCaptureStopRequest,
    request: Request,
    current_user: ManualCaptureWriter,
    settings: SettingsDep,
) -> RuntimeManualCaptureSessionResponse:
    with session_scope(settings) as session:
        try:
            session_status = stop_manual_capture_session(session, channel_id=channel_id)
        except ValueError as exc:
            raise _manual_capture_error(exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.manual_capture.interval.stopped.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "manual_capture_session_id": session_status.manual_capture_session_id,
            "state": session_status.state,
            "capture_count": session_status.capture_count,
            "failure_count": session_status.failure_count,
            "reason": payload.reason,
        },
    )
    return RuntimeManualCaptureSessionResponse(session=session_status, meta=build_meta())


@router.get(
    "/runtime/channels/{channel_id}/manual-capture/session",
    response_model=RuntimeManualCaptureSessionResponse,
)
def get_manual_capture_session(
    channel_id: str,
    _current_user: ManualCaptureReader,
    settings: SettingsDep,
) -> RuntimeManualCaptureSessionResponse:
    with session_scope(settings) as session:
        try:
            session_status = get_manual_capture_session_status(session, channel_id)
        except ValueError as exc:
            raise _manual_capture_error(exc) from exc
    return RuntimeManualCaptureSessionResponse(session=session_status, meta=build_meta())


@router.get(
    "/runtime/channels/{channel_id}/manual-capture/history",
    response_model=RuntimeManualCaptureHistoryResponse,
)
def get_manual_capture_history(
    channel_id: str,
    _current_user: ManualCaptureReader,
    settings: SettingsDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> RuntimeManualCaptureHistoryResponse:
    with session_scope(settings) as session:
        try:
            items = list_manual_capture_history(session, channel_id=channel_id, limit=limit)
        except ValueError as exc:
            raise _manual_capture_error(exc) from exc
    return RuntimeManualCaptureHistoryResponse(items=items, meta=build_meta())


@router.get(
    "/runtime/channels/{channel_id}/captures/history",
    response_model=RuntimeCaptureHistoryResponse,
)
def get_capture_delivery_history(
    channel_id: str,
    _current_user: ManualCaptureReader,
    settings: SettingsDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> RuntimeCaptureHistoryResponse:
    with session_scope(settings) as session:
        if get_runtime_channel_detail(session, channel_id) is None:
            raise NotFoundError(
                message="Channel not found",
                detail=f"No runtime channel exists for id {channel_id}.",
            )
        items, total = list_runtime_capture_history(
            session,
            channel_id=channel_id,
            limit=limit,
        )
    return RuntimeCaptureHistoryResponse(items=items, total=total, meta=build_meta())


@router.post(
    "/runtime/channels/{channel_id}:start",
    response_model=RuntimeChannelCommandResponse,
    status_code=202,
)
def start_channel(
    channel_id: str,
    payload: RuntimeChannelCommandRequest,
    request: Request,
    current_user: RuntimeControlWriter,
    settings: SettingsDep,
) -> RuntimeChannelCommandResponse:
    channel, accepted_job = _mutate_channel_command(
        settings=settings,
        channel_id=channel_id,
        command="start",
        current_user=current_user,
        payload=payload,
    )
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.start.requested.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={"reason": payload.reason, "accepted_job": accepted_job.model_dump(mode="json")},
    )
    return RuntimeChannelCommandResponse(
        channel=channel,
        accepted_job=accepted_job,
        meta=build_meta(),
    )


@router.post(
    "/runtime/channels/{channel_id}:stop",
    response_model=RuntimeChannelCommandResponse,
    status_code=202,
)
def stop_channel(
    channel_id: str,
    payload: RuntimeChannelCommandRequest,
    request: Request,
    current_user: RuntimeControlWriter,
    settings: SettingsDep,
) -> RuntimeChannelCommandResponse:
    channel, accepted_job = _mutate_channel_command(
        settings=settings,
        channel_id=channel_id,
        command="stop",
        current_user=current_user,
        payload=payload,
    )
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.stop.requested.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "reason": payload.reason,
            "force": payload.force,
            "accepted_job": accepted_job.model_dump(mode="json"),
        },
    )
    return RuntimeChannelCommandResponse(
        channel=channel,
        accepted_job=accepted_job,
        meta=build_meta(),
    )


@router.post(
    "/runtime/channels/{channel_id}:restart",
    response_model=RuntimeChannelCommandResponse,
    status_code=202,
)
def restart_channel(
    channel_id: str,
    payload: RuntimeChannelCommandRequest,
    request: Request,
    current_user: RuntimeControlWriter,
    settings: SettingsDep,
) -> RuntimeChannelCommandResponse:
    channel, accepted_job = _mutate_channel_command(
        settings=settings,
        channel_id=channel_id,
        command="restart",
        current_user=current_user,
        payload=payload,
    )
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="runtime.channel.restart.requested.v1",
        aggregate_type="runtime_channel",
        aggregate_id=channel_id,
        payload={
            "reason": payload.reason,
            "force": payload.force,
            "accepted_job": accepted_job.model_dump(mode="json"),
        },
    )
    return RuntimeChannelCommandResponse(
        channel=channel,
        accepted_job=accepted_job,
        meta=build_meta(),
    )


def _mutate_channel_command(
    *,
    settings: PlatformSettings,
    channel_id: str,
    command: str,
    current_user: RuntimeAdminPrincipal,
    payload: RuntimeChannelCommandRequest,
) -> tuple[RuntimeChannelSummary, RuntimeControlCommandSummary]:
    requested_by = _runtime_command_actor(current_user)
    with session_scope(settings) as session:
        try:
            if command == "start":
                return request_runtime_channel_start(
                    session,
                    channel_id,
                    requested_by=requested_by,
                    reason=payload.reason,
                    idempotency_key=payload.idempotency_key,
                    source_secret_root=settings.storage.runtime_root() / "source-secrets",
                )
            if command == "stop":
                return request_runtime_channel_stop(
                    session,
                    channel_id,
                    requested_by=requested_by,
                    reason=payload.reason,
                    idempotency_key=payload.idempotency_key,
                    command_payload={"force": True} if payload.force else None,
                )
            if command == "restart":
                return request_runtime_channel_restart(
                    session,
                    channel_id,
                    requested_by=requested_by,
                    reason=payload.reason,
                    idempotency_key=payload.idempotency_key,
                    source_secret_root=settings.storage.runtime_root() / "source-secrets",
                    command_payload={"force": True} if payload.force else None,
                )
            raise ValueError("Unsupported channel runtime command.")
        except ValueError as exc:
            raise _channel_error(exc) from exc


def _mutate_channel_stream(
    *,
    settings: PlatformSettings,
    request: Request,
    current_user: RuntimeAdminPrincipal,
    channel_id: str,
    command: Literal["start", "stop", "restart"],
) -> RuntimeChannelStreamControlResponse:
    with session_scope(settings) as session:
        try:
            if command == "start":
                row = request_channel_stream_start(session, channel_id)
            elif command == "stop":
                row = request_channel_stream_stop(session, channel_id)
            else:
                row = request_channel_stream_restart(session, channel_id)
            desired_state = str(row.stream_desired_state)
            actual_state = str(row.stream_actual_state)
        except ValueError as exc:
            raise _channel_error(exc) from exc
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type=f"runtime.channel.stream.{command}.requested.v1",
        aggregate_type="runtime_channel_stream",
        aggregate_id=channel_id,
        payload={"desired_state": desired_state, "actual_state": actual_state},
    )
    return RuntimeChannelStreamControlResponse(
        channel_id=channel_id,
        desired_state=desired_state,
        actual_state=actual_state,
        meta=build_meta(),
    )


def _runtime_command_actor(principal: RuntimeAdminPrincipal) -> str:
    username = getattr(principal, "username", None)
    if isinstance(username, str) and username:
        return username
    subject_id = getattr(principal, "subject_id", None)
    if isinstance(subject_id, str) and subject_id:
        return subject_id
    key_id = getattr(principal, "key_id", None)
    if isinstance(key_id, str) and key_id:
        return key_id
    return "runtime-admin-api"


async def _persist_channel_test_file(
    file: UploadFile,
    *,
    settings: PlatformSettings,
) -> dict[str, object]:
    raw_name = sanitize_filename(file.filename or "test-file")
    suffix = Path(raw_name).suffix.lower()
    if suffix not in _TEST_FILE_IMAGE_EXTENSIONS | _TEST_FILE_VIDEO_EXTENSIONS:
        raise PlatformError(
            code="runtime_channel_test_file_invalid",
            message="The selected test file type is not supported.",
            detail="Use an image file or a short video file for channel test sources.",
            status_code=415,
        )
    raw_content_type = (file.content_type or "application/octet-stream").split(";", 1)[0].lower()
    if (
        suffix in _TEST_FILE_VIDEO_EXTENSIONS
        and raw_content_type not in _TEST_FILE_VIDEO_MIME_TYPES
    ):
        raise PlatformError(
            code="runtime_channel_test_file_invalid",
            message="The selected test video type is not supported.",
            detail=f"Received content type {raw_content_type}.",
            status_code=415,
        )
    upload_id = generate_prefixed_id("tfl")
    destination_root = settings.storage.runtime_test_files_root()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / f"{upload_id}_{raw_name}"
    total_bytes = 0
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await file.read(_TEST_FILE_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > settings.request_limits.upload_body_limit_bytes:
                    raise PlatformError(
                        code="runtime_channel_test_file_too_large",
                        message="The selected test file is too large.",
                        detail=(
                            "Channel test files must fit within the configured upload body "
                            f"limit of {settings.request_limits.upload_body_limit_bytes} bytes."
                        ),
                        status_code=413,
                    )
                handle.write(chunk)
        if total_bytes <= 0:
            raise PlatformError(
                code="runtime_channel_test_file_empty",
                message="The selected test file is empty.",
                status_code=422,
            )
        content_type = raw_content_type
        kind = "video"
        if suffix in _TEST_FILE_IMAGE_EXTENSIONS:
            if total_bytes > settings.request_limits.max_image_bytes:
                raise PlatformError(
                    code="runtime_channel_test_file_too_large",
                    message="The selected test image is too large.",
                    detail=(
                        "Channel test images must fit within the configured image limit "
                        f"of {settings.request_limits.max_image_bytes} bytes."
                    ),
                    status_code=413,
                )
            inspection = inspect_image_bytes(destination.read_bytes())
            content_type = inspection.mime_type
            kind = "image"
        source_config = {
            "path": str(destination),
            "file_name": raw_name,
            "original_filename": raw_name,
            "content_type": content_type,
            "size_bytes": total_bytes,
            "kind": kind,
            "loop": True,
            "managed_test_file": True,
            "managed_upload_id": upload_id,
        }
        return {
            "file_name": raw_name,
            "content_type": content_type,
            "size_bytes": total_bytes,
            "path": str(destination),
            "source_config": source_config,
        }
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _camera_stream_is_active(channel: RuntimeChannel) -> bool:
    return bool(
        channel.stream_desired_state == "running"
        or channel.stream_actual_state in {"starting", "ready", "recovering"}
    )


def _capture_calibration_frame_from_stream_ring(
    *,
    channel_id: str,
    settings: PlatformSettings,
    timeout_ms: int,
    max_bytes: int,
) -> RuntimeLiveFrame:
    reader = LatestFrameRingReader(
        runtime_dir=settings.runtime.camera_media_runtime_dir,
        channel_id=channel_id,
    )
    try:
        shared = reader.read_latest(timeout_seconds=max(timeout_ms / 1000, 0.05))
    except Exception as exc:
        raise PlatformError(
            code="runtime_calibration_stream_frame_unavailable",
            message="The running camera stream has no readable source frame.",
            detail=str(exc),
            status_code=503,
            retryable=True,
            metadata={"channel_id": channel_id},
        ) from exc
    finally:
        reader.close()
    if shared is None:
        raise PlatformError(
            code="runtime_calibration_stream_frame_unavailable",
            message="The running camera stream has no readable source frame.",
            detail=(
                "Wait until the stream is ready and camera-media has published its raw "
                "shared-memory frame. The inference pipeline does not need to be started."
            ),
            status_code=503,
            retryable=True,
            metadata={"channel_id": channel_id},
        )
    captured_at = utc_now_iso()
    descriptor = shared.descriptor
    try:
        payload = FramePayload.from_cv2_frame(
            shared.frame,
            jpeg_quality=95,
            source_uri=descriptor.source_uri,
            timestamp=captured_at,
            image_context=RuntimeImageContext(
                context_id=f"calibration-stream:{channel_id}:{shared.sequence}",
                kind="original",
                width=descriptor.width,
                height=descriptor.height,
                source_uri=descriptor.source_uri,
                transform=RuntimeImageTransform(),
                metadata={"calibration_source": "camera_media_shared_memory"},
            ),
            metadata={
                **dict(descriptor.source_metadata or {}),
                "source": "camera_media_shared_memory",
                "transport": "shared_memory_latest_frame",
                "capture_monotonic_ns": shared.capture_monotonic_ns,
                "capture_wall_time_ns": shared.capture_wall_time_ns,
                "frame_ring_generation": descriptor.generation,
            },
        )
        image_bytes = payload.image_bytes()
    except Exception as exc:
        raise PlatformError(
            code="runtime_calibration_stream_frame_encode_failed",
            message="The camera stream source frame could not be encoded for calibration.",
            detail=str(exc),
            status_code=503,
            retryable=True,
            metadata={"channel_id": channel_id, "sequence": shared.sequence},
        ) from exc
    if len(image_bytes) > max_bytes:
        raise PlatformError(
            code="runtime_calibration_frame_too_large",
            message="The camera stream calibration frame is too large.",
            status_code=413,
            retryable=False,
            metadata={
                "channel_id": channel_id,
                "max_bytes": max_bytes,
                "actual_bytes": len(image_bytes),
            },
        )
    return RuntimeLiveFrame(
        image_base64=base64.b64encode(image_bytes).decode("ascii"),
        mime_type="image/jpeg",
        width=payload.width,
        height=payload.height,
        source_timestamp=captured_at,
        captured_at=captured_at,
        sequence=max(int(shared.sequence), 0),
        image_context=payload.image_context.model_dump(mode="json")
        if payload.image_context is not None
        else {},
        metadata={
            **payload.metadata,
            "calibration_capture_mode": "camera_media_stream",
            "camera_released_after_capture": False,
            "stream_remained_active": True,
        },
    )


def _calibration_live_state(
    *,
    channel_id: str,
    actual_state: RuntimeObservedState,
    frame: RuntimeLiveFrame,
    source_type: str,
    capture_mode: Literal["camera_media_stream", "exclusive_single_frame"],
) -> RuntimeLiveViewState:
    stream_capture = capture_mode == "camera_media_stream"
    return RuntimeLiveViewState(
        channel_id=channel_id,
        actual_state=actual_state,
        available=True,
        stale=False,
        display_context_id=f"calibration:{channel_id}:{frame.sequence}",
        display_context={
            "kind": "original",
            "width": frame.width,
            "height": frame.height,
            "purpose": "runtime_calibration",
            "capture_mode": capture_mode,
            "supported_workflows": [
                "board_surface_calibration",
                "intrinsic_camera_calibration",
                "stem_curvature_process_calibration",
            ],
        },
        frame=frame,
        source_health=RuntimeLiveSourceHealth(
            state=HealthState.OK,
            status_text=(
                "Calibration source frame read from the running camera stream"
                if stream_capture
                else "Exclusive calibration frame captured; camera released"
            ),
            connected=stream_capture,
        ),
        diagnostics=RuntimeLiveDiagnostics(
            source_type=source_type,
            warnings=[
                (
                    "The raw camera-media frame was sampled without starting the inference "
                    "pipeline or interrupting the stream."
                    if stream_capture
                    else "The camera was opened for one frame and released immediately."
                )
            ],
        ),
        recovery_hint=None,
        updated_at=utc_now_iso(),
    )


def _capture_calibration_frame_from_source(
    *,
    source_type: RuntimeSourceType,
    source_config: dict[str, Any],
    source_secret_refs: dict[str, str],
    timeout_ms: int,
    max_bytes: int,
) -> RuntimeLiveFrame:
    if build_frame_source is None:
        raise PlatformError(
            code="runtime_calibration_capture_unavailable",
            message="This service cannot acquire a calibration frame from camera sources.",
            detail="The realtime camera source adapters are not available in this runtime.",
            status_code=503,
            retryable=True,
            metadata={"source_type": source_type.value},
        )
    timeout_seconds = max(timeout_ms / 1000, 0.5)
    capture_config = dict(source_config or {})
    for key in ("read_timeout_seconds", "fetch_timeout_seconds"):
        try:
            configured = float(capture_config.get(key) or timeout_seconds)
        except (TypeError, ValueError):
            configured = timeout_seconds
        capture_config[key] = max(min(configured, timeout_seconds), 0.05)
    try:
        reconnect_seconds = float(capture_config.get("reconnect_seconds") or 0.05)
    except (TypeError, ValueError):
        reconnect_seconds = 0.05
    capture_config["reconnect_seconds"] = max(
        min(reconnect_seconds, timeout_seconds),
        0.05,
    )
    stop_event = threading.Event()
    try:
        source = build_frame_source(
            source_type=source_type.value,
            config=capture_config,
            secret_refs=source_secret_refs,
        )
    except (RtspSourceConfigurationError, ValueError) as exc:
        raise PlatformError(
            code="runtime_calibration_source_invalid",
            message="The selected camera source is not configured for calibration capture.",
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
            encoded = packet.ensure_encoded()
            if not encoded.image_bytes:
                continue
            if len(encoded.image_bytes) > max_bytes:
                raise PlatformError(
                    code="runtime_calibration_frame_too_large",
                    message="The calibration camera returned an image that is too large.",
                    status_code=413,
                    retryable=False,
                    metadata={
                        "max_bytes": max_bytes,
                        "source_type": source_type.value,
                    },
                )
            captured_at = utc_now_iso()
            return RuntimeLiveFrame(
                image_base64=base64.b64encode(encoded.image_bytes).decode("ascii"),
                mime_type=encoded.mime_type,
                width=encoded.width,
                height=encoded.height,
                source_timestamp=encoded.source_timestamp,
                captured_at=captured_at,
                sequence=max(int(encoded.sequence), 0),
                image_context={
                    "context_id": f"calibration:{captured_at}:{encoded.sequence}",
                    "kind": "original",
                    "width": encoded.width,
                    "height": encoded.height,
                },
                metadata={
                    **dict(encoded.metadata or {}),
                    "source": "exclusive_calibration_capture",
                    "source_type": source_type.value,
                    "camera_released_after_capture": True,
                },
            )
        health = getattr(source, "health", None)
        detail = (
            getattr(health, "last_error", None)
            or getattr(health, "status_text", None)
            or "The camera did not return a frame before the calibration timeout."
        )
        raise PlatformError(
            code="runtime_calibration_capture_unavailable",
            message="The selected camera did not produce a calibration frame.",
            detail=str(detail),
            status_code=502,
            retryable=True,
            metadata={"source_type": source_type.value},
        )
    except PlatformError:
        raise
    except Exception as exc:
        raise PlatformError(
            code="runtime_calibration_capture_unavailable",
            message="The selected camera could not be read for calibration.",
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


def _channel_error(exc: ValueError) -> PlatformError:
    detail = str(exc)
    if "does not exist" in detail:
        return NotFoundError(message="Channel not found", detail=detail)
    return PlatformError(
        code="runtime_channel_invalid",
        message="The channel request could not be completed.",
        detail=detail,
        status_code=422,
    )


def _trigger_endpoint_error(exc: ValueError) -> PlatformError:
    detail = str(exc)
    if "does not exist" in detail:
        return NotFoundError(message="Trigger endpoint metadata not found", detail=detail)
    return PlatformError(
        code="runtime_trigger_endpoint_invalid",
        message="The trigger endpoint metadata request could not be completed.",
        detail=detail,
        status_code=422,
    )


def _connector_error(exc: ValueError) -> PlatformError:
    detail = str(exc)
    if "does not exist" in detail:
        return NotFoundError(message="Output connector not found", detail=detail)
    return PlatformError(
        code="runtime_output_connector_invalid",
        message="The output connector request could not be completed.",
        detail=detail,
        status_code=422,
    )


def _pipeline_error(exc: ValueError) -> PlatformError:
    detail = str(exc)
    if "does not exist" in detail:
        return NotFoundError(message="Pipeline not found", detail=detail)
    return PlatformError(
        code="runtime_pipeline_invalid",
        message="The pipeline request could not be completed.",
        detail=detail,
        status_code=422,
    )


def _manual_capture_error(exc: ValueError) -> PlatformError:
    detail = str(exc)
    if "Channel does not exist" in detail:
        return NotFoundError(message="Channel not found", detail=detail)
    if "capture target does not exist" in detail:
        return NotFoundError(message="Label Studio project not found", detail=detail)
    return PlatformError(
        code="runtime_manual_capture_invalid",
        message="The manual live capture request could not be completed.",
        detail=detail,
        status_code=422,
    )


def _validate_manual_interval(interval_seconds: int, settings: PlatformSettings) -> None:
    min_seconds = max((settings.runtime.interval_min_period_ms + 999) // 1000, 1)
    if interval_seconds < min_seconds:
        raise PlatformError(
            code="runtime_manual_capture_interval_invalid",
            message="The manual capture interval is too short.",
            detail=f"Use an interval of at least {min_seconds} second(s).",
            status_code=422,
        )


def _write_pipeline_audit(
    *,
    settings: PlatformSettings,
    request: Request,
    current_user: SessionUser,
    event_type: str,
    aggregate_id: str,
    payload: dict[str, object],
) -> None:
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type=event_type,
        aggregate_type="runtime_pipeline",
        aggregate_id=aggregate_id,
        payload=payload,
    )


def _write_trigger_endpoint_audit(
    *,
    settings: PlatformSettings,
    request: Request,
    current_user: SessionUser,
    event_type: str,
    item: RuntimeTriggerEndpointDetail,
) -> None:
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type=event_type,
        aggregate_type="runtime_trigger_endpoint",
        aggregate_id=item.trigger_endpoint_id,
        payload={
            "name": item.name,
            "description": item.description,
            "slug": item.slug,
            "channel_id": item.channel_id,
            "pipeline_version_id": item.pipeline_version_id,
            "trigger_type": item.trigger_type,
            "enabled": item.enabled,
            "auth_required": item.auth_required,
            "auth_policy": item.auth_policy,
            "timeout_ms": item.timeout_ms,
            "response_policy": item.response_policy.model_dump(mode="json"),
        },
    )


def _write_connector_audit(
    *,
    settings: PlatformSettings,
    request: Request,
    current_user: SessionUser,
    event_type: str,
    item: RuntimeOutputConnectorDetail,
) -> None:
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type=event_type,
        aggregate_type="output_connector",
        aggregate_id=item.output_connector_id,
        payload={
            "channel_id": item.channel_id,
            "connector_type": item.connector_type,
            "name": item.name,
            "enabled": item.enabled,
            "status": item.status.value,
            "source_node_key": item.source_node_key,
        },
    )
