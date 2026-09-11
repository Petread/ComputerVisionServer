from __future__ import annotations

import asyncio
import base64
import hashlib
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from cv_contracts import (
    BatchInputItem,
    BatchJobCreateRequest,
    PublicBatchChunkSummary,
    PublicAnomalyHeatmap,
    PublicAnomalyMask,
    PublicAnomalyResponse,
    PublicBatchJobCreateRequest,
    PublicBatchJobDetail,
    PublicBatchJobMutationResponse,
    PublicBatchJobSummary,
    PublicBatchProgress,
    PublicBatchResultItem,
    PublicBatchResultPackage,
    PublicBatchResultsResponse,
    PublicBoundingBox,
    PublicClassificationPrediction,
    PublicClassificationResponse,
    PublicCoordinateSystem,
    PublicDetection,
    PublicDetectionResponse,
    PublicImageMetadata,
    PublicInferenceTiming,
    PublicModelAvailability,
    PublicModelDetail,
    PublicModelDetailResponse,
    PublicModelListResponse,
    PublicBatchAvailability,
    PublicRealtimeAvailability,
    PublicModelSummary,
    PublicOrientedBoundingBox,
    PublicOrientedPoint,
    PublicParameterSpec,
    PublicResponseSchema,
    PublicSegmentation,
    PublicSegmentationPoint,
    PublicSegmentationResponse,
    RealtimeAnomalyRequest,
)
from cv_db import session_scope, update_batch_job_state
from cv_status import BatchJobStatus, TargetPath, TaskType
from cv_utils import PlatformError, get_correlation_id, get_request_id, inspect_image_bytes, utc_now_iso

from inference_gateway.runtime.resolver import PublicModelContext, ReleaseContext
from inference_gateway.runtime.service import (
    BatchGatewayService,
    ConcurrencyGate,
    RealtimeGatewayService,
    _elapsed_ms,
    _normalize_image_bytes,
    _timeout_seconds,
)

PUBLIC_REALTIME_TIMEOUT_MAX_MS = 30_000


class PublicGatewayMixin:
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._public_model_gates: dict[str, ConcurrencyGate] = {}

    def list_public_models(self) -> PublicModelListResponse:
        items = [self._to_public_model_summary(item) for item in self.resolver.list_public_models()]
        return PublicModelListResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            items=items,
        )

    def get_public_model(self, model_id: str) -> PublicModelDetailResponse:
        context = self._public_model_context(model_id)
        return PublicModelDetailResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            item=self._to_public_model_detail(context),
        )

    def _public_model_context(self, model_id: str) -> PublicModelContext:
        context = self.resolver.get_public_model_context(model_id=model_id)
        if context is None:
            raise PlatformError(
                code="model_not_found",
                message="The requested public model_id does not exist.",
                detail=model_id,
                status_code=404,
                retryable=False,
            )
        return context

    def _public_release(self, *, model_id: str, target_path: TargetPath) -> ReleaseContext:
        release = self.resolver.resolve_public_model_release(model_id=model_id, target_path=target_path)
        if release is None:
            raise PlatformError(
                code="model_unavailable",
                message="The requested public model is not currently deployed on this inference path.",
                detail=f"{model_id}:{target_path.value}",
                status_code=503,
                retryable=True,
            )
        return release

    def _supported_parameters(self, context: PublicModelContext) -> list[PublicParameterSpec]:
        raw_items = (context.manifest.get("public_inference") or {}).get("supported_parameters")
        if isinstance(raw_items, list) and raw_items:
            return [PublicParameterSpec.model_validate(item) for item in raw_items]
        if context.task_type == TaskType.CLASSIFICATION:
            return [
                PublicParameterSpec(
                    name="top_k",
                    parameter_type="integer",
                    required=False,
                    default=3,
                    minimum=1,
                    maximum=20,
                    description="Number of top labels to return.",
                )
            ]
        if context.task_type == TaskType.SEGMENTATION:
            return [
                PublicParameterSpec(
                    name="threshold",
                    parameter_type="number",
                    required=False,
                    default=0.35,
                    minimum=0.0,
                    maximum=1.0,
                    description="Confidence threshold applied after inference.",
                ),
                PublicParameterSpec(
                    name="max_segments",
                    parameter_type="integer",
                    required=False,
                    default=10,
                    minimum=1,
                    maximum=100,
                    description="Maximum number of polygon segments returned by the gateway.",
                ),
            ]
        if context.task_type == TaskType.ANOMALY:
            return [
                PublicParameterSpec(
                    name="threshold",
                    parameter_type="number",
                    required=False,
                    default=None,
                    minimum=0.0,
                    maximum=1.0,
                    description="Optional anomaly decision threshold. When omitted, the deployed model threshold is used.",
                ),
                PublicParameterSpec(
                    name="return_mask",
                    parameter_type="boolean",
                    required=False,
                    default=False,
                    description="Request a thresholded anomaly mask only when the deployed model declares mask support.",
                ),
            ]
        return [
            PublicParameterSpec(
                name="threshold",
                parameter_type="number",
                required=False,
                default=0.5,
                minimum=0.0,
                maximum=1.0,
                description="Confidence threshold applied after inference.",
            ),
            PublicParameterSpec(
                name="max_detections",
                parameter_type="integer",
                required=False,
                default=10,
                minimum=1,
                maximum=100,
                description="Maximum number of detections returned by the gateway.",
            ),
        ]

    def _validate_supported_parameters(
        self,
        *,
        context: PublicModelContext,
        provided_parameters: dict[str, Any],
    ) -> None:
        allowed = {item.name for item in self._supported_parameters(context)}
        for name, value in provided_parameters.items():
            if value is None:
                continue
            if name not in allowed:
                raise PlatformError(
                    code="unsupported_parameter",
                    message="The selected public model does not support one or more supplied parameters.",
                    detail=name,
                    status_code=422,
                    retryable=False,
                    validation_errors=[
                        {
                            "field": name,
                            "location": ["body", name],
                            "message": f"Parameter '{name}' is not supported for model_id '{context.model_id}'.",
                            "error_type": "unsupported_parameter",
                        }
                    ],
                )

    def _ensure_task(self, *, release: ReleaseContext, expected: TaskType | set[TaskType]) -> None:
        expected_values = {expected} if isinstance(expected, TaskType) else set(expected)
        if release.task_type not in expected_values:
            expected_detail = ",".join(sorted(item.value for item in expected_values))
            raise PlatformError(
                code="unsupported_capability",
                message="The selected public model does not support this inference capability.",
                detail=f"expected={expected_detail}, actual={release.task_type.value}",
                status_code=409,
                retryable=False,
            )

    def _prepare_image(
        self,
        *,
        file_name: str | None,
        content_type: str | None,
        image_bytes: bytes,
    ) -> tuple[PublicImageMetadata, bytes, float, float]:
        validation_started = time.perf_counter()
        if not image_bytes:
            raise PlatformError(
                code="image_decode_failure",
                message="The uploaded image payload is empty.",
                status_code=422,
                retryable=False,
            )
        if len(image_bytes) > self.settings.request_limits.max_image_bytes:
            raise PlatformError(
                code="payload_too_large",
                message="The image payload exceeds the maximum supported size for realtime inference.",
                detail=str(self.settings.request_limits.max_image_bytes),
                status_code=413,
                retryable=False,
            )
        try:
            inspection = inspect_image_bytes(image_bytes)
        except PlatformError as exc:
            raise PlatformError(
                code="image_decode_failure",
                message="The uploaded image could not be decoded or is not supported.",
                detail=exc.detail or exc.message,
                status_code=422,
                retryable=False,
            ) from exc
        validation_ms = _elapsed_ms(validation_started)

        preprocessing_started = time.perf_counter()
        normalized_bytes = _normalize_image_bytes(image_bytes)
        preprocessing_ms = _elapsed_ms(preprocessing_started)
        return (
            PublicImageMetadata(
                file_name=file_name,
                content_type=inspection.mime_type if inspection.mime_type else (content_type or "application/octet-stream"),
                size_bytes=len(image_bytes),
                width=inspection.width,
                height=inspection.height,
                sha256=f"sha256:{hashlib.sha256(image_bytes).hexdigest()}",
            ),
            normalized_bytes,
            validation_ms,
            preprocessing_ms,
        )

    def _gate_key(self, *, model_id: str, target_path: TargetPath) -> str:
        return f"{target_path.value}:{model_id}"

    def _model_gate(self, *, release: ReleaseContext) -> ConcurrencyGate:
        public_config = release.manifest.get("public_inference") or {}
        limit = int(public_config.get("concurrency_limit") or self.gate.limit)
        key = self._gate_key(model_id=release.model_family_slug, target_path=release.target_path)
        gate = self._public_model_gates.get(key)
        if gate is None or gate.limit != limit:
            gate = ConcurrencyGate(limit)
            self._public_model_gates[key] = gate
        return gate

    @asynccontextmanager
    async def _public_admission(self, *, release: ReleaseContext, operation: str, wait: bool):
        gate = self._model_gate(release=release)
        admitted = await gate.try_acquire(wait=wait)
        if not admitted:
            self.metrics.record_overload(operation=operation)
            raise PlatformError(
                code="overload",
                message="The selected public model is overloaded on this gateway path.",
                detail=release.model_family_slug,
                status_code=429,
                retryable=True,
            )
        try:
            async with self._admission(
                release=release,
                operation=f"{operation}_public",
                wait=wait,
            ):
                yield gate
        finally:
            await gate.release()

    def _to_public_model_summary(self, context: PublicModelContext) -> PublicModelSummary:
        return PublicModelSummary(
            model_id=context.model_id,
            display_name=context.display_name,
            description=context.description,
            model_family=context.model_family_slug,
            task_type=context.task_type,
            architecture=context.architecture,
            model_version=self._current_model_version(context),
            availability=self._availability(context),
        )

    def _to_public_model_detail(self, context: PublicModelContext) -> PublicModelDetail:
        if context.task_type == TaskType.CLASSIFICATION:
            task_schema = "inference.public.classification-response.v1"
        elif context.task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
            task_schema = "inference.public.detection-response.v1"
        elif context.task_type == TaskType.SEGMENTATION:
            task_schema = "inference.public.segmentation-response.v1"
        else:
            task_schema = "inference.public.anomaly-response.v1"
        return PublicModelDetail(
            **self._to_public_model_summary(context).model_dump(),
            input_constraints=self._input_constraints(),
            response_schema=PublicResponseSchema(schema_id=task_schema),
            supported_parameters=self._supported_parameters(context),
            class_labels=context.class_labels,
        )

    def _current_model_version(self, context: PublicModelContext) -> int | None:
        for target_path in (self.target_path, TargetPath.REALTIME, TargetPath.BATCH):
            release = context.releases.get(target_path)
            if release is not None:
                return release.model_version_no
        return None

    def _availability(self, context: PublicModelContext) -> PublicModelAvailability:
        return PublicModelAvailability(
            realtime=PublicRealtimeAvailability(
                available=self._public_release_available(context, TargetPath.REALTIME),
                timeout_default_ms=self.settings.inference_limits.realtime_timeout_ms,
                timeout_max_ms=PUBLIC_REALTIME_TIMEOUT_MAX_MS,
            ),
            batch=PublicBatchAvailability(
                available=self._public_release_available(context, TargetPath.BATCH),
                timeout_default_seconds=self.settings.inference_limits.batch_timeout_seconds,
                timeout_max_seconds=self.settings.inference_limits.batch_timeout_seconds,
            ),
        )

    def _public_release_available(
        self,
        context: PublicModelContext,
        target_path: TargetPath,
    ) -> bool:
        release = context.releases.get(target_path)
        if release is None:
            return False
        if target_path != self.target_path:
            return True
        ready_versions = self.backend.ready_model_versions()
        if ready_versions is None:
            return True
        return (release.triton_model_name, release.triton_model_version) in ready_versions

    def _input_constraints(self) -> Any:
        from cv_contracts import PublicInputConstraints

        return PublicInputConstraints(
            supported_content_types=list(self.settings.uploads.allowed_mime_types),
            max_image_bytes=self.settings.request_limits.max_image_bytes,
            max_request_body_bytes=self.settings.request_limits.inference_body_limit_bytes,
        )

    def _to_public_timing(self, *, timing, completed_at: str) -> PublicInferenceTiming:
        return PublicInferenceTiming(
            received_at=timing.received_at,
            completed_at=completed_at,
            validation_ms=timing.validation_ms,
            routing_ms=timing.routing_ms,
            preprocessing_ms=timing.preprocessing_ms,
            inference_ms=timing.inference_ms,
            postprocessing_ms=timing.postprocessing_ms,
            total_ms=timing.total_ms,
        )

    def _batch_progress(self, progress) -> PublicBatchProgress:
        return PublicBatchProgress(
            total_items=progress.total_items,
            completed_items=progress.completed_items,
            failed_items=progress.failed_items,
            total_chunks=progress.total_chunks,
            queued_chunks=progress.queued_chunks,
            running_chunks=progress.running_chunks,
            completed_chunks=progress.completed_chunks,
            failed_chunks=progress.failed_chunks,
            progress_percent=progress.progress_percent,
        )

    def _result_package(self, artifacts) -> PublicBatchResultPackage:
        return PublicBatchResultPackage(
            manifest_uri=artifacts.manifest_uri,
            jsonl_uri=artifacts.jsonl_uri,
            bundle_uri=artifacts.bundle_uri or artifacts.download_path,
            recoverable=artifacts.recoverable,
            incomplete_chunk_nos=list(artifacts.incomplete_chunk_nos),
        )


class PublicRealtimeGatewayService(PublicGatewayMixin, RealtimeGatewayService):
    async def classify_public(
        self,
        *,
        model_id: str,
        file_name: str | None,
        content_type: str | None,
        image_bytes: bytes,
        timeout_ms: int | None,
        top_k: int | None,
        parameters: dict[str, Any] | None = None,
    ) -> PublicClassificationResponse:
        context = self._public_model_context(model_id)
        self._validate_supported_parameters(context=context, provided_parameters=parameters or {})
        received_at = utc_now_iso()
        routing_started = time.perf_counter()
        release = self._public_release(model_id=model_id, target_path=TargetPath.REALTIME)
        self._ensure_task(release=release, expected=TaskType.CLASSIFICATION)
        routing_ms = _elapsed_ms(routing_started)
        image_meta, normalized_bytes, validation_ms, preprocessing_ms = self._prepare_image(
            file_name=file_name,
            content_type=content_type,
            image_bytes=image_bytes,
        )
        started = time.perf_counter()
        async with self._public_admission(release=release, operation="classify", wait=False):
            inference_started = time.perf_counter()
            timeout_seconds = _timeout_seconds(
                timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            )
            try:
                labels = await asyncio.wait_for(
                    self.backend.classify(
                        route=release,
                        image_bytes=normalized_bytes,
                        top_k=top_k or 3,
                        timeout_seconds=timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError as exc:
                raise PlatformError(
                    code="timeout",
                    message="The realtime inference request exceeded its timeout budget.",
                    status_code=504,
                    retryable=True,
                ) from exc
            inference_ms = _elapsed_ms(inference_started)
        completed_at = utc_now_iso()
        timing = PublicInferenceTiming(
            received_at=received_at,
            completed_at=completed_at,
            validation_ms=validation_ms,
            routing_ms=routing_ms,
            preprocessing_ms=preprocessing_ms,
            inference_ms=inference_ms,
            postprocessing_ms=0.0,
            total_ms=round(validation_ms + routing_ms + preprocessing_ms + inference_ms, 3),
        )
        self.metrics.record_success(
            operation="classify_public",
            deployment_release_id=release.deployment_release_id,
            total_ms=_elapsed_ms(started),
        )
        return PublicClassificationResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            model_id=model_id,
            model_family=release.model_family_slug,
            model_version=release.model_version_no,
            task_type=TaskType.CLASSIFICATION,
            architecture=release.architecture,
            input=image_meta,
            timing=timing,
            predictions=[
                PublicClassificationPrediction(label=item.label, score=item.score) for item in labels
            ],
        )

    async def detect_public(
        self,
        *,
        model_id: str,
        file_name: str | None,
        content_type: str | None,
        image_bytes: bytes,
        timeout_ms: int | None,
        threshold: float | None,
        max_detections: int | None,
        parameters: dict[str, Any] | None = None,
    ) -> PublicDetectionResponse:
        context = self._public_model_context(model_id)
        self._validate_supported_parameters(context=context, provided_parameters=parameters or {})
        received_at = utc_now_iso()
        routing_started = time.perf_counter()
        release = self._public_release(model_id=model_id, target_path=TargetPath.REALTIME)
        self._ensure_task(
            release=release,
            expected={TaskType.DETECTION, TaskType.ORIENTED_DETECTION},
        )
        routing_ms = _elapsed_ms(routing_started)
        image_meta, normalized_bytes, validation_ms, preprocessing_ms = self._prepare_image(
            file_name=file_name,
            content_type=content_type,
            image_bytes=image_bytes,
        )
        started = time.perf_counter()
        async with self._public_admission(release=release, operation="detect", wait=False):
            inference_started = time.perf_counter()
            timeout_seconds = _timeout_seconds(
                timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            )
            try:
                detections = await asyncio.wait_for(
                    self.backend.detect(
                        route=release,
                        image_bytes=normalized_bytes,
                        max_detections=max_detections or 10,
                        score_threshold=threshold or release.confidence_threshold_default,
                        timeout_seconds=timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError as exc:
                raise PlatformError(
                    code="timeout",
                    message="The realtime inference request exceeded its timeout budget.",
                    status_code=504,
                    retryable=True,
                ) from exc
            inference_ms = _elapsed_ms(inference_started)
        completed_at = utc_now_iso()
        timing = PublicInferenceTiming(
            received_at=received_at,
            completed_at=completed_at,
            validation_ms=validation_ms,
            routing_ms=routing_ms,
            preprocessing_ms=preprocessing_ms,
            inference_ms=inference_ms,
            postprocessing_ms=0.0,
            total_ms=round(validation_ms + routing_ms + preprocessing_ms + inference_ms, 3),
        )
        self.metrics.record_success(
            operation="detect_public",
            deployment_release_id=release.deployment_release_id,
            total_ms=_elapsed_ms(started),
        )
        return PublicDetectionResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            model_id=model_id,
            model_family=release.model_family_slug,
            model_version=release.model_version_no,
            task_type=release.task_type,
            architecture=release.architecture,
            input=image_meta,
            timing=timing,
            coordinate_system=PublicCoordinateSystem(),
            detections=[_public_detection_from_prediction(item, release=release) for item in detections],
        )

    async def segment_public(
        self,
        *,
        model_id: str,
        file_name: str | None,
        content_type: str | None,
        image_bytes: bytes,
        timeout_ms: int | None,
        threshold: float | None,
        max_segments: int | None,
        parameters: dict[str, Any] | None = None,
    ) -> PublicSegmentationResponse:
        context = self._public_model_context(model_id)
        self._validate_supported_parameters(context=context, provided_parameters=parameters or {})
        received_at = utc_now_iso()
        routing_started = time.perf_counter()
        release = self._public_release(model_id=model_id, target_path=TargetPath.REALTIME)
        self._ensure_task(release=release, expected=TaskType.SEGMENTATION)
        routing_ms = _elapsed_ms(routing_started)
        image_meta, normalized_bytes, validation_ms, preprocessing_ms = self._prepare_image(
            file_name=file_name,
            content_type=content_type,
            image_bytes=image_bytes,
        )
        started = time.perf_counter()
        async with self._public_admission(release=release, operation="segment", wait=False):
            inference_started = time.perf_counter()
            timeout_seconds = _timeout_seconds(
                timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            )
            try:
                segments = await asyncio.wait_for(
                    self.backend.segment(
                        route=release,
                        image_bytes=normalized_bytes,
                        max_segments=max_segments or 10,
                        score_threshold=threshold or release.confidence_threshold_default,
                        timeout_seconds=timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError as exc:
                raise PlatformError(
                    code="timeout",
                    message="The realtime inference request exceeded its timeout budget.",
                    status_code=504,
                    retryable=True,
                ) from exc
            inference_ms = _elapsed_ms(inference_started)
        completed_at = utc_now_iso()
        timing = PublicInferenceTiming(
            received_at=received_at,
            completed_at=completed_at,
            validation_ms=validation_ms,
            routing_ms=routing_ms,
            preprocessing_ms=preprocessing_ms,
            inference_ms=inference_ms,
            postprocessing_ms=0.0,
            total_ms=round(validation_ms + routing_ms + preprocessing_ms + inference_ms, 3),
        )
        self.metrics.record_success(
            operation="segment_public",
            deployment_release_id=release.deployment_release_id,
            total_ms=_elapsed_ms(started),
        )
        return PublicSegmentationResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            model_id=model_id,
            model_family=release.model_family_slug,
            model_version=release.model_version_no,
            task_type=TaskType.SEGMENTATION,
            architecture=release.architecture,
            input=image_meta,
            timing=timing,
            image_width=max(int(image_meta.width or 1), 1),
            image_height=max(int(image_meta.height or 1), 1),
            segments=[
                PublicSegmentation(
                    label=item.label,
                    class_id=release.class_labels.index(item.label)
                    if item.label in release.class_labels
                    else None,
                    confidence=item.score,
                    polygon=[
                        PublicSegmentationPoint(x=point.x, y=point.y)
                        for point in item.polygon
                    ],
                )
                for item in segments
            ],
        )

    async def anomaly_public(
        self,
        *,
        model_id: str,
        file_name: str | None,
        content_type: str | None,
        image_bytes: bytes,
        timeout_ms: int | None,
        threshold: float | None,
        return_mask: bool | None,
        parameters: dict[str, Any] | None = None,
    ) -> PublicAnomalyResponse:
        context = self._public_model_context(model_id)
        self._validate_supported_parameters(context=context, provided_parameters=parameters or {})
        received_at = utc_now_iso()
        routing_started = time.perf_counter()
        release = self._public_release(model_id=model_id, target_path=TargetPath.REALTIME)
        self._ensure_task(release=release, expected=TaskType.ANOMALY)
        routing_ms = _elapsed_ms(routing_started)
        image_meta, normalized_bytes, validation_ms, preprocessing_ms = self._prepare_image(
            file_name=file_name,
            content_type=content_type,
            image_bytes=image_bytes,
        )
        started = time.perf_counter()
        async with self._public_admission(release=release, operation="anomaly", wait=False):
            inference_started = time.perf_counter()
            try:
                response = await asyncio.wait_for(
                    self.anomaly(
                        RealtimeAnomalyRequest(
                            deployment_target=release.target_path.value,
                            deployment_release_id=release.deployment_release_id,
                            image_base64=base64.b64encode(normalized_bytes).decode("ascii"),
                            timeout_ms=timeout_ms,
                            threshold=threshold,
                            return_mask=bool(return_mask),
                        )
                    ),
                    timeout=_timeout_seconds(
                        timeout_ms,
                        default_ms=self.settings.inference_limits.realtime_timeout_ms,
                    ),
                )
            except TimeoutError as exc:
                raise PlatformError(
                    code="timeout",
                    message="The realtime anomaly request exceeded its timeout budget.",
                    status_code=504,
                    retryable=True,
                ) from exc
            inference_ms = _elapsed_ms(inference_started)
        completed_at = utc_now_iso()
        timing = PublicInferenceTiming(
            received_at=received_at,
            completed_at=completed_at,
            validation_ms=validation_ms,
            routing_ms=routing_ms,
            preprocessing_ms=preprocessing_ms,
            inference_ms=inference_ms,
            postprocessing_ms=0.0,
            total_ms=round(validation_ms + routing_ms + preprocessing_ms + inference_ms, 3),
        )
        self.metrics.record_success(
            operation="anomaly_public",
            deployment_release_id=release.deployment_release_id,
            total_ms=_elapsed_ms(started),
        )
        return PublicAnomalyResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            model_id=model_id,
            model_family=release.model_family_slug,
            model_version=release.model_version_no,
            task_type=TaskType.ANOMALY,
            architecture=release.architecture,
            input=image_meta,
            timing=timing,
            anomaly_score=response.anomaly_score,
            predicted_label=response.predicted_label,
            is_anomalous=response.is_anomalous,
            threshold=response.threshold,
            heatmap=PublicAnomalyHeatmap(**response.heatmap.model_dump(mode="json")),
            mask=PublicAnomalyMask(**response.mask.model_dump(mode="json")) if response.mask is not None else None,
            image_width=response.image_width,
            image_height=response.image_height,
        )


def _public_detection_from_prediction(item: Any, *, release: ReleaseContext) -> PublicDetection:
    oriented_box = None
    if getattr(item, "oriented_box", None) is not None:
        oriented = item.oriented_box
        oriented_box = PublicOrientedBoundingBox(
            points=[PublicOrientedPoint(x=point.x, y=point.y) for point in oriented.points],
            rotation_degrees=oriented.rotation_degrees,
        )
    return PublicDetection(
        label=item.label,
        class_id=release.class_labels.index(item.label) if item.label in release.class_labels else None,
        confidence=item.score,
        bounding_box=PublicBoundingBox(
            x_min=item.box.x_min,
            y_min=item.box.y_min,
            x_max=item.box.x_max,
            y_max=item.box.y_max,
        ),
        oriented_box=oriented_box,
    )


class PublicBatchGatewayService(PublicGatewayMixin, BatchGatewayService):
    def submit_public_batch_job(
        self,
        payload: PublicBatchJobCreateRequest,
        *,
        submitted_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> PublicBatchJobSummary:
        release = self._public_release(model_id=payload.model_id, target_path=TargetPath.BATCH)
        summary = self.submit_batch_job(
            BatchJobCreateRequest(
                deployment_target=TargetPath.BATCH.value,
                deployment_release_id=release.deployment_release_id,
                input_manifest_uri=payload.input_manifest_uri,
                inputs=[
                    BatchInputItem(
                        item_id=item.item_id,
                        image_base64=item.image_base64,
                        external_ref=item.external_ref,
                        metadata=item.metadata,
                    )
                    for item in payload.inputs
                ],
                timeout_seconds=payload.timeout_seconds,
                idempotency_key=idempotency_key or None,
            ),
            submitted_by=submitted_by,
            idempotency_key=idempotency_key,
        )
        return self.get_public_batch_job(summary.batch_job_id)

    def get_public_batch_job(self, job_id: str) -> PublicBatchJobSummary | PublicBatchJobDetail:
        detail = self.get_job_detail(job_id)
        release = self._public_release(model_id=detail.route.model_family_slug, target_path=TargetPath.BATCH)
        return PublicBatchJobDetail(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            job_id=detail.batch_job_id,
            model_id=release.model_family_slug,
            model_family=release.model_family_slug,
            model_version=release.model_version_no,
            task_type=release.task_type,
            status=detail.status,
            submitted_at=detail.created_at,
            started_at=detail.started_at,
            finished_at=detail.finished_at,
            resumable=detail.resumable,
            progress=self._batch_progress(detail.progress),
            result_package=self._result_package(detail.artifacts),
            chunks=[
                PublicBatchChunkSummary(
                    chunk_no=item.chunk_no,
                    status=item.status.value if hasattr(item.status, "value") else str(item.status),
                    retry_count=item.retry_count,
                    started_at=item.started_at,
                    finished_at=item.finished_at,
                )
                for item in detail.chunks
            ],
        )

    def get_public_batch_results(self, job_id: str) -> PublicBatchResultsResponse:
        detail = self.get_job_detail(job_id)
        release = self._public_release(model_id=detail.route.model_family_slug, target_path=TargetPath.BATCH)
        manifest_path = Path(detail.output_manifest_uri) if detail.output_manifest_uri else None
        if detail.status in {BatchJobStatus.COMPLETED, BatchJobStatus.FAILED, BatchJobStatus.CANCELED} and (
            manifest_path is None or not manifest_path.exists()
        ):
            raise PlatformError(
                code="batch_job_expired",
                message="The batch job metadata still exists, but its retained result package has expired or was cleared.",
                detail=job_id,
                status_code=410,
                retryable=False,
            )
        payload = self.get_job_results(job_id)
        return PublicBatchResultsResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            job_id=job_id,
            model_id=release.model_family_slug,
            model_family=release.model_family_slug,
            model_version=release.model_version_no,
            task_type=release.task_type,
            status=payload.status,
            progress=self._batch_progress(detail.progress),
            result_package=self._result_package(payload.artifacts),
            items=[
                PublicBatchResultItem(
                    item_id=item.item_id,
                    external_ref=item.external_ref,
                    status=item.status,
                    predictions=[
                        PublicClassificationPrediction(label=label.label, score=label.score)
                        for label in item.labels
                    ],
                    detections=[
                        _public_detection_from_prediction(detection, release=release)
                        for detection in item.detections
                    ],
                    image_width=item.image_width,
                    image_height=item.image_height,
                    segments=[
                        PublicSegmentation(
                            label=segment.label,
                            class_id=release.class_labels.index(segment.label)
                            if segment.label in release.class_labels
                            else None,
                            confidence=segment.score,
                            polygon=[
                                PublicSegmentationPoint(x=point.x, y=point.y)
                                for point in segment.polygon
                            ],
                        )
                        for segment in item.segments
                    ],
                    anomaly_score=item.anomaly_score,
                    predicted_label=item.predicted_label,
                    is_anomalous=item.is_anomalous,
                    threshold=item.threshold,
                    heatmap=PublicAnomalyHeatmap(**item.heatmap.model_dump(mode="json"))
                    if item.heatmap is not None
                    else None,
                    mask=PublicAnomalyMask(**item.mask.model_dump(mode="json"))
                    if item.mask is not None
                    else None,
                    error_code="internal_processing_failure" if item.error else None,
                    error_message=item.error,
                    timing=(
                        self._to_public_timing(timing=item.timing, completed_at=utc_now_iso())
                        if item.timing is not None
                        else None
                    ),
                )
                for item in payload.items
            ],
        )

    def delete_public_batch_job(self, job_id: str) -> PublicBatchJobMutationResponse:
        detail = self.get_job_detail(job_id)
        if detail.status in {BatchJobStatus.ACCEPTED, BatchJobStatus.QUEUED, BatchJobStatus.RUNNING}:
            with session_scope(self.settings) as session:
                update_batch_job_state(
                    session,
                    batch_job_id=job_id,
                    status=BatchJobStatus.CANCELED,
                    source_service=self.service_name,
                    actor_type="system",
                    actor_id=self.service_name,
                    reason="Canceled through public inference API.",
                )
            return PublicBatchJobMutationResponse(
                request_id=get_request_id() or "unknown",
                correlation_id=get_correlation_id(),
                job_id=job_id,
                action="canceled",
                status=BatchJobStatus.CANCELED,
                detail="Batch job marked as canceled. In-flight chunk execution is best-effort and may finish.",
            )

        job_root = self.settings.storage.batch_job_root(job_id)
        results_root = job_root / "results"
        if results_root.exists():
            shutil.rmtree(results_root)
        return PublicBatchJobMutationResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            job_id=job_id,
            action="cleared",
            status=detail.status,
            detail="Terminal batch job artifacts were cleared from retained storage.",
        )
