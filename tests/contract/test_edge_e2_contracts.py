from __future__ import annotations

import json
from pathlib import Path

from cv_contracts import EdgeCameraAdapterCapability, EdgeCapabilitySnapshot

ROOT = Path(__file__).resolve().parents[2]


def test_capability_contract_exposes_probe_and_camera_inventory() -> None:
    schema = EdgeCapabilitySnapshot.model_json_schema()
    assert "inventory_source" in schema["properties"]
    assert "camera_inventory" in schema["properties"]
    assert EdgeCameraAdapterCapability(adapter="rtsp", available=True, provider="opencv")


def test_all_platform_profiles_and_golden_images_are_strictly_versioned() -> None:
    profiles = ROOT / "deployment/edge/profiles"
    images = ROOT / "deployment/edge/provisioning/golden-images"
    for name, platform in (
        ("jetson-orin-nano.json", "jetson_orin_nano"),
        ("jetson-agx-orin.json", "jetson_agx_orin"),
        ("windows-11-nvidia.json", "windows_11"),
    ):
        profile = json.loads((profiles / name).read_text(encoding="utf-8"))
        golden = json.loads((images / name).read_text(encoding="utf-8"))
        assert profile["schema_version"] == "ybb.edge.platform-profile.v1"
        assert profile["platform"] == platform
        assert profile["approved_camera_adapters"]
        assert golden["schema_version"] == "ybb.edge.golden-image.v1"
        assert golden["platform"] == platform


def test_native_gui_and_ci_workflow_are_real_build_assets() -> None:
    source = (ROOT / "deployment/edge/bundle-builder/YBB.EdgeBundleBuilder.cs").read_text(
        encoding="utf-8"
    )
    workflow = (ROOT / ".github/workflows/edge-offline-bundles.yml").read_text(encoding="utf-8")
    assert "Application.Run(new MainForm())" in source
    assert "PropertyGrid" in source
    assert "TargetPlatform" in source
    assert "Online-Inhalte vorbereiten (Internet)" in source
    assert '"prepare-wheelhouse"' in source
    assert "windows-payload-sources.json" in (
        ROOT / "scripts/release/build_edge_offline_bundle.py"
    ).read_text(encoding="utf-8")
    assert "YBB-Edge-Bundle-Builder.exe" in workflow
    assert "windows-latest" in workflow


def test_ip_roles_and_plc_directions_are_unambiguous_across_both_guis() -> None:
    builder = (ROOT / "deployment/edge/bundle-builder/YBB.EdgeBundleBuilder.cs").read_text(
        encoding="utf-8"
    )
    fleet_ui = (
        ROOT
        / "frontend/apps/admin-web/src/features/edge-devices/edge-device-pages.tsx"
    ).read_text(encoding="utf-8")
    graph = (
        ROOT / "backend/services/edge-agent/src/edge_agent/graph.py"
    ).read_text(encoding="utf-8")
    workload_builder = (
        ROOT
        / "backend/services/runtime-admin-api/src/runtime_admin_api/edge_workload_builder.py"
    ).read_text(encoding="utf-8")
    assert "Edge-Geräte-IP (feste IPv4)" in builder
    assert "Orchestrierungsserver-IP (feste IPv4)" in builder
    assert "Erlaubte HTTP-Trigger-Clients (SPS-IP, Komma)" in builder
    assert "SPS-Schreiben aus Pipeline erlauben" in builder
    assert "Edge-Geräte-IP (feste IPv4)" in fleet_ui
    assert "Orchestrierungsserver-IP (feste IPv4)" in fleet_ui
    assert "HTTP-Trigger-Port am Edge (SPS → Edge)" in fleet_ui
    assert "/edge-plant/v1/channels/&lt;kanal-id&gt;/trigger" in fleet_ui
    assert "<span>Management-IP</span>" not in fleet_ui
    assert "<span>Server-IP</span>" not in fleet_ui
    assert '"output.siemens_sps_write"' in graph
    assert "SiemensSpsWriteHandler" in graph
    assert '"graph": loaded.graph.model_dump(mode="json")' in workload_builder


def test_offline_installers_require_checksums() -> None:
    jetson = (ROOT / "deployment/edge/install-jetson-offline.sh").read_text(encoding="utf-8")
    windows = (ROOT / "deployment/edge/Install-WindowsOffline.ps1").read_text(encoding="utf-8")
    assert "Mandatory checksum manifest missing" in jetson
    assert "Mandatory SHA256SUMS missing" in windows
    assert "SIGNATURE_POLICY" in jetson and "SIGNATURE_POLICY" in windows
    assert 'Join-Path $BundleRoot "cuda-runtime-installer.exe"' in windows
    assert "Start-Process -FilePath $cudaInstaller" in windows
    assert "pip install --no-index" in windows
    assert "-m edge_agent.tensorrt_smoke" in windows
    assert "DeviceConfigPath" in windows
    assert "validate-device-env.py" in windows and "validate-device-env.py" in jetson


def test_native_windows_and_jetson_installer_uis_use_existing_installers() -> None:
    windows_ui = (
        ROOT / "deployment/edge/bundle-builder/YBB.EdgeWindowsInstaller.cs"
    ).read_text(encoding="utf-8")
    jetson_ui = (
        ROOT / "deployment/edge/install-jetson-offline-ui.py"
    ).read_text(encoding="utf-8")
    native_build = (
        ROOT / "deployment/edge/bundle-builder/Build-NativeBundleBuilder.ps1"
    ).read_text(encoding="utf-8")
    assert "YBB-Edge-Windows-Installer.exe" in native_build
    assert "Install-WindowsOffline.ps1" in windows_ui
    assert '"-DeviceConfigPath"' in windows_ui
    assert '"-VerifyOnly"' in windows_ui
    assert "SelectBundleArchive" in windows_ui and "ExtractBundleArchive" in windows_ui
    assert "install-jetson-offline.sh" in jetson_ui
    assert '"--config"' in jetson_ui
    assert '"--verify-only"' in jetson_ui


def test_server_materializes_native_vendor_camera_sources_for_edge() -> None:
    builder = (
        ROOT / "backend/services/runtime-admin-api/src/runtime_admin_api/edge_workload_builder.py"
    ).read_text(encoding="utf-8")
    assert '"type": "aravis_gige"' in builder
    assert '"type": "basler_pylon"' in builder
    assert '"type": "flir_spinnaker"' in builder
    assert "needs a GStreamer pipeline or RTSP bridge for M3" not in builder
