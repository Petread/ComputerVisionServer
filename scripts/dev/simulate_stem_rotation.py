from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cv_runtime.payloads import StemRotationOptimizationPayload
from cv_runtime.stem_rotation_validation import (
    SyntheticStemCase,
    SyntheticStemConfig,
    build_diagnostic_package,
    export_diagnostic_package,
    load_validation_document,
    replay_validation_input,
    synthetic_validation_input,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run real production stem data (preferred) or an explicit synthetic case "
            "through the production priority/runtime/optimizer/HMI path."
        )
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--input", type=Path, help="production JSON to replay")
    source_group.add_argument(
        "--synthetic",
        choices=tuple(item.value for item in SyntheticStemCase),
        help="explicit synthetic geometry case",
    )
    parser.add_argument("--output", type=Path, help="optional simulator report JSON")
    parser.add_argument("--export-dir", type=Path, help="optional JSON/CSV package directory")
    parser.add_argument("--config", type=Path, help="optional optimizer config overrides")
    parser.add_argument("--negative-limit-deg", type=float)
    parser.add_argument("--positive-limit-deg", type=float)
    parser.add_argument(
        "--priority-mode",
        choices=("VORSCHNITT_KRITISCH", "NACHSCHNITT_KRITISCH", "NONE"),
        default="VORSCHNITT_KRITISCH",
    )
    parser.add_argument("--seed", type=int, default=20260821)
    parser.add_argument("--length-mm", type=float, default=4_000.0)
    parser.add_argument("--slice-spacing-mm", type=float, default=50.0)
    parser.add_argument("--diameter-start-mm", type=float, default=240.0)
    parser.add_argument("--diameter-end-mm", type=float)
    parser.add_argument("--axis-ratio", type=float, default=0.75)
    parser.add_argument("--ellipse-angle-deg", type=float, default=25.0)
    parser.add_argument("--ellipse-angle-variation-deg", type=float, default=0.0)
    parser.add_argument("--curvature-amplitude-mm", type=float, default=35.0)
    parser.add_argument("--curvature-direction-deg", type=float, default=90.0)
    parser.add_argument("--curvature-direction-variation-deg", type=float, default=0.0)
    parser.add_argument("--bottleneck-x-mm", type=float)
    parser.add_argument("--bottleneck-reduction-mm", type=float, default=45.0)
    parser.add_argument("--center-noise-std-mm", type=float, default=0.0)
    parser.add_argument("--axis-noise-std-mm", type=float, default=0.0)
    parser.add_argument("--angle-noise-std-deg", type=float, default=0.0)
    parser.add_argument("--outlier-count", type=int, default=0)
    parser.add_argument("--short-gap-slices", type=int, default=0)
    parser.add_argument("--long-gap-slices", type=int, default=0)
    arguments = parser.parse_args()

    overrides = _load_json(arguments.config) if arguments.config is not None else {}
    if arguments.negative_limit_deg is not None:
        overrides["rotation_negative_limit_deg"] = arguments.negative_limit_deg
    if arguments.positive_limit_deg is not None:
        overrides["rotation_positive_limit_deg"] = arguments.positive_limit_deg
    synthetic_config: SyntheticStemConfig | None = None
    if arguments.input is not None:
        source = load_validation_document(_load_json(arguments.input))
    else:
        overrides.setdefault("slice_spacing_mm", arguments.slice_spacing_mm)
        synthetic_config = SyntheticStemConfig(
            case=SyntheticStemCase(arguments.synthetic),
            seed=arguments.seed,
            length_mm=arguments.length_mm,
            slice_spacing_mm=arguments.slice_spacing_mm,
            diameter_start_mm=arguments.diameter_start_mm,
            diameter_end_mm=arguments.diameter_end_mm,
            axis_ratio=arguments.axis_ratio,
            ellipse_angle_deg=arguments.ellipse_angle_deg,
            ellipse_angle_variation_deg=arguments.ellipse_angle_variation_deg,
            curvature_amplitude_mm=arguments.curvature_amplitude_mm,
            curvature_direction_deg=arguments.curvature_direction_deg,
            curvature_direction_variation_deg=(arguments.curvature_direction_variation_deg),
            bottleneck_x_mm=arguments.bottleneck_x_mm,
            bottleneck_diameter_reduction_mm=arguments.bottleneck_reduction_mm,
            center_noise_std_mm=arguments.center_noise_std_mm,
            axis_noise_std_mm=arguments.axis_noise_std_mm,
            angle_noise_std_deg=arguments.angle_noise_std_deg,
            outlier_count=arguments.outlier_count,
            short_gap_slices=arguments.short_gap_slices,
            long_gap_slices=arguments.long_gap_slices,
        )
        source = synthetic_validation_input(
            synthetic_config,
            priority_mode=arguments.priority_mode,
            optimizer_config=overrides,
        )

    report = replay_validation_input(
        source,
        priority_mode=arguments.priority_mode,
        config_overrides=overrides,
    )
    result = StemRotationOptimizationPayload.model_validate(report["replayed_result"])
    package = build_diagnostic_package(
        result,
        source_kind=source.source_kind,
        original_result=source.original_result,
        warnings=report["warnings"],
    )
    if synthetic_config is not None:
        package["synthetic_generator"] = _jsonable_synthetic_config(synthetic_config)
    simulator_report: dict[str, Any] = {
        "validation": report,
        "diagnostic_package": package,
    }
    if arguments.export_dir is not None:
        simulator_report["diagnostic_exports"] = [
            str(path) for path in export_diagnostic_package(package, arguments.export_dir)
        ]
    _render_json(simulator_report, arguments.output)
    return 0


def _jsonable_synthetic_config(config: SyntheticStemConfig) -> dict[str, Any]:
    document = asdict(config)
    document["case"] = config.case.value
    return document


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
