"""Make runtime result history explicit and opt-in."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260720_0026"
down_revision = "20260511_0025"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return column_name in {column["name"] for column in columns}


def _empty_json_default() -> sa.TextClause:
    if op.get_bind().dialect.name == "postgresql":
        return sa.text("'{}'::json")
    return sa.text("'{}'")


def upgrade() -> None:
    if _has_table("runtime_channels") and not _has_column(
        "runtime_channels", "result_history_policy"
    ):
        op.add_column(
            "runtime_channels",
            sa.Column(
                "result_history_policy",
                sa.JSON(),
                nullable=False,
                # Keep the DDL default free of JSON key/value colons. SQLAlchemy
                # TextClause treats ``:name`` inside text as a bind parameter,
                # which corrupted the previous object literal into JSON NULLs.
                # The validated contract expands {} to the disabled defaults.
                server_default=_empty_json_default(),
            ),
        )
    if _has_table("result_records") and not _has_column(
        "result_records", "history_retained"
    ):
        # A constant default is a metadata-only change on supported PostgreSQL
        # versions, which is important for already large installations.
        op.add_column(
            "result_records",
            sa.Column(
                "history_retained",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    if _has_column("result_records", "history_retained"):
        op.drop_column("result_records", "history_retained")
    if _has_column("runtime_channels", "result_history_policy"):
        op.drop_column("runtime_channels", "result_history_policy")
