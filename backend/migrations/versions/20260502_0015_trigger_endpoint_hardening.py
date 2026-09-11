"""Harden trigger endpoint ownership and response policy."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260502_0015"
down_revision = "20260501_0014"
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


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def _json_object_default() -> sa.TextClause:
    return sa.text("'{}'::json")


def upgrade() -> None:
    if not _has_table("trigger_endpoints"):
        return

    columns = (
        sa.Column("pipeline_version_id", sa.String(length=64)),
        sa.Column(
            "display_name",
            sa.String(length=160),
            nullable=False,
            server_default="Trigger endpoint",
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("auth_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default=sa.text("60")),
        sa.Column("burst_limit", sa.Integer(), nullable=False, server_default=sa.text("4")),
        sa.Column("response_policy", sa.JSON(), nullable=False, server_default=_json_object_default()),
        sa.Column("last_status", sa.String(length=32)),
        sa.Column("last_latency_ms", sa.Float()),
        sa.Column("last_error_code", sa.String(length=120)),
        sa.Column("last_error_message", sa.Text()),
        sa.Column("invocation_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by", sa.String(length=64)),
        sa.Column("updated_by", sa.String(length=64)),
    )
    for column in columns:
        if not _has_column("trigger_endpoints", column.name):
            op.add_column("trigger_endpoints", column)

    if _has_column("trigger_endpoints", "pipeline_version_id") and not _has_index(
        "trigger_endpoints",
        "ix_trigger_endpoints_pipeline_version_id",
    ):
        op.create_index(
            "ix_trigger_endpoints_pipeline_version_id",
            "trigger_endpoints",
            ["pipeline_version_id"],
        )


def downgrade() -> None:
    if not _has_table("trigger_endpoints"):
        return
    if _has_index("trigger_endpoints", "ix_trigger_endpoints_pipeline_version_id"):
        op.drop_index("ix_trigger_endpoints_pipeline_version_id", table_name="trigger_endpoints")
    for column_name in (
        "updated_by",
        "created_by",
        "failure_count",
        "invocation_count",
        "last_error_message",
        "last_error_code",
        "last_latency_ms",
        "last_status",
        "response_policy",
        "burst_limit",
        "rate_limit_per_minute",
        "auth_required",
        "enabled",
        "display_name",
        "pipeline_version_id",
    ):
        if _has_column("trigger_endpoints", column_name):
            op.drop_column("trigger_endpoints", column_name)
