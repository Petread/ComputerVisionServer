from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cv_contracts import EdgeDeploymentManifest, EdgeDeviceAddress
from edge_agent.cameras import (
    CameraAdapterInventory,
    OpenCvCameraSource,
    create_camera_source,
)
from edge_agent.inventory import PlatformInventory, PlatformInventoryProbe
from edge_agent.state import EdgeAgentConfig, EdgeAgentState, EdgeStateError


def test_jetson_inventory_uses_real_probe_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "etc").mkdir()
    (tmp_path / "proc/device-tree").mkdir(parents=True)
    (tmp_path / "etc/nv_tegra_release").write_text(
        "# R39 (release), REVISION: 2.0", encoding="utf-8"
    )
    (tmp_path / "etc/os-release").write_text('PRETTY_NAME="Ubuntu 24.04 LTS"\n', encoding="utf-8")
    (tmp_path / "proc/device-tree/model").write_bytes(b"NVIDIA Jetson Orin Nano\x00")

    def runner(command: list[str]) -> str | None:
        joined = " ".join(command)
        if "dpkg-query" in joined:
            return "7.2.0"
        if "nvcc" in joined:
            return "Cuda compilation tools, release 13.2, V13.2.88"
        if "tensorrt" in joined:
            return "10.16.0"
        if "deepstream-app" in joined:
            return "DeepStreamSDK 9.1.0"
        return None

    monkeypatch.setattr("edge_agent.inventory.platform.system", lambda: "Linux")
    monkeypatch.setattr("edge_agent.inventory.platform.machine", lambda: "aarch64")
    inventory = PlatformInventoryProbe(runner=runner, root=tmp_path).probe()
    assert inventory.architecture == "aarch64"
    assert inventory.os_version == "Ubuntu 24.04 LTS"
    assert inventory.jetpack_version == "7.2.0"
    assert inventory.l4t_version == "39.2.0"
    assert inventory.cuda_version == "13.2"
    assert inventory.tensorrt_version == "10.16.0"
    assert inventory.gpu_model == "NVIDIA Jetson Orin Nano"


def test_opencv_adapter_applies_bounds_and_returns_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    class Capture:
        def __init__(self, uri: object, backend: int) -> None:
            self.uri = uri
            self.backend = backend
            self.values: list[tuple[int, float]] = []

        def isOpened(self) -> bool:  # noqa: N802 - mirrors OpenCV's public API
            return True

        def set(self, key: int, value: float) -> None:
            self.values.append((key, value))

        def read(self) -> tuple[bool, object]:
            return True, SimpleNamespace(shape=(1080, 1920, 3))

        def release(self) -> None:
            return None

    cv2 = SimpleNamespace(
        CAP_GSTREAMER=1800,
        CAP_ANY=0,
        CAP_DSHOW=700,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_BUFFERSIZE=6,
        CAP_PROP_FOURCC=7,
        VideoWriter_fourcc=lambda *value: "".join(value),
        VideoCapture=Capture,
    )
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    source = OpenCvCameraSource(
        {
            "type": "usb_opencv",
            "device_index": 2,
            "backend": "dshow",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "buffer_size": 2,
            "fourcc": "mjpg",
        }
    )
    frame = source.read(threading.Event())
    assert frame is not None and (frame.width, frame.height) == (1920, 1080)
    assert source._capture.uri == 2
    assert source._capture.backend == cv2.CAP_DSHOW
    assert (cv2.CAP_PROP_BUFFERSIZE, 2) in source._capture.values
    assert (cv2.CAP_PROP_FOURCC, "MJPG") in source._capture.values
    source.close()


def test_camera_registry_has_vendor_adapters_and_rejects_unknown() -> None:
    with pytest.raises(RuntimeError, match="Unsupported"):
        create_camera_source({"type": "unreleased_sdk"})


def test_agent_capabilities_only_advertise_probed_and_approved_adapters(tmp_path: Path) -> None:
    class Probe:
        def probe(self) -> PlatformInventory:
            return PlatformInventory(
                source="probed",
                architecture="x86_64",
                os_version="Windows 11 24H2",
                tensorrt_version="10.16.0",
                gpu_model="NVIDIA RTX",
                gpu_driver_version="600.1",
                gpu_memory_bytes=8 * 1024**3,
                ram_total_bytes=32 * 1024**3,
                python_version="3.12.10",
                camera_adapters=[
                    CameraAdapterInventory("rtsp", True, "opencv", "4.12"),
                    CameraAdapterInventory("basler_pylon", False, "pypylon"),
                ],
            )

    state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.31"),
            data_root=tmp_path,
            platform="windows_11",
            architecture="x86_64",
            capability_inventory="probe",
            camera_adapters=("rtsp", "basler_pylon"),
            engine_builder_mode="simulated",
        ),
        inventory_probe=Probe(),  # type: ignore[arg-type]
    )
    try:
        capabilities = state.capabilities()
        assert capabilities.inventory_source == "probed"
        assert capabilities.camera_adapters == ["rtsp"]
        assert capabilities.gpu_driver_version == "600.1"
        assert capabilities.python_version == "3.12.10"
    finally:
        state.close()


def test_local_admission_revalidates_against_probed_runtime_profile(tmp_path: Path) -> None:
    class Probe:
        def probe(self) -> PlatformInventory:
            return PlatformInventory(
                source="probed",
                architecture="aarch64",
                os_version="Ubuntu 24.04",
                cuda_version="13.2.2",
                tensorrt_version="10.16.2.10",
                gpu_model="NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super",
            )

    state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path,
            platform="jetson_orin_nano",
            architecture="aarch64",
            cuda_version="13.2",
            tensorrt_version="10.16",
            gpu_model="Jetson Orin Nano",
            capability_inventory="probe",
            engine_builder_mode="simulated",
        ),
        inventory_probe=Probe(),  # type: ignore[arg-type]
    )
    manifest = EdgeDeploymentManifest(
        deployment_id="deployment-probed-profile",
        workload_release_id="release-probed-profile",
        channel_id="channel-probed-profile",
        pipeline_version_id="pipeline-probed-profile",
        idempotency_key="prepare-probed-profile",
        runtime_requirements={
            "source": {"type": "passive"},
            "target_profile": {
                "platform": "jetson_orin_nano",
                "architecture": "aarch64",
                "cuda_version": "13.2.2",
                "tensorrt_version": "10.16.2.10",
                "gpu_model": (
                    "NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super"
                ),
            },
        },
        created_at=datetime.now(UTC).isoformat(),
    )
    try:
        assert state.prepare(manifest).status == "prepared"
    finally:
        state.close()


def test_local_admission_rejects_camera_not_in_device_allowlist(tmp_path: Path) -> None:
    state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path,
            camera_adapters=("rtsp",),
            engine_builder_mode="simulated",
        )
    )
    manifest = EdgeDeploymentManifest(
        deployment_id="deployment-e2-camera",
        workload_release_id="release-e2-camera",
        channel_id="channel-e2-camera",
        pipeline_version_id="pipeline-e2-camera",
        idempotency_key="prepare-e2-camera",
        runtime_requirements={"source": {"type": "flir_spinnaker"}},
        created_at=datetime.now(UTC).isoformat(),
    )
    try:
        with pytest.raises(EdgeStateError) as exc_info:
            state.prepare(manifest)
        assert exc_info.value.code == "camera_adapter_not_approved"
    finally:
        state.close()
