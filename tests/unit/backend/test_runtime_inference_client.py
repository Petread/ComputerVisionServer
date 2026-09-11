from __future__ import annotations

import json

import httpx
import pytest
from cv_contracts import (
    RealtimeClassifyRequest,
    RealtimeTemporalSequenceRequest,
    TemporalSequenceFrame,
)
from cv_runtime import HttpInferenceGatewayClient, RuntimeNodeExecutionError
from pytest import MonkeyPatch


def test_inference_client_waits_and_retries_gateway_overload(monkeypatch: MonkeyPatch) -> None:
    calls: list[float] = []

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.closed = False

        def post(self, _path: str, *, json: dict[str, object], timeout: float) -> _JsonResponse:
            assert json["image_base64"]
            calls.append(timeout)
            if len(calls) == 1:
                return _JsonResponse({"code": "gateway_overloaded"}, status_code=429)
            return _JsonResponse(_classification_response())

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("httpx.Client", FakeClient)

    client = HttpInferenceGatewayClient(
        base_url="http://gateway",
        timeout_seconds=1.0,
        overload_policy="wait",
        overload_retry_interval_seconds=0.0,
    )

    response = client.classify(
        RealtimeClassifyRequest(
            deployment_target="realtime",
            image_base64="aW1hZ2UtYnl0ZXMtcGxhY2Vob2xkZXI=",
        )
    )

    assert response.labels[0].label == "ok"
    assert len(calls) == 2
    client.close()


def test_inference_client_does_not_retry_rate_limit_until_transport_timeout(
    monkeypatch: MonkeyPatch,
) -> None:
    calls = 0

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def post(
            self,
            _path: str,
            *,
            json: dict[str, object],
            timeout: float,
        ) -> _JsonResponse:
            nonlocal calls
            del json, timeout
            calls += 1
            return _JsonResponse(
                {
                    "code": "rate_limit_exceeded",
                    "message": "The request rate exceeded its configured limit.",
                    "retryable": False,
                },
                status_code=429,
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("httpx.Client", FakeClient)
    client = HttpInferenceGatewayClient(
        base_url="http://gateway",
        timeout_seconds=1.0,
        overload_policy="wait",
    )

    with pytest.raises(RuntimeNodeExecutionError) as caught:
        client.classify(
            RealtimeClassifyRequest(
                deployment_target="realtime",
                image_base64="aW1hZ2UtYnl0ZXMtcGxhY2Vob2xkZXI=",
            )
        )

    assert calls == 1
    assert caught.value.error.code == "rate_limit_exceeded"
    assert caught.value.error.retryable is False
    client.close()


def test_inference_client_can_send_classification_bytes(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.closed = False

        def post(
            self,
            path: str,
            *,
            content: bytes,
            params: dict[str, object],
            headers: dict[str, str],
            timeout: float,
        ) -> _JsonResponse:
            captured.update(
                {
                    "path": path,
                    "content": content,
                    "params": params,
                    "headers": headers,
                    "timeout": timeout,
                }
            )
            return _JsonResponse(_classification_response())

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("httpx.Client", FakeClient)

    client = HttpInferenceGatewayClient(base_url="http://gateway", timeout_seconds=1.0)

    response = client.classify_bytes(
        deployment_target="realtime",
        deployment_release_id="rel_classifier",
        image_bytes=b"jpeg-bytes",
        mime_type="image/jpeg",
        timeout_ms=750,
        top_k=2,
    )

    assert response.labels[0].label == "ok"
    assert captured["path"] == "/api/v1/realtime/classify-bytes"
    assert captured["content"] == b"jpeg-bytes"
    assert captured["params"] == {
        "deployment_target": "realtime",
        "deployment_release_id": "rel_classifier",
        "timeout_ms": 750,
        "top_k": 2,
    }
    assert captured["headers"] == {"Content-Type": "image/jpeg"}
    client.close()


def test_inference_client_honors_request_timeout_budget(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def post(self, _path: str, *, json: dict[str, object], timeout: float) -> _JsonResponse:
            captured["timeout"] = timeout
            return _JsonResponse(_classification_response())

        def close(self) -> None:
            pass

    monkeypatch.setattr("httpx.Client", FakeClient)

    client = HttpInferenceGatewayClient(base_url="http://gateway", timeout_seconds=1.0)

    client.classify(
        RealtimeClassifyRequest(
            deployment_target="realtime",
            image_base64="aW1hZ2UtYnl0ZXMtcGxhY2Vob2xkZXI=",
            timeout_ms=3000,
        )
    )

    assert captured["timeout"] > 3.0
    client.close()


def test_inference_client_rotates_transport_after_timeout(
    monkeypatch: MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.closed = False
            clients.append(self)

        def post(
            self,
            path: str,
            *,
            json: dict[str, object],
            timeout: float,
        ) -> _JsonResponse:
            del json, timeout
            if len(clients) == 1:
                raise httpx.ReadTimeout(
                    "timed out",
                    request=httpx.Request("POST", f"http://gateway{path}"),
                )
            return _JsonResponse(_classification_response())

        def close(self) -> None:
            self.closed = True

    clients: list[FakeClient] = []
    monkeypatch.setattr("httpx.Client", FakeClient)
    client = HttpInferenceGatewayClient(base_url="http://gateway", timeout_seconds=1.0)
    payload = RealtimeClassifyRequest(
        deployment_target="realtime",
        image_base64="aW1hZ2UtYnl0ZXMtcGxhY2Vob2xkZXI=",
    )

    with pytest.raises(RuntimeNodeExecutionError) as caught:
        client.classify(payload)

    assert caught.value.error.metadata["transport_error"] == "ReadTimeout"
    assert caught.value.error.metadata["client_reset"] is True
    assert clients[0].closed is True

    response = client.classify(payload)

    assert response.labels[0].label == "ok"
    assert len(clients) == 2
    client.close()


def test_inference_client_preserves_structured_gateway_timeout(
    monkeypatch: MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def post(self, _path: str, *, json: dict[str, object], timeout: float) -> _JsonResponse:
            return _JsonResponse(
                {
                    "code": "inference_timeout",
                    "message": "The inference request exceeded its timeout budget.",
                    "status_code": 504,
                    "correlation_id": "req_timeout",
                    "retryable": True,
                    "metadata": {"deployment_release_id": "rel_segmenter"},
                },
                status_code=504,
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("httpx.Client", FakeClient)
    client = HttpInferenceGatewayClient(base_url="http://gateway", timeout_seconds=1.0)

    with pytest.raises(RuntimeNodeExecutionError) as caught:
        client.classify(
            RealtimeClassifyRequest(
                deployment_target="realtime",
                image_base64="aW1hZ2UtYnl0ZXMtcGxhY2Vob2xkZXI=",
            )
        )

    assert caught.value.error.code == "inference_timeout"
    assert caught.value.error.message == "The inference request exceeded its timeout budget."
    assert caught.value.error.retryable is True
    assert caught.value.error.metadata["status_code"] == 504
    assert caught.value.error.metadata["correlation_id"] == "req_timeout"
    assert caught.value.error.metadata["gateway_metadata"] == {
        "deployment_release_id": "rel_segmenter"
    }
    client.close()


def test_inference_client_sends_temporal_sequence_to_existing_realtime_gateway(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def post(self, path: str, *, json: dict[str, object], timeout: float) -> _JsonResponse:
            captured.update({"path": path, "json": json, "timeout": timeout})
            return _JsonResponse(_temporal_response())

        def close(self) -> None:
            pass

    monkeypatch.setattr("httpx.Client", FakeClient)
    client = HttpInferenceGatewayClient(base_url="http://gateway", timeout_seconds=1.0)
    response = client.temporal_sequence(
        RealtimeTemporalSequenceRequest(
            deployment_target="realtime",
            deployment_release_id="rel_temporal",
            sequence_id="sequence-1",
            track_id="track-1",
            feature_set_id="features.v1",
            feature_schema_version="stem.geometry.v1",
            rotation_schema_version="stem.surface-rotation.v1",
            calibration_schema_version="1.0.0",
            rotation_viewpoint_convention="viewed_from_log_end_a",
            positive_rotation_direction="clockwise_viewed_from_log_end_a",
            frames=[
                TemporalSequenceFrame(
                    frame_index=4,
                    timestamp_seconds=0.3,
                    values={"geometry": {"signed_curvature": 0.2}},
                )
            ],
            current_frame_index=4,
            decision_frame_index=4,
        )
    )

    assert captured["path"] == "/api/v1/realtime/temporal-sequence"
    assert captured["json"]["post_decision_frames_included"] is False  # type: ignore[index]
    assert response.signed_correction_deg == pytest.approx(-12.0)
    assert response.snapshot_id == "snapshot-1"
    client.close()


class _JsonResponse:
    def __init__(self, payload: dict[str, object], *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.request = httpx.Request("POST", "http://gateway/api/v1/realtime/classify")

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "gateway error",
                request=self.request,
                response=self,
            )


def _classification_response() -> dict[str, object]:
    return {
        "schema_version": "inference.classification.response.v1",
        "request_id": "req_retry",
        "correlation_id": "corr_retry",
        "deployment_target": "realtime",
        "route": {
            "deployment_release_id": "rel_classifier",
            "target_path": "realtime",
            "release_status": "activated",
            "model_family_slug": "classifier",
            "model_version_id": "mvr_classifier",
            "task_type": "classification",
            "triton_model_name": "classifier",
            "triton_model_version": 1,
            "contract_major": 1,
        },
        "architecture": {
            "task_type": "classification",
            "family": "resnet",
            "version": "v1",
            "size": "s",
            "display_name": "ResNet Small",
            "internal_key": "resnet-s",
        },
        "warm_model": True,
        "timing": {
            "received_at": "2026-05-01T12:00:00Z",
            "validation_ms": 1,
            "routing_ms": 1,
            "preprocessing_ms": 1,
            "inference_ms": 1,
            "postprocessing_ms": 1,
            "total_ms": 5,
        },
        "labels": [{"label": "ok", "score": 0.95, "rank": 1}],
    }


def _temporal_response() -> dict[str, object]:
    return {
        "schema_version": "inference.temporal-sequence.response.v1",
        "request_id": "req_temporal",
        "correlation_id": "corr_temporal",
        "deployment_target": "realtime",
        "route": {
            "deployment_release_id": "rel_temporal",
            "target_path": "realtime",
            "release_status": "activated",
            "model_family_slug": "temporal-angle",
            "model_version_id": "mvr_temporal",
            "task_type": "temporal_sequence",
            "triton_model_name": "temporal_angle",
            "triton_model_version": 1,
            "serving_artifact_format": "tensorrt_plan",
            "contract_major": 1,
        },
        "architecture": {
            "task_type": "temporal_sequence",
            "family": "tcn",
            "version": "v1",
            "size": "small",
            "display_name": "Causal TCN Small",
            "internal_key": "temporal_sequence:tcn:v1:small",
        },
        "warm_model": True,
        "timing": {
            "received_at": "2026-08-04T12:00:00Z",
            "validation_ms": 1,
            "routing_ms": 1,
            "preprocessing_ms": 1,
            "inference_ms": 1,
            "postprocessing_ms": 1,
            "total_ms": 5,
        },
        "sequence_id": "sequence-1",
        "track_id": "track-1",
        "sequence_length": 1,
        "last_frame_index": 4,
        "decision_frame_index": 4,
        "feature_set_id": "features.v1",
        "feature_schema_digest": "feature-digest",
        "scaler_sha256": "scaler-sha256",
        "snapshot_id": "snapshot-1",
        "feature_schema_version": "stem.geometry.v1",
        "rotation_schema_version": "stem.surface-rotation.v1",
        "calibration_schema_version": "1.0.0",
        "rotation_calibration_id": None,
        "rotation_calibration_hash": None,
        "rotation_viewpoint_convention": "viewed_from_log_end_a",
        "positive_rotation_direction": "clockwise_viewed_from_log_end_a",
        "bow_relevance_probability": 0.9,
        "target_observed_probability": 0.8,
        "angle_sin_2e": -0.406736,
        "angle_cos_2e": 0.913545,
        "signed_correction_deg": -12.0,
        "raw_angle_confidence": 0.8,
        "angle_confidence": 0.84,
        "confidence_calibration_valid": True,
        "final_quality_probabilities": {},
        "deadband_deg": 5.0,
        "estimate_method": "tcn",
        "reason_codes": [],
    }
