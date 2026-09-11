"""Default pipeline trigger endpoint auth to off."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260504_0022"
down_revision = "20260502_0021"
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
    if not _has_column("trigger_endpoints", "auth_required"):
        return
    if _has_column("trigger_endpoints", "derived_from_pipeline"):
        op.execute(
            "UPDATE trigger_endpoints SET auth_required = false "
            "WHERE derived_from_pipeline = true"
        )
    with op.batch_alter_table("trigger_endpoints") as batch_op:
        batch_op.alter_column(
            "auth_required",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )


def downgrade() -> None:
    if not _has_column("trigger_endpoints", "auth_required"):
        return
    with op.batch_alter_table("trigger_endpoints") as batch_op:
        batch_op.alter_column(
            "auth_required",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        )
