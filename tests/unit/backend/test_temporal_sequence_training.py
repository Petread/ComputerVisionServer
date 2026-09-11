from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cv_db.model_catalog import (
    is_trainable_architecture,
    recipe_for_architecture,
    trainable_model_capability,
)
from cv_deploy.manifests import ExportManifest
from cv_status import TaskType
from cv_training.backends.temporal_sequence import (
    _multitask_loss,
    _split_identity_digest,
    build_temporal_model,
)
from cv_training.recipes import load_training_recipe
from cv_training.temporal_data import (
    TemporalDatasetPreparationError,
    load_staged_temporal_samples,
    resolve_temporal_feature_specs,
    stage_temporal_sequence_dataset,
)
from cv_training.temporal_metrics import (
    evaluate_temporal_predictions,
    temporal_angle_error_deg,
)
from cv_training.trainer import get_default_trainer, resolve_trainer_architecture
from cv_training.trainer_types import TrainerArtifactValidation, TrainerResult
from training_worker.runtime import (
    _build_evaluation_summary,
    _build_io_schema,
    _build_preprocessing,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    path.write_bytes(content.encode("utf-8"))
    return hashlib.sha256(content.encode()).hexdigest()


def _derived(name: str, kind: str, value: object) -> dict[str, object]:
    return {
        "name": name,
        "kind": kind,
        "value": value,
        "scope": "decision_frame",
        "valid": True,
        "builder_id": "stem_orientation_angle",
        "builder_version": "1.0.0",
        "source_annotation_schema": "stem_orientation_angle.v1",
        "quality": 0.9,
        "reason_codes": [],
        "metadata": {},
    }


def _snapshot_item(
    root: Path,
    *,
    sequence_id: str,
    split: str,
    base_value: float,
    bow: str = "relevant",
    target_visible: str = "yes",
    sequence_valid: str = "usable",
    angle_valid: bool = True,
    label_quality: str = "silver",
) -> dict[str, object]:
    directory = root / "sequences" / sequence_id
    frames = [{"frame_index": index, "timestamp": index / 10.0} for index in range(4)]
    features = [
        {
            "frame_index": index,
            "video_timestamp_seconds": index / 10.0,
            "geometry": {"signed_curvature": base_value + index},
        }
        for index in range(4)
    ]
    rotations = [
        {
            "frame_index": index,
            "video_timestamp_seconds": index / 10.0,
            "delta_rotation_deg": 0.0 if index == 0 else 10.0,
            "cumulative_rotation_deg_unwrapped": index * 10.0,
            "rotation_confidence": 0.9,
            "valid": True,
            "calibration_valid": True,
            "trace_discontinuity": False,
            "trace_generation": 0,
            "calibration_id": "rotation-calibration-1",
            "calibration_method": "known_angle_positions",
            "calibration_version": "1.0.0",
            "reason_codes": [],
        }
        for index in range(4)
    ]
    feature_sha = _write_jsonl(directory / "features.jsonl", features)
    rotation_sha = _write_jsonl(directory / "rotation-trace.jsonl", rotations)
    _write_json(
        directory / "source-sequence-manifest.json",
        {
            "baseline_prediction": {
                "frame_start_index": 1,
                "frame_end_index": 2,
                "confidence": 0.8,
                "estimate_method": "projected_baseline",
            }
        },
    )
    angle_targets = [_derived("angle_target_valid", "boolean", angle_valid)]
    if angle_valid:
        angle_targets.extend(
            [
                _derived("signed_correction_deg", "regression", -20.0),
                _derived("sin_2e", "regression", -0.6427876097),
                _derived("cos_2e", "regression", 0.7660444431),
                _derived("angle_label_confidence", "regression", 0.9),
            ]
        )
    labels = [
        {"kind": "sequence_choice", "name": "sequence_valid", "value": sequence_valid},
        {"kind": "sequence_choice", "name": "bow_relevance", "value": bow},
        {"kind": "sequence_choice", "name": "target_visible", "value": target_visible},
        {
            "kind": "sequence_choice",
            "name": "final_orientation_quality",
            "value": "acceptable",
        },
    ]
    video_sha = hashlib.sha256(sequence_id.encode()).hexdigest()
    return {
        "sequence_id": sequence_id,
        "group_id": f"group-{sequence_id}",
        "primary_video": {
            "uri": f"sequences/{sequence_id}/sequence.mp4",
            "sha256": video_sha,
            "media_type": "video/mp4",
        },
        "diagnostic_assets": [],
        "frames": frames,
        "decision_frame_index": 3,
        "decision_timestamp": 0.3,
        "feature_manifest": {
            "uri": f"sequences/{sequence_id}/features.jsonl",
            "sha256": feature_sha,
            "media_type": "application/x-ndjson",
        },
        "rotation_trace": {
            "uri": f"sequences/{sequence_id}/rotation-trace.jsonl",
            "sha256": rotation_sha,
            "media_type": "application/x-ndjson",
        },
        "schemas": {
            "feature_schema": "temporal.frame-features.v1",
            "rotation_schema": "temporal.rotation-trace.v1",
            "calibration_schema": "rotation-calibration.v1",
            "annotation_schema": "stem_orientation_angle.v1",
        },
        "human_labels": {"sequence_labels": labels, "timeline_labels": []},
        "derived_targets": angle_targets,
        "label_source": "label_studio",
        "label_quality": {
            "status": "reviewed",
            "tier": label_quality,
            "score": 0.9,
            "reason_codes": [],
        },
        "exclusion_reason": None,
        "split": split,
        "provenance": {
            "source_system": "inference-server-runtime",
            "recording_id": sequence_id,
            "created_at": "2026-08-04T10:00:00Z",
            "software_version": "temporal-sample-recorder.v1",
            "component_versions": {},
            "attributes": {
                "rotation_viewpoint_convention": "viewed_from_configured_log_end_a",
                "positive_rotation_direction": "clockwise_viewed_from_configured_log_end",
            },
        },
    }


def _build_snapshot(root: Path) -> None:
    items = [
        _snapshot_item(root, sequence_id="train-angle", split="train", base_value=1.0),
        _snapshot_item(root, sequence_id="val-angle", split="val", base_value=101.0),
        _snapshot_item(
            root,
            sequence_id="test-straight",
            split="test",
            base_value=201.0,
            bow="nearly_straight",
            target_visible="no",
            angle_valid=False,
        ),
        _snapshot_item(
            root,
            sequence_id="excluded-unclear",
            split="test",
            base_value=301.0,
            target_visible="unclear",
            angle_valid=False,
        ),
        _snapshot_item(
            root,
            sequence_id="excluded-unusable",
            split="test",
            base_value=401.0,
            sequence_valid="unusable",
            target_visible="no",
            angle_valid=False,
        ),
    ]
    _write_json(
        root / "annotations" / "canonical_dataset.json",
        {
            "dataset_id": "dataset-temporal",
            "task_type": "temporal_sequence",
            "annotation_schema": "stem_orientation_angle.v1",
            "items": items,
        },
    )
    _write_json(
        root / "manifests" / "validation_report.json",
        {
            "status": "passed",
            "checks": {
                "group_split_integrity": True,
                "no_post_decision_frames": True,
                "no_duplicate_asset_hashes_across_splits": True,
                "no_time_overlap_across_splits": True,
                "checksums_valid": True,
                "fit_statistics_train_only": True,
            },
        },
    )
    _write_json(root / "manifests" / "split_metadata.json", {"group_split_key": "group_id"})
    _write_json(
        root / "snapshot_manifest.json",
        {
            "content_checksum": "sha256:fixture",
            "training_constraints": {
                "group_split_key": "group_id",
                "primary_input_ends_at_decision_frame": True,
                "fit_scalers_and_thresholds_on": "train_only",
                "gold_and_silver_metrics_separate": True,
            },
        },
    )


def _test_recipe() -> dict[str, Any]:
    return {
        "task_type": "temporal_sequence",
        "temporal": {
            "minimum_window_steps": 4,
            "maximum_window_steps": 8,
            "require_angle_targets": True,
            "exclude_unclear_labels": ["bow_relevance", "target_visible"],
            "feature_set_id": "test.temporal-features.v1",
            "feature_specs": [
                {
                    "name": "signed_curvature",
                    "path": "geometry.signed_curvature",
                    "required": True,
                },
                {
                    "name": "cumulative_rotation",
                    "path": "rotation.cumulative_rotation_deg_unwrapped",
                    "required": True,
                },
                {
                    "name": "rotation_confidence",
                    "path": "rotation.rotation_confidence",
                    "required": True,
                },
                {
                    "name": "rotation_valid",
                    "path": "rotation.valid",
                    "required": True,
                    "scale": False,
                },
            ],
        },
    }


def test_temporal_staging_is_causal_group_safe_and_train_scaled(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    _build_snapshot(snapshot)
    artifact = stage_temporal_sequence_dataset(
        training_job_id="training-1",
        snapshot_id="snapshot-1",
        snapshot_root=snapshot,
        stage_root=tmp_path / "stage",
        recipe=_test_recipe(),
    )
    comparison_artifact = stage_temporal_sequence_dataset(
        training_job_id="training-2",
        snapshot_id="snapshot-1",
        snapshot_root=snapshot,
        stage_root=tmp_path / "comparison-stage",
        recipe=_test_recipe(),
    )

    train = load_staged_temporal_samples(artifact, "train")
    validation = load_staged_temporal_samples(artifact, "val")
    test = load_staged_temporal_samples(artifact, "test")
    scaler = json.loads(Path(artifact.metadata["scaler_path"]).read_text(encoding="utf-8"))
    contract = json.loads(
        Path(artifact.metadata["temporal_contract_path"]).read_text(encoding="utf-8")
    )

    assert [sample["sequence_id"] for sample in train] == ["train-angle"]
    assert [sample["sequence_id"] for sample in validation] == ["val-angle"]
    assert [sample["sequence_id"] for sample in test] == ["test-straight"]
    assert scaler["fit_split"] == "train"
    assert scaler["mean"][0] == pytest.approx(2.5)
    assert contract["causal"] is True
    assert contract["input_end"] == "decision_frame_or_earlier"
    assert contract["post_decision_frames_allowed"] is False
    assert test[0]["targets"]["angle_target_valid"] is False
    assert test[0]["targets"]["nearly_straight"] is True
    assert test[0]["targets"]["target_observed"] == 0.0
    assert artifact.metadata["split_counts"] == {"train": 1, "val": 1, "test": 1}
    comparison_contract = json.loads(
        Path(comparison_artifact.metadata["temporal_contract_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert comparison_contract["schema_digest"] == contract["schema_digest"]
    assert comparison_contract["scaler_sha256"] == contract["scaler_sha256"]
    assert _split_identity_digest(comparison_artifact) == _split_identity_digest(artifact)


def test_temporal_snapshot_rejects_post_decision_input(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    _build_snapshot(snapshot)
    canonical_path = snapshot / "annotations" / "canonical_dataset.json"
    payload = json.loads(canonical_path.read_text(encoding="utf-8"))
    payload["items"][0]["decision_frame_index"] = 2
    payload["items"][0]["decision_timestamp"] = 0.2
    _write_json(canonical_path, payload)

    with pytest.raises((TemporalDatasetPreparationError, ValueError), match="decision"):
        stage_temporal_sequence_dataset(
            training_job_id="training-1",
            snapshot_id="snapshot-1",
            snapshot_root=snapshot,
            stage_root=tmp_path / "stage",
            recipe=_test_recipe(),
        )


def test_temporal_feature_contract_rejects_plc_or_action_inputs() -> None:
    with pytest.raises(TemporalDatasetPreparationError, match="forbidden"):
        resolve_temporal_feature_specs(
            {
                "feature_set_id": "invalid.v1",
                "feature_specs": [
                    {"name": "plc_action", "path": "machine.command", "required": True}
                ],
            }
        )


def test_prefix_augmentation_rebuilds_angle_after_target_interval(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    _build_snapshot(snapshot)
    canonical_path = snapshot / "annotations" / "canonical_dataset.json"
    payload = json.loads(canonical_path.read_text(encoding="utf-8"))
    payload["items"][0]["human_labels"]["timeline_labels"] = [
        {
            "kind": "timeline_interval",
            "name": "target_orientation",
            "start_frame_index": 1,
            "end_frame_index": 2,
            "start_timestamp": 0.1,
            "end_timestamp": 0.2,
        }
    ]
    _write_json(canonical_path, payload)
    recipe = _test_recipe()
    recipe["temporal"]["minimum_window_steps"] = 3
    recipe["temporal"]["prefix_augmentation"] = {
        "enabled": True,
        "stride_steps": 1,
        "maximum_prefixes_per_sequence": 4,
        "minimum_rotation_confidence": 0.8,
    }

    artifact = stage_temporal_sequence_dataset(
        training_job_id="training-prefix",
        snapshot_id="snapshot-prefix",
        snapshot_root=snapshot,
        stage_root=tmp_path / "stage",
        recipe=recipe,
    )
    samples = load_staged_temporal_samples(artifact, "train")
    prefix = next(sample for sample in samples if sample["prefix_augmented"])

    assert prefix["sequence_length"] == 3
    assert prefix["metadata"]["decision_frame_index"] == 2
    assert prefix["targets"]["angle_target_valid"] is True
    assert prefix["targets"]["signed_correction_deg"] == pytest.approx(-5.0)


def test_angle_metrics_are_modulo_180_and_separate_gold_silver() -> None:
    records = [
        {
            "angle_target_valid": True,
            "predicted_angle_deg": -89.0,
            "expected_angle_deg": 89.0,
            "raw_angle_confidence": 0.9,
            "angle_confidence": 0.8,
            "label_quality_tier": "gold",
            "expected_bow_relevance": 1.0,
            "predicted_bow_relevance_probability": 0.9,
            "expected_target_observed": 1.0,
            "predicted_target_observed_probability": 0.8,
            "nearly_straight": False,
            "metadata": {"sequence_length": 64},
        },
        {
            "angle_target_valid": True,
            "predicted_angle_deg": -20.0,
            "expected_angle_deg": 20.0,
            "raw_angle_confidence": 0.4,
            "angle_confidence": 0.3,
            "label_quality_tier": "silver",
            "expected_bow_relevance": 1.0,
            "predicted_bow_relevance_probability": 0.6,
            "expected_target_observed": 1.0,
            "predicted_target_observed_probability": 0.6,
            "nearly_straight": False,
            "metadata": {"sequence_length": 64},
        },
    ]

    report = evaluate_temporal_predictions(records, direction_deadband_deg=5.0)

    assert temporal_angle_error_deg(-89.0, 89.0) == pytest.approx(2.0)
    assert report["gold"]["mean_absolute_error_deg"] == pytest.approx(2.0)
    assert report["silver"]["mean_absolute_error_deg"] == pytest.approx(40.0)
    assert report["all_valid"]["direction_accuracy"] == pytest.approx(0.5)
    assert report["all_valid"]["dangerous_opposite_direction_rate"] == pytest.approx(0.5)
    assert report["all_valid"]["severe_error_rate_gt_30_deg"] == pytest.approx(0.5)


def test_temporal_export_contract_has_no_dummy_image_or_class_contract() -> None:
    io_schema = _build_io_schema(
        TaskType.TEMPORAL_SEQUENCE,
        image_size=None,
        onnx_path=None,
    )
    preprocessing = _build_preprocessing(
        {"task_type": "temporal_sequence", "temporal": {}},
        trainer_backend="pytorch-temporal-sequence",
        temporal_contract={
            "feature_set_id": "stem_orientation.frame_features.v1",
            "minimum_window_steps": 12,
            "maximum_window_steps": 180,
        },
    )
    manifest = ExportManifest.model_validate(
        {
            "training_job_id": "training-1",
            "model_family_slug": "stem-angle",
            "task_type": "temporal_sequence",
            "recipe_name": "baseline_temporal_tcn",
            "recipe_version": "1.0.0",
            "image_size": None,
            "generated_at": "2026-08-04T10:00:00Z",
            "source_snapshot_id": "snapshot-1",
            "class_map_version": None,
            "class_map_sha256": None,
            "class_count": 0,
            "confidence_threshold_default": None,
            "io_schema": io_schema,
            "hardware_compatibility": {
                "accelerator": "nvidia_gpu",
                "target_gpu_profile": "RTX 4500 Ada",
                "min_compute_capability": "8.9",
                "cuda_version": "12",
                "tensorrt_version": "10",
                "precision": "fp16",
            },
            "onnx": {
                "uri": "model.onnx",
                "sha256": "0" * 64,
                "size_bytes": 1,
                "format": "onnx",
                "file_name": "model.onnx",
            },
            "temporal": {"shadow_only": True},
        }
    )

    assert manifest.image_size is None
    assert manifest.class_count == 0
    assert io_schema["input_tensors"][0]["layout"] == "TF"
    assert io_schema["input_tensors"][0]["dims"] == ["T", "F"]
    assert preprocessing["minimum_window_steps"] == 12
    assert preprocessing["maximum_window_steps"] == 180


def test_temporal_evaluation_is_never_automatically_promotion_ready() -> None:
    result = TrainerResult(
        final_epoch=1,
        final_step=1,
        best_metric_name="gold_mean_absolute_error_deg",
        best_metric_value=1.0,
        best_metrics={"gold_mean_absolute_error_deg": 1.0},
        final_validation_metrics={"gold_mean_absolute_error_deg": 1.0},
    )
    summary = _build_evaluation_summary(
        context=SimpleNamespace(
            task_type=TaskType.TEMPORAL_SEQUENCE,
            promotion_criteria=[
                {
                    "metric_name": "gold_mean_absolute_error_deg",
                    "comparison": "lte",
                    "threshold": 10.0,
                    "phase": "evaluation",
                }
            ],
        ),
        result=result,
        artifact_validation=TrainerArtifactValidation(status="passed"),
        trainer=SimpleNamespace(trainer_backend="pytorch-temporal-sequence"),
        trainer_mode="real",
    )

    assert summary["promotion_results"][0]["passed"] is True
    assert summary["promotion_ready"] is False
    assert summary["promotion_block_reason"].startswith("temporal_sequence_shadow_only")


@pytest.mark.parametrize("family", ["tcn", "gru"])
def test_temporal_architectures_have_shadow_runtime_and_recipes(
    family: str,
) -> None:
    assert is_trainable_architecture(
        task_type=TaskType.TEMPORAL_SEQUENCE,
        family=family,
        version="v1",
        size="small",
    )
    capability = trainable_model_capability(
        task_type=TaskType.TEMPORAL_SEQUENCE,
        family=family,
        version="v1",
        size="small",
    )
    assert capability is not None
    assert capability.export_support is True
    assert capability.deployment_support is True
    assert capability.inference_support is True
    assert capability.status == "experimental"
    assert "shadow_only" in capability.status_flags
    resolved = resolve_trainer_architecture(
        task_type=TaskType.TEMPORAL_SEQUENCE,
        family=family,
        version="v1",
        size="small",
    )
    assert resolved.supported is True
    assert get_default_trainer(
        TaskType.TEMPORAL_SEQUENCE,
        architecture=resolved,
    ).trainer_backend == "pytorch-temporal-sequence"
    recipe_name, recipe_version = recipe_for_architecture(
        task_type=TaskType.TEMPORAL_SEQUENCE,
        family=family,
        version="v1",
    )
    assert recipe_version == "1.0.0"
    recipe = load_training_recipe(
        TaskType.TEMPORAL_SEQUENCE,
        recipe_name,
        workspace_root=Path.cwd(),
    )
    assert recipe["trainer_backend"] == "pytorch-temporal-sequence"
    assert recipe["temporal"]["require_angle_targets"] is True


@pytest.mark.parametrize("model_kind", ["tcn", "gru", "decision_frame"])
def test_temporal_models_cpu_smoke_and_state_roundtrip(model_kind: str, tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    model = build_temporal_model(
        torch=torch,
        nn=torch.nn,
        model_kind=model_kind,
        input_dim=9,
        hidden_dim=8,
        maximum_steps=6,
        temporal_config={"dropout": 0.0, "tcn_levels": 2},
    ).eval()
    inputs = torch.randn(2, 6, 9)
    inputs[:, :, -1] = 1.0
    with torch.no_grad():
        first = model(inputs)
    assert len(first) == 6
    assert all(tuple(output.shape)[0] == 2 for output in first)
    angle_norm = torch.sqrt(first[2] ** 2 + first[3] ** 2)
    assert torch.allclose(angle_norm, torch.ones_like(angle_norm), atol=1e-6)

    checkpoint = tmp_path / f"{model_kind}.pt"
    torch.save(model.state_dict(), checkpoint)
    restored = build_temporal_model(
        torch=torch,
        nn=torch.nn,
        model_kind=model_kind,
        input_dim=9,
        hidden_dim=8,
        maximum_steps=6,
        temporal_config={"dropout": 0.0, "tcn_levels": 2},
    ).eval()
    restored.load_state_dict(torch.load(checkpoint, map_location="cpu"))
    with torch.no_grad():
        second = restored(inputs)
    for expected, actual in zip(first, second, strict=True):
        assert torch.allclose(expected, actual, atol=1e-7)


def test_invalid_angle_target_masks_only_angle_specific_losses() -> None:
    torch = pytest.importorskip("torch")
    batch = {
        "bow_relevance": torch.tensor([1.0]),
        "target_observed": torch.tensor([1.0]),
        "angle_target_valid": torch.tensor([False]),
        "sin_2e": torch.tensor([1.0]),
        "cos_2e": torch.tensor([0.0]),
        "angle_label_confidence": torch.tensor([1.0]),
        "final_quality_valid": torch.tensor([False]),
        "final_quality_ordinal": torch.tensor([0]),
    }
    common = (
        torch.tensor([[0.8]]),
        torch.tensor([[0.7]]),
    )
    first, first_metrics = _multitask_loss(
        torch,
        (
            *common,
            torch.tensor([[1.0]]),
            torch.tensor([[0.0]]),
            torch.tensor([[0.9]]),
            torch.zeros((1, 2)),
        ),
        batch,
        {},
    )
    second, second_metrics = _multitask_loss(
        torch,
        (
            *common,
            torch.tensor([[-1.0]]),
            torch.tensor([[0.0]]),
            torch.tensor([[0.1]]),
            torch.zeros((1, 2)),
        ),
        batch,
        {},
    )

    assert first_metrics["loss_angle_circular"] == 0.0
    assert first_metrics["loss_angle_confidence"] == 0.0
    assert second_metrics["loss_angle_circular"] == 0.0
    assert second_metrics["loss_angle_confidence"] == 0.0
    assert first.item() == pytest.approx(second.item())
