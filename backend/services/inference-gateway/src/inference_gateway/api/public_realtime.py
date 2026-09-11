from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile

from cv_contracts import (
    HealthResponse,
    PublicAnomalyResponse,
    PublicClassificationResponse,
    PublicDetectionResponse,
    PublicModelDetailResponse,
    PublicModelListResponse,
    PublicSegmentationResponse,
)
from cv_status import HealthState, TargetPath

from inference_gateway.api.public_common import (
    build_public_health_response,
    reject_unexpected_form_fields,
)
from inference_gateway.api.realtime import get_service
from inference_gateway.dependencies import RequestIdentity, require_public_realtime_machine_access
from inference_gateway.runtime.public_service import PublicRealtimeGatewayService

router = APIRouter(tags=["public-realtime-inference"])


@router.get("/health", response_model=HealthResponse)
async def public_health(
    request: Request,
    response: Response,
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> HealthResponse:
    models = await asyncio.to_thread(service.list_public_models)
    payload = await build_public_health_response(
        request,
        target_path=TargetPath.REALTIME,
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
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> HealthResponse:
    models = await asyncio.to_thread(service.list_public_models)
    payload = await build_public_health_response(
        request,
        target_path=TargetPath.REALTIME,
        ready=True,
        model_count=len(models.items),
    )
    if payload.state != HealthState.OK:
        response.status_code = 503
    return payload


@router.get("/models", response_model=PublicModelListResponse)
async def list_models(
    _identity: RequestIdentity = Depends(require_public_realtime_machine_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> PublicModelListResponse:
    return await asyncio.to_thread(service.list_public_models)


@router.get("/models/{model_id}", response_model=PublicModelDetailResponse)
async def model_detail(
    model_id: str,
    _identity: RequestIdentity = Depends(require_public_realtime_machine_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> PublicModelDetailResponse:
    return await asyncio.to_thread(service.get_public_model, model_id)


@router.post("/infer/classify", response_model=PublicClassificationResponse)
async def classify(
    request: Request,
    model_id: Annotated[str, Form(min_length=3, max_length=120)],
    image: Annotated[UploadFile, File()],
    top_k: Annotated[int | None, Form(ge=1, le=20)] = None,
    timeout_ms: Annotated[int | None, Form(ge=50, le=30_000)] = None,
    _identity: RequestIdentity = Depends(require_public_realtime_machine_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> PublicClassificationResponse:
    await reject_unexpected_form_fields(
        request,
        allowed_fields={"model_id", "image", "top_k", "timeout_ms"},
    )
    return await service.classify_public(
        model_id=model_id,
        file_name=image.filename,
        content_type=image.content_type,
        image_bytes=await image.read(),
        timeout_ms=timeout_ms,
        top_k=top_k,
        parameters={"top_k": top_k},
    )


@router.post("/infer/detect", response_model=PublicDetectionResponse)
async def detect(
    request: Request,
    model_id: Annotated[str, Form(min_length=3, max_length=120)],
    image: Annotated[UploadFile, File()],
    threshold: Annotated[float | None, Form(ge=0, le=1)] = None,
    iou_threshold: Annotated[float | None, Form(ge=0, le=1)] = None,
    max_detections: Annotated[int | None, Form(ge=1, le=100)] = None,
    timeout_ms: Annotated[int | None, Form(ge=50, le=30_000)] = None,
    _identity: RequestIdentity = Depends(require_public_realtime_machine_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> PublicDetectionResponse:
    await reject_unexpected_form_fields(
        request,
        allowed_fields={
            "model_id",
            "image",
            "threshold",
            "iou_threshold",
            "max_detections",
            "timeout_ms",
        },
    )
    return await service.detect_public(
        model_id=model_id,
        file_name=image.filename,
        content_type=image.content_type,
        image_bytes=await image.read(),
        timeout_ms=timeout_ms,
        threshold=threshold,
        max_detections=max_detections,
        parameters={
            "threshold": threshold,
            "iou_threshold": iou_threshold,
            "max_detections": max_detections,
        },
    )


@router.post("/infer/segment", response_model=PublicSegmentationResponse)
async def segment(
    request: Request,
    model_id: Annotated[str, Form(min_length=3, max_length=120)],
    image: Annotated[UploadFile, File()],
    threshold: Annotated[float | None, Form(ge=0, le=1)] = None,
    max_segments: Annotated[int | None, Form(ge=1, le=100)] = None,
    timeout_ms: Annotated[int | None, Form(ge=50, le=30_000)] = None,
    _identity: RequestIdentity = Depends(require_public_realtime_machine_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> PublicSegmentationResponse:
    await reject_unexpected_form_fields(
        request,
        allowed_fields={
            "model_id",
            "image",
            "threshold",
            "max_segments",
            "timeout_ms",
        },
    )
    return await service.segment_public(
        model_id=model_id,
        file_name=image.filename,
        content_type=image.content_type,
        image_bytes=await image.read(),
        timeout_ms=timeout_ms,
        threshold=threshold,
        max_segments=max_segments,
        parameters={
            "threshold": threshold,
            "max_segments": max_segments,
        },
    )


@router.post("/infer/anomaly", response_model=PublicAnomalyResponse)
async def anomaly(
    request: Request,
    model_id: Annotated[str, Form(min_length=3, max_length=120)],
    image: Annotated[UploadFile, File()],
    threshold: Annotated[float | None, Form(ge=0, le=1)] = None,
    return_mask: Annotated[bool | None, Form()] = None,
    timeout_ms: Annotated[int | None, Form(ge=50, le=30_000)] = None,
    _identity: RequestIdentity = Depends(require_public_realtime_machine_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> PublicAnomalyResponse:
    await reject_unexpected_form_fields(
        request,
        allowed_fields={
            "model_id",
            "image",
            "threshold",
            "return_mask",
            "timeout_ms",
        },
    )
    return await service.anomaly_public(
        model_id=model_id,
        file_name=image.filename,
        content_type=image.content_type,
        image_bytes=await image.read(),
        timeout_ms=timeout_ms,
        threshold=threshold,
        return_mask=return_mask,
        parameters={
            "threshold": threshold,
            "return_mask": return_mask,
        },
    )
