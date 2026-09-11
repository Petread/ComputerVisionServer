"""Complete runtime output connector delivery state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260502_0018"
down_revision = "20260502_0017"
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


def _json_default(value: str) -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text(f"'{value}'::json")
    return sa.text(f"'{value}'")


def upgrade() -> None:
    if _has_table("output_connectors"):
        output_columns = (
            sa.Column("pipeline_version_id", sa.String(length=64)),
            sa.Column("source_node_key", sa.String(length=64)),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_delivery_status", sa.String(length=32)),
            sa.Column("last_success_at", sa.DateTime(timezone=True)),
            sa.Column("created_by", sa.String(length=64)),
            sa.Column("updated_by", sa.String(length=64)),
        )
        for column in output_columns:
            if not _has_column("output_connectors", column.name):
                op.add_column("output_connectors", column)
        for index_name, column_names in (
            ("ix_output_connectors_pipeline_version_id", ["pipeline_version_id"]),
            ("ix_output_connectors_source_node_key", ["source_node_key"]),
            ("ix_output_connectors_channel_type", ["channel_id", "connector_type"]),
        ):
            if not _has_index("output_connectors", index_name):
                op.create_index(index_name, "output_connectors", column_names)
        op.execute(
            "UPDATE output_connectors SET status = 'active' "
            "WHERE status IS NULL OR status = 'draft'"
        )

    if not _has_table("runtime_connector_deliveries"):
        op.create_table(
            "runtime_connector_deliveries",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("output_connector_id", sa.String(length=64), nullable=False),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("result_record_id", sa.String(length=64), nullable=False),
            sa.Column("connector_type", sa.String(length=120), nullable=False),
            sa.Column("source_node_key", sa.String(length=64)),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("request_payload", sa.JSON(), nullable=False, server_default=_json_default("{}")),
            sa.Column("response_payload", sa.JSON(), nullable=False, server_default=_json_default("{}")),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
            sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
            sa.Column("delivered_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.Text()),
        )
    else:
        delivery_columns = (
            sa.Column("channel_id", sa.String(length=64), nullable=False, server_default="unknown"),
            sa.Column("connector_type", sa.String(length=120), nullable=False, server_default="output.unknown"),
            sa.Column("source_node_key", sa.String(length=64)),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
            sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
        )
        for column in delivery_columns:
            if not _has_column("runtime_connector_deliveries", column.name):
                op.add_column("runtime_connector_deliveries", column)
        op.execute(
            "UPDATE runtime_connector_deliveries SET status = 'queued' "
            "WHERE status IS NULL OR status = 'draft'"
        )

    if _has_table("runtime_connector_deliveries"):
        for index_name, column_names in (
            ("ix_runtime_connector_deliveries_output_connector_id", ["output_connector_id"]),
            ("ix_runtime_connector_deliveries_channel_id", ["channel_id"]),
            ("ix_runtime_connector_deliveries_result_record_id", ["result_record_id"]),
            ("ix_runtime_connector_deliveries_connector_type", ["connector_type"]),
            ("ix_runtime_connector_deliveries_source_node_key", ["source_node_key"]),
            ("ix_runtime_connector_deliveries_status", ["status"]),
            ("ix_runtime_connector_deliveries_next_attempt_at", ["next_attempt_at"]),
            ("ix_runtime_connector_delivery_due", ["status", "next_attempt_at"]),
            ("ix_runtime_connector_delivery_channel_created", ["channel_id", "created_at"]),
        ):
            if not _has_index("runtime_connector_deliveries", index_name):
                op.create_index(index_name, "runtime_connector_deliveries", column_names)


def downgrade() -> None:
    if _has_table("runtime_connector_deliveries"):
        for index_name in (
            "ix_runtime_connector_delivery_channel_created",
            "ix_runtime_connector_delivery_due",
            "ix_runtime_connector_deliveries_next_attempt_at",
            "ix_runtime_connector_deliveries_status",
            "ix_runtime_connector_deliveries_source_node_key",
            "ix_runtime_connector_deliveries_connector_type",
            "ix_runtime_connector_deliveries_result_record_id",
            "ix_runtime_connector_deliveries_channel_id",
            "ix_runtime_connector_deliveries_output_connector_id",
        ):
            if _has_index("runtime_connector_deliveries", index_name):
                op.drop_index(index_name, table_name="runtime_connector_deliveries")
        for column_name in (
            "completed_at",
            "next_attempt_at",
            "claimed_at",
            "queued_at",
            "max_attempts",
            "source_node_key",
            "connector_type",
            "channel_id",
        ):
            if _has_column("runtime_connector_deliveries", column_name):
                op.drop_column("runtime_connector_deliveries", column_name)

    if _has_table("output_connectors"):
        for index_name in (
            "ix_output_connectors_channel_type",
            "ix_output_connectors_source_node_key",
            "ix_output_connectors_pipeline_version_id",
        ):
            if _has_index("output_connectors", index_name):
                op.drop_index(index_name, table_name="output_connectors")
        for column_name in (
            "updated_by",
            "created_by",
            "last_success_at",
            "last_delivery_status",
            "enabled",
            "source_node_key",
            "pipeline_version_id",
        ):
            if _has_column("output_connectors", column_name):
                op.drop_column("output_connectors", column_name)
