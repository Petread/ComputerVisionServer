from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.usefixtures("edge_system_package_cache")

ROOT = Path(__file__).resolve().parents[3]
WINDOWS_GIT_OPENSSL = Path(r"C:\Program Files\Git\usr\bin\openssl.exe")
OPENSSL = shutil.which("openssl") or (
    str(WINDOWS_GIT_OPENSSL) if WINDOWS_GIT_OPENSSL.is_file() else None
)


def _builder() -> ModuleType:
    path = ROOT / "scripts/release/build_edge_offline_bundle.py"
    spec = importlib.util.spec_from_file_location("edge_bundle_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel(path: Path, distribution: str) -> None:
    normalized = distribution.replace("-", "_")
    with zipfile.ZipFile(path / f"{normalized}-1.0.0-py3-none-any.whl", "w") as archive:
        kind = "services" if distribution == "edge-agent" else "packages"
        source = ROOT / "backend" / kind / distribution / "src"
        if distribution in {
            "cv-status", "cv-contracts", "cv-utils", "cv-edge", "cv-runtime", "edge-agent"
        }:
            for item in sorted(source.rglob("*.py")):
                archive.writestr(item.relative_to(source).as_posix(), item.read_bytes())
        else:
            archive.writestr(f"{normalized}/__init__.py", "__version__ = '1.0.0'\n")
        archive.writestr(
            f"{normalized}-1.0.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 1.0.0\n",
        )


@pytest.mark.parametrize("change", ["outdated", "missing", "extra", "duplicate"])
def test_bundle_rejects_cached_internal_code_that_differs_from_installer_checkout(
    tmp_path: Path, change: str
) -> None:
    builder = _builder()
    config_path = _config(tmp_path, "jetson_orin_nano")
    wheels = tmp_path / "wheels"
    wheel = next(wheels.glob("cv_contracts-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    key = "cv_contracts/edge.py"
    if change == "outdated":
        # Same package version, but the schema shipped before system_packages existed.
        contents[key] = contents[key].replace(
            b"    system_packages: dict[str, str] = Field(default_factory=dict, max_length=201)\n",
            b"",
        ).replace(
            b"    system_packages: dict[str, str] = "
            b"Field(default_factory=dict, max_length=201)\r\n",
            b"",
        )
    elif change == "missing":
        del contents[key]
    elif change == "extra":
        contents["cv_contracts/obsolete.py"] = b"old = True\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
        if change == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr(key, contents[key])
    with pytest.raises(builder.BundleError, match="cv-contracts.*Online-Inhalte vorbereiten"):
        builder.build(config_path, tmp_path / "output")
    assert not list((tmp_path / "output").glob("*.tar.gz"))


def test_internal_wheel_source_comparison_accepts_crlf_and_bom(tmp_path: Path) -> None:
    builder = _builder()
    _config(tmp_path, "jetson_orin_nano")
    wheels = tmp_path / "wheels"
    wheel = next(wheels.glob("cv_contracts-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in contents.items():
            if name.endswith(".py"):
                text = content.decode("utf-8-sig").replace("\r\n", "\n")
                content = ("\ufeff" + text.replace("\n", "\r\n")).encode("utf-8")
            archive.writestr(name, content)
    builder._validate_internal_wheel_sources(wheels)


def _config(tmp_path: Path, platform: str) -> Path:
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir(exist_ok=True)
    required = [
        "edge-agent",
        "cv-status",
        "cv-contracts",
        "cv-runtime",
        "cv-edge",
        "cv-utils",
        "python-snap7",
        "fastapi",
        "uvicorn",
        "numpy",
        "pillow",
        "cuda-python",
        "pypylon",
    ]
    if platform == "windows_11":
        required.extend(
            [
                "opencv-python-headless",
                "tensorrt-cu13",
                "tensorrt-cu13-bindings",
                "tensorrt-cu13-libs",
            ]
        )
    else:
        required.append("uvloop")
    for distribution in required:
        _wheel(wheelhouse, distribution)
    config: dict[str, object] = {
        "target_platform": platform,
        "release_version": "2.0.0-e2",
        "source_date_epoch": 1704067200,
        "wheelhouse_directory": str(wheelhouse),
        "build_internal_wheels": False,
        "device_hostname": "edge-e2-01",
        "device_ipv4": "192.168.50.21",
        "server_ipv4": "192.168.50.10",
        "camera_adapters": "rtsp,usb_opencv,gstreamer",
    }
    if platform == "windows_11":
        winsw = tmp_path / "winsw.exe"
        winsw.write_bytes(b"fixture-winsw")
        runtime = tmp_path / "python-runtime.zip"
        with zipfile.ZipFile(runtime, "w") as archive:
            archive.writestr("python.exe", b"fixture-python")
        config["winsw_executable"] = str(winsw)
        config["python_runtime_archive"] = str(runtime)
        cuda_installer = tmp_path / "cuda-runtime-installer.exe"
        cuda_installer.write_bytes(b"fixture-cuda")
        config["cuda_installer"] = str(cuda_installer)
        native_installer = tmp_path / "YBB-Edge-Windows-Installer.exe"
        native_installer.write_bytes(b"fixture-native-installer")
        config["windows_installer_executable"] = str(native_installer)
    path = tmp_path / f"{platform}.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@pytest.mark.parametrize("platform", ["jetson_orin_nano", "jetson_agx_orin", "windows_11"])
def test_offline_bundle_is_reproducible_and_contains_mandatory_integrity_files(
    tmp_path: Path, platform: str
) -> None:
    builder = _builder()
    config = _config(tmp_path, platform)
    first = builder.build(config, tmp_path / "first")
    second = builder.build(config, tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    if platform == "windows_11":
        companion = first.with_name(first.stem + "-installer.exe")
        assert companion.read_bytes() == b"fixture-native-installer"

    extracted = tmp_path / "extracted"
    if first.suffix == ".zip":
        with zipfile.ZipFile(first) as archive:
            archive.extractall(extracted)
    else:
        with tarfile.open(first, "r:gz") as archive:
            assert archive.getmember("deployment/edge/Install-JetsonOfflineUI.sh").mode == 0o755
            assert archive.getmember("deployment/edge/install-jetson-offline-ui.py").mode == 0o755
            archive.extractall(extracted, filter="data")
    assert (extracted / "SHA256SUMS").is_file()
    assert (extracted / "release-manifest.json").is_file()
    for settings_file in (
        "edge-settings-helper.py", "edge_environment.py",
        "ybb-edge-settings.service", "ybb-edge-settings.timer",
    ):
        assert (extracted / "deployment/edge" / settings_file).is_file()
    assert (extracted / "deployment/edge/edge_environment.py").read_bytes() == (
        ROOT / "backend/packages/cv-contracts/src/cv_contracts/edge_environment.py"
    ).read_bytes()
    assert (extracted / "SIGNATURE_POLICY").read_text(encoding="ascii").strip() == "prepared"
    environment = (extracted / "device.env").read_text(encoding="utf-8")
    assert "YBB_EDGE_MANAGEMENT_IP=192.168.50.21\n" in environment
    assert "YBB_EDGE_SERVER_IP=192.168.50.10\n" in environment
    assert "YBB_EDGE_PLANT_ALLOWED_IPS=\n" in environment
    assert "YBB_ALLOW_PLC_WRITES=false\n" in environment
    assert "YBB_EDGE_OUTBOX_QUOTA_BYTES=67108864\n" in environment
    assert "YBB_EDGE_OUTBOX_MAX_ITEMS=10000\n" in environment
    if platform == "windows_11":
        assert (extracted / "cuda-runtime-installer.exe").is_file()
        assert not (extracted / "system-packages").exists()
        assert (extracted / "YBB-Edge-Windows-Installer.exe").is_file()
        assert (extracted / "deployment/edge/WindowsReleaseSignatureVerifier.cs").is_file()
        runtime_requirements = (extracted / "runtime-requirements.txt").read_text(
            encoding="utf-8"
        )
        assert "opencv-python-headless\n" in runtime_requirements
        assert "tensorrt-cu13==10.16.1.11\n" in runtime_requirements
        wheel_names = {path.name for path in (extracted / "wheelhouse").glob("*.whl")}
        assert any(name.startswith("tensorrt_cu13-") for name in wheel_names)
        assert any(name.startswith("tensorrt_cu13_bindings-") for name in wheel_names)
        assert any(name.startswith("tensorrt_cu13_libs-") for name in wheel_names)
    else:
        assert (extracted / "deployment/edge/Install-JetsonOfflineUI.sh").is_file()
        assert (extracted / "deployment/edge/edge-system-packages.py").is_file()
        assert (extracted / "system-packages/ufw_0.36.2-6_all.deb").is_file()
        assert (extracted / "system-packages/Packages").is_file()
        assert (extracted / "deployment/edge/install-jetson-offline-ui.py").is_file()
        installer = (extracted / "deployment/edge/install-jetson-offline.sh").read_text(
            encoding="utf-8"
        )
        assert "for device_group in video render" in installer
        assert 'usermod -aG "${device_group}" "${SERVICE_USER}"' in installer
    builder.verify(extracted)


def test_bundle_separates_trigger_client_allowlist_from_pipeline_write_gate(
    tmp_path: Path,
) -> None:
    builder = _builder()
    config_path = _config(tmp_path, "jetson_orin_nano")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["plant_allowed_ipv4s"] = "192.168.50.40"
    config["allow_plc_writes"] = True
    config_path.write_text(json.dumps(config), encoding="utf-8")
    bundle = builder.build(config_path, tmp_path / "output")
    extracted = tmp_path / "extracted"
    with tarfile.open(bundle, "r:gz") as archive:
        archive.extractall(extracted, filter="data")
    environment = (extracted / "device.env").read_text(encoding="utf-8")
    assert "YBB_EDGE_PLANT_ALLOWED_IPS=192.168.50.40\n" in environment
    assert "YBB_ALLOW_PLC_WRITES=true\n" in environment


def test_checksum_verification_rejects_modified_payload(tmp_path: Path) -> None:
    builder = _builder()
    bundle = builder.build(_config(tmp_path, "jetson_orin_nano"), tmp_path / "output")
    extracted = tmp_path / "extracted"
    with tarfile.open(bundle, "r:gz") as archive:
        archive.extractall(extracted, filter="data")
    (extracted / "device.env").write_text("tampered=true\n", encoding="utf-8")
    with pytest.raises(builder.BundleError, match="Checksum mismatch"):
        builder.verify(extracted)


def test_windows_bundle_requires_runtime_and_service_wrapper(tmp_path: Path) -> None:
    builder = _builder()
    config_path = _config(tmp_path, "windows_11")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["winsw_executable"] = str(tmp_path / "missing-winsw.exe")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(builder.BundleError, match="winsw_executable"):
        builder.build(config_path, tmp_path / "output")


def test_prepare_wheelhouse_downloads_for_selected_platform_without_touching_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _builder()
    wheelhouse = tmp_path / "prepared" / "windows_11"
    config = {
        "target_platform": "windows_11",
        "wheelhouse_directory": str(wheelhouse),
        "package_index_url": "https://packages.example.invalid/simple",
        "python_executable": "python",
        "runtime_distributions": "edge-agent",
    }
    config_path = tmp_path / "prepare.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    def fake_internal_wheels(
        _config_value: dict[str, object], destination: Path, *, build_isolation: bool
    ) -> None:
        assert build_isolation is True
        for distribution in (
            "cv-status",
            "cv-contracts",
            "cv-utils",
            "cv-runtime",
            "cv-edge",
            "edge-agent",
        ):
            _wheel(destination, distribution)

    observed: list[str] = []

    def fake_download(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(command)
        destination = Path(command[command.index("--dest") + 1])
        for distribution in (
            "python-snap7",
            "fastapi",
            "uvicorn",
            "numpy",
            "pillow",
            "opencv-python-headless",
            "cuda-python",
            "pypylon",
        ):
            _wheel(destination, distribution)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder, "_build_internal_wheels", fake_internal_wheels)
    monkeypatch.setattr(builder.subprocess, "run", fake_download)
    monkeypatch.setattr(
        builder,
        "prepare_windows_payloads",
        lambda *_args, **_kwargs: (tmp_path / "python-runtime.zip", tmp_path / "winsw.exe"),
    )
    monkeypatch.setattr(
        builder,
        "_add_windows_inference_wheels",
        lambda destination, *_args, **_kwargs: [
            _wheel(destination, distribution)
            for distribution in (
                "tensorrt-cu13",
                "tensorrt-cu13-bindings",
                "tensorrt-cu13-libs",
            )
        ],
    )

    result = builder.prepare_wheelhouse(config_path)

    assert result == wheelhouse.resolve()
    assert (result / "wheelhouse-source.lock.json").is_file()
    assert {builder._normalized_distribution(path.name) for path in result.glob("*.whl")} >= {
        "edge-agent",
        "cv-contracts",
        "cv-runtime",
        "cv-edge",
        "cv-utils",
        "python-snap7",
        "fastapi",
        "uvicorn",
        "numpy",
        "pillow",
        "opencv-python-headless",
        "cuda-python",
        "pypylon",
        "tensorrt-cu13",
        "tensorrt-cu13-bindings",
        "tensorrt-cu13-libs",
    }
    assert observed[observed.index("--platform") + 1] == "win_amd64"
    assert observed[observed.index("--python-version") + 1] == "312"
    assert "--only-binary=:all:" in observed


def test_jetson_target_marker_dependencies_are_requested_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _builder()
    wheelhouse = tmp_path / "prepared" / "jetson_orin_nano"
    config = {
        "target_platform": "jetson_orin_nano",
        "wheelhouse_directory": str(wheelhouse),
        "package_index_url": "https://packages.example.invalid/simple",
        "python_executable": "python",
        "runtime_distributions": "edge-agent",
    }
    config_path = tmp_path / "prepare-jetson.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    def fake_internal_wheels(
        _config_value: dict[str, object], destination: Path, *, build_isolation: bool
    ) -> None:
        assert build_isolation is True
        for distribution in (
            "cv-status",
            "cv-contracts",
            "cv-utils",
            "cv-runtime",
            "cv-edge",
            "edge-agent",
        ):
            _wheel(destination, distribution)

    observed: list[str] = []

    def fake_download(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(command)
        destination = Path(command[command.index("--dest") + 1])
        for distribution in (
            "python-snap7",
            "fastapi",
            "uvicorn",
            "uvloop",
            "numpy",
            "pillow",
            "cuda-python",
            "pypylon",
        ):
            _wheel(destination, distribution)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder, "_build_internal_wheels", fake_internal_wheels)
    monkeypatch.setattr(builder.subprocess, "run", fake_download)

    result = builder.prepare_wheelhouse(config_path)

    assert result == wheelhouse.resolve()
    assert "uvloop" in observed
    assert observed[observed.index("--platform") + 1] == "manylinux_2_31_aarch64"
    assert any(path.name.startswith("uvloop-") for path in result.glob("*.whl"))


def test_prepare_wheelhouse_reuses_locked_dependencies_and_refreshes_internal_wheels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _builder()
    config_path = _config(tmp_path, "jetson_orin_nano")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    wheelhouse = Path(config["wheelhouse_directory"])
    (wheelhouse / "wheelhouse-source.lock.json").write_text(
        json.dumps(
            {
                "schema_version": "ybb.edge.wheelhouse-source.v1",
                "platform": "jetson_orin_nano",
                "python_tag": "312",
                "python_abi": "cp312",
                "package_index": "https://pypi.org/simple",
                "wheels": [
                    {"file_name": wheel.name, "sha256": builder._sha256(wheel)}
                    for wheel in sorted(wheelhouse.glob("*.whl"))
                ],
            }
        ),
        encoding="utf-8",
    )

    refreshed: list[Path] = []
    monkeypatch.setattr(
        builder,
        "_refresh_internal_wheels",
        lambda _config_value, destination: refreshed.append(destination),
    )

    assert builder.prepare_wheelhouse(config_path) == wheelhouse.resolve()
    assert refreshed == [wheelhouse.resolve()]
    builder._validate_prepared_wheelhouse(
        wheelhouse, builder._json(ROOT / "deployment/edge/profiles/jetson-orin-nano.json"),
        config, "jetson_orin_nano",
    )


@pytest.mark.parametrize("platform", ["jetson_orin_nano", "jetson_agx_orin"])
def test_basler_supplement_downloads_aarch64_binary_without_rebuilding_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    builder = _builder()
    config_path = _config(tmp_path, platform)
    config = builder._json(config_path)
    profile = builder._json(ROOT / "deployment/edge/profiles" / builder.PROFILE_FILES[platform])
    directory = Path(config["wheelhouse_directory"])
    (directory / "pypylon-1.0.0-py3-none-any.whl").unlink()
    original = {p.name: p.read_bytes() for p in directory.glob("*.whl")}
    commands = []

    def download(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert "--no-deps" in command and "--only-binary=:all:" in command
        assert "manylinux_2_31_aarch64" in command and "abi3" in command
        _wheel(Path(command[command.index("--dest") + 1]), "pypylon")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder.subprocess, "run", download)
    builder._ensure_basler_wheel(config, profile, directory)
    builder._ensure_basler_wheel(config, profile, directory)
    assert len(commands) == 1
    assert all((directory / name).read_bytes() == content for name, content in original.items())
    builder._validate_wheelhouse(directory, profile, config)


def test_prepare_windows_payloads_downloads_pinned_verified_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _builder()
    runtime_tree = tmp_path / "runtime-source" / "python"
    (runtime_tree / "Lib" / "venv").mkdir(parents=True)
    (runtime_tree / "Lib" / "ensurepip").mkdir(parents=True)
    (runtime_tree / "python.exe").write_bytes(b"fixture-python")
    (runtime_tree / "Lib" / "venv" / "__init__.py").write_text("", encoding="utf-8")
    (runtime_tree / "Lib" / "ensurepip" / "__init__.py").write_text("", encoding="utf-8")
    runtime_source = tmp_path / "runtime.tar.gz"
    with tarfile.open(runtime_source, "w:gz") as archive:
        archive.add(runtime_tree, arcname="python")
    winsw_source = tmp_path / "WinSW-x64.exe"
    winsw_source.write_bytes(b"fixture-winsw")
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps(
            {
                "schema_version": "ybb.edge.windows-payload-sources.v2",
                "python_runtime": {
                    "version": "3.12.fixture",
                    "python_version": "3.12",
                    "url": "https://downloads.example.invalid/runtime.tar.gz",
                    "sha256": builder._sha256(runtime_source),
                    "archive_format": "tar.gz",
                },
                "winsw": {
                    "version": "2.12.0",
                    "url": "https://downloads.example.invalid/winsw.exe",
                    "sha256": builder._sha256(winsw_source),
                },
                "cuda_toolkit": {
                    "version": "13.2.fixture",
                    "profile_version": "13.2",
                    "file_name": "cuda.exe",
                    "url": "https://downloads.example.invalid/cuda.exe",
                    "sha256": builder._sha256(winsw_source),
                },
                "tensorrt": {
                    "version": "10.16.fixture",
                    "frontend": {
                        "file_name": "tensorrt.tar.gz",
                        "wheel_file_name": "tensorrt_cu13-10.16.fixture-py3-none-any.whl",
                        "url": "https://downloads.example.invalid/tensorrt.tar.gz",
                        "sha256": builder._sha256(runtime_source),
                    },
                    "runtime": {
                        "file_name": "tensorrt_cu13_libs-10.16.fixture-py3-none-win_amd64.whl",
                        "url": "https://downloads.example.invalid/tensorrt-libs.whl",
                        "sha256": builder._sha256(winsw_source),
                    },
                    "bindings": {
                        "file_name": (
                            "tensorrt_cu13_bindings-10.16.fixture-cp312-none-win_amd64.whl"
                        ),
                        "url": "https://downloads.example.invalid/tensorrt-bindings.whl",
                        "sha256": builder._sha256(winsw_source),
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    runtime_output = tmp_path / "prepared" / "python-runtime.zip"
    winsw_output = tmp_path / "prepared" / "winsw.exe"
    config = {
        "python_runtime_archive": str(runtime_output),
        "winsw_executable": str(winsw_output),
        "cuda_installer": str(tmp_path / "prepared" / "cuda.exe"),
        "tensorrt_frontend_source": str(tmp_path / "prepared" / "tensorrt.tar.gz"),
        "tensorrt_frontend_wheel": str(
            tmp_path / "prepared" / "tensorrt_cu13-10.16.fixture-py3-none-any.whl"
        ),
        "tensorrt_runtime_wheel": str(
            tmp_path / "prepared" / "tensorrt_cu13_libs-10.16.fixture-py3-none-win_amd64.whl"
        ),
        "tensorrt_bindings_wheel": str(
            tmp_path / "prepared" / "tensorrt_cu13_bindings-10.16.fixture-cp312-none-win_amd64.whl"
        ),
    }
    profile = {"python_tag": "312", "cuda_version": "13.2", "tensorrt_version": "10.16"}
    downloads = {
        "https://downloads.example.invalid/runtime.tar.gz": runtime_source,
        "https://downloads.example.invalid/winsw.exe": winsw_source,
        "https://downloads.example.invalid/cuda.exe": winsw_source,
        "https://downloads.example.invalid/tensorrt.tar.gz": runtime_source,
        "https://downloads.example.invalid/tensorrt-libs.whl": winsw_source,
        "https://downloads.example.invalid/tensorrt-bindings.whl": winsw_source,
    }
    observed: list[str] = []

    def fake_download(source: dict[str, object], destination: Path) -> None:
        url = str(source["url"])
        observed.append(url)
        shutil.copyfile(downloads[url], destination)
        assert builder._sha256(destination) == source["sha256"]

    def fake_runtime_validation(root: Path, expected_version: str) -> None:
        assert expected_version == "3.12"
        assert (root / "python" / "python.exe").is_file()
        assert (root / "python" / "Lib" / "venv" / "__init__.py").is_file()

    monkeypatch.setattr(builder, "_download_verified", fake_download)
    monkeypatch.setattr(builder, "_validate_windows_runtime_tree", fake_runtime_validation)

    def fake_frontend_wheel(
        _config_value: dict[str, object], _source: Path, destination: Path
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _wheel(destination.parent, "tensorrt-cu13")
        generated = next(destination.parent.glob("tensorrt_cu13-1.0.0-*.whl"))
        generated.replace(destination)

    monkeypatch.setattr(builder, "_build_tensorrt_frontend_wheel", fake_frontend_wheel)

    assert builder.prepare_windows_payloads(config, profile, sources_path=sources_path) == (
        runtime_output.resolve(),
        winsw_output.resolve(),
    )
    assert len(observed) == 6
    assert winsw_output.read_bytes() == b"fixture-winsw"
    with zipfile.ZipFile(runtime_output) as archive:
        assert "python/python.exe" in archive.namelist()
        assert "python/Lib/venv/__init__.py" in archive.namelist()
    lock = json.loads(
        (runtime_output.parent / "windows-payload-source.lock.json").read_text(encoding="utf-8")
    )
    assert lock["python_runtime_source_sha256"] == builder._sha256(runtime_source)
    assert lock["winsw_sha256"] == builder._sha256(winsw_output)
    assert lock["cuda_installer_sha256"] == builder._sha256(Path(config["cuda_installer"]))

    observed.clear()
    builder.prepare_windows_payloads(config, profile, sources_path=sources_path)
    assert observed == []


def test_prepare_wheelhouse_rejects_nonempty_target_and_unsafe_index(tmp_path: Path) -> None:
    builder = _builder()
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    (wheelhouse / "existing.txt").write_text("do not overwrite", encoding="utf-8")
    config_path = tmp_path / "prepare.json"
    config_path.write_text(
        json.dumps(
            {
                "target_platform": "jetson_orin_nano",
                "wheelhouse_directory": str(wheelhouse),
                "package_index_url": "https://user:secret@example.invalid/simple",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(builder.BundleError, match="not empty"):
        builder.prepare_wheelhouse(config_path)

    shutil.rmtree(wheelhouse)
    with pytest.raises(builder.BundleError, match="must not contain credentials"):
        builder.prepare_wheelhouse(config_path)


@pytest.mark.skipif(OPENSSL is None, reason="OpenSSL is not installed")
def test_signed_bundle_requires_an_external_pinned_trust_anchor(tmp_path: Path) -> None:
    builder = _builder()
    openssl = OPENSSL
    assert openssl is not None
    private_key = tmp_path / "release-private.pem"
    public_key = tmp_path / "release-public.pem"
    subprocess.run(
        [
            openssl,
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            private_key,
        ],
        check=True,
    )
    subprocess.run(
        [openssl, "pkey", "-in", private_key, "-pubout", "-out", public_key],
        check=True,
    )
    config_path = _config(tmp_path, "jetson_orin_nano")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(
        {
            "require_signature": True,
            "signing_private_key": str(private_key),
            "signing_public_key": str(public_key),
            "openssl_executable": openssl,
        }
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")
    bundle = builder.build(config_path, tmp_path / "signed")
    extracted = tmp_path / "signed-extracted"
    with tarfile.open(bundle, "r:gz") as archive:
        archive.extractall(extracted, filter="data")
    assert (extracted / "SHA256SUMS.sig").is_file()
    with pytest.raises(builder.BundleError, match="pinned trusted public key"):
        builder.verify(extracted, openssl)
    builder.verify(extracted, openssl, public_key)
