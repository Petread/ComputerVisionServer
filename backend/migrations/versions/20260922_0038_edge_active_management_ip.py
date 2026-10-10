"""Allow a new Edge registration to reuse an archived device's IP.

Revision ID: 20260922_0038
Revises: 20260907_0037
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260922_0038"
down_revision = "20260907_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("edge_devices_management_ip_key", "edge_devices", type_="unique")
    op.create_index(
        "uq_edge_devices_active_management_ip",
        "edge_devices",
        ["management_ip"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT management_ip FROM edge_devices GROUP BY management_ip "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicates is not None:
        raise RuntimeError(
            "Cannot restore global Edge IP uniqueness while archived and active devices "
            "share an IP; preserve the records and keep this migration applied."
        )
    op.drop_index("uq_edge_devices_active_management_ip", table_name="edge_devices")
    op.create_unique_constraint(
        "edge_devices_management_ip_key", "edge_devices", ["management_ip"]
    )
