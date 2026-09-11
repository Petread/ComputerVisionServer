from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from capture_service.executor import (
    _replace_temporal_orientation_references,
    _replace_temporal_result_references,
    drain_capture_work,
)
from cv_datasets import CanonicalSequenceItem
from cv_db import (
    Dataset,
    LabelProject,
    ResultRecord,
    RuntimeCaptureRequest,
    RuntimeChannel,
    record_runtime_execution_result,
    session_scope,
)
from cv_runtime.payloads import FramePayload, RuntimeExecutionResult
from cv_runtime.temporal_samples import (
    TEMPORAL_FEATURE_SCHEMA,
    TEMPORAL_ROTATION_TRACE_SCHEMA,
    TemporalSampleRecorderConfig,
    TemporalSampleSpoolWriter,
    extract_temporal_sample_event,
    materialize_temporal_sample_spool,
    temporal_sample_spool_path,
)
from realtime_executor.temporal_samples import (
    RealtimeTemporalSampleRecorder,
    TemporalSampleCapturePlan,
    attach_temporal_capture_plans,
    temporal_recorder_config_from_graph,
)
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories


def _encoded_frame(sequence: int) -> bytes:
    image = np.zeros((96, 160, 3), dtype=np.uint8)
    image[:, :, 1] = 40 + sequence * 20
    cv2.line(image, (20 + sequence, 20), (130 + sequence, 75), (255, 255, 255), 3)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    return encoded.tobytes()


def _runtime_result(
    sequence: int,
    *,
    session_id: str = "stem-session-a",
    track_id: str = "track-a",
    decision_frame_sequence: int | None = None,
    discontinuity: bool = False,
) -> tuple[RuntimeExecutionResult, FramePayload]:
    timestamp = f"2026-08-04T10:00:0{sequence}Z"
    geometry = {
        "payload_type": "stem_geometry",
        "schema_version": "stem.geometry.v1",
        "feature_schema_version": "stem.geometry.v1",
        "valid": True,
        "track_id": track_id,
        "frame_sequence": sequence,
        "timestamp": timestamp,
        "quality_score": 0.9,
        "visible_fraction": 0.85,
        "signed_bow_normalized": 0.6 - sequence * 0.15,
        "rms_residual_normalized": 0.2,
        "shape_class": "c_bow",
        "segmentation_route": {"model_version_id": "mvr-seg-1"},
    }
    rotation = {
        "payload_type": "stem_rotation",
        "schema_version": "stem.rotation.v1",
        "valid": not discontinuity,
        "track_id": track_id,
        "frame_sequence": sequence,
        "timestamp": timestamp,
        "delta_rotation_deg": None if sequence == 1 else 10.0,
        "cumulative_rotation_deg_unwrapped": float((sequence - 1) * 10),
        "rotation_direction": "stationary" if sequence == 1 else "positive",
        "rotation_confidence": 0.9,
        "detected_feature_count": 24,
        "matched_feature_count": 22,
        "inlier_feature_count": 20,
        "inlier_ratio": 0.9,
        "translation_component": {"x_px": 2.0, "y_px": 0.5, "magnitude_px": 2.06},
        "trace_discontinuity": discontinuity,
        "trace_generation": 1 if discontinuity else 0,
        "calibration_valid": True,
        "calibration_id": "rotation-cal-1",
        "calibration_version": "1.0.0",
        "calibration_method": "known_angle_positions",
        "calibration_hash": "rotation-hash",
        "reason_codes": [],
    }
    decision_ready = decision_frame_sequence is not None
    orientation = {
        "schema_version": "stem.orientation.v2",
        "session_id": session_id,
        "track_id": track_id,
        "channel_id": "channel-stem",
        "pipeline_version_id": "pipeline-v1",
        "observation_started_at": "2026-08-04T10:00:01Z",
        "session_state": ("PRE_ENTRY_DECISION_READY" if decision_ready else "THROW_IN_PROGRESS"),
        "decision_ready": decision_ready,
        "decision_frame": (
            {
                "frame_sequence": decision_frame_sequence,
                "timestamp": f"2026-08-04T10:00:0{decision_frame_sequence}Z",
            }
            if decision_ready
            else None
        ),
        "target_frame_or_interval_candidate": (
            {
                "frame_start": max(int(decision_frame_sequence or 1) - 1, 1),
                "frame_end": int(decision_frame_sequence or 1),
            }
            if decision_ready
            else None
        ),
        "confidence": 0.82 if decision_ready else 0.0,
        "estimate_method": "projected_baseline",
        "reason_codes": [],
        "current_observation": {"frame_sequence": sequence},
        "direction_provenance": {
            "rotation_viewpoint_convention": "viewed_from_configured_log_end_a",
            "positive_rotation_direction": "clockwise_viewed_from_configured_log_end",
        },
        "versions": {
            "rotation_schema_version": "stem.rotation.v1",
            "rotation_calibration_version": "1.0.0",
            "feature_schema_version": "stem.geometry.v1",
            "segmentation_model_version_id": "mvr-seg-1",
        },
    }
    result = RuntimeExecutionResult(
        status="completed",
        channel_id="channel-stem",
        pipeline_version_id="pipeline-v1",
        runtime_execution_id="runtime-file-test-1",
        correlation_id=f"corr-{sequence}",
        started_at=timestamp,
        completed_at=timestamp,
        node_outputs={
            "geometry": {"stem_geometry": geometry},
            "rotation": {"stem_rotation": rotation},
            "temporal": {"result": {"payload_type": "result", "result": orientation}},
        },
    )
    frame = FramePayload.from_bytes(
        _encoded_frame(sequence),
        mime_type="image/jpeg",
        width=160,
        height=96,
        timestamp=timestamp,
    )
    return result, frame


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _curvature_diagnostic_runtime_result(
    sequence: int,
    *,
    decision_ready: bool = False,
    relocked: bool = False,
) -> tuple[RuntimeExecutionResult, FramePayload]:
    timestamp = f"2026-08-04T11:00:0{sequence}Z"
    frame = FramePayload.from_bytes(
        _encoded_frame(sequence),
        mime_type="image/jpeg",
        width=160,
        height=96,
        timestamp=timestamp,
    )
    points = [
        {
            "frame_id": str(index),
            "frame_sequence": index,
            "timestamp": f"2026-08-04T11:00:0{index}Z",
            "cumulative_phase_deg": float(index * 20),
            "curvature_rms_mm": float(7 - index),
            "geometry_confidence": 0.9,
            "phase_confidence": 0.9,
            "common_coverage": 0.85,
            "base_weight": 0.6885,
            "fit_weight": 0.65,
            "status": "weighted",
            "included": True,
            "reason_codes": [],
        }
        for index in range(1, sequence + 1)
    ]
    graph = {
        "schema_version": "stem.curvature-phase-diagnostic.v1",
        "x_axis_label": "cumulative roll phase",
        "x_axis_unit": "deg",
        "y_axis_label": "horizontal curvature",
        "y_axis_unit": "mm",
        "points": points,
        "fit_curve": [
            {"cumulative_phase_deg": float(phase), "fitted_curvature_mm": 4.0}
            for phase in range(0, 181, 20)
        ],
        "observed_phase_span_deg": float(max(sequence - 1, 0) * 20),
        "minimum_phase_deg": 90.0,
        "minimum_curvature_mm": 3.0,
        "maximum_phase_deg": 0.0,
        "maximum_curvature_mm": 7.0,
        "current_phase_deg": float(sequence * 20),
        "decision_phase_deg": float(sequence * 20) if decision_ready else None,
        "curvature_frozen": decision_ready,
        "fit_available": True,
        "reason_codes": [],
    }
    orientation = (
        {
            "schema_version": "stem.orientation.v3",
            "session_id": "curvature-session-a",
            "track_id": "track-a",
            "decision_ready": True,
            "decision_frame": {"frame_sequence": sequence, "timestamp": timestamp},
            "decision": "ROTATE",
            "recommended_rotation_deg_signed": 20.0,
            "overall_confidence": 0.82,
            "calibration_id": "process-cal-1",
            "rotation_calibration_id": "phase-cal-1",
            "reason_codes": ["CURVATURE_MINIMUM_CORRECTION_READY"],
        }
        if decision_ready
        else None
    )
    curvature = {
        "payload_type": "stem_horizontal_curvature",
        "track_id": "track-a",
        "frame_sequence": sequence,
        "timestamp": timestamp,
        "geometry_confidence": 0.9,
        "visible_length_ratio": 0.85,
        "signed_bow_mm": 5.0,
        "curvature_rms_mm": 6.0,
    }
    phase = {
        "payload_type": "stem_surface_phase",
        "track_id": "track-a",
        "frame_sequence": sequence,
        "timestamp": timestamp,
        "delta_phase_deg": 20.0,
        "cumulative_phase_deg": float(sequence * 20),
        "raw_delta_phase": 0.1,
        "cumulative_raw_phase": sequence * 0.1,
        "direction": "positive",
        "tracking_mode": "KEYFRAME_RELOCK" if relocked else "INCREMENTAL",
        "relock_status": "RELOCKED" if relocked else "NOT_REQUIRED",
        "feature_count": 24,
        "inlier_ratio": 0.9,
        "confidence": 0.9,
        "calibration_id": "phase-cal-1",
        "reason_codes": [],
    }
    solution = {
        "payload_type": "stem_curvature_phase_solution",
        "session_id": "curvature-session-a",
        "track_id": "track-a",
        "frame_sequence": sequence,
        "session_state": "RESULT_READY" if decision_ready else "OBSERVING_CURVATURE",
        "state": "FROZEN" if decision_ready else "COLLECTING",
        "observed_phase_span_deg": graph["observed_phase_span_deg"],
        "current_phase_deg": graph["current_phase_deg"],
        "target_phase_deg": 90.0,
        "confidence": 0.8,
        "diagnostic_graph": graph,
        "process_calibration_id": "process-cal-1",
        "process_calibration_hash": "process-hash",
        "phase_calibration_id": "phase-cal-1",
        "phase_calibration_hash": "phase-hash",
        "reason_codes": [],
        "orientation_result": orientation,
    }
    result = RuntimeExecutionResult(
        status="completed",
        channel_id="channel-stem",
        pipeline_version_id="pipeline-v1",
        runtime_execution_id="runtime-curvature-test-1",
        correlation_id=f"curvature-corr-{sequence}",
        started_at=timestamp,
        completed_at=timestamp,
        node_outputs={
            "curvature": {"curvature": curvature},
            "phase": {"surface_phase": phase},
            "solver": {"solution": solution},
        },
        overlay_primitives=[
            {
                "primitive_type": "label",
                "layer": "stem_curvature_phase_diagnostic",
                "label": "diagnostic",
                "geometry": {},
                "style": {},
                "metadata": {"diagnostic_graph": graph},
            }
        ],
    )
    result.set_recording_frame(frame)
    return result, frame


def test_file_test_sequence_materializes_aligned_deterministic_artifacts(tmp_path: Path) -> None:
    config = TemporalSampleRecorderConfig(
        fps=10.0,
        diagnostic_video=True,
        verification_frame_count=1,
    )
    manifests: list[dict[str, object]] = []
    for run_name in ("first", "second"):
        capture_root = tmp_path / run_name
        writer = TemporalSampleSpoolWriter()
        events = []
        for sequence in range(1, 5):
            result, frame = _runtime_result(
                sequence,
                decision_frame_sequence=3 if sequence == 4 else None,
            )
            event = extract_temporal_sample_event(result, frame, config=config)
            assert event is not None
            events.append(event)
        spool = temporal_sample_spool_path(
            capture_root,
            runtime_execution_id=events[0].runtime_execution_id,
            sequence_id=events[0].sequence_id,
        )
        for event in events:
            assert writer.append(spool, event) is True
        writer.finalize(
            spool,
            decision_frame_sequence=3,
            complete=True,
            reason_code="decision_frame_reached",
        )

        materialized = materialize_temporal_sample_spool(
            spool,
            capture_root=capture_root,
            request_id=f"request-{run_name}",
        )
        manifest = json.loads(materialized.manifest_path.read_text(encoding="utf-8"))
        manifests.append(manifest)
        output_root = materialized.manifest_path.parent

        assert manifest["decision_source_frame_sequence"] == 3
        assert manifest["decision_frame_index"] == 2
        assert manifest["constant_fps"] == 10.0
        assert manifest["verification_separate_from_primary"] is True
        assert manifest["angle_label_eligible"] is True
        assert (output_root / "sequence.mp4").is_file()
        assert (output_root / "diagnostic.mp4").is_file()
        assert (output_root / "verification.mp4").is_file()
        features = _read_jsonl(output_root / "features.jsonl")
        rotation = _read_jsonl(output_root / "rotation-trace.jsonl")
        assert [item["frame_index"] for item in features] == [0, 1, 2]
        assert [item["source_frame_sequence"] for item in features] == [1, 2, 3]
        assert [item["source_frame_sequence"] for item in rotation] == [1, 2, 3]
        assert all(item["schema_version"] == TEMPORAL_FEATURE_SCHEMA for item in features)
        assert all(item["schema_version"] == TEMPORAL_ROTATION_TRACE_SCHEMA for item in rotation)
        assert (
            cv2.VideoCapture(str(output_root / "sequence.mp4")).get(cv2.CAP_PROP_FRAME_COUNT) == 3
        )
        assert (
            cv2.VideoCapture(str(output_root / "verification.mp4")).get(cv2.CAP_PROP_FRAME_COUNT)
            == 1
        )
        CanonicalSequenceItem.model_validate(manifest["canonical_sequence_item"])

    assert manifests[0] == manifests[1]


def test_discontinuous_rotation_trace_is_not_angle_label_eligible(tmp_path: Path) -> None:
    config = TemporalSampleRecorderConfig(fps=10.0, diagnostic_video=False)
    writer = TemporalSampleSpoolWriter()
    events = []
    for sequence in range(1, 4):
        result, frame = _runtime_result(
            sequence,
            decision_frame_sequence=3 if sequence == 3 else None,
            discontinuity=sequence == 2,
        )
        event = extract_temporal_sample_event(result, frame, config=config)
        assert event is not None
        events.append(event)
    spool = temporal_sample_spool_path(
        tmp_path,
        runtime_execution_id=events[0].runtime_execution_id,
        sequence_id=events[0].sequence_id,
    )
    for event in events:
        writer.append(spool, event)
    writer.finalize(
        spool,
        decision_frame_sequence=3,
        complete=True,
        reason_code="decision_frame_reached",
    )

    materialized = materialize_temporal_sample_spool(spool, capture_root=tmp_path)
    manifest = json.loads(materialized.manifest_path.read_text(encoding="utf-8"))

    assert materialized.sample_status == "recorded"
    assert materialized.angle_label_eligible is False
    assert manifest["eligible_training_heads"] == ["bow_relevance"]
    assert manifest["ground_truth_status"] == "pending_label_studio"


def test_realtime_handoff_contains_only_spool_reference_and_closes_track_switch(
    tmp_path: Path,
) -> None:
    recorder = RealtimeTemporalSampleRecorder(
        capture_root=tmp_path,
        config=TemporalSampleRecorderConfig(diagnostic_video=False),
        queue_capacity=16,
    )
    first_result, first_frame = _runtime_result(1)
    switched_result, switched_frame = _runtime_result(
        2,
        session_id="stem-session-b",
        track_id="track-b",
    )

    assert recorder.observe(first_result, first_frame) == []
    plans = recorder.observe(switched_result, switched_frame)
    recorder.close()

    assert len(plans) == 1
    intent = plans[0].capture_intent["intent"]
    assert intent["capture_mode"] == "temporal_sequence"
    assert intent["target_project_ids"] == []
    assert intent["asset"]["payload_type"] == "temporal_sample_spool"
    assert "image_base64" not in json.dumps(intent)
    spool = Path(intent["asset"]["spool_uri"])
    ready = json.loads((spool / "READY.json").read_text(encoding="utf-8"))
    assert ready["complete"] is False
    assert ready["reason_code"] == "track_changed_before_decision"


def test_temporal_capture_uses_existing_queue_and_updates_persisted_result(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "capture-service")
    settings = build_test_settings("capture-service", tmp_path)
    channel_id = "rch_temporal_sample"
    dataset_id = "dst_temporal_sample"
    label_project_id = "lpr_temporal_sample"
    with session_scope(settings) as session:
        session.add(
            Dataset(
                id=dataset_id,
                slug="stem-orientation-sequences",
                name="Stem orientation sequences",
                task_type="temporal_sequence",
                label_schema_version="stem_orientation_angle.v1",
                status="ready",
            )
        )
        session.add(
            LabelProject(
                id=label_project_id,
                dataset_id=dataset_id,
                label_studio_project_id=741,
                title="Stem orientation timeline",
                task_type="temporal_sequence",
                label_schema_version="stem_orientation_angle.v1",
                status="active",
                auto_sync_enabled=True,
            )
        )
        session.add(
            RuntimeChannel(
                id=channel_id,
                slug="temporal-sample",
                name="Temporal sample",
                technical_place="test-line",
                mode="realtime",
                authoring_label_project_ids=[label_project_id],
                result_history_policy={
                    "enabled": True,
                    "retention_hours": 24,
                    "sample_interval_seconds": 1,
                    "max_records": 100,
                },
            )
        )

    recorder = RealtimeTemporalSampleRecorder(
        capture_root=settings.storage.runtime_capture_root(),
        config=TemporalSampleRecorderConfig(diagnostic_video=False),
        queue_capacity=16,
    )
    plan = None
    final_result = None
    for sequence in range(1, 4):
        result, frame = _runtime_result(
            sequence,
            decision_frame_sequence=3 if sequence == 3 else None,
        )
        plans = recorder.observe(result, frame)
        if plans:
            plan = plans[0]
            final_result = result
    recorder.close()
    assert plan is not None
    assert final_result is not None
    final_result.channel_id = channel_id
    orientation = final_result.node_outputs["temporal"]["result"]["result"]
    final_result.persisted_results = [
        {
            "node_key": "persisted-result",
            "node_type": "output.persisted_result",
            "payload": {"payload_type": "result", "result": orientation},
        }
    ]
    final_result.final_response = dict(orientation)
    final_result.capture_intents = [plan.capture_intent]
    with session_scope(settings) as session:
        record_runtime_execution_result(
            session,
            final_result,
            channel_id=channel_id,
            retain_history=True,
        )
        row = session.query(RuntimeCaptureRequest).one()
        assert row.source_type == "temporal_sample"
        assert row.target_label_project_id == "temporal_sequence_dataset"
        assert row.asset_payload["payload_type"] == "temporal_sample_spool"
        assert "image_base64" not in json.dumps(row.asset_payload)

    class _FakeLabelStudioClient:
        imported: list[dict[str, object]] = []

        def __init__(self, **_kwargs: object) -> None:
            pass

        def list_project_tasks(self, _project_id: int) -> list[dict[str, object]]:
            return []

        def import_tasks_with_ids(
            self,
            _project_id: int,
            tasks: list[dict[str, object]],
        ) -> dict[str, object]:
            self.imported.extend(tasks)
            return {"task_ids": [910]}

    stats = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)

    assert stats.delivered == 1
    manifest_path: Path
    with session_scope(settings) as session:
        row = session.query(RuntimeCaptureRequest).one()
        assert row.status == "delivered"
        assert row.target_label_project_id == label_project_id
        assert row.label_studio_task_ids == [910]
        assert row.metadata_json["ground_truth_status"] == "pending_human_annotation"
        assert Path(row.asset_uri or "").name == "sequence-manifest.json"
        manifest_path = Path(row.asset_uri or "")
        result_record = session.query(ResultRecord).one()
        saved = result_record.result["persisted_results"][0]["payload"]["result"]
        assert saved["asset_references"]["status"] == "recorded"
        assert saved["asset_references"]["sequence_manifest_ref"] == row.asset_uri
        assert saved["asset_references"]["label_studio_import"]["status"] == "delivered"
        assert saved["asset_references"]["label_studio_import"][
            "label_studio_task_ids"
        ] == [910]
        assert result_record.trigger_response["asset_references"]["status"] == "recorded"
    assert len(_FakeLabelStudioClient.imported) == 1




    imported_task = _FakeLabelStudioClient.imported[0]
    assert imported_task["data"]["video"].startswith("/data/local-files/?d=")
    assert imported_task["meta"]["decision_frame_index"] == 2
    assert imported_task["predictions"][0]["result"][0]["origin"] == "prediction"
    assert "annotations" not in imported_task

    repeated = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)
    assert repeated.processed == 0
    assert len(_FakeLabelStudioClient.imported) == 1

    with session_scope(settings) as session:
        session.add(
            RuntimeCaptureRequest(
                id="rcr_temporal_duplicate",
                channel_id=channel_id,
                capture_rule_id="node_temporal-sample-recorder",
                target_label_project_id="temporal_sequence_dataset",
                source_type="temporal_sample",
                capture_mode="temporal_sequence",
                asset_uri=str(manifest_path),
                asset_payload={},
                source_frame_metadata={"session_id": "stem-session-a"},
                capture_intent={},
                status="queued",
                retry_count=0,
                max_attempts=3,
                metadata_json={
                    "dataset_id": dataset_id,
                    "annotation_schema_id": "stem_orientation_angle.v1",
                    "auto_import_enabled": True,
                },
            )
        )
    duplicate = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)
    assert duplicate.delivered == 1
    with session_scope(settings) as session:
        duplicate_row = session.get(RuntimeCaptureRequest, "rcr_temporal_duplicate")
        assert duplicate_row is not None
        assert duplicate_row.label_studio_task_ids == [910]
        assert duplicate_row.duplicate_of_request_id is not None
        assert duplicate_row.metadata_json["label_studio_submission"] == (
            "reused_internal_ledger"
        )
    assert len(_FakeLabelStudioClient.imported) == 1

    (manifest_path.parent / "sequence.mp4").write_bytes(b"corrupted-video")
    with session_scope(settings) as session:
        session.add(
            RuntimeCaptureRequest(
                id="rcr_temporal_bad_media",
                channel_id=channel_id,
                capture_rule_id="node_temporal-sample-recorder",
                target_label_project_id="temporal_sequence_dataset",
                source_type="temporal_sample",
                capture_mode="temporal_sequence",
                asset_uri=str(manifest_path),
                asset_payload={},
                source_frame_metadata={"session_id": "stem-session-a"},
                capture_intent={},
                status="queued",
                retry_count=0,
                max_attempts=3,
                metadata_json={
                    "dataset_id": dataset_id,
                    "annotation_schema_id": "stem_orientation_angle.v1",
                    "auto_import_enabled": True,
                },
            )
        )

    attention = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)
    assert attention.needs_attention == 1
    with session_scope(settings) as session:
        failed = session.get(RuntimeCaptureRequest, "rcr_temporal_bad_media")
        assert failed is not None
        assert failed.status == "needs_attention"
        assert "checksum" in (failed.last_error or "")
    assert len(_FakeLabelStudioClient.imported) == 1


def test_curvature_diagnostic_profile_is_optional_and_materializes_bounded_artifacts(
    tmp_path: Path,
) -> None:
    disabled = temporal_recorder_config_from_graph(
        [
            {
                "node_type": "analysis.stem_curvature_phase_solver",
                "config": {"record_session_artifacts": False},
            }
        ],
        default_retention_days=17,
    )
    assert disabled is None
    config = temporal_recorder_config_from_graph(
        [
            {
                "node_type": "analysis.stem_curvature_phase_solver",
                "config": {
                    "record_session_artifacts": True,
                    "record_rectified_video": True,
                    "record_overlay_video": True,
                    "session_recording_fps": 10,
                    "session_recording_maximum_frames": 2,
                    "session_recording_retention_days": 17,
                },
            }
        ],
        default_retention_days=30,
    )
    assert config is not None
    assert config.profile == "curvature_diagnostics"
    assert config.auto_import_enabled is False
    assert config.maximum_frames == 2
    assert config.retention_days == 17

    final_runtime_result, _ = _curvature_diagnostic_runtime_result(
        4,
        decision_ready=True,
        relocked=True,
    )
    attach_temporal_capture_plans(
        final_runtime_result,
        [
            TemporalSampleCapturePlan(
                session_id="curvature-session-a",
                sequence_id="diagnostic-sequence",
                capture_intent={},
                asset_references={"status": "queued", "fit_ref": "fit.json"},
            )
        ],
    )
    orientation_with_assets = final_runtime_result.node_outputs["solver"]["solution"][
        "orientation_result"
    ]
    assert orientation_with_assets["asset_references"]["fit_ref"] == "fit.json"

    capture_root = tmp_path / "curvature-diagnostics"
    writer = TemporalSampleSpoolWriter()
    spool = None
    for sequence in range(1, 5):
        result, frame = _curvature_diagnostic_runtime_result(
            sequence,
            decision_ready=sequence == 4,
            relocked=sequence == 4,
        )
        event = extract_temporal_sample_event(result, frame, config=config)
        assert event is not None
        assert event.config.profile == "curvature_diagnostics"
        spool = temporal_sample_spool_path(
            capture_root,
            runtime_execution_id=event.runtime_execution_id,
            sequence_id=event.sequence_id,
        )
        writer.append(spool, event)
    assert spool is not None
    assert len(list((spool / "records").glob("*.json"))) == 2
    assert len(list((spool / "frames").glob("*.jpg"))) == 2
    writer.finalize(
        spool,
        decision_frame_sequence=4,
        complete=True,
        reason_code="decision_frame_reached",
    )

    materialized = materialize_temporal_sample_spool(
        spool,
        capture_root=capture_root,
    )
    artifact_root = materialized.manifest_path.parent
    assert materialized.sample_status == "recorded"
    assert materialized.angle_label_eligible is False
    assert (artifact_root / "rectified.mp4").is_file()
    assert (artifact_root / "overlay.mp4").is_file()
    assert (artifact_root / "observations.jsonl").is_file()
    assert (artifact_root / "phase_trace.json").is_file()
    assert (artifact_root / "fit.json").is_file()
    assert (artifact_root / "final-result.json").is_file()
    assert (artifact_root / "calibrations.json").is_file()
    phase_trace = json.loads((artifact_root / "phase_trace.json").read_text(encoding="utf-8"))
    assert len(phase_trace["samples"]) == 2
    assert phase_trace["samples"][-1]["relock_status"] == "RELOCKED"
    final_result = json.loads((artifact_root / "final-result.json").read_text(encoding="utf-8"))
    assert final_result["schema_version"] == "stem.orientation.v3"
    assert final_result["decision"] == "ROTATE"


def test_recorded_diagnostic_references_update_v3_history_and_solution_payloads() -> None:
    orientation = {
        "schema_version": "stem.orientation.v3",
        "session_id": "curvature-session-a",
        "asset_references": {"status": "queued"},
    }
    references = {"status": "recorded", "fit_ref": "fit.json"}
    values = [
        {"payload": {"payload_type": "result", "result": orientation}},
        {
            "payload": {
                "payload_type": "stem_curvature_phase_solution",
                "orientation_result": orientation,
            }
        },
    ]

    updated = _replace_temporal_result_references(
        values,
        session_id="curvature-session-a",
        references=references,
    )
    trigger = _replace_temporal_orientation_references(
        orientation,
        session_id="curvature-session-a",
        references=references,
    )

    assert updated[0]["payload"]["result"]["asset_references"] == references
    assert updated[1]["payload"]["orientation_result"]["asset_references"] == references
    assert trigger["asset_references"] == references
