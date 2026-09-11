from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
from cv_contracts import (
    ModelArchitectureSummary,
    RealtimeTemporalSequenceRequest,
    TemporalSequenceFrame,
)
from cv_status import DeploymentReleaseStatus, TargetPath, TaskType
from cv_training.temporal_data import TemporalFeatureSpec, _extract_feature
from cv_utils import PlatformError
from inference_gateway.runtime.resolver import ReleaseContext
from inference_gateway.runtime.service import (
    _OutputTensor,
    _prepare_temporal_model_tensor,
    _temporal_prediction_from_outputs,
)
from pydantic import ValidationError


def _write_artifact(root: Path, name: str, payload: dict[str, object]) -> dict[str, str]:
    path = root / "source" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return {
        "path": f"source/{name}",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _temporal_route(tmp_path: Path) -> ReleaseContext:
    scaler = {
        "schema_version": "temporal-scaler.v1",
        "feature_order": ["curvature", "motion_speed"],
        "mean": [1.0, 10.0],
        "scale": [2.0, 5.0],
    }
    descriptors = {
        "scaler": _write_artifact(tmp_path, "scaler.json", scaler),
        "angle_contract": _write_artifact(
            tmp_path,
            "angle-contract.json",
            {
                "schema_version": "temporal-angle.v1",
                "representation": ["sin_2e", "cos_2e"],
                "periodicity_deg": 180.0,
                "decode": "0.5 * atan2(sin_2e, cos_2e)",
                "wrap_interval_deg": [-90.0, 90.0],
                "wrap_upper_bound_exclusive": True,
                "deadband_deg": 5.0,
                "rotation_viewpoint_convention": "viewed_from_log_end_a",
                "positive_rotation_direction": "clockwise_viewed_from_log_end_a",
            },
        ),
        "head_class_order": _write_artifact(
            tmp_path,
            "head-class-order.json",
            {
                "schema_version": "temporal-head-order.v1",
                "bow_relevance": ["nearly_straight", "relevant"],
                "target_observed": ["no", "yes"],
                "final_orientation_quality": ["bad", "acceptable", "good"],
                "angle_vector": ["sin_2e", "cos_2e"],
            },
        ),
        "confidence_calibration": _write_artifact(
            tmp_path,
            "confidence-calibration.json",
            {
                "schema_version": "temporal-confidence-calibration.v1",
                "valid": True,
                "bins": [
                    {
                        "raw_lower": 0.0,
                        "raw_upper": 1.0,
                        "calibrated_confidence": 0.8,
                    }
                ],
            },
        ),
    }
    contract = {
        "schema_version": "temporal-training-contract.v1",
        "schema_digest": "feature-schema-digest",
        "snapshot_id": "snapshot-1",
        "task_type": "temporal_sequence",
        "source_feature_schemas": ["stem.geometry.v1"],
        "rotation_schemas": ["stem.surface-rotation.v1"],
        "calibration_schemas": ["1.0.0"],
        "feature_set_id": "test.features.v1",
        "feature_specs": [
            {
                "name": "curvature",
                "path": "geometry.signed_curvature",
                "kind": "scalar",
                "length": 1,
                "validity_path": None,
                "divisor_path": None,
                "scale": True,
                "required": True,
            },
            {
                "name": "motion_speed",
                "path": "tracking.movement.speed",
                "kind": "scalar",
                "length": 1,
                "validity_path": None,
                "divisor_path": None,
                "scale": True,
                "required": False,
            },
        ],
        "feature_order": [
            "curvature",
            "motion_speed",
            "curvature__valid",
            "motion_speed__valid",
            "__sequence_present",
        ],
        "raw_feature_count": 2,
        "model_feature_count": 5,
        "minimum_window_steps": 2,
        "maximum_window_steps": 4,
        "padding": {"direction": "left", "value": 0.0},
        "sequence_presence_feature": "__sequence_present",
        "causal": True,
        "input_end": "decision_frame_or_earlier",
        "post_decision_frames_allowed": False,
        "scaler_sha256": descriptors["scaler"]["sha256"],
        "rotation_viewpoint_convention": "viewed_from_log_end_a",
        "positive_rotation_direction": "clockwise_viewed_from_log_end_a",
    }
    descriptors["temporal_contract"] = _write_artifact(
        tmp_path,
        "temporal-contract.json",
        contract,
    )
    return ReleaseContext(
        deployment_release_id="rel-temporal",
        target_path=TargetPath.REALTIME,
        active=False,
        rollback_candidate=False,
        release_status=DeploymentReleaseStatus.ACTIVATED,
        model_family_id="mfy-temporal",
        model_family_slug="temporal-angle",
        model_version_id="mvr-temporal",
        model_version_no=1,
        task_type=TaskType.TEMPORAL_SEQUENCE,
        architecture=ModelArchitectureSummary(
            task_type=TaskType.TEMPORAL_SEQUENCE,
            family="tcn",
            version="v1",
            size="small",
            display_name="Causal TCN Small",
            internal_key="temporal_sequence:tcn:v1:small",
        ),
        triton_model_name="rt__temporal_angle_v1",
        triton_model_version=1,
        contract_major=1,
        manifest={
            "package_root": str(tmp_path),
            "temporal": {"runtime_contract_implemented": True, "shadow_only": True},
            "source_manifests": descriptors,
            "io_schema": {
                "input_tensors": [{"name": "features", "dims": [4, 5]}],
                "output_tensors": [],
            },
        },
        class_labels=[],
        confidence_threshold_default=0.5,
        input_schema_ref="schema://runtime/temporal-sequence.v1",
        output_schema_ref="schema://runtime/temporal-angle.v1",
    )


def _request() -> RealtimeTemporalSequenceRequest:
    return RealtimeTemporalSequenceRequest(
        deployment_target="realtime",
        deployment_release_id="rel-temporal",
        sequence_id="sequence-1",
        track_id="track-1",
        feature_set_id="test.features.v1",
        feature_schema_version="stem.geometry.v1",
        rotation_schema_version="stem.surface-rotation.v1",
        calibration_schema_version="1.0.0",
        rotation_calibration_id="rotation-cal-1",
        rotation_calibration_hash="rotation-cal-hash",
        rotation_viewpoint_convention="viewed_from_log_end_a",
        positive_rotation_direction="clockwise_viewed_from_log_end_a",
        frames=[
            TemporalSequenceFrame(
                frame_index=7,
                timestamp_seconds=1.0,
                values={"geometry": {"signed_curvature": 3.0}, "tracking": {}},
            ),
            TemporalSequenceFrame(
                frame_index=8,
                timestamp_seconds=1.1,
                values={
                    "geometry": {"signed_curvature": 5.0},
                    "tracking": {"movement": {"speed": 20.0}},
                },
            ),
        ],
        current_frame_index=8,
        decision_frame_index=8,
    )


def test_temporal_runtime_preprocessing_matches_training_scaler_and_left_padding(
    tmp_path: Path,
) -> None:
    request = _request()
    tensor, _contracts = _prepare_temporal_model_tensor(
        route=_temporal_route(tmp_path), payload=request
    )

    training_specs = (
        TemporalFeatureSpec(
            name="curvature",
            path="geometry.signed_curvature",
            required=True,
        ),
        TemporalFeatureSpec(
            name="motion_speed",
            path="tracking.movement.speed",
        ),
    )
    training_rows: list[list[float]] = []
    for frame in request.frames:
        root = dict(frame.values)
        values: list[float] = []
        valid: list[bool] = []
        for spec in training_specs:
            extracted, present = _extract_feature(root, spec)
            values.extend(extracted)
            valid.extend(present)
        training_rows.append(
            [
                ((value - [1.0, 10.0][index]) / [2.0, 5.0][index])
                if valid[index]
                else 0.0
                for index, value in enumerate(values)
            ]
            + [1.0 if present else 0.0 for present in valid]
            + [1.0]
        )
    training_tensor = np.asarray(
        [[[0.0] * 5] * 2 + training_rows],
        dtype=np.float32,
    ).reshape(1, 4, 5)

    assert tensor.dtype == np.float32
    assert tensor.shape == (1, 4, 5)
    np.testing.assert_allclose(tensor, training_tensor)


def test_temporal_request_rejects_future_frames_after_decision() -> None:
    payload = _request().model_dump(mode="json")
    payload["decision_frame_index"] = 7

    with pytest.raises(ValidationError, match="after the decision frame"):
        RealtimeTemporalSequenceRequest.model_validate(payload)


@pytest.mark.parametrize("expected_angle", [89.0, -89.0])
def test_runtime_angle_decode_matches_training_modulo_half_turn(expected_angle: float) -> None:
    radians = math.radians(2.0 * expected_angle)
    outputs = {
        "output__bow_relevance_probability": _OutputTensor(
            name="output__bow_relevance_probability", shape=[1], values=[0.9]
        ),
        "output__target_observed_probability": _OutputTensor(
            name="output__target_observed_probability", shape=[1], values=[0.9]
        ),
        "output__angle_sin_2e": _OutputTensor(
            name="output__angle_sin_2e", shape=[1], values=[math.sin(radians)]
        ),
        "output__angle_cos_2e": _OutputTensor(
            name="output__angle_cos_2e", shape=[1], values=[math.cos(radians)]
        ),
        "output__angle_confidence_raw": _OutputTensor(
            name="output__angle_confidence_raw", shape=[1], values=[0.7]
        ),
        "output__final_quality_ordinal_logits": _OutputTensor(
            name="output__final_quality_ordinal_logits", shape=[1, 2], values=[1000.0, -1000.0]
        ),
    }

    result = _temporal_prediction_from_outputs(
        outputs,
        contracts={
            "confidence_calibration": {
                "valid": True,
                "bins": [
                    {
                        "raw_lower": 0.0,
                        "raw_upper": 1.0,
                        "calibrated_confidence": 0.8,
                    }
                ],
            }
        },
    )

    assert result["signed_correction_deg"] == pytest.approx(expected_angle, abs=1e-6)
    assert result["angle_confidence"] == pytest.approx(0.8)
    assert sum(result["final_quality_probabilities"].values()) == pytest.approx(1.0)


def test_temporal_runtime_rejects_changed_packaged_scaler_contract(tmp_path: Path) -> None:
    route = _temporal_route(tmp_path)
    route.manifest["source_manifests"]["scaler"]["sha256"] = "0" * 64

    with pytest.raises(PlatformError, match="changed after packaging") as caught:
        _prepare_temporal_model_tensor(route=route, payload=_request())

    assert caught.value.code == "temporal_artifact_checksum_mismatch"


def test_temporal_confidence_calibration_matches_training_for_empty_bin_gap() -> None:
    outputs = {
        "output__bow_relevance_probability": _OutputTensor(
            name="output__bow_relevance_probability", shape=[1], values=[0.9]
        ),
        "output__target_observed_probability": _OutputTensor(
            name="output__target_observed_probability", shape=[1], values=[0.9]
        ),
        "output__angle_sin_2e": _OutputTensor(
            name="output__angle_sin_2e", shape=[1], values=[0.0]
        ),
        "output__angle_cos_2e": _OutputTensor(
            name="output__angle_cos_2e", shape=[1], values=[1.0]
        ),
        "output__angle_confidence_raw": _OutputTensor(
            name="output__angle_confidence_raw", shape=[1], values=[0.4]
        ),
    }

    result = _temporal_prediction_from_outputs(
        outputs,
        contracts={
            "confidence_calibration": {
                "valid": True,
                "bins": [
                    {
                        "raw_lower": 0.0,
                        "raw_upper": 0.2,
                        "calibrated_confidence": 0.25,
                    },
                    {
                        "raw_lower": 0.8,
                        "raw_upper": 1.0,
                        "calibrated_confidence": 0.9,
                    },
                ],
            }
        },
    )

    # Training resolves an empty calibration-bin gap by nearest bin centre.
    assert result["angle_confidence"] == pytest.approx(0.25)
