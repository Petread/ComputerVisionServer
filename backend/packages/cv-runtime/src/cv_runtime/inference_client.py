from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal, Protocol

import httpx
from cv_contracts import (
    AnomalyInferenceResponse,
    ClassificationInferenceResponse,
    DetectionInferenceResponse,
    RealtimeAnomalyRequest,
    RealtimeClassifyRequest,
    RealtimeDetectRequest,
    RealtimeSegmentRequest,
    RealtimeTemporalSequenceRequest,
    SegmentationInferenceResponse,
    TemporalSequenceInferenceResponse,
)

from cv_runtime.errors import RuntimeNodeExecutionError


class RuntimeInferenceClient(Protocol):
    def temporal_sequence(
        self, payload: RealtimeTemporalSequenceRequest
    ) -> TemporalSequenceInferenceResponse: ...

    def anomaly(self, payload: RealtimeAnomalyRequest) -> AnomalyInferenceResponse: ...

    def anomaly_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        threshold: float | None = None,
        return_mask: bool = False,
    ) -> AnomalyInferenceResponse: ...

    def classify(self, payload: RealtimeClassifyRequest) -> ClassificationInferenceResponse: ...

    def classify_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        top_k: int = 1,
    ) -> ClassificationInferenceResponse: ...

    def detect(self, payload: RealtimeDetectRequest) -> DetectionInferenceResponse: ...

    def detect_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        max_detections: int = 100,
        score_threshold: float = 0.35,
    ) -> DetectionInferenceResponse: ...

    def segment(self, payload: RealtimeSegmentRequest) -> SegmentationInferenceResponse: ...

    def segment_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        max_segments: int = 50,
        score_threshold: float = 0.35,
    ) -> SegmentationInferenceResponse: ...


@dataclass
class HttpInferenceGatewayClient:
    base_url: str
    bearer_token: str | None = None
    timeout_seconds: float = 30.0
    overload_policy: Literal["fail", "wait"] = "fail"
    overload_retry_interval_seconds: float = 0.025
    overload_retry_max_interval_seconds: float = 0.25
    _client: httpx.Client | None = field(default=None, init=False, repr=False)
    _client_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def temporal_sequence(
        self, payload: RealtimeTemporalSequenceRequest
    ) -> TemporalSequenceInferenceResponse:
        return TemporalSequenceInferenceResponse.model_validate(
            self._post(
                "/api/v1/realtime/temporal-sequence",
                payload.model_dump(mode="json"),
            )
        )

    def classify(self, payload: RealtimeClassifyRequest) -> ClassificationInferenceResponse:
        return ClassificationInferenceResponse.model_validate(
            self._post("/api/v1/realtime/classify", payload.model_dump(mode="json"))
        )

    def classify_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        top_k: int = 1,
    ) -> ClassificationInferenceResponse:
        params: dict[str, object] = {
            "deployment_target": deployment_target,
            "top_k": top_k,
        }
        if deployment_release_id:
            params["deployment_release_id"] = deployment_release_id
        if timeout_ms is not None:
            params["timeout_ms"] = timeout_ms
        return ClassificationInferenceResponse.model_validate(
            self._post_content(
                "/api/v1/realtime/classify-bytes",
                content=image_bytes,
                params=params,
                content_type=mime_type,
            )
        )

    def anomaly(self, payload: RealtimeAnomalyRequest) -> AnomalyInferenceResponse:
        return AnomalyInferenceResponse.model_validate(
            self._post("/api/v1/realtime/anomaly", payload.model_dump(mode="json"))
        )

    def anomaly_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        threshold: float | None = None,
        return_mask: bool = False,
    ) -> AnomalyInferenceResponse:
        params: dict[str, object] = {
            "deployment_target": deployment_target,
            "return_mask": return_mask,
        }
        if deployment_release_id:
            params["deployment_release_id"] = deployment_release_id
        if timeout_ms is not None:
            params["timeout_ms"] = timeout_ms
        if threshold is not None:
            params["threshold"] = threshold
        return AnomalyInferenceResponse.model_validate(
            self._post_content(
                "/api/v1/realtime/anomaly-bytes",
                content=image_bytes,
                params=params,
                content_type=mime_type,
            )
        )

    def detect(self, payload: RealtimeDetectRequest) -> DetectionInferenceResponse:
        return DetectionInferenceResponse.model_validate(
            self._post("/api/v1/realtime/detect", payload.model_dump(mode="json"))
        )

    def detect_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        max_detections: int = 100,
        score_threshold: float = 0.35,
    ) -> DetectionInferenceResponse:
        params: dict[str, object] = {
            "deployment_target": deployment_target,
            "max_detections": max_detections,
            "score_threshold": score_threshold,
        }
        if deployment_release_id:
            params["deployment_release_id"] = deployment_release_id
        if timeout_ms is not None:
            params["timeout_ms"] = timeout_ms
        return DetectionInferenceResponse.model_validate(
            self._post_content(
                "/api/v1/realtime/detect-bytes",
                content=image_bytes,
                params=params,
                content_type=mime_type,
            )
        )

    def segment(self, payload: RealtimeSegmentRequest) -> SegmentationInferenceResponse:
        return SegmentationInferenceResponse.model_validate(
            self._post("/api/v1/realtime/segment", payload.model_dump(mode="json"))
        )

    def segment_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        max_segments: int = 50,
        score_threshold: float = 0.35,
    ) -> SegmentationInferenceResponse:
        params: dict[str, object] = {
            "deployment_target": deployment_target,
            "max_segments": max_segments,
            "score_threshold": score_threshold,
        }
        if deployment_release_id:
            params["deployment_release_id"] = deployment_release_id
        if timeout_ms is not None:
            params["timeout_ms"] = timeout_ms
        return SegmentationInferenceResponse.model_validate(
            self._post_content(
                "/api/v1/realtime/segment-bytes",
                content=image_bytes,
                params=params,
                content_type=mime_type,
            )
        )

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        return self._post_content(path, json_payload=payload)

    def _post_content(
        self,
        path: str,
        *,
        json_payload: dict[str, object] | None = None,
        content: bytes | None = None,
        params: dict[str, object] | None = None,
        content_type: str | None = None,
    ) -> dict[str, object]:
        timeout_seconds = _effective_timeout_seconds(
            self.timeout_seconds,
            json_payload=json_payload,
            params=params,
        )
        deadline = time.monotonic() + timeout_seconds
        attempts = 0
        retry_interval = max(float(self.overload_retry_interval_seconds), 0.001)
        while True:
            attempts += 1
            try:
                remaining_timeout = max(deadline - time.monotonic(), 0.001)
                request_kwargs: dict[str, object] = {"timeout": remaining_timeout}
                if json_payload is not None:
                    request_kwargs["json"] = json_payload
                if content is not None:
                    request_kwargs["content"] = content
                if params is not None:
                    request_kwargs["params"] = params
                if content_type:
                    request_kwargs["headers"] = {"Content-Type": content_type}
                response = self._http_client().post(
                    path,
                    **request_kwargs,
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text
                error_payload = _gateway_error_payload(exc.response)
                if (
                    exc.response.status_code == 429
                    and self.overload_policy == "wait"
                    and error_payload.get("code")
                    in {
                        "gateway_overloaded",
                        "model_overloaded",
                        "inference_gateway_overloaded",
                    }
                    and time.monotonic() < deadline
                ):
                    retry_after = _retry_after_seconds(exc.response)
                    time.sleep(
                        min(
                            max(retry_after or retry_interval, 0.0),
                            max(deadline - time.monotonic(), 0.0),
                        )
                    )
                    retry_interval = min(
                        retry_interval * 2,
                        max(float(self.overload_retry_max_interval_seconds), retry_interval),
                    )
                    continue
                raise _gateway_http_error(
                    exc.response.status_code,
                    detail=detail,
                    attempts=attempts,
                    payload=error_payload,
                ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._reset_http_client()
                raise RuntimeNodeExecutionError(
                    code="inference_gateway_unreachable",
                    message="Inference gateway could not be reached.",
                    retryable=True,
                    metadata={
                        "detail": f"{type(exc).__name__}: {exc}",
                        "transport_error": type(exc).__name__,
                        "client_reset": True,
                    },
                ) from exc

    def _http_client(self) -> httpx.Client:
        client = self._client
        if client is not None:
            return client
        with self._client_lock:
            client = self._client
            if client is None:
                client = httpx.Client(
                    base_url=self.base_url.rstrip("/"),
                    headers={
                        "Content-Type": "application/json",
                        **(
                            {"Authorization": f"Bearer {self.bearer_token}"}
                            if self.bearer_token
                            else {}
                        ),
                    },
                    timeout=self.timeout_seconds,
                    trust_env=False,
                )
                self._client = client
            return client

    def _reset_http_client(self) -> None:
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def close(self) -> None:
        self._reset_http_client()


def _gateway_http_error(
    status_code: int,
    *,
    detail: str,
    attempts: int,
    payload: dict[str, object] | None = None,
) -> RuntimeNodeExecutionError:
    payload = payload or {}
    code = payload.get("code")
    message = payload.get("message")
    if (
        isinstance(code, str)
        and 3 <= len(code) <= 120
        and code.isascii()
        and code == code.lower()
        and code[0].isalpha()
        and code.replace("_", "").isalnum()
    ):
        retryable_value = payload.get("retryable")
        retryable = (
            retryable_value if isinstance(retryable_value, bool) else status_code >= 500
        )
        metadata: dict[str, object] = {
            "status_code": status_code,
            "detail": detail[:1000],
            "attempts": attempts,
        }
        correlation_id = payload.get("correlation_id")
        if isinstance(correlation_id, str) and correlation_id:
            metadata["correlation_id"] = correlation_id
        gateway_metadata = payload.get("metadata")
        if isinstance(gateway_metadata, dict) and gateway_metadata:
            metadata["gateway_metadata"] = gateway_metadata
        return RuntimeNodeExecutionError(
            code=code,
            message=(
                message
                if isinstance(message, str) and message
                else f"Inference gateway returned HTTP {status_code}."
            ),
            retryable=retryable,
            metadata=metadata,
        )
    if status_code == 429:
        return RuntimeNodeExecutionError(
            code="inference_gateway_overloaded",
            message="Inference gateway is overloaded.",
            retryable=True,
            metadata={
                "status_code": status_code,
                "detail": detail[:1000],
                "attempts": attempts,
            },
        )
    return RuntimeNodeExecutionError(
        code="inference_gateway_error",
        message=f"Inference gateway returned HTTP {status_code}.",
        retryable=status_code >= 500,
        metadata={"status_code": status_code, "detail": detail[:1000], "attempts": attempts},
    )


def _gateway_error_payload(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _retry_after_seconds(response: httpx.Response) -> float | None:
    headers = getattr(response, "headers", {})
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return None


def _effective_timeout_seconds(
    configured_timeout_seconds: float,
    *,
    json_payload: dict[str, object] | None,
    params: dict[str, object] | None,
) -> float:
    request_timeout_ms = None
    if json_payload is not None:
        request_timeout_ms = json_payload.get("timeout_ms")
    if request_timeout_ms is None and params is not None:
        request_timeout_ms = params.get("timeout_ms")
    try:
        request_timeout_seconds = (
            float(request_timeout_ms) / 1000 if request_timeout_ms is not None else 0.0
        )
    except (TypeError, ValueError):
        request_timeout_seconds = 0.0
    return max(float(configured_timeout_seconds), request_timeout_seconds + 0.25, 0.001)
