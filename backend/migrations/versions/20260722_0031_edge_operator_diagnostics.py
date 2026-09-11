"""Add M5 Edge operator diagnostic reports."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260722_0031"
down_revision = "20260721_0030"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def upgrade() -> None:
    if _has_table("edge_diagnostic_reports"):
        return
    op.create_table(
        "edge_diagnostic_reports",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("edge_device_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_edge_diagnostic_reports_edge_device_id",
        "edge_diagnostic_reports",
        ["edge_device_id"],
    )
    op.create_index(
        "ix_edge_diagnostic_reports_status",
        "edge_diagnostic_reports",
        ["status"],
    )
    op.create_index(
        "ix_edge_diagnostic_reports_device_generated",
        "edge_diagnostic_reports",
        ["edge_device_id", "generated_at"],
    )
    op.create_index(
        "ix_edge_diagnostic_reports_status_generated",
        "edge_diagnostic_reports",
        ["status", "generated_at"],
    )


def downgrade() -> None:
    if _has_table("edge_diagnostic_reports"):
        op.drop_table("edge_diagnostic_reports")
