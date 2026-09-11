from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cv_runtime.payloads import StemRotationOptimizationPayload
from cv_runtime.stem_rotation_validation import (
    build_diagnostic_package,
    export_diagnostic_package,
    load_validation_document,
    replay_validation_input,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay a production stem optimization, slice model, or calibrated "
            "projection-observation document through the production runtime node."
        )
    )
    parser.add_argument("input", type=Path, help="production JSON document")
    parser.add_argument("--output", type=Path, help="optional validation report JSON")
    parser.add_argument("--config", type=Path, help="optional optimizer config overrides")
    parser.add_argument(
        "--priority-mode",
        choices=("VORSCHNITT_KRITISCH", "NACHSCHNITT_KRITISCH", "NONE"),
        help="optional replay priority; uses the same frontend priority contract",
    )
    parser.add_argument("--negative-limit-deg", type=float)
    parser.add_argument("--positive-limit-deg", type=float)
    parser.add_argument(
        "--export-dir",
        type=Path,
        help="optional JSON/CSV diagnostic package directory",
    )
    arguments = parser.parse_args()

    document = _load_json(arguments.input)
    source = load_validation_document(document)
    overrides = _load_json(arguments.config) if arguments.config is not None else {}
    if arguments.negative_limit_deg is not None:
        overrides["rotation_negative_limit_deg"] = arguments.negative_limit_deg
    if arguments.positive_limit_deg is not None:
        overrides["rotation_positive_limit_deg"] = arguments.positive_limit_deg
    report = replay_validation_input(
        source,
        priority_mode=arguments.priority_mode,
        config_overrides=overrides,
    )

    if arguments.export_dir is not None:
        result = StemRotationOptimizationPayload.model_validate(report["replayed_result"])
        package = build_diagnostic_package(
            result,
            source_kind=source.source_kind,
            original_result=source.original_result,
            warnings=report["warnings"],
        )
        paths = export_diagnostic_package(package, arguments.export_dir)
        report["diagnostic_exports"] = [str(path) for path in paths]

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(rendered, end="")
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    return 0


def _load_json(path: Path) -> dict[str, Any]:
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return document


if __name__ == "__main__":
    raise SystemExit(main())
