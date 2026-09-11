"""Standalone system-Python GStreamer/NVENC worker.

This module intentionally imports no application or venv packages. Ubuntu's
PyGObject binding is compiled for the distribution Python, while the platform
service venv can use a different Python minor release. Frames are consumed
directly from the shared-memory ring; status is the only file-based IPC.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import struct
import sys
import time
import uuid
from dataclasses import dataclass
from multiprocessing import resource_tracker, shared_memory
from pathlib import Path
from typing import Any

_MAGIC = b"YBBFRM01"
_GLOBAL_HEADER = struct.Struct("<8sIIQQ")
_SLOT_HEADER = struct.Struct("<QQQIIIIQQ")
_GLOBAL_HEADER_SIZE = 64
_SLOT_HEADER_SIZE = 64
_MANIFEST_VERSION = 1
_REQUIRED_ELEMENTS = (
    "appsrc",
    "queue",
    "cudaupload",
    "cudaconvert",
    "nvcudah264enc",
    "h264parse",
    "identity",
    "rtspclientsink",
)


class PreflightError(RuntimeError):
    pass


@dataclass
class RawFrame:
    sequence: int
    capture_monotonic_ns: int
    payload: bytes
    descriptor: dict[str, Any]
    skipped_frames: int = 0


class RingReader:
    def __init__(self, runtime_dir: Path, channel_id: str) -> None:
        self.manifest_path = runtime_dir / f"{_safe_name(channel_id)}.json"
        self.descriptor: dict[str, Any] | None = None
        self.shm: shared_memory.SharedMemory | None = None
        self.last_sequence = 0

    def latest(self) -> RawFrame | None:
        self._attach_current()
        if self.shm is None or self.descriptor is None:
            return None
        latest_sequence = struct.unpack_from("<Q", self.shm.buf, _GLOBAL_HEADER.size - 8)[0]
        if latest_sequence <= self.last_sequence:
            return None
        slot_count = int(self.descriptor["slot_count"])
        capacity = int(self.descriptor["slot_capacity"])
        slot_offset = _GLOBAL_HEADER_SIZE + (latest_sequence % slot_count) * (
            _SLOT_HEADER_SIZE + capacity
        )
        (
            begin_sequence,
            capture_monotonic_ns,
            _capture_wall_time_ns,
            width,
            height,
            channels,
            _stride,
            payload_size,
            end_sequence,
        ) = _SLOT_HEADER.unpack_from(self.shm.buf, slot_offset)
        if begin_sequence != latest_sequence or end_sequence != latest_sequence:
            return None
        if payload_size <= 0 or payload_size > capacity:
            return None
        if int(width) != int(self.descriptor["width"]):
            return None
        if int(height) != int(self.descriptor["height"]):
            return None
        if int(channels) != int(self.descriptor["channels"]):
            return None
        payload_offset = slot_offset + _SLOT_HEADER_SIZE
        payload = bytes(self.shm.buf[payload_offset : payload_offset + payload_size])
        if struct.unpack_from("<Q", self.shm.buf, slot_offset)[0] != latest_sequence:
            return None
        skipped_frames = (
            max(int(latest_sequence) - self.last_sequence - 1, 0)
            if self.last_sequence > 0
            else 0
        )
        self.last_sequence = int(latest_sequence)
        return RawFrame(
            sequence=int(latest_sequence),
            capture_monotonic_ns=int(capture_monotonic_ns),
            payload=payload,
            descriptor=self.descriptor,
            skipped_frames=skipped_frames,
        )

    def close(self) -> None:
        self._detach()

    def _attach_current(self) -> None:
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Shared frame ring manifest must be an object.")
        if int(payload.get("schema_version") or 0) != _MANIFEST_VERSION:
            raise ValueError("Shared frame ring manifest version is unsupported.")
        if str(payload.get("dtype")) != "uint8" or int(payload.get("channels") or 0) != 3:
            raise ValueError("NVENC publisher requires a contiguous BGR uint8 frame ring.")
        if self.descriptor is not None and payload.get("generation") == self.descriptor.get(
            "generation"
        ):
            return
        self._detach()
        shm = shared_memory.SharedMemory(name=str(payload["shared_memory_name"]), create=False)
        try:
            resource_tracker.unregister(shm._name, "shared_memory")
        except (AttributeError, KeyError):
            pass
        magic, version, slots, capacity, _sequence = _GLOBAL_HEADER.unpack_from(shm.buf, 0)
        if (
            magic != _MAGIC
            or version != _MANIFEST_VERSION
            or slots != int(payload["slot_count"])
            or capacity != int(payload["slot_capacity"])
        ):
            shm.close()
            raise ValueError("Shared frame ring header does not match its manifest.")
        self.shm = shm
        self.descriptor = payload
        self.last_sequence = 0

    def _detach(self) -> None:
        shm = self.shm
        self.shm = None
        self.descriptor = None
        self.last_sequence = 0
        if shm is not None:
            shm.close()


class NvencWorker:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stop = False
        self.reader = RingReader(Path(args.runtime_dir), args.channel_id)
        self.gst = _load_gst()
        _require_elements(self.gst, _REQUIRED_ELEMENTS)
        self.pipeline: Any | None = None
        self.appsrc: Any | None = None
        self.generation: str | None = None
        self.first_capture_ns: int | None = None
        self.input_events: collections.deque[float] = collections.deque(maxlen=1000)
        self.output_events: collections.deque[float] = collections.deque(maxlen=1000)
        self.frame_duration_ns = 0
        self.last_status_write = 0.0
        self.status: dict[str, Any] = {
            "ready": False,
            "input_fps": 0.0,
            "output_fps": 0.0,
            "submitted_frames": 0,
            "published_frames": 0,
            "dropped_frames": 0,
            "encoder_dropped_frames": 0,
            "last_frame_sequence": 0,
            "publication_latency_ms": None,
            "source_frame_age_ms": None,
            "error": None,
            "permanent_failure": False,
            "pid": os.getpid(),
        }

    def run(self) -> None:
        _install_signal_handlers(self)
        _write_status(Path(self.args.status_path), self.status)
        try:
            while not self.stop:
                self._raise_pipeline_error()
                try:
                    frame = self.reader.latest()
                except FileNotFoundError:
                    time.sleep(0.01)
                    continue
                if frame is None:
                    time.sleep(0.002)
                    continue
                generation = str(frame.descriptor["generation"])
                if self.pipeline is None or generation != self.generation:
                    self._stop_pipeline()
                    self._start_pipeline(frame.descriptor)
                    self.generation = generation
                started = time.perf_counter()
                self.status["dropped_frames"] += frame.skipped_frames
                self._push(frame)
                now = time.perf_counter()
                self.status["error"] = None
                self.status["submitted_frames"] += 1
                self.status["last_frame_sequence"] = frame.sequence
                self.status["publication_latency_ms"] = round((now - started) * 1000, 3)
                self.status["source_frame_age_ms"] = round(
                    max(time.monotonic_ns() - frame.capture_monotonic_ns, 0) / 1_000_000,
                    3,
                )
                self.input_events.append(now)
                self._refresh_rates(now)
                if now - self.last_status_write >= 0.2:
                    _write_status(Path(self.args.status_path), self.status)
                    self.last_status_write = now
        finally:
            self._stop_pipeline()
            self.reader.close()

    def _start_pipeline(self, descriptor: dict[str, Any]) -> None:
        width = int(descriptor["width"])
        height = int(descriptor["height"])
        if width <= 0 or height <= 0 or width % 2 or height % 2:
            raise PreflightError(
                f"NVENC requires positive even dimensions; received {width}x{height}."
            )
        configured_fps = max(float(descriptor.get("configured_fps") or 30.0), 0.1)
        bitrate = self.args.bitrate_kbps or _automatic_bitrate(
            width,
            height,
            configured_fps,
            self.args.auto_bitrate_bpp,
            self.args.min_bitrate_kbps,
            self.args.max_bitrate_kbps,
        )
        stream_path = _validated_stream_path(self.args.stream_path)
        publish_url = f"rtsp://{self.args.publish_host}:{self.args.rtsp_port}/{stream_path}"
        fps = max(round(configured_fps), 1)
        self.frame_duration_ns = max(int(round(1_000_000_000 / configured_fps)), 1)
        self.input_events.clear()
        self.output_events.clear()
        description = (
            "appsrc name=frames is-live=true block=false format=time do-timestamp=false "
            "max-buffers=2 leaky-type=downstream "
            f"caps=video/x-raw,format=BGR,width={width},height={height},framerate={fps}/1 ! "
            "queue name=encoder_queue leaky=downstream max-size-buffers=1 "
            "max-size-bytes=0 max-size-time=0 ! "
            "cudaupload ! cudaconvert ! video/x-raw(memory:CUDAMemory),format=NV12 ! "
            "nvcudah264enc preset=p1 tune=ultra-low-latency "
            "zero-reorder-delay=true b-frames=0 rc-lookahead=0 "
            f"gop-size={fps} bitrate={int(bitrate)} ! "
            "video/x-h264,profile=constrained-baseline ! h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "identity name=encoded_meter silent=true ! "
            f"rtspclientsink location={publish_url} protocols=tcp latency=0"
        )
        pipeline = self.gst.parse_launch(description)
        appsrc = pipeline.get_by_name("frames")
        encoder_queue = pipeline.get_by_name("encoder_queue")
        encoded_meter = pipeline.get_by_name("encoded_meter")
        if appsrc is None or encoder_queue is None or encoded_meter is None:
            pipeline.set_state(self.gst.State.NULL)
            raise PreflightError("GStreamer stream instrumentation could not be created.")
        encoded_pad = encoded_meter.get_static_pad("src")
        if encoded_pad is None:
            pipeline.set_state(self.gst.State.NULL)
            raise PreflightError("GStreamer encoded-output pad could not be created.")
        encoded_pad.add_probe(self.gst.PadProbeType.BUFFER, self._encoded_buffer_probe)
        encoder_queue.connect("overrun", self._encoder_queue_overrun)
        result = pipeline.set_state(self.gst.State.PLAYING)
        if result == self.gst.StateChangeReturn.FAILURE:
            pipeline.set_state(self.gst.State.NULL)
            raise PreflightError("GStreamer NVENC pipeline failed to enter PLAYING state.")
        self.pipeline = pipeline
        self.appsrc = appsrc
        self.first_capture_ns = None

    def _push(self, frame: RawFrame) -> None:
        if self.appsrc is None:
            raise RuntimeError("GStreamer pipeline is not running.")
        # The ring read already made the one ownership-safe copy required
        # before its slot can be overwritten. Wrap that immutable bytes object
        # directly instead of copying it a second time with Buffer.fill().
        buffer = self.gst.Buffer.new_wrapped(frame.payload)
        if self.first_capture_ns is None:
            self.first_capture_ns = frame.capture_monotonic_ns
        buffer.pts = max(frame.capture_monotonic_ns - self.first_capture_ns, 0)
        buffer.dts = buffer.pts
        buffer.duration = self.frame_duration_ns
        buffer.offset = frame.sequence
        result = self.appsrc.emit("push-buffer", buffer)
        if result != self.gst.FlowReturn.OK:
            self.status["dropped_frames"] += 1
            raise RuntimeError(f"GStreamer appsrc rejected frame: {result.value_nick}")

    def _encoded_buffer_probe(self, _pad: Any, _info: Any) -> Any:
        now = time.perf_counter()
        self.output_events.append(now)
        self.status["published_frames"] += 1
        self.status["ready"] = True
        return self.gst.PadProbeReturn.OK

    def _encoder_queue_overrun(self, _queue: Any) -> None:
        self.status["encoder_dropped_frames"] += 1
        self.status["dropped_frames"] += 1

    def _refresh_rates(self, now: float) -> None:
        cutoff = now - 5.0
        while self.input_events and self.input_events[0] < cutoff:
            self.input_events.popleft()
        while self.output_events and self.output_events[0] < cutoff:
            self.output_events.popleft()
        self.status["input_fps"] = round(_event_rate(self.input_events), 3)
        self.status["output_fps"] = round(_event_rate(self.output_events), 3)

    def _raise_pipeline_error(self) -> None:
        if self.pipeline is None:
            return
        message = self.pipeline.get_bus().timed_pop_filtered(
            0,
            self.gst.MessageType.ERROR | self.gst.MessageType.EOS,
        )
        if message is None:
            return
        if message.type == self.gst.MessageType.ERROR:
            error, debug = message.parse_error()
            raise RuntimeError(f"GStreamer NVENC error: {error}; {debug or 'no debug detail'}")
        raise RuntimeError("GStreamer NVENC pipeline ended unexpectedly.")

    def _stop_pipeline(self) -> None:
        pipeline = self.pipeline
        self.pipeline = None
        self.appsrc = None
        self.generation = None
        self.frame_duration_ns = 0
        if pipeline is not None:
            pipeline.set_state(self.gst.State.NULL)


def _load_gst() -> Any:  # noqa: ANN401
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise PreflightError("Python GStreamer bindings are unavailable.") from exc
    Gst.init(None)
    return Gst


def _require_elements(gst: Any, names: tuple[str, ...]) -> None:  # noqa: ANN401
    missing = [name for name in names if gst.ElementFactory.find(name) is None]
    if missing:
        raise PreflightError(
            "NVENC-only stream preflight failed; missing GStreamer elements: " + ", ".join(missing)
        )


def _automatic_bitrate(
    width: int,
    height: int,
    fps: float,
    bits_per_pixel: float,
    minimum: int,
    maximum: int,
) -> int:
    calculated = int(round(width * height * max(fps, 1.0) * bits_per_pixel / 1000))
    return min(max(calculated, minimum), maximum)


def _event_rate(events: collections.deque[float]) -> float:
    if len(events) < 2:
        return 0.0
    return (len(events) - 1) / max(events[-1] - events[0], 0.001)


def _safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "_.-" else "_" for character in value
    )


def _validated_stream_path(value: str) -> str:
    path = str(value or "").strip().strip("/")
    if not path or any(not (character.isalnum() or character in "_.-/") for character in path):
        raise PreflightError("RTSP stream path contains unsupported characters.")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise PreflightError("RTSP stream path is invalid.")
    return path


def _write_status(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(status, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _install_signal_handlers(worker: NvencWorker) -> None:
    def stop(_signum: int, _frame: object) -> None:
        worker.stop = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--channel-slug", required=True)
    parser.add_argument("--stream-path", required=True)
    parser.add_argument("--publish-host", required=True)
    parser.add_argument("--rtsp-port", required=True, type=int)
    parser.add_argument("--status-path", required=True)
    parser.add_argument("--bitrate-kbps", type=int)
    parser.add_argument("--auto-bitrate-bpp", required=True, type=float)
    parser.add_argument("--min-bitrate-kbps", required=True, type=int)
    parser.add_argument("--max-bitrate-kbps", required=True, type=int)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    status_path = Path(args.status_path)
    try:
        NvencWorker(args).run()
        return 0
    except Exception as exc:
        permanent = isinstance(exc, PreflightError | ValueError)
        _write_status(
            status_path,
            {
                "ready": False,
                "input_fps": 0.0,
                "output_fps": 0.0,
                "submitted_frames": 0,
                "published_frames": 0,
                "dropped_frames": 0,
                "encoder_dropped_frames": 0,
                "last_frame_sequence": 0,
                "publication_latency_ms": None,
                "source_frame_age_ms": None,
                "error": str(exc),
                "permanent_failure": permanent,
                "pid": os.getpid(),
            },
        )
        print(f"camera-media NVENC worker failed: {exc}", file=sys.stderr, flush=True)
        return 78 if permanent else 1


if __name__ == "__main__":
    raise SystemExit(main())
