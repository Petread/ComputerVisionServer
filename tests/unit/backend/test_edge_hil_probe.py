from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.benchmark.edge_hil_probe import _read_metrics


def _valid_metrics() -> dict[str, float | int | bool]:
    return {
        "capture_width": 1920,
        "capture_height": 1080,
        "capture_fps": 30,
        "frame_sampling_interval": 2,
        "golden_input_passed": True,
        "latency_p50_ms": 20.0,
        "latency_p95_ms": 60.0,
        "latency_p99_ms": 90.0,
        "source_fps": 30.0,
        "processed_fps": 15.0,
        "drop_fps": 0.0,
        "cpu_utilization_avg_pct": 35.0,
        "ram_used_peak_bytes": 2_000_000_000,
        "gpu_utilization_avg_pct": 55.0,
        "gpu_memory_used_peak_bytes": 1_000_000_000,
        "disk_used_delta_bytes": 100_000,
        "tensorrt_build_seconds": 42.0,
        "tensorrt_engine_size_bytes": 25_000_000,
        "warmup_seconds": 300,
        "measurement_seconds": 1800,
    }


def test_hil_metrics_accept_complete_reference_run(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    metrics = _valid_metrics()
    path.write_text(json.dumps(metrics), encoding="utf-8")

    assert _read_metrics(path) == metrics


def test_hil_metrics_reject_failed_golden_input(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    metrics = _valid_metrics()
    metrics["golden_input_passed"] = False
    path.write_text(json.dumps(metrics), encoding="utf-8")

    with pytest.raises(ValueError, match="Golden-input"):
        _read_metrics(path)
