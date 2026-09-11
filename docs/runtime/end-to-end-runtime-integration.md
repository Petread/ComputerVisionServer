# End-to-End Runtime Integration

This guide describes how the runtime plane is integrated into the existing
platform. It is an engineering and operator-support companion to
`vision-runtime-spec.md` and `vision-runtime-roadmap.md`.

## Product Path

The normal browser workflow is:

1. Deploy or promote a model through the existing model registry and deployment
   workflow.
2. Build and publish a runtime pipeline that selects logical model refs backed by
   deployed model capabilities.
3. Create or edit channels as source/camera connections.
4. Start the runtime from the channel card or detail page.
5. Confirm runtime health, latest frame, overlay, recent results, capture
   delivery, and output connector delivery from the channel detail page.
6. For PLC/web request-response workflows, configure `HTTP Trigger Input ->
   Channel frame input -> Response Output` in the runtime pipeline designer and
   publish the pipeline. The callable endpoint is derived from the published
   pipeline and the channel assignment.

Operators do not type internal channel IDs, pipeline version IDs, deployment
release IDs, or Label Studio project numbers in normal workflows.

## Plane Separation

- Control plane: admin UI, `admin-api`, and `runtime-admin-api` own browser
  sessions, channel configuration, pipeline authoring, derived trigger endpoint
  metadata reads, audit events, and operator workflows.
- Runtime plane: `runtime-orchestrator`, realtime and trigger executors,
  overlay, capture, result, and connector services own execution, health,
  latest overlay state, capture queues, result persistence, and delivery.
- Inference plane: inference gateways and deployed model targets own prediction
  execution. Runtime nodes call the inference plane through existing gateway
  clients and logical model refs.

Cross-plane calls use the existing shared auth model. Browser users use shared
sessions. Trigger/runtime service endpoints use machine API keys only when the
published pipeline's HTTP Trigger Input has `auth_required=true`. Trigger smoke
tests call the same runtime path exposed by that node rather than bypassing
trigger-executor through the control plane.

## Shared Auth And Keys

Pipeline-derived trigger endpoints default to `auth_required=false` for local
PLC/application integration testing. Enabling the HTTP Trigger Input
`Require API key` option switches the endpoint to scope
`runtime.trigger.invoke`. Service drain endpoints use non-assignable internal
scopes:

- `runtime.capture.drain`
- `runtime.connector.drain`
- `runtime.executor.invoke`
- `runtime.control`
- `runtime.overlay.read`

These scopes live in the shared access registry. No separate runtime-only hidden
auth model is introduced.

## Model Discovery

Runtime model capability options are projected from:

- `deployment_releases`
- `deployment_targets`
- `model_versions`
- `model_families`
- `model_promotions`

The runtime capability cache is a short-lived projection with
`source_of_truth=deployment_releases`. Selection follows deployment state:

- `active_production` resolves production/active releases.
- `staged_test` can resolve staged releases for validation.
- retired, rolled-back, failed, archived, inactive, and missing releases are not
  selectable.

The runtime plane no longer needs a separate stale model metadata source for
normal operation.

## Label Studio Capture

Manual and pipeline capture target platform `LabelProject` records. Operators
choose projects by name from the existing Label Studio integration. Pipeline
capture intents must carry platform capture target IDs produced by the runtime
pipeline configuration; raw Label Studio project numbers are rejected.

Capture service delivery records keep the external Label Studio project number
for audit and API submission, but operators do not provide that number manually.

## Acceptance Coverage

The focused acceptance checks verify:

- channel creation and runtime command flow
- published pipeline selection and validation
- deployed-release runtime model discovery
- pipeline-derived trigger endpoint machine-auth execution and structured
  error paths
- authenticated trigger tests through the same trigger-executor PUT path
- realtime execution with persisted results and overlay state
- capture delivery to one or more Label Studio projects
- rejection of raw Label Studio project IDs in capture intents
- runtime health, lifecycle, and result browsing contracts
- production reverse-proxy exposure for runtime public prefixes and blocking of
  direct runtime service prefixes

Useful commands from this workspace:

```powershell
python -m pytest tests/unit/backend/test_runtime_pipeline_definition_system.py tests/unit/backend/test_runtime_channel_management.py tests/unit/backend/test_runtime_execution_core.py tests/unit/backend/test_runtime_manual_capture.py tests/unit/backend/test_runtime_lifecycle_core.py tests/unit/backend/test_realtime_live_path.py
python -m pytest tests/contract/test_runtime_plane_contracts.py tests/contract/test_platform_security_contracts.py tests/unit/backend/test_runtime_domain_contracts.py
npm.cmd --prefix frontend/apps/admin-web run build
npm.cmd --prefix frontend/apps/admin-web run test -- channel-detail-page.test.tsx
```
