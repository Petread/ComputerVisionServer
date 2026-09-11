from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Full
from typing import Any, Protocol, TypeVar

import numpy as np
from cv_contracts.runtime import (
    RuntimeCameraIntrinsicCalibrationProfile,
    RuntimeImageViewProfile,
)
from cv_core import PlatformSettings
from cv_db import (
    CameraMediaBinding,
    ImageViewStreamBinding,
    list_camera_media_bindings,
    mark_channel_stream_stopped,
    record_service_heartbeat,
    session_scope,
    update_channel_stream_observation,
)
from cv_runtime import (
    CameraUndistortRemap,
    LatestFrameRingReader,
    LatestFrameRingWriter,
    apply_camera_undistort,
    apply_image_view,
    create_camera_undistort_remap,
    validate_basler_link_budget,
    validate_camera_intrinsic_calibration,
    validate_image_view_frame,
    validate_image_view_profile,
    validate_nvenc_dimensions,
)
from realtime_executor.sources import build_frame_source

from camera_media.frames import encoder_frame
from camera_media.gstreamer import NvencRtspPublisher, PublisherSnapshot
from camera_media.mediamtx import load_mediamtx_paths, path_is_ready, path_reader_count

LOGGER = logging.getLogger(__name__)
SnapshotT = TypeVar("SnapshotT")


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class StatusQueue(Protocol[SnapshotT]):
    def put_nowait(self, value: SnapshotT) -> None: ...

    def get_nowait(self) -> SnapshotT: ...

    def cancel_join_thread(self) -> None: ...

    def close(self) -> None: ...


class QueueLifecycle(Protocol):
    def cancel_join_thread(self) -> None: ...

    def close(self) -> None: ...


@dataclass
class CaptureSnapshot:
    ready: bool = False
    frame_count: int = 0
    source_fps: float = 0.0
    last_sequence: int = 0
    width: int | None = None
    height: int | None = None
    error: str | None = None
    permanent_failure: bool = False


@dataclass
class ImageViewSnapshot:
    ready: bool = False
    frame_count: int = 0
    output_fps: float = 0.0
    last_sequence: int = 0
    width: int | None = None
    height: int | None = None
    processing_latency_ms: float | None = None
    source_frame_age_ms: float | None = None
    skipped_frames: int = 0
    error: str | None = None
    permanent_failure: bool = False


class ChannelMediaHandle:
    """Supervise acquisition plus the raw and canonical publications."""

    def __init__(self, settings: PlatformSettings, binding: CameraMediaBinding) -> None:
        self.settings = settings
        self.binding = binding
        self._context = mp.get_context("spawn")
        self._capture_process: mp.Process | None = None
        self._capture_stop_event: StopSignal | None = None
        self._capture_status_queue: StatusQueue[CaptureSnapshot] | None = None
        self._publisher_process: mp.Process | None = None
        self._publisher_stop_event: StopSignal | None = None
        self._publisher_status_queue: StatusQueue[PublisherSnapshot] | None = None
        self._image_view_process: mp.Process | None = None
        self._image_view_stop_event: StopSignal | None = None
        self._image_view_status_queue: StatusQueue[ImageViewSnapshot] | None = None
        self._canonical_view_process: mp.Process | None = None
        self._canonical_view_stop_event: StopSignal | None = None
        self._canonical_view_status_queue: StatusQueue[ImageViewSnapshot] | None = None
        self._undistorted_publisher_process: mp.Process | None = None
        self._undistorted_publisher_stop_event: StopSignal | None = None
        self._undistorted_publisher_status_queue: StatusQueue[PublisherSnapshot] | None = None
        self._image_view_publisher_process: mp.Process | None = None
        self._image_view_publisher_stop_event: StopSignal | None = None
        self._image_view_publisher_status_queue: StatusQueue[PublisherSnapshot] | None = None
        self.capture_snapshot = CaptureSnapshot()
        self.publisher_snapshot: PublisherSnapshot | None = None
        self.image_view_snapshot: ImageViewSnapshot | None = None
        self.canonical_view_snapshot: ImageViewSnapshot | None = None
        self.undistorted_publisher_snapshot: PublisherSnapshot | None = None
        self.image_view_publisher_snapshot: PublisherSnapshot | None = None

    @property
    def camera_pid(self) -> int | None:
        return self._capture_process.pid if self._capture_process is not None else None

    @property
    def publisher_pid(self) -> int | None:
        return self._publisher_process.pid if self._publisher_process is not None else None

    @property
    def image_view_pid(self) -> int | None:
        return self._image_view_process.pid if self._image_view_process is not None else None

    @property
    def canonical_view_pid(self) -> int | None:
        process = self._canonical_view_process
        return process.pid if process is not None else None

    @property
    def image_view_publisher_pid(self) -> int | None:
        process = self._image_view_publisher_process
        return process.pid if process is not None else None

    @property
    def undistorted_publisher_pid(self) -> int | None:
        process = self._undistorted_publisher_process
        return process.pid if process is not None else None

    def start(self) -> None:
        self.ensure_capture_process()
        if self.binding.stream_required:
            self.start_publisher()

    def refresh_snapshots(self) -> None:
        self.capture_snapshot = _drain_latest(
            self._capture_status_queue,
            self.capture_snapshot,
        )
        self.publisher_snapshot = _drain_latest(
            self._publisher_status_queue,
            self.publisher_snapshot,
        )
        self.image_view_snapshot = _drain_latest(
            self._image_view_status_queue,
            self.image_view_snapshot,
        )
        self.canonical_view_snapshot = _drain_latest(
            self._canonical_view_status_queue,
            self.canonical_view_snapshot,
        )
        self.undistorted_publisher_snapshot = _drain_latest(
            self._undistorted_publisher_status_queue,
            self.undistorted_publisher_snapshot,
        )
        self.image_view_publisher_snapshot = _drain_latest(
            self._image_view_publisher_status_queue,
            self.image_view_publisher_snapshot,
        )

    def ensure_capture_process(self) -> None:
        self.refresh_snapshots()
        if self._capture_process is not None and self._capture_process.is_alive():
            return
        if self.capture_snapshot.permanent_failure:
            return
        _close_process(self._capture_process)
        _close_queue(self._capture_status_queue)
        self._capture_stop_event = self._context.Event()
        self._capture_status_queue = self._context.Queue(maxsize=2)
        self._capture_process = self._context.Process(
            target=_capture_process_main,
            args=(
                self.settings,
                self.binding,
                self._capture_stop_event,
                self._capture_status_queue,
            ),
            name=f"camera-capture-{self.binding.channel_id}",
            daemon=True,
        )
        self._capture_process.start()

    def start_publisher(self) -> None:
        self.refresh_snapshots()
        raw_alive = self._publisher_process is not None and self._publisher_process.is_alive()
        raw_failed = bool(
            self.publisher_snapshot is not None and self.publisher_snapshot.permanent_failure
        )
        if not raw_alive and not raw_failed:
            _close_process(self._publisher_process)
            _close_queue(self._publisher_status_queue)
            self._publisher_stop_event = self._context.Event()
            self._publisher_status_queue = self._context.Queue(maxsize=2)
            self._publisher_process = self._context.Process(
                target=_publisher_process_main,
                args=(
                    self.settings,
                    self.binding.channel_id,
                    self.binding.channel_slug,
                    self._publisher_stop_event,
                    self._publisher_status_queue,
                ),
                name=f"camera-publisher-{self.binding.channel_id}",
                daemon=True,
            )
            self._publisher_process.start()
        self._start_image_view_stream()

    def _start_image_view_stream(self) -> None:
        binding = self.binding.image_view_stream
        if binding is None:
            self._stop_image_view_stream()
            return
        if binding.has_intrinsic_undistortion:
            undistort_alive = (
                self._image_view_process is not None and self._image_view_process.is_alive()
            )
            undistort_failed = bool(
                self.image_view_snapshot is not None
                and self.image_view_snapshot.permanent_failure
            )
            if not undistort_alive and not undistort_failed:
                _close_process(self._image_view_process)
                _close_queue(self._image_view_status_queue)
                self._image_view_stop_event = self._context.Event()
                self._image_view_status_queue = self._context.Queue(maxsize=2)
                self._image_view_process = self._context.Process(
                    target=_undistorted_image_view_process_main,
                    args=(
                        self.settings,
                        self.binding.channel_id,
                        binding,
                        self._image_view_stop_event,
                        self._image_view_status_queue,
                    ),
                    name=f"camera-undistort-view-{self.binding.channel_id}",
                    daemon=True,
                )
                self._image_view_process.start()
        else:
            self._stop_undistorted_view_stream()
        canonical_alive = (
            self._canonical_view_process is not None
            and self._canonical_view_process.is_alive()
        )
        canonical_failed = bool(
            self.canonical_view_snapshot is not None
            and self.canonical_view_snapshot.permanent_failure
        )
        if not canonical_alive and not canonical_failed:
            _close_process(self._canonical_view_process)
            _close_queue(self._canonical_view_status_queue)
            self._canonical_view_stop_event = self._context.Event()
            self._canonical_view_status_queue = self._context.Queue(maxsize=2)
            self._canonical_view_process = self._context.Process(
                target=_canonical_image_view_process_main,
                args=(
                    self.settings,
                    self.binding.channel_id,
                    binding,
                    self._canonical_view_stop_event,
                    self._canonical_view_status_queue,
                ),
                name=f"camera-canonical-view-{self.binding.channel_id}",
                daemon=True,
            )
            self._canonical_view_process.start()
        # Intrinsic undistortion remains an internal preprocessing stage for the
        # canonical stream. Publishing it separately would consume a third NVENC
        # session although the operator UI now has only raw and model views.
        _stop_process(
            self._undistorted_publisher_process,
            self._undistorted_publisher_stop_event,
        )
        self._undistorted_publisher_process = None
        self._undistorted_publisher_stop_event = None
        _close_queue(self._undistorted_publisher_status_queue)
        self._undistorted_publisher_status_queue = None
        self.undistorted_publisher_snapshot = None
        publisher_alive = (
            self._image_view_publisher_process is not None
            and self._image_view_publisher_process.is_alive()
        )
        publisher_failed = bool(
            self.image_view_publisher_snapshot is not None
            and self.image_view_publisher_snapshot.permanent_failure
        )
        if not publisher_alive and not publisher_failed:
            _close_process(self._image_view_publisher_process)
            _close_queue(self._image_view_publisher_status_queue)
            self._image_view_publisher_stop_event = self._context.Event()
            self._image_view_publisher_status_queue = self._context.Queue(maxsize=2)
            self._image_view_publisher_process = self._context.Process(
                target=_publisher_process_main,
                args=(
                    self.settings,
                    self.binding.channel_id,
                    self.binding.channel_slug,
                    self._image_view_publisher_stop_event,
                    self._image_view_publisher_status_queue,
                    _image_view_ring_id(self.binding.channel_id),
                    f"channels/{self.binding.channel_slug}/model",
                    "model-publisher",
                ),
                name=f"camera-model-publisher-{self.binding.channel_id}",
                daemon=True,
            )
            self._image_view_publisher_process.start()

    def _stop_undistorted_view_stream(self, *, reset_snapshot: bool = True) -> None:
        _stop_process(self._image_view_process, self._image_view_stop_event)
        self._image_view_process = None
        self._image_view_stop_event = None
        _close_queue(self._image_view_status_queue)
        self._image_view_status_queue = None
        _stop_process(
            self._undistorted_publisher_process,
            self._undistorted_publisher_stop_event,
        )
        self._undistorted_publisher_process = None
        self._undistorted_publisher_stop_event = None
        _close_queue(self._undistorted_publisher_status_queue)
        self._undistorted_publisher_status_queue = None
        if reset_snapshot:
            self.image_view_snapshot = None
            self.undistorted_publisher_snapshot = None

    def stop_publisher(self, *, reset_snapshot: bool = True) -> None:
        _stop_process(self._publisher_process, self._publisher_stop_event)
        self._publisher_process = None
        self._publisher_stop_event = None
        _close_queue(self._publisher_status_queue)
        self._publisher_status_queue = None
        if reset_snapshot:
            self.publisher_snapshot = None
        self._stop_image_view_stream(reset_snapshot=reset_snapshot)

    def _stop_image_view_stream(self, *, reset_snapshot: bool = True) -> None:
        self._stop_undistorted_view_stream(reset_snapshot=reset_snapshot)
        _stop_process(
            self._image_view_publisher_process,
            self._image_view_publisher_stop_event,
        )
        self._image_view_publisher_process = None
        self._image_view_publisher_stop_event = None
        _close_queue(self._image_view_publisher_status_queue)
        self._image_view_publisher_status_queue = None
        _stop_process(
            self._canonical_view_process,
            self._canonical_view_stop_event,
        )
        self._canonical_view_process = None
        self._canonical_view_stop_event = None
        _close_queue(self._canonical_view_status_queue)
        self._canonical_view_status_queue = None
        if reset_snapshot:
            self.canonical_view_snapshot = None
            self.image_view_publisher_snapshot = None

    def stop(self) -> None:
        self.stop_publisher()
        _stop_process(self._capture_process, self._capture_stop_event)
        self._capture_process = None
        self._capture_stop_event = None
        _close_queue(self._capture_status_queue)
        self._capture_status_queue = None


class CameraMediaCoordinator:
    def __init__(self, *, settings: PlatformSettings, instance_id: str) -> None:
        self.settings = settings
        self.instance_id = instance_id
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="camera-media-coordinator", daemon=True
        )
        self._handles: dict[str, ChannelMediaHandle] = {}
        self._last_service_heartbeat_perf = 0.0

    def start(self) -> None:
        Path(self.settings.runtime.camera_media_runtime_dir).mkdir(parents=True, exist_ok=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        for handle in list(self._handles.values()):
            handle.stop()
        self._handles.clear()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._reconcile()
                self._record_heartbeat("running")
            except Exception:
                LOGGER.exception("Camera-media reconciliation failed.")
                self._record_heartbeat("degraded", force=True)
            self._stop_event.wait(
                float(self.settings.runtime.camera_media_reconcile_interval_seconds)
            )

    def _reconcile(self) -> None:
        with session_scope(self.settings) as session:
            bindings = {item.channel_id: item for item in list_camera_media_bindings(session)}
        media_paths, media_api_error = load_mediamtx_paths(self.settings)
        for channel_id in list(self._handles):
            if channel_id not in bindings:
                handle = self._handles.pop(channel_id)
                handle.stop()
                with session_scope(self.settings) as session:
                    mark_channel_stream_stopped(
                        session,
                        channel_id,
                        worker_instance_id=self.instance_id,
                    )
        for channel_id, binding in bindings.items():
            handle = self._handles.get(channel_id)
            if handle is None or not _same_source_binding(handle.binding, binding):
                if handle is not None:
                    handle.stop()
                handle = ChannelMediaHandle(self.settings, binding)
                self._handles[channel_id] = handle
                handle.start()
            else:
                handle.refresh_snapshots()
                handle.ensure_capture_process()
                restart_requested = (
                    binding.stream_required
                    and binding.stream_restart_token != handle.binding.stream_restart_token
                )
                handle.binding = binding
                if restart_requested:
                    handle.stop_publisher()
                    handle.start_publisher()
                elif binding.stream_required:
                    handle.start_publisher()
                else:
                    handle.stop_publisher()
            handle.refresh_snapshots()
            self._write_channel_state(
                handle,
                media_paths=media_paths,
                media_api_error=media_api_error,
            )

    def _write_channel_state(
        self,
        handle: ChannelMediaHandle,
        *,
        media_paths: dict[str, dict[str, Any]] | None,
        media_api_error: str | None,
    ) -> None:
        if not handle.binding.stream_required:
            return
        capture = handle.capture_snapshot
        publisher = handle.publisher_snapshot
        path_name = f"channels/{handle.binding.channel_slug}/live"
        path_info = media_paths.get(path_name) if media_paths is not None else None
        publication_ready = path_is_ready(path_info)
        reader_count = path_reader_count(path_info)
        image_view_binding = handle.binding.image_view_stream
        image_view = handle.image_view_snapshot
        canonical_view = handle.canonical_view_snapshot
        image_view_publisher = handle.image_view_publisher_snapshot
        image_view_path = f"channels/{handle.binding.channel_slug}/model"
        image_view_path_info = media_paths.get(image_view_path) if media_paths is not None else None
        image_view_publication_ready = path_is_ready(image_view_path_info)
        image_view_reader_count = path_reader_count(image_view_path_info)
        if capture.permanent_failure:
            state = "failed"
            error = capture.error
        elif publisher is not None and publisher.ready and capture.ready and publication_ready:
            state = "ready"
            error = None
        elif publisher is not None and publisher.permanent_failure:
            state = "failed"
            error = publisher.error
        elif capture.error or (publisher is not None and publisher.error):
            state = "recovering"
            error = (publisher.error if publisher is not None else None) or capture.error
        elif media_api_error:
            state = "recovering"
            error = media_api_error
        elif publisher is not None and publisher.ready and not publication_ready:
            state = "starting"
            error = "NVENC is running, but MediaMTX has not reported the stream path ready yet."
        else:
            state = "starting"
            error = None
        profile = {
            "width": capture.width,
            "height": capture.height,
            "fps": round(capture.source_fps, 3) if capture.source_fps else None,
            "source_pixel_format": handle.binding.source_config.get("pixel_format"),
            "encoder_pixel_format": "NV12",
            "undistorted_full": _undistorted_profile_state(
                image_view_binding,
                image_view,
                source_fps=capture.source_fps,
            ),
            "canonical_model": _image_view_profile_state(
                image_view_binding,
                canonical_view,
                source_fps=capture.source_fps,
            ),
        }
        publication_state = (
            "ready"
            if publication_ready
            else "recovering"
            if media_api_error or (publisher is not None and publisher.error)
            else "starting"
        )
        image_view_error = (
            image_view_publisher.error if image_view_publisher is not None else None
        ) or (canonical_view.error if canonical_view is not None else None) or (
            image_view.error
            if image_view_binding is not None
            and image_view_binding.has_intrinsic_undistortion
            and image_view is not None
            else None
        )
        image_view_ready = bool(
            image_view_binding is not None
            and canonical_view is not None
            and canonical_view.ready
            and image_view_publisher is not None
            and image_view_publisher.ready
            and image_view_publication_ready
        )
        image_view_publication_state = (
            "stopped"
            if image_view_binding is None
            else "ready"
            if image_view_publication_ready
            else "recovering"
            if media_api_error or image_view_error
            else "starting"
        )
        image_view_status = (
            "unavailable"
            if image_view_binding is None
            or (image_view is not None and image_view.permanent_failure)
            or (canonical_view is not None and canonical_view.permanent_failure)
            or (image_view_publisher is not None and image_view_publisher.permanent_failure)
            else "ready"
            if image_view_ready
            else "degraded"
            if image_view_error or media_api_error
            else "starting"
        )
        metrics = {
            **(publisher.metrics() if publisher is not None else {}),
            "restart_requested_at": handle.binding.stream_restart_token,
            "source_fps": round(capture.source_fps, 3),
            "captured_frames": capture.frame_count,
            "last_capture_sequence": capture.last_sequence,
            "camera_worker_pid": handle.camera_pid,
            "encoder_worker_pid": handle.publisher_pid,
            "camera_state": _camera_state(capture),
            "encoder_state": _encoder_state(publisher),
            "publication_state": publication_state,
            "browser_reader_state": "ready" if reader_count > 0 else "idle",
            "connected_clients": reader_count,
            "undistorted_full": {
                "configured": False,
                "status": "unavailable",
                "ready": False,
                "warning": "Undistortion is an internal canonical-stream preprocessing stage.",
                "transform_state": _image_view_state(image_view),
                "encoder_state": "stopped",
                "publication_state": "stopped",
                "browser_reader_state": "idle",
                "connected_clients": 0,
                "transformed_frames": image_view.frame_count if image_view else 0,
                "transform_fps": round(image_view.output_fps, 3) if image_view else 0.0,
                "transform_dropped_frames": image_view.skipped_frames if image_view else 0,
                "processing_latency_ms": (
                    image_view.processing_latency_ms if image_view else None
                ),
                "transform_source_frame_age_ms": (
                    image_view.source_frame_age_ms if image_view else None
                ),
                "image_view_worker_pid": handle.image_view_pid,
                "encoder_worker_pid": None,
            },
            "canonical_model": {
                **(image_view_publisher.metrics() if image_view_publisher is not None else {}),
                "configured": image_view_binding is not None,
                "status": image_view_status,
                "ready": image_view_ready,
                "warning": (
                    "Assigned pipeline has no unambiguous canonical model-view preprocessing."
                    if image_view_binding is None
                    else image_view_error
                    or (
                        "Canonical NVENC is running, but MediaMTX has not reported the "
                        "model stream path ready yet."
                        if image_view_publisher is not None
                        and image_view_publisher.ready
                        and not image_view_publication_ready
                        else None
                    )
                ),
                "transform_state": _image_view_state(canonical_view),
                "encoder_state": _encoder_state(image_view_publisher),
                "publication_state": image_view_publication_state,
                "browser_reader_state": ("ready" if image_view_reader_count > 0 else "idle"),
                "connected_clients": image_view_reader_count,
                "transformed_frames": canonical_view.frame_count if canonical_view else 0,
                "transform_fps": (
                    round(canonical_view.output_fps, 3) if canonical_view else 0.0
                ),
                "transform_dropped_frames": (
                    canonical_view.skipped_frames if canonical_view else 0
                ),
                "processing_latency_ms": (
                    canonical_view.processing_latency_ms if canonical_view else None
                ),
                "transform_source_frame_age_ms": (
                    canonical_view.source_frame_age_ms if canonical_view else None
                ),
                "image_view_worker_pid": handle.canonical_view_pid,
                "encoder_worker_pid": handle.image_view_publisher_pid,
            },
        }
        with session_scope(self.settings) as session:
            update_channel_stream_observation(
                session,
                handle.binding.channel_id,
                actual_state=state,
                worker_instance_id=self.instance_id,
                error=error,
                profile=profile,
                metrics=metrics,
            )

    def _record_heartbeat(self, status: str, *, force: bool = False) -> None:
        now = time.perf_counter()
        heartbeat_interval = float(self.settings.runtime.camera_media_heartbeat_interval_seconds)
        if not force and now - self._last_service_heartbeat_perf < heartbeat_interval:
            return
        try:
            with session_scope(self.settings) as session:
                record_service_heartbeat(
                    session,
                    service_name="camera-media",
                    instance_id=self.instance_id,
                    status=status,
                    details={"active_channels": len(self._handles)},
                )
            self._last_service_heartbeat_perf = now
        except Exception:
            LOGGER.exception("Camera-media service heartbeat failed.")


def _capture_process_main(
    settings: PlatformSettings,
    binding: CameraMediaBinding,
    stop_event: StopSignal,
    status_queue: StatusQueue[CaptureSnapshot],
) -> None:
    snapshot = CaptureSnapshot()
    ring: LatestFrameRingWriter | None = None
    events: deque[float] = deque(maxlen=1000)
    while not stop_event.is_set():
        source = None
        try:
            source_config = _strict_camera_source_config(
                binding.source_type,
                binding.source_config,
            )
            if binding.source_type == "basler_pylon":
                validate_basler_link_budget(source_config, required=False)
            source = build_frame_source(
                source_type=binding.source_type,
                config=source_config,
                secret_refs=binding.source_secret_refs,
            )
            while not stop_event.is_set():
                packet = source.read(stop_event)
                if packet is None or packet.raw_frame is None:
                    source_health = getattr(source, "health", None)
                    source_error = getattr(source_health, "last_error", None)
                    if source_error:
                        snapshot.ready = False
                        snapshot.error = str(source_error)
                        snapshot.permanent_failure = False
                        _publish_latest(status_queue, replace(snapshot))
                    continue
                frame = encoder_frame(packet.raw_frame)
                height, width = frame.shape[:2]
                validate_nvenc_dimensions(width, height)
                if binding.source_type == "basler_pylon":
                    capture_profile = packet.metadata.get("capture_profile")
                    if not isinstance(capture_profile, dict):
                        capture_profile = {}
                    effective = {
                        **source_config,
                        **capture_profile,
                        "width": width,
                        "height": height,
                        "fps": capture_profile.get("fps") or source_config.get("fps") or 30.0,
                        "link_speed_mbps": (
                            capture_profile.get("link_speed_mbps")
                            or source_config.get("link_speed_mbps")
                            or 1000.0
                        ),
                        "pixel_format": (
                            capture_profile.get("pixel_format") or source_config.get("pixel_format")
                        ),
                    }
                    validate_basler_link_budget(effective, required=True)
                if ring is None or not ring.accepts(frame):
                    if ring is not None:
                        ring.close()
                    ring = LatestFrameRingWriter(
                        channel_id=binding.channel_id,
                        frame=frame,
                        runtime_dir=Path(settings.runtime.camera_media_runtime_dir),
                        slot_count=int(settings.runtime.camera_media_frame_slots),
                        source_uri=packet.source_uri,
                        source_metadata={
                            "source_type": binding.source_type,
                            **dict(packet.metadata or {}),
                        },
                        configured_fps=_configured_source_fps(binding.source_config),
                    )
                ring.write(
                    frame,
                    sequence=packet.sequence,
                    capture_monotonic_ns=time.monotonic_ns(),
                )
                now = time.monotonic()
                events.append(now)
                cutoff = now - 5.0
                while events and events[0] < cutoff:
                    events.popleft()
                snapshot.ready = True
                snapshot.error = None
                snapshot.permanent_failure = False
                snapshot.frame_count += 1
                snapshot.source_fps = _event_rate(events)
                snapshot.last_sequence = packet.sequence
                snapshot.width = width
                snapshot.height = height
                _publish_latest(status_queue, replace(snapshot))
        except Exception as exc:
            snapshot.ready = False
            snapshot.error = str(exc)
            snapshot.permanent_failure = isinstance(exc, ValueError)
            _publish_latest(status_queue, replace(snapshot))
            LOGGER.exception("Camera capture failed for channel %s", binding.channel_id)
            if snapshot.permanent_failure:
                break
            stop_event.wait(float(settings.runtime.camera_media_source_retry_seconds))
        finally:
            if source is not None:
                source.close()
    if ring is not None:
        ring.close()


def _publisher_process_main(
    settings: PlatformSettings,
    channel_id: str,
    channel_slug: str,
    stop_event: StopSignal,
    status_queue: StatusQueue[PublisherSnapshot],
    ring_channel_id: str | None = None,
    stream_path: str | None = None,
    status_suffix: str = "publisher",
) -> None:
    publisher = NvencRtspPublisher(
        settings=settings,
        channel_id=channel_id,
        channel_slug=channel_slug,
        stop_event=stop_event,
        ring_channel_id=ring_channel_id,
        stream_path=stream_path,
        status_suffix=status_suffix,
    )
    reporter_stop = threading.Event()

    def report() -> None:
        while not reporter_stop.wait(0.2):
            _publish_latest(status_queue, replace(publisher.snapshot))

    reporter = threading.Thread(target=report, name=f"publisher-status-{channel_id}", daemon=True)
    reporter.start()
    try:
        publisher.run()
    finally:
        reporter_stop.set()
        reporter.join(timeout=1.0)
        _publish_latest(status_queue, replace(publisher.snapshot))


def _undistorted_image_view_process_main(
    settings: PlatformSettings,
    channel_id: str,
    binding: ImageViewStreamBinding,
    stop_event: StopSignal,
    status_queue: StatusQueue[ImageViewSnapshot],
) -> None:
    snapshot = ImageViewSnapshot()
    reader = LatestFrameRingReader(
        runtime_dir=Path(settings.runtime.camera_media_runtime_dir),
        channel_id=channel_id,
    )
    writer: LatestFrameRingWriter | None = None
    events: deque[float] = deque(maxlen=1000)
    try:
        intrinsic = validate_camera_intrinsic_calibration(binding.intrinsic_calibration)
        if intrinsic.source_channel_id != channel_id:
            raise ValueError("intrinsic calibration belongs to a different runtime channel")
        validate_nvenc_dimensions(intrinsic.image_width, intrinsic.image_height)
        remap = create_camera_undistort_remap(intrinsic)
        while not stop_event.is_set():
            shared = reader.read_latest(stop_event=stop_event, timeout_seconds=0.5)
            if shared is None:
                continue
            started = time.perf_counter()
            undistorted = _undistorted_image_view_frame(
                shared.frame,
                intrinsic,
                undistort_interpolation=binding.undistort_interpolation,
                remap=remap,
            )
            shared_metadata = {
                **dict(shared.descriptor.source_metadata or {}),
                "pipeline_version_id": binding.pipeline_version_id,
                "undistort_node_key": binding.undistort_node_key,
                "calibration_hash": intrinsic.calibration_hash,
                "frame_role": "intrinsically_undistorted_full_frame",
            }
            if writer is None or not writer.accepts(undistorted):
                if writer is not None:
                    writer.close()
                writer = LatestFrameRingWriter(
                    channel_id=_undistorted_image_view_ring_id(channel_id),
                    frame=undistorted,
                    runtime_dir=Path(settings.runtime.camera_media_runtime_dir),
                    slot_count=int(settings.runtime.camera_media_frame_slots),
                    source_uri=shared.descriptor.source_uri,
                    source_metadata=shared_metadata,
                    configured_fps=shared.descriptor.configured_fps,
                )
            writer.write(
                undistorted,
                sequence=shared.sequence,
                capture_monotonic_ns=shared.capture_monotonic_ns,
                capture_wall_time_ns=shared.capture_wall_time_ns,
            )
            now = time.perf_counter()
            events.append(now)
            cutoff = now - 5.0
            while events and events[0] < cutoff:
                events.popleft()
            if snapshot.last_sequence and shared.sequence > snapshot.last_sequence + 1:
                snapshot.skipped_frames += shared.sequence - snapshot.last_sequence - 1
            snapshot.ready = True
            snapshot.error = None
            snapshot.permanent_failure = False
            snapshot.frame_count += 1
            snapshot.output_fps = _event_rate(events)
            snapshot.last_sequence = shared.sequence
            snapshot.width = intrinsic.image_width
            snapshot.height = intrinsic.image_height
            snapshot.processing_latency_ms = round((now - started) * 1000, 3)
            snapshot.source_frame_age_ms = round(
                max(time.monotonic_ns() - shared.capture_monotonic_ns, 0) / 1_000_000,
                3,
            )
            _publish_latest(status_queue, replace(snapshot))
    except Exception as exc:
        snapshot.ready = False
        snapshot.error = str(exc)
        snapshot.permanent_failure = isinstance(exc, ValueError)
        _publish_latest(status_queue, replace(snapshot))
        LOGGER.exception("Undistorted image-view stream failed for channel %s", channel_id)
    finally:
        reader.close()
        if writer is not None:
            writer.close()


def _canonical_image_view_process_main(
    settings: PlatformSettings,
    channel_id: str,
    binding: ImageViewStreamBinding,
    stop_event: StopSignal,
    status_queue: StatusQueue[ImageViewSnapshot],
) -> None:
    snapshot = ImageViewSnapshot()
    source_ring_id = (
        _undistorted_image_view_ring_id(channel_id)
        if binding.has_intrinsic_undistortion
        else channel_id
    )
    reader = LatestFrameRingReader(
        runtime_dir=Path(settings.runtime.camera_media_runtime_dir),
        channel_id=source_ring_id,
    )
    writer: LatestFrameRingWriter | None = None
    events: deque[float] = deque(maxlen=1000)
    try:
        canonical_steps = _validated_canonical_steps(binding, channel_id=channel_id)
        while not stop_event.is_set():
            shared = reader.read_latest(stop_event=stop_event, timeout_seconds=0.5)
            if shared is None:
                continue
            started = time.perf_counter()
            canonical = _apply_canonical_steps(shared.frame, canonical_steps)
            content_height, content_width = canonical.shape[:2]
            encoded = _pad_frame_for_nvenc(canonical)
            encoded_height, encoded_width = encoded.shape[:2]
            shared_metadata = {
                **dict(shared.descriptor.source_metadata or {}),
                "image_view_node_key": binding.image_view_node_key,
                "canonical_preprocessing": [
                    {
                        "node_key": item.get("node_key"),
                        "node_type": item.get("node_type"),
                    }
                    for item in binding.canonical_preprocessing
                ],
                "view_profile_id": (
                    binding.view_profile.get("view_profile_id")
                    if binding.view_profile is not None
                    else None
                ),
                "view_profile_checksum": (
                    binding.view_profile.get("profile_checksum")
                    if binding.view_profile is not None
                    else None
                ),
                "content_width": content_width,
                "content_height": content_height,
                "encoder_padding_right": encoded_width - content_width,
                "encoder_padding_bottom": encoded_height - content_height,
                "frame_role": "canonical_model_view",
            }
            if writer is None or not writer.accepts(encoded):
                if writer is not None:
                    writer.close()
                writer = LatestFrameRingWriter(
                    channel_id=_image_view_ring_id(channel_id),
                    frame=encoded,
                    runtime_dir=Path(settings.runtime.camera_media_runtime_dir),
                    slot_count=int(settings.runtime.camera_media_frame_slots),
                    source_uri=shared.descriptor.source_uri,
                    source_metadata=shared_metadata,
                    configured_fps=shared.descriptor.configured_fps,
                )
            writer.write(
                encoded,
                sequence=shared.sequence,
                capture_monotonic_ns=shared.capture_monotonic_ns,
                capture_wall_time_ns=shared.capture_wall_time_ns,
            )
            now = time.perf_counter()
            events.append(now)
            cutoff = now - 5.0
            while events and events[0] < cutoff:
                events.popleft()
            if snapshot.last_sequence and shared.sequence > snapshot.last_sequence + 1:
                snapshot.skipped_frames += shared.sequence - snapshot.last_sequence - 1
            snapshot.ready = True
            snapshot.error = None
            snapshot.permanent_failure = False
            snapshot.frame_count += 1
            snapshot.output_fps = _event_rate(events)
            snapshot.last_sequence = shared.sequence
            snapshot.width = encoded_width
            snapshot.height = encoded_height
            snapshot.processing_latency_ms = round((now - started) * 1000, 3)
            snapshot.source_frame_age_ms = round(
                max(time.monotonic_ns() - shared.capture_monotonic_ns, 0) / 1_000_000,
                3,
            )
            _publish_latest(status_queue, replace(snapshot))
    except Exception as exc:
        snapshot.ready = False
        snapshot.error = str(exc)
        snapshot.permanent_failure = isinstance(exc, ValueError)
        _publish_latest(status_queue, replace(snapshot))
        LOGGER.exception("Canonical image-view stream failed for channel %s", channel_id)
    finally:
        reader.close()
        if writer is not None:
            writer.close()


def _validated_canonical_steps(
    binding: ImageViewStreamBinding,
    *,
    channel_id: str,
) -> tuple[tuple[str, dict[str, Any] | RuntimeImageViewProfile], ...]:
    raw_steps = binding.canonical_preprocessing
    if not raw_steps and binding.view_profile is not None:
        raw_steps = (
            {
                "node_key": binding.image_view_node_key,
                "node_type": "preprocessing.process_coordinate_rectify",
                "config": {"view_profile": binding.view_profile},
            },
        )
    validated: list[tuple[str, dict[str, Any] | RuntimeImageViewProfile]] = []
    for item in raw_steps:
        node_type = str(item.get("node_type") or "")
        config = item.get("config")
        if not isinstance(config, dict):
            raise ValueError(f"canonical preprocessing {node_type} has no configuration")
        if node_type == "preprocessing.process_coordinate_rectify":
            profile = validate_image_view_profile(config.get("view_profile"))
            if binding.has_intrinsic_undistortion:
                intrinsic = validate_camera_intrinsic_calibration(binding.intrinsic_calibration)
                validate_image_view_frame(
                    profile,
                    image_width=intrinsic.image_width,
                    image_height=intrinsic.image_height,
                    intrinsic_calibration_hash=intrinsic.calibration_hash,
                    source_channel_id=channel_id,
                )
            validated.append((node_type, profile))
        elif node_type in {"preprocessing.crop", "preprocessing.resize"}:
            validated.append((node_type, dict(config)))
        else:
            raise ValueError(f"unsupported canonical preprocessing node: {node_type}")
    if not validated:
        raise ValueError("canonical model stream has no spatial preprocessing")
    return tuple(validated)


def _apply_canonical_steps(
    frame: object,
    steps: tuple[tuple[str, dict[str, Any] | RuntimeImageViewProfile], ...],
) -> Any:
    image = np.asarray(frame)
    for node_type, config in steps:
        if node_type == "preprocessing.crop":
            assert isinstance(config, dict)
            image = _crop_canonical_frame(image, config)
        elif node_type == "preprocessing.resize":
            assert isinstance(config, dict)
            image = _resize_canonical_frame(image, config)
        else:
            assert isinstance(config, RuntimeImageViewProfile)
            image = np.asarray(apply_image_view(image, config))
    return np.ascontiguousarray(image)


def _crop_canonical_frame(frame: Any, config: dict[str, Any]) -> Any:
    height, width = frame.shape[:2]
    x = max(int(config.get("x") or 0), 0)
    y = max(int(config.get("y") or 0), 0)
    crop_width = max(int(config.get("width") or width), 1)
    crop_height = max(int(config.get("height") or height), 1)
    right = min(x + crop_width, width)
    bottom = min(y + crop_height, height)
    if x >= right or y >= bottom:
        raise ValueError("canonical crop lies outside the camera frame")
    return np.ascontiguousarray(frame[y:bottom, x:right])


def _resize_canonical_frame(
    frame: Any,
    config: dict[str, Any],
) -> Any:
    try:
        import cv2
    except ImportError as exc:
        raise ValueError("canonical resize requires OpenCV") from exc
    target_width = max(int(config.get("width") or 1280), 1)
    target_height = max(int(config.get("height") or 720), 1)
    height, width = frame.shape[:2]
    keep_aspect = bool(config.get("keep_aspect", config.get("keep_aspect_ratio", True)))
    if keep_aspect:
        scale = min(target_width / max(width, 1), target_height / max(height, 1), 1.0)
        target_width = max(int(round(width * scale)), 1)
        target_height = max(int(round(height * scale)), 1)
    interpolation = (
        cv2.INTER_AREA
        if target_width < width or target_height < height
        else cv2.INTER_LINEAR
    )
    return np.ascontiguousarray(
        cv2.resize(frame, (target_width, target_height), interpolation=interpolation)
    )


def _pad_frame_for_nvenc(frame: Any) -> Any:
    height, width = frame.shape[:2]
    target_width = width + (width % 2)
    target_height = height + (height % 2)
    if target_width == width and target_height == height:
        return np.ascontiguousarray(frame)
    padded = np.zeros((target_height, target_width, 3), dtype=np.uint8)
    padded[:height, :width] = frame
    validate_nvenc_dimensions(target_width, target_height)
    return padded


def _publish_latest(status_queue: StatusQueue[SnapshotT], value: SnapshotT) -> None:
    try:
        status_queue.put_nowait(value)
        return
    except Full:
        pass
    try:
        status_queue.get_nowait()
    except Empty:
        pass
    try:
        status_queue.put_nowait(value)
    except Full:
        pass


def _drain_latest(
    status_queue: StatusQueue[SnapshotT] | None,
    current: SnapshotT,
) -> SnapshotT:
    if status_queue is None:
        return current
    latest = current
    while True:
        try:
            latest = status_queue.get_nowait()
        except Empty:
            return latest


def _stop_process(process: mp.Process | None, stop_event: StopSignal | None) -> None:
    if stop_event is not None:
        stop_event.set()
    if process is None:
        return
    process.join(timeout=3.0)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1.0)
    _close_process(process)


def _close_process(process: mp.Process | None) -> None:
    if process is None or process.is_alive():
        return
    try:
        process.close()
    except ValueError:
        pass


def _close_queue(status_queue: QueueLifecycle | None) -> None:
    if status_queue is None:
        return
    status_queue.cancel_join_thread()
    status_queue.close()


def _same_source_binding(left: CameraMediaBinding, right: CameraMediaBinding) -> bool:
    return (
        left.channel_id == right.channel_id
        and left.channel_slug == right.channel_slug
        and left.source_type == right.source_type
        and left.source_config == right.source_config
        and left.source_secret_refs == right.source_secret_refs
        and left.image_view_stream == right.image_view_stream
    )


def _image_view_ring_id(channel_id: str) -> str:
    return f"{channel_id}.canonical-model"


def _undistorted_image_view_ring_id(channel_id: str) -> str:
    return f"{channel_id}.undistorted-full"


def _canonical_image_view_frame(
    frame: object,
    intrinsic: RuntimeCameraIntrinsicCalibrationProfile,
    view_profile: RuntimeImageViewProfile,
    *,
    undistort_interpolation: str,
    remap: CameraUndistortRemap | None = None,
) -> object:
    undistorted = _undistorted_image_view_frame(
        frame,
        intrinsic,
        undistort_interpolation=undistort_interpolation,
        remap=remap,
    )
    return apply_image_view(undistorted, view_profile)


def _undistorted_image_view_frame(
    frame: object,
    intrinsic: RuntimeCameraIntrinsicCalibrationProfile,
    *,
    undistort_interpolation: str,
    remap: CameraUndistortRemap | None = None,
) -> object:
    return apply_camera_undistort(
        frame,
        intrinsic,
        interpolation=undistort_interpolation,
        remap=remap,
    )


def _undistorted_profile_state(
    binding: ImageViewStreamBinding | None,
    snapshot: ImageViewSnapshot | None,
    *,
    source_fps: float,
) -> dict[str, Any]:
    if binding is None or not binding.has_intrinsic_undistortion:
        return {"configured": False}
    intrinsic = binding.intrinsic_calibration
    assert intrinsic is not None
    return {
        "configured": True,
        "pipeline_version_id": binding.pipeline_version_id,
        "calibration_hash": intrinsic.get("calibration_hash"),
        "width": intrinsic.get("image_width"),
        "height": intrinsic.get("image_height"),
        "fps": round(snapshot.output_fps, 3)
        if snapshot is not None and snapshot.output_fps
        else round(source_fps, 3)
        if source_fps
        else None,
        "encoder_pixel_format": "NV12",
    }


def _image_view_profile_state(
    binding: ImageViewStreamBinding | None,
    snapshot: ImageViewSnapshot | None,
    *,
    source_fps: float,
) -> dict[str, Any]:
    if binding is None:
        return {"configured": False}
    profile = binding.view_profile or {}
    configured_width, configured_height = _configured_canonical_dimensions(binding)
    return {
        "configured": True,
        "pipeline_version_id": binding.pipeline_version_id,
        "view_profile_id": profile.get("view_profile_id"),
        "profile_checksum": profile.get("profile_checksum"),
        "width": snapshot.width if snapshot is not None else configured_width,
        "height": snapshot.height if snapshot is not None else configured_height,
        "fps": round(snapshot.output_fps, 3)
        if snapshot is not None and snapshot.output_fps
        else round(source_fps, 3)
        if source_fps
        else None,
        "encoder_pixel_format": "NV12",
        "transform_node_keys": [
            item.get("node_key") for item in binding.canonical_preprocessing
        ],
        "transform_node_types": [
            item.get("node_type") for item in binding.canonical_preprocessing
        ],
    }


def _configured_canonical_dimensions(
    binding: ImageViewStreamBinding,
) -> tuple[int | None, int | None]:
    for item in reversed(binding.canonical_preprocessing):
        config = item.get("config")
        if not isinstance(config, dict):
            continue
        node_type = item.get("node_type")
        if node_type in {"preprocessing.crop", "preprocessing.resize"}:
            width = config.get("width")
            height = config.get("height")
            return (
                int(width) if isinstance(width, (int, float)) else None,
                int(height) if isinstance(height, (int, float)) else None,
            )
        if node_type == "preprocessing.process_coordinate_rectify":
            view_profile = config.get("view_profile")
            if isinstance(view_profile, dict):
                width = view_profile.get("output_width")
                height = view_profile.get("output_height")
                return (
                    int(width) if isinstance(width, (int, float)) else None,
                    int(height) if isinstance(height, (int, float)) else None,
                )
    return None, None


def _configured_source_fps(config: dict[str, Any]) -> float:
    try:
        return max(float(config.get("fps") or 30.0), 0.1)
    except (TypeError, ValueError):
        return 30.0


def _strict_camera_source_config(
    source_type: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    strict = dict(config or {})
    if source_type == "aravis_gige":
        raise ValueError(
            "The legacy Aravis RTSP/x264 bridge is not supported by camera-media. "
            "Use a direct GenICam/Pylon camera source or an external RTSP source."
        )
    if source_type == "basler_pylon":
        requested = str(strict.get("transport_mode") or "auto").strip().lower().replace("-", "_")
        if requested in {"bridge", "aravis", "aravis_rtsp_bridge"}:
            raise ValueError(
                "Basler camera-media requires direct Pylon acquisition; the Aravis RTSP/x264 "
                "bridge would add a second encoder."
            )
        strict["transport_mode"] = "pylon_direct"
    return strict


def _event_rate(events: deque[float]) -> float:
    if len(events) < 2:
        return 0.0
    return (len(events) - 1) / max(events[-1] - events[0], 0.001)


def _camera_state(snapshot: CaptureSnapshot) -> str:
    if snapshot.permanent_failure:
        return "failed"
    if snapshot.ready:
        return "ready"
    if snapshot.error:
        return "recovering"
    return "starting"


def _encoder_state(publisher: PublisherSnapshot | None) -> str:
    if publisher is None:
        return "stopped"
    if publisher.permanent_failure:
        return "failed"
    if publisher.ready:
        return "ready"
    if publisher.error:
        return "recovering"
    return "starting"


def _image_view_state(snapshot: ImageViewSnapshot | None) -> str:
    if snapshot is None:
        return "stopped"
    if snapshot.permanent_failure:
        return "failed"
    if snapshot.ready:
        return "ready"
    if snapshot.error:
        return "recovering"
    return "starting"
