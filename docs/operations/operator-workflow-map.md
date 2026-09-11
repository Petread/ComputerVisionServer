# Operator Workflow Map

This map ties each normal operator workflow to the visible UI surface, authoritative API, queue ownership, and durable outputs.

| Workflow | Admin UI Surface | Authoritative API | Background Queue | Durable Output |
| --- | --- | --- | --- | --- |
| User management | `Access & Security -> Users` | user access routes on `admin-api` | none | user rows, session-version invalidation state, audit trail |
| API key lifecycle | `Access & Security -> API Keys` | API key access routes on `admin-api` plus inference gateway auth reads | none | managed API key rows, masked metadata, usage timestamps, audit trail |
| Dataset creation | `Dataset Snapshots` | `POST /api/v1/datasets` | none | dataset row in PostgreSQL |
| Browser upload session | `Dataset Snapshots` | upload-session routes on `admin-api` | `dataset` for finalization | dataset assets under deterministic storage root |
| Label Studio discovery/mapping | `Label Studio Projects` | label-project routes on `admin-api` | `dataset` for sync | label project mapping and audit trail |
| Immutable snapshot | `Dataset Snapshots`, `Label Studio Projects` | `POST /api/v1/dataset-snapshots` | `dataset` | immutable snapshot directory, validation report, DVC tracking |
| Training job | `Training Jobs` | `POST /api/v1/training-jobs` | `training` | checkpoints, logs, metrics, export manifests, model version |
| Model review and approval | `Experiments & Registry` | registry routes on `admin-api` | none | approvals, lifecycle state, rollback markers |
| Deployment package build | `Deployment` | `POST /api/v1/deployments/packages` | `deploy` | immutable serving package |
| Stage release | `Deployment` | `POST /api/v1/deployments/promotions` | `deploy` | deployment release, compatibility report, smoke report |
| Promote to production | `Deployment` | `POST /api/v1/deployments/promotions` | `deploy` | updated deployment target pointer |
| Rollback | `Deployment` | `POST /api/v1/deployments/{target_path}:rollback` | `deploy` | previous immutable release restored as active |
| Deactivate model serving | `Deployment -> Deployment Releases` | `POST /api/v1/deployments/releases/{id}:deactivate` | `deploy` | retained release history with Triton model unloaded |
| Reactivate model serving | `Deployment -> Deployment Releases` | `POST /api/v1/deployments/releases/{id}:activate` | `deploy` | retained immutable release loaded into Triton again |
| Realtime testing | `Realtime Testing` | realtime inference gateway | none | request/response evidence, latency samples |
| Batch inference | `Batch Inference` | batch-job routes on `admin-api` plus batch gateway reads | `batch` | chunk manifests, batch results, bundle |
| Runtime pipeline authoring | `Runtime Pipelines` | `/runtime-api/api/v1/runtime/pipeline-definitions*` | none | versioned published runtime graph, validation report, node contracts, derived HTTP trigger endpoint metadata |
| Channel lifecycle and live view | `Channels` | `/runtime-api/api/v1/runtime/channels*` plus `runtime-control` worker/orchestrator | runtime-control path | runtime instance rows, live frame/overlay state, metrics, audit trail |
| Trigger endpoint invocation | `Runtime Pipelines` derived endpoint and external PLC route | `/runtime-trigger/api/v1/runtime/triggers/{slug}` | none in critical path | deterministic trigger response from Response Output, persisted result, endpoint latency/status |
| Manual and pipeline capture | `Channels -> Live View / Capture` | `/runtime-api/api/v1/runtime/channels/{id}/manual-capture/*` plus capture-service drain | runtime capture delivery | capture request rows, materialized full-frame/ROI assets, Label Studio task IDs |
| Output connector delivery | `Channels -> Output Connectors` | `/runtime-api/api/v1/runtime/output-connectors*` plus connector-service drain | runtime connector delivery | connector definitions, delivery rows, internal events/overlay/persisted outputs |
| Runtime results | `Results`, `Channels -> Recent Results` | `/runtime-results/api/v1/runtime/results*` | none | typed raw, normalized, derived, overlay, and trigger-response result records |
| Audit and health | `Audit Log`, `Overview`, `Operational Health`, `Settings` | `admin-api` reads | maintenance queue updates supporting data | audit rows, health snapshots, heartbeats |

## Route And Navigation Map

| Navigation | Route | Primary Service | Notes |
| --- | --- | --- | --- |
| `Home` | `/` | `admin-api` aggregated reads | workflow health, runtime summary, alerts |
| `Pipelines` | `/pipelines` | `admin-api` | dataset/training/deployment happy path |
| `Models` | `/models` | `admin-api` | project-aware model catalog |
| `Endpoints` | `/endpoints` | `admin-api` and inference gateways | logical inference endpoint visibility |
| `Channels` | `/channels`, `/channels/{channel_id}` | `runtime-admin-api` | source/channel setup, lifecycle, live view, triggerable pipeline references, capture, connectors |
| `Runtime Pipelines` | `/runtime-pipelines` | `runtime-admin-api` | no-code runtime graph designer, triggerable pipeline configuration, publisher |
| `Results` | `/results` | `result-service` through runtime result routes | historical runtime result browsing |
| `Advanced` | `/operations/*` | `admin-api` | snapshots, training, registry, deployment, inference, batch, audit, settings, health |

## Operator Rule

If a workflow is in this map, the supported operator path is the admin UI. Shell commands are for engineering bring-up, debugging, or maintenance only.

Bootstrap API keys are the deliberate exception for mutation: they are visible in the UI for migration awareness, but changes still happen through configuration and mounted secret files.
