from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cv_core.app import _request_body_limit_bytes, _request_timeout_seconds
from tests.conftest import build_test_settings


def _request(path: str, method: str = "POST") -> SimpleNamespace:
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


def test_camera_calibration_uses_dedicated_large_request_limit(tmp_path: Path) -> None:
    settings = build_test_settings("runtime-admin-api", tmp_path)
    request = _request(
        "/api/v1/runtime/channels/chn_camera/camera-calibration/calculate"
    )

    assert _request_body_limit_bytes(settings, request) == (
        settings.request_limits.camera_calibration_body_limit_bytes
    )
    assert _request_body_limit_bytes(settings, request) > (
        settings.request_limits.default_body_limit_bytes
    )
    assert _request_timeout_seconds(settings, request) == float(
        settings.request_limits.upload_timeout_seconds
    )


def test_unrelated_runtime_json_keeps_the_default_request_limit(tmp_path: Path) -> None:
    settings = build_test_settings("runtime-admin-api", tmp_path)

    assert _request_body_limit_bytes(
        settings,
        _request("/api/v1/runtime/pipelines/validate"),
    ) == settings.request_limits.default_body_limit_bytes
