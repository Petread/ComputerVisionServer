from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _module(name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _profile(platform: str) -> dict[str, object]:
    windows = platform == "windows_11"
    return {
        "platform": platform,
        "architecture": "x86_64" if windows else "aarch64",
        "base_os": "Windows 11" if windows else "Jetson Linux",
        "jetpack_version": None if windows else "7.2",
        "l4t_version": None if windows else "39.2",
        "cuda_version": "13.2",
        "tensorrt_version": "10.16",
        "deepstream_version": None if windows else "9.1",
        "approved_camera_adapters": ["rtsp", "usb_opencv"],
    }


def _environment(profile: dict[str, object]) -> dict[str, str]:
    return {
        "YBB_ALLOW_PLC_WRITES": "false",
        "YBB_EDGE_ARCHITECTURE": str(profile["architecture"]),
        "YBB_EDGE_ARTIFACT_QUOTA_BYTES": "34359738368",
        "YBB_EDGE_CAMERA_ADAPTERS": "rtsp,usb_opencv",
        "YBB_EDGE_CAPABILITY_INVENTORY": "probe",
        "YBB_EDGE_CONTROL_PORT": "9443",
        "YBB_EDGE_CUDA_VERSION": "13.2",
        "YBB_EDGE_DATA_ROOT": (
            r"C:\ProgramData\YBB\Edge"
            if profile["platform"] == "windows_11"
            else "/var/lib/ybb-edge"
        ),
        "YBB_EDGE_DEEPSTREAM_VERSION": str(profile.get("deepstream_version") or ""),
        "YBB_EDGE_ENGINE_BUILDER_MODE": (
            "tensorrt_python" if profile["platform"] == "windows_11" else "trtexec"
        ),
        "YBB_EDGE_HOSTNAME": "ybb-edge-01",
        "YBB_EDGE_JETPACK_VERSION": str(profile.get("jetpack_version") or ""),
        "YBB_EDGE_L4T_VERSION": str(profile.get("l4t_version") or ""),
        "YBB_EDGE_MANAGEMENT_IP": "192.168.50.21",
        "YBB_EDGE_MINIMUM_DISK_FREE_BYTES": "2147483648",
        "YBB_EDGE_OS_VERSION": str(profile["base_os"]),
        "YBB_EDGE_OUTBOX_QUOTA_BYTES": "2147483648",
        "YBB_EDGE_PLANT_ALLOWED_IPS": "192.168.50.40",
        "YBB_EDGE_PLANT_API_PORT": "9444",
        "YBB_EDGE_PLATFORM": str(profile["platform"]),
        "YBB_EDGE_RTSP_PORT": "8554",
        "YBB_EDGE_SERVER_IP": "192.168.50.10",
        "YBB_EDGE_TENSORRT_VERSION": "10.16",
        "YBB_EDGE_TRTEXEC_PATH": "trtexec",
    }


def _write_environment(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(values.items())),
        encoding="utf-8",
    )


def test_device_environment_validator_canonicalizes_ui_configuration(tmp_path: Path) -> None:
    validator = _module(
        "edge_device_environment_validator", "deployment/edge/validate-device-env.py"
    )
    profile = _profile("windows_11")
    values = _environment(profile)
    values["YBB_EDGE_PLANT_ALLOWED_IPS"] = "192.168.50.40, 192.168.50.41"

    observed = validator.validate_environment(values, profile)

    assert observed["YBB_EDGE_PLANT_ALLOWED_IPS"] == "192.168.50.40,192.168.50.41"
    assert observed["YBB_EDGE_ENGINE_BUILDER_MODE"] == "tensorrt_python"

    source = tmp_path / "device.env"
    _write_environment(source, {**values, "UNSUPPORTED_VARIABLE": "unsafe"})
    with pytest.raises(validator.DeviceEnvironmentError, match="Unsupported"):
        validator.read_environment(source)


@pytest.mark.parametrize("platform", ["jetson_orin_nano", "jetson_agx_orin"])
def test_jetson_ui_generates_validated_environment_without_manual_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    installer_ui = _module(
        "edge_jetson_installer_ui", "deployment/edge/install-jetson-offline-ui.py"
    )
    profile = _profile(platform)
    _write_environment(tmp_path / "device.env", _environment(profile))
    (tmp_path / "provisioning.json").write_text(
        json.dumps(
            {
                "prefix_length": 24,
                "gateway_ipv4": "192.168.50.1",
                "dns_servers": "192.168.50.1",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(installer_ui, "network_connections", lambda: ["Wired connection 1"])

    settings = installer_ui.default_settings(tmp_path)
    settings["device_ipv4"] = "192.168.50.31"
    settings = installer_ui.validate_settings(settings)
    generated = installer_ui.build_environment(tmp_path, settings)

    assert "YBB_EDGE_MANAGEMENT_IP=192.168.50.31\n" in generated
    assert f"YBB_EDGE_PLATFORM={platform}\n" in generated
    assert settings["connection"] == "Wired connection 1"


def test_jetson_ui_rejects_invalid_network_values() -> None:
    installer_ui = _module(
        "edge_jetson_installer_ui_invalid", "deployment/edge/install-jetson-offline-ui.py"
    )
    values = {
        "hostname": "edge-01",
        "device_ipv4": "not-an-ip",
        "prefix_length": "24",
        "gateway_ipv4": "192.168.50.1",
        "dns_servers": "192.168.50.1",
        "server_ipv4": "192.168.50.10",
        "plant_allowed_ipv4s": "",
        "control_port": "9443",
        "plant_api_port": "9444",
        "rtsp_port": "8554",
        "state_root": "/var/lib/ybb-edge",
        "camera_adapters": "rtsp",
        "connection": "Wired connection 1",
    }
    with pytest.raises(installer_ui.InstallerUiError, match="Edge device IP"):
        installer_ui.validate_settings(values)
