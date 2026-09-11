# vision-runtime-roadmap.md

## Purpose
This roadmap is the authoritative implementation and delivery plan for the runtime plane.
It complements, but does not replace, the platform and runtime specifications.

Authoritative references:
- `docs/architecture/cv-platform-spec.md`
- `docs/runtime/vision-runtime-spec.md`

This roadmap defines:
- the current audited implementation baseline
- the ordered runtime workstreams
- dependencies between workstreams
- completion criteria per workstream
- which Codex prompt belongs to which workstream
- whether a prompt is greenfield, consolidation, or hardening

---

## Status vocabulary
Every workstream must use one of these statuses:
- `not_started`
- `partial`
- `boundary_only`
- `complete`

Definitions:
- `not_started`: no meaningful implementation exists
- `partial`: meaningful implementation exists, but the end-to-end path is incomplete
- `boundary_only`: APIs, schemas, UI shells, service declarations, or contracts exist, but the real runtime behavior is not implemented end-to-end
- `complete`: operator-facing and runtime-facing behavior is fully implemented end-to-end, aligned with specs, and covered by meaningful tests

A workstream is only `complete` if:
- the operator can use it through the real UI or documented integration path
- the backend persists and validates it correctly
- the runtime actually executes it through the intended platform path
- observability and recovery behavior are present where required
- tests prove the same end-to-end behavior

Code presence alone does not count as complete.

---

## Current audited baseline
Latest known audit summary after Prompts 1, 2, 3, 3b:

### Already useful and should be preserved
- runtime DB schema foundations
- pipeline definition/versioning store
- node registry
- logical model references
- model capability cache
- executable pipeline engine core
- builtin calculation/capture/output node handlers (partial)
- trigger executor skeleton
- manual full-frame capture path (partial)
- designer UI
- channel UI scaffolding
- compose/proxy service topology

### Main blocking mismatch
The previous major blocker was the missing real runtime lifecycle execution path:
- channel start/stop returned accepted runtime-control commands
- but there was no complete runtime-control worker/orchestrator consuming and supervising them end-to-end

W1 now provides the persisted runtime-control command path, runtime instance
records, executor heartbeat supervision, stop/restart, and reconciliation
foundation. Later areas remain incomplete until their own workstreams add the
domain-specific execution loops and user-facing data:
- end-to-end operator runtime experience

W2 now provides the realtime-specific execution loop on top of W1:
per-channel RTSP/file-test source adapters, bounded low-latency buffering,
sampling/drop policy, inference dispatch through the realtime inference gateway,
runtime-bound latest frame/overlay state, operator live view, and live metrics.

### Known unsafe or misleading implementations to correct
- accepted runtime-control commands without real consumers (corrected in W1)
- unauthenticated trigger/realtime endpoints by default (hardened in W1)
- `image_path` payloads enabling unsafe server-local path reads (blocked in W1 executor APIs)
- RTSP URLs with inline credentials are not persisted; runtime-admin converts
  legacy or pasted credentialed URLs into sanitized source config plus managed
  source secret refs before start/restart.
- boundary-only overlay/connector services that look production-ready (corrected for connector delivery in W6)
- capture nodes emitting intents without a real processor (corrected in W5)
- live-view UI that implies streaming exists when it does not (corrected in W2)
- in-memory interval capture inside runtime-admin instead of durable backend/runtime-owned execution (corrected in W5)
- trigger behavior modeled as a channel mode or separate operator object; the
  corrected model is pipeline-centric and channel-bound: HTTP Trigger Input ->
  Channel frame input -> processing -> Response Output.

---

## Implementation strategy
The runtime plane must now proceed as a consolidation-and-completion program, not as a greenfield linear build.

Rules:
- preserve useful forward-implemented pieces
- correct unsafe or misleading behavior before adding more surface area
- finish the earliest blocking dependency first
- do not mark later workstreams complete if they depend on incomplete lifecycle/runtime semantics
- prefer backend truth and real runtime behavior over UI appearance

---

## Workstream map

### W1. Runtime lifecycle core
**Status:** `complete`

**Goal:**
Make channel start/stop actually create, supervise, and persist real runtime instances.

**Why this is first:**
All later runtime features depend on a real lifecycle.

**Includes:**
- runtime-control worker/orchestrator
- channel desired_state vs actual_state model
- runtime instance persistence
- executor registration/heartbeat
- start/stop/restart/reconcile flows
- safe command semantics
- UI/runtime alignment for channel state

**Depends on:**
- Prompts 1, 2, 3, 3b baseline

**Blocks:**
- W2, W3, W4, W5, W7, W8

**Completion criteria:**
- starting a channel causes a real runtime-control path to execute
- a runtime instance is created, supervised, and state-tracked
- start/stop commands are consumed by a real worker/orchestrator path
- runtime state is persisted and visible in UI
- realtime channel lifecycle is supervised by the lifecycle model; HTTP trigger
  execution is exposed through pipeline-derived endpoints rather than channel
  runtime commands
- restart/reconcile behavior exists

**Prompt:** `Prompt 4a`
**Prompt type:** `consolidate_and_complete`

---

### W2. Realtime executor, RTSP, live view, overlay
**Status:** `complete`

**Goal:**
Implement true low-latency live channel processing with RTSP, frame loop, overlay, and operator live view.

**Includes:**
- RTSP reader loop
- reconnect handling
- sampling/drop policy
- per-channel realtime executor binding
- live frame/overlay stream API
- live-view UI rendering
- runtime FPS/latency/drop metrics

**Depends on:**
- W1 complete enough for runtime lifecycle ownership

**Blocks:**
- true live manual capture
- full operator runtime experience

**Completion criteria:**
- a live RTSP channel can be started from UI
- a real executor loop runs
- frames are processed continuously
- overlay data is rendered in live view
- drop/reconnect policies are real
- channel runtime metrics show meaningful live data

**Delivered in W2:**
- `realtime_executor.sources` implements RTSP/file-test frame adapters, timeout
  handling, reconnect health, and inline credential rejection.
- `realtime_executor.live` implements a per-channel runtime worker, bounded
  latest-frame buffer, sampling, pipeline execution, live overlay state writes,
  heartbeats, and p50/p95/p99/FPS/drop/reconnect metrics.
- `runtime-orchestrator` launches realtime workers from W1 runtime instances
  and reconciles dead executor handles.
- Runtime admin and overlay APIs expose latest runtime-bound live state.
- Admin UI live view renders actual latest frames and overlays only for active
  runtime instances.

**Production streaming hardening added after W2:**
- Runtime-owned GStreamer publication plus MediaMTX fanout is the production
  livestream path for RTSP/WebRTC consumers.
- Latest-frame and MJPEG APIs remain debug/snapshot paths only.
- Stream metadata exposes raw/overlay/debug variants, encoder mode, LAN-open
  access policy, URLs, readiness, and warnings.
- Offline Windows/WSL updater bundles and validates MediaMTX installation and
  post-update stream smoke checks.

Documentation:
- `docs/runtime/realtime-live-runtime.md`
- `docs/runtime/livestream-streaming-architecture.md`

**Prompt:** `Prompt 4b`
**Prompt type:** `consolidate_and_complete`

---

### W3. Pipeline HTTP trigger hardening and PLC contract
**Status:** `complete`

**Goal:**
Turn the partial trigger backend into a secure, deterministic, externally usable pipeline-trigger runtime.

**Includes:**
- first-class pipeline nodes: HTTP Trigger Input, Channel frame input, and
  Response Output
- pipeline-derived HTTP PUT trigger endpoint metadata owned by runtime
  deployment state, not normal operator CRUD
- endpoint/channel/pipeline binding derived from the published pipeline graph,
  with enablement, timeout, rate/admission control, auth requirement, response
  mapping, readiness, and audit metadata
- deterministic `trigger-executor` path bound to pipeline readiness and channel
  frame acquisition capability
- safe request image handling and HTTP(S)-only channel frame acquisition
- structured timeout/error taxonomy, persisted results, and response field
  mapping
- pipeline designer UI for configuring the HTTP Trigger Input, Channel frame
  input, and Response Output nodes
- pipeline/channel detail surfaces endpoint URL, method, auth, timeout,
  readiness, response contract, and which channels are referenced
- OpenAPI security schemes for bearer and API-key trigger calls
- PLC/control-system contract documentation

**Depends on:**
- W1 runtime lifecycle

**Completion criteria:**
- an external client can securely call the trigger endpoint
- the endpoint executes the configured pipeline graph deterministically
- trigger endpoint metadata is derived from the published pipeline applied to a
  channel, while the graph itself uses Channel frame input
- timeouts/errors are well-defined
- response body returns configured result fields
- UI reflects real trigger readiness without a separate trigger-management area

**Delivered artifacts:**
- `backend/packages/cv-db/src/cv_db/trigger_endpoint_store.py`
- `backend/services/trigger-executor/src/trigger_executor/api/routes.py`
- `frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx`
- `frontend/apps/admin-web/src/features/channels/channel-detail-page.tsx`
- `contracts/openapi/trigger-executor.v1.yaml`
- `contracts/openapi/runtime-admin-api.v1.yaml`
- `docs/runtime/trigger-runtime-plc-contract.md`

**Prompt:** `Prompt 5 + pipeline-trigger correction`
**Prompt type:** `complete_and_harden`

---

### W4. Result service and operator-facing result workflows
**Status:** `complete`

**Goal:**
Turn existing calculation/result nodes into a real result subsystem with APIs and UI.

**Includes:**
- result-service APIs
- recent result history
- result detail views
- query/filtering
- integration with trigger and realtime outputs
- operator-visible derived results

**Depends on:**
- W1
- W2 or W3 for real runtime inputs

**Completion criteria:**
- runtime executions persist usable result records
- operators can browse recent results
- result payloads are typed and visible in UI
- calculations are not only engine-level but operator-visible

**Delivered in W4:**
- `ResultRecord` persistence now separates raw inference outputs, normalized
  inference outputs, derived outputs, overlay primitives, trigger response
  fields, runtime metadata, mode, pipeline, status, and result type.
- `result-service` exposes list, latest, and detail APIs with channel,
  pipeline, mode, time range, status, result type, and search filters.
- Trigger execution persists completed, failed, and timeout records with mapped
  trigger response fields.
- Realtime execution preserves the low-latency policy by persisting explicit
  `output.persisted_result` emissions and failure records.
- Admin UI exposes recent results on channel detail and a dedicated Result
  History page with compact cards, filters, and detail inspection.

Documentation:
- `docs/runtime/result-semantics-guide.md`
- `docs/engineering/adding-derived-runtime-result-fields.md`

**Prompt:** `Prompt 6`
**Prompt type:** `complete_and_align`

---

### W5. Capture service, ROI capture, Label Studio delivery
**Status:** `complete`

**Goal:**
Turn manual and pipeline-emitted capture intents into a real capture subsystem with durable execution.

**Includes:**
- capture-service execution
- durable capture jobs/sessions
- ROI materialization
- multi-project delivery
- retry/rate-limit behavior
- live-view manual capture binding to real live frames
- capture history/status

**Depends on:**
- W1
- W2 for true live-frame capture
- existing Label Studio integration

**Completion criteria:**
- manual full-frame live capture works from live view
- interval capture is backend/runtime-owned
- pipeline-based capture intents are delivered
- ROI capture works end-to-end
- one channel can feed multiple Label Studio projects

**Delivered in W5:**
- Manual capture commands now enqueue durable capture delivery work instead of
  running Label Studio submission or interval loops in `runtime-admin-api`.
- `capture-service` owns interval ticks, request claiming, frame resolution,
  asset materialization, Label Studio submission, retries, rate limiting,
  duplicate suppression, and per-project delivery status.
- Runtime execution result persistence turns pipeline `CaptureIntent` payloads
  into `runtime_capture_requests`; realtime execution records capture-bearing
  results without blocking the live inference path.
- ROI capture supports extracted ROI payloads and detection-derived ROI capture
  from frame plus detection outputs.
- Channel detail shows manual capture controls and capture delivery history.

Documentation:
- `docs/runtime/manual-live-capture.md`
- `docs/runtime/pipeline-vs-manual-capture.md`
- `docs/runtime/roi-capture-guide.md`
- `docs/runtime/capture-failure-handling.md`

**Prompt:** `Prompt 7`
**Prompt type:** `complete_and_align`

---

### W6. Output connectors and extensibility
**Status:** `complete`

**Goal:**
Make output connectors real, modular, and execution-backed.

**Includes:**
- connector-service delivery path
- connector worker/runtime registry
- retries, delivery status, secret handling
- built-in connectors completion
- plugin/extension developer model

**Depends on:**
- W1
- W4 for result payloads

**Completion criteria:**
- at least the built-in connectors execute end-to-end
- connector status is observable
- extension model is documented and usable
- boundary-only connector placeholders are removed or completed

**Completed implementation:**
- typed output connector contracts and config validation in `cv_runtime.connectors`
- durable `output_connectors` and `runtime_connector_deliveries` status model
- runtime-admin API and channel UI surfaces for connector configuration/status
- connector-service worker loop with isolated delivery execution and retry state
- built-in overlay, HTTP trigger response, persisted result, and internal event connectors
- connector architecture, operator behavior, and developer extension documentation
- unit/component tests for registration, validation, execution, retry isolation, and UI status

**Prompt:** `Prompt 8`
**Prompt type:** `complete_and_harden`

---

### W7. Performance, observability, recovery, operator UX hardening
**Status:** `complete`

**Goal:**
Add operational completeness for the runtime plane.

**Includes:**
- runtime metrics aggregation
- system status integration
- Prometheus-friendly metrics where applicable
- restart/recovery matrices
- stale runtime cleanup
- operator-friendly health/status UX
- performance visibility (FPS/latency/drop rate)

**Depends on:**
- W1
- W2 and W3 for meaningful live/trigger metrics

**Completion criteria:**
- runtime services show meaningful operational health
- restarts/recovery behave as documented
- performance indicators are visible and useful
- operator UI stays compact and understandable

**Delivered in W7:**
- Runtime operational aggregation now summarizes channel health, observed state,
  executor heartbeat age, recovery attempts, FPS, latency, drop rate, trigger
  counts, trigger latency, stale runtime instances, and failed runtime
  instances.
- `runtime-admin-api` exposes Prometheus-friendly runtime-plane metrics at
  `/api/v1/runtime/metrics` while preserving every generic service `/metrics`
  endpoint.
- Prometheus scrapes runtime services and the runtime-plane aggregation target.
- Admin system status includes runtime summary, runtime alerts, and compact
  runtime status cards.
- Channel list/detail and Health views show compact degraded/failed/stale
  badges, error summaries, heartbeat age, recovery attempts, and remediation
  hints.
- Runtime-control command creation coalesces duplicate pending commands for the
  same channel and command, while explicit idempotency keys remain supported.
- Runtime reconciliation cleans dangling terminal active-runtime references and
  keeps degraded or failed evidence visible to operators.
- Orchestrator, capture-service, and connector-service loops write service
  heartbeats for worker visibility.

Documentation:
- `docs/runtime/runtime-operations-runbook.md`

**Prompt:** `Prompt 9`
**Prompt type:** `hardening`

---

### W8. End-to-end runtime integration and final consistency pass
**Status:** `complete`

**Goal:**
Align the runtime plane fully with the existing platform and remove inconsistencies.

**Includes:**
- shared auth/key integration
- deployed-model discovery alignment
- navigation alignment
- final acceptance paths
- docs consistency
- cleanup of stale placeholder assumptions

**Depends on:**
- W1 through W7 substantially complete

**Completion criteria:**
- channel -> runtime -> inference -> result -> overlay/capture/output works coherently
- control plane and runtime plane integrate cleanly
- docs, UI, APIs, and services agree
- final acceptance checklist passes

**Delivered in W8:**
- Runtime model capability options are projected from deployment releases,
  deployment targets, model versions, and model families. The runtime cache is a
  short-lived deployment-state projection rather than a separate hidden model
  source.
- Pipeline-derived trigger endpoints default to API-key authentication. Runtime
  drain endpoints for capture and connector services require service-scoped
  machine credentials when API keys are enabled.
- Operator trigger smoke tests call the real trigger-executor PUT path exposed
  from a published pipeline's HTTP Trigger Input; authenticated tests use a
  scoped machine token instead of bypassing runtime auth through the control
  plane.
- Channel create/edit forms select published runtime pipeline versions instead
  of asking operators to type pipeline names or internal IDs.
- Capture routing resolves Label Studio targets through platform
  `LabelProject` records only. Raw Label Studio project numbers are rejected in
  runtime capture intents and manual capture remains selector-driven.
- Navigation labels separate channels, runtime pipelines, results, and advanced
  operations without duplicating operator concepts.
- Tracked local secret/token files were removed from the repository and the
  reverse proxy exposure map now includes runtime public prefixes while blocking
  direct runtime service prefixes in production.
- Focused acceptance tests cover deployment-driven model discovery, trigger
  machine auth, runtime execution/result paths, live overlay state, capture
  delivery, and raw Label Studio ID rejection.

**Delivered extension: Siemens SPS write output node**

- `output.siemens_sps_write` is a real runtime output node, grouped as
  Industrial / PLC inside the existing Output category.
- The node uses typed structured config, publish validation, dry-run defaults,
  environment-level write gating, upstream result field selection, mapping and
  conversion, a mockable S7 client abstraction, runtime status payloads, and
  offline dependency deployment hooks.
- It supports DB writes for `BOOL`, `BYTE`, `WORD`, `DWORD`, `INT`, `DINT`,
  and `REAL` only. `STRING`, M/I/Q areas, and named PLC connector registry
  selection remain future work.

Documentation:
- `docs/runtime/end-to-end-runtime-integration.md`
- `docs/operations/operator-guide.md`

**Prompts:**
- `Prompt 10`
- `Prompt 11`

**Prompt type:**
- `integration`
- `final_consistency_pass`

---

### W9. Context-aware visual/object rendering foundation
**Status:** `partial`

**Goal:**
Move runtime visual execution from a naive latest-frame overlay model to
image-context, object-reference, provenance, enrichment, and display-context
semantics.

**Includes:**
- typed runtime image contexts
- typed object references
- provenance links for derived image/object paths
- basic object enrichment model
- context-bound overlay primitives
- `output.display_output` live-view selector node
- validation for ambiguous visual branches and missing overlay context
- documentation and migration guidance

**Depends on:**
- W1 through W8 baseline

**Completion criteria for the foundation:**
- simple single-path pipelines still render by default
- multi-branch visual pipelines require Display output
- detection/segmentation produce object references
- ROI-derived payloads preserve source object and source image context
- downstream ROI classification can attach enrichment to a source object
- live-view overlays are tied to the selected display context

**Known remaining future work:**
- executable ROI Fan-Out node
- explicit fan-in/merge node for multi-ROI result aggregation
- richer coordinate projection beyond conservative same-context rendering
- operator-facing advanced debug views for image context lineage

**Prompt type:** `consolidate_and_extend`

---

### W10. First-class anomaly model and runtime support
**Status:** `implemented for PatchCore baseline`

**Goal:**
Expose anomaly detection/localization as its own supported model task without
treating it as binary classification.

**Delivered:**
- `anomaly` task type in shared contracts, model catalog, templates, training
  jobs, deployment manifests, runtime capability cache, and UI schemas
- Anomalib PatchCore support matrix entry only; PaDiM, EfficientAD, and other
  Anomalib methods remain hidden until their full path is implemented
- Label Studio good/bad image labels map to canonical `normal` and
  `anomalous` dataset semantics
- anomaly snapshot validation and Anomalib folder staging require normal
  training images and both normal/anomalous evaluation images
- real Anomalib PatchCore trainer integration with checkpoint, evaluation
  metrics, threshold metadata, and ONNX export validation
- anomaly deployment manifests preserve backend/method/threshold/heatmap
  capability metadata
- realtime and public anomaly inference responses include score, decision,
  threshold, heatmap, optional mask, and image dimensions
- runtime `inference.anomaly` node outputs anomaly results and heatmap overlay
  primitives tied to image context
- live/runtime UI renders heatmap overlays and compact anomaly score/decision
  results

**Current support boundary:**
- Supported now: PatchCore, image-level good/bad labels, score output,
  thresholding, heatmap overlay.
- Not exposed yet: mask-supervised anomaly localization, PaDiM, EfficientAD,
  FastFlow, Reverse Distillation, STFPM, or any method without complete
  train/export/deploy/infer/render support.

---

### W11. Board measurement and first-free-region workflow
**Status:** `complete for single-image/two-edge baseline`

**Goal:**
Turn existing Feature Detection outputs into a real measurement/rule workflow
for a board visible in one camera image.

**Delivered:**
- reusable runtime/result contracts for reference edges, coordinate frames,
  feature measurements, clearance zones, free regions, and measurement
  decisions
- runnable nodes for reference-edge extraction from Feature Detection,
  two-edge coordinate frame construction, feature measurement, clearance zone
  generation, first-free-region search, pixel-to-metric conversion, and simple
  quality rule evaluation
- runtime result envelope, response output, persisted result, and overlay
  integration for the typed measurement outputs
- pipeline publish validation for Feature Detection wiring, required edge
  roles, clearance measurement bases, metric calibration, free-region inputs,
  and response fields
- admin pipeline editor controls using structured mapping/rule tables instead
  of raw JSON
- live/result overlay rendering for lines, axes, zones, first free region, and
  distance arrows
- backend and frontend tests proving the board-like end-to-end scenario

**Current support boundary:**
- Supported now: single image, two detected edges, image-coordinate frame,
  bbox/polygon/mask/axis-projected measurements where geometry exists, simple
  `mm_per_pixel` calibration, one-dimensional blocked interval search.
- Not in this workstream: perspective correction/homography, multi-camera
  workpiece fusion, tracking, full grading models, or automatic edge ML beyond
  whichever classes the existing Feature Detection node produces.

**Prompt type:** `consolidate_and_extend`

---

### W12. Classical CV edges and multi-branch board workflow
**Status:** `complete for Canny/reference-edge baseline`

**Goal:**
Add a dedicated Computer Vision node category for deterministic edge workflows
and prove that the pipeline engine/editor support true fan-out/fan-in for
parallel CV and Feature Detection branches.

**Delivered:**
- Computer Vision category in the runtime node registry and admin node palette
- runnable OpenCV Canny Edge Detection node with structured configuration,
  validation, provenance-linked edge-mask image context, timing metadata, and
  optional contour/debug overlays
- runnable Reference Edge From CV Edges node that extracts Hough line segment
  candidates, selects configured roles, and emits the same `ReferenceEdge`
  contract as Feature Detection based edge extraction
- preserved Feature Detection edge extraction path through Reference Edge From
  Features
- graph validation and execution coverage for fan-out from one input frame and
  fan-in into coordinate-frame, measurement, clearance, and free-region nodes
- structured admin controls for CV line-selection rules instead of raw JSON
- overlay/debug layers for Canny mask, candidate lines, selected reference
  edges, coordinate frame axes, measurements, zones, and free-region outputs
- backend and frontend tests for CV node contracts, branch validation, and the
  parallel board workflow

**Current support boundary:**
- Supported now: CPU OpenCV Canny, optional contours, Hough line segment based
  reference-edge selection, image-coordinate two-edge coordinate frames,
  one-to-many and many-to-one pipeline graph execution.
- Not exposed yet: standalone Hough Lines node, perspective/homography
  correction, multi-camera fusion, GPU CV acceleration, or learned edge
  extraction outside the existing Feature Detection inference node.

**Prompt type:** `consolidate_and_extend`

---

## Prompt mapping summary
1. Prompt 4a -> W1 Runtime lifecycle core
2. Prompt 4b -> W2 Realtime executor, RTSP, live view, overlay
3. Prompt 5 -> W3 pipeline HTTP trigger hardening
4. Prompt 6 -> W4 Result service and result workflows
5. Prompt 7 -> W5 Capture service and Label Studio delivery
6. Prompt 8 -> W6 Output connectors and extensibility
7. Prompt 9 -> W7 Performance, observability, recovery, operator UX hardening
8. Prompt 10 -> W8 End-to-end runtime integration
9. Prompt 11 -> W8 Final consistency pass
10. Context-aware visual/object rendering prompt -> W9 foundation
11. First-class anomaly model prompt -> W10 PatchCore baseline
12. Board measurement/free-region prompt -> W11 single-image baseline
13. Classical CV edges and multi-branch prompt -> W12 Canny/reference-edge
    baseline

---

## Required prompt preamble for every new Codex thread
Every new Codex thread for runtime work must start with:

```text
Use docs/architecture/cv-platform-spec.md as the authoritative architecture specification.
Use docs/runtime/vision-runtime-spec.md as the authoritative runtime specification.
Use docs/runtime/vision-runtime-roadmap.md as the authoritative implementation roadmap.
Use docs/runtime/pipeline-node-plugin-model.md as the authoritative node/plugin model document where relevant.

Non-negotiable constraints:
- open-source only
- do not simplify architectural decisions
- keep control plane, runtime plane, and inference plane separated
- no operator CLI for normal workflows
- realtime inference path, trigger path, and training path must remain separated
- no fake integrations or placeholder critical paths
- no manual internal IDs required from operators
- use the current platform as the integration baseline, not a greenfield rewrite
- preserve useful forward-implemented pieces unless they are unsafe or misleading
- implement with production-safe defaults, strong typing, migrations, tests, and documentation
```

Additionally, each prompt must explicitly name:
- the workstream ID (for example `W1`)
- the current known audited status
- whether the task is greenfield, consolidation, completion, or hardening
- what existing partial implementations must be preserved
- what misleading/unsafe implementations must be corrected

---

## Governance rule
If a future audit contradicts this roadmap, update the roadmap before continuing with more implementation prompts.
The roadmap is the authoritative delivery plan.
