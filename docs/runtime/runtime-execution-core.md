# Runtime Execution Core

Authoritative inputs:

- `docs/architecture/cv-platform-spec.md`
- `docs/runtime/vision-runtime-spec.md`

This note describes the implemented execution path that turns a published
runtime pipeline version into executable work for trigger and realtime runtime
services.

## Constraints Restated

- Runtime execution stays in the runtime plane. It is not collapsed into
  `admin-api`.
- Trigger and realtime execution stay separated.
- Pipelines execute as typed node graphs. There is no per-channel hardcoded
  inference path.
- Runtime execution carries image contexts, object references, provenance,
  object enrichments, and display context selection. Live view is not modeled as
  only latest frame plus overlay.
- Inference nodes resolve logical deployed model references through the
  existing inference plane contracts, not Triton internals.
- Publishability is stricter than canvas validity: handlers, model bindings,
  ports, graph shape, and runtime-relevant properties must all be executable.

## Executable Path

1. A channel is assigned a published `pipeline_version_id`.
2. `runtime-orchestrator` validates and compiles the published pipeline into a
   supervised runtime instance. Executors load the published pipeline only for a
   ready `runtime_execution_id`.
3. The pipeline graph is normalized from the persisted node/edge DSL.
4. Node handler references are resolved from the node type registry.
5. `cv_runtime.engine.ExecutablePipelineGraph.build()` validates the graph as an
   acyclic executable DAG and produces a deterministic node order.
6. For each frame or trigger snapshot, `ExecutablePipelineGraph.execute()` runs
   nodes in topological order, propagates typed payloads across ports, and
   accumulates structured execution results.
7. If a running channel is saved with a different pipeline version or source
   binding, runtime-admin queues a restart. Reconcile also compares the active
   runtime execution against the saved channel pipeline and restarts stale
   bindings instead of letting an executor keep processing an old version.

## Determinism

Graph execution is deterministic within one pipeline version:

- topological ordering is stable
- duplicate upstream connections into one input port are rejected
- handler compatibility is checked before publish and before execution graph
  build
- branch fan-out uses the persisted graph edges rather than implicit routing

## Trigger Flow

- a published triggerable pipeline contains `trigger.http_put`,
  `source.channel_frame`, and `output.http_trigger_response`
- runtime persists endpoint metadata derived from the published pipeline
  version and HTTP Trigger Input node; it is not a separate operator-managed
  domain object
- `trigger-executor` accepts HTTP PUT calls only for enabled derived endpoints
  whose pipeline, auth config, runtime service, and channel frame acquisition are
  ready
- authentication is configured by the HTTP Trigger Input node and uses scope
  `runtime.trigger.invoke`
- admission is bounded by per-endpoint rate/burst limits and executor
  concurrency
- `trigger.http_put` creates the execution context
- `source.channel_frame` yields exactly one frame captured from request
  `image_base64`, the endpoint channel's HTTP(S) source, or an explicit
  live-frame fallback when configured and available
- inference and analysis nodes execute once
- `output.http_trigger_response` maps the public response body
- capture/output side effects are emitted as structured intents in the result
  model
- timeout, rejected, failed, and completed trigger attempts update endpoint
  last-result metadata and persist structured result records

## Realtime Flow

- `realtime-executor` binds to a supervised realtime runtime instance
- the realtime loop remains outside the graph engine
- `source.channel_frame` consumes frames supplied by the realtime loop
- source acquisition uses a bounded latest-frame buffer and drops older frames
  under load
- frame sampling, inference, analysis, overlay generation, and result shaping
  happen in graph order
- the latest live frame and overlay state is bound to `runtime_execution_id`,
  selected by `output.display_output` when required, and exposed through
  runtime-admin/overlay APIs

## Visual/Object Execution Data

Each execution result can carry:

- image contexts from source, crop, resize, ROI, and display nodes
- object sets from detection, segmentation, and classical CV region sets
- enriched object annotations from downstream per-object inference
- overlay primitives tied to image context and object references
- display output selections for live view

Simple single-path pipelines keep their safe default display behavior. Multiple
visual branches require an explicit `output.display_output` node before the
pipeline can publish.

## Initial Supported Runtime Nodes

Implemented runtime handlers:

- source: `source.channel_frame`
- trigger/input: `trigger.http_put`
- trigger/input: `trigger.interval`
- legacy/development source: `source.trigger_snapshot`, `source.rtsp`, and
  `source.file_test` remain executable for already-published versions and test
  fixtures, but new operator pipelines should use `source.channel_frame` because
  camera/source configuration belongs to the channel.
- preprocessing: `preprocessing.resize`, `preprocessing.crop`,
  `preprocessing.roi_extract`, `preprocessing.frame_sampler`,
  `preprocessing.zone_filter`
- inference: `inference.detection`, `inference.classification`,
  `inference.segmentation`
- computer vision: `computer_vision.canny_edge_detection`,
  `computer_vision.color_object_segmentation`,
  `computer_vision.reference_edge_from_cv_edges`
- analysis: `analysis.position_determination`,
  `analysis.zone_entry_exit`, `analysis.optical_flow_motion`,
  `analysis.detection_size`, `analysis.object_counting`,
  `analysis.object_spatial_signal`, `analysis.object_orientation_signal`,
  `analysis.movement_direction`, `analysis.rule_evaluation`
- capture: `capture.full_frame_labelstudio`, `capture.roi_labelstudio`,
  `capture.conditional`, `capture.multi_project`
- output: `output.overlay`, `output.display_output`,
  `output.http_trigger_response`, `output.persisted_result`,
  `output.internal_event`

## Hidden Assumptions Chosen Safely

- Movement direction is currently supported from repeated position observations
  keyed to the same logical object, not from full multi-object tracking.
- Template inference nodes may remain intentionally unbound until an operator
  chooses a deployed capability through the picker UI. Those drafts are not
  runnable until bound and published.
- Runtime handlers may fail fast with structured node errors; they do not
  silently skip incompatible dependencies.
