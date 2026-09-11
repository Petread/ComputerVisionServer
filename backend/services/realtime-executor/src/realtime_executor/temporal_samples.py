from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv_runtime.payloads import FramePayload, RuntimeExecutionResult
from cv_runtime.temporal_samples import (
    TemporalSampleEvent,
    TemporalSampleRecorderConfig,
    TemporalSampleSpoolWriter,
    extract_temporal_sample_event,
    temporal_sample_asset_references,
    temporal_sample_spool_path,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TemporalSampleCapturePlan:
    session_id: str
    sequence_id: str
    capture_intent: dict[str, Any]
    asset_references: dict[str, Any]


@dataclass(frozen=True)
class _AppendCommand:
    spool_path: Path
    event: TemporalSampleEvent


@dataclass(frozen=True)
class _FinalizeCommand:
    spool_path: Path
    decision_frame_sequence: int
    complete: bool
    reason_code: str


class RealtimeTemporalSampleRecorder:
    """Non-blocking spool handoff owned by an existing realtime worker."""

    def __init__(
        self,
        *,
        capture_root: Path,
        config: TemporalSampleRecorderConfig,
        queue_capacity: int = 512,
    ) -> None:
        self.capture_root = capture_root
        self.config = config
        self._queue: queue.Queue[_AppendCommand | _FinalizeCommand | None] = queue.Queue(
            maxsize=max(int(queue_capacity), 8)
        )
        self._writer = TemporalSampleSpoolWriter()
        self._thread = threading.Thread(
            target=self._run,
            name="runtime-temporal-sample-spool",
            daemon=True,
        )
        self._active: TemporalSampleEvent | None = None
        self._decision_sequence: int | None = None
        self._verification_remaining = 0
        self._finalized_session_ids: set[str] = set()
        self._closed = False
        self._thread.start()

    def observe(
        self,
        result: RuntimeExecutionResult,
        frame: FramePayload,
    ) -> list[TemporalSampleCapturePlan]:
        if self._closed or not self.config.enabled:
            return []
        event = extract_temporal_sample_event(result, frame, config=self.config)
        if event is None:
            return []
        plans: list[TemporalSampleCapturePlan] = []
        if self._active is not None and self._active.session_id != event.session_id:
            plan = self._finalize_active(
                decision_frame_sequence=self._active.source_frame_sequence,
                complete=False,
                reason_code="track_changed_before_decision",
            )
            if plan is not None:
                plans.append(plan)
        if event.session_id in self._finalized_session_ids:
            return plans
        if self._active is None:
            if not event.quality_gate_passed:
                return plans
            self._active = event
            self._decision_sequence = None
            self._verification_remaining = 0

        if not self._enqueue(_AppendCommand(self._spool_path(event), event)):
            LOGGER.error(
                "Temporal sample spool queue is full; frame was not recorded: "
                "session_id=%s frame_sequence=%s",
                event.session_id,
                event.source_frame_sequence,
            )
            return plans
        self._active = event

        if event.decision_ready and self._decision_sequence is None:
            decision_sequence = event.decision_frame_sequence
            if decision_sequence is None:
                return plans
            self._decision_sequence = decision_sequence
            self._verification_remaining = self.config.verification_frame_count
            plan = self._plan(event)
            plans.append(plan)
            if self._verification_remaining > 0 and event.source_frame_sequence > decision_sequence:
                self._verification_remaining -= 1
            if self._verification_remaining == 0:
                self._finalize_active(
                    decision_frame_sequence=decision_sequence,
                    complete=True,
                    reason_code="decision_frame_reached",
                    plan_already_created=True,
                )
            return plans

        if (
            self._decision_sequence is not None
            and event.source_frame_sequence > self._decision_sequence
        ):
            if self._verification_remaining > 0:
                self._verification_remaining -= 1
            if self._verification_remaining == 0:
                self._finalize_active(
                    decision_frame_sequence=self._decision_sequence,
                    complete=True,
                    reason_code="decision_frame_reached",
                    plan_already_created=True,
                )
        return plans

    def close(self, *, timeout_seconds: float = 5.0) -> None:
        if self._closed:
            return
        if self._active is not None:
            self._finalize_active(
                decision_frame_sequence=(
                    self._decision_sequence or self._active.source_frame_sequence
                ),
                complete=self._decision_sequence is not None,
                reason_code=(
                    "decision_frame_reached"
                    if self._decision_sequence is not None
                    else "runtime_stopped_before_decision"
                ),
                plan_already_created=self._decision_sequence is not None,
            )
        self._closed = True
        try:
            self._queue.put(None, timeout=min(max(float(timeout_seconds), 0.1), 1.0))
        except queue.Full:
            LOGGER.error("Temporal sample spool queue was full during recorder shutdown.")
        self._thread.join(timeout=max(float(timeout_seconds), 0.0))

    def _finalize_active(
        self,
        *,
        decision_frame_sequence: int,
        complete: bool,
        reason_code: str,
        plan_already_created: bool = False,
    ) -> TemporalSampleCapturePlan | None:
        event = self._active
        if event is None:
            return None
        command = _FinalizeCommand(
            spool_path=self._spool_path(event),
            decision_frame_sequence=decision_frame_sequence,
            complete=complete,
            reason_code=reason_code,
        )
        if not self._enqueue(command):
            LOGGER.error(
                "Temporal sample spool queue is full; sample could not be finalized: session_id=%s",
                event.session_id,
            )
            return None
        self._finalized_session_ids.add(event.session_id)
        self._active = None
        self._decision_sequence = None
        self._verification_remaining = 0
        return None if plan_already_created else self._plan(event)

    def _plan(self, event: TemporalSampleEvent) -> TemporalSampleCapturePlan:
        spool_path = self._spool_path(event)
        asset = {
            "payload_type": "temporal_sample_spool",
            "spool_uri": str(spool_path),
            "sequence_id": event.sequence_id,
            "session_id": event.session_id,
            "track_id": event.track_id,
        }
        intent = {
            "node_key": "temporal-sample-recorder",
            "node_type": "output.temporal_sample_recorder",
            "node_category": "output",
            "output_port": "capture",
            "payload_type": "capture_intents",
            "intent": {
                "capture_mode": "temporal_sequence",
                "target_project_ids": [],
                "asset": asset,
                "condition": "temporal_session_finalized",
                "metadata": {
                    "node_key": "temporal-sample-recorder",
                    "duplicate_policy": (
                        "keep" if event.config.deduplication == "keep" else "suppress_by_digest"
                    ),
                    "retention_days": event.config.retention_days,
                    "annotation_schema_id": event.config.annotation_schema_id,
                    "dataset_id": event.config.dataset_id,
                    "auto_import_enabled": event.config.auto_import_enabled,
                    "import_sample_every_n": event.config.import_sample_every_n,
                    "import_maximum_per_hour": event.config.import_maximum_per_hour,
                    "ground_truth_status": (
                        "technical_review_only"
                        if event.config.profile == "curvature_diagnostics"
                        else "pending_label_studio"
                    ),
                },
            },
        }
        return TemporalSampleCapturePlan(
            session_id=event.session_id,
            sequence_id=event.sequence_id,
            capture_intent=intent,
            asset_references=temporal_sample_asset_references(
                capture_root=self.capture_root,
                channel_id=event.channel_id,
                sequence_id=event.sequence_id,
                queued=True,
                diagnostic_video=event.config.diagnostic_video,
                verification_video=event.config.verification_frame_count > 0,
                profile=event.config.profile,
                rectified_video=event.config.rectified_video,
            ),
        )

    def _spool_path(self, event: TemporalSampleEvent) -> Path:
        return temporal_sample_spool_path(
            self.capture_root,
            runtime_execution_id=event.runtime_execution_id,
            sequence_id=event.sequence_id,
        )

    def _enqueue(self, command: _AppendCommand | _FinalizeCommand) -> bool:
        try:
            self._queue.put_nowait(command)
        except queue.Full:
            return False
        return True

    def _run(self) -> None:
        while True:
            command = self._queue.get()
            try:
                if command is None:
                    return
                if isinstance(command, _AppendCommand):
                    self._writer.append(command.spool_path, command.event)
                else:
                    self._writer.finalize(
                        command.spool_path,
                        decision_frame_sequence=command.decision_frame_sequence,
                        complete=command.complete,
                        reason_code=command.reason_code,
                    )
            except Exception:
                LOGGER.exception("Temporal sample spool command failed.")
            finally:
                self._queue.task_done()


def temporal_recorder_config_from_graph(
    graph_nodes: list[dict[str, Any]],
    *,
    default_retention_days: int,
) -> TemporalSampleRecorderConfig | None:
    for node in graph_nodes:
        if node.get("node_type") != "analysis.stem_curvature_phase_solver":
            continue
        raw = node.get("config")
        config = raw if isinstance(raw, Mapping) else {}
        if not bool(config.get("record_session_artifacts", False)):
            continue
        return TemporalSampleRecorderConfig(
            enabled=True,
            profile="curvature_diagnostics",
            fps=float(config.get("session_recording_fps", 15.0)),
            rectified_video=bool(config.get("record_rectified_video", True)),
            diagnostic_video=bool(config.get("record_overlay_video", True)),
            verification_frame_count=0,
            maximum_frames=int(config.get("session_recording_maximum_frames", 900)),
            minimum_geometry_quality=float(
                config.get("minimum_recording_geometry_quality", 0.0)
            ),
            minimum_visible_fraction=float(
                config.get("minimum_recording_visible_fraction", 0.0)
            ),
            minimum_rotation_confidence=float(
                config.get("minimum_recording_phase_confidence", 0.0)
            ),
            retention_days=int(
                config.get("session_recording_retention_days", default_retention_days)
            ),
            annotation_schema_id="stem_curvature_technical_review.v1",
            dataset_id="",
            auto_import_enabled=False,
        )
    for node in graph_nodes:
        if node.get("node_type") != "analysis.stem_temporal_orientation":
            continue
        raw = node.get("config")
        config = TemporalSampleRecorderConfig.from_node_config(
            raw if isinstance(raw, Mapping) else {},
            default_retention_days=default_retention_days,
        )
        return config if config.enabled else None
    return None


def attach_temporal_capture_plans(
    result: RuntimeExecutionResult,
    plans: list[TemporalSampleCapturePlan],
) -> None:
    if not plans:
        return
    result.capture_intents.extend(plan.capture_intent for plan in plans)
    for plan in plans:
        _replace_asset_references(result, plan.session_id, plan.asset_references)


def _replace_asset_references(
    result: RuntimeExecutionResult,
    session_id: str,
    references: Mapping[str, Any],
) -> None:
    for ports in result.node_outputs.values():
        for payload in ports.values():
            if not isinstance(payload, dict):
                continue
            _replace_payload_asset_references(payload, session_id, references)
    for entry in result.persisted_results:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if isinstance(payload, dict):
            _replace_payload_asset_references(payload, session_id, references)


def _replace_payload_asset_references(
    payload: dict[str, Any],
    session_id: str,
    references: Mapping[str, Any],
) -> None:
    candidates = [payload.get("result")]
    if payload.get("payload_type") == "stem_curvature_phase_solution":
        candidates.append(payload.get("orientation_result"))
    for value in candidates:
        if isinstance(value, dict) and value.get("session_id") == session_id:
            value["asset_references"] = dict(references)
