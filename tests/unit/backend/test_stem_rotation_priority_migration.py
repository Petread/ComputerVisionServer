from __future__ import annotations

import json
import re
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateColumn


def _migration_module() -> ModuleType:
    path = Path(
        "backend/migrations/versions/20260820_0036_stem_rotation_priority_control.py"
    )
    spec = importlib_util.spec_from_file_location(
        "stem_rotation_priority_control_migration",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_priority_control_migration_backfills_valid_json(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'stem-rotation-priority.db'}")
    module = _migration_module()

    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE runtime_channels (id VARCHAR(64) PRIMARY KEY)")
        )
        connection.execute(
            sa.text("INSERT INTO runtime_channels (id) VALUES ('chn_existing')")
        )
        module.op = Operations(MigrationContext.configure(connection))

        module.upgrade()

        stored = connection.execute(
            sa.text(
                "SELECT stem_rotation_priority_control FROM runtime_channels "
                "WHERE id = 'chn_existing'"
            )
        ).scalar_one()
        columns = {
            item["name"]: item
            for item in sa.inspect(connection).get_columns("runtime_channels")
        }

    assert json.loads(stored) == {
        "schema_version": "runtime.stem-rotation-priority-control.v1",
        "priority_source": "FRONTEND",
        "priority_mode": "NONE",
        "optimization_enable": True,
        "revision": 0,
        "updated_at": None,
        "updated_by": None,
    }
    assert columns["stem_rotation_priority_control"]["nullable"] is False


def test_priority_control_postgresql_default_compiles_as_valid_json() -> None:
    module = _migration_module()
    captured: list[sa.Column[object]] = []

    def capture_column(_table_name: str, column: sa.Column[object]) -> None:
        captured.append(column)

    module.op.add_column = capture_column
    module.upgrade()

    assert len(captured) == 1
    ddl = str(CreateColumn(captured[0]).compile(dialect=postgresql.dialect()))
    match = re.search(r" DEFAULT '(.+)' NOT NULL$", ddl)
    assert match is not None, ddl
    assert json.loads(match.group(1).replace("''", "'")) == json.loads(
        module._DEFAULT_CONTROL
    )
    assert '"optimization_enable"NULL' not in ddl
