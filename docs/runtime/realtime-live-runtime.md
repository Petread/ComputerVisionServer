# Realtime Live Runtime

Authoritative references:

- `docs/architecture/cv-platform-spec.md`
- `docs/runtime/vision-runtime-spec.md`
- `docs/runtime/vision-runtime-roadmap.md`
- `docs/runtime/runtime-lifecycle-core.md`

## Purpose

W2 completes the realtime execution path for live channels. A started realtime
channel now owns a supervised `runtime_execution_id`, a per-channel realtime
executor loop, a bounded frame buffer, live runtime metrics, and a latest
context-aware frame/display/overlay state document for operator live view.

This remains runtime-plane behavior. The admin UI does not run the stream, the
inference gateway remains the inference plane, and trigger/training paths are
not merged with realtime.

## Architecture

1. Stream and inference are requested independently from the browser.
2. `camera-media` opens the source once and writes native frames into a
   four-slot, latest-wins shared-memory ring.
3. Its NVENC worker consumes the ring independently and publishes one H.264
   path to MediaMTX over internal RTSP/TCP.
4. `realtime-executor` consumes the same ring, binds the published pipeline,
   and calls the realtime inference gateway without owning camera or publisher.
5. The browser reads the single WHEP endpoint returned by
   `GET /api/v1/runtime/channels/{channel_id}/streams`.
6. Inference overlay metadata arrives through SSE and is drawn on a canvas over
   the WebRTC video.

Overlay and result consumers must bind to `runtime_execution_id`. Channel ID is
the operator navigation key; runtime instance ID is the execution key.

There is no HLS, MJPEG, snapshot-player, UDP-ICE, or CPU-encoder fallback.
Reconnects only retry the same TCP/WHEP path. Stream authorization is disabled
by default and is reported as `unauthenticated_lan` until explicitly enabled.

## RTSP Handling

RTSP sources use the open-source OpenCV `VideoCapture` adapter when available.
The adapter:

- rejects RTSP URLs with inline credentials
- accepts RTSP URL or host/path configuration with credential secret references
- receives credentials from the channel UI as request-only data; the API writes
  managed secret files and stores only `secret_refs`
- strips credentialed RTSP URLs into sanitized config plus managed secret refs
  for legacy or pasted camera URLs
- sets the capture buffer to one frame where OpenCV supports it
- applies open/read timeout settings where OpenCV exposes them
- reconnects after failed open, failed read, or read timeout
- reports `connected`, reconnect count, source error count, status text, and
  last error through runtime health
- redacts credential-bearing source metadata before exposing it to live view

If OpenCV is missing, the channel becomes degraded with an operator-visible
source error rather than pretending to stream.

## Teledyne FLIR Blackfly Handling

FLIR Blackfly/Blackfly S GigE cameras are not RTSP streams. They are
GigE Vision/GenICam devices and use the FLIR/Teledyne Spinnaker stack.

The managed `aravis_gige` RTSP bridge remains a legacy diagnostic/edge path.
The server `camera-media` service rejects it because its x264 encode plus RTSP
decode would violate the single-NVENC-stream architecture. Productive server
channels require the direct `flir_blackfly` GenTL adapter or an external RTSP
source.

Optional bridge components:

- `libaravis-0.8-0`, `aravis-tools-cli`, and the `aravissrc` GStreamer plugin
- GStreamer RTSP server bindings and H.264 encoder plugins
- systemd template unit `ybb-aravis-rtsp@.service`
- camera instance config under `/etc/ybb-inferenzserver/aravis-rtsp/<name>.env`

Runtime source type: `aravis_gige`

Do not select `GigE Vision (Aravis)` for a server channel that uses the new
livestream architecture.

Runtime source type: `flir_blackfly`

Required runtime libraries/components:

- Teledyne Spinnaker SDK on the executor host. The SDK provides the GigE Vision
  driver, SpinView tools, camera discovery/configuration utilities, and the
  Spinnaker GenTL producer.
- Spinnaker GenTL producer `.cti` file discoverable through
  `GENICAM_GENTL64_PATH`, or explicitly configured as `cti_path`/`cti_paths`.
- Python `harvesters` package as the GenTL consumer used by
  `realtime-executor`, plus its `genicam`/`numpy` dependencies.
- OpenCV remains used for pixel-format conversion and JPEG encoding in the
  live runtime.

The source config supports `serial_number`, `user_defined_name`, `device_id`,
`id_`, or `ip_address` to select the camera. `serial_number` is preferred for
stable production configuration. Optional keys include `pixel_format`,
`exposure_time_us`, `gain_db`, `fetch_timeout_seconds`, `reconnect_seconds`,
and `features` for explicit GenICam node values.

The official PySpin wrapper is still the vendor-native Python API, but it is
installed from the Spinnaker download package, not PyPI, and must match the
host OS and Python version. The platform uses GenTL/Harvesters for the runtime
adapter so the normal Python 3.11/3.12 service runtime can consume the
Spinnaker producer without adding a proprietary wheel to the repository.

## Basler Pylon Handling

Basler cameras use the native Pylon/GenICam source type `basler_pylon`. The
runtime supports direct camera selection, acquisition/trigger settings, pixel
format, ROI, exposure/gain, GigE packet tuning, bounded grab buffers, and
stream-grabber frame retention.

Server-side `camera-media` always normalizes `auto` to `pylon_direct` and rejects
the Aravis RTSP bridge. This prevents a hidden x264 encode and RTSP decode before
the required NVENC publisher.

GigE configuration is validated against `link_speed_mbps`. Known pixel formats
are converted into an estimated raw payload:

`width * height * bits_per_pixel * fps`

Configurations at or above 80% of link capacity are rejected because
GenICam/GigE Vision packet and protocol overhead still needs headroom. For
example, `1900x1200 RGB8 at 30 fps` is about `1642 Mbit/s` before overhead and
cannot run on a 1-Gbit/s link. Prefer Bayer/Mono8, reduce ROI or camera FPS, or
provision a faster end-to-end NIC/switch path. The channel UI calculates the
same estimate before saving.

Continuous acquisition should normally use `TriggerMode=Off`. A continuous
channel combined with a software trigger is reported as a configuration
warning because synchronous per-frame triggering can stall live acquisition.

## Low-Latency Policy

Realtime prioritizes current frames over perfect retention.

- `runtime.realtime_frame_buffer_size` defaults to `2`.
- `runtime.realtime_drop_policy` defaults to `drop_oldest`.
- The worker pops the latest available frame and discards older buffered frames.
- Pipelines with inference nodes use `runtime.realtime_pipeline_max_inflight`,
  which defaults to `1`, unless a channel is deliberately tuned for parallel
  execution. Stateful nodes always require serialized execution. When all
  inference slots are busy, newer frames stay in the bounded buffer and older
  frames are dropped instead of queueing more inference work.
- Realtime inference nodes inherit `inference_limits.realtime_timeout_ms` when
  no node-specific timeout is configured. The executor keeps a small transport
  margin around that budget, but does not wait for multi-second gateway calls by
  default.
- Raw stream publication is fed directly from source acquisition and therefore
  remains fresh while inference is busy. Busy frames update drop telemetry but
  do not advance the latest completed-result sequence. A completed in-flight
  result can still be published even when newer source frames were discarded;
  this prevents permanent result starvation.
- Executor heartbeats are emitted independently from completed inference
  results, and the first processed result or inference drop is persisted
  immediately. A slow or dropped frame therefore does not make the runtime
  instance stale or leave initial diagnostics several seconds behind.
- `sample_every_n` and `max_fps` in source configuration throttle processing
  before inference. Operators configure these as `Sample every N` and
  `Max processing FPS` on the channel form.
- Capture/result persistence is not allowed to block the live inference path.
  The live worker only records required runtime output and latest state; durable
  capture/connector delivery belongs to later workstreams.

Overload behavior is explicit: frame drops increase `drop_count` and
`drop_rate_percent`; operators see lower processed FPS and higher latency.
The realtime executor waits briefly and retries when the inference gateway
returns overload, bounded by the realtime inference timeout budget. Retryable
inference overload or timeout on Classification, Detection, Segmentation, and
Anomaly nodes is treated as a dropped realtime frame only after that budget is
spent. The live view may stutter or temporarily lose overlays, but the channel
must remain running while heartbeats and source health are good. Terminal
pipeline or model configuration errors are still persisted as failed runtime
results.

Realtime image requests use binary HTTP bodies when the gateway supports them.
This removes the base64 copy and expansion from the normal executor-to-gateway
path. Gateway admission is bounded globally and per deployment release; the
effective per-release limit is capped by the corresponding global limit, and
realtime callers receive a short fair-wait window before an explicit overload
response.

When source FPS is higher than processed FPS but latency remains low and the
source has no reconnects, a high drop rate usually means the low-latency buffer
is discarding older frames as designed. Cap processing FPS or sample frames when
the operator wants quieter metrics at the same live-view freshness.

## Overlay State

The latest live state is persisted under the runtime overlay storage root as:

- `channel_id`

## GPU Serving Checks

Production realtime inference must be served by Triton, not by the local ONNX
fallback. Before diagnosing model latency, confirm:

- `ybb-triton-rt.service` is active, or `triton-rt` is running in Compose.
- Triton readiness succeeds on the configured realtime HTTP URL.
- The realtime Triton repository contains loaded model versions.
- Production settings keep `training.allow_onnx_deployment_fallback` disabled.

If Triton is missing or down, the inference gateway reports `triton_rt` as
down and realtime channels should be treated as serving-unhealthy instead of
silently running CPU inference.
- `runtime_instance_id`
- `actual_state`
- `display_context_id`
- `display_context`
- `frame` with base64 image, dimensions, timestamps, sequence, and image context
- `overlay` primitives bound to image contexts and/or object references
- `layers_available`
- `metrics`
- `source_health`
- `recovery_hint`

Overlay primitives are transport-level objects:

- `box` for detections and ROI-like regions
- `polygon` for segmentation
- `point` and `motion` for movement/position indicators
- `zone` and `roi` for configured regions
- `line`, `axis`, and `arrow` for detected edges, reference edges, and
  coordinate frames
- `mask` and `heatmap` for processed visual layers such as Canny edge masks
- `measurement` and `label` for calculated values

The live view can toggle layers without changing runtime execution.

If a pipeline has one clear visual path, the executor uses the latest image
context on that path as the display context. If multiple visual branches exist,
pipeline validation requires `output.display_output` so the display context is
chosen explicitly.

## Runtime Metrics

Realtime heartbeats publish:

- source FPS over a rolling 10-second window
- processed FPS over a rolling 10-second window
- latest latency
- p50/p95/p99 latency over the recent window
- frame count
- dropped frame count and rolling drop rate
- reconnect count
- source error count
- buffer depth
- last frame time

Channel cards and channel details show the compact subset; the live view shows
runtime state, FPS, drop rate, p95 latency, reconnect count, and last frame time.

## Failure And Recovery

- Source disconnected: runtime stays supervised but health becomes `degraded`;
  reconnect attempts continue and live view shows the source status and hint.
- Worker crash: the supervisor drops dead handles from its active set; reconcile
  can relaunch recoverable desired-running runtime instances.
- Stale heartbeat: W1 lifecycle logic marks the runtime `degraded`, then
  `failed` after the failure threshold.
- Stale `draining`/`stopping` execution: after
  `runtime.executor_stop_grace_period_seconds`, reconcile finalizes it as
  stopped, clears the active runtime pointer, and records forced-stop recovery
  metadata.
- Host or container restart: reconcile treats a desired-running channel with no
  active runtime instance, or only a terminal active instance, as needing a fresh
  durable `start` control command. Operators do not clear internal execution IDs
  manually.
- Stop: runtime-control requests stop, signals the executor, persists the
  stopped state, and live view no longer displays the old frame as active.

Operators should restart a channel only after checking the source status and
pipeline assignment. Repeated reconnects usually indicate camera/network
availability, RTSP credential/configuration problems, or missing Spinnaker
GenTL setup for FLIR Blackfly sources.

## Operator Live View

Open `Channels`, select a realtime channel, start the runtime, and open live
view when the state is `running` or `degraded`. The panel renders the latest
actual frame only when the runtime has published one for the active runtime
instance. It does not show stale frames for stopped channels.

Layer toggles affect only the browser rendering. Runtime processing, metrics,
and overlays continue to be produced by the executor.

## API Surface

- Runtime admin:
  `GET /api/v1/runtime/channels/{channel_id}/live-view/latest`
- Runtime admin authoring preview:
  `POST /api/v1/runtime/channels/{channel_id}/pipeline-preview`
- Overlay service:
  `GET /api/v1/runtime/channels/{channel_id}/overlay/latest`

All return `RuntimeLiveViewResponse`. The preview endpoint accepts an unsaved
`RuntimePipelineGraph`, executes it against the channel's latest frame, and
returns a live-view-shaped draft state for operator authoring. Overlay service
access is machine authenticated when API keys are enabled and requires
`runtime.overlay.read`.

## Later Workstreams

W5 manual live capture should capture from this runtime-bound latest frame
state. W4 result views and W6 connector delivery should continue to use result
records tied to `runtime_execution_id`; they should not infer live execution
from a channel ID alone.
