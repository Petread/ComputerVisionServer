from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response

from cv_contracts import (
    HealthResponse,
    PublicBatchJobCreateRequest,
    PublicBatchJobDetail,
    PublicBatchJobMutationResponse,
    PublicBatchJobSummary,
    PublicBatchResultsResponse,
    PublicModelDetailResponse,
    PublicModelListResponse,
)
from cv_status import HealthState, TargetPath

from inference_gateway.api.batch import get_service
from inference_gateway.api.public_common import build_public_health_response
from inference_gateway.dependencies import RequestIdentity, require_public_batch_machine_access
from inference_gateway.runtime.public_service import PublicBatchGatewayService

router = APIRouter(tags=["public-batch-inference"])


@router.get("/health", response_model=HealthResponse)
async def public_health(
    request: Request,
    response: Response,
    service: PublicBatchGatewayService = Depends(get_service),
) -> HealthResponse:
    models = await asyncio.to_thread(service.list_public_models)
    payload = await build_public_health_response(
        request,
        target_path=TargetPath.BATCH,
        ready=False,
        model_count=len(models.items),
    )
    if payload.state == HealthState.DOWN:
        response.status_code = 503
    return payload


@router.get("/ready", response_model=HealthResponse)
async def public_ready(
    request: Request,
    response: Response,
    service: PublicBatchGatewayService = Depends(get_service),
) -> HealthResponse:
    models = await asyncio.to_thread(service.list_public_models)
    payload = await build_public_health_response(
        request,
        target_path=TargetPath.BATCH,
        ready=True,
        model_count=len(models.items),
    )
    if payload.state != HealthState.OK:
        response.status_code = 503
    return payload


@router.get("/models", response_model=PublicModelListResponse)
async def list_models(
    _identity: RequestIdentity = Depends(require_public_batch_machine_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> PublicModelListResponse:
    return await asyncio.to_thread(service.list_public_models)


@router.get("/models/{model_id}", response_model=PublicModelDetailResponse)
async def model_detail(
    model_id: str,
    _identity: RequestIdentity = Depends(require_public_batch_machine_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> PublicModelDetailResponse:
    return await asyncio.to_thread(service.get_public_model, model_id)


@router.post("/batch/jobs", response_model=PublicBatchJobSummary, status_code=202)
async def submit_batch_job(
    payload: PublicBatchJobCreateRequest,
    _identity: RequestIdentity = Depends(require_public_batch_machine_access),
    service: PublicBatchGatewayService = Depends(get_service),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PublicBatchJobSummary:
    return service.submit_public_batch_job(payload, idempotency_key=idempotency_key)


@router.get("/batch/jobs/{job_id}", response_model=PublicBatchJobDetail)
async def batch_job_detail(
    job_id: str,
    _identity: RequestIdentity = Depends(require_public_batch_machine_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> PublicBatchJobDetail:
    detail = service.get_public_batch_job(job_id)
    return PublicBatchJobDetail.model_validate(detail)


@router.get("/batch/jobs/{job_id}/results", response_model=PublicBatchResultsResponse)
async def batch_job_results(
    job_id: str,
    _identity: RequestIdentity = Depends(require_public_batch_machine_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> PublicBatchResultsResponse:
    return service.get_public_batch_results(job_id)


@router.delete("/batch/jobs/{job_id}", response_model=PublicBatchJobMutationResponse)
async def delete_batch_job(
    job_id: str,
    _identity: RequestIdentity = Depends(require_public_batch_machine_access),
    service: PublicBatchGatewayService = Depends(get_service),
) -> PublicBatchJobMutationResponse:
    return service.delete_public_batch_job(job_id)
