from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = (
    REPO_ROOT
    / "backend"
    / "migrations"
    / "versions"
    / "20260720_0026_opt_in_result_history.py"
)


def test_result_history_json_default_has_no_text_clause_bind_parameters(
    monkeypatch,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "opt_in_result_history_migration",
        MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )

    default = migration._empty_json_default()
    compiled = default.compile()

    assert str(compiled) == "'{}'::json"
    assert compiled.params == {}
