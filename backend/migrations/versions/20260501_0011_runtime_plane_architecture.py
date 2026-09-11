"""Add runtime plane architecture tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260501_0011"
down_revision = "20260430_0010"
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
    if not _has_table("runtime_sources"):
        op.create_table(
            "runtime_sources",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("slug", sa.String(length=120), nullable=False, unique=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("source_type", sa.String(length=64), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column(
                "secret_refs",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column("validation_status", sa.String(length=32), nullable=False),
            sa.Column("health_state", sa.String(length=32), nullable=False),
            sa.Column("last_validated_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not _has_table("runtime_channels"):
        op.create_table(
            "runtime_channels",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("slug", sa.String(length=120), nullable=False, unique=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("technical_place", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("mode", sa.String(length=32), nullable=False),
            sa.Column("assigned_pipeline", sa.String(length=160)),
            sa.Column("source_id", sa.String(length=64)),
            sa.Column("pipeline_version_id", sa.String(length=64)),
            sa.Column(
                "output_summary",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column(
                "capture_summary",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column(
                "health_summary",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column(
                "performance_summary",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column("desired_state", sa.String(length=32), nullable=False),
            sa.Column("observed_state", sa.String(length=32), nullable=False),
            sa.Column("health_state", sa.String(length=32), nullable=False),
            sa.Column("last_activity_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_by", sa.String(length=64)),
            sa.Column("updated_by", sa.String(length=64)),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
            sa.Column("deleted_by", sa.String(length=64)),
            sa.Column("delete_reason", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_runtime_channels_source_id", "runtime_channels", ["source_id"])
        op.create_index(
            "ix_runtime_channels_pipeline_version_id",
            "runtime_channels",
            ["pipeline_version_id"],
        )
        op.create_index("ix_runtime_channels_deleted_at", "runtime_channels", ["deleted_at"])

    if not _has_table("pipeline_definitions"):
        op.create_table(
            "pipeline_definitions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("slug", sa.String(length=120), nullable=False, unique=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("latest_version_id", sa.String(length=64)),
            sa.Column("published_version_id", sa.String(length=64)),
            sa.Column("template", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_by", sa.String(length=64)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not _has_table("pipeline_versions"):
        op.create_table(
            "pipeline_versions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("pipeline_definition_id", sa.String(length=64), nullable=False),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("mode", sa.String(length=32), nullable=False),
            sa.Column("graph", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column(
                "validation_report",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column(
                "model_capability_constraints",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column(
                "node_contract_versions",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column("config_digest", sa.String(length=128), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True)),
            sa.Column("published_by", sa.String(length=64)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint(
                "pipeline_definition_id",
                "version_no",
                name="uq_pipeline_definition_version",
            ),
        )
        op.create_index(
            "ix_pipeline_versions_pipeline_definition_id",
            "pipeline_versions",
            ["pipeline_definition_id"],
        )

    if not _has_table("pipeline_nodes"):
        op.create_table(
            "pipeline_nodes",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("pipeline_version_id", sa.String(length=64), nullable=False),
            sa.Column("node_key", sa.String(length=64), nullable=False),
            sa.Column("node_type", sa.String(length=120), nullable=False),
            sa.Column("node_category", sa.String(length=64), nullable=False),
            sa.Column("node_contract_version", sa.Integer(), nullable=False),
            sa.Column(
                "input_ports",
                sa.JSON(),
                nullable=False,
                server_default=_json_array_default(),
            ),
            sa.Column(
                "output_ports",
                sa.JSON(),
                nullable=False,
                server_default=_json_array_default(),
            ),
            sa.Column("config", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column("position", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("pipeline_version_id", "node_key", name="uq_pipeline_node_key"),
        )
        op.create_index(
            "ix_pipeline_nodes_pipeline_version_id",
            "pipeline_nodes",
            ["pipeline_version_id"],
        )

    if not _has_table("runtime_executions"):
        op.create_table(
            "runtime_executions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("pipeline_version_id", sa.String(length=64), nullable=False),
            sa.Column("mode", sa.String(length=32), nullable=False),
            sa.Column("executor_service", sa.String(length=64), nullable=False),
            sa.Column("executor_instance_id", sa.String(length=64)),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("desired_state", sa.String(length=32), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("stopped_at", sa.DateTime(timezone=True)),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
            sa.Column("frame_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("drop_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("trigger_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_result_id", sa.String(length=64)),
            sa.Column("last_error", sa.Text()),
            sa.Column(
                "recovery_attempts",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_runtime_executions_channel_id", "runtime_executions", ["channel_id"])

    if not _has_table("trigger_endpoints"):
        op.create_table(
            "trigger_endpoints",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("slug", sa.String(length=120), nullable=False, unique=True),
            sa.Column("method", sa.String(length=16), nullable=False, server_default="PUT"),
            sa.Column("auth_scope", sa.String(length=128), nullable=False),
            sa.Column("timeout_ms", sa.Integer(), nullable=False),
            sa.Column(
                "request_schema",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column(
                "response_schema",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("last_invoked_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_trigger_endpoints_channel_id", "trigger_endpoints", ["channel_id"])

    if not _has_table("capture_rules"):
        op.create_table(
            "capture_rules",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("pipeline_version_id", sa.String(length=64)),
            sa.Column("rule_key", sa.String(length=64), nullable=False),
            sa.Column("capture_mode", sa.String(length=64), nullable=False),
            sa.Column(
                "condition",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column(
                "target_label_project_ids",
                sa.JSON(),
                nullable=False,
                server_default=_json_array_default(),
            ),
            sa.Column(
                "rate_limit",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column(
                "dedupe_policy",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("channel_id", "rule_key", name="uq_capture_rule_channel_key"),
        )
        op.create_index("ix_capture_rules_channel_id", "capture_rules", ["channel_id"])

    if not _has_table("output_connectors"):
        op.create_table(
            "output_connectors",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("connector_type", sa.String(length=120), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column(
                "secret_refs",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column(
                "delivery_policy",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("last_delivery_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_output_connectors_channel_id", "output_connectors", ["channel_id"])

    if not _has_table("result_records"):
        op.create_table(
            "result_records",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("runtime_execution_id", sa.String(length=64)),
            sa.Column("trigger_endpoint_id", sa.String(length=64)),
            sa.Column("correlation_id", sa.String(length=64), nullable=False),
            sa.Column("source_timestamp", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("schema_version", sa.String(length=80), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column("raw_outputs_ref", sa.String(length=512)),
            sa.Column("timing", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column(
                "model_routes",
                sa.JSON(),
                nullable=False,
                server_default=_json_array_default(),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_result_records_channel_id", "result_records", ["channel_id"])
        op.create_index("ix_result_records_correlation_id", "result_records", ["correlation_id"])

    if not _has_table("runtime_capture_requests"):
        op.create_table(
            "runtime_capture_requests",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("capture_rule_id", sa.String(length=64), nullable=False),
            sa.Column("result_record_id", sa.String(length=64)),
            sa.Column("target_label_project_id", sa.String(length=64), nullable=False),
            sa.Column("asset_uri", sa.String(length=512), nullable=False),
            sa.Column("roi", sa.JSON(), nullable=False, server_default=_json_object_default()),
            sa.Column("digest", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not _has_table("runtime_connector_deliveries"):
        op.create_table(
            "runtime_connector_deliveries",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("output_connector_id", sa.String(length=64), nullable=False),
            sa.Column("result_record_id", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column(
                "request_payload",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column(
                "response_payload",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("delivered_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not _has_table("runtime_node_type_registry"):
        op.create_table(
            "runtime_node_type_registry",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("node_type", sa.String(length=120), nullable=False),
            sa.Column("node_category", sa.String(length=64), nullable=False),
            sa.Column("contract_version", sa.Integer(), nullable=False),
            sa.Column("display_name", sa.String(length=160), nullable=False),
            sa.Column(
                "supported_modes",
                sa.JSON(),
                nullable=False,
                server_default=_json_array_default(),
            ),
            sa.Column(
                "input_ports",
                sa.JSON(),
                nullable=False,
                server_default=_json_array_default(),
            ),
            sa.Column(
                "output_ports",
                sa.JSON(),
                nullable=False,
                server_default=_json_array_default(),
            ),
            sa.Column("config_schema_ref", sa.String(length=512), nullable=False),
            sa.Column("runtime_handler_ref", sa.String(length=256), nullable=False),
            sa.Column(
                "compatibility_constraints",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint(
                "node_type",
                "contract_version",
                name="uq_runtime_node_type_contract",
            ),
        )

    if not _has_table("runtime_model_capability_cache"):
        op.create_table(
            "runtime_model_capability_cache",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("model_family_slug", sa.String(length=120), nullable=False),
            sa.Column("target_path", sa.String(length=32), nullable=False),
            sa.Column("contract_major", sa.Integer(), nullable=False),
            sa.Column("deployment_release_id", sa.String(length=64), nullable=False),
            sa.Column("model_version_id", sa.String(length=64), nullable=False),
            sa.Column("task_type", sa.String(length=32), nullable=False),
            sa.Column(
                "capability",
                sa.JSON(),
                nullable=False,
                server_default=_json_object_default(),
            ),
            sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint(
                "model_family_slug",
                "target_path",
                "contract_major",
                name="uq_runtime_model_capability_ref",
            ),
        )


def downgrade() -> None:
    for table_name in (
        "runtime_model_capability_cache",
        "runtime_node_type_registry",
        "runtime_connector_deliveries",
        "runtime_capture_requests",
        "result_records",
        "output_connectors",
        "capture_rules",
        "trigger_endpoints",
        "runtime_executions",
        "pipeline_nodes",
        "pipeline_versions",
        "pipeline_definitions",
        "runtime_channels",
        "runtime_sources",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
