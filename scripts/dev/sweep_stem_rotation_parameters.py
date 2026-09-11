from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from cv_runtime.stem_rotation_validation import (
    SyntheticStemCase,
    SyntheticStemConfig,
    load_validation_document,
    parameter_sweep,
    synthetic_validation_input,
)

DEFAULT_SWEEP: dict[str, list[int | float]] = {
    "slice_spacing_mm": [25.0, 50.0, 100.0],
    "smoothing_window_slices": [1, 3, 5],
    "coarse_angle_step_deg": [0.5, 1.0, 2.0],
    "fine_angle_step_deg": [0.1, 0.25, 0.5],
    "score_tolerance_mm": [0.25, 1.0, 2.0],
    "min_gain_mm": [1.0, 3.0, 5.0],
    "min_rotation_deg": [0.5, 1.0, 2.0],
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one-at-a-time commissioning parameter sweeps via production APIs."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--input", type=Path, help="production JSON (preferred)")
    source_group.add_argument(
        "--synthetic",
        choices=tuple(item.value for item in SyntheticStemCase),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path, help="optional baseline optimizer config")
    parser.add_argument(
        "--priority-mode",
        choices=("VORSCHNITT_KRITISCH", "NACHSCHNITT_KRITISCH"),
    )
    parser.add_argument(
        "--sweep",
        action="append",
        default=[],
        metavar="KEY=VALUE,VALUE",
        help="repeatable; replaces the built-in default sweep when supplied",
    )
    parser.add_argument("--seed", type=int, default=20260821)
    parser.add_argument("--length-mm", type=float, default=4_000.0)
    parser.add_argument("--slice-spacing-mm", type=float, default=50.0)
    arguments = parser.parse_args()

    baseline = _load_json(arguments.config) if arguments.config is not None else {}
    if arguments.input is not None:
        source = load_validation_document(_load_json(arguments.input))
        source = replace(
            source,
            optimizer_config={**source.optimizer_config, **baseline},
        )
    else:
        baseline.setdefault("slice_spacing_mm", arguments.slice_spacing_mm)
        source = synthetic_validation_input(
            SyntheticStemConfig(
                case=SyntheticStemCase(arguments.synthetic),
                seed=arguments.seed,
                length_mm=arguments.length_mm,
                slice_spacing_mm=arguments.slice_spacing_mm,
            ),
            priority_mode=arguments.priority_mode or "VORSCHNITT_KRITISCH",
            optimizer_config=baseline,
        )
    sweep = _parse_sweeps(arguments.sweep) if arguments.sweep else DEFAULT_SWEEP
    report = parameter_sweep(source, sweep, priority_mode=arguments.priority_mode)
    _render_json(report, arguments.output)
    return 0


def _parse_sweeps(values: list[str]) -> dict[str, list[int | float]]:
    output: dict[str, list[int | float]] = {}
    for value in values:
        key, separator, raw_values = value.partition("=")
        if not separator or not key or not raw_values:
            raise ValueError(f"invalid sweep {value!r}; expected KEY=VALUE,VALUE")
        parsed: list[int | float] = []
        for raw in raw_values.split(","):
            number = float(raw)
            parsed.append(int(number) if key == "smoothing_window_slices" else number)
        output[key] = parsed
    return output


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
