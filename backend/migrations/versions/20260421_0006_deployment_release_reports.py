"""Backfill missing deployment release report columns for migrated databases."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260421_0006"
down_revision = "20260421_0005"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("deployment_releases", "compatibility_report"):
        op.add_column(
            "deployment_releases",
            sa.Column(
                "compatibility_report",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )
    if not _has_column("deployment_releases", "smoke_report"):
        op.add_column(
            "deployment_releases",
            sa.Column(
                "smoke_report",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )


def downgrade() -> None:
    if _has_column("deployment_releases", "smoke_report"):
        op.drop_column("deployment_releases", "smoke_report")
    if _has_column("deployment_releases", "compatibility_report"):
        op.drop_column("deployment_releases", "compatibility_report")
