from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Protocol
from urllib.parse import quote, urlparse

from cv_runtime import LatestFrameRingReader, validate_basler_link_budget
from cv_runtime.basler import basler_gain_setting, basler_pixel_format
from cv_utils import utc_now_iso
from PIL import Image

_IMAGE_FILE_EXTENSIONS = {".bmp", ".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_FILE_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
_GENTL_SEARCH_KEYS = (
    "serial_number",
    "user_defined_name",
    "id_",
    "model",
    "vendor",
    "tl_type",
    "display_name",
)
_PYLON_SEARCH_KEYS = (
    "serial_number",
    "user_defined_name",
    "device_id",
    "id_",
    "ip_address",
    "model",
    "device_class",
    "friendly_name",
    "full_name",
)
_DEFAULT_OPENCV_FFMPEG_CAPTURE_OPTIONS = "rtsp_transport;tcp"
_ARAVIS_DEFAULT_RTSP_PORT_BASE = 18554
_ARAVIS_DEFAULT_RTSP_PORT_SPAN = 10000
_ARAVIS_RTSP_ENV_DIR = Path("/etc/ybb-inferenzserver/aravis-rtsp")
_ARAVIS_RTSP_UNIT_TEMPLATE = Path("/etc/systemd/system/ybb-aravis-rtsp@.service")
_ARAVIS_RTSP_HELPER = Path("/usr/local/bin/ybb-aravis-rtsp-bridge")
LOGGER = logging.getLogger(__name__)


def _ensure_default_rtsp_capture_options() -> None:
    os.environ.setdefault(
        "OPENCV_FFMPEG_CAPTURE_OPTIONS",
        _DEFAULT_OPENCV_FFMPEG_CAPTURE_OPTIONS,
    )


_ensure_default_rtsp_capture_options()


@dataclass(frozen=True)
class FramePacket:
    image_bytes: bytes | None
    mime_type: str
    width: int | None
    height: int | None
    source_timestamp: str
    source_uri: str | None
    sequence: int
    metadata: dict[str, Any]
    raw_frame: Any | None = None
    jpeg_quality: int = 80

    def ensure_encoded(self) -> FramePacket:
        if self.image_bytes is not None:
            return self
        if self.raw_frame is None:
            raise RuntimeError("Frame packet has neither encoded bytes nor a raw frame.")
        image_bytes, width, height = _encode_cv2_frame(
            self.raw_frame,
            quality=self.jpeg_quality,
        )
        return replace(
            self,
            image_bytes=image_bytes,
            mime_type="image/jpeg",
            width=width,
            height=height,
            raw_frame=None,
            metadata={**self.metadata, "encoded_after_sampling": True},
        )


@dataclass
class SourceHealth:
    connected: bool = False
    status_text: str = "Source not connected"
    last_error: str | None = None
    reconnect_count: int = 0
    source_error_count: int = 0


class RealtimeFrameSource(Protocol):
    health: SourceHealth

    def read(self, stop_event: Event) -> FramePacket | None: ...

    def close(self) -> None: ...


class SharedMemoryFrameSource:
    """Latest-frame subscriber for the camera-media owned capture process."""

    def __init__(
        self,
        *,
        runtime_dir: str | Path,
        channel_id: str,
        source_type: str,
        ordered: bool = False,
    ) -> None:
        self.health = SourceHealth(status_text="Waiting for camera-media frame ring")
        self._reader = LatestFrameRingReader(
            runtime_dir=runtime_dir,
            channel_id=channel_id,
        )
        self._source_type = source_type
        self._ordered = ordered
        self._generation: str | None = None

    def read(self, stop_event: Event) -> FramePacket | None:
        try:
            read_frame = self._reader.read_next if self._ordered else self._reader.read_latest
            shared = read_frame(
                stop_event=stop_event,
                timeout_seconds=1.0,
            )
        except Exception as exc:
            self.health.connected = False
            self.health.status_text = "Camera-media frame transport unavailable"
            self.health.last_error = str(exc)
            self.health.source_error_count += 1
            return None
        if shared is None:
            self.health.connected = False
            self.health.status_text = "Waiting for camera-media frame"
            return None
        descriptor = shared.descriptor
        if self._generation is not None and descriptor.generation != self._generation:
            self.health.reconnect_count += 1
        self._generation = descriptor.generation
        self.health.connected = True
        self.health.status_text = "Camera-media frame transport connected"
        self.health.last_error = None
        return FramePacket(
            image_bytes=None,
            mime_type="image/jpeg",
            width=descriptor.width,
            height=descriptor.height,
            source_timestamp=_wall_time_iso(shared.capture_wall_time_ns),
            source_uri=descriptor.source_uri,
            sequence=shared.sequence,
            metadata={
                **descriptor.source_metadata,
                "source_type": self._source_type,
                "transport": (
                    "shared_memory_ordered_frames"
                    if self._ordered else "shared_memory_latest_frame"
                ),
                "frame_ring_overwritten_frames": self._reader.overwritten_frames,
                "frame_ring_skipped_frames": self._reader.skipped_frames,
                "capture_monotonic_ns": shared.capture_monotonic_ns,
                "frame_ring_generation": descriptor.generation,
            },
            raw_frame=shared.frame,
        )

    def close(self) -> None:
        self._reader.close()


def _wall_time_iso(value_ns: int) -> str:
    if value_ns <= 0:
        return utc_now_iso()
    return (
        datetime.fromtimestamp(value_ns / 1_000_000_000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class RtspSourceConfigurationError(ValueError):
    pass


class BaslerProfileValidationError(ValueError):
    pass


class BaslerDeviceUnavailableError(RuntimeError):
    """Discovery did not find the requested camera; retry without changing identity."""


class OpenCvRtspFrameSource:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        secret_refs: dict[str, str] | None = None,
    ) -> None:
        self.config = dict(config or {})
        self.secret_refs = dict(secret_refs or {})
        self.health = SourceHealth(status_text="RTSP source connecting")
        self._capture = None
        self._sequence = 0
        self._source_uri = _rtsp_uri_from_config(self.config, self.secret_refs)
        self._safe_source_uri = _redact_inline_credentials(self._source_uri)
        self._reconnect_seconds = max(float(self.config.get("reconnect_seconds") or 5), 0.25)
        self._read_timeout_seconds = max(float(self.config.get("read_timeout_seconds") or 2), 0.25)

    def read(self, stop_event: Event) -> FramePacket | None:
        if self._capture is None and not self._open_capture():
            stop_event.wait(self._reconnect_seconds)
            return None
        started = time.monotonic()
        try:
            ok, frame = self._capture.read()  # type: ignore[union-attr]
        except Exception as exc:
            self._mark_error(f"RTSP read failed: {exc}", reconnect=True)
            stop_event.wait(self._reconnect_seconds)
            return None
        if not ok or frame is None:
            self._mark_error("RTSP read returned no frame.", reconnect=True)
            stop_event.wait(self._reconnect_seconds)
            return None
        if time.monotonic() - started > self._read_timeout_seconds:
            self._mark_error("RTSP read exceeded timeout.", reconnect=True)
            stop_event.wait(self._reconnect_seconds)
            return None
        width, height = _cv2_frame_dimensions(frame)
        self._sequence += 1
        self.health.connected = True
        self.health.status_text = "RTSP source connected"
        self.health.last_error = None
        return FramePacket(
            image_bytes=None,
            mime_type="image/jpeg",
            width=width,
            height=height,
            source_timestamp=utc_now_iso(),
            source_uri=self._safe_source_uri,
            sequence=self._sequence,
            metadata={"source_type": "rtsp"},
            raw_frame=frame,
        )

    def close(self) -> None:
        capture = self._capture
        self._capture = None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                return

    def _open_capture(self) -> bool:
        _ensure_default_rtsp_capture_options()
        try:
            import cv2  # type: ignore[import-not-found]
        except Exception as exc:
            self._mark_error(
                "OpenCV is required for RTSP live sources but is not available.",
                detail=str(exc),
                reconnect=False,
            )
            return False
        try:
            capture = _open_cv2_capture(
                cv2,
                self._source_uri,
                timeout_msec=int(self._read_timeout_seconds * 1000),
                prefer_ffmpeg=True,
            )
            if capture is None:
                self._mark_error(
                    _rtsp_open_failure_message(self._safe_source_uri),
                    reconnect=True,
                )
                return False
            self._capture = capture
            self.health.connected = True
            self.health.status_text = "RTSP source connected"
            self.health.last_error = None
            return True
        except Exception as exc:
            self._mark_error(f"RTSP source open failed: {exc}", reconnect=True)
            return False

    def _mark_error(self, message: str, *, reconnect: bool, detail: str | None = None) -> None:
        self.health.source_error_count += 1
        if reconnect:
            self.health.reconnect_count += 1
        self.close()
        self.health.connected = False
        self.health.status_text = message
        self.health.last_error = detail or message


class OpenCvUsbFrameSource:
    """Open a local USB/UVC camera through a bounded OpenCV capture adapter."""

    def __init__(self, *, config: dict[str, Any]) -> None:
        self.config = dict(config or {})
        self.health = SourceHealth(status_text="USB camera connecting")
        self._capture = None
        self._sequence = 0
        self._target = _usb_capture_target(self.config)
        self._source_uri = _usb_source_uri(self._target)
        self._reconnect_seconds = max(float(self.config.get("reconnect_seconds") or 2), 0.25)
        self._read_timeout_seconds = max(
            float(self.config.get("read_timeout_seconds") or 2),
            0.25,
        )

    def read(self, stop_event: Event) -> FramePacket | None:
        if self._capture is None and not self._open_capture():
            stop_event.wait(self._reconnect_seconds)
            return None
        started = time.monotonic()
        try:
            ok, frame = self._capture.read()  # type: ignore[union-attr]
        except Exception as exc:
            self._mark_error(f"USB camera read failed: {exc}", reconnect=True)
            stop_event.wait(self._reconnect_seconds)
            return None
        if not ok or frame is None:
            self._mark_error("USB camera returned no frame.", reconnect=True)
            stop_event.wait(self._reconnect_seconds)
            return None
        if time.monotonic() - started > self._read_timeout_seconds:
            self._mark_error("USB camera read exceeded timeout.", reconnect=True)
            stop_event.wait(self._reconnect_seconds)
            return None
        width, height = _cv2_frame_dimensions(frame)
        self._sequence += 1
        self.health.connected = True
        self.health.status_text = "USB camera connected"
        self.health.last_error = None
        return FramePacket(
            image_bytes=None,
            mime_type="image/jpeg",
            width=width,
            height=height,
            source_timestamp=utc_now_iso(),
            source_uri=self._source_uri,
            sequence=self._sequence,
            metadata={
                "source_type": "usb_opencv",
                "device_index": self.config.get("device_index"),
                "device_path": self.config.get("device_path"),
                "backend": str(self.config.get("backend") or "auto"),
            },
            raw_frame=frame,
        )

    def close(self) -> None:
        capture = self._capture
        self._capture = None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                return

    def _open_capture(self) -> bool:
        try:
            import cv2  # type: ignore[import-not-found]
        except Exception as exc:
            self._mark_error(
                "OpenCV is required for USB camera sources but is not available.",
                detail=str(exc),
                reconnect=False,
            )
            return False
        try:
            capture = _open_cv2_capture(
                cv2,
                self._target,
                timeout_msec=int(self._read_timeout_seconds * 1000),
                backend_name=str(self.config.get("backend") or "auto"),
            )
            if capture is None:
                self._mark_error(
                    f"USB camera could not be opened ({self._source_uri}).",
                    reconnect=True,
                )
                return False
            _configure_usb_capture_properties(cv2, capture, self.config)
            self._capture = capture
            self.health.connected = True
            self.health.status_text = "USB camera connected"
            self.health.last_error = None
            return True
        except Exception as exc:
            self._mark_error(f"USB camera open failed: {exc}", reconnect=True)
            return False

    def _mark_error(self, message: str, *, reconnect: bool, detail: str | None = None) -> None:
        self.health.source_error_count += 1
        if reconnect:
            self.health.reconnect_count += 1
        self.close()
        self.health.connected = False
        self.health.status_text = message
        self.health.last_error = detail or message


class AravisGigeRtspFrameSource:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        secret_refs: dict[str, str] | None = None,
    ) -> None:
        self.config = dict(config or {})
        self.secret_refs = dict(secret_refs or {})
        self.health = SourceHealth(status_text="Aravis GigE bridge connecting")
        self._rtsp_source: OpenCvRtspFrameSource | None = None
        self._bridge_ready = False
        self._reconnect_seconds = max(float(self.config.get("reconnect_seconds") or 5), 0.25)

    def read(self, stop_event: Event) -> FramePacket | None:
        if not self._bridge_ready:
            self._bridge_ready = _ensure_aravis_rtsp_bridge(self.config, health=self.health)
            if not self._bridge_ready:
                stop_event.wait(self._reconnect_seconds)
                return None
        if self._rtsp_source is None:
            self._rtsp_source = OpenCvRtspFrameSource(
                config=_aravis_rtsp_source_config(self.config),
                secret_refs=self.secret_refs,
            )
            self.health = self._rtsp_source.health
        packet = self._rtsp_source.read(stop_event)
        self.health = self._rtsp_source.health
        if packet is None:
            return None
        metadata = {
            **dict(packet.metadata or {}),
            "source_type": "aravis_gige",
            "transport": "rtsp",
            "producer": "aravis",
            "camera_name": self.config.get("camera_name"),
            "bridge_name": self.config.get("bridge_name"),
        }
        return replace(packet, metadata=metadata)

    def close(self) -> None:
        if self._rtsp_source is not None:
            self._rtsp_source.close()
            self._rtsp_source = None
        self.health.connected = False
        self.health.status_text = "Aravis GigE source closed"


class HarvesterBlackflyFrameSource:
    def __init__(self, *, config: dict[str, Any]) -> None:
        self.config = dict(config or {})
        self.health = SourceHealth(status_text="FLIR Blackfly source connecting")
        self._harvester = None
        self._acquirer = None
        self._sequence = 0
        self._device_info: dict[str, Any] = {}
        self._source_uri = _blackfly_source_uri(self.config)
        self._reconnect_seconds = max(float(self.config.get("reconnect_seconds") or 5), 0.25)
        self._fetch_timeout_seconds = max(
            float(self.config.get("fetch_timeout_seconds") or 2),
            0.25,
        )

    def read(self, stop_event: Event) -> FramePacket | None:
        if self._acquirer is None and not self._open_acquirer():
            stop_event.wait(self._reconnect_seconds)
            return None
        try:
            buffer = _try_fetch_harvester_buffer(
                self._acquirer,
                timeout_seconds=self._fetch_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error(f"FLIR Blackfly frame fetch failed: {exc}", reconnect=True)
            stop_event.wait(self._reconnect_seconds)
            return None
        if buffer is None:
            self.health.connected = True
            self.health.status_text = "FLIR Blackfly source connected; waiting for frame"
            self.health.last_error = None
            return None
        try:
            component = buffer.payload.components[0]
            frame = _cv2_frame_from_harvester_component(component)
        except Exception as exc:
            self._mark_error(f"FLIR Blackfly frame conversion failed: {exc}", reconnect=True)
            stop_event.wait(self._reconnect_seconds)
            return None
        finally:
            try:
                buffer.queue()
            except Exception:
                pass
        width, height = _cv2_frame_dimensions(frame)
        self._sequence += 1
        self.health.connected = True
        self.health.status_text = "FLIR Blackfly source connected"
        self.health.last_error = None
        return FramePacket(
            image_bytes=None,
            mime_type="image/jpeg",
            width=width,
            height=height,
            source_timestamp=utc_now_iso(),
            source_uri=self._source_uri,
            sequence=self._sequence,
            metadata={
                "source_type": "flir_blackfly",
                "camera": self._device_info,
                "transport": "gentl",
                "producer": "spinnaker",
            },
            raw_frame=frame,
        )

    def close(self) -> None:
        acquirer = self._acquirer
        harvester = self._harvester
        self._acquirer = None
        self._harvester = None
        if acquirer is not None:
            try:
                acquirer.stop()
            except Exception:
                pass
            try:
                acquirer.destroy()
            except Exception:
                pass
        if harvester is not None:
            try:
                harvester.reset()
            except Exception:
                pass
        self.health.connected = False
        self.health.status_text = "FLIR Blackfly source closed"

    def _open_acquirer(self) -> bool:
        try:
            from harvesters.core import Harvester  # type: ignore[import-not-found]
        except Exception as exc:
            self._mark_error(
                "The harvesters package is required for FLIR Blackfly GenTL sources.",
                detail=str(exc),
                reconnect=False,
            )
            return False
        cti_paths = _blackfly_gentl_cti_paths(self.config)
        if not cti_paths:
            self._mark_error(
                "FLIR Blackfly sources require a Spinnaker GenTL .cti producer path.",
                detail=(
                    "Install the Teledyne Spinnaker SDK or set GENICAM_GENTL64_PATH, "
                    "cti_path, or cti_paths."
                ),
                reconnect=False,
            )
            return False
        try:
            harvester = Harvester()
            for cti_path in cti_paths:
                harvester.add_file(str(cti_path))
            harvester.update()
            devices = list(getattr(harvester, "device_info_list", []) or [])
            selector, device_info = _blackfly_device_selector(self.config, devices)
            acquirer = harvester.create(selector)
            _configure_blackfly_acquirer(acquirer, self.config)
            acquirer.start()
            self._harvester = harvester
            self._acquirer = acquirer
            self._device_info = device_info
            self._source_uri = _blackfly_source_uri({**self.config, **device_info})
            self.health.connected = True
            self.health.status_text = "FLIR Blackfly source connected"
            self.health.last_error = None
            return True
        except Exception as exc:
            self._mark_error(f"FLIR Blackfly source open failed: {exc}", reconnect=True)
            return False

    def _mark_error(self, message: str, *, reconnect: bool, detail: str | None = None) -> None:
        self.health.connected = False
        self.health.status_text = message
        self.health.last_error = detail or message
        self.health.source_error_count += 1
        if reconnect:
            self.health.reconnect_count += 1
        self.close()


class BaslerPylonFrameSource:
    def __init__(self, *, config: dict[str, Any]) -> None:
        self.config = dict(config or {})
        self.health = SourceHealth(status_text="Basler Pylon source connecting")
        self._camera = None
        self._converter = None
        self._aravis_source: AravisGigeRtspFrameSource | None = None
        self._sequence = 0
        self._device_info: dict[str, Any] = {}
        self._capture_profile: dict[str, Any] = {}
        self._source_uri = _basler_source_uri(self.config)
        self._last_wait_log_monotonic = 0.0
        self._last_grab_failure_log_monotonic = 0.0
        self._consecutive_grab_failures = 0
        self._transport_mode = _basler_transport_mode(self.config)
        self._reconnect_seconds = max(float(self.config.get("reconnect_seconds") or 5), 0.25)
        self._fetch_timeout_seconds = max(
            float(self.config.get("fetch_timeout_seconds") or 2),
            0.25,
        )

    def read(self, stop_event: Event) -> FramePacket | None:
        if stop_event.is_set():
            return None
        if self._transport_mode == "aravis_rtsp_bridge":
            return self._read_aravis_bridge(stop_event, reason="configured")
        if self._aravis_source is not None:
            return self._read_aravis_bridge(stop_event, reason="fallback")
        if self._camera is None and not self._open_camera():
            if self._aravis_source is not None:
                return self._read_aravis_bridge(stop_event, reason="fallback")
            stop_event.wait(self._reconnect_seconds)
            return None
        if self._aravis_source is not None:
            return self._read_aravis_bridge(stop_event, reason="fallback")
        try:
            from pypylon import pylon  # type: ignore[import-not-found]
        except Exception as exc:
            self._mark_error(
                "The pypylon package and Basler Pylon runtime are required for Basler Pylon sources.",
                detail=str(exc),
                reconnect=False,
            )
            return None
        try:
            if _basler_uses_software_trigger(self.config):
                _execute_basler_software_trigger(
                    self._camera,
                    pylon,
                    timeout_seconds=self._fetch_timeout_seconds,
                )
            grab_result = _try_retrieve_pylon_result(
                self._camera,
                pylon,
                timeout_seconds=self._fetch_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error(f"Basler Pylon frame fetch failed: {exc}", reconnect=True)
            stop_event.wait(self._reconnect_seconds)
            return None
        if grab_result is None:
            self.health.connected = True
            self.health.status_text = "Basler Pylon source connected; waiting for frame"
            self.health.last_error = None
            self._log_waiting_for_frame()
            return None
        try:
            if hasattr(grab_result, "GrabSucceeded") and not grab_result.GrabSucceeded():
                error = ""
                if hasattr(grab_result, "GetErrorDescription"):
                    error = str(grab_result.GetErrorDescription())
                self._handle_failed_grab(error, stop_event)
                return None
            frame = _bgr_frame_from_basler_grab_result(
                grab_result,
                pylon,
                converter=self._converter,
            )
        except Exception as exc:
            self._mark_error(f"Basler Pylon frame conversion failed: {exc}", reconnect=True)
            stop_event.wait(self._reconnect_seconds)
            return None
        finally:
            release = getattr(grab_result, "Release", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    pass
        width, height = _cv2_frame_dimensions(frame)
        self._sequence += 1
        self._consecutive_grab_failures = 0
        self.health.connected = True
        self.health.status_text = "Basler Pylon source connected"
        self.health.last_error = None
        return FramePacket(
            image_bytes=None,
            mime_type="image/jpeg",
            width=width,
            height=height,
            source_timestamp=utc_now_iso(),
            source_uri=self._source_uri,
            sequence=self._sequence,
            metadata={
                "source_type": "basler_pylon",
                "camera": self._device_info,
                "transport": "pylon",
                "producer": "basler_pylon",
                "grab_strategy": self.config.get("grab_strategy") or "LatestImageOnly",
                "capture_profile": dict(self._capture_profile),
            },
            raw_frame=frame,
        )

    def close(self) -> None:
        self._close_camera()
        if self._aravis_source is not None:
            self._aravis_source.close()
            self._aravis_source = None
        self.health.connected = False
        self.health.status_text = "Basler Pylon source closed"

    def _open_camera(self) -> bool:
        try:
            from pypylon import pylon  # type: ignore[import-not-found]
        except Exception as exc:
            self._mark_error(
                "The pypylon package and Basler Pylon runtime are required for Basler Pylon sources.",
                detail=str(exc),
                reconnect=False,
            )
            if self._transport_mode == "auto":
                self._activate_aravis_fallback(reason=str(exc))
                return True
            return False
        try:
            factory = pylon.TlFactory.GetInstance()
            device_info, info = _discover_basler_device(factory, self.config)
            camera = pylon.InstantCamera(factory.CreateDevice(device_info))
            # Retain the handle even when Open partially succeeds and then raises.
            self._camera = camera
            camera.Open()
            _configure_basler_camera(camera, pylon, self.config)
            capture_profile = _basler_effective_camera_profile(camera, self.config)
            try:
                validate_basler_link_budget(capture_profile, required=False)
            except ValueError as exc:
                raise BaslerProfileValidationError(str(exc)) from exc
            converter = _basler_image_converter(pylon)
            grab_strategy = _pylon_grab_strategy(pylon, str(self.config.get("grab_strategy") or "LatestImageOnly"))
            try:
                camera.StartGrabbing(grab_strategy)
            except TypeError:
                camera.StartGrabbing()
            self._camera = camera
            self._converter = converter
            self._device_info = info
            self._capture_profile = capture_profile
            self._source_uri = _basler_source_uri({**self.config, **info})
            self.health.connected = True
            self.health.status_text = "Basler Pylon source connected"
            self.health.last_error = None
            LOGGER.info(
                "Basler Pylon source opened: source_uri=%s camera=%s config=%s "
                "effective_profile=%s",
                self._source_uri,
                info,
                _basler_config_summary(self.config),
                capture_profile,
            )
            return True
        except BaslerDeviceUnavailableError as exc:
            self._mark_error(f"Basler Pylon discovery unavailable: {exc}", reconnect=True)
            if _basler_should_use_aravis_fallback(self.config, exc):
                self._activate_aravis_fallback(reason=str(exc))
                return True
            return False
        except BaslerProfileValidationError as exc:
            self._mark_error(
                f"Basler Pylon source profile rejected: {exc}",
                reconnect=False,
            )
            raise
        except RtspSourceConfigurationError as exc:
            use_fallback = _basler_should_use_aravis_fallback(self.config, exc)
            self._mark_error(
                f"Basler Pylon source open failed: {exc}",
                reconnect=use_fallback,
            )
            if use_fallback:
                self._activate_aravis_fallback(reason=str(exc))
                return True
            raise
        except Exception as exc:
            self._mark_error(f"Basler Pylon source open failed: {exc}", reconnect=True)
            return False

    def _close_camera(self) -> None:
        camera = self._camera
        self._camera = None
        self._converter = None
        self._capture_profile = {}
        if camera is None:
            return
        try:
            is_grabbing = getattr(camera, "IsGrabbing", None)
            if callable(is_grabbing) and is_grabbing():
                camera.StopGrabbing()
        except Exception:
            pass
        try:
            is_open = getattr(camera, "IsOpen", None)
            if not callable(is_open) or is_open():
                camera.Close()
        except Exception:
            pass

    def _mark_error(self, message: str, *, reconnect: bool, detail: str | None = None) -> None:
        self._consecutive_grab_failures = 0
        self.health.connected = False
        self.health.status_text = message
        self.health.last_error = detail or message
        self.health.source_error_count += 1
        if reconnect:
            self.health.reconnect_count += 1
        log = LOGGER.warning if reconnect else LOGGER.error
        log(
            "Basler Pylon source error: %s detail=%s reconnect=%s source_uri=%s config=%s",
            message,
            detail or message,
            reconnect,
            self._source_uri,
            _basler_config_summary(self.config),
        )
        self._close_camera()

    def _handle_failed_grab(self, error: str, stop_event: Event) -> None:
        self.health.source_error_count += 1
        self._consecutive_grab_failures += 1
        message = f"Basler Pylon grab failed{f': {error}' if error else ''}."
        threshold = _positive_int_config(self.config, "grab_failure_reconnect_threshold", 10) or 10
        if self._consecutive_grab_failures >= max(threshold, 1):
            self._mark_error(
                (
                    f"{message} Reconnecting after "
                    f"{self._consecutive_grab_failures} consecutive failed grabs."
                ),
                reconnect=True,
                detail=error or message,
            )
            stop_event.wait(self._reconnect_seconds)
            return
        self.health.connected = True
        self.health.status_text = f"{message} Skipping incomplete frame."
        self.health.last_error = error or message
        self._log_failed_grab(message)
        stop_event.wait(0.001)

    def _log_failed_grab(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_grab_failure_log_monotonic < 10.0:
            return
        self._last_grab_failure_log_monotonic = now
        LOGGER.warning(
            "%s consecutive_grab_failures=%s source_uri=%s config=%s",
            message,
            self._consecutive_grab_failures,
            self._source_uri,
            _basler_config_summary(self.config),
        )

    def _log_waiting_for_frame(self) -> None:
        now = time.monotonic()
        if now - self._last_wait_log_monotonic < 10.0:
            return
        self._last_wait_log_monotonic = now
        LOGGER.warning(
            "Basler Pylon source is open but no frame was returned within %.3fs: "
            "source_uri=%s reconnect_count=%s source_error_count=%s config=%s",
            self._fetch_timeout_seconds,
            self._source_uri,
            self.health.reconnect_count,
            self.health.source_error_count,
            _basler_config_summary(self.config),
        )

    def _activate_aravis_fallback(self, *, reason: str) -> None:
        if self._aravis_source is not None:
            return
        bridge_config = _basler_aravis_bridge_config(self.config)
        self._aravis_source = AravisGigeRtspFrameSource(config=bridge_config, secret_refs={})
        self.health.connected = False
        self.health.status_text = "Basler Pylon discovery failed; using Aravis RTSP bridge"
        self.health.last_error = reason
        LOGGER.warning(
            "Basler Pylon direct discovery failed; switching to Aravis RTSP bridge: "
            "reason=%s bridge_config=%s",
            reason,
            _basler_config_summary(bridge_config),
        )

    def _read_aravis_bridge(self, stop_event: Event, *, reason: str) -> FramePacket | None:
        if self._aravis_source is None:
            self._aravis_source = AravisGigeRtspFrameSource(
                config=_basler_aravis_bridge_config(self.config),
                secret_refs={},
            )
        packet = self._aravis_source.read(stop_event)
        bridge_health = self._aravis_source.health
        self.health.connected = bridge_health.connected
        self.health.status_text = (
            "Basler Pylon via Aravis RTSP bridge"
            if bridge_health.connected
            else f"Basler Pylon Aravis RTSP bridge waiting: {bridge_health.status_text}"
        )
        self.health.last_error = bridge_health.last_error
        self.health.reconnect_count = bridge_health.reconnect_count
        self.health.source_error_count = bridge_health.source_error_count
        if packet is None:
            return None
        metadata = {
            **dict(packet.metadata or {}),
            "source_type": "basler_pylon",
            "transport": "aravis_rtsp",
            "producer": "aravis",
            "basler_transport_mode": self._transport_mode,
            "basler_bridge_reason": reason,
        }
        return replace(packet, metadata=metadata)


class FileTestFrameSource:
    def __init__(self, *, config: dict[str, Any]) -> None:
        self.config = dict(config or {})
        self.health = SourceHealth(connected=True, status_text="File/test source ready")
        self._sequence = 0
        self._path = Path(str(self.config.get("path") or ""))
        photos = _embedded_test_photos(self.config)
        photo = photos[0] if photos else self.config.get("photo")
        self._source_uri = (
            f"pipeline-test-photo://{photo.get('name') or 'test-photo'}"
            if isinstance(photo, dict)
            else str(self._path)
        )
        self._loop = bool(self.config.get("loop", True))
        self._capture = None
        self._static_frames: list[tuple[bytes, str, int, int, str]] = []
        self._photo_index = 0
        self._photo_started_at: float | None = None
        self._photo_duration_seconds = max(
            float(self.config.get("photo_duration_seconds") or 1), 0.1
        )
        if photos:
            self._static_frames = [
                (*_load_embedded_photo(item), str(item.get("name") or "test-photo"))
                for item in photos
            ]
            fps = float(self.config.get("max_fps") or self.config.get("fps") or 5)
        elif _is_video_file(self._path, self.config):
            native_fps = self._open_video_capture()
            fps = float(self.config.get("max_fps") or self.config.get("fps") or native_fps or 10)
        else:
            self._static_frames = [(*_load_file_frame(self._path), self._path.name)]
            fps = float(self.config.get("max_fps") or self.config.get("fps") or 5)
        self._interval = 1.0 / max(fps, 0.1)
        self._last_read_at = 0.0

    def read(self, stop_event: Event) -> FramePacket | None:
        elapsed = time.monotonic() - self._last_read_at
        if elapsed < self._interval:
            stop_event.wait(self._interval - elapsed)
            if stop_event.is_set():
                return None
        self._last_read_at = time.monotonic()
        self._sequence += 1
        if self._capture is not None:
            return self._read_video_frame(stop_event)
        if not self._static_frames:
            self._mark_error("File/test source has no readable frame.")
            return None
        image_bytes, mime_type, width, height, name = self._current_static_frame()
        return FramePacket(
            image_bytes=image_bytes,
            mime_type=mime_type,
            width=width,
            height=height,
            source_timestamp=utc_now_iso(),
            source_uri=(
                f"pipeline-test-photo://{name}"
                if len(self._static_frames) > 1
                else self._source_uri
            ),
            sequence=self._sequence,
            metadata={
                "source_type": "file_test",
                "file_name": name,
                "photo_index": self._photo_index,
                "photo_count": len(self._static_frames),
            },
        )

    def _current_static_frame(self) -> tuple[bytes, str, int, int, str]:
        if len(self._static_frames) == 1:
            return self._static_frames[0]
        now = time.monotonic()
        if self._photo_started_at is None:
            self._photo_started_at = now
        elapsed = now - self._photo_started_at
        if elapsed >= self._photo_duration_seconds:
            steps = int(elapsed / self._photo_duration_seconds)
            self._photo_index = (self._photo_index + steps) % len(self._static_frames)
            self._photo_started_at += steps * self._photo_duration_seconds
        return self._static_frames[self._photo_index]

    def close(self) -> None:
        capture = self._capture
        self._capture = None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass
        self.health.connected = False
        self.health.status_text = "File/test source closed"

    def _open_video_capture(self) -> float:
        if not self._path.exists() or not self._path.is_file():
            raise RtspSourceConfigurationError(
                f"File/test source video does not exist: {self._path}"
            )
        try:
            import cv2  # type: ignore[import-not-found]
        except Exception as exc:
            raise RtspSourceConfigurationError(
                "OpenCV is required for video test file sources but is not available."
            ) from exc
        capture = cv2.VideoCapture(str(self._path))
        if not capture.isOpened():
            try:
                capture.release()
            except Exception:
                pass
            raise RtspSourceConfigurationError(
                f"File/test source video could not be opened: {self._path}"
            )
        self._capture = capture
        self.health.connected = True
        self.health.status_text = "File/test video source ready"
        try:
            native_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        except Exception:
            native_fps = 0.0
        return native_fps

    def _read_video_frame(self, stop_event: Event) -> FramePacket | None:
        if self._capture is None:
            return None
        try:
            ok, frame = self._capture.read()
            if (not ok or frame is None) and self._loop:
                self._capture.set(1, 0)
                ok, frame = self._capture.read()
        except Exception as exc:
            self._mark_error(f"File/test video read failed: {exc}")
            stop_event.wait(0.25)
            return None
        if not ok or frame is None:
            self._mark_error("File/test video reached end of file.")
            stop_event.wait(0.25)
            return None
        width, height = _cv2_frame_dimensions(frame)
        self.health.connected = True
        self.health.status_text = "File/test video source ready"
        self.health.last_error = None
        return FramePacket(
            image_bytes=None,
            mime_type="image/jpeg",
            width=width,
            height=height,
            source_timestamp=utc_now_iso(),
            source_uri=self._source_uri,
            sequence=self._sequence,
            metadata={"source_type": "file_test", "file_kind": "video"},
            raw_frame=frame,
        )

    def _mark_error(self, message: str) -> None:
        self.health.connected = False
        self.health.status_text = message
        self.health.last_error = message
        self.health.source_error_count += 1


def build_frame_source(
    *,
    source_type: str,
    config: dict[str, Any],
    secret_refs: dict[str, str] | None = None,
) -> RealtimeFrameSource:
    if source_type == "rtsp":
        return OpenCvRtspFrameSource(config=config, secret_refs=secret_refs)
    if source_type == "usb_opencv":
        return OpenCvUsbFrameSource(config=config)
    if source_type == "aravis_gige":
        return AravisGigeRtspFrameSource(config=config, secret_refs=secret_refs)
    if source_type == "flir_blackfly":
        return HarvesterBlackflyFrameSource(config=config)
    if source_type == "basler_pylon":
        return BaslerPylonFrameSource(config=config)
    if source_type == "file_test":
        return FileTestFrameSource(config=config)
    raise RtspSourceConfigurationError(
        "Realtime live execution requires an RTSP, USB/OpenCV, Aravis GigE, FLIR Blackfly, "
        "Basler Pylon, or file/test source, "
        f"got {source_type}."
    )


def _ensure_aravis_rtsp_bridge(config: dict[str, Any], *, health: SourceHealth) -> bool:
    bridge_name = _aravis_bridge_name(config)
    service_name = f"ybb-aravis-rtsp@{bridge_name}.service"
    if not _ARAVIS_RTSP_HELPER.exists():
        _mark_aravis_bridge_error(
            health,
            "Aravis RTSP bridge helper is not installed.",
            detail=str(_ARAVIS_RTSP_HELPER),
        )
        return False
    if not _ARAVIS_RTSP_UNIT_TEMPLATE.exists():
        _mark_aravis_bridge_error(
            health,
            "Aravis RTSP bridge service template is not installed.",
            detail=str(_ARAVIS_RTSP_UNIT_TEMPLATE),
        )
        return False
    try:
        _ARAVIS_RTSP_ENV_DIR.mkdir(parents=True, exist_ok=True)
        env_path = _ARAVIS_RTSP_ENV_DIR / f"{bridge_name}.env"
        env_content = _aravis_bridge_env_content(config)
        previous_content = env_path.read_text(encoding="utf-8") if env_path.exists() else None
        env_changed = previous_content != env_content
        if env_changed:
            env_path.write_text(env_content, encoding="utf-8")
            try:
                env_path.chmod(0o640)
            except OSError:
                pass
    except OSError as exc:
        _mark_aravis_bridge_error(
            health,
            "Aravis RTSP bridge configuration could not be written.",
            detail=str(exc),
        )
        return False

    active = _systemctl(["is-active", "--quiet", service_name]).returncode == 0
    action = "restart" if active and env_changed else "start"
    result = _systemctl([action, service_name])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"systemctl {action} failed"
        _mark_aravis_bridge_error(
            health,
            f"Aravis RTSP bridge {service_name} could not be started.",
            detail=detail,
        )
        return False
    health.connected = True
    health.status_text = f"Aravis RTSP bridge {bridge_name} running"
    health.last_error = None
    return True


def _aravis_rtsp_source_config(config: dict[str, Any]) -> dict[str, Any]:
    rtsp_config = dict(config or {})
    url = str(
        rtsp_config.get("url")
        or rtsp_config.get("rtsp_url")
        or ""
    ).strip()
    if not url:
        host = str(rtsp_config.get("rtsp_host") or "127.0.0.1").strip()
        port = int(rtsp_config.get("rtsp_port") or 8554)
        mount = _aravis_rtsp_mount(rtsp_config)
        url = f"rtsp://{host}:{port}{mount}"
    rtsp_config["url"] = url
    return rtsp_config


def _aravis_bridge_env_content(config: dict[str, Any]) -> str:
    values = {
        "ARAVIS_RTSP_ADDRESS": str(config.get("rtsp_address") or "127.0.0.1"),
        "ARAVIS_RTSP_PORT": str(config.get("rtsp_port") or 8554),
        "ARAVIS_RTSP_MOUNT": _aravis_rtsp_mount(config),
        "ARAVIS_CAMERA_NAME": str(config.get("camera_name") or ""),
        "ARAVIS_FEATURES": _aravis_features(config),
        "ARAVIS_CONVERSION_MODE": str(config.get("conversion_mode") or "bayer"),
        "ARAVIS_SRC_PROPERTIES": _aravis_src_properties(config),
        "ARAVIS_H264_BITRATE_KBIT": str(config.get("h264_bitrate_kbit") or 6000),
        "ARAVIS_H264_KEY_INT_MAX": str(config.get("h264_key_int_max") or 30),
        "ARAVIS_QUEUE_SIZE": str(config.get("queue_size") or 2),
    }
    optional = {
        "ARAVIS_SOURCE_CAPS": config.get("source_caps"),
        "ARAVIS_CONVERSION_PIPELINE": config.get("conversion_pipeline"),
    }
    for key, value in optional.items():
        if value:
            values[key] = str(value)
    lines = [
        "# Managed by YBB Inferenzserver runtime. Changes may be overwritten.",
        *[
            f"{key}={_systemd_env_value(value)}"
            for key, value in values.items()
            if value is not None
        ],
        "",
    ]
    return "\n".join(lines)


def _aravis_features(config: dict[str, Any]) -> str:
    features = config.get("features")
    if isinstance(features, dict):
        return " ".join(
            f"{str(name).strip()}={value}"
            for name, value in features.items()
            if str(name).strip() and value is not None
        )
    if features:
        return str(features)
    pixel_format = str(config.get("pixel_format") or "").strip()
    return f"PixelFormat={pixel_format}" if pixel_format else ""


def _aravis_src_properties(config: dict[str, Any]) -> str:
    raw_properties = str(config.get("src_properties") or "").strip()
    if raw_properties:
        return raw_properties
    properties = []
    packet_size = config.get("packet_size")
    if packet_size:
        properties.append(f"packet-size={int(packet_size)}")
    packet_delay = config.get("packet_delay")
    if packet_delay:
        properties.append(f"packet-delay={int(packet_delay)}")
    return " ".join(properties)


def _aravis_rtsp_mount(config: dict[str, Any]) -> str:
    mount = str(config.get("rtsp_mount") or config.get("mount") or "/aravis-gige").strip()
    if not mount.startswith("/"):
        mount = f"/{mount}"
    return mount


def _aravis_bridge_name(config: dict[str, Any]) -> str:
    raw_name = str(config.get("bridge_name") or config.get("camera_name") or "aravis-gige")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_name.strip()).strip(".-")
    if not name:
        raise RtspSourceConfigurationError("Aravis GigE sources require a bridge_name.")
    return name


def _systemd_env_value(value: object) -> str:
    raw = str(value)
    if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]*", raw):
        return raw
    escaped = (
        raw.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


def _systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["systemctl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(
            ["systemctl", *args],
            returncode=1,
            stdout="",
            stderr=str(exc),
        )


def _mark_aravis_bridge_error(
    health: SourceHealth,
    message: str,
    *,
    detail: str | None = None,
) -> None:
    health.connected = False
    health.status_text = message
    health.last_error = detail or message
    health.source_error_count += 1
    health.reconnect_count += 1


def _open_cv2_capture(
    cv2: Any,
    source_uri: str | int,
    *,
    timeout_msec: int,
    prefer_ffmpeg: bool = False,
    backend_name: str | None = None,
) -> Any | None:
    backend_candidates: list[int | None] = []
    backend_attribute = {
        "v4l2": "CAP_V4L2",
        "dshow": "CAP_DSHOW",
        "msmf": "CAP_MSMF",
    }.get(str(backend_name or "auto").strip().lower())
    if backend_attribute:
        backend = getattr(cv2, backend_attribute, None)
        if backend is None:
            raise RtspSourceConfigurationError(
                f"The configured OpenCV capture backend {backend_name!r} is not available."
            )
        backend_candidates.append(int(backend))
    elif prefer_ffmpeg and hasattr(cv2, "CAP_FFMPEG"):
        backend_candidates.append(int(cv2.CAP_FFMPEG))
    backend_candidates.append(None)

    for backend in backend_candidates:
        capture = cv2.VideoCapture()
        _configure_cv2_capture(cv2, capture, timeout_msec=timeout_msec)
        try:
            opened = (
                capture.open(source_uri, backend)
                if backend is not None
                else capture.open(source_uri)
            )
        except TypeError:
            opened = False
        except Exception:
            opened = False
        if opened and capture.isOpened():
            return capture
        try:
            capture.release()
        except Exception:
            pass
    return None


def _usb_capture_target(config: dict[str, Any]) -> str | int:
    device_path = str(config.get("device_path") or "").strip()
    if device_path:
        return device_path
    raw_index = config.get("device_index")
    if raw_index is None:
        raw_index = config.get("uri")
    if raw_index is None or isinstance(raw_index, bool):
        raise RtspSourceConfigurationError(
            "USB/OpenCV source requires a device_index or device_path."
        )
    try:
        device_index = int(raw_index)
    except (TypeError, ValueError) as exc:
        raise RtspSourceConfigurationError(
            "USB/OpenCV device_index must be an integer between 0 and 255."
        ) from exc
    if device_index < 0 or device_index > 255:
        raise RtspSourceConfigurationError(
            "USB/OpenCV device_index must be between 0 and 255."
        )
    return device_index


def _usb_source_uri(target: str | int) -> str:
    if isinstance(target, int):
        return f"usb-opencv://index/{target}"
    return f"usb-opencv://device/{target}"


def _configure_usb_capture_properties(cv2: Any, capture: Any, config: dict[str, Any]) -> None:
    properties = (
        ("width", "CAP_PROP_FRAME_WIDTH"),
        ("height", "CAP_PROP_FRAME_HEIGHT"),
        ("fps", "CAP_PROP_FPS"),
        ("buffer_size", "CAP_PROP_BUFFERSIZE"),
    )
    for config_name, property_name in properties:
        value = config.get(config_name)
        prop = getattr(cv2, property_name, None)
        if value in (None, "") or prop is None:
            continue
        capture.set(prop, float(value))
    fourcc = str(config.get("fourcc") or "").strip().upper()
    fourcc_property = getattr(cv2, "CAP_PROP_FOURCC", None)
    fourcc_builder = getattr(cv2, "VideoWriter_fourcc", None)
    if fourcc and fourcc_property is not None and callable(fourcc_builder):
        capture.set(fourcc_property, fourcc_builder(*fourcc))


def _configure_cv2_capture(cv2: Any, capture: Any, *, timeout_msec: int) -> None:
    properties = (
        ("CAP_PROP_BUFFERSIZE", 1),
        ("CAP_PROP_OPEN_TIMEOUT_MSEC", timeout_msec),
        ("CAP_PROP_READ_TIMEOUT_MSEC", timeout_msec),
    )
    for prop_name, value in properties:
        prop = getattr(cv2, prop_name, None)
        if prop is None:
            continue
        try:
            capture.set(prop, value)
        except Exception:
            continue


def _rtsp_open_failure_message(safe_source_uri: str) -> str:
    parsed = urlparse(safe_source_uri)
    target = parsed.netloc or safe_source_uri
    options = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS") or "<unset>"
    return (
        f"RTSP source could not be opened from executor runtime ({target}). "
        "Check WSL network access, RTSP credentials, and camera transport settings. "
        f"OpenCV FFmpeg options: {options}"
    )


def _rtsp_uri_from_config(config: dict[str, Any], secret_refs: dict[str, str]) -> str:
    raw_url = str(config.get("url") or "")
    if raw_url:
        parsed = urlparse(raw_url)
        if parsed.scheme.lower() not in {"rtsp", "rtsps"}:
            raise RtspSourceConfigurationError("RTSP source URLs must use rtsp:// or rtsps://.")
        if parsed.username or parsed.password or "@" in parsed.netloc:
            raise RtspSourceConfigurationError(
                "RTSP URLs must not contain inline credentials; use secret references."
            )
        username = _read_secret(secret_refs.get("username_file") or secret_refs.get("username"))
        password = _read_secret(secret_refs.get("password_file") or secret_refs.get("password"))
        if username and password:
            host = parsed.hostname or parsed.netloc
            port = f":{parsed.port}" if parsed.port else ""
            path = parsed.path or ""
            if parsed.params:
                path = f"{path};{parsed.params}"
            query = f"?{parsed.query}" if parsed.query else ""
            credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"
            return f"{parsed.scheme}://{credentials}{host}{port}{path}{query}"
        return raw_url
    host = str(config.get("host") or "").strip()
    if not host:
        raise RtspSourceConfigurationError("RTSP source requires a url or host.")
    scheme = str(config.get("scheme") or "rtsp").lower()
    if scheme not in {"rtsp", "rtsps"}:
        raise RtspSourceConfigurationError("RTSP source scheme must be rtsp or rtsps.")
    port = int(config.get("port") or 554)
    path = str(config.get("path") or config.get("stream_path") or "").lstrip("/")
    username = _read_secret(secret_refs.get("username_file") or secret_refs.get("username"))
    password = _read_secret(secret_refs.get("password_file") or secret_refs.get("password"))
    credentials = f"{username}:{password}@" if username and password else ""
    return f"{scheme}://{credentials}{host}:{port}/{path}".rstrip("/")


def _read_secret(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        raise RtspSourceConfigurationError("Configured RTSP secret reference is not readable.")
    return path.read_text(encoding="utf-8").strip()


def _cv2_frame_dimensions(frame: object) -> tuple[int | None, int | None]:
    height = int(frame.shape[0]) if getattr(frame, "shape", None) is not None else None
    width = int(frame.shape[1]) if getattr(frame, "shape", None) is not None else None
    return width, height


def _encode_cv2_frame(frame: object, *, quality: int = 80) -> tuple[bytes, int | None, int | None]:
    import cv2  # type: ignore[import-not-found]

    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("OpenCV failed to encode RTSP frame as JPEG.")
    width, height = _cv2_frame_dimensions(frame)
    return bytes(encoded), width, height


def _is_video_file(path: Path, config: dict[str, Any]) -> bool:
    kind = str(config.get("kind") or "").lower()
    if kind == "video":
        return True
    if kind == "image":
        return False
    suffix = path.suffix.lower()
    return suffix in _VIDEO_FILE_EXTENSIONS and suffix not in _IMAGE_FILE_EXTENSIONS


def _load_file_frame(path: Path) -> tuple[bytes, str, int, int]:
    if not path.exists() or not path.is_file():
        raise RtspSourceConfigurationError(f"File/test source image does not exist: {path}")
    data = path.read_bytes()
    with Image.open(path) as image:
        mime_type = Image.MIME.get(str(image.format), "image/jpeg")
        width = image.width
        height = image.height
    return data, mime_type, width, height


def _embedded_test_photos(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw_photos = config.get("photos")
    if isinstance(raw_photos, list):
        photos = [item for item in raw_photos if isinstance(item, dict)]
        if len(photos) > 100:
            raise RtspSourceConfigurationError(
                "A pipeline test photo sequence can contain at most 100 photos."
            )
        if photos:
            return photos
    photo = config.get("photo")
    return [photo] if isinstance(photo, dict) else []


def _load_embedded_photo(photo: dict[str, Any]) -> tuple[bytes, str, int, int]:
    data_url = str(photo.get("data_url") or "")
    try:
        header, encoded = data_url.split(",", 1)
        if header not in {
            "data:image/jpeg;base64",
            "data:image/png;base64",
            "data:image/webp;base64",
            "data:image/bmp;base64",
        }:
            raise ValueError("unsupported data URL")
        data = base64.b64decode(encoded, validate=True)
        if len(data) > 5 * 1024 * 1024:
            raise ValueError("image exceeds 5 MB")
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            width, height = image.size
    except (ValueError, OSError) as exc:
        raise RtspSourceConfigurationError(
            "The embedded pipeline test photo is invalid or unsupported."
        ) from exc
    mime_type = header[5:].split(";", 1)[0]
    return data, mime_type, width, height


def _blackfly_gentl_cti_paths(config: dict[str, Any]) -> list[Path]:
    raw_entries: list[tuple[str, bool]] = []
    cti_path = config.get("cti_path")
    if cti_path:
        raw_entries.append((str(cti_path), True))
    cti_paths = config.get("cti_paths") or []
    if isinstance(cti_paths, str):
        raw_entries.append((cti_paths, True))
    elif isinstance(cti_paths, list):
        raw_entries.extend((str(path), True) for path in cti_paths)
    for env_name in ("GENICAM_GENTL64_PATH", "GENICAM_GENTL32_PATH"):
        value = os.environ.get(env_name)
        if value:
            raw_entries.extend((entry, False) for entry in value.split(os.pathsep))
    resolved: list[Path] = []
    seen: set[str] = set()
    for raw_entry, explicitly_configured in raw_entries:
        for raw_part in str(raw_entry).replace(";", os.pathsep).split(os.pathsep):
            part = raw_part.strip().strip('"')
            if not part:
                continue
            path = Path(part)
            candidates = [path] if path.suffix.lower() == ".cti" else sorted(path.glob("*.cti"))
            for candidate in candidates:
                if not candidate.exists() or not candidate.is_file():
                    continue
                if not explicitly_configured and not _is_blackfly_gentl_producer(candidate):
                    continue
                key = str(candidate.resolve())
                if key in seen:
                    continue
                resolved.append(candidate)
                seen.add(key)
    return resolved


def _is_blackfly_gentl_producer(path: Path) -> bool:
    vendor_hint = str(path).casefold()
    return "spinnaker" in vendor_hint or "flir" in vendor_hint


def _blackfly_device_selector(
    config: dict[str, Any],
    devices: list[Any],
) -> tuple[int | dict[str, str], dict[str, Any]]:
    if not devices:
        raise RtspSourceConfigurationError("No FLIR Blackfly GenTL devices were discovered.")
    for key in ("serial_number", "user_defined_name", "id_"):
        value = str(config.get(key) or "").strip()
        if not value:
            continue
        selector = {key: value}
        return selector, _find_device_info(devices, selector) or selector
    for key in ("device_id", "ip_address"):
        value = str(config.get(key) or "").strip()
        if not value:
            continue
        index, info = _find_device_info_by_text(devices, value)
        if index is None:
            raise RtspSourceConfigurationError(
                f"No FLIR Blackfly GenTL device matched {key}={value}."
            )
        return index, info
    if len(devices) == 1:
        return 0, _device_info_dict(devices[0])
    raise RtspSourceConfigurationError(
        "Multiple FLIR Blackfly GenTL devices were discovered; configure serial_number, "
        "user_defined_name, device_id, id_, or ip_address."
    )


def _find_device_info(devices: list[Any], selector: dict[str, str]) -> dict[str, Any] | None:
    matches = [
        info
        for info in (_device_info_dict(device) for device in devices)
        if all(str(info.get(key) or "") == value for key, value in selector.items())
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _find_device_info_by_text(
    devices: list[Any],
    value: str,
) -> tuple[int | None, dict[str, Any]]:
    needle = value.casefold()
    matches: list[tuple[int, dict[str, Any]]] = []
    for index, device in enumerate(devices):
        info = _device_info_dict(device)
        haystack = " ".join(str(info.get(key) or "") for key in _GENTL_SEARCH_KEYS).casefold()
        if needle in haystack:
            matches.append((index, info))
    if len(matches) == 1:
        return matches[0]
    return None, {}


def _device_info_dict(device: Any) -> dict[str, Any]:
    info: dict[str, Any] = {}
    if isinstance(device, dict):
        source = device
    else:
        source = {key: getattr(device, key, None) for key in _GENTL_SEARCH_KEYS}
    for key in _GENTL_SEARCH_KEYS:
        value = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
        if value is not None and str(value):
            info[key] = str(value)
    return info


def _configure_blackfly_acquirer(acquirer: Any, config: dict[str, Any]) -> None:
    node_map = getattr(getattr(acquirer, "remote_device", None), "node_map", None)
    if node_map is None:
        return
    acquisition_mode = str(config.get("acquisition_mode") or "Continuous").strip()
    if acquisition_mode:
        _set_genicam_node_value(node_map, "AcquisitionMode", acquisition_mode, strict=False)
    pixel_format = str(config.get("pixel_format") or "").strip()
    if pixel_format:
        _set_genicam_node_value(node_map, "PixelFormat", pixel_format, strict=True)
    if config.get("exposure_time_us") is not None:
        _set_genicam_node_value(node_map, "ExposureAuto", "Off", strict=False)
        _set_genicam_node_value(
            node_map,
            "ExposureTime",
            float(config["exposure_time_us"]),
            strict=True,
        )
    if config.get("gain_db") is not None:
        _set_genicam_node_value(node_map, "GainAuto", "Off", strict=False)
        _set_genicam_node_value(node_map, "Gain", float(config["gain_db"]), strict=True)
    features = config.get("features")
    if isinstance(features, dict):
        for name, value in features.items():
            _set_genicam_node_value(node_map, str(name), value, strict=True)


class _PylonDiscoveryTransport(Protocol):
    # Method spelling is defined by the Pylon SDK.
    def AnnounceRemoteDevice(self, address: str) -> tuple[bool, Any]: ...  # noqa: N802


class _PylonDiscoveryFactory(Protocol):
    def EnumerateDevices(self) -> list[Any]: ...  # noqa: N802

    def CreateTl(self, device_class: str) -> _PylonDiscoveryTransport: ...  # noqa: N802


def _discover_basler_device(
    factory: _PylonDiscoveryFactory, config: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """Prefer normal discovery; use only an explicitly configured IP as fallback."""
    address = str(config.get("ip_address") or "").strip()
    if address:
        try:
            parsed = ipaddress.IPv4Address(address)
            if parsed.is_unspecified or parsed.is_multicast or int(parsed) == 0xFFFFFFFF:
                raise ValueError("a unicast camera address is required")
        except ValueError as exc:
            raise RtspSourceConfigurationError(
                f"Invalid Basler camera ip_address={address!r}: {exc}"
            ) from exc
    try:
        devices = list(factory.EnumerateDevices() or [])
    except Exception as exc:
        LOGGER.warning("Basler Pylon general discovery failed: %s", exc)
        if not address:
            raise BaslerDeviceUnavailableError(str(exc)) from exc
        devices = []
    LOGGER.info(
        "Basler Pylon general discovery found %s device(s) for %s: %s",
        len(devices), _basler_source_uri(config),
        [_pylon_device_info_dict(device) for device in devices],
    )
    try:
        return _basler_device_selector(config, devices)
    except RtspSourceConfigurationError as exc:
        # Ambiguous selection with no configured identity needs user correction.
        if not address:
            if devices and not any(config.get(key) for key in (
                "serial_number", "user_defined_name", "camera_name", "camera",
                "device_id", "id_", "ip_address", "model",
            )):
                raise
            raise BaslerDeviceUnavailableError(str(exc)) from exc
    LOGGER.warning("Basler Pylon trying directed discovery at %s", address)
    try:
        transport = factory.CreateTl("BaslerGigE")
        found, device = transport.AnnounceRemoteDevice(address)
    except AttributeError as exc:
        raise RtspSourceConfigurationError(
            "The installed Pylon runtime does not support directed GigE discovery."
        ) from exc
    except Exception as exc:
        raise BaslerDeviceUnavailableError(
            f"Directed discovery at {address} failed: {exc}"
        ) from exc
    if not found:
        raise BaslerDeviceUnavailableError(f"No Basler camera responded at {address}.")
    # Never open another camera just because it now occupies the configured IP.
    device, info = _basler_device_selector(config, [device])
    LOGGER.info("Basler Pylon directed discovery succeeded at %s: %s", address, info)
    return device, info


def _basler_device_selector(
    config: dict[str, Any],
    devices: list[Any],
) -> tuple[Any, dict[str, Any]]:
    if not devices:
        raise RtspSourceConfigurationError("No Basler Pylon devices were discovered.")
    discovered = [_pylon_device_info_dict(device) for device in devices]
    for key in ("serial_number", "user_defined_name"):
        value = str(config.get(key) or "").strip()
        if not value:
            continue
        matches = [
            (device, info)
            for device in devices
            for info in [_pylon_device_info_dict(device)]
            if str(info.get(key) or "") == value
        ]
        if len(matches) == 1:
            return matches[0]
        raise RtspSourceConfigurationError(
            f"No Basler Pylon device matched {key}={value}. Discovered devices: {discovered}"
        )
    for key in ("camera_name", "camera", "device_id", "id_", "ip_address", "model"):
        value = str(config.get(key) or "").strip()
        if not value:
            continue
        device, info = _find_pylon_device_info_by_text(devices, value)
        if device is None:
            raise RtspSourceConfigurationError(
                f"No Basler Pylon device matched {key}={value}. Discovered devices: {discovered}"
            )
        return device, info
    if len(devices) == 1:
        return devices[0], _pylon_device_info_dict(devices[0])
    raise RtspSourceConfigurationError(
        "Multiple Basler Pylon devices were discovered; configure serial_number, "
        f"user_defined_name, device_id, id_, ip_address, or model. Discovered devices: {discovered}"
    )


def _find_pylon_device_info_by_text(
    devices: list[Any],
    value: str,
) -> tuple[Any | None, dict[str, Any]]:
    needle = value.casefold()
    matches: list[tuple[Any, dict[str, Any]]] = []
    for device in devices:
        info = _pylon_device_info_dict(device)
        haystack = " ".join(str(info.get(key) or "") for key in _PYLON_SEARCH_KEYS).casefold()
        if needle in haystack:
            matches.append((device, info))
    if len(matches) == 1:
        return matches[0]
    return None, {}


def _pylon_device_info_dict(device: Any) -> dict[str, Any]:
    method_names = {
        "serial_number": "GetSerialNumber",
        "user_defined_name": "GetUserDefinedName",
        "device_id": "GetDeviceID",
        "id_": "GetDeviceID",
        "ip_address": "GetIpAddress",
        "model": "GetModelName",
        "device_class": "GetDeviceClass",
        "friendly_name": "GetFriendlyName",
        "full_name": "GetFullName",
    }
    info: dict[str, Any] = {}
    for key, method_name in method_names.items():
        value = None
        if isinstance(device, dict):
            value = device.get(key)
        else:
            method = getattr(device, method_name, None)
            if callable(method):
                try:
                    value = method()
                except Exception:
                    value = None
            if value is None:
                value = getattr(device, key, None)
        if value is not None and str(value).strip():
            info[key] = str(value).strip()
    if info.get("device_id") and not info.get("id_"):
        info["id_"] = info["device_id"]
    return info


def _configure_basler_camera(camera: Any, pylon: Any, config: dict[str, Any]) -> None:
    acquisition_mode = str(config.get("acquisition_mode") or "Continuous").strip()
    if acquisition_mode:
        _set_pylon_node_value(camera, "AcquisitionMode", acquisition_mode, strict=False)
    pixel_format = str(config.get("pixel_format") or "").strip()
    if pixel_format:
        _set_pylon_node_value(
            camera,
            "PixelFormat",
            basler_pixel_format(_basler_camera_model(camera, config), pixel_format),
            strict=True,
        )

    trigger_mode = str(config.get("trigger_mode") or "Off").strip()
    if trigger_mode:
        _set_pylon_node_value(camera, "TriggerMode", trigger_mode, strict=False)
    if trigger_mode.casefold() not in {"", "off", "false", "0"}:
        trigger_source = str(config.get("trigger_source") or "").strip()
        if trigger_source:
            _set_pylon_node_value(camera, "TriggerSource", trigger_source, strict=True)
        trigger_activation = str(config.get("trigger_activation") or "").strip()
        if trigger_activation:
            _set_pylon_node_value(camera, "TriggerActivation", trigger_activation, strict=False)

    if config.get("fps") is not None:
        _set_pylon_node_value(
            camera,
            "AcquisitionFrameRateEnable",
            bool(config.get("acquisition_frame_rate_enable", True)),
            strict=False,
        )
        _set_first_available_pylon_node_value(
            camera,
            ("AcquisitionFrameRate", "AcquisitionFrameRateAbs"),
            float(config["fps"]),
            strict=True,
        )
    if config.get("exposure_auto") or config.get("exposure_time_us") is not None:
        exposure_auto = str(config.get("exposure_auto") or "Off").strip()
        if exposure_auto and _pylon_node(camera, "ExposureAuto") is not None:
            _set_pylon_node_value(camera, "ExposureAuto", exposure_auto, strict=True)
    if config.get("exposure_time_us") is not None:
        _set_first_available_pylon_node_value(
            camera,
            ("ExposureTime", "ExposureTimeAbs"),
            float(config["exposure_time_us"]),
            strict=True,
        )
    if (
        config.get("gain_auto")
        or config.get("gain_db") is not None
        or config.get("gain_raw") is not None
    ):
        gain_auto = str(config.get("gain_auto") or "Off").strip()
        if gain_auto and _pylon_node(camera, "GainAuto") is not None:
            _set_pylon_node_value(camera, "GainAuto", gain_auto, strict=True)
    if config.get("gain_db") is not None or config.get("gain_raw") is not None:
        _configure_basler_gain(camera, config)
    _configure_basler_grab_buffers(camera, config)
    for name, key in (
        ("Width", "width"),
        ("Height", "height"),
        ("OffsetX", "offset_x"),
        ("OffsetY", "offset_y"),
        ("GevSCPSPacketSize", "packet_size"),
        ("GevSCPD", "inter_packet_delay"),
    ):
        if config.get(key) is not None:
            _set_pylon_node_value(camera, name, int(config[key]), strict=True)
    features = config.get("features")
    if isinstance(features, dict):
        for name, value in features.items():
            _set_pylon_node_value(camera, str(name), value, strict=True)
    _ensure_basler_even_roi(camera)


def _configure_basler_gain(camera: Any, config: dict[str, Any]) -> None:
    gain_raw = config.get("gain_raw")
    setting = basler_gain_setting(
        _basler_camera_model(camera, config),
        gain_db=float(config["gain_db"]) if config.get("gain_db") is not None else None,
        gain_raw=int(gain_raw) if gain_raw is not None else None,
    )
    if setting is None:
        return
    if setting.selector and _pylon_node(camera, "GainSelector") is not None:
        _set_pylon_node_value(camera, "GainSelector", setting.selector, strict=True)
    _set_first_available_pylon_node_value(
        camera,
        setting.node_names,
        setting.value,
        strict=True,
    )


def _ensure_basler_even_roi(camera: Any) -> None:
    """Trim an odd effective ROI dimension before the NVENC frame path starts."""
    for name in ("Width", "Height"):
        value = _pylon_node_value(camera, (name,))
        try:
            dimension = int(value)
        except (TypeError, ValueError):
            continue
        if dimension > 1 and dimension % 2:
            _set_pylon_node_value(camera, name, dimension - 1, strict=True)


def _basler_camera_model(camera: Any, config: dict[str, Any]) -> str:
    configured = str(config.get("model") or "").strip()
    if configured:
        return configured
    try:
        device_info = camera.GetDeviceInfo()
        return str(device_info.GetModelName() or "").strip()
    except Exception:
        return ""


def _configure_basler_grab_buffers(camera: Any, config: dict[str, Any]) -> None:
    buffer_count = _positive_int_config(config, "buffer_count", None)
    if buffer_count is None:
        buffer_count = _positive_int_config(config, "max_num_buffer", 16)
    if buffer_count is not None:
        _set_pylon_node_value(camera, "MaxNumBuffer", buffer_count, strict=False)
    output_queue_size = _positive_int_config(config, "output_queue_size", None)
    if output_queue_size is not None:
        _set_pylon_node_value(
            camera,
            "OutputQueueSize",
            output_queue_size,
            strict=False,
        )
    frame_retention_ms = _positive_int_config(config, "frame_retention_ms", None)
    if frame_retention_ms is not None:
        _set_pylon_stream_node_value(
            camera,
            "FrameRetention",
            frame_retention_ms,
            strict=True,
        )


def _basler_uses_software_trigger(config: dict[str, Any]) -> bool:
    trigger_mode = str(config.get("trigger_mode") or "Off").strip().casefold()
    if trigger_mode in {"", "off", "false", "0"}:
        return False
    trigger_source = str(config.get("trigger_source") or "").strip().casefold()
    return trigger_source == "software"


def _execute_basler_software_trigger(
    camera: Any,
    pylon: Any,
    *,
    timeout_seconds: float,
) -> None:
    if camera is None:
        return
    timeout_ms = max(int(timeout_seconds * 1000), 1)
    timeout_handling = getattr(
        pylon,
        "TimeoutHandling_ThrowException",
        getattr(pylon, "TimeoutHandling_Return", None),
    )
    wait_ready = getattr(camera, "WaitForFrameTriggerReady", None)
    if callable(wait_ready):
        try:
            if timeout_handling is not None:
                ready = wait_ready(timeout_ms, timeout_handling)
            else:
                ready = wait_ready(timeout_ms)
        except TypeError:
            ready = wait_ready(timeout_ms)
        if ready is False:
            raise RtspSourceConfigurationError(
                "Basler Pylon software trigger did not become ready before timeout."
            )
    execute = getattr(camera, "ExecuteSoftwareTrigger", None)
    if callable(execute):
        execute()
        return
    trigger_node = _pylon_node(camera, "TriggerSoftware")
    if trigger_node is not None:
        execute_node = getattr(trigger_node, "Execute", None)
        if callable(execute_node):
            execute_node()
            return
        set_value = getattr(trigger_node, "SetValue", None)
        if callable(set_value):
            set_value(True)
            return
    raise RtspSourceConfigurationError(
        "Basler Pylon software trigger is configured but TriggerSoftware is not available."
    )


def _positive_int_config(
    config: dict[str, Any],
    key: str,
    default: int | None,
) -> int | None:
    value = config.get(key)
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _set_first_available_pylon_node_value(
    target: Any,
    names: tuple[str, ...],
    value: Any,
    *,
    strict: bool,
) -> None:
    for name in names:
        if _pylon_node(target, name) is not None:
            _set_pylon_node_value(target, name, value, strict=True)
            return
    if strict:
        raise RtspSourceConfigurationError(
            f"Basler Pylon GenICam node {'/'.join(names)} is not available."
        )


def _set_pylon_node_value(target: Any, name: str, value: Any, *, strict: bool) -> None:
    node = _pylon_node(target, name)
    if node is None:
        if strict:
            raise RtspSourceConfigurationError(f"Basler Pylon GenICam node {name} is not available.")
        return
    try:
        set_value = getattr(node, "SetValue", None)
        if callable(set_value):
            set_value(value)
        elif hasattr(node, "Value"):
            node.Value = value
        elif hasattr(node, "value"):
            node.value = value
        else:
            setattr(target, name, value)
    except Exception as exc:
        if strict:
            raise RtspSourceConfigurationError(
                f"Basler Pylon GenICam node {name} could not be set to {value!r}: {exc}"
            ) from exc


def _set_pylon_stream_node_value(target: Any, name: str, value: Any, *, strict: bool) -> None:
    node_map = _pylon_stream_node_map(target)
    if node_map is None:
        if strict:
            raise RtspSourceConfigurationError(
                f"Basler Pylon stream node {name} is not available."
            )
        return
    _set_pylon_node_value(node_map, name, value, strict=strict)


def _pylon_stream_node_map(target: Any) -> Any | None:
    for method_name in ("GetStreamGrabberNodeMap", "GetTLStreamNodeMap"):
        method = getattr(target, method_name, None)
        if callable(method):
            try:
                node_map = method()
            except Exception:
                node_map = None
            if node_map is not None:
                return node_map
    stream_grabber = getattr(target, "StreamGrabber", None)
    if stream_grabber is None:
        return None
    for method_name in ("GetNodeMap", "GetStreamGrabberNodeMap"):
        method = getattr(stream_grabber, method_name, None)
        if callable(method):
            try:
                node_map = method()
            except Exception:
                node_map = None
            if node_map is not None:
                return node_map
    return stream_grabber


def _pylon_node(target: Any, name: str) -> Any | None:
    # Pypylon's dynamic GenICam wrapper can raise "Node not existing" from
    # getattr() instead of honoring the supplied default. Optional-node probes
    # must treat that model-specific exception like a normal missing node.
    try:
        node = getattr(target, name, None)
    except Exception:
        node = None
    if node is not None:
        return node
    try:
        get_node = getattr(target, "GetNode", None)
    except Exception:
        get_node = None
    if callable(get_node):
        try:
            return get_node(name)
        except Exception:
            return None
    try:
        get_node_map = getattr(target, "GetNodeMap", None)
    except Exception:
        get_node_map = None
    if not callable(get_node_map):
        return None
    try:
        node_map = get_node_map()
        get_node = getattr(node_map, "GetNode", None)
        if callable(get_node):
            return get_node(name)
    except Exception:
        return None
    return None


def _basler_effective_camera_profile(
    camera: Any,
    config: dict[str, Any],
) -> dict[str, Any]:
    width = _first_positive_float(
        _pylon_node_value(camera, ("Width",)),
        config.get("width"),
    )
    height = _first_positive_float(
        _pylon_node_value(camera, ("Height",)),
        config.get("height"),
    )
    fps = _first_positive_float(
        _pylon_node_value(
            camera,
            (
                "BslResultingAcquisitionFrameRate",
                "ResultingFrameRateAbs",
                "AcquisitionResultingFrameRate",
                "AcquisitionFrameRate",
                "AcquisitionFrameRateAbs",
            ),
        ),
        config.get("fps"),
        30.0,
    )
    link_speed = _first_positive_float(
        config.get("link_speed_mbps"),
        _pylon_node_value(camera, ("GevLinkSpeed",)),
        1000.0,
    )
    if link_speed is not None and link_speed > 100_000:
        link_speed /= 1_000_000
    pixel_format = str(
        _pylon_node_value(camera, ("PixelFormat",))
        or config.get("pixel_format")
        or ""
    ).strip()
    return {
        "width": int(width) if width is not None else 0,
        "height": int(height) if height is not None else 0,
        "fps": fps or 0.0,
        "link_speed_mbps": link_speed or 0.0,
        "pixel_format": pixel_format,
        **{
            key: value
            for key, names in (
                ("exposure_time_us", ("ExposureTime", "ExposureTimeAbs")),
                ("exposure_auto", ("ExposureAuto",)),
                ("gain_db", ("Gain",)),
                ("gain_raw", ("GainRaw",)),
                ("gain_auto", ("GainAuto",)),
            )
            if (value := _pylon_node_value(camera, names)) is not None
        },
    }


def _pylon_node_value(target: Any, names: tuple[str, ...]) -> Any | None:
    for name in names:
        node = _pylon_node(target, name)
        if node is None:
            continue
        try:
            get_value = getattr(node, "GetValue", None)
        except Exception:
            get_value = None
        if callable(get_value):
            try:
                value = get_value()
            except Exception:
                value = None
        else:
            try:
                value = getattr(node, "Value", None)
            except Exception:
                value = None
            if value is None:
                try:
                    value = getattr(node, "value", None)
                except Exception:
                    value = None
        if value not in (None, ""):
            return value
    return None


def _first_positive_float(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _pylon_grab_strategy(pylon: Any, name: str) -> Any:
    normalized = re.sub(r"[^a-z0-9]+", "", name.casefold())
    strategy_name = {
        "onebyone": "GrabStrategy_OneByOne",
        "latestimageonly": "GrabStrategy_LatestImageOnly",
        "latestimages": "GrabStrategy_LatestImages",
        "upcomingimage": "GrabStrategy_UpcomingImage",
    }.get(normalized, "GrabStrategy_LatestImageOnly")
    return getattr(pylon, strategy_name, getattr(pylon, "GrabStrategy_LatestImageOnly", 0))


def _basler_image_converter(pylon: Any) -> Any | None:
    converter_type = getattr(pylon, "ImageFormatConverter", None)
    if converter_type is None:
        return None
    converter = converter_type()
    pixel_type = getattr(pylon, "PixelType_BGR8packed", None)
    if pixel_type is not None:
        converter.OutputPixelFormat = pixel_type
    bit_alignment = getattr(pylon, "OutputBitAlignment_MsbAligned", None)
    if bit_alignment is not None:
        converter.OutputBitAlignment = bit_alignment
    return converter


def _try_retrieve_pylon_result(camera: Any, pylon: Any, *, timeout_seconds: float) -> Any | None:
    if camera is None:
        return None
    timeout_ms = max(int(timeout_seconds * 1000), 1)
    timeout_handling = getattr(
        pylon,
        "TimeoutHandling_Return",
        getattr(pylon, "TimeoutHandling_ThrowException", None),
    )
    try:
        if timeout_handling is not None:
            return camera.RetrieveResult(timeout_ms, timeout_handling)
        return camera.RetrieveResult(timeout_ms)
    except TypeError:
        return camera.RetrieveResult(timeout_ms)


def _bgr_frame_from_basler_grab_result(grab_result: Any, pylon: Any, *, converter: Any | None) -> Any:
    import cv2  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]

    if converter is not None:
        try:
            converted = converter.Convert(grab_result)
            return np.asarray(converted.GetArray()).copy()
        except Exception:
            pass
    raw_array = getattr(grab_result, "Array", None)
    if raw_array is None:
        get_array = getattr(grab_result, "GetArray", None)
        if callable(get_array):
            raw_array = get_array()
    if raw_array is None:
        raise RtspSourceConfigurationError("Basler Pylon grab result did not contain an image array.")
    array = np.asarray(raw_array).copy()
    pixel_format = _basler_grab_pixel_format(grab_result, pylon)
    if array.ndim == 2:
        bayer_code = _bayer_conversion_code(cv2, pixel_format.casefold())
        if bayer_code is not None:
            return _ensure_uint8_bgr_frame(cv2.cvtColor(array, bayer_code), cv2, np)
        return _ensure_uint8_bgr_frame(cv2.cvtColor(array, cv2.COLOR_GRAY2BGR), cv2, np)
    if array.ndim == 3 and array.shape[2] == 3:
        if "rgb" in pixel_format.casefold() and "bgr" not in pixel_format.casefold():
            return _ensure_uint8_bgr_frame(cv2.cvtColor(array, cv2.COLOR_RGB2BGR), cv2, np)
        return _ensure_uint8_bgr_frame(array, cv2, np)
    if array.ndim == 3 and array.shape[2] == 4:
        if "rgba" in pixel_format.casefold():
            return _ensure_uint8_bgr_frame(cv2.cvtColor(array, cv2.COLOR_RGBA2BGR), cv2, np)
        return _ensure_uint8_bgr_frame(cv2.cvtColor(array, cv2.COLOR_BGRA2BGR), cv2, np)
    return _ensure_uint8_bgr_frame(array, cv2, np)


def _ensure_uint8_bgr_frame(frame: Any, cv2: Any, np: Any) -> Any:
    if frame.dtype != np.uint8:
        if np.issubdtype(frame.dtype, np.integer):
            max_value = max(np.iinfo(frame.dtype).max, 1)
            frame = cv2.convertScaleAbs(frame, alpha=255.0 / max_value)
        else:
            frame = np.clip(frame, 0, 255).astype("uint8")
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    return frame


def _basler_grab_pixel_format(grab_result: Any, pylon: Any) -> str:
    for method_name in ("GetPixelType", "GetPixelFormat"):
        method = getattr(grab_result, method_name, None)
        if callable(method):
            try:
                value = method()
            except Exception:
                continue
            name = _pylon_pixel_type_name(pylon, value)
            if name:
                return name
            if value is not None:
                return str(value)
    return str(getattr(grab_result, "PixelType", "") or "")


def _pylon_pixel_type_name(pylon: Any, value: Any) -> str | None:
    for name in dir(pylon):
        if not name.startswith("PixelType_"):
            continue
        try:
            if getattr(pylon, name) == value:
                return name.removeprefix("PixelType_")
        except Exception:
            continue
    return None


def _set_genicam_node_value(
    node_map: Any,
    name: str,
    value: Any,
    *,
    strict: bool,
) -> None:
    node = getattr(node_map, name, None)
    if node is None:
        if strict:
            raise RtspSourceConfigurationError(f"FLIR Blackfly GenICam node {name} is not available.")
        return
    try:
        node.value = value
    except Exception as exc:
        if strict:
            raise RtspSourceConfigurationError(
                f"FLIR Blackfly GenICam node {name} could not be set to {value!r}: {exc}"
            ) from exc


def _try_fetch_harvester_buffer(acquirer: Any, *, timeout_seconds: float) -> Any | None:
    if acquirer is None:
        return None
    try_fetch = getattr(acquirer, "try_fetch", None)
    if callable(try_fetch):
        return try_fetch(timeout=timeout_seconds)
    return acquirer.fetch(timeout=timeout_seconds)


def _cv2_frame_from_harvester_component(component: Any) -> Any:
    import cv2  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]

    width = int(component.width)
    height = int(component.height)
    data = np.asarray(component.data)
    data_format = str(getattr(component, "data_format", "") or "").lower()
    components = int(float(getattr(component, "num_components_per_pixel", 1) or 1))
    if components <= 1:
        frame = data.reshape(height, width)
        bayer_code = _bayer_conversion_code(cv2, data_format)
        if bayer_code is not None:
            frame = cv2.cvtColor(frame, bayer_code)
    else:
        frame = data.reshape(height, width, components)
        if components >= 4:
            if "rgba" in data_format:
                frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
            elif "bgra" in data_format:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            else:
                frame = frame[:, :, :3]
        elif "rgb" in data_format and "bgr" not in data_format:
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    if frame.dtype != np.uint8:
        if np.issubdtype(frame.dtype, np.integer):
            max_value = max(np.iinfo(frame.dtype).max, 1)
            frame = cv2.convertScaleAbs(frame, alpha=255.0 / max_value)
        else:
            frame = np.clip(frame, 0, 255).astype("uint8")
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    return frame


def _bayer_conversion_code(cv2: Any, data_format: str) -> int | None:
    if "bayer" not in data_format:
        return None
    if "bayerrg" in data_format:
        return cv2.COLOR_BAYER_RG2BGR
    if "bayergr" in data_format:
        return cv2.COLOR_BAYER_GR2BGR
    if "bayergb" in data_format:
        return cv2.COLOR_BAYER_GB2BGR
    if "bayerbg" in data_format:
        return cv2.COLOR_BAYER_BG2BGR
    return None


def _blackfly_source_uri(config: dict[str, Any]) -> str:
    label = (
        config.get("user_defined_name")
        or config.get("serial_number")
        or config.get("ip_address")
        or config.get("device_id")
        or config.get("id_")
        or "auto"
    )
    return f"flir-blackfly://{label}"


def _basler_transport_mode(config: dict[str, Any]) -> str:
    mode = str(config.get("transport_mode") or "auto").strip().lower().replace("-", "_")
    if mode in {"direct", "pylon"}:
        return "pylon_direct"
    if mode in {"bridge", "aravis"}:
        return "aravis_rtsp_bridge"
    if mode in {"auto", "pylon_direct", "aravis_rtsp_bridge"}:
        return mode
    return "auto"


def _basler_should_use_aravis_fallback(
    config: dict[str, Any],
    error: Exception,
) -> bool:
    if _basler_transport_mode(config) != "auto":
        return False
    message = str(error).casefold()
    return "no basler pylon devices were discovered" in message


def _basler_aravis_bridge_config(config: dict[str, Any]) -> dict[str, Any]:
    bridge_config = dict(config or {})
    bridge_config["vendor"] = "basler_pylon"
    bridge_config["camera_name"] = (
        bridge_config.get("camera_name")
        or bridge_config.get("user_defined_name")
        or bridge_config.get("serial_number")
        or bridge_config.get("ip_address")
        or bridge_config.get("device_id")
        or bridge_config.get("id_")
        or bridge_config.get("model")
        or ""
    )
    bridge_config["bridge_name"] = str(
        bridge_config.get("bridge_name")
        or bridge_config.get("camera_name")
        or "basler-pylon"
    )
    bridge_name = _aravis_bridge_name(bridge_config)
    bridge_config["bridge_name"] = bridge_name
    bridge_config["rtsp_host"] = str(bridge_config.get("rtsp_host") or "127.0.0.1")
    bridge_config["rtsp_address"] = str(bridge_config.get("rtsp_address") or "127.0.0.1")
    bridge_config["rtsp_port"] = int(
        bridge_config.get("rtsp_port") or _stable_aravis_rtsp_port(bridge_name)
    )
    bridge_config["rtsp_mount"] = _aravis_rtsp_mount(bridge_config)
    bridge_config["url"] = str(
        bridge_config.get("url")
        or bridge_config.get("rtsp_url")
        or f"rtsp://{bridge_config['rtsp_host']}:{bridge_config['rtsp_port']}"
        f"{bridge_config['rtsp_mount']}"
    )
    bridge_config["rtsp_url"] = bridge_config["url"]
    bridge_config["pixel_format"] = str(bridge_config.get("pixel_format") or "BayerRG8")
    bridge_config["conversion_mode"] = str(
        bridge_config.get("conversion_mode")
        or _basler_aravis_conversion_mode(bridge_config["pixel_format"])
    )
    bridge_config.setdefault("h264_bitrate_kbit", 6000)
    bridge_config.setdefault("h264_key_int_max", 30)
    bridge_config.setdefault("queue_size", 2)
    if (
        bridge_config.get("inter_packet_delay") is not None
        and bridge_config.get("packet_delay") is None
    ):
        bridge_config["packet_delay"] = bridge_config["inter_packet_delay"]

    feature_updates = _basler_aravis_feature_updates(bridge_config)
    existing_features = bridge_config.get("features")
    if isinstance(existing_features, dict):
        bridge_config["features"] = {**feature_updates, **existing_features}
    elif existing_features:
        generated = " ".join(
            f"{name}={value}"
            for name, value in feature_updates.items()
            if value not in (None, "")
        )
        bridge_config["features"] = f"{generated} {existing_features}".strip()
    else:
        bridge_config["features"] = feature_updates
    return bridge_config


def _stable_aravis_rtsp_port(seed: str) -> int:
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    offset = int(digest[:8], 16) % _ARAVIS_DEFAULT_RTSP_PORT_SPAN
    return _ARAVIS_DEFAULT_RTSP_PORT_BASE + offset


def _basler_aravis_conversion_mode(pixel_format: object) -> str:
    value = str(pixel_format or "").strip().lower()
    if value.startswith("mono"):
        return "mono"
    if "rgb" in value or "bgr" in value or "yuv" in value:
        return "rgb"
    return "bayer"


def _basler_aravis_feature_updates(config: dict[str, Any]) -> dict[str, Any]:
    features: dict[str, Any] = {}
    for node_name, key in (
        ("AcquisitionMode", "acquisition_mode"),
        ("PixelFormat", "pixel_format"),
        ("TriggerMode", "trigger_mode"),
        ("TriggerSource", "trigger_source"),
        ("TriggerActivation", "trigger_activation"),
        ("ExposureAuto", "exposure_auto"),
        ("ExposureTime", "exposure_time_us"),
        ("GainAuto", "gain_auto"),
        ("Gain", "gain_db"),
        ("Width", "width"),
        ("Height", "height"),
        ("OffsetX", "offset_x"),
        ("OffsetY", "offset_y"),
        ("GevSCPSPacketSize", "packet_size"),
        ("GevSCPD", "inter_packet_delay"),
    ):
        value = config.get(key)
        if value not in (None, ""):
            features[node_name] = value
    if config.get("fps") not in (None, ""):
        features["AcquisitionFrameRateEnable"] = bool(
            config.get("acquisition_frame_rate_enable", True)
        )
        features["AcquisitionFrameRate"] = config["fps"]
    return features


def _basler_source_uri(config: dict[str, Any]) -> str:
    label = (
        config.get("user_defined_name")
        or config.get("camera_name")
        or config.get("serial_number")
        or config.get("ip_address")
        or config.get("device_id")
        or config.get("id_")
        or config.get("model")
        or "auto"
    )
    return f"basler-pylon://{label}"


def _basler_config_summary(config: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "transport_mode",
        "serial_number",
        "user_defined_name",
        "camera_name",
        "device_id",
        "id_",
        "ip_address",
        "model",
        "acquisition_mode",
        "grab_strategy",
        "pixel_format",
        "fps",
        "exposure_time_us",
        "exposure_auto",
        "gain_db",
        "gain_raw",
        "gain_auto",
        "trigger_mode",
        "trigger_source",
        "trigger_activation",
        "packet_size",
        "inter_packet_delay",
        "buffer_count",
        "output_queue_size",
        "frame_retention_ms",
        "grab_failure_reconnect_threshold",
        "width",
        "height",
        "bridge_name",
        "rtsp_host",
        "rtsp_port",
        "rtsp_mount",
        "url",
        "conversion_mode",
        "h264_bitrate_kbit",
        "h264_key_int_max",
        "queue_size",
        "fetch_timeout_seconds",
        "reconnect_seconds",
    )
    return {
        key: config.get(key)
        for key in keys
        if config.get(key) not in (None, "")
    }


def _redact_inline_credentials(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.username and not parsed.password:
        return value
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return parsed._replace(netloc=f"***@{hostname}{port}").geturl()
