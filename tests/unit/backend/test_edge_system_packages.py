from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
PROFILE = {"platform": "jetson_orin_nano", "architecture": "aarch64"}
SignedUpdate = tuple[Path, Path, list[dict], Callable[..., str]]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "deployment/edge" / name
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def system_module() -> ModuleType:
    return load_script("edge-system-packages.py")


@pytest.fixture
def repository(tmp_path: Path, edge_system_package_cache: ModuleType) -> Path:
    edge_system_package_cache.stage(PROFILE, tmp_path)
    return tmp_path / "system-packages"


def test_pinned_prerequisites_stage_without_network(
    repository: Path, system_module: ModuleType, edge_system_package_cache: ModuleType
) -> None:
    manifest = system_module.verify_repository(repository)
    assert manifest["install"] == ["ufw"]
    assert "ufw_0.36.2-6_all.deb" in manifest["files"]
    assert edge_system_package_cache.prepare({"platform": "windows_11"}) is None
    with pytest.raises(ValueError, match="ARM64"):
        edge_system_package_cache.prepare({**PROFILE, "architecture": "x86_64"})


def test_damaged_download_never_replaces_cached_package(
    edge_system_package_cache: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = edge_system_package_cache.CACHE_ROOT / "ufw_0.36.2-6_all.deb"
    package.write_bytes(b"old-incomplete-download")
    monkeypatch.setattr(
        edge_system_package_cache, "urlopen", lambda *a, **kw: io.BytesIO(b"truncated")
    )
    with pytest.raises(ValueError, match="unvollständig"):
        edge_system_package_cache.prepare(PROFILE)
    assert package.read_bytes() == b"old-incomplete-download"
    assert not list(package.parent.glob("*.part"))


def test_offline_bundle_staging_never_downloads_missing_prerequisites(
    edge_system_package_cache: ModuleType, tmp_path: Path
) -> None:
    (edge_system_package_cache.CACHE_ROOT / "ufw_0.36.2-6_all.deb").unlink()
    with pytest.raises(ValueError, match="Online-Inhalte vorbereiten"):
        edge_system_package_cache.stage(PROFILE, tmp_path)


@pytest.mark.parametrize("fault", ["bytes", "path", "remote_index", "undeclared"])
def test_repository_rejects_unverified_inputs(
    repository: Path, system_module: ModuleType, fault: str
) -> None:
    manifest_path = repository / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if fault == "bytes":
        (repository / "ufw_0.36.2-6_all.deb").write_bytes(b"corrupt")
    elif fault == "path":
        manifest["files"]["../outside.deb"] = "a" * 64
    elif fault == "undeclared":
        (repository / "extra.deb").write_bytes(b"unsigned")
    else:
        index = repository / "Packages"
        index.write_text(
            index.read_text().replace("Filename: ./", "Filename: https://example.org/")
        )
        manifest["files"]["Packages"] = system_module.digest(index)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        system_module.verify_repository(repository)


@pytest.mark.parametrize("existing,recovering", [(False, False), (True, False), (True, True)])
def test_apt_uses_only_offline_repository_and_preserves_configuration(
    repository: Path,
    system_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    recovering: bool,
) -> None:
    config = tmp_path / "etc"
    config.mkdir()
    env = config / "device.env"
    original = "YBB_EDGE_SERVER_IP=192.168.50.10\nYBB_EDGE_PLANT_ALLOWED_IPS=192.168.50.40\n"
    env.write_text(original)
    ufw = tmp_path / "ufw"
    if existing:
        ufw.touch()
    if recovering:
        (config / "ufw-bootstrap.json").write_text('{"managed":true}')
    monkeypatch.setattr(system_module, "UFW_PATH", ufw)
    monkeypatch.setattr(
        system_module.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "24.04"},
    )
    commands, firewall = [], []

    def run(args: list[str]) -> str:
        commands.append(args)
        if args[0] == "/usr/bin/dpkg":
            return "arm64\n"
        sources = next(a.split("=", 1)[1] for a in args if a.startswith("Dir::Etc::sourcelist="))
        line = Path(sources).read_text()
        assert line.startswith("deb [trusted=yes] file:")
        assert "https:" not in line and "http:" not in line
        assert "Dir::Etc::sourceparts=-" in args
        assert "--allow-downgrades" not in args
        if "install" in args:
            assert "--no-remove" in args
            assert args[-1] == "ufw"
        return ""

    monkeypatch.setattr(system_module, "run", run)
    monkeypatch.setattr(
        system_module, "configure_firewall", lambda values, **kw: firewall.append(kw)
    )
    system_module.install(repository, config)
    assert len(commands) == (1 if existing and not recovering else 4)
    assert firewall == [{"activate_new": not existing or recovering}]
    assert env.read_text() == original
    assert not (config / "ufw-bootstrap.json").exists()


@pytest.mark.parametrize("os_version,architecture", [("22.04", "arm64"), ("24.04", "amd64")])
def test_install_rejects_wrong_platform_before_apt(
    repository: Path,
    system_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    os_version: str,
    architecture: str,
) -> None:
    monkeypatch.setattr(
        system_module.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": os_version},
    )

    def run(args: list[str]) -> str:
        assert args == ["/usr/bin/dpkg", "--print-architecture"]
        return architecture

    monkeypatch.setattr(system_module, "run", run)
    with pytest.raises(ValueError, match="Ubuntu 24.04|ARM64"):
        system_module.install(repository, tmp_path)


@pytest.mark.parametrize("active,activate_new", [(True, False), (False, True), (False, False)])
def test_firewall_keeps_existing_policy_and_only_bootstraps_new_ufw(
    system_module: ModuleType, monkeypatch: pytest.MonkeyPatch, active: bool, activate_new: bool
) -> None:
    commands = []

    def run(args: list[str]) -> str:
        commands.append(args)
        if args[-2:] == ["status", "numbered"]:
            return (
                (
                    "Status: active\n[ 1] 22/tcp ALLOW Anywhere # Admin SSH\n"
                    "[ 2] 9444/tcp ALLOW 192.168.50.99 # YBB Edge plant trigger client\n"
                )
                if active
                else "Status: inactive"
            )
        return "Status: active"

    monkeypatch.setattr(system_module, "run", run)
    values = {"YBB_EDGE_SERVER_IP": "192.168.50.10", "YBB_EDGE_PLANT_ALLOWED_IPS": "192.168.50.40"}
    if not active and not activate_new:
        with pytest.raises(ValueError, match="deaktiviert"):
            system_module.configure_firewall(values)
        assert len(commands) == 1
        return
    system_module.configure_firewall(values, activate_new=activate_new)
    assert [a for a in commands if "delete" in a] == (
        [["/usr/sbin/ufw", "--force", "delete", "2"]] if active else []
    )
    assert not any("reset" in a or "disable" in a for a in commands)
    assert any(a[1:4] == ["prepend", "deny", "9444/tcp"] for a in commands)
    assert any("192.168.50.10" in a and "9443" in a for a in commands)
    assert any("192.168.50.40" in a and "9444" in a for a in commands)
    policy = [a for a in commands if "default" in a]
    assert policy == ([] if active else [["/usr/sbin/ufw", "default", "allow", "incoming"]])


@pytest.fixture
def tegra_backend(
    system_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict:
    monkeypatch.setattr(system_module.platform, "release", lambda: "6.8.12-1021-tegra")
    config = tmp_path / "config"
    config.mkdir()
    (config / "ufw-bootstrap.json").write_text('{"managed":true}')
    defaults = tmp_path / "ufw-defaults"
    defaults.write_text("MANAGE_BUILTINS=no\nIPV6=yes\n")
    monkeypatch.setattr(system_module, "UFW_DEFAULTS", defaults)
    state = {
        "config": config,
        "defaults": defaults,
        "calls": [],
        "missing": {"nft_limit", "nft_log"},
        "legacy_test_failure": False,
        "switch_failure": False,
        "saved": "*filter\n:INPUT ACCEPT [0:0]\n"
        ":ufw-before-input - [0:0]\n-A INPUT -j ufw-before-input\n"
        "-A ufw-before-input -j ACCEPT\nCOMMIT\n",
        "versions": {"iptables": "nf_tables", "ip6tables": "nf_tables"},
    }

    def run(args: list[str], **kwargs: object) -> str:
        state["calls"].append((args, kwargs))
        name = Path(args[0]).name
        if name == "modprobe":
            if args[-1] in state["missing"]:
                raise ValueError("Module not found: " + args[-1])
            return ""
        if name in {"iptables", "ip6tables"}:
            assert args[1:] == ["--version"]
            return f"{name} v1.8.10 ({state['versions'][name]})"
        if name.endswith("-save"):
            return state["saved"]
        if name.endswith("-legacy-restore"):
            assert args[1:] == ["--test", "--noflush"]
            assert "-m limit" in kwargs["input"] and "-j LOG" in kwargs["input"]
            if state["legacy_test_failure"]:
                raise ValueError("Legacy kernel support unavailable")
            return ""
        if name == "update-alternatives":
            if args[1] == "--query":
                mode = "manual" if args[2] == "ip6tables" else "auto"
                return f"Status: {mode}\nValue: /usr/sbin/{args[2]}-nft\n"
            if args[1] == "--auto":
                state["versions"][args[2]] = "nf_tables"
            else:
                assert args[1] == "--set"
                if state["switch_failure"] and args[3] == "/usr/sbin/ip6tables-legacy":
                    raise ValueError("Failed to switch IPv6 alternative")
                state["versions"][args[2]] = (
                    "legacy" if args[3].endswith("-legacy") else "nf_tables"
                )
            return ""
        assert args == ["/usr/sbin/ufw", "--force", "disable"]
        assert not any("--set" in call[0] for call in state["calls"][:-1])
        return ""

    monkeypatch.setattr(system_module, "run", run)
    return state


def test_tegra_missing_nft_modules_switches_only_after_legacy_probe(
    system_module: ModuleType, tegra_backend: dict
) -> None:
    system_module.ensure_firewall_backend(tegra_backend["config"])
    assert set(tegra_backend["versions"].values()) == {"legacy"}
    calls = [args for args, _kwargs in tegra_backend["calls"]]
    disable = calls.index(["/usr/sbin/ufw", "--force", "disable"])
    assert len([args for args in calls[:disable] if "--test" in args]) == 2
    assert len([args for args in calls[disable + 1 :] if "--set" in args]) == 2
    assert not any("reset" in args for args in calls)
    # Subsequent updates preserve the selected backend without another switch.
    tegra_backend["calls"].clear()
    system_module.ensure_firewall_backend(tegra_backend["config"])
    assert len(tegra_backend["calls"]) == 2


def test_tegra_partial_ufw_bootstrap_can_recover_its_forward_drop_policy(
    system_module: ModuleType, tegra_backend: dict
) -> None:
    tegra_backend["saved"] = tegra_backend["saved"].replace(
        ":INPUT ACCEPT [0:0]", ":INPUT ACCEPT [0:0]\n:FORWARD DROP [0:0]"
    )
    system_module.ensure_firewall_backend(tegra_backend["config"])
    assert set(tegra_backend["versions"].values()) == {"legacy"}


@pytest.mark.parametrize(
    "failure",
    ["foreign_rules", "existing_firewall", "legacy_probe", "legacy_module", "manage_builtins"],
)
def test_tegra_backend_refuses_switch_before_firewall_changes(
    system_module: ModuleType, tegra_backend: dict, failure: str
) -> None:
    if failure == "foreign_rules":
        tegra_backend["saved"] += ":DOCKER - [0:0]\n"
    elif failure == "existing_firewall":
        (tegra_backend["config"] / "ufw-bootstrap.json").unlink()
    elif failure == "legacy_probe":
        tegra_backend["legacy_test_failure"] = True
    elif failure == "legacy_module":
        tegra_backend["missing"].add("xt_limit")
    else:
        tegra_backend["defaults"].write_text("MANAGE_BUILTINS=yes\n")
    with pytest.raises(ValueError):
        system_module.ensure_firewall_backend(tegra_backend["config"])
    assert set(tegra_backend["versions"].values()) == {"nf_tables"}
    assert not any("--set" in args or "disable" in args for args, _kwargs in tegra_backend["calls"])


def test_tegra_working_nft_backend_is_preserved(
    system_module: ModuleType, tegra_backend: dict
) -> None:
    tegra_backend["missing"].clear()
    system_module.ensure_firewall_backend(tegra_backend["config"])
    assert set(tegra_backend["versions"].values()) == {"nf_tables"}
    assert len(tegra_backend["calls"]) == 4


def test_tegra_failed_switch_restores_both_alternatives_and_modes(
    system_module: ModuleType, tegra_backend: dict
) -> None:
    tegra_backend["switch_failure"] = True
    with pytest.raises(ValueError, match="IPv6 alternative"):
        system_module.ensure_firewall_backend(tegra_backend["config"])
    assert set(tegra_backend["versions"].values()) == {"nf_tables"}
    calls = [args for args, _kwargs in tegra_backend["calls"]]
    assert calls[-2:] == [
        ["/usr/bin/update-alternatives", "--auto", "iptables"],
        ["/usr/bin/update-alternatives", "--set", "ip6tables", "/usr/sbin/ip6tables-nft"],
    ]
    assert (tegra_backend["config"] / "ufw-bootstrap.json").exists()


@pytest.mark.parametrize(
    "extra", [":INPUT DROP [0:0]", "-A INPUT -p tcp --dport 22 -j ACCEPT", ":CUSTOM - [0:0]"]
)
def test_backend_guard_rejects_unmanaged_policy_and_rules(
    system_module: ModuleType, extra: str
) -> None:
    assert not system_module.only_ufw_rules("*filter\n" + extra + "\nCOMMIT\n")


@pytest.mark.parametrize("initial", ["empty", "ipv6_only", "only_managed"])
def test_firewall_bootstrap_and_repeat_handle_empty_ufw_rule_lists(
    system_module: ModuleType, monkeypatch: pytest.MonkeyPatch, initial: str
) -> None:
    # UFW 0.36.2 rejects insert 1 for an empty address-family list; prepend
    # explicitly maps that case to append. Model that external CLI behavior.
    rules: list[dict] = []
    active = initial != "empty"
    policy = "deny" if active else None
    if initial == "ipv6_only":
        rules.append(dict(family=6, action="allow", port="22", address="any", comment="Admin SSH"))
    elif initial == "only_managed":
        rules.append(
            dict(
                family=4,
                action="allow",
                port="9444",
                address="192.168.50.99",
                comment="YBB Edge plant trigger client",
            )
        )

    def run(args: list[str]) -> str:
        nonlocal active, policy
        if args[1] == "status":
            if not active:
                return "Status: inactive\n"
            return "Status: active\n" + "".join(
                f"[{i + 1:2}] {r['port']}/tcp {r['action']} # {r['comment']}\n"
                for i, r in enumerate(rules)
            )
        if args[1:3] == ["--force", "delete"]:
            rules.pop(int(args[3]) - 1)
            return "Rule deleted"
        if args[1] == "default":
            assert args[2:] == ["allow", "incoming"]
            policy = "allow"
            return ""
        if args[1:] == ["--force", "enable"]:
            active = True
            return ""
        action = next(a for a in args if a in {"allow", "deny"})
        port = (
            args[args.index("port") + 1]
            if "port" in args
            else args[args.index(action) + 1].split("/")[0]
        )
        address = args[args.index("from") + 1] if "from" in args else "any"
        families = [4] if address != "any" else [4, 6]
        comment = args[args.index("comment") + 1]
        for family in families:
            matching = [i for i, r in enumerate(rules) if r["family"] == family]
            if args[1] == "insert" and int(args[2]) > len(matching):
                raise ValueError(f"ufw: ERROR: Invalid position '{args[2]}'")
            assert args[1] in {"prepend", "insert"}
            position = matching[0] if matching else len(rules)
            rules.insert(
                position,
                dict(family=family, action=action, port=port, address=address, comment=comment),
            )
        rules.sort(key=lambda r: r["family"])
        return "Rule added"

    monkeypatch.setattr(system_module, "run", run)
    values = {"YBB_EDGE_SERVER_IP": "192.168.50.10", "YBB_EDGE_PLANT_ALLOWED_IPS": "192.168.50.40"}
    system_module.configure_firewall(values, activate_new=initial == "empty")
    first_result = list(rules)
    # Reapplying deletes managed rules and may empty a whole family again.
    system_module.configure_firewall(values)
    assert rules == first_result
    assert policy == ("allow" if initial == "empty" else "deny")
    assert active
    for port, address in [("9443", "192.168.50.10"), ("9444", "192.168.50.40")]:
        matching = [r for r in rules if r["family"] == 4 and r["port"] == port]
        assert [(r["action"], r["address"]) for r in matching] == [
            ("allow", address),
            ("deny", "any"),
        ]
    assert not any(r["address"] == "192.168.50.99" for r in rules)
    if initial == "ipv6_only":
        assert any(r["comment"] == "Admin SSH" for r in rules)


@pytest.fixture
def signed_update(
    repository: Path, system_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SignedUpdate:
    openssl = shutil.which("openssl") or "C:/Program Files/Git/usr/bin/openssl.exe"
    if not Path(openssl).is_file():
        pytest.skip("OpenSSL required")
    config, data = tmp_path / "etc", tmp_path / "data"
    config.mkdir()
    data.mkdir()
    key = tmp_path / "private.pem"
    subprocess.run(
        [
            openssl,
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            openssl,
            "pkey",
            "-in",
            str(key),
            "-pubout",
            "-out",
            str(config / "release-signing-public.pem"),
        ],
        check=True,
        capture_output=True,
    )
    files = {p.name: p.read_bytes() for p in repository.iterdir()}
    release = {
        "schema_version": "edge.software.release.v1",
        **PROFILE,
        "system_packages": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    }
    raw = tmp_path / "release.json"
    raw.write_text(json.dumps(release))
    sig = tmp_path / "release.sig"
    subprocess.run(
        [openssl, "dgst", "-sha256", "-sign", str(key), "-out", str(sig), str(raw)],
        check=True,
        capture_output=True,
    )
    payload = {
        "release.json": raw.read_bytes(),
        "release.sig": sig.read_bytes(),
        **{"system-packages/" + name: content for name, content in files.items()},
    }
    installed = []
    monkeypatch.setattr(
        system_module,
        "install",
        lambda repo, root: installed.append(system_module.verify_repository(repo)),
    )
    original_run = system_module.run
    monkeypatch.setattr(system_module, "run", lambda args: original_run([openssl, *args[1:]]))

    def submit(*, fault: str | None = None) -> str:
        copied = dict(payload)
        if fault == "deb":
            copied["system-packages/ufw_0.36.2-6_all.deb"] += b"corrupt"
        elif fault == "signature":
            copied["release.sig"] = b"invalid"
        elif fault == "manifest":
            copied["release.json"] = copied["release.json"].replace(b"noble", b"badOS") + b" "
        artifact = tmp_path / "update.zip"
        with zipfile.ZipFile(artifact, "w") as archive:
            for name, content in copied.items():
                archive.writestr(name, content)
        checksum = system_module.digest(artifact)
        target = data / "artifacts/sha256" / checksum[:2] / checksum
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact, target)
        (data / "system-package-request.json").write_text(
            json.dumps({"request_id": "test-update-001", "package_sha256": checksum})
        )
        return checksum

    return config, data, installed, submit


@pytest.mark.parametrize("fault", [None, "deb", "signature", "manifest"])
def test_privileged_installer_checks_signature_and_bytes_before_install(
    system_module: ModuleType, signed_update: SignedUpdate, fault: str | None
) -> None:
    config, data, installed, submit = signed_update
    submit(fault=fault)
    assert system_module.tick(config, data)
    status = json.loads((config / "system-package-status.json").read_bytes())
    assert status["phase"] == ("failed" if fault else "succeeded")
    assert len(installed) == (0 if fault else 1)
    assert not system_module.tick(config, data)
    assert not list(config.glob(".system-update-*"))


def test_interrupted_system_install_resumes_same_request(
    system_module: ModuleType, signed_update: SignedUpdate
) -> None:
    config, data, installed, submit = signed_update
    digest = submit()
    (config / "system-package-status.json").write_text(
        json.dumps(
            {"request_id": "test-update-001", "package_sha256": digest, "phase": "installing"}
        )
    )
    assert system_module.tick(config, data)
    assert len(installed) == 1


def test_launcher_waits_for_verified_root_install(
    system_module: ModuleType, signed_update: SignedUpdate, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, data, installed, submit = signed_update
    digest = submit()
    module = load_script("edge-runtime-launcher.py")
    monkeypatch.setattr(module, "SYSTEM_CONFIG_ROOT", config)
    launcher = module.RuntimeLauncher(data)
    launcher.state["request_id"] = "test-update-001"
    (config / "settings-status.json").write_text(
        json.dumps({"system_update_protocol": 1, "observed_at": time.time()})
    )
    monkeypatch.setattr(module.time, "sleep", lambda _: system_module.tick(config, data))
    launcher.prepare_system_packages(digest)
    assert len(installed) == 1
    assert launcher.state["active_release"] == "bootstrap"


def test_launcher_rejects_missing_privileged_helper_before_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_script("edge-runtime-launcher.py")
    monkeypatch.setattr(module, "SYSTEM_CONFIG_ROOT", tmp_path)
    launcher = module.RuntimeLauncher(tmp_path)
    with pytest.raises(ValueError, match="Offline-Installer"):
        launcher.prepare_system_packages("a" * 64)
    assert not (tmp_path / "system-package-request.json").exists()


def test_catalogue_requires_new_launcher_only_for_system_payload() -> None:
    from cv_contracts.edge import EdgeSoftwareStatus
    from runtime_admin_api.edge_software import compatibility, launcher_capabilities
    from tests.unit.backend.test_central_edge_updates import _caps

    old = _caps().model_copy(update={"software_update_protocol": 1})
    release = {"python_version": "3.12", "system_packages": {"manifest.json": "a" * 64}}
    assert "Offline-Installer" in ";".join(compatibility(old, release))
    observed = launcher_capabilities(old, EdgeSoftwareStatus(protocol_version=2))
    assert not compatibility(observed, release)
    assert not compatibility(old, {"python_version": "3.12"})
