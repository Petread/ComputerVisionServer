"""Add label-project class balancing policy."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260509_0023"
down_revision = "20260504_0022"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if _has_column("label_projects", "class_balance_strategy"):
        return
    op.add_column(
        "label_projects",
        sa.Column(
            "class_balance_strategy",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
    )


def downgrade() -> None:
    if not _has_column("label_projects", "class_balance_strategy"):
        return
    op.drop_column("label_projects", "class_balance_strategy")
