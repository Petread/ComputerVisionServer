from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from cv_runtime.payloads import FramePayload, RuntimeExecutionResult

TEMPORAL_SAMPLE_RECORDER_VERSION = "temporal-sample-recorder.v1"
TEMPORAL_SAMPLE_MANIFEST_SCHEMA = "temporal.sample.manifest.v1"
TEMPORAL_FEATURE_SCHEMA = "temporal.frame-features.v1"
TEMPORAL_ROTATION_TRACE_SCHEMA = "temporal.rotation-trace.v1"
STEM_CURVATURE_DIAGNOSTIC_MANIFEST_SCHEMA = "stem.curvature-diagnostic.manifest.v1"
_READY_SCHEMA = "temporal.sample.spool-ready.v1"
_SPOOL_SCHEMA = "temporal.sample.spool.v1"


class TemporalSampleRecorderConfig(BaseModel):
    """Bounded, node-configurable policy for the generic temporal recorder."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    profile: Literal["temporal_training", "curvature_diagnostics"] = "temporal_training"
    fps: float = Field(default=15.0, ge=1.0, le=120.0)
    rectified_video: bool = True
    diagnostic_video: bool = True
    verification_frame_count: int = Field(default=0, ge=0, le=600)
    maximum_frames: int = Field(default=900, ge=2, le=100_000)
    minimum_geometry_quality: float = Field(default=0.5, ge=0.0, le=1.0)
    minimum_visible_fraction: float = Field(default=0.3, ge=0.0, le=1.0)
    minimum_rotation_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    deduplication: Literal["sequence_id", "keep"] = "sequence_id"
    retention_days: int = Field(default=30, ge=1, le=3650)
    split: Literal["train", "val", "test"] = "train"
    annotation_schema_id: str = "stem_orientation_angle.v1"
    dataset_id: str = ""
    auto_import_enabled: bool = True
    import_sample_every_n: int = Field(default=1, ge=1, le=100_000)
    import_maximum_per_hour: int = Field(default=120, ge=0, le=100_000)

    @classmethod
    def from_node_config(
        cls,
        value: Mapping[str, Any],
        *,
        default_retention_days: int = 30,
    ) -> TemporalSampleRecorderConfig:
        return cls(
            enabled=bool(value.get("record_temporal_samples", True)),
            fps=float(value.get("temporal_sample_fps", 15.0)),
            diagnostic_video=bool(value.get("record_diagnostic_video", True)),
            verification_frame_count=int(value.get("verification_frame_count", 0)),
            maximum_frames=int(value.get("temporal_sample_maximum_frames", 900)),
            minimum_geometry_quality=float(
                value.get(
                    "minimum_recording_geometry_quality",
                    value.get("minimum_geometry_quality", 0.5),
                )
            ),
            minimum_visible_fraction=float(
                value.get(
                    "minimum_recording_visible_fraction",
                    value.get("minimum_visible_fraction", 0.3),
                )
            ),
            minimum_rotation_confidence=float(
                value.get("minimum_recording_rotation_confidence", 0.0)
            ),
            deduplication=str(value.get("temporal_sample_deduplication", "sequence_id")),
            retention_days=int(value.get("temporal_sample_retention_days", default_retention_days)),
            split=str(value.get("temporal_sample_split", "train")),
            annotation_schema_id=str(
                value.get("temporal_annotation_schema_id", "stem_orientation_angle.v1")
            ),
            dataset_id=str(value.get("temporal_dataset_id", "") or "").strip(),
            auto_import_enabled=bool(value.get("temporal_auto_import_enabled", True)),
            import_sample_every_n=int(value.get("temporal_import_sample_every_n", 1)),
            import_maximum_per_hour=int(
                value.get("temporal_import_maximum_per_hour", 120)
            ),
        )


@dataclass(frozen=True)
class TemporalSampleEvent:
    sequence_id: str
    session_id: str
    track_id: str
    channel_id: str
    pipeline_version_id: str
    runtime_execution_id: str | None
    source_frame_sequence: int
    source_timestamp: str | None
    frame_bytes: bytes
    frame_mime_type: str
    frame_width: int | None
    frame_height: int | None
    geometry: dict[str, Any]
    rotation: dict[str, Any]
    tracking: dict[str, Any]
    orientation: dict[str, Any]
    diagnostics: dict[str, Any]
    overlays: tuple[dict[str, Any], ...]
    config: TemporalSampleRecorderConfig

    @property
    def quality_gate_passed(self) -> bool:
        if self.config.profile == "curvature_diagnostics":
            return (
                float(self.geometry.get("geometry_confidence") or 0.0)
                >= self.config.minimum_geometry_quality
                and float(self.geometry.get("visible_length_ratio") or 0.0)
                >= self.config.minimum_visible_fraction
                and float(self.rotation.get("confidence") or 0.0)
                >= self.config.minimum_rotation_confidence
            )
        return (
            float(self.geometry.get("quality_score") or 0.0) >= self.config.minimum_geometry_quality
            and float(self.geometry.get("visible_fraction") or 0.0)
            >= self.config.minimum_visible_fraction
            and float(self.rotation.get("rotation_confidence") or 0.0)
            >= self.config.minimum_rotation_confidence
        )

    @property
    def decision_ready(self) -> bool:
        return bool(self.orientation.get("decision_ready"))

    @property
    def decision_frame_sequence(self) -> int | None:
        decision = self.orientation.get("decision_frame")
        if not isinstance(decision, Mapping):
            return None
        value = decision.get("frame_sequence")
        return int(value) if isinstance(value, int | float) else None


@dataclass(frozen=True)
class TemporalSampleMaterialization:
    manifest_path: Path
    manifest_sha256: str
    sequence_id: str
    sample_status: str
    angle_label_eligible: bool
    asset_references: dict[str, Any]


def extract_temporal_sample_event(
    result: RuntimeExecutionResult,
    frame: FramePayload,
    *,
    config: TemporalSampleRecorderConfig,
) -> TemporalSampleEvent | None:
    """Extract one aligned observation without placing image data in result JSON."""

    if config.profile == "curvature_diagnostics":
        return _extract_curvature_diagnostic_event(result, frame, config=config)

    orientation = _orientation_result(result)
    if orientation is None:
        return None
    session_id = _nonempty_string(orientation.get("session_id"))
    track_id = _nonempty_string(orientation.get("track_id"))
    if session_id is None or track_id is None:
        return None
    geometry = _node_payload(result, "stem_geometry", track_id=track_id)
    rotation = _node_payload(result, "stem_rotation", track_id=track_id)
    if geometry is None or rotation is None:
        return None
    geometry_sequence = geometry.get("frame_sequence")
    rotation_sequence = rotation.get("frame_sequence")
    if not isinstance(geometry_sequence, int) or rotation_sequence != geometry_sequence:
        return None
    if orientation.get("current_observation", {}).get("frame_sequence") not in {
        None,
        geometry_sequence,
    }:
        return None
    channel_id = _nonempty_string(orientation.get("channel_id") or result.channel_id)
    if channel_id is None:
        return None
    pipeline_version_id = _nonempty_string(
        orientation.get("pipeline_version_id") or result.pipeline_version_id
    )
    if pipeline_version_id is None:
        return None
    return TemporalSampleEvent(
        sequence_id=temporal_sample_sequence_id(
            channel_id=channel_id,
            pipeline_version_id=pipeline_version_id,
            session_id=session_id,
            track_id=track_id,
        ),
        session_id=session_id,
        track_id=track_id,
        channel_id=channel_id,
        pipeline_version_id=pipeline_version_id,
        runtime_execution_id=result.runtime_execution_id,
        source_frame_sequence=geometry_sequence,
        source_timestamp=_nonempty_string(
            geometry.get("timestamp") or rotation.get("timestamp") or frame.timestamp
        ),
        frame_bytes=frame.image_bytes(),
        frame_mime_type=frame.mime_type,
        frame_width=frame.width,
        frame_height=frame.height,
        geometry=geometry,
        rotation=rotation,
        tracking=_tracking_payload(result, track_id=track_id),
        orientation=orientation,
        diagnostics={},
        overlays=tuple(dict(item) for item in result.overlay_primitives if isinstance(item, dict)),
        config=config,
    )


def _extract_curvature_diagnostic_event(
    result: RuntimeExecutionResult,
    source_frame: FramePayload,
    *,
    config: TemporalSampleRecorderConfig,
) -> TemporalSampleEvent | None:
    solution = _node_payload(result, "stem_curvature_phase_solution", track_id=None)
    if solution is None:
        return None
    session_id = _nonempty_string(solution.get("session_id"))
    track_id = _nonempty_string(solution.get("track_id"))
    if session_id is None or track_id is None:
        return None
    geometry = _node_payload(result, "stem_horizontal_curvature", track_id=track_id)
    rotation = _node_payload(result, "stem_surface_phase", track_id=track_id)
    if geometry is None or rotation is None:
        return None
    geometry_sequence = geometry.get("frame_sequence")
    rotation_sequence = rotation.get("frame_sequence")
    if not isinstance(geometry_sequence, int) or rotation_sequence != geometry_sequence:
        return None
    rectified_frame = result.recording_frame()
    if rectified_frame is None:
        # The diagnostic contract promises intrinsically undistorted video. A raw
        # source frame is therefore not silently substituted.
        return None
    orientation_result = solution.get("orientation_result")
    orientation = (
        dict(orientation_result)
        if isinstance(orientation_result, Mapping)
        else {
            "schema_version": "stem.orientation.v3",
            "session_id": session_id,
            "track_id": track_id,
            "session_state": solution.get("session_state"),
            "decision_ready": False,
            "decision_frame": {},
            "reason_codes": list(solution.get("reason_codes") or []),
        }
    )
    channel_id = _nonempty_string(result.channel_id)
    pipeline_version_id = _nonempty_string(result.pipeline_version_id)
    if channel_id is None or pipeline_version_id is None:
        return None
    return TemporalSampleEvent(
        sequence_id=temporal_sample_sequence_id(
            channel_id=channel_id,
            pipeline_version_id=pipeline_version_id,
            session_id=session_id,
            track_id=track_id,
        ),
        session_id=session_id,
        track_id=track_id,
        channel_id=channel_id,
        pipeline_version_id=pipeline_version_id,
        runtime_execution_id=result.runtime_execution_id,
        source_frame_sequence=geometry_sequence,
        source_timestamp=_nonempty_string(
            geometry.get("timestamp") or rotation.get("timestamp") or source_frame.timestamp
        ),
        frame_bytes=rectified_frame.image_bytes(),
        frame_mime_type=rectified_frame.mime_type,
        frame_width=rectified_frame.width,
        frame_height=rectified_frame.height,
        geometry=geometry,
        rotation=rotation,
        tracking=_tracking_payload(result, track_id=track_id),
        orientation=orientation,
        diagnostics=solution,
        overlays=tuple(dict(item) for item in result.overlay_primitives if isinstance(item, dict)),
        config=config,
    )


def temporal_sample_sequence_id(
    *,
    channel_id: str,
    pipeline_version_id: str,
    session_id: str,
    track_id: str,
) -> str:
    digest = hashlib.sha256(
        "|".join((channel_id, pipeline_version_id, session_id, track_id)).encode("utf-8")
    ).hexdigest()[:24]
    return f"tsq_{digest}"


def temporal_sample_spool_path(
    capture_root: Path,
    *,
    runtime_execution_id: str | None,
    sequence_id: str,
) -> Path:
    runtime_key = _safe_component(runtime_execution_id or "no-runtime")
    return capture_root / ".temporal-spool" / runtime_key / _safe_component(sequence_id)


class TemporalSampleSpoolWriter:
    """Durable frame spool used by the realtime executor's bounded writer thread."""

    def append(self, spool_path: Path, event: TemporalSampleEvent) -> bool:
        spool_path.mkdir(parents=True, exist_ok=True)
        frames_root = spool_path / "frames"
        records_root = spool_path / "records"
        frames_root.mkdir(exist_ok=True)
        records_root.mkdir(exist_ok=True)
        _write_json_once(spool_path / "spool.json", _spool_metadata(event))
        key = f"{event.source_frame_sequence:012d}"
        record_path = records_root / f"{key}.json"
        if record_path.exists():
            return False
        extension = _image_extension(event.frame_mime_type)
        frame_name = f"{key}{extension}"
        _atomic_write_bytes(frames_root / frame_name, event.frame_bytes)
        _atomic_write_json(
            record_path,
            {
                "source_frame_sequence": event.source_frame_sequence,
                "source_timestamp": event.source_timestamp,
                "frame_file": f"frames/{frame_name}",
                "frame_mime_type": event.frame_mime_type,
                "frame_width": event.frame_width,
                "frame_height": event.frame_height,
                "frame_sha256": _sha256_bytes(event.frame_bytes),
                "geometry": event.geometry,
                "rotation": event.rotation,
                "tracking": event.tracking,
                "orientation": event.orientation,
                "diagnostics": event.diagnostics,
                "overlays": list(event.overlays),
            },
        )
        _prune_spool_frames(spool_path, maximum_frames=event.config.maximum_frames)
        return True

    def finalize(
        self,
        spool_path: Path,
        *,
        decision_frame_sequence: int,
        complete: bool,
        reason_code: str,
    ) -> None:
        spool_path.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            spool_path / "READY.json",
            {
                "schema_version": _READY_SCHEMA,
                "decision_frame_sequence": int(decision_frame_sequence),
                "complete": bool(complete),
                "reason_code": str(reason_code),
            },
        )


def _prune_spool_frames(spool_path: Path, *, maximum_frames: int) -> None:
    records_root = spool_path / "records"
    records = sorted(records_root.glob("*.json"))
    excess = len(records) - max(int(maximum_frames), 2)
    for record_path in records[: max(excess, 0)]:
        record = _read_json(record_path)
        frame_file = record.get("frame_file")
        if isinstance(frame_file, str):
            frame_path = (spool_path / frame_file).resolve()
            try:
                frame_path.relative_to(spool_path.resolve())
            except ValueError:
                frame_path = None
            if frame_path is not None:
                frame_path.unlink(missing_ok=True)
        record_path.unlink(missing_ok=True)


def materialize_temporal_sample_spool(
    spool_path: Path,
    *,
    capture_root: Path,
    request_id: str | None = None,
) -> TemporalSampleMaterialization:
    """Build deterministic videos/traces from a ready spool in the capture service."""

    capture_root = capture_root.resolve()
    spool_path = spool_path.resolve()
    expected_spool_root = (capture_root / ".temporal-spool").resolve()
    try:
        spool_path.relative_to(expected_spool_root)
    except ValueError as exc:
        raise ValueError("Temporal sample spool path is outside runtime capture storage.") from exc
    ready_path = spool_path / "READY.json"
    if not ready_path.is_file():
        raise TemporalSampleSpoolNotReadyError("Temporal sample spool is not finalized yet.")
    metadata = _read_json(spool_path / "spool.json")
    ready = _read_json(ready_path)
    if metadata.get("schema_version") != _SPOOL_SCHEMA:
        raise ValueError("Unsupported temporal sample spool schema.")
    if ready.get("schema_version") != _READY_SCHEMA:
        raise ValueError("Unsupported temporal sample ready marker schema.")
    config = TemporalSampleRecorderConfig.model_validate(metadata.get("config") or {})
    records = [_read_json(path) for path in sorted((spool_path / "records").glob("*.json"))]
    records.sort(key=lambda item: int(item["source_frame_sequence"]))
    if not records:
        raise ValueError("Temporal sample spool does not contain any aligned frame records.")
    decision_sequence = int(ready["decision_frame_sequence"])
    primary = [item for item in records if int(item["source_frame_sequence"]) <= decision_sequence]
    verification = [
        item for item in records if int(item["source_frame_sequence"]) > decision_sequence
    ][: config.verification_frame_count]
    decision_orientation = _decision_orientation(records, decision_sequence)
    decision_recorded = (
        bool(primary) and int(primary[-1]["source_frame_sequence"]) == decision_sequence
    )
    complete = bool(ready.get("complete")) and decision_recorded
    if len(primary) > config.maximum_frames:
        primary = primary[-config.maximum_frames :]
        decision_recorded = int(primary[-1]["source_frame_sequence"]) == decision_sequence
        complete = complete and decision_recorded
    sequence_id = str(metadata["sequence_id"])
    output_name = sequence_id
    if config.deduplication == "keep" and request_id:
        output_name = f"{sequence_id}_{_safe_component(request_id)}"
    output_root = (
        capture_root
        / "temporal-samples"
        / _safe_component(str(metadata["channel_id"]))
        / output_name
    )
    existing_manifest = output_root / "sequence-manifest.json"
    if config.deduplication == "sequence_id" and existing_manifest.is_file():
        existing = _read_json(existing_manifest)
        return _materialization_from_manifest(existing_manifest, existing)

    temp_root = output_root.parent / f".{output_root.name}.tmp"
    if temp_root.exists():
        shutil.rmtree(temp_root)
    temp_root.mkdir(parents=True, exist_ok=False)
    try:
        frame_map = _write_primary_artifacts(
            spool_path,
            temp_root,
            primary,
            config=config,
        )
        verification_ref = None
        if verification:
            verification_path = temp_root / "verification.mp4"
            _write_video(spool_path, verification_path, verification, fps=config.fps)
            verification_ref = _artifact_reference(verification_path, temp_root)
        decision_frame_index = len(primary) - 1 if decision_recorded else None
        angle_label_eligible = (
            complete
            and config.profile == "temporal_training"
            and _valid_rotation_trace(primary)
        )
        sample_status = "recorded" if complete else "excluded"
        exclusion_reason = (
            None if complete else str(ready.get("reason_code") or "decision_frame_not_recorded")
        )
        if config.profile == "curvature_diagnostics":
            manifest = _build_curvature_diagnostic_manifest(
                metadata=metadata,
                ready=ready,
                records=primary,
                output_root=temp_root,
                frame_map=frame_map,
                decision_frame_index=decision_frame_index,
                sample_status=sample_status,
                exclusion_reason=exclusion_reason,
                decision_orientation=decision_orientation,
            )
        else:
            manifest = _build_manifest(
                metadata=metadata,
                ready=ready,
                output_root=temp_root,
                frame_map=frame_map,
                decision_frame_index=decision_frame_index,
                sample_status=sample_status,
                exclusion_reason=exclusion_reason,
                angle_label_eligible=angle_label_eligible,
                verification_ref=verification_ref,
                decision_orientation=decision_orientation,
            )
        _atomic_write_json(temp_root / "sequence-manifest.json", manifest)
        output_root.parent.mkdir(parents=True, exist_ok=True)
        if output_root.exists():
            shutil.rmtree(output_root)
        # Directory replacement is denied on some Windows/WSL2-backed volumes
        # even though file-level atomic replacements in the spool work. The
        # capture worker owns this target, so a complete-tree copy followed by
        # removal is the portable finalization boundary.
        shutil.copytree(temp_root, output_root)
        shutil.rmtree(temp_root)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    final_manifest = output_root / "sequence-manifest.json"
    result = _materialization_from_manifest(final_manifest, _read_json(final_manifest))
    shutil.rmtree(spool_path, ignore_errors=True)
    return result


def load_temporal_sample_materialization(
    manifest_path: Path,
) -> TemporalSampleMaterialization:
    """Load an already-materialized sample for retry after an external import failure."""

    resolved = manifest_path.resolve()
    if not resolved.is_file():
        raise ValueError("Temporal sample manifest does not exist.")
    manifest = _read_json(resolved)
    if manifest.get("schema_version") not in {
        TEMPORAL_SAMPLE_MANIFEST_SCHEMA,
        STEM_CURVATURE_DIAGNOSTIC_MANIFEST_SCHEMA,
    }:
        raise ValueError("Unsupported temporal sample manifest schema.")
    return _materialization_from_manifest(resolved, manifest)


class TemporalSampleSpoolNotReadyError(RuntimeError):
    pass


def temporal_sample_asset_references(
    *,
    capture_root: Path,
    channel_id: str,
    sequence_id: str,
    queued: bool,
    diagnostic_video: bool = True,
    verification_video: bool = False,
    profile: Literal["temporal_training", "curvature_diagnostics"] = "temporal_training",
    rectified_video: bool = True,
) -> dict[str, Any]:
    root = capture_root / "temporal-samples" / _safe_component(channel_id) / sequence_id
    if profile == "curvature_diagnostics":
        return {
            "sequence_id": sequence_id,
            "sequence_manifest_ref": str(root / "sequence-manifest.json"),
            "rectified_video_ref": str(root / "rectified.mp4") if rectified_video else None,
            "overlay_video_ref": str(root / "overlay.mp4") if diagnostic_video else None,
            "observations_ref": str(root / "observations.jsonl"),
            "phase_trace_ref": str(root / "phase_trace.json"),
            "fit_ref": str(root / "fit.json"),
            "result_ref": str(root / "final-result.json"),
            "calibrations_ref": str(root / "calibrations.json"),
            "status": "queued" if queued else "recorded",
            "reason_codes": [
                "stem_diagnostic_capture_queued" if queued else "stem_diagnostic_recorded"
            ],
        }
    return {
        "sequence_id": sequence_id,
        "sequence_manifest_ref": str(root / "sequence-manifest.json"),
        "raw_video_ref": str(root / "sequence.mp4"),
        "diagnostic_video_ref": (str(root / "diagnostic.mp4") if diagnostic_video else None),
        "verification_video_ref": (str(root / "verification.mp4") if verification_video else None),
        "feature_trace_ref": str(root / "features.jsonl"),
        "rotation_trace_ref": str(root / "rotation-trace.jsonl"),
        "status": "queued" if queued else "recorded",
        "reason_codes": [
            "temporal_sample_capture_queued" if queued else "temporal_sample_recorded"
        ],
    }


def _write_primary_artifacts(
    spool_path: Path,
    output_root: Path,
    records: Sequence[dict[str, Any]],
    *,
    config: TemporalSampleRecorderConfig,
) -> list[dict[str, Any]]:
    if not records:
        raise ValueError("A temporal sample requires at least one primary frame.")
    fps = config.fps
    if config.profile == "curvature_diagnostics":
        if config.rectified_video:
            _write_video(spool_path, output_root / "rectified.mp4", records, fps=fps)
        if config.diagnostic_video:
            _write_video(
                spool_path,
                output_root / "overlay.mp4",
                records,
                fps=fps,
                diagnostic=True,
            )
    else:
        _write_video(spool_path, output_root / "sequence.mp4", records, fps=fps)
    if config.profile == "temporal_training" and config.diagnostic_video:
        _write_video(
            spool_path,
            output_root / "diagnostic.mp4",
            records,
            fps=fps,
            diagnostic=True,
        )
    frame_map: list[dict[str, Any]] = []
    feature_lines: list[dict[str, Any]] = []
    rotation_lines: list[dict[str, Any]] = []
    for frame_index, record in enumerate(records):
        video_timestamp = round(frame_index / fps, 9)
        mapping = {
            "frame_index": frame_index,
            "video_timestamp_seconds": video_timestamp,
            "source_frame_sequence": int(record["source_frame_sequence"]),
            "source_timestamp": record.get("source_timestamp"),
            "frame_sha256": record["frame_sha256"],
        }
        frame_map.append(mapping)
        feature_lines.append(
            {
                "schema_version": TEMPORAL_FEATURE_SCHEMA,
                **mapping,
                "geometry": record.get("geometry") or {},
                "tracking": record.get("tracking") or {},
                "orientation": _compact_orientation(record.get("orientation") or {}),
            }
        )
        rotation = dict(record.get("rotation") or {})
        rotation_lines.append(
            {
                "schema_version": TEMPORAL_ROTATION_TRACE_SCHEMA,
                **mapping,
                "valid": bool(rotation.get("valid")),
                "delta_rotation_deg": rotation.get("delta_rotation_deg"),
                "cumulative_rotation_deg_unwrapped": rotation.get(
                    "cumulative_rotation_deg_unwrapped"
                ),
                "rotation_direction": rotation.get("rotation_direction"),
                "rotation_confidence": rotation.get("rotation_confidence"),
                "detected_feature_count": rotation.get("detected_feature_count"),
                "matched_feature_count": rotation.get("matched_feature_count"),
                "inlier_feature_count": rotation.get("inlier_feature_count"),
                "inlier_ratio": rotation.get("inlier_ratio"),
                "translation_component": rotation.get("translation_component") or {},
                "trace_discontinuity": bool(rotation.get("trace_discontinuity")),
                "trace_generation": rotation.get("trace_generation"),
                "calibration_valid": bool(rotation.get("calibration_valid")),
                "calibration_id": rotation.get("calibration_id"),
                "calibration_version": rotation.get("calibration_version"),
                "calibration_method": rotation.get("calibration_method"),
                "calibration_hash": rotation.get("calibration_hash"),
                "reason_codes": list(rotation.get("reason_codes") or []),
            }
        )
    _write_jsonl(output_root / "features.jsonl", feature_lines)
    _write_jsonl(output_root / "rotation-trace.jsonl", rotation_lines)
    if config.profile == "curvature_diagnostics":
        _write_curvature_diagnostic_json(output_root, records)
    return frame_map


def _write_curvature_diagnostic_json(
    output_root: Path,
    records: Sequence[Mapping[str, Any]],
) -> None:
    final_diagnostics = dict(records[-1].get("diagnostics") or {})
    graph = dict(final_diagnostics.get("diagnostic_graph") or {})
    observations = [
        dict(item)
        for item in graph.get("points") or []
        if isinstance(item, Mapping)
    ]
    _write_jsonl(output_root / "observations.jsonl", observations)
    phase_samples: list[dict[str, Any]] = []
    for record in records:
        phase = dict(record.get("rotation") or {})
        phase_samples.append(
            {
                "frame_sequence": record.get("source_frame_sequence"),
                "timestamp": record.get("source_timestamp"),
                "delta_phase_deg": phase.get("delta_phase_deg"),
                "cumulative_phase_deg": phase.get("cumulative_phase_deg"),
                "raw_delta_phase": phase.get("delta_raw_phase", phase.get("raw_delta_phase")),
                "cumulative_raw_phase": phase.get("cumulative_raw_phase"),
                "direction": phase.get("direction"),
                "tracking_mode": phase.get("tracking_mode"),
                "relock_status": phase.get("relock_status"),
                "feature_count": phase.get("feature_count"),
                "inlier_ratio": phase.get("inlier_ratio"),
                "confidence": phase.get("confidence"),
                "calibration_id": phase.get("calibration_id"),
                "reason_codes": list(phase.get("reason_codes") or []),
            }
        )
    _atomic_write_json(
        output_root / "phase_trace.json",
        {
            "schema_version": "stem.surface-phase-trace.v1",
            "samples": phase_samples,
        },
    )
    _atomic_write_json(
        output_root / "fit.json",
        {
            "schema_version": "stem.curvature-phase-fit-artifact.v1",
            "state": final_diagnostics.get("state"),
            "session_state": final_diagnostics.get("session_state"),
            "signed_fit": final_diagnostics.get("signed_fit"),
            "energy_fit": final_diagnostics.get("energy_fit"),
            "diagnostic_graph": graph,
            "confidence": final_diagnostics.get("confidence"),
            "reason_codes": list(final_diagnostics.get("reason_codes") or []),
        },
    )
    orientation = records[-1].get("orientation")
    _atomic_write_json(
        output_root / "final-result.json",
        dict(orientation) if isinstance(orientation, Mapping) else {"status": "unavailable"},
    )
    _atomic_write_json(
        output_root / "calibrations.json",
        {
            "schema_version": "stem.curvature-diagnostic-calibrations.v1",
            "process_calibration_id": final_diagnostics.get("process_calibration_id"),
            "process_calibration_hash": final_diagnostics.get("process_calibration_hash"),
            "phase_calibration_id": final_diagnostics.get("phase_calibration_id"),
            "phase_calibration_hash": final_diagnostics.get("phase_calibration_hash"),
        },
    )


def _build_curvature_diagnostic_manifest(
    *,
    metadata: dict[str, Any],
    ready: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
    output_root: Path,
    frame_map: list[dict[str, Any]],
    decision_frame_index: int | None,
    sample_status: str,
    exclusion_reason: str | None,
    decision_orientation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    config = TemporalSampleRecorderConfig.model_validate(metadata["config"])

    def reference(name: str) -> dict[str, Any] | None:
        path = output_root / name
        return _artifact_reference(path, output_root) if path.is_file() else None

    artifacts = {
        "rectified_video": reference("rectified.mp4"),
        "overlay_video": reference("overlay.mp4"),
        "observations": reference("observations.jsonl"),
        "phase_trace": reference("phase_trace.json"),
        "fit": reference("fit.json"),
        "final_result": reference("final-result.json"),
        "calibrations": reference("calibrations.json"),
    }
    return {
        "schema_version": STEM_CURVATURE_DIAGNOSTIC_MANIFEST_SCHEMA,
        "recorder_version": TEMPORAL_SAMPLE_RECORDER_VERSION,
        "profile": config.profile,
        "sequence_id": metadata["sequence_id"],
        "group_id": metadata["track_id"],
        "session_id": metadata["session_id"],
        "track_id": metadata["track_id"],
        "channel_id": metadata["channel_id"],
        "pipeline_version_id": metadata["pipeline_version_id"],
        "runtime_execution_id": metadata.get("runtime_execution_id"),
        "sample_status": sample_status,
        "exclusion_reason": exclusion_reason,
        "decision_frame_index": decision_frame_index,
        "decision_source_frame_sequence": int(ready["decision_frame_sequence"]),
        "constant_fps": config.fps,
        "frame_index_mapping": frame_map,
        "angle_label_eligible": False,
        "ground_truth_status": "technical_review_only",
        "eligible_training_heads": [],
        "artifacts": artifacts,
        "calibration_ids": {
            "process": (decision_orientation or {}).get("calibration_id"),
            "rotation": (decision_orientation or {}).get("rotation_calibration_id"),
        },
        "reason_codes": list((decision_orientation or {}).get("reason_codes") or []),
        "retention_days": config.retention_days,
        "record_count": len(records),
    }


def _build_manifest(
    *,
    metadata: dict[str, Any],
    ready: dict[str, Any],
    output_root: Path,
    frame_map: list[dict[str, Any]],
    decision_frame_index: int | None,
    sample_status: str,
    exclusion_reason: str | None,
    angle_label_eligible: bool,
    verification_ref: dict[str, Any] | None,
    decision_orientation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    config = TemporalSampleRecorderConfig.model_validate(metadata["config"])
    orientation = dict(metadata.get("orientation") or {})
    versions = dict(orientation.get("versions") or {})
    direction = dict(orientation.get("direction_provenance") or {})
    sequence_ref = _artifact_reference(output_root / "sequence.mp4", output_root)
    feature_ref = _artifact_reference(output_root / "features.jsonl", output_root)
    rotation_ref = _artifact_reference(output_root / "rotation-trace.jsonl", output_root)
    diagnostics: list[dict[str, Any]] = []
    diagnostic_path = output_root / "diagnostic.mp4"
    if diagnostic_path.is_file():
        diagnostics.append(_artifact_reference(diagnostic_path, output_root))
    if verification_ref is not None:
        diagnostics.append({**verification_ref, "role": "verification"})
    decision_timestamp = (
        frame_map[decision_frame_index]["video_timestamp_seconds"]
        if decision_frame_index is not None
        else frame_map[-1]["video_timestamp_seconds"]
    )
    canonical_item = {
        "sequence_id": metadata["sequence_id"],
        "group_id": metadata["track_id"],
        "primary_video": sequence_ref,
        "diagnostic_assets": [
            {key: value for key, value in item.items() if key != "role"} for item in diagnostics
        ],
        "frames": [
            {
                "frame_index": item["frame_index"],
                "timestamp": item["video_timestamp_seconds"],
            }
            for item in frame_map
        ],
        "decision_frame_index": decision_frame_index or 0,
        "decision_timestamp": decision_timestamp,
        "feature_manifest": feature_ref,
        "rotation_trace": rotation_ref,
        "schemas": {
            "feature_schema": TEMPORAL_FEATURE_SCHEMA,
            "rotation_schema": str(
                versions.get("rotation_schema_version") or TEMPORAL_ROTATION_TRACE_SCHEMA
            ),
            "calibration_schema": str(
                versions.get("rotation_calibration_version") or "unversioned"
            ),
            "annotation_schema": config.annotation_schema_id,
        },
        "human_labels": {"sequence_labels": [], "timeline_labels": []},
        "derived_targets": [],
        "label_source": "automatic",
        "label_quality": {
            "status": "unreviewed",
            "score": None,
            "reason_codes": ["ground_truth_pending_label_studio"],
        },
        "exclusion_reason": exclusion_reason,
        "split": config.split,
        "provenance": {
            "source_system": "inference-server-runtime",
            "recording_id": metadata["sequence_id"],
            "created_at": str(
                orientation.get("observation_started_at")
                or frame_map[0].get("source_timestamp")
                or "timestamp_unavailable"
            ),
            "software_version": TEMPORAL_SAMPLE_RECORDER_VERSION,
            "source_channel_id": metadata["channel_id"],
            "pipeline_version_id": metadata["pipeline_version_id"],
            "runtime_session_id": metadata["session_id"],
            "track_id": metadata["track_id"],
            "component_versions": {
                key: str(value)
                for key, value in versions.items()
                if value is not None and isinstance(value, str | int | float | bool)
            },
            "attributes": {
                "decision_source_frame_sequence": int(ready["decision_frame_sequence"]),
                "angle_label_eligible": angle_label_eligible,
                "sample_status": sample_status,
            },
        },
    }
    baseline_prediction = _manifest_baseline_prediction(
        decision_orientation,
        frame_map=frame_map,
        decision_frame_index=decision_frame_index,
        angle_label_eligible=angle_label_eligible,
    )
    return {
        "schema_version": TEMPORAL_SAMPLE_MANIFEST_SCHEMA,
        "recorder_version": TEMPORAL_SAMPLE_RECORDER_VERSION,
        "sequence_id": metadata["sequence_id"],
        "group_id": metadata["track_id"],
        "session_id": metadata["session_id"],
        "track_id": metadata["track_id"],
        "channel_id": metadata["channel_id"],
        "pipeline_version_id": metadata["pipeline_version_id"],
        "runtime_execution_id": metadata.get("runtime_execution_id"),
        "sample_status": sample_status,
        "exclusion_reason": exclusion_reason,
        "decision_frame_index": decision_frame_index,
        "decision_source_frame_sequence": int(ready["decision_frame_sequence"]),
        "decision_source_timestamp": (
            frame_map[decision_frame_index].get("source_timestamp")
            if decision_frame_index is not None
            else None
        ),
        "constant_fps": config.fps,
        "frame_index_mapping": frame_map,
        "angle_label_eligible": angle_label_eligible,
        "ground_truth_status": "pending_label_studio",
        "eligible_training_heads": [
            "bow_relevance",
            *(["orientation_angle"] if angle_label_eligible else []),
        ],
        "direction_provenance": direction,
        "versions": versions,
        "artifacts": {
            "sequence": sequence_ref,
            "features": feature_ref,
            "rotation_trace": rotation_ref,
            "diagnostics": diagnostics,
        },
        "verification_separate_from_primary": verification_ref is not None,
        "canonical_sequence_item": canonical_item,
        "baseline_prediction": baseline_prediction,
    }


def _decision_orientation(
    records: Sequence[Mapping[str, Any]],
    decision_sequence: int,
) -> dict[str, Any] | None:
    for record in reversed(records):
        orientation = record.get("orientation")
        if not isinstance(orientation, Mapping) or not bool(orientation.get("decision_ready")):
            continue
        decision = orientation.get("decision_frame")
        if (
            isinstance(decision, Mapping)
            and decision.get("frame_sequence") == decision_sequence
        ):
            return dict(orientation)
    return None


def _manifest_baseline_prediction(
    orientation: Mapping[str, Any] | None,
    *,
    frame_map: Sequence[Mapping[str, Any]],
    decision_frame_index: int | None,
    angle_label_eligible: bool,
) -> dict[str, Any]:
    if (
        orientation is None
        or decision_frame_index is None
        or not angle_label_eligible
    ):
        return {"available": False, "is_ground_truth": False}
    target = orientation.get("target_frame_or_interval_candidate")
    if not isinstance(target, Mapping):
        return {"available": False, "is_ground_truth": False}
    source_to_index = {
        item.get("source_frame_sequence"): item.get("frame_index") for item in frame_map
    }
    start = source_to_index.get(target.get("frame_start"))
    end = source_to_index.get(target.get("frame_end"))
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end < start
        or end > decision_frame_index
    ):
        return {"available": False, "is_ground_truth": False}
    confidence = orientation.get("confidence")
    return {
        "available": True,
        "is_ground_truth": False,
        "frame_start_index": start,
        "frame_end_index": end,
        "confidence": (
            max(0.0, min(float(confidence), 1.0))
            if isinstance(confidence, int | float)
            else 0.0
        ),
        "estimate_method": str(
            orientation.get("estimate_method") or "projected_baseline"
        ),
        "source": "deterministic_runtime_prediction",
    }


def _write_video(
    spool_path: Path,
    path: Path,
    records: Sequence[dict[str, Any]],
    *,
    fps: float,
    diagnostic: bool = False,
) -> None:
    first = _decode_spooled_frame(spool_path, records[0])
    height, width = first.shape[:2]
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        float(fps),
        (width, height),
    )
    if not writer.isOpened():
        raise ValueError("OpenCV could not initialize the MP4V temporal sample writer.")
    try:
        for record in records:
            image = _decode_spooled_frame(spool_path, record)
            if image.shape[:2] != (height, width):
                raise ValueError("Temporal sample frame resolution changed within one sequence.")
            writer.write(_draw_diagnostic(image, record) if diagnostic else image)
    finally:
        writer.release()


def _decode_spooled_frame(spool_path: Path, record: Mapping[str, Any]) -> np.ndarray:
    frame_path = (spool_path / str(record["frame_file"])).resolve()
    try:
        frame_path.relative_to(spool_path.resolve())
    except ValueError as exc:
        raise ValueError("Temporal sample frame path escapes its spool.") from exc
    image = cv2.imdecode(np.frombuffer(frame_path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Temporal sample frame cannot be decoded: {frame_path.name}")
    return image


def _draw_diagnostic(image: np.ndarray, record: Mapping[str, Any]) -> np.ndarray:
    output = image.copy()
    height, width = output.shape[:2]
    for entry in record.get("overlays") or []:
        primitive = entry.get("primitive") if isinstance(entry, Mapping) else None
        value = primitive if isinstance(primitive, Mapping) else entry
        if not isinstance(value, Mapping):
            continue
        geometry = value.get("geometry")
        if not isinstance(geometry, Mapping):
            continue
        primitive_type = value.get("primitive_type")
        if primitive_type in {"line", "axis", "arrow", "measurement"}:
            start = _overlay_point(geometry.get("start"), width, height)
            end = _overlay_point(geometry.get("end"), width, height)
            color = (
                (255, 64, 192)
                if primitive_type == "measurement"
                else (0, 255, 255)
            )
            if start and end:
                cv2.line(output, start, end, color, 2, cv2.LINE_AA)
            else:
                points = _overlay_polygon(geometry.get("points"), width, height)
                if points is not None:
                    cv2.polylines(
                        output,
                        [points],
                        False,
                        color,
                        2,
                        cv2.LINE_AA,
                    )
        elif primitive_type == "point":
            point = _overlay_point(geometry, width, height)
            if point:
                cv2.circle(output, point, 4, (0, 255, 0), -1, cv2.LINE_AA)
        elif primitive_type == "zone":
            box = _overlay_box(geometry, width, height)
            if box:
                cv2.rectangle(output, box[0], box[1], (255, 180, 0), 2, cv2.LINE_AA)
            else:
                points = _overlay_polygon(geometry.get("points"), width, height)
                if points is not None:
                    cv2.polylines(output, [points], True, (255, 180, 0), 2, cv2.LINE_AA)
        elif primitive_type == "polygon":
            points = _overlay_polygon(geometry.get("points"), width, height)
            if points is not None:
                cv2.polylines(output, [points], True, (255, 180, 0), 2, cv2.LINE_AA)
    geometry = record.get("geometry") or {}
    rotation = record.get("rotation") or {}
    diagnostics = record.get("diagnostics") or {}
    orientation = record.get("orientation") or {}
    if geometry.get("payload_type") == "stem_horizontal_curvature":
        lines = (
            f"frame={record.get('source_frame_sequence')} stem={diagnostics.get('track_id', 'n/a')}",
            f"curvature={_fmt(geometry.get('curvature_rms_mm'))} mm bow={_fmt(geometry.get('signed_bow_mm'))} mm",
            f"phase={_fmt(rotation.get('cumulative_phase_deg'))} deg span={_fmt(diagnostics.get('observed_phase_span_deg'))} deg",
            f"target={_fmt(diagnostics.get('target_phase_deg'))} deg current={_fmt(diagnostics.get('current_phase_deg'))} deg",
            f"correction={_fmt(orientation.get('recommended_rotation_deg_signed'))} deg confidence={_fmt(orientation.get('overall_confidence') or diagnostics.get('confidence'))}",
            f"relock={rotation.get('relock_status', 'n/a')} reasons={','.join(list(diagnostics.get('reason_codes') or [])[:2]) or 'OK'}",
        )
    else:
        lines = (
            f"frame={record.get('source_frame_sequence')}",
            f"bow={_fmt(geometry.get('signed_bow_normalized'))}",
            f"rotation={_fmt(rotation.get('cumulative_rotation_deg_unwrapped'))} deg",
            f"confidence={_fmt(rotation.get('rotation_confidence'))}",
        )
    for index, line in enumerate(lines):
        cv2.putText(
            output,
            line,
            (12, 24 + 22 * index),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
    return output


def _valid_rotation_trace(records: Sequence[Mapping[str, Any]]) -> bool:
    if not records:
        return False
    generations: set[int] = set()
    for record in records:
        rotation = record.get("rotation")
        if not isinstance(rotation, Mapping):
            return False
        if not bool(rotation.get("valid")) or not bool(rotation.get("calibration_valid")):
            return False
        if bool(rotation.get("trace_discontinuity")):
            return False
        generation = rotation.get("trace_generation")
        if isinstance(generation, int):
            generations.add(generation)
    final_rotation = records[-1].get("rotation") or {}
    return (
        len(generations) <= 1
        and final_rotation.get("cumulative_rotation_deg_unwrapped") is not None
    )


def _orientation_result(result: RuntimeExecutionResult) -> dict[str, Any] | None:
    for ports in result.node_outputs.values():
        for payload in ports.values():
            if not isinstance(payload, Mapping) or payload.get("payload_type") != "result":
                continue
            value = payload.get("result")
            if isinstance(value, Mapping) and value.get("schema_version") == "stem.orientation.v2":
                return dict(value)
    for entry in result.persisted_results:
        payload = entry.get("payload") if isinstance(entry, Mapping) else None
        value = payload.get("result") if isinstance(payload, Mapping) else None
        if isinstance(value, Mapping) and value.get("schema_version") == "stem.orientation.v2":
            return dict(value)
    return None


def _node_payload(
    result: RuntimeExecutionResult,
    payload_type: str,
    *,
    track_id: str | None,
) -> dict[str, Any] | None:
    for ports in result.node_outputs.values():
        for payload in ports.values():
            if (
                isinstance(payload, Mapping)
                and payload.get("payload_type") == payload_type
                and (track_id is None or payload.get("track_id") == track_id)
            ):
                return dict(payload)
    return None


def _tracking_payload(result: RuntimeExecutionResult, *, track_id: str) -> dict[str, Any]:
    for ports in result.node_outputs.values():
        for payload in ports.values():
            if not isinstance(payload, Mapping) or payload.get("payload_type") != "segment":
                continue
            metadata = payload.get("metadata")
            tracking = (
                metadata.get("active_stem_tracking") if isinstance(metadata, Mapping) else None
            )
            if isinstance(tracking, Mapping) and tracking.get("track_id") == track_id:
                return dict(tracking)
            for obj in payload.get("objects") or []:
                if isinstance(obj, Mapping) and obj.get("track_id") == track_id:
                    return {
                        "track_id": track_id,
                        "source_object_id": obj.get("object_id"),
                        "label": obj.get("label"),
                        "score": obj.get("score"),
                        "metadata": obj.get("metadata") or {},
                    }
    return {"track_id": track_id}


def _spool_metadata(event: TemporalSampleEvent) -> dict[str, Any]:
    return {
        "schema_version": _SPOOL_SCHEMA,
        "recorder_version": TEMPORAL_SAMPLE_RECORDER_VERSION,
        "sequence_id": event.sequence_id,
        "session_id": event.session_id,
        "track_id": event.track_id,
        "channel_id": event.channel_id,
        "pipeline_version_id": event.pipeline_version_id,
        "runtime_execution_id": event.runtime_execution_id,
        "config": event.config.model_dump(mode="json"),
        "orientation": {
            key: event.orientation.get(key)
            for key in (
                "schema_version",
                "observation_started_at",
                "direction_provenance",
                "versions",
            )
        },
    }


def _compact_orientation(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "session_state",
            "decision_ready",
            "confidence",
            "estimate_method",
            "reason_codes",
            "current_observation",
            "decision_frame",
        )
    }


def _materialization_from_manifest(
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> TemporalSampleMaterialization:
    artifacts = manifest.get("artifacts") or {}
    base = manifest_path.parent
    if manifest.get("schema_version") == STEM_CURVATURE_DIAGNOSTIC_MANIFEST_SCHEMA:
        refs = {
            "sequence_id": manifest.get("sequence_id"),
            "sequence_manifest_ref": str(manifest_path),
            "rectified_video_ref": _absolute_ref(base, artifacts.get("rectified_video")),
            "overlay_video_ref": _absolute_ref(base, artifacts.get("overlay_video")),
            "observations_ref": _absolute_ref(base, artifacts.get("observations")),
            "phase_trace_ref": _absolute_ref(base, artifacts.get("phase_trace")),
            "fit_ref": _absolute_ref(base, artifacts.get("fit")),
            "result_ref": _absolute_ref(base, artifacts.get("final_result")),
            "calibrations_ref": _absolute_ref(base, artifacts.get("calibrations")),
            "status": manifest.get("sample_status"),
            "reason_codes": (
                ["stem_diagnostic_recorded"]
                if manifest.get("sample_status") == "recorded"
                else [str(manifest.get("exclusion_reason") or "stem_diagnostic_excluded")]
            ),
        }
        return TemporalSampleMaterialization(
            manifest_path=manifest_path,
            manifest_sha256=_sha256_bytes(manifest_path.read_bytes()),
            sequence_id=str(manifest["sequence_id"]),
            sample_status=str(manifest["sample_status"]),
            angle_label_eligible=False,
            asset_references=refs,
        )
    diagnostics = artifacts.get("diagnostics") or []
    diagnostic = next(
        (item for item in diagnostics if item.get("role") != "verification"),
        None,
    )
    verification = next(
        (item for item in diagnostics if item.get("role") == "verification"),
        None,
    )
    refs = {
        "sequence_id": manifest.get("sequence_id"),
        "sequence_manifest_ref": str(manifest_path),
        "raw_video_ref": _absolute_ref(base, artifacts.get("sequence")),
        "diagnostic_video_ref": _absolute_ref(base, diagnostic),
        "verification_video_ref": _absolute_ref(base, verification),
        "feature_trace_ref": _absolute_ref(base, artifacts.get("features")),
        "rotation_trace_ref": _absolute_ref(base, artifacts.get("rotation_trace")),
        "status": manifest.get("sample_status"),
        "reason_codes": (
            ["temporal_sample_recorded"]
            if manifest.get("sample_status") == "recorded"
            else [str(manifest.get("exclusion_reason") or "temporal_sample_excluded")]
        ),
    }
    return TemporalSampleMaterialization(
        manifest_path=manifest_path,
        manifest_sha256=_sha256_bytes(manifest_path.read_bytes()),
        sequence_id=str(manifest["sequence_id"]),
        sample_status=str(manifest["sample_status"]),
        angle_label_eligible=bool(manifest.get("angle_label_eligible")),
        asset_references=refs,
    )


def _artifact_reference(path: Path, root: Path) -> dict[str, Any]:
    media_types = {
        ".mp4": "video/mp4",
        ".jsonl": "application/x-ndjson",
        ".json": "application/json",
    }
    return {
        "uri": path.relative_to(root).as_posix(),
        "sha256": _sha256_bytes(path.read_bytes()),
        "media_type": media_types.get(path.suffix.lower(), "application/octet-stream"),
    }


def _absolute_ref(root: Path, value: object) -> str | None:
    if not isinstance(value, Mapping) or not value.get("uri"):
        return None
    return str(root / str(value["uri"]))


def _write_jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    data = "".join(f"{_canonical_json(value)}\n" for value in values).encode("utf-8")
    _atomic_write_bytes(path, data)


def _write_json_once(path: Path, value: Mapping[str, Any]) -> None:
    if not path.exists():
        _atomic_write_json(path, value)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, (_canonical_json(value) + "\n").encode("utf-8"))


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_bytes(value)
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _image_extension(mime_type: str) -> str:
    return {
        "image/png": ".png",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(mime_type.lower(), ".jpg")


def _safe_component(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in "-_." else "_" for character in value
    )
    return safe[:160] or "unnamed"


def _nonempty_string(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _overlay_point(value: object, width: int, height: int) -> tuple[int, int] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        x = float(value["x"])
        y = float(value["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
        x *= width
        y *= height
    return int(round(x)), int(round(y))


def _overlay_box(
    value: Mapping[str, Any], width: int, height: int
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    source = value.get("box") if isinstance(value.get("box"), Mapping) else value
    try:
        if {"x", "y", "width", "height"}.issubset(source):
            x1 = float(source["x"])
            y1 = float(source["y"])
            x2 = x1 + float(source["width"])
            y2 = y1 + float(source["height"])
        else:
            x1 = float(source["x_min"])
            y1 = float(source["y_min"])
            x2 = float(source["x_max"])
            y2 = float(source["y_max"])
    except (KeyError, TypeError, ValueError):
        return None
    if all(0.0 <= item <= 1.0 for item in (x1, y1, x2, y2)):
        x1, x2 = x1 * width, x2 * width
        y1, y2 = y1 * height, y2 * height
    return (int(round(x1)), int(round(y1))), (int(round(x2)), int(round(y2)))


def _overlay_polygon(value: object, width: int, height: int) -> np.ndarray | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return None
    points = [_overlay_point(item, width, height) for item in value]
    if len(points) < 2 or any(point is None for point in points):
        return None
    return np.asarray(points, dtype=np.int32).reshape((-1, 1, 2))


def _fmt(value: object) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "n/a"
