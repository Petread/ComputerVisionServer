# Channel Operator Workflows

This page documents the operator-facing channel management workflow for the runtime vision layer.

## Channel Overview

Open **Channels** from the main navigation.

Operators use this page to:

- search channels by name, technical place, source, or selected runtime pipeline
- filter by source type, mode, status, and technical place
- scan compact cards with traffic-light status, source summary, pipeline, output, capture, last activity, and live metrics
- open a channel detail page without copying internal IDs
- request start, stop, and live view actions
- see which triggerable pipelines are applied to the channel through
  channel-bound trigger endpoints

## Create A Channel

1. Enter a clear channel name.
2. Set the technical place, such as `press-line-1` or `packing-station`.
3. Choose the source type.
4. Enter the source address in the operator field:
   - RTSP stream: camera stream URL
   - FLIR Blackfly: camera serial number, user-defined name, device id, or IP
   - Snapshot: HTTP(S) snapshot URL
   - Trigger capture: camera label or device hint
   - Test file: choose an image or short video with the browser file picker
5. Choose live runtime behavior when the channel needs continuous processing.
6. Choose a published runtime pipeline template from the selector when
   applicable. The platform creates a channel-scoped pipeline instance from the
   selected template.
7. Choose authoring label projects when pipeline configuration should offer
   channel-specific class/label suggestions.
8. Add short output and capture summaries.
9. Create the channel.

Inline secrets are rejected. Use secret references in service configuration instead of storing passwords or tokens in source configuration. Test files are stored as managed runtime artifacts and are intended for pipeline tests and configuration work; assigned realtime and triggerable pipelines receive them through `Channel frame input` like a camera frame.

## Manage Runtime State

Start and stop buttons create audited runtime commands. They do not execute live
or trigger processing inside the runtime admin API process.

The runtime admin API only persists the desired state and command intent. Runtime execution remains the responsibility of the runtime orchestrator and executor services.

## Channel Detail

The detail page shows:

- master data and technical place
- source type and source summary
- live runtime state and selected live/default runtime pipeline when applicable
- output and capture summaries
- current health and compact performance metrics
- runtime-bound live view for realtime channels
- triggerable pipelines applied to this channel
- latest result status when available

Operators can edit channel setup fields from the detail page. Raw internal database identifiers are intentionally not part of the normal workflow.

## Pipeline Templates And Channel Instances

Runtime pipelines shown under **Runtime Pipelines** are templates. Publishing a
template creates or updates the template version only; it does not automatically
move any channel to that version.

Each channel assignment uses its own channel-scoped pipeline definition and
published version. When an operator assigns a template to a channel, the runtime
admin service creates or replaces that channel instance by copying the template
graph, validation report, mode, and node contract versions. The channel then
points at the instance version, not at the shared template version.

This split keeps camera-specific geometry, label choices, PLC mappings, and
trigger endpoints local to the channel. Starting a channel requires a published
channel instance. A template version cannot be started directly by a channel.

## Triggerable Pipelines

Operators configure trigger behavior in the runtime pipeline designer, not from
a separate channel trigger editor.

The normal triggerable pipeline shape is:

```text
HTTP Trigger Input -> Channel frame input -> processing -> Response Output
```

- `HTTP Trigger Input` configures method, auth, timeout, request schema, and
  endpoint visibility.
- `Channel frame input` receives the frame from the channel that applies the
  pipeline. Operators do not type internal channel IDs inside the pipeline.
- `Response Output` maps typed result fields into the HTTP response body.

When the channel pipeline instance is published, runtime derives externally
callable endpoint metadata from the channel, pipeline identity, version, and
HTTP Trigger Input node. Pipeline/deployment detail shows endpoint URL, method,
auth requirement, timeout, readiness, and example request/response. Channel
detail shows the triggerable endpoints applied to that channel.

Endpoint readiness depends on pipeline publication/deployment, runtime service
readiness, auth validity, channel frame acquisition capability, and Response Output
mapping validity.

See [trigger-runtime-plc-contract.md](./trigger-runtime-plc-contract.md) for
the PLC/control-system HTTP contract.

## Realtime Live View

For realtime channels, the live view is backed by the active
`runtime_execution_id`. It renders a frame only after the realtime executor has
published a latest live state for the current runtime instance.

The live view shows:

- latest frame
- overlay primitives for detections, segmentation, ROIs, zones, motion, and calculated values
- layer toggles
- runtime state
- processed FPS
- drop rate
- p95 latency
- reconnect count
- source health
- last frame time

If the runtime is stopped or starting, the panel does not show the previous
runtime's frame as if it were still live. If the source disconnects, the channel
can remain supervised in `degraded` state while reconnect attempts continue.

See [realtime-live-runtime.md](./realtime-live-runtime.md) for RTSP and FLIR
Blackfly handling, overlay state, metrics, overload behavior, and
troubleshooting.

## Channel-Bound Pipeline Editing

The channel detail page embeds the runtime pipeline designer for the
channel-scoped pipeline instance currently assigned to the channel. This is
intentional: geometry-heavy pipelines are calibrated for a specific camera view,
lens, mounting angle, and technical place.

Operators edit and publish the pipeline in the channel context. The channel
supplies the current live frame, source metadata, selected authoring label
projects, extracted label names, and upstream result-field choices as authoring
context.

The embedded designer supports:

- crop windows drawn directly on the channel frame
- ROI lists drawn and removed directly on the channel frame
- zone lists drawn and removed directly on the channel frame
- dynamic class/label selections from the channel's authoring label projects
- dynamic result-field selections from connected upstream nodes
- draft preview execution against the latest channel frame before saving or
  publishing
- preview rendering for pipeline output frames, masks, edge candidates,
  reference edges, coordinate-frame axes, measurements, and configured
  authoring regions when their image context matches the displayed frame

The draft preview endpoint does not replace validation or publishing. It runs
the current browser graph as an authoring preview with the latest channel frame
and returns a `RuntimeLiveViewResponse` shape so the same live-view rendering
contracts apply. Validation remains the publish gate.

Publishing a channel instance rebinds only that owner channel. If the channel is
running, a restart command is queued so the runtime executor loads the new
published instance version. Publishing a template does not queue channel
restarts.

## Manual Live Capture

The live view includes a compact **Manual live capture** panel for operator
dataset collection during live operation.

Operators can:

- choose a Label Studio project from the platform-discovered project list
- capture the current full frame once
- start full-frame capture every configured number of seconds
- stop interval capture explicitly
- see state, last capture time, last result, and short success or error feedback

Manual live capture is separate from **Pipeline capture**. Pipeline capture
remains the automated capture-node path inside a published runtime pipeline.
Manual live capture is scoped to the live view and records separate manual
capture history for the channel.

See [manual-live-capture.md](./manual-live-capture.md) for API, state, and
failure behavior.

## Deleting Channels

Delete only stopped channels. If a channel has a running or transitioning runtime, stop it first and wait for the runtime state to settle before deletion.
