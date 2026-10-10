#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

SUPPORTED_PLATFORMS = {"jetson_orin_nano", "jetson_agx_orin"}
TRUSTED_KEY = Path("/etc/ybb-edge/release-signing-public.pem")
ENVIRONMENT_OVERRIDES = {
    "hostname": "YBB_EDGE_HOSTNAME",
    "device_ipv4": "YBB_EDGE_MANAGEMENT_IP",
    "server_ipv4": "YBB_EDGE_SERVER_IP",
    "plant_allowed_ipv4s": "YBB_EDGE_PLANT_ALLOWED_IPS",
    "control_port": "YBB_EDGE_CONTROL_PORT",
    "plant_api_port": "YBB_EDGE_PLANT_API_PORT",
    "rtsp_port": "YBB_EDGE_RTSP_PORT",
    "state_root": "YBB_EDGE_DATA_ROOT",
    "camera_adapters": "YBB_EDGE_CAMERA_ADAPTERS",
}


class InstallerUiError(RuntimeError):
    pass


def bundle_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    for required in (
        "SHA256SUMS",
        "SIGNATURE_POLICY",
        "device.env",
        "platform-profile.json",
        "provisioning.json",
    ):
        if not (root / required).is_file():
            raise InstallerUiError(f"Bundle content is missing: {required}")
    return root


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InstallerUiError(f"JSON object expected: {path}")
    return value


def read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise InstallerUiError("The bundled device defaults are invalid.")
        key, value = line.split("=", 1)
        values[key] = value
    return values


def network_connections() -> list[str]:
    if shutil.which("nmcli") is None:
        return []
    completed = subprocess.run(
        ["nmcli", "-t", "-f", "NAME", "connection", "show"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def default_settings(root: Path) -> dict[str, Any]:
    environment = read_environment(root / "device.env")
    provisioning = read_json(root / "provisioning.json")
    connections = network_connections()
    return {
        "hostname": environment.get("YBB_EDGE_HOSTNAME", "ybb-edge-01"),
        "device_ipv4": environment.get("YBB_EDGE_MANAGEMENT_IP", "192.168.50.21"),
        "prefix_length": str(provisioning.get("prefix_length", 24)),
        "gateway_ipv4": str(provisioning.get("gateway_ipv4", "192.168.50.1")),
        "dns_servers": str(provisioning.get("dns_servers", "192.168.50.1")),
        "server_ipv4": environment.get("YBB_EDGE_SERVER_IP", "192.168.50.10"),
        "plant_allowed_ipv4s": environment.get("YBB_EDGE_PLANT_ALLOWED_IPS", ""),
        "control_port": environment.get("YBB_EDGE_CONTROL_PORT", "9443"),
        "plant_api_port": environment.get("YBB_EDGE_PLANT_API_PORT", "9444"),
        "rtsp_port": environment.get("YBB_EDGE_RTSP_PORT", "8554"),
        "state_root": environment.get("YBB_EDGE_DATA_ROOT", "/var/lib/ybb-edge"),
        "camera_adapters": environment.get("YBB_EDGE_CAMERA_ADAPTERS", "rtsp"),
        "connection": connections[0] if connections else "Wired connection 1",
        "allow_plc_writes": environment.get("YBB_ALLOW_PLC_WRITES") == "true",
        "apply_base_provisioning": True,
        "release_public_key": "",
    }


def _ipv4(value: str, label: str) -> None:
    try:
        ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise InstallerUiError(f"{label} is not a valid IPv4 address.") from exc


def _ipv4_list(value: str, label: str, *, required: bool) -> str:
    entries = [item.strip() for item in value.split(",") if item.strip()]
    if required and not entries:
        raise InstallerUiError(f"{label} must not be empty.")
    for entry in entries:
        _ipv4(entry, label)
    return ",".join(entries)


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(settings)
    hostname = str(normalized.get("hostname") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", hostname):
        raise InstallerUiError("The device hostname is invalid.")
    normalized["hostname"] = hostname
    for key, label in (
        ("device_ipv4", "Edge device IP"),
        ("gateway_ipv4", "Gateway"),
        ("server_ipv4", "Orchestration server IP"),
    ):
        value = str(normalized.get(key) or "").strip()
        _ipv4(value, label)
        normalized[key] = value
    normalized["dns_servers"] = _ipv4_list(
        str(normalized.get("dns_servers") or ""), "DNS servers", required=True
    )
    normalized["plant_allowed_ipv4s"] = _ipv4_list(
        str(normalized.get("plant_allowed_ipv4s") or ""),
        "HTTP trigger clients",
        required=False,
    )
    try:
        prefix = int(str(normalized.get("prefix_length") or ""))
    except ValueError as exc:
        raise InstallerUiError("The IPv4 prefix length must be an integer.") from exc
    if not 8 <= prefix <= 32:
        raise InstallerUiError("The IPv4 prefix length must be between 8 and 32.")
    normalized["prefix_length"] = str(prefix)
    for key, label in (
        ("control_port", "Edge control port"),
        ("plant_api_port", "HTTP trigger port"),
        ("rtsp_port", "RTSP port"),
    ):
        try:
            port = int(str(normalized.get(key) or ""))
        except ValueError as exc:
            raise InstallerUiError(f"{label} must be an integer.") from exc
        if not 1 <= port <= 65535:
            raise InstallerUiError(f"{label} must be between 1 and 65535.")
        normalized[key] = str(port)
    state_root = str(normalized.get("state_root") or "").strip()
    if not PurePosixPath(state_root).is_absolute():
        raise InstallerUiError("The state root must be an absolute Linux path.")
    normalized["state_root"] = state_root
    for key in ("connection", "camera_adapters"):
        normalized[key] = str(normalized.get(key) or "").strip()
        if not normalized[key]:
            raise InstallerUiError(f"{key} must not be empty.")
    for key, value in normalized.items():
        if isinstance(value, str) and any(character in value for character in ("\r", "\n", "\x00")):
            raise InstallerUiError(f"Unsafe control character in {key}.")
    normalized["allow_plc_writes"] = bool(normalized.get("allow_plc_writes", False))
    normalized["apply_base_provisioning"] = bool(
        normalized.get("apply_base_provisioning", True)
    )
    normalized["release_public_key"] = str(
        normalized.get("release_public_key") or ""
    ).strip()
    return normalized


def build_environment(root: Path, settings: dict[str, Any]) -> str:
    values = read_environment(root / "device.env")
    for setting_key, environment_key in ENVIRONMENT_OVERRIDES.items():
        values[environment_key] = str(settings[setting_key])
    values["YBB_ALLOW_PLC_WRITES"] = (
        "true" if settings["allow_plc_writes"] else "false"
    )
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def _run(command: list[str]) -> None:
    print("+ " + " ".join(command), flush=True)
    completed = subprocess.run(command, check=False, text=True)
    if completed.returncode:
        raise InstallerUiError(
            f"Installer command failed with exit code {completed.returncode}: {command[0]}"
        )


def public_key_identity(path: Path) -> tuple[bytes, str]:
    with path.open("rb") as stream:
        raw = stream.read(32769)
    if len(raw) > 32768 or b"PRIVATE KEY" in raw:
        raise InstallerUiError("Nur den öffentlichen RSA-Schlüssel auswählen.")
    public = subprocess.run(
        ["openssl", "rsa", "-pubin", "-pubout"],
        input=raw,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if public.returncode:
        raise InstallerUiError("Die Datei enthält keinen öffentlichen RSA-Schlüssel.")
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "DER"],
        input=public.stdout,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if der.returncode:
        raise InstallerUiError("RSA-Fingerabdruck konnte nicht geprüft werden.")
    return public.stdout, hashlib.sha256(der.stdout).hexdigest()


def install_release_trust(settings: dict[str, Any]) -> None:
    public, fingerprint = public_key_identity(Path(settings["release_public_key"]))
    if TRUSTED_KEY.is_symlink():
        raise InstallerUiError("Der Vertrauensanker darf kein symbolischer Link sein.")
    previous = public_key_identity(TRUSTED_KEY)[1] if TRUSTED_KEY.exists() else None
    if fingerprint != settings.get("selected_key_fingerprint"):
        raise InstallerUiError("Ausgewählter Schlüssel wurde verändert. Erneut prüfen.")
    if previous != settings.get("installed_key_fingerprint"):
        raise InstallerUiError("Installierter Schlüssel wurde verändert. Erneut prüfen.")
    if fingerprint == previous:
        print(f"RSA-Schlüssel bereits hinterlegt. SHA-256 (SPKI): {fingerprint}", flush=True)
        return
    TRUSTED_KEY.parent.mkdir(parents=True, exist_ok=True)
    if TRUSTED_KEY.exists():
        backup = TRUSTED_KEY.with_name(f"{TRUSTED_KEY.name}.{uuid.uuid4().hex}.bak")
        with backup.open("xb") as stream:
            stream.write(TRUSTED_KEY.read_bytes())
    descriptor, temporary = tempfile.mkstemp(prefix=".release-key-", dir=TRUSTED_KEY.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(public)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, TRUSTED_KEY)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(f"RSA-Vertrauen hinterlegt. SHA-256 (SPKI): {fingerprint}", flush=True)


def privileged_install(root: Path, settings_path: Path) -> int:
    if os.geteuid() != 0:
        raise InstallerUiError("The privileged installer phase must run as root.")
    settings_value = read_json(settings_path)
    settings = validate_settings(settings_value)
    if settings.get("trust_only"):
        install_release_trust(settings)
        return 0
    profile = read_json(root / "platform-profile.json")
    if profile.get("platform") not in SUPPORTED_PLATFORMS:
        raise InstallerUiError("This UI only supports Jetson Orin Nano and AGX Orin bundles.")
    release_key = settings["release_public_key"]
    verification_environment = os.environ.copy()
    if release_key:
        verification_environment["YBB_EDGE_TRUSTED_RELEASE_KEY"] = release_key
    print("+ verifying complete offline bundle", flush=True)
    verified = subprocess.run(
        [
            "bash",
            str(root / "deployment/edge/install-jetson-offline.sh"),
            str(root),
            "--verify-only",
        ],
        check=False,
        text=True,
        env=verification_environment,
    )
    if verified.returncode:
        raise InstallerUiError("Bundle integrity or signature verification failed.")
    runtime_directory = Path("/run/ybb-edge-installer")
    runtime_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    generated_environment = runtime_directory / "device.env"
    generated_environment.write_text(
        build_environment(root, settings), encoding="utf-8", newline="\n"
    )
    generated_environment.chmod(0o600)
    if settings["apply_base_provisioning"]:
        command = [
            "bash",
            str(root / "deployment/edge/provisioning/apply-jetson-base.sh"),
            "--hostname",
            settings["hostname"],
            "--address",
            settings["device_ipv4"],
            "--prefix",
            settings["prefix_length"],
            "--gateway",
            settings["gateway_ipv4"],
            "--dns",
            settings["dns_servers"],
            "--server-ip",
            settings["server_ipv4"],
            "--connection",
            settings["connection"],
            "--state-root",
            settings["state_root"],
        ]
        if release_key:
            command.extend(["--release-public-key", release_key])
        _run(command)
    elif release_key:
        release_key_source = Path(release_key)
        if not release_key_source.is_file():
            raise InstallerUiError("The selected release public key does not exist.")
        trust_root = Path("/etc/ybb-edge")
        trust_root.mkdir(mode=0o755, parents=True, exist_ok=True)
        trusted_key = trust_root / "release-signing-public.pem"
        if release_key_source.resolve() != trusted_key.resolve():
            shutil.copyfile(release_key_source, trusted_key)
        trusted_key.chmod(0o644)
    _run(
        [
            "bash",
            str(root / "deployment/edge/install-jetson-offline.sh"),
            str(root),
            "--config",
            str(generated_environment),
        ]
    )
    print("YBB Edge Jetson installation completed.", flush=True)
    return 0


def _write_settings(settings: dict[str, Any]) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix="ybb-edge-install-", suffix=".json")
    path = Path(raw_path)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(settings, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
    path.chmod(0o600)
    return path


def installer_command(script: Path, settings_path: Path) -> list[str]:
    base = [sys.executable, str(script), "--apply", str(settings_path)]
    if os.geteuid() == 0:
        return base
    if shutil.which("pkexec"):
        return ["pkexec", *base]
    if shutil.which("sudo"):
        return ["sudo", *base]
    raise InstallerUiError("Neither pkexec nor sudo is available for elevation.")


def execute_from_ui(
    root: Path,
    settings: dict[str, Any],
    output: Callable[[str], None],
) -> None:
    settings_path = _write_settings(validate_settings(settings))
    try:
        command = installer_command(Path(__file__).resolve(), settings_path)
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            output(line.rstrip())
        return_code = process.wait()
        if return_code:
            raise InstallerUiError(
                f"Installation failed with exit code {return_code}."
            )
    finally:
        settings_path.unlink(missing_ok=True)


def run_graphical_ui(root: Path) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    defaults = default_settings(root)
    window = tk.Tk()
    window.title("YBB Edge Jetson Installer")
    window.geometry("940x820")
    window.minsize(780, 640)
    ttk.Style(window).configure("Title.TLabel", font=("Sans", 17, "bold"))

    container = ttk.Frame(window, padding=12)
    container.pack(fill=tk.BOTH, expand=True)
    ttk.Label(
        container, text="Edge installieren und Release-Vertrauen verwalten", style="Title.TLabel"
    ).pack(anchor="w", pady=(0, 8))
    ttk.Label(
        container,
        text=(
            "Bestehendes Gerät: Runtime-Updates zentral ausrollen. Hier lässt sich auch nur "
            "der öffentliche RSA-Schlüssel hinterlegen."
        ),
        wraplength=870,
    ).pack(anchor="w", pady=(0, 12))
    form = ttk.Frame(container)
    form.pack(fill=tk.X)
    entries: dict[str, Any] = {}
    rows = [
        ("hostname", "Gerätename"),
        ("connection", "NetworkManager-Verbindung"),
        ("device_ipv4", "Edge-Geräte-IP"),
        ("prefix_length", "IPv4-Präfixlänge"),
        ("gateway_ipv4", "Gateway"),
        ("dns_servers", "DNS-Server (Komma)"),
        ("server_ipv4", "Orchestrierungsserver-IP"),
        ("plant_allowed_ipv4s", "Erlaubte HTTP-Trigger-Clients"),
        ("control_port", "Edge-Control-Port"),
        ("plant_api_port", "HTTP-Trigger-Port"),
        ("rtsp_port", "RTSP-Port"),
        ("state_root", "Gemountetes NVMe-Datenverzeichnis"),
        ("camera_adapters", "Kameraadapter (Komma)"),
        ("release_public_key", "Öffentlicher RSA-Schlüssel (PEM)"),
    ]
    connections = network_connections()
    for row, (key, label) in enumerate(rows):
        ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=3)
        if key == "connection" and connections:
            control = ttk.Combobox(form, values=connections)
        else:
            control = ttk.Entry(form)
        control.insert(0, str(defaults[key]))
        control.grid(row=row, column=1, sticky="ew", padx=5, pady=3)
        entries[key] = control
    form.columnconfigure(1, weight=1)

    apply_base = tk.BooleanVar(value=bool(defaults["apply_base_provisioning"]))
    plc_writes = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        form,
        text="Hostname, statische IP, NVMe-State-Root und Leistungsprofil provisionieren",
        variable=apply_base,
    ).grid(row=len(rows), column=1, sticky="w", padx=5, pady=4)
    ttk.Checkbutton(
        form,
        text="SPS-Schreiben aus Pipeline erlauben",
        variable=plc_writes,
        state=tk.DISABLED,
    ).grid(row=len(rows) + 1, column=1, sticky="w", padx=5, pady=4)

    def select_key() -> None:
        selected = filedialog.askopenfilename(
            title="Öffentlichen Release-Schlüssel auswählen",
            filetypes=[("PEM", "*.pem"), ("Alle Dateien", "*")],
        )
        if selected:
            entries["release_public_key"].delete(0, tk.END)
            entries["release_public_key"].insert(0, selected)

    ttk.Button(form, text="Release-Schlüssel auswählen", command=select_key).grid(
        row=len(rows) + 2, column=1, sticky="w", padx=5, pady=3
    )
    log = tk.Text(container, height=12, bg="#191919", fg="#dddddd", state=tk.DISABLED)
    log.pack(fill=tk.BOTH, expand=True, pady=(10, 5))
    actions = ttk.Frame(container)
    actions.pack(fill=tk.X)

    def append(message: str) -> None:
        def update() -> None:
            log.configure(state=tk.NORMAL)
            log.insert(tk.END, message + "\n")
            log.see(tk.END)
            log.configure(state=tk.DISABLED)

        window.after(0, update)

    def values() -> dict[str, Any]:
        result = {key: control.get() for key, control in entries.items()}
        result["apply_base_provisioning"] = apply_base.get()
        result["allow_plc_writes"] = plc_writes.get()
        return result

    install_button: Any

    def worker(settings: dict[str, Any]) -> None:
        try:
            execute_from_ui(root, settings, append)
        except Exception as exc:  # noqa: BLE001 - surfaced in the installer UI
            error_message = str(exc)
            append("FEHLER: " + error_message)
            window.after(
                0,
                lambda message=error_message: messagebox.showerror(
                    "Installation fehlgeschlagen", message
                ),
            )
        else:
            window.after(
                0,
                lambda: messagebox.showinfo(
                    "Vorgang abgeschlossen",
                    "Öffentlicher RSA-Schlüssel hinterlegt."
                    if settings.get("trust_only")
                    else "YBB Edge wurde erfolgreich installiert.",
                ),
            )
        finally:
            window.after(0, lambda: install_button.configure(state=tk.NORMAL))
            window.after(0, lambda: trust_button.configure(state=tk.NORMAL))

    def start_install() -> None:
        try:
            settings = validate_settings(values())
        except InstallerUiError as exc:
            messagebox.showerror("Ungültige Eingabe", str(exc))
            return
        if settings["apply_base_provisioning"] and not messagebox.askyesno(
            "Netzwerk wird geändert",
            "Die Erstprovisionierung setzt die statische IP. Eine Remote-Verbindung kann "
            "dadurch getrennt werden. Fortfahren?",
        ):
            return
        install_button.configure(state=tk.DISABLED)
        trust_button.configure(state=tk.DISABLED)
        append("Installation gestartet …")
        threading.Thread(target=worker, args=(settings,), daemon=True).start()

    def store_trust() -> None:
        try:
            settings = validate_settings(values())
            if not settings["release_public_key"]:
                raise InstallerUiError("Bitte den öffentlichen RSA-Schlüssel auswählen.")
            fingerprint = public_key_identity(Path(settings["release_public_key"]))[1]
            previous = public_key_identity(TRUSTED_KEY)[1] if TRUSTED_KEY.exists() else None
            if not messagebox.askyesno(
                "RSA-Vertrauen hinterlegen",
                f"SHA-256 (SPKI) des ausgewählten Schlüssels:\n{fingerprint}\n\n"
                f"Bisher: {previous or 'Noch nicht eingerichtet'}\n\n"
                "Mit dem Fingerabdruck der Zentrale vergleichen. Nur der öffentliche Schlüssel "
                "wird hinterlegt; Runtime und Gerätekonfiguration bleiben erhalten. Fortfahren?",
            ):
                return
            settings.update(
                trust_only=True,
                selected_key_fingerprint=fingerprint,
                installed_key_fingerprint=previous,
            )
            install_button.configure(state=tk.DISABLED)
            trust_button.configure(state=tk.DISABLED)
            threading.Thread(target=worker, args=(settings,), daemon=True).start()
        except (InstallerUiError, OSError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror("RSA-Schlüssel", str(exc))

    trust_button = ttk.Button(actions, text="Nur RSA-Vertrauen hinterlegen", command=store_trust)
    trust_button.pack(side=tk.LEFT, padx=5)
    install_button = ttk.Button(actions, text="Gerät installieren", command=start_install)
    install_button.pack(side=tk.RIGHT, padx=5)
    ttk.Button(actions, text="Schließen", command=window.destroy).pack(side=tk.RIGHT, padx=5)
    window.mainloop()
    return 0


def _prompt(label: str, default: str) -> str:
    if shutil.which("whiptail") and sys.stdin.isatty():
        completed = subprocess.run(
            [
                "whiptail",
                "--title",
                "YBB Edge Jetson Installer",
                "--inputbox",
                label,
                "10",
                "78",
                default,
            ],
            check=False,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode:
            raise InstallerUiError("Installation was cancelled.")
        return completed.stderr.strip() or default
    answer = input(f"{label} [{default}]: ").strip()
    return answer or default


def _confirm(label: str, *, default: bool) -> bool:
    if shutil.which("whiptail") and sys.stdin.isatty():
        command = [
            "whiptail",
            "--title",
            "YBB Edge Jetson Installer",
        ]
        if not default:
            command.append("--defaultno")
        command.extend(["--yesno", label, "10", "78"])
        return subprocess.run(command, check=False).returncode == 0
    default_text = "j" if default else "n"
    return _prompt(label + " (j/n)", default_text).lower() == "j"


def run_terminal_ui(root: Path) -> int:
    print("YBB Edge Jetson Installer (Terminal-UI)")
    print("Die grafische Tk-Oberfläche ist auf diesem Image nicht verfügbar.\n")
    settings = default_settings(root)
    for key, label in (
        ("hostname", "Gerätename"),
        ("connection", "NetworkManager-Verbindung"),
        ("device_ipv4", "Edge-Geräte-IP"),
        ("prefix_length", "IPv4-Präfixlänge"),
        ("gateway_ipv4", "Gateway"),
        ("dns_servers", "DNS-Server (Komma)"),
        ("server_ipv4", "Orchestrierungsserver-IP"),
        ("plant_allowed_ipv4s", "HTTP-Trigger-Clients"),
        ("state_root", "NVMe-Datenverzeichnis"),
        ("camera_adapters", "Kameraadapter"),
    ):
        settings[key] = _prompt(label, str(settings[key]))
    settings["apply_base_provisioning"] = _confirm(
        "Basisprovisionierung ausführen?", default=True
    )
    settings["allow_plc_writes"] = _confirm(
        "SPS-Schreiben aus Pipeline erlauben?", default=False
    )
    execute_from_ui(root, validate_settings(settings), print)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="YBB Edge Jetson installer UI")
    parser.add_argument("--apply", type=Path)
    args = parser.parse_args()
    root = bundle_root()
    profile = read_json(root / "platform-profile.json")
    if profile.get("platform") not in SUPPORTED_PLATFORMS:
        raise InstallerUiError("This installer only supports Jetson Orin Nano and AGX Orin.")
    if args.apply is not None:
        return privileged_install(root, args.apply)
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return run_terminal_ui(root)
    try:
        import tkinter  # noqa: F401

        return run_graphical_ui(root)
    except ImportError:
        return run_terminal_ui(root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallerUiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
