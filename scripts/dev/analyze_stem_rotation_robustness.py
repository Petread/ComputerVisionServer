from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cv_runtime.stem_rotation_validation import (
    SyntheticStemCase,
    SyntheticStemConfig,
    load_validation_document,
    monte_carlo_robustness,
    synthetic_validation_input,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure stem rotation decision stability under seeded slice noise."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--input", type=Path, help="production JSON (preferred)")
    source_group.add_argument(
        "--synthetic",
        choices=tuple(item.value for item in SyntheticStemCase),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path, help="optional optimizer config overrides")
    parser.add_argument(
        "--priority-mode",
        choices=("VORSCHNITT_KRITISCH", "NACHSCHNITT_KRITISCH"),
    )
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260821)
    parser.add_argument("--center-noise-std-mm", type=float, default=1.0)
    parser.add_argument("--axis-noise-std-mm", type=float, default=1.5)
    parser.add_argument("--angle-noise-std-deg", type=float, default=1.0)
    parser.add_argument("--length-mm", type=float, default=4_000.0)
    parser.add_argument("--slice-spacing-mm", type=float, default=50.0)
    arguments = parser.parse_args()

    overrides = _load_json(arguments.config) if arguments.config is not None else {}
    if arguments.input is not None:
        source = load_validation_document(_load_json(arguments.input))
    else:
        overrides.setdefault("slice_spacing_mm", arguments.slice_spacing_mm)
        source = synthetic_validation_input(
            SyntheticStemConfig(
                case=SyntheticStemCase(arguments.synthetic),
                seed=arguments.seed,
                length_mm=arguments.length_mm,
                slice_spacing_mm=arguments.slice_spacing_mm,
            ),
            priority_mode=arguments.priority_mode or "VORSCHNITT_KRITISCH",
            optimizer_config=overrides,
        )
    report = monte_carlo_robustness(
        source,
        iterations=arguments.iterations,
        seed=arguments.seed,
        center_noise_std_mm=arguments.center_noise_std_mm,
        axis_noise_std_mm=arguments.axis_noise_std_mm,
        angle_noise_std_deg=arguments.angle_noise_std_deg,
        priority_mode=arguments.priority_mode,
        config_overrides=overrides,
    )
    _render_json(report, arguments.output)
    return 0


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
