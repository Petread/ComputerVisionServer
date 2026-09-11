from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in [
    ROOT / "backend" / "packages" / "cv-core" / "src",
    ROOT / "backend" / "packages" / "cv-contracts" / "src",
    ROOT / "backend" / "packages" / "cv-db" / "src",
    ROOT / "backend" / "packages" / "cv-events" / "src",
    ROOT / "backend" / "packages" / "cv-observability" / "src",
    ROOT / "backend" / "packages" / "cv-status" / "src",
    ROOT / "backend" / "packages" / "cv-utils" / "src",
]:
    sys.path.insert(0, str(path))

from cv_core import load_platform_settings


def main() -> None:
    service_name = sys.argv[1] if len(sys.argv) > 1 else "admin-api"
    settings = load_platform_settings(service_name, root_dir=ROOT)
    print(json.dumps(settings.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
