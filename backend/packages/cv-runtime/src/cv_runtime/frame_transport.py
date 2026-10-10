from __future__ import annotations

import json
import os
import re
import struct
import time
import uuid
from dataclasses import asdict, dataclass, field
from multiprocessing import resource_tracker, shared_memory
from pathlib import Path
from threading import Event
from typing import Any

import numpy as np

_MAGIC = b"YBBFRM01"
_GLOBAL_HEADER = struct.Struct("<8sIIQQ")
_SLOT_HEADER = struct.Struct("<QQQIIIIQQ")
_GLOBAL_HEADER_SIZE = 64
_SLOT_HEADER_SIZE = 64
_MANIFEST_VERSION = 1


@dataclass(frozen=True)
class FrameRingDescriptor:
    schema_version: int
    channel_id: str
    generation: str
    shared_memory_name: str
    slot_count: int
    slot_capacity: int
    width: int
    height: int
    channels: int
    dtype: str
    source_uri: str | None = None
    source_metadata: dict[str, Any] = field(default_factory=dict)
    configured_fps: float | None = None
    owner_pid: int = 0
    created_at_ns: int = 0

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> FrameRingDescriptor:
        return cls(
            schema_version=int(payload["schema_version"]),
            channel_id=str(payload["channel_id"]),
            generation=str(payload["generation"]),
            shared_memory_name=str(payload["shared_memory_name"]),
            slot_count=int(payload["slot_count"]),
            slot_capacity=int(payload["slot_capacity"]),
            width=int(payload["width"]),
            height=int(payload["height"]),
            channels=int(payload["channels"]),
            dtype=str(payload["dtype"]),
            source_uri=str(payload["source_uri"]) if payload.get("source_uri") else None,
            source_metadata=dict(payload.get("source_metadata") or {}),
            configured_fps=(
                float(payload["configured_fps"])
                if payload.get("configured_fps") is not None
                else None
            ),
            owner_pid=int(payload.get("owner_pid") or 0),
            created_at_ns=int(payload.get("created_at_ns") or 0),
        )


@dataclass(frozen=True)
class SharedFrame:
    sequence: int
    capture_monotonic_ns: int
    capture_wall_time_ns: int
    frame: np.ndarray
    descriptor: FrameRingDescriptor


def frame_manifest_path(runtime_dir: str | Path, channel_id: str) -> Path:
    safe_channel_id = re.sub(r"[^A-Za-z0-9_.-]", "_", channel_id)
    return Path(runtime_dir) / f"{safe_channel_id}.json"


class SharedFrameRingWriter:
    """Single-writer, multi-reader latest-frame ring backed by POSIX shared memory."""

    def __init__(
        self,
        *,
        runtime_dir: str | Path,
        channel_id: str,
        frame: np.ndarray,
        slot_count: int = 4,
        source_uri: str | None = None,
        source_metadata: dict[str, Any] | None = None,
        configured_fps: float | None = None,
    ) -> None:
        contiguous = _validated_frame(frame)
        height, width, channels = _frame_shape(contiguous)
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.channel_id = channel_id
        self.slot_count = max(int(slot_count), 2)
        self.slot_capacity = int(contiguous.nbytes)
        generation = uuid.uuid4().hex
        shm_name = f"ybb_{_safe_shm_component(channel_id)}_{generation[:12]}"
        total_size = _GLOBAL_HEADER_SIZE + self.slot_count * (
            _SLOT_HEADER_SIZE + self.slot_capacity
        )
        self._shm = shared_memory.SharedMemory(name=shm_name, create=True, size=total_size)
        self._closed = False
        self._sequence = 0
        self.descriptor = FrameRingDescriptor(
            schema_version=_MANIFEST_VERSION,
            channel_id=channel_id,
            generation=generation,
            shared_memory_name=shm_name,
            slot_count=self.slot_count,
            slot_capacity=self.slot_capacity,
            width=width,
            height=height,
            channels=channels,
            dtype=str(contiguous.dtype),
            source_uri=source_uri,
            source_metadata=_json_safe_dict(source_metadata or {}),
            configured_fps=configured_fps,
            owner_pid=os.getpid(),
            created_at_ns=time.time_ns(),
        )
        self._shm.buf[:] = b"\x00" * total_size
        _GLOBAL_HEADER.pack_into(
            self._shm.buf,
            0,
            _MAGIC,
            _MANIFEST_VERSION,
            self.slot_count,
            self.slot_capacity,
            0,
        )
        self._write_manifest()

    @property
    def sequence(self) -> int:
        return self._sequence

    def compatible(self, frame: np.ndarray) -> bool:
        contiguous = np.asarray(frame)
        height, width, channels = _frame_shape(contiguous)
        return (
            contiguous.dtype == np.dtype(self.descriptor.dtype)
            and contiguous.nbytes <= self.slot_capacity
            and width == self.descriptor.width
            and height == self.descriptor.height
            and channels == self.descriptor.channels
        )

    def accepts(self, frame: np.ndarray) -> bool:
        return self.compatible(frame)

    def write(
        self,
        frame: np.ndarray,
        *,
        sequence: int | None = None,
        capture_monotonic_ns: int | None = None,
        capture_wall_time_ns: int | None = None,
    ) -> int:
        if self._closed:
            raise RuntimeError("Shared frame ring is closed.")
        contiguous = _validated_frame(frame)
        if not self.compatible(contiguous):
            raise ValueError(
                "Frame shape or dtype changed; create a new shared frame ring generation."
            )
        next_sequence = max(int(sequence or self._sequence + 1), self._sequence + 1)
        slot_index = next_sequence % self.slot_count
        slot_offset = _GLOBAL_HEADER_SIZE + slot_index * (_SLOT_HEADER_SIZE + self.slot_capacity)
        payload_offset = slot_offset + _SLOT_HEADER_SIZE
        capture_monotonic_ns = int(capture_monotonic_ns or time.monotonic_ns())
        capture_wall_time_ns = int(capture_wall_time_ns or time.time_ns())
        height, width, channels = _frame_shape(contiguous)
        stride = int(contiguous.strides[0])
        payload_size = int(contiguous.nbytes)

        # A zero begin sequence marks the slot as being written. Readers only
        # accept a slot when begin and end sequences match.
        struct.pack_into("<Q", self._shm.buf, slot_offset, 0)
        self._shm.buf[payload_offset : payload_offset + payload_size] = contiguous.reshape(-1)
        _SLOT_HEADER.pack_into(
            self._shm.buf,
            slot_offset,
            next_sequence,
            capture_monotonic_ns,
            capture_wall_time_ns,
            width,
            height,
            channels,
            stride,
            payload_size,
            next_sequence,
        )
        struct.pack_into("<Q", self._shm.buf, _GLOBAL_HEADER.size - 8, next_sequence)
        self._sequence = next_sequence
        return next_sequence

    def close(self, *, unlink: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        manifest = frame_manifest_path(self.runtime_dir, self.channel_id)
        try:
            current = load_frame_ring_descriptor(self.runtime_dir, self.channel_id)
        except (FileNotFoundError, ValueError, OSError):
            current = None
        if current is not None and current.generation == self.descriptor.generation:
            manifest.unlink(missing_ok=True)
        self._shm.close()
        if unlink:
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass

    def _write_manifest(self) -> None:
        path = frame_manifest_path(self.runtime_dir, self.channel_id)
        temporary = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(asdict(self.descriptor), separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)


class SharedFrameRingReader:
    def __init__(self, *, runtime_dir: str | Path, channel_id: str) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.channel_id = channel_id
        self.descriptor: FrameRingDescriptor | None = None
        self._shm: shared_memory.SharedMemory | None = None
        self._last_sequence = 0
        self.overwritten_frames = 0
        self.skipped_frames = 0

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    def read_latest(
        self,
        *,
        stop_event: Event | None = None,
        timeout_seconds: float = 1.0,
    ) -> SharedFrame | None:
        return self._read(stop_event=stop_event, timeout_seconds=timeout_seconds, ordered=False)

    def read_next(
        self, *, stop_event: Event | None = None, timeout_seconds: float = 1.0,
    ) -> SharedFrame | None:
        """Read the oldest unread frame still present in the unchanged ring format."""
        return self._read(stop_event=stop_event, timeout_seconds=timeout_seconds, ordered=True)

    def _read(self, *, stop_event: Event | None, timeout_seconds: float,
              ordered: bool) -> SharedFrame | None:
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while stop_event is None or not stop_event.is_set():
            try:
                self._attach_current_generation()
                frame = self._read_once(ordered=ordered)
            except (FileNotFoundError, ValueError, OSError):
                self._detach()
                frame = None
            if frame is not None:
                return frame
            if time.monotonic() >= deadline:
                return None
            if stop_event is not None:
                stop_event.wait(0.005)
            else:
                time.sleep(0.005)
        return None

    def close(self) -> None:
        self._detach()

    @classmethod
    def attach(
        cls,
        *,
        runtime_dir: str | Path,
        channel_id: str,
        timeout_seconds: float = 1.0,
    ) -> SharedFrameRingReader:
        reader = cls(runtime_dir=runtime_dir, channel_id=channel_id)
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            try:
                reader._attach_current_generation()
                return reader
            except (FileNotFoundError, ValueError, OSError) as exc:
                reader._detach()
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"No shared camera frame ring is ready for channel {channel_id}."
                    ) from exc
                time.sleep(0.01)

    def latest(self, *, after_sequence: int = 0) -> SharedFrame | None:
        if after_sequence > self._last_sequence:
            self._last_sequence = after_sequence
        return self.read_latest(timeout_seconds=0.0)

    def _attach_current_generation(self) -> None:
        descriptor = load_frame_ring_descriptor(self.runtime_dir, self.channel_id)
        if (
            self.descriptor is not None
            and self._shm is not None
            and descriptor.generation == self.descriptor.generation
        ):
            return
        self._detach()
        shm = shared_memory.SharedMemory(name=descriptor.shared_memory_name, create=False)
        if descriptor.owner_pid and descriptor.owner_pid != os.getpid():
            # Attached readers must never become owners. Otherwise Python's
            # resource tracker can unlink the camera worker's segment when an
            # inference process exits.
            try:
                resource_tracker.unregister(shm._name, "shared_memory")
            except (AttributeError, KeyError):
                pass
        magic, version, slots, capacity, _sequence = _GLOBAL_HEADER.unpack_from(shm.buf, 0)
        if magic != _MAGIC or version != _MANIFEST_VERSION:
            shm.close()
            raise ValueError("Unsupported shared frame ring header.")
        if slots != descriptor.slot_count or capacity != descriptor.slot_capacity:
            shm.close()
            raise ValueError("Shared frame ring manifest does not match its memory header.")
        self.descriptor = descriptor
        self._shm = shm
        self._last_sequence = 0

    def _read_once(self, *, ordered: bool = False) -> SharedFrame | None:
        if self._shm is None or self.descriptor is None:
            return None
        latest_sequence = struct.unpack_from("<Q", self._shm.buf, _GLOBAL_HEADER.size - 8)[0]
        if latest_sequence <= self._last_sequence:
            return None
        oldest_sequence = max(1, latest_sequence - self.descriptor.slot_count + 1)
        overwritten = max(oldest_sequence - self._last_sequence - 1, 0)
        if ordered:
            # Writers may provide non-contiguous camera sequence numbers.
            # Find committed slots, otherwise an absent number could stall us.
            available = []
            for index in range(self.descriptor.slot_count):
                offset = _GLOBAL_HEADER_SIZE + index * (
                    _SLOT_HEADER_SIZE + self.descriptor.slot_capacity)
                header = _SLOT_HEADER.unpack_from(self._shm.buf, offset)
                if self._last_sequence < header[0] <= latest_sequence and header[0] == header[-1]:
                    available.append(header[0])
            if not available:
                return None
            latest_sequence = min(available)
            overwritten = min(overwritten, latest_sequence - self._last_sequence - 1)
        slot_index = latest_sequence % self.descriptor.slot_count
        slot_offset = _GLOBAL_HEADER_SIZE + slot_index * (
            _SLOT_HEADER_SIZE + self.descriptor.slot_capacity
        )
        values = _SLOT_HEADER.unpack_from(self._shm.buf, slot_offset)
        (
            begin_sequence,
            capture_monotonic_ns,
            capture_wall_time_ns,
            width,
            height,
            channels,
            _stride,
            payload_size,
            end_sequence,
        ) = values
        if begin_sequence != latest_sequence or end_sequence != latest_sequence:
            return None
        if payload_size <= 0 or payload_size > self.descriptor.slot_capacity:
            return None
        payload_offset = slot_offset + _SLOT_HEADER_SIZE
        copied = bytes(self._shm.buf[payload_offset : payload_offset + payload_size])
        confirm_sequence = struct.unpack_from("<Q", self._shm.buf, slot_offset)[0]
        if confirm_sequence != latest_sequence:
            return None
        dtype = np.dtype(self.descriptor.dtype)
        expected_values = int(width) * int(height) * int(channels)
        frame = np.frombuffer(copied, dtype=dtype, count=expected_values)
        shape = (
            (int(height), int(width)) if channels == 1 else (int(height), int(width), int(channels))
        )
        frame = frame.reshape(shape)
        self.overwritten_frames += overwritten
        self.skipped_frames += max(latest_sequence - self._last_sequence - 1 - overwritten, 0)
        self._last_sequence = int(latest_sequence)
        return SharedFrame(
            sequence=int(latest_sequence),
            capture_monotonic_ns=int(capture_monotonic_ns),
            capture_wall_time_ns=int(capture_wall_time_ns),
            frame=frame,
            descriptor=self.descriptor,
        )

    def _detach(self) -> None:
        shm = self._shm
        self._shm = None
        self.descriptor = None
        self._last_sequence = 0
        if shm is not None:
            shm.close()


def load_frame_ring_descriptor(
    runtime_dir: str | Path,
    channel_id: str,
) -> FrameRingDescriptor:
    path = frame_manifest_path(runtime_dir, channel_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Shared frame ring manifest must be an object.")
    descriptor = FrameRingDescriptor.from_json(payload)
    if descriptor.schema_version != _MANIFEST_VERSION or descriptor.channel_id != channel_id:
        raise ValueError("Shared frame ring manifest is incompatible.")
    return descriptor


def _validated_frame(frame: np.ndarray) -> np.ndarray:
    contiguous = np.ascontiguousarray(frame)
    if contiguous.dtype != np.uint8:
        raise ValueError("Shared frame transport currently requires uint8 frames.")
    _frame_shape(contiguous)
    return contiguous


def _frame_shape(frame: np.ndarray) -> tuple[int, int, int]:
    if frame.ndim == 2:
        height, width = frame.shape
        return int(height), int(width), 1
    if frame.ndim == 3 and frame.shape[2] in {1, 3, 4}:
        height, width, channels = frame.shape
        return int(height), int(width), int(channels)
    raise ValueError("Shared frame transport requires HxW, HxWx1, HxWx3, or HxWx4 frames.")


def _safe_shm_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]", "", value)[:24]
    return safe or "channel"


def _json_safe_dict(value: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


# Public names describe the transport semantics used by camera-media and its
# consumers while preserving the explicit implementation names above.
LatestFrameRingWriter = SharedFrameRingWriter
LatestFrameRingReader = SharedFrameRingReader
