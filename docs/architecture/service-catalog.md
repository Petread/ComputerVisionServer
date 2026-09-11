# Service Catalog

## Why Each Service Exists

- `admin-api`: the authoritative operator-facing control-plane API. The admin UI submits normal workflows here instead of calling internal bounded services directly.
- `webhook-receiver`: persists Label Studio webhook payloads quickly and queues asynchronous follow-up work.
- `dataset-service`: bounded dataset and snapshot API surface sharing the same contracts and persistence logic as the control plane.
- `training-orchestrator`: bounded training API surface for queue-backed training lifecycle commands and read models.
- `model-registry-service`: bounded registry API surface for model families, versions, approvals, and lifecycle reads.
- `deployment-controller`: bounded API surface for package builds, stage/promote flows, smoke tests, and rollback commands.
- `inference-gateway`: shared codebase with isolated realtime and batch runtime entrypoints, contracts, caches, and target pointers.
- `runtime-admin-api`: operator-facing runtime-plane API for channels, sources, pipeline versions, derived trigger endpoint metadata, capture rules, output connectors, status, and results.
- `channel-service`: runtime service of record for channel/source metadata, desired state, observed state, technical place assignment, and channel lifecycle.
- `pipeline-definition-service`: runtime service of record for node types, pipeline DSL validation, immutable pipeline versions, and template pipelines.
- `runtime-orchestrator`: runtime execution coordinator for channel start/stop, executor leases, admission control, heartbeat reconciliation, and recovery.
- `realtime-executor`: realtime runtime executor for RTSP/live frame flow, bounded frame dropping, calls to `inference-gateway-rt`, overlay emission, and result emission.
- `trigger-executor`: runtime executor for API-key protected HTTP PUT calls derived from triggerable pipelines, channel-bound frame acquisition, deterministic pipeline execution, and Response Output shaping.
- `overlay-service`: runtime service for latest overlay state and browser-facing overlay event delivery.
- `capture-service`: runtime service for full-image/ROI capture, dedupe, rate control, and Label Studio project routing.
- `result-service`: runtime service for latest result and result-history persistence.
- `connector-service`: runtime service for output connector delivery, retry state, and connector plugin extension.
- `control-worker`: executes `webhook`, `dataset`, `deploy`, and `maintenance` queues.
- `training-worker`: owns the single-GPU training queue with resumable checkpoint recovery.
- `batch-worker`: executes chunked batch inference without sharing the realtime request path.
- `maintenance-worker`: owns reconciliation, retention, cleanup, and backup jobs.

## Service Boundary Summary

- Control plane: `admin-api`, `webhook-receiver`, `dataset-service`, `training-orchestrator`, `model-registry-service`, `deployment-controller`
- Runtime plane: `runtime-admin-api`, `channel-service`, `pipeline-definition-service`, `runtime-orchestrator`, `realtime-executor`, `trigger-executor`, `overlay-service`, `capture-service`, `result-service`, `connector-service`
- Training plane: `training-worker` plus shared `cv-training` and `cv-deploy` packages
- Data plane: `inference-gateway-rt`, `inference-gateway-batch`, `triton-rt`, `triton-batch`
- Shared infrastructure: PostgreSQL, Redis, MLflow, Label Studio, DVC storage, reverse proxy, observability
