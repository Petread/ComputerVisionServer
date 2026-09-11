# Config Schema Exports

The canonical platform configuration schema is generated from `cv_core.settings.PlatformSettings`.

Generate or refresh it with:

```powershell
uv run python scripts/dev/export_config_schema.py
```

The exported schema file is written to `config/schemas/platform-settings.schema.json`.

