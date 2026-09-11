# Adding An Output Connector

Use this path when a pipeline output needs a new delivery target. The runtime
engine should not be changed for connector additions.

## Add The Shared Connector Type

1. Add a typed config model in
   `backend/packages/cv-runtime/src/cv_runtime/connectors.py`.
2. Register an `OutputConnectorTypeDefinition` in
   `BUILTIN_OUTPUT_CONNECTOR_TYPES`.
3. Keep defaults production-safe. Reject inline credentials and use
   `secret_refs` only when the connector type explicitly supports secrets.
4. Add or update contract/UI schemas if the connector exposes new operator
   fields.

## Add The Runtime Executor

1. Add an executor class in
   `backend/services/connector-service/src/connector_service/builtins.py`.
2. Register it in `BUILTIN_CONNECTOR_EXECUTORS`.
3. Return a small response payload with counts, external ids, or storage refs.
   Do not put large images or secrets into delivery status.
4. Raise `RuntimeConnectorDeliveryError` for connector failures and set
   `retryable` accurately. Bad config or malformed payloads should be terminal;
   temporary dependency failures should be retryable.

## Add Tests

Cover these cases:

- connector type appears in the catalog
- config validation accepts defaults and rejects unsafe config
- delivery succeeds end-to-end from `record_runtime_execution_result`
- retryable failures update delivery status without blocking other connectors
- terminal failures set a visible connector error

The existing reference test is
`tests/unit/backend/test_runtime_output_connectors.py`.

## Source And Analysis Node Extensions

Source, preprocessing, inference, analysis, capture, and output nodes stay on
the runtime node extension path:

- Implement `RuntimeNodeHandler`.
- Register a node type in `RuntimeNodeTypeRegistry` with input/output ports,
  supported modes, property schema, default config, and `runtime_handler_ref`.
- Keep output nodes focused on producing structured runtime payloads. Delivery
  belongs to output connectors, not to node handlers.

See `docs/runtime/node-handler-contract.md`,
`docs/runtime/pipeline-node-plugin-model.md`, and
`docs/runtime/adding-runtime-node-type.md`.

