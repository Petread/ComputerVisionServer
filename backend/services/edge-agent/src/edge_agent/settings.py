"""Bounded settings handoff to the OS helper installed with the Edge installer."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from cv_contracts.edge import EdgeSettingsCommand, EdgeSettingsStatus
from cv_contracts.edge_environment import revision, validate_values

if TYPE_CHECKING:
    from edge_agent.state import EdgeAgentState

_LOCK = threading.RLock()
_FIELDS = {
    "YBB_EDGE_PLANT_MAX_REQUEST_BYTES": "plant_max_request_bytes",
    "YBB_EDGE_PLANT_MAX_CONCURRENT_TRIGGERS": "plant_max_concurrent_triggers",
    "YBB_EDGE_PLANT_TRIGGER_TIMEOUT_MS": "plant_trigger_timeout_ms",
    "YBB_EDGE_PLANT_RATE_LIMIT_PER_SECOND": "plant_rate_limit_per_second",
    "YBB_EDGE_PREVIEW_JPEG_QUALITY": "preview_jpeg_quality",
    "YBB_EDGE_PREVIEW_MAX_BYTES": "preview_max_bytes",
    "YBB_EDGE_ENGINE_BUILD_TIMEOUT_SECONDS": "engine_build_timeout_seconds",
}


def config_root() -> Path:
    return Path(os.getenv("YBB_EDGE_SETTINGS_ROOT", "/etc/ybb-edge"))


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            value = json.loads(stream.read(16385))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def settings_status(state: EdgeAgentState) -> EdgeSettingsStatus:
    values = {key: str(int(getattr(state.config, field))) for key, field in _FIELDS.items()}
    values["YBB_EDGE_PLANT_ALLOWED_IPS"] = ",".join(state.config.plant_allowed_ipv4s)
    # Existing locally configured values remain visible even outside the editable limits.
    try:
        values = validate_values(values)
    except ValueError:
        pass
    helper = _read(config_root() / "settings-status.json")
    available = (
        helper.get("protocol_version") == 1 and time.time() - helper.get("observed_at", 0) < 180
    )
    message = None
    if not available:
        message = (
            "Einstellungsdienst fehlt oder ist nicht erreichbar. Den aktuellen "
            "Edge-Offline-Installer einmalig auf dem Gerät ausführen."
        )
    elif helper.get("recovery_pending"):
        available = False
        message = "Die Wiederherstellung der vorherigen Einstellungen läuft. Fehlerdetails prüfen."
    elif helper.get("readiness_error"):
        available = False
        message = str(helper["readiness_error"])
    request = _read(state.config.data_root / "settings-request.json")
    phase = helper.get("phase", "idle")
    request_id = helper.get("request_id")
    if request and request.get("request_id") != request_id:
        request_id, phase = request.get("request_id"), "queued"
    return EdgeSettingsStatus(
        writable=available,
        message=message,
        boot_id=state.boot_id,
        revision=revision(values),
        values=values,
        request_id=request_id,
        phase=phase,
        last_error=helper.get("last_error") if helper.get("request_id") == request_id else None,
    )


def queue_settings(state: EdgeAgentState, command: EdgeSettingsCommand) -> EdgeSettingsStatus:
    from edge_agent.software import software_status

    with _LOCK:
        status = settings_status(state)
        if not status.writable:
            raise ValueError(status.message)
        path = state.config.data_root / "settings-request.json"
        previous = _read(path)
        payload = command.model_dump(mode="json")
        if previous.get("request_id") == command.request_id:
            if previous != payload:
                raise ValueError(
                    "Die Auftrags-ID wurde bereits für andere Einstellungen verwendet."
                )
            return status
        if status.phase in {"queued", "applying"}:
            raise ValueError("Eine Einstellungsänderung läuft bereits.")
        if status.revision != command.expected_revision:
            raise ValueError("Einstellungen wurden inzwischen geändert. Aktuellen Stand neu laden.")
        software = software_status(state.config.data_root)
        if system_packages_busy() or software.phase not in {"idle", "succeeded", "failed", "rolled_back"} or any(
            not (state.config.data_root / "software/completed" / item.name).exists()
            for item in (state.config.data_root / "software/requests").glob("*.json")
        ):
            raise ValueError("Zuerst das laufende Software-Update abschließen.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, delete=False, mode="w", encoding="utf-8"
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return settings_status(state)


def settings_busy(state: EdgeAgentState) -> bool:
    return system_packages_busy() or settings_status(state).phase in {"queued", "applying"} or bool(
        _read(config_root() / "settings-status.json").get("recovery_pending")
    )


def system_packages_busy() -> bool:
    # A root operation may still finish after the unprivileged launcher's timeout.
    return _read(config_root() / "system-package-status.json").get("phase") == "installing"
