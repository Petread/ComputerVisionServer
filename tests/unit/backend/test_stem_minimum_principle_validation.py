from __future__ import annotations

import pytest
from cv_contracts.runtime import (
    RuntimeStemMinimumPrincipleValidationCalculateRequest,
    RuntimeStemMinimumPrincipleValidationObservation,
    RuntimeStemMinimumPrincipleValidationThresholds,
)
from cv_runtime.stem_minimum_principle_validation import (
    calculate_stem_minimum_principle_validation,
    stem_minimum_principle_validation_hash,
    validate_stem_minimum_principle_validation_artifact,
)
from pydantic import ValidationError


def _thresholds() -> RuntimeStemMinimumPrincipleValidationThresholds:
    return RuntimeStemMinimumPrincipleValidationThresholds(
        minimum_total_observations=6,
        minimum_observations_per_setup=2,
        minimum_valid_angle_observations=6,
        maximum_median_angle_error_deg=2,
        maximum_p95_angle_error_deg=3,
        maximum_sign_error_count=0,
        maximum_target_phase_repeatability_deg=2,
        maximum_homography_error_mm=2,
        maximum_straight_curvature_mm=0.5,
        maximum_translation_phase_error_deg=2,
        maximum_curvature_abs_error_mm=1,
        maximum_curvature_repeatability_mm=1,
        minimum_relock_success_rate=0.8,
        maximum_uncertainty_rate=0.1,
    )


def _observation(
    setup: str,
    index: int,
) -> RuntimeStemMinimumPrincipleValidationObservation:
    common = {
        "observation_id": f"objective-{index}",
        "reference_setup": setup,
        "run_id": f"run-{index}",
        "actual_marking": f"ring-{index}",
        "known_angle_deg": float(index * 20),
        "calculated_phase_deg": float(index * 20) + 0.5,
        "observed_phase_span_deg": 45.0 + index * 20,
        "diameter_mm": 260.0 + index * 25,
        "diameter_class": "small" if index <= 3 else "large",
        "image_center_x_px": 80.0 + index * 70,
        "image_center_y_px": 60.0 + index * 45,
        "known_x_mm": float(index * 100),
        "known_y_mm": 20.0,
        "measured_x_mm": float(index * 100) + 0.4,
        "measured_y_mm": 20.3,
        "translation_distance_mm": 100.0,
        "relock_attempted": True,
        "relock_succeeded": True,
        "confidence": 0.95,
        "reason_codes": [],
    }
    if setup == "STRAIGHT_REFERENCE_CYLINDER":
        return RuntimeStemMinimumPrincipleValidationObservation(
            **common,
            reference_body_id="straight-certified-1",
            decision="NO_ROTATION",
            expected_curvature_min_mm=0.0,
            measured_curvature_min_mm=0.1 + 0.02 * index,
            expected_curvature_max_mm=0.0,
            measured_curvature_max_mm=0.2 + 0.02 * index,
        )
    if setup == "CURVED_REFERENCE_BODY":
        return RuntimeStemMinimumPrincipleValidationObservation(
            **common,
            reference_body_id="curved-certified-1",
            expected_target_phase_deg=30.0,
            calculated_target_phase_deg=29.5 + 0.5 * (index % 2),
            expected_correction_deg=20.0,
            calculated_correction_deg=20.5,
            decision="ROTATE",
            expected_curvature_min_mm=5.0,
            measured_curvature_min_mm=5.1 + 0.05 * (index % 2),
            expected_curvature_max_mm=15.0,
            measured_curvature_max_mm=14.8 + 0.05 * (index % 2),
        )
    return RuntimeStemMinimumPrincipleValidationObservation(
        **common,
        reference_body_id="marked-stem-1",
        expected_target_phase_deg=50.0,
        calculated_target_phase_deg=49.5 + 0.5 * (index % 2),
        expected_correction_deg=-15.0,
        calculated_correction_deg=-14.5,
        decision="ROTATE",
        expected_curvature_min_mm=6.0,
        measured_curvature_min_mm=6.1 + 0.05 * (index % 2),
        expected_curvature_max_mm=14.0,
        measured_curvature_max_mm=13.8 + 0.05 * (index % 2),
    )


def _request() -> RuntimeStemMinimumPrincipleValidationCalculateRequest:
    setups = [
        "STRAIGHT_REFERENCE_CYLINDER",
        "STRAIGHT_REFERENCE_CYLINDER",
        "CURVED_REFERENCE_BODY",
        "CURVED_REFERENCE_BODY",
        "MARKED_TEST_STEM",
        "MARKED_TEST_STEM",
    ]
    return RuntimeStemMinimumPrincipleValidationCalculateRequest(
        artifact_id="minimum-principle-acceptance",
        artifact_version="1",
        image_width=640,
        image_height=480,
        process_calibration_id="process-calibration-1",
        process_calibration_hash="a" * 64,
        phase_calibration_id="phase-calibration-1",
        phase_calibration_version="3",
        phase_calibration_hash="b" * 64,
        thresholds_confirmed=True,
        thresholds=_thresholds(),
        observations=[
            _observation(setup, index)
            for index, setup in enumerate(setups, start=1)
        ],
        reference_asset_ids=["straight-cert", "curved-cert", "marked-stem-batch"],
        notes="Objective commissioning sequence",
    )


def test_objective_reference_runs_produce_reproducible_passed_report() -> None:
    computation = calculate_stem_minimum_principle_validation(
        source_channel_id="chn_stem",
        request=_request(),
        operator_user_id="usr_test",
        generated_at="2026-08-05T12:00:00Z",
    )
    artifact = computation.artifact
    restored = type(artifact).model_validate_json(artifact.model_dump_json())

    assert computation.status == "VALIDATION_PASSED"
    assert all(item.status == "PASS" for item in artifact.checks)
    assert artifact.metrics.total_observation_count == 6
    assert artifact.metrics.valid_angle_observation_count == 6
    assert artifact.metrics.median_angle_error_deg == pytest.approx(0.5)
    assert artifact.metrics.p90_angle_error_deg == pytest.approx(0.5)
    assert artifact.metrics.p95_angle_error_deg == pytest.approx(0.5)
    assert artifact.metrics.sign_error_count == 0
    assert artifact.metrics.relock_success_rate == pytest.approx(1.0)
    assert artifact.metrics.uncertainty_rate == pytest.approx(0.0)
    assert {item.group_key for item in artifact.metrics.by_phase_span} == {
        "phase_span_60_to_120_deg",
        "phase_span_ge_120_deg",
    }
    assert {item.group_key for item in artifact.metrics.by_diameter} == {
        "large",
        "small",
    }
    assert "Error by image position" in artifact.report_markdown
    assert "no target frame" in artifact.report_markdown
    assert artifact.report_html.startswith("<!doctype html>")
    assert stem_minimum_principle_validation_hash(artifact) == artifact.artifact_hash
    assert validate_stem_minimum_principle_validation_artifact(restored) == restored


def test_sign_error_fails_objective_acceptance() -> None:
    request = _request()
    observations = list(request.observations)
    observations[2] = observations[2].model_copy(
        update={"calculated_correction_deg": -20.0}
    )

    computation = calculate_stem_minimum_principle_validation(
        source_channel_id="chn_stem",
        request=request.model_copy(update={"observations": observations}),
        generated_at="2026-08-05T12:00:00Z",
    )

    assert computation.status == "VALIDATION_FAILED"
    assert computation.artifact.metrics.sign_error_count == 1
    assert next(
        item for item in computation.artifact.checks if item.check_id == "sign_errors"
    ).status == "FAIL"
    assert "VALIDATION_THRESHOLD_SIGN_ERRORS" in computation.artifact.reason_codes


def test_missing_reference_evidence_fails_closed_as_insufficient_data() -> None:
    request = _request().model_copy(update={"observations": [_observation("MARKED_TEST_STEM", 1)]})

    computation = calculate_stem_minimum_principle_validation(
        source_channel_id="chn_stem",
        request=request,
        generated_at="2026-08-05T12:00:00Z",
    )

    assert computation.status == "INSUFFICIENT_DATA"
    assert any(item.status == "INSUFFICIENT_DATA" for item in computation.artifact.checks)
    assert computation.artifact.reason_codes


def test_incomplete_curved_body_runs_cannot_be_hidden_by_other_measurements() -> None:
    request = _request()
    observations = list(request.observations)
    observations[2] = observations[2].model_copy(
        update={
            "expected_target_phase_deg": None,
            "calculated_target_phase_deg": None,
            "expected_correction_deg": None,
            "calculated_correction_deg": None,
            "decision": "UNCERTAIN",
        }
    )

    computation = calculate_stem_minimum_principle_validation(
        source_channel_id="chn_stem",
        request=request.model_copy(update={"observations": observations}),
        generated_at="2026-08-05T12:00:00Z",
    )

    assert computation.status == "INSUFFICIENT_DATA"
    evidence_check = next(
        item
        for item in computation.artifact.checks
        if item.check_id == "complete_curved_reference_evidence"
    )
    assert evidence_check.status == "INSUFFICIENT_DATA"


def test_tampered_artifact_hash_and_uncertain_angle_are_rejected() -> None:
    artifact = calculate_stem_minimum_principle_validation(
        source_channel_id="chn_stem",
        request=_request(),
        generated_at="2026-08-05T12:00:00Z",
    ).artifact

    with pytest.raises(ValueError, match="hash does not match"):
        validate_stem_minimum_principle_validation_artifact(
            artifact.model_copy(update={"report_markdown": "tampered"})
        )
    with pytest.raises(ValidationError, match="must not publish a correction angle"):
        RuntimeStemMinimumPrincipleValidationObservation(
            **{
                **_observation("CURVED_REFERENCE_BODY", 7).model_dump(),
                "decision": "UNCERTAIN",
            }
        )
