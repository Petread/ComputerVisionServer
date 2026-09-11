"""Add per-model class selection and segmentation semantics."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260723_0033"
down_revision = "20260722_0032"
branch_labels = None
depends_on = None


def _column_names(table: str) -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    columns = _column_names("model_families")
    if "class_names" not in columns:
        op.add_column(
            "model_families",
            sa.Column("class_names", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )
    if "segmentation_mode" not in columns:
        op.add_column(
            "model_families",
            sa.Column("segmentation_mode", sa.String(length=32), nullable=True),
        )

    op.execute(
        """
        UPDATE model_families
        SET segmentation_mode = CASE
            WHEN task_type = 'segmentation' AND architecture_family = 'yolo' THEN 'instance'
            WHEN task_type = 'segmentation' THEN 'semantic'
            ELSE NULL
        END
        WHERE segmentation_mode IS NULL
        """
    )


def downgrade() -> None:
    columns = _column_names("model_families")
    if "segmentation_mode" in columns:
        op.drop_column("model_families", "segmentation_mode")
    if "class_names" in columns:
        op.drop_column("model_families", "class_names")
