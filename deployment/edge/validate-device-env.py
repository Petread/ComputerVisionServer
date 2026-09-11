#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

ALLOWED_KEYS = {
    "YBB_ALLOW_PLC_WRITES",
    "YBB_EDGE_ARCHITECTURE",
    "YBB_EDGE_ARTIFACT_QUOTA_BYTES",
    "YBB_EDGE_CAMERA_ADAPTERS",
    "YBB_EDGE_CAPABILITY_INVENTORY",
    "YBB_EDGE_CONTROL_PORT",
    "YBB_EDGE_CUDA_VERSION",
    "YBB_EDGE_DATA_ROOT",
    "YBB_EDGE_DEEPSTREAM_VERSION",
    "YBB_EDGE_ENGINE_BUILDER_MODE",
    "YBB_EDGE_HOSTNAME",
    "YBB_EDGE_JETPACK_VERSION",
    "YBB_EDGE_L4T_VERSION",
    "YBB_EDGE_MANAGEMENT_IP",
    "YBB_EDGE_MINIMUM_DISK_FREE_BYTES",
    "YBB_EDGE_OS_VERSION",
    "YBB_EDGE_OUTBOX_QUOTA_BYTES",
    "YBB_EDGE_PLANT_ALLOWED_IPS",
    "YBB_EDGE_PLANT_API_PORT",
    "YBB_EDGE_PLATFORM",
    "YBB_EDGE_RTSP_PORT",
    "YBB_EDGE_SERVER_IP",
    "YBB_EDGE_TENSORRT_VERSION",
    "YBB_EDGE_TRTEXEC_PATH",
}
REQUIRED_KEYS = ALLOWED_KEYS


class DeviceEnvironmentError(ValueError):
    pass


def read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise DeviceEnvironmentError(f"Invalid device environment line {line_number}.")
        key, value = line.split("=", 1)
        if key not in ALLOWED_KEYS:
            raise DeviceEnvironmentError(f"Unsupported device environment key: {key!r}")
        if key in values:
            raise DeviceEnvironmentError(f"Duplicate device environment key: {key!r}")
        if any(character in value for character in ("\r", "\n", "\x00")):
            raise DeviceEnvironmentError(f"Unsafe control character in {key}.")
        values[key] = value
    missing = sorted(REQUIRED_KEYS - values.keys())
    if missing:
        raise DeviceEnvironmentError(
            "Device environment is missing required keys: " + ", ".join(missing)
        )
    return values


def _ipv4(value: str, key: str) -> None:
    try:
        ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise DeviceEnvironmentError(f"{key} must be a valid IPv4 address.") from exc


def _integer(values: dict[str, str], key: str, minimum: int, maximum: int) -> None:
    try:
        observed = int(values[key])
    except ValueError as exc:
        raise DeviceEnvironmentError(f"{key} must be an integer.") from exc
    if not minimum <= observed <= maximum:
        raise DeviceEnvironmentError(
            f"{key} must be between {minimum} and {maximum}."
        )
    values[key] = str(observed)


def validate_environment(
    values: dict[str, str], profile: dict[str, object]
) -> dict[str, str]:
    metadata = {
        "YBB_EDGE_PLATFORM": profile.get("platform") or "",
        "YBB_EDGE_ARCHITECTURE": profile.get("architecture") or "",
        "YBB_EDGE_OS_VERSION": profile.get("base_os") or "",
        "YBB_EDGE_JETPACK_VERSION": profile.get("jetpack_version") or "",
        "YBB_EDGE_L4T_VERSION": profile.get("l4t_version") or "",
        "YBB_EDGE_CUDA_VERSION": profile.get("cuda_version") or "",
        "YBB_EDGE_TENSORRT_VERSION": profile.get("tensorrt_version") or "",
        "YBB_EDGE_DEEPSTREAM_VERSION": profile.get("deepstream_version") or "",
    }
    for key, expected in metadata.items():
        if values[key] != str(expected):
            raise DeviceEnvironmentError(
                f"{key} does not match the signed platform profile."
            )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", values["YBB_EDGE_HOSTNAME"]):
        raise DeviceEnvironmentError("YBB_EDGE_HOSTNAME is invalid.")
    _ipv4(values["YBB_EDGE_MANAGEMENT_IP"], "YBB_EDGE_MANAGEMENT_IP")
    _ipv4(values["YBB_EDGE_SERVER_IP"], "YBB_EDGE_SERVER_IP")
    plant_ips = []
    for item in values["YBB_EDGE_PLANT_ALLOWED_IPS"].split(","):
        candidate = item.strip()
        if candidate:
            _ipv4(candidate, "YBB_EDGE_PLANT_ALLOWED_IPS")
            plant_ips.append(candidate)
    values["YBB_EDGE_PLANT_ALLOWED_IPS"] = ",".join(plant_ips)
    for key in (
        "YBB_EDGE_CONTROL_PORT",
        "YBB_EDGE_PLANT_API_PORT",
        "YBB_EDGE_RTSP_PORT",
    ):
        _integer(values, key, 1, 65535)
    for key in (
        "YBB_EDGE_ARTIFACT_QUOTA_BYTES",
        "YBB_EDGE_OUTBOX_QUOTA_BYTES",
        "YBB_EDGE_MINIMUM_DISK_FREE_BYTES",
    ):
        _integer(values, key, 0, 2**63 - 1)
    plc_writes = values["YBB_ALLOW_PLC_WRITES"].strip().lower()
    if plc_writes not in {"true", "false"}:
        raise DeviceEnvironmentError("YBB_ALLOW_PLC_WRITES must be true or false.")
    values["YBB_ALLOW_PLC_WRITES"] = plc_writes
    if values["YBB_EDGE_CAPABILITY_INVENTORY"] != "probe":
        raise DeviceEnvironmentError("Capability inventory must use the productive probe mode.")
    if values["YBB_EDGE_ENGINE_BUILDER_MODE"] not in {
        "tensorrt_python",
        "trtexec",
        "simulated",
    }:
        raise DeviceEnvironmentError("Unsupported TensorRT engine builder mode.")
    platform = str(profile["platform"])
    data_root = values["YBB_EDGE_DATA_ROOT"]
    if platform == "windows_11":
        if not PureWindowsPath(data_root).is_absolute():
            raise DeviceEnvironmentError("Windows data root must be an absolute path.")
    elif not PurePosixPath(data_root).is_absolute():
        raise DeviceEnvironmentError("Jetson data root must be an absolute path.")
    approved = {str(item) for item in profile.get("approved_camera_adapters", [])}
    adapters = [
        item.strip() for item in values["YBB_EDGE_CAMERA_ADAPTERS"].split(",") if item.strip()
    ]
    invalid = sorted(set(adapters) - approved)
    if invalid:
        raise DeviceEnvironmentError(
            "Camera adapters are not approved for this platform: " + ", ".join(invalid)
        )
    values["YBB_EDGE_CAMERA_ADAPTERS"] = ",".join(adapters)
    return values


def write_environment(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{key}={values[key]}\n" for key in sorted(values)),
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and canonicalize YBB device settings.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    if not isinstance(profile, dict):
        raise DeviceEnvironmentError("Platform profile must be a JSON object.")
    values = validate_environment(read_environment(args.input), profile)
    write_environment(args.output, values)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeviceEnvironmentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
