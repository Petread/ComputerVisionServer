"""Align runtime result records for result-service queries."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260502_0016"
down_revision = "20260502_0015"
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
    if not _has_table("result_records"):
        return

    columns = (
        sa.Column("pipeline_version_id", sa.String(length=64)),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="realtime"),
        sa.Column("result_type", sa.String(length=64), nullable=False, server_default="pipeline_execution"),
        sa.Column("raw_inference_outputs", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("normalized_inference_outputs", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("derived_outputs", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("overlay_primitives", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("trigger_response", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("runtime_metadata", sa.JSON(), nullable=False, server_default=_json_default("{}")),
    )
    for column in columns:
        if not _has_column("result_records", column.name):
            op.add_column("result_records", column)

    for index_name, column_names in (
        ("ix_result_records_pipeline_version_id", ["pipeline_version_id"]),
        ("ix_result_records_mode", ["mode"]),
        ("ix_result_records_status", ["status"]),
        ("ix_result_records_result_type", ["result_type"]),
        ("ix_result_records_completed_at", ["completed_at"]),
    ):
        if not _has_index("result_records", index_name):
            op.create_index(index_name, "result_records", column_names)

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            UPDATE result_records
            SET
              pipeline_version_id = COALESCE(
                pipeline_version_id,
                result->>'pipeline_version_id'
              ),
              mode = COALESCE(
                NULLIF(mode, ''),
                CASE WHEN trigger_endpoint_id IS NOT NULL THEN 'trigger' ELSE 'realtime' END
              ),
              result_type = CASE
                WHEN status = 'timeout' THEN 'timeout'
                WHEN trigger_endpoint_id IS NOT NULL THEN 'trigger_execution'
                ELSE COALESCE(NULLIF(result_type, ''), 'pipeline_execution')
              END,
              raw_inference_outputs = COALESCE(result->'raw_inference_outputs', raw_inference_outputs, '[]'::json),
              normalized_inference_outputs = COALESCE(result->'normalized_inference_outputs', normalized_inference_outputs, '[]'::json),
              derived_outputs = COALESCE(result->'derived_outputs', derived_outputs, '[]'::json),
              trigger_response = COALESCE(result->'final_response', trigger_response, '{}'::json),
              runtime_metadata = COALESCE(NULLIF(runtime_metadata::text, '{}')::json, '{}'::json)
            """
        )


def downgrade() -> None:
    if not _has_table("result_records"):
        return
    for index_name in (
        "ix_result_records_completed_at",
        "ix_result_records_result_type",
        "ix_result_records_status",
        "ix_result_records_mode",
        "ix_result_records_pipeline_version_id",
    ):
        if _has_index("result_records", index_name):
            op.drop_index(index_name, table_name="result_records")
    for column_name in (
        "runtime_metadata",
        "trigger_response",
        "overlay_primitives",
        "derived_outputs",
        "normalized_inference_outputs",
        "raw_inference_outputs",
        "result_type",
        "mode",
        "pipeline_version_id",
    ):
        if _has_column("result_records", column_name):
            op.drop_column("result_records", column_name)
