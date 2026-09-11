"""Add M1 edge fleet persistence and runtime channel placement."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260721_0028"
down_revision = "20260721_0027"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return column_name in {column["name"] for column in _inspector().get_columns(table_name)}


def upgrade() -> None:
    if not _has_table("edge_devices"):
        op.create_table(
            "edge_devices",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("technical_place", sa.String(length=160)),
            sa.Column("management_ip", sa.String(length=15), nullable=False, unique=True),
            sa.Column("control_port", sa.Integer(), nullable=False),
            sa.Column("plant_api_port", sa.Integer(), nullable=False),
            sa.Column("rtsp_port", sa.Integer(), nullable=False),
            sa.Column("allowed_cidr", sa.String(length=32), nullable=False),
            sa.Column("server_ip", sa.String(length=15), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("connectivity_state", sa.String(length=32), nullable=False),
            sa.Column("reported_health_state", sa.String(length=32)),
            sa.Column("last_seen_at", sa.DateTime(timezone=True)),
            sa.Column("last_poll_attempt_at", sa.DateTime(timezone=True)),
            sa.Column("last_connectivity_error", sa.Text()),
            sa.Column("current_boot_id", sa.String(length=128)),
            sa.Column("identity_generation", sa.Integer(), nullable=False),
            sa.Column("deployment_revalidation_required", sa.Boolean(), nullable=False),
            sa.Column("identity_payload", sa.JSON()),
            sa.Column("health_payload", sa.JSON()),
            sa.Column("latest_capability_snapshot_id", sa.String(length=64)),
            sa.Column("latest_capability_digest", sa.String(length=64)),
            sa.Column("created_by", sa.String(length=64)),
            sa.Column("updated_by", sa.String(length=64)),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_edge_devices_connectivity",
            "edge_devices",
            ["connectivity_state", "last_seen_at"],
        )
        op.create_index(
            "ix_edge_devices_last_seen_at",
            "edge_devices",
            ["last_seen_at"],
        )
        op.create_index(
            "ix_edge_devices_latest_capability_snapshot_id",
            "edge_devices",
            ["latest_capability_snapshot_id"],
        )
        op.create_index("ix_edge_devices_deleted_at", "edge_devices", ["deleted_at"])

    if not _has_table("edge_capability_snapshots"):
        op.create_table(
            "edge_capability_snapshots",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("edge_device_id", sa.String(length=64), nullable=False),
            sa.Column("digest", sa.String(length=64), nullable=False),
            sa.Column("boot_id", sa.String(length=128), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_edge_capability_snapshots_edge_device_id",
            "edge_capability_snapshots",
            ["edge_device_id"],
        )
        op.create_index(
            "ix_edge_capability_snapshots_device_observed",
            "edge_capability_snapshots",
            ["edge_device_id", "observed_at"],
        )

    if not _has_table("edge_fleet_commands"):
        op.create_table(
            "edge_fleet_commands",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("edge_device_id", sa.String(length=64), nullable=False),
            sa.Column("command_type", sa.String(length=64), nullable=False),
            sa.Column("idempotency_key", sa.String(length=160), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("request_payload", sa.JSON(), nullable=False),
            sa.Column("response_payload", sa.JSON(), nullable=False),
            sa.Column("last_error", sa.Text()),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint(
                "edge_device_id",
                "idempotency_key",
                name="uq_edge_fleet_command_idempotency",
            ),
        )
        op.create_index(
            "ix_edge_fleet_commands_edge_device_id",
            "edge_fleet_commands",
            ["edge_device_id"],
        )
        op.create_index(
            "ix_edge_fleet_commands_device_created",
            "edge_fleet_commands",
            ["edge_device_id", "created_at"],
        )

    if not _has_table("edge_result_cursors"):
        op.create_table(
            "edge_result_cursors",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("edge_device_id", sa.String(length=64), nullable=False),
            sa.Column("management_ip", sa.String(length=15), nullable=False),
            sa.Column("boot_id", sa.String(length=128), nullable=False),
            sa.Column("last_persisted_sequence", sa.Integer(), nullable=False),
            sa.Column("last_acknowledged_sequence", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "edge_device_id",
                "boot_id",
                name="uq_edge_result_cursor_device_boot",
            ),
        )
        op.create_index(
            "ix_edge_result_cursors_edge_device_id",
            "edge_result_cursors",
            ["edge_device_id"],
        )

    if _has_table("runtime_channels") and not _has_column("runtime_channels", "execution_target"):
        op.add_column(
            "runtime_channels",
            sa.Column(
                "execution_target",
                sa.String(length=32),
                nullable=False,
                server_default="server",
            ),
        )
    if _has_table("runtime_channels") and not _has_column("runtime_channels", "edge_device_id"):
        op.add_column(
            "runtime_channels",
            sa.Column("edge_device_id", sa.String(length=64)),
        )
        op.create_index(
            "ix_runtime_channels_edge_device_id",
            "runtime_channels",
            ["edge_device_id"],
        )


def downgrade() -> None:
    if _has_column("runtime_channels", "edge_device_id"):
        op.drop_index("ix_runtime_channels_edge_device_id", table_name="runtime_channels")
        op.drop_column("runtime_channels", "edge_device_id")
    if _has_column("runtime_channels", "execution_target"):
        op.drop_column("runtime_channels", "execution_target")
    for table_name in (
        "edge_result_cursors",
        "edge_fleet_commands",
        "edge_capability_snapshots",
        "edge_devices",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
