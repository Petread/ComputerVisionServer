from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cv_datasets.temporal_import import (
    TemporalSampleImportError,
    build_temporal_label_studio_task,
    find_temporal_task_id,
    temporal_import_idempotency_key,
)


def _artifact(root: Path, name: str, content: bytes, media_type: str) -> dict[str, str]:
    path = root / name
    path.write_bytes(content)
    return {
        "uri": name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": media_type,
    }


def _manifest(root: Path) -> Path:
    root.mkdir(parents=True)
    primary = _artifact(root, "sequence.mp4", b"synthetic-mp4", "video/mp4")
    diagnostic = _artifact(root, "diagnostic.mp4", b"diagnostic-mp4", "video/mp4")
    features = _artifact(
        root,
        "features.jsonl",
        b'{"frame_index":0}\n{"frame_index":1}\n',
        "application/x-ndjson",
    )
    rotation = _artifact(
        root,
        "rotation-trace.jsonl",
        b'{"frame_index":0}\n{"frame_index":1}\n',
        "application/x-ndjson",
    )
    canonical_item = {
        "sequence_id": "sequence-001",
        "group_id": "track-001",
        "primary_video": primary,
        "diagnostic_assets": [diagnostic],
        "frames": [
            {"frame_index": 0, "timestamp": 0.0},
            {"frame_index": 1, "timestamp": 0.1},
        ],
        "decision_frame_index": 1,
        "decision_timestamp": 0.1,
        "feature_manifest": features,
        "rotation_trace": rotation,
        "schemas": {
            "feature_schema": "temporal.frame-features.v1",
            "rotation_schema": "stem.rotation.v1",
            "calibration_schema": "rotation-calibration.v2",
            "annotation_schema": "stem_orientation_angle.v1",
        },
        "human_labels": {"sequence_labels": [], "timeline_labels": []},
        "derived_targets": [],
        "label_source": "automatic",
        "label_quality": {
            "status": "unreviewed",
            "score": None,
            "reason_codes": ["ground_truth_pending_label_studio"],
        },
        "exclusion_reason": None,
        "split": "train",
        "provenance": {
            "source_system": "inference-server-runtime",
            "recording_id": "sequence-001",
            "created_at": "2026-08-04T10:00:00Z",
            "software_version": "temporal-sample-recorder.v1",
            "source_channel_id": "channel-1",
            "pipeline_version_id": "pipeline-v1",
            "runtime_session_id": "session-1",
            "track_id": "track-001",
            "component_versions": {"feature": "v1"},
            "attributes": {"angle_label_eligible": True},
        },
    }
    manifest = {
        "schema_version": "temporal.sample.manifest.v1",
        "sequence_id": "sequence-001",
        "sample_status": "recorded",
        "constant_fps": 10.0,
        "decision_source_timestamp": "2026-08-04T10:00:01Z",
        "angle_label_eligible": True,
        "ground_truth_status": "pending_label_studio",
        "direction_provenance": {
            "rotation_viewpoint_convention": "viewed_from_configured_log_end_a",
            "positive_rotation_direction": "clockwise_viewed_from_configured_log_end",
        },
        "artifacts": {
            "sequence": primary,
            "features": features,
            "rotation_trace": rotation,
            "diagnostics": [diagnostic],
        },
        "baseline_prediction": {
            "available": True,
            "is_ground_truth": False,
            "frame_start_index": 0,
            "frame_end_index": 1,
            "confidence": 0.84,
            "estimate_method": "projected_baseline",
        },
        "canonical_sequence_item": canonical_item,
    }
    path = root / "sequence-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_temporal_task_is_complete_and_prediction_is_not_ground_truth(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "state" / "runtime-captures" / "sample")

    built = build_temporal_label_studio_task(
        manifest,
        dataset_id="dataset-1",
        label_project_id="label-project-1",
        local_files_root=tmp_path / "state",
        local_files_url_prefix="/data/local-files/?d=",
    )

    task_data = built.task["data"]
    assert task_data["video"] == "/data/local-files/?d=runtime-captures/sample/sequence.mp4"
    assert task_data["frame_rate"] == 10.0
    assert task_data["diagnostic_video"] == (
        "/data/local-files/?d=runtime-captures/sample/diagnostic.mp4"
    )
    assert task_data["sequence_id"] == "sequence-001"
    assert task_data["decision_frame_index"] == 1
    assert task_data["schemas"]["annotation_schema"] == "stem_orientation_angle.v1"
    metadata = built.task["meta"]
    assert metadata["sequence_id"] == "sequence-001"
    assert metadata["group_id"] == "track-001"
    assert metadata["frame_count"] == 2
    assert metadata["decision_frame_index"] == 1
    assert metadata["decision_video_timestamp_seconds"] == 0.1
    assert metadata["feature_manifest"]["sha256"]
    assert metadata["rotation_trace"]["sha256"]
    assert metadata["direction_provenance"]["rotation_viewpoint_convention"]
    assert metadata["baseline_prediction"]["is_ground_truth"] is False
    assert "annotations" not in built.task
    prediction = built.task["predictions"][0]
    assert prediction["result"][0]["from_name"] == "target_orientation"
    assert prediction["result"][0]["origin"] == "prediction"
    assert prediction["result"][0]["value"]["ranges"] == [{"start": 0, "end": 1}]


def test_temporal_import_key_uses_all_required_identity_parts() -> None:
    common = {
        "dataset_id": "dataset-1",
        "label_project_id": "project-1",
        "sequence_id": "sequence-1",
        "primary_asset_sha256": "a" * 64,
    }
    first = temporal_import_idempotency_key(**common)

    assert first == temporal_import_idempotency_key(**common)
    for key in common:
        changed = dict(common)
        changed[key] = "b" * 64 if key == "primary_asset_sha256" else f"{common[key]}-new"
        assert temporal_import_idempotency_key(**changed) != first


def test_temporal_import_rejects_missing_or_changed_media(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "state" / "sample")
    (manifest.parent / "sequence.mp4").write_bytes(b"changed")

    with pytest.raises(TemporalSampleImportError, match="checksum"):
        build_temporal_label_studio_task(
            manifest,
            dataset_id="dataset-1",
            label_project_id="project-1",
            local_files_root=tmp_path / "state",
            local_files_url_prefix="/data/local-files/?d=",
        )


def test_temporal_import_recovery_detects_duplicate_external_tasks() -> None:
    tasks = [
        {"id": 12, "meta": {"temporal_import_key": "key-1"}},
        {"id": 13, "meta": {"temporal_import_key": "key-1"}},
    ]

    with pytest.raises(TemporalSampleImportError, match="multiple tasks"):
        find_temporal_task_id(tasks, idempotency_key="key-1")
