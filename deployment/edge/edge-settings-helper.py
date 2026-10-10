"""Privileged, installer-owned helper. Accepts only the bounded operating settings schema.

Linux: systemd timer as root. Windows: scheduled task as SYSTEM. Never import Edge wheels.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

_spec = importlib.util.spec_from_file_location(
    "edge_environment", Path(__file__).with_name("edge_environment.py")
)
schema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(schema)
WINDOWS = os.name == "nt"
system_packages = None
if not WINDOWS and Path(__file__).with_name("edge-system-packages.py").is_file():
    system_spec = importlib.util.spec_from_file_location(
        "edge_system_packages", Path(__file__).with_name("edge-system-packages.py")
    )
    system_packages = importlib.util.module_from_spec(system_spec)
    system_spec.loader.exec_module(system_packages)


def read_json(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("Settings document exceeds 16 KiB.")
    return json.loads(raw)


def atomic_write(path: Path, content: bytes) -> None:
    # Destinations are always installer-owned fixed paths, never request-provided paths.
    metadata = path.stat() if path.exists() else None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, (metadata.st_mode & 0o777) if metadata else 0o644)
            if metadata:
                os.chown(temporary, metadata.st_uid, metadata.st_gid)
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # WinSW, scanners or a status reader can briefly hold a Windows file handle.
                if os.name != "nt" or attempt == 9:
                    raise
                time.sleep(0.1)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_environment(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        if key in values:
            raise ValueError("Duplicate device environment key.")
        values[key] = value
    return values


def run(args: list[str]) -> str:
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ).stdout


def powershell(script: str) -> str:
    executable = str(
        Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    encoded = base64.b64encode(
        ("$ErrorActionPreference='Stop'; " + script).encode("utf-16-le")
    ).decode()
    return run([executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])


def ufw_rules() -> str:
    try:
        output = run(["/usr/sbin/ufw", "status", "numbered"])
    except FileNotFoundError as exc:
        if system_packages is not None:
            raise ValueError(
                "UFW ist auf dem Edge nicht installiert. Ein aktuelles Jetson-Runtime-Paket "
                "mit Ubuntu-Systempaketen über 'Runtime aktualisieren' installieren."
            ) from exc
        raise ValueError(
            "UFW ist auf dem Edge nicht installiert. Ein neu gebautes, zur Ubuntu-Version "
            "passendes Edge-Offlinepaket einmalig installieren; es bringt UFW und die "
            "Unterstützung für zukünftige Systempaket-Updates mit."
        ) from exc
    if not re.search(r"^Status: active\s*$", output, re.MULTILINE):
        raise ValueError(
            "UFW muss auf dem Edge aktiv sein, bevor Trigger-Freigaben verwaltet werden können."
        )
    return output


def readiness_error() -> str | None:
    if not WINDOWS:
        try:
            ufw_rules()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return str(exc)[:2000]
    return None


def firewall(values: dict[str, str]) -> None:
    port = int(values["YBB_EDGE_PLANT_API_PORT"])
    if not 1 <= port <= 65535:
        raise ValueError("Invalid installed trigger port.")
    clients = schema.validate_values(
        {schema.IP_KEY: values.get(schema.IP_KEY, "")}, complete=False
    )[schema.IP_KEY].split(",")
    clients = [ip for ip in clients if ip]
    if WINDOWS:
        # Remove both the old installer rule and our replacement. Other rules are untouched.
        powershell(
            "Get-NetFirewallRule -DisplayName 'YBB Edge Plant Trigger' "
            "-ErrorAction SilentlyContinue | Remove-NetFirewallRule"
        )
        if clients:
            addresses = ",".join("'" + ip + "'" for ip in clients)
            powershell(
                "New-NetFirewallRule -DisplayName 'YBB Edge Plant Trigger' "
                f"-Direction Inbound -Action Allow -Protocol TCP -LocalPort {port} "
                f"-RemoteAddress @({addresses}) -Profile Any | Out-Null"
            )
        return
    # UFW's numbered format identifies only the rules created by our existing installer.
    # Insert allows before the existing default deny; remove old clients (including revoked IPs).
    output = ufw_rules()
    numbers = []
    for line in output.splitlines():
        match = re.match(r"\[\s*(\d+)\]", line)
        if match and "# YBB Edge plant trigger client" in line:
            numbers.append(int(match[1]))
    for number in sorted(numbers, reverse=True):
        run(["/usr/sbin/ufw", "--force", "delete", str(number)])
    # A final explicit deny also covers devices installed before configure-ufw.sh was run.
    run(["/usr/sbin/ufw", "deny", f"{port}/tcp"])
    for client in reversed(clients):
        run(
            [
                "/usr/sbin/ufw",
                "prepend",
                "allow",
                "from",
                client,
                "to",
                "any",
                "port",
                str(port),
                "proto",
                "tcp",
                "comment",
                "YBB Edge plant trigger client",
            ]
        )


class SettingsHelper:
    def __init__(self, config_root: Path, data_root: Path) -> None:
        self.config_root, self.data_root = config_root, data_root
        self.env_path = config_root / "device.env"
        self.status_path = config_root / "settings-status.json"
        self.journal_path = config_root / "settings-rollback.json"
        self.status = (
            read_json(self.status_path) if self.status_path.exists() else {"phase": "idle"}
        )

    def save(self, **values: object) -> None:
        self.status.update(
            values,
            protocol_version=1,
            observed_at=time.time(),
            recovery_pending=self.journal_path.exists(),
            system_update_protocol=1 if system_packages is not None else 0,
        )
        atomic_write(self.status_path, json.dumps(self.status).encode())

    def restart(self) -> None:
        if WINDOWS:
            powershell("Restart-Service -Name 'YBBEdgeAgent' -Force")
        else:
            run(["/usr/bin/systemctl", "restart", "ybb-edge-agent.service"])

    def observe(self, environment: dict[str, str]) -> dict:
        address = str(ipaddress.IPv4Address(environment["YBB_EDGE_MANAGEMENT_IP"]))
        port = int(environment.get("YBB_EDGE_CONTROL_PORT", "9443"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{address}:{port}/edge/v1/settings", timeout=3) as response:
            return json.load(response)

    def healthy(self, environment: dict[str, str]) -> bool:
        address = str(ipaddress.IPv4Address(environment["YBB_EDGE_MANAGEMENT_IP"]))
        port = int(environment.get("YBB_EDGE_CONTROL_PORT", "9443"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{address}:{port}/edge/v1/health", timeout=3) as response:
            health = json.load(response)
        with socket.create_connection(
            (address, int(environment["YBB_EDGE_PLANT_API_PORT"])), timeout=3
        ):
            return health.get("state") == "ok"

    def confirm(self, environment: dict[str, str], expected: dict[str, str], old_boot: str) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                current = self.observe(environment)
                if (
                    current.get("boot_id") != old_boot
                    and current.get("revision") == schema.revision(expected)
                    and self.healthy(environment)
                ):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise ValueError("Der neu gestartete Agent hat die Einstellungen nicht bestätigt.")

    def write_values(self, values: dict[str, str]) -> None:
        original = self.env_path.read_text(encoding="utf-8-sig")
        # Preserve all unrelated settings and comments, including installer defaults and keys.
        lines = [line for line in original.splitlines() if line.split("=", 1)[0] not in values]
        atomic_write(
            self.env_path,
            (
                "\n".join(lines)
                + "\n"
                + "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
            ).encode(),
        )
        if WINDOWS:
            path = self.config_root / "ybb-edge-service.xml"
            root = ElementTree.fromstring(path.read_bytes())
            for key, value in values.items():
                nodes = [node for node in root.findall("env") if node.get("name") == key]
                node = nodes[0] if nodes else ElementTree.SubElement(root, "env", name=key)
                node.set("value", value)
                for duplicate in nodes[1:]:
                    root.remove(duplicate)
            atomic_write(path, ElementTree.tostring(root, encoding="utf-8"))

    def rollback(self) -> None:
        journal = read_json(self.journal_path)
        self.write_values(schema.validate_values(journal["values"]))
        old = read_environment(self.env_path)
        firewall(old)
        self.restart()
        self.confirm(old, journal["values"], "")
        self.journal_path.unlink()

    def tick(self) -> None:
        if system_packages is not None and not self.journal_path.exists():
            if system_packages.tick(self.config_root, self.data_root):
                self.save(readiness_error=readiness_error())
                return
        self.save(readiness_error=readiness_error())
        if self.journal_path.exists():
            try:
                self.rollback()
                self.save(
                    phase="failed",
                    last_error="Unterbrochene Änderung: vorherige Einstellungen wiederhergestellt.",
                )
            except Exception as exc:
                self.save(
                    phase="failed", last_error=f"Wiederherstellung fehlgeschlagen: {exc}"[:2000]
                )
            return
        path = self.data_root / "settings-request.json"
        if not path.exists():
            return
        request = schema.validate_command(read_json(path))
        if request["request_id"] == self.status.get("request_id"):
            if self.status.get("phase") == "applying":
                # Covers a crash before the journal was written or after it was removed.
                self.save(
                    phase="failed",
                    last_error=(
                        "Einstellungsauftrag wurde unterbrochen. Aktuellen Gerätestand prüfen."
                    ),
                )
            return
        self.save(request_id=request["request_id"], phase="applying", last_error=None)
        try:
            environment = read_environment(self.env_path)
            before = self.observe(environment)
            if before["revision"] != request["expected_revision"]:
                raise ValueError("Veralteter Einstellungsstand. Aktuelle Werte neu laden.")
            software = self.data_root / "software"
            if any(
                not (software / "completed" / entry.name).exists()
                for entry in (software / "requests").glob("*.json")
            ):
                raise ValueError("Ein Software-Update läuft bereits.")
            if not WINDOWS:
                ufw_rules()  # Readiness check before creating a rollback obligation.
            journal = {"values": schema.validate_values(before["values"])}
            atomic_write(self.journal_path, json.dumps(journal).encode())
            # Do not expose unrelated env or secrets through the status file.
            firewall({**environment, **request["values"]})
            self.write_values(request["values"])
            self.restart()
            self.confirm(environment, request["values"], before["boot_id"])
            self.journal_path.unlink()
            self.save(phase="succeeded")
        except Exception as exc:
            message = str(exc)
            if self.journal_path.exists():
                try:
                    self.rollback()
                except Exception as recovery:
                    message += f"; Wiederherstellung fehlgeschlagen: {recovery}"
            self.save(phase="failed", last_error=message[:2000])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-root", type=Path, default=Path("/etc/ybb-edge"))
    parser.add_argument("--data-root", type=Path, default=Path("/var/lib/ybb-edge"))
    args = parser.parse_args()
    # OS scheduler guarantees one instance. Paths are fixed by the administrator at install time.
    SettingsHelper(args.config_root, args.data_root).tick()
