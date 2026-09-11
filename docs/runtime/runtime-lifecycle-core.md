# Runtime Lifecycle Core

Authoritative references:

- `docs/architecture/cv-platform-spec.md`
- `docs/runtime/vision-runtime-spec.md`
- `docs/runtime/vision-runtime-roadmap.md`

## Purpose

W1 turns channels from stored configuration into supervised runtime instances.
`runtime-admin-api` accepts operator commands, but `runtime-orchestrator` owns
runtime-control consumption, validation, instance creation, executor launch,
heartbeat supervision, stop/restart, and reconciliation.

## Planes And Ownership

- `runtime-admin-api`: authenticated operator command API and read model.
- `runtime-orchestrator`: runtime-control command consumer and supervisor.
- `realtime-executor`: realtime runtime endpoint, RTSP/file-test frame loop,
  bounded buffering, pipeline execution, live overlay state, and heartbeats.
- `trigger-executor`: authenticated pipeline HTTP trigger endpoint; resolves
  endpoint metadata derived from published pipelines and acquires one frame from
  the endpoint channel source.
- `inference-gateway-rt`: inference plane call target for runtime inference
  nodes. Runtime services do not talk to Triton directly.

The control plane, runtime plane, inference plane, and training plane remain
separate.

## Command Semantics

1. `Start runtime` performs synchronous validation in `runtime-admin-api`.
2. If validation fails, the API returns `422` and no runtime-control command is
   queued.
3. If validation passes, a `runtime_control_commands` row is persisted with
   status `queued`, and the channel desired state becomes `start_requested`.
4. `runtime-orchestrator` claims the command, transitions it through
   `processing`, creates a `runtime_executions` instance, and starts the
   executor handle.
5. The command becomes `succeeded` only after the instance reaches `running`.
6. Stop and restart use the same durable command path. Stop completes only after
   the active runtime instance is stopped and persisted.

There are no accepted runtime-control commands without a persisted command row
and a runtime-orchestrator consumer path.

## State Machine

Channel runtime state is exposed as `runtime_state` on channel list and detail
APIs.

Required operational states:

- `stopped`: no active runtime instance.
- `starting`: executor startup has begun.
- `validating`: runtime-control is validating source, pipeline, handlers, and
  model capabilities.
- `loading_pipeline`: the published pipeline is compiled into an executable
  runtime plan.
- `running`: executor heartbeat is current and the endpoint is ready.
- `degraded`: heartbeat is stale or the instance is recoverable but unhealthy.
- `stopping`: runtime-control is draining and stopping the instance.
- `failed`: runtime-control or heartbeat failure requires operator attention.

## Desired State Vs Actual State

- `desired_state` records the operator/runtime-control intent, such as
  `start_requested`, `running`, `stop_requested`, or `stopped`.
- `actual_state` records what the supervised runtime instance is actually doing.
- `loading_state` adds progress detail during validation, pipeline loading, and
  executor startup.

Operators should trust `actual_state` and `health_summary` for operational
status. A desired state of `running` does not mean the channel is ready until
`actual_state` is also `running` and heartbeat is fresh.

## Runtime Instance Binding

Every started channel gets a `runtime_executions` record with:

- `channel_id`
- `runtime_execution_id`
- `mode`
- `pipeline_version_id`
- `executor_service`
- `assigned_runtime_worker`
- `desired_state`
- `actual_state`
- `loading_state`
- `heartbeat_at`
- `health_summary`
- `metrics_summary`
- `runtime_plan`

Later result, overlay, capture, and connector work must bind to
`runtime_execution_id` when recording outputs. Channel ID remains the operator
navigation concept; runtime instance ID is the execution truth.

## Readiness Validation

Start validates:

- channel exists and is not disabled
- source is present and safe
- RTSP URLs do not contain inline credentials
- assigned live pipeline version exists, is published, and matches realtime
  channel lifecycle expectations
- registered runtime handlers can be loaded
- inference nodes resolve logical model capabilities for the realtime target

HTTP trigger PUT calls do not use channel runtime-control commands. They
resolve a pipeline-derived endpoint from a published pipeline containing HTTP
Trigger Input, Channel frame input, and Response Output nodes. Trigger requests
with `image_path`, `path`, `file_path`, `local_path`, or `server_path` are
rejected; clients must send image bytes/base64 or let the Channel frame input
node acquire one frame from the endpoint channel.

## Heartbeats And Reconciliation

Executor handles update `runtime_executions.heartbeat_at` and metrics summaries.
`runtime-orchestrator` marks stale heartbeats as `degraded`, then `failed` when
the stale period exceeds the failure threshold.

On orchestrator restart, reconciliation inspects desired running instances and
recreates supervisor handles for recoverable records. Admin UI refreshes do not
affect runtime instances because state is persisted in PostgreSQL.

## Failure And Recovery

- Synchronous validation errors: operator sees `422` and recovery hints.
- Command processing failure: command becomes `failed`, channel actual state
  becomes `failed`, and `last_error` is visible.
- Stale heartbeat: channel becomes `degraded`, then `failed` if unrecovered.
- Stop: channel becomes `stopping`, then `stopped`; active runtime pointer is
  cleared.
- Restart: active instance is stopped, then a new instance is started and bound.

## Forward Bindings

- W2 realtime frames and overlay state use the active `runtime_execution_id`.
- W3 PLC trigger endpoint readiness is derived from the published triggerable
  pipeline, endpoint metadata, auth configuration, runtime service readiness,
  and channel frame acquisition capability.
- W4 result records already carry `runtime_execution_id`.
- W5 capture requests and manual capture records should bind captured frames to
  the runtime instance that produced them.
- W6 connector deliveries should use result records that are tied to runtime
  instances.
