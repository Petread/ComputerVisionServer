"""Offline geometry kernel benchmark; does not start services or operate a camera."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--native-threads", type=int, choices=(1, 2), required=True)
    parser.add_argument("--pipeline-frames", type=int, choices=(1, 2), required=True)
    parser.add_argument("--frames", type=int, default=100)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.frames < 2:
        parser.error("--frames must be at least 2")
    os.environ["YBB_REALTIME_EXECUTOR_NATIVE_THREADS"] = str(arguments.native_threads)
    # Package initialization applies native limits before numerical imports.
    importlib.import_module("realtime_executor")
    import cv2
    import numpy as np
    from cv_runtime.nodes.stem_orientation import _geometry_config
    from cv_runtime.stem_geometry import analyze_stem_geometry
    from cv_runtime.stem_horizontal_curvature import (
        StemHorizontalCurvatureConfig,
        measure_stem_horizontal_curvature,
    )
    from cv_runtime.stem_process_coordinates import (
        process_profile_for_aligned_measurement_grid,
        process_profile_for_rectified_view,
        validate_stem_curvature_process_calibration,
    )

    document_bytes = arguments.input.read_bytes()
    document = json.loads(document_bytes)
    profile = validate_stem_curvature_process_calibration(document["process_calibration"])
    mask_path = arguments.input.parent / document["aligned_mask_path"]
    mask = np.load(mask_path, allow_pickle=False)
    if mask.ndim != 2 or min(mask.shape) < 2 or mask.dtype != np.uint8:
        parser.error("aligned mask must be a two-dimensional uint8 array")
    profile = process_profile_for_aligned_measurement_grid(
        process_profile_for_rectified_view(
            profile,
            image_width=mask.shape[1],
            image_height=mask.shape[0],
            transform_parameters=document.get("view_transform_parameters"),
        )
    )
    mask.flags.writeable = False
    polygon = tuple(tuple(point) for point in document["polygon_normalized"])
    geometry_config = _geometry_config(document.get("geometry_config", {}))
    fast_config = _geometry_config({"analysis_mode": "phase_tracking_fast"})
    curvature_config = StemHorizontalCurvatureConfig(**document.get("curvature_config", {}))

    def measure(_index: int) -> tuple[float, str, dict[str, float]]:
        started = time.perf_counter()
        outputs = []
        stages: dict[str, float] = {}
        for label, config in (("geometry", geometry_config), ("fast_geometry", fast_config)):
            timings: dict[str, float] = {}
            outputs.append(asdict(analyze_stem_geometry(
                polygon, image_width=profile.image_width, image_height=profile.image_height,
                config=config, timings_ms=timings,
            )))
            stages.update({f"{label}_{key}": value for key, value in timings.items()})
        curvature_started = time.perf_counter()
        outputs.append(
            asdict(
                measure_stem_horizontal_curvature(
                    mask, profile, config=curvature_config, internal_aligned_process_grid=True
                )
            )
        )
        stages["curvature_ms"] = (time.perf_counter() - curvature_started) * 1000
        elapsed_ms = (time.perf_counter() - started) * 1000
        digest = hashlib.sha256(json.dumps(outputs, sort_keys=True).encode()).hexdigest()
        return elapsed_ms, digest, stages

    reference = measure(0)[1]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=arguments.pipeline_frames) as executor:
        rows = list(executor.map(measure, range(arguments.frames)))
    elapsed = time.perf_counter() - started
    if any(digest != reference for _latency, digest, _stages in rows):
        raise RuntimeError("Concurrent geometry results differ from the serial warm-up result")
    latencies = sorted(latency for latency, _digest, _stages in rows)
    report = {
        "benchmark": "stem-geometry-kernels.v1",
        "input_sha256": hashlib.sha256(document_bytes + mask_path.read_bytes()).hexdigest(),
        "result_sha256": reference,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "opencv": cv2.__version__,
        "platform": platform.platform(),
        "native_threads": arguments.native_threads,
        "opencv_threads": cv2.getNumThreads(),
        "pipeline_frames": arguments.pipeline_frames,
        "frames": arguments.frames,
        "elapsed_seconds": elapsed,
        "kernel_frames_per_second": arguments.frames / elapsed,
        "frame_compute_p50_ms": statistics.median(latencies),
        "frame_compute_p95_ms": latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)],
        "stage_p50_ms": {
            key: statistics.median(stages[key] for _, _, stages in rows if key in stages)
            for key in sorted({key for _, _, stages in rows for key in stages})
        },
        "scope": "geometry + fast geometry + curvature; excludes acquisition, inference and phase",
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if arguments.output is not None:
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
