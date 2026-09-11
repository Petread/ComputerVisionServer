# Runtime Result Semantics

Runtime result records are owned by `result-service` and persisted in
`result_records`. They are produced by runtime executions, not by the control
plane and not by the inference gateway.

## What Gets Stored

Each result record separates these payload layers:

- raw inference outputs: gateway-facing inference responses
- normalized inference outputs: runtime payloads used by later nodes
- derived outputs: analysis, counting, measurement, spatial/orientation signal,
  rule, and decision outputs
- overlay primitives: render-ready primitives emitted by overlay output nodes
- object sets: AI detection/segmentation objects and classical CV region-set
  objects such as color-segmented parts
- trigger response fields: final fields returned to PLC or trigger callers
- runtime metadata: executor, runtime instance, correlation, and mode details

The full `RuntimeExecutionResult` envelope is still stored for compatibility,
but operator UI and connector code should read the separated fields.

## Realtime Policy

Realtime execution stays low-latency. A realtime frame is persisted when:

- the pipeline emits `output.persisted_result`
- the execution fails and a failure record is needed

Pipelines that need every sampled frame recorded must include an explicit
persisted-result output and should set sampling or frame-rate limits on the
source.

## Trigger Policy

Trigger execution always persists a usable result record for completed, failed,
and timeout responses. The persisted `trigger_response` field matches the
endpoint response policy after field mapping.

## Query Surface

`result-service` exposes:

- `GET /api/v1/runtime/results`
- `GET /api/v1/runtime/results/{result_record_id}`
- `GET /api/v1/runtime/channels/{channel_id}/results/latest`

History queries support channel, pipeline version, time range, mode, status,
result type, and text search across channel/place/correlation metadata.
