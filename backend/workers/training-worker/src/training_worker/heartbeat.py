from __future__ import annotations

from cv_utils import utc_now_iso


def build_heartbeat(service_name: str) -> dict[str, str]:
    return {"service_name": service_name, "observed_at": utc_now_iso()}

