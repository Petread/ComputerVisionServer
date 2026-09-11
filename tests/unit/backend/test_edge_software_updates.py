from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from cv_contracts.edge import EdgeSoftwareRelease
from cv_edge.software import canonical_manifest, verify_software_package

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("required", "installed", "matches"),
    [
        ("13.2", "13.2", True),
        ("13.2", "13.2.2", True),
        ("10.16", "10.16.2.10", True),
        ("10.16.2", "10.16.2.10", True),
        ("10.16.2.10", "10.16.2.10", True),
        ("13.2", "13.20.2", False),
        ("13.2", "13.3.0", False),
        ("13.2", "12.2.2", False),
        ("13.2.2", "13.2.3", False),
        ("13.2.2", "13.2", False),
        ("10.16.2.10", "10.16.2.11", False),
        ("13.2", None, False),
        ("13.2", "", False),
        ("13.2", "13.2-unknown", False),
        ("13.2", "unknown", False),
        ("13", "13.2.2", False),
        ("invalid", "invalid", False),
        (None, None, True),
        ("", "13.2.2", True),
    ],
)
def test_runtime_platform_version_precision(
    required: str | None, installed: str | None, matches: bool
) -> None:
    from cv_edge.software import runtime_platform_version_matches

    assert runtime_platform_version_matches(required, installed) is matches


def launcher_class() -> type:
    spec = importlib.util.spec_from_file_location(
        "edge_launcher_test", ROOT / "deployment/edge/edge-runtime-launcher.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RuntimeLauncher


@pytest.mark.parametrize(
    ("cuda", "tensorrt", "error"),
    [
        ("13.2", "10.16", None),
        ("13.2.2", "10.16.2.10", None),
        ("13.3.0", "10.16.2.10", "cuda_version"),
        ("13.2.2", "10.17.0", "tensorrt_version"),
        ("13.2.2", "", "tensorrt_version"),
    ],
)
def test_launcher_checks_runtime_platform_version_precision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cuda: str,
    tensorrt: str,
    error: str | None,
) -> None:
    launcher = launcher_class()(tmp_path)
    release = EdgeSoftwareRelease(
        release_id="platform-test",
        platform="jetson_orin_nano",
        architecture="aarch64",
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        cuda_version="13.2",
        tensorrt_version="10.16",
        runtime_code_digest="c" * 64,
        wheels={"example.whl": "a" * 64},
    )
    package_bytes = io.BytesIO()
    with zipfile.ZipFile(package_bytes, "w") as archive:
        archive.writestr("fixture", b"runtime")
    content = package_bytes.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    artifact = tmp_path / "artifacts/sha256" / digest[:2] / digest
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(content)
    # A prepared slot avoids installation; this test exercises the compatibility
    # gate before reuse. Signature verification is covered by the signer tests.
    slot = launcher.release_root(digest)
    slot.mkdir(parents=True)
    (slot / "ready.json").write_text(release.model_dump_json())
    monkeypatch.setitem(
        launcher.prepare.__globals__, "verify_software_package", lambda *args: release
    )
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=4 * 1024**3))
    monkeypatch.setenv("YBB_EDGE_PLATFORM", "jetson_orin_nano")
    monkeypatch.setenv("YBB_EDGE_ARCHITECTURE", "aarch64")
    monkeypatch.setenv("YBB_EDGE_CUDA_VERSION", cuda)
    monkeypatch.setenv("YBB_EDGE_TENSORRT_VERSION", tensorrt)
    if error:
        with pytest.raises(ValueError, match=error):
            launcher.prepare(digest)
    else:
        assert launcher.prepare(digest) == release.runtime_code_digest


def test_failed_activation_restores_previous_runtime_and_preserves_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = launcher_class()(tmp_path)
    calibration = tmp_path / "calibration.json"
    calibration.write_text('{"camera": "retained"}')
    outbox = tmp_path / "outbox.bin"
    outbox.write_bytes(b"pending-results")
    started = []
    monkeypatch.setattr(launcher, "prepare", lambda digest: "c" * 64)
    monkeypatch.setattr(launcher, "stop", lambda: None)
    monkeypatch.setattr(launcher, "start", lambda: started.append(launcher.state["active_release"]))

    def fail_health(*args: object) -> None:
        raise RuntimeError("Candidate failed health check")

    monkeypatch.setattr(launcher, "healthy", fail_health)
    launcher.apply({"request_id": "update-001", "action": "update", "package_sha256": "a" * 64})
    assert started == ["a" * 64, "bootstrap"]
    assert launcher.state["active_release"] == "bootstrap"
    assert launcher.state["phase"] == "failed"
    assert "health check" in launcher.state["last_error"]
    assert calibration.read_text() == '{"camera": "retained"}'
    assert outbox.read_bytes() == b"pending-results"


def test_successful_runtime_update_and_manual_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = launcher_class()(tmp_path)
    monkeypatch.setattr(launcher, "prepare", lambda digest: "c" * 64)
    monkeypatch.setattr(launcher, "stop", lambda: None)
    monkeypatch.setattr(launcher, "start", lambda: None)
    monkeypatch.setattr(launcher, "healthy", lambda *args: None)
    launcher.apply({"request_id": "update-001", "action": "update", "package_sha256": "a" * 64})
    assert launcher.state["phase"] == "succeeded"
    assert launcher.state["previous_release"] == "bootstrap"
    launcher.apply({"request_id": "rollback-001", "action": "rollback"})
    assert launcher.state["phase"] == "rolled_back"
    assert launcher.state["active_release"] == "bootstrap"
    assert launcher.state["previous_release"] == "a" * 64


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_dependency_failure_reaches_status_before_runtime_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    launcher = launcher_class()(tmp_path)
    conflict = "example 2.0 requires dependency<2, but you have dependency 3.0."
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, **{"stdout": "", "stderr": "", stream: conflict}
        ),
    )
    monkeypatch.setattr(launcher, "prepare", launcher.check_dependencies)
    monkeypatch.setattr(launcher, "stop", lambda: pytest.fail("Active runtime was stopped"))
    monkeypatch.setattr(launcher, "start", lambda: pytest.fail("Candidate runtime was started"))
    launcher.apply(
        {"request_id": "dependency-check", "action": "update", "package_sha256": "a" * 64}
    )
    assert launcher.state["phase"] == "failed"
    assert launcher.state["active_release"] == "bootstrap"
    assert conflict in launcher.state["last_error"]
    assert conflict in json.loads((launcher.root / "status.json").read_text())["last_error"]


def test_interrupted_activation_recovers_confirmed_runtime_without_replaying_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = launcher_class()(tmp_path)
    launcher.save(
        active_release="a" * 64,
        previous_release="bootstrap",
        phase="checking",
        request_id="update-001",
    )
    requests = tmp_path / "software/requests/update-001.json"
    requests.write_text(
        json.dumps({"request_id": "update-001", "action": "update", "package_sha256": "a" * 64})
    )
    recovered = launcher_class()(tmp_path)
    started = []

    def start() -> None:
        started.append(recovered.state["active_release"])
        recovered.stopping = True

    monkeypatch.setattr(recovered, "start", start)
    monkeypatch.setattr(recovered, "stop", lambda: None)
    recovered.run()
    assert started == ["bootstrap"]
    assert recovered.state["phase"] == "failed"


def test_signed_package_rejects_tampering_and_undeclared_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = shutil.which("openssl")
    if executable is None:
        candidate = Path("C:/Program Files/Git/usr/bin/openssl.exe")
        if not candidate.exists():
            pytest.skip("OpenSSL is required for release signature integration tests")
        executable = str(candidate)
        monkeypatch.setenv("PATH", str(candidate.parent) + ";" + os.environ["PATH"])
    private = tmp_path / "private.pem"
    public = tmp_path / "public.pem"
    subprocess.run(
        [
            executable,
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [executable, "pkey", "-in", str(private), "-pubout", "-out", str(public)],
        check=True,
        capture_output=True,
    )
    wheel_name = "example-1.0-py3-none-any.whl"
    wheel_bytes = b"immutable signed wheel"
    release = EdgeSoftwareRelease(
        release_id="release-001",
        platform="windows_11",
        architecture="x86_64",
        python_version="3.12",
        runtime_code_digest="c" * 64,
        wheels={wheel_name: hashlib.sha256(wheel_bytes).hexdigest()},
    )
    manifest = tmp_path / "release.json"
    manifest.write_bytes(canonical_manifest(release))
    signature = tmp_path / "release.sig"
    subprocess.run(
        [
            executable,
            "dgst",
            "-sha256",
            "-sign",
            str(private),
            "-out",
            str(signature),
            str(manifest),
        ],
        check=True,
        capture_output=True,
    )
    package = tmp_path / "runtime.zip"

    def write_package(content: bytes, extra: bool = False) -> None:
        with zipfile.ZipFile(package, "w") as archive:
            archive.write(manifest, "release.json")
            archive.write(signature, "release.sig")
            archive.writestr(f"wheelhouse/{wheel_name}", content)
            if extra:
                archive.writestr("../escape", b"invalid")

    write_package(wheel_bytes)
    assert verify_software_package(package, public).release_id == "release-001"
    with pytest.raises(ValueError, match="public key is missing"):
        verify_software_package(package, tmp_path / "unconfigured-public.pem")
    invalid_key = tmp_path / "invalid-public.pem"
    invalid_key.write_text("not a public key")
    with pytest.raises(ValueError, match="signature could not be verified"):
        verify_software_package(package, invalid_key)
    original_signature = signature.read_bytes()
    signature.write_bytes(b"invalid signature")
    write_package(wheel_bytes)
    with pytest.raises(ValueError, match="signature could not be verified"):
        verify_software_package(package, public)
    signature.write_bytes(original_signature)
    with monkeypatch.context() as unreadable:
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if path == public:
                raise PermissionError("fixture permission denied")
            return original_open(path, *args, **kwargs)

        unreadable.setattr(Path, "open", guarded_open)
        write_package(wheel_bytes)
        with pytest.raises(ValueError, match="public key is not readable"):
            verify_software_package(package, public)
    write_package(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        verify_software_package(package, public)
    write_package(wheel_bytes, extra=True)
    with pytest.raises(ValueError, match="undeclared"):
        verify_software_package(package, public)

    # Exercise the real exporter against this checkout's runtime wheel contents.
    from cv_runtime.distribution import edge_node_contracts, runtime_code_digest
    from scripts.release.build_edge_runtime_update import export_runtime_update

    bundle = tmp_path / "bundle"
    wheelhouse = bundle / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    source = ROOT / "backend/packages/cv-runtime/src/cv_runtime"
    with zipfile.ZipFile(wheelhouse / "cv_runtime-0.1.0-py3-none-any.whl", "w") as archive:
        for path in source.rglob("*.py"):
            archive.write(path, "cv_runtime/" + path.relative_to(source).as_posix())
    (wheelhouse / "cv_edge-0.1.0-py3-none-any.whl").write_bytes(b"fixture")
    (bundle / "platform-profile.json").write_text(
        json.dumps(
            {
                "platform": "windows_11",
                "architecture": "x86_64",
                "python_tag": "312",
            }
        )
    )
    exported = tmp_path / "exported.zip"
    exported_again = tmp_path / "exported-again.zip"
    export_runtime_update(bundle, exported, release_id="runtime-test", signing_key=private)
    export_runtime_update(bundle, exported_again, release_id="runtime-test", signing_key=private)
    exported_release = verify_software_package(exported, public)
    assert exported_release.runtime_code_digest == runtime_code_digest()
    assert exported_release.node_contract_versions == edge_node_contracts()
    assert exported.read_bytes() == exported_again.read_bytes()

    # Refresh only Python wheels, retain the input bundle and its target profile.
    from scripts.release import build_edge_offline_bundle as builder

    encrypted_key = tmp_path / "encrypted-private.pem"
    passphrase = "Test-Passphrase äö 42!"
    subprocess.run(
        [
            executable,
            "pkey",
            "-in",
            str(private),
            "-aes-256-cbc",
            "-passout",
            "stdin",
            "-out",
            str(encrypted_key),
        ],
        input=(passphrase + "\n").encode("utf-8"),
        check=True,
        capture_output=True,
    )
    original_runtime = (wheelhouse / "cv_runtime-0.1.0-py3-none-any.whl").read_bytes()
    original_profile = (bundle / "platform-profile.json").read_bytes()
    calls = []

    def add_basler(config, profile, destination):  # noqa: ANN001, ANN202
        assert profile["platform"] == "windows_11"
        assert config["package_index_url"] == "https://packages.example.invalid/simple"
        assert config["additional_wheel_directory"] == str(tmp_path / "vendor wheels")
        assert destination != wheelhouse
        calls.append("sdk")
        (destination / "pypylon-26.8-cp39-abi3-win_amd64.whl").write_bytes(b"basler fixture")

    def refresh(config, destination):  # noqa: ANN001, ANN202
        calls.append("runtime")
        (destination / "edge_agent-0.8.0-py3-none-any.whl").write_bytes(b"updated agent")

    monkeypatch.setattr(builder, "_ensure_basler_wheel", add_basler)
    monkeypatch.setattr(builder, "_refresh_internal_wheels", refresh)
    monkeypatch.setattr(builder, "_validate_wheelhouse", lambda *args: calls.append("validate"))
    refreshed = tmp_path / "refreshed.zip"
    export_runtime_update(
        bundle,
        refreshed,
        release_id="basler-update",
        signing_key=encrypted_key,
        signing_passphrase=passphrase,
        refresh_runtime=True,
        openssl_executable=executable,
        package_index_url="https://packages.example.invalid/simple",
        additional_wheel_directory=tmp_path / "vendor wheels",
    )
    refreshed_release = verify_software_package(refreshed, public)
    assert calls == ["sdk", "runtime", "validate"]
    assert "pypylon-26.8-cp39-abi3-win_amd64.whl" in refreshed_release.wheels
    assert refreshed_release.runtime_code_digest == runtime_code_digest()
    assert (wheelhouse / "cv_runtime-0.1.0-py3-none-any.whl").read_bytes() == original_runtime
    assert (bundle / "platform-profile.json").read_bytes() == original_profile
    assert not list(wheelhouse.glob("pypylon-*.whl"))

    # Selecting a complete ZIP or Jetson tarball produces the same signed runtime package.
    for extension in ("zip", "tar.gz"):
        packed = tmp_path / f"existing build.{extension}"
        if extension == "zip":
            with zipfile.ZipFile(packed, "w") as archive:
                for path in bundle.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(bundle).as_posix())
                archive.writestr("cuda-runtime-installer.exe", b"unused OS payload")
        else:
            with tarfile.open(packed, "w:gz") as archive:
                archive.add(bundle, arcname=".")
        original_archive = packed.read_bytes()
        from_archive = tmp_path / f"from-{extension}.zip"
        export_runtime_update(
            packed,
            from_archive,
            release_id="runtime-test",
            signing_key=encrypted_key,
            signing_passphrase=passphrase,
            openssl_executable=executable,
        )
        assert from_archive.read_bytes() == exported.read_bytes()
        assert packed.read_bytes() == original_archive

    # Exercise the same UTF-8 stdin protocol used by the native builder.
    cli_output = tmp_path / "cli-encrypted.zip"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/release/build_edge_runtime_update.py"),
            "--bundle",
            str(packed),
            "--output",
            str(cli_output),
            "--release-id",
            "runtime-test",
            "--signing-key",
            str(encrypted_key),
            "--openssl",
            executable,
            "--signing-passphrase-stdin",
        ],
        input=(passphrase + "\n").encode("utf-8"),
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert cli_output.read_bytes() == exported.read_bytes()
    assert passphrase.encode("utf-8") not in completed.stdout + completed.stderr

    from scripts.release import build_edge_runtime_update as exporter

    with monkeypatch.context() as bad_password:
        bad_password.setattr(
            exporter, "_extract_runtime_inputs", lambda *args: pytest.fail("unpacked")
        )
        for incorrect in ("", "wrong password"):
            with pytest.raises(ValueError, match="richtige Schlüssel-Passphrase") as failure:
                export_runtime_update(
                    packed,
                    tmp_path / "should-not-exist.zip",
                    release_id="invalid-key",
                    signing_key=encrypted_key,
                    signing_passphrase=incorrect,
                    refresh_runtime=True,
                    openssl_executable=executable,
                )
            assert passphrase not in str(failure.value)
            assert not (tmp_path / "should-not-exist.zip").exists()

    # A failed write must not truncate a previous valid output package.

    def fail_write(destination, *args):  # noqa: ANN001, ANN002, ANN202
        destination.write_bytes(b"partial output")
        raise OSError("simulated disk error")

    monkeypatch.setattr(exporter, "_write_update_archive", fail_write)
    original_output = exported.read_bytes()
    with pytest.raises(OSError, match="simulated disk"):
        export_runtime_update(
            bundle,
            exported,
            release_id="failed-update",
            signing_key=private,
            openssl_executable=executable,
        )
    assert exported.read_bytes() == original_output
    assert not list(exported.parent.glob(".edge-update-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL inheritance regression")
def test_runtime_zip_inherits_output_directory_access(tmp_path: Path) -> None:
    from scripts.release.build_edge_runtime_update import _publish_update_archive

    output_dir = tmp_path / "shared exports"
    output_dir.mkdir()
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    # Give the output directory an inherited reader rule, as a shared build
    # directory has. Only fixture artifacts are stored here, never private keys.
    subprocess.run(
        [str(system32 / "icacls.exe"), str(output_dir), "/grant", "*S-1-5-32-545:(OI)(CI)RX"],
        capture_output=True,
        check=True,
    )
    reference = output_dir / "normal-file.txt"
    reference.write_text("fixture")
    signature = tmp_path / "release.sig"
    signature.write_bytes(b"fixture signature")
    exported = output_dir / "runtime.zip"
    _publish_update_archive(exported, b"{}", signature, [])
    with zipfile.ZipFile(exported) as archive:
        assert archive.read("release.sig") == b"fixture signature"
    acl_check = tmp_path / "compare-acl.ps1"
    acl_check.write_text(
        "param([string]$Reference, [string]$Actual)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$section = [System.Security.AccessControl.AccessControlSections]::Access\n"
        "$expected = (Get-Acl -LiteralPath $Reference).GetSecurityDescriptorSddlForm($section)\n"
        "$actual = (Get-Acl -LiteralPath $Actual).GetSecurityDescriptorSddlForm($section)\n"
        "if ($expected -ne $actual) { throw 'Exported ZIP lost destination ACL inheritance' }\n",
        encoding="utf-8",
    )
    checked = subprocess.run(
        [
            str(system32 / "WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(acl_check),
            str(reference),
            str(exported),
        ],
        capture_output=True,
    )
    assert checked.returncode == 0, checked.stderr.decode(errors="replace")
    assert not list(output_dir.glob(".edge-update-*"))


@pytest.mark.parametrize("extension", ["zip", "tar.gz"])
def test_runtime_archive_extracts_only_profile_and_wheels(tmp_path: Path, extension: str) -> None:
    from scripts.release.build_edge_runtime_update import _extract_runtime_inputs

    entries = {
        "platform-profile.json": b'{"platform": "jetson_orin_nano"}',
        "wheelhouse/edge_agent-0.8.0-py3-none-any.whl": b"wheel fixture",
        "python-runtime.zip": b"unused Python installer",
        "deployment/edge/install-jetson-offline.sh": b"unused installer",
    }
    packed = tmp_path / f"build.{extension}"
    _input_archive(packed, entries)
    destination = tmp_path / "extracted"
    _extract_runtime_inputs(packed, destination)
    assert sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    ) == [
        "platform-profile.json",
        "wheelhouse/edge_agent-0.8.0-py3-none-any.whl",
    ]


@pytest.mark.parametrize("extension", ["zip", "tar.gz"])
@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "wheelhouse/../../escape", "C:/escape", "wheelhouse\\escape"]
)
def test_runtime_archive_rejects_unsafe_paths(tmp_path: Path, extension: str, name: str) -> None:
    from scripts.release.build_edge_runtime_update import _extract_runtime_inputs

    packed = tmp_path / f"build.{extension}"
    _input_archive(packed, {name: b"unsafe"})
    with pytest.raises(ValueError, match="Unsafe"):
        _extract_runtime_inputs(packed, tmp_path / "extract")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("extension", ["zip", "tar.gz"])
def test_runtime_archive_rejects_symlink_inputs(tmp_path: Path, extension: str) -> None:
    from scripts.release.build_edge_runtime_update import _extract_runtime_inputs

    packed = tmp_path / f"build.{extension}"
    if extension == "zip":
        with zipfile.ZipFile(packed, "w") as archive:
            entry = zipfile.ZipInfo("platform-profile.json")
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, "../profile.json")
    else:
        with tarfile.open(packed, "w:gz") as archive:
            entry = tarfile.TarInfo("platform-profile.json")
            entry.type = tarfile.SYMTYPE
            entry.linkname = "../profile.json"
            archive.addfile(entry)
    with pytest.raises(ValueError, match="regular file"):
        _extract_runtime_inputs(packed, tmp_path / "extract")


def test_runtime_export_rejects_output_inside_original_build(tmp_path: Path) -> None:
    from scripts.release.build_edge_runtime_update import export_runtime_update

    signing_key = tmp_path / "signing.pem"
    signing_key.write_text("fixture")
    with pytest.raises(ValueError, match="outside"):
        export_runtime_update(
            tmp_path,
            tmp_path / "update.zip",
            release_id="update",
            signing_key=signing_key,
        )
    assert not (tmp_path / "update.zip").exists()


@pytest.mark.parametrize("passphrase", ["line1\nline2", "line1\rline2", "nul\0byte"])
def test_runtime_signer_rejects_multiline_passphrases(passphrase: str) -> None:
    from scripts.release.build_edge_runtime_update import _passphrase_input

    with pytest.raises(ValueError, match="Zeilenumbrüche"):
        _passphrase_input(passphrase)


@pytest.mark.parametrize(
    ("stderr", "passphrase", "message"),
    [
        (b"error:bad decrypt", "", "leere Passphrase"),
        (b"error:bad decrypt", "fixture-secret", "Zeichenkodierung"),
        (b"error:cipherfinal error", "fixture-secret", "entschlüsseln"),
        (b"Not an RSA key", "fixture-secret", "keinen privaten RSA-Schlüssel"),
        (b"Permission denied", "fixture-secret", "Leseberechtigung"),
        (b"error:unsupported", "fixture-secret", "nicht unterstütztes"),
        (b"unknown failure", "fixture-secret", "OpenSSL-Exit 1"),
    ],
)
def test_runtime_signer_reports_specific_errors_without_raw_diagnostics(
    monkeypatch: pytest.MonkeyPatch, stderr: bytes, passphrase: str, message: str
) -> None:
    from scripts.release import build_edge_runtime_update as exporter

    monkeypatch.setattr(
        exporter.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout=b"", stderr=stderr + b"\nfixture-secret private-data"
        ),
    )
    with pytest.raises(ValueError, match=message) as failure:
        exporter._validate_signing_key("openssl", Path("fixture.pem"), passphrase)
    assert "fixture-secret" not in str(failure.value)
    assert "private-data" not in str(failure.value)


def _input_archive(path: Path, entries: dict[str, bytes]) -> None:
    if path.suffix == ".zip":
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in entries.items():
                entry = zipfile.ZipInfo(name)
                entry.filename = name  # Preserve malicious backslashes on Windows as well.
                archive.writestr(entry, content)
    else:
        with tarfile.open(path, "w:gz") as archive:
            for name, content in entries.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))


def test_runtime_signer_resolves_explicit_command_from_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.release import build_edge_runtime_update as exporter

    executable = tmp_path / "signing tools" / "custom-openssl.exe"
    monkeypatch.setattr(exporter.shutil, "which", lambda name: str(executable))
    assert exporter._resolve_openssl("custom-openssl") == str(executable.resolve())


def test_runtime_signer_discovers_git_openssl_without_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.release import build_edge_runtime_update as exporter

    executable = tmp_path / "Program Files" / "Git" / "usr" / "bin" / "openssl.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture executable")
    monkeypatch.setattr(exporter.sys, "platform", "win32")
    monkeypatch.setattr(exporter.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    assert exporter._resolve_openssl("openssl") == str(executable.resolve())
    # An explicitly configured missing executable must not silently select another signer.
    with pytest.raises(ValueError, match="OpenSSL wurde nicht gefunden"):
        exporter._resolve_openssl(str(tmp_path / "missing-openssl.exe"))


def test_missing_signer_fails_before_unpacking_or_refreshing_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.release import build_edge_runtime_update as exporter

    bundle = tmp_path / "existing.tar.gz"
    bundle.write_bytes(b"original bundle")
    key = tmp_path / "private.pem"
    key.write_bytes(b"private key fixture")
    monkeypatch.setattr(exporter.shutil, "which", lambda name: None)
    monkeypatch.setattr(exporter, "_extract_runtime_inputs", lambda *args: pytest.fail("unpacked"))
    with pytest.raises(ValueError, match="noch keine Runtime-Pakete neu gebaut"):
        exporter.export_runtime_update(
            bundle,
            tmp_path / "update.zip",
            release_id="test",
            signing_key=key,
            refresh_runtime=True,
            openssl_executable=str(tmp_path / "missing-openssl.exe"),
        )
    assert bundle.read_bytes() == b"original bundle"
    assert not (tmp_path / "update.zip").exists()
