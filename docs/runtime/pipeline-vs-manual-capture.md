# Pipeline Capture Vs Manual Capture

Both capture paths deliver images to Label Studio, but they are separate
operator concepts.

| Path | Trigger | Owner | Typical Use |
|---|---|---|---|
| Manual capture | Operator button or interval session | `capture-service` from a runtime-admin command | Quick samples from live view |
| Pipeline capture | Capture nodes in a pipeline graph | runtime executor emits intents, `capture-service` delivers | Rule-driven full-frame or ROI capture |

## Pipeline Capture

Pipeline nodes emit typed `CaptureIntent` payloads. When a realtime or trigger
execution result is persisted, those intents become durable
`runtime_capture_requests`. Realtime execution does not wait for Label Studio.
Trigger execution returns its deterministic response independently of capture
delivery unless a future explicit connector changes that contract.

Supported capture nodes:

- `capture.full_frame_labelstudio`
- `capture.roi_labelstudio`
- `capture.conditional`
- `capture.multi_project`

One intent can route to multiple Label Studio projects. Capture-service creates
one delivery row per target project so status is visible per project and per
rule.

## Manual Capture

Manual capture is always full-frame today. It records a manual history row for
operator UX and a durable runtime capture request for delivery. Interval capture
is service-owned and uses one in-flight request per session for backpressure.

## Routing Rules

Capture targets come from the node configuration or from a matching
`capture_rules` row. Project IDs are resolved through the platform Label Studio
mapping. Numeric Label Studio project IDs are rejected in runtime capture
intents, and normal operator workflows use selectors.
