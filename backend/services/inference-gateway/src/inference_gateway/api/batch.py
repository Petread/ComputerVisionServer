from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse

from cv_contracts import (
    BatchJobCreateRequest,
    BatchJobDetail,
    BatchJobSummary,
    BatchResultResponse,
    ListResponse,
    ModelCapabilitiesResponse,
    ModelStatusResponse,
    ResponseMeta,
)
from cv_status import TargetPath
from cv_utils import PlatformError, get_correlation_id, get_request_id, utc_now_iso

from inference_gateway.dependencies import (
    RequestIdentity,
    get_settings,
    require_batch_access,
    require_internal_batch_access,
)
from inference_gateway.runtime.public_service import PublicBatchGatewayService

router = APIRouter(prefix="/batch", tags=["batch-inference"])


def get_service(request: Request, settings=Depends(get_settings)) -> PublicBatchGatewayService:
    service = getattr(request.app.state, "batch_gateway_service", None)
    if (
        service is None
        or not isinstance(service, PublicBatchGatewayService)
        or getattr(service, "settings", None) is not settings
    ):
        service = PublicBatchGatewayService(
            settings,
            service_name="inference-gateway-batch",
            target_path=TargetPath.BATCH,
        )
        request.app.state.batch_gateway_service = service
    return service


def build_meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )


@router.post("/internal/jobs", response_model=BatchJobSummary, status_code=202)
def submit_batch_job(
    payload: BatchJobCreateRequest,
    _identity: RequestIdentity = Depends(require_batch_access),
    service: PublicBatchGatewayService = Depends(get_service),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> BatchJobSummary:
    return service.submit_batch_job(payload, idempotency_key=idempotency_key)


@router.get("/internal/jobs", response_model=ListResponse[BatchJobSummary])
def list_jobs(
    _identity: RequestIdentity = Depends(require_batch_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> ListResponse[BatchJobSummary]:
    return ListResponse(items=service.list_jobs(), meta=build_meta())


@router.get("/internal/jobs/{batch_job_id}", response_model=BatchJobDetail)
def batch_job_status(
    batch_job_id: str,
    _identity: RequestIdentity = Depends(require_batch_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> BatchJobDetail:
    return service.get_job_detail(batch_job_id)


@router.get("/internal/jobs/{batch_job_id}/results", response_model=BatchResultResponse)
def batch_job_results(
    batch_job_id: str,
    _identity: RequestIdentity = Depends(require_batch_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> BatchResultResponse:
    return service.get_job_results(batch_job_id)


@router.get("/internal/jobs/{batch_job_id}/download")
def batch_job_download(
    batch_job_id: str,
    _identity: RequestIdentity = Depends(require_batch_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> FileResponse:
    detail = service.get_job_detail(batch_job_id)
    bundle_path = detail.artifacts.bundle_uri or detail.artifacts.download_path
    if not bundle_path:
        raise PlatformError(
            code="batch_results_unavailable",
            message="The batch job does not have a downloadable bundle yet.",
            detail=batch_job_id,
            status_code=404,
        )
    return FileResponse(bundle_path, filename=f"{batch_job_id}.zip", media_type="application/zip")


@router.post("/internal/chunks/{batch_job_id}/{chunk_no}:execute")
async def execute_chunk(
    batch_job_id: str,
    chunk_no: int,
    _identity: RequestIdentity = Depends(require_internal_batch_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> dict[str, object]:
    return await service.run_batch_chunk(batch_job_id, chunk_no)


@router.get("/models/capabilities", response_model=ModelCapabilitiesResponse)
async def batch_capabilities(
    _identity: RequestIdentity = Depends(require_batch_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> ModelCapabilitiesResponse:
    return await service.list_capabilities()


@router.get("/models/status", response_model=ModelStatusResponse)
async def batch_status(
    _identity: RequestIdentity = Depends(require_batch_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> ModelStatusResponse:
    return await service.list_status()
