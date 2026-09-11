"""Add project-model workflow policy and override fields."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260721_0027"
down_revision = "20260720_0026"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _has_column("label_projects", "default_model_family_id"):
        op.add_column(
            "label_projects",
            sa.Column("default_model_family_id", sa.String(length=64), nullable=True),
        )
    if not _has_index("label_projects", "ix_label_projects_default_model_family_id"):
        op.create_index(
            "ix_label_projects_default_model_family_id",
            "label_projects",
            ["default_model_family_id"],
            unique=False,
        )
    if not _has_index("training_jobs", "ix_training_jobs_idempotency_key"):
        op.create_index(
            "ix_training_jobs_idempotency_key",
            "training_jobs",
            ["idempotency_key"],
            unique=True,
        )
    if not _has_column("model_families", "training_overrides"):
        op.add_column(
            "model_families",
            sa.Column("training_overrides", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("model_families", "training_overrides"):
        op.drop_column("model_families", "training_overrides")
    if _has_index("training_jobs", "ix_training_jobs_idempotency_key"):
        op.drop_index("ix_training_jobs_idempotency_key", table_name="training_jobs")
    if _has_index("label_projects", "ix_label_projects_default_model_family_id"):
        op.drop_index("ix_label_projects_default_model_family_id", table_name="label_projects")
    if _has_column("label_projects", "default_model_family_id"):
        op.drop_column("label_projects", "default_model_family_id")
