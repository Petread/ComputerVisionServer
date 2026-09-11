"""Add persistent areas and facilities without interpreting legacy place labels.

Revision ID: 20260907_0037
Revises: 20260820_0036
"""

import sqlalchemy as sa
from alembic import op

revision = "20260907_0037"
down_revision = "20260820_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_areas",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("name_key", sa.String(480), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "runtime_facilities",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "area_id",
            sa.String(64),
            sa.ForeignKey("runtime_areas.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("name_key", sa.String(480), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("area_id", "name_key", name="uq_runtime_facility_area_name"),
    )
    op.create_index("ix_runtime_facilities_area_id", "runtime_facilities", ["area_id"])
    op.add_column("runtime_channels", sa.Column("facility_id", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_runtime_channel_facility",
        "runtime_channels",
        "runtime_facilities",
        ["facility_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_runtime_channels_facility_id", "runtime_channels", ["facility_id"])


def downgrade() -> None:
    # Application rollbacks retain this additive schema. Explicit schema downgrade
    # necessarily removes structure and must be preceded by a database backup.
    op.drop_index("ix_runtime_channels_facility_id", table_name="runtime_channels")
    op.drop_constraint("fk_runtime_channel_facility", "runtime_channels", type_="foreignkey")
    op.drop_column("runtime_channels", "facility_id")
    op.drop_index("ix_runtime_facilities_area_id", table_name="runtime_facilities")
    op.drop_table("runtime_facilities")
    op.drop_table("runtime_areas")
