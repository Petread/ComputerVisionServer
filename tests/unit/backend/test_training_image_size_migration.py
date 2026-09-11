from __future__ import annotations

from importlib import util as importlib_util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_training_image_size_migration_repairs_legacy_json_and_job_columns(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'training-image-size.db'}")
    metadata = sa.MetaData()
    model_templates = sa.Table(
        "model_templates",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("training_defaults", sa.JSON(), nullable=False),
    )
    model_families = sa.Table(
        "model_families",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("training_overrides", sa.JSON(), nullable=False),
    )
    training_jobs = sa.Table(
        "training_jobs",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("image_size", sa.Integer()),
        sa.Column("resolved_training_defaults", sa.JSON(), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
    )
    model_versions = sa.Table(
        "model_versions",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("resolved_training_defaults", sa.JSON(), nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            model_templates.insert().values(
                id="mtp_legacy",
                training_defaults={"training": {"image_size": 1_212}},
            )
        )
        connection.execute(
            model_families.insert().values(
                id="mfy_legacy",
                training_overrides={"image_size": 1_212},
            )
        )
        connection.execute(
            training_jobs.insert().values(
                id="trn_legacy",
                image_size=1_212,
                resolved_training_defaults={"image_size": 1_212},
                config_snapshot={
                    "image_size": 1_212,
                    "resolved_training_defaults": {"image_size": 1_212},
                    "parameter_overrides": {"image_size": 1_212},
                },
            )
        )
        connection.execute(
            model_versions.insert().values(
                id="mvr_legacy",
                resolved_training_defaults={"image_size": 1_212},
            )
        )

        context = MigrationContext.configure(connection)
        operations = Operations(context)
        path = Path(
            "backend/migrations/versions/20260724_0034_normalize_training_image_sizes.py"
        )
        spec = importlib_util.spec_from_file_location(
            "normalize_training_image_sizes",
            path,
        )
        assert spec is not None and spec.loader is not None
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = operations

        module.upgrade()
        module.upgrade()

        template = connection.execute(sa.select(model_templates)).mappings().one()
        family = connection.execute(sa.select(model_families)).mappings().one()
        job = connection.execute(sa.select(training_jobs)).mappings().one()
        version = connection.execute(sa.select(model_versions)).mappings().one()

    assert template["training_defaults"]["training"]["image_size"] == 1_216
    assert family["training_overrides"]["image_size"] == 1_216
    assert job["image_size"] == 1_216
    assert job["resolved_training_defaults"]["image_size"] == 1_216
    assert job["config_snapshot"]["image_size"] == 1_216
    assert job["config_snapshot"]["resolved_training_defaults"]["image_size"] == 1_216
    assert job["config_snapshot"]["parameter_overrides"]["image_size"] == 1_216
    assert version["resolved_training_defaults"]["image_size"] == 1_216
