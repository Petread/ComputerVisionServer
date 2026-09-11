"""Add runtime lifecycle command and instance state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260501_0014"
down_revision = "20260501_0013"
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


def _json_object_default() -> sa.TextClause:
    return sa.text("'{}'::json")


def upgrade() -> None:
    if _has_table("runtime_channels") and not _has_column(
        "runtime_channels",
        "active_runtime_execution_id",
    ):
        op.add_column(
            "runtime_channels",
            sa.Column("active_runtime_execution_id", sa.String(length=64)),
        )
        op.create_index(
            "ix_runtime_channels_active_runtime_execution_id",
            "runtime_channels",
            ["active_runtime_execution_id"],
        )

    if _has_table("runtime_executions"):
        for column in (
            sa.Column("assigned_runtime_worker", sa.String(length=128)),
            sa.Column("control_command_id", sa.String(length=64)),
            sa.Column("actual_state", sa.String(length=32), nullable=False, server_default="starting"),
            sa.Column("loading_state", sa.String(length=64), nullable=False, server_default="not_started"),
            sa.Column("health_summary", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column("metrics_summary", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column("runtime_plan", sa.JSON(), nullable=False, server_default=_json_object_default()),
        ):
            if not _has_column("runtime_executions", column.name):
                op.add_column("runtime_executions", column)
        for index_name, column_name in (
            ("ix_runtime_executions_assigned_runtime_worker", "assigned_runtime_worker"),
            ("ix_runtime_executions_control_command_id", "control_command_id"),
        ):
            if _has_column("runtime_executions", column_name):
                op.create_index(index_name, "runtime_executions", [column_name])

    if not _has_table("runtime_control_commands"):
        op.create_table(
            "runtime_control_commands",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("command", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("requested_by", sa.String(length=128)),
            sa.Column("requested_state", sa.String(length=32)),
            sa.Column("reason", sa.Text()),
            sa.Column("idempotency_key", sa.String(length=160)),
            sa.Column("payload", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column("claimed_by", sa.String(length=128)),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("consumed_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("runtime_execution_id", sa.String(length=64)),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint(
                "channel_id",
                "idempotency_key",
                name="uq_runtime_control_channel_idempotency",
            ),
        )
        op.create_index(
            "ix_runtime_control_commands_channel_id",
            "runtime_control_commands",
            ["channel_id"],
        )
        op.create_index(
            "ix_runtime_control_commands_status",
            "runtime_control_commands",
            ["status"],
        )
        op.create_index(
            "ix_runtime_control_commands_claimed_by",
            "runtime_control_commands",
            ["claimed_by"],
        )
        op.create_index(
            "ix_runtime_control_commands_runtime_execution_id",
            "runtime_control_commands",
            ["runtime_execution_id"],
        )


def downgrade() -> None:
    if _has_table("runtime_control_commands"):
        op.drop_table("runtime_control_commands")
    if _has_table("runtime_executions"):
        for column_name in (
            "runtime_plan",
            "metrics_summary",
            "health_summary",
            "loading_state",
            "actual_state",
            "control_command_id",
            "assigned_runtime_worker",
        ):
            if _has_column("runtime_executions", column_name):
                op.drop_column("runtime_executions", column_name)
    if _has_table("runtime_channels") and _has_column(
        "runtime_channels",
        "active_runtime_execution_id",
    ):
        op.drop_column("runtime_channels", "active_runtime_execution_id")
