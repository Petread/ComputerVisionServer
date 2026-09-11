# Livestream Streaming Architecture

This document defines the only supported production livestream path.

```text
camera -> camera worker -> four-slot shared-memory ring
                              |-> GStreamer/NVENC -> RTSP/TCP -> MediaMTX -> WHEP/WebRTC via TURN/TCP -> browser
                              `-> realtime inference -> overlay metadata SSE -> browser canvas
```

## Ownership and failure boundaries

`camera-media` owns camera acquisition and stream publication. One capture process
opens each active source, and one optional encoder process publishes that channel.
The camera remains open while either stream or inference is requested. The realtime
executor only consumes the latest shared frame; it never opens the camera or owns a
video publisher.

The shared-memory ring has four slots and `latest wins` semantics. Capture never
waits for inference or the encoder. Inference overload may drop inference frames,
but it cannot interrupt capture, H.264 publication, or the browser PeerConnection.
Likewise, stopping Triton or the inference gateway must not stop video.

Stream and inference have independent desired and actual states. Stream state also
reports camera, encoder, MediaMTX publication, and browser-reader components. A
stale `camera-media` heartbeat is reported as recovering rather than ready.

## Camera profile

The camera worker publishes the negotiated source resolution and FPS without global
scaling or frame repetition. Inference `max_fps` and `sample_every_n` never throttle
the stream.

Basler production channels use direct Pylon acquisition. The legacy
Aravis-to-x264-to-RTSP bridge is rejected by `camera-media`, because it would add a
CPU encoder and a second decode/encode cycle. Width, height, FPS, pixel format, and
link speed are required for the Basler preflight. Profiles above 80 percent of the
configured link payload are rejected before capture starts. NVENC also requires
positive, even dimensions; no implicit resize is performed.

## Encoder

The only production encoder is GStreamer `nvcudah264enc`:

```text
appsrc -> leaky queue -> cudaupload -> cudaconvert(NV12)
       -> nvcudah264enc -> h264parse -> rtspclientsink(TCP)
```

`appsrc` is live and uses capture timestamps as PTS. Queues are bounded and leaky.
H.264 uses constrained baseline, zero latency, no B-frames, no lookahead, and a
one-second GOP. Bitrate is `width * height * fps * 0.12 bit/pixel`, clamped to
2-20 Mbit/s unless a channel-independent platform override is configured.

Ubuntu's PyGObject package belongs to system Python, while the application venv may
use another Python minor version. The service therefore supervises a dedicated
`/usr/bin/python3` GI worker. It reads frames directly from shared memory; image
payloads are never sent through a subprocess pipe. The ownership-safe ring copy is
wrapped directly in a `Gst.Buffer`, avoiding a second CPU copy.

Missing GI bindings, `cudaupload`, `cudaconvert`, `nvcudah264enc`, `h264parse`, or
`rtspclientsink` are permanent stream failures. There is no FFmpeg, x264, HLS,
MJPEG, snapshot-player, or alternate-protocol fallback. A transient encoder error
restarts only the same NVENC path with bounded exponential backoff.

## MediaMTX and browser

RTSP is internal ingest only. MediaMTX disables HLS and UDP ICE and exposes one
local ICE listener on port 8189. MediaMTX issues short-lived TURN credentials and
the browser uses the static TURN/TCP listener on port 3478. Interface-derived candidates are disabled;
the configured LAN host is the only advertised host in production.

The browser receives exactly one endpoint:

```text
https://<host>/stream-webrtc/channels/<channel-slug>/live/whep
```

The React player loads the `reader.js` supplied by the installed MediaMTX release.
It may reconnect to the same WHEP URL, but never changes protocol. Five browser
clients still consume the same MediaMTX publication and must not create additional
encoders.

## Overlay metadata

Inference emits the current overlay state without image bytes. Runtime Admin
streams those events to the browser with SSE. The canvas draws class-specific
colors, labels, and confidence over the WebRTC video. Events include frame sequence
and capture timestamp. Stale overlays expire, and stopping or losing inference
clears the canvas without touching video.

## Offline deployment checks

Windows hosts MediaMTX directly while coturn runs in WSL. The updater opens TCP
8554, 8889, 8189, 3478, and 9997 plus local ICE UDP 8189;
it creates no UDP or HLS firewall rule. WSL installs the offline GStreamer/GI
packages and runs all of these checks before reporting a successful update:

- system Python imports `gi.repository.Gst`;
- every required CUDA/NVENC GStreamer element exists;
- a real NVENC test pipeline encodes frames;
- the test H.264 stream publishes to MediaMTX over RTSP/TCP and can be read back;
- MediaMTX API and HTTPS WHEP reverse proxy are reachable.

Keep the GStreamer and MediaMTX checks enabled for production updates. The Triton
check is independent of video, but is required when inference is expected to work.

## Acceptance

- browser decoded FPS is at least 95 percent of measured camera FPS;
- first frame appears within three seconds;
- LAN glass-to-glass latency is at most 150 ms p50 and 250 ms p95;
- stopping inference services does not disconnect WebRTC;
- killing the encoder recovers the same path within five seconds;
- an eight-hour run has less than one percent stream drops and no CPU encoder;
- the streaming component averages no more than one logical CPU core.

These hardware criteria must be measured on the target server after installing the
release; repository tests cannot prove camera, driver, GPU, browser, or LAN timing.
