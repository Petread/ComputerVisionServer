"""Align runtime capture delivery tables with durable capture-service execution."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260502_0017"
down_revision = "20260502_0016"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _json_object_default() -> sa.TextClause:
    return sa.text("'{}'::json")


def _json_array_default() -> sa.TextClause:
    return sa.text("'[]'::json")


def upgrade() -> None:
    if not _has_table("runtime_capture_requests"):
        return

    additions = [
        ("manual_capture_session_id", sa.Column("manual_capture_session_id", sa.String(length=64))),
        ("manual_capture_request_id", sa.Column("manual_capture_request_id", sa.String(length=64))),
        ("label_studio_project_id", sa.Column("label_studio_project_id", sa.Integer())),
        ("target_project_title", sa.Column("target_project_title", sa.String(length=200))),
        (
            "source_type",
            sa.Column("source_type", sa.String(length=32), nullable=False, server_default="pipeline"),
        ),
        (
            "capture_mode",
            sa.Column("capture_mode", sa.String(length=64), nullable=False, server_default="full_frame"),
        ),
        ("asset_payload", sa.Column("asset_payload", sa.JSON(), nullable=False, server_default=_json_object_default())),
        ("dedupe_key", sa.Column("dedupe_key", sa.String(length=256))),
        ("duplicate_of_request_id", sa.Column("duplicate_of_request_id", sa.String(length=64))),
        (
            "source_frame_metadata",
            sa.Column("source_frame_metadata", sa.JSON(), nullable=False, server_default=_json_object_default()),
        ),
        ("capture_intent", sa.Column("capture_intent", sa.JSON(), nullable=False, server_default=_json_object_default())),
        (
            "label_studio_task_ids",
            sa.Column("label_studio_task_ids", sa.JSON(), nullable=False, server_default=_json_array_default()),
        ),
        ("max_attempts", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3"))),
        ("queued_at", sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.func.now())),
        ("claimed_at", sa.Column("claimed_at", sa.DateTime(timezone=True))),
        ("submitted_at", sa.Column("submitted_at", sa.DateTime(timezone=True))),
        ("delivered_at", sa.Column("delivered_at", sa.DateTime(timezone=True))),
        ("next_attempt_at", sa.Column("next_attempt_at", sa.DateTime(timezone=True))),
        ("rate_limited_until", sa.Column("rate_limited_until", sa.DateTime(timezone=True))),
        ("metadata", sa.Column("metadata", sa.JSON(), nullable=False, server_default=_json_object_default())),
    ]
    for column_name, column in additions:
        if not _has_column("runtime_capture_requests", column_name):
            op.add_column("runtime_capture_requests", column)

    for column_name in ("asset_uri", "digest"):
        if _has_column("runtime_capture_requests", column_name):
            op.alter_column("runtime_capture_requests", column_name, nullable=True)

    for index_name, column_name in (
        ("ix_runtime_capture_requests_manual_capture_session_id", "manual_capture_session_id"),
        ("ix_runtime_capture_requests_manual_capture_request_id", "manual_capture_request_id"),
        ("ix_runtime_capture_requests_label_studio_project_id", "label_studio_project_id"),
        ("ix_runtime_capture_requests_source_type", "source_type"),
        ("ix_runtime_capture_requests_capture_mode", "capture_mode"),
        ("ix_runtime_capture_requests_dedupe_key", "dedupe_key"),
        ("ix_runtime_capture_requests_duplicate_of_request_id", "duplicate_of_request_id"),
        ("ix_runtime_capture_requests_status", "status"),
        ("ix_runtime_capture_requests_next_attempt_at", "next_attempt_at"),
    ):
        if _has_column("runtime_capture_requests", column_name) and not _has_index(
            "runtime_capture_requests",
            index_name,
        ):
            op.create_index(index_name, "runtime_capture_requests", [column_name])


def downgrade() -> None:
    if not _has_table("runtime_capture_requests"):
        return
    for index_name in (
        "ix_runtime_capture_requests_next_attempt_at",
        "ix_runtime_capture_requests_status",
        "ix_runtime_capture_requests_duplicate_of_request_id",
        "ix_runtime_capture_requests_dedupe_key",
        "ix_runtime_capture_requests_capture_mode",
        "ix_runtime_capture_requests_source_type",
        "ix_runtime_capture_requests_label_studio_project_id",
        "ix_runtime_capture_requests_manual_capture_request_id",
        "ix_runtime_capture_requests_manual_capture_session_id",
    ):
        if _has_index("runtime_capture_requests", index_name):
            op.drop_index(index_name, table_name="runtime_capture_requests")
    for column_name in (
        "metadata",
        "rate_limited_until",
        "next_attempt_at",
        "delivered_at",
        "submitted_at",
        "claimed_at",
        "queued_at",
        "max_attempts",
        "label_studio_task_ids",
        "capture_intent",
        "source_frame_metadata",
        "duplicate_of_request_id",
        "dedupe_key",
        "asset_payload",
        "capture_mode",
        "source_type",
        "target_project_title",
        "label_studio_project_id",
        "manual_capture_request_id",
        "manual_capture_session_id",
    ):
        if _has_column("runtime_capture_requests", column_name):
            op.drop_column("runtime_capture_requests", column_name)
