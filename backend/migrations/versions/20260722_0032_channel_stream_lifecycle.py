"""Add an inference-independent channel stream lifecycle."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260722_0032"
down_revision = "20260722_0031"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _column_names(table: str) -> set[str]:
    return {str(item["name"]) for item in _inspector().get_columns(table)}


def upgrade() -> None:
    columns = _column_names("runtime_channels")
    additions = (
        ("stream_desired_state", sa.String(length=32), "'stopped'", False),
        ("stream_actual_state", sa.String(length=32), "'stopped'", False),
        ("stream_worker_instance_id", sa.String(length=128), None, True),
        ("stream_heartbeat_at", sa.DateTime(timezone=True), None, True),
        ("stream_last_error", sa.Text(), None, True),
        ("stream_profile", sa.JSON(), "'{}'", False),
        ("stream_metrics", sa.JSON(), "'{}'", False),
    )
    for name, column_type, server_default, nullable in additions:
        if name in columns:
            continue
        op.add_column(
            "runtime_channels",
            sa.Column(
                name,
                column_type,
                nullable=nullable,
                server_default=sa.text(server_default) if server_default else None,
            ),
        )

    op.execute(
        """
        UPDATE runtime_channels
        SET stream_desired_state = CASE
            WHEN desired_state IN ('start_requested', 'running') THEN 'running'
            ELSE 'stopped'
        END
        """
    )
    index_names = {item["name"] for item in _inspector().get_indexes("runtime_channels")}
    if "ix_runtime_channels_stream_worker_instance_id" not in index_names:
        op.create_index(
            "ix_runtime_channels_stream_worker_instance_id",
            "runtime_channels",
            ["stream_worker_instance_id"],
        )


def downgrade() -> None:
    columns = _column_names("runtime_channels")
    index_names = {item["name"] for item in _inspector().get_indexes("runtime_channels")}
    if "ix_runtime_channels_stream_worker_instance_id" in index_names:
        op.drop_index("ix_runtime_channels_stream_worker_instance_id", table_name="runtime_channels")
    for name in (
        "stream_metrics",
        "stream_profile",
        "stream_last_error",
        "stream_heartbeat_at",
        "stream_worker_instance_id",
        "stream_actual_state",
        "stream_desired_state",
    ):
        if name in columns:
            op.drop_column("runtime_channels", name)
