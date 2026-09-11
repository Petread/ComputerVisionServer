from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import math
import time
import uuid
import zipfile
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Condition, Lock
from typing import Any
from urllib.parse import urlparse

from cv_contracts import (
    AnomalyHeatmap,
    AnomalyInferenceResponse,
    AnomalyMask,
    BatchArtifactSummary,
    BatchInputItem,
    BatchJobCreateRequest,
    BatchJobDetail,
    BatchJobProgress,
    BatchJobSummary,
    BatchResultRecord,
    BatchResultResponse,
    ClassificationInferenceResponse,
    ClassificationLabelScore,
    DetectionBox,
    DetectionInferenceResponse,
    DetectionPoint,
    DetectionPrediction,
    InferenceTiming,
    LatencyQuantiles,
    ModelCapabilitiesResponse,
    ModelCapabilitySummary,
    ModelStatusResponse,
    ModelStatusSummary,
    OrientedDetectionBox,
    RealtimeAnomalyRequest,
    RealtimeClassifyRequest,
    RealtimeDetectRequest,
    RealtimeInferenceRequest,
    RealtimeInferenceResponse,
    RealtimeSegmentRequest,
    RealtimeTemporalSequenceRequest,
    ResponseMeta,
    SegmentationInferenceResponse,
    SegmentationPoint,
    SegmentationPrediction,
    TemporalSequenceInferenceResponse,
)
from cv_core import PlatformSettings
from cv_db import (
    BatchJobChunk,
    build_batch_request_hash,
    create_batch_job_request,
    get_batch_job_detail,
    get_batch_job_record,
    get_batch_result_response,
    list_batch_jobs,
    read_idempotency_response,
    refresh_batch_job_progress,
    replace_batch_chunks,
    session_scope,
    update_batch_chunk_state,
    update_batch_job_state,
    write_idempotency_response,
)
from cv_runtime.temporal_execution import (
    _bounded_probability as _bounded_probability,
)
from cv_runtime.temporal_execution import (
    _calibrated_temporal_confidence as _calibrated_temporal_confidence,
)
from cv_runtime.temporal_execution import (
    _extract_temporal_feature as _extract_temporal_feature,
)
from cv_runtime.temporal_execution import (
    _finite_float as _finite_float,
)
from cv_runtime.temporal_execution import (
    _load_temporal_contract_artifact as _load_temporal_contract_artifact,
)
from cv_runtime.temporal_execution import (
    _ordinal_quality_probabilities as _ordinal_quality_probabilities,
)
from cv_runtime.temporal_execution import (
    _prepare_temporal_model_tensor as _prepare_temporal_model_tensor,
)
from cv_runtime.temporal_execution import (
    _stable_sigmoid as _stable_sigmoid,
)
from cv_runtime.temporal_execution import (
    _temporal_path_value as _temporal_path_value,
)
from cv_runtime.temporal_execution import (
    _temporal_prediction_from_outputs as _temporal_prediction_from_outputs,
)
from cv_runtime.temporal_execution import (
    _wrap_to_half_turn as _wrap_to_half_turn,
)
from cv_status import (
    BatchChunkStatus,
    BatchJobStatus,
    EnvironmentName,
    QueueName,
    TargetPath,
    TaskType,
)
from cv_utils import (
    PlatformError,
    ensure_path_within_root,
    get_correlation_id,
    get_request_id,
    inspect_image_bytes,
    utc_now_iso,
)
from inference_gateway.runtime.metrics import RuntimeMetrics
from inference_gateway.runtime.resolver import ReleaseContext, ReleaseResolver
from redis import Redis
from rq import Queue, Retry
from sqlalchemy import select


@dataclass(frozen=True)
class _OutputTensor:
    name: str
    shape: list[int]
    values: Sequence[float]


@dataclass
class _BackendPhaseTiming:
    preprocessing_ms: float = 0.0
    inference_ms: float = 0.0
    postprocessing_ms: float = 0.0
    measured: bool = False
    shared_memory_transport: bool = False


_backend_phase_timing: ContextVar[_BackendPhaseTiming | None] = ContextVar(
    "inference_backend_phase_timing",
    default=None,
)


@dataclass(frozen=True)
class _SharedMemoryTensor:
    name: str
    data_type: str
    shape: list[int]
    offset: int
    byte_size: int


class _SharedMemoryUnavailable(Exception):
    def __init__(self, detail: str, *, disable_endpoint: bool) -> None:
        super().__init__(detail)
        self.disable_endpoint = disable_endpoint


@dataclass
class _RegisteredSharedMemoryRegion:
    name: str
    memory: Any
    byte_size: int
    endpoint: Any
    client: Any
    in_use: bool = True


class ConcurrencyGate:
    def __init__(self, limit: int, *, on_change: Callable[..., None] | None = None) -> None:
        self.limit = max(limit, 1)
        self.current_inflight = 0
        self._condition = asyncio.Condition()
        self._on_change = on_change

    async def try_acquire(
        self,
        *,
        wait: bool,
        timeout_seconds: float | None = None,
    ) -> bool:
        deadline = (
            asyncio.get_running_loop().time() + max(float(timeout_seconds), 0.0)
            if timeout_seconds is not None
            else None
        )
        async with self._condition:
            while self.current_inflight >= self.limit:
                if not wait:
                    return False
                if deadline is None:
                    await self._condition.wait()
                    continue
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except TimeoutError:
                    return False
            if self.current_inflight >= self.limit:
                return False
            self.current_inflight += 1
            self._notify()
            return True

    async def release(self) -> None:
        async with self._condition:
            self.current_inflight = max(self.current_inflight - 1, 0)
            self._notify()
            self._condition.notify(1)

    def overloaded(self) -> bool:
        return self.current_inflight >= self.limit

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change(current_inflight=self.current_inflight)


class DeterministicInferenceBackend:
    def __init__(self, *, warm_ttl_seconds: int) -> None:
        self._warm_ttl_seconds = warm_ttl_seconds
        self._last_used_at: dict[str, float] = {}

    def is_warm(self, deployment_release_id: str) -> bool:
        last_used = self._last_used_at.get(deployment_release_id)
        if last_used is None:
            return False
        return (time.time() - last_used) <= self._warm_ttl_seconds

    def last_request_at(self, deployment_release_id: str) -> str | None:
        last_used = self._last_used_at.get(deployment_release_id)
        if last_used is None:
            return None
        return datetime.fromtimestamp(last_used, tz=UTC).isoformat().replace("+00:00", "Z")

    async def classify(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        top_k: int,
    ) -> list[ClassificationLabelScore]:
        await self._simulate_runtime(route.deployment_release_id, image_bytes=image_bytes)
        labels = route.class_labels or ["accepted", "rework"]
        digest = _digest_words(image_bytes=image_bytes, salt=route.deployment_release_id)
        raw_scores = [
            1.0 + ((digest[index % len(digest)] % 1000) / 1000.0)
            for index in range(len(labels))
        ]
        total = sum(raw_scores) or 1.0
        ranked = sorted(
            ((label, score / total) for label, score in zip(labels, raw_scores, strict=False)),
            key=lambda item: item[1],
            reverse=True,
        )
        return [
            ClassificationLabelScore(label=label, score=round(score, 6), rank=index + 1)
            for index, (label, score) in enumerate(ranked[:top_k])
        ]

    async def detect(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        max_detections: int,
        score_threshold: float,
    ) -> list[DetectionPrediction]:
        await self._simulate_runtime(route.deployment_release_id, image_bytes=image_bytes)
        labels = route.class_labels or ["object"]
        digest = _digest_words(image_bytes=image_bytes, salt=f"{route.deployment_release_id}:det")
        detection_count = max(1, min(max_detections, 1 + (digest[0] % 3)))
        results: list[DetectionPrediction] = []
        for index in range(detection_count):
            seed = digest[index % len(digest)]
            score = round(0.45 + ((seed % 550) / 1000.0), 6)
            if score < score_threshold:
                continue
            x_min = round(((seed % 400) / 1000.0), 4)
            y_min = round((((seed // 7) % 400) / 1000.0), 4)
            width = round(0.2 + (((seed // 11) % 220) / 1000.0), 4)
            height = round(0.15 + (((seed // 13) % 260) / 1000.0), 4)
            box = DetectionBox(
                x_min=x_min,
                y_min=y_min,
                x_max=min(round(x_min + width, 4), 0.99),
                y_max=min(round(y_min + height, 4), 0.99),
            )
            oriented_box = None
            if route.task_type == TaskType.ORIENTED_DETECTION:
                angle_degrees = float(((seed // 17) % 90) - 45)
                oriented_box = _oriented_box_from_xywhr_values(
                    [
                        (box.x_min + box.x_max) / 2.0,
                        (box.y_min + box.y_max) / 2.0,
                        max(box.x_max - box.x_min, 0.0),
                        max(box.y_max - box.y_min, 0.0),
                        angle_degrees,
                    ],
                    route=route,
                )
            results.append(
                DetectionPrediction(
                    label=labels[index % len(labels)],
                    score=score,
                    box=_axis_aligned_box_from_oriented(oriented_box) if oriented_box else box,
                    oriented_box=oriented_box,
                )
            )
        return results

    async def segment(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        max_segments: int,
        score_threshold: float,
    ) -> list[SegmentationPrediction]:
        await self._simulate_runtime(route.deployment_release_id, image_bytes=image_bytes)
        labels = route.class_labels or ["region"]
        digest = _digest_words(image_bytes=image_bytes, salt=f"{route.deployment_release_id}:seg")
        segment_count = max(1, min(max_segments, 1 + (digest[0] % 3)))
        results: list[SegmentationPrediction] = []
        for index in range(segment_count):
            seed = digest[index % len(digest)]
            score = round(0.42 + ((seed % 520) / 1000.0), 6)
            if score < score_threshold:
                continue
            center_x = 0.15 + ((seed % 650) / 1000.0)
            center_y = 0.15 + (((seed // 5) % 650) / 1000.0)
            half_width = 0.08 + (((seed // 7) % 120) / 1000.0)
            half_height = 0.06 + (((seed // 11) % 120) / 1000.0)
            points = [
                SegmentationPoint(
                    x=max(0.0, min(round(center_x - half_width, 4), 0.99)),
                    y=max(0.0, min(round(center_y - half_height, 4), 0.99)),
                ),
                SegmentationPoint(
                    x=max(0.0, min(round(center_x + half_width, 4), 0.99)),
                    y=max(0.0, min(round(center_y - (half_height * 0.7), 4), 0.99)),
                ),
                SegmentationPoint(
                    x=max(0.0, min(round(center_x + (half_width * 0.8), 4), 0.99)),
                    y=max(0.0, min(round(center_y + half_height, 4), 0.99)),
                ),
                SegmentationPoint(
                    x=max(0.0, min(round(center_x - half_width, 4), 0.99)),
                    y=max(0.0, min(round(center_y + (half_height * 0.8), 4), 0.99)),
                ),
            ]
            results.append(
                SegmentationPrediction(
                    label=labels[index % len(labels)],
                    score=score,
                    polygon=points,
                )
            )
        return results

    async def _simulate_runtime(self, deployment_release_id: str, *, image_bytes: bytes) -> None:
        cold_penalty = 0.015 if not self.is_warm(deployment_release_id) else 0.003
        payload_penalty = min(len(image_bytes) / 1_000_000.0, 0.02)
        await asyncio.sleep(cold_penalty + payload_penalty)
        self._last_used_at[deployment_release_id] = time.time()


class TritonInferenceBackend:
    def __init__(
        self,
        *,
        settings: PlatformSettings,
        target_path: TargetPath,
        warm_ttl_seconds: int,
    ) -> None:
        self._settings = settings
        self._target_path = target_path
        self._simulation_backend = DeterministicInferenceBackend(
            warm_ttl_seconds=warm_ttl_seconds
        )
        self._warm_ttl_seconds = warm_ttl_seconds
        self._last_used_at: dict[str, float] = {}
        self._onnx_sessions: dict[str, object] = {}
        self._triton_clients: dict[str, Any] = {}
        self._system_shared_memory_supported: dict[str, bool] = {}
        self._shared_memory_output_tensors: dict[
            tuple[str, str, int],
            list[dict[str, Any]],
        ] = {}
        self._triton_client_lock = Lock()
        self._shared_memory_pool_condition = Condition(Lock())
        self._shared_memory_regions: dict[
            tuple[Any, ...],
            list[_RegisteredSharedMemoryRegion],
        ] = {}
        configured_pool_size = (
            settings.inference_limits.realtime_per_release_max_concurrency
            if target_path == TargetPath.REALTIME
            else settings.inference_limits.batch_per_release_max_concurrency
        )
        self._shared_memory_pool_size = max(int(configured_pool_size), 1)
        self._closed = False

    def is_warm(self, deployment_release_id: str) -> bool:
        if self._simulation_backend.is_warm(deployment_release_id):
            return True
        last_used = self._last_used_at.get(deployment_release_id)
        if last_used is None:
            return False
        return (time.time() - last_used) <= self._warm_ttl_seconds

    def last_request_at(self, deployment_release_id: str) -> str | None:
        simulated = self._simulation_backend.last_request_at(deployment_release_id)
        actual = self._last_used_at.get(deployment_release_id)
        if actual is None:
            return simulated
        actual_iso = datetime.fromtimestamp(actual, tz=UTC).isoformat().replace(
            "+00:00", "Z"
        )
        return max(filter(None, [simulated, actual_iso]), default=None)

    def ready_model_versions(self) -> set[tuple[str, int]] | None:
        endpoint = _triton_endpoint(self._settings, self._target_path)
        try:
            client = self._triton_client(endpoint)
            response = client.post(
                f"{endpoint.http_url.rstrip('/')}/v2/repository/index",
                json={"ready": True},
                timeout=min(max(float(endpoint.timeout_seconds), 0.05), 2.0),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None
        if not isinstance(payload, list):
            return set()
        ready: set[tuple[str, int]] = set()
        for item in payload:
            if not isinstance(item, dict) or item.get("state") != "READY":
                continue
            model_name = item.get("name")
            model_version = item.get("version")
            if model_name is None or model_version is None:
                continue
            try:
                ready.add((str(model_name), int(model_version)))
            except (TypeError, ValueError):
                continue
        return ready

    def model_statistics(self) -> dict[tuple[str, int], dict[str, int | float]]:
        endpoint = _triton_endpoint(self._settings, self._target_path)
        try:
            client = self._triton_client(endpoint)
            response = client.get(
                f"{endpoint.http_url.rstrip('/')}/v2/models/stats",
                timeout=min(max(float(endpoint.timeout_seconds), 0.05), 2.0),
            )
            response.raise_for_status()
            return _triton_model_statistics(response.json())
        except Exception:
            return {}

    async def classify(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        top_k: int,
        timeout_seconds: float | None = None,
    ) -> list[ClassificationLabelScore]:
        if _uses_simulated_inference(route):
            return await self._simulation_backend.classify(
                route=route,
                image_bytes=image_bytes,
                top_k=top_k,
            )
        try:
            labels = await self._classify_via_triton(
                route=route,
                image_bytes=image_bytes,
                top_k=top_k,
                timeout_seconds=timeout_seconds,
            )
        except PlatformError as exc:
            if not _local_onnx_fallback_enabled(self._settings) or not _can_fallback_to_local_onnx(exc):
                raise
            labels = await asyncio.to_thread(
                self._classify_via_local_onnx,
                route=route,
                image_bytes=image_bytes,
                top_k=top_k,
            )
        self._last_used_at[route.deployment_release_id] = time.time()
        return labels

    async def detect(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        max_detections: int,
        score_threshold: float,
        timeout_seconds: float | None = None,
    ) -> list[DetectionPrediction]:
        if _uses_simulated_inference(route):
            return await self._simulation_backend.detect(
                route=route,
                image_bytes=image_bytes,
                max_detections=max_detections,
                score_threshold=score_threshold,
            )
        try:
            detections = await self._detect_via_triton(
                route=route,
                image_bytes=image_bytes,
                max_detections=max_detections,
                score_threshold=score_threshold,
                timeout_seconds=timeout_seconds,
            )
        except PlatformError as exc:
            if not _local_onnx_fallback_enabled(self._settings) or not _can_fallback_to_local_onnx(exc):
                raise
            detections = await asyncio.to_thread(
                self._detect_via_local_onnx,
                route=route,
                image_bytes=image_bytes,
                max_detections=max_detections,
                score_threshold=score_threshold,
            )
        self._last_used_at[route.deployment_release_id] = time.time()
        return detections

    async def segment(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        max_segments: int,
        score_threshold: float,
        timeout_seconds: float | None = None,
    ) -> list[SegmentationPrediction]:
        if _uses_simulated_inference(route):
            return await self._simulation_backend.segment(
                route=route,
                image_bytes=image_bytes,
                max_segments=max_segments,
                score_threshold=score_threshold,
            )
        try:
            segments = await self._segment_via_triton(
                route=route,
                image_bytes=image_bytes,
                max_segments=max_segments,
                score_threshold=score_threshold,
                timeout_seconds=timeout_seconds,
            )
        except PlatformError as exc:
            if not _local_onnx_fallback_enabled(self._settings) or not _can_fallback_to_local_onnx(exc):
                raise
            segments = await asyncio.to_thread(
                self._segment_via_local_onnx,
                route=route,
                image_bytes=image_bytes,
                max_segments=max_segments,
                score_threshold=score_threshold,
            )
        self._last_used_at[route.deployment_release_id] = time.time()
        return segments

    async def temporal_sequence(
        self,
        *,
        route: ReleaseContext,
        tensor: Any,
        timeout_seconds: float | None = None,
    ) -> dict[str, _OutputTensor]:
        if _uses_simulated_inference(route):
            raise PlatformError(
                code="temporal_simulation_unsupported",
                message="Temporal angle inference requires a real versioned model artifact.",
                status_code=503,
            )
        try:
            outputs = await self._model_outputs_for_tensor_via_triton(
                route=route,
                tensor=tensor,
                timeout_seconds=timeout_seconds,
            )
        except PlatformError as exc:
            if not _local_onnx_fallback_enabled(self._settings) or not _can_fallback_to_local_onnx(exc):
                raise
            outputs = await asyncio.to_thread(
                self._model_outputs_for_tensor_via_local_onnx,
                route=route,
                tensor=tensor,
            )
        self._last_used_at[route.deployment_release_id] = time.time()
        return outputs

    async def _classify_via_triton(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        top_k: int,
        timeout_seconds: float | None,
    ) -> list[ClassificationLabelScore]:
        output_name = _first_tensor_name(route.manifest, kind="output")
        if not output_name:
            raise PlatformError(
                code="inference_schema_missing",
                message=(
                    "The deployment manifest does not declare classification "
                    "output tensors."
                ),
                status_code=500,
                metadata={"deployment_release_id": route.deployment_release_id},
            )
        outputs = await self._model_outputs_via_triton(
            route=route,
            image_bytes=image_bytes,
            timeout_seconds=timeout_seconds,
        )
        return _classification_labels_from_outputs(
            route=route,
            outputs=outputs,
            fallback_output_name=output_name,
            top_k=top_k,
        )

    async def _detect_via_triton(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        max_detections: int,
        score_threshold: float,
        timeout_seconds: float | None,
    ) -> list[DetectionPrediction]:
        outputs = await self._model_outputs_via_triton(
            route=route,
            image_bytes=image_bytes,
            timeout_seconds=timeout_seconds,
        )
        return _detection_predictions_from_outputs(
            route=route,
            outputs=outputs,
            max_detections=max_detections,
            score_threshold=score_threshold,
        )

    async def _segment_via_triton(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        max_segments: int,
        score_threshold: float,
        timeout_seconds: float | None,
    ) -> list[SegmentationPrediction]:
        outputs = await self._model_outputs_via_triton(
            route=route,
            image_bytes=image_bytes,
            timeout_seconds=timeout_seconds,
        )
        postprocessing_started = time.perf_counter()
        segments = _segmentation_predictions_from_outputs(
            route=route,
            outputs=outputs,
            max_segments=max_segments,
            score_threshold=score_threshold,
        )
        timing = _backend_phase_timing.get()
        if timing is not None:
            timing.postprocessing_ms += _elapsed_ms(postprocessing_started)
        return segments

    async def _model_outputs_via_triton(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        timeout_seconds: float | None = None,
    ) -> dict[str, _OutputTensor]:
        tensor = await asyncio.to_thread(_classification_tensor, image_bytes, route)
        return await self._model_outputs_for_tensor_via_triton(
            route=route,
            tensor=tensor,
            timeout_seconds=timeout_seconds,
        )

    async def _model_outputs_for_tensor_via_triton(
        self,
        *,
        route: ReleaseContext,
        tensor: Any,
        timeout_seconds: float | None = None,
    ) -> dict[str, _OutputTensor]:
        preprocessing_started = time.perf_counter()
        input_name = _first_tensor_name(route.manifest, kind="input")
        output_names = _tensor_names(route.manifest, kind="output")
        if not input_name or not output_names:
            raise PlatformError(
                code="inference_schema_missing",
                message="The deployment manifest does not declare model input/output tensors.",
                status_code=500,
                metadata={"deployment_release_id": route.deployment_release_id},
            )
        endpoint = _triton_endpoint(self._settings, self._target_path)
        tensor_bytes = tensor.tobytes(order="C")
        try:
            timing = _backend_phase_timing.get()
            endpoint_url = str(endpoint.http_url)
            model_cache_key = (
                endpoint_url,
                route.triton_model_name,
                route.triton_model_version,
            )
            shared_layout = _triton_shared_memory_layout(
                route=route,
                input_name=input_name,
                input_shape=[int(item) for item in tensor.shape],
                input_byte_size=len(tensor_bytes),
                output_tensors=self._shared_memory_output_tensors.get(model_cache_key),
            )
            outputs: dict[str, _OutputTensor] | None = None
            if (
                shared_layout is not None
                and _is_local_triton_endpoint(endpoint)
                and self._system_shared_memory_supported.get(endpoint_url, True)
            ):
                if timing is not None:
                    timing.preprocessing_ms = _elapsed_ms(preprocessing_started)
                inference_started = time.perf_counter()
                try:
                    outputs = await asyncio.to_thread(
                        self._post_triton_shared_memory_request,
                        endpoint=endpoint,
                        route=route,
                        tensor_bytes=tensor_bytes,
                        layout=shared_layout,
                        timeout_seconds=timeout_seconds,
                    )
                    self._system_shared_memory_supported[endpoint_url] = True
                    if timing is not None:
                        timing.shared_memory_transport = True
                except _SharedMemoryUnavailable as exc:
                    if exc.disable_endpoint:
                        self._system_shared_memory_supported[endpoint_url] = False
                if outputs is not None and timing is not None:
                    timing.inference_ms = _elapsed_ms(inference_started)
                    timing.measured = True
            if outputs is not None:
                return outputs

            payload = {
                "inputs": [
                    {
                        "name": input_name,
                        "shape": list(tensor.shape),
                        "datatype": "FP32",
                        "parameters": {"binary_data_size": len(tensor_bytes)},
                    }
                ],
                "outputs": [
                    {"name": name, "parameters": {"binary_data": True}}
                    for name in output_names
                ],
            }
            payload_bytes = json.dumps(payload).encode("utf-8")
            if timing is not None:
                timing.preprocessing_ms = _elapsed_ms(preprocessing_started)
            inference_started = time.perf_counter()
            response = await asyncio.to_thread(
                self._post_triton_binary_request,
                endpoint=endpoint,
                route=route,
                payload_bytes=payload_bytes,
                tensor_bytes=tensor_bytes,
                timeout_seconds=timeout_seconds,
            )
            if timing is not None:
                timing.inference_ms = _elapsed_ms(inference_started)
            postprocessing_started = time.perf_counter()
            outputs = _triton_response_output_tensors(response)
            learned_outputs = _triton_response_output_specs(response)
            if learned_outputs:
                self._shared_memory_output_tensors[model_cache_key] = learned_outputs
            if timing is not None:
                timing.postprocessing_ms = _elapsed_ms(postprocessing_started)
                timing.measured = True
            return outputs
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError(
                code="triton_inference_failed",
                message="Triton inference failed for the selected deployment.",
                detail=str(exc),
                status_code=503,
                metadata={
                    "deployment_release_id": route.deployment_release_id,
                    "model_name": route.triton_model_name,
                    "model_version": route.triton_model_version,
                },
            ) from exc

    def _post_triton_shared_memory_request(
        self,
        *,
        endpoint: Any,
        route: ReleaseContext,
        tensor_bytes: bytes,
        layout: list[_SharedMemoryTensor],
        timeout_seconds: float | None = None,
    ) -> dict[str, _OutputTensor]:
        import numpy as np

        pool_key = self._shared_memory_pool_key(endpoint=endpoint, route=route)
        region = self._acquire_shared_memory_region(
            pool_key=pool_key,
            endpoint=endpoint,
            layout=layout,
            timeout_seconds=timeout_seconds,
        )
        memory = region.memory
        client = region.client
        reusable = True
        try:
            input_layout = layout[0]
            memory.buf[
                input_layout.offset : input_layout.offset + input_layout.byte_size
            ] = tensor_bytes

            payload = {
                "inputs": [
                    {
                        "name": input_layout.name,
                        "shape": input_layout.shape,
                        "datatype": input_layout.data_type,
                        "parameters": {
                            "shared_memory_region": region.name,
                            "shared_memory_offset": input_layout.offset,
                            "shared_memory_byte_size": input_layout.byte_size,
                        },
                    }
                ],
                "outputs": [
                    {
                        "name": item.name,
                        "parameters": {
                            "shared_memory_region": region.name,
                            "shared_memory_offset": item.offset,
                            "shared_memory_byte_size": item.byte_size,
                        },
                    }
                    for item in layout[1:]
                ],
            }
            response = client.post(
                (
                    f"{endpoint.http_url.rstrip('/')}/v2/models/{route.triton_model_name}"
                    f"/versions/{route.triton_model_version}/infer"
                ),
                json=payload,
                timeout=max(float(timeout_seconds or endpoint.timeout_seconds), 0.05),
            )
            if response.status_code == 400:
                reusable = False
                raise _SharedMemoryUnavailable(
                    response.text,
                    disable_endpoint=False,
                )
            if response.status_code == 404:
                raise PlatformError(
                    code="triton_model_unavailable",
                    message="Triton rejected the selected deployment model.",
                    detail=response.text,
                    status_code=503,
                    metadata={
                        "deployment_release_id": route.deployment_release_id,
                        "model_name": route.triton_model_name,
                        "model_version": route.triton_model_version,
                    },
                )
            response.raise_for_status()
            response_payload = response.json()
            response_outputs = {
                str(item.get("name") or ""): item
                for item in response_payload.get("outputs") or []
                if isinstance(item, dict)
            }
            parsed: dict[str, _OutputTensor] = {}
            for item in layout[1:]:
                output = response_outputs.get(item.name)
                if output is None:
                    raise PlatformError(
                        code="triton_output_missing",
                        message="Triton did not return the expected model output tensor.",
                        status_code=502,
                        metadata={"output_name": item.name},
                    )
                shape = _shape_from_triton_output(output)
                actual_size = _tensor_byte_size(
                    shape,
                    str(output.get("datatype") or item.data_type),
                )
                if actual_size <= 0 or actual_size > item.byte_size:
                    raise PlatformError(
                        code="triton_output_shape_invalid",
                        message="Triton returned an output larger than its shared-memory region.",
                        status_code=502,
                        metadata={
                            "output_name": item.name,
                            "allocated_bytes": item.byte_size,
                            "actual_bytes": actual_size,
                        },
                    )
                raw = memory.buf[item.offset : item.offset + actual_size]
                values = np.frombuffer(
                    raw,
                    dtype=_numpy_dtype_for_triton(
                        str(output.get("datatype") or item.data_type)
                    ),
                ).reshape(-1)
                if values.dtype == np.dtype("float32"):
                    values = values.copy()
                else:
                    values = values.astype("float32")
                raw.release()
                parsed[item.name] = _OutputTensor(
                    name=item.name,
                    shape=shape,
                    values=values,
                )
            return parsed
        except Exception:
            reusable = False
            raise
        finally:
            self._release_shared_memory_region(
                pool_key=pool_key,
                region=region,
                reusable=reusable,
                timeout_seconds=timeout_seconds,
            )

    def _shared_memory_pool_key(
        self,
        *,
        endpoint: Any,
        route: ReleaseContext,
    ) -> tuple[Any, ...]:
        return (
            str(endpoint.http_url).rstrip("/"),
            route.deployment_release_id,
            route.triton_model_name,
            route.triton_model_version,
        )

    def _acquire_shared_memory_region(
        self,
        *,
        pool_key: tuple[Any, ...],
        endpoint: Any,
        layout: list[_SharedMemoryTensor],
        timeout_seconds: float | None,
    ) -> _RegisteredSharedMemoryRegion:
        from multiprocessing import shared_memory

        timeout = max(float(timeout_seconds or endpoint.timeout_seconds), 0.05)
        deadline = time.monotonic() + timeout
        required_size = max(item.offset + item.byte_size for item in layout)
        with self._shared_memory_pool_condition:
            while True:
                if self._closed:
                    raise _SharedMemoryUnavailable(
                        "The Triton shared-memory pool is closed.",
                        disable_endpoint=False,
                    )
                regions = self._shared_memory_regions.setdefault(pool_key, [])
                available = next(
                    (
                        item
                        for item in regions
                        if not item.in_use and item.byte_size >= required_size
                    ),
                    None,
                )
                if available is not None:
                    available.in_use = True
                    return available
                undersized = next((item for item in regions if not item.in_use), None)
                if undersized is not None:
                    regions.remove(undersized)
                    self._destroy_shared_memory_region(
                        undersized,
                        timeout_seconds=timeout_seconds,
                    )
                    continue
                if len(regions) < self._shared_memory_pool_size:
                    region_name = f"ybb_{uuid.uuid4().hex}"
                    memory = shared_memory.SharedMemory(
                        name=region_name,
                        create=True,
                        size=required_size,
                    )
                    try:
                        client = self._triton_client(endpoint)
                        response = client.post(
                            (
                                f"{endpoint.http_url.rstrip('/')}/v2/systemsharedmemory/region/"
                                f"{region_name}/register"
                            ),
                            json={
                                "key": f"/{region_name}",
                                "offset": 0,
                                "byte_size": required_size,
                            },
                            timeout=timeout,
                        )
                        if response.status_code >= 400:
                            raise _SharedMemoryUnavailable(
                                response.text,
                                disable_endpoint=response.status_code in {400, 404, 405},
                            )
                    except Exception:
                        memory.close()
                        memory.unlink()
                        raise
                    region = _RegisteredSharedMemoryRegion(
                        name=region_name,
                        memory=memory,
                        byte_size=required_size,
                        endpoint=endpoint,
                        client=client,
                    )
                    regions.append(region)
                    return region
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _SharedMemoryUnavailable(
                        "Timed out waiting for a registered Triton shared-memory region.",
                        disable_endpoint=False,
                    )
                self._shared_memory_pool_condition.wait(timeout=remaining)

    def _release_shared_memory_region(
        self,
        *,
        pool_key: tuple[Any, ...],
        region: _RegisteredSharedMemoryRegion,
        reusable: bool,
        timeout_seconds: float | None,
    ) -> None:
        destroy = not reusable
        with self._shared_memory_pool_condition:
            regions = self._shared_memory_regions.get(pool_key, [])
            if self._closed or destroy:
                if region in regions:
                    regions.remove(region)
                if not regions:
                    self._shared_memory_regions.pop(pool_key, None)
                destroy = True
            else:
                region.in_use = False
            self._shared_memory_pool_condition.notify_all()
        if destroy:
            self._destroy_shared_memory_region(region, timeout_seconds=timeout_seconds)

    @staticmethod
    def _destroy_shared_memory_region(
        region: _RegisteredSharedMemoryRegion,
        *,
        timeout_seconds: float | None,
    ) -> None:
        try:
            region.client.post(
                (
                    f"{region.endpoint.http_url.rstrip('/')}/v2/systemsharedmemory/region/"
                    f"{region.name}/unregister"
                ),
                timeout=max(
                    float(timeout_seconds or region.endpoint.timeout_seconds),
                    0.05,
                ),
            )
        except Exception:
            pass
        try:
            region.memory.close()
        finally:
            try:
                region.memory.unlink()
            except FileNotFoundError:
                pass

    def close(self) -> None:
        with self._shared_memory_pool_condition:
            if self._closed:
                return
            self._closed = True
            regions = [
                region
                for pool_regions in self._shared_memory_regions.values()
                for region in pool_regions
            ]
            self._shared_memory_regions.clear()
            self._shared_memory_pool_condition.notify_all()
        for region in regions:
            self._destroy_shared_memory_region(region, timeout_seconds=0.25)
        with self._triton_client_lock:
            clients = list(self._triton_clients.values())
            self._triton_clients.clear()
        for client in clients:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _post_triton_binary_request(
        self,
        *,
        endpoint: Any,
        route: ReleaseContext,
        payload_bytes: bytes,
        tensor_bytes: bytes,
        timeout_seconds: float | None = None,
    ) -> Any:
        client = self._triton_client(endpoint)
        response = client.post(
            (
                f"{endpoint.http_url.rstrip('/')}/v2/models/{route.triton_model_name}"
                f"/versions/{route.triton_model_version}/infer"
            ),
            content=payload_bytes + tensor_bytes,
            headers={
                "Content-Type": "application/octet-stream",
                "Inference-Header-Content-Length": str(len(payload_bytes)),
            },
            timeout=max(float(timeout_seconds or endpoint.timeout_seconds), 0.05),
        )
        if response.status_code in {400, 404}:
            raise PlatformError(
                code="triton_model_unavailable",
                message=(
                    "Triton rejected the selected deployment model. The model is "
                    "not loaded or its repository config does not match the artifact."
                ),
                detail=response.text,
                status_code=503,
                metadata={
                    "deployment_release_id": route.deployment_release_id,
                    "model_name": route.triton_model_name,
                    "model_version": route.triton_model_version,
                },
            )
        response.raise_for_status()
        return response

    def _triton_client(self, endpoint: Any) -> Any:
        base_url = str(endpoint.http_url).rstrip("/")
        client = self._triton_clients.get(base_url)
        if client is not None:
            return client
        with self._triton_client_lock:
            client = self._triton_clients.get(base_url)
            if client is None:
                import httpx

                client = httpx.Client(
                    timeout=endpoint.timeout_seconds,
                    trust_env=False,
                )
                self._triton_clients[base_url] = client
            return client

    def _classify_via_local_onnx(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        top_k: int,
    ) -> list[ClassificationLabelScore]:
        outputs = self._model_outputs_via_local_onnx(route=route, image_bytes=image_bytes)
        first_output = next(iter(outputs.values()), None)
        if first_output is None:
            raise PlatformError(
                code="onnx_output_missing",
                message="The deployed ONNX model did not return a classification output.",
                status_code=500,
                metadata={"deployment_release_id": route.deployment_release_id},
            )
        return _classification_labels_from_scores(
            route=route,
            scores=first_output.values,
            top_k=top_k,
        )

    def _detect_via_local_onnx(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        max_detections: int,
        score_threshold: float,
    ) -> list[DetectionPrediction]:
        outputs = self._model_outputs_via_local_onnx(route=route, image_bytes=image_bytes)
        return _detection_predictions_from_outputs(
            route=route,
            outputs=outputs,
            max_detections=max_detections,
            score_threshold=score_threshold,
        )

    def _segment_via_local_onnx(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        max_segments: int,
        score_threshold: float,
    ) -> list[SegmentationPrediction]:
        outputs = self._model_outputs_via_local_onnx(route=route, image_bytes=image_bytes)
        return _segmentation_predictions_from_outputs(
            route=route,
            outputs=outputs,
            max_segments=max_segments,
            score_threshold=score_threshold,
        )

    def _model_outputs_via_local_onnx(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
    ) -> dict[str, _OutputTensor]:
        artifact_path = _deployment_artifact_path(route)
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise PlatformError(
                code="dependency_missing",
                message=(
                    "Triton inference failed and the local ONNX fallback cannot run because "
                    "onnxruntime is not installed."
                ),
                status_code=503,
            ) from exc
        session = self._onnx_sessions.get(str(artifact_path))
        if session is None:
            try:
                session = ort.InferenceSession(
                    str(artifact_path),
                    providers=["CPUExecutionProvider"],
                )
            except Exception as exc:
                raise PlatformError(
                    code="invalid_model_artifact",
                    message="The deployed ONNX artifact cannot be loaded by onnxruntime.",
                    detail=str(exc),
                    status_code=503,
                    metadata={
                        "deployment_release_id": route.deployment_release_id,
                        "artifact_path": str(artifact_path),
                    },
                ) from exc
            self._onnx_sessions[str(artifact_path)] = session
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        if not inputs or not outputs:
            raise PlatformError(
                code="onnx_schema_missing",
                message="The deployed ONNX model does not declare input/output tensors.",
                status_code=500,
                metadata={"deployment_release_id": route.deployment_release_id},
            )
        tensor = _classification_tensor(image_bytes, route)
        return self._run_local_onnx_tensor(session=session, tensor=tensor)

    def _model_outputs_for_tensor_via_local_onnx(
        self,
        *,
        route: ReleaseContext,
        tensor: Any,
    ) -> dict[str, _OutputTensor]:
        artifact_path = _deployment_artifact_path(route)
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise PlatformError(
                code="dependency_missing",
                message="NumPy and onnxruntime are required for temporal ONNX inference.",
                status_code=503,
            ) from exc
        session = self._onnx_sessions.get(str(artifact_path))
        if session is None:
            try:
                session = ort.InferenceSession(
                    str(artifact_path),
                    providers=["CPUExecutionProvider"],
                )
            except Exception as exc:
                raise PlatformError(
                    code="invalid_model_artifact",
                    message="The deployed temporal ONNX artifact cannot be loaded.",
                    detail=str(exc),
                    status_code=503,
                    metadata={"deployment_release_id": route.deployment_release_id},
                ) from exc
            self._onnx_sessions[str(artifact_path)] = session
        return self._run_local_onnx_tensor(session=session, tensor=tensor)

    @staticmethod
    def _run_local_onnx_tensor(*, session: Any, tensor: Any) -> dict[str, _OutputTensor]:
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        if not inputs or not outputs:
            raise PlatformError(
                code="onnx_schema_missing",
                message="The deployed ONNX model does not declare input/output tensors.",
                status_code=500,
            )
        raw_outputs = session.run([output.name for output in outputs], {inputs[0].name: tensor})
        return {
            output.name: _OutputTensor(
                name=output.name,
                shape=_shape_of(raw_output),
                values=_flatten_scores(raw_output),
            )
            for output, raw_output in zip(outputs, raw_outputs, strict=False)
        }


class AnomalibRealtimeBackend:
    def __init__(self, *, warm_ttl_seconds: int) -> None:
        self._warm_ttl_seconds = warm_ttl_seconds
        self._last_used_at: dict[str, float] = {}
        self._inferencers: dict[str, object] = {}

    def is_warm(self, deployment_release_id: str) -> bool:
        last_used = self._last_used_at.get(deployment_release_id)
        if last_used is None:
            return False
        return (time.time() - last_used) <= self._warm_ttl_seconds

    def last_request_at(self, deployment_release_id: str) -> str | None:
        last_used = self._last_used_at.get(deployment_release_id)
        if last_used is None:
            return None
        return datetime.fromtimestamp(last_used, tz=UTC).isoformat().replace("+00:00", "Z")

    def predict(
        self,
        *,
        route: ReleaseContext,
        image_bytes: bytes,
        threshold: float,
        return_mask: bool,
    ) -> dict[str, Any]:
        inferencer = self._inferencer_for_route(route)
        image = _pil_image_from_bytes(image_bytes)
        predict = getattr(inferencer, "predict", None)
        if predict is None:
            raise PlatformError(
                code="anomaly_inferencer_invalid",
                message="The configured Anomalib inferencer does not expose predict().",
                status_code=503,
            )
        result = predict(image=image)
        score = _prediction_score(result)
        anomaly_map = _prediction_map(result)
        if anomaly_map is None:
            raise PlatformError(
                code="anomaly_heatmap_missing",
                message="The deployed anomaly model did not return a heatmap.",
                status_code=500,
            )
        heatmap = _heatmap_from_array(anomaly_map, image_size=image.size)
        is_anomalous = bool(score >= threshold)
        mask = None
        if return_mask:
            raw_mask = _prediction_mask(result)
            if raw_mask is not None:
                mask = _mask_from_array(raw_mask, threshold=threshold, image_size=image.size)
        self._last_used_at[route.deployment_release_id] = time.time()
        return {
            "anomaly_score": score,
            "predicted_label": "anomalous" if is_anomalous else "normal",
            "is_anomalous": is_anomalous,
            "threshold": threshold,
            "heatmap": heatmap,
            "mask": mask,
            "image_width": image.width,
            "image_height": image.height,
        }

    def _inferencer_for_route(self, route: ReleaseContext) -> object:
        cached = self._inferencers.get(route.deployment_release_id)
        if cached is not None:
            return cached
        try:
            from anomalib.deploy import OpenVINOInferencer
        except ImportError as exc:
            raise PlatformError(
                code="dependency_missing",
                message="Anomalib OpenVINO inference dependencies are not installed.",
                status_code=503,
            ) from exc
        artifact_path = _anomaly_artifact_path(route)
        inferencer = OpenVINOInferencer(path=str(artifact_path))
        self._inferencers[route.deployment_release_id] = inferencer
        return inferencer


class BaseGatewayService:
    def __init__(self, settings: PlatformSettings, *, service_name: str, target_path: TargetPath) -> None:
        self.settings = settings
        self.service_name = service_name
        self.target_path = target_path
        self.resolver = ReleaseResolver(settings)
        self.metrics = RuntimeMetrics(
            service_name=service_name,
            target_path=target_path.value,
            window_size=settings.inference_limits.metrics_window_size,
        )
        limit = (
            settings.inference_limits.realtime_max_concurrency
            if target_path == TargetPath.REALTIME
            else settings.inference_limits.batch_max_concurrency
        )
        self.gate = ConcurrencyGate(limit, on_change=self.metrics.inflight_changed)
        self._release_gates: dict[str, ConcurrencyGate] = {}
        configured_release_limit = (
            settings.inference_limits.realtime_per_release_max_concurrency
            if target_path == TargetPath.REALTIME
            else settings.inference_limits.batch_per_release_max_concurrency
        )
        self._release_concurrency_limit = min(limit, configured_release_limit)
        self.backend = TritonInferenceBackend(
            settings=settings,
            target_path=target_path,
            warm_ttl_seconds=settings.inference_limits.warm_ttl_seconds,
        )
        self.anomaly_backend = AnomalibRealtimeBackend(
            warm_ttl_seconds=settings.inference_limits.warm_ttl_seconds
        )

    def close(self) -> None:
        self.backend.close()

    async def list_capabilities(self) -> ModelCapabilitiesResponse:
        releases = await asyncio.to_thread(self._servable_release_contexts)
        items = [
            ModelCapabilitySummary(
                deployment_release_id=release.deployment_release_id,
                target_path=release.target_path,
                active=release.active,
                rollback_candidate=release.rollback_candidate,
                model_family_slug=release.model_family_slug,
                model_version_id=release.model_version_id,
                task_type=release.task_type,
                architecture=release.architecture,
                contract_major=release.contract_major,
                input_schema_ref=release.input_schema_ref,
                output_schema_ref=release.output_schema_ref,
                class_labels=release.class_labels,
                compatible_targets=[
                    TargetPath(item)
                    for item in release.manifest.get("compatible_targets", [self.target_path.value])
                ],
                max_batch_size=int(
                    ((release.manifest.get("hardware_compatibility") or {}).get("max_batch_size") or {}).get(
                        self.target_path.value,
                        1,
                    )
                ),
                confidence_threshold_default=release.confidence_threshold_default,
                preprocessing=dict(release.manifest.get("preprocessing") or {}),
                postprocessing=dict(release.manifest.get("postprocessing") or {}),
                capabilities=dict(release.manifest.get("capabilities") or {}),
                anomaly=dict(release.manifest.get("anomaly") or {}),
            )
            for release in releases
        ]
        return ModelCapabilitiesResponse(items=items, meta=self._meta())

    async def list_status(self) -> ModelStatusResponse:
        backend_statistics, releases = await asyncio.gather(
            asyncio.to_thread(self.backend.model_statistics),
            asyncio.to_thread(self._servable_release_contexts),
        )
        items = [
            ModelStatusSummary(
                route=self._route_ref(release),
                active=release.active,
                rollback_candidate=release.rollback_candidate,
                warm_model=(
                    self.anomaly_backend.is_warm(release.deployment_release_id)
                    if release.task_type == TaskType.ANOMALY
                    else self.backend.is_warm(release.deployment_release_id)
                ),
                overloaded=(
                    self.gate.overloaded() or self._release_gate(release).overloaded()
                ),
                current_inflight=self._release_gate(release).current_inflight,
                concurrency_limit=self._release_gate(release).limit,
                last_request_at=(
                    self.anomaly_backend.last_request_at(release.deployment_release_id)
                    if release.task_type == TaskType.ANOMALY
                    else self.backend.last_request_at(release.deployment_release_id)
                ),
                latency=self.metrics.snapshot(deployment_release_id=release.deployment_release_id),
                backend_statistics=backend_statistics.get(
                    (release.triton_model_name, release.triton_model_version),
                    {},
                ),
            )
            for release in releases
        ]
        return ModelStatusResponse(items=items, meta=self._meta())

    def _servable_release_contexts(self) -> list[ReleaseContext]:
        releases = self.resolver.list_release_contexts(target_path=self.target_path)
        if not releases or all(_uses_simulated_inference(release) for release in releases):
            return releases
        ready_versions = self.backend.ready_model_versions()
        if ready_versions is None:
            return releases
        return [
            release
            for release in releases
            if _uses_simulated_inference(release)
            or (release.triton_model_name, release.triton_model_version) in ready_versions
        ]

    def _resolve_release(
        self,
        *,
        deployment_release_id: str | None,
        missing_active_message: str,
    ) -> ReleaseContext:
        if deployment_release_id:
            release = self.resolver.resolve_release(
                deployment_release_id=deployment_release_id,
                target_path=self.target_path,
            )
            if release is None:
                raise PlatformError(
                    code="deployment_release_not_found",
                    message="The requested deployment release is not available.",
                    detail=deployment_release_id,
                    status_code=404,
                )
            return release
        release = self.resolver.resolve_active_release(target_path=self.target_path)
        if release is None:
            raise PlatformError(
                code="deployment_release_unavailable",
                message=missing_active_message,
                status_code=503,
            )
        return release

    def _release_gate(self, release: ReleaseContext) -> ConcurrencyGate:
        gate = self._release_gates.get(release.deployment_release_id)
        if gate is None:
            gate = ConcurrencyGate(
                min(max(int(self._release_concurrency_limit), 1), self.gate.limit)
            )
            self._release_gates[release.deployment_release_id] = gate
        return gate

    @asynccontextmanager
    async def _admission(
        self,
        *,
        release: ReleaseContext,
        operation: str,
        wait: bool,
        wait_timeout_seconds: float | None = None,
    ):
        release_gate = self._release_gate(release)
        release_admitted = await release_gate.try_acquire(
            wait=wait,
            timeout_seconds=wait_timeout_seconds,
        )
        if not release_admitted:
            self.metrics.record_overload(operation=operation)
            raise PlatformError(
                code="model_overloaded",
                message="The selected model has reached its realtime concurrency limit.",
                detail=release.deployment_release_id,
                status_code=429,
            )
        global_admitted = False
        try:
            global_admitted = await self.gate.try_acquire(
                wait=wait,
                timeout_seconds=wait_timeout_seconds,
            )
            if not global_admitted:
                self.metrics.record_overload(operation=operation)
                raise PlatformError(
                    code="gateway_overloaded",
                    message=(
                        "The inference gateway is overloaded. Retry on the realtime path later."
                    ),
                    status_code=429,
                )
            yield
        finally:
            if global_admitted:
                await self.gate.release()
            await release_gate.release()

    def _meta(self) -> ResponseMeta:
        return ResponseMeta(
            request_id=get_request_id(),
            correlation_id=get_correlation_id(),
            generated_at=utc_now_iso(),
        )

    def _route_ref(self, release: ReleaseContext):
        from cv_contracts import ModelRouteRef
        from cv_status import DeploymentReleaseStatus

        return ModelRouteRef(
            deployment_release_id=release.deployment_release_id,
            target_path=release.target_path,
            release_status=DeploymentReleaseStatus(release.release_status),
            model_family_slug=release.model_family_slug,
            model_version_id=release.model_version_id,
            task_type=release.task_type,
            triton_model_name=release.triton_model_name,
            triton_model_version=release.triton_model_version,
            serving_artifact_format=_serving_artifact_format(release),
            contract_major=release.contract_major,
        )

    async def _run_classification(
        self,
        *,
        payload: RealtimeClassifyRequest,
        route: ReleaseContext,
    ) -> tuple[InferenceTiming, list[ClassificationLabelScore]]:
        return await self._run_classification_from_bytes(
            image_bytes=_decode_image_base64(
                payload.image_base64,
                max_bytes=self.settings.request_limits.max_image_bytes,
            ),
            route=route,
            top_k=payload.top_k,
            timeout_ms=payload.timeout_ms,
        )

    async def _run_classification_from_bytes(
        self,
        *,
        image_bytes: bytes,
        route: ReleaseContext,
        top_k: int,
        timeout_ms: int | None,
    ) -> tuple[InferenceTiming, list[ClassificationLabelScore]]:
        received_at = utc_now_iso()
        _, normalized_bytes, validation_ms, preprocessing_ms, _width, _height = (
            _prepare_image_bytes(
                image_bytes,
                max_bytes=self.settings.request_limits.max_image_bytes,
            )
        )

        routing_ms = 0.0
        inference_started = time.perf_counter()
        timeout_seconds = _timeout_seconds(
            timeout_ms,
            default_ms=self.settings.inference_limits.realtime_timeout_ms,
        )
        try:
            labels = await asyncio.wait_for(
                self.backend.classify(
                    route=route,
                    image_bytes=normalized_bytes,
                    top_k=top_k,
                    timeout_seconds=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise _inference_timeout_error(route, timeout_seconds) from exc
        inference_ms = _elapsed_ms(inference_started)
        postprocessing_started = time.perf_counter()
        postprocessing_ms = _elapsed_ms(postprocessing_started)
        timing = InferenceTiming(
            received_at=received_at,
            validation_ms=validation_ms,
            routing_ms=routing_ms,
            preprocessing_ms=preprocessing_ms,
            inference_ms=inference_ms,
            postprocessing_ms=postprocessing_ms,
            total_ms=round(
                validation_ms + routing_ms + preprocessing_ms + inference_ms + postprocessing_ms,
                3,
            ),
        )
        return timing, labels

    async def _run_detection(
        self,
        *,
        payload: RealtimeDetectRequest,
        route: ReleaseContext,
    ) -> tuple[InferenceTiming, list[DetectionPrediction]]:
        return await self._run_detection_from_bytes(
            image_bytes=_decode_image_base64(
                payload.image_base64,
                max_bytes=self.settings.request_limits.max_image_bytes,
            ),
            route=route,
            max_detections=payload.max_detections,
            score_threshold=payload.score_threshold,
            timeout_ms=payload.timeout_ms,
        )

    async def _run_detection_from_bytes(
        self,
        *,
        image_bytes: bytes,
        route: ReleaseContext,
        max_detections: int,
        score_threshold: float | None,
        timeout_ms: int | None,
    ) -> tuple[InferenceTiming, list[DetectionPrediction]]:
        received_at = utc_now_iso()
        _, normalized_bytes, validation_ms, preprocessing_ms, _width, _height = (
            _prepare_image_bytes(
                image_bytes,
                max_bytes=self.settings.request_limits.max_image_bytes,
            )
        )

        routing_ms = 0.0
        inference_started = time.perf_counter()
        timeout_seconds = _timeout_seconds(
            timeout_ms,
            default_ms=self.settings.inference_limits.realtime_timeout_ms,
        )
        threshold = score_threshold or route.confidence_threshold_default
        try:
            detections = await asyncio.wait_for(
                self.backend.detect(
                    route=route,
                    image_bytes=normalized_bytes,
                    max_detections=max_detections,
                    score_threshold=threshold,
                    timeout_seconds=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise _inference_timeout_error(route, timeout_seconds) from exc
        inference_ms = _elapsed_ms(inference_started)
        postprocessing_started = time.perf_counter()
        postprocessing_ms = _elapsed_ms(postprocessing_started)
        timing = InferenceTiming(
            received_at=received_at,
            validation_ms=validation_ms,
            routing_ms=routing_ms,
            preprocessing_ms=preprocessing_ms,
            inference_ms=inference_ms,
            postprocessing_ms=postprocessing_ms,
            total_ms=round(
                validation_ms + routing_ms + preprocessing_ms + inference_ms + postprocessing_ms,
                3,
            ),
        )
        return timing, detections

    async def _run_segmentation(
        self,
        *,
        payload: RealtimeSegmentRequest,
        route: ReleaseContext,
    ) -> tuple[InferenceTiming, int, int, list[SegmentationPrediction]]:
        return await self._run_segmentation_from_bytes(
            image_bytes=_decode_image_base64(
                payload.image_base64,
                max_bytes=self.settings.request_limits.max_image_bytes,
            ),
            route=route,
            max_segments=payload.max_segments,
            score_threshold=payload.score_threshold,
            timeout_ms=payload.timeout_ms,
        )

    async def _run_segmentation_from_bytes(
        self,
        *,
        image_bytes: bytes,
        route: ReleaseContext,
        max_segments: int,
        score_threshold: float | None,
        timeout_ms: int | None,
    ) -> tuple[InferenceTiming, int, int, list[SegmentationPrediction]]:
        received_at = utc_now_iso()
        _, normalized_bytes, validation_ms, preprocessing_ms, width, height = (
            _prepare_image_bytes(
                image_bytes,
                max_bytes=self.settings.request_limits.max_image_bytes,
            )
        )

        routing_ms = 0.0
        timeout_seconds = _timeout_seconds(
            timeout_ms,
            default_ms=self.settings.inference_limits.realtime_timeout_ms,
        )
        threshold = score_threshold or route.confidence_threshold_default
        backend_started = time.perf_counter()
        backend_timing = _BackendPhaseTiming()
        timing_token = _backend_phase_timing.set(backend_timing)
        try:
            segments = await asyncio.wait_for(
                self.backend.segment(
                    route=route,
                    image_bytes=normalized_bytes,
                    max_segments=max_segments,
                    score_threshold=threshold,
                    timeout_seconds=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise _inference_timeout_error(route, timeout_seconds) from exc
        finally:
            _backend_phase_timing.reset(timing_token)
        backend_total_ms = _elapsed_ms(backend_started)
        if backend_timing.measured:
            preprocessing_ms += backend_timing.preprocessing_ms
            inference_ms = backend_timing.inference_ms
            postprocessing_ms = backend_timing.postprocessing_ms
        else:
            inference_ms = backend_total_ms
            postprocessing_ms = 0.0
        timing = InferenceTiming(
            received_at=received_at,
            validation_ms=validation_ms,
            routing_ms=routing_ms,
            preprocessing_ms=preprocessing_ms,
            inference_ms=inference_ms,
            postprocessing_ms=postprocessing_ms,
            total_ms=round(
                validation_ms + routing_ms + preprocessing_ms + inference_ms + postprocessing_ms,
                3,
            ),
            shared_memory_transport=backend_timing.shared_memory_transport,
        )
        return timing, width, height, segments

    async def _run_anomaly(
        self,
        *,
        payload: RealtimeAnomalyRequest,
        route: ReleaseContext,
    ) -> tuple[InferenceTiming, dict[str, Any]]:
        return await self._run_anomaly_from_bytes(
            image_bytes=_decode_image_base64(
                payload.image_base64,
                max_bytes=self.settings.request_limits.max_image_bytes,
            ),
            route=route,
            threshold=payload.threshold,
            return_mask=payload.return_mask,
            timeout_ms=payload.timeout_ms,
        )

    async def _run_anomaly_from_bytes(
        self,
        *,
        image_bytes: bytes,
        route: ReleaseContext,
        threshold: float | None,
        return_mask: bool,
        timeout_ms: int | None,
    ) -> tuple[InferenceTiming, dict[str, Any]]:
        received_at = utc_now_iso()
        _, normalized_bytes, validation_ms, preprocessing_ms, _width, _height = (
            _prepare_image_bytes(
                image_bytes,
                max_bytes=self.settings.request_limits.max_image_bytes,
            )
        )

        routing_ms = 0.0
        inference_started = time.perf_counter()
        timeout_seconds = _timeout_seconds(
            timeout_ms,
            default_ms=self.settings.inference_limits.realtime_timeout_ms,
        )
        if threshold is None:
            threshold = _anomaly_threshold(route)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.anomaly_backend.predict,
                    route=route,
                    image_bytes=normalized_bytes,
                    threshold=threshold,
                    return_mask=return_mask,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise _inference_timeout_error(route, timeout_seconds) from exc
        inference_ms = _elapsed_ms(inference_started)
        postprocessing_started = time.perf_counter()
        postprocessing_ms = _elapsed_ms(postprocessing_started)
        timing = InferenceTiming(
            received_at=received_at,
            validation_ms=validation_ms,
            routing_ms=routing_ms,
            preprocessing_ms=preprocessing_ms,
            inference_ms=inference_ms,
            postprocessing_ms=postprocessing_ms,
            total_ms=round(
                validation_ms + routing_ms + preprocessing_ms + inference_ms + postprocessing_ms,
                3,
            ),
        )
        return timing, result


class RealtimeGatewayService(BaseGatewayService):
    async def classify(self, payload: RealtimeClassifyRequest) -> ClassificationInferenceResponse:
        route = await asyncio.to_thread(
            self._resolve_release,
            deployment_release_id=payload.deployment_release_id,
            missing_active_message="No active realtime deployment is available.",
        )
        if route.task_type != TaskType.CLASSIFICATION:
            raise PlatformError(
                code="task_type_mismatch",
                message="The selected deployment release does not support classification.",
                detail=route.task_type.value,
                status_code=409,
            )
        warm_before = self.backend.is_warm(route.deployment_release_id)
        started = time.perf_counter()
        async with self._admission(
            release=route,
            operation="classify",
            wait=True,
            wait_timeout_seconds=_admission_wait_seconds(
                payload.timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            ),
        ):
            timing, labels = await self._run_classification(payload=payload, route=route)
        total_ms = _elapsed_ms(started)
        self.metrics.record_success(
            operation="classify",
            deployment_release_id=route.deployment_release_id,
            total_ms=total_ms,
        )
        return ClassificationInferenceResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            deployment_target=payload.deployment_target,
            route=self._route_ref(route),
            architecture=route.architecture,
            warm_model=warm_before,
            timing=timing,
            labels=labels,
        )

    async def classify_image_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        top_k: int = 1,
    ) -> ClassificationInferenceResponse:
        route = await asyncio.to_thread(
            self._resolve_release,
            deployment_release_id=deployment_release_id,
            missing_active_message="No active realtime deployment is available.",
        )
        if route.task_type != TaskType.CLASSIFICATION:
            raise PlatformError(
                code="task_type_mismatch",
                message="The selected deployment release does not support classification.",
                detail=route.task_type.value,
                status_code=409,
            )
        warm_before = self.backend.is_warm(route.deployment_release_id)
        started = time.perf_counter()
        async with self._admission(
            release=route,
            operation="classify",
            wait=True,
            wait_timeout_seconds=_admission_wait_seconds(
                timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            ),
        ):
            timing, labels = await self._run_classification_from_bytes(
                image_bytes=image_bytes,
                route=route,
                top_k=top_k,
                timeout_ms=timeout_ms,
            )
        total_ms = _elapsed_ms(started)
        self.metrics.record_success(
            operation="classify",
            deployment_release_id=route.deployment_release_id,
            total_ms=total_ms,
        )
        return ClassificationInferenceResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            deployment_target=deployment_target,
            route=self._route_ref(route),
            architecture=route.architecture,
            warm_model=warm_before,
            timing=timing,
            labels=labels,
        )

    async def detect_image_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        max_detections: int = 100,
        score_threshold: float = 0.35,
    ) -> DetectionInferenceResponse:
        route = await asyncio.to_thread(
            self._resolve_release,
            deployment_release_id=deployment_release_id,
            missing_active_message="No active realtime deployment is available.",
        )
        if route.task_type not in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
            raise PlatformError(
                code="task_type_mismatch",
                message="The selected deployment release does not support detection.",
                detail=route.task_type.value,
                status_code=409,
            )
        warm_before = self.backend.is_warm(route.deployment_release_id)
        started = time.perf_counter()
        async with self._admission(
            release=route,
            operation="detect",
            wait=True,
            wait_timeout_seconds=_admission_wait_seconds(
                timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            ),
        ):
            timing, detections = await self._run_detection_from_bytes(
                image_bytes=image_bytes,
                route=route,
                max_detections=max_detections,
                score_threshold=score_threshold,
                timeout_ms=timeout_ms,
            )
        self.metrics.record_success(
            operation="detect",
            deployment_release_id=route.deployment_release_id,
            total_ms=_elapsed_ms(started),
        )
        return DetectionInferenceResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            deployment_target=deployment_target,
            route=self._route_ref(route),
            architecture=route.architecture,
            warm_model=warm_before,
            timing=timing,
            detections=detections,
        )

    async def segment_image_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        max_segments: int = 50,
        score_threshold: float = 0.35,
    ) -> SegmentationInferenceResponse:
        route = await asyncio.to_thread(
            self._resolve_release,
            deployment_release_id=deployment_release_id,
            missing_active_message="No active realtime deployment is available.",
        )
        if route.task_type != TaskType.SEGMENTATION:
            raise PlatformError(
                code="task_type_mismatch",
                message="The selected deployment release does not support segmentation.",
                detail=route.task_type.value,
                status_code=409,
            )
        warm_before = self.backend.is_warm(route.deployment_release_id)
        started = time.perf_counter()
        async with self._admission(
            release=route,
            operation="segment",
            wait=True,
            wait_timeout_seconds=_admission_wait_seconds(
                timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            ),
        ):
            timing, image_width, image_height, segments = (
                await self._run_segmentation_from_bytes(
                    image_bytes=image_bytes,
                    route=route,
                    max_segments=max_segments,
                    score_threshold=score_threshold,
                    timeout_ms=timeout_ms,
                )
            )
        self.metrics.record_success(
            operation="segment",
            deployment_release_id=route.deployment_release_id,
            total_ms=_elapsed_ms(started),
        )
        return SegmentationInferenceResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            deployment_target=deployment_target,
            route=self._route_ref(route),
            architecture=route.architecture,
            warm_model=warm_before,
            timing=timing,
            image_width=image_width,
            image_height=image_height,
            segments=segments,
        )

    async def anomaly_image_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        threshold: float | None = None,
        return_mask: bool = False,
    ) -> AnomalyInferenceResponse:
        route = await asyncio.to_thread(
            self._resolve_release,
            deployment_release_id=deployment_release_id,
            missing_active_message="No active realtime deployment is available.",
        )
        if route.task_type != TaskType.ANOMALY:
            raise PlatformError(
                code="task_type_mismatch",
                message="The selected deployment release does not support anomaly inference.",
                detail=route.task_type.value,
                status_code=409,
            )
        _ensure_anomaly_runtime_support(route)
        warm_before = self.anomaly_backend.is_warm(route.deployment_release_id)
        started = time.perf_counter()
        async with self._admission(
            release=route,
            operation="anomaly",
            wait=True,
            wait_timeout_seconds=_admission_wait_seconds(
                timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            ),
        ):
            timing, result = await self._run_anomaly_from_bytes(
                image_bytes=image_bytes,
                route=route,
                threshold=threshold,
                return_mask=return_mask,
                timeout_ms=timeout_ms,
            )
        self.metrics.record_success(
            operation="anomaly",
            deployment_release_id=route.deployment_release_id,
            total_ms=_elapsed_ms(started),
        )
        return AnomalyInferenceResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            deployment_target=deployment_target,
            route=self._route_ref(route),
            architecture=route.architecture,
            warm_model=warm_before,
            timing=timing,
            anomaly_score=float(result["anomaly_score"]),
            predicted_label=result["predicted_label"],
            is_anomalous=bool(result["is_anomalous"]),
            threshold=float(result["threshold"]),
            heatmap=result["heatmap"],
            mask=result.get("mask"),
            image_width=int(result["image_width"]),
            image_height=int(result["image_height"]),
            model_metadata=dict(route.manifest.get("anomaly") or {}),
        )

    async def detect(self, payload: RealtimeDetectRequest) -> DetectionInferenceResponse:
        route = await asyncio.to_thread(
            self._resolve_release,
            deployment_release_id=payload.deployment_release_id,
            missing_active_message="No active realtime deployment is available.",
        )
        if route.task_type not in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
            raise PlatformError(
                code="task_type_mismatch",
                message="The selected deployment release does not support detection.",
                detail=route.task_type.value,
                status_code=409,
            )
        warm_before = self.backend.is_warm(route.deployment_release_id)
        started = time.perf_counter()
        async with self._admission(
            release=route,
            operation="detect",
            wait=True,
            wait_timeout_seconds=_admission_wait_seconds(
                payload.timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            ),
        ):
            timing, detections = await self._run_detection(payload=payload, route=route)
        total_ms = _elapsed_ms(started)
        self.metrics.record_success(
            operation="detect",
            deployment_release_id=route.deployment_release_id,
            total_ms=total_ms,
        )
        return DetectionInferenceResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            deployment_target=payload.deployment_target,
            route=self._route_ref(route),
            architecture=route.architecture,
            warm_model=warm_before,
            timing=timing,
            detections=detections,
        )

    async def segment(self, payload: RealtimeSegmentRequest) -> SegmentationInferenceResponse:
        route = await asyncio.to_thread(
            self._resolve_release,
            deployment_release_id=payload.deployment_release_id,
            missing_active_message="No active realtime deployment is available.",
        )
        if route.task_type != TaskType.SEGMENTATION:
            raise PlatformError(
                code="task_type_mismatch",
                message="The selected deployment release does not support segmentation.",
                detail=route.task_type.value,
                status_code=409,
            )
        warm_before = self.backend.is_warm(route.deployment_release_id)
        started = time.perf_counter()
        async with self._admission(
            release=route,
            operation="segment",
            wait=True,
            wait_timeout_seconds=_admission_wait_seconds(
                payload.timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            ),
        ):
            timing, image_width, image_height, segments = await self._run_segmentation(
                payload=payload,
                route=route,
            )
        total_ms = _elapsed_ms(started)
        self.metrics.record_success(
            operation="segment",
            deployment_release_id=route.deployment_release_id,
            total_ms=total_ms,
        )
        return SegmentationInferenceResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            deployment_target=payload.deployment_target,
            route=self._route_ref(route),
            architecture=route.architecture,
            warm_model=warm_before,
            timing=timing,
            image_width=image_width,
            image_height=image_height,
            segments=segments,
        )

    async def anomaly(self, payload: RealtimeAnomalyRequest) -> AnomalyInferenceResponse:
        route = await asyncio.to_thread(
            self._resolve_release,
            deployment_release_id=payload.deployment_release_id,
            missing_active_message="No active realtime deployment is available.",
        )
        if route.task_type != TaskType.ANOMALY:
            raise PlatformError(
                code="task_type_mismatch",
                message="The selected deployment release does not support anomaly inference.",
                detail=route.task_type.value,
                status_code=409,
            )
        _ensure_anomaly_runtime_support(route)
        warm_before = self.anomaly_backend.is_warm(route.deployment_release_id)
        started = time.perf_counter()
        async with self._admission(
            release=route,
            operation="anomaly",
            wait=True,
            wait_timeout_seconds=_admission_wait_seconds(
                payload.timeout_ms,
                default_ms=self.settings.inference_limits.realtime_timeout_ms,
            ),
        ):
            timing, result = await self._run_anomaly(payload=payload, route=route)
        total_ms = _elapsed_ms(started)
        self.metrics.record_success(
            operation="anomaly",
            deployment_release_id=route.deployment_release_id,
            total_ms=total_ms,
        )
        return AnomalyInferenceResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            deployment_target=payload.deployment_target,
            route=self._route_ref(route),
            architecture=route.architecture,
            warm_model=warm_before,
            timing=timing,
            anomaly_score=float(result["anomaly_score"]),
            predicted_label=result["predicted_label"],
            is_anomalous=bool(result["is_anomalous"]),
            threshold=float(result["threshold"]),
            heatmap=result["heatmap"],
            mask=result.get("mask"),
            image_width=int(result["image_width"]),
            image_height=int(result["image_height"]),
            model_metadata=dict(route.manifest.get("anomaly") or {}),
        )

    async def temporal_sequence(
        self,
        payload: RealtimeTemporalSequenceRequest,
    ) -> TemporalSequenceInferenceResponse:
        routing_started = time.perf_counter()
        route = await asyncio.to_thread(
            self._resolve_release,
            deployment_release_id=payload.deployment_release_id,
            missing_active_message="No active realtime deployment is available.",
        )
        routing_ms = _elapsed_ms(routing_started)
        if route.task_type != TaskType.TEMPORAL_SEQUENCE:
            raise PlatformError(
                code="task_type_mismatch",
                message="The selected deployment release does not support temporal sequences.",
                detail=route.task_type.value,
                status_code=409,
            )
        received_at = utc_now_iso()
        preprocessing_started = time.perf_counter()
        tensor, contracts = _prepare_temporal_model_tensor(route=route, payload=payload)
        preprocessing_ms = _elapsed_ms(preprocessing_started)
        warm_before = self.backend.is_warm(route.deployment_release_id)
        timeout_seconds = _timeout_seconds(
            payload.timeout_ms,
            default_ms=self.settings.inference_limits.realtime_timeout_ms,
        )
        started = time.perf_counter()
        backend_timing = _BackendPhaseTiming()
        timing_token = _backend_phase_timing.set(backend_timing)
        try:
            async with self._admission(
                release=route,
                operation="temporal_sequence",
                wait=True,
                wait_timeout_seconds=_admission_wait_seconds(
                    payload.timeout_ms,
                    default_ms=self.settings.inference_limits.realtime_timeout_ms,
                ),
            ):
                try:
                    outputs = await asyncio.wait_for(
                        self.backend.temporal_sequence(
                            route=route,
                            tensor=tensor,
                            timeout_seconds=timeout_seconds,
                        ),
                        timeout=timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise _inference_timeout_error(route, timeout_seconds) from exc
        finally:
            _backend_phase_timing.reset(timing_token)
        parsed = _temporal_prediction_from_outputs(outputs, contracts=contracts)
        backend_total_ms = _elapsed_ms(started)
        inference_ms = (
            backend_timing.inference_ms if backend_timing.measured else backend_total_ms
        )
        postprocessing_ms = backend_timing.postprocessing_ms
        timing = InferenceTiming(
            received_at=received_at,
            validation_ms=0.0,
            routing_ms=routing_ms,
            preprocessing_ms=preprocessing_ms + backend_timing.preprocessing_ms,
            inference_ms=inference_ms,
            postprocessing_ms=postprocessing_ms,
            total_ms=round(
                routing_ms
                + preprocessing_ms
                + backend_timing.preprocessing_ms
                + inference_ms
                + postprocessing_ms,
                3,
            ),
            shared_memory_transport=backend_timing.shared_memory_transport,
        )
        self.metrics.record_success(
            operation="temporal_sequence",
            deployment_release_id=route.deployment_release_id,
            total_ms=timing.total_ms,
        )
        contract = contracts["temporal_contract"]
        angle = contracts["angle_contract"]
        return TemporalSequenceInferenceResponse(
            request_id=get_request_id() or "unknown",
            correlation_id=get_correlation_id(),
            deployment_target=payload.deployment_target,
            route=self._route_ref(route),
            architecture=route.architecture,
            warm_model=warm_before,
            timing=timing,
            sequence_id=payload.sequence_id,
            track_id=payload.track_id,
            sequence_length=len(payload.frames),
            last_frame_index=payload.frames[-1].frame_index,
            decision_frame_index=payload.decision_frame_index,
            feature_set_id=str(contract["feature_set_id"]),
            feature_schema_digest=str(contract["schema_digest"]),
            scaler_sha256=str(contract["scaler_sha256"]),
            snapshot_id=str(contract["snapshot_id"]),
            feature_schema_version=payload.feature_schema_version,
            rotation_schema_version=payload.rotation_schema_version,
            calibration_schema_version=payload.calibration_schema_version,
            rotation_calibration_id=payload.rotation_calibration_id,
            rotation_calibration_hash=payload.rotation_calibration_hash,
            rotation_viewpoint_convention=str(angle["rotation_viewpoint_convention"]),
            positive_rotation_direction=str(angle["positive_rotation_direction"]),
            bow_relevance_probability=parsed["bow_relevance_probability"],
            target_observed_probability=parsed["target_observed_probability"],
            angle_sin_2e=parsed["angle_sin_2e"],
            angle_cos_2e=parsed["angle_cos_2e"],
            signed_correction_deg=parsed["signed_correction_deg"],
            raw_angle_confidence=parsed["raw_angle_confidence"],
            angle_confidence=parsed["angle_confidence"],
            confidence_calibration_valid=bool(
                contracts["confidence_calibration"].get("valid")
            ),
            final_quality_probabilities=parsed["final_quality_probabilities"],
            deadband_deg=float(angle["deadband_deg"]),
            estimate_method=(
                "gru" if route.architecture.family.lower() == "gru" else "tcn"
            ),
            reason_codes=parsed["reason_codes"],
        )

    async def predict(self, payload: RealtimeInferenceRequest) -> RealtimeInferenceResponse:
        route = await asyncio.to_thread(
            self._resolve_release,
            deployment_release_id=payload.deployment_release_id,
            missing_active_message="No active realtime deployment is available.",
        )
        if route.task_type == TaskType.CLASSIFICATION:
            response = await self.classify(
                RealtimeClassifyRequest(
                    deployment_target=payload.deployment_target,
                    image_base64=payload.image_base64,
                    deployment_release_id=payload.deployment_release_id,
                    timeout_ms=payload.timeout_ms,
                    top_k=getattr(payload, "top_k", 3),
                )
            )
            return RealtimeInferenceResponse(
                request_id=response.request_id,
                correlation_id=response.correlation_id,
                deployment_target=response.deployment_target,
                route=response.route,
                architecture=response.architecture,
                warm_model=response.warm_model,
                timing=response.timing,
                task_type=TaskType.CLASSIFICATION,
                labels=response.labels,
                detections=[],
                image_width=None,
                image_height=None,
                segments=[],
            )
        if route.task_type == TaskType.SEGMENTATION:
            response = await self.segment(
                RealtimeSegmentRequest(
                    deployment_target=payload.deployment_target,
                    image_base64=payload.image_base64,
                    deployment_release_id=payload.deployment_release_id,
                    timeout_ms=payload.timeout_ms,
                )
            )
            return RealtimeInferenceResponse(
                request_id=response.request_id,
                correlation_id=response.correlation_id,
                deployment_target=response.deployment_target,
                route=response.route,
                architecture=response.architecture,
                warm_model=response.warm_model,
                timing=response.timing,
                task_type=TaskType.SEGMENTATION,
                labels=[],
                detections=[],
                image_width=response.image_width,
                image_height=response.image_height,
                segments=response.segments,
            )
        if route.task_type == TaskType.ANOMALY:
            response = await self.anomaly(
                RealtimeAnomalyRequest(
                    deployment_target=payload.deployment_target,
                    image_base64=payload.image_base64,
                    deployment_release_id=payload.deployment_release_id,
                    timeout_ms=payload.timeout_ms,
                )
            )
            return RealtimeInferenceResponse(
                request_id=response.request_id,
                correlation_id=response.correlation_id,
                deployment_target=response.deployment_target,
                route=response.route,
                architecture=response.architecture,
                warm_model=response.warm_model,
                timing=response.timing,
                task_type=TaskType.ANOMALY,
                labels=[],
                detections=[],
                image_width=response.image_width,
                image_height=response.image_height,
                segments=[],
                anomaly_score=response.anomaly_score,
                predicted_label=response.predicted_label,
                is_anomalous=response.is_anomalous,
                threshold=response.threshold,
                heatmap=response.heatmap,
                mask=response.mask,
            )
        if route.task_type not in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
            raise PlatformError(
                code="task_type_mismatch",
                message="The generic image prediction endpoint does not support this task type.",
                detail=route.task_type.value,
                status_code=409,
            )
        detect_response = await self.detect(
            RealtimeDetectRequest(
                deployment_target=payload.deployment_target,
                image_base64=payload.image_base64,
                deployment_release_id=payload.deployment_release_id,
                timeout_ms=payload.timeout_ms,
            )
        )
        return RealtimeInferenceResponse(
            request_id=detect_response.request_id,
            correlation_id=detect_response.correlation_id,
            deployment_target=detect_response.deployment_target,
            route=detect_response.route,
            architecture=detect_response.architecture,
            warm_model=detect_response.warm_model,
            timing=detect_response.timing,
            task_type=detect_response.route.task_type,
            labels=[],
            detections=detect_response.detections,
            image_width=None,
            image_height=None,
            segments=[],
        )


class BatchGatewayService(BaseGatewayService):
    def submit_batch_job(
        self,
        payload: BatchJobCreateRequest,
        *,
        submitted_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> BatchJobSummary:
        effective_idempotency_key = idempotency_key or payload.idempotency_key
        request_hash = build_batch_request_hash(payload)
        if effective_idempotency_key:
            with session_scope(self.settings) as session:
                try:
                    cached = read_idempotency_response(
                        session,
                        scope="batch.job.submit",
                        key=effective_idempotency_key,
                        request_hash=request_hash,
                    )
                except ValueError as exc:
                    raise PlatformError(
                        code="idempotency_conflict",
                        message="The supplied Idempotency-Key conflicts with a previous batch job request.",
                        detail=str(exc),
                        status_code=409,
                    ) from exc
            if cached is not None:
                return BatchJobSummary.model_validate(cached)

        route = self._resolve_release(
            deployment_release_id=payload.deployment_release_id,
            missing_active_message="No active batch deployment is available.",
        )
        input_manifest_uri = self._materialize_submission_manifest(payload)
        with session_scope(self.settings) as session:
            row = create_batch_job_request(
                session,
                payload=payload,
                deployment_release_id=route.deployment_release_id,
                input_manifest_uri=input_manifest_uri,
                submitted_by=submitted_by,
                source_service=self.service_name,
            )
            summary = BatchJobSummary(
                batch_job_id=row.id,
                deployment_target=payload.deployment_target,
                deployment_release_id=route.deployment_release_id,
                status=BatchJobStatus(row.status),
                created_at=_iso(row.created_at) or utc_now_iso(),
                progress=BatchJobProgress(
                    total_items=row.total_items,
                    completed_items=0,
                    failed_items=0,
                    total_chunks=0,
                    queued_chunks=0,
                    running_chunks=0,
                    completed_chunks=0,
                    failed_chunks=0,
                    progress_percent=0,
                ),
            )
            if effective_idempotency_key:
                write_idempotency_response(
                    session,
                    scope="batch.job.submit",
                    key=effective_idempotency_key,
                    request_hash=request_hash,
                    response_code=202,
                    response_body=summary.model_dump(mode="json"),
                )
        self._batch_queue().enqueue(
            "batch_worker.jobs.materialize_batch_input",
            batch_job_id=summary.batch_job_id,
            retry=Retry(max=3, interval=[5, 30, 120]),
        )
        return summary

    def list_jobs(self) -> list[BatchJobSummary]:
        with session_scope(self.settings) as session:
            return list_batch_jobs(session, settings=self.settings)

    def get_job_detail(self, batch_job_id: str) -> BatchJobDetail:
        with session_scope(self.settings) as session:
            detail = get_batch_job_detail(session, settings=self.settings, batch_job_id=batch_job_id)
        if detail is None:
            raise PlatformError(
                code="batch_job_not_found",
                message="The requested batch job does not exist.",
                detail=batch_job_id,
                status_code=404,
            )
        return detail

    def get_job_results(self, batch_job_id: str) -> BatchResultResponse:
        with session_scope(self.settings) as session:
            payload = get_batch_result_response(session, batch_job_id=batch_job_id)
        if payload is None:
            raise PlatformError(
                code="batch_results_unavailable",
                message="The batch job results are not available yet.",
                detail=batch_job_id,
                status_code=404,
            )
        return payload

    def materialize_batch_input(self, batch_job_id: str) -> dict[str, Any]:
        with session_scope(self.settings) as session:
            row = get_batch_job_record(session, batch_job_id)
            if row is None:
                raise ValueError(f"Batch job {batch_job_id} does not exist.")
            source_payload = _read_manifest_payload(
                row.input_manifest_uri,
                max_bytes=self.settings.request_limits.batch_manifest_max_bytes,
                allowed_root=self.settings.storage.batch_root(),
            )
            payload = _payload_items(source_payload)
            canonical_path = self.settings.storage.batch_job_input_manifest(batch_job_id)
            _write_json(
                canonical_path,
                {
                    "schema_version": "inference.batch.input-manifest.v1",
                    "deployment_target": source_payload.get("deployment_target", TargetPath.BATCH.value),
                    "deployment_release_id": row.deployment_release_id,
                    "items": [item.model_dump(mode="json") for item in payload],
                },
            )
            update_batch_job_state(
                session,
                batch_job_id=batch_job_id,
                status=BatchJobStatus.QUEUED,
                source_service=self.service_name,
                actor_type="system",
                actor_id=self.service_name,
                total_items=len(payload),
                reason="Batch input manifest materialized.",
            )
            row.input_manifest_uri = str(canonical_path)
            session.flush()
        return self.split_batch_into_chunks(batch_job_id)

    def split_batch_into_chunks(self, batch_job_id: str) -> dict[str, Any]:
        input_path = self.settings.storage.batch_job_input_manifest(batch_job_id)
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        items = [BatchInputItem.model_validate(item) for item in payload.get("items", [])]
        chunk_size = self.settings.inference_limits.batch_chunk_size
        chunks_payload: list[dict[str, Any]] = []
        for chunk_no, start_index in enumerate(range(0, len(items), chunk_size), start=1):
            chunk_items = items[start_index : start_index + chunk_size]
            chunk_path = self.settings.storage.batch_job_chunk_input(batch_job_id, chunk_no)
            _write_json(
                chunk_path,
                {
                    "schema_version": "inference.batch.chunk-input.v1",
                    "batch_job_id": batch_job_id,
                    "chunk_no": chunk_no,
                    "items": [item.model_dump(mode="json") for item in chunk_items],
                },
            )
            chunks_payload.append({"chunk_no": chunk_no, "input_uri": str(chunk_path)})

        with session_scope(self.settings) as session:
            replace_batch_chunks(session, batch_job_id=batch_job_id, chunks=chunks_payload)
            progress = refresh_batch_job_progress(session, batch_job_id=batch_job_id)
            update_batch_job_state(
                session,
                batch_job_id=batch_job_id,
                status=BatchJobStatus.QUEUED,
                source_service=self.service_name,
                actor_type="system",
                actor_id=self.service_name,
                total_items=progress.total_items,
                completed_items=0,
                failed_items=0,
                reason="Batch job split into chunked work items.",
            )
        queue = self._batch_queue()
        for chunk in chunks_payload:
            queue.enqueue(
                "batch_worker.jobs.run_batch_chunk",
                batch_job_id=batch_job_id,
                chunk_no=chunk["chunk_no"],
                retry=Retry(max=2, interval=[5, 20]),
            )
        return {
            "batch_job_id": batch_job_id,
            "chunk_count": len(chunks_payload),
            "status": BatchJobStatus.QUEUED.value,
        }

    async def run_batch_chunk(self, batch_job_id: str, chunk_no: int) -> dict[str, Any]:
        started = time.perf_counter()
        with session_scope(self.settings) as session:
            chunk = session.scalar(
                select(BatchJobChunk).where(
                    BatchJobChunk.batch_job_id == batch_job_id,
                    BatchJobChunk.chunk_no == chunk_no,
                )
            )
            job = get_batch_job_record(session, batch_job_id)
            if chunk is None or job is None:
                raise ValueError(f"Batch chunk {batch_job_id}:{chunk_no} does not exist.")
            if chunk.status == BatchChunkStatus.COMPLETED.value:
                return {
                    "batch_job_id": batch_job_id,
                    "chunk_no": chunk_no,
                    "status": BatchChunkStatus.COMPLETED.value,
                    "finished_at": _iso(chunk.finished_at),
                }
            update_batch_chunk_state(
                session,
                chunk_id=chunk.id,
                status=BatchChunkStatus.RUNNING,
            )
            update_batch_job_state(
                session,
                batch_job_id=batch_job_id,
                status=BatchJobStatus.RUNNING,
                source_service=self.service_name,
                actor_type="system",
                actor_id=self.service_name,
                reason=f"Chunk {chunk_no} started.",
            )
            chunk_path = Path(chunk.input_uri)
            route = self._resolve_release(
                deployment_release_id=job.deployment_release_id,
                missing_active_message="No active batch deployment is available.",
            )

        chunk_payload = json.loads(chunk_path.read_text(encoding="utf-8"))
        items = [BatchInputItem.model_validate(item) for item in chunk_payload.get("items", [])]
        results: list[BatchResultRecord] = []
        success_count = 0
        failure_count = 0
        async with self._admission(
            release=route,
            operation="batch_chunk",
            wait=True,
        ):
            for item in items:
                try:
                    result = await self._run_batch_item(route=route, item=item)
                    results.append(result)
                    success_count += 1
                except Exception as exc:  # noqa: BLE001
                    failure_count += 1
                    results.append(
                        BatchResultRecord(
                            item_id=item.item_id,
                            external_ref=item.external_ref,
                            status="failed",
                            error=str(exc),
                        )
                    )
        output_path = self.settings.storage.batch_job_chunk_output(batch_job_id, chunk_no)
        _write_json(
            output_path,
            {
                "schema_version": "inference.batch.chunk-output.v1",
                "batch_job_id": batch_job_id,
                "chunk_no": chunk_no,
                "items": [item.model_dump(mode="json") for item in results],
            },
        )
        with session_scope(self.settings) as session:
            row = get_batch_job_record(session, batch_job_id)
            if row is None:
                raise ValueError(f"Batch job {batch_job_id} does not exist.")
            update_batch_chunk_state(
                session,
                chunk_id=chunk.id,
                status=(
                    BatchChunkStatus.COMPLETED
                    if success_count > 0 or failure_count == 0
                    else BatchChunkStatus.FAILED
                ),
                output_uri=str(output_path),
            )
            update_batch_job_state(
                session,
                batch_job_id=batch_job_id,
                status=BatchJobStatus.RUNNING,
                source_service=self.service_name,
                actor_type="system",
                actor_id=self.service_name,
                completed_items=row.completed_items + success_count,
                failed_items=row.failed_items + failure_count,
                reason=f"Chunk {chunk_no} finished.",
            )
            progress = refresh_batch_job_progress(session, batch_job_id=batch_job_id)
        self.metrics.record_success(
            operation="batch_chunk",
            deployment_release_id=route.deployment_release_id,
            total_ms=_elapsed_ms(started),
        )
        if progress.total_chunks > 0 and (
            progress.completed_chunks + progress.failed_chunks == progress.total_chunks
        ):
            self.aggregate_batch_results(batch_job_id)
            self.publish_batch_outputs(batch_job_id)
        return {
            "batch_job_id": batch_job_id,
            "chunk_no": chunk_no,
            "status": BatchChunkStatus.COMPLETED.value,
            "success_count": success_count,
            "failure_count": failure_count,
        }

    def aggregate_batch_results(self, batch_job_id: str) -> dict[str, Any]:
        detail = self.get_job_detail(batch_job_id)
        route = self._resolve_release(
            deployment_release_id=detail.deployment_release_id,
            missing_active_message="No active batch deployment is available.",
        )
        items: list[BatchResultRecord] = []
        incomplete_chunk_nos = [
            chunk.chunk_no for chunk in detail.chunks if chunk.status != BatchChunkStatus.COMPLETED
        ]
        for chunk in detail.chunks:
            if not chunk.output_uri:
                continue
            payload = json.loads(Path(chunk.output_uri).read_text(encoding="utf-8"))
            items.extend(
                BatchResultRecord.model_validate(item) for item in payload.get("items", [])
            )
        latencies = [
            item.timing.total_ms for item in items if item.timing is not None and item.status == "completed"
        ]
        metrics = _latency_metrics(latencies)
        result_status = (
            BatchJobStatus.FAILED
            if incomplete_chunk_nos
            else (
                BatchJobStatus.COMPLETED
                if any(item.status == "completed" for item in items) or not items
                else BatchJobStatus.FAILED
            )
        )
        result_manifest = BatchResultResponse(
            batch_job_id=batch_job_id,
            route=self._route_ref(route),
            status=result_status,
            items=items,
            artifacts=BatchArtifactSummary(),
            metrics=metrics,
            meta=self._meta(),
        )
        manifest_path = self.settings.storage.batch_job_result_manifest(batch_job_id)
        jsonl_path = self.settings.storage.batch_job_result_jsonl(batch_job_id)
        artifacts = BatchArtifactSummary(
            manifest_uri=str(manifest_path),
            results_uri=str(manifest_path),
            jsonl_uri=str(jsonl_path),
            recoverable=bool(incomplete_chunk_nos),
            incomplete_chunk_nos=incomplete_chunk_nos,
        )
        payload = result_manifest.model_dump(mode="json")
        payload["artifacts"] = artifacts.model_dump(mode="json")
        _write_json(manifest_path, payload)
        _write_jsonl(jsonl_path, [item.model_dump(mode="json") for item in items])
        with session_scope(self.settings) as session:
            status = payload["status"]
            update_batch_job_state(
                session,
                batch_job_id=batch_job_id,
                status=BatchJobStatus(status),
                source_service=self.service_name,
                actor_type="system",
                actor_id=self.service_name,
                output_manifest_uri=str(manifest_path),
                reason="Batch chunk outputs aggregated into a manifest.",
            )
        return {
            "batch_job_id": batch_job_id,
            "item_count": len(items),
            "manifest_uri": str(manifest_path),
        }

    def publish_batch_outputs(self, batch_job_id: str) -> dict[str, Any]:
        manifest_path = self.settings.storage.batch_job_result_manifest(batch_job_id)
        jsonl_path = self.settings.storage.batch_job_result_jsonl(batch_job_id)
        bundle_path = self.settings.storage.batch_job_result_bundle(batch_job_id)
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(bundle_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            if manifest_path.exists():
                archive.write(manifest_path, arcname="manifest.json")
            if jsonl_path.exists():
                archive.write(jsonl_path, arcname="results.jsonl")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["artifacts"] = {
            **(payload.get("artifacts") or {}),
            "bundle_uri": str(bundle_path),
            "download_path": str(bundle_path),
        }
        _write_json(manifest_path, payload)
        with session_scope(self.settings) as session:
            detail = get_batch_job_record(session, batch_job_id)
            if detail is None:
                raise ValueError(f"Batch job {batch_job_id} does not exist.")
            final_status = (
                BatchJobStatus.FAILED
                if (payload.get("artifacts") or {}).get("incomplete_chunk_nos")
                else (
                    BatchJobStatus.COMPLETED
                    if detail.completed_items > 0 or detail.failed_items == 0
                    else BatchJobStatus.FAILED
                )
            )
            update_batch_job_state(
                session,
                batch_job_id=batch_job_id,
                status=final_status,
                source_service=self.service_name,
                actor_type="system",
                actor_id=self.service_name,
                output_manifest_uri=str(manifest_path),
                reason="Batch result artifacts packaged for retrieval.",
            )
        return {
            "batch_job_id": batch_job_id,
            "bundle_uri": str(bundle_path),
            "status": "published",
        }

    async def _run_batch_item(
        self,
        *,
        route: ReleaseContext,
        item: BatchInputItem,
    ) -> BatchResultRecord:
        if route.task_type == TaskType.CLASSIFICATION:
            timing, labels = await self._run_classification(
                payload=RealtimeClassifyRequest(
                    deployment_target=TargetPath.BATCH.value,
                    image_base64=item.image_base64,
                    deployment_release_id=route.deployment_release_id,
                    timeout_ms=self.settings.inference_limits.batch_timeout_seconds * 1000,
                    top_k=min(len(route.class_labels) or 3, 5),
                ),
                route=route,
            )
            return BatchResultRecord(
                item_id=item.item_id,
                external_ref=item.external_ref,
                status="completed",
                labels=labels,
                timing=timing,
            )
        if route.task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
            timing, detections = await self._run_detection(
                payload=RealtimeDetectRequest(
                    deployment_target=TargetPath.BATCH.value,
                    image_base64=item.image_base64,
                    deployment_release_id=route.deployment_release_id,
                    timeout_ms=self.settings.inference_limits.batch_timeout_seconds * 1000,
                    max_detections=10,
                    score_threshold=route.confidence_threshold_default,
                ),
                route=route,
            )
            return BatchResultRecord(
                item_id=item.item_id,
                external_ref=item.external_ref,
                status="completed",
                detections=detections,
                timing=timing,
            )
        if route.task_type == TaskType.ANOMALY:
            timing, result = await self._run_anomaly(
                payload=RealtimeAnomalyRequest(
                    deployment_target=TargetPath.BATCH.value,
                    image_base64=item.image_base64,
                    deployment_release_id=route.deployment_release_id,
                    timeout_ms=self.settings.inference_limits.batch_timeout_seconds * 1000,
                    threshold=_anomaly_threshold(route),
                    return_mask=False,
                ),
                route=route,
            )
            return BatchResultRecord(
                item_id=item.item_id,
                external_ref=item.external_ref,
                status="completed",
                anomaly_score=float(result["anomaly_score"]),
                predicted_label=result["predicted_label"],
                is_anomalous=bool(result["is_anomalous"]),
                threshold=float(result["threshold"]),
                heatmap=result["heatmap"],
                image_width=int(result["image_width"]),
                image_height=int(result["image_height"]),
                timing=timing,
            )
        timing, image_width, image_height, segments = await self._run_segmentation(
            payload=RealtimeSegmentRequest(
                deployment_target=TargetPath.BATCH.value,
                image_base64=item.image_base64,
                deployment_release_id=route.deployment_release_id,
                timeout_ms=self.settings.inference_limits.batch_timeout_seconds * 1000,
                max_segments=10,
                score_threshold=route.confidence_threshold_default,
            ),
            route=route,
        )
        return BatchResultRecord(
            item_id=item.item_id,
            external_ref=item.external_ref,
            status="completed",
            image_width=image_width,
            image_height=image_height,
            segments=segments,
            timing=timing,
        )

    def _materialize_submission_manifest(self, payload: BatchJobCreateRequest) -> str:
        if payload.inputs:
            temp_root = self.settings.storage.batch_root() / "submissions"
            manifest_path = temp_root / f"submission_{int(time.time() * 1000)}.json"
            _write_json(
                manifest_path,
                {
                    "schema_version": "inference.batch.submission.v1",
                    "deployment_target": payload.deployment_target,
                    "inputs": [item.model_dump(mode="json") for item in payload.inputs],
                },
            )
            return str(manifest_path)
        return str(_uri_to_path(payload.input_manifest_uri or ""))

    def _batch_queue(self) -> Queue:
        return Queue(QueueName.BATCH.value, connection=Redis.from_url(self.settings.redis.url()))


def _ensure_anomaly_runtime_support(route: ReleaseContext) -> None:
    anomaly = dict(route.manifest.get("anomaly") or {})
    postprocessing = dict(route.manifest.get("postprocessing") or {})
    if not bool(anomaly.get("heatmap_support", postprocessing.get("heatmap_support", False))):
        raise PlatformError(
            code="unsupported_capability",
            message="The selected anomaly deployment does not declare heatmap support.",
            status_code=409,
        )


def _anomaly_threshold(route: ReleaseContext) -> float:
    anomaly = dict(route.manifest.get("anomaly") or {})
    postprocessing = dict(route.manifest.get("postprocessing") or {})
    value = (
        anomaly.get("threshold")
        or postprocessing.get("threshold")
        or postprocessing.get("confidence_threshold_default")
        or route.confidence_threshold_default
        or 0.5
    )
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.5


def _anomaly_artifact_path(route: ReleaseContext) -> Path:
    package_root = Path(str(route.manifest.get("package_root") or ""))
    source_manifests = route.manifest.get("source_manifests") or {}
    for key in ("openvino", "onnx", "deployment_artifact"):
        entry = source_manifests.get(key) or {}
        relative = entry.get("path") if isinstance(entry, dict) else None
        if relative and package_root:
            candidate = package_root / str(relative)
            if candidate.exists():
                _ensure_not_json_artifact(candidate, deployment_release_id=route.deployment_release_id)
                return candidate
    raise PlatformError(
        code="anomaly_artifact_missing",
        message="The anomaly deployment package does not contain a usable inference artifact.",
        status_code=503,
        metadata={"deployment_release_id": route.deployment_release_id},
    )


def _pil_image_from_bytes(image_bytes: bytes):
    try:
        from PIL import Image
    except ImportError as exc:
        raise PlatformError(
            code="dependency_missing",
            message="Pillow is required for anomaly inference image decoding.",
            status_code=503,
        ) from exc
    with Image.open(io.BytesIO(image_bytes)) as image:
        return image.convert("RGB")


def _prediction_score(result: object) -> float:
    for name in ("pred_score", "anomaly_score", "score"):
        value = getattr(result, name, None)
        parsed = _float_or_none(value)
        if parsed is not None:
            return max(0.0, parsed)
    if isinstance(result, dict):
        for name in ("pred_score", "anomaly_score", "score"):
            parsed = _float_or_none(result.get(name))
            if parsed is not None:
                return max(0.0, parsed)
    raise PlatformError(
        code="anomaly_score_missing",
        message="The deployed anomaly model did not return an anomaly score.",
        status_code=500,
    )


def _prediction_map(result: object) -> object | None:
    for name in ("anomaly_map", "heat_map", "heatmap"):
        value = getattr(result, name, None)
        if value is not None:
            return value
    if isinstance(result, dict):
        for name in ("anomaly_map", "heat_map", "heatmap"):
            value = result.get(name)
            if value is not None:
                return value
    return None


def _prediction_mask(result: object) -> object | None:
    for name in ("pred_mask", "mask"):
        value = getattr(result, name, None)
        if value is not None:
            return value
    if isinstance(result, dict):
        return result.get("pred_mask") or result.get("mask")
    return None


def _heatmap_from_array(value: object, *, image_size: tuple[int, int]) -> AnomalyHeatmap:
    array = _array2d(value)
    min_value = min(min(row) for row in array)
    max_value = max(max(row) for row in array)
    span = max(max_value - min_value, 1e-9)
    pixels = [
        int(max(0, min(((item - min_value) / span) * 255.0, 255)))
        for row in array
        for item in row
    ]
    data, width, height = _png_base64_from_luma(pixels, len(array[0]), len(array), image_size)
    return AnomalyHeatmap(
        data=data,
        width=width,
        height=height,
        min_value=float(min_value),
        max_value=float(max_value),
        colormap="turbo",
    )


def _mask_from_array(value: object, *, threshold: float, image_size: tuple[int, int]) -> AnomalyMask:
    array = _array2d(value)
    pixels = [255 if float(item) > 0 else 0 for row in array for item in row]
    data, width, height = _png_base64_from_luma(pixels, len(array[0]), len(array), image_size)
    return AnomalyMask(data=data, width=width, height=height, threshold=threshold)


def _array2d(value: object) -> list[list[float]]:
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    if hasattr(value, "tolist"):
        value = value.tolist()
    while isinstance(value, list) and value and isinstance(value[0], list) and len(value) == 1:
        value = value[0]
    if isinstance(value, list) and value and not isinstance(value[0], list):
        return [[float(item) for item in value]]
    if isinstance(value, list):
        rows: list[list[float]] = []
        for row in value:
            if isinstance(row, list):
                rows.append(
                    [
                        float(item[0] if isinstance(item, list) and item else item)
                        for item in row
                    ]
                )
        if rows and rows[0]:
            return rows
    raise PlatformError(
        code="anomaly_heatmap_invalid",
        message="The anomaly heatmap could not be converted to a 2D numeric array.",
        status_code=500,
    )


def _png_base64_from_luma(
    pixels: list[int],
    width: int,
    height: int,
    image_size: tuple[int, int],
) -> tuple[str, int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise PlatformError(
            code="dependency_missing",
            message="Pillow is required for anomaly heatmap encoding.",
            status_code=503,
        ) from exc
    image = Image.new("L", (width, height))
    image.putdata(pixels)
    if image.size != image_size:
        image = image.resize(image_size, resample=Image.Resampling.BILINEAR)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii"), image.width, image.height


def _float_or_none(value: object) -> float | None:
    try:
        if hasattr(value, "detach"):
            value = value.detach().cpu()
        if hasattr(value, "item"):
            value = value.item()
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _timeout_seconds(request_timeout_ms: int | None, *, default_ms: int) -> float:
    effective_ms = request_timeout_ms if request_timeout_ms is not None else default_ms
    return max(effective_ms / 1000.0, 0.05)


def _inference_timeout_error(
    route: ReleaseContext,
    timeout_seconds: float,
) -> PlatformError:
    return PlatformError(
        code="inference_timeout",
        message="The inference request exceeded its timeout budget.",
        status_code=504,
        retryable=True,
        metadata={
            "phase": "backend_inference_and_postprocessing",
            "timeout_ms": round(timeout_seconds * 1000),
            "deployment_release_id": route.deployment_release_id,
            "triton_model_name": route.triton_model_name,
            "triton_model_version": route.triton_model_version,
        },
    )


def _admission_wait_seconds(request_timeout_ms: int | None, *, default_ms: int) -> float:
    timeout_seconds = _timeout_seconds(request_timeout_ms, default_ms=default_ms)
    return min(max(timeout_seconds * 0.2, 0.05), 0.5)


def _decode_and_prepare_image_bytes(
    value: str,
    *,
    max_bytes: int,
) -> tuple[bytes, bytes, float, float, int, int]:
    return _prepare_image_bytes(
        _decode_image_base64(value, max_bytes=max_bytes),
        max_bytes=max_bytes,
    )


def _prepare_image_bytes(
    image_bytes: bytes,
    *,
    max_bytes: int,
) -> tuple[bytes, bytes, float, float, int, int]:
    validation_started = time.perf_counter()
    if len(image_bytes) > max_bytes:
        raise PlatformError(
            code="image_payload_too_large",
            message="The decoded image payload exceeds the configured size limit.",
            status_code=413,
        )
    inspection = inspect_image_bytes(image_bytes)
    validation_ms = _elapsed_ms(validation_started)

    preprocessing_started = time.perf_counter()
    normalized_bytes = _normalize_image_bytes(image_bytes)
    preprocessing_ms = _elapsed_ms(preprocessing_started)
    width = max(int(inspection.width or 1), 1)
    height = max(int(inspection.height or 1), 1)
    return image_bytes, normalized_bytes, validation_ms, preprocessing_ms, width, height


def _decode_image_base64(value: str, *, max_bytes: int) -> bytes:
    try:
        payload = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PlatformError(
            code="invalid_image_payload",
            message="The image_base64 payload is not valid base64-encoded image content.",
            detail=str(exc),
            status_code=422,
        ) from exc
    if len(payload) > max_bytes:
        raise PlatformError(
            code="image_payload_too_large",
            message="The decoded image payload exceeds the configured size limit.",
            status_code=413,
        )
    return payload


def _normalize_image_bytes(image_bytes: bytes) -> bytes:
    if not image_bytes:
        raise PlatformError(
            code="empty_image_payload",
            message="The image payload must not be empty.",
            status_code=422,
        )
    return image_bytes


def _triton_endpoint(settings: PlatformSettings, target_path: TargetPath) -> object:
    return (
        settings.triton.realtime
        if target_path == TargetPath.REALTIME
        else settings.triton.batch
    )


def _is_local_triton_endpoint(endpoint: Any) -> bool:
    hostname = (urlparse(str(endpoint.http_url)).hostname or "").lower()
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _triton_shared_memory_layout(
    *,
    route: ReleaseContext,
    input_name: str,
    input_shape: list[int],
    input_byte_size: int,
    output_tensors: Sequence[dict[str, Any]] | None = None,
) -> list[_SharedMemoryTensor] | None:
    if input_byte_size <= 0 or not input_shape:
        return None
    layout = [
        _SharedMemoryTensor(
            name=input_name,
            data_type="FP32",
            shape=input_shape,
            offset=0,
            byte_size=input_byte_size,
        )
    ]
    offset = _aligned_byte_offset(input_byte_size)
    resolved_output_tensors = output_tensors
    if resolved_output_tensors is None:
        resolved_output_tensors = (
            (route.manifest.get("io_schema") or {}).get("output_tensors") or []
        )
    for output in resolved_output_tensors:
        if not isinstance(output, dict):
            return None
        name = str(output.get("name") or "").strip()
        data_type = str(output.get("data_type") or "FP32").upper()
        try:
            shape = [int(item) for item in output.get("dims") or []]
        except (TypeError, ValueError):
            return None
        byte_size = _tensor_byte_size(shape, data_type)
        if not name or byte_size <= 0:
            return None
        layout.append(
            _SharedMemoryTensor(
                name=name,
                data_type=data_type,
                shape=shape,
                offset=offset,
                byte_size=byte_size,
            )
        )
        offset = _aligned_byte_offset(offset + byte_size)
    if len(layout) < 2 or offset > 512 * 1024 * 1024:
        return None
    return layout


def _aligned_byte_offset(value: int, alignment: int = 64) -> int:
    return ((max(int(value), 0) + alignment - 1) // alignment) * alignment


def _tensor_byte_size(shape: Sequence[int], data_type: str) -> int:
    if not shape or any(int(item) <= 0 for item in shape):
        return 0
    item_sizes = {
        "BOOL": 1,
        "UINT8": 1,
        "INT8": 1,
        "FP16": 2,
        "UINT16": 2,
        "INT16": 2,
        "FP32": 4,
        "UINT32": 4,
        "INT32": 4,
        "FP64": 8,
        "UINT64": 8,
        "INT64": 8,
    }
    normalized = str(data_type).upper().removeprefix("TYPE_")
    item_size = item_sizes.get(normalized)
    if item_size is None:
        return 0
    return math.prod(int(item) for item in shape) * item_size


def _triton_model_statistics(
    payload: dict[str, Any],
) -> dict[tuple[str, int], dict[str, int | float]]:
    parsed: dict[tuple[str, int], dict[str, int | float]] = {}
    for item in payload.get("model_stats") or []:
        if not isinstance(item, dict):
            continue
        model_name = str(item.get("name") or "").strip()
        try:
            model_version = int(item.get("version"))
        except (TypeError, ValueError):
            continue
        if not model_name or model_version < 1:
            continue
        inference_stats = item.get("inference_stats") or {}
        if not isinstance(inference_stats, dict):
            inference_stats = {}
        statistics: dict[str, int | float] = {
            "inference_count": _nonnegative_int(item.get("inference_count")),
            "execution_count": _nonnegative_int(item.get("execution_count")),
        }
        for phase_name in (
            "success",
            "fail",
            "queue",
            "compute_input",
            "compute_infer",
            "compute_output",
        ):
            phase = inference_stats.get(phase_name) or {}
            if not isinstance(phase, dict):
                continue
            count = _nonnegative_int(phase.get("count"))
            duration_ns = _nonnegative_int(phase.get("ns"))
            statistics[f"{phase_name}_count"] = count
            statistics[f"{phase_name}_avg_ms"] = (
                round(duration_ns / count / 1_000_000.0, 3) if count else 0.0
            )
        parsed[(model_name, model_version)] = statistics
    return parsed


def _nonnegative_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _uses_simulated_inference(route: ReleaseContext) -> bool:
    manifest = route.manifest
    if manifest.get("simulation_only") is True or manifest.get("trainer_mode") == "simulation":
        return True
    source_manifests = manifest.get("source_manifests") or {}
    artifact = source_manifests.get("deployment_artifact") or source_manifests.get("onnx")
    return not (isinstance(artifact, dict) and artifact.get("path"))


def _can_fallback_to_local_onnx(exc: PlatformError) -> bool:
    return exc.code in {"triton_inference_failed"}


def _local_onnx_fallback_enabled(settings: PlatformSettings) -> bool:
    return settings.env == EnvironmentName.DEV or bool(settings.training.allow_onnx_deployment_fallback)


def _deployment_artifact_path(route: ReleaseContext) -> Path:
    package_root = Path(str(route.manifest.get("package_root") or ""))
    source_manifests = route.manifest.get("source_manifests") or {}
    for key in ("deployment_artifact", "onnx"):
        entry = source_manifests.get(key) or {}
        relative = entry.get("path") if isinstance(entry, dict) else None
        if relative and package_root:
            candidate = package_root / str(relative)
            if candidate.exists():
                _ensure_not_json_artifact(candidate, deployment_release_id=route.deployment_release_id)
                return candidate
    raise PlatformError(
        code="deployment_artifact_missing",
        message="The deployment package does not contain a usable model artifact.",
        status_code=503,
        metadata={"deployment_release_id": route.deployment_release_id},
    )


def _serving_artifact_format(route: ReleaseContext) -> str | None:
    source_manifests = route.manifest.get("source_manifests") or {}
    deployment_artifact = source_manifests.get("deployment_artifact") or {}
    if isinstance(deployment_artifact, dict):
        artifact_format = str(deployment_artifact.get("format") or "").strip()
        if artifact_format in {"tensorrt_plan", "onnx"}:
            return artifact_format
    for variant in route.manifest.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        if str(variant.get("target_path") or "") != route.target_path.value:
            continue
        artifact_format = str(variant.get("artifact_format") or "").strip()
        if artifact_format in {"tensorrt_plan", "onnx"}:
            return artifact_format
    return None


def _ensure_not_json_artifact(path: Path, *, deployment_release_id: str) -> None:
    try:
        prefix = path.read_bytes()[:64].lstrip()
    except OSError:
        return
    if prefix.startswith(b"{") or prefix.startswith(b"["):
        raise PlatformError(
            code="invalid_model_artifact",
            message="The deployment artifact is metadata JSON, not a runnable model artifact.",
            status_code=503,
            metadata={
                "deployment_release_id": deployment_release_id,
                "artifact_path": str(path),
            },
        )


def _first_tensor_name(manifest: dict[str, Any], *, kind: str) -> str | None:
    names = _tensor_names(manifest, kind=kind)
    return names[0] if names else None


def _tensor_names(manifest: dict[str, Any], *, kind: str) -> list[str]:
    tensors = (manifest.get("io_schema") or {}).get(f"{kind}_tensors") or []
    names: list[str] = []
    for tensor in tensors:
        name = tensor.get("name") if isinstance(tensor, dict) else None
        if name:
            names.append(str(name))
    return names


def _classification_tensor(image_bytes: bytes, route: ReleaseContext) -> object:
    try:
        import numpy as np
    except ImportError as exc:
        raise PlatformError(
            code="dependency_missing",
            message="NumPy is required for image tensor preprocessing.",
            status_code=503,
        ) from exc
    width, height = _classification_image_size(route)
    tensor = _image_tensor_rgb_nchw(image_bytes, width=width, height=height)
    if _is_ultralytics_route(route):
        tensor *= np.float32(1.0 / 255.0)
    else:
        _apply_manifest_normalization_inplace(tensor, route)
    return tensor


def _image_tensor_rgb_nchw(image_bytes: bytes, *, width: int, height: int) -> Any:
    try:
        import cv2
        import numpy as np

        encoded = np.frombuffer(image_bytes, dtype=np.uint8)
        decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if decoded is not None:
            if decoded.shape[0] == height and decoded.shape[1] == width:
                resized = decoded
            else:
                interpolation = (
                    cv2.INTER_AREA
                    if decoded.shape[0] > height or decoded.shape[1] > width
                    else cv2.INTER_LINEAR
                )
                resized = cv2.resize(decoded, (width, height), interpolation=interpolation)
            tensor = np.empty((1, 3, height, width), dtype="float32")
            tensor[0, 0, :, :] = resized[:, :, 2]
            tensor[0, 1, :, :] = resized[:, :, 1]
            tensor[0, 2, :, :] = resized[:, :, 0]
            return tensor
    except Exception:
        pass

    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise PlatformError(
            code="dependency_missing",
            message="Pillow and NumPy are required for image tensor preprocessing.",
            status_code=503,
        ) from exc
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB").resize((width, height), Image.Resampling.BILINEAR)
        pixels = np.asarray(image)
        tensor = np.empty((1, 3, height, width), dtype="float32")
        tensor[0, 0, :, :] = pixels[:, :, 0]
        tensor[0, 1, :, :] = pixels[:, :, 1]
        tensor[0, 2, :, :] = pixels[:, :, 2]
        return tensor


def _classification_image_size(route: ReleaseContext) -> tuple[int, int]:
    input_tensors = (route.manifest.get("io_schema") or {}).get("input_tensors") or []
    if input_tensors and isinstance(input_tensors[0], dict):
        dims = input_tensors[0].get("dims") or []
        numeric = [int(item) for item in dims if isinstance(item, int) and int(item) > 0]
        if len(numeric) >= 3:
            return max(numeric[-1], 1), max(numeric[-2], 1)
        if len(numeric) >= 2:
            return max(numeric[-1], 1), max(numeric[-2], 1)
    resize = (route.manifest.get("preprocessing") or {}).get("resize")
    if isinstance(resize, dict):
        width = int(resize.get("width") or resize.get("size") or 224)
        height = int(resize.get("height") or resize.get("size") or width)
        return max(width, 1), max(height, 1)
    if resize:
        size = int(resize)
        return max(size, 1), max(size, 1)
    return 224, 224


def _is_ultralytics_route(route: ReleaseContext) -> bool:
    value = str(route.manifest.get("trainer_backend") or "").lower()
    if value == "ultralytics-yolo":
        return True
    family = str(route.architecture.family or "").lower()
    return family == "yolo" and route.task_type == TaskType.CLASSIFICATION


def _apply_manifest_normalization_inplace(tensor: object, route: ReleaseContext) -> None:
    import numpy as np

    normalize = (route.manifest.get("preprocessing") or {}).get("normalize") or {}
    mean = normalize.get("mean") if isinstance(normalize, dict) else None
    std = normalize.get("std") if isinstance(normalize, dict) else None
    if not isinstance(mean, list) or not isinstance(std, list) or len(mean) != 3 or len(std) != 3:
        tensor *= np.float32(1.0 / 255.0)
        return
    mean_array = np.asarray([float(item) for item in mean], dtype="float32")
    std_array = np.asarray([max(float(item), 1e-9) for item in std], dtype="float32")
    if float(np.max(mean_array)) <= 10 and float(np.max(std_array)) <= 10:
        tensor *= np.float32(1.0 / 255.0)
    mean_array = mean_array.reshape(1, 3, 1, 1)
    std_array = std_array.reshape(1, 3, 1, 1)
    tensor -= mean_array
    tensor /= std_array


def _triton_response_output_tensors(response: Any) -> dict[str, _OutputTensor]:
    header_length = int(response.headers.get("Inference-Header-Content-Length") or 0)
    if header_length <= 0:
        return _triton_output_tensors(response.json())
    response_bytes = response.content
    payload = _triton_response_payload(response)
    return _triton_binary_output_tensors(payload, response_bytes[header_length:])


def _triton_response_output_specs(response: Any) -> list[dict[str, Any]]:
    payload = _triton_response_payload(response)
    specs: list[dict[str, Any]] = []
    for output in payload.get("outputs") or []:
        if not isinstance(output, dict):
            continue
        name = str(output.get("name") or "").strip()
        shape = _shape_from_triton_output(output)
        data_type = str(output.get("datatype") or "FP32").upper()
        if name and _tensor_byte_size(shape, data_type) > 0:
            specs.append(
                {
                    "name": name,
                    "data_type": data_type,
                    "dims": shape,
                }
            )
    return specs


def _triton_response_payload(response: Any) -> dict[str, Any]:
    header_length = int(response.headers.get("Inference-Header-Content-Length") or 0)
    if header_length <= 0:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    response_bytes = response.content
    payload = json.loads(response_bytes[:header_length].decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _triton_binary_output_tensors(
    payload: dict[str, Any],
    binary_payload: bytes,
) -> dict[str, _OutputTensor]:
    import numpy as np

    outputs = payload.get("outputs") or []
    parsed: dict[str, _OutputTensor] = {}
    offset = 0
    for output in outputs:
        if not isinstance(output, dict):
            continue
        name = str(output.get("name") or f"output_{len(parsed)}")
        parameters = output.get("parameters") or {}
        byte_count = int(parameters.get("binary_data_size") or 0)
        if byte_count <= 0:
            values = _flatten_scores(_triton_output_data(output))
        else:
            dtype = _numpy_dtype_for_triton(str(output.get("datatype") or "FP32"))
            raw = memoryview(binary_payload)[offset : offset + byte_count]
            values = np.frombuffer(raw, dtype=dtype).reshape(-1)
            if values.dtype != np.dtype("float32"):
                values = values.astype("float32")
            offset += byte_count
        parsed[name] = _OutputTensor(
            name=name,
            shape=_shape_from_triton_output(output),
            values=values,
        )
    if not parsed:
        raise PlatformError(
            code="triton_output_missing",
            message="Triton did not return any model output tensors.",
            status_code=502,
        )
    return parsed


def _numpy_dtype_for_triton(data_type: str) -> str:
    normalized = data_type.upper()
    mapping = {
        "FP32": "<f4",
        "TYPE_FP32": "<f4",
        "FP16": "<f2",
        "TYPE_FP16": "<f2",
        "INT64": "<i8",
        "TYPE_INT64": "<i8",
        "INT32": "<i4",
        "TYPE_INT32": "<i4",
    }
    return mapping.get(normalized, "<f4")


def _triton_output_tensors(payload: dict[str, Any]) -> dict[str, _OutputTensor]:
    outputs = payload.get("outputs") or []
    parsed: dict[str, _OutputTensor] = {}
    for output in outputs:
        if not isinstance(output, dict):
            continue
        name = str(output.get("name") or f"output_{len(parsed)}")
        parsed[name] = _OutputTensor(
            name=name,
            shape=_shape_from_triton_output(output),
            values=_flatten_scores(_triton_output_data(output)),
        )
    if not parsed:
        raise PlatformError(
            code="triton_output_missing",
            message="Triton did not return any model output tensors.",
            status_code=502,
        )
    return parsed


def _triton_output_data(output: dict[str, Any]) -> object:
    data = output.get("data")
    if data is not None:
        return data
    contents = output.get("contents") or {}
    for key in (
        "fp32_contents",
        "float_contents",
        "int64_contents",
        "int_contents",
        "int32_contents",
    ):
        if key in contents:
            return contents[key]
    return []


def _shape_from_triton_output(output: dict[str, Any]) -> list[int]:
    shape = output.get("shape") or []
    parsed: list[int] = []
    for item in shape:
        try:
            parsed.append(int(item))
        except (TypeError, ValueError):
            parsed.append(-1)
    return parsed


def _shape_of(value: object) -> list[int]:
    shape = getattr(value, "shape", None)
    if shape is not None:
        return [int(item) for item in shape]
    if hasattr(value, "tolist"):
        value = value.tolist()
    parsed: list[int] = []
    current = value
    while isinstance(current, list):
        parsed.append(len(current))
        current = current[0] if current else None
    return parsed


def _tensor_values(
    outputs: dict[str, _OutputTensor],
    *,
    output_name: str,
) -> Sequence[float]:
    selected = outputs.get(output_name) or next(iter(outputs.values()), None)
    if selected is None:
        raise PlatformError(
            code="model_output_missing",
            message="The model did not return the expected output tensor.",
            status_code=502,
            metadata={"output_name": output_name},
        )
    return selected.values


def _flatten_scores(value: object) -> list[float]:
    if value is None:
        return []
    if hasattr(value, "reshape") and hasattr(value, "tolist"):
        return [float(item) for item in value.reshape(-1).tolist()]
    if hasattr(value, "tolist"):
        value = value.tolist()
    while isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
        value = value[0]
    if isinstance(value, list):
        flattened: list[float] = []
        for item in value:
            if isinstance(item, list):
                flattened.extend(float(inner) for inner in item)
            else:
                flattened.append(float(item))
        return flattened
    return [float(value)]


def _detection_predictions_from_outputs(
    *,
    route: ReleaseContext,
    outputs: dict[str, _OutputTensor],
    max_detections: int,
    score_threshold: float,
) -> list[DetectionPrediction]:
    if not outputs:
        return []
    boxes = _output_by_role(outputs, "box")
    scores = _output_by_role(outputs, "score")
    labels = _output_by_role(outputs, "label")
    if boxes is not None and scores is not None and labels is not None:
        return _detections_from_explicit_tensors(
            route=route,
            boxes=boxes,
            scores=scores,
            labels=labels,
            max_detections=max_detections,
            score_threshold=score_threshold,
        )
    if boxes is not None and labels is not None:
        return _detections_from_dets_labels(
            route=route,
            dets=boxes,
            labels=labels,
            max_detections=max_detections,
            score_threshold=score_threshold,
        )
    return _detections_from_compact_tensor(
        route=route,
        tensor=next(iter(outputs.values())),
        max_detections=max_detections,
        score_threshold=score_threshold,
    )


def _detections_from_explicit_tensors(
    *,
    route: ReleaseContext,
    boxes: _OutputTensor,
    scores: _OutputTensor,
    labels: _OutputTensor,
    max_detections: int,
    score_threshold: float,
) -> list[DetectionPrediction]:
    rows = _rows_from_tensor(
        boxes,
        default_width=5 if route.task_type == TaskType.ORIENTED_DETECTION else 4,
    )
    results: list[DetectionPrediction] = []
    for index, box in enumerate(rows):
        score = scores.values[index] if index < len(scores.values) else 0.0
        label_index = int(labels.values[index]) if index < len(labels.values) else 0
        if score < score_threshold:
            continue
        oriented_box = (
            _oriented_box_from_box_values(box, route=route)
            if route.task_type == TaskType.ORIENTED_DETECTION
            else None
        )
        results.append(
            DetectionPrediction(
                label=_label_for_index(route, label_index),
                score=round(_clip01(score), 6),
                box=(
                    _axis_aligned_box_from_oriented(oriented_box)
                    if oriented_box is not None
                    else _detection_box_from_values(box, route=route)
                ),
                oriented_box=oriented_box,
            )
        )
    return sorted(results, key=lambda item: item.score, reverse=True)[:max_detections]


def _detections_from_dets_labels(
    *,
    route: ReleaseContext,
    dets: _OutputTensor,
    labels: _OutputTensor,
    max_detections: int,
    score_threshold: float,
) -> list[DetectionPrediction]:
    rows = _rows_from_tensor(
        dets,
        default_width=6 if route.task_type == TaskType.ORIENTED_DETECTION else 5,
    )
    results: list[DetectionPrediction] = []
    for index, row in enumerate(rows):
        if route.task_type == TaskType.ORIENTED_DETECTION:
            if len(row) < 6:
                continue
            score = _oriented_score_from_row(row)
        else:
            if len(row) < 5:
                continue
            score = row[4]
        label_index = int(labels.values[index]) if index < len(labels.values) else 0
        if score < score_threshold:
            continue
        oriented_box = (
            _oriented_box_from_box_values(row, route=route)
            if route.task_type == TaskType.ORIENTED_DETECTION
            else None
        )
        results.append(
            DetectionPrediction(
                label=_label_for_index(route, label_index),
                score=round(_clip01(score), 6),
                box=(
                    _axis_aligned_box_from_oriented(oriented_box)
                    if oriented_box is not None
                    else _detection_box_from_values(row[:4], route=route)
                ),
                oriented_box=oriented_box,
            )
        )
    return sorted(results, key=lambda item: item.score, reverse=True)[:max_detections]


def _detections_from_compact_tensor(
    *,
    route: ReleaseContext,
    tensor: _OutputTensor,
    max_detections: int,
    score_threshold: float,
) -> list[DetectionPrediction]:
    if route.task_type == TaskType.ORIENTED_DETECTION:
        return _oriented_detections_from_compact_tensor(
            route=route,
            tensor=tensor,
            max_detections=max_detections,
            score_threshold=score_threshold,
        )
    rows = _rows_from_model_output(tensor)
    results: list[DetectionPrediction] = []
    class_count = max(len(route.class_labels), 1)
    for row in rows:
        if len(row) < 6:
            continue
        if len(row) == 6:
            x1, y1, x2, y2, score, label_index = row
            if score < score_threshold:
                continue
            box_values = [x1, y1, x2, y2]
        else:
            box_values = _xywh_to_xyxy(row[:4])
            class_scores = row[4 : 4 + class_count] or row[4:]
            label_index, score = max(
                enumerate(class_scores),
                key=lambda item: item[1],
            )
            if score < score_threshold:
                continue
        results.append(
            DetectionPrediction(
                label=_label_for_index(route, int(label_index)),
                score=round(_clip01(score), 6),
                box=_detection_box_from_values(box_values, route=route),
            )
        )
    results = sorted(results, key=lambda item: item.score, reverse=True)
    if bool((route.manifest.get("nms") or {}).get("enabled", False)):
        results = _nms(results, iou_threshold=float((route.manifest.get("nms") or {}).get("iou_threshold") or 0.5))
    return results[:max_detections]


def _oriented_detections_from_compact_tensor(
    *,
    route: ReleaseContext,
    tensor: _OutputTensor,
    max_detections: int,
    score_threshold: float,
) -> list[DetectionPrediction]:
    rows = _rows_from_model_output(tensor)
    results: list[DetectionPrediction] = []
    class_count = max(len(route.class_labels), 1)
    for row in rows:
        if len(row) < 6:
            continue
        if _looks_like_corner_detection_row(row, class_count=class_count):
            geometry_values = row[:8]
            score = row[8]
            label_index = int(row[9])
        elif len(row) == 7:
            geometry_values = row[:5]
            score = row[5]
            label_index = int(row[6])
        elif len(row) >= 5 + class_count:
            geometry_values = row[:5]
            class_scores = row[5 : 5 + class_count] or row[5:]
            label_index, score = max(enumerate(class_scores), key=lambda item: item[1])
        else:
            continue
        if score < score_threshold:
            continue
        oriented_box = _oriented_box_from_box_values(geometry_values, route=route)
        if oriented_box is None:
            continue
        results.append(
            DetectionPrediction(
                label=_label_for_index(route, int(label_index)),
                score=round(_clip01(score), 6),
                box=_axis_aligned_box_from_oriented(oriented_box),
                oriented_box=oriented_box,
            )
        )
    results = sorted(results, key=lambda item: item.score, reverse=True)
    if bool((route.manifest.get("nms") or {}).get("enabled", False)):
        results = _nms(
            results,
            iou_threshold=float((route.manifest.get("nms") or {}).get("iou_threshold") or 0.5),
        )
    return results[:max_detections]


def _segmentation_predictions_from_outputs(
    *,
    route: ReleaseContext,
    outputs: dict[str, _OutputTensor],
    max_segments: int,
    score_threshold: float,
) -> list[SegmentationPrediction]:
    if not outputs:
        return []
    segments = _output_by_role(outputs, "segment")
    scores = _output_by_role(outputs, "score")
    labels = _output_by_role(outputs, "label")
    if segments is not None and scores is not None and labels is not None:
        return _segments_from_explicit_tensors(
            route=route,
            segments=segments,
            scores=scores,
            labels=labels,
            max_segments=max_segments,
            score_threshold=score_threshold,
        )
    yolo_segments = _segments_from_yolo_tensors(
        route=route,
        outputs=outputs,
        max_segments=max_segments,
        score_threshold=score_threshold,
    )
    if yolo_segments is not None:
        return yolo_segments
    masks = _output_by_role(outputs, "mask") or next(iter(outputs.values()))
    return _segments_from_semantic_masks(
        route=route,
        masks=masks,
        max_segments=max_segments,
        score_threshold=score_threshold,
    )


def _segments_from_explicit_tensors(
    *,
    route: ReleaseContext,
    segments: _OutputTensor,
    scores: _OutputTensor,
    labels: _OutputTensor,
    max_segments: int,
    score_threshold: float,
) -> list[SegmentationPrediction]:
    polygons = _polygons_from_tensor(segments)
    results: list[SegmentationPrediction] = []
    for index, polygon_values in enumerate(polygons):
        score = scores.values[index] if index < len(scores.values) else 0.0
        label_index = int(labels.values[index]) if index < len(labels.values) else 0
        if score < score_threshold:
            continue
        polygon = [
            SegmentationPoint(x=_clip01(x), y=_clip01(y))
            for x, y in _normalize_polygon_points(polygon_values, route=route)
        ]
        if len(polygon) >= 3:
            results.append(
                SegmentationPrediction(
                    label=_label_for_index(route, label_index),
                    score=round(_clip01(score), 6),
                    polygon=polygon,
                )
            )
    return sorted(results, key=lambda item: item.score, reverse=True)[:max_segments]


def _segments_from_yolo_tensors(
    *,
    route: ReleaseContext,
    outputs: dict[str, _OutputTensor],
    max_segments: int,
    score_threshold: float,
) -> list[SegmentationPrediction] | None:
    class_count = max(len(route.class_labels), 1)
    tensors = list(outputs.values())
    for detections_tensor in tensors:
        rows = _rows_from_model_output(detections_tensor)
        first_row = next((row for row in rows if len(row) > 4 + class_count), None)
        if first_row is None:
            continue
        mask_coeff_count = len(first_row) - 4 - class_count
        proto_tensor = _matching_yolo_proto_tensor(
            tensors,
            detections_tensor=detections_tensor,
            mask_coeff_count=mask_coeff_count,
        )
        if proto_tensor is None:
            continue
        return _segments_from_yolo_rows(
            route=route,
            rows=rows,
            proto_tensor=proto_tensor,
            class_count=class_count,
            max_segments=max_segments,
            score_threshold=score_threshold,
        )
    return None


def _matching_yolo_proto_tensor(
    tensors: Sequence[_OutputTensor],
    *,
    detections_tensor: _OutputTensor,
    mask_coeff_count: int,
) -> _OutputTensor | None:
    if mask_coeff_count <= 0:
        return None
    for tensor in tensors:
        if tensor is detections_tensor or len(tensor.shape) < 3:
            continue
        channel_count = int(tensor.shape[-3])
        if channel_count == mask_coeff_count:
            return tensor
    return None


def _segments_from_yolo_rows(
    *,
    route: ReleaseContext,
    rows: Sequence[Sequence[float]],
    proto_tensor: _OutputTensor,
    class_count: int,
    max_segments: int,
    score_threshold: float,
) -> list[SegmentationPrediction]:
    candidates: list[tuple[DetectionPrediction, list[float]]] = []
    pre_nms_limit = _yolo_segment_pre_nms_limit(route)
    try:
        import numpy as np

        row_matrix = np.asarray(rows, dtype=np.float32)
    except (ImportError, TypeError, ValueError):
        row_matrix = None
    if (
        row_matrix is not None
        and row_matrix.ndim == 2
        and row_matrix.shape[1] > 4 + class_count
    ):
        class_scores = row_matrix[:, 4 : 4 + class_count]
        label_indices = np.argmax(class_scores, axis=1)
        scores = class_scores[np.arange(class_scores.shape[0]), label_indices]
        candidate_indices = np.flatnonzero(scores >= float(score_threshold))
        if candidate_indices.size > pre_nms_limit:
            candidate_scores = scores[candidate_indices]
            top_indices = np.argpartition(
                candidate_scores,
                -pre_nms_limit,
            )[-pre_nms_limit:]
            candidate_indices = candidate_indices[top_indices]
        candidate_indices = candidate_indices[
            np.argsort(scores[candidate_indices], kind="stable")[::-1]
        ]
        for index in candidate_indices:
            row = row_matrix[int(index)]
            label_index = int(label_indices[int(index)])
            score = float(scores[int(index)])
            candidates.append(
                (
                    DetectionPrediction(
                        label=_label_for_index(route, label_index),
                        score=round(_clip01(score), 6),
                        box=_detection_box_from_values(
                            _xywh_to_xyxy(row[:4]),
                            route=route,
                        ),
                    ),
                    row[4 + class_count :].astype(np.float32, copy=False).tolist(),
                )
            )
    else:
        for row in rows:
            if len(row) <= 4 + class_count:
                continue
            class_scores = row[4 : 4 + class_count]
            label_index, score = max(enumerate(class_scores), key=lambda item: item[1])
            if score < score_threshold:
                continue
            candidates.append(
                (
                    DetectionPrediction(
                        label=_label_for_index(route, int(label_index)),
                        score=round(_clip01(score), 6),
                        box=_detection_box_from_values(
                            _xywh_to_xyxy(row[:4]),
                            route=route,
                        ),
                    ),
                    [float(item) for item in row[4 + class_count :]],
                )
            )
        candidates = sorted(
            candidates,
            key=lambda item: item[0].score,
            reverse=True,
        )[:pre_nms_limit]
    selected = _nms_yolo_segments(route, candidates)[:max_segments]
    polygons = _yolo_mask_polygons(
        proto_tensor=proto_tensor,
        selected=selected,
        route=route,
    )
    results: list[SegmentationPrediction] = []
    for (detection, _coefficients), polygon in zip(selected, polygons, strict=False):
        polygon = polygon or _box_polygon(
            detection.box.x_min,
            detection.box.y_min,
            detection.box.x_max,
            detection.box.y_max,
        )
        results.append(
            SegmentationPrediction(
                label=detection.label,
                score=detection.score,
                polygon=polygon,
            )
        )
    return results


def _yolo_segment_pre_nms_limit(route: ReleaseContext) -> int:
    raw_limit = (route.manifest.get("nms") or {}).get("max_detections")
    try:
        return max(int(raw_limit or 300), 1)
    except (TypeError, ValueError):
        return 300


def _yolo_segment_nms_enabled(route: ReleaseContext) -> bool:
    if bool((route.manifest.get("nms") or {}).get("enabled", False)):
        return True
    # Compatibility for releases exported before segmentation was included in
    # the manifest NMS builder. This function is reached only for raw YOLO
    # detection-head plus mask-prototype outputs, which always require NMS.
    architecture_family = route.architecture.family.casefold()
    trainer_backend = str(route.manifest.get("trainer_backend") or "").casefold()
    return route.task_type == TaskType.SEGMENTATION and (
        architecture_family == "yolo" or trainer_backend == "ultralytics-yolo"
    )


def _nms_yolo_segments(
    route: ReleaseContext,
    candidates: list[tuple[DetectionPrediction, list[float]]],
) -> list[tuple[DetectionPrediction, list[float]]]:
    candidates = sorted(candidates, key=lambda item: item[0].score, reverse=True)
    if not _yolo_segment_nms_enabled(route):
        return candidates
    iou_threshold = float((route.manifest.get("nms") or {}).get("iou_threshold") or 0.5)
    if len(candidates) < 2:
        return candidates
    try:
        import numpy as np

        boxes = np.asarray(
            [
                [
                    item[0].box.x_min,
                    item[0].box.y_min,
                    item[0].box.x_max,
                    item[0].box.y_max,
                ]
                for item in candidates
            ],
            dtype=np.float32,
        )
        labels = np.asarray([item[0].label for item in candidates], dtype=object)
        scores = np.asarray([item[0].score for item in candidates], dtype=np.float32)
        order = np.argsort(-scores, kind="stable")
        keep_indices: list[int] = []
        while order.size:
            current = int(order[0])
            keep_indices.append(current)
            remaining = order[1:]
            if not remaining.size:
                break
            x_min = np.maximum(boxes[current, 0], boxes[remaining, 0])
            y_min = np.maximum(boxes[current, 1], boxes[remaining, 1])
            x_max = np.minimum(boxes[current, 2], boxes[remaining, 2])
            y_max = np.minimum(boxes[current, 3], boxes[remaining, 3])
            intersection = np.maximum(x_max - x_min, 0.0) * np.maximum(
                y_max - y_min,
                0.0,
            )
            current_area = max(
                float(boxes[current, 2] - boxes[current, 0]),
                0.0,
            ) * max(float(boxes[current, 3] - boxes[current, 1]), 0.0)
            remaining_areas = np.maximum(
                boxes[remaining, 2] - boxes[remaining, 0],
                0.0,
            ) * np.maximum(boxes[remaining, 3] - boxes[remaining, 1], 0.0)
            union = current_area + remaining_areas - intersection
            iou = np.divide(
                intersection,
                union,
                out=np.zeros_like(intersection),
                where=union > 0.0,
            )
            same_class = labels[remaining] == labels[current]
            order = remaining[~(same_class & (iou >= iou_threshold))]
        return [candidates[index] for index in keep_indices]
    except (ImportError, TypeError, ValueError):
        pass
    kept: list[tuple[DetectionPrediction, list[float]]] = []
    for candidate in candidates:
        if all(
            candidate[0].label != kept_candidate[0].label
            or _box_iou(candidate[0].box, kept_candidate[0].box) < iou_threshold
            for kept_candidate in kept
        ):
            kept.append(candidate)
    return kept


def _yolo_mask_polygons(
    *,
    proto_tensor: _OutputTensor,
    selected: Sequence[tuple[DetectionPrediction, list[float]]],
    route: ReleaseContext,
) -> list[list[SegmentationPoint] | None]:
    if not selected:
        return []
    if len(proto_tensor.shape) < 3:
        return [None] * len(selected)
    channel_count = int(proto_tensor.shape[-3])
    height = max(int(proto_tensor.shape[-2]), 1)
    width = max(int(proto_tensor.shape[-1]), 1)
    area = height * width
    if channel_count <= 0 or len(proto_tensor.values) < channel_count * area:
        return [None] * len(selected)
    mask_threshold = float(
        (route.manifest.get("postprocessing") or {}).get("mask_threshold") or 0.5
    )
    try:
        import numpy as np

        proto = np.asarray(proto_tensor.values, dtype=np.float32).reshape(
            channel_count,
            area,
        )
        coefficients = np.asarray(
            [items[:channel_count] for _detection, items in selected],
            dtype=np.float32,
        )
        if coefficients.shape != (len(selected), channel_count):
            raise ValueError("YOLO mask coefficient count does not match proto channels.")
        logits = coefficients @ proto
        if mask_threshold <= 0:
            active_masks = np.ones((len(selected), area), dtype=bool)
        elif mask_threshold >= 1:
            active_masks = np.zeros((len(selected), area), dtype=bool)
        else:
            logit_threshold = math.log(mask_threshold / (1.0 - mask_threshold))
            active_masks = logits >= np.float32(logit_threshold)
        active_masks = active_masks.reshape(len(selected), height, width)
        x_centers = (np.arange(width, dtype=np.float32) + 0.5) / float(width)
        y_centers = (np.arange(height, dtype=np.float32) + 0.5) / float(height)
        polygons: list[list[SegmentationPoint] | None] = []
        for index, (detection, _items) in enumerate(selected):
            active_mask = active_masks[index]
            box = detection.box
            active_mask &= (
                (x_centers >= float(box.x_min)) & (x_centers <= float(box.x_max))
            )[None, :]
            active_mask &= (
                (y_centers >= float(box.y_min)) & (y_centers <= float(box.y_max))
            )[:, None]
            polygons.append(
                _mask_polygon_from_binary_mask(active_mask)
                if bool(active_mask.any())
                else None
            )
        return polygons
    except (ImportError, TypeError, ValueError):
        return [
            _yolo_mask_polygon(
                proto_tensor=proto_tensor,
                coefficients=coefficients,
                box=detection.box,
                route=route,
            )
            for detection, coefficients in selected
        ]


def _yolo_mask_polygon(
    *,
    proto_tensor: _OutputTensor,
    coefficients: Sequence[float],
    box: DetectionBox,
    route: ReleaseContext,
) -> list[SegmentationPoint] | None:
    if len(proto_tensor.shape) < 3:
        return None
    channel_count = int(proto_tensor.shape[-3])
    height = max(int(proto_tensor.shape[-2]), 1)
    width = max(int(proto_tensor.shape[-1]), 1)
    area = height * width
    if channel_count <= 0 or len(proto_tensor.values) < channel_count * area:
        return None
    mask_threshold = float(
        (route.manifest.get("postprocessing") or {}).get("mask_threshold") or 0.5
    )
    try:
        import numpy as np

        proto = np.asarray(proto_tensor.values, dtype=np.float32).reshape(
            channel_count,
            height,
            width,
        )
        coeffs = np.asarray(coefficients[:channel_count], dtype=np.float32)
        logits = np.tensordot(coeffs, proto, axes=(0, 0))
        if mask_threshold <= 0:
            active_mask = np.ones((height, width), dtype=bool)
        elif mask_threshold >= 1:
            active_mask = np.zeros((height, width), dtype=bool)
        else:
            logit_threshold = math.log(mask_threshold / (1.0 - mask_threshold))
            active_mask = logits >= np.float32(logit_threshold)
        x_centers = (np.arange(width, dtype=np.float32) + 0.5) / float(width)
        y_centers = (np.arange(height, dtype=np.float32) + 0.5) / float(height)
        active_mask &= (
            (x_centers >= float(box.x_min)) & (x_centers <= float(box.x_max))
        )[None, :]
        active_mask &= (
            (y_centers >= float(box.y_min)) & (y_centers <= float(box.y_max))
        )[:, None]
    except (ImportError, TypeError, ValueError):
        active_indices: list[int] = []
        coeffs = list(coefficients[:channel_count])
        for index in range(area):
            x = (index % width + 0.5) / width
            y = (index // width + 0.5) / height
            if x < box.x_min or x > box.x_max or y < box.y_min or y > box.y_max:
                continue
            logit = sum(
                coeff * proto_tensor.values[channel * area + index]
                for channel, coeff in enumerate(coeffs)
            )
            if _sigmoid(logit) >= mask_threshold:
                active_indices.append(index)
        if not active_indices:
            return None
        return _mask_polygon_from_active_indices(
            active_indices,
            width=width,
            height=height,
        )
    if not bool(active_mask.any()):
        return None
    return _mask_polygon_from_binary_mask(active_mask)


def _segments_from_semantic_masks(
    *,
    route: ReleaseContext,
    masks: _OutputTensor,
    max_segments: int,
    score_threshold: float,
) -> list[SegmentationPrediction]:
    shape = masks.shape
    if len(shape) < 3:
        return []
    height = max(int(shape[-2]), 1)
    width = max(int(shape[-1]), 1)
    class_count = max(len(route.class_labels), 1)
    channel_count = max(int(shape[-3]), class_count)
    channels = _rows(masks.values, height * width)
    if len(channels) < channel_count and len(masks.values) == channel_count * height * width:
        channels = _rows(masks.values, height * width)
    results: list[SegmentationPrediction] = []
    for class_index in range(min(class_count, len(channels))):
        channel = channels[class_index]
        if not channel:
            continue
        max_score = max(channel)
        if max_score < score_threshold and _looks_like_probability_mask(channel):
            continue
        active_indices = [
            index for index, value in enumerate(channel) if value == max_score or value >= score_threshold
        ]
        if not active_indices:
            continue
        polygon = _mask_polygon_from_active_indices(
            active_indices,
            width=width,
            height=height,
        )
        if polygon is None:
            continue
        results.append(
            SegmentationPrediction(
                label=_label_for_index(route, class_index),
                score=round(_clip01(max_score), 6),
                polygon=polygon,
            )
        )
    return sorted(results, key=lambda item: item.score, reverse=True)[:max_segments]


def _output_by_role(outputs: dict[str, _OutputTensor], role: str) -> _OutputTensor | None:
    role_tokens = {
        "box": ("box", "bbox", "obb", "rbox", "xywhr", "det"),
        "score": ("score", "conf"),
        "label": ("label", "class"),
        "segment": ("segment", "polygon"),
        "mask": ("mask", "logit"),
    }[role]
    for name, tensor in outputs.items():
        normalized = name.lower()
        if any(token in normalized for token in role_tokens):
            return tensor
    return None


def _rows_from_tensor(tensor: _OutputTensor, *, default_width: int) -> list[list[float]]:
    width = default_width
    if tensor.shape:
        last_dim = int(tensor.shape[-1])
        if last_dim > 0:
            width = last_dim
    return _rows(tensor.values, width)


def _rows_from_model_output(
    tensor: _OutputTensor,
) -> Sequence[Sequence[float]]:
    shape = tensor.shape
    try:
        import numpy as np

        values = np.asarray(tensor.values)
        expected_size = math.prod(item for item in shape if item > 0)
        if shape and expected_size == values.size:
            shaped = values.reshape(shape)
            if len(shape) == 3:
                _, dim_a, dim_b = shape
                matrix = shaped[0]
                if dim_a > 0 and dim_b > 0 and dim_a < dim_b:
                    return matrix.T
                return matrix
            if len(shape) >= 2 and shape[-1] > 0:
                return shaped.reshape(-1, int(shape[-1]))
    except (ImportError, TypeError, ValueError):
        pass
    if len(shape) == 3:
        _, dim_a, dim_b = shape
        if dim_a > 0 and dim_b > 0 and dim_a < dim_b:
            return [
                [tensor.values[channel * dim_b + index] for channel in range(dim_a)]
                for index in range(dim_b)
            ]
        if dim_b > 0:
            return _rows(tensor.values, dim_b)
    if len(shape) >= 2 and shape[-1] > 0:
        return _rows(tensor.values, int(shape[-1]))
    return _rows(tensor.values, 6)


def _rows(values: Sequence[float], width: int) -> list[list[float]]:
    width = max(int(width), 1)
    return [
        [float(item) for item in values[index : index + width]]
        for index in range(0, len(values), width)
        if len(values[index : index + width]) == width
    ]


def _polygons_from_tensor(tensor: _OutputTensor) -> list[list[tuple[float, float]]]:
    shape = tensor.shape
    if len(shape) >= 3 and shape[-1] == 2:
        point_count = int(shape[-2])
        rows = _rows(tensor.values, point_count * 2)
        return [
            [(row[index], row[index + 1]) for index in range(0, len(row), 2)]
            for row in rows
        ]
    row_width = int(shape[-1]) if shape and shape[-1] > 0 else 8
    rows = _rows(tensor.values, row_width)
    return [
        [(row[index], row[index + 1]) for index in range(0, len(row) - 1, 2)]
        for row in rows
    ]


def _label_for_index(route: ReleaseContext, index: int) -> str:
    labels = route.class_labels or ["object"]
    if 0 <= index < len(labels):
        return labels[index]
    if 1 <= index <= len(labels):
        return labels[index - 1]
    return labels[0]


def _oriented_score_from_row(row: Sequence[float]) -> float:
    if len(row) >= 9 and _looks_like_corner_geometry(row[:8]):
        return float(row[8])
    if len(row) >= 6:
        return float(row[5])
    if len(row) >= 5:
        return float(row[4])
    return 0.0


def _looks_like_corner_detection_row(row: Sequence[float], *, class_count: int) -> bool:
    if len(row) < 10:
        return False
    label_value = row[9]
    if abs(label_value - round(label_value)) > 1e-6:
        return False
    label_index = int(round(label_value))
    return 0 <= label_index <= max(class_count, 1) and _looks_like_corner_geometry(row[:8])


def _looks_like_corner_geometry(values: Sequence[float]) -> bool:
    if len(values) < 8:
        return False
    xs = [float(values[index]) for index in range(0, 8, 2)]
    ys = [float(values[index]) for index in range(1, 8, 2)]
    return (max(xs) - min(xs)) > 0 and (max(ys) - min(ys)) > 0


def _oriented_box_from_box_values(
    values: Sequence[float],
    *,
    route: ReleaseContext,
) -> OrientedDetectionBox | None:
    if len(values) >= 8 and _looks_like_corner_geometry(values[:8]):
        return _oriented_box_from_corner_values(values[:8], route=route)
    if len(values) >= 5:
        return _oriented_box_from_xywhr_values(values[:5], route=route)
    return None


def _oriented_box_from_xywhr_values(
    values: Sequence[float],
    *,
    route: ReleaseContext,
) -> OrientedDetectionBox:
    center_x, center_y, box_width, box_height, angle = [float(item) for item in values[:5]]
    image_width, image_height = _classification_image_size(route)
    if abs(center_x) <= 1.0:
        center_x *= image_width
    if abs(center_y) <= 1.0:
        center_y *= image_height
    if abs(box_width) <= 1.0:
        box_width *= image_width
    if abs(box_height) <= 1.0:
        box_height *= image_height
    angle_radians = angle if abs(angle) <= (math.tau + 1e-6) else math.radians(angle)
    rotation_degrees = _normalize_degrees(math.degrees(angle_radians))
    cos_a = math.cos(angle_radians)
    sin_a = math.sin(angle_radians)
    half_width = max(abs(box_width), 0.0) / 2.0
    half_height = max(abs(box_height), 0.0) / 2.0
    points: list[DetectionPoint] = []
    for offset_x, offset_y in (
        (-half_width, -half_height),
        (half_width, -half_height),
        (half_width, half_height),
        (-half_width, half_height),
    ):
        point_x = center_x + offset_x * cos_a - offset_y * sin_a
        point_y = center_y + offset_x * sin_a + offset_y * cos_a
        points.append(
            DetectionPoint(
                x=_clip01(point_x / max(image_width, 1)),
                y=_clip01(point_y / max(image_height, 1)),
            )
        )
    return OrientedDetectionBox(points=points, rotation_degrees=rotation_degrees)


def _oriented_box_from_corner_values(
    values: Sequence[float],
    *,
    route: ReleaseContext,
) -> OrientedDetectionBox:
    normalized_points = _normalize_polygon_points(
        [(values[index], values[index + 1]) for index in range(0, 8, 2)],
        route=route,
    )
    width, height = _classification_image_size(route)
    rotation_degrees: float | None = None
    if len(normalized_points) >= 2:
        first, second = normalized_points[0], normalized_points[1]
        rotation_degrees = _normalize_degrees(
            math.degrees(
                math.atan2(
                    (second[1] - first[1]) * height,
                    (second[0] - first[0]) * width,
                )
            )
        )
    return OrientedDetectionBox(
        points=[DetectionPoint(x=x, y=y) for x, y in normalized_points[:4]],
        rotation_degrees=rotation_degrees,
    )


def _axis_aligned_box_from_oriented(oriented_box: OrientedDetectionBox) -> DetectionBox:
    xs = [point.x for point in oriented_box.points]
    ys = [point.y for point in oriented_box.points]
    return DetectionBox(
        x_min=_clip01(min(xs)),
        y_min=_clip01(min(ys)),
        x_max=_clip01(max(xs)),
        y_max=_clip01(max(ys)),
    )


def _normalize_degrees(value: float) -> float:
    normalized = (float(value) + 180.0) % 360.0 - 180.0
    return round(normalized, 6)


def _detection_box_from_values(values: Sequence[float], *, route: ReleaseContext) -> DetectionBox:
    x1, y1, x2, y2 = [float(item) for item in values[:4]]
    width, height = _classification_image_size(route)
    if max(abs(x1), abs(x2)) > 1.0:
        x1 /= width
        x2 /= width
    if max(abs(y1), abs(y2)) > 1.0:
        y1 /= height
        y2 /= height
    x_min = _clip01(min(x1, x2))
    y_min = _clip01(min(y1, y2))
    x_max = _clip01(max(x1, x2))
    y_max = _clip01(max(y1, y2))
    return DetectionBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


def _xywh_to_xyxy(values: Sequence[float]) -> list[float]:
    x, y, width, height = [float(item) for item in values[:4]]
    return [x - width / 2, y - height / 2, x + width / 2, y + height / 2]


def _normalize_polygon_points(
    points: Sequence[tuple[float, float]],
    *,
    route: ReleaseContext,
) -> list[tuple[float, float]]:
    width, height = _classification_image_size(route)
    normalized: list[tuple[float, float]] = []
    for x, y in points:
        if abs(x) > 1.0:
            x = x / width
        if abs(y) > 1.0:
            y = y / height
        normalized.append((_clip01(x), _clip01(y)))
    return normalized


def _box_polygon(x_min: float, y_min: float, x_max: float, y_max: float) -> list[SegmentationPoint]:
    return [
        SegmentationPoint(x=_clip01(x_min), y=_clip01(y_min)),
        SegmentationPoint(x=_clip01(x_max), y=_clip01(y_min)),
        SegmentationPoint(x=_clip01(x_max), y=_clip01(y_max)),
        SegmentationPoint(x=_clip01(x_min), y=_clip01(y_max)),
    ]


def _mask_polygon_from_active_indices(
    active_indices: Sequence[int],
    *,
    width: int,
    height: int,
) -> list[SegmentationPoint] | None:
    width = max(int(width), 1)
    height = max(int(height), 1)
    try:
        import numpy as np

        active_mask = np.zeros((height, width), dtype=bool)
        indices = np.asarray(active_indices, dtype=np.int64)
        indices = indices[(indices >= 0) & (indices < width * height)]
        if indices.size == 0:
            return None
        active_mask.reshape(-1)[indices] = True
        return _mask_polygon_from_binary_mask(active_mask)
    except (ImportError, TypeError, ValueError):
        active_cells = {
            (int(index) % width, int(index) // width)
            for index in active_indices
            if 0 <= int(index) < width * height
        }
    return _mask_polygon_from_active_cells(
        active_cells,
        width=width,
        height=height,
    )


def _mask_polygon_from_binary_mask(mask: Any) -> list[SegmentationPoint] | None:
    import numpy as np

    active_mask = np.asarray(mask, dtype=bool)
    if active_mask.ndim != 2 or not bool(active_mask.any()):
        return None
    try:
        import cv2

        mask_u8 = np.ascontiguousarray(active_mask, dtype=np.uint8)
        contours, _hierarchy = cv2.findContours(
            mask_u8,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        if contours:
            contour = max(
                contours,
                key=lambda item: (float(cv2.contourArea(item)), float(cv2.arcLength(item, True))),
            )
            perimeter = float(cv2.arcLength(contour, True))
            approximated = cv2.approxPolyDP(
                contour,
                epsilon=max(perimeter * 0.002, 0.05),
                closed=True,
            ).reshape(-1, 2)
            if len(approximated) < 3:
                x, y, box_width, box_height = cv2.boundingRect(contour)
                approximated = np.asarray(
                    [
                        [x, y],
                        [x + box_width - 1, y],
                        [x + box_width - 1, y + box_height - 1],
                        [x, y + box_height - 1],
                    ],
                    dtype=np.int32,
                )
            center_x = float(approximated[:, 0].min() + approximated[:, 0].max()) / 2.0
            center_y = float(approximated[:, 1].min() + approximated[:, 1].max()) / 2.0
            height, width = active_mask.shape
            points = _dedupe_collinear_points(
                [
                    (
                        _clip01((float(x) + (1.0 if float(x) > center_x else 0.0)) / width),
                        _clip01((float(y) + (1.0 if float(y) > center_y else 0.0)) / height),
                    )
                    for x, y in approximated
                ]
            )
            if len(points) > 256:
                stride = math.ceil(len(points) / 256)
                points = points[::stride]
            if len(points) >= 3:
                return [SegmentationPoint(x=x, y=y) for x, y in points]
    except (ImportError, TypeError, ValueError):
        pass
    return _mask_polygon_from_binary_mask_edges(active_mask)


def _mask_polygon_from_binary_mask_edges(active_mask: Any) -> list[SegmentationPoint] | None:
    import numpy as np

    active_mask = np.asarray(active_mask, dtype=bool)
    height, width = active_mask.shape
    above = np.zeros_like(active_mask)
    above[1:, :] = active_mask[:-1, :]
    right = np.zeros_like(active_mask)
    right[:, :-1] = active_mask[:, 1:]
    below = np.zeros_like(active_mask)
    below[:-1, :] = active_mask[1:, :]
    left = np.zeros_like(active_mask)
    left[:, 1:] = active_mask[:, :-1]

    edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for y, x in zip(*np.nonzero(active_mask & ~above), strict=False):
        edges.append(((int(x), int(y)), (int(x) + 1, int(y))))
    for y, x in zip(*np.nonzero(active_mask & ~right), strict=False):
        edges.append(((int(x) + 1, int(y)), (int(x) + 1, int(y) + 1)))
    for y, x in zip(*np.nonzero(active_mask & ~below), strict=False):
        edges.append(((int(x) + 1, int(y) + 1), (int(x), int(y) + 1)))
    for y, x in zip(*np.nonzero(active_mask & ~left), strict=False):
        edges.append(((int(x), int(y) + 1), (int(x), int(y))))
    return _polygon_from_mask_edges(edges, width=width, height=height)


def _mask_polygon_from_active_cells(
    active_cells: set[tuple[int, int]],
    *,
    width: int,
    height: int,
) -> list[SegmentationPoint] | None:
    if not active_cells:
        return None
    edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for x, y in active_cells:
        if (x, y - 1) not in active_cells:
            edges.append(((x, y), (x + 1, y)))
        if (x + 1, y) not in active_cells:
            edges.append(((x + 1, y), (x + 1, y + 1)))
        if (x, y + 1) not in active_cells:
            edges.append(((x + 1, y + 1), (x, y + 1)))
        if (x - 1, y) not in active_cells:
            edges.append(((x, y + 1), (x, y)))
    return _polygon_from_mask_edges(edges, width=width, height=height)


def _polygon_from_mask_edges(
    edges: Sequence[tuple[tuple[int, int], tuple[int, int]]],
    *,
    width: int,
    height: int,
) -> list[SegmentationPoint] | None:
    if not edges:
        return None

    loop = _largest_boundary_loop(edges)
    if len(loop) < 3:
        return None

    points = _dedupe_collinear_points(
        [
            (
                _clip01(x / width),
                _clip01(y / height),
            )
            for x, y in loop
        ]
    )
    if len(points) > 256:
        stride = math.ceil(len(points) / 256)
        points = points[::stride]
    if len(points) < 3:
        return None
    return [SegmentationPoint(x=x, y=y) for x, y in points]


def _largest_boundary_loop(
    edges: Sequence[tuple[tuple[int, int], tuple[int, int]]],
) -> list[tuple[int, int]]:
    outgoing: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for start, end in edges:
        outgoing.setdefault(start, []).append(end)
    for vertices in outgoing.values():
        vertices.sort()

    unused = set(edges)
    loops: list[list[tuple[int, int]]] = []
    while unused:
        start, end = min(unused)
        unused.remove((start, end))
        loop = [start]
        current = end
        guard = len(edges) + 1
        while current != start and guard > 0:
            loop.append(current)
            next_edge = next(
                (
                    (current, candidate)
                    for candidate in outgoing.get(current, [])
                    if (current, candidate) in unused
                ),
                None,
            )
            if next_edge is None:
                break
            unused.remove(next_edge)
            current = next_edge[1]
            guard -= 1
        if current == start and len(loop) >= 3:
            loops.append(loop)
    if not loops:
        return []
    return max(loops, key=_grid_polygon_area)


def _grid_polygon_area(points: Sequence[tuple[int, int]]) -> float:
    if len(points) < 3:
        return 0.0
    area = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _dedupe_collinear_points(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    deduped: list[tuple[float, float]] = []
    for point in points:
        if not deduped or point != deduped[-1]:
            deduped.append(point)
    if len(deduped) > 1 and deduped[0] == deduped[-1]:
        deduped.pop()
    if len(deduped) < 3:
        return deduped

    simplified: list[tuple[float, float]] = []
    for index, current in enumerate(deduped):
        previous = deduped[index - 1]
        following = deduped[(index + 1) % len(deduped)]
        cross = (current[0] - previous[0]) * (following[1] - current[1]) - (
            current[1] - previous[1]
        ) * (following[0] - current[0])
        if abs(cross) > 1e-12:
            simplified.append(current)
    return simplified


def _looks_like_probability_mask(values: Sequence[float]) -> bool:
    return bool(values) and min(values) >= 0.0 and max(values) <= 1.0


def _nms(detections: list[DetectionPrediction], *, iou_threshold: float) -> list[DetectionPrediction]:
    kept: list[DetectionPrediction] = []
    for detection in detections:
        if all(_box_iou(detection.box, kept_detection.box) <= iou_threshold for kept_detection in kept):
            kept.append(detection)
    return kept


def _box_iou(left: DetectionBox, right: DetectionBox) -> float:
    x_min = max(left.x_min, right.x_min)
    y_min = max(left.y_min, right.y_min)
    x_max = min(left.x_max, right.x_max)
    y_max = min(left.y_max, right.y_max)
    intersection = max(x_max - x_min, 0.0) * max(y_max - y_min, 0.0)
    left_area = max(left.x_max - left.x_min, 0.0) * max(left.y_max - left.y_min, 0.0)
    right_area = max(right.x_max - right.x_min, 0.0) * max(right.y_max - right.y_min, 0.0)
    return intersection / max(left_area + right_area - intersection, 1e-9)


def _clip01(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _classification_labels_from_scores(
    *,
    route: ReleaseContext,
    scores: Sequence[float],
    top_k: int,
) -> list[ClassificationLabelScore]:
    labels = route.class_labels
    if not labels:
        raise PlatformError(
            code="class_map_missing",
            message="The deployment release does not expose classification labels.",
            status_code=500,
            metadata={"deployment_release_id": route.deployment_release_id},
        )
    if len(scores) != len(labels):
        raise PlatformError(
            code="classification_output_mismatch",
            message="The model output count does not match the deployment class map.",
            status_code=500,
            metadata={
                "deployment_release_id": route.deployment_release_id,
                "output_count": len(scores),
                "class_count": len(labels),
            },
        )
    probabilities = _probabilities(scores)
    limit = max(1, min(int(top_k or 1), len(labels)))
    ranked = sorted(
        zip(labels, probabilities, strict=False),
        key=lambda item: item[1],
        reverse=True,
    )
    return [
        ClassificationLabelScore(label=label, score=round(float(score), 6), rank=index + 1)
        for index, (label, score) in enumerate(ranked[:limit])
    ]


def _classification_labels_from_outputs(
    *,
    route: ReleaseContext,
    outputs: dict[str, _OutputTensor],
    fallback_output_name: str,
    top_k: int,
) -> list[ClassificationLabelScore]:
    labels = _classification_labels_from_topk_outputs(route=route, outputs=outputs, top_k=top_k)
    if labels is not None:
        return labels
    scores = _tensor_values(outputs, output_name=fallback_output_name)
    return _classification_labels_from_scores(route=route, scores=scores, top_k=top_k)


def _classification_labels_from_topk_outputs(
    *,
    route: ReleaseContext,
    outputs: dict[str, _OutputTensor],
    top_k: int,
) -> list[ClassificationLabelScore] | None:
    class_count = len(route.class_labels)
    if class_count <= 0 or len(outputs) < 2:
        return None
    tensors = list(outputs.values())
    index_candidates = [
        tensor for tensor in tensors if _looks_like_class_index_tensor(tensor, class_count=class_count)
    ]
    if not index_candidates:
        return None
    for index_tensor in index_candidates:
        score_candidates = [
            tensor
            for tensor in tensors
            if tensor is not index_tensor
            and len(tensor.values) == len(index_tensor.values)
            and _looks_like_score_tensor(tensor)
            and all(tensor is not candidate for candidate in index_candidates)
        ]
        if not score_candidates:
            score_candidates = [
                tensor
                for tensor in tensors
                if tensor is not index_tensor
                and len(tensor.values) == len(index_tensor.values)
                and _looks_like_score_tensor(tensor)
            ]
        if not score_candidates:
            continue
        return _classification_labels_from_topk(
            route=route,
            scores=score_candidates[0].values,
            indices=index_tensor.values,
            top_k=top_k,
        )
    return None


def _classification_labels_from_topk(
    *,
    route: ReleaseContext,
    scores: Sequence[float],
    indices: Sequence[float],
    top_k: int,
) -> list[ClassificationLabelScore]:
    labels = route.class_labels
    class_count = len(labels)
    parsed_indices = [int(round(float(item))) for item in indices]
    one_based = (
        bool(parsed_indices)
        and 0 not in parsed_indices
        and max(parsed_indices) == class_count
        and all(1 <= index <= class_count for index in parsed_indices)
    )
    probabilities = _probabilities(scores)
    pairs: list[tuple[str, float]] = []
    for raw_index, score in zip(parsed_indices, probabilities, strict=False):
        label_index = raw_index - 1 if one_based else raw_index
        if 0 <= label_index < class_count:
            pairs.append((labels[label_index], float(score)))
    pairs.sort(key=lambda item: item[1], reverse=True)
    limit = max(1, min(int(top_k or 1), len(pairs)))
    return [
        ClassificationLabelScore(label=label, score=round(_clip01(score), 6), rank=index + 1)
        for index, (label, score) in enumerate(pairs[:limit])
    ]


def _looks_like_class_index_tensor(tensor: _OutputTensor, *, class_count: int) -> bool:
    values = tensor.values
    if len(values) == 0 or len(values) > max(class_count, 1):
        return False
    parsed: list[int] = []
    for value in values:
        if not math.isfinite(float(value)) or abs(float(value) - round(float(value))) > 1e-6:
            return False
        parsed.append(int(round(float(value))))
    if len(set(parsed)) != len(parsed):
        return False
    zero_based = all(0 <= index < class_count for index in parsed)
    one_based = all(1 <= index <= class_count for index in parsed)
    return zero_based or one_based


def _looks_like_score_tensor(tensor: _OutputTensor) -> bool:
    return len(tensor.values) > 0 and all(
        math.isfinite(float(item)) for item in tensor.values
    )


def _probabilities(scores: Sequence[float]) -> list[float]:
    values = [float(item) for item in scores]
    total = sum(values)
    if values and all(0.0 <= item <= 1.0 for item in values) and 0.999 <= total <= 1.001:
        return values
    shifted = [item - max(values or [0.0]) for item in values]
    exp_values = [math.exp(max(min(item, 60.0), -60.0)) for item in shifted]
    exp_total = sum(exp_values) or 1.0
    return [item / exp_total for item in exp_values]


def _digest_words(*, image_bytes: bytes, salt: str) -> list[int]:

    digest = hashlib.sha256(image_bytes + salt.encode("utf-8")).hexdigest()
    return [int(digest[index : index + 4], 16) for index in range(0, len(digest), 4)]


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000.0, 3)


def _read_manifest_payload(
    path_or_uri: str,
    *,
    max_bytes: int,
    allowed_root: Path,
) -> dict[str, Any]:
    path = _uri_to_path(path_or_uri)
    ensure_path_within_root(root=allowed_root, target=path)
    if path.stat().st_size > max_bytes:
        raise PlatformError(
            code="batch_manifest_too_large",
            message="The batch input manifest exceeds the configured size limit.",
            status_code=413,
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {"items": payload}
    raise ValueError(f"Batch manifest {path} does not contain a valid JSON object or list.")


def _payload_items(payload: dict[str, Any]) -> list[BatchInputItem]:
    raw_items = payload.get("inputs") or payload.get("items") or []
    if isinstance(raw_items, list):
        return [BatchInputItem.model_validate(item) for item in raw_items]
    raise ValueError("Batch manifest payload does not contain a list of inputs.")


def _uri_to_path(path_or_uri: str) -> Path:
    parsed = urlparse(path_or_uri)
    if parsed.scheme == "file":
        from urllib.request import url2pathname

        path_str = url2pathname(parsed.path)
        if parsed.netloc:
            path_str = f"//{parsed.netloc}{path_str}"
        if len(path_str) >= 3 and path_str[0] == "/" and path_str[2] == ":":
            path_str = path_str[1:]
        return Path(path_str)
    return Path(path_or_uri)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows),
        encoding="utf-8",
    )


def _latency_metrics(values: list[float]) -> LatencyQuantiles:
    if not values:
        return LatencyQuantiles()
    ordered = sorted(values)
    return LatencyQuantiles(
        sample_count=len(values),
        p50_ms=_select_quantile(ordered, 0.50),
        p95_ms=_select_quantile(ordered, 0.95),
        p99_ms=_select_quantile(ordered, 0.99),
        throughput_per_second=round(len(values) / max(sum(values) / 1000.0, 1e-6), 3),
    )


def _select_quantile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = min(max(math.ceil(len(values) * quantile) - 1, 0), len(values) - 1)
    return round(values[index], 3)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)
