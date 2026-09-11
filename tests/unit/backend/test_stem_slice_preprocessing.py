from __future__ import annotations

import math

import pytest
from cv_runtime.stem_rotation_types import StemSliceGeometry, StemSliceModel
from cv_runtime.stem_slice_preprocessing import (
    INTERPOLATED_REASON,
    OUTLIER_REASON,
    UNSUPPORTED_GRID_REASON,
    StemSlicePreprocessingConfig,
    preprocess_stem_slice_model,
)
from tests.support.stem_rotation_models import (
    local_bottleneck_stem,
    noisy_stem,
    stem_with_short_gap,
)


def _slice(
    x_mm: float,
    *,
    cy_mm: float = 0.0,
    cz_mm: float = 0.0,
    a_mm: float = 110.0,
    b_mm: float = 90.0,
    phi_deg: float = 0.0,
    quality: float = 1.0,
    valid: bool = True,
) -> StemSliceGeometry:
    return StemSliceGeometry(
        x_mm=x_mm,
        cy_mm=cy_mm,
        cz_mm=cz_mm,
        a_mm=a_mm,
        b_mm=b_mm,
        phi_rad=math.radians(phi_deg),
        quality=quality,
        valid=valid,
    )


def test_regular_grid_interpolates_only_a_short_bounded_gap() -> None:
    result = preprocess_stem_slice_model(
        stem_with_short_gap(),
        config=StemSlicePreprocessingConfig(
            max_interpolation_gap_mm=50.0,
            smoothing_window_slices=1,
        ),
    )

    assert tuple(item.x_mm for item in result.model.slices) == tuple(
        float(value) for value in range(0, 401, 50)
    )
    filled = result.model.slices[4]
    assert filled.valid is True
    assert filled.interpolated is True
    assert filled.cy_mm == pytest.approx(20.0)
    assert filled.cz_mm == pytest.approx(-10.0)
    assert INTERPOLATED_REASON in filled.reason_codes
    assert result.interpolated_slice_count == 1


def test_long_gap_is_not_interpolated_or_extrapolated() -> None:
    model = StemSliceModel(
        slices=(
            _slice(0.0),
            _slice(50.0),
            _slice(250.0),
            _slice(300.0),
        )
    )

    result = preprocess_stem_slice_model(
        model,
        config=StemSlicePreprocessingConfig(
            max_interpolation_gap_mm=50.0,
            smoothing_window_slices=1,
        ),
    )

    middle = [item for item in result.model.slices if 50.0 < item.x_mm < 250.0]
    assert middle
    assert all(item.valid is False for item in middle)
    assert all(UNSUPPORTED_GRID_REASON in item.reason_codes for item in middle)
    assert result.interpolated_slice_count == 0


def test_phi_interpolation_uses_180_degree_periodicity() -> None:
    model = StemSliceModel(
        slices=(
            _slice(0.0, phi_deg=89.0),
            _slice(100.0, phi_deg=-89.0),
        )
    )

    result = preprocess_stem_slice_model(
        model,
        config=StemSlicePreprocessingConfig(
            max_interpolation_gap_mm=50.0,
            smoothing_window_slices=1,
        ),
    )

    middle = result.model.slices[1]
    assert middle.valid is True
    assert abs(abs(math.degrees(middle.phi_rad)) - 90.0) < 1.0e-9


def test_low_quality_multi_parameter_outlier_is_rejected_then_filled() -> None:
    slices = [_slice(float(index * 50)) for index in range(7)]
    slices[3] = _slice(
        150.0,
        cy_mm=80.0,
        cz_mm=-75.0,
        a_mm=145.0,
        b_mm=55.0,
        quality=0.5,
    )

    result = preprocess_stem_slice_model(
        StemSliceModel(slices=tuple(slices)),
        config=StemSlicePreprocessingConfig(
            max_interpolation_gap_mm=50.0,
            smoothing_window_slices=1,
        ),
    )

    replacement = result.model.slices[3]
    assert result.rejected_outlier_count == 1
    assert OUTLIER_REASON in result.reason_codes
    assert replacement.valid is True
    assert replacement.interpolated is True
    assert replacement.cy_mm == pytest.approx(0.0)
    assert replacement.cz_mm == pytest.approx(0.0)


def test_robust_smoothing_reduces_noise_without_invalidating_slices() -> None:
    model = noisy_stem()
    center_before = model.slices[len(model.slices) // 2]

    result = preprocess_stem_slice_model(
        model,
        config=StemSlicePreprocessingConfig(
            smoothing_window_slices=5,
            smoothing_strength=1.0,
        ),
    )
    center_after = result.model.slices[len(result.model.slices) // 2]

    neighborhood = model.slices[4:9]
    expected_cy_median = sorted(item.cy_mm for item in neighborhood)[2]
    assert center_after.cy_mm == pytest.approx(expected_cy_median)
    assert center_after.valid is True
    assert result.valid_output_slice_count == len(model.slices)
    assert center_after != center_before


def test_high_quality_local_bottleneck_is_preserved() -> None:
    model = local_bottleneck_stem()
    bottleneck_index = len(model.slices) // 2

    result = preprocess_stem_slice_model(model)

    bottleneck = result.model.slices[bottleneck_index]
    neighbor = result.model.slices[bottleneck_index - 1]
    assert result.rejected_outlier_count == 0
    assert bottleneck.valid is True
    assert bottleneck.a_mm < neighbor.a_mm
    assert bottleneck.b_mm < neighbor.b_mm


def test_evaluation_range_is_carried_without_reconstructing_hidden_ends() -> None:
    source = StemSliceModel(
        slices=tuple(_slice(float(index * 50)) for index in range(7)),
        x_eval_start_mm=50.0,
        x_eval_end_mm=250.0,
    )

    result = preprocess_stem_slice_model(
        source,
        config=StemSlicePreprocessingConfig(
            smoothing_window_slices=1,
            x_eval_start_mm=100.0,
            x_eval_end_mm=200.0,
        ),
    )

    assert result.model.x_eval_start_mm == 100.0
    assert result.model.x_eval_end_mm == 200.0
    assert result.model.slices[0].x_mm == 0.0
    assert result.model.slices[-1].x_mm == 300.0
