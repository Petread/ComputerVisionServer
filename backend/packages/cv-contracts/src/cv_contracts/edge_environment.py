"""Operator-editable Edge environment, also copied into the privileged installer helper.

Keep this module stdlib-only: the helper must never import device-writable wheels.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re

# key: (default, minimum, maximum). Ports, identity, executables and trust stay local.
LIMITS = {
    "YBB_EDGE_PLANT_MAX_REQUEST_BYTES": (65536, 1024, 1048576),
    "YBB_EDGE_PLANT_MAX_CONCURRENT_TRIGGERS": (4, 1, 32),
    "YBB_EDGE_PLANT_TRIGGER_TIMEOUT_MS": (2500, 100, 120000),
    "YBB_EDGE_PLANT_RATE_LIMIT_PER_SECOND": (20, 1, 1000),
    "YBB_EDGE_PREVIEW_JPEG_QUALITY": (70, 10, 95),
    "YBB_EDGE_PREVIEW_MAX_BYTES": (1048576, 65536, 8388608),
    "YBB_EDGE_ENGINE_BUILD_TIMEOUT_SECONDS": (900, 60, 7200),
}
IP_KEY = "YBB_EDGE_PLANT_ALLOWED_IPS"
DEFAULTS = {IP_KEY: "", **{key: str(spec[0]) for key, spec in LIMITS.items()}}


def validate_values(values: dict[str, str], *, complete: bool = True) -> dict[str, str]:
    if not isinstance(values, dict) or values.keys() - DEFAULTS.keys():
        raise ValueError("Only the documented Edge operating settings may be changed.")
    if complete and values.keys() != DEFAULTS.keys():
        raise ValueError("A complete settings snapshot is required.")
    result = {}
    for key, value in values.items():
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError(f"Invalid value for {key}.")
        if key == IP_KEY:
            addresses = []
            for item in value.split(","):
                if not item.strip():
                    continue
                address = ipaddress.IPv4Address(item.strip())
                if (
                    address.is_unspecified
                    or address.is_multicast
                    or address.is_loopback
                    or int(address) == 2**32 - 1
                ):
                    raise ValueError(
                        "Trigger clients must be individual remote unicast IPv4 addresses."
                    )
                addresses.append(str(address))
            if len(addresses) > 128:
                raise ValueError("At most 128 trigger clients are supported.")
            result[key] = ",".join(sorted(set(addresses), key=ipaddress.IPv4Address))
        else:
            if not re.fullmatch(r"[0-9]{1,8}", value):
                raise ValueError(f"{key} must be an integer.")
            _, minimum, maximum = LIMITS[key]
            if not minimum <= int(value) <= maximum:
                raise ValueError(f"{key} must be between {minimum} and {maximum}.")
            result[key] = str(int(value))
    return result


def revision(values: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def validate_command(command: dict) -> dict:
    if not isinstance(command, dict) or set(command) != {
        "request_id",
        "expected_revision",
        "values",
    }:
        raise ValueError("Invalid settings request.")
    if not isinstance(command["request_id"], str) or not re.fullmatch(
        r"[a-zA-Z0-9-]{8,80}", command["request_id"]
    ):
        raise ValueError("Invalid settings request ID.")
    if not isinstance(command["expected_revision"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", command["expected_revision"]
    ):
        raise ValueError("Invalid settings revision.")
    return {**command, "values": validate_values(command["values"])}
