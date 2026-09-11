# Runtime Output Connector Architecture

Output connectors are the runtime-owned delivery layer for pipeline output nodes.
They complete the path from an executed runtime graph to operator-visible output
state without mixing control-plane configuration, runtime execution, or inference
serving responsibilities.

## Plane Boundaries

- Control plane: `runtime-admin-api` owns connector configuration APIs and the
  channel detail UI uses those APIs. Operators do not use a CLI for normal
  connector work.
- Runtime plane: `connector-service` owns durable connector delivery execution.
  It claims queued deliveries, runs connector executors, records status, and
  emits audit events.
- Inference plane: inference gateways only serve model requests. Connectors do
  not call or configure model serving.

## Durable Data Model

- `output_connectors` stores operator-visible connector configuration, status,
  enablement, last delivery status, last success time, and last error.
- `runtime_connector_deliveries` stores one durable delivery attempt per emitted
  output payload, including status, retry count, max attempts, queued/claimed/
  delivered/completed times, request envelope, response envelope, and last error.
- `result_records` remains the durable runtime result truth. The connector queue
  is populated from the structured output sections of `RuntimeExecutionResult`.

## Delivery Flow

1. The runtime engine executes output nodes and records structured sections:
   overlays, HTTP trigger responses, persisted results, and internal events.
2. `record_runtime_execution_result` persists the result record and enqueues
   connector deliveries for the emitted output sections.
3. If a pipeline output has no configured connector yet, the store creates a
   safe default connector bound to the channel/type/node. If an operator has
   disabled the matching connector, delivery is not re-enabled implicitly.
4. `connector-service` claims due deliveries independently.
5. A typed connector executor validates configuration, performs the built-in
   delivery action, and writes success or retryable/terminal failure state.
6. Each delivery writes audit state. A connector failure affects only that
   connector delivery and does not fail the runtime execution that produced the
   result.

## Built-In Connectors

- `output.overlay`: writes overlay primitives to the latest live-view state when
  a runtime execution id is available and records primitive/frame counts.
- `output.http_trigger_response`: records the final trigger response delivery
  status. The trigger executor still owns the synchronous HTTP response.
- `output.persisted_result`: confirms that the runtime result is durably stored.
- `output.internal_event`: writes emitted runtime events into the platform audit
  event stream.

## Status And Retry Semantics

Delivery statuses are `queued`, `processing`, `delivered`,
`failed_retryable`, `failed_terminal`, and `skipped_no_payload`.

Retryable failures use the connector delivery policy (`max_attempts` and
`retry_backoff_seconds`). Terminal failures set the connector to `failed`.
Retryable failures set it to `degraded`. Successful delivery restores an enabled
connector to `active`.

## Configuration And Secrets

Connector configuration is validated by typed Pydantic models in
`cv_runtime.connectors`. Inline secret-looking config keys and URL credentials
are rejected. Built-in connectors currently do not accept secret references.
Future connectors that need credentials must expose named `secret_refs` and
resolve them inside the connector service.

