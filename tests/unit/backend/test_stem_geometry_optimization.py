from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from dataclasses import asdict

import numpy as np
import pytest
from cv_contracts.runtime import RuntimeStemCurvatureProcessCalibrationProfile
from cv_runtime import stem_horizontal_curvature as curvature
from cv_runtime.frame_analysis_cache import FrameAnalysisCache, immutable_roi_mask
from tests.unit.backend.test_stem_horizontal_curvature import _profile, _stem_mask


@pytest.mark.parametrize("fast", [False, True])
def test_batched_geometry_quantiles_preserve_measurements_and_quality(
    fast: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cv_runtime.nodes.stem_orientation import _geometry_config
    from cv_runtime.stem_geometry import analyze_stem_geometry

    config = _geometry_config({"analysis_mode": "phase_tracking_fast" if fast else "robust_full"})
    polygon = ((0.1, 0.3), (0.4, 0.2), (0.9, 0.3), (0.9, 0.5), (0.4, 0.4), (0.1, 0.5))
    timings: dict[str, float] = {}
    actual = analyze_stem_geometry(
        polygon, image_width=640, image_height=480, config=config, timings_ms=timings
    )
    original = np.quantile

    def scalar_quantiles(
        a: np.ndarray,
        q: float | list[float],
        *args: object,
        **kwargs: object,
    ) -> np.ndarray | np.float64:
        if isinstance(q, list):
            return np.array([original(a, item, *args, **kwargs) for item in q])
        return original(a, q, *args, **kwargs)

    monkeypatch.setattr(np, "quantile", scalar_quantiles)
    expected = analyze_stem_geometry(polygon, image_width=640, image_height=480, config=config)
    assert asdict(actual) == asdict(expected)
    assert all(timings[key] >= 0 for key in ("support_ms", "axis_ms", "cross_sections_ms"))


def _reference_stations(
    mask: np.ndarray,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    config: curvature.StemHorizontalCurvatureConfig,
) -> list[curvature.HorizontalCurvatureStation]:
    """Pre-optimization definition: nonzero pixels of every full-height station bin."""
    roi = [
        curvature.rectified_process_point_to_pixel(p.x_mm, p.y_mm, profile)
        for p in profile.curvature_observation_roi.process_points
    ]
    low, high = max(min(p[0] for p in roi), 0), min(max(p[0] for p in roi), profile.image_width - 1)
    half_bin = max((high - low) / max(config.station_count - 1, 1) * 0.45, 0.5)
    stations = []
    for center in np.linspace(low, high, config.station_count):
        start, end = (
            max(math.floor(center - half_bin), 0),
            min(math.ceil(center + half_bin) + 1, profile.image_width),
        )
        ys, _xs = np.nonzero(mask[:, start:end])
        x_mm = curvature.rectified_pixel_to_process_point(center, 0, profile)[0]
        y_low = (
            curvature.rectified_pixel_to_process_point(center, float(ys.min()), profile)[1]
            if len(ys)
            else None
        )
        y_high = (
            curvature.rectified_pixel_to_process_point(center, float(ys.max()), profile)[1]
            if len(ys)
            else None
        )
        stations.append(
            curvature.HorizontalCurvatureStation(
                x_mm=x_mm,
                x_normalized=(x_mm - profile.process_bounds.x_min_mm)
                / (profile.process_bounds.x_max_mm - profile.process_bounds.x_min_mm),
                y_center_mm=(y_low + y_high) / 2 if len(ys) else None,
                width_mm=abs(y_high - y_low) if len(ys) else None,
                valid=bool(len(ys)),
                exclusion_reason=None if len(ys) else "station_not_visible",
            )
        )
    return stations


@pytest.mark.parametrize("case", ["straight", "bow", "s_bow", "empty", "fragments", "noise"])
def test_optimized_geometry_preserves_values_and_rejection_reasons(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    mask = _stem_mask(
        profile, bow_mm=40 if case in {"bow", "s_bow"} else 0, s_bow=case == "s_bow", slope=0.06
    )
    if case == "empty":
        mask[:] = 0
    elif case == "fragments":
        mask[:, mask.shape[1] // 2 - 20 : mask.shape[1] // 2 + 20] = 0
    elif case == "noise":
        mask[np.random.default_rng(42).random(mask.shape) > 0.999] = 255
    config = curvature.StemHorizontalCurvatureConfig()
    assert curvature._sample_stations(mask, profile, config) == _reference_stations(
        mask, profile, config
    )
    actual = curvature.measure_stem_horizontal_curvature(mask, profile, config=config)
    monkeypatch.setattr(curvature, "_sample_stations", _reference_stations)
    expected = curvature.measure_stem_horizontal_curvature(mask, profile, config=config)
    assert json.dumps(asdict(actual), sort_keys=True) == json.dumps(
        asdict(expected), sort_keys=True
    )


def test_roi_cache_is_immutable_bounded_and_invalidated_by_version_or_projection() -> None:
    vertices = ((2, 2), (20, 2), (20, 20), (2, 20))
    original = immutable_roi_mask("v1", 32, 32, vertices)
    assert immutable_roi_mask("v1", 32, 32, vertices) is original
    with pytest.raises(ValueError):
        original[0, 0] = 255
    assert immutable_roi_mask("v2", 32, 32, vertices) is not original
    assert immutable_roi_mask("v1", 32, 32, ((3, 3), *vertices[1:])) is not original
    for version in range(16):
        immutable_roi_mask(str(version), 32, 32, vertices)
    assert immutable_roi_mask.cache_info().currsize <= 8


def test_frame_intermediates_are_reused_only_for_same_owner_and_execution() -> None:
    first, second = FrameAnalysisCache(), FrameAnalysisCache()
    owner = object()
    value = first.get(owner, "gray", object)
    assert first.get(owner, "gray", object) is value
    assert first.get(object(), "gray", object) is not value
    assert second.get(owner, "gray", object) is not value


@pytest.mark.parametrize("setting", [None, "1", "2", "0", "invalid"])
def test_executor_native_threads_are_opt_in(setting: str | None) -> None:
    environment = dict(os.environ)
    environment.pop("YBB_REALTIME_EXECUTOR_NATIVE_THREADS", None)
    environment["OPENBLAS_NUM_THREADS"] = "7"
    if setting is not None:
        environment["YBB_REALTIME_EXECUTOR_NATIVE_THREADS"] = setting
    # Separate processes are essential: native pools are initialized on import.
    script = (
        "import realtime_executor, os, json; import cv2; "
        "print(json.dumps([os.environ['OPENBLAS_NUM_THREADS'], cv2.getNumThreads()]))"
    )
    environment["PYTHONPATH"] = os.pathsep.join(sys.path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if setting in {"0", "invalid"}:
        assert result.returncode != 0
        assert "must be a positive integer" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        blas, opencv = json.loads(result.stdout)
        assert blas == (setting or "7")
        if setting is not None:
            assert opencv == int(setting)
