from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cv_runtime.stem_rotation_validation import (
    STEM_ROTATION_DIAGNOSTIC_PACKAGE_SCHEMA,
    STEM_ROTATION_MONTE_CARLO_SCHEMA,
    STEM_ROTATION_PARAMETER_SWEEP_SCHEMA,
    SyntheticStemCase,
    SyntheticStemConfig,
    build_diagnostic_package,
    compare_optimization_results,
    export_diagnostic_package,
    generate_synthetic_stem,
    load_validation_document,
    monte_carlo_robustness,
    parameter_sweep,
    replay_validation_input,
    run_runtime_optimization,
    synthetic_validation_input,
)

REFERENCE_CASES_PATH = Path("tests/fixtures/stem_rotation_validation/reference_cases.v1.json")
PROJECTION_REPLAY_PATH = Path("tests/fixtures/synthetic/stem-projection-observations.v1.json")


def _reference_document() -> dict[str, Any]:
    document = json.loads(REFERENCE_CASES_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


REFERENCE_CASES = _reference_document()["cases"]


def _synthetic_config(values: dict[str, Any]) -> SyntheticStemConfig:
    document = dict(values)
    document["case"] = SyntheticStemCase(document["case"])
    return SyntheticStemConfig(**document)


@pytest.mark.parametrize(
    "case",
    tuple(SyntheticStemCase),
    ids=lambda case: case.value,
)
def test_synthetic_generators_are_seeded_and_use_canonical_units(
    case: SyntheticStemCase,
) -> None:
    config = SyntheticStemConfig(
        case=case,
        seed=81,
        length_mm=600.0,
        slice_spacing_mm=50.0,
        diameter_start_mm=240.0,
        diameter_end_mm=210.0,
        ellipse_angle_variation_deg=40.0,
        curvature_direction_variation_deg=60.0,
        center_noise_std_mm=0.5,
        axis_noise_std_mm=0.5,
        angle_noise_std_deg=0.5,
    )

    first = generate_synthetic_stem(config)
    second = generate_synthetic_stem(config)

    assert first == second
    assert first.slices[0].x_mm == 0.0
    assert first.slices[-1].x_mm == 600.0
    assert all(item.a_mm >= item.b_mm > 0 for item in first.slices)
    assert all(0.0 <= item.quality <= 1.0 for item in first.slices)


def test_generator_models_taper_bottleneck_outliers_and_gaps() -> None:
    taper = generate_synthetic_stem(
        SyntheticStemConfig(
            case=SyntheticStemCase.TAPER,
            length_mm=500.0,
            slice_spacing_mm=50.0,
            diameter_start_mm=260.0,
            diameter_end_mm=190.0,
        )
    )
    disturbed = generate_synthetic_stem(
        SyntheticStemConfig(
            case=SyntheticStemCase.LOCAL_BOTTLENECK,
            seed=9,
            length_mm=500.0,
            slice_spacing_mm=50.0,
            outlier_count=1,
            short_gap_slices=1,
            long_gap_slices=3,
        )
    )

    assert taper.slices[0].a_mm > taper.slices[-1].a_mm
    assert min(item.a_mm for item in disturbed.slices) < disturbed.slices[0].a_mm
    assert sum(not item.valid for item in disturbed.slices) == 4
    assert sum("SYNTHETIC_OUTLIER" in item.reason_codes for item in disturbed.slices) == 1


@pytest.mark.parametrize(
    "case_document",
    REFERENCE_CASES,
    ids=[case["id"] for case in REFERENCE_CASES],
)
def test_versioned_reference_cases_use_tolerant_expected_ranges(
    case_document: dict[str, Any],
) -> None:
    collection = _reference_document()
    source = synthetic_validation_input(
        _synthetic_config(case_document["synthetic"]),
        priority_mode=case_document["priority_mode"],
        optimizer_config=collection["optimizer_config"],
    )

    result = run_runtime_optimization(
        source.model_payload,
        source.priority_control,
        optimizer_config=source.optimizer_config,
    )
    expected = case_document["expected"]

    assert result.result_valid is expected["result_valid"]
    assert result.should_rotate is expected["should_rotate"]
    _assert_range(result.delta_angle_deg, expected["delta_angle_deg"])
    _assert_range(result.gain_mm, expected["gain_mm"])
    _assert_range(result.limiting_x_current_mm, expected["limiting_x_current_mm"])
    _assert_range(result.limiting_x_optimal_mm, expected["limiting_x_optimal_mm"])


def test_production_observation_replay_reaches_optimizer_and_hmi_view_model() -> None:
    document = json.loads(PROJECTION_REPLAY_PATH.read_text(encoding="utf-8"))
    source = load_validation_document(document)

    result = run_runtime_optimization(
        source.model_payload,
        source.priority_control,
        optimizer_config=source.optimizer_config,
    )

    assert source.source_kind == "production_projection_observations"
    assert source.model_payload.algorithm_version.startswith("stem-slice-measurement")
    assert result.result_ready is True
    assert result.result_valid is True
    assert result.view_model is not None
    assert result.view_model.priority_mode == result.priority_mode
    assert result.view_model.delta_geometry_by_x


def test_replay_is_deterministic_and_compares_original_result() -> None:
    source = synthetic_validation_input(
        SyntheticStemConfig(
            case=SyntheticStemCase.CURVED_ELLIPSE,
            length_mm=500.0,
            slice_spacing_mm=100.0,
        )
    )
    original = run_runtime_optimization(
        source.model_payload,
        source.priority_control,
        optimizer_config=source.optimizer_config,
    )
    replay_source = load_validation_document(original.model_dump(mode="json"))

    first = replay_validation_input(replay_source)
    second = replay_validation_input(replay_source)

    assert first == second
    assert first["matches_persisted_decision"] is True
    assert first["difference"]["delta_angle_deg_difference"] == pytest.approx(0.0)


def test_missing_original_number_cannot_match_a_numeric_replay_value() -> None:
    source = synthetic_validation_input(
        SyntheticStemConfig(
            case=SyntheticStemCase.CURVED_ELLIPSE,
            length_mm=500.0,
            slice_spacing_mm=100.0,
        )
    )
    result = run_runtime_optimization(
        source.model_payload,
        source.priority_control,
        optimizer_config=source.optimizer_config,
    )

    comparison = compare_optimization_results(
        {"result_valid": True, "should_rotate": result.should_rotate},
        result,
    )

    assert comparison["decision_matches"] is False


def test_replay_warns_about_software_and_trial_configuration_changes() -> None:
    source = synthetic_validation_input(
        SyntheticStemConfig(
            case=SyntheticStemCase.CURVED_ELLIPSE,
            length_mm=500.0,
            slice_spacing_mm=100.0,
        )
    )
    original = run_runtime_optimization(
        source.model_payload,
        source.priority_control,
        optimizer_config=source.optimizer_config,
    ).model_dump(mode="json")
    original["algorithm_version"] = "historic-optimizer-version"

    report = replay_validation_input(
        load_validation_document(original),
        config_overrides={"min_gain_mm": 4.0},
    )

    assert any(
        warning.startswith("SOFTWARE_VERSION_MISMATCH:algorithm_version")
        for warning in report["warnings"]
    )
    assert "CONFIGURATION_CHANGED_FOR_REPLAY" in report["warnings"]
    assert "CONFIGURATION_HASH_MISMATCH" in report["warnings"]
    assert report["difference"]["original_available"] is True


def test_monte_carlo_report_is_seeded_and_reports_decision_stability() -> None:
    source = synthetic_validation_input(
        SyntheticStemConfig(
            case=SyntheticStemCase.CURVED_ELLIPSE,
            length_mm=500.0,
            slice_spacing_mm=100.0,
        )
    )
    arguments = {
        "iterations": 4,
        "seed": 27,
        "center_noise_std_mm": 0.5,
        "axis_noise_std_mm": 0.8,
        "angle_noise_std_deg": 0.5,
    }

    first = monte_carlo_robustness(source, **arguments)
    second = monte_carlo_robustness(source, **arguments)

    assert first == second
    assert first["schema_version"] == STEM_ROTATION_MONTE_CARLO_SCHEMA
    assert first["valid_result_count"] == 4
    assert first["delta_angle_deg"]["count"] == 4
    assert 0.0 <= first["limiting_slice_switch_ratio"] <= 1.0


def test_parameter_sweep_reuses_product_preprocessing_and_optimizer() -> None:
    source = synthetic_validation_input(
        SyntheticStemConfig(
            case=SyntheticStemCase.CURVED_ELLIPSE,
            length_mm=500.0,
            slice_spacing_mm=50.0,
        )
    )

    report = parameter_sweep(
        source,
        {
            "slice_spacing_mm": [100.0],
            "score_tolerance_mm": [0.5, 1.5],
        },
    )

    assert report["schema_version"] == STEM_ROTATION_PARAMETER_SWEEP_SCHEMA
    assert len(report["rows"]) == 4
    assert report["rows"][0]["label"] == "baseline"
    assert any(row["preprocessing_replayed"] for row in report["rows"])
    assert all(row["result_valid"] for row in report["rows"])


def test_diagnostic_export_contains_production_result_hmi_and_csv(
    tmp_path: Path,
) -> None:
    source = synthetic_validation_input(
        SyntheticStemConfig(
            case=SyntheticStemCase.CENTERED_ELLIPSE,
            length_mm=500.0,
            slice_spacing_mm=100.0,
        )
    )
    result = run_runtime_optimization(
        source.model_payload,
        source.priority_control,
        optimizer_config=source.optimizer_config,
    )
    package = build_diagnostic_package(result, source_kind=source.source_kind)

    paths = export_diagnostic_package(package, tmp_path)
    persisted = json.loads(paths[0].read_text(encoding="utf-8"))

    assert package["schema_version"] == STEM_ROTATION_DIAGNOSTIC_PACKAGE_SCHEMA
    assert persisted["result"] == result.model_dump(mode="json")
    assert persisted["hmi_view_model"] == result.view_model.model_dump(mode="json")
    assert all(path.exists() for path in paths)
    assert "x_mm" in paths[1].read_text(encoding="utf-8").splitlines()[0]
    assert "angle_deg" in paths[2].read_text(encoding="utf-8").splitlines()[0]


def _assert_range(value: float | None, expected: list[float]) -> None:
    assert value is not None
    assert expected[0] <= value <= expected[1]
