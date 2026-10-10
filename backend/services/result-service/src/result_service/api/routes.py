from datetime import datetime
from typing import Annotated

from cv_contracts import SessionUser
from cv_contracts.runtime import (
    RuntimeBoundaryResponse,
    RuntimeResultDetailResponse,
    RuntimeResultListResponse,
    RuntimeStemOrientationReviewCreateRequest,
    RuntimeStemOrientationReviewListResponse,
    RuntimeStemOrientationReviewResponse,
    RuntimeStemShadowEvaluationResponse,
)
from cv_core import PlatformSettings
from cv_db import (
    RuntimeResultQuery,
    RuntimeStemShadowEvaluationQuery,
    create_stem_orientation_review,
    get_latest_runtime_result_detail,
    get_runtime_result_detail,
    list_runtime_results_response,
    list_stem_orientation_reviews,
    runtime_result_detail_response,
    session_scope,
    stem_shadow_evaluation_response,
)
from cv_runtime import runtime_service_boundary
from cv_status import RuntimeMode, UserRole
from cv_utils import NotFoundError, PlatformError
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from result_service import SERVICE_NAME
from result_service.dependencies import (
    build_meta,
    get_settings,
    require_permission,
    resolve_result_endpoint_access,
)

router = APIRouter(tags=["runtime-results"])

ResultEndpointAccess = Annotated[None, Depends(resolve_result_endpoint_access)]
ResultReader = Annotated[
    object,
    Depends(
        require_permission(
            "runtime.results.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.VIEWER,
        )
    ),
]
ResultReviewer = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "runtime.results.review",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
        )
    ),
]


@router.get("/runtime/boundary", response_model=RuntimeBoundaryResponse)
def get_runtime_boundary() -> RuntimeBoundaryResponse:
    boundary = runtime_service_boundary(SERVICE_NAME)
    return RuntimeBoundaryResponse(
        service_name=boundary.service_name,
        responsibilities=list(boundary.responsibilities),
        non_responsibilities=list(boundary.non_responsibilities),
    )


@router.get("/runtime/results", response_model=RuntimeResultListResponse)
def list_runtime_results(
    _current_user: ResultReader,
    settings=Depends(get_settings),
    channel_id: str | None = Query(default=None, min_length=3, max_length=64),
    pipeline_version_id: str | None = Query(default=None, min_length=3, max_length=64),
    mode: RuntimeMode | None = None,
    status: str | None = Query(default=None, pattern="^(completed|failed|timeout)$"),
    result_type: str | None = Query(default=None, min_length=2, max_length=64),
    completed_from: datetime | None = None,
    completed_to: datetime | None = None,
    search: str | None = Query(default=None, min_length=1, max_length=160),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RuntimeResultListResponse:
    query = RuntimeResultQuery(
        channel_id=channel_id,
        pipeline_version_id=pipeline_version_id,
        mode=mode,
        status=status,
        result_type=result_type,
        completed_from=completed_from,
        completed_to=completed_to,
        search=search,
        limit=limit,
        offset=offset,
    )
    with session_scope(settings) as session:
        return list_runtime_results_response(session, query=query, meta=build_meta())


@router.get(
    "/runtime/pipeline-versions/{pipeline_version_id}/stem-shadow-evaluation",
    response_model=RuntimeStemShadowEvaluationResponse,
)
def get_stem_shadow_evaluation(
    pipeline_version_id: str,
    _current_user: ResultReader,
    settings=Depends(get_settings),
    completed_from: datetime | None = None,
    completed_to: datetime | None = None,
    maximum_sessions: int = Query(default=2_000, ge=10, le=10_000),
) -> RuntimeStemShadowEvaluationResponse:
    query = RuntimeStemShadowEvaluationQuery(
        completed_from=completed_from,
        completed_to=completed_to,
        maximum_sessions=maximum_sessions,
    )
    with session_scope(settings) as session:
        try:
            return stem_shadow_evaluation_response(
                session,
                pipeline_version_id=pipeline_version_id,
                query=query,
                meta=build_meta(),
            )
        except LookupError as exc:
            raise NotFoundError(message="Pipeline version not found", detail=str(exc)) from exc


@router.get(
    "/runtime/channels/{channel_id}/results/latest",
    response_model=RuntimeResultDetailResponse,
)
def get_latest_channel_result(
    channel_id: str,
    _access: ResultEndpointAccess,
    settings=Depends(get_settings),
) -> RuntimeResultDetailResponse:
    with session_scope(settings) as session:
        item = get_latest_runtime_result_detail(
            session,
            channel_id=channel_id,
            mode=RuntimeMode.REALTIME,
        )
    if item is None:
        raise NotFoundError(
            message="Result not found",
            detail=f"No runtime result exists for channel {channel_id}.",
        )
    return runtime_result_detail_response(item, meta=build_meta())


@router.get(
    "/runtime/results/{result_record_id}/artifacts/{artifact_name}", response_class=FileResponse
)
def get_runtime_result_artifact(
    result_record_id: str,
    artifact_name: str,
    _current_user: ResultReader,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
) -> FileResponse:
    from cv_db.bark_artifacts import bark_artifact_path
    from cv_db.models import ResultRecord

    with session_scope(settings) as session:
        row = session.get(ResultRecord, result_record_id)
        if row is None or not row.history_retained:
            raise NotFoundError(message="Result not found")
        try:
            path = bark_artifact_path(settings, row, artifact_name)
        except ValueError as exc:
            raise NotFoundError(message="Artifact not found", detail=str(exc)) from exc
    return FileResponse(path, filename=artifact_name)


@router.get("/runtime/results/{result_record_id}", response_model=RuntimeResultDetailResponse)
def get_runtime_result(
    result_record_id: str,
    _current_user: ResultReader,
    settings=Depends(get_settings),
) -> RuntimeResultDetailResponse:
    with session_scope(settings) as session:
        item = get_runtime_result_detail(session, result_record_id)
    if item is None:
        raise NotFoundError(
            message="Result not found",
            detail=f"No runtime result exists for id {result_record_id}.",
        )
    return runtime_result_detail_response(item, meta=build_meta())


@router.get(
    "/runtime/results/{result_record_id}/stem-orientation-reviews",
    response_model=RuntimeStemOrientationReviewListResponse,
)
def get_stem_orientation_reviews(
    result_record_id: str,
    _current_user: ResultReader,
    settings=Depends(get_settings),
) -> RuntimeStemOrientationReviewListResponse:
    with session_scope(settings) as session:
        result = get_runtime_result_detail(session, result_record_id)
        if result is None:
            raise NotFoundError(
                message="Result not found",
                detail=f"No retained runtime result exists for id {result_record_id}.",
            )
        items = list_stem_orientation_reviews(
            session,
            result_record_id=result_record_id,
        )
    return RuntimeStemOrientationReviewListResponse(
        result_record_id=result_record_id,
        items=items,
        latest=items[0] if items else None,
        meta=build_meta(),
    )


@router.post(
    "/runtime/results/{result_record_id}/stem-orientation-reviews",
    response_model=RuntimeStemOrientationReviewResponse,
    status_code=201,
)
def append_stem_orientation_review(
    result_record_id: str,
    payload: RuntimeStemOrientationReviewCreateRequest,
    current_user: ResultReviewer,
    settings=Depends(get_settings),
) -> RuntimeStemOrientationReviewResponse:
    with session_scope(settings) as session:
        try:
            item = create_stem_orientation_review(
                session,
                result_record_id=result_record_id,
                payload=payload,
                reviewer_user_id=current_user.user_id,
                reviewer_username=current_user.username,
            )
        except LookupError as exc:
            raise NotFoundError(message="Result not found", detail=str(exc)) from exc
        except ValueError as exc:
            raise PlatformError(
                code="stem_orientation_review_not_allowed",
                message="Stem-orientation review is not allowed for this result.",
                status_code=409,
                detail=str(exc),
            ) from exc
    return RuntimeStemOrientationReviewResponse(item=item, meta=build_meta())
