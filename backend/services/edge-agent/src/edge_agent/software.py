"""Durable handoff to the installed launcher; requests never execute client supplied code."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from cv_contracts.edge import EdgeSoftwareCommand, EdgeSoftwareStatus


def software_status(data_root: Path) -> EdgeSoftwareStatus:
    path = data_root / "software" / "status.json"
    if not path.exists():
        return EdgeSoftwareStatus()
    return EdgeSoftwareStatus.model_validate_json(path.read_bytes())


def queue_software_command(data_root: Path, command: EdgeSoftwareCommand) -> EdgeSoftwareStatus:
    if os.getenv("YBB_EDGE_SOFTWARE_LAUNCHER") != "1":
        raise ValueError("This device needs the one-time launcher installation.")
    if command.action == "update" and not command.package_sha256:
        raise ValueError("An update requires a verified uploaded package digest.")
    root = data_root / "software" / "requests"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{command.request_id}.json"
    raw = command.model_dump_json()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=root, suffix=".tmp", mode="w", encoding="utf-8", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError:
        if json.loads(path.read_text()) != json.loads(raw):
            raise ValueError("Software command idempotency key has a different request.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    current = software_status(data_root)
    if current.request_id != command.request_id:
        return current.model_copy(update={"request_id": command.request_id, "phase": "queued"})
    return current
