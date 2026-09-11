"""Add M3 immutable Edge workload releases and deployment attempts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260721_0029"
down_revision = "20260721_0028"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return _has_table(table) and column in {
        item["name"] for item in _inspector().get_columns(table)
    }


def upgrade() -> None:
    if not _has_table("edge_workload_releases"):
        op.create_table(
            "edge_workload_releases",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("release_digest", sa.String(length=64), unique=True, nullable=False),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("pipeline_version_id", sa.String(length=64), nullable=False),
            sa.Column("edge_device_id", sa.String(length=64), nullable=False),
            sa.Column("capability_digest", sa.String(length=64), nullable=False),
            sa.Column("platform", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("manifest", sa.JSON(), nullable=False),
            sa.Column("artifact_sources", sa.JSON(), nullable=False),
            sa.Column("admission_report", sa.JSON(), nullable=False),
            sa.Column("artifact_count", sa.Integer(), nullable=False),
            sa.Column("total_artifact_bytes", sa.Integer(), nullable=False),
            sa.Column("required_node_types", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(length=128)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_edge_workload_release_channel_created",
            "edge_workload_releases",
            ["channel_id", "created_at"],
        )
        for column in ("channel_id", "pipeline_version_id", "edge_device_id"):
            op.create_index(
                f"ix_edge_workload_releases_{column}", "edge_workload_releases", [column]
            )

    if not _has_table("edge_deployment_attempts"):
        op.create_table(
            "edge_deployment_attempts",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("edge_device_id", sa.String(length=64), nullable=False),
            sa.Column("workload_release_id", sa.String(length=64), nullable=False),
            sa.Column("previous_edge_deployment_id", sa.String(length=64)),
            sa.Column("command_id", sa.String(length=64), unique=True),
            sa.Column("desired_state", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("remote_status", sa.String(length=32)),
            sa.Column("progress", sa.JSON(), nullable=False),
            sa.Column("last_error", sa.Text()),
            sa.Column("activated_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_edge_deployment_channel_created",
            "edge_deployment_attempts",
            ["channel_id", "created_at"],
        )
        op.create_index(
            "ix_edge_deployment_status_updated",
            "edge_deployment_attempts",
            ["status", "updated_at"],
        )
        for column in ("channel_id", "edge_device_id", "workload_release_id", "command_id"):
            op.create_index(
                f"ix_edge_deployment_attempts_{column}",
                "edge_deployment_attempts",
                [column],
            )

    for column in ("edge_workload_release_id", "edge_deployment_id"):
        if _has_table("runtime_channels") and not _has_column("runtime_channels", column):
            op.add_column("runtime_channels", sa.Column(column, sa.String(length=64)))
            op.create_index(f"ix_runtime_channels_{column}", "runtime_channels", [column])


def downgrade() -> None:
    for column in ("edge_deployment_id", "edge_workload_release_id"):
        if _has_column("runtime_channels", column):
            op.drop_index(f"ix_runtime_channels_{column}", table_name="runtime_channels")
            op.drop_column("runtime_channels", column)
    for table in ("edge_deployment_attempts", "edge_workload_releases"):
        if _has_table(table):
            op.drop_table(table)
