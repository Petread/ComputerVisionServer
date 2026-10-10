from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from cv_contracts import (
    AnomalyInferenceResponse,
    ClassificationInferenceResponse,
    DetectionInferenceResponse,
    ModelCapabilitiesResponse,
    ModelStatusResponse,
    RealtimeAnomalyRequest,
    RealtimeClassifyRequest,
    RealtimeDetectRequest,
    RealtimeInferenceRequest,
    RealtimeInferenceResponse,
    RealtimeSegmentRequest,
    RealtimeTemporalSequenceRequest,
    SegmentationInferenceResponse,
    TemporalSequenceInferenceResponse,
)
from cv_status import TargetPath

from inference_gateway.dependencies import RequestIdentity, get_settings, require_realtime_access
from inference_gateway.dependencies import require_runtime_inference
from inference_gateway.runtime.public_service import PublicRealtimeGatewayService

router = APIRouter(prefix="/realtime", tags=["realtime-inference"],
                   dependencies=[Depends(require_runtime_inference)])


def get_service(request: Request, settings=Depends(get_settings)) -> PublicRealtimeGatewayService:
    service = getattr(request.app.state, "realtime_gateway_service", None)
    if (
        service is None
        or not isinstance(service, PublicRealtimeGatewayService)
        or getattr(service, "settings", None) is not settings
    ):
        service = PublicRealtimeGatewayService(
            settings,
            service_name="inference-gateway-rt",
            target_path=TargetPath.REALTIME,
        )
        request.app.state.realtime_gateway_service = service
    return service


@router.post("/classify", response_model=ClassificationInferenceResponse)
async def classify_realtime(
    payload: RealtimeClassifyRequest,
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> ClassificationInferenceResponse:
    return await service.classify(payload)


@router.post("/classify-bytes", response_model=ClassificationInferenceResponse)
async def classify_realtime_bytes(
    request: Request,
    deployment_target: str = Query(..., min_length=3, max_length=120),
    deployment_release_id: str | None = Query(default=None, min_length=3, max_length=64),
    timeout_ms: int | None = Query(default=None, ge=50, le=30_000),
    top_k: int = Query(default=1, ge=1, le=20),
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> ClassificationInferenceResponse:
    return await service.classify_image_bytes(
        deployment_target=deployment_target,
        deployment_release_id=deployment_release_id,
        timeout_ms=timeout_ms,
        top_k=top_k,
        image_bytes=await request.body(),
    )


@router.post("/detect", response_model=DetectionInferenceResponse)
async def detect_realtime(
    payload: RealtimeDetectRequest,
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> DetectionInferenceResponse:
    return await service.detect(payload)


@router.post("/detect-bytes", response_model=DetectionInferenceResponse)
async def detect_realtime_bytes(
    request: Request,
    deployment_target: str = Query(..., min_length=3, max_length=120),
    deployment_release_id: str | None = Query(default=None, min_length=3, max_length=64),
    timeout_ms: int | None = Query(default=None, ge=50, le=30_000),
    max_detections: int = Query(default=100, ge=1, le=1_000),
    score_threshold: float = Query(default=0.35, ge=0, le=1),
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> DetectionInferenceResponse:
    return await service.detect_image_bytes(
        deployment_target=deployment_target,
        deployment_release_id=deployment_release_id,
        timeout_ms=timeout_ms,
        max_detections=max_detections,
        score_threshold=score_threshold,
        image_bytes=await request.body(),
    )


@router.post("/segment", response_model=SegmentationInferenceResponse)
async def segment_realtime(
    payload: RealtimeSegmentRequest,
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> SegmentationInferenceResponse:
    return await service.segment(payload)


@router.post("/segment-bytes", response_model=SegmentationInferenceResponse)
async def segment_realtime_bytes(
    request: Request,
    deployment_target: str = Query(..., min_length=3, max_length=120),
    deployment_release_id: str | None = Query(default=None, min_length=3, max_length=64),
    timeout_ms: int | None = Query(default=None, ge=50, le=30_000),
    max_segments: int = Query(default=50, ge=1, le=1_000),
    score_threshold: float = Query(default=0.35, ge=0, le=1),
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> SegmentationInferenceResponse:
    return await service.segment_image_bytes(
        deployment_target=deployment_target,
        deployment_release_id=deployment_release_id,
        timeout_ms=timeout_ms,
        max_segments=max_segments,
        score_threshold=score_threshold,
        image_bytes=await request.body(),
    )


@router.post("/anomaly", response_model=AnomalyInferenceResponse)
async def anomaly_realtime(
    payload: RealtimeAnomalyRequest,
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> AnomalyInferenceResponse:
    return await service.anomaly(payload)


@router.post("/temporal-sequence", response_model=TemporalSequenceInferenceResponse)
async def infer_temporal_sequence(
    payload: RealtimeTemporalSequenceRequest,
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> TemporalSequenceInferenceResponse:
    return await service.temporal_sequence(payload)


@router.post("/anomaly-bytes", response_model=AnomalyInferenceResponse)
async def anomaly_realtime_bytes(
    request: Request,
    deployment_target: str = Query(..., min_length=3, max_length=120),
    deployment_release_id: str | None = Query(default=None, min_length=3, max_length=64),
    timeout_ms: int | None = Query(default=None, ge=50, le=30_000),
    threshold: float | None = Query(default=None, ge=0, le=1),
    return_mask: bool = Query(default=False),
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> AnomalyInferenceResponse:
    return await service.anomaly_image_bytes(
        deployment_target=deployment_target,
        deployment_release_id=deployment_release_id,
        timeout_ms=timeout_ms,
        threshold=threshold,
        return_mask=return_mask,
        image_bytes=await request.body(),
    )


@router.post("/predict", response_model=RealtimeInferenceResponse)
async def predict_realtime(
    payload: RealtimeInferenceRequest,
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> RealtimeInferenceResponse:
    return await service.predict(payload)


@router.get("/models/capabilities", response_model=ModelCapabilitiesResponse)
async def realtime_capabilities(
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> ModelCapabilitiesResponse:
    return await service.list_capabilities()


@router.get("/models/status", response_model=ModelStatusResponse)
async def realtime_status(
    _identity: RequestIdentity = Depends(require_realtime_access),
    service: PublicRealtimeGatewayService = Depends(get_service),
) -> ModelStatusResponse:
    return await service.list_status()
