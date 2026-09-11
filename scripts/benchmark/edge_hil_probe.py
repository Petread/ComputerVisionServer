from __future__ import annotations

import argparse
import json
import platform
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect the YBB Jetson M0 HIL inventory.")
    parser.add_argument("--management-ip", required=True, type=IPv4Address)
    parser.add_argument("--server-ip", required=True, type=IPv4Address)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--metrics-json",
        type=Path,
        help="Optional runtime benchmark metrics to validate and embed.",
    )
    args = parser.parse_args()

    metrics = _read_metrics(args.metrics_json) if args.metrics_json else None
    report = {
        "schema_version": "ybb.edge-hil.inventory.v1",
        "status": "observed_hardware",
        "observed_at": datetime.now(UTC).isoformat(),
        "hostname": socket.gethostname(),
        "management_ip": str(args.management_ip),
        "server_ip": str(args.server_ip),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
        },
        "jetson": {
            "nv_tegra_release": _read_text(Path("/etc/nv_tegra_release")),
            "device_model": _read_text(Path("/proc/device-tree/model")),
        },
        "commands": {
            "trtexec": _probe_command(["trtexec", "--version"]),
            "gst_inspect": _probe_command(["gst-inspect-1.0", "--version"]),
            "deepstream": _probe_command(["deepstream-app", "--version-all"]),
            "ip_address": _probe_command(["ip", "-json", "address", "show"]),
            "server_ping": _probe_command(["ping", "-c", "1", "-W", "2", str(args.server_ip)]),
        },
        "packages": {
            name: _probe_command(["dpkg-query", "-W", "-f=${Version}", name])
            for name in [
                "nvidia-jetpack",
                "nvidia-l4t-core",
                "nvidia-cuda",
                "tensorrt",
                "deepstream",
            ]
        },
        "storage": _storage_report(Path("/srv/ybb-edge")),
        "metrics": metrics,
    }
    serialized = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{serialized}\n", encoding="utf-8")
    print(serialized)


def _probe_command(command: list[str]) -> dict[str, Any]:
    executable = shutil.which(command[0])
    if executable is None:
        return {"available": False, "exit_code": None, "output": None}
    try:
        completed = subprocess.run(
            [executable, *command[1:]],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": True, "exit_code": None, "output": str(exc)}
    output = (completed.stdout or completed.stderr).strip()
    return {
        "available": True,
        "exit_code": completed.returncode,
        "output": output[:4000],
    }


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip("\x00\n ")
    except OSError:
        return None


def _storage_report(path: Path) -> dict[str, Any]:
    probe_path = path if path.exists() else Path("/")
    usage = shutil.disk_usage(probe_path)
    return {
        "path": str(probe_path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def _read_metrics(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "capture_fps",
        "capture_height",
        "capture_width",
        "cpu_utilization_avg_pct",
        "disk_used_delta_bytes",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "frame_sampling_interval",
        "golden_input_passed",
        "gpu_memory_used_peak_bytes",
        "gpu_utilization_avg_pct",
        "ram_used_peak_bytes",
        "source_fps",
        "processed_fps",
        "drop_fps",
        "tensorrt_build_seconds",
        "tensorrt_engine_size_bytes",
        "warmup_seconds",
        "measurement_seconds",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"Metrics JSON is missing required keys: {', '.join(missing)}")
    if payload["warmup_seconds"] < 300 or payload["measurement_seconds"] < 1800:
        raise ValueError(
            "HIL metrics require at least 5 minutes warmup and 30 minutes measurement."
        )
    if (
        payload["capture_width"] < 1920
        or payload["capture_height"] < 1080
        or payload["capture_fps"] < 30
    ):
        raise ValueError("HIL capture must provide at least 1920x1080 at 30 FPS.")
    if payload["frame_sampling_interval"] < 1:
        raise ValueError("Frame sampling interval must be at least one.")
    if payload["golden_input_passed"] is not True:
        raise ValueError("Golden-input comparison must pass before HIL acceptance.")
    if payload["tensorrt_build_seconds"] <= 0 or payload["tensorrt_engine_size_bytes"] <= 0:
        raise ValueError("TensorRT build duration and engine size must be positive.")
    for field_name in ("cpu_utilization_avg_pct", "gpu_utilization_avg_pct"):
        if not 0 <= payload[field_name] <= 100:
            raise ValueError(f"{field_name} must be between 0 and 100.")
    return payload


if __name__ == "__main__":
    main()
