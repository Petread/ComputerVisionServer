"""Add runtime manual live capture tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260501_0013"
down_revision = "20260501_0012"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _json_object_default() -> sa.TextClause:
    return sa.text("'{}'::json")


def _json_array_default() -> sa.TextClause:
    return sa.text("'[]'::json")


def upgrade() -> None:
    if not _has_table("runtime_manual_capture_sessions"):
        op.create_table(
            "runtime_manual_capture_sessions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("target_label_project_id", sa.String(length=64), nullable=False),
            sa.Column("label_studio_project_id", sa.Integer(), nullable=False),
            sa.Column("interval_seconds", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("operator_user_id", sa.String(length=64), nullable=False),
            sa.Column("live_session_id", sa.String(length=128)),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("stopped_at", sa.DateTime(timezone=True)),
            sa.Column("capture_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_capture_at", sa.DateTime(timezone=True)),
            sa.Column("last_success_at", sa.DateTime(timezone=True)),
            sa.Column("last_failure_at", sa.DateTime(timezone=True)),
            sa.Column("last_capture_request_id", sa.String(length=64)),
            sa.Column("last_error", sa.Text()),
            sa.Column("metadata", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_runtime_manual_capture_sessions_channel_id",
            "runtime_manual_capture_sessions",
            ["channel_id"],
        )
        op.create_index(
            "ix_runtime_manual_capture_sessions_target_label_project_id",
            "runtime_manual_capture_sessions",
            ["target_label_project_id"],
        )
        op.create_index(
            "ix_runtime_manual_capture_sessions_state",
            "runtime_manual_capture_sessions",
            ["state"],
        )
        op.create_index(
            "ix_runtime_manual_capture_sessions_live_session_id",
            "runtime_manual_capture_sessions",
            ["live_session_id"],
        )

    if not _has_table("runtime_manual_capture_requests"):
        op.create_table(
            "runtime_manual_capture_requests",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("manual_capture_session_id", sa.String(length=64)),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("channel_display_name", sa.String(length=160), nullable=False),
            sa.Column("target_label_project_id", sa.String(length=64), nullable=False),
            sa.Column("target_project_title", sa.String(length=200), nullable=False),
            sa.Column("label_studio_project_id", sa.Integer(), nullable=False),
            sa.Column("capture_mode", sa.String(length=32), nullable=False),
            sa.Column("interval_seconds", sa.Integer()),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("operator_user_id", sa.String(length=64), nullable=False),
            sa.Column("live_session_id", sa.String(length=128)),
            sa.Column("runtime_execution_id", sa.String(length=64)),
            sa.Column("source_frame_metadata", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column("asset_uri", sa.String(length=512)),
            sa.Column("digest", sa.String(length=128)),
            sa.Column("label_studio_task_ids", sa.JSON(), nullable=False, server_default=_json_array_default()),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("error_summary", sa.Text()),
            sa.Column("metadata", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_runtime_manual_capture_requests_manual_capture_session_id",
            "runtime_manual_capture_requests",
            ["manual_capture_session_id"],
        )
        op.create_index(
            "ix_runtime_manual_capture_requests_channel_id",
            "runtime_manual_capture_requests",
            ["channel_id"],
        )
        op.create_index(
            "ix_runtime_manual_capture_requests_target_label_project_id",
            "runtime_manual_capture_requests",
            ["target_label_project_id"],
        )
        op.create_index(
            "ix_runtime_manual_capture_requests_live_session_id",
            "runtime_manual_capture_requests",
            ["live_session_id"],
        )
        op.create_index(
            "ix_runtime_manual_capture_requests_runtime_execution_id",
            "runtime_manual_capture_requests",
            ["runtime_execution_id"],
        )
        op.create_index(
            "ix_runtime_manual_capture_requests_digest",
            "runtime_manual_capture_requests",
            ["digest"],
        )


def downgrade() -> None:
    for table_name in (
        "runtime_manual_capture_requests",
        "runtime_manual_capture_sessions",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
