from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from cv_runtime.media_validation import validate_basler_link_budget
from cv_utils import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_db.models import PipelineVersion, RuntimeChannel, RuntimeSource

StreamDesiredState = Literal["stopped", "running"]
StreamActualState = Literal["stopped", "starting", "ready", "recovering", "failed"]


@dataclass(frozen=True)
class CameraMediaBinding:
    channel_id: str
    channel_slug: str
    source_type: str
    source_config: dict[str, Any]
    source_secret_refs: dict[str, str]
    stream_required: bool
    inference_required: bool
    image_view_stream: ImageViewStreamBinding | None = None
    stream_restart_token: str | None = None

    @property
    def capture_required(self) -> bool:
        return self.stream_required or self.inference_required


@dataclass(frozen=True)
class ImageViewStreamBinding:
    """Versioned preprocessing needed for the continuous pipeline image stream."""

    pipeline_version_id: str
    image_view_node_key: str
    undistort_node_key: str | None = None
    intrinsic_calibration: dict[str, Any] | None = None
    undistort_interpolation: str = "linear"
    view_profile: dict[str, Any] | None = None
    canonical_preprocessing: tuple[dict[str, Any], ...] = ()

    @property
    def has_intrinsic_undistortion(self) -> bool:
        return bool(self.undistort_node_key and self.intrinsic_calibration)


def request_channel_stream_start(session: Session, channel_id: str) -> RuntimeChannel:
    row = _get_channel(session, channel_id)
    if row.execution_target != "server":
        raise ValueError("Server stream control is only available for server-placed channels.")
    if not row.source_id:
        raise ValueError("The channel has no configured source.")
    source = session.get(RuntimeSource, row.source_id)
    if source is None:
        raise ValueError("The channel source does not exist.")
    if source.source_type == "basler_pylon":
        # Older channels intentionally omit width/height to use the camera's native
        # ROI. Validate a fully configured profile immediately, otherwise let the
        # camera worker read and validate the effective GenICam profile before
        # acquisition starts.
        validate_basler_link_budget(dict(source.config or {}), required=False)
    row.stream_desired_state = "running"
    row.stream_actual_state = "starting"
    row.stream_last_error = None
    row.stream_heartbeat_at = None
    session.flush()
    return row


def request_channel_stream_stop(session: Session, channel_id: str) -> RuntimeChannel:
    row = _get_channel(session, channel_id)
    row.stream_desired_state = "stopped"
    row.stream_actual_state = "stopped"
    row.stream_worker_instance_id = None
    row.stream_heartbeat_at = None
    row.stream_last_error = None
    row.stream_profile = {}
    row.stream_metrics = {}
    session.flush()
    return row


def request_channel_stream_restart(session: Session, channel_id: str) -> RuntimeChannel:
    row = request_channel_stream_start(session, channel_id)
    row.stream_worker_instance_id = None
    row.stream_heartbeat_at = None
    row.stream_metrics = {
        **dict(row.stream_metrics or {}),
        "restart_requested_at": utc_now().isoformat(),
    }
    session.flush()
    return row


def list_camera_media_bindings(session: Session) -> list[CameraMediaBinding]:
    rows = session.execute(
        select(RuntimeChannel, RuntimeSource)
        .join(RuntimeSource, RuntimeSource.id == RuntimeChannel.source_id)
        .where(
            RuntimeChannel.deleted_at.is_(None),
            RuntimeChannel.execution_target == "server",
        )
    ).all()
    bindings: list[CameraMediaBinding] = []
    for channel, source in rows:
        stream_required = channel.stream_desired_state == "running"
        inference_required = channel.desired_state in {"start_requested", "running"}
        if not (stream_required or inference_required):
            continue
        bindings.append(
            CameraMediaBinding(
                channel_id=channel.id,
                channel_slug=channel.slug,
                source_type=source.source_type,
                source_config=dict(source.config or {}),
                source_secret_refs=dict(source.secret_refs or {}),
                stream_required=stream_required,
                inference_required=inference_required,
                image_view_stream=_image_view_stream_binding(session, channel),
                stream_restart_token=str(
                    (channel.stream_metrics or {}).get("restart_requested_at") or ""
                )
                or None,
            )
        )
    return bindings


def _image_view_stream_binding(
    session: Session,
    channel: RuntimeChannel,
) -> ImageViewStreamBinding | None:
    """Resolve the canonical model input from the assigned pipeline graph.

    Ambiguous graphs fail closed for this optional stream only. The camera and raw
    publication remain usable and the normal pipeline validator continues to own
    user-facing graph diagnostics.
    """

    if not channel.pipeline_version_id:
        return None
    version = session.get(PipelineVersion, channel.pipeline_version_id)
    if version is None or not isinstance(version.graph, dict):
        return None
    graph = dict(version.graph)
    raw_nodes = graph.get("nodes")
    raw_edges = graph.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        return None
    nodes = {
        str(item.get("node_key")): item
        for item in raw_nodes
        if isinstance(item, dict) and item.get("node_key")
    }
    incoming_frame: dict[str, list[str]] = {}
    for item in raw_edges:
        if not isinstance(item, dict):
            continue
        source = str(item.get("from_node_key") or "")
        target = str(item.get("to_node_key") or "")
        if source in nodes and target in nodes:
            to_port = str(item.get("to_port") or "frame")
            from_port = str(item.get("from_port") or "frame")
            if to_port == "frame" and from_port == "frame":
                incoming_frame.setdefault(target, []).append(source)

    preferred_inference_keys = {
        key for key, item in nodes.items() if item.get("node_type") == "inference.segmentation"
    }
    inference_keys = {
        key
        for key, item in nodes.items()
        if str(item.get("node_type") or "").startswith("inference.")
    }
    paths = _model_preprocessing_paths(
        nodes,
        incoming_frame,
        preferred_inference_keys or inference_keys,
    )
    if not paths and preferred_inference_keys:
        paths = _model_preprocessing_paths(nodes, incoming_frame, inference_keys)
    if not paths:
        # A canonical process-coordinate view can still be useful for capture-only
        # pipelines without an inference node.
        rectify_keys = [
            key
            for key, item in nodes.items()
            if item.get("node_type") == "preprocessing.process_coordinate_rectify"
        ]
        paths = {
            path
            for key in rectify_keys
            if (path := _frame_preprocessing_path(key, nodes, incoming_frame)) is not None
        }
    if len(paths) != 1:
        return None
    path = next(iter(paths))
    if not path:
        # The model receives the raw frame. The frontend can use the raw stream
        # directly and a duplicate encoder would waste one NVENC session.
        return None

    undistort_keys = [
        key
        for key in path
        if nodes[key].get("node_type") == "preprocessing.camera_undistort"
    ]
    if len(undistort_keys) > 1:
        return None
    undistort_key = undistort_keys[0] if undistort_keys else None
    if undistort_key is not None and path[0] != undistort_key:
        # Intrinsic calibration is resolution-bound and must operate on the raw
        # source frame before crop/resize transforms.
        return None
    intrinsic_calibration: dict[str, Any] | None = None
    undistort_interpolation = "linear"
    if undistort_key is not None:
        undistort_config = nodes[undistort_key].get("config")
        if not isinstance(undistort_config, dict):
            return None
        calibration = undistort_config.get("calibration")
        if not isinstance(calibration, dict):
            return None
        intrinsic_calibration = dict(calibration)
        undistort_interpolation = str(undistort_config.get("interpolation") or "linear")

    canonical_preprocessing: list[dict[str, Any]] = []
    view_profile: dict[str, Any] | None = None
    for key in path:
        node = nodes[key]
        node_type = str(node.get("node_type") or "")
        if node_type in {"preprocessing.camera_undistort", "preprocessing.frame_sampler"}:
            continue
        config = node.get("config")
        if not isinstance(config, dict):
            return None
        if node_type == "preprocessing.process_coordinate_rectify":
            raw_view_profile = config.get("view_profile")
            if not isinstance(raw_view_profile, dict):
                return None
            view_profile = dict(raw_view_profile)
        canonical_preprocessing.append(
            {
                "node_key": key,
                "node_type": node_type,
                "config": dict(config),
            }
        )
    if not canonical_preprocessing:
        return None

    return ImageViewStreamBinding(
        pipeline_version_id=version.id,
        image_view_node_key=path[-1],
        undistort_node_key=undistort_key,
        intrinsic_calibration=intrinsic_calibration,
        undistort_interpolation=undistort_interpolation,
        view_profile=view_profile,
        canonical_preprocessing=tuple(canonical_preprocessing),
    )


_CANONICAL_FRAME_TRANSFORMS = {
    "preprocessing.camera_undistort",
    "preprocessing.crop",
    "preprocessing.process_coordinate_rectify",
    "preprocessing.resize",
}
_CANONICAL_FRAME_PASSTHROUGH = {"preprocessing.frame_sampler"}


def _model_preprocessing_paths(
    nodes: dict[str, dict[str, Any]],
    incoming_frame: dict[str, list[str]],
    inference_keys: set[str],
) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for inference_key in inference_keys:
        feeders = incoming_frame.get(inference_key, [])
        if len(feeders) != 1:
            continue
        path = _frame_preprocessing_path(feeders[0], nodes, incoming_frame)
        if path is not None:
            paths.add(path)
    return paths


def _frame_preprocessing_path(
    start: str,
    nodes: dict[str, dict[str, Any]],
    incoming_frame: dict[str, list[str]],
) -> tuple[str, ...] | None:
    current = start
    reverse_path: list[str] = []
    visited: set[str] = set()
    while current and current not in visited:
        visited.add(current)
        node = nodes.get(current)
        if node is None:
            return None
        node_type = str(node.get("node_type") or "")
        if node_type == "source.channel_frame":
            return tuple(reversed(reverse_path))
        if node_type not in _CANONICAL_FRAME_TRANSFORMS | _CANONICAL_FRAME_PASSTHROUGH:
            return None
        reverse_path.append(current)
        parents = incoming_frame.get(current, [])
        if len(parents) != 1:
            return None
        current = parents[0]
    return None


def update_channel_stream_observation(
    session: Session,
    channel_id: str,
    *,
    actual_state: StreamActualState,
    worker_instance_id: str | None,
    error: str | None = None,
    profile: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    heartbeat_at: datetime | None = None,
) -> None:
    row = session.get(RuntimeChannel, channel_id)
    if row is None or row.deleted_at is not None:
        return
    if row.stream_desired_state == "stopped" and actual_state != "stopped":
        return
    row.stream_actual_state = actual_state
    row.stream_worker_instance_id = worker_instance_id
    row.stream_heartbeat_at = heartbeat_at or utc_now()
    row.stream_last_error = error
    if profile is not None:
        row.stream_profile = dict(profile)
    if metrics is not None:
        row.stream_metrics = dict(metrics)
    session.flush()


def mark_channel_stream_stopped(
    session: Session,
    channel_id: str,
    *,
    worker_instance_id: str | None = None,
) -> None:
    row = session.get(RuntimeChannel, channel_id)
    if row is None:
        return
    if (
        worker_instance_id
        and row.stream_worker_instance_id
        and row.stream_worker_instance_id != worker_instance_id
    ):
        return
    row.stream_actual_state = "stopped"
    row.stream_worker_instance_id = None
    row.stream_heartbeat_at = utc_now()
    row.stream_profile = {}
    row.stream_metrics = {}
    session.flush()


def _get_channel(session: Session, channel_id: str) -> RuntimeChannel:
    row = session.get(RuntimeChannel, channel_id)
    if row is None or row.deleted_at is not None:
        raise ValueError("Runtime channel does not exist.")
    return row
