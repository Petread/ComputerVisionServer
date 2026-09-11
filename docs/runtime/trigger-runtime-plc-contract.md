# Trigger Runtime PLC Contract

Authoritative inputs:

- `docs/architecture/cv-platform-spec.md`
- `docs/runtime/vision-runtime-spec.md`
- `docs/runtime/vision-runtime-roadmap.md`

This guide describes the hardened pipeline HTTP trigger contract for PLC and
control-system clients. A trigger is a pipeline ingress node, not a channel
mode and not a separate operator-managed business object.

## Plane Boundaries

- Operators configure triggerability in the runtime pipeline designer.
- External clients invoke endpoints through `trigger-executor`.
- Pipeline inference still goes through the inference gateway.
- Realtime, trigger, and training paths remain separate.
- Runtime may persist endpoint metadata, but it is derived from the published
  pipeline and deployment state.

## Operator Setup

1. Create a channel for the camera/source. The channel owns source address,
   source health, technical place, and live-view behavior.
2. In the runtime pipeline designer, create a triggerable pipeline shaped like
   `HTTP Trigger Input -> Channel frame input -> processing -> Response Output`.
3. Configure `HTTP Trigger Input` with method, timeout, auth, request schema,
   and endpoint visibility settings.
4. Use `Channel frame input` as the source node. The channel is selected by
   applying/assigning the published pipeline to a channel, not by typing a
   channel ID in the pipeline graph.
5. Configure `Response Output` with the response field mapping.
6. Publish/deploy the pipeline. Runtime derives the stable endpoint metadata
   and readiness from that published version.
7. Open pipeline/deployment detail to copy the endpoint URL, method, auth
   requirement, timeout, and example request/response. Channel detail may show
   that this pipeline references the channel, but it is informational.

## External Route

Direct service route:

```text
PUT /api/v1/runtime/triggers/{endpoint_slug}
```

Reverse-proxy route:

```text
PUT /runtime-trigger/api/v1/runtime/triggers/{endpoint_slug}
```

The platform generates the endpoint slug from channel identity, pipeline
identity, published version/deployment state, and the HTTP Trigger Input node
configuration. A constrained custom slug may be allowed, but raw database IDs
are not required in the exposed URL.

## Request Schema

```json
{
  "trigger_id": "plc-cycle-20260502-000123",
  "timeout_ms": 1200,
  "image_base64": "iVBORw0KGgo...",
  "mime_type": "image/png",
  "payload": {
    "station": "packing-line-1",
    "part_no": "A-100"
  }
}
```

- `trigger_id` is optional but recommended. Use a stable PLC-side cycle or
  workpiece identifier.
- `timeout_ms` may reduce the endpoint timeout for one request, but cannot
  extend the configured endpoint timeout.
- `image_base64` is optional. If omitted, trigger execution captures from the
  endpoint's channel according to the channel source acquisition policy.
- `mime_type` must match the supplied image bytes.
- Server-local paths such as `image_path`, `path`, `file_path`, `local_path`,
  and `server_path` are rejected, including inside nested `payload` objects.

## Channel Frame Sources

The source is selected by the channel that the published trigger pipeline is
applied to. Sources must use channel-owned configuration and must not require
operators to paste internal IDs into the pipeline graph. HTTP(S) snapshot URLs
must not include inline credentials. Server-local file paths are not accepted
for trigger execution. This keeps the external contract deterministic and
prevents a client from causing arbitrary file reads on the server.

## Response Schema

Successful response:

```json
{
  "schema_version": "runtime.trigger.execution-response.v1",
  "status": "completed",
  "trigger_endpoint_id": "tep_...",
  "endpoint_slug": "packing-station-plc",
  "channel_id": "chn_...",
  "pipeline_version_id": "plv_...",
  "runtime_execution_id": "rex_...",
  "trigger_id": "plc-cycle-20260502-000123",
  "correlation_id": "req_...",
  "final_response": {
    "piece_count": 1,
    "reject": true
  },
  "errors": [],
  "timing": {
    "total_ms": 42.7
  },
  "error_code": null,
  "retryable": false,
  "meta": {
    "request_id": "req_...",
    "correlation_id": "req_...",
    "generated_at": "2026-05-02T08:00:00Z"
  }
}
```

`final_response` is shaped by the Response Output node. Operators can return
all final-response fields or a configured set such as `count -> piece_count`
without writing raw code.

If the pipeline also includes `Display output` and `Overlay output`, the
trigger executor publishes the captured frame plus overlay primitives as the
channel's latest trigger display state. This lets the channel detail view show
the last triggered frame with classification labels, detections, or other
context-bound overlays even when no continuous realtime runtime is running.

## Error Taxonomy

| HTTP | Code | Retry | Meaning |
| --- | --- | --- | --- |
| 401 | `authentication_failed` | no | API key is missing or invalid when auth is required. |
| 404 | `not_found` | no | Endpoint slug does not exist. |
| 409 | `runtime_trigger_endpoint_disabled` | no | Endpoint exists but is disabled. |
| 422 | `runtime_trigger_invalid_endpoint_config` | no | The derived endpoint no longer points at a valid triggerable pipeline. |
| 422 | `runtime_trigger_payload_unsafe` | no | Request payload references server-local path fields. |
| 422 | `runtime_trigger_image_invalid` | no | Image bytes, base64, or MIME type are invalid. |
| 429 | `runtime_trigger_rate_limited` | yes | Endpoint rate or burst limit was reached. |
| 429 | `runtime_trigger_admission_rejected` | yes | Trigger executor concurrency is full. |
| 502 | `runtime_trigger_snapshot_unavailable` | yes | Configured channel frame source could not be reached. |
| 503 | `runtime_trigger_runtime_unavailable` | yes | Pipeline, runtime service, auth, or channel frame readiness is missing. |
| 504 | `runtime_trigger_timeout` | yes | Execution exceeded the endpoint timeout. |

All trigger responses include a correlation ID. Control clients should log both
`trigger_id` and `correlation_id`.

## Timeout Guidance

Choose endpoint timeouts from the control-system cycle budget. The timeout
covers snapshot acquisition, graph execution, inference gateway calls, response
mapping, and result persistence. A timed-out request returns HTTP 504 with
`status="timeout"` and a persisted timeout result. Late pipeline completion is
not allowed to overwrite the timeout result.

## Retry Guidance

Retry only when `retryable=true`. For PLC-style clients:

- Retry 429, 502, 503, and 504 with bounded backoff.
- Do not retry 401, 404, 409, or unsafe/invalid 422 responses without operator
  action.
- Preserve the same `trigger_id` across retries so logs can be correlated.

## Auth Guide

Authentication is configured by the HTTP Trigger Input node. The default for
local PLC/web application testing is no API key (`auth_required=false`).
Operators can later enable `Require API key`, which sets
`auth_required=true` on the derived endpoint.

When auth is enabled, send either:

```text
Authorization: Bearer <key_id>.<secret>
```

or the legacy header:

```text
X-API-Key: <key_id>.<secret>
```

The API key must include scope:

```text
runtime.trigger.invoke
```

OpenAPI declares `TriggerBearer` and `TriggerApiKey` security schemes, but the
route also allows unauthenticated calls when the derived endpoint has
`auth_required=false`.
