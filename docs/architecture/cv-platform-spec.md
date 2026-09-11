# Production CV Platform Specification

## 1. Scope

This specification defines an implementation-ready architecture for a production-grade, fully open-source, on-prem computer vision platform running on a Windows workstation with an NVIDIA RTX 4500 Ada GPU.

The system is designed for:

- browser-based dataset upload
- Label Studio CE based annotation management
- training orchestration without operator CLI usage
- experiment tracking and model registry with MLflow OSS
- dataset and artifact versioning with DVC
- safe deployment to Triton Inference Server
- isolated realtime and batch inference paths
- deterministic, resumable, auditable operations

This is not a demo architecture. The design assumes long-running use by multiple labeling users and internal inference consumers on a local network.

## 2. Design Decisions

### 2.1 Runtime Target

Production and development both run the backend stack inside `Ubuntu 24.04 LTS on WSL2` on the Windows workstation.

Reasoning:

- CUDA on WSL2 is supported by NVIDIA and Microsoft for Linux CUDA workloads on Windows.
- Triton, TensorRT, PyTorch, Redis, PostgreSQL, MLflow, and Label Studio are materially easier to operate in Linux than as a mixed Windows-native stack.
- The operator experience remains browser-first. WSL2 is an infrastructure detail, not part of the normal operating workflow.

### 2.2 Platform Shape

The platform is split into:

- `Control plane`: admin UI, orchestration API, webhook ingestion, operational metadata, queues, workers, model registry state, audit, deployment control
- `Data plane`: inference gateways, Triton servers, serving model repositories
- `Training plane`: training worker, checkpointing, exports, evaluation

### 2.3 Source of Truth

Different subsystems own different truths:

- `PostgreSQL`: operational state machines and business truth
- `Label Studio CE`: active annotation project/task UI and annotation entry
- `MLflow OSS`: experiment lineage, metrics, run artifacts, model registry metadata mirror
- `DVC`: content-addressed dataset and artifact version history
- `Filesystem`: immutable artifacts and serving packages
- `Triton`: loaded serving state, never training truth

### 2.4 Hard Rules

- No operator CLI is required for normal workflows.
- Webhook handling is always asynchronous after a fast persistence step.
- Training outputs never write directly into Triton model repositories.
- Deployed models are immutable, versioned, and rollbackable.
- Realtime inference never shares a request path, queue, or Triton process with batch inference.
- Training and inference are decoupled operationally; training jobs survive UI refreshes and browser closure.

## 3. External Constraints Validated Against Upstream Docs

These choices are deliberate because of current upstream behavior:

- Label Studio CE can export snapshots through the API; large exports should use snapshot export rather than simple export to avoid timeouts.
- MLflow now recommends aliases and tags over legacy stage semantics, so platform lifecycle state is stored in PostgreSQL and mirrored into MLflow tags and aliases instead of using MLflow stages as the primary control mechanism.
- Triton production rollout uses `model-control-mode=explicit`, because Triton documents that `poll` mode is not recommended for production due to partial repository change risk.

Useful upstream references:

- Label Studio export docs: <https://labelstud.io/guide/export.html>
- Label Studio webhook event reference: <https://labelstud.io/guide/webhook_reference>
- MLflow model registry docs: <https://www.mlflow.org/docs/latest/ml/model-registry/workflow/>
- Triton model management docs: <https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html>
- CUDA on WSL guide: <https://docs.nvidia.com/cuda/wsl-user-guide/index.html>
- Microsoft WSL CUDA guidance: <https://learn.microsoft.com/en-us/windows/ai/directml/gpu-cuda-in-wsl>

## 4. Production Architecture

### 4.1 Runtime Topology

All services run on one workstation, but as isolated processes or containers with dedicated storage roots and network boundaries.

```text
Browser
  -> Reverse proxy
    -> admin-web
    -> control-plane-api
    -> label-studio
    -> mlflow
    -> inference-gateway-rt
    -> system-status endpoints

Label Studio CE
  -> webhook-receiver
  -> PostgreSQL

control-plane-api
  -> PostgreSQL
  -> Redis
  -> Label Studio API
  -> MLflow API

RQ workers
  -> PostgreSQL
  -> Redis
  -> Label Studio API
  -> DVC repo and remote
  -> MLflow API
  -> local artifact storage
  -> Triton control APIs

inference-gateway-rt
  -> Triton RT (gRPC)

inference-gateway-batch
  -> Triton Batch (gRPC)

batch-worker
  -> inference-gateway-batch

runtime-admin-api
  -> PostgreSQL
  -> Redis

runtime-orchestrator
  -> PostgreSQL
  -> runtime executors

realtime-executor
  -> inference-gateway-rt

trigger-executor
  -> inference-gateway-rt

training-worker
  -> GPU
  -> MLflow
  -> DVC
  -> artifact storage
```

### 4.2 Production Services

#### Entry and UX

- `reverse-proxy`
  - Single browser entry point
  - TLS termination
  - routes `/`, `/api`, `/label-studio`, `/mlflow`, `/inference`, `/health`
- `admin-web`
  - Single operational entry point for operators and ML engineers
  - manages datasets, snapshots, training jobs, promotions, deployments, batch jobs, health
  - deep-links and embeds Label Studio and MLflow where useful

#### Control Plane

- `control-plane-api`
  - authoritative API for platform workflows
  - owns PostgreSQL schema and state transitions
  - exposes versioned REST APIs
  - enqueues background work only after durable state write
- `webhook-receiver`
  - dedicated Label Studio webhook ingress
  - validates source, persists raw event, acknowledges immediately
  - emits follow-up jobs to Redis/RQ
- `postgres`
  - operational metadata
  - append-only audit/event tables
- `redis`
  - RQ queue backbone
  - transient locks and coordination
- `rq-control-worker`
  - dataset, snapshot, registry, deployment, and maintenance jobs
- `rq-training-worker`
  - single-concurrency GPU training execution
  - resume-safe checkpointing
- `rq-batch-worker`
  - batch job orchestration and chunk execution
- `rq-maintenance-worker`
  - cleanup, reconciliations, retries, health snapshots, backup orchestration
- `mlflow`
  - experiment tracking and model metadata mirror
- `dvc-repo`
  - version history for snapshots and artifacts

#### Data Plane

- `inference-gateway-rt`
  - low-latency synchronous inference API
  - request validation, auth, preprocessing, postprocessing, contract enforcement
  - only talks to realtime Triton
- `triton-rt`
  - serves production and staged realtime models
  - separate model repository root
- `inference-gateway-batch`
  - internal and optional external asynchronous batch inference API
  - separate deployment from realtime gateway
  - only talks to batch Triton
- `triton-batch`
  - serves batch models
  - separate model repository root

#### Annotation Plane

- `label-studio`
  - project/task/annotation UI
  - browser-based labeling operations
  - project membership managed inside Label Studio CE

## 5. Development Mode Architecture

Development mode preserves the same logical services and contracts, but uses smaller persistence roots and relaxed non-functional settings.

### 5.1 Development Profile

- all services run through one `podman compose` profile in WSL2
- self-signed local TLS may be disabled
- storage root is repository-local, for example `.local/state/`
- one engineer may run all queues on one machine
- a single GPU can still be used, but lower concurrency and smaller benchmark data are expected
- Triton RT and Triton Batch remain separate processes even in development

### 5.2 Development Differences

- lower retention periods for logs and artifacts
- reduced queue concurrency
- smaller checkpoint frequency interval allowed
- smaller model benchmark suite
- optional CPU fallback for developer laptops without an NVIDIA GPU, but only for non-performance tests and not as a substitute for production qualification

## 6. Migration Path: Development to Production

Development and production use the same:

- service boundaries
- APIs
- database schema
- queue names
- artifact manifests
- DVC structure
- MLflow layout
- Triton repository conventions

Migration path:

1. Build and validate on developer WSL2 profile.
2. Run integration and acceptance tests against the `compose.prod` profile on the target workstation.
3. Move storage roots from `.local/state` to `/srv/cv-platform`.
4. Switch from local test certificates to internal CA-issued TLS certificates.
5. Enable Prometheus scraping, backup jobs, retention policies, and production queue limits.
6. Run qualification benchmarks on the target GPU for every supported model family.
7. Mark the workstation profile as production only after deployment, rollback, and recovery drills pass.

## 7. Service Boundaries and Responsibilities

### 7.1 `admin-web`

Responsibilities:

- operator UI shell
- dataset upload workflow
- snapshot creation and monitoring
- training trigger and monitoring
- model evaluation dashboards
- promotion and deployment workflow
- realtime inference test console
- batch inference submission and status
- health and audit screens

Non-responsibilities:

- no direct database access
- no orchestration logic beyond calling APIs
- no direct Triton or MLflow writes

### 7.2 `control-plane-api`

Responsibilities:

- authoritative workflow API
- transactional state machine enforcement
- idempotency key handling
- audit event creation
- queue submission
- read models for UI
- rollout and rollback commands
- service status aggregation

Non-responsibilities:

- no GPU training
- no direct synchronous batch execution
- no realtime inference execution
- no webhook payload business logic inline

### 7.3 `webhook-receiver`

Responsibilities:

- receive Label Studio webhook POSTs
- authenticate source with network restrictions and shared secret header
- persist raw payload and metadata
- deduplicate by source event key
- enqueue processing

Non-responsibilities:

- no export or training inline
- no long-running Label Studio API calls

### 7.4 `training-service` codebase

Responsibilities:

- model family definitions
- dataset adapters
- deterministic config assembly
- checkpoint management
- training loop execution
- evaluation
- ONNX export
- TensorRT engine build
- artifact manifests

Deployed runtime:

- executed by `rq-training-worker`
- optionally exposes a small metrics and health HTTP sidecar if desired

### 7.5 `rq-control-worker`

Responsibilities:

- snapshot export orchestration
- DVC add and push operations
- MLflow model registration updates
- staging and production deployment jobs
- rollback jobs
- periodic reconciliation

### 7.6 `inference-gateway-rt`

Responsibilities:

- synchronous low-latency inference API
- request auth and quota enforcement
- contract validation
- image decode and normalization
- Triton request shaping
- postprocessing to stable API schema
- route selection by deployment target

Non-responsibilities:

- no batch job ownership
- no training-related mutations

### 7.7 `inference-gateway-batch`

Responsibilities:

- internal chunk inference endpoint for batch workers
- optional external asynchronous submission API if required later
- separate concurrency and timeout policy
- large payload handling

Non-responsibilities:

- no low-latency SLA ownership

### 7.8 `triton-rt`

Responsibilities:

- serve loaded realtime models only
- never host batch-only model repos
- explicit model load and unload

### 7.9 `triton-batch`

Responsibilities:

- serve loaded batch models only
- no external browser-facing traffic

### 7.10 `label-studio`

Responsibilities:

- annotation UI
- labeling project and membership administration
- task storage and annotation edits
- webhook emission
- export API

Important boundary:

- Label Studio is not the source of truth for immutable training datasets after a snapshot is created. The platform snapshot is.

### 7.11 `mlflow`

Responsibilities:

- experiment runs
- run metrics
- run parameters
- artifact lineage
- model version mirror

Important boundary:

- MLflow is not the source of truth for deployment state. PostgreSQL is.

### 7.12 Runtime Visual/Object Semantics

The runtime plane treats visual state and object state as first-class execution
data. A live channel must not be modeled as only `latest image plus overlay`.

Runtime pipeline execution carries these typed concepts:

- `image context`: a visual frame state such as the original channel frame,
  resize, crop, ROI image, masked image, or other transformed image.
- `object reference`: a stable per-execution object identity produced by
  detection, segmentation, configured regions, or future tracking.
- `provenance link`: the relationship between image contexts, objects, ROIs,
  nodes, and model outputs.
- `object enrichment`: downstream per-object results such as ROI
  classification labels, segmentation-derived measurements, or scores attached
  back to the source object.
- `display context`: the image context selected for live-view rendering.

Visual-processing nodes must declare whether they consume, preserve, transform,
or produce image contexts. Object-producing nodes must emit object references
with source image context, geometry, label/class, score, producing node, and
provenance. ROI/crop paths must retain links to both the source image context
and source object so downstream results can be merged back to the correct
object.

The default live-view rule is intentionally conservative:

- if the pipeline has one clear visual path, the live view uses the latest image
  context on that path;
- overlays render only on the image context they belong to unless a valid
  projection through context lineage is available;
- if multiple visual branches exist, the pipeline must include a Display output
  node that explicitly selects the live-view display context.

Production livestream publication is also a runtime-plane responsibility. The
admin frontend and control plane may read stream metadata, but they must not
generate, render, or encode the live media stream. `camera-media` publishes
exactly one NVENC H.264 stream per channel and MediaMTX fans it out through
WHEP/WebRTC over TURN/TCP. HLS, MJPEG, snapshot-player, and CPU
encoder fallbacks are not production paths. Stream URLs are LAN-open by default
(`unauthenticated_lan`) unless stream authorization is explicitly enabled.

This model supports future per-object fan-out/fan-in flows such as:

```text
Input Frame -> Detection -> ROI Fan-Out -> Classification per ROI
            -> Merge/Enrich -> Overlay -> Display Output
```

The runtime plane owns these execution semantics. The control plane stores and
validates pipeline definitions; the inference plane remains responsible only
for model execution through deployed-model contracts.

#### 7.12.1 Computer Vision And Board Measurement Workflow

The first production measurement workflow builds on the existing Feature
Detection inference node. Feature Detection remains the only ML inference step
for feature classes such as `knot`, `crack`, `resin_pocket`,
`board_start_edge`, and `board_side_edge`; downstream runtime nodes process
those typed detections and must not introduce a parallel detector.

The platform also supports classical Computer Vision nodes in the runtime
plane. These nodes use deterministic open-source CV algorithms and do not call
the inference plane. `computer_vision.canny_edge_detection` produces
provenance-linked edge masks, optional contours, and debug overlay primitives.
`computer_vision.reference_edge_from_cv_edges` extracts candidate Hough line
segments from those edges and emits the same `ReferenceEdge` contract as
Feature Detection based edge extraction. Downstream coordinate-frame and
measurement nodes must not care whether a `ReferenceEdge` came from AI Feature
Detection or classical CV.

The runtime plane supports both single-path and multi-branch board inspection
chains. AI-only edge extraction remains valid:

```text
Channel Frame Input
-> Feature Detection
-> Reference Edge From Features
-> Coordinate Frame From Two Edges
-> Feature Measurement
-> Clearance Zone Generator
-> First Free Region Finder
-> Pixel To Metric Conversion
-> Overlay Output / Response Output / Persisted Result
```

Parallel CV and AI branches are also valid when the same input frame feeds both
branches and fan-in nodes consume typed outputs from each branch:

```text
Channel Frame Input
+-> Canny Edge Detection
|   -> Reference Edge From CV Edges
|   -> Coordinate Frame From Two Edges
+-> Feature Detection
    -> Feature Measurement

Coordinate Frame + Feature Measurements
-> Clearance Zone Generator
-> First Free Region Finder
-> Pixel To Metric Conversion
-> Overlay Output / Response Output / Persisted Result
```

Contracts added for this chain are `ReferenceEdge`,
`InspectionCoordinateFrame`, `FeatureMeasurement`, `ClearanceZone`,
`FreeRegion`, and `MeasurementDecision`. Classical CV edge output is carried
as typed `cv_edges` runtime data until it is converted into `ReferenceEdge`.
These objects are runtime result data, can be persisted, can feed response
mappings, and can render as context-bound overlays. The current
coordinate-frame node uses two detected edges in image coordinates and does not
perform perspective correction; a future homography node must remain separate.

Operator configuration is structured: feature-class to edge-role mappings,
CV line-selection rules, measurement bases, clearance rule tables, minimum free
length, search direction, and `mm_per_pixel` calibration are edited through the
pipeline designer. Operators never type internal object IDs or manually bind
channels inside the graph.

The design is intentionally future-compatible with multi-camera quality
scanner workflows. Future workpiece identity, multiple views, homography, and
quality-grade aggregation may extend these contracts, but the current
single-image/two-edge board workflow is executable end to end today.

### 7.13 Runtime Channel And HTTP Trigger Semantics

Channels are source abstractions. A channel represents a camera/source
connection, its technical place, display metadata, source configuration,
source health/capabilities, live-view configuration, and optional default live
pipeline binding. A channel is not a trigger endpoint and must not require an
operator to enter internal IDs.

HTTP trigger behavior is pipeline-driven:

- `trigger.http_put` is a pipeline ingress node. It declares that a published
  pipeline can be called through an HTTP endpoint, and owns method, auth,
  timeout, request schema, and endpoint visibility settings.
- `source.channel_frame` is the canonical channel-bound source node. For HTTP
  trigger execution the endpoint's channel supplies one captured frame to this
  node; operators do not select or type a channel inside the pipeline graph.
- `source.channel_snapshot` is legacy compatibility for existing published
  versions and migrations, not the normal operator path.
- `output.http_trigger_response` maps typed pipeline results into the HTTP
  response body.

The externally callable route is derived from a published/deployed pipeline
version containing an HTTP Trigger Input node. Runtime may persist
`trigger_endpoints` metadata for slug, readiness, last run status, auth, and
audit, but that metadata is derived from pipeline deployment state and is not a
separate primary business object. Operators should think: "this pipeline is
triggerable", not "this channel is a trigger" or "I created a separate trigger
object".

One channel can be used by multiple published triggerable pipelines because
trigger endpoints are derived from the channel plus the published pipeline's
HTTP Trigger Input node. The same channel may simultaneously have a live
runtime for visualization, manual snapshot capture, and one or more
pipeline-derived HTTP trigger endpoints. Live runtime lifecycle remains
separate from HTTP trigger execution; a triggerable pipeline does not imply a
permanently running live stream unless its channel/source requires a live frame
fallback.

Endpoint readiness is derived from:

- published/deployed pipeline state;
- the HTTP Trigger Input node configuration;
- auth configuration validity;
- runtime service readiness;
- channel-to-pipeline binding validity and channel frame acquisition capability;
- Response Output reachability and mapping validity.

Industrial PLC writes are modeled as runtime pipeline output nodes. The
operator-facing `Write to Siemens SPS` node consumes typed upstream pipeline
results and writes explicitly mapped values to Siemens S7 DB addresses during
runtime execution. It is not a control-plane connector-only setting, not an
operator CLI workflow, and not an inference-plane concern. Control plane stores
and validates the node configuration, runtime plane performs the side effect,
and inference plane remains limited to model execution.

PLC write nodes are disabled and dry-run by default. A published pipeline that
contains an enabled, non-dry-run PLC write must surface an industrial side
effect warning. Validation and authoring preview never write to PLC hardware.
Runtime writes require valid connection configuration, valid source field
selection, valid type conversion, explicit failure behavior, and an
environment-level write allow flag.

## 8. Service-to-Service Communication

### 8.1 Communication Matrix

| From | To | Protocol | Purpose |
|---|---|---|---|
| Browser | reverse-proxy | HTTPS | Single entry point |
| Browser/admin-web | control-plane-api | HTTPS JSON | All operator workflows |
| Browser/admin-web | inference-gateway-rt | HTTPS JSON | Realtime test console |
| Label Studio | webhook-receiver | HTTPS POST | Event delivery |
| control-plane-api | PostgreSQL | TCP | Operational state |
| control-plane-api | Redis | TCP | Queue enqueue, lightweight locks |
| control-plane-api | Label Studio API | HTTPS JSON | Project and snapshot control |
| control-plane-api | MLflow API | HTTP/HTTPS | Read experiment and registry data |
| webhook-receiver | PostgreSQL | TCP | Raw webhook persistence |
| webhook-receiver | Redis | TCP | Enqueue processing jobs |
| rq-control-worker | Label Studio API | HTTPS JSON | Snapshot export lifecycle |
| rq-control-worker | MLflow API | HTTP/HTTPS | Register and tag model versions |
| rq-control-worker | Triton control API | HTTP/gRPC | Load, unload, probe model readiness |
| rq-training-worker | GPU | CUDA | Training and export |
| rq-training-worker | MLflow API | HTTP/HTTPS | Run logging |
| rq-training-worker | DVC repo | filesystem/CLI | Versioned artifact tracking |
| rq-batch-worker | inference-gateway-batch | HTTP | Chunk inference |
| inference-gateway-rt | triton-rt | gRPC | Realtime predictions |
| inference-gateway-batch | triton-batch | gRPC | Batch chunk predictions |
| runtime-admin-api | PostgreSQL | TCP | Channels, pipeline versions, endpoint metadata, runtime status |
| runtime-admin-api | Redis | TCP | Runtime-control command enqueue |
| runtime-orchestrator | PostgreSQL | TCP | Runtime command claim, execution state, reconciliation |
| runtime-orchestrator | realtime-executor | in-process/supervised worker | Launch and supervise live channel runtimes |
| trigger-executor | PostgreSQL | TCP | Resolve pipeline-derived HTTP trigger endpoint metadata |
| trigger-executor | inference-gateway-rt | HTTP | Trigger pipeline inference calls |

### 8.2 Control Plane vs Data Plane

Control plane calls never sit in the low-latency path for production inference.

- `control-plane-api` owns deployment selection
- `inference-gateway-*` cache active deployment mappings in memory with short TTL refresh from PostgreSQL
- a production prediction never requires an MLflow, DVC, or Label Studio lookup

## 9. Queueing Policy: Exactly What Uses Redis/RQ

### 9.1 Requests That Bypass Queueing

These requests are synchronous because they must return immediately or are short metadata operations:

- `GET /api/v1/*` read APIs
- `POST /api/v1/datasets`
- `POST /api/v1/label-projects`
- upload session creation and part upload
- `POST /api/v1/training-jobs` only through `accepted` state write and queue submission
- `POST /api/v1/deployments/*` only through `accepted` state write and queue submission
- `POST /api/v1/batch-jobs` only through `accepted` state write and queue submission
- realtime inference requests to `inference-gateway-rt`
- all `health` and `metrics` endpoints
- Label Studio webhook receive and persist acknowledgment

### 9.2 Jobs That Must Use Redis/RQ

Queue `webhook`:

- `process_labelstudio_event`
- `reconcile_project_counters`

Queue `dataset`:

- `finalize_upload_session`
- `sync_uploaded_assets_to_labelstudio`
- `create_labelstudio_snapshot_export`
- `poll_labelstudio_snapshot_export`
- `materialize_dataset_snapshot`
- `validate_dataset_snapshot`
- `dvc_track_dataset_snapshot`

Queue `training`:

- `prepare_training_workspace`
- `run_training_job`
- `resume_training_job`
- `cancel_training_job`
- `evaluate_training_run`
- `export_onnx`
- `build_tensorrt_engine`
- `register_model_version`

Queue `deploy`:

- `stage_model_release`
- `promote_model_release`
- `rollback_model_release`
- `retire_deployment_release`

Queue `batch`:

- `materialize_batch_input`
- `split_batch_into_chunks`
- `run_batch_chunk`
- `aggregate_batch_results`
- `publish_batch_outputs`

Queue `maintenance`:

- `collect_service_health`
- `reconcile_abandoned_jobs`
- `checkpoint_pruning`
- `artifact_retention`
- `backup_metadata`

### 9.3 Queue Isolation Rules

- `rq-training-worker` listens only to `training`
- `rq-batch-worker` listens only to `batch`
- `rq-control-worker` listens only to `webhook,dataset,deploy,maintenance`
- batch workers never consume from realtime queues
- training worker concurrency is `1` on the workstation unless a second GPU is added

## 10. Promotion Model

### 10.1 Lifecycle Names

Platform lifecycle names visible to operators:

- `trained`
- `staged`
- `production`

Internal implementation states are more granular, but the UI uses these three operator states.

### 10.2 Meaning

- `trained`
  - training completed
  - evaluation artifacts exist
  - ONNX export exists
  - TensorRT engine exists for the target GPU profile
  - model version is immutable and registered
- `staged`
  - model package successfully loaded into the staging deployment target
  - smoke tests and compatibility checks passed
  - available for operator testing in the admin UI
- `production`
  - model is the active release for a deployment target
  - traffic is routed to it by the gateway

### 10.3 Important Detail

Promotion state is tracked per deployment target:

- realtime
- batch

A model version may be:

- `production` for batch
- `staged` for realtime

at the same time.

## 11. Immutable Deployment and Rollback Model

### 11.1 Artifact Separation

Training artifacts and serving artifacts are stored in separate roots.

- Training root: `/srv/cv-platform/training/...`
- Serving package root: `/srv/cv-platform/serving/packages/...`
- Triton repositories: `/srv/cv-platform/serving/triton/realtime/...` and `/srv/cv-platform/serving/triton/batch/...`

Training workers never write directly to Triton repositories.

### 11.2 Deployment Release Unit

The immutable deployment unit is `deployment_release`.

Each release contains:

- `deployment_release_id`
- target path: `realtime` or `batch`
- model family
- model version
- Triton model name
- Triton numeric version
- exact artifact manifest with checksums
- gateway contract version
- benchmark and smoke test results

### 11.3 Triton Naming Convention

Stable Triton model names are used per contract-major and target path.

Examples:

- `rt__widget_inspection_cls_v1`
- `rt__defect_detection_det_v1`
- `bt__widget_inspection_cls_v1`
- `bt__defect_detection_det_v1`

Within each stable model name, Triton version directories are numeric and immutable:

- `1`
- `2`
- `3`

The gateway always sends the exact `model_version` when calling Triton. It never relies on Triton serving the latest version implicitly.

### 11.4 Production Promotion

Production promotion works like this:

1. Operator selects a `trained` model version and chooses target path `realtime`, `batch`, or both.
2. `control-plane-api` writes a `deployment_release` in `requested` state and enqueues a deploy job.
3. Deploy worker copies the immutable serving package into a temporary staging area under the target Triton repository.
4. Worker atomically renames the prepared version directory into place.
5. Worker calls Triton explicit load for that model version.
6. Worker performs smoke inference through the correct gateway path.
7. If checks pass, PostgreSQL active deployment pointer is switched transactionally to the new release.
8. Gateways refresh their cached active pointer.
9. Old release remains loaded for a configurable rollback window.

### 11.5 Rollback

Rollback does not rebuild anything.

Rollback works like this:

1. Operator clicks rollback on the active deployment target.
2. `control-plane-api` identifies the previous healthy release for that target.
3. API writes a `rollback_requested` transition and enqueues a rollback job.
4. Rollback job verifies the previous release is still present in the Triton repository.
5. If not loaded, it explicitly loads that version.
6. Gateway active deployment pointer is switched back to the previous release.
7. Failed release remains preserved for forensics until explicit retirement.

Rollback is therefore:

- versioned
- immutable
- auditable
- fast

## 12. Monorepo Structure

### 12.1 Exact File and Folder Tree

```text
.
├── README.md
├── docs/
│   └── architecture/
│       └── cv-platform-spec.md
├── contracts/
│   ├── openapi/
│   │   ├── control-plane.v1.yaml
│   │   ├── inference-realtime.v1.yaml
│   │   ├── inference-batch.v1.yaml
│   │   └── webhook-receiver.v1.yaml
│   └── asyncapi/
│       └── platform-events.v1.yaml
├── apps/
│   └── admin-web/
│       ├── package.json
│       ├── tsconfig.json
│       ├── vite.config.ts
│       ├── src/
│       │   ├── app/
│       │   ├── pages/
│       │   ├── features/
│       │   │   ├── datasets/
│       │   │   ├── labeling/
│       │   │   ├── snapshots/
│       │   │   ├── training/
│       │   │   ├── models/
│       │   │   ├── deployments/
│       │   │   ├── inference/
│       │   │   ├── batch/
│       │   │   └── health/
│       │   ├── components/
│       │   ├── hooks/
│       │   └── lib/
│       └── public/
├── services/
│   ├── control-plane-api/
│   │   ├── pyproject.toml
│   │   ├── alembic.ini
│   │   ├── alembic/
│   │   │   └── versions/
│   │   └── src/control_plane_api/
│   │       ├── main.py
│   │       ├── config.py
│   │       ├── api/
│   │       │   └── v1/
│   │       ├── application/
│   │       ├── domain/
│   │       ├── persistence/
│   │       ├── services/
│   │       ├── integrations/
│   │       └── observability/
│   ├── labelstudio-webhook/
│   │   ├── pyproject.toml
│   │   └── src/labelstudio_webhook/
│   │       ├── main.py
│   │       ├── api.py
│   │       ├── parser.py
│   │       ├── dedupe.py
│   │       └── persistence.py
│   ├── inference-gateway/
│   │   ├── pyproject.toml
│   │   └── src/inference_gateway/
│   │       ├── main.py
│   │       ├── config.py
│   │       ├── api/
│   │       ├── preprocessing/
│   │       ├── postprocessing/
│   │       ├── contracts/
│   │       ├── routing/
│   │       ├── triton_client/
│   │       └── auth/
│   └── training-service/
│       ├── pyproject.toml
│       └── src/training_service/
│           ├── config.py
│           ├── registry/
│           ├── datasets/
│           ├── tasks/
│           │   ├── classification/
│           │   └── detection/
│           ├── trainers/
│           ├── evaluators/
│           ├── exporters/
│           ├── tensorrt/
│           └── checkpoints/
├── workers/
│   ├── control-worker/
│   │   ├── pyproject.toml
│   │   └── src/control_worker/
│   │       ├── worker.py
│   │       ├── jobs/
│   │       │   ├── dataset.py
│   │       │   ├── deployment.py
│   │       │   ├── model_registry.py
│   │       │   ├── webhook.py
│   │       │   └── maintenance.py
│   │       └── middleware/
│   ├── training-worker/
│   │   ├── pyproject.toml
│   │   └── src/training_worker/
│   │       ├── worker.py
│   │       ├── jobs.py
│   │       ├── gpu_guard.py
│   │       └── heartbeat.py
│   ├── batch-worker/
│   │   ├── pyproject.toml
│   │   └── src/batch_worker/
│   │       ├── worker.py
│   │       ├── jobs.py
│   │       ├── chunking.py
│   │       └── aggregation.py
│   └── maintenance-worker/
│       ├── pyproject.toml
│       └── src/maintenance_worker/
│           ├── worker.py
│           ├── jobs.py
│           └── schedulers.py
├── packages/
│   ├── py/
│   │   ├── cv_core/
│   │   ├── cv_contracts/
│   │   ├── cv_db/
│   │   ├── cv_events/
│   │   ├── cv_audit/
│   │   ├── cv_observability/
│   │   ├── cv_artifacts/
│   │   ├── cv_datasets/
│   │   ├── cv_ml/
│   │   └── cv_inference/
│   └── ts/
│       ├── api-client/
│       ├── schemas/
│       └── ui-kit/
├── ml/
│   ├── configs/
│   │   ├── task-types/
│   │   │   ├── classification/
│   │   │   └── detection/
│   │   ├── optimization/
│   │   └── benchmarks/
│   ├── manifests/
│   ├── label-maps/
│   ├── metric-definitions/
│   └── compatibility-matrix/
├── config/
│   ├── base/
│   ├── dev/
│   ├── prod/
│   └── schemas/
├── infra/
│   ├── compose/
│   │   ├── compose.dev.yaml
│   │   └── compose.prod.yaml
│   ├── reverse-proxy/
│   ├── postgres/
│   ├── redis/
│   ├── labelstudio/
│   ├── mlflow/
│   ├── triton/
│   │   ├── realtime/
│   │   └── batch/
│   ├── observability/
│   ├── wsl/
│   └── systemd/
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── e2e/
│   ├── performance/
│   └── acceptance/
└── scripts/
    ├── dev/
    ├── release/
    ├── backup/
    ├── benchmark/
    └── migrations/
```

### 12.2 Why This Structure

- `apps/` is operator-facing UI only
- `services/` contains HTTP-facing backend services and shared service codebases
- `workers/` contains RQ runtime entrypoints with isolated write scopes
- `packages/py` and `packages/ts` hold reusable typed libraries
- `contracts/` stores exported API contracts for review and code generation
- `ml/` holds model-family configs and compatibility manifests, not runtime artifacts
- `infra/` owns deployment manifests and service runtime configuration

## 13. Module List

### 13.1 Python Shared Modules

- `cv_core`
  - ULID helpers, idempotency helpers, config primitives, enums
- `cv_contracts`
  - Pydantic request and response models
- `cv_db`
  - SQLAlchemy models, repositories, transaction helpers
- `cv_events`
  - event envelope and event type definitions
- `cv_audit`
  - append-only audit writers and readers
- `cv_observability`
  - logging, metrics, tracing context propagation
- `cv_artifacts`
  - manifest generation, checksum validation, atomic move helpers
- `cv_datasets`
  - snapshot manifests, dataset adapters, label conversions
- `cv_ml`
  - training config resolution, task abstractions, evaluation primitives
- `cv_inference`
  - gateway contracts, preprocessing, postprocessing, Triton request mapping

### 13.2 Frontend Modules

- `features/datasets`
- `features/labeling`
- `features/snapshots`
- `features/training`
- `features/models`
- `features/deployments`
- `features/inference`
- `features/batch`
- `features/health`

## 14. Naming Conventions and Identifiers

### 14.1 Entity IDs

Use string ULIDs with stable prefixes:

- `usr_` user
- `dts_` dataset
- `upl_` upload session
- `lpr_` labeling project
- `dss_` dataset snapshot
- `trn_` training job
- `chk_` checkpoint
- `mfm_` model family
- `mvr_` model version
- `dpr_` deployment release
- `bji_` batch inference job
- `evt_` audit or domain event

### 14.2 Slugs

Human-readable slugs are lowercase kebab case:

- `widget-inspection-goodbad`
- `surface-defect-detection`

### 14.3 Model Family Names

Convention:

`<use-case-slug>_<task>_v<contract-major>`

Examples:

- `widget_inspection_cls_v1`
- `surface_defect_det_v1`
- `surface_anomaly_ad_v1`

### 14.4 Supported Task Types

The platform treats `anomaly` as a first-class model task. It is distinct from
`classification`, `detection`, and `segmentation` in contracts, model
templates, training jobs, deployment records, runtime model capability cache,
and runtime node selection.

Current production-supported task families are:

| Task | Operator category | Current backend path |
| --- | --- | --- |
| classification | Classification | Torchvision or Ultralytics, real checkpoint and ONNX/Triton deployment |
| detection | Detection | OpenMMLab/MMYOLO or Ultralytics, real checkpoint and ONNX/TensorRT deployment |
| segmentation | Segmentation | Torchvision or Ultralytics, real checkpoint and deployment |
| anomaly | Anomaly | Anomalib PatchCore, image-level good/bad dataset, real checkpoint, ONNX/OpenVINO runtime inference, score, thresholded decision, heatmap overlay |

Anomaly support must never be implemented as a disguised binary classifier. A
ResNet-style network can be used as a backbone inside an anomaly method, but
operators must see and select an anomaly model, and runtime outputs must remain
anomaly-specific.

Supported anomaly method matrix:

| Method | Backend | Dataset semantics | Export/deployment | Runtime output | Exposed |
| --- | --- | --- | --- | --- | --- |
| PatchCore | Anomalib | image-level `normal` / `anomalous` from Label Studio good/bad labels | real checkpoint plus ONNX/OpenVINO deployment package | anomaly score, `normal`/`anomalous` decision, heatmap | yes |
| PaDiM | Anomalib candidate | not wired end to end | not deployed | not inferable | no |
| EfficientAD | Anomalib candidate | not wired end to end | not deployed | not inferable | no |

Only methods in the `Exposed = yes` row may appear in model templates,
catalogs, frontend selectors, or pipeline node model pickers.

### 14.5 Artifact Names

Artifact file names are deterministic:

`<entity-id>__<family-slug>__<kind>__sha256_<first16>.ext`

Examples:

- `mvr_01J...__widget_inspection_cls_v1__onnx__sha256_ab12cd34ef56aa90.onnx`
- `mvr_01J...__surface_defect_det_v1__trt__sha256_9911aa22bb33cc44.plan`

## 15. Versioning Strategy

### 15.1 API Versioning

- REST endpoints are versioned as `/api/v1/...`
- inference gateways expose `/v1/...`
- breaking API changes create `/api/v2/...`

### 15.2 Event Versioning

Event type names are explicit and versioned:

- `dataset.snapshot.created.v1`
- `training.job.state_changed.v1`
- `model.version.registered.v1`
- `deployment.release.activated.v1`
- `batch.job.completed.v1`

### 15.3 Model Contract Versioning

Contract-major is embedded in model family name.

If request or response schema changes incompatibly:

- create a new model family major version
- create a new Triton model name
- keep old deployed versions intact until consumers migrate

### 15.4 Dataset Snapshot Versioning

Each dataset has a monotonically increasing snapshot number plus a content hash.

Example:

- snapshot id: `dss_01J...`
- snapshot number: `42`
- content digest: `sha256:...`

### 15.5 Deployment Versioning

Each stable Triton model name receives immutable numeric versions:

- `1`
- `2`
- `3`

Gateway route selection points to a specific `deployment_release`, not just a model version number.

## 16. API Boundaries

### 16.1 Control Plane API

Representative endpoints:

- `POST /api/v1/datasets`
- `POST /api/v1/datasets/{dataset_id}/upload-sessions`
- `PUT /api/v1/upload-sessions/{upload_session_id}/parts/{part_no}`
- `POST /api/v1/upload-sessions/{upload_session_id}:complete`
- `POST /api/v1/label-projects`
- `POST /api/v1/dataset-snapshots`
- `GET /api/v1/dataset-snapshots/{snapshot_id}`
- `POST /api/v1/training-jobs`
- `POST /api/v1/training-jobs/{training_job_id}:pause`
- `POST /api/v1/training-jobs/{training_job_id}:resume`
- `GET /api/v1/training-jobs/{training_job_id}`
- `POST /api/v1/model-versions/{model_version_id}:stage`
- `POST /api/v1/deployments/{target_path}:promote`
- `POST /api/v1/deployments/{target_path}:rollback`
- `POST /api/v1/batch-jobs`
- `GET /api/v1/batch-jobs/{batch_job_id}`
- `GET /api/v1/system/status`
- `GET /api/v1/audit-events`

Contract rules:

- every mutating request accepts `Idempotency-Key`
- every response includes `request_id`
- long-running actions return `202 Accepted` with `operation_id`

### 16.2 Realtime Inference API

Representative endpoints:

- `POST /v1/models/{model_family}/predict`
- `GET /v1/models/{model_family}/metadata`
- `GET /health/live`
- `GET /health/ready`

Realtime request rules:

- single image or bounded small batch only
- strict size limits
- short timeout budget
- no queueing

### 16.3 Batch Inference API

The operator-facing entry is still the control plane:

- `POST /api/v1/batch-jobs`
- `GET /api/v1/batch-jobs/{batch_job_id}`

Internal batch gateway endpoints:

- `POST /internal/v1/chunks:infer`
- `GET /internal/health/ready`

### 16.4 Webhook Receiver API

- `POST /webhooks/label-studio`
- `GET /health/live`
- `GET /health/ready`

Rules:

- persist first
- acknowledge fast
- no downstream blocking calls before response

## 17. Event Flows

### 17.1 Dataset Upload to Labeling

1. Operator creates dataset in admin UI.
2. Browser opens upload session through `control-plane-api`.
3. Browser uploads image chunks to upload endpoints.
4. Browser completes upload session.
5. API stores `upload_session=uploaded` and enqueues `finalize_upload_session`.
6. Dataset worker validates checksums, extracts image metadata, writes dataset asset records.
7. Operator creates or links a Label Studio project.
8. Control worker imports tasks into Label Studio through API.
9. Label Studio webhook events update project counters asynchronously.

### 17.2 Dataset Snapshot Flow

1. Operator clicks `Create Snapshot`.
2. API writes `dataset_snapshot=requested` and enqueues export job.
3. Worker creates Label Studio snapshot export through API.
4. Worker polls export status.
5. Worker downloads export archive and stores it under `labelstudio/exports/...`.
6. Worker materializes immutable snapshot directory with images, annotations, manifest, and label map.
7. Worker validates schema, file counts, checksums, and task compatibility.
8. Worker writes DVC tracking metadata and marks snapshot `ready`.

### 17.3 Training Flow

1. Operator selects snapshot and training recipe and clicks `Train`.
2. API writes `training_job=requested`, persists config snapshot, and enqueues job.
3. Training worker acquires the training slot and prepares workspace.
4. Training starts and logs to MLflow.
5. Worker checkpoints on schedule and on pause or pressure events.
6. On success, worker runs evaluation, ONNX export, TensorRT engine build, and artifact manifest generation.
7. Worker registers model version metadata in PostgreSQL, MLflow, and DVC.
8. Model version becomes `trained`.

### 17.4 Promotion and Deployment Flow

1. Operator selects a trained model version and chooses `Stage`.
2. API writes promotion request and enqueues `stage_model_release`.
3. Worker builds serving package from immutable artifacts.
4. Worker copies the package into the target staging Triton repository version.
5. Worker explicitly loads the model version in Triton.
6. Worker runs smoke tests through the appropriate gateway.
7. If successful, promotion state becomes `staged`.
8. Operator runs manual or saved test sets in admin UI.
9. Operator clicks `Promote to Production`.
10. API writes deployment request and enqueues `promote_model_release`.
11. Worker switches the active deployment pointer atomically.
12. Gateway refresh causes live traffic to move to the new release.

### 17.5 Batch Inference Flow

1. Operator submits batch job from admin UI.
2. API writes `batch_job=submitted` and enqueues job.
3. Batch worker materializes inputs and partitions them into chunks.
4. Each chunk job calls `inference-gateway-batch`.
5. Gateway submits chunk requests to `triton-batch`.
6. Chunk results are stored as immutable part files.
7. Aggregation job produces final output manifest and bundle.
8. Batch job is marked `completed` or `completed_with_errors`.

## 18. State Machines

### 18.1 Dataset Snapshot Lifecycle

States:

- `requested`
- `export_pending`
- `exporting`
- `download_pending`
- `materializing`
- `validating`
- `versioning`
- `ready`
- `failed_retryable`
- `failed_terminal`
- `superseded`

Transitions:

- `requested -> export_pending`
- `export_pending -> exporting`
- `exporting -> download_pending`
- `download_pending -> materializing`
- `materializing -> validating`
- `validating -> versioning`
- `versioning -> ready`
- any active state -> `failed_retryable` on transient API or IO failure
- `failed_retryable -> export_pending` on retry
- any active state -> `failed_terminal` on schema mismatch or missing required assets
- `ready -> superseded` when a newer approved snapshot replaces it operationally

### 18.2 Training Job Lifecycle

States:

- `requested`
- `queued`
- `waiting_for_gpu`
- `preparing`
- `running`
- `checkpointing`
- `paused_operator`
- `paused_slo`
- `resuming`
- `evaluating`
- `exporting_onnx`
- `building_tensorrt`
- `registering_model`
- `completed`
- `cancel_requested`
- `cancelled`
- `failed_retryable`
- `failed_terminal`

Transitions:

- `requested -> queued`
- `queued -> waiting_for_gpu`
- `waiting_for_gpu -> preparing`
- `preparing -> running`
- `running -> checkpointing -> running`
- `running -> paused_operator`
- `running -> paused_slo`
- `paused_* -> resuming -> running`
- `running -> evaluating`
- `evaluating -> exporting_onnx`
- `exporting_onnx -> building_tensorrt`
- `building_tensorrt -> registering_model`
- `registering_model -> completed`
- any active state -> `cancel_requested -> cancelled`
- transient failure -> `failed_retryable`
- non-recoverable failure -> `failed_terminal`

### 18.3 Model Registration Lifecycle

States:

- `pending_registration`
- `trained`
- `staging_requested`
- `staged`
- `production_requested`
- `production`
- `rejected`
- `retired`

Transitions:

- `pending_registration -> trained`
- `trained -> staging_requested`
- `staging_requested -> staged`
- `staged -> production_requested`
- `production_requested -> production`
- `trained or staged -> rejected`
- `production -> retired`

Implementation detail:

- per-target promotion records carry the true route-specific state
- UI collapses that detail to a clear operator-facing label

### 18.4 Deployment Lifecycle

States:

- `requested`
- `packaging`
- `repository_sync`
- `loading`
- `smoke_testing`
- `activated`
- `activation_failed`
- `rollback_requested`
- `rolled_back`
- `retired`

Transitions:

- `requested -> packaging`
- `packaging -> repository_sync`
- `repository_sync -> loading`
- `loading -> smoke_testing`
- `smoke_testing -> activated`
- `smoke_testing -> activation_failed`
- `activated -> rollback_requested -> rolled_back`
- `activated or rolled_back -> retired`

### 18.5 Batch Inference Job Lifecycle

States:

- `submitted`
- `queued`
- `materializing_input`
- `chunking`
- `running_chunks`
- `aggregating`
- `publishing`
- `completed`
- `completed_with_errors`
- `cancel_requested`
- `cancelled`
- `failed_retryable`
- `failed_terminal`

Transitions:

- `submitted -> queued`
- `queued -> materializing_input`
- `materializing_input -> chunking`
- `chunking -> running_chunks`
- `running_chunks -> aggregating`
- `aggregating -> publishing`
- `publishing -> completed`
- `publishing -> completed_with_errors`
- active -> `cancel_requested -> cancelled`
- transient failure -> `failed_retryable`
- permanent input or contract failure -> `failed_terminal`

## 19. Storage Layout

All persistent runtime data sits under:

`/srv/cv-platform`

### 19.1 Exact Storage Layout

```text
/srv/cv-platform
├── control
│   ├── uploads
│   │   └── dts_<dataset_id>/
│   │       └── upl_<upload_session_id>/
│   │           ├── parts/
│   │           ├── assembled/
│   │           └── manifests/
│   ├── labelstudio
│   │   ├── imports/
│   │   └── exports/
│   │       └── lpr_<label_project_id>/
│   │           └── dss_<snapshot_id>/
│   ├── snapshots
│   │   └── dss_<snapshot_id>/
│   │       ├── images/
│   │       ├── annotations/
│   │       ├── manifests/
│   │       ├── label_map.json
│   │       └── snapshot_manifest.json
│   ├── dvc
│   │   ├── repo/
│   │   ├── cache/
│   │   └── remote/
│   └── mlflow
│       ├── backend/
│       └── artifacts/
├── training
│   └── trn_<training_job_id>/
│       ├── workspace/
│       ├── configs/
│       ├── logs/
│       ├── outputs/
│       ├── checkpoints/
│       ├── evaluation/
│       ├── onnx/
│       └── tensorrt/
├── serving
│   ├── packages
│   │   └── mvr_<model_version_id>/
│   │       ├── manifest.json
│   │       ├── realtime/
│   │       └── batch/
│   └── triton
│       ├── realtime/
│       │   └── rt__<family_name>/
│       │       ├── config.pbtxt
│       │       ├── 1/
│       │       ├── 2/
│       │       └── ...
│       └── batch/
│           └── bt__<family_name>/
│               ├── config.pbtxt
│               ├── 1/
│               ├── 2/
│               └── ...
├── batch
│   └── bji_<batch_job_id>/
│       ├── input/
│       ├── chunks/
│       ├── partial_results/
│       ├── final/
│       └── manifests/
├── logs
│   ├── services/
│   ├── workers/
│   └── reverse-proxy/
└── audit
    ├── events/
    └── exports/
```

### 19.2 Required Asset Locations

- uploaded images
  - `/srv/cv-platform/control/uploads/.../assembled/`
- Label Studio exports
  - `/srv/cv-platform/control/labelstudio/exports/...`
- dataset snapshots
  - `/srv/cv-platform/control/snapshots/dss_<snapshot_id>/`
- training outputs
  - `/srv/cv-platform/training/trn_<training_job_id>/outputs/`
- checkpoints
  - `/srv/cv-platform/training/trn_<training_job_id>/checkpoints/`
- ONNX exports
  - `/srv/cv-platform/training/trn_<training_job_id>/onnx/`
  - copied immutable serving copy under `/srv/cv-platform/serving/packages/...`
- TensorRT engines
  - `/srv/cv-platform/training/trn_<training_job_id>/tensorrt/`
  - copied immutable serving copy under `/srv/cv-platform/serving/packages/...`
- Triton model repository
  - `/srv/cv-platform/serving/triton/realtime/`
  - `/srv/cv-platform/serving/triton/batch/`
- logs and audit records
  - `/srv/cv-platform/logs/...`
  - `/srv/cv-platform/audit/...`

## 20. Persistence Model

### 20.1 Database Ownership

`control-plane-api` owns all PostgreSQL schema migrations.

`label-studio` and `mlflow` may use the same PostgreSQL instance but must use separate databases and separate credentials.

Recommended databases:

- `cv_platform`
- `labelstudio`
- `mlflow`

### 20.2 Database Schema Outline

#### `users`

- `id`
- `username`
- `password_hash`
- `role`
- `is_active`
- `created_at`

#### `api_keys`

- `id`
- `subject_type`
- `subject_id`
- `scope`
- `hashed_secret`
- `expires_at`

#### `datasets`

- `id`
- `slug`
- `name`
- `task_type`
- `label_schema_version`
- `status`
- `created_by`
- `created_at`

#### `upload_sessions`

- `id`
- `dataset_id`
- `status`
- `expected_file_count`
- `received_file_count`
- `total_bytes`
- `content_manifest_sha256`
- `created_at`
- `completed_at`

#### `dataset_assets`

- `id`
- `dataset_id`
- `upload_session_id`
- `relative_path`
- `sha256`
- `size_bytes`
- `mime_type`
- `width`
- `height`
- `created_at`

#### `label_projects`

- `id`
- `dataset_id`
- `label_studio_project_id`
- `title`
- `task_type`
- `label_config`
- `status`
- `created_at`

#### `label_project_webhooks`

- `id`
- `label_project_id`
- `secret_ref`
- `target_url`
- `enabled`

#### `webhook_events`

- `id`
- `source`
- `source_event_key`
- `action`
- `payload`
- `received_at`
- `processed_at`
- `processing_status`

#### `dataset_snapshots`

- `id`
- `dataset_id`
- `label_project_id`
- `snapshot_no`
- `status`
- `item_count`
- `annotation_count`
- `manifest_uri`
- `content_sha256`
- `dvc_rev`
- `created_by`
- `created_at`

#### `training_jobs`

- `id`
- `model_family_id`
- `dataset_snapshot_id`
- `status`
- `queue_name`
- `requested_by`
- `recipe_name`
- `recipe_version`
- `config_snapshot`
- `mlflow_run_id`
- `latest_checkpoint_id`
- `retry_count`
- `started_at`
- `finished_at`

#### `training_checkpoints`

- `id`
- `training_job_id`
- `step_no`
- `epoch_no`
- `metric_primary`
- `storage_uri`
- `sha256`
- `resume_safe`
- `created_at`

#### `model_families`

- `id`
- `slug`
- `task_type`
- `contract_major`
- `input_schema_ref`
- `output_schema_ref`
- `active`

#### `model_versions`

- `id`
- `model_family_id`
- `version_no`
- `training_job_id`
- `status`
- `metrics_summary`
- `artifact_manifest_uri`
- `onnx_uri`
- `tensorrt_uri`
- `dvc_rev`
- `mlflow_registered_model`
- `mlflow_model_version`
- `created_at`

#### `model_promotions`

- `id`
- `model_version_id`
- `target_path`
- `requested_environment`
- `status`
- `requested_by`
- `validated_at`
- `deployment_release_id`

#### `deployment_releases`

- `id`
- `target_path`
- `model_family_id`
- `model_version_id`
- `triton_model_name`
- `triton_model_version`
- `status`
- `artifact_manifest_uri`
- `smoke_report_uri`
- `activated_at`
- `retired_at`

#### `deployment_targets`

- `target_path`
- `active_release_id`
- `previous_release_id`
- `updated_at`

#### `batch_jobs`

- `id`
- `deployment_release_id`
- `status`
- `input_manifest_uri`
- `output_manifest_uri`
- `total_items`
- `completed_items`
- `failed_items`
- `submitted_by`
- `started_at`
- `finished_at`

#### `batch_job_chunks`

- `id`
- `batch_job_id`
- `chunk_no`
- `status`
- `retry_count`
- `input_uri`
- `output_uri`
- `started_at`
- `finished_at`

#### `audit_events`

- `id`
- `event_type`
- `aggregate_type`
- `aggregate_id`
- `sequence_no`
- `actor_type`
- `actor_id`
- `request_id`
- `correlation_id`
- `causation_id`
- `payload_schema_version`
- `payload`
- `created_at`

#### `idempotency_keys`

- `id`
- `scope`
- `key`
- `request_hash`
- `response_code`
- `response_body`
- `expires_at`

#### `service_heartbeats`

- `service_name`
- `instance_id`
- `status`
- `observed_at`
- `details`

## 21. Audit and Event Schema Outline

Every domain event uses the same envelope:

```json
{
  "event_id": "evt_01J...",
  "event_type": "training.job.state_changed.v1",
  "aggregate_type": "training_job",
  "aggregate_id": "trn_01J...",
  "sequence_no": 14,
  "source_service": "training-worker",
  "actor_type": "user",
  "actor_id": "usr_01J...",
  "request_id": "req_01J...",
  "correlation_id": "cor_01J...",
  "causation_id": "cmd_01J...",
  "payload_schema_version": 1,
  "occurred_at": "2026-04-20T10:15:42Z",
  "payload": {}
}
```

Rules:

- append-only
- immutable after insert
- monotonically increasing `sequence_no` per aggregate
- emitted on every state transition and every operator command with side effects
- also written to JSONL export files under `/srv/cv-platform/audit/events/`

## 22. Config Strategy

### 22.1 Principles

- fail fast on invalid config
- environment-specific values separated from code
- secrets never committed
- config is typed and validated with Pydantic v2

### 22.2 Structure

- `config/base/`
  - shared defaults
- `config/dev/`
  - development overrides
- `config/prod/`
  - production overrides
- `config/schemas/`
  - JSON schema exports for validation and UI introspection

### 22.3 Resolution Order

1. compiled defaults in code
2. base YAML
3. environment YAML
4. mounted secret files
5. environment variables

### 22.4 Required Config Domains

- database
- redis
- storage roots
- Label Studio API
- MLflow API
- DVC repo and remote
- training recipes
- GPU coexistence guardrails
- Triton connection settings
- realtime and batch inference limits
- auth and API key settings
- TLS paths
- log retention

## 23. Environment Strategy

### 23.1 Development

- `ENV=dev`
- repository-local storage
- debug logging enabled
- optional self-signed TLS
- smaller retention windows

### 23.2 Production

- `ENV=prod`
- storage under `/srv/cv-platform`
- TLS required
- JSON logs only
- Prometheus scrape enabled
- backup jobs enabled
- stricter auth, quotas, and retention

### 23.3 Secrets

Use mounted secret files, not plain `.env` files, for:

- database passwords
- API signing secrets
- Label Studio API token
- MLflow admin credentials
- internal service tokens

## 24. Logging, Metrics, and Health

### 24.1 Logging

All backend logs are structured JSON.

Required fields:

- `timestamp`
- `level`
- `service`
- `instance_id`
- `env`
- `request_id`
- `correlation_id`
- `job_id`
- `entity_type`
- `entity_id`
- `message`
- `exception_type`
- `exception_message`

Audit logs are not normal operational logs:

- operational logs go to `/srv/cv-platform/logs/...`
- audit events go to PostgreSQL and `/srv/cv-platform/audit/...`

### 24.2 Metrics

Expose Prometheus metrics for:

- HTTP latency and error rates
- queue depth and oldest-job age
- training epoch and checkpoint age
- batch chunk throughput
- deployment load and smoke-test outcomes
- GPU utilization and memory pressure
- Triton inference latency and request count
- database pool health

Recommended metrics stack:

- Prometheus
- Grafana
- NVIDIA DCGM exporter
- Postgres exporter
- Redis exporter

### 24.3 Health Endpoint Strategy

Every Python HTTP service exposes:

- `GET /health/live`
- `GET /health/ready`
- `GET /health/startup`
- `GET /metrics`

Worker processes expose health via:

- heartbeat rows in PostgreSQL
- Prometheus exporter or sidecar endpoint

Triton uses built-in health endpoints.

Admin UI does not query every service directly. Instead:

- `control-plane-api /api/v1/system/status` aggregates health
- UI renders one consistent status dashboard

## 25. Failure Recovery Design

### 25.1 Core Principles

- all long-running jobs are at-least-once and therefore idempotent
- every critical side effect uses durable state plus retries
- every mutable file operation uses temp paths plus atomic rename
- every restart path is resume-aware

### 25.2 Idempotency

Use `Idempotency-Key` on all mutating operator requests.

Workers enforce idempotency by:

- checking current entity state before doing work
- using unique constraints on event keys and release versions
- writing manifests before completion
- treating already-completed steps as success

### 25.3 Webhook Recovery

- raw event is stored before any business logic
- duplicate source events are ignored using `source_event_key`
- failed processing remains retryable because the raw payload is durable

### 25.4 Snapshot Recovery

- export polling can resume from stored export identifiers
- download can restart if checksum missing or mismatched
- materialization uses a temp directory and atomic rename into final snapshot root

### 25.5 Training Recovery

- checkpoints written on interval, on operator pause, and on SLO pause
- checkpoint manifest includes trainer config digest and optimizer state checksum
- if worker dies, next retry inspects latest resume-safe checkpoint and resumes
- if no safe checkpoint exists, job restarts from initial state but keeps the same `training_job_id` and audit trail

### 25.6 Deployment Recovery

- release copy uses temp version directory and checksum verification
- Triton load occurs only after files are complete
- active pointer flips only after smoke tests pass
- if deployment fails, current production pointer is unchanged

### 25.7 Batch Recovery

- each chunk has independent status and output manifest
- reruns process only incomplete or failed chunks
- final aggregation is idempotent and based on chunk manifests

### 25.8 RQ Failure Modes

RQ is at-least-once, not exactly-once.

Design response:

- use job retries for transient failures
- detect abandoned jobs via maintenance worker
- requeue only if entity state says the step is incomplete
- never assume that one queue execution means one real-world side effect

## 26. GPU Coexistence Strategy

The workstation has one GPU, so coexistence must be explicit.

### 26.1 Guarantees

The platform guarantees:

- inference traffic is isolated from training in process topology
- training cannot overwrite serving assets
- batch inference cannot consume realtime serving capacity directly
- training can be paused and resumed safely

### 26.2 Runtime Policy

- `triton-rt` gets priority and strict memory headroom reservation
- `triton-batch` has separate instance counts and can be throttled independently
- training worker starts only if GPU admission checks pass
- if realtime SLO is threatened, training transitions to `paused_slo` at the next checkpoint boundary
- if necessary, batch queue dispatch is temporarily reduced before training is paused

### 26.3 Why This Is the Right Approach

On a single workstation GPU, true physical isolation is impossible without additional hardware. This design provides operational isolation, artifact isolation, and cooperative resource control so realtime serving stays protected and all long-running work remains resumable.

## 27. Security Model for Internal LAN Usage

### 27.1 Network Exposure

Expose only these browser-facing paths externally on the LAN:

- reverse proxy HTTPS
- realtime inference gateway HTTPS for approved internal clients
- trigger executor HTTPS route for approved PLC/web application clients calling
  pipeline-derived HTTP Trigger Input endpoints
- optional batch submission API HTTPS if external applications need it

Do not expose directly:

- PostgreSQL
- Redis
- Triton ports
- worker ports
- internal batch gateway endpoints

### 27.2 Authentication

Admin UI and control plane:

- username/password with Argon2id
- secure HTTP-only session cookie
- CSRF protection for browser sessions

Inference clients:

- scoped API keys
- separate scopes for `realtime.predict` and `batch.submit`

Label Studio:

- separate local accounts in CE
- role mapping stored in platform metadata
- linked from the admin UI under `/label-studio`

### 27.3 Authorization

Recommended roles:

- `platform_admin`
- `ml_engineer`
- `operator`
- `label_manager`
- `viewer`
- `inference_client`

### 27.4 Service-to-Service Security

- internal service tokens on private container network
- separate credentials per service for PostgreSQL
- webhook receiver validates secret header and source network
- reverse proxy enforces TLS

### 27.5 Audit

Audit all:

- login events
- dataset uploads
- snapshot creation
- training requests
- pause, resume, cancel actions
- model stage promotions
- deployment activations
- rollbacks
- batch submissions
- API key creation and revocation

## 28. Recommended Technology Choices

### 28.1 UI

- `React + TypeScript + Vite`
- `TanStack Router`
- `TanStack Query`
- `Zod`
- `OpenAPI-generated client`

Reason:

- strong typing
- maintainable data-fetching model
- low operational complexity

### 28.2 Control Plane API

- `FastAPI`
- `Pydantic v2`
- `SQLAlchemy 2`
- `Alembic`
- `psycopg`
- `Redis`
- `RQ`
- `structlog`

### 28.3 Webhook Receiver

- `FastAPI`
- `Pydantic v2`
- shared `cv_events` and `cv_db` packages

### 28.4 Training Service

- `PyTorch`
- `MMEngine`
- `MMDetection` for detection
- `MMPreTrain` for classification
- `Anomalib` for anomaly detection and localization
- `Albumentations`
- `ONNX`
- `OpenVINO` for the supported Anomalib runtime inferencer path
- `TensorRT`
- `Polygraphy` or `trtexec` wrapped by service code for engine build and validation

Reason:

- fully open-source
- supports classification, detection, segmentation, and the supported anomaly path
- mature enough for production recipes
- portable export path to ONNX plus TensorRT or OpenVINO where task-specific
- anomaly heatmaps come from the deployed anomaly model, not synthetic overlay code

### 28.5 Experiment Tracking

- `MLflow OSS`
- PostgreSQL backend
- local artifact store

### 28.6 Data and Artifact Versioning

- `DVC`
- local filesystem remote or mounted NAS path

### 28.7 Inference Serving

- `Triton Inference Server`
- explicit model control mode
- separate realtime and batch instances
- `tritonclient` in the gateway

### 28.8 Operational Storage

- `PostgreSQL` for metadata
- `Redis` for queueing and transient coordination
- POSIX filesystem under WSL2 ext4 for artifacts and repositories

### 28.9 Reverse Proxy

- `Caddy` or `Nginx`

Recommended default:

- `Caddy` for simpler local TLS and small-team maintainability

### 28.10 Runtime

- `WSL2 Ubuntu 24.04 LTS`
- `Podman`
- `podman compose`
- `systemd` inside WSL for production service management

## 29. Operator Workflow

### 29.1 Dataset and Labeling

1. Open admin UI.
2. Create dataset.
3. Upload images in browser.
4. Create or link Label Studio project.
5. Launch Label Studio from the admin UI.
6. Annotators label data in Label Studio.
7. Operator watches labeling progress in admin UI.
8. Operator creates immutable dataset snapshot.

For anomaly projects, Label Studio image-level labels map to anomaly semantics:

- `good` / `normal` become `normal`
- `bad` / `abnormal` / `defect` / `fail` become `anomalous`
- conflicting good and bad labels on the same image fail snapshot validation
- image-level anomaly training is supported now; operator-visible mask/localization
  annotation support remains hidden until masks are trainable, exported,
  deployable, inferable, and renderable end to end

### 29.2 Training

1. Choose snapshot.
2. Choose task type, recipe, hyperparameter profile, and output model family.
3. Click `Train`.
4. Monitor job state, checkpoints, metrics, and logs in admin UI.
5. If needed, pause or resume from admin UI.
6. Review evaluation results and artifact manifest.

For anomaly training, the task type is `Anomaly`, the current exposed method is
`PatchCore`, and evaluation persists anomaly-specific metrics such as
`image_auroc`, `image_f1`, precision, recall, and threshold values.

### 29.3 Promotion and Deployment

1. Select a trained model version.
2. Click `Stage` for realtime, batch, or both.
3. Run test images and saved regression cases from the admin UI.
4. Review smoke-test and benchmark report.
5. Click `Promote to Production`.
6. Watch rollout complete.
7. If necessary, click `Rollback`.

### 29.4 Batch Inference

1. Select deployment target.
2. Upload or reference batch input manifest.
3. Submit batch job.
4. Monitor chunk progress in admin UI.
5. Download or integrate final results bundle through API.

### 29.5 Health and Audit

1. Open `System Status`.
2. Review service health, queue depth, GPU headroom, and active deployments.
3. Open `Audit` to inspect every operator action and state transition.

## 30. Acceptance Tests

### 30.1 Platform Acceptance Criteria

The platform is accepted only if all of the following pass.

#### Dataset and Labeling

- browser upload of at least 50,000 images completes without CLI use
- interrupted upload session can resume without duplicate assets
- Label Studio project can be created and linked from admin UI
- webhook events are persisted even if downstream worker is offline
- immutable dataset snapshot can be created from Label Studio without operator CLI use

#### Training

- training can start from admin UI
- UI refresh does not affect the running job
- worker crash recovery resumes from the last valid checkpoint
- training failure is visible in UI with logs and audit trail
- classification, detection, segmentation, and supported anomaly recipes complete end to end
- anomaly training uses a real anomaly backend and produces score, threshold,
  heatmap-capable deployment artifacts

#### Registry and Promotion

- completed training creates MLflow run, DVC reference, and model version record
- `trained -> staged -> production` transition works without manual file edits
- staged model can be tested from admin UI before production activation
- model promotion never overwrites an active version directory

#### Deployment and Rollback

- deployment loads exact Triton version explicitly
- production traffic switches only after smoke test success
- rollback restores previous release without rebuilding artifacts
- failed deployment leaves previous production release active

#### Inference

- realtime inference path remains synchronous and queue-free
- batch inference never traverses realtime gateway or Triton instance
- realtime and batch targets can use different active releases of the same model family
- strict response targets are measured and stored for realtime path

#### Reliability

- all mutating commands accept idempotency keys
- duplicate webhook delivery does not duplicate business effects
- abandoned RQ job is detectable and recoverable
- audit trail records every state transition and operator command

#### Maintainability

- full stack can be started in development with one documented command sequence
- production config validates on startup
- schema migrations are automated and reversible
- service boundaries are reflected in the repo structure and tests

## 31. Acceptance Test Suite Structure

- `tests/contract`
  - OpenAPI contract validation
  - event schema validation
- `tests/integration`
  - Label Studio export
  - MLflow registration
  - DVC tracking
  - Triton load and unload
- `tests/e2e`
  - dataset upload to snapshot
  - snapshot to training
  - training to stage
  - stage to production
  - batch submit to result bundle
- `tests/performance`
  - realtime latency under load
  - batch throughput
  - training plus inference coexistence
- `tests/acceptance`
  - operator workflows with browser automation
  - rollback drill
  - worker crash and recovery drill

## 32. Implementation Roadmap

### Milestone 1: Foundations

- monorepo scaffolding
- shared contracts and core packages
- PostgreSQL schema and Alembic migrations
- Redis and RQ worker runtime
- reverse proxy and local TLS
- admin UI shell

Exit criteria:

- authenticated admin UI
- health dashboard
- queue-backed background job framework

### Milestone 2: Dataset and Labeling Integration

- upload sessions
- dataset asset registration
- Label Studio project management integration
- webhook receiver
- snapshot export and materialization
- DVC tracking for snapshots

Exit criteria:

- browser upload to immutable snapshot works end to end

### Milestone 3: Training Plane

- training recipes for classification, detection, segmentation, and supported anomaly
- checkpointing
- MLflow integration
- ONNX export
- TensorRT engine build
- evaluation reports

Exit criteria:

- operator can train supported task types from UI and recover from worker restart

### Milestone 4: Model Registry and Promotion

- model family and version registry
- trained and staged lifecycle
- serving package manifest generation
- smoke-test harness

Exit criteria:

- trained model can be staged and tested from UI

### Milestone 5: Serving Plane

- inference gateway realtime
- inference gateway batch
- separate Triton instances and repositories
- deployment and rollback
- active release routing

Exit criteria:

- safe production promotion and rollback on both target paths

### Milestone 6: Batch, Reliability, and Hardening

- batch chunk pipeline
- idempotency and retry hardening
- maintenance workers
- backups and retention
- observability dashboards
- acceptance suite

Exit criteria:

- full platform passes acceptance criteria

## 33. Final Recommendation

Build this platform as a strict multi-service monorepo with one operator UI, one control plane, one dedicated webhook ingress, one training codebase executed by a dedicated worker, and two fully separated inference paths backed by separate Triton instances.

Do not collapse these boundaries:

- control plane vs training
- training artifacts vs serving artifacts
- realtime vs batch inference
- operator UI vs orchestration backend

If those boundaries hold, the platform remains deterministic, recoverable, maintainable by a small team, and safe to operate on one GPU workstation.
