from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOYMENT_DIR = REPO_ROOT / "deployment"
ADMIN_OPERATIONAL_PROBES = (
    REPO_ROOT
    / "backend"
    / "services"
    / "admin-api"
    / "src"
    / "admin_api"
    / "application"
    / "operational_probes.py"
)


def _read_script(name: str) -> str:
    return (DEPLOYMENT_DIR / name).read_text(encoding="utf-8")


def _read_wsl_script(name: str) -> str:
    return (DEPLOYMENT_DIR / "wsl" / name).read_text(encoding="utf-8")


def test_windows_update_bundle_exports_persistent_updater() -> None:
    content = _read_script("export-windows-update.ps1")

    assert "deployment\\apply-windows-update.ps1" in content
    assert '"apply-windows-update.ps1"' in content
    assert 'UpdaterScript = "apply-windows-update.ps1"' in content
    assert "Ensure-DeploymentUiExe" in content
    assert '"YBB-Deployment-UI.exe"' in content
    assert '"windows-deployment-ui.ps1"' in content
    assert "MediaMtxArchivePath" in content
    assert "Resolve-MediaMtxArchiveForExport" in content
    assert "New-MediaMtxArchiveFromRelease" in content
    assert "MediaMtxArchiveCacheDir" in content
    assert "PrepareRuntimeSkipMediaMtxCheck" in content
    assert "runtime/mediamtx.zip" in content
    assert '"install-windows-mediamtx.ps1"' in content
    assert "MediaMtxArchiveSha256" in content
    assert "MediaMtxSkipped" in content
    assert 'MediaMtxHostPlatform = "windows"' in content
    assert "StreamingHlsPort" not in content
    assert "StreamingWebRtcIceTcpPort" in content
    assert "GStreamerDebBundle" in content
    assert "runtime/gstreamer-debs.tar.gz" in content
    assert "Resolve-GStreamerDebBundleForExport" in content
    assert "StreamAuthEnabled" in content
    assert "PlcWritesAllowed" in content
    assert "WslNetworkingMode" in content
    assert "WslHyperVFirewallDefaultInboundAction" in content
    assert "WslRestartAllowed" in content
    assert 'Resolve-ExecutableCommand -Names @("git.exe", "git")' in content
    assert "Git wurde nicht gefunden; AppVersion wird ohne Commit-SHA erzeugt." in content


def test_windows_installer_bundle_exports_persistent_updater() -> None:
    content = _read_script("export-windows-installer.ps1")

    assert "deployment\\apply-windows-update.ps1" in content
    assert '"apply-windows-update.ps1"' in content
    assert 'UpdaterScript = "apply-windows-update.ps1"' in content
    assert "Ensure-DeploymentUiExe" in content
    assert '"YBB-Deployment-UI.exe"' in content
    assert '"windows-deployment-ui.ps1"' in content
    assert "MediaMtxArchivePath" in content
    assert "Resolve-MediaMtxArchiveForExport" in content
    assert "New-MediaMtxArchiveFromRelease" in content
    assert "MediaMtxArchiveCacheDir" in content
    assert "PrepareRuntimeSkipMediaMtxCheck" in content
    assert "runtime/mediamtx.zip" in content
    assert '"install-windows-mediamtx.ps1"' in content
    assert "MediaMtxArchiveSha256" in content
    assert "MediaMtxSkipped" in content
    assert 'MediaMtxHostPlatform = "windows"' in content
    assert "StreamingHlsPort" not in content
    assert "StreamingWebRtcIceTcpPort" in content
    assert "GStreamerDebBundle" in content
    assert "runtime/gstreamer-debs.tar.gz" in content
    assert "Resolve-GStreamerDebBundleForExport" in content
    assert "StreamAuthEnabled" in content
    assert "PlcWritesAllowed" in content
    assert "WslNetworkingMode" in content
    assert "WslHyperVFirewallDefaultInboundAction" in content
    assert "WslRestartAllowed" in content
    assert 'Resolve-ExecutableCommand -Names @("git.exe", "git")' in content
    assert "Git wurde nicht gefunden; AppVersion wird ohne Commit-SHA erzeugt." in content


def test_windows_bundles_validate_and_describe_temporal_compatibility_surface() -> None:
    for script_name in (
        "export-windows-installer.ps1",
        "export-windows-update.ps1",
    ):
        content = _read_script(script_name)

        assert "function Assert-TemporalCompatibilitySources" in content
        assert content.count("Assert-TemporalCompatibilitySources") >= 2
        assert 'CompatibilityProfile = "temporal-sequence.v1"' in content
        assert 'MinimumDatabaseRevision = "20260803_0035"' in content
        assert 'PersistentStateRoot = "/srv/cv-platform"' in content
        assert (
            'TemporalSampleStorage = "/srv/cv-platform/runtime/captures/temporal-samples"'
            in content
        )
        assert 'TemporalSnapshotStorage = "/srv/cv-platform/control/snapshots"' in content
        assert 'StemOrientationActivation = "pipeline_configuration_required"' in content
        assert 'TemporalModelPromotion = "manual_shadow_only"' in content
        assert "frontend\\packages\\schemas\\src\\index.ts" in content
        assert "contracts\\openapi\\dataset-service.v1.yaml" in content
        assert "contracts\\openapi\\result-service.v1.yaml" in content
        assert "20260803_0035_stem_orientation_reviews.py" in content
        assert "type RuntimePreviewView = 'full' | 'model'" in content
        assert "Full camera view + overlays" in content
        assert "Model / Label Studio view + overlays" in content
        assert "Full view + projected overlay" not in content


def test_wsl_apply_validates_temporal_contracts_before_migration_and_activation() -> None:
    content = _read_wsl_script("apply-release.sh")

    assert "validate_temporal_release_compatibility()" in content
    assert (
        content.index("validate_temporal_release_compatibility\n")
        < content.index("run_migrations\n")
        < content.index("activate_release\n")
    )
    assert "read_bundle_manifest_value CompatibilityProfile" in content
    assert "read_bundle_manifest_value MinimumDatabaseRevision" in content
    assert "read_bundle_manifest_value StemOrientationActivation" in content
    assert "release-candidate-service-imports-ok" in content
    assert "release-candidate-temporal-contracts-ok" in content
    assert 'get_annotation_schema("stem_orientation_angle.v1")' in content
    assert 'get_default_trainer(TaskType.TEMPORAL_SEQUENCE)' in content
    assert 'trainer.trainer_backend != "pytorch-temporal-sequence"' in content
    assert "default_promotion_criteria(TaskType.TEMPORAL_SEQUENCE)" in content


def test_wsl_apply_preserves_temporal_media_snapshots_models_and_reviews() -> None:
    content = _read_wsl_script("apply-release.sh")
    migration = (
        REPO_ROOT
        / "backend"
        / "migrations"
        / "versions"
        / "20260803_0035_stem_orientation_reviews.py"
    ).read_text(encoding="utf-8")

    for persistent_path in (
        '"$STATE_ROOT/runtime/captures/.temporal-spool"',
        '"$STATE_ROOT/runtime/captures/temporal-samples"',
        '"$STATE_ROOT/runtime/results"',
        '"$STATE_ROOT/control/labelstudio/exports"',
        '"$STATE_ROOT/control/snapshots"',
        '"$STATE_ROOT/control/dvc/repo"',
        '"$STATE_ROOT/control/dvc/remote"',
        '"$STATE_ROOT/serving/packages"',
    ):
        assert persistent_path in content
    assert "Range: bytes=2-5" in content
    assert 'video_probe_status" != "206"' in content
    assert 'video_probe_body" != "2345"' in content
    assert "Video-Byte-Ranges" in content
    assert "def downgrade() -> None:" in migration
    assert "operator-entered durable data" in migration
    assert migration.rstrip().endswith("pass")


def test_windows_triton_export_resolves_docker_from_persisted_windows_configuration() -> None:
    for script_name in (
        "export-windows-update.ps1",
        "build-windows-installer.ps1",
        "prepare-wsl-runtime.ps1",
    ):
        content = _read_script(script_name)

        assert 'GetEnvironmentVariable("YBB_DOCKER_CLI", $target)' in content
        assert 'GetEnvironmentVariable("Path", $target)' in content
        assert "[System.EnvironmentVariableTarget]::User" in content
        assert "[System.EnvironmentVariableTarget]::Machine" in content
        assert r"CurrentVersion\App Paths\docker.exe" in content
        assert "Triton-Archiv ueberspringen (kein Docker)" in content

    assert 'New-CheckBox "Triton-Archiv ueberspringen (kein Docker)" $true' in _read_script(
        "windows-deployment-ui.ps1"
    )


def test_windows_deployment_does_not_resolve_powershell_through_path() -> None:
    for script_name in (
        "apply-windows-update.ps1",
        "build-windows-installer.ps1",
        "export-windows-installer.ps1",
        "export-windows-update.ps1",
        "install-windows-offline.ps1",
        "update-windows-offline.ps1",
    ):
        content = _read_script(script_name)

        assert "Get-Command powershell.exe" not in content
        assert '-FilePath "powershell.exe"' not in content
        assert r"System32\WindowsPowerShell\v1.0\powershell.exe" in content


def test_windows_app_archives_exclude_test_and_temporary_directories() -> None:
    for script_name in (
        "export-windows-installer.ps1",
        "export-windows-update.ps1",
    ):
        content = _read_script(script_name)

        assert content.count('".test-tmp"') >= 1
        assert content.count('".tmp"') >= 1
        assert '"--exclude=.test-tmp"' in content
        assert '"--exclude=.tmp"' in content


def test_windows_bundles_reject_frontend_built_for_the_root_path() -> None:
    for script_name in ("export-windows-installer.ps1", "export-windows-update.ps1"):
        content = _read_script(script_name)

        assert '(?:src|href)="/frontend/assets/' in content
        assert "FrontendBasePath muss /frontend/ sein" in content
        assert content.count("Assert-FrontendBuildOutput") >= 2

    apply_release = _read_wsl_script("apply-release.sh")
    assert "validate_release_frontend_assets()" in apply_release
    assert (
        "validate_release_frontend_assets\n"
        "validate_temporal_release_compatibility\n"
        "ensure_internal_machine_credentials"
        in apply_release
    )
    assert "Release-Kandidat wurde nicht fuer /frontend/ gebaut" in apply_release


def test_windows_install_persists_updater_and_incoming_directory() -> None:
    content = _read_script("install-windows-offline.ps1")

    assert "Install-TargetUpdaterFiles" in content
    assert 'Join-Path $appDir "incoming"' in content
    assert 'Join-Path $appDir "updater"' in content
    assert "update-ybb-inferenzserver.cmd" in content
    assert '"apply-windows-update.ps1"' in content
    assert "Invoke-PortProxySetup" in content
    assert "Start-TargetApplication" in content
    assert "Starte und validiere Windows-Autostart-Aufgabe." in content
    assert "Windows-Autostart ist eine Pflichtkomponente" in content
    assert "Get-PortProxyListenAddress" in content
    assert '"-ListenAddress", $listenAddress' in content
    assert '"-ConnectPort", [string]$connectPort' in content
    assert 'Alias("AppHostName", "HostName")' in content
    assert '"YBB-Deployment-UI.exe"' in content
    assert '"windows-deployment-ui.ps1"' in content
    assert "ybb-start-platform" in content
    assert "ybb-keepalive" in content
    assert "start-ybb-inferenzserver.ps1" in content
    assert "stop-ybb-inferenzserver.cmd" in content
    assert "rtsp-probe-ybb-inferenzserver.cmd" in content
    assert "RtspProbeCommand" in content
    assert "pgrep -f ybb-keepalive" in content
    assert "systemctl --no-pager list-units ''ybb-*'' caddy" in content
    assert "Set-WslGlobalNetworkingConfig" in content
    assert "Backup-WslConfig" in content
    assert "Remove-WindowsPortProxyRulesForPorts" in content
    assert "starte vorhandenen Windows-Host-Fanout" in content
    assert "Starte vorhandenes MediaMTX direkt auf Windows." in content
    assert '"-Action", "Start"' in content
    assert '"v4tov6"' in content
    assert '"v6tov6"' in content
    assert "Reset-WslMirroredWebPortBindings" in content
    assert "Reset-WindowsPortProxyState" in content
    assert "netsh interface portproxy reset" in content
    assert "Test-WindowsTcpPortsFreeForWslMirrored" in content
    assert "Restart-IpHelperServiceForMirroredNetworking" in content
    assert "Assert-WindowsTcpPortsFreeForWslMirrored" in content
    assert (
        'Reset-WslMirroredWebPortBindings -Ports $portsToRelease -Reason "WSL Mirrored Networking vor WSL-Start" -RestartAllowed $wslRestartAllowed'
        in content
    )
    assert (
        'Invoke-LoggedCommand -Description "Beende WSL fuer Mirrored-Webport-Freigabe." -FilePath "wsl" -Arguments @("--shutdown") -AllowedExitCodes @(0) | Out-Null'
        in content
    )
    assert (
        "Starte Windows-Dienst iphlpsvc neu, damit geloeschte Portproxy-Listener freigegeben werden."
        in content
    )
    assert "Windows-Port vor WSL-Start belegt" in content
    assert (
        'Invoke-LoggedCommand -Description "Beende WSL fuer .wslconfig-Neustart." -FilePath "wsl" -Arguments @("--shutdown") -AllowedExitCodes @(0) | Out-Null'
        in content
    )
    assert (
        'Set-NetFirewallHyperVVMSetting -Name "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" -DefaultInboundAction $normalizedAction -ErrorAction Stop | Out-Null'
        in content
    )
    assert (
        "Set-YbbWslHyperVFirewallDefaultInboundAction -NetworkingMode $requestedMode -Action $HyperVFirewallDefaultInboundAction | Out-Null"
        in content
    )
    assert "-WslNetworkingMode $effectiveWslNetworkingMode" in content


def test_windows_offline_defaults_use_ip_port_authority() -> None:
    for name in (
        "build-windows-installer.ps1",
        "export-windows-installer.ps1",
        "export-windows-update.ps1",
        "windows-offline.config.example.ps1",
        "windows-offline.config.ps1",
    ):
        assert "127.0.0.1" in _read_script(name)
        assert "127.0.0.1:8443" not in _read_script(name)


def test_windows_wsl_autostart_is_login_independent_and_mandatory() -> None:
    autostart = _read_script("install-windows-autostart.ps1")

    assert "Assert-WslDistroAccessible" in autostart
    assert "& $wslPath -d $Name -u root -- /bin/true" in autostart
    assert "New-ScheduledTaskTrigger -AtStartup" in autostart
    assert "-LogonType S4U" in autostart
    assert "MSFT_TaskBootTrigger" in autostart
    assert "Assert-YbbScheduledTaskDefinition" in autostart
    assert "function Test-WindowsAccountEquivalent" in autostart
    assert ".Translate([System.Security.Principal.SecurityIdentifier])" in autostart
    assert "-RegisteredUserId ([string]$task.Principal.UserId)" in autostart
    assert "Windows-Autostart-Aufgabe gestartet und laeuft dauerhaft" in autostart
    assert "Anmelden als Stapelverarbeitungsauftrag" in autostart

    for script_name in (
        "install-windows-offline.ps1",
        "update-windows-offline.ps1",
    ):
        content = _read_script(script_name)
        assert "Windows-Autostart ist eine Pflichtkomponente" in content
        assert "Der login-unabhaengige WSL-Autostart konnte nicht gestartet" in content
        assert "Starte WSL-Keepalive direkt als Fallback." not in content


def test_windows_update_refreshes_installed_updater() -> None:
    content = _read_script("update-windows-offline.ps1")

    assert "Install-TargetUpdaterFiles" in content
    assert '"apply-windows-update.ps1"' in content
    assert '"configure-windows-portproxy.ps1"' in content
    assert '"YBB-Deployment-UI.exe"' in content
    assert "status-ybb-inferenzserver.cmd" in content
    assert "ybb-start-platform" in content
    assert "ybb-keepalive" in content
    assert "start-ybb-inferenzserver.ps1" in content
    assert "stop-ybb-inferenzserver.cmd" in content
    assert "rtsp-probe-ybb-inferenzserver.cmd" in content
    assert "RtspProbeCommand" in content
    assert "pgrep -f ybb-keepalive" in content
    assert "systemctl --no-pager list-units ''ybb-*'' caddy" in content
    assert "Installierter Updater wurde aktualisiert" in content
    assert "Start-TargetApplication" in content
    assert "Starte und validiere Windows-Autostart-Aufgabe." in content
    assert "Windows-Autostart ist eine Pflichtkomponente" in content
    assert '[Alias("AppHostName", "HostName")]' in content
    assert "Aktualisiere oeffentliche Adresse in WSL" in content
    assert "--host-name" in content
    assert "$authorityHost" in content
    assert "$host =" not in content.lower()
    assert "[string]$DefaultValue" not in content
    assert "$DefaultValue = $null" in content
    assert "$value = $Object.$Name" in content
    assert (
        '$mediaMtxSkipped = [bool](Get-OptionalJsonValue -Object $manifest -Name "MediaMtxSkipped" -DefaultValue $false)'
        in content
    )
    assert (
        '$streamingWebRtcIceTcpPort = [int](Get-OptionalJsonValue -Object $manifest -Name "StreamingWebRtcIceTcpPort" -DefaultValue 8189)'
        in content
    )
    assert "$streamingTcpPorts = @($streamingTurnTcpPort)" in content
    assert "$streamingUdpPorts = @()" in content
    assert "Set-WslGlobalNetworkingConfig" in content
    assert "Backup-WslConfig" in content
    assert "Remove-WindowsPortProxyRulesForPorts" in content
    assert "starte vorhandenen Windows-Host-Fanout" in content
    assert "Starte vorhandenes MediaMTX direkt auf Windows." in content
    assert '"-Action", "Start"' in content
    assert '"v4tov6"' in content
    assert '"v6tov6"' in content
    assert "Reset-WslMirroredWebPortBindings" in content
    assert "Reset-WindowsPortProxyState" in content
    assert "netsh interface portproxy reset" in content
    assert "Test-WindowsTcpPortsFreeForWslMirrored" in content
    assert "Restart-IpHelperServiceForMirroredNetworking" in content
    assert "Assert-WindowsTcpPortsFreeForWslMirrored" in content
    assert (
        'Reset-WslMirroredWebPortBindings -Ports $portsToRelease -Reason "WSL Mirrored Networking vor WSL-Update" -RestartAllowed $wslRestartAllowed'
        in content
    )
    assert (
        'Invoke-LoggedCommand -Description "Beende WSL fuer Mirrored-Webport-Freigabe." -FilePath "wsl" -Arguments @("--shutdown") -AllowedExitCodes @(0) | Out-Null'
        in content
    )
    assert (
        "Starte Windows-Dienst iphlpsvc neu, damit geloeschte Portproxy-Listener freigegeben werden."
        in content
    )
    assert "Windows-Port vor WSL-Start belegt" in content
    assert (
        'Invoke-LoggedCommand -Description "Beende WSL fuer .wslconfig-Neustart." -FilePath "wsl" -Arguments @("--shutdown") -AllowedExitCodes @(0) | Out-Null'
        in content
    )
    assert (
        'Set-NetFirewallHyperVVMSetting -Name "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" -DefaultInboundAction $normalizedAction -ErrorAction Stop | Out-Null'
        in content
    )
    assert (
        "Set-YbbWslHyperVFirewallDefaultInboundAction -NetworkingMode $requestedMode -Action $HyperVFirewallDefaultInboundAction | Out-Null"
        in content
    )
    assert "YBB_WSL_NETWORKING_MODE=$(Quote-Bash $effectiveWslNetworkingMode)" in content


def test_deployment_ui_exposes_mediamtx_archive_for_export() -> None:
    content = _read_script("windows-deployment-ui.ps1")

    assert "MediaMTX-Archiv" in content
    assert "MediaMTX Windows Archiv (*.zip)" in content
    assert "MediaMTX ueberspringen" in content
    assert "$exportMediaMtxArchive" in content
    assert "$exportSkipMediaMtx" in content
    assert "$exportSkipGStreamer" in content
    assert "$PrepareRuntimeSkipMediaMtxCheck" in content
    assert "$PrepareRuntimeSkipGStreamerCheck" in content
    assert "-SkipMediaMtxCheck $exportSkipMediaMtx.Checked" in content
    assert "-SkipGStreamerCheck $exportSkipGStreamer.Checked" in content
    assert "-MediaMtxArchivePath $exportMediaMtxArchive.Text" in content
    assert "$StreamAuthEnabled = $false" in content
    assert "PLC-Schreiben erlauben" in content
    assert "WSL Mirrored Networking" in content
    assert '$WslNetworkingMode = "mirrored"' in content
    assert '$WslHyperVFirewallDefaultInboundAction = "Allow"' in content
    assert "[bool]$PlcWritesAllowed" in content
    assert "[bool]$WslMirroredNetworking" in content
    assert "'$PlcWritesAllowed = ' + (ConvertTo-PowerShellLiteral $PlcWritesAllowed)" in content
    assert "-WslMirroredNetworking $exportWslMirrored.Checked" in content
    assert "-PlcWritesAllowed $exportPlcWritesAllowed.Checked" in content
    assert "$exitCode = [int]$process.ExitCode" in content
    assert "fehlgeschlagen\\. Exit Code: [1-9]" in content
    assert "exit $exitCode" in content


def test_mediamtx_archive_is_auto_cached_when_not_skipped() -> None:
    build_content = _read_script("build-windows-installer.ps1")
    config_content = _read_script("windows-offline.config.ps1")
    operations_content = (
        REPO_ROOT
        / "backend"
        / "services"
        / "admin-api"
        / "src"
        / "admin_api"
        / "api"
        / "routes"
        / "operations.py"
    ).read_text(encoding="utf-8")

    assert "$PrepareRuntimeSkipMediaMtxCheck = $false" in config_content
    assert '$MediaMtxArchiveCacheDir = ".runtime\\mediamtx"' in config_content
    assert '$MediaMtxVersion = "1.19.0"' in config_content
    assert "$StreamingWebRtcIceTcpPort = 8189" in config_content
    assert "$StreamingTurnTcpPort = 3478" in config_content
    assert "StreamingWebRtcIceUdpPort" not in config_content
    assert '$WslNetworkingMode = "mirrored"' in config_content
    assert '$WslHyperVFirewallDefaultInboundAction = "Allow"' in config_content
    assert "$WslRestartAllowed = $true" in config_content
    assert "$PrepareRuntimeSkipGStreamerCheck = $false" in config_content
    assert '$GStreamerDebBundleCacheDir = ".runtime\\apt\\gstreamer"' in config_content
    assert '"gstreamer1.0-rtsp"' in config_content
    assert '"gstreamer1.0-tools"' in config_content
    assert '"coturn"' in config_content
    generator_content = (DEPLOYMENT_DIR / "create-gstreamer-deb-bundle.ps1").read_text(
        encoding="utf-8"
    )
    assert "PackageListPath" in generator_content
    assert "enable_ubuntu_components" in generator_content
    assert "universe" in generator_content
    assert "multiverse" in generator_content
    assert "root_package=\"${root_package//$'\\r'/}\"" in generator_content
    assert "has_package_candidate" in generator_content
    assert "Dir::State::status" in generator_content
    assert "--download-only" in generator_content
    assert "--no-install-recommends" in generator_content
    assert "APT::Sandbox::User=root" in generator_content
    assert "dpkg-deb -f" in generator_content
    assert "root-packages.resolved.txt" in generator_content
    assert "No resolvable GStreamer Debian packages were found" in generator_content
    assert "gstreamer1.0-plugins-ugly" in generator_content
    assert "gstreamer1.0-rtsp" in generator_content
    assert "gstreamer-debs-v4-" in _read_script("export-windows-update.ps1")
    assert "runtime/python-wheels.tar.gz" in _read_script("export-windows-update.ps1")
    assert "PythonWheelBundlePath" in _read_script("export-windows-update.ps1")
    assert "New-PythonWheelBundleForExport" in _read_script("export-windows-update.ps1")
    assert "Test-PythonWheelBundleContainsSnap7" in _read_script("export-windows-update.ps1")
    assert "Test-PythonWheelBundleContainsPypylon" in _read_script("export-windows-update.ps1")
    assert "Test-PythonWheelBundleContainsDeploymentRuntime" in _read_script(
        "export-windows-update.ps1"
    )
    assert "Start-Process" in _read_script("export-windows-update.ps1")
    assert "RedirectStandardError" in _read_script("export-windows-update.ps1")
    assert "pip" in _read_script("export-windows-update.ps1")
    assert "download" in _read_script("export-windows-update.ps1")
    assert "python-snap7>=3,<4" in _read_script("export-windows-update.ps1")
    assert "pypylon>=3.0.0,<27" in _read_script("export-windows-update.ps1")
    assert "pypylon-.*\\.whl" in _read_script("export-windows-update.ps1")
    assert "deployment-requirements.txt" in _read_script("export-windows-update.ps1")
    assert "mmdeploy==1.3.1" in _read_script("export-windows-update.ps1")
    assert "tensorrt-cu12-bindings==10.3.0" in _read_script("export-windows-update.ps1")
    assert "onnxruntime==1.19.0" in _read_script("export-windows-update.ps1")
    assert "python[-_]snap7" in _read_script("export-windows-update.ps1")
    assert "mmdeploy-.*\\.whl" in _read_script("export-windows-update.ps1")
    assert "tensorrt_cu12_bindings-.*\\.whl" in _read_script("export-windows-update.ps1")
    assert "manylinux2014_x86_64" in _read_script("export-windows-update.ps1")
    assert "manylinux_2_17_x86_64" in _read_script("export-windows-update.ps1")
    assert "manylinux_2_27_x86_64" in _read_script("export-windows-update.ps1")
    assert "manylinux_2_31_x86_64" in _read_script("export-windows-update.ps1")
    assert "/usr/src/tensorrt/bin/trtexec" in _read_script("export-windows-update.ps1")
    assert "tritonserver/bin/trtexec" in _read_script("export-windows-update.ps1")
    assert "libnvinfer_builder_resource.so*" in _read_script("export-windows-update.ps1")
    assert "libnvinfer*.so*" in _read_script("export-windows-update.ps1")
    assert "gstreamer-packages-" in _read_script("export-windows-update.ps1")
    assert (
        '$ManagedRuntimeDistroName = [string](Get-ConfigValue -Name "ManagedRuntimeDistroName" -DefaultValue "YBB-Inferenzserver-Builder")'
        in _read_script("export-windows-update.ps1")
    )
    assert "$GStreamerDebBundleDistroName = $ManagedRuntimeDistroName" in _read_script(
        "export-windows-update.ps1"
    )
    assert "-ForwardOutputAsHost" in _read_script("export-windows-update.ps1")
    assert "-PackageListPath" in _read_script("export-windows-update.ps1")
    assert '$arguments += "-PackageNames"' not in _read_script("export-windows-update.ps1")
    assert "gstreamer-debs-v4-" in _read_script("export-windows-installer.ps1")
    assert "runtime/python-wheels.tar.gz" in _read_script("export-windows-installer.ps1")
    assert "PythonWheelBundlePath" in _read_script("export-windows-installer.ps1")
    assert "New-PythonWheelBundleForExport" in _read_script("export-windows-installer.ps1")
    assert "Test-PythonWheelBundleContainsSnap7" in _read_script("export-windows-installer.ps1")
    assert "Test-PythonWheelBundleContainsPypylon" in _read_script("export-windows-installer.ps1")
    assert "Test-PythonWheelBundleContainsDeploymentRuntime" in _read_script(
        "export-windows-installer.ps1"
    )
    assert "Start-Process" in _read_script("export-windows-installer.ps1")
    assert "RedirectStandardError" in _read_script("export-windows-installer.ps1")
    assert "pip" in _read_script("export-windows-installer.ps1")
    assert "download" in _read_script("export-windows-installer.ps1")
    assert "python-snap7>=3,<4" in _read_script("export-windows-installer.ps1")
    assert "pypylon>=3.0.0,<27" in _read_script("export-windows-installer.ps1")
    assert "pypylon-.*\\.whl" in _read_script("export-windows-installer.ps1")
    assert "deployment-requirements.txt" in _read_script("export-windows-installer.ps1")
    assert "mmdeploy==1.3.1" in _read_script("export-windows-installer.ps1")
    assert "tensorrt-cu12-bindings==10.3.0" in _read_script("export-windows-installer.ps1")
    assert "onnxruntime==1.19.0" in _read_script("export-windows-installer.ps1")
    assert "python[-_]snap7" in _read_script("export-windows-installer.ps1")
    assert "mmdeploy-.*\\.whl" in _read_script("export-windows-installer.ps1")
    assert "tensorrt_cu12_bindings-.*\\.whl" in _read_script("export-windows-installer.ps1")
    assert "manylinux2014_x86_64" in _read_script("export-windows-installer.ps1")
    assert "manylinux_2_17_x86_64" in _read_script("export-windows-installer.ps1")
    assert "manylinux_2_27_x86_64" in _read_script("export-windows-installer.ps1")
    assert "manylinux_2_31_x86_64" in _read_script("export-windows-installer.ps1")
    assert "gstreamer-packages-" in _read_script("export-windows-installer.ps1")
    assert (
        '$ManagedRuntimeDistroName = [string](Get-ConfigValue -Name "ManagedRuntimeDistroName" -DefaultValue "YBB-Inferenzserver-Builder")'
        in _read_script("export-windows-installer.ps1")
    )
    assert "$GStreamerDebBundleDistroName = $ManagedRuntimeDistroName" in _read_script(
        "export-windows-installer.ps1"
    )
    assert "-ForwardOutputAsHost" in _read_script("export-windows-installer.ps1")
    assert "-PackageListPath" in _read_script("export-windows-installer.ps1")
    assert '$arguments += "-PackageNames"' not in _read_script("export-windows-installer.ps1")
    assert 'Write-Host ("FEHLER: {0}" -f $_.Exception.Message)' in _read_script(
        "export-windows-update.ps1"
    )
    assert "Resolve-MediaMtxArchiveForBuild" in build_content
    assert "New-MediaMtxArchiveFromRelease" in build_content
    assert "mediamtx_${tag}_windows_amd64.zip" in build_content
    assert "Expand-Archive" in build_content
    assert "MediaMtxArchivePath = $mediaMtxArchivePath" in build_content
    assert "$gStreamerDebBundleDistroName = $managedRuntimeDistroName" in build_content
    assert "MediaMtxSkipped" in operations_content
    assert "MediaMtxHostPlatform" in operations_content
    assert "StreamingHlsPort" in operations_content
    assert "StreamingWebRtcIceUdpPort" in operations_content
    assert "StreamingWebRtcIceTcpPort" in operations_content
    assert "StreamingTurnTcpPort" in operations_content
    assert "WslNetworkingMode" in operations_content
    assert "WslHyperVFirewallDefaultInboundAction" in operations_content
    assert "WSL2 mirrored networking will be applied" in operations_content
    assert "GStreamer Debian package bundle is missing from the manifest" in operations_content
    assert "GStreamerDebBundleSha256" in operations_content
    assert "Get-Command git.exe, git -ErrorAction SilentlyContinue" in build_content


def test_windows_portproxy_defaults_to_local_and_explicit_lan_address() -> None:
    content = _read_script("configure-windows-portproxy.ps1")
    mediamtx_content = _read_script("install-windows-mediamtx.ps1")

    assert "[switch]$AllHostAddresses" in content
    assert "[switch]$WildcardListenAddress" in content
    assert "[switch]$SkipHttpRedirectPort" in content
    assert "[int]$ListenPort = 443" in content
    assert "[int]$ConnectPort = 443" in content
    assert "[string[]]$AdditionalTcpPorts = @()" in content
    assert "[string[]]$AdditionalUdpPorts = @()" in content
    assert '[string]$WslNetworkingMode = ""' in content
    assert "[string[]]$RemainingArguments = @()" in content
    assert "$additionalTcpPortTokens = @($AdditionalTcpPorts + $RemainingArguments)" in content
    assert "Normalize-WslNetworkingMode" in content
    assert "WSL Mirrored Networking ist aktiv" in content
    assert "netsh portproxy wird fuer WSL-Webports uebersprungen" in content
    assert "Remove-PortProxyEndpointRules" in content
    assert 'Remove-PortProxyRule -Mode "v4tov6"' in content
    assert 'Remove-PortProxyRule -Mode "v6tov6"' in content
    assert "Reset-WindowsPortProxyState" in content
    assert "Test-IpHelperOwnsTcpPorts" in content
    assert "netsh interface portproxy reset" in content
    assert "Restart-IpHelperServiceForMirroredNetworking" in content
    assert (
        "Starte Windows-Dienst iphlpsvc neu, damit geloeschte Portproxy-Listener freigegeben werden."
        in content
    )
    assert content.index('if ($normalizedWslNetworkingMode -eq "mirrored")') < content.index(
        "if (-not $NoStartServices)"
    )
    assert "ConvertTo-TcpPortList" in content
    assert "ConvertTo-UdpPortList" in content
    assert '([string]$value -split "[,;\\s]+")' in content
    assert '$listenAddresses = @("127.0.0.1")' in content
    assert '$listenAddresses = @("0.0.0.0")' in content
    assert "$listenAddresses += $RequestedListenAddress" in content
    assert "$useWildcardListenAddress = $WildcardListenAddress" in content
    assert "elseif ($UseAllHostAddresses)" in content
    assert "$lanListenAddresses = @($plan.LanListenAddresses" in content
    assert "LocalAddress = $lanListenAddresses" in content
    assert "$plan.LanListenAddresses.Count" not in content
    assert "Set-PortProxyEndpoint" in content
    assert "Set-UdpFirewallEndpoint" in content
    assert "EndpointListenPort 80" in content
    assert "EndpointConnectPort 80" in content
    assert "$AdditionalTcpPorts" in content
    assert "$AdditionalUdpPorts" in content
    assert "YBB-Inferenzserver TCP $additionalPort" in content
    assert "YBB-Inferenzserver UDP $additionalPort" in content
    assert "Portproxy fuer zusaetzlichen TCP-Port" in content
    assert "UDP-ICE fuer WebRTC nutzt UDP/$EndpointListenPort direkt" in content
    assert "netsh portproxy kann UDP nicht weiterleiten" in content
    assert "Firewall-Regel nicht noetig" in content
    assert "Portproxy lauscht per Wildcard" in content
    assert "ybb-start-platform" in content
    assert "Start-WslKeepalive" in content
    assert "exec sleep infinity" in content
    assert "Remove-ObsoletePortProxyRules" in content
    assert "Entferne veraltete Portproxy-Regel" in content
    assert "-WildcardListenAddress" in _read_script("install-windows-offline.ps1")
    assert "-AdditionalTcpPorts" in _read_script("install-windows-offline.ps1")
    assert "-WslNetworkingMode" in _read_script("install-windows-offline.ps1")
    assert "$AdditionalTcpPorts.Count" not in _read_script("install-windows-offline.ps1")
    assert "$additionalTcpPortsList.Count" in _read_script("install-windows-offline.ps1")
    assert '$additionalTcpPortsArgument = $additionalTcpPortsList -join ","' in _read_script(
        "install-windows-offline.ps1"
    )
    assert (
        '$proxyArguments += @("-AdditionalTcpPorts", $additionalTcpPortsArgument)'
        in _read_script("install-windows-offline.ps1")
    )
    assert "$additionalUdpPortsList.Count" in _read_script("install-windows-offline.ps1")
    assert '$additionalUdpPortsArgument = $additionalUdpPortsList -join ","' in _read_script(
        "install-windows-offline.ps1"
    )
    assert (
        '$proxyArguments += @("-AdditionalUdpPorts", $additionalUdpPortsArgument)'
        in _read_script("install-windows-offline.ps1")
    )
    assert "$streamingTcpPorts = @($streamingTurnTcpPort)" in _read_script(
        "install-windows-offline.ps1"
    )
    assert "$streamingUdpPorts = @()" in _read_script("install-windows-offline.ps1")
    assert "$proxyArguments += @($additionalTcpPortsList | ForEach-Object" not in _read_script(
        "install-windows-offline.ps1"
    )
    assert "StreamingRtspPort" in _read_script("install-windows-offline.ps1")
    assert "StreamingWebRtcPort" in _read_script("install-windows-offline.ps1")
    assert "StreamingWebRtcIceTcpPort" in _read_script("install-windows-offline.ps1")
    assert 'return "0.0.0.0"' in _read_script("install-windows-offline.ps1")
    assert "-AdditionalTcpPorts" in _read_script("update-windows-offline.ps1")
    assert "-WslNetworkingMode" in _read_script("update-windows-offline.ps1")
    assert "$AdditionalTcpPorts.Count" not in _read_script("update-windows-offline.ps1")
    assert "$additionalTcpPortsList.Count" in _read_script("update-windows-offline.ps1")
    assert '$additionalTcpPortsArgument = $additionalTcpPortsList -join ","' in _read_script(
        "update-windows-offline.ps1"
    )
    assert (
        '$proxyArguments += @("-AdditionalTcpPorts", $additionalTcpPortsArgument)'
        in _read_script("update-windows-offline.ps1")
    )
    assert "$additionalUdpPortsList.Count" in _read_script("update-windows-offline.ps1")
    assert '$additionalUdpPortsArgument = $additionalUdpPortsList -join ","' in _read_script(
        "update-windows-offline.ps1"
    )
    assert (
        '$proxyArguments += @("-AdditionalUdpPorts", $additionalUdpPortsArgument)'
        in _read_script("update-windows-offline.ps1")
    )
    assert "$streamingTcpPorts = @($streamingTurnTcpPort)" in _read_script(
        "update-windows-offline.ps1"
    )
    assert "$streamingUdpPorts = @()" in _read_script("update-windows-offline.ps1")
    assert "$proxyArguments += @($additionalTcpPortsList | ForEach-Object" not in _read_script(
        "update-windows-offline.ps1"
    )
    assert "Get-ExistingAppHostName" in _read_script("update-windows-offline.ps1")
    assert 'return "0.0.0.0"' in _read_script("update-windows-offline.ps1")
    assert "-WildcardListenAddress" in _read_script("windows-deployment-ui.ps1")
    assert "$tcpPortValues = [string[]]@($TcpPorts" in mediamtx_content
    assert "-LocalPort $tcpPortValues" in mediamtx_content
    assert "-Program $ProgramPath" in mediamtx_content
    assert "$udpPortValues = [string[]]@($UdpPorts" in mediamtx_content
    assert "YBB-Inferenzserver MediaMTX Program TCP" in mediamtx_content
    assert "YBB-Inferenzserver MediaMTX Program UDP" not in mediamtx_content
    assert "-ProgramPath $paths.Exe" in mediamtx_content
    assert '-LocalPort (@($TcpPorts | Sort-Object -Unique) -join ",")' not in mediamtx_content
    assert "Get-OrCreate-MediaMtxApiCredentials" in mediamtx_content
    assert "mediamtx-client.env" in mediamtx_content
    assert "MEDIAMTX_API_USER" in mediamtx_content
    assert "MEDIAMTX_API_PASSWORD" in mediamtx_content
    assert "TURN_AUTH_SECRET" in mediamtx_content
    assert "webrtcICEServers2:" in mediamtx_content
    assert "username: AUTH_SECRET" in mediamtx_content
    assert "?transport=tcp" in mediamtx_content
    assert "turn:${turnPublicHost}:${TurnTcpPort}?transport=tcp" in mediamtx_content
    assert "$TurnTcpPort?transport" not in mediamtx_content
    assert "authInternalUsers:" in mediamtx_content
    assert "      - action: api" in mediamtx_content
    assert "hls: no" in mediamtx_content
    start_case = mediamtx_content.split('"Start" {', maxsplit=1)[1].split('"Run" {', maxsplit=1)[0]
    assert "Write-MediaMtxConfig" in start_case
    assert "Write-MediaMtxClientEnv" in start_case
    assert "resolvedWebRtcIceUdpPort" not in mediamtx_content
    assert "resolvedWebRtcIceTcpPort" in mediamtx_content
    assert "logDestinations: [stdout, file]" in mediamtx_content
    assert 'logFile: `"$normalizedLogFile`"' in mediamtx_content
    assert 'Join-Path $paths.LogsDir "mediamtx.log"' in mediamtx_content
    assert "RedirectStandardOutput" not in mediamtx_content
    assert "RedirectStandardError" not in mediamtx_content
    assert "Get-MediaMtxClientCredentials" in _read_script("install-windows-offline.ps1")
    assert "Get-MediaMtxClientCredentials" in _read_script("update-windows-offline.ps1")
    assert "--mediamtx-api-user" in _read_script("install-windows-offline.ps1")
    assert "--mediamtx-api-password" in _read_script("install-windows-offline.ps1")
    assert "--mediamtx-api-user" in _read_script("update-windows-offline.ps1")
    assert "--mediamtx-api-password" in _read_script("update-windows-offline.ps1")
    assert "--turn-auth-secret" in _read_script("install-windows-offline.ps1")
    assert "--turn-auth-secret" in _read_script("update-windows-offline.ps1")


def test_windows_mediamtx_keepalive_checks_real_service_health() -> None:
    content = _read_script("install-windows-mediamtx.ps1")

    assert (
        "[Parameter(Mandatory = $true)][AllowEmptyString()][string]$RequestedPublicAddress"
        in content
    )
    assert "function Test-MediaMtxHealth" in content
    assert "Invoke-WebRequest `" in content
    assert "http://127.0.0.1:9997/v3/paths/list" in content
    assert "$consecutiveHealthFailures -ge 3" in content
    assert "Stop-MediaMtxProcess -ConfigPath $Paths.Config" in content
    assert "webrtcLocalUDPAddress: :$resolvedWebRtcIceTcpPort" in content
    assert "function Test-UdpPortListener" not in content
    assert "Get-NetUDPEndpoint" not in content
    assert "WebRtcIceUdpPort" not in content
    assert "Repair-MediaMtxFirewallRules" in content
    assert "-EdgeTraversalPolicy Allow" in content
    assert '-UserId "SYSTEM"' in content
    assert "-LogonType ServiceAccount" in content
    assert "New-ScheduledTaskTrigger -AtStartup" in content
    start_case = content.split('    "Start" {', 1)[1].split('    "Run" {', 1)[0]
    assert "Register-MediaMtxTask -ResolvedAppDir $resolvedAppDir -Name $TaskName" in start_case
    assert "$managedExePath" in content
    assert "[string]$_.ExecutablePath" in content
    assert "da die YBB-Konfiguration nicht geladen wurde" in content
    assert "Start-Process -WindowStyle Hidden -FilePath $Paths.Exe -ArgumentList @($Paths.Config)" in content


def test_wsl_release_waits_for_reverse_proxy_backends() -> None:
    content = _read_wsl_script("apply-release.sh")

    assert "wait_runtime_ports 300" in content
    assert "validate_reverse_proxy_routes 120" in content
    assert (
        "Reverse-Proxy-Routen konnten nach dem Update nicht vollstaendig validiert werden"
        in content
    )


def test_wsl_release_is_activated_only_after_successful_migrations() -> None:
    content = _read_wsl_script("apply-release.sh")

    assert 'cd "$RELEASE_DIR"' in content
    assert 'PYTHONPATH="$(build_pythonpath "$RELEASE_DIR")"' in content
    assert (
        "validate_service_runtime_imports\n"
        "validate_nvenc_stream_encoder\n"
        "run_migrations\n"
        "activate_release"
    ) in content
    assert content.index("release-candidate-service-imports-ok") < content.index("activate_release")
    assert content.rindex("validate_nvenc_stream_encoder\nrun_migrations") < content.rindex(
        "activate_release"
    )
    assert "wait_runtime_ports 300\nvalidate_mediamtx_streaming" in content
    assert "run_migrations\nactivate_release\ninstall_systemd_units" in content
    extract_body = content.split("extract_release() {", maxsplit=1)[1].split(
        "activate_release() {",
        maxsplit=1,
    )[0]
    assert 'ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"' not in extract_body
    assert "read_bundle_host_name" in content
    assert "is_local_public_authority" in content
    assert '"AppHostName"' in content
    assert "(443|8443)" in content
    assert "Caddy HTTP:80" in content
    assert "Admin-API:8080" in content
    assert "Runtime-API:8090" in content
    assert "MediaMTX API:9997" in content
    assert "validate_mediamtx_streaming" in content
    assert "mediamtx_api_url_for_wsl" in content
    assert "MEDIAMTX_API_USER" in content
    assert "MEDIAMTX_API_PASSWORD" in content
    assert "CV_PLATFORM__RUNTIME__STREAM_MEDIAMTX_API_URL=$mediamtx_api_url" in content
    assert 'curl -fsS "${mediamtx_api_url}/v3/paths/list"' in content
    assert "timeout 45s gst-launch-1.0" in content
    assert "num-buffers=675" in content
    assert "cudaconvert" in content
    assert "video/x-raw(memory:CUDAMemory),format=NV12" in content
    assert "MediaMTX NVENC/RTSP Publish/Read-Smoke-Test erfolgreich" in content
    assert "keinen CPU-Fallback" in content
    assert content.count("preset=p1 tune=ultra-low-latency zero-reorder-delay=true") >= 2
    assert "zerolatency/default-preset" not in content
    assert "NVENC-GStreamer-Smoke-Test erfolgreich: low-latency/zerolatency." not in content
    assert "GST_DEBUG=2 gst-launch-1.0" in content
    assert "NVENC-Diagnose (letzte maximal 60 stderr-Zeilen)" in content
    assert 'nvenc_last_error="$(tail -n 60 "$smoke_stderr"' in content
    assert "Label Studio:8100" in content
    assert "MLflow:5000" in content
    assert "Camera-Media:8102" in content
    assert "MediaMTX RTSP:${stream_rtsp_port}" in content
    assert "MediaMTX WHEP:${stream_webrtc_port}" in content
    assert "MediaMTX ICE TCP:${stream_webrtc_ice_tcp_port}" in content
    assert "TURN TCP:${stream_turn_tcp_port}" in content
    assert "turnserver_ready()" in content
    assert "systemctl show ybb-turnserver.service --property=MainPID --value" in content
    assert "log_startup_diagnostics" in content
    assert "journalctl" in content
    assert "handle /static/*" in content
    assert "handle /react-app/*" in content
    assert "handle /data/local-files*" in content
    assert "handle /label-studio/data/local-files*" in content
    assert "handle /label-studio/ {" in content
    assert "handle_path /label-studio/*" in content
    assert "handle_path /stream-webrtc/*" in content
    assert "reverse_proxy ${stream_media_host}:${stream_webrtc_port}" in content
    assert "header_down Location ^/ /stream-webrtc/" in content
    assert "handle_path /stream-hls/*" not in content
    assert "stream_hls_port" not in content
    assert "@frontend_assets path /frontend/assets/*" in content
    assert "root * /opt/ybb-inferenzserver/current/frontend/apps/admin-web/dist/assets" in content
    assert "Frontend-Asset wird nicht korrekt ausgeliefert" in content
    assert "/stream-webrtc/__ybb_reverse_proxy_probe__/whep" in content
    assert "wird vom Admin-Frontend beantwortet statt von MediaMTX" in content
    assert "/stream-webrtc/.../whep kann MediaMTX nicht erreichen" in content
    assert "/stream-hls/" not in content
    assert "Label-Studio-Asset wird nicht korrekt ausgeliefert" in content
    assert "Reverse-Proxy-Routen validiert" in content
    assert "handle_path /frontend/*" in content
    assert "try_files {path} =404" in content
    assert (
        "handle {\n    root * /opt/ybb-inferenzserver/current/frontend/apps/admin-web/dist"
        in content
    )
    assert "redir / /frontend/ 308" in content
    assert "redir https://{host}{uri} 308" in content
    assert "CV_PLATFORM_SERVICE_HEALTH_HOST=127.0.0.1" in content
    assert "CV_PLATFORM_SERVICE_HEALTH_EXCLUDE=operations-agent" in content
    assert "CV_PLATFORM__LABEL_STUDIO__LOCAL_FILES_URL_PREFIX=/data/local-files/?d=" in content
    assert (
        'data.setdefault("label_studio", {})["local_files_url_prefix"] = "/data/local-files/?d="'
        in content
    )
    assert "Label-Studio-Local-Files-Route liefert keine Capture-Dateien aus" in content
    assert "Optionale Label-Studio-Local-Files-Route liefert keine Probe-Datei" in content
    assert "/data/local-files/:required" in content
    assert "/label-studio/data/local-files/:optional" in content
    assert 'chmod 755 "$STATE_ROOT" "$(dirname "$local_file_probe_path")"' in content
    assert 'chmod 644 "$local_file_probe_path"' in content
    assert "normalize_label_studio_capture_permissions" in content
    assert 'chmod 755 "$STATE_ROOT" "$STATE_ROOT/runtime"' in content
    assert 'find "$captures_dir" -type d -exec chmod 755 {} +' in content
    assert 'find "$captures_dir" -type f -exec chmod 644 {} +' in content
    assert ("OPENCV_FFMPEG_CAPTURE_OPTIONS='rtsp_transport;tcp'") in content
    assert "ybb-rtsp-probe" in content
    assert "RTSP-Quelle konnte aus der WSL-Runtime nicht geoeffnet werden" in content


def test_wsl_runtime_includes_optional_aravis_rtsp_bridge() -> None:
    prepare_content = _read_wsl_script("prepare-runtime.sh")
    apply_content = _read_wsl_script("apply-release.sh")
    bridge_content = (DEPLOYMENT_DIR / "wsl" / "aravis-rtsp-bridge.py").read_text(encoding="utf-8")

    assert "libaravis-0.8-0" in prepare_content
    assert "aravis-tools-cli" in prepare_content
    assert "gir1.2-gst-rtsp-server-1.0" in prepare_content
    assert "gstreamer1.0-rtsp" in prepare_content
    assert "gstreamer1.0-plugins-bad" in prepare_content
    assert "gstreamer1.0-plugins-ugly" in prepare_content
    assert "gst-inspect-1.0 aravissrc" in prepare_content
    assert "gst-inspect-1.0 appsrc" in prepare_content
    assert "gst-inspect-1.0 h264parse" in prepare_content
    assert "gst-inspect-1.0 rtspclientsink" in prepare_content
    assert "gst-inspect-1.0 nvcudah264enc" in prepare_content
    assert "gst-inspect-1.0 nvh264enc" not in prepare_content
    assert "gst-inspect-1.0 cudaupload" in prepare_content
    assert "gst-inspect-1.0 x264enc" not in prepare_content
    assert "gst-inspect-1.0 cudaconvert" in prepare_content
    assert "/usr/bin/python3" in apply_content
    assert 'gi.require_version("Gst", "1.0")' in apply_content
    assert "aravis-rtsp-bridge-imports-ok" in prepare_content

    assert "ybb-aravis-rtsp-bridge" in apply_content
    assert "ybb-aravis-rtsp@.service" in apply_content
    assert "ybb-mediamtx.service" in apply_content
    assert "install_gstreamer_deb_bundle_from_bundle" in apply_content
    assert "runtime/gstreamer-debs.tar.gz" in apply_content
    assert (
        'export LD_LIBRARY_PATH="$service_library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"'
        in apply_content
    )
    assert "install_python_wheel_bundle_from_bundle" in apply_content
    assert "install_python_wheels_from_dir" in apply_content
    assert "runtime/python-wheels.tar.gz" in apply_content
    assert "python-snap7" in apply_content
    assert "pypylon" in apply_content
    assert "validate_basler_pypylon_package" in apply_content
    assert "runtime/python-wheels.tar.gz muss ein pypylon-*.whl enthalten" in apply_content
    assert (
        'install_python_wheel_bundle_from_bundle "$PYTHON_BIN" "requirements.txt" "Service-Runtime"'
        in apply_content
    )
    assert 'validate_basler_pypylon_package "$PYTHON_BIN"' in apply_content
    assert (
        'install_python_wheel_bundle_from_bundle "$TRAINING_PYTHON_BIN" "deployment-requirements.txt" "Training-/Deployment-Runtime"'
        in apply_content
    )
    assert "validate_deployment_packaging_runtime" in apply_content
    assert "deployment-packaging-runtime-imports-ok" in apply_content
    assert "_load_mmdeploy_tensorrt_api" in apply_content
    assert "_find_trtexec_binary" in apply_content
    assert "CV_PLATFORM_TRTEXEC_BIN=$trtexec_bin" in apply_content
    assert "tensorrt_bindings" in apply_content
    assert "/opt/tritonserver/external-libs" in apply_content
    assert "CV_PLATFORM__RUNTIME__PLC_WRITES_ALLOWED=$plc_writes_allowed" in apply_content
    assert "PlcWritesAllowed false" in apply_content
    assert "uv pip install --python" in apply_content
    assert "ensurepip --upgrade" in apply_content
    assert "Das Debian-Paketbundle fehlt oder enthaelt noch keinen TURN-Server" in apply_content
    assert "Dir::Etc::sourcelist=/dev/null" in apply_content
    assert 'units=(ybb-mediamtx.service "${units[@]}")' in apply_content
    assert "public_authority_host" in apply_content
    assert "windows_host_for_wsl" in apply_content
    assert "wsl_networking_mode" in apply_content
    assert "is_wsl_mirrored_networking" in apply_content
    assert "YBB_WSL_NETWORKING_MODE=$effective_wsl_networking_mode" in apply_content
    assert "WSL Networking Mode:" in apply_content
    assert "mediamtx_public_host_for_wsl" in apply_content
    assert 'host="$(mediamtx_public_host_for_wsl)"' in apply_content
    assert 'host="${1:-}"' in apply_content
    assert 'host="127.0.0.1"' in apply_content
    assert 'stream_media_host="127.0.0.1"' in apply_content
    assert 'mediamtx_api_url="http://${stream_media_host}:9997"' in apply_content
    assert "YBB_MEDIAMTX_HOST_PLATFORM=$(mediamtx_host_platform)" in apply_content
    assert 'APP_HOST_NAME="$HOST_NAME"' in apply_content
    assert "CV_PLATFORM__RUNTIME__STREAM_RTSP_PUBLISH_HOST=$stream_media_host" in apply_content
    assert "stream_webrtc_ice_tcp_port" in apply_content
    assert "webrtcLocalUDPAddress: :$stream_webrtc_ice_tcp_port" in apply_content
    assert "rtspTransports: [tcp]" in apply_content
    assert "hls: no" in apply_content
    assert "webrtcLocalTCPAddress: :$stream_webrtc_ice_tcp_port" in apply_content
    assert "webrtcIPsFromInterfaces: false" in apply_content
    assert "webrtcAdditionalHosts:" in apply_content
    assert '  - "$stream_public_host"' in apply_content
    assert "webrtcICEServers2:" in apply_content
    assert "ybb-turnserver.service" in apply_content
    assert "static-auth-secret=$TURN_AUTH_SECRET" in apply_content
    assert "systemctl mask coturn.service" in apply_content
    assert "systemctl mask turnserver.service" in apply_content
    assert "Conflicts=coturn.service turnserver.service" in apply_content
    assert 'grep -Fq "pid=${main_pid},"' in apply_content
    assert "webrtcIPsFromInterfaces: false" in (
        DEPLOYMENT_DIR.parent / "infra" / "mediamtx" / "mediamtx.yml"
    ).read_text(encoding="utf-8")
    mediamtx_content = (DEPLOYMENT_DIR.parent / "infra" / "mediamtx" / "mediamtx.yml").read_text(
        encoding="utf-8"
    )
    assert 'webrtcLocalUDPAddress: ""' in mediamtx_content
    assert "rtspTransports: [tcp]" in mediamtx_content
    assert "hls: no" in mediamtx_content
    assert "webrtcLocalTCPAddress: :8189" in mediamtx_content
    assert "StreamAuthEnabled false" in apply_content
    assert (
        "StreamAuthEnabled=true ist im aktuellen Offline-Streaming-Bundle nicht unterstuetzt"
        in apply_content
    )
    assert (
        "MediaMTX Streams sind standardmaessig ohne Autorisierung im LAN erreichbar"
        in apply_content
    )
    assert "flir-blackfly.env.example" in apply_content
    assert "ARAVIS_FEATURES=PixelFormat=BayerRG8" in apply_content
    assert "ybb-aravis-rtsp@*.service" in apply_content
    assert "$1 !~ /@\\.service$/" in apply_content
    assert "storage-maintenance.sh" in apply_content
    assert "ybb-storage-maintenance.timer" in apply_content
    assert "clear-result-history.sh" in apply_content
    assert "ybb-result-retention.timer" in apply_content
    assert "maintenance_worker.result_retention" in apply_content
    assert "SystemMaxUse=2G" in apply_content
    assert "MaxRetentionSec=14day" in apply_content

    assert "GstRtspServer.RTSPServer" in bridge_content
    assert "aravissrc" in bridge_content
    assert "bayer2rgb ! videoconvert" in bridge_content
    assert "rtph264pay name=pay0" in bridge_content


def test_wsl_control_worker_uses_training_runtime_for_deployment_packaging() -> None:
    content = _read_wsl_script("apply-release.sh")

    assert "control_worker.worker|training_worker.worker" in content
    assert (
        'if [[ "$needs_training_runtime" == "true" && -n "${YBB_TRAINING_PYTHON_BIN:-}" ]]; then'
        in content
    )
    assert 'python_bin="$YBB_TRAINING_PYTHON_BIN"' in content
    assert 'if [[ "$needs_training_runtime" == "true" ]]; then' in content


def test_wsl_inference_gateway_caps_native_cpu_thread_pools() -> None:
    content = _read_wsl_script("apply-release.sh")

    assert "inference_gateway.*)" in content
    assert "YBB_INFERENCE_GATEWAY_NATIVE_THREADS:-1" in content
    assert 'export OPENBLAS_NUM_THREADS="$inference_native_threads"' in content
    assert 'export OMP_NUM_THREADS="$inference_native_threads"' in content
    assert 'export MKL_NUM_THREADS="$inference_native_threads"' in content
    assert 'export OPENCV_FOR_THREADS_NUM="$inference_native_threads"' in content
    assert "YBB_INFERENCE_GATEWAY_OMP_WAIT_POLICY:-PASSIVE" in content
    assert "YBB_INFERENCE_GATEWAY_KMP_BLOCKTIME:-0" in content


def test_wsl_label_studio_service_is_offline_friendly() -> None:
    content = _read_wsl_script("apply-release.sh")

    assert "ybb-label-studio-service" in content
    assert "ybb-start-platform" in content
    assert "ybb-keepalive" in content
    assert "ybb-platform.keepalive.lock" in content
    assert "start_systemd_units" in content
    assert "/usr/local/bin/ybb-start-platform || true" in content
    assert 'public_label_studio_path="/label-studio"' in content
    assert 'public_label_studio_url="${public_origin}${public_label_studio_path}"' in content
    assert 'export HOST="$public_label_studio_url"' in content
    assert 'export LABEL_STUDIO_HOST="$public_label_studio_url"' in content
    assert 'export FRONTEND_HOSTNAME="$public_label_studio_url"' in content
    assert "export DOMAIN_FROM_REQUEST=False" in content
    assert "export DOMAIN_FROM_REQUEST=True" not in content
    assert (
        'export CSRF_TRUSTED_ORIGINS="$public_origin,http://${APP_HOST_NAME},https://127.0.0.1,http://127.0.0.1,https://localhost,http://localhost"'
        in content
    )
    assert "export COLLECT_ANALYTICS=False" in content
    assert "export LATEST_VERSION_CHECK=False" in content
    assert "label_studio_python" in content
    assert 'find_spec("label_studio")' in content
    assert 'text.replace("LATEST_VERSION_CHECK = True", "LATEST_VERSION_CHECK = False")' in content
    assert "Hostname path prefix is set" in content
    assert "FORCE_SCRIPT_NAME = HOSTNAME" in content
    assert "export SENTRY_DSN=" in content
    assert 'exec "$YBB_LABEL_STUDIO_BIN" start --port 8100 --no-browser' in content
    assert "--host 127.0.0.1 --port 8100" not in content


def test_admin_operational_probes_support_wsl_localhost_targets() -> None:
    content = ADMIN_OPERATIONAL_PROBES.read_text(encoding="utf-8")

    assert "CV_PLATFORM_SERVICE_HEALTH_HOST" in content
    assert "CV_PLATFORM_SERVICE_HEALTH_TARGETS_JSON" in content
    assert "CV_PLATFORM_SERVICE_HEALTH_EXCLUDE" in content
    assert '"runtime-admin-api": 8090' in content
    assert '"operations-agent": 8101' in content


def test_windows_deployment_ui_wrapper_is_buildable() -> None:
    ui_content = _read_script("windows-deployment-ui.ps1")
    builder_content = _read_script("build-windows-ui-exe.ps1")

    assert "System.Windows.Forms" in ui_content
    assert "Installer exportieren" in ui_content
    assert "Installieren" in ui_content
    assert "Update einspielen" in ui_content
    assert "[string]$Host" not in ui_content
    assert "Join-PublicAddress -AddressHost" in ui_content
    assert "[AllowEmptyString()][string]$Text" in ui_content
    assert "[Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text" in ui_content
    assert "Vorgang gestartet. Prozess-ID" in ui_content
    assert "Set-UiStatus" in ui_content
    assert "Lauf-Logdatei" in ui_content
    assert "Append-NewProcessLog" in ui_content
    assert "Read-TextFileShared" in ui_content
    assert "Write-SharedLogText" in ui_content
    assert "[System.IO.FileShare]::ReadWrite" in ui_content
    assert "Add-Content -LiteralPath $DestinationPath" not in ui_content
    assert "Initialize-CrashLog" in ui_content
    assert "Resolve-DeploymentScriptPath" in ui_content
    assert "IP/Host optional" in ui_content
    assert '"-PublicAddress", $publicAddress' in ui_content
    assert '[Alias("AppHostName", "HostName")]' in _read_script("apply-windows-update.ps1")
    assert "Position = [int64]0" in ui_content
    assert ".GetNewClosure()" in ui_content
    assert "Tee-Object" not in ui_content
    assert "*>>" not in ui_content
    assert "$childArgs" in ui_content
    assert "Start-Process -FilePath $powerShell" in ui_content
    assert "Read-TextFileShared -Path $logPath" in ui_content
    assert "fehlgeschlagen\\. Exit Code: [1-9]" in ui_content
    assert "try { $process.Kill() } catch { }" in ui_content
    assert "RedirectStandardOutput $stdoutPath" in ui_content
    assert "RedirectStandardError $stderrPath" in ui_content
    assert '"-ListenAddress", $listenHost' in ui_content
    assert '"-ConnectPort", $connectPort' in ui_content
    assert '"-AdditionalTcpPorts", "8554,8889,8189"' not in ui_content
    assert '"-AdditionalUdpPorts", "8189"' not in ui_content
    assert "YBB-Deployment-UI.exe" in builder_content
    assert "VITE_FRONTEND_BASE_PATH = $FrontendBasePath" in _read_script(
        "export-windows-installer.ps1"
    )
    assert "VITE_FRONTEND_BASE_PATH = $FrontendBasePath" in _read_script(
        "export-windows-update.ps1"
    )
    assert '$FrontendBasePath = "/frontend/"' in ui_content


def test_persistent_updater_supports_incoming_bundle_archive_flow() -> None:
    content = _read_script("apply-windows-update.ps1")

    assert "Find-LatestIncomingBundle" in content
    assert "Expand-UpdateBundleForBootstrap" in content
    assert "Verwende Update-Skript aus dem neuen Bundle" in content
    assert '"*-windows-update.zip"' in content
    assert "Move-ProcessedIncomingBundle" in content
    assert '"deployed"' in content
    assert '"failed"' in content


def test_windows_bundle_extraction_avoids_expand_archive_cleanup_bug() -> None:
    for script_name in (
        "apply-windows-update.ps1",
        "update-windows-offline.ps1",
        "install-windows-offline.ps1",
    ):
        content = _read_script(script_name)

        assert "function Expand-ZipArchiveSafely" in content
        assert "[System.IO.Compression.ZipFile]::OpenRead" in content
        assert "$inputStream.CopyTo($outputStream)" in content
        assert "Expand-Archive" not in content


def test_persistent_updater_verifies_the_installed_app_version() -> None:
    content = _read_script("apply-windows-update.ps1")

    assert "$bundleArgument = $bootstrapBundleRoot" in content
    assert "function Assert-InstalledAppVersion" in content
    assert '"install-state.json"' in content
    assert "Update meldete Erfolg, aber installiert ist Version" in content


def test_wsl_storage_maintenance_bounds_releases_logs_and_free_blocks() -> None:
    content = _read_wsl_script("storage-maintenance.sh")

    assert 'RELEASE_KEEP_COUNT="${YBB_RELEASE_KEEP_COUNT:-3}"' in content
    assert "maintenance_worker.scheduled" in content
    assert "journalctl --vacuum-time=14d --vacuum-size=2G" in content
    assert "fstrim -av" in content
    assert "--report-only" in content


def test_wsl_result_history_cleanup_requires_explicit_confirmation() -> None:
    content = _read_wsl_script("clear-result-history.sh")

    assert "CONFIRMED=false" in content
    assert 'if [[ "$CONFIRMED" != "true" ]]' in content
    assert "TRUNCATE TABLE runtime_connector_deliveries, result_records" in content
    assert "UPDATE runtime_executions SET last_result_id = NULL" in content


def test_wsl_service_pythonpaths_include_cv_edge_and_smoke_test_its_import() -> None:
    apply_content = _read_wsl_script("apply-release.sh")
    prepare_content = _read_wsl_script("prepare-runtime.sh")

    for content in (apply_content, prepare_content):
        assert '"backend/packages/cv-edge/src"' in content
        assert "import cv_edge" in content

    assert "validate_service_runtime_imports" in apply_content
    assert "release-candidate-service-imports-ok" in apply_content
    assert "service-runtime-imports-ok" in prepare_content


def test_minimum_principle_update_is_offline_complete_and_fail_closed_by_default() -> None:
    for script_name in ("export-windows-installer.ps1", "export-windows-update.ps1"):
        content = _read_script(script_name)
        assert '"numpy>=1.26,<2"' in content
        assert '"opencv-python-headless>=4.10.0,<5"' in content
        assert 'node_type="analysis.stem_horizontal_curvature"' in content
        assert 'node_type="preprocessing.process_coordinate_rectify"' in content
        assert 'node_type="analysis.stem_measurement_eligibility"' in content
        assert "def calculate_image_view_profile" in content
        assert "def view_overlay_geometry_to_source" in content
        assert "def _dataset_image_view_metadata" in content
        assert "Actual Label Studio / model input" in content
        assert "export function commissioningPreviewTargetNodeKey" in content
        assert "preferCameraStreamFrame: fullResolutionCalibrationSourceSelected" in content
        assert "def _capture_calibration_frame_from_stream_ring" in content
        assert "No current canonical pipeline image output" in content
        assert "_source_overlay_from_image_view" in content
        assert 'node_type="analysis.stem_surface_phase"' in content
        assert 'node_type="analysis.stem_curvature_phase_solver"' in content
        assert "class HighRateStemPhaseBridge" in content
        assert "PHASE_CAMERA_RECONNECT_RESET" in content
        assert '"slug": "template-realtime-stem-curvature-minimum"' in content
        assert '"shadow_mode_enabled": False' in content
        assert '"stem_orientation_command_enabled": False' in content
        assert "stem_shadow_store.py" in content
        assert "stem-shadow-evaluation" in content
        assert "stem-curvature-minimum-production-runbook.md" in content

        realtime_check = next(
            line
            for line in content.splitlines()
            if 'realtime-executor\\src\\realtime_executor\\live.py"; Tokens' in line
        )
        assert '"analysis.stem_horizontal_curvature"' in realtime_check
        assert '"analysis.stem_surface_phase"' in realtime_check
        assert '"analysis.stem_curvature_phase_solver"' in realtime_check
        assert "STEM_CAPTURE_MULTIPLE_STEMS_IN_VIEW" in content
        assert "Wartende Stämme außerhalb" not in content

    apply_content = _read_wsl_script("apply-release.sh")
    prepare_content = _read_wsl_script("prepare-runtime.sh")
    for content in (apply_content, prepare_content):
        assert "import cv2" in content
        assert "import numpy" in content
    assert '"analysis.stem_horizontal_curvature"' in apply_content
    assert '"analysis.stem_measurement_eligibility"' in apply_content
    assert "calculate_image_view_profile" in apply_content
    assert "restore_view_mask" in apply_content
    assert "view_overlay_geometry_to_source" in apply_content
    assert "No current canonical pipeline image output" in apply_content
    assert "export function commissioningPreviewTargetNodeKey" in apply_content
    assert "def _capture_calibration_frame_from_stream_ring" in apply_content
    assert '"analysis.stem_surface_phase"' in apply_content
    assert '"analysis.stem_curvature_phase_solver"' in apply_content
    assert '"slug": "template-realtime-stem-curvature-minimum"' in apply_content
    assert '"stem_orientation_command_enabled": False' in apply_content
    assert "stem-shadow-evaluation" in apply_content
