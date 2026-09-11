from __future__ import annotations

import importlib.util
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
    ROOT / "backend" / "packages" / "cv-runtime" / "src",
    ROOT / "backend" / "packages" / "cv-status" / "src",
    ROOT / "backend" / "packages" / "cv-utils" / "src",
]:
    sys.path.insert(0, str(path))

settings_path = ROOT / "backend" / "packages" / "cv-core" / "src" / "cv_core" / "settings.py"
settings_spec = importlib.util.spec_from_file_location("_cv_core_settings_schema", settings_path)
if settings_spec is None or settings_spec.loader is None:
    raise RuntimeError(f"Could not load settings module from {settings_path}")
settings_module = importlib.util.module_from_spec(settings_spec)
sys.modules[settings_spec.name] = settings_module
settings_spec.loader.exec_module(settings_module)
PlatformSettings = settings_module.PlatformSettings


def main() -> None:
    output_path = ROOT / "config" / "schemas" / "platform-settings.schema.json"
    output_path.write_text(
        json.dumps(PlatformSettings.model_json_schema(), indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
