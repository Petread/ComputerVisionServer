"""Add M4 Edge result receipts and viewer leases."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260721_0030"
down_revision = "20260721_0029"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def upgrade() -> None:
    if not _has_table("edge_result_receipts"):
        op.create_table(
            "edge_result_receipts",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("edge_device_id", sa.String(length=64), nullable=False),
            sa.Column("management_ip", sa.String(length=15), nullable=False),
            sa.Column("boot_id", sa.String(length=128), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("workload_release_id", sa.String(length=64), nullable=False),
            sa.Column("result_record_id", sa.String(length=64), nullable=False, unique=True),
            sa.Column("envelope_sha256", sa.String(length=64), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint(
                "edge_device_id",
                "boot_id",
                "sequence",
                name="uq_edge_result_receipt_device_boot_sequence",
            ),
        )
        for column in (
            "edge_device_id",
            "channel_id",
            "workload_release_id",
            "result_record_id",
        ):
            op.create_index(f"ix_edge_result_receipts_{column}", "edge_result_receipts", [column])
        op.create_index(
            "ix_edge_result_receipts_channel_created",
            "edge_result_receipts",
            ["channel_id", "created_at"],
        )

    if not _has_table("edge_viewer_leases"):
        op.create_table(
            "edge_viewer_leases",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("edge_device_id", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("max_fps", sa.Float(), nullable=False),
            sa.Column("max_width", sa.Integer(), nullable=False),
            sa.Column("include_overlay", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in ("channel_id", "edge_device_id", "status"):
            op.create_index(f"ix_edge_viewer_leases_{column}", "edge_viewer_leases", [column])
        op.create_index(
            "ix_edge_viewer_leases_channel_expiry",
            "edge_viewer_leases",
            ["channel_id", "expires_at"],
        )
        op.create_index(
            "ix_edge_viewer_leases_device_expiry",
            "edge_viewer_leases",
            ["edge_device_id", "expires_at"],
        )


def downgrade() -> None:
    for table in ("edge_viewer_leases", "edge_result_receipts"):
        if _has_table(table):
            op.drop_table(table)
