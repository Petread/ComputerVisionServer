#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

port = int(os.getenv("YBB_EDGE_CONTROL_PORT", "9443"))
host = os.getenv("YBB_EDGE_MANAGEMENT_IP", "127.0.0.1")
url = f"http://{host}:{port}/edge/v1/health"
software_status = (
    Path(os.getenv("YBB_EDGE_DATA_ROOT", "/var/lib/ybb-edge")) / "software/status.json"
)
if software_status.is_file():
    try:
        software = json.loads(software_status.read_text())
        age = (datetime.now(UTC) - datetime.fromisoformat(software["updated_at"])).total_seconds()
        if software.get("phase") in {"activating", "checking"} and 0 <= age < 120:
            # The launcher performs its own bounded health check and rollback during this window.
            raise SystemExit(0)
    except (OSError, ValueError, KeyError):
        pass
try:
    # Target is the fixed local interface from the root-owned device environment.
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"health returned HTTP {response.status}")
except Exception as exc:  # noqa: BLE001 - watchdog must signal every health failure
    print(f"YBB Edge watchdog failed: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc
