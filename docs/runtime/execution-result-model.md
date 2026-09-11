# Runtime Execution Result Model

The shared execution result contract is `cv_runtime.payloads.RuntimeExecutionResult`.

It is designed to be reusable across:

- trigger responses
- realtime overlay emission
- persisted runtime results
- capture routing
- future output connectors

## Top-Level Shape

`RuntimeExecutionResult` includes:

- execution identity and timestamps
- `node_outputs`
- `raw_inference_outputs`
- `normalized_inference_outputs`
- `derived_outputs`
- `overlay_outputs`
- `capture_intents`
- `persisted_results`
- `internal_events`
- `final_response`
- `timing`
- `errors`

## Separation Rules

### Raw Inference Outputs

These are gateway-facing envelopes for inference node emissions:

- node identity
- output port
- payload type
- `gateway_response`

### Normalized Inference Outputs

These are runtime payload envelopes used for downstream graph propagation:

- node identity
- output port
- payload type
- normalized `payload`

### Derived Outputs

Analysis and rule-evaluation outputs are collected separately from raw model
predictions so operator-facing logic and model-facing logic remain distinct.

### Overlay Outputs

Overlay nodes emit primitives only. Rendering remains an overlay-service
concern.

### Capture Intents

Capture nodes emit structured intents, not synchronous Label Studio writes.

### Final Response

`final_response` is populated from `output.result` or the legacy
`output.http_trigger_response` when present. If no explicit result output is
configured, the runtime falls back to the latest upstream `ResultPayload`.
Legacy persisted-result connector payloads are not used as the response source.

`output.result` is the operator-facing default for both realtime and trigger
pipelines. In realtime mode it also marks the execution as worth persisting, so
the channel result panel and result history can show the configured pipeline
output without requiring a separate persisted-result node.

The result node maps available upstream fields to display keys. For example,
`top_label.label -> class` returns `{ "class": "scratch" }`. `wrap_key` is
optional; if set to `result`, the same mapping is nested as
`{ "result": { "class": "scratch" } }`. Leaving it empty keeps mapped fields at
the top level, which is the default for channel display and trigger responses.

## Persisted Records

`cv_db.runtime_execution_store.record_runtime_execution_result()` stores the
full result envelope in `result_records.result` for compatibility, and also
persists queryable, typed sections:

- `pipeline_version_id`
- `mode`
- `result_type`
- `raw_inference_outputs`
- `normalized_inference_outputs`
- `derived_outputs`
- `overlay_primitives`
- `trigger_response`
- `runtime_metadata`
- `timing`
- `model_routes`

The result-service reads these separated sections. New UI and connector paths
must use the typed sections instead of JSON-path scraping the legacy envelope.
