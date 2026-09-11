from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cv_core import PlatformSettings

LOGGER = logging.getLogger(__name__)


@dataclass
class PublisherSnapshot:
    ready: bool = False
    input_fps: float = 0.0
    output_fps: float = 0.0
    submitted_frames: int = 0
    published_frames: int = 0
    dropped_frames: int = 0
    encoder_dropped_frames: int = 0
    reconnect_count: int = 0
    last_frame_sequence: int = 0
    publication_latency_ms: float | None = None
    source_frame_age_ms: float | None = None
    helper_pid: int | None = None
    error: str | None = None
    permanent_failure: bool = False

    def metrics(self) -> dict[str, Any]:
        return {
            "encoder": "gstreamer:nvcudah264enc",
            "hardware_accelerated": True,
            "input_fps": round(self.input_fps, 3),
            "output_fps": round(self.output_fps, 3),
            "submitted_frames": self.submitted_frames,
            "published_frames": self.published_frames,
            "dropped_frames": self.dropped_frames,
            "encoder_dropped_frames": self.encoder_dropped_frames,
            "reconnect_count": self.reconnect_count,
            "last_frame_sequence": self.last_frame_sequence,
            "publication_latency_ms": self.publication_latency_ms,
            "source_frame_age_ms": self.source_frame_age_ms,
            "gstreamer_worker_pid": self.helper_pid,
        }


class NvencRtspPublisher:
    """Supervise the system-Python GI worker without piping frame payloads."""

    def __init__(
        self,
        *,
        settings: PlatformSettings,
        channel_id: str,
        channel_slug: str,
        stop_event: threading.Event,
        ring_channel_id: str | None = None,
        stream_path: str | None = None,
        status_suffix: str = "publisher",
    ) -> None:
        self.settings = settings
        self.channel_id = channel_id
        self.channel_slug = channel_slug
        self.ring_channel_id = ring_channel_id or channel_id
        self.stream_path = stream_path or f"channels/{channel_slug}/live"
        self.stop_event = stop_event
        self.snapshot = PublisherSnapshot()
        self._process: subprocess.Popen[bytes] | None = None
        runtime_dir = Path(settings.runtime.camera_media_runtime_dir)
        self._status_path = runtime_dir / (
            f"{_safe_name(channel_id)}.{_safe_name(status_suffix)}.json"
        )

    def run(self) -> None:
        backoff = float(self.settings.runtime.stream_publisher_restart_backoff_initial_seconds)
        maximum = float(self.settings.runtime.stream_publisher_restart_backoff_max_seconds)
        while not self.stop_event.is_set():
            try:
                self._run_once()
                if self.stop_event.is_set():
                    break
                if self.snapshot.permanent_failure:
                    break
                raise RuntimeError(
                    self.snapshot.error or "GStreamer NVENC worker stopped unexpectedly."
                )
            except Exception as exc:
                self.snapshot.ready = False
                self.snapshot.error = str(exc)
                self.snapshot.permanent_failure = isinstance(exc, NvencPreflightError)
                LOGGER.exception("NVENC publisher failed for channel %s", self.channel_id)
                if self.snapshot.permanent_failure:
                    break
                if self.stop_event.wait(backoff):
                    break
                self.snapshot.reconnect_count += 1
                backoff = min(backoff * 2, maximum)
            finally:
                self._stop_worker()

    def _run_once(self) -> None:
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        self._status_path.unlink(missing_ok=True)
        helper = Path(__file__).with_name("gst_worker.py")
        python_bin = str(self.settings.runtime.stream_gstreamer_python_bin)
        if not Path(python_bin).is_file():
            raise NvencPreflightError(f"GStreamer system Python was not found at {python_bin}.")
        if not helper.is_file():
            raise NvencPreflightError(f"GStreamer worker is missing: {helper}")
        command = [
            python_bin,
            str(helper),
            "--runtime-dir",
            str(self.settings.runtime.camera_media_runtime_dir),
            "--channel-id",
            self.ring_channel_id,
            "--channel-slug",
            self.channel_slug,
            "--stream-path",
            self.stream_path,
            "--publish-host",
            str(self.settings.runtime.stream_rtsp_publish_host),
            "--rtsp-port",
            str(self.settings.runtime.stream_rtsp_port),
            "--status-path",
            str(self._status_path),
            "--auto-bitrate-bpp",
            str(self.settings.runtime.stream_auto_bitrate_bits_per_pixel),
            "--min-bitrate-kbps",
            str(self.settings.runtime.stream_auto_bitrate_min_kbps),
            "--max-bitrate-kbps",
            str(self.settings.runtime.stream_auto_bitrate_max_kbps),
        ]
        override = self.settings.runtime.stream_bitrate_override_kbps
        if override is not None:
            command.extend(("--bitrate-kbps", str(override)))
        self._process = subprocess.Popen(command, close_fds=True)
        self.snapshot.helper_pid = self._process.pid
        while not self.stop_event.wait(0.1):
            self._load_status()
            return_code = self._process.poll()
            if return_code is None:
                continue
            self._load_status()
            if self.snapshot.permanent_failure:
                raise NvencPreflightError(
                    self.snapshot.error or "GStreamer NVENC preflight failed."
                )
            raise RuntimeError(
                self.snapshot.error or f"GStreamer NVENC worker exited with status {return_code}."
            )

    def _load_status(self) -> None:
        try:
            payload = json.loads(self._status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        self.snapshot.ready = bool(payload.get("ready"))
        self.snapshot.input_fps = _as_float(payload.get("input_fps"))
        self.snapshot.output_fps = _as_float(payload.get("output_fps"))
        self.snapshot.submitted_frames = _as_int(payload.get("submitted_frames"))
        self.snapshot.published_frames = _as_int(payload.get("published_frames"))
        self.snapshot.dropped_frames = _as_int(payload.get("dropped_frames"))
        self.snapshot.encoder_dropped_frames = _as_int(
            payload.get("encoder_dropped_frames")
        )
        self.snapshot.last_frame_sequence = _as_int(payload.get("last_frame_sequence"))
        self.snapshot.publication_latency_ms = _optional_float(
            payload.get("publication_latency_ms")
        )
        self.snapshot.source_frame_age_ms = _optional_float(payload.get("source_frame_age_ms"))
        self.snapshot.error = str(payload["error"]) if payload.get("error") else None
        self.snapshot.permanent_failure = bool(payload.get("permanent_failure"))
        self.snapshot.helper_pid = _as_int(payload.get("pid")) or self.snapshot.helper_pid

    def _stop_worker(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        self.snapshot.helper_pid = None
        self._status_path.unlink(missing_ok=True)


class NvencPreflightError(RuntimeError):
    pass


def _require_elements(gst: Any, names: tuple[str, ...]) -> None:  # noqa: ANN401
    missing = [name for name in names if gst.ElementFactory.find(name) is None]
    if missing:
        raise NvencPreflightError(
            "NVENC-only stream preflight failed; missing GStreamer elements: " + ", ".join(missing)
        )


def _safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "_.-" else "_" for character in value
    )


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
