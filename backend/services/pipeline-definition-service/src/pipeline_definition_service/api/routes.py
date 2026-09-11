from __future__ import annotations

from typing import Annotated, Literal

from cv_contracts import ResponseMeta
from cv_contracts.runtime import (
    RuntimeBoundaryResponse,
    RuntimeModelCapabilityListResponse,
    RuntimeNodeTypeCatalogResponse,
    RuntimePipelineDefinitionCreateRequest,
    RuntimePipelineDefinitionDetail,
    RuntimePipelineDefinitionListResponse,
    RuntimePipelineDefinitionSummary,
    RuntimePipelineTemplateInstantiateRequest,
    RuntimePipelineValidationRequest,
    RuntimePipelineValidationResponse,
    RuntimePipelineVersionCreateRequest,
    RuntimePipelineVersionDetail,
)
from cv_core import PlatformSettings
from cv_db import (
    archive_pipeline_definition,
    create_pipeline_definition,
    create_pipeline_version,
    get_pipeline_definition_detail,
    instantiate_pipeline_template,
    list_pipeline_definitions,
    list_runtime_model_capability_options,
    list_runtime_node_types,
    normalize_pipeline_request_graph,
    publish_pipeline_version,
    runtime_pipeline_output_catalog,
    session_scope,
    validate_pipeline_request,
    validate_pipeline_version,
)
from cv_runtime import runtime_service_boundary
from cv_status import RuntimeMode, RuntimeNodeCategory, TargetPath, TaskType
from cv_utils import NotFoundError, PlatformError, get_correlation_id, get_request_id, utc_now_iso
from fastapi import APIRouter, Depends, Query, Request

from pipeline_definition_service import SERVICE_NAME

router = APIRouter(tags=["runtime-boundary"])


def get_settings(request: Request) -> PlatformSettings:
    return request.app.state.settings


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
    settings: SettingsDep,
    category: RuntimeNodeCategory | None = None,
    mode: RuntimeMode | None = None,
) -> RuntimeNodeTypeCatalogResponse:
    with session_scope(settings) as session:
        items = list_runtime_node_types(session, category=category, mode=mode)
    return RuntimeNodeTypeCatalogResponse(items=items, meta=_meta())


@router.get("/runtime/model-capabilities", response_model=RuntimeModelCapabilityListResponse)
def get_model_capabilities(
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
    return RuntimeModelCapabilityListResponse(items=items, meta=_meta())


@router.get("/runtime/pipeline-definitions", response_model=RuntimePipelineDefinitionListResponse)
def get_pipeline_definitions(
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
    return RuntimePipelineDefinitionListResponse(items=items, meta=_meta())


@router.post(
    "/runtime/pipeline-definitions",
    response_model=RuntimePipelineDefinitionDetail,
    status_code=201,
)
def create_definition(
    payload: RuntimePipelineDefinitionCreateRequest,
    settings: SettingsDep,
) -> RuntimePipelineDefinitionDetail:
    with session_scope(settings) as session:
        return create_pipeline_definition(session, payload, created_by="service")


@router.get(
    "/runtime/pipeline-definitions/{pipeline_definition_id}",
    response_model=RuntimePipelineDefinitionDetail,
)
def get_definition(
    pipeline_definition_id: str,
    settings: SettingsDep,
) -> RuntimePipelineDefinitionDetail:
    with session_scope(settings) as session:
        item = get_pipeline_definition_detail(session, pipeline_definition_id)
    if item is None:
        raise NotFoundError(message="Pipeline not found", detail="No pipeline definition exists.")
    return item


@router.delete(
    "/runtime/pipeline-definitions/{pipeline_definition_id}",
    response_model=RuntimePipelineDefinitionSummary,
)
def delete_definition(
    pipeline_definition_id: str,
    settings: SettingsDep,
    reason: str | None = Query(default=None, max_length=500),
) -> RuntimePipelineDefinitionSummary:
    with session_scope(settings) as session:
        try:
            return archive_pipeline_definition(
                session,
                pipeline_definition_id,
                archived_by="service",
                reason=reason,
            )
        except ValueError as exc:
            raise _pipeline_error(exc) from exc


@router.post(
    "/runtime/pipeline-definitions/{pipeline_definition_id}/versions",
    response_model=RuntimePipelineVersionDetail,
    status_code=201,
)
def create_definition_version(
    pipeline_definition_id: str,
    payload: RuntimePipelineVersionCreateRequest,
    settings: SettingsDep,
) -> RuntimePipelineVersionDetail:
    with session_scope(settings) as session:
        try:
            return create_pipeline_version(
                session,
                pipeline_definition_id,
                payload,
                created_by="service",
            )
        except ValueError as exc:
            raise _pipeline_error(exc) from exc


@router.post(
    "/runtime/pipeline-templates/{template_id}:instantiate",
    response_model=RuntimePipelineDefinitionDetail,
    status_code=201,
)
def instantiate_template(
    template_id: str,
    payload: RuntimePipelineTemplateInstantiateRequest,
    settings: SettingsDep,
) -> RuntimePipelineDefinitionDetail:
    with session_scope(settings) as session:
        try:
            return instantiate_pipeline_template(
                session,
                template_id,
                payload,
                created_by="service",
            )
        except ValueError as exc:
            raise _pipeline_error(exc) from exc


@router.post("/runtime/pipeline-versions:validate", response_model=RuntimePipelineValidationResponse)
def validate_graph(
    payload: RuntimePipelineValidationRequest,
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
        meta=_meta(),
    )


@router.post(
    "/runtime/pipeline-versions/{pipeline_version_id}:validate",
    response_model=RuntimePipelineVersionDetail,
)
def validate_version(
    pipeline_version_id: str,
    settings: SettingsDep,
) -> RuntimePipelineVersionDetail:
    with session_scope(settings) as session:
        try:
            return validate_pipeline_version(session, pipeline_version_id)
        except ValueError as exc:
            raise _pipeline_error(exc) from exc


@router.post(
    "/runtime/pipeline-versions/{pipeline_version_id}:publish",
    response_model=RuntimePipelineVersionDetail,
)
def publish_version(
    pipeline_version_id: str,
    settings: SettingsDep,
) -> RuntimePipelineVersionDetail:
    with session_scope(settings) as session:
        try:
            return publish_pipeline_version(
                session,
                pipeline_version_id,
                published_by="service",
            )
        except ValueError as exc:
            raise _pipeline_error(exc) from exc


def _meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
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
