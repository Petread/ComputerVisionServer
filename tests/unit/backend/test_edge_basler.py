from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from cv_contracts import EdgeCapabilitySnapshot
from cv_db import RuntimeSource
from cv_runtime.distribution import runtime_code_digest
from edge_agent import cameras
from runtime_admin_api.edge_workload_builder import EdgeWorkloadBuildError, _admit, _edge_source


@pytest.fixture()
def pylon_sdk(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    writes = []
    image = np.full((24, 32, 3), 17, dtype=np.uint8)
    state = SimpleNamespace(open=False, grabbing=False, triggered=0, released=0, timeout=False)

    class Node:
        def __init__(self, name: str) -> None:
            self.name = name
            self.Value = None

        def SetValue(self, value: object) -> None:  # noqa: N802
            self.Value = value
            writes.append((self.name, value))
            if self.name == "PixelFormat" and value == "invalid":
                raise RuntimeError("unsupported pixel format")

    def release() -> None:
        state.released += 1
        image[:] = 0

    result = SimpleNamespace(GrabSucceeded=lambda: True, Release=release)
    camera = SimpleNamespace(
        Open=lambda: setattr(state, "open", True),
        Close=lambda: setattr(state, "open", False),
        IsOpen=lambda: state.open,
        IsGrabbing=lambda: state.grabbing,
        StopGrabbing=lambda: setattr(state, "grabbing", False),
        StartGrabbing=lambda strategy: setattr(state, "grabbing", True),
        CanWaitForFrameTriggerReady=lambda: True,
        WaitForFrameTriggerReady=lambda *args: True,
        ExecuteSoftwareTrigger=lambda: setattr(state, "triggered", state.triggered + 1),
        RetrieveResult=lambda *args: None if state.timeout else result,
    )
    for name in (
        "AcquisitionMode",
        "Width",
        "Height",
        "OffsetX",
        "OffsetY",
        "PixelFormat",
        "AcquisitionFrameRateEnable",
        "AcquisitionFrameRateAbs",
        "ExposureTimeAbs",
        "ExposureAuto",
        "Gain",
        "GainRaw",
        "GainAuto",
        "GainSelector",
        "TriggerSelector",
        "TriggerMode",
        "TriggerSource",
        "TriggerActivation",
        "GevSCPSPacketSize",
        "GevSCPD",
        "MaxNumBuffer",
    ):
        setattr(camera, name, Node(name))

    def device(serial: str, ip: str) -> SimpleNamespace:
        return SimpleNamespace(
            GetSerialNumber=lambda: serial,
            GetIpAddress=lambda: ip,
            GetUserDefinedName=lambda: f"camera-{serial}",
        )

    devices = [device("111", "192.168.1.11"), device("222", "192.168.1.12")]
    selected = []
    factory = SimpleNamespace(
        EnumerateDevices=lambda: devices,
        CreateDevice=lambda item: selected.append(item) or item,
    )
    pylon = SimpleNamespace(
        TlFactory=SimpleNamespace(GetInstance=lambda: factory),
        InstantCamera=lambda item: camera,
        ImageFormatConverter=lambda: SimpleNamespace(
            Convert=lambda result: SimpleNamespace(GetArray=lambda: image)
        ),
        PixelType_BGR8packed=1,
        OutputBitAlignment_MsbAligned=1,
        GrabStrategy_LatestImageOnly=1,
        GrabStrategy_OneByOne=2,
        TimeoutHandling_Return=0,
        TimeoutHandling_ThrowException=1,
    )
    monkeypatch.setitem(sys.modules, "pypylon", SimpleNamespace(pylon=pylon, __version__="26.8"))
    return SimpleNamespace(
        state=state,
        writes=writes,
        devices=devices,
        selected=selected,
        camera=camera,
        pylon=pylon,
    )


def test_basler_channel_settings_reach_sdk_and_frame_owns_pixels(
    pylon_sdk: SimpleNamespace,
) -> None:
    config = {
        "ip_address": "192.168.1.12",
        "transport_mode": "auto",
        "width": 32,
        "height": 24,
        "offset_x": 4,
        "offset_y": 2,
        "fps": 20,
        "exposure_time_us": 2500,
        "gain_db": 3,
        "pixel_format": "BayerRG8",
        "packet_size": 1500,
        "inter_packet_delay": 100,
        "trigger_mode": "On",
        "trigger_source": "Software",
        "url": "rtsp://unused-server-bridge.invalid/live",
    }
    mapped = _edge_source(channel_source=RuntimeSource(source_type="basler_pylon", config=config))
    assert mapped["transport_mode"] == "pylon_direct"
    assert "url" not in mapped
    source = cameras.create_camera_source(mapped)
    assert pylon_sdk.selected == [pylon_sdk.devices[1]]
    assert ("Width", 32) in pylon_sdk.writes
    assert ("AcquisitionFrameRateAbs", 20.0) in pylon_sdk.writes
    assert ("ExposureTimeAbs", 2500.0) in pylon_sdk.writes
    assert ("GevSCPD", 100) in pylon_sdk.writes
    frame = source.read(threading.Event())
    assert frame is not None and (frame.width, frame.height) == (32, 24)
    assert np.all(frame.image == 17)
    assert pylon_sdk.state.released == 1 and pylon_sdk.state.triggered == 1
    source.close()
    assert not pylon_sdk.state.open and not pylon_sdk.state.grabbing


def test_basler_auto_settings_apply_without_manual_values(pylon_sdk: SimpleNamespace) -> None:
    source = cameras.create_camera_source(
        {
            "type": "basler_pylon",
            "serial_number": "111",
            "exposure_auto": "Off",
            "gain_auto": "Continuous",
        }
    )
    try:
        assert ("ExposureAuto", "Off") in pylon_sdk.writes
        assert ("GainAuto", "Continuous") in pylon_sdk.writes
        assert not any(name == "ExposureTimeAbs" for name, _ in pylon_sdk.writes)
    finally:
        source.close()


def test_basler_ace_classic_profile_uses_model_specific_raw_gain(
    pylon_sdk: SimpleNamespace,
) -> None:
    source = cameras.create_camera_source(
        {
            "type": "basler_pylon",
            "serial_number": "111",
            "model": "acA1300-60gm",
            "gain_db": 6,
            "pixel_format": "BayerRG8",
        }
    )
    try:
        assert ("GainSelector", "AnalogAll") in pylon_sdk.writes
        assert ("GainRaw", 2) in pylon_sdk.writes
        assert ("PixelFormat", "Mono8") in pylon_sdk.writes
        assert not any(name == "Gain" for name, _ in pylon_sdk.writes)
    finally:
        source.close()


def test_basler_trims_odd_effective_roi_for_nvenc(pylon_sdk: SimpleNamespace) -> None:
    source = cameras.create_camera_source(
        {
            "type": "basler_pylon",
            "serial_number": "111",
            "width": 1281,
            "height": 1080,
        }
    )
    try:
        assert ("Width", 1281) in pylon_sdk.writes
        assert ("Width", 1280) in pylon_sdk.writes
        assert not any(name == "Height" and value == 1079 for name, value in pylon_sdk.writes)
    finally:
        source.close()


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"serial_number": "missing"},
        {
            "serial_number": "111",
            "ip_address": "192.168.1.12",
        },
    ],
)
def test_basler_never_opens_arbitrary_camera(
    pylon_sdk: SimpleNamespace,
    config: dict[str, Any],
) -> None:
    with pytest.raises(RuntimeError, match="Expected one matching"):
        cameras.BaslerPylonCameraSource(config)
    assert pylon_sdk.selected == []


def test_basler_closes_on_configuration_failure(pylon_sdk: SimpleNamespace) -> None:
    with pytest.raises(RuntimeError, match="unsupported pixel"):
        cameras.BaslerPylonCameraSource({"serial_number": "111", "pixel_format": "invalid"})
    assert not pylon_sdk.state.open and not pylon_sdk.state.grabbing


@pytest.mark.parametrize("mode,trigger", [("free_run", "Off"), ("software", "On"), ("line1", "On")])
def test_basler_legacy_manifests(pylon_sdk: SimpleNamespace, mode: str, trigger: str) -> None:
    source = cameras.BaslerPylonCameraSource(
        {
            "serial_number": "111",
            "exposure_us": 1000,
            "gain": 2,
            "trigger_mode": mode,
        }
    )
    assert ("ExposureTimeAbs", 1000) in pylon_sdk.writes
    assert ("TriggerMode", trigger) in pylon_sdk.writes
    source.close()


def test_basler_keeps_line_selection_and_idle_trigger_is_not_failure(
    pylon_sdk: SimpleNamespace,
) -> None:
    source = cameras.BaslerPylonCameraSource(
        {
            "serial_number": "111",
            "trigger_mode": "On",
            "trigger_source": "Line2",
            "trigger_activation": "FallingEdge",
        }
    )
    assert ("TriggerSource", "Line2") in pylon_sdk.writes
    assert ("TriggerActivation", "FallingEdge") in pylon_sdk.writes
    assert pylon_sdk.writes.index(("TriggerMode", "Off")) < pylon_sdk.writes.index(
        ("TriggerSource", "Line2")
    )
    pylon_sdk.state.timeout = True
    assert source.read(threading.Event()) is None
    assert pylon_sdk.state.triggered == 0
    stop = threading.Event()
    stop.set()
    assert source.read(stop) is None
    source.close()


def test_basler_does_not_remap_server_bridge_to_different_transport() -> None:
    with pytest.raises(EdgeWorkloadBuildError, match="pylon_direct"):
        _edge_source(
            channel_source=RuntimeSource(
                source_type="basler_pylon",
                config={"transport_mode": "aravis_rtsp_bridge"},
            )
        )


@pytest.mark.parametrize("sdk_state", ["missing", "broken", "empty", "cameras"])
def test_basler_inventory_requires_working_sdk(
    monkeypatch: pytest.MonkeyPatch,
    pylon_sdk: SimpleNamespace,
    sdk_state: str,
) -> None:
    monkeypatch.setattr(
        cameras.importlib.util,
        "find_spec",
        lambda name: (object() if name == "pypylon" and sdk_state != "missing" else None),
    )
    monkeypatch.setattr(cameras.shutil, "which", lambda name: None)
    if sdk_state == "broken":
        monkeypatch.setitem(sys.modules, "pypylon", SimpleNamespace())
    if sdk_state == "empty":
        pylon_sdk.devices.clear()
    inventory = next(
        item for item in cameras.inventory_camera_adapters() if item.adapter == "basler_pylon"
    )
    assert inventory.available is (sdk_state in {"empty", "cameras"})
    if sdk_state in {"missing", "broken"}:
        assert inventory.detail
    if sdk_state == "empty":
        assert inventory.device_count == 0


@pytest.mark.parametrize("platform", ["jetson_orin_nano", "jetson_agx_orin"])
@pytest.mark.parametrize("available", [True, False])
def test_basler_preflight_uses_probed_adapter(platform: str, available: bool) -> None:
    capabilities = EdgeCapabilitySnapshot(
        management_ip="192.168.50.21",
        platform=platform,
        architecture="aarch64",
        os_version="Jetson Linux",
        ram_total_bytes=8 * 1024**3,
        storage_total_bytes=128 * 1024**3,
        agent_version="0.8.0",
        runtime_version="0.8.0",
        runtime_code_digest=runtime_code_digest(),
        camera_adapters=["basler_pylon"] if available else [],
        storage_free_bytes=10 * 1024**3,
        observed_at="2026-09-11T12:00:00+00:00",
    )
    report = _admit(
        device_id="edge-basler",
        connectivity="online",
        enabled=True,
        deployment_revalidation_required=False,
        capability_digest="a" * 64,
        capabilities=capabilities,
        required_node_types=[],
        artifact_bytes=0,
        model_count=0,
        golden_tests=[],
        source_adapter="basler_pylon",
        source_secrets_available=True,
    )
    check = next(item for item in report.checks if item.key == "source.adapter")
    assert check.outcome == ("pass" if available else "fail")
    assert report.status == ("admitted" if available else "rejected")


def test_basler_free_run_timeout_reports_camera_failure(pylon_sdk: SimpleNamespace) -> None:
    source = cameras.BaslerPylonCameraSource({"serial_number": "111"})
    pylon_sdk.state.timeout = True
    with pytest.raises(RuntimeError, match="stopped delivering"):
        source.read(threading.Event())
    source.close()


def test_runtime_keeps_triggered_camera_running_after_idle_reads(
    pylon_sdk: SimpleNamespace,
) -> None:
    from cv_contracts import EdgeDeploymentManifest
    from edge_agent.runtime import EdgeRuntimeSupervisor, _WorkerHandle

    source = cameras.BaslerPylonCameraSource(
        {
            "serial_number": "111",
            "trigger_mode": "On",
            "trigger_source": "Line2",
        }
    )
    stop = threading.Event()
    reads = []

    def retrieve(*args: object) -> object:
        reads.append(1)
        if len(reads) == 1:
            return None
        return SimpleNamespace(GrabSucceeded=lambda: True, Release=lambda: None)

    pylon_sdk.camera.RetrieveResult = retrieve
    observed = []
    results = []

    def sink(*args: object) -> None:
        results.append(args)
        stop.set()

    supervisor = EdgeRuntimeSupervisor(
        store=SimpleNamespace(save_process=lambda process, **kwargs: observed.append(process)),
        result_sink=sink,
    )
    manifest = EdgeDeploymentManifest(
        deployment_id="basler-idle",
        workload_release_id="basler-release",
        channel_id="basler",
        pipeline_version_id="pipeline",
        idempotency_key="idle-test",
        created_at="2026-09-11T12:00:00+00:00",
        runtime_requirements={"emit_source_results": True},
    )
    handle = _WorkerHandle(stop, threading.Thread(), manifest)
    supervisor._process_frames(handle, source, 0, manifest.created_at)
    assert len(reads) == 2 and len(results) == 1
    assert all(process.state == "running" and process.restart_count == 0 for process in observed)
    source.close()
