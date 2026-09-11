from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse

from admin_api.application.audit import write_admin_audit_event
from admin_api.dependencies import build_meta, get_settings, require_permission
from cv_contracts import (
    BatchJobCreateRequest,
    BatchJobDetail,
    BatchJobSummary,
    BatchResultResponse,
    ListResponse,
    SessionUser,
)
from cv_status import UserRole
from cv_utils import PlatformError
from inference_gateway.runtime.service import BatchGatewayService

router = APIRouter(tags=["batch-jobs"])

BatchReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "batch.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.VIEWER,
        )
    ),
]
BatchWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "batch.write",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
        )
    ),
]


def _service(settings) -> BatchGatewayService:
    from cv_status import TargetPath

    return BatchGatewayService(
        settings,
        service_name="admin-api",
        target_path=TargetPath.BATCH,
    )


@router.post("/batch-jobs", response_model=BatchJobSummary, status_code=202)
def submit_batch_job(
    payload: BatchJobCreateRequest,
    request: Request,
    current_user: BatchWriter,
    settings=Depends(get_settings),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> BatchJobSummary:
    item = _service(settings).submit_batch_job(
        payload,
        submitted_by=current_user.username,
        idempotency_key=idempotency_key,
    )
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="batch.submitted.v1",
        aggregate_type="batch_job",
        aggregate_id=item.batch_job_id,
        payload={
            "deployment_release_id": item.deployment_release_id,
            "deployment_target": item.deployment_target,
        },
    )
    return item


@router.get("/batch-jobs", response_model=ListResponse[BatchJobSummary])
def list_batch_jobs(
    _current_user: BatchReader,
    settings=Depends(get_settings),
) -> ListResponse[BatchJobSummary]:
    return ListResponse(items=_service(settings).list_jobs(), meta=build_meta())


@router.get("/batch-jobs/{batch_job_id}", response_model=BatchJobDetail)
def get_batch_job(
    batch_job_id: str,
    _current_user: BatchReader,
    settings=Depends(get_settings),
) -> BatchJobDetail:
    return _service(settings).get_job_detail(batch_job_id)


@router.get("/batch-jobs/{batch_job_id}/results", response_model=BatchResultResponse)
def get_batch_job_results(
    batch_job_id: str,
    _current_user: BatchReader,
    settings=Depends(get_settings),
) -> BatchResultResponse:
    return _service(settings).get_job_results(batch_job_id)


@router.get("/batch-jobs/{batch_job_id}/download")
def download_batch_job_results(
    batch_job_id: str,
    _current_user: BatchReader,
    settings=Depends(get_settings),
) -> FileResponse:
    detail = _service(settings).get_job_detail(batch_job_id)
    bundle_path = detail.artifacts.bundle_uri or detail.artifacts.download_path
    if not bundle_path:
        raise PlatformError(
            code="batch_results_unavailable",
            message="The batch job does not have a downloadable bundle yet.",
            detail=batch_job_id,
            status_code=404,
        )
    return FileResponse(bundle_path, filename=f"{batch_job_id}.zip", media_type="application/zip")
