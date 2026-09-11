from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cv_runtime.stem_slice_measurement import (
    replay_projection_observation_document,
    stem_slice_build_result_to_dict,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay calibrated stem projection observations through the slice builder."
    )
    parser.add_argument("input", type=Path, help="stem.projection-observations.v1 JSON")
    parser.add_argument("--output", type=Path, help="optional JSON result path")
    arguments = parser.parse_args()
    document: Any = json.loads(arguments.input.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        parser.error("the input document must be a JSON object")
    result = stem_slice_build_result_to_dict(
        replay_projection_observation_document(document)
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(rendered, end="")
    else:
        arguments.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
