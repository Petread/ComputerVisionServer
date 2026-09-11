from __future__ import annotations

import base64
import io
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cv_contracts import ResponseMeta
from cv_contracts.runtime import (
    RuntimeLiveFrame,
    RuntimeLiveMetrics,
    RuntimeLiveSourceHealth,
    RuntimeLiveViewResponse,
    RuntimeLiveViewState,
)
from cv_core import PlatformSettings
from cv_status import HealthState, RuntimeObservedState
from cv_utils import get_correlation_id, get_request_id, utc_now_iso
from sqlalchemy.orm import Session

from cv_db.models import RuntimeChannel
from cv_db.runtime_execution_store import get_active_runtime_execution


_SPLIT_FRAME_GENERATIONS_TO_KEEP = 8


@dataclass(frozen=True)
class RuntimeLiveFrameSnapshot:
    image_bytes: bytes
    mime_type: str
    sequence: int
    width: int | None
    height: int | None
    captured_at: str | None
    source_timestamp: str | None
    updated_at: str | None


def write_runtime_live_view_state(
    settings: PlatformSettings,
    state: RuntimeLiveViewState,
    *,
    split_frame_payload: bool = False,
) -> Path:
    if not state.runtime_instance_id:
        raise ValueError("runtime_instance_id is required for live view state writes.")
    if split_frame_payload:
        state = _write_split_frame_payload(settings, state)
    path = runtime_live_view_state_path(settings, state.runtime_instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(state.model_dump(mode="json")),
        encoding="utf-8",
    )
    tmp_path.replace(path)
    if split_frame_payload:
        _prune_split_frame_payloads(settings, state)
    return path


def trigger_live_view_runtime_id(channel_id: str) -> str:
    return f"trigger-latest-{channel_id}"


def edge_live_view_runtime_id(channel_id: str) -> str:
    return f"edge-latest-{channel_id}"


def get_runtime_live_view_response(
    session: Session,
    settings: PlatformSettings,
    *,
    channel_id: str,
    max_frame_width: int | None = None,
    include_frame: bool = True,
) -> RuntimeLiveViewResponse:
    state = get_runtime_live_view_state(
        session,
        settings,
        channel_id=channel_id,
        include_frame=include_frame or bool(max_frame_width),
    )
    if max_frame_width:
        state = _with_preview_frame(state, max_frame_width=max_frame_width)
    if not include_frame:
        state = _without_frame_payload(state)
    return RuntimeLiveViewResponse(
        state=state,
        meta=ResponseMeta(
            request_id=get_request_id(),
            correlation_id=get_correlation_id(),
            generated_at=utc_now_iso(),
        ),
    )


def get_runtime_live_frame_snapshot(
    session: Session,
    settings: PlatformSettings,
    *,
    channel_id: str,
) -> RuntimeLiveFrameSnapshot | None:
    state = get_runtime_live_view_state(
        session,
        settings,
        channel_id=channel_id,
        include_frame=False,
    )
    if not state.available or state.stale or state.frame is None:
        return None
    image_bytes = (
        _read_versioned_split_frame_payload(
            runtime_live_view_frame_path(settings, state.runtime_instance_id),
            state.frame,
        )
        if state.runtime_instance_id is not None
        else None
    )
    if image_bytes is None and state.frame.image_base64:
        image_bytes = _decode_image_base64(state.frame.image_base64)
    if not image_bytes:
        return None
    return RuntimeLiveFrameSnapshot(
        image_bytes=image_bytes,
        mime_type=state.frame.mime_type or "image/jpeg",
        sequence=state.frame.sequence,
        width=state.frame.width,
        height=state.frame.height,
        captured_at=state.frame.captured_at,
        source_timestamp=state.frame.source_timestamp,
        updated_at=state.updated_at,
    )


def get_runtime_live_view_state(
    session: Session,
    settings: PlatformSettings,
    *,
    channel_id: str,
    include_frame: bool = True,
) -> RuntimeLiveViewState:
    channel = session.get(RuntimeChannel, channel_id)
    if channel is None or channel.deleted_at is not None:
        raise ValueError(f"Runtime channel {channel_id} does not exist.")
    execution = get_active_runtime_execution(session, channel_id=channel_id)
    actual_state = RuntimeObservedState(channel.observed_state)
    if execution is None:
        if channel.execution_target == "edge_device":
            edge_state = _read_edge_live_view_state(
                settings,
                channel_id=channel_id,
                actual_state=actual_state,
            )
            if edge_state is not None:
                return (
                    _with_split_frame_payload(settings, edge_state)
                    if include_frame
                    else edge_state
                )
            return _empty_state(
                channel_id=channel_id,
                runtime_execution_id=edge_live_view_runtime_id(channel_id),
                actual_state=actual_state,
                recovery_hint=(
                    "Open the Edge channel to acquire a viewer lease and wait for the relay."
                ),
            )
        trigger_state = _read_trigger_live_view_state(
            settings,
            channel_id=channel_id,
            actual_state=actual_state,
        )
        if trigger_state is not None:
            return (
                _with_split_frame_payload(settings, trigger_state)
                if include_frame
                else trigger_state
            )
        return _empty_state(
            channel_id=channel_id,
            actual_state=actual_state,
            recovery_hint="Start realtime runtime or invoke a trigger pipeline to publish a frame.",
        )
    actual_state = RuntimeObservedState(
        getattr(execution, "actual_state", None) or channel.observed_state
    )
    if actual_state not in {RuntimeObservedState.RUNNING, RuntimeObservedState.DEGRADED}:
        trigger_state = _read_trigger_live_view_state(
            settings,
            channel_id=channel_id,
            actual_state=actual_state,
        )
        if trigger_state is not None and not trigger_state.stale:
            return (
                _with_split_frame_payload(settings, trigger_state)
                if include_frame
                else trigger_state
            )
        return _empty_state(
            channel_id=channel_id,
            runtime_execution_id=execution.id,
            actual_state=actual_state,
            metrics=_live_metrics_from_summary(execution.metrics_summary or {}),
            source_health=RuntimeLiveSourceHealth.model_validate(
                (execution.health_summary or {}).get("source_health")
                or {
                    "state": execution.health_summary.get("state", HealthState.DEGRADED.value)
                    if execution.health_summary
                    else HealthState.DEGRADED.value,
                    "status_text": execution.health_summary.get(
                        "status_text", "Runtime is not live"
                    )
                    if execution.health_summary
                    else "Runtime is not live",
                }
            ),
            recovery_hint=(
                "Wait for the runtime to reach running state or inspect the channel error."
            ),
        )

    path = runtime_live_view_state_path(settings, execution.id)
    if not path.exists():
        health_summary = execution.health_summary or {}
        metrics_summary = execution.metrics_summary or {}
        source_health_payload = health_summary.get("source_health")
        if not source_health_payload and (health_summary.get("last_error") or health_summary.get("status_text")):
            status_text = str(
                health_summary.get("last_error")
                or health_summary.get("status_text")
                or "Runtime is running but no live frame has arrived yet."
            )
            source_health_payload = {
                "state": health_summary.get("state", HealthState.DEGRADED.value),
                "status_text": status_text,
                "last_error": health_summary.get("last_error"),
                "connected": False,
            }
        source_health = RuntimeLiveSourceHealth.model_validate(
            source_health_payload
            or {
                "state": HealthState.DEGRADED.value,
                "status_text": "Runtime is running but no live frame has arrived yet.",
                "connected": False,
            }
        )
        return _empty_state(
            channel_id=channel_id,
            runtime_execution_id=execution.id,
            actual_state=actual_state,
            metrics=_live_metrics_from_summary(metrics_summary),
            source_health=source_health,
            recovery_hint=_missing_live_state_recovery_hint(
                source_health=source_health,
                metrics_summary=metrics_summary,
            ),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = RuntimeLiveViewState.model_validate(payload)
    except Exception as exc:
        return _empty_state(
            channel_id=channel_id,
            runtime_execution_id=execution.id,
            actual_state=RuntimeObservedState.DEGRADED,
            metrics=_live_metrics_from_summary(execution.metrics_summary or {}),
            source_health=RuntimeLiveSourceHealth(
                state=HealthState.DEGRADED,
                status_text="Live frame state could not be read.",
                last_error=str(exc),
            ),
            recovery_hint="Refresh live view after the next executor heartbeat.",
        )
    if state.runtime_instance_id != execution.id:
        return _empty_state(
            channel_id=channel_id,
            runtime_execution_id=execution.id,
            actual_state=actual_state,
            metrics=_live_metrics_from_summary(execution.metrics_summary or {}),
            source_health=RuntimeLiveSourceHealth(
                state=HealthState.DEGRADED,
                status_text="Live frame belongs to an older runtime instance.",
                connected=False,
            ),
            recovery_hint="Wait for the new runtime instance to publish its first frame.",
        )
    stale_after = max(settings.runtime.executor_stale_after_seconds, 2)
    stale = _is_stale(state.updated_at, stale_after_seconds=stale_after)
    state = state.model_copy(update={"actual_state": actual_state, "stale": stale})
    if stale:
        state = _with_stale_runtime_health(state, execution)
    return _with_split_frame_payload(settings, state) if include_frame else state


def runtime_live_view_state_path(settings: PlatformSettings, runtime_execution_id: str) -> Path:
    return settings.storage.runtime_overlays_root() / f"{runtime_execution_id}.latest.json"


def _missing_live_state_recovery_hint(
    *,
    source_health: RuntimeLiveSourceHealth,
    metrics_summary: dict[str, Any],
) -> str:
    if not source_health.connected:
        return "The executor may still be connecting to the source."
    inference_drops = int(
        metrics_summary.get("inference_drop_count")
        or metrics_summary.get("inference_dropped_frame_count")
        or 0
    )
    deadline_drops = int(metrics_summary.get("pipeline_deadline_drop_count") or 0)
    if inference_drops > 0 or deadline_drops > 0:
        return (
            "The camera source is connected, but pipeline work has not produced a live result. "
            "Check pipeline deadline drops and the slowest analysis or inference node."
        )
    return "The camera source is connected; waiting for the first processed pipeline result."


def runtime_live_view_frame_path(settings: PlatformSettings, runtime_execution_id: str) -> Path:
    return settings.storage.runtime_overlays_root() / f"{runtime_execution_id}.latest.frame"


def runtime_live_view_source_frame_path(
    settings: PlatformSettings,
    runtime_execution_id: str,
) -> Path:
    return settings.storage.runtime_overlays_root() / f"{runtime_execution_id}.latest.source.frame"


def runtime_live_view_undistorted_frame_path(
    settings: PlatformSettings,
    runtime_execution_id: str,
) -> Path:
    return (
        settings.storage.runtime_overlays_root()
        / f"{runtime_execution_id}.latest.undistorted.frame"
    )


def _read_trigger_live_view_state(
    settings: PlatformSettings,
    *,
    channel_id: str,
    actual_state: RuntimeObservedState,
) -> RuntimeLiveViewState | None:
    path = runtime_live_view_state_path(settings, trigger_live_view_runtime_id(channel_id))
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = RuntimeLiveViewState.model_validate(payload)
    except Exception:
        return None
    if state.channel_id != channel_id:
        return None
    stale_after = max(settings.runtime.executor_stale_after_seconds, 2)
    stale = _is_stale(state.updated_at, stale_after_seconds=stale_after)
    return state.model_copy(update={"actual_state": actual_state, "stale": stale})


def _read_edge_live_view_state(
    settings: PlatformSettings,
    *,
    channel_id: str,
    actual_state: RuntimeObservedState,
) -> RuntimeLiveViewState | None:
    path = runtime_live_view_state_path(settings, edge_live_view_runtime_id(channel_id))
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = RuntimeLiveViewState.model_validate(payload)
    except Exception:
        return None
    if state.channel_id != channel_id:
        return None
    stale_after = max(settings.runtime.executor_stale_after_seconds, 5)
    stale = _is_stale(state.updated_at, stale_after_seconds=stale_after)
    return state.model_copy(
        update={
            "actual_state": actual_state,
            "available": state.available and not stale,
            "stale": stale,
        }
    )


def _empty_state(
    *,
    channel_id: str,
    actual_state: RuntimeObservedState,
    runtime_execution_id: str | None = None,
    metrics: RuntimeLiveMetrics | None = None,
    source_health: RuntimeLiveSourceHealth | None = None,
    recovery_hint: str | None = None,
) -> RuntimeLiveViewState:
    return RuntimeLiveViewState(
        channel_id=channel_id,
        runtime_instance_id=runtime_execution_id,
        actual_state=actual_state,
        available=False,
        frame=RuntimeLiveFrame(),
        overlay=[],
        metrics=metrics or RuntimeLiveMetrics(),
        source_health=source_health or RuntimeLiveSourceHealth(),
        recovery_hint=recovery_hint,
        updated_at=utc_now_iso(),
    )


def _is_stale(value: str | None, *, stale_after_seconds: int) -> bool:
    if not value:
        return True
    try:
        normalized = value.replace("Z", "+00:00")
        updated_at = datetime.fromisoformat(normalized)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
    except ValueError:
        return True
    return (datetime.now(tz=UTC) - updated_at).total_seconds() > stale_after_seconds


def _live_metrics_from_summary(payload: dict[str, Any]) -> RuntimeLiveMetrics:
    allowed = set(RuntimeLiveMetrics.model_fields)
    normalized = dict(payload or {})
    if "drop_count" in normalized and "dropped_frame_count" not in normalized:
        normalized["dropped_frame_count"] = normalized["drop_count"]
    return RuntimeLiveMetrics.model_validate(
        {key: value for key, value in normalized.items() if key in allowed}
    )


def _with_stale_runtime_health(
    state: RuntimeLiveViewState,
    execution,
) -> RuntimeLiveViewState:
    health_summary = dict(execution.health_summary or {})
    last_error = health_summary.get("last_error")
    status_text = str(
        last_error
        or health_summary.get("status_text")
        or "Live frame state is stale."
    )
    source_health = RuntimeLiveSourceHealth(
        state=HealthState.DEGRADED,
        status_text=status_text,
        connected=False,
        last_error=str(last_error) if last_error else None,
    )
    diagnostics = state.diagnostics
    warnings = list(diagnostics.warnings)
    stale_warning = "Live frame state is stale; the executor has not published a fresh frame."
    if stale_warning not in warnings:
        warnings.insert(0, stale_warning)
    recovery_hint = (
        health_summary.get("recovery_hint")
        if isinstance(health_summary.get("recovery_hint"), str)
        else None
    ) or "Restart the channel or runtime orchestrator if no fresh live frame appears."
    return state.model_copy(
        update={
            "available": False,
            "source_health": source_health,
            "diagnostics": diagnostics.model_copy(update={"warnings": warnings}),
            "recovery_hint": recovery_hint,
        }
    )


def _without_frame_payload(state: RuntimeLiveViewState) -> RuntimeLiveViewState:
    updates: dict[str, RuntimeLiveFrame] = {}
    for field_name in ("frame", "source_frame", "undistorted_frame"):
        frame = getattr(state, field_name)
        if frame is not None and frame.image_base64:
            updates[field_name] = frame.model_copy(update={"image_base64": None})
    return state.model_copy(update=updates) if updates else state


def _write_split_frame_payload(
    settings: PlatformSettings,
    state: RuntimeLiveViewState,
) -> RuntimeLiveViewState:
    if state.runtime_instance_id is None:
        return state
    updates: dict[str, RuntimeLiveFrame] = {}
    frame_paths = {
        "frame": runtime_live_view_frame_path(settings, state.runtime_instance_id),
        "source_frame": runtime_live_view_source_frame_path(
            settings,
            state.runtime_instance_id,
        ),
        "undistorted_frame": runtime_live_view_undistorted_frame_path(
            settings,
            state.runtime_instance_id,
        ),
    }
    for field_name, path in frame_paths.items():
        frame = getattr(state, field_name)
        if frame is None or not frame.image_base64:
            continue
        image_bytes = _decode_image_base64(frame.image_base64)
        if not image_bytes:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        versioned_path = _versioned_split_frame_path(path, frame.sequence)
        _replace_frame_payload(versioned_path, image_bytes)
        # Keep the legacy latest-frame path during rolling updates. New readers
        # bind pixels to the sequence-specific path referenced by the JSON state.
        _replace_frame_alias(versioned_path, path, image_bytes)
        updates[field_name] = frame.model_copy(
            update={
                "image_base64": None,
                "metadata": {
                    **frame.metadata,
                    "_split_frame_sequence": frame.sequence,
                },
            }
        )
    return state.model_copy(update=updates) if updates else state


def _replace_frame_payload(path: Path, image_bytes: bytes) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(image_bytes)
    tmp_path.replace(path)


def _replace_frame_alias(source_path: Path, path: Path, image_bytes: bytes) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.unlink(missing_ok=True)
        os.link(source_path, tmp_path)
        tmp_path.replace(path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        _replace_frame_payload(path, image_bytes)


def _versioned_split_frame_path(path: Path, sequence: int) -> Path:
    return path.with_name(f"{path.name}.{sequence}")


def _read_versioned_split_frame_payload(
    path: Path,
    frame: RuntimeLiveFrame,
) -> bytes | None:
    versioned_payload = _read_split_frame_payload(
        _versioned_split_frame_path(path, frame.sequence)
    )
    if versioned_payload is not None:
        return versioned_payload
    if frame.metadata.get("_split_frame_sequence") == frame.sequence:
        # This state explicitly references a versioned frame. Returning the
        # mutable legacy path here could attach newer pixels to older overlays.
        return None
    return _read_split_frame_payload(path)


def _read_split_frame_payload(
    path: Path,
) -> bytes | None:
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except Exception:
        return None


def _with_split_frame_payload(
    settings: PlatformSettings,
    state: RuntimeLiveViewState,
) -> RuntimeLiveViewState:
    if state.runtime_instance_id is None:
        return state
    updates: dict[str, RuntimeLiveFrame] = {}
    frame_paths = {
        "frame": runtime_live_view_frame_path(settings, state.runtime_instance_id),
        "source_frame": runtime_live_view_source_frame_path(
            settings,
            state.runtime_instance_id,
        ),
        "undistorted_frame": runtime_live_view_undistorted_frame_path(
            settings,
            state.runtime_instance_id,
        ),
    }
    for field_name, path in frame_paths.items():
        frame = getattr(state, field_name)
        if frame is None or frame.image_base64:
            continue
        image_bytes = _read_versioned_split_frame_payload(path, frame)
        if image_bytes:
            updates[field_name] = frame.model_copy(
                update={"image_base64": base64.b64encode(image_bytes).decode("ascii")}
            )
    return state.model_copy(update=updates) if updates else state


def _prune_split_frame_payloads(
    settings: PlatformSettings,
    state: RuntimeLiveViewState,
) -> None:
    if state.runtime_instance_id is None:
        return
    if (
        state.frame is None
        or state.frame.sequence % _SPLIT_FRAME_GENERATIONS_TO_KEEP != 0
    ):
        return
    frame_paths = (
        runtime_live_view_frame_path(settings, state.runtime_instance_id),
        runtime_live_view_source_frame_path(settings, state.runtime_instance_id),
        runtime_live_view_undistorted_frame_path(settings, state.runtime_instance_id),
    )
    for path in frame_paths:
        generations: list[tuple[int, Path]] = []
        for candidate in path.parent.glob(f"{path.name}.*"):
            try:
                sequence = int(candidate.name.removeprefix(f"{path.name}."))
            except ValueError:
                continue
            generations.append((sequence, candidate))
        generations.sort(key=lambda item: item[0], reverse=True)
        for _, candidate in generations[_SPLIT_FRAME_GENERATIONS_TO_KEEP:]:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                continue


def _decode_image_base64(image_base64: str) -> bytes | None:
    try:
        payload = (
            image_base64.split(",", 1)[1] if image_base64.startswith("data:") else image_base64
        )
        return base64.b64decode(payload.encode("ascii"), validate=True)
    except Exception:
        return None


def _with_preview_frame(
    state: RuntimeLiveViewState,
    *,
    max_frame_width: int,
) -> RuntimeLiveViewState:
    updates: dict[str, RuntimeLiveFrame] = {}
    for field_name in ("frame", "source_frame", "undistorted_frame"):
        frame = getattr(state, field_name)
        preview_frame = _preview_live_frame(frame, max_frame_width=max_frame_width)
        if preview_frame is not frame and preview_frame is not None:
            updates[field_name] = preview_frame
    return state.model_copy(update=updates) if updates else state


def _preview_live_frame(
    frame: RuntimeLiveFrame | None,
    *,
    max_frame_width: int,
) -> RuntimeLiveFrame | None:
    if frame is None or not frame.image_base64 or max_frame_width <= 0:
        return frame
    if frame.width is not None and frame.width <= max_frame_width:
        return frame

    try:
        from PIL import Image

        image_bytes = _decode_image_base64(frame.image_base64)
        if not image_bytes:
            return frame
        with Image.open(io.BytesIO(image_bytes)) as image:
            original_width, original_height = image.size
            if original_width <= max_frame_width:
                return frame
            preview = image.convert("RGB")
            preview_height = max(
                1,
                round(original_height * (max_frame_width / original_width)),
            )
            resampling = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
            preview.thumbnail((max_frame_width, preview_height), resampling)
            buffer = io.BytesIO()
            preview.save(buffer, format="JPEG", quality=65, optimize=True)
    except Exception:
        return frame

    metadata = {
        **dict(frame.metadata or {}),
        "preview": True,
        "preview_width": preview.width,
        "preview_height": preview.height,
        "original_mime_type": frame.mime_type,
    }
    return frame.model_copy(
        update={
            "image_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
            "mime_type": "image/jpeg",
            "width": frame.width or original_width,
            "height": frame.height or original_height,
            "metadata": metadata,
        }
    )
