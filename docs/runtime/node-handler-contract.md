# Runtime Node Handler Contract

Runtime node handlers live under `backend/packages/cv-runtime/src/cv_runtime/nodes/`
or in explicitly allowlisted extension packages.

## Handler Shape

Each handler exposes:

- `node_type`
- `execute(context, node, inputs) -> NodeExecutionResult`

The contract is defined in `cv_runtime.handlers`.

## Inputs

Handlers receive:

- `RuntimeExecutionContext`
  - correlation ID
  - channel ID
  - pipeline version ID
  - runtime execution ID
  - runtime mode
  - source configuration
  - request-scoped state
  - inference client
  - logical model resolver
- `RuntimeNodeSpec`
  - node type
  - typed ports
  - normalized config
  - logical model reference when applicable
- `NodeInputs`
  - typed payloads keyed by input port

## Outputs

Handlers return `NodeExecutionResult(outputs=...)`.

Supported payload families are defined in `cv_runtime.payloads`, including:

- `FramePayload`
- `DetectionPayload`
- `ClassificationPayload`
- `SegmentPayload`
- `PositionsPayload`
- `MeasurementsPayload`
- `EventsPayload`
- `ResultPayload`
- `CaptureIntentsPayload`
- `OverlayPayload`

## Error Model

Handlers raise `RuntimeNodeExecutionError` for structured runtime failures.

Required error fields:

- `code`
- `message`
- `retryable`
- optional node metadata

Unstructured exceptions are converted into `node_execution_failed`, but new
handlers should raise structured runtime errors directly.

## Inference Handlers

Inference handlers must:

- require `logical_model_ref`
- resolve that reference through `RuntimeExecutionContext.resolve_model()`
- validate task compatibility
- call the existing inference gateway contracts
- attach resolved deployment metadata to the emitted payload metadata

## State

Handlers may use `context.state[node.node_key]` for per-execution or
per-runtime-node state such as:

- frame sampling counters
- dwell timers
- previous positions for direction estimation
- previous frame references for motion magnitude
