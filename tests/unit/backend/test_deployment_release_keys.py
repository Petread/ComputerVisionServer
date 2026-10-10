from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path
from types import ModuleType

import pytest
from scripts.release.build_edge_offline_bundle import BundleError, _sign_checksums
from scripts.release.build_edge_runtime_update import _resolve_openssl, manage_release_key

ROOT = Path(__file__).resolve().parents[3]
PASSPHRASE = "Test äö private passphrase 2026"


@pytest.fixture()
def release_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    try:
        openssl = _resolve_openssl("openssl")
    except ValueError:
        pytest.skip("OpenSSL is required for release signing tests")
    monkeypatch.setenv("PATH", str(Path(openssl).parent) + os.pathsep + os.environ["PATH"])
    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    fingerprint = manage_release_key(private, public, generate=True, passphrase=PASSPHRASE)
    return private, public, fingerprint


def _jetson() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_key_jetson_ui", ROOT / "deployment/edge/install-jetson-offline-ui.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _central() -> dict:
    source = (ROOT / "deployment/windows-deployment-ui.ps1").read_text(encoding="utf-8-sig")
    code = source.split("$script:ReleaseTrustPython = @'\n", 1)[1].split("\n'@", 1)[0]
    namespace = {"__name__": "trust_unit_test"}
    exec(compile(code, "central-release-trust", "exec"), namespace)
    return namespace


def test_generated_keys_are_encrypted_and_existing_private_keys_are_retained(
    tmp_path: Path, release_keys: tuple[Path, Path, str]
) -> None:
    private, public, fingerprint = release_keys
    original = private.read_bytes()
    assert b"ENCRYPTED PRIVATE KEY" in original
    assert PASSPHRASE.encode() not in original
    assert b"PRIVATE KEY" not in public.read_bytes()
    copied = tmp_path / "public-copy.pem"
    assert manage_release_key(private, copied, passphrase=PASSPHRASE) == fingerprint
    assert copied.read_bytes() == public.read_bytes()
    with pytest.raises(ValueError, match="existiert bereits"):
        manage_release_key(private, tmp_path / "new.pem", generate=True, passphrase=PASSPHRASE)
    with pytest.raises(ValueError, match="Passphrase"):
        manage_release_key(private, tmp_path / "wrong.pem", passphrase="wrong")
    assert not (tmp_path / "wrong.pem").exists()
    assert private.read_bytes() == original


def test_failed_key_creation_does_not_leave_private_material(tmp_path: Path) -> None:
    private = tmp_path / "private.pem"
    with pytest.raises(ValueError, match="12 Zeichen"):
        manage_release_key(private, tmp_path / "public.pem", generate=True, passphrase="short")
    assert not private.exists()


def test_full_bundle_signing_supports_encrypted_keys_and_rejects_wrong_public_key(
    tmp_path: Path, release_keys: tuple[Path, Path, str]
) -> None:
    private, public, _ = release_keys
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "SHA256SUMS").write_text("fixture payload\n")
    shutil.copyfile(public, stage / "release-signing-public.pem")
    _sign_checksums(stage, {"signing_private_key": str(private)}, PASSPHRASE)
    assert (stage / "SHA256SUMS.sig").stat().st_size == 384
    (stage / "release-signing-public.pem").write_text("wrong public key")
    with pytest.raises(BundleError, match="passen nicht zusammen"):
        _sign_checksums(stage, {"signing_private_key": str(private)}, PASSPHRASE)


def test_central_trust_requires_reinspection_and_backs_up_rotation(
    tmp_path: Path, release_keys: tuple[Path, Path, str]
) -> None:
    private, public, fingerprint = release_keys
    central = _central()
    target = tmp_path / "central" / "trusted.pem"
    manage = central["manage_trust"]
    assert manage(public.read_bytes(), target, "inspect")["installed"] is None
    assert not target.exists()
    assert manage(public.read_bytes(), target, "install")["installed"] == fingerprint
    with pytest.raises(ValueError, match="Erneut pruefen"):
        manage(public.read_bytes(), target, "install", expected="stale")
    with pytest.raises(ValueError, match="oeffentlichen"):
        manage(private.read_bytes(), target, "inspect")
    replacement = tmp_path / "replacement.pem"
    replacement_fingerprint = manage_release_key(
        tmp_path / "replacement-private.pem", replacement, generate=True, passphrase=PASSPHRASE
    )
    result = manage(replacement.read_bytes(), target, "install", expected=fingerprint)
    assert result["installed"] == replacement_fingerprint
    assert Path(result["backup"]).read_bytes() == public.read_bytes()
    assert target.read_bytes() == replacement.read_bytes()


def test_jetson_trust_only_keeps_runtime_and_configuration_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_keys: tuple[Path, Path, str]
) -> None:
    private, public, fingerprint = release_keys
    module = _jetson()
    target = tmp_path / "etc" / "release-signing-public.pem"
    monkeypatch.setattr(module, "TRUSTED_KEY", target)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(module, "validate_settings", lambda settings: settings)
    monkeypatch.setattr(module, "_run", lambda command: pytest.fail("Installer must not run"))
    config = tmp_path / "device.env"
    config.write_text("existing-device-configuration")
    settings = {
        "release_public_key": str(public),
        "selected_key_fingerprint": fingerprint,
        "installed_key_fingerprint": None,
        "trust_only": True,
    }
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(settings))
    assert module.privileged_install(tmp_path, settings_path) == 0
    assert module.public_key_identity(target)[1] == fingerprint
    assert config.read_text() == "existing-device-configuration"
    with pytest.raises(module.InstallerUiError, match="verändert"):
        module.install_release_trust(settings)
    settings["installed_key_fingerprint"] = fingerprint
    settings["release_public_key"] = str(target)
    module.install_release_trust(settings)  # Reusing the installed path is safe.
    with pytest.raises(module.InstallerUiError, match="öffentlichen"):
        module.public_key_identity(private)
