"""Add runtime channel read-model indexes."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260502_0020"
down_revision = "20260502_0019"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    for table_name, index_name, column_names in (
        (
            "runtime_executions",
            "ix_runtime_executions_channel_created",
            ["channel_id", "created_at"],
        ),
        (
            "result_records",
            "ix_result_records_channel_completed",
            ["channel_id", "completed_at"],
        ),
    ):
        if _has_table(table_name) and not _has_index(table_name, index_name):
            op.create_index(index_name, table_name, column_names)


def downgrade() -> None:
    for table_name, index_name in (
        ("result_records", "ix_result_records_channel_completed"),
        ("runtime_executions", "ix_runtime_executions_channel_created"),
    ):
        if _has_table(table_name) and _has_index(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
