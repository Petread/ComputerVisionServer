"""Installer-owned Ubuntu prerequisite installer; stdlib only, never load runtime wheels."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

UFW_PATH = Path("/usr/sbin/ufw")
UFW_DEFAULTS = Path("/etc/default/ufw")


def run(args: list[str], **kwargs: object) -> str:
    try:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"},
            **kwargs,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()[-1800:]
        raise ValueError(f"{Path(args[0]).name}: {detail}") from exc
    return result.stdout


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path: Path, values: dict) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(json.dumps(values).encode())
        output.flush()
        os.fsync(output.fileno())
    try:
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def environment(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise ValueError("Duplicate device setting.")
        values[key] = value
    return values


def firewall_values(values: dict[str, str]) -> tuple[str, int, int, list[str]]:
    server = str(ipaddress.IPv4Address(values["YBB_EDGE_SERVER_IP"]))
    control = int(values.get("YBB_EDGE_CONTROL_PORT", "9443"))
    plant = int(values.get("YBB_EDGE_PLANT_API_PORT", "9444"))
    if control == plant or not all(1 <= port <= 65535 for port in (control, plant)):
        raise ValueError("Invalid Edge control/trigger ports.")
    clients = [
        str(ipaddress.IPv4Address(ip.strip()))
        for ip in values.get("YBB_EDGE_PLANT_ALLOWED_IPS", "").split(",")
        if ip.strip()
    ]
    return server, control, plant, sorted(set(clients))


def only_ufw_rules(saved: str) -> bool:
    """A backend switch cannot migrate rules owned by Docker or another firewall."""
    builtins = {"INPUT", "OUTPUT", "FORWARD", "PREROUTING", "POSTROUTING"}
    ufw_chains = bool(re.search(r"^:ufw6?-[A-Za-z0-9_-]+ - ", saved, re.MULTILINE))
    for line in saved.splitlines():
        if not line or line.startswith(("#", "*")) or line == "COMMIT":
            continue
        if re.match(r"^:ufw6?-[A-Za-z0-9_-]+ - \[\d+:\d+\]$", line):
            continue
        if line.startswith(":"):
            parts = line[1:].split()
            if len(parts) == 3 and parts[0] in builtins:
                # A failed UFW bootstrap may already have set its forwarding policy
                # to DROP. Without UFW chains a DROP policy belongs to someone else.
                if parts[1] == "ACCEPT" or (
                    ufw_chains and parts[0] in {"INPUT", "OUTPUT", "FORWARD"} and parts[1] == "DROP"
                ):
                    continue
        if re.match(r"^-A ufw6?-[A-Za-z0-9_-]+ ", line):
            continue
        if re.fullmatch(r"-A (INPUT|OUTPUT|FORWARD) -j ufw6?-[A-Za-z0-9_-]+", line):
            continue
        return False
    return True


def ensure_firewall_backend(config_root: Path) -> None:
    # Some Tegra kernels provide xt_limit/xt_LOG but not nft_limit/nft_log.
    # Do not change working nft systems or an existing administrator-owned firewall.
    if "tegra" not in platform.release().lower():
        return
    families = ("iptables", "ip6tables")
    versions = {name: run([f"/usr/sbin/{name}", "--version"]) for name in families}
    if not any("nf_tables" in version for version in versions.values()):
        return
    missing = []
    for module in ("nft_limit", "nft_log"):
        try:
            run(["/usr/sbin/modprobe", "--dry-run", module])
        except ValueError:
            missing.append(module)
    if not missing:
        return
    if not (config_root / "ufw-bootstrap.json").exists():
        raise ValueError(
            f"Tegra-Kernel {platform.release()}: {', '.join(missing)} fehlt. "
            "Bestehende Firewall benötigt eine lokale Umstellung auf iptables-legacy."
        )
    defaults = environment(UFW_DEFAULTS)
    if defaults.get("MANAGE_BUILTINS", "no").strip("\"'").lower() != "no":
        raise ValueError("UFW verwaltet Basisregeln; automatische Backend-Umstellung abgebrochen.")
    previous = {}
    for name in families:
        if not only_ufw_rules(run([f"/usr/sbin/{name}-save"])):
            raise ValueError(
                f"{name}: Fremde Firewallregeln vorhanden. Backend lokal prüfen; "
                "keine automatische Umstellung auf Legacy."
            )
        query = run(["/usr/bin/update-alternatives", "--query", name])
        fields = dict(re.findall(r"^(Status|Value): (.+)$", query, re.MULTILINE))
        if fields.get("Status") not in {"auto", "manual"} or not re.fullmatch(
            rf"/(usr/)?sbin/{name}-(nft|legacy)", fields.get("Value", "")
        ):
            raise ValueError(f"Unbekannte {name}-Alternative; automatische Umstellung abgebrochen.")
        previous[name] = fields
    for module in ("xt_limit", "xt_LOG", "nf_log_syslog"):
        run(["/usr/sbin/modprobe", module])
    probe = (
        "*filter\n:YBB_EDGE_UFW_CHECK - [0:0]\n"
        '-A YBB_EDGE_UFW_CHECK -m limit --limit 3/minute -j LOG --log-prefix "[YBB TEST] "\n'
        "COMMIT\n"
    )
    for name in families:
        # --test constructs the candidate ruleset without committing any rules.
        run([f"/usr/sbin/{name}-legacy-restore", "--test", "--noflush"], input=probe)
    print(
        f"Tegra-Kernel ohne {', '.join(missing)}: UFW wird auf iptables-legacy eingerichtet.",
        flush=True,
    )
    # Remove partially loaded UFW rules using the OLD backend first. Keep rule files;
    # never use ufw reset or flush unrelated tables. The installer then enables UFW.
    run(["/usr/sbin/ufw", "--force", "disable"])
    try:
        for name in families:
            run(["/usr/bin/update-alternatives", "--set", name, f"/usr/sbin/{name}-legacy"])
        for name in families:
            if "legacy" not in run([f"/usr/sbin/{name}", "--version"]):
                raise ValueError(f"{name}: Legacy-Backend wurde nicht bestätigt.")
    except Exception as exc:
        recovery_errors = []
        for name, fields in previous.items():
            try:
                args = (
                    ["--auto", name]
                    if fields["Status"] == "auto"
                    else ["--set", name, fields["Value"]]
                )
                run(["/usr/bin/update-alternatives", *args])
            except Exception as recovery:
                recovery_errors.append(f"{name}: {recovery}")
        if recovery_errors:
            raise ValueError(
                f"{exc}; Backend-Wiederherstellung fehlgeschlagen: {'; '.join(recovery_errors)}"
            ) from exc
        raise


def configure_firewall(values: dict[str, str], *, activate_new: bool = False) -> None:
    server, control, plant, clients = firewall_values(values)
    status = run(["/usr/sbin/ufw", "status", "numbered"])
    active = bool(re.search(r"^Status: active\s*$", status, re.MULTILINE))
    # Existing firewall policies are administrator-owned. An update never enables
    # a previously configured, deliberately disabled firewall.
    if not active and not activate_new:
        raise ValueError("Vorhandenes UFW ist deaktiviert. Firewall im Wartungsfenster aktivieren.")
    comments = {
        "YBB Edge control from orchestration server",
        "YBB Edge plant trigger client",
        "YBB Edge port protection",
    }
    numbers = []
    for line in status.splitlines():
        match = re.match(r"\[\s*(\d+)\].*# (.+)$", line)
        if match and match[2].strip() in comments:
            numbers.append(int(match[1]))
    for number in sorted(numbers, reverse=True):
        run(["/usr/sbin/ufw", "--force", "delete", str(number)])
    # Numeric insert 1 is invalid when a UFW address-family rule list is empty.
    # Prepend handles both the first rule and existing IPv4/IPv6 lists.
    for port in (control, plant):
        run(
            [
                "/usr/sbin/ufw",
                "prepend",
                "deny",
                f"{port}/tcp",
                "comment",
                "YBB Edge port protection",
            ]
        )
    for address, port, comment in [
        (server, control, "YBB Edge control from orchestration server"),
        *((client, plant, "YBB Edge plant trigger client") for client in clients),
    ]:
        run(
            [
                "/usr/sbin/ufw",
                "prepend",
                "allow",
                "from",
                address,
                "to",
                "any",
                "port",
                str(port),
                "proto",
                "tcp",
                "comment",
                comment,
            ]
        )
    if not active:
        # UFW was absent: constrain the two Edge ports without changing access to
        # SSH, camera/GigE traffic or other existing services on the running host.
        run(["/usr/sbin/ufw", "default", "allow", "incoming"])
        run(["/usr/sbin/ufw", "--force", "enable"])
    if not re.search(r"^Status: active\s*$", run(["/usr/sbin/ufw", "status"]), re.MULTILINE):
        raise ValueError("UFW-Aktivierung wurde nicht bestätigt.")


def verify_repository(repository: Path) -> dict:
    manifest = json.loads((repository / "manifest.json").read_bytes())
    if (
        manifest.get("schema_version") != "ybb.edge.system-packages.v1"
        or manifest.get("ubuntu_codename") != "noble"
        or manifest.get("architecture") != "arm64"
        or manifest.get("install") != ["ufw"]
    ):
        raise ValueError("Unsupported Edge system package repository.")
    files = manifest.get("files", {})
    if not isinstance(files, dict) or not 2 <= len(files) <= 200 or "Packages" not in files:
        raise ValueError("Invalid Edge system package inventory.")
    for name, expected in files.items():
        if (
            name != "Packages" and not re.fullmatch(r"[A-Za-z0-9_.+~:%-]+\.deb", name)
        ) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("Invalid Ubuntu package filename or checksum.")
        path = repository / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError(f"Ubuntu package checksum mismatch: {name}")
    if {p.name for p in repository.iterdir()} != set(files) | {"manifest.json"}:
        raise ValueError("Unexpected Ubuntu repository files.")
    # Packages may only refer to the verified files in this directory.
    references = re.findall(
        r"^Filename: (.+)$", (repository / "Packages").read_text(), re.MULTILINE
    )
    if {ref.removeprefix("./") for ref in references} != set(files) - {"Packages"} or any(
        not ref.startswith("./") or ref[2:] not in files for ref in references
    ):
        raise ValueError("Ubuntu repository refers to unverified files.")
    return manifest


def install(repository: Path, config_root: Path) -> None:
    verify_repository(repository)
    os_info = platform.freedesktop_os_release()
    if (os_info.get("ID"), os_info.get("VERSION_ID")) != ("ubuntu", "24.04"):
        raise ValueError("Dieses Systempaket benötigt Ubuntu 24.04 Noble.")
    if run(["/usr/bin/dpkg", "--print-architecture"]).strip() != "arm64":
        raise ValueError("Dieses Systempaket benötigt ARM64.")
    values = environment(config_root / "device.env")
    firewall_values(values)  # Validate before installing or changing any rules.
    marker = config_root / "ufw-bootstrap.json"
    if not UFW_PATH.is_file() or marker.exists():
        save(marker, {"managed": True})
        with tempfile.TemporaryDirectory(prefix="ybb-edge-apt-") as folder:
            root = Path(folder)
            shutil.copytree(repository, root / "repo")
            (root / "lists/partial").mkdir(parents=True)
            (root / "archives/partial").mkdir(parents=True)
            (root / "sources.list").write_text(f"deb [trusted=yes] file:{root / 'repo'} ./\n")
            options = [
                "/usr/bin/apt-get",
                "-o",
                f"Dir::Etc::sourcelist={root / 'sources.list'}",
                "-o",
                "Dir::Etc::sourceparts=-",
                "-o",
                f"Dir::State::lists={root / 'lists'}",
                "-o",
                f"Dir::Cache::archives={root / 'archives'}",
                "-o",
                "APT::Sandbox::User=root",
                "-o",
                "Acquire::Languages=none",
                "-o",
                "APT::Get::List-Cleanup=0",
                "-o",
                "Dpkg::Options::=--force-confold",
            ]
            run([*options, "update"])
            arguments = ["--no-remove", "--no-install-recommends", "install", "ufw"]
            run([*options, "--simulate", *arguments])
            run([*options, "--assume-yes", *arguments])
    ensure_firewall_backend(config_root)
    configure_firewall(values, activate_new=marker.exists())
    marker.unlink(missing_ok=True)


def tick(config_root: Path, data_root: Path) -> bool:
    request_path = data_root / "system-package-request.json"
    if not request_path.exists():
        return False
    with request_path.open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("System package request exceeds its size limit.")
    request = json.loads(raw)
    request_id, package_hash = request.get("request_id", ""), request.get("package_sha256", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", request_id) or not re.fullmatch(
        r"[a-f0-9]{64}", package_hash
    ):
        raise ValueError("Invalid system package request.")
    status_path = config_root / "system-package-status.json"
    previous = json.loads(status_path.read_bytes()) if status_path.exists() else {}
    if (
        previous.get("request_id") == request_id
        and previous.get("package_sha256") == package_hash
        and previous.get("phase") in {"succeeded", "failed"}
    ):
        return False
    status = {**request, "phase": "installing", "last_error": None, "observed_at": time.time()}
    save(status_path, status)
    try:
        if (
            previous.get("request_id") == request_id
            and previous.get("package_sha256") != package_hash
        ):
            raise ValueError("System update request ID already used for another package.")
        if (config_root / "settings-rollback.json").exists():
            raise ValueError("Zuerst die Wiederherstellung der Betriebseinstellungen abschließen.")
        package = data_root / "artifacts/sha256" / package_hash[:2] / package_hash
        if (
            not package.resolve().is_relative_to(data_root.resolve())
            or package.stat().st_size > 8 * 1024**3
        ):
            raise ValueError("Invalid system update artifact.")
        with tempfile.TemporaryDirectory(prefix=".system-update-", dir=config_root) as folder:
            root = Path(folder)
            # Copy before verifying. The unprivileged agent cannot change our checked input.
            shutil.copyfile(package, root / "update.zip")
            if digest(root / "update.zip") != package_hash:
                raise ValueError("System update artifact checksum mismatch.")
            with zipfile.ZipFile(root / "update.zip") as archive:
                if (
                    archive.getinfo("release.json").file_size > 1024**2
                    or archive.getinfo("release.sig").file_size > 16384
                ):
                    raise ValueError("System update manifest/signature exceeds its size limit.")
                (root / "release.json").write_bytes(archive.read("release.json"))
                (root / "release.sig").write_bytes(archive.read("release.sig"))
                run(
                    [
                        "/usr/bin/openssl",
                        "dgst",
                        "-sha256",
                        "-verify",
                        str(config_root / "release-signing-public.pem"),
                        "-signature",
                        str(root / "release.sig"),
                        str(root / "release.json"),
                    ]
                )
                release = json.loads((root / "release.json").read_bytes())
                files = release.get("system_packages", {})
                if (
                    release.get("schema_version") != "edge.software.release.v1"
                    or release.get("platform") not in {"jetson_orin_nano", "jetson_agx_orin"}
                    or release.get("architecture") != "aarch64"
                    or not isinstance(files, dict)
                    or not 3 <= len(files) <= 201
                ):
                    raise ValueError("No supported system prerequisites in signed update.")
                if len(archive.namelist()) != len(set(archive.namelist())):
                    raise ValueError("Duplicate files in signed update.")
                repo = root / "repository"
                repo.mkdir()
                total_bytes = 0
                for name, expected in files.items():
                    if name not in {"manifest.json", "Packages"} and not re.fullmatch(
                        r"[A-Za-z0-9_.+~:%-]+\.deb", name
                    ):
                        raise ValueError("Invalid system package path.")
                    info = archive.getinfo("system-packages/" + name)
                    total_bytes += info.file_size
                    if info.file_size > 128 * 1024**2 or total_bytes > 512 * 1024**2:
                        raise ValueError("System package exceeds its size limit.")
                    with archive.open(info) as source, (repo / name).open("wb") as output:
                        shutil.copyfileobj(source, output)
                    if digest(repo / name) != expected:
                        raise ValueError("Signed system package checksum mismatch.")
                install(repo, config_root)
        status["phase"] = "succeeded"
    except Exception as exc:
        status.update(phase="failed", last_error=str(exc)[:2000])
    save(status_path, {**status, "observed_at": time.time()})
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "configure"])
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--config", type=Path, default=Path("/etc/ybb-edge/device.env"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root.")
    try:
        if args.action == "install":
            if args.repository is None:
                parser.error("--repository is required")
            install(args.repository, args.config.parent)
        else:
            configure_firewall(environment(args.config))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Edge-Systempakete konnten nicht eingerichtet werden: {exc}\n")
