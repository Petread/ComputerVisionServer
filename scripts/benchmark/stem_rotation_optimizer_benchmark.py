from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from cv_runtime.stem_rotation_validation import (
    StemRotationValidationInput,
    SyntheticStemCase,
    SyntheticStemConfig,
    load_validation_document,
    run_runtime_optimization,
    synthetic_validation_input,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure the complete production stem optimizer/runtime/HMI path."
    )
    parser.add_argument("--input", type=Path, help="optional production JSON (preferred)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--length-mm", type=float, action="append")
    parser.add_argument("--slice-spacing-mm", type=float, action="append")
    arguments = parser.parse_args()
    if arguments.repeats < 1:
        parser.error("--repeats must be positive")

    if arguments.input is not None:
        cases = [("production-input", load_validation_document(_load_json(arguments.input)))]
    else:
        lengths = arguments.length_mm or [3_000.0, 6_000.0]
        spacings = arguments.slice_spacing_mm or [25.0, 50.0, 100.0]
        cases = [
            (
                f"synthetic-{length_mm:g}mm-{spacing_mm:g}mm",
                synthetic_validation_input(
                    SyntheticStemConfig(
                        case=SyntheticStemCase.CURVED_ELLIPSE,
                        seed=20260821,
                        length_mm=length_mm,
                        slice_spacing_mm=spacing_mm,
                    ),
                    optimizer_config={"slice_spacing_mm": spacing_mm},
                ),
            )
            for length_mm in lengths
            for spacing_mm in spacings
        ]

    rows: list[dict[str, Any]] = []
    for label, source in cases:
        elapsed: list[float] = []
        last_result = None
        for repeat in range(arguments.repeats):
            started = time.perf_counter()
            last_result = run_runtime_optimization(
                source.model_payload,
                source.priority_control,
                optimizer_config=source.optimizer_config,
                correlation_id=f"benchmark-{label}-{repeat}",
            )
            elapsed.append((time.perf_counter() - started) * 1_000.0)
        assert last_result is not None
        ordered = sorted(elapsed)
        rows.append(
            {
                "case": label,
                "source_kind": source.source_kind,
                "stem_length_mm": _stem_length(source),
                "processed_slice_count": len(source.model_payload.processed_slices),
                "valid_slice_count": last_result.valid_slice_count,
                "coarse_angle_step_deg": source.optimizer_config["coarse_angle_step_deg"],
                "fine_angle_step_deg": source.optimizer_config["fine_angle_step_deg"],
                "rotation_limits_deg": [
                    source.optimizer_config["rotation_negative_limit_deg"],
                    source.optimizer_config["rotation_positive_limit_deg"],
                ],
                "repeats": arguments.repeats,
                "elapsed_ms_min": ordered[0],
                "elapsed_ms_median": statistics.median(ordered),
                "elapsed_ms_p95": _percentile(ordered, 0.95),
                "elapsed_ms_max": ordered[-1],
                "result_valid": last_result.result_valid,
                "score_curve_point_count": len(last_result.score_curve),
            }
        )
    report = {
        "schema_version": "stem.rotation-performance-report.v1",
        "scope": "production_priority_runtime_optimizer_and_hmi_path",
        "threshold_enforced": False,
        "rows": rows,
    }
    _render_json(report, arguments.output)
    return 0


def _stem_length(source: StemRotationValidationInput) -> float | None:
    slices = source.model_payload.processed_slices
    if len(slices) < 2:
        return None
    return float(max(item.x_mm for item in slices) - min(item.x_mm for item in slices))


def _percentile(values: list[float], quantile: float) -> float:
    position = quantile * (len(values) - 1)
    left = int(position)
    right = min(left + 1, len(values) - 1)
    fraction = position - left
    return values[left] + fraction * (values[right] - values[left])


def _load_json(path: Path) -> dict[str, Any]:
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return document


def _render_json(document: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if output is None:
        print(rendered, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
