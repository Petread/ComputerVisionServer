from __future__ import annotations

import math

import numpy as np
import pytest
from cv_runtime.stem_rotation_optimizer import (
    StemRotationOptimizationConfig,
    optimize_stem_rotation,
)
from cv_runtime.stem_rotation_types import PriorityMode, RotationConstraints
from cv_runtime.stem_slice_measurement import (
    STEM_PROJECTION_OBSERVATION_SCHEMA,
    StemProjectionObservation,
    StemSliceFitConfig,
    StemSliceModelBuilderConfig,
    StemSliceModelStatus,
    build_stem_slice_model,
    fit_stem_slice,
    replay_projection_observation_document,
)
from cv_runtime.stem_slice_preprocessing import StemSlicePreprocessingConfig


def _observations(
    *,
    x_mm: float = 0.0,
    cy_mm: float = 0.0,
    cz_mm: float = 0.0,
    a_mm: float = 120.0,
    b_mm: float = 80.0,
    phi_deg: float = 25.0,
    phases_deg: tuple[float, ...] = tuple(range(0, 181, 15)),
    reference_phase_deg: float = 0.0,
    projected_center_offset_mm: float = 0.0,
    noise_mm: float = 0.0,
    seed: int = 20260820,
) -> list[StemProjectionObservation]:
    rng = np.random.default_rng(seed)
    phi = math.radians(phi_deg)
    cosine_phi = math.cos(phi)
    sine_phi = math.sin(phi)
    shape = np.asarray(
        [
            [
                a_mm * a_mm * cosine_phi**2 + b_mm * b_mm * sine_phi**2,
                (a_mm * a_mm - b_mm * b_mm) * cosine_phi * sine_phi,
            ],
            [
                (a_mm * a_mm - b_mm * b_mm) * cosine_phi * sine_phi,
                a_mm * a_mm * sine_phi**2 + b_mm * b_mm * cosine_phi**2,
            ],
        ]
    )
    output: list[StemProjectionObservation] = []
    for sequence, phase_deg in enumerate(phases_deg):
        theta = math.radians(phase_deg - reference_phase_deg)
        direction = np.asarray([math.cos(theta), -math.sin(theta)])
        center = (
            projected_center_offset_mm
            + cy_mm * math.cos(theta)
            - cz_mm * math.sin(theta)
        )
        width = 2.0 * math.sqrt(float(direction @ shape @ direction))
        output.append(
            StemProjectionObservation(
                x_mm=x_mm,
                phase_deg=phase_deg,
                projected_center_mm=center + float(rng.normal(0, noise_mm)),
                visible_width_mm=width + float(rng.normal(0, noise_mm)),
                quality=0.95,
                frame_sequence=sequence,
            )
        )
    return output


def _ellipse_angle_distance_deg(first_rad: float, second_deg: float) -> float:
    delta = math.degrees(first_rad) - second_deg
    return abs((delta + 90.0) % 180.0 - 90.0)


def _builder_config(**changes: object) -> StemSliceModelBuilderConfig:
    values: dict[str, object] = {
        "minimum_valid_slice_count": 1,
        "minimum_valid_slice_ratio": 0.5,
        "fit": StemSliceFitConfig(minimum_slice_quality=0.1),
        "preprocessing": StemSlicePreprocessingConfig(
            slice_spacing_mm=50.0,
            smoothing_window_slices=1,
        ),
    }
    values.update(changes)
    return StemSliceModelBuilderConfig(**values)


def test_circle_fit_has_equal_axes_and_deterministic_uninformative_phi() -> None:
    fit = fit_stem_slice(
        _observations(a_mm=100, b_mm=100, phi_deg=67),
        reference_phase_deg=0,
    )

    assert fit.geometry is not None
    assert fit.geometry.a_mm == pytest.approx(100, abs=1.0e-8)
    assert fit.geometry.b_mm == pytest.approx(100, abs=1.0e-8)
    assert fit.geometry.phi_rad == 0
    assert fit.diagnostics.near_circle is True
    assert fit.diagnostics.phi_informative is False


def test_ellipse_support_fit_recovers_axes_and_orientation() -> None:
    fit = fit_stem_slice(
        _observations(a_mm=142, b_mm=83, phi_deg=-31),
        reference_phase_deg=0,
    )

    assert fit.geometry is not None
    assert fit.geometry.a_mm == pytest.approx(142, abs=1.0e-7)
    assert fit.geometry.b_mm == pytest.approx(83, abs=1.0e-7)
    assert _ellipse_angle_distance_deg(fit.geometry.phi_rad, -31) < 1.0e-7
    assert fit.diagnostics.width_residual_rms_mm == pytest.approx(0, abs=1.0e-8)


def test_harmonic_center_fit_recovers_both_transverse_components_at_current_pose() -> None:
    fit = fit_stem_slice(
        _observations(cy_mm=17.5, cz_mm=-26.0, reference_phase_deg=35),
        reference_phase_deg=35,
    )

    assert fit.geometry is not None
    assert fit.geometry.cy_mm == pytest.approx(17.5, abs=1.0e-8)
    assert fit.geometry.cz_mm == pytest.approx(-26, abs=1.0e-8)


def test_harmonic_center_fit_separates_static_projection_offset() -> None:
    fit = fit_stem_slice(
        _observations(
            cy_mm=17.5,
            cz_mm=-26.0,
            phases_deg=(0, 16, 32, 48, 64, 80, 96),
            projected_center_offset_mm=430.0,
        ),
        reference_phase_deg=0,
        config=StemSliceFitConfig(
            minimum_unique_phases=3,
            minimum_phase_span_deg=60,
        ),
    )

    assert fit.geometry is not None
    assert fit.geometry.cy_mm == pytest.approx(17.5, abs=1.0e-7)
    assert fit.geometry.cz_mm == pytest.approx(-26.0, abs=1.0e-7)
    assert fit.diagnostics.center_residual_rms_mm == pytest.approx(0, abs=1.0e-8)


def test_noise_and_single_gross_outlier_are_robustly_downweighted() -> None:
    observations = _observations(
        cy_mm=12,
        cz_mm=-8,
        a_mm=130,
        b_mm=88,
        phi_deg=22,
        noise_mm=0.5,
    )
    observations[4] = StemProjectionObservation(
        **{
            **observations[4].__dict__,
            "projected_center_mm": 90,
            "visible_width_mm": 390,
        }
    )

    fit = fit_stem_slice(observations, reference_phase_deg=0)

    assert fit.geometry is not None
    assert fit.geometry.cy_mm == pytest.approx(12, abs=1.5)
    assert fit.geometry.cz_mm == pytest.approx(-8, abs=1.5)
    assert fit.geometry.a_mm == pytest.approx(130, abs=2.0)
    assert fit.geometry.b_mm == pytest.approx(88, abs=2.0)
    assert fit.diagnostics.center_inlier_weight_ratio < 1
    assert fit.diagnostics.width_inlier_weight_ratio < 1


def test_missing_phase_range_degrades_then_invalidates_support() -> None:
    partial = _observations(phases_deg=(0, 10, 20, 30, 40, 50, 60))
    fit = fit_stem_slice(partial, reference_phase_deg=0)

    assert fit.geometry is None
    assert "PHASE_COVERAGE_INSUFFICIENT" in fit.diagnostics.reason_codes
    assert fit.diagnostics.phase_coverage_ratio < 0.5


def test_multiple_slices_follow_slow_geometry_change_and_short_gap_is_interpolated() -> None:
    observations: list[StemProjectionObservation] = []
    for index in (0, 1, 3, 4):
        observations.extend(
            _observations(
                x_mm=index * 50,
                cy_mm=index * 2,
                cz_mm=-index,
                a_mm=120 + index,
                b_mm=85 + index,
                phi_deg=5 + index,
                seed=10 + index,
            )
        )
    result = build_stem_slice_model(
        observations,
        reference_phase_deg=0,
        config=_builder_config(),
    )

    assert result.status in {StemSliceModelStatus.VALID, StemSliceModelStatus.DEGRADED}
    assert len(result.processed_model.slices) == 5
    gap = result.processed_model.slices[2]
    assert gap.interpolated is True
    assert gap.valid is True
    assert gap.cy_mm == pytest.approx(4, abs=0.1)


def test_long_gap_keeps_model_invalid_and_reports_gap_length() -> None:
    observations = [
        *_observations(x_mm=0),
        *_observations(x_mm=250),
    ]
    result = build_stem_slice_model(
        observations,
        reference_phase_deg=0,
        config=_builder_config(maximum_invalid_gap_mm=100),
    )

    assert result.status is StemSliceModelStatus.INVALID
    assert result.maximum_invalid_gap_mm == 200
    assert "INVALID_GAP_TOO_LONG" in result.reason_codes
    assert any(not item.valid for item in result.processed_model.slices)


def test_replay_document_builds_same_metric_model_without_camera() -> None:
    observations = _observations(x_mm=100, cy_mm=8, cz_mm=13, a_mm=110, b_mm=90)
    document = {
        "schema_version": STEM_PROJECTION_OBSERVATION_SCHEMA,
        "reference_phase_deg": 0,
        "observations": [item.__dict__ for item in observations],
    }

    replay = replay_projection_observation_document(document, config=_builder_config())

    assert replay.processed_model.slices[0].x_mm == 100
    assert replay.processed_model.slices[0].cy_mm == pytest.approx(8, abs=1.0e-7)
    assert replay.processed_model.slices[0].cz_mm == pytest.approx(13, abs=1.0e-7)


def test_built_model_runs_through_real_rotation_optimizer() -> None:
    observations: list[StemProjectionObservation] = []
    for x_mm in (0, 50, 100):
        observations.extend(
            _observations(
                x_mm=x_mm,
                a_mm=135,
                b_mm=75,
                phi_deg=42,
                seed=100 + int(x_mm),
            )
        )
    built = build_stem_slice_model(
        observations,
        reference_phase_deg=0,
        config=_builder_config(),
    )

    result = optimize_stem_rotation(
        built.processed_model,
        PriorityMode.NACHSCHNITT_KRITISCH,
        constraints=RotationConstraints(-90, 90),
        config=StemRotationOptimizationConfig(
            coarse_angle_step_deg=1,
            fine_angle_step_deg=0.1,
            score_tolerance_mm=0.001,
            min_gain_mm=0.1,
            min_rotation_deg=0.1,
        ),
    )

    assert built.status is StemSliceModelStatus.VALID
    assert result.valid is True
    assert result.should_rotate is True
    assert result.delta_angle_deg == pytest.approx(-42, abs=0.2)
    assert result.gain_mm is not None and result.gain_mm > 20
