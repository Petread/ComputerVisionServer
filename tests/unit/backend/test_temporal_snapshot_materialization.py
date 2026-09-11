from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cv_datasets import materialize_temporal_snapshot


def _artifact(root: Path, name: str, content: bytes, media_type: str) -> dict[str, str]:
    path = root / name
    path.write_bytes(content)
    return {
        "uri": name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": media_type,
    }


def _jsonl(rows: list[dict[str, object]]) -> bytes:
    return "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ).encode()


def _sample_manifest(
    root: Path,
    *,
    sequence_id: str = "sequence-001",
    group_id: str = "physical-stem-001",
    trace_confidence: float = 0.9,
    gold: bool = False,
    primary_content: bytes | None = None,
    calibration_valid: bool = True,
) -> Path:
    root.mkdir(parents=True)
    frames = [
        {
            "frame_index": index,
            "video_timestamp_seconds": index / 10.0,
            "source_frame_sequence": index + 10,
            "source_timestamp": f"2026-08-04T10:00:0{index}Z",
            "frame_sha256": hashlib.sha256(f"frame-{index}".encode()).hexdigest(),
        }
        for index in range(4)
    ]
    features = [
        {
            "schema_version": "temporal.frame-features.v1",
            **frame,
            "geometry": {"signed_bow_normalized": 0.5},
        }
        for frame in frames
    ]
    rotations = [
        {
            "schema_version": "temporal.rotation-trace.v1",
            **frame,
            "valid": True,
            "cumulative_rotation_deg_unwrapped": index * 20.0,
            "rotation_confidence": trace_confidence,
            "trace_discontinuity": False,
            "trace_generation": 0,
            "calibration_valid": calibration_valid,
            "calibration_id": "rotation-calibration-001",
            "calibration_version": "2.1.0",
            "calibration_method": "known_angle_positions",
            "reason_codes": [],
        }
        for index, frame in enumerate(frames)
    ]
    primary = _artifact(
        root,
        "sequence.mp4",
        primary_content or f"deterministic-video-{sequence_id}".encode(),
        "video/mp4",
    )
    feature_ref = _artifact(
        root,
        "features.jsonl",
        _jsonl(features),
        "application/x-ndjson",
    )
    rotation_ref = _artifact(
        root,
        "rotation-trace.jsonl",
        _jsonl(rotations),
        "application/x-ndjson",
    )
    item = {
        "sequence_id": sequence_id,
        "group_id": group_id,
        "primary_video": primary,
        "diagnostic_assets": [],
        "frames": [
            {"frame_index": frame["frame_index"], "timestamp": frame["video_timestamp_seconds"]}
            for frame in frames
        ],
        "decision_frame_index": 3,
        "decision_timestamp": 0.3,
        "feature_manifest": feature_ref,
        "rotation_trace": rotation_ref,
        "schemas": {
            "feature_schema": "temporal.frame-features.v1",
            "rotation_schema": "temporal.rotation-trace.v1",
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
            "recording_id": sequence_id,
            "created_at": "2026-08-04T10:00:00Z",
            "software_version": "temporal-sample-recorder.v1",
            "source_channel_id": "channel-001",
            "pipeline_version_id": "pipeline-v1",
            "runtime_session_id": "session-001",
            "track_id": group_id,
            "component_versions": {"rotation": "v1"},
            "attributes": {},
        },
    }
    manifest: dict[str, object] = {
        "schema_version": "temporal.sample.manifest.v1",
        "recorder_version": "temporal-sample-recorder.v1",
        "sequence_id": sequence_id,
        "sample_status": "recorded",
        "decision_frame_index": 3,
        "frame_index_mapping": frames,
        "direction_provenance": {
            "rotation_viewpoint_convention": "viewed_from_configured_log_end_a",
            "positive_rotation_direction": "clockwise_viewed_from_configured_log_end",
        },
        "artifacts": {
            "sequence": primary,
            "features": feature_ref,
            "rotation_trace": rotation_ref,
            "diagnostics": [],
        },
        "canonical_sequence_item": item,
    }
    if gold:
        manifest["verified_rotation_reference"] = {
            "verified": True,
            "reference_id": "encoder-measurement-001",
            "method": "independent_rotation_measurement",
            "version": "encoder-reference.v1",
        }
    path = root / "sequence-manifest.json"
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return path


def _task(
    *,
    task_id: int = 17,
    primary_sha256: str,
    sequence_id: str = "sequence-001",
    updated_at: str = "2026-08-04T11:00:00Z",
) -> dict[str, object]:
    return {
        "id": task_id,
        "meta": {
            "sequence_id": sequence_id,
            "primary_asset_sha256": primary_sha256,
        },
        "annotations": [
            {
                "id": 41,
                "updated_at": updated_at,
                "completed_by": {"id": 7, "email": "labeler@example.test"},
                "result": [
                    {"from_name": "sequence_valid", "value": {"choices": ["usable"]}},
                    {"from_name": "bow_relevance", "value": {"choices": ["relevant"]}},
                    {"from_name": "target_visible", "value": {"choices": ["yes"]}},
                    {
                        "from_name": "target_orientation",
                        "value": {
                            "ranges": [{"start": 1, "end": 2}],
                            "timelinelabels": ["target_orientation"],
                        },
                    },
                    {
                        "from_name": "final_orientation_quality",
                        "value": {"choices": ["acceptable"]},
                    },
                ],
            }
        ],
    }


def _primary_sha256(manifest: Path) -> str:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return str(payload["artifacts"]["sequence"]["sha256"])


def test_temporal_export_materializes_versioned_signed_angle_snapshot(tmp_path: Path) -> None:
    source = _sample_manifest(tmp_path / "source")
    task = _task(primary_sha256=_primary_sha256(source))

    first = materialize_temporal_snapshot(
        snapshot_id="snapshot-001",
        dataset_id="dataset-001",
        export_format="JSON",
        annotation_schema_id="stem_orientation_angle.v1",
        project_label_schema_version="stem_orientation_angle.v1",
        minimum_annotation_count=1,
        train_ratio=100,
        val_ratio=0,
        test_ratio=0,
        export_payload=[task],
        manifest_paths_by_task_id={17: source},
        target_root=tmp_path / "snapshot-a",
        minimum_rotation_confidence=0.8,
    )
    second = materialize_temporal_snapshot(
        snapshot_id="snapshot-002",
        dataset_id="dataset-001",
        export_format="JSON",
        annotation_schema_id="stem_orientation_angle.v1",
        project_label_schema_version="stem_orientation_angle.v1",
        minimum_annotation_count=1,
        train_ratio=100,
        val_ratio=0,
        test_ratio=0,
        export_payload=[task],
        manifest_paths_by_task_id={"17": source},
        target_root=tmp_path / "snapshot-b",
        minimum_rotation_confidence=0.8,
    )

    assert first.validation.status == "passed"
    assert first.content_sha256 == second.content_sha256
    item = first.canonical_dataset.items[0]
    targets = {target.name: target for target in item.derived_targets}
    assert targets["angle_target_valid"].value is True
    assert targets["signed_correction_deg"].value == pytest.approx(-30.0)
    assert targets["rotation_direction"].value == "negative"
    assert targets["target_angle_interval_deg"].value == pytest.approx([-40.0, -20.0])
    assert targets["angle_tolerance_deg"].value == pytest.approx(10.0)
    assert targets["sin_2e"].value == pytest.approx(-0.8660254)
    assert targets["cos_2e"].value == pytest.approx(0.5)
    assert targets["angle_label_source"].value.startswith("silver_")
    assert targets["final_orientation_quality"].value == "acceptable"
    assert targets["final_orientation_quality"].scope == "decision_window"
    assert item.label_quality.tier == "silver"
    assert item.human_labels.source_annotation_id == "41"
    assert item.human_labels.source_revision == "2026-08-04T11:00:00Z"
    assert item.primary_video.uri.startswith("sequences/")
    assert item.frames[-1].frame_index == item.decision_frame_index
    assert (tmp_path / "snapshot-a" / item.primary_video.uri).read_bytes() == (
        b"deterministic-video-sequence-001"
    )
    training_root = (
        tmp_path
        / "snapshot-a"
        / "annotations"
        / "training-ready"
        / "temporal_sequence"
    )
    assert (training_root / "train.human-labels.jsonl").is_file()
    assert (training_root / "train.derived-targets.jsonl").is_file()


def test_low_rotation_confidence_keeps_sample_without_angle_target(tmp_path: Path) -> None:
    source = _sample_manifest(tmp_path / "source", trace_confidence=0.2)
    artifacts = materialize_temporal_snapshot(
        snapshot_id="snapshot-low-confidence",
        dataset_id="dataset-001",
        export_format="JSON",
        annotation_schema_id="stem_orientation_angle.v1",
        project_label_schema_version="stem_orientation_angle.v1",
        minimum_annotation_count=1,
        train_ratio=100,
        val_ratio=0,
        test_ratio=0,
        export_payload=[_task(primary_sha256=_primary_sha256(source))],
        manifest_paths_by_task_id={17: source},
        target_root=tmp_path / "snapshot",
        minimum_rotation_confidence=0.8,
    )

    item = artifacts.canonical_dataset.items[0]
    targets = {target.name: target for target in item.derived_targets}
    assert artifacts.validation.status == "passed"
    assert item.exclusion_reason is None
    assert item.label_quality.tier == "weak"
    assert targets["angle_target_valid"].value is False
    assert "rotation_confidence_too_low" in targets["angle_target_valid"].reason_codes
    assert "signed_correction_deg" not in targets
    assert targets["bow_relevance"].value == "relevant"


def test_invalid_rotation_calibration_keeps_non_angle_heads(tmp_path: Path) -> None:
    source = _sample_manifest(tmp_path / "source", calibration_valid=False)
    artifacts = materialize_temporal_snapshot(
        snapshot_id="snapshot-invalid-calibration",
        dataset_id="dataset-001",
        export_format="JSON",
        annotation_schema_id="stem_orientation_angle.v1",
        project_label_schema_version="stem_orientation_angle.v1",
        minimum_annotation_count=1,
        train_ratio=100,
        val_ratio=0,
        test_ratio=0,
        export_payload=[_task(primary_sha256=_primary_sha256(source))],
        manifest_paths_by_task_id={17: source},
        target_root=tmp_path / "snapshot",
    )

    item = artifacts.canonical_dataset.items[0]
    targets = {target.name: target for target in item.derived_targets}
    assert artifacts.validation.status == "passed"
    assert item.exclusion_reason is None
    assert item.label_quality.tier == "weak"
    assert targets["angle_target_valid"].value is False
    assert "rotation_calibration_invalid" in targets["angle_target_valid"].reason_codes
    assert targets["final_orientation_quality"].value == "acceptable"


def test_changed_primary_video_fails_checksum_validation(tmp_path: Path) -> None:
    source = _sample_manifest(tmp_path / "source")
    task = _task(primary_sha256=_primary_sha256(source))
    (source.parent / "sequence.mp4").write_bytes(b"tampered")

    artifacts = materialize_temporal_snapshot(
        snapshot_id="snapshot-tampered",
        dataset_id="dataset-001",
        export_format="JSON",
        annotation_schema_id="stem_orientation_angle.v1",
        project_label_schema_version="stem_orientation_angle.v1",
        minimum_annotation_count=1,
        train_ratio=100,
        val_ratio=0,
        test_ratio=0,
        export_payload=[task],
        manifest_paths_by_task_id={17: source},
        target_root=tmp_path / "snapshot",
    )

    assert artifacts.validation.status == "failed"
    assert artifacts.canonical_dataset.items == []
    assert any(
        issue.code == "primary_video_checksum_mismatch"
        for issue in artifacts.validation.issues
    )


def test_verified_independent_reference_marks_angle_as_gold(tmp_path: Path) -> None:
    source = _sample_manifest(tmp_path / "source", gold=True)
    artifacts = materialize_temporal_snapshot(
        snapshot_id="snapshot-gold",
        dataset_id="dataset-001",
        export_format="JSON",
        annotation_schema_id="stem_orientation_angle.v1",
        project_label_schema_version="stem_orientation_angle.v1",
        minimum_annotation_count=1,
        train_ratio=100,
        val_ratio=0,
        test_ratio=0,
        export_payload=[_task(primary_sha256=_primary_sha256(source))],
        manifest_paths_by_task_id={17: source},
        target_root=tmp_path / "snapshot",
    )

    item = artifacts.canonical_dataset.items[0]
    targets = {target.name: target for target in item.derived_targets}
    assert item.label_quality.tier == "gold"
    assert targets["angle_label_quality"].value == "gold"
    assert targets["angle_label_source"].value == "gold_verified_rotation_reference"


def test_group_split_keeps_all_variants_of_one_stem_together(tmp_path: Path) -> None:
    first = _sample_manifest(
        tmp_path / "source-a",
        sequence_id="sequence-a",
        group_id="physical-stem-shared",
    )
    second = _sample_manifest(
        tmp_path / "source-b",
        sequence_id="sequence-b",
        group_id="physical-stem-shared",
    )
    artifacts = materialize_temporal_snapshot(
        snapshot_id="snapshot-grouped",
        dataset_id="dataset-001",
        export_format="JSON",
        annotation_schema_id="stem_orientation_angle.v1",
        project_label_schema_version="stem_orientation_angle.v1",
        minimum_annotation_count=1,
        train_ratio=100,
        val_ratio=0,
        test_ratio=0,
        export_payload=[
            _task(
                task_id=17,
                sequence_id="sequence-a",
                primary_sha256=_primary_sha256(first),
            ),
            _task(
                task_id=18,
                sequence_id="sequence-b",
                primary_sha256=_primary_sha256(second),
            ),
        ],
        manifest_paths_by_task_id={17: first, 18: second},
        target_root=tmp_path / "snapshot",
    )

    assert artifacts.validation.status == "passed"
    assert {item.split for item in artifacts.canonical_dataset.items} == {"train"}
    assert {item.group_id for item in artifacts.canonical_dataset.items} == {
        "physical-stem-shared"
    }


def test_duplicate_hash_and_overlapping_time_across_splits_are_rejected(
    tmp_path: Path,
) -> None:
    shared_video = b"same-physical-input"
    first = _sample_manifest(
        tmp_path / "source-a",
        sequence_id="sequence-a",
        group_id="group-0",
        primary_content=shared_video,
    )
    second = _sample_manifest(
        tmp_path / "source-b",
        sequence_id="sequence-b",
        group_id="group-2",
        primary_content=shared_video,
    )
    artifacts = materialize_temporal_snapshot(
        snapshot_id="snapshot-leakage",
        dataset_id="dataset-001",
        export_format="JSON",
        annotation_schema_id="stem_orientation_angle.v1",
        project_label_schema_version="stem_orientation_angle.v1",
        minimum_annotation_count=1,
        train_ratio=50,
        val_ratio=0,
        test_ratio=50,
        export_payload=[
            _task(
                task_id=17,
                sequence_id="sequence-a",
                primary_sha256=_primary_sha256(first),
            ),
            _task(
                task_id=18,
                sequence_id="sequence-b",
                primary_sha256=_primary_sha256(second),
            ),
        ],
        manifest_paths_by_task_id={17: first, 18: second},
        target_root=tmp_path / "snapshot",
    )

    codes = {issue.code for issue in artifacts.validation.issues}
    assert {item.split for item in artifacts.canonical_dataset.items} == {"train", "test"}
    assert artifacts.validation.status == "failed"
    assert "duplicate_asset_hash_across_splits" in codes
    assert "source_time_overlap_across_splits" in codes
