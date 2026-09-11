from __future__ import annotations

from importlib import util as importlib_util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_project_multi_model_migration_adds_scope_and_backfills_segmentation_mode(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'project-multi-model.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE model_families (
                    id VARCHAR(64) PRIMARY KEY,
                    task_type VARCHAR(64) NOT NULL,
                    architecture_family VARCHAR(64) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO model_families (id, task_type, architecture_family)
                VALUES
                    ('mfy_instance', 'segmentation', 'yolo'),
                    ('mfy_semantic', 'segmentation', 'deeplab'),
                    ('mfy_detection', 'detection', 'yolo')
                """
            )
        )

        context = MigrationContext.configure(connection)
        operations = Operations(context)
        path = Path(
            "backend/migrations/versions/20260723_0033_project_multi_model_profiles.py"
        )
        spec = importlib_util.spec_from_file_location("project_multi_model_migration", path)
        assert spec is not None and spec.loader is not None
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = operations

        module.upgrade()
        module.upgrade()

        columns = {item["name"] for item in inspect(connection).get_columns("model_families")}
        assert {"class_names", "segmentation_mode"} <= columns
        rows = {
            row.id: (row.class_names, row.segmentation_mode)
            for row in connection.execute(
                text(
                    """
                    SELECT id, class_names, segmentation_mode
                    FROM model_families
                    ORDER BY id
                    """
                )
            )
        }

    assert rows["mfy_instance"] == ("[]", "instance")
    assert rows["mfy_semantic"] == ("[]", "semantic")
    assert rows["mfy_detection"] == ("[]", None)
