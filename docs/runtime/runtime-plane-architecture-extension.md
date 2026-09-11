# Runtime Plane Architecture Extension

Status: implementation-ready extension

Authoritative inputs:

- `docs/architecture/cv-platform-spec.md`
- `docs/runtime/vision-runtime-spec.md`

This document extends the existing platform. It does not replace the control
plane, training plane, deployment flow, inference gateways, Label Studio
integration, MLflow registry mirror, DVC layout, or Triton serving topology.

## 1. Constraints Restated

### 1.1 From The Platform Architecture

- The platform is open-source only and runs the backend stack in Ubuntu 24.04
  on WSL2.
- Normal workflows are browser-first. Operators must not need a CLI.
- PostgreSQL owns operational state machines and business truth.
- Label Studio CE owns the annotation UI, but immutable training snapshots are
  platform-owned after creation.
- MLflow OSS is an experiment and model metadata mirror, not deployment truth.
- DVC owns content-addressed dataset and artifact history.
- Triton owns loaded serving state only. It is never training truth.
- Training outputs never write directly into Triton repositories.
- Deployed model releases are immutable, versioned, and rollbackable.
- Realtime and batch inference already have separate gateways, Triton
  instances, queues, repositories, and active deployment pointers.
- Control-plane requests must not sit in the low-latency production inference
  path.
- Long-running work is durable, idempotent, auditable, retryable, and
  resume-aware.
- Production uses explicit Triton model control and exact Triton model versions.
- Development and production preserve the same service boundaries, APIs,
  schemas, queue names, and artifact conventions.

### 1.2 From The Vision Runtime Specification

- The runtime part is a second operational application part inside the same
  platform, not a foreign standalone system.
- Runtime owns channels, sources, live and trigger operation, pipeline
  execution, live overlay, result processing, output delivery, and data capture
  back into Label Studio.
- Runtime does not own training, dataset snapshot management for training,
  model registry, deployment creation, or raw model serving.
- Control plane, runtime plane, inference plane, trigger path, realtime path,
  and training path stay separated.
- Runtime execution is channel-driven. Operators think in channel, source,
  pipeline, result, and output.
- No manual internal IDs are required from operators.
- Pipelines are modular node graphs and are configured no-code by default.
- Realtime execution has no queue before inference, uses frame dropping instead
  of unbounded backlog, and keeps UI/admin APIs out of the live data path.
- Trigger execution accepts external HTTP PUT requests, captures a snapshot,
  runs the assigned pipeline deterministically, and returns a structured body
  inside a configured timeout.
- Realtime and trigger executors are separated.
- Inference nodes reference logical deployed model identifiers, never internal
  Triton names.
- A channel can capture full images or ROIs into one or more Label Studio
  projects.
- Extension must happen through defined source, node, capture, and connector
  plugin contracts without cross-service rewrites.

### 1.3 Hidden Assumptions And Safer Decisions

| Assumption | Safer Production-Oriented Decision |
|---|---|
| The existing `admin-api` is the only browser-facing API today. | Keep the existing `admin-api` intact and add a separate `runtime-admin-api` under the runtime plane. The admin web can call both authenticated APIs through the reverse proxy. |
| Siemens PLC trigger clients need stable network addresses. | Expose only `trigger-executor` trigger routes through a dedicated reverse-proxy path and scoped API keys. Do not expose internal runtime services. |
| RTSP streams can be flaky or slow. | Persist source health separately from channel desired state, use bounded frame buffers, and recover channels through the orchestrator. |
| Label Studio capture can become slow. | Capture rules emit durable capture work outside the realtime inference path. Trigger responses never wait for Label Studio ingestion unless an explicit connector requires it. |
| Model capabilities can change after promotion or rollback. | Discover capabilities from the existing deployment tables and inference gateway capability endpoints, cache them briefly, and bind pipeline versions to immutable logical model references. |
| Pipeline node plugins will expand. | Store node specs by stable `node_type`, `node_contract_version`, and JSON schema refs; reject unknown or incompatible nodes at publish time. |
| A single GPU must serve runtime, batch, and training. | Realtime runtime admission has priority; trigger concurrency is bounded; training remains pauseable and never shares runtime request paths. |

## 2. Runtime Plane Position

The platform has these logical planes after this extension:

- Control plane: existing admin, dataset, training, registry, deployment,
  webhook, worker, audit, and maintenance workflows.
- Runtime plane: channel-driven live execution, pipeline-derived HTTP trigger
  execution, pipeline graphs, overlay, capture, results, and output connectors.
- Inference plane: existing realtime and batch inference gateways plus Triton
  RT and Triton Batch.
- Training plane: existing training worker, training packages, checkpoints,
  export, MLflow, and DVC integration.

Runtime calls the inference plane through the existing inference gateway APIs.
Runtime never calls Triton directly in normal execution. Runtime can read model
and deployment metadata from PostgreSQL and the registry/deployment services,
but it does not mutate model versions, deployment releases, or Triton
repositories.

## 3. Required Service Set

### 3.1 `runtime-admin-api`

Owns the operator runtime API.

Responsibilities:

- channel CRUD and lifecycle commands
- source configuration API
- pipeline definition and version API aggregation
- derived trigger endpoint metadata reads
- capture rule and output connector configuration API
- runtime status, live session metadata, recent result, and audit reads
- capability reads for deployable logical model choices

Non-responsibilities:

- no frame processing
- no inference execution
- no model deployment mutation
- no Label Studio export or training snapshot ownership

### 3.2 `channel-service`

Owns channel and source business state.

Responsibilities:

- channel identity, display name, slug, technical place, description
- source configuration validation state
- channel desired state and observed state
- channel-to-pipeline assignment
- channel lifecycle transitions
- source health summaries

### 3.3 `pipeline-definition-service`

Owns pipeline graph definitions.

Responsibilities:

- node catalog
- node contract schemas
- pipeline DSL validation
- acyclic graph and port compatibility validation
- pipeline version publishing
- template pipeline definitions
- compatibility checks against model capabilities and source modes

### 3.4 `runtime-orchestrator`

Owns runtime process assignment and recovery.

Responsibilities:

- start, stop, pause, and recover runtime executions
- assign channels to realtime executors and reconcile runtime state
- enforce admission control
- track executor leases and heartbeats
- reconcile desired channel state with observed executor state
- stop workers before incompatible pipeline/source changes are activated

### 3.5 `realtime-executor`

Owns low-latency RTSP/live channel execution.

Responsibilities:

- RTSP and live source acquisition
- bounded frame buffering and drop policy
- realtime pipeline execution
- calls to `inference-gateway-rt`
- overlay payload emission
- result emission
- nonblocking capture event emission

Hard boundary:

- no queue before inference
- no training calls
- no batch gateway calls
- no operator API calls in the frame path

### 3.6 `trigger-executor`

Owns deterministic trigger request execution.

Responsibilities:

- external HTTP PUT endpoints derived from published pipelines with HTTP
  Trigger Input nodes
- trigger auth and request validation
- channel-bound frame acquisition
- triggerable pipeline execution
- calls to `inference-gateway-rt`
- structured response body
- timeout and error mapping
- result emission and optional capture event emission

Hard boundary:

- no RTSP continuous frame loop
- no training calls
- no batch gateway calls
- no Label Studio wait in the default response path

### 3.7 `overlay-service`

Owns live overlay state and render preparation.

Responsibilities:

- overlay payload model
- latest overlay by channel and runtime execution
- websocket/SSE delivery to admin web
- optional debug layers
- frame/result timestamp alignment metadata

### 3.8 `capture-service`

Owns image and ROI capture into Label Studio projects.

Responsibilities:

- capture rule execution outside the realtime critical path
- full-frame and ROI image materialization
- multi-project routing
- deduplication and rate limiting
- Label Studio upload/storage-sync abstraction
- capture audit and retry state

### 3.9 `result-service`

Owns runtime result records.

Responsibilities:

- latest result per channel
- result history with retention
- normalized result payloads
- raw model output references when retained
- business decisions and computed fields
- trigger request/result linkage

### 3.10 `connector-service`

Owns output connector delivery.

Responsibilities:

- overlay output connector
- HTTP trigger response connector schema
- persisted result output connector
- internal event output connector
- future connector plugin registration
- retries and delivery status outside realtime inference

## 4. Monorepo Extension Plan

Initial additive repository structure:

```text
backend/
  packages/
    cv-runtime/
      src/cv_runtime/
        domain.py
        topology.py
  services/
    runtime-admin-api/
    channel-service/
    pipeline-definition-service/
    runtime-orchestrator/
    realtime-executor/
    trigger-executor/
    overlay-service/
    capture-service/
    result-service/
    connector-service/
contracts/
  openapi/
    runtime-admin-api.v1.yaml
docs/
  runtime/
    runtime-plane-architecture-extension.md
backend/migrations/versions/
  20260501_0011_runtime_plane_architecture.py
tests/
  contract/
    test_runtime_contracts.py
  unit/backend/
    test_runtime_domain_contracts.py
```

Existing infrastructure files are extended, not replaced:

- `pyproject.toml`: add `cv-runtime` and runtime services as workspace members.
- `scripts/dev/export_openapi.py`: include runtime service OpenAPI export once
  implementation routes are registered.
- `infra/compose/compose.dev.yaml`: add runtime services for local topology.
- `infra/compose/compose.prod.yaml`: add production runtime services with the
  same logical boundaries.
- `infra/reverse-proxy/Caddyfile.*`: expose only browser/PLC entry points.
- `config/base/platform.yaml`: add typed runtime defaults.
- `config/schemas/platform-settings.schema.json`: regenerate from Pydantic.

## 5. API Boundaries

### 5.1 Browser-Facing Runtime Admin API

Base path through reverse proxy:

- development: `/runtime-api`
- production: `/runtime-api`

Canonical service prefix:

- `/api/v1/runtime`

Required route groups:

- `GET /api/v1/runtime/channels`
- `POST /api/v1/runtime/channels`
- `GET /api/v1/runtime/channels/{channel_id}`
- `PATCH /api/v1/runtime/channels/{channel_id}`
- `POST /api/v1/runtime/channels/{channel_id}:start`
- `POST /api/v1/runtime/channels/{channel_id}:stop`
- `GET /api/v1/runtime/channels/{channel_id}/state`
- `GET /api/v1/runtime/channels/{channel_id}/results/latest`
- `GET /api/v1/runtime/channels/{channel_id}/overlay/latest`
- `GET /api/v1/runtime/sources`
- `POST /api/v1/runtime/sources`
- `POST /api/v1/runtime/sources/{source_id}:validate`
- `GET /api/v1/runtime/pipeline-definitions`
- `POST /api/v1/runtime/pipeline-definitions`
- `GET /api/v1/runtime/pipeline-definitions/{pipeline_definition_id}`
- `POST /api/v1/runtime/pipeline-definitions/{pipeline_definition_id}/versions`
- `POST /api/v1/runtime/pipeline-versions/{pipeline_version_id}:publish`
- `POST /api/v1/runtime/pipeline-versions/{pipeline_version_id}:validate`
- `GET /api/v1/runtime/node-types`
- `GET /api/v1/runtime/model-capabilities`
- `GET /api/v1/runtime/trigger-endpoints`
- `POST /api/v1/runtime/trigger-endpoints`
- `GET /api/v1/runtime/trigger-endpoints/{trigger_endpoint_id}`
- `PUT /api/v1/runtime/trigger-endpoints/{trigger_endpoint_id}`
- `GET /api/v1/runtime/capture-rules`
- `POST /api/v1/runtime/capture-rules`
- `GET /api/v1/runtime/output-connectors`
- `POST /api/v1/runtime/output-connectors`
- `GET /api/v1/runtime/output-connectors/types`
- `GET /api/v1/runtime/output-connectors/{output_connector_id}`
- `PUT /api/v1/runtime/output-connectors/{output_connector_id}`
- `GET /api/v1/runtime/channels/{channel_id}/output-connectors`
- `GET /api/v1/runtime/channels/{channel_id}/connector-deliveries`
- `GET /api/v1/runtime/results`
- `GET /api/v1/runtime/executions`

Mutating routes require:

- authenticated operator session
- CSRF for browser session use
- `Idempotency-Key`
- audit event
- no manual internal ID in request bodies

### 5.2 Trigger Executor API

Base path through reverse proxy:

- development: `/runtime-trigger`
- production: `/runtime-trigger`

Required routes:

- `PUT /api/v1/runtime/triggers/{endpoint_slug}`

Trigger requests use:

- optional API-key authentication by default, with per-endpoint
  `auth_required` support and scope `runtime.trigger.invoke`
- trigger slug generated by the platform, editable only as a safe slug
- request timeout default from config
- deterministic response schema
- correlation ID in response

### 5.3 Overlay API

Browser access should normally flow through `runtime-admin-api`. If direct
streaming is required:

- `GET /api/v1/overlay/channels/{channel_id}/events`
- `GET /api/v1/overlay/channels/{channel_id}/latest`

The direct overlay route requires the same session auth as admin-web and must
not expose raw source credentials.

### 5.4 Internal Runtime APIs

These are only reachable on the private runtime/control networks:

- `channel-service`
- `pipeline-definition-service`
- `runtime-orchestrator`
- `realtime-executor`
- `capture-service`
- `result-service`
- `connector-service`

Internal calls use service tokens, correlation IDs, and typed JSON contracts.

## 6. Event Flows

### 6.1 Channel Start

1. Operator clicks start on a channel.
2. `runtime-admin-api` validates permissions and idempotency.
3. `channel-service` transitions channel desired state to `start_requested`.
4. `runtime-orchestrator` observes requested state or receives an internal
   command.
5. Orchestrator validates source, pipeline version, executor capacity, and model
   capabilities.
6. Orchestrator creates a `runtime_execution` row.
7. Orchestrator leases the execution to `realtime-executor` or
   `trigger-executor`.
8. Executor heartbeats and transitions observed state to `running`.
9. Channel observed state becomes `running`.
10. Audit and state change events are appended.

### 6.2 Realtime Frame Flow

1. `realtime-executor` reads from the configured source.
2. Frame sampler applies bounded buffer and drop strategy.
3. Pipeline nodes run in process.
4. Inference node resolves logical model reference from the immutable pipeline
   version to an active deployment capability.
5. Executor calls `inference-gateway-rt`.
6. Analysis nodes compute normalized result fields.
7. Executor emits latest result to `result-service`.
8. Executor emits overlay payload to `overlay-service`.
9. Capture nodes emit nonblocking capture work to `capture-service`.
10. Connector nodes emit nonblocking connector work to `connector-service`.

### 6.3 Trigger Request Flow

1. PLC sends `PUT /runtime-trigger/api/v1/runtime/triggers/{endpoint_slug}`.
2. `trigger-executor` authenticates and resolves the endpoint metadata derived
   from the published pipeline and HTTP Trigger Input node.
3. The trigger executor creates request execution context.
4. `source.channel_frame` captures one image from the endpoint channel-owned
   source or accepted request image payload.
5. Pipeline runs with a request-scoped timeout budget.
6. Inference nodes call `inference-gateway-rt`.
7. `output.http_trigger_response` builds the deterministic response body.
8. Result or timeout state is persisted inside the trigger path.
9. Response returns structured result, error, timing, and correlation metadata.

### 6.4 Capture To Label Studio

1. A capture node emits `capture.requested.v1` with image/ROI references and
   target project mappings.
2. `capture-service` deduplicates by channel, rule, frame digest, ROI digest,
   and target project.
3. Captured assets are written to runtime capture storage using temp paths and
   atomic rename.
4. Capture uses the existing Label Studio API/storage integration abstraction.
5. Capture rows transition to `submitted`, `accepted`, or `failed_retryable`.
6. The training-side Label Studio webhooks remain independent and continue to
   drive dataset/snapshot workflows.

### 6.5 Manual Live Capture To Label Studio

1. An operator opens the channel live view.
2. `runtime-admin-api` lists available Label Studio capture targets from the
   platform Label Studio project mapping.
3. The operator chooses a project and clicks `Capture once`, or starts an
   interval session.
4. The runtime backend captures the current full frame, writes the asset to
   runtime capture storage, submits it to Label Studio, and records manual
   capture metadata.
5. Interval capture is owned by a backend task associated with the channel. The
   browser polls state and history but does not own the timer.
6. Manual live capture records remain separate from pipeline capture requests.

Manual live capture is documented in
`docs/runtime/manual-live-capture.md`. It is an operator convenience path for
real data collection and is not coupled to training, snapshot creation, model
promotion, or deployment workflows.

### 6.6 Pipeline Publish Flow

1. Operator edits a draft pipeline through the no-code UI.
2. `pipeline-definition-service` validates graph shape, node schemas, ports,
   mode compatibility, and logical model references.
3. A new immutable `pipeline_version` is created.
4. Publish checks current model capabilities but stores logical model refs,
   capability constraints, and contract major, not Triton names.
5. Channels move to the new pipeline version only through an explicit assignment
   or template default update.

## 7. Database Schema Outline

All tables live in the existing `cv_platform` PostgreSQL database and use the
same migration chain and audit model.

### `runtime_channels`

- `id`
- `slug`
- `name`
- `technical_place`
- `description`
- `mode`
- `source_id`
- `pipeline_version_id`
- `desired_state`
- `observed_state`
- `health_state`
- `last_activity_at`
- `last_error`
- `created_by`
- `updated_by`
- `created_at`
- `updated_at`

### `runtime_sources`

- `id`
- `slug`
- `name`
- `source_type`
- `config`
- `secret_refs`
- `validation_status`
- `health_state`
- `last_validated_at`
- `last_error`
- `created_at`
- `updated_at`

### `pipeline_definitions`

- `id`
- `slug`
- `name`
- `description`
- `status`
- `latest_version_id`
- `published_version_id`
- `template`
- `created_by`
- `created_at`
- `updated_at`

### `pipeline_versions`

- `id`
- `pipeline_definition_id`
- `version_no`
- `status`
- `mode`
- `graph`
- `validation_report`
- `model_capability_constraints`
- `node_contract_versions`
- `config_digest`
- `change_note`
- `created_by`
- `published_at`
- `published_by`
- `created_at`

### `pipeline_nodes`

- `id`
- `pipeline_version_id`
- `node_key`
- `node_type`
- `node_category`
- `node_contract_version`
- `input_ports`
- `output_ports`
- `config`
- `logical_model_ref`
- `position`
- `created_at`

### `runtime_executions`

- `id`
- `channel_id`
- `pipeline_version_id`
- `mode`
- `executor_service`
- `executor_instance_id`
- `status`
- `desired_state`
- `started_at`
- `stopped_at`
- `heartbeat_at`
- `frame_count`
- `drop_count`
- `trigger_count`
- `last_result_id`
- `last_error`
- `recovery_attempts`
- `created_at`

### `trigger_endpoints`

- `id`
- `channel_id`
- `slug`
- `method`
- `auth_scope`
- `timeout_ms`
- `request_schema`
- `response_schema`
- `status`
- `last_invoked_at`
- `created_at`
- `updated_at`

### `capture_rules`

- `id`
- `channel_id`
- `pipeline_version_id`
- `rule_key`
- `capture_mode`
- `condition`
- `target_label_project_ids`
- `rate_limit`
- `dedupe_policy`
- `status`
- `created_at`
- `updated_at`

### `output_connectors`

- `id`
- `channel_id`
- `connector_type`
- `name`
- `config`
- `secret_refs`
- `delivery_policy`
- `status`
- `last_delivery_at`
- `last_error`
- `created_at`
- `updated_at`

### `result_records`

- `id`
- `channel_id`
- `runtime_execution_id`
- `trigger_endpoint_id`
- `correlation_id`
- `source_timestamp`
- `completed_at`
- `status`
- `schema_version`
- `result`
- `raw_outputs_ref`
- `timing`
- `model_routes`
- `created_at`

Additional implementation tables:

- `runtime_capture_requests`
- `runtime_connector_deliveries`
- `runtime_node_type_registry`
- `runtime_model_capability_cache`

## 8. Runtime State Model

### 8.1 Channel Desired State

- `draft`
- `stopped`
- `start_requested`
- `running`
- `stop_requested`
- `disabled`

Only operator commands and orchestrator reconciliations write desired state.

### 8.2 Channel Observed State

- `unassigned`
- `starting`
- `running`
- `degraded`
- `stopping`
- `stopped`
- `failed`
- `recovering`

Only executors and the orchestrator write observed state.

### 8.3 Runtime Execution State

- `requested`
- `admitted`
- `starting`
- `running`
- `draining`
- `stopped`
- `failed_retryable`
- `failed_terminal`
- `recovered`

Every execution state transition emits an audit event and updates
`runtime_executions`.

### 8.4 Pipeline Version State

- `draft`
- `validated`
- `published`
- `superseded`
- `archived`

Published versions are immutable. Editing a published pipeline creates a new
version.

## 9. Integration With Existing Model And Inference Services

### 9.1 Capability Discovery

Runtime model capability choices come from:

- `deployment_targets`
- `deployment_releases`
- `model_families`
- `model_versions`
- `GET /api/v1/realtime/models/capabilities` from `inference-gateway-rt`

`pipeline-definition-service` uses these sources to validate that a logical
model reference can satisfy a node. `realtime-executor` and `trigger-executor`
use a short-lived cache for runtime routing. A capability is only usable when:

- target path is `realtime`
- deployment release is active or explicitly selected as a staged test target
- model family task type matches the inference node type
- contract major matches the node contract
- active gateway reports the model route as available

### 9.2 Logical Model References

Inference nodes store logical refs:

```json
{
  "logical_model_ref": {
    "model_family_slug": "defect_detection",
    "target_path": "realtime",
    "contract_major": 1,
    "selection_policy": "active_production",
    "required_task_type": "detection"
  }
}
```

They do not store:

- Triton model name
- Triton numeric model version
- serving repository path
- MLflow run ID
- model artifact path

Runtime resolves the logical ref to the current active deployment release at
execution time with a bounded cache TTL. For traceability, each `result_record`
stores the resolved `deployment_release_id`, `model_version_id`,
`triton_model_name`, and `triton_model_version` in `model_routes`.

## 10. Worker And Process Lifecycle

Runtime workers are normal services managed by compose/systemd, but individual
channel executions are leased by `runtime-orchestrator`.

Rules:

- Executors do not choose channels themselves.
- Orchestrator writes a lease with `runtime_execution_id`,
  `executor_service`, `executor_instance_id`, and `lease_expires_at`.
- Executor heartbeats refresh the lease and observed state.
- Missing heartbeat beyond `runtime.executor_stale_after_seconds` makes the
  execution recoverable.
- Realtime channel recovery starts a new execution and marks the old execution
  `failed_retryable` or `recovered` after handoff.
- Pipeline-derived trigger endpoints are ready only when the endpoint is
  enabled, the published pipeline remains valid, auth configuration is valid,
  runtime services are available, and the Channel frame input node can acquire
  from the endpoint channel. A live channel runtime is required only when the
  channel/source uses live-frame fallback.

## 11. Realtime And Trigger Isolation

Realtime and trigger execution are isolated by:

- separate services
- separate HTTP route surfaces
- separate concurrency settings
- separate runtime execution/result correlation records
- separate timeout budgets
- separate result correlation semantics
- no shared pre-inference queues

They may share read-only pipeline definitions, published pipeline versions,
model capability caches, and output/capture services. Shared services are never
in the synchronous inference call before `inference-gateway-rt`.

## 12. Failure And Recovery Strategy

### 12.1 Realtime

- RTSP reconnect uses exponential backoff with jitter.
- Bounded frame queues drop oldest frames by default.
- If inference latency exceeds channel SLO, sampling is reduced before the
  channel is marked degraded.
- If gateway readiness fails, channel state becomes degraded and then
  recoverable failed after configured timeout.
- Last good overlay/result remains available with stale markers.

### 12.2 Trigger

- Trigger requests have a total endpoint timeout.
- On timeout, response is deterministic with `status=timeout`,
  `error_code=runtime_trigger_timeout`, and the correlation ID.
- Capture/connector failures do not change the trigger response unless the
  pipeline explicitly marks that output connector as response-critical.
- Duplicate trigger idempotency remains future work; clients should preserve
  `trigger_id` across retries for correlation.

### 12.3 Capture

- Capture writes to temp paths and uses atomic rename.
- Label Studio submission is retryable.
- Duplicate image/ROI/project submissions are suppressed by digest.
- Failed capture never blocks live inference.

### 12.4 Orchestration

- Orchestrator reconciliation is idempotent.
- Channel desired state is preserved across service restarts.
- Executor leases expire safely.
- A crashed executor cannot leave a channel falsely marked healthy because
  heartbeat freshness is part of readiness.

## 13. Development And Production Topology

### 13.1 Development

- all runtime services run in the existing `podman compose` dev profile
- routes for internal runtime services may be exposed for developer debugging
- storage root is `.local/state/runtime`
- RTSP/test file sources are supported
- trigger endpoints are reachable on localhost through reverse proxy
- executors use low concurrency and aggressive stale-heartbeat detection

### 13.2 Production

- runtime services run in the existing production compose/systemd stack
- browser-facing route: `/runtime-api`
- PLC-facing route: `/runtime-trigger`
- optional browser overlay route: `/runtime-overlay`
- internal runtime services are private to compose networks
- storage root is `/srv/cv-platform/runtime`
- TLS is required at the reverse proxy
- trigger API keys are scoped and revocable
- channel execution state is recovered after service restart

## 14. Plugin And Node Extension Model

Node plugins are registered through `runtime_node_type_registry`.
The implementation guide for node schemas, validation, templates, and plugin
extension is `docs/runtime/pipeline-node-plugin-model.md`.

Each node type declares:

- `node_type`
- `node_category`
- `contract_version`
- `display_name`
- `supported_modes`
- `input_ports`
- `output_ports`
- `config_schema_ref`
- `runtime_handler_ref`
- `compatibility_constraints`

Plugin rules:

- plugin packages must be open-source compatible
- no plugin can require a Triton internal name from an operator
- config schemas are validated at draft save and publish
- runtime handlers are loaded only from allowlisted package entry points
- a missing plugin prevents publish or start; it does not silently skip a node

## 15. Milestone Roadmap

### Milestone R1: Runtime Foundations

- `cv-runtime` package
- runtime contracts
- runtime database migration
- service skeletons and health checks
- reverse-proxy and compose topology
- runtime architecture tests

Exit criteria:

- all runtime services start and expose health
- migration creates runtime domain tables
- docs and contract tests assert required boundaries

### Milestone R2: Channel And Pipeline Management

- channel CRUD
- source CRUD and validation
- pipeline DSL persistence
- node type registry
- pipeline publish validation
- model capability discovery

Exit criteria:

- operator can create a channel, attach a source, build a pipeline, and publish
  a version without manual internal IDs

### Milestone R3: Trigger Execution

- trigger endpoint management
- API-key protected PUT endpoint
- channel frame input node for trigger snapshots
- realtime inference node through `inference-gateway-rt`
- deterministic response output node
- result persistence

Exit criteria:

- PLC-style HTTP PUT returns real pipeline results from a deployed model

### Milestone R4: Realtime Execution And Overlay

- channel frame input node for realtime streams
- realtime executor loop
- frame sampler/drop policy
- overlay event stream
- latency/FPS/drop metrics
- channel recovery

Exit criteria:

- RTSP channel runs continuously with overlay and no queue before inference

### Milestone R5: Capture And Connectors

- full-image and ROI capture
- multi-project Label Studio routing
- capture dedupe/rate limits
- persisted result connector
- internal event connector

Exit criteria:

- channel captures full images and ROIs into multiple Label Studio projects

### Milestone R6: Hardening And Acceptance

- recovery drills
- stale executor reconciliation
- trigger idempotency
- load/performance tests
- production runbook updates
- admin UI no-code workflow completion

Exit criteria:

- runtime acceptance criteria from `vision-runtime-spec.md` pass end to end
