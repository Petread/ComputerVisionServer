from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from realtime_executor.sources import (
    RtspSourceConfigurationError,
    _basler_effective_camera_profile,
    _configure_basler_camera,
)


@pytest.mark.parametrize("node_name", ["ExposureAuto", "GainAuto"])
def test_failed_auto_disable_is_reported_before_manual_value_is_written(node_name: str) -> None:
    manual = Mock()
    camera = SimpleNamespace(
        ExposureAuto=Mock(),
        GainAuto=Mock(),
        ExposureTime=manual,
        Gain=manual,
    )
    getattr(camera, node_name).SetValue.side_effect = RuntimeError("camera node is not writable")
    config = {"exposure_time_us": 2500} if node_name == "ExposureAuto" else {"gain_db": 2}
    with pytest.raises(RtspSourceConfigurationError, match=node_name):
        _configure_basler_camera(camera, SimpleNamespace(), config)
    manual.SetValue.assert_not_called()


def test_auto_settings_apply_without_manual_values() -> None:
    camera = SimpleNamespace(ExposureAuto=Mock(), GainAuto=Mock())
    _configure_basler_camera(
        camera,
        SimpleNamespace(),
        {"exposure_auto": "Off", "gain_auto": "Continuous"},
    )
    camera.ExposureAuto.SetValue.assert_called_once_with("Off")
    camera.GainAuto.SetValue.assert_called_once_with("Continuous")


def test_legacy_exposure_node_and_camera_without_auto_support() -> None:
    camera = SimpleNamespace(ExposureTimeAbs=Mock())
    _configure_basler_camera(camera, SimpleNamespace(), {"exposure_time_us": 2500})
    camera.ExposureTimeAbs.SetValue.assert_called_once_with(2500.0)


def test_ace_classic_aca1300_gain_profile_uses_raw_gain_node() -> None:
    camera = SimpleNamespace(GainSelector=Mock(), GainRaw=Mock())
    _configure_basler_camera(
        camera,
        SimpleNamespace(),
        {"model": "acA1300-60gm", "gain_db": 6},
    )
    camera.GainSelector.SetValue.assert_called_once_with("AnalogAll")
    camera.GainRaw.SetValue.assert_called_once_with(2)


def test_ace_classic_aca1300_replaces_inherited_bayer_default_with_mono8() -> None:
    camera = SimpleNamespace(PixelFormat=Mock())
    _configure_basler_camera(
        camera,
        SimpleNamespace(),
        {"model": "acA1300-60gm", "pixel_format": "BayerRG8"},
    )
    camera.PixelFormat.SetValue.assert_called_once_with("Mono8")


def test_basler_trims_odd_effective_roi_for_nvenc() -> None:
    width = SimpleNamespace(Value=1281, SetValue=Mock())
    height = SimpleNamespace(Value=1080, SetValue=Mock())
    _configure_basler_camera(
        SimpleNamespace(Width=width, Height=height),
        SimpleNamespace(),
        {},
    )
    width.SetValue.assert_called_once_with(1280)
    height.SetValue.assert_not_called()


def test_explicit_basler_raw_gain_override_does_not_require_camera_profile() -> None:
    camera = SimpleNamespace(GainRaw=Mock())
    _configure_basler_camera(camera, SimpleNamespace(), {"gain_raw": 47})
    camera.GainRaw.SetValue.assert_called_once_with(47)


def test_effective_profile_reports_camera_values_including_feature_overrides() -> None:
    camera = SimpleNamespace(
        ExposureTimeAbs=SimpleNamespace(Value=1234),
        ExposureAuto=SimpleNamespace(Value="Continuous"),
        Gain=SimpleNamespace(Value=0),
        GainAuto=SimpleNamespace(Value="Off"),
    )
    profile = _basler_effective_camera_profile(camera, {"exposure_time_us": 2500})
    assert profile["exposure_time_us"] == 1234
    assert profile["exposure_auto"] == "Continuous"
    assert profile["gain_db"] == 0
    assert profile["gain_auto"] == "Off"
