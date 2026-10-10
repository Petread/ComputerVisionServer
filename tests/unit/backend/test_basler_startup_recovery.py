from __future__ import annotations

import sys
import threading
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import camera_media.worker as media
import pytest
import realtime_executor.sources as sources
from cv_db.stream_store import CameraMediaBinding, ImageViewStreamBinding

CAMERA = {
    "serial_number": "41949247",
    "ip_address": "172.23.27.243",
    "model": "a2A1920-51gcBAS",
    "device_class": "BaslerGigE",
}
CONFIG = {"serial_number": CAMERA["serial_number"], "ip_address": CAMERA["ip_address"]}


def factory(
    devices: Iterable[dict[str, str]] = (),
    *,
    announced: dict[str, str] = CAMERA,
    found: bool = True,
) -> SimpleNamespace:
    transport = SimpleNamespace(AnnounceRemoteDevice=Mock(return_value=(found, announced)))
    return SimpleNamespace(
        EnumerateDevices=Mock(return_value=list(devices)),
        CreateTl=Mock(return_value=transport),
        CreateDevice=Mock(side_effect=lambda device: device),
    )


def test_directed_discovery_checks_identity_without_opening_camera() -> None:
    sdk = factory()
    device, info = sources._discover_basler_device(sdk, CONFIG)
    assert device is CAMERA
    assert info["serial_number"] == CONFIG["serial_number"]
    sdk.CreateTl.assert_called_once_with("BaslerGigE")
    sdk.CreateTl.return_value.AnnounceRemoteDevice.assert_called_once_with(CONFIG["ip_address"])
    sdk.CreateDevice.assert_not_called()


def test_general_discovery_keeps_existing_path() -> None:
    sdk = factory([CAMERA])
    assert sources._discover_basler_device(sdk, CONFIG)[0] is CAMERA
    sdk.CreateTl.assert_not_called()


def test_other_enumerated_camera_does_not_prevent_directed_search() -> None:
    sdk = factory([{**CAMERA, "serial_number": "other"}])
    assert sources._discover_basler_device(sdk, CONFIG)[0] is CAMERA


def test_directed_search_after_enumeration_exception() -> None:
    sdk = factory()
    sdk.EnumerateDevices.side_effect = RuntimeError("temporary discovery error")
    assert sources._discover_basler_device(sdk, CONFIG)[0] is CAMERA


def test_wrong_camera_at_ip_is_configuration_error() -> None:
    sdk = factory(announced={**CAMERA, "serial_number": "other"})
    with pytest.raises(sources.RtspSourceConfigurationError, match="serial_number"):
        sources._discover_basler_device(sdk, CONFIG)
    sdk.CreateDevice.assert_not_called()


@pytest.mark.parametrize("address", ["not-an-ip", "0.0.0.0", "255.255.255.255", "224.0.0.1", "::1"])
def test_invalid_directed_address_fails_before_sdk_access(address: str) -> None:
    sdk = factory()
    with pytest.raises(sources.RtspSourceConfigurationError, match="ip_address"):
        sources._discover_basler_device(sdk, {**CONFIG, "ip_address": address})
    sdk.EnumerateDevices.assert_not_called()


@pytest.mark.parametrize("devices", [[], [{**CAMERA, "serial_number": "other"}]])
def test_serial_only_camera_temporarily_absent_is_retryable(devices: list[dict[str, str]]) -> None:
    sdk = factory(devices)
    with pytest.raises(sources.BaslerDeviceUnavailableError):
        sources._discover_basler_device(sdk, {"serial_number": CONFIG["serial_number"]})
    sdk.CreateTl.assert_not_called()


def test_ambiguous_cameras_remain_permanent_configuration_error() -> None:
    sdk = factory([CAMERA, {**CAMERA, "serial_number": "other"}])
    with pytest.raises(sources.RtspSourceConfigurationError, match="Multiple"):
        sources._discover_basler_device(sdk, {})


@pytest.mark.parametrize("failure", [False, RuntimeError("timeout")])
def test_directed_discovery_failure_is_retryable(failure: bool | RuntimeError) -> None:
    sdk = factory(found=False)
    if failure:
        sdk.CreateTl.return_value.AnnounceRemoteDevice.side_effect = failure
    with pytest.raises(sources.BaslerDeviceUnavailableError):
        sources._discover_basler_device(sdk, CONFIG)


def install_sdk(
    monkeypatch: pytest.MonkeyPatch,
    sdk: SimpleNamespace,
    *,
    open_error: Exception | None = None,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    camera = SimpleNamespace(
        Open=Mock(side_effect=open_error),
        IsOpen=Mock(return_value=True),
        Close=Mock(),
        IsGrabbing=Mock(return_value=False),
        StartGrabbing=Mock(),
    )
    pylon = SimpleNamespace(
        TlFactory=SimpleNamespace(GetInstance=lambda: sdk),
        InstantCamera=Mock(return_value=camera),
    )
    monkeypatch.setitem(sys.modules, "pypylon", SimpleNamespace(pylon=pylon))
    monkeypatch.setattr(sources, "_configure_basler_camera", Mock())
    monkeypatch.setattr(sources, "_basler_effective_camera_profile", lambda *a: {})
    monkeypatch.setattr(sources, "_basler_image_converter", lambda *a: None)
    monkeypatch.setattr(sources, "_pylon_grab_strategy", lambda *a: "latest")
    monkeypatch.setattr(sources, "_try_retrieve_pylon_result", lambda *a, **kw: None)
    return camera, pylon


def test_read_retries_with_wait_then_recovers_in_same_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = factory(found=False)
    camera, _ = install_sdk(monkeypatch, sdk)
    source = sources.BaslerPylonFrameSource(config={**CONFIG, "transport_mode": "pylon_direct"})
    stop = SimpleNamespace(is_set=lambda: False, wait=Mock(return_value=False))
    assert source.read(stop) is None
    stop.wait.assert_called_once_with(5.0)
    assert "responded" in source.health.last_error
    assert source.health.reconnect_count == 1
    camera.Open.assert_not_called()
    sdk.CreateTl.return_value.AnnounceRemoteDevice.return_value = (True, CAMERA)
    assert source.read(stop) is None  # Connected; no frame yet.
    assert source.health.connected
    assert source.health.last_error is None
    camera.Open.assert_called_once()
    source.close()
    camera.Close.assert_called_once()


def test_partial_open_failure_closes_handle_before_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = factory([CAMERA])
    camera, _ = install_sdk(monkeypatch, sdk, open_error=RuntimeError("open failed"))
    source = sources.BaslerPylonFrameSource(config={**CONFIG, "transport_mode": "pylon_direct"})
    assert not source._open_camera()
    camera.Close.assert_called_once()
    assert source._camera is None


def test_identity_mismatch_never_opens_camera_even_in_auto_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = factory(announced={**CAMERA, "serial_number": "wrong"})
    camera, pylon = install_sdk(monkeypatch, sdk)
    source = sources.BaslerPylonFrameSource(config=CONFIG)
    with pytest.raises(sources.RtspSourceConfigurationError):
        source.read(threading.Event())
    camera.Open.assert_not_called()
    pylon.InstantCamera.assert_not_called()
    assert source._aravis_source is None


def test_cancelled_read_does_not_reopen_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = factory()
    install_sdk(monkeypatch, sdk)
    stop = threading.Event()
    stop.set()
    source = sources.BaslerPylonFrameSource(config=CONFIG)
    assert source.read(stop) is None
    sdk.EnumerateDevices.assert_not_called()


@pytest.mark.parametrize("exit_on", ["join", "terminate", "kill", "never"])
def test_process_stop_verifies_exit_before_replacement(exit_on: str) -> None:
    class Process:
        pid = 123
        alive = True
        calls = []

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout: float) -> None:
            self.calls.append("join")
            if exit_on == "join":
                self.alive = False

        def terminate(self) -> None:
            self.calls.append("terminate")
            self.alive = exit_on != "terminate"

        def kill(self) -> None:
            self.calls.append("kill")
            self.alive = exit_on != "kill"

        def close(self) -> None:
            assert not self.alive
            self.calls.append("close")

    process = Process()
    stop = threading.Event()
    if exit_on == "never":
        with pytest.raises(RuntimeError, match="could not be stopped"):
            media._stop_process(process, stop)
        assert "close" not in process.calls
    else:
        media._stop_process(process, stop)
        assert process.calls[-1] == "close"
    assert stop.is_set()
    assert ("kill" in process.calls) == (exit_on in {"kill", "never"})


def binding() -> CameraMediaBinding:
    return CameraMediaBinding(
        channel_id="basler",
        channel_slug="basler",
        source_type="basler_pylon",
        source_config=CONFIG,
        source_secret_refs={},
        stream_required=True,
        inference_required=True,
        image_view_stream=ImageViewStreamBinding(
            pipeline_version_id="v1", image_view_node_key="view"
        ),
    )


def test_pipeline_view_change_preserves_capture_and_raw_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = binding()
    new = replace(old, image_view_stream=replace(old.image_view_stream, pipeline_version_id="v2"))
    assert media._same_source_binding(old, new)
    handle = SimpleNamespace(
        binding=old,
        refresh_snapshots=Mock(),
        ensure_capture_process=Mock(),
        _stop_image_view_stream=Mock(),
        stop_publisher=Mock(),
        start_publisher=Mock(),
        stop=Mock(),
    )
    coordinator = media.CameraMediaCoordinator(settings=SimpleNamespace(), instance_id="test")
    coordinator._handles[old.channel_id] = handle
    monkeypatch.setattr(media, "session_scope", lambda *a: nullcontext(None))
    monkeypatch.setattr(media, "list_camera_media_bindings", lambda *a: [new])
    monkeypatch.setattr(media, "load_mediamtx_paths", lambda *a: ({}, None))
    monkeypatch.setattr(coordinator, "_write_channel_state", Mock())
    coordinator._reconcile()
    handle.stop.assert_not_called()
    handle.stop_publisher.assert_not_called()
    handle._stop_image_view_stream.assert_called_once()
    handle.start_publisher.assert_called_once()
    assert handle.binding == new


def test_camera_setting_change_still_requires_capture_restart() -> None:
    old = binding()
    new = replace(old, source_config={**CONFIG, "exposure_time_us": 20000})
    assert not media._same_source_binding(old, new)


def test_unstoppable_removed_handle_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = SimpleNamespace(stop=Mock(side_effect=RuntimeError("still alive")))
    coordinator = media.CameraMediaCoordinator(settings=SimpleNamespace(), instance_id="test")
    coordinator._handles["basler"] = handle
    monkeypatch.setattr(media, "session_scope", lambda *a: nullcontext(None))
    monkeypatch.setattr(media, "list_camera_media_bindings", lambda *a: [])
    monkeypatch.setattr(media, "load_mediamtx_paths", lambda *a: ({}, None))
    with pytest.raises(RuntimeError, match="still alive"):
        coordinator._reconcile()
    assert coordinator._handles["basler"] is handle
