from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from cv_contracts.runtime import RuntimeLiveViewState
from cv_core import PlatformSettings


def write_runtime_overlay_event(
    settings: PlatformSettings,
    state: RuntimeLiveViewState,
) -> Path:
    frame_updates = {}
    for field_name in ("frame", "source_frame", "undistorted_frame"):
        frame = getattr(state, field_name)
        if frame is not None:
            frame_updates[field_name] = frame.model_copy(update={"image_base64": None})
    # Overlay events are a lightweight metadata channel. Full pixel payloads are
    # hydrated independently through the live-view REST endpoint.
    metadata_state = state.model_copy(update=frame_updates)
    path = runtime_overlay_event_path(settings, state.channel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(metadata_state.model_dump(mode="json"), separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def read_runtime_overlay_event(
    settings: PlatformSettings,
    channel_id: str,
) -> RuntimeLiveViewState | None:
    path = runtime_overlay_event_path(settings, channel_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return RuntimeLiveViewState.model_validate(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def clear_runtime_overlay_event(
    settings: PlatformSettings,
    channel_id: str,
    *,
    runtime_instance_id: str | None = None,
) -> None:
    path = runtime_overlay_event_path(settings, channel_id)
    if runtime_instance_id:
        current = read_runtime_overlay_event(settings, channel_id)
        if current is not None and current.runtime_instance_id != runtime_instance_id:
            return
    path.unlink(missing_ok=True)


def runtime_overlay_event_path(settings: PlatformSettings, channel_id: str) -> Path:
    safe_channel_id = re.sub(r"[^A-Za-z0-9_.-]", "_", channel_id)
    return settings.storage.runtime_root() / "overlay-events" / f"{safe_channel_id}.json"
