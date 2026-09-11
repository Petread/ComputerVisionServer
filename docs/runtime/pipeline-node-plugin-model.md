# Runtime Pipeline Node and Plugin Model

This document defines the extension model for no-code runtime pipelines. The
authoritative architecture remains `docs/architecture/cv-platform-spec.md`; the
authoritative runtime behavior remains `docs/runtime/vision-runtime-spec.md`.

## Ownership

`pipeline-definition-service` owns:

- node capability registry
- node property schemas
- visual/object/data port contracts
- pipeline graph validation
- pipeline definition and version lifecycle
- template pipeline definitions

`runtime-admin-api` exposes the same operator-facing contracts for the admin UI.
Executors consume only published pipeline versions.

## Pipeline DSL

A pipeline version stores a directed graph:

- `nodes`: stable node keys, node type, category, contract version, ports,
  config, optional logical model reference, visual/object compatibility
  metadata, and canvas position
- `edges`: source node/port to target node/port connections
- `validation_report`: publish-blocking validation result
- `config_digest`: deterministic digest of the normalized graph

Operators never edit this graph as raw JSON. The UI creates and updates it
through the node palette, canvas, and contextual node properties modal.

## Runtime Data Contracts

Runtime pipelines move three related kinds of data:

- visual data: image contexts and frame payloads
- object data: object references, object sets, ROIs, and geometry
- result data: inference results, derived measurements, object enrichments,
  overlays, captures, events, result outputs, persisted results, and trigger
  responses

Node authors must model these explicitly. A node is not just a generic function
over JSON.

## Image Context Contract

An image context is a first-class visual frame state.

Examples:

- original channel frame
- resized frame
- fixed crop
- ROI image derived from an object
- masked or processed frame

Each image context carries:

- `context_id`
- `kind`: `original`, `crop`, `roi`, `transformed`, `masked`, or `processed`
- width/height when known
- source URI when available
- source node key
- source image context id
- parent image context id
- optional source object id
- transform metadata and coordinate projection policy
- provenance links

Every visual-processing node must declare whether it:

- consumes one or more image contexts
- preserves an image context unchanged
- transforms an image context and creates a new one
- produces a new image context from object or ROI data

Frame outputs must carry the image context with the frame payload.

## Object Reference Contract

Object-producing nodes such as detection and segmentation emit object
references. An object reference is stable for the current runtime execution or
frame. Future tracking may add cross-frame `track_id`, but tracking is not
assumed by this contract.

Each object reference carries:

- `object_id`
- object type, for example `detection`, `segment`, `region`, or future
  `tracked_object`
- source image context id
- geometry and coordinate system
- class/label and score when applicable
- optional class id and track id
- producing node key
- provenance metadata

Detection payloads still expose model detections for compatibility, but they
also carry object references so later ROI, classification, segmentation, and
merge nodes can attach results to the source object.

## Provenance Contract

Any object-derived or image-derived path must preserve enough lineage to answer:

- which image context produced this data?
- which source object caused this ROI or downstream inference?
- which node and model produced the result?
- whether coordinates can be projected to another image context?

ROI and crop payloads must link:

- source image context
- parent image context
- source object reference when object-derived
- transform relationship
- projection policy

Projection is conservative. If a renderer cannot prove that an overlay can be
projected onto the selected display context, it must not silently render it as
though it were valid.

## Enrichment And Merge Contract

Downstream per-object results are represented as object enrichments. Examples:

- ROI classification labels
- segmentation-derived area or shape measurements
- per-object quality scores
- downstream labels or custom attributes

An enrichment carries:

- target object id
- source object id when different
- source image context id
- derived image context id when produced from an ROI/crop
- enrichment type
- typed attributes
- producing node key
- provenance links

Merge/fan-in nodes must require valid source object linkage. A pipeline cannot
publish a merge/enrichment path that loses the source object reference.

## Overlay And Display Contract

Overlay primitives are bound to an image context and/or object reference.

Overlay primitives may represent:

- boxes
- polygons
- points
- text/labels
- measurements
- enriched labels from downstream nodes

Each primitive should carry:

- primitive type
- label/confidence when available
- geometry
- image context id
- frame id or timestamp for live synchronization
- display context id when the overlay has been projected for viewing
- coordinate system and resolution
- object id or source object id when object-bound
- projected-from context id only when projection has been explicitly performed
- style and layer metadata

Overlay Output nodes produce overlay payloads, not full image frames. Lightweight
overlays use primitives such as boxes, polygons, lines, axes, motion vectors,
measurements, clearance zones, and text. Heavy overlays such as segmentation
masks, anomaly heatmaps, CV masks, and free-region masks must use efficient
encodings such as polygons, RLE, or compressed/downsampled alpha or heatmap
textures. Raw full-frame mask or image blobs are debug-only payloads and must not
be the production livestream transport. Overlay payload generation happens once
per processed frame/runtime result; multiple viewers consume the same metadata or
the same server-composited stream variant without rerunning inference or overlay
generation.

The live view renders on a display context.

Default rules:

- If the pipeline has one clear visual path, the live view uses the latest image
  context on that path.
- Overlays render on the image context they belong to.
- If multiple visual branches exist, the pipeline must include
  `output.display_output`.
- `output.display_output` selects the live-view image context by receiving a
  frame input; operators are not asked to type internal context ids.
- `output.display_output` may declare allowed object, overlay, and enriched
  label layers.

`output.display_output` is not an external connector. It is a runtime/live-view
visual output node.

## Node Registry Contract

Every node type has:

- `node_type`, for example `inference.detection`
- `node_category`: `trigger`, `source`, `preprocessing`, `inference`,
  `computer_vision`, `analysis`, `capture`, or `output`
- `contract_version`
- supported runtime modes
- input and output ports with data types
- default config
- property schema for the no-code form
- runtime handler reference
- compatibility constraints

Visible built-in nodes must also declare authoring metadata under
`compatibility_constraints.authoring`:

- `starter`: whether the node can appear in an empty graph palette
- `singleton`: whether only one node of this type may exist in a graph
- `blocks_with_node_types`: mutually exclusive node types such as HTTP and
  interval triggers
- `requires_upstream_data_types`: additional palette-time data requirements
  beyond required input ports
- `terminal`: whether the node ends a result/output path
- `palette_reason`: the concrete reason the node is offered in the current
  authoring workflow

Required input ports remain the primary compatibility contract. Authoring
metadata documents special UX rules that ports cannot express by themselves,
such as source singleton behavior, trigger exclusivity, and terminal output
placement. Nodes missing these declarations should be treated as extension or
legacy nodes and should not appear in the normal operator palette.

Property schemas must include:

- a stable `key`
- a human-readable `label`
- a `section` used to group fields in the node properties modal
- concrete `help_text` explaining the runtime behavior controlled by the field
- typed options or min/max constraints where applicable

Structured fields must label their subfields in the editor and should prefer
dynamic channel/upstream choices over free-text input. Manual advanced entry is
allowed only where validation already accepts custom paths or labels.

Node handlers must declare through ports and compatibility constraints:

- input image contexts
- output image contexts
- input object sets
- output object sets
- whether coordinates are preserved, transformed, or no longer projectable
- whether downstream merge/enrichment support is required
- whether a node is hidden from the normal operator palette

The initial built-in registry covers:

- trigger/input: `trigger.http_put` declares an externally callable HTTP
  trigger entrypoint for a published pipeline. It is a pipeline node, not a
  channel mode and not a separate operator-managed business object.
- trigger/input: `trigger.interval` declares periodic channel-frame execution
  for interval workflows. It is configured in the pipeline graph and uses the
  same channel-owned source binding as other executions.
- source: channel frame input
- legacy trigger source binding: `source.channel_snapshot` is kept for existing
  published versions and migrations, but hidden from the normal palette. New
  trigger and interval pipelines use `source.channel_frame`; the channel that
  applies the pipeline supplies the source frame.
- legacy/development source: RTSP, trigger snapshot, and file/test are kept for
  existing published versions or dev fixtures, but hidden from the normal
  palette because source configuration is owned by the channel
- preprocessing: resize, crop, ROI extract, frame sampler, zone filter
- inference: detection, classification, segmentation
- computer vision: color object segmentation, Canny edge detection, and
  reference edge extraction from CV edges. A Hough line extraction node must
  not appear as supported until it has its own runnable handler and validation.
- analysis: position, zone entry/exit, optical flow/motion, detection size,
  reference edge extraction, coordinate frame construction, feature
  measurement, clearance zone generation, first-free-region search,
  pixel-to-metric conversion, quality rule evaluation, object spatial signal,
  object orientation signal, counting, direction, rule evaluation
- capture: full-frame, ROI, conditional, multi-project Label Studio capture
- output: overlay, display output, result output, and legacy HTTP trigger
  response, persisted result, and internal event nodes

Structured property field types include `edge_role_mapping`,
`cv_line_selection_rules`, `measurement_rule_table`, `clearance_rule_table`,
`metric_apply_to`, `quality_rule_table`, `spatial_rule_table`,
`color_profile_table`, and `direction_reference`. These are first-class UI/API
contracts, not JSON blobs.

## Live Authoring Contract

Runtime nodes that describe visual geometry should be authorable against a real
channel frame when the designer is opened from channel detail. The UI must not
force operators to type raw coordinates for camera-specific geometry when an
on-screen editor can produce the same config.

Current live authoring behavior:

- `preprocessing.crop` exposes a crop rectangle editor and writes pixel
  `x`, `y`, `width`, and `height` config values.
- `roi` property fields render an ROI rectangle editor and write normalized
  `{ label, x, y, width, height }` entries.
- `zone_list` property fields render a zone rectangle editor and write
  normalized `{ label, x, y, width, height }` entries.
- `direction_reference` property fields render a line editor and write
  normalized `{ label, start, end }` entries for operator-drawn reference
  directions.
- `computer_vision.color_object_segmentation` uses `color_profile_table` for
  HSV threshold profiles and `zone_list` for operator-drawn inspection fields.
- `analysis.object_spatial_signal` uses `zone_list` for operator-drawn fields
  and `spatial_rule_table` for object relation rules. Distance and approaching
  thresholds are frame-relative percentages, not pixel values.
- Draft preview runs the current unsaved graph against the channel's latest
  live/test frame and returns the same live-view state contract used by the
  realtime runtime.

Node authors should prefer these structured property types for visual geometry:

- crop-like nodes should expose pixel crop fields or an equivalent structured
  crop contract
- ROI extractors should use `roi`
- region filters should use `zone_list`
- edge, coordinate-frame, and measurement nodes should emit overlay primitives
  (`line`, `axis`, `box`, `polygon`, `measurement`, `mask`, or `heatmap`) so
  draft preview can render the result in context

The preview path is an authoring tool. It is allowed to execute the draft graph
against the current frame, but it must not be the publication contract. Saved
pipeline versions, validation reports, and published runtime execution remain
the durable source of truth.

## HTTP Trigger Input Node

`trigger.http_put`:

- is available in trigger runtime mode
- produces a `trigger` execution-context payload
- defines method, currently `PUT`
- defines auth mode/scope binding
- defines timeout behavior
- may define an accepted request schema
- controls endpoint visibility/enablement
- derives endpoint metadata only when the containing pipeline version is
  published/deployed

The endpoint path is generated from the published pipeline identity and trigger
node configuration. A constrained operator slug may be accepted by the node
config, but raw database IDs are not required in the URL.

## Channel Frame Input Node

`source.channel_frame`:

- is the canonical source node for channel-bound realtime, trigger, and
  interval execution
- does not ask operators to select or type a channel inside the pipeline
- consumes an optional `trigger` payload from `trigger.http_put` or
  `trigger.interval`
- outputs a `frame` payload and may pass through trigger context
- receives the frame from the runtime execution context for the channel that
  applies the pipeline

The channel owns source type and source configuration. For HTTP trigger
execution, runtime captures a frame from the endpoint's channel using the
channel source policy, then supplies that frame to `source.channel_frame`.
`source.channel_snapshot` remains a legacy compatibility node only.

## Result Output Node

`output.result`:

- consumes typed result payloads
- maps source fields to output keys through a structured form
- supports required/optional mapping semantics
- determines the channel-visible pipeline result
- determines the public HTTP response body for trigger execution
- marks realtime executions for result persistence
- is required for a triggerable pipeline to publish

Operators configure result mappings with fields, not raw code. The runtime
persists the final mapped result in result history and returns it as the HTTP
response body for trigger executions.

The designer derives selectable mapping fields from the connected upstream node
and known result contracts. Operators choose a source field and an output name;
manual field paths remain possible for plugin-provided fields. `wrap_key` is
only a response-shaping option: empty returns mapped fields at the top level,
while a value such as `result` nests the mapped object under that key.

`output.http_trigger_response`, `output.persisted_result`, and
`output.internal_event` remain loadable for existing versions but are hidden
from the normal palette. New pipelines should use `output.result` for business
results, plus visual/capture outputs only when those workflows are needed.

## Siemens SPS Write Output Node

`output.siemens_sps_write`:

- display name: `Write to Siemens SPS`
- category: existing `output` category, palette group `Industrial / PLC`
- consumes one `result` input
- has no normal downstream output ports
- emits a typed runtime status payload `siemens_sps_write_status`
- is backed by `cv_runtime.siemens_sps:output_siemens_sps_write`

The node is a terminal side-effect output node. It writes selected upstream
result fields to Siemens S7/SPS DB addresses through a mockable S7 client
abstraction. It is not a connector-only settings page and does not require
operators to type internal IDs.

Configuration is structured:

- `enabled`, `dry_run`, and `realtime_min_interval_ms`
- inline `connection`: display name, host, port default `102`, rack, slot,
  connection/write timeouts, reconnect policy, and reuse policy
- `mappings`: enabled rows with source selector, DB target, data type,
  conversion, fallback, optional condition, and failure behavior
- node-level `failure_behavior`

Supported data types are `BOOL`, `BYTE`, `WORD`, `DWORD`, `INT`, `DINT`, and
`REAL`. v1 exposes only DB area writes; `STRING`, M/I/Q areas, and other
fieldbus nodes need separate implementation and validation before they can
appear in the palette.

Validation blocks publish for invalid host/port/rack/slot/timeouts, invalid DB
number/offset/bit offset, unsupported data type, unknown non-advanced source
fields, missing enum mappings, enum/default values outside the target type
range, BOOL targets without bit offset, and active realtime writes without a
safe throttle. It warns when an enabled, non-dry-run industrial side effect is
present.

Runtime execution resolves the configured source fields, applies conversion,
validates Siemens type/range, enforces dry-run/preview/environment write gates,
then performs ordered sequential writes. Validation and authoring preview never
write. Unknown enum values and conversion failures do not silently write `0`;
they follow explicit fallback/failure behavior.

## Triggerable Pipeline Validation

A pipeline with `trigger.http_put` is triggerable only if validation proves:

- exactly one HTTP Trigger Input ingress is present
- one Channel frame input node is reachable from the trigger ingress
- the published pipeline is applied to a channel with a valid source
- at least one Result Output node is reachable from the processing graph
- no ambiguous half-triggerable state exists

Realtime, trigger, and interval execution paths remain separate, but they share
the channel-owned frame source contract. `source.channel_frame` consumes frames
supplied by the active execution path: live frames for realtime, one captured
frame for HTTP trigger, and periodic captured frames for interval execution.

## Display Output Node

`output.display_output`:

- consumes a `frame`
- optionally consumes object/result context for layer declarations
- emits a runtime display selection payload
- chooses the live-view display image context from the connected frame
- supports enriched-object labels
- is available for realtime and interval visual workflows

Safe default behavior remains available for simple one-path pipelines, but
multi-branch visual pipelines are not publishable without an explicit display
output.

## Current Fan-Out/Fan-In Execution

The runtime graph executor supports true fan-out and fan-in for normal
pipeline execution:

- one output port can feed multiple downstream inputs
- independent branches can consume the same `frame` / image context without
  requiring duplicate source nodes
- a node can require multiple inputs from different upstream branches
- topological execution waits until all required fan-in inputs are available
- the execution result envelope preserves typed outputs from each branch
- validation rejects missing required fan-in inputs and incompatible port pairs

The editor may use drag-wire edges or property-panel connection controls, but
it must preserve the same graph semantics. Operators must be able to connect
one Input Frame to both Computer Vision and Feature Detection branches, and
then connect CV-derived coordinate frames together with AI-derived feature
measurements into downstream analysis nodes.

## Future Fan-Out/Fan-In Nodes

ROI Fan-Out is not exposed as a supported operator node until it is executable
end to end. The current model prepares for it.

Future `ROI Fan-Out` contract:

- input: object set such as detections or segments
- input: source image context/frame
- output: one ROI image context per object
- output: ROI payloads with source object and source image context links
- behavior: preserve coordinate/projection lineage
- downstream: allow per-object classification or segmentation
- fan-in/merge: attach downstream enrichments back to source objects

Future fan-in/merge nodes must validate:

- every enrichment has a target object id
- every target object id belongs to a reachable upstream object set
- derived image contexts link back to the source object or source context
- projected overlays are valid for the selected display context

## Logical Model References

Inference nodes reference logical deployed models, not Triton internals:

- `model_family_slug`
- `target_path`
- `contract_major`
- `selection_policy`
- `required_task_type`
- optional explicit deployment release id

Validation checks the runtime model capability cache. A pipeline cannot publish
if an inference node points to a missing model capability or a model with an
incompatible task type.

## Anomaly Inference Node

`inference.anomaly` is a first-class inference node, not a classification node
with `good` and `bad` labels.

Contract:

- input: `frame`
- model reference: `required_task_type=anomaly`
- output: `anomaly`
- result fields: `anomaly_score`, `predicted_label`, `is_anomalous`,
  `threshold`, model metadata, and image dimensions
- overlay fields: `heatmap` primitive on layer `anomaly_heatmap`; optional
  `mask` primitive only when the deployed model declares mask support

The node calls the inference plane anomaly endpoint through the runtime
inference client. The runtime plane never synthesizes heatmaps. Heatmap data is
copied from the deployed anomaly model response and remains bound to the source
image context for display validation.

## Object Spatial Signal Node

`analysis.zone_detection` and `analysis.close_detection` are the operator-facing
spatial nodes. `analysis.object_spatial_signal` remains the generic hidden
compatibility node. The spatial nodes consume detections, segmentation outputs,
or compatible region sets, and can compare objects from one object set or from
two upstream object branches.

Supported rules:

- approaching object pairs
- overlapping object boxes or segmentation polygons
- object distance thresholds
- object counts inside or touching operator-drawn fields

The output is a normal `result` payload with `signal`, `match_count`, per-rule
status, and match details. Its `decision` mirrors the signal boolean, so Result
Output, Conditional Capture, and connector paths can consume it without a
special adapter. The nodes also emit an `overlay` payload with boxes, pair
lines, and fields for live preview.

Operators draw fields on the live stream frame through the `zone_list` editor.
Distance and approaching thresholds are configured as percentages of the frame
diagonal; overlap thresholds are configured as percentages of the smaller
object footprint. No operator workflow requires typing pixel coordinates.

`analysis.zone_detection` forwards an enriched object set with zone metadata,
so pipelines can stay linear:

```text
Channel Frame Input
-> Detection / Segmentation / CV Region Set
-> Zone Detection
-> Close Detection
-> Result Output
```

For approaching rules, the runtime keeps previous pair observations in node
state and matches pairs by labels and nearest centers. This works without a
tracker for simple streams; future tracking nodes can provide stronger
cross-frame identity through `track_id`.

See [object-spatial-signal-node.md](object-spatial-signal-node.md) for the
operator-facing contract and result shape.

## Color Object And Orientation Nodes

`computer_vision.color_object_segmentation` detects visually distinct objects
without AI inference. It thresholds one or more HSV color profiles, optionally
limits processing to operator-drawn fields, cleans the binary mask, extracts
external contours, can split compact touching same-color blobs with
distance-transform watershed, and emits a `region_set` with normalized boxes,
contour polygons, oriented rectangles, major/minor axis lengths, aspect ratio,
area, and orientation angle. It also emits per-label `counts` measurements and
keeps exclusive profile matching enabled by default so overlapping HSV ranges
do not double-count the same pixels.

`analysis.object_orientation_signal` consumes the resulting `region_set` or
compatible detection/segmentation regions. It evaluates only selected labels
and only objects inside or touching configured fields. The reference direction
can be drawn on the live frame, configured as an angle, or taken from an
`InspectionCoordinateFrame` axis. The node emits a normal `result` signal and
an overlay containing the reference direction, object axes, zones, and
violating objects.

Typical use for separated elongated objects:

```text
Channel Frame Input
-> Color Object Segmentation
-> Object Orientation Signal
-> Result Output
```

Use `analysis.object_spatial_signal` after color segmentation when separated
objects must also be checked for overlap or distance. If two same-color
objects merge into one contour, `analysis.object_orientation_signal` can flag
the merged shape through low aspect ratio, excessive width, or angle
deviation.

See [classical-color-orientation-nodes.md](classical-color-orientation-nodes.md)
for the operator-facing contract.

## Computer Vision Edge Nodes

Computer Vision nodes are built-in runtime nodes for deterministic classical CV
workloads. They run in the runtime plane using open-source OpenCV libraries and
must not call the inference plane. They are exposed in the normal palette only
when they have a real runnable handler, structured validation, and
operator-friendly configuration.

### CV Runtime Payload

- `cv_edges`: source image context, processed edge-mask frame context, edge
  pixel count, optional contours, optional candidate line metadata, debug
  overlay layers, timings, and provenance.

### Built-in CV Node Contracts

- `computer_vision.canny_edge_detection`
  - input: `frame:frame`, optional `mask:frame`
  - output: `edges:cv_edges`
  - properties: grayscale conversion (`auto`, `yes`, `no`), blur kernel size,
    lower and upper Canny thresholds, aperture size, L2 gradient toggle, and
    output mode (`edge_mask`, `edge_contours`, or `both`)
  - validation: input image must decode, thresholds must be ordered and
    non-negative, blur/aperture settings must be supported, optional mask must
    match image dimensions
  - overlays: optional `cv_edge_mask` and `cv_edge_contours` debug layers
- `computer_vision.reference_edge_from_cv_edges`
  - input: `edges:cv_edges`
  - output: `reference_edges:reference_edges`
  - properties: line extraction method, minimum line length, angle tolerance,
    and structured per-role line selection rules
  - selection roles: `start_edge`, `side_edge`, `left_edge`, `right_edge`, and
    `custom`
  - ranking/filtering: angle range, region preference, nearest image side,
    longest/strongest line scoring, and confidence score
  - validation: required roles must be configured and found at runtime; debug
    metadata reports candidate and selected lines

`ReferenceEdge` objects emitted by `computer_vision.reference_edge_from_cv_edges`
use the same contract as `analysis.reference_edge_from_features`. Downstream
nodes such as Coordinate Frame From Two Edges must consume the contract, not the
upstream extraction method.

## Board Measurement Nodes

These nodes are built-in runtime analysis nodes. They extend the result/object
contract without changing the inference plane. The existing
`inference.detection` / Feature Detection node remains the ML inference step;
the nodes below consume its detections and object references. Classical CV can
provide reference edges in parallel with Feature Detection, but it does not
replace AI feature detection for knots, cracks, resin pockets, and similar
semantic feature classes.

### Runtime Payloads

- `reference_edges`: list of `ReferenceEdge` objects with edge id, source image
  context, pixel line geometry, confidence, role, source metadata, and
  provenance.
- `coordinate_frame`: one `InspectionCoordinateFrame` with origin,
  longitudinal/transverse axes, confidence, optional inspected object/camera/view
  ids, source metadata, and provenance.
- `feature_measurements`: measured feature values plus enriched feature
  metadata. Bases are `bbox_width`, `bbox_height`, `approximate_diameter`,
  `polygon_length`, `mask_area`, `axis_projected_width`, and
  `axis_projected_length`.
- `clearance_zones`: blocked longitudinal intervals with pixel and optional
  metric values, clearance amount, rule metadata, severity, and category.
- `free_region`: first selected clear interval with pixel and optional metric
  distance from the coordinate-frame origin.
- `quality_decision`: pass/fail/warning decision with evaluated rules,
  messages, and selected output values.

### Built-in Node Contracts

- `analysis.reference_edge_from_features`
  - input: `features:detections` (also accepts compatible region sets)
  - output: `reference_edges:reference_edges`
  - properties: structured edge-role mapping table, minimum confidence,
    extraction mode, default bbox side
- `analysis.coordinate_frame_from_edges`
  - input: `reference_edges:reference_edges`
  - output: `coordinate_frame:coordinate_frame`
  - properties: start role, direction role, origin rule, axis rule, orientation,
    confidence aggregation
- `analysis.feature_measurement`
  - input: `features:detections`, optional `coordinate_frame`
  - output: `feature_measurements:feature_measurements`
  - properties: structured measurement rule table, fallback behavior, unit mode
- `analysis.clearance_zone_generator`
  - inputs: `measurements:feature_measurements`, `coordinate_frame`
  - output: `clearance_zones:clearance_zones`
  - properties: structured clearance rule table and confidence threshold
- `analysis.first_free_region_finder`
  - inputs: `coordinate_frame`, `clearance_zones`
  - output: `free_region:free_region`
  - properties: search direction, start offset, minimum free length, optional
    end limit, outside-board behavior, merge setting
- `analysis.pixel_to_metric`
  - optional inputs: free region, measurements, clearance zones, frame, edges,
    and quality decision
  - outputs: metric-enriched typed payloads and a normalized `result`
  - properties: positive `mm_per_pixel` and structured apply-to checkboxes
- `analysis.quality_rule_evaluation`
  - optional inputs: measurements and free region
  - outputs: `quality_decision` and normalized `result`
  - properties: structured rules for feature count, feature size, total
    length/area, and free-region distance

The editor must expose these through structured controls, not JSON text areas.
Normal operator flow must not require manual image context ids, object ids, or
channel ids. The node palette groups them under Reference / Geometry,
Measurement, and Rules / Decision.

### Board Pipeline Example

AI-only edge extraction remains supported:

```text
Channel Frame Input
-> Feature Detection
-> Reference Edge From Features
-> Coordinate Frame From Two Edges
-> Feature Measurement
-> Clearance Zone Generator
-> First Free Region Finder
-> Pixel To Metric Conversion
-> Overlay Output
-> Result Output
```

The board/free-region workflow can also split the same input frame into
parallel CV and AI branches:

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
-> Overlay Output
-> Result Output
```

For a board with `board_start_edge`, `board_side_edge`, and a `knot`, configure
either the edge mapping table or the CV line selection table to produce
`start_edge` and `side_edge`, measure the knot with `bbox_width` and
axis-projected dimensions, generate a clearance interval using multiplier
`1.0`, search from offset `0`, and map `distance_mm` in Result Output.

### Operator Configuration Guide

Feature Detection edge path:

1. Add the existing Feature Detection node and select a deployed detection
   model that emits board edge classes and feature classes.
2. Add Reference Edge From Features. In the mapping table, map the detected
   start-edge class to `start_edge` and the detected side/direction class to
   `side_edge`; choose the bbox side that represents the line.
3. Add Coordinate Frame From Two Edges. Use `intersection_with_side_edge` for
   boards where the start and side edges meet in the image, or
   `center_of_start_edge` when the side edge is used only for direction.
4. Add Feature Measurement. For `knot`, `crack`, `resin_pocket`, and other
   feature classes, choose the measurement bases that downstream rules need.
5. Add Clearance Zone Generator. Configure one row per feature type, including
   measurement basis, multiplier, minimum clearance, before/after flags, and
   whether overlapping intervals should merge.
6. Add First Free Region Finder. Configure search direction, start offset,
   minimum free length, and optional board end limit.
7. Add Pixel To Metric Conversion. Enter a positive `mm_per_pixel` calibration
   and select which typed outputs should receive metric values.
8. Optionally add Quality Rule Evaluation for count, size, total length/area,
   or free-region-distance thresholds.
9. Add Overlay Output and Result Output. Map named result
   fields such as `distance_mm`; do not enter internal ids.

Computer Vision edge path:

1. Connect the same Channel Frame Input to Canny Edge Detection and Feature
   Detection.
2. Configure Canny thresholds and blur/aperture values from the structured
   panel. Use CV edges when the board boundary is visually crisp and a
   deterministic image-edge method is preferred over a learned edge class.
3. Add Reference Edge From CV Edges. Configure per-role line-selection rules
   for `start_edge` and `side_edge`, using angle, region, side proximity, and
   minimum length instead of internal line ids.
4. Feed those reference edges into Coordinate Frame From Two Edges.
5. Feed Feature Detection outputs into Feature Measurement, and feed the
   coordinate frame from the CV branch into measurement and clearance nodes.
6. Add Overlay Output with debug layers available for Canny mask, candidate
   lines, selected reference edges, axes, measurements, clearance zones, and
   first free region.

### Extension Guidance

Future multi-camera quality scanners should extend these payloads rather than
replace them. Add camera/view/workpiece identity, homography, tracking, and
grade aggregation as separate nodes with explicit contracts. Perspective
correction must not be hidden inside the current two-edge coordinate-frame
node.

## Validation Rules

Validation blocks publishing when:

- the graph has no nodes
- there is not exactly one source node
- there is no output or capture node
- the inferred execution kind from trigger/source nodes is inconsistent
- a node type is unknown or used in an unsupported execution kind
- required node properties are missing
- inline secret-looking properties are present
- required input ports are not connected
- an edge uses unknown or incompatible ports
- the graph contains a cycle
- an inference model reference is missing or incompatible
- overlays have no reachable image context
- overlay output payloads omit frame/timestamp, display context, coordinate
  system, or resolution metadata required for live stream synchronization
- non-debug overlay outputs serialize full image frames or raw full-frame masks
  instead of efficient overlay primitives, RLE/polygon masks, or compressed
  texture payloads
- multiple visual branches exist without `output.display_output`
- more than one live-view display output is present
- display output does not receive a frame context
- Canny CV edge detection has missing frame input or invalid threshold,
  blur-kernel, aperture, output-mode, or optional mask configuration
- Reference Edge From CV Edges has missing `cv_edges` input, unsupported line
  extraction method, missing role configuration, or ambiguous/invalid
  line-selection rules
- board measurement nodes are missing the existing Feature Detection input
- reference-edge mappings do not produce required start and side roles
- Coordinate Frame From Two Edges does not receive compatible `ReferenceEdge`
  roles regardless of whether they came from Feature Detection or CV edges
- fan-in nodes are missing any required input from parallel branches
- clearance rules reference measurement bases not produced upstream
- metric conversion uses a missing or non-positive `mm_per_pixel`
- response mappings request fields that no reachable upstream node produces
- merge/enrichment nodes lose source object linkage
- future fan-out/fan-in contracts are wired to incompatible context/object data

Pipeline versions still persist an execution `mode` for API compatibility and
runtime routing, but normal operator configuration does not expose a separate
mode selector. The effective mode is inferred from ingress nodes:
`trigger.http_put` -> trigger, `trigger.interval` -> interval, otherwise
realtime.

Warnings are allowed for non-terminal nodes whose output is unused.

## Versioning

Pipeline definitions are reusable named containers. Pipeline versions are
immutable snapshots of the graph. Saving creates a new version; publishing
validates the saved version, marks it `published`, and supersedes the previous
published version for the same definition.

Templates are normal pipeline definitions with `template=true`. Instantiating a
template creates a new reusable definition and copies the template's latest
graph as version 1.

## Extension Pattern

To add a node plugin:

1. Register a node type with category, ports, supported modes, defaults,
   property schema, and visual/object compatibility constraints.
2. Define whether the node preserves or transforms image contexts.
3. Define whether it produces or consumes object references or enrichments.
4. Implement the runtime handler referenced by `runtime_handler_ref`.
5. Add compatibility constraints for task type, target path, accepted port
   aliases, coordinate projection, and merge/enrichment requirements.
6. Add validation tests for mode, ports, visual context behavior, object
   lineage, required properties, and model capability behavior.
7. Expose any new property field type in the UI property panel only if the
   existing schema fields are insufficient.

Extensions should not require changes across all runtime services. The registry
and property schema are the stable contract between definition, UI, and
execution.
