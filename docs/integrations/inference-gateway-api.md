# Inference Gateway Integrator API

## Architecture Alignment

This document implements the external application-to-application inference boundary defined in [the platform specification](../architecture/cv-platform-spec.md).

Authoritative constraints carried through unchanged:

- The platform remains fully open-source and on-prem.
- External applications never call Triton directly.
- External applications call only the inference gateways.
- Realtime and batch inference remain separate services, paths, concurrency policies, and Triton backends.
- Public inference contracts may expose classification, detection, segmentation, and supported anomaly models when those deployed releases are available.
- Public APIs are versioned independently from model versions.
- No operator CLI is required for normal operation.
- Multiple model families and multiple simultaneous requests are supported in parallel.
- Public contracts do not expose Triton model names, MLflow internals, DVC internals, or deployment metadata.

## Hidden Assumptions And Safer Choices

The platform specification is explicit about immutable `deployment_release` units and separate realtime and batch targets, but the current operator deployment tables model one target pointer per path. For external consumer integration, the safer production choice is:

- Treat `model_family.slug` as the stable public `model_id`.
- Resolve the deployed release per `model_id` and target path inside the gateway.
- Keep realtime and batch physically separated.
- Keep model version visibility in the public response while hiding Triton names and release ids.
- Apply admission control per public model so one overloaded model does not block healthy models.

This preserves the spec's deployment model while giving LAN clients a stable, explicit public model contract.

## Base URLs

Use the reverse-proxy LAN entrypoints:

- Realtime: `https://<host>/inference-rt/api/v1`
- Batch: `https://<host>/inference-batch/api/v1`

## Authentication

Authentication is controlled by the main platform setting `api_keys.enabled`.

Current local/base configuration sets `api_keys.enabled: true`, so
machine-to-machine authentication is enforced by default. Open mode is only for
explicitly isolated development or lab deployments where the operator sets
`api_keys.enabled: false`; request limits still apply in that mode, keyed by the
anonymous client address when available.

Supported headers in authenticated mode:

- `Authorization: Bearer <key_id>.<secret>`
- Legacy compatibility: `X-API-Key: <key_id>.<secret>`

Required scopes in authenticated mode:

- Realtime routes: `realtime.predict`
- Batch routes: `batch.submit`

Managed key lifecycle in authenticated mode:

- Normal operations use UI-created managed keys from `Access & Security -> API Keys`.
- The canonical token format remains `Authorization: Bearer <key_id>.<secret>`.
- Managed `key_id` values are generated server-side and look like `key_<random-hex>`.
- The full token is shown exactly once after creation. Store it securely in the calling application because later UI and API reads return only masked metadata.
- The platform stores only a hashed secret, not the plaintext token.
- Bootstrap/config-managed keys remain supported during migration and are visible in the UI as read-only `bootstrap` entries.
- Browser session cookies are reserved for internal operator routes and are not part of the external integration contract.

Recommended client behavior:

- Send `X-Request-ID` with a client-generated UUID for every request.
- Reuse the same value as `X-Correlation-ID` for one end-to-end business operation.
- Log HTTP status, `x-request-id`, `x-correlation-id`, `model_id`, `model_version`, and timeout values.

## Discovery Flow

1. Call `GET /models`.
2. Choose the `model_id` and target path based on task type (`classification`, `detection`, `segmentation`, or `anomaly`) and availability.
3. Read `GET /models/{model_id}` to confirm supported parameters, input constraints, and response schema.
4. Call realtime `/infer/*` or batch `/batch/jobs` with that `model_id`.

## Realtime Vs Batch

- Use realtime for one image per request and low-latency downstream decisions.
- Use batch for asynchronous manifests or inline item lists, resumable server-side processing, and result package retrieval.
- Realtime never queues.
- Batch may queue and chunk server-side.

## Public Endpoints

Realtime gateway:

- `GET /health`
- `GET /ready`
- `GET /models`
- `GET /models/{model_id}`
- `POST /infer/classify`
- `POST /infer/detect`
- `POST /infer/segment`
- `POST /infer/anomaly`

Batch gateway:

- `GET /health`
- `GET /ready`
- `GET /models`
- `GET /models/{model_id}`
- `POST /batch/jobs`
- `GET /batch/jobs/{job_id}`
- `GET /batch/jobs/{job_id}/results`
- `DELETE /batch/jobs/{job_id}`

## Task Contracts

- Classification models return `predictions[]` with `label` and `score`.
- Detection models return `coordinate_system` plus `detections[]` with normalized `x_min,y_min,x_max,y_max` boxes.
- Segmentation models return `image_width`, `image_height`, and `segments[]`.
- Each segmentation result contains `label`, optional `class_id`, `confidence`, and `polygon[]`.
- Segmentation polygon points are normalized `x`,`y` coordinates in `[0,1]`.
- Public v1 uses polygon output for segmentation. Clients should not expect raster mask blobs in the public response.
- Anomaly models return `anomaly_score`, `predicted_label`, `is_anomalous`, `threshold`, `heatmap`, and image dimensions.
- Anomaly heatmaps are real model outputs encoded as `png_base64`; clients should not synthesize heatmaps from the score alone.
- The only public anomaly method currently exposed by the platform is PatchCore via Anomalib. Other methods remain absent from discovery until they are implemented end to end.

Segmentation-specific discovery details from `GET /models/{model_id}`:

- `response_schema.schema_id` is `inference.public.segmentation-response.v1`
- supported realtime parameters are `threshold` and `max_segments`
- batch v1 does not expose task-specific inference overrides; deployed release defaults apply

Anomaly-specific discovery details from `GET /models/{model_id}`:

- `response_schema.schema_id` is `inference.public.anomaly-response.v1`
- supported realtime parameters are `threshold` and `return_mask`
- `return_mask` only produces a mask when the deployed model declares mask support

## Input Rules

Canonical realtime request form in v1:

- `multipart/form-data`
- exactly one image per request
- supported types: `image/jpeg`, `image/png`, `image/bmp`, `image/webp`
- size limit: configured gateway `max_image_bytes`

Not part of the required v1 contract:

- JSON base64 image payloads
- remote URI fetch by the gateway

Batch submission in v1:

- JSON body with one `model_id`
- either inline `inputs[]` with base64 images or `input_manifest_uri`
- `Idempotency-Key` header is strongly recommended and treated as the canonical idempotency control

## Key Rotation And Revocation

This section applies only when `api_keys.enabled: true`.

- Preferred rotation flow:
  1. Create a new managed key in the UI.
  2. Deploy the new token to the calling application.
  3. Verify traffic with the new key.
  4. Revoke the old managed key in the UI.
- Revoked keys stop working immediately or within the short gateway cache window.
- Expired keys fail authentication without requiring client-side changes.
- Bootstrap keys are rotated by changing configuration plus mounted secret files, then reloading the platform service that owns them.

## Realtime Examples

Classification:

```bash
curl -X POST "https://127.0.0.1:8443/inference-rt/api/v1/infer/classify" \
  -H "X-Request-ID: 6b9bfc9f-2399-4269-b31d-3e0bbcfb2d6a" \
  -F "model_id=quality-classifier" \
  -F "top_k=2" \
  -F "image=@photo.png;type=image/png"
```

If `api_keys.enabled: true`, add:

```bash
-H "Authorization: Bearer key_7f8c9d0e1a2b3c4d5e6f7890.S3cureTokenFromOneTimeReveal"
```

Classification response:

```json
{
  "api_version": "v1",
  "schema_version": "inference.public.classification-response.v1",
  "request_id": "req_123",
  "correlation_id": "6b9bfc9f-2399-4269-b31d-3e0bbcfb2d6a",
  "model_id": "quality-classifier",
  "model_family": "quality-classifier",
  "model_version": 3,
  "task_type": "classification",
  "architecture": {
    "task_type": "classification",
    "family": "yolo",
    "version": "v11",
    "size": "n",
    "display_name": "YOLOv11 Nano",
    "internal_key": "classification:yolo:v11:n"
  },
  "input": {
    "file_name": "photo.png",
    "content_type": "image/png",
    "size_bytes": 182044,
    "width": 1280,
    "height": 720,
    "sha256": "sha256:..."
  },
  "timing": {
    "received_at": "2026-04-21T08:15:00Z",
    "completed_at": "2026-04-21T08:15:00Z",
    "validation_ms": 1.4,
    "routing_ms": 0.7,
    "preprocessing_ms": 2.1,
    "inference_ms": 18.9,
    "postprocessing_ms": 0.4,
    "total_ms": 23.5
  },
  "warnings": [],
  "predictions": [
    {"label": "good", "score": 0.982},
    {"label": "bad", "score": 0.018}
  ]
}
```

Detection:

```bash
curl -X POST "https://127.0.0.1:8443/inference-rt/api/v1/infer/detect" \
  -F "model_id=car-detector" \
  -F "threshold=0.35" \
  -F "max_detections=10" \
  -F "image=@road.jpg;type=image/jpeg"
```

Detection response fields always include:

- `model_id`
- `model_family`
- `model_version`
- `task_type`
- `architecture`
- `input`
- `coordinate_system`
- `detections[]`
- `timing`

Segmentation:

```bash
curl -X POST "https://127.0.0.1:8443/inference-rt/api/v1/infer/segment" \
  -H "X-Request-ID: 93e0d0d5-4c74-4d59-a29b-d90f18c7a0f4" \
  -F "model_id=handsegment-segmenter" \
  -F "threshold=0.35" \
  -F "max_segments=5" \
  -F "image=@hand.jpg;type=image/jpeg"
```

Segmentation response:

```json
{
  "api_version": "v1",
  "schema_version": "inference.public.segmentation-response.v1",
  "request_id": "req_987",
  "correlation_id": "93e0d0d5-4c74-4d59-a29b-d90f18c7a0f4",
  "model_id": "handsegment-segmenter",
  "model_family": "handsegment-segmenter",
  "model_version": 1,
  "task_type": "segmentation",
  "architecture": {
    "task_type": "segmentation",
    "family": "yolo",
    "version": "v8",
    "size": "s",
    "display_name": "YOLOv8 Small",
    "internal_key": "segmentation:yolo:v8:s"
  },
  "input": {
    "file_name": "hand.jpg",
    "content_type": "image/jpeg",
    "size_bytes": 524188,
    "width": 3024,
    "height": 4032,
    "sha256": "sha256:..."
  },
  "timing": {
    "received_at": "2026-04-23T19:50:00Z",
    "completed_at": "2026-04-23T19:50:00Z",
    "validation_ms": 1.6,
    "routing_ms": 0.8,
    "preprocessing_ms": 2.4,
    "inference_ms": 31.2,
    "postprocessing_ms": 0.0,
    "total_ms": 36.0
  },
  "warnings": [],
  "image_width": 3024,
  "image_height": 4032,
  "segments": [
    {
      "label": "Hand",
      "class_id": 0,
      "confidence": 0.93,
      "polygon": [
        {"x": 0.291, "y": 0.658},
        {"x": 0.394, "y": 0.606},
        {"x": 0.459, "y": 0.558},
        {"x": 0.488, "y": 0.503}
      ]
    }
  ]
}
```

Segmentation response fields always include:

- `model_id`
- `model_family`
- `model_version`
- `task_type`
- `architecture`
- `input`
- `image_width`
- `image_height`
- `segments[]`
- `timing`

## Batch Example

Submit:

```http
POST /inference-batch/api/v1/batch/jobs
Idempotency-Key: wood-batch-20260421-001
Content-Type: application/json

{
  "model_id": "wood-log-detector",
  "inputs": [
    {
      "item_id": "frame-0001",
      "image_base64": "<base64 image>",
      "external_ref": "camera-7/frame-0001.png",
      "metadata": {
        "line_id": "saw-1"
      }
    }
  ]
}
```

Accepted response:

```json
{
  "api_version": "v1",
  "schema_version": "inference.public.batch-job.v1",
  "request_id": "req_456",
  "correlation_id": "req_456",
  "job_id": "btj_01J...",
  "model_id": "wood-log-detector",
  "model_family": "wood-log-detector",
  "model_version": 2,
  "task_type": "detection",
  "status": "accepted",
  "submitted_at": "2026-04-21T08:16:00Z",
  "progress": {
    "total_items": 0,
    "completed_items": 0,
    "failed_items": 0,
    "total_chunks": 0,
    "queued_chunks": 0,
    "running_chunks": 0,
    "completed_chunks": 0,
    "failed_chunks": 0,
    "progress_percent": 0.0
  },
  "result_package": {
    "manifest_uri": null,
    "jsonl_uri": null,
    "bundle_uri": null,
    "result_schema": "inference.public.batch-results.v1",
    "recoverable": false,
    "incomplete_chunk_nos": []
  }
}
```

Batch result shape depends on task type:

- classification items populate `predictions[]`
- detection items populate `detections[]`
- segmentation items populate `segments[]`, `image_width`, and `image_height`
- anomaly items populate `anomaly_score`, `predicted_label`, `is_anomalous`,
  `threshold`, `heatmap`, optional `mask`, `image_width`, and `image_height`

Segmentation batch result item example:

```json
{
  "item_id": "frame-0001",
  "external_ref": "camera-7/frame-0001.png",
  "status": "completed",
  "image_width": 3024,
  "image_height": 4032,
  "segments": [
    {
      "label": "Hand",
      "class_id": 0,
      "confidence": 0.93,
      "polygon": [
        {"x": 0.291, "y": 0.658},
        {"x": 0.394, "y": 0.606},
        {"x": 0.459, "y": 0.558},
        {"x": 0.488, "y": 0.503}
      ]
    }
  ],
  "timing": {
    "received_at": "2026-04-23T19:51:00Z",
    "completed_at": "2026-04-23T19:51:00Z",
    "validation_ms": 1.5,
    "routing_ms": 0.8,
    "preprocessing_ms": 2.3,
    "inference_ms": 30.6,
    "postprocessing_ms": 0.0,
    "total_ms": 35.2
  }
}
```

## Error Model

Every error response contains:

- `code`
- `message`
- `correlation_id`
- `retryable`
- optional `validation_errors[]`

Important codes:

- `invalid_request`
- `authentication_failed` when `api_keys.enabled: true`
- `authorization_failed` when `api_keys.enabled: true`
- `model_not_found`
- `model_unavailable`
- `unsupported_capability`
- `unsupported_parameter`
- `image_decode_failure`
- `payload_too_large`
- `timeout`
- `overload`
- `internal_processing_failure`
- `batch_job_not_found`
- `batch_job_expired`
- `incompatible_api_version`

Unsupported parameter example:

```json
{
  "code": "unsupported_parameter",
  "message": "The selected public model does not support one or more supplied parameters.",
  "detail": "iou_threshold",
  "status_code": 422,
  "correlation_id": "req_789",
  "retryable": false,
  "validation_errors": [
    {
      "field": "iou_threshold",
      "location": ["body", "iou_threshold"],
      "message": "Parameter 'iou_threshold' is not supported for model_id 'wood-log-detector'.",
      "error_type": "unsupported_parameter"
    }
  ],
  "metadata": {}
}
```

## Timeout And Retry Guidance

- Realtime clients should set a client timeout slightly above the gateway timeout budget.
- Realtime default timeout is exposed in model discovery.
- Do not blindly retry realtime requests on `422`, `409`, or authentication errors when authenticated mode is enabled.
- Retry realtime `429` and `504` with bounded exponential backoff and jitter.
- Batch submission should always send `Idempotency-Key`.
- Batch status polling should use a moderate interval, for example 2 to 10 seconds depending on workload size.

To measure latency distribution on the client side:

- use `timing.total_ms` for per-request measurements
- aggregate p50, p95, and p99 by `model_id`
- include `request_id`, `correlation_id`, and `model_version` in logs

## Idempotency

- Realtime inference is not idempotency-keyed in v1; clients may repeat safely only when their own workflow allows it.
- Batch submission uses `Idempotency-Key` to deduplicate retries.
- Reusing the same key with a different request body returns a conflict.

## Versioning Policy

- Public HTTP routes are versioned under `/api/v1`.
- API version is independent from `model_version`.
- Incompatible model contract changes require a new model family major as defined by the platform spec.
- Backward-compatible additions may be introduced within v1.
- Future deprecation approach:
  - announce a new API version
  - keep `/api/v1` live during a migration window
  - expose deprecation headers and update capability docs before retirement

## Future gRPC Extension

REST is the primary implemented interface in v1.

A future gRPC surface may be added only as a parallel public contract that preserves:

- the same `model_id` addressing
- the same auth scopes when authenticated mode is enabled
- the same response normalization
- the same realtime vs batch separation

## Example Clients

- Python: [examples/inference/python_client.py](../../examples/inference/python_client.py)
- C#: [examples/inference/CSharpClient.cs](../../examples/inference/CSharpClient.cs)
- Batch request sample: [examples/inference/batch-job.request.v1.json](../../examples/inference/batch-job.request.v1.json)
