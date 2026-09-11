"""Add runtime pipeline designer persistence fields."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260501_0012"
down_revision = "20260501_0011"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("pipeline_versions", "change_note"):
        op.add_column("pipeline_versions", sa.Column("change_note", sa.Text()))
    if not _has_column("pipeline_versions", "created_by"):
        op.add_column("pipeline_versions", sa.Column("created_by", sa.String(length=64)))
    if not _has_column("pipeline_nodes", "logical_model_ref"):
        op.add_column("pipeline_nodes", sa.Column("logical_model_ref", sa.JSON()))
    if not _has_column("runtime_node_type_registry", "description"):
        op.add_column(
            "runtime_node_type_registry",
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
        )
    if not _has_column("runtime_node_type_registry", "property_schema"):
        op.add_column(
            "runtime_node_type_registry",
            sa.Column("property_schema", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        )
    if not _has_column("runtime_node_type_registry", "default_config"):
        op.add_column(
            "runtime_node_type_registry",
            sa.Column("default_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        )


def downgrade() -> None:
    if _has_column("runtime_node_type_registry", "default_config"):
        op.drop_column("runtime_node_type_registry", "default_config")
    if _has_column("runtime_node_type_registry", "property_schema"):
        op.drop_column("runtime_node_type_registry", "property_schema")
    if _has_column("runtime_node_type_registry", "description"):
        op.drop_column("runtime_node_type_registry", "description")
    if _has_column("pipeline_nodes", "logical_model_ref"):
        op.drop_column("pipeline_nodes", "logical_model_ref")
    if _has_column("pipeline_versions", "created_by"):
        op.drop_column("pipeline_versions", "created_by")
    if _has_column("pipeline_versions", "change_note"):
        op.drop_column("pipeline_versions", "change_note")
