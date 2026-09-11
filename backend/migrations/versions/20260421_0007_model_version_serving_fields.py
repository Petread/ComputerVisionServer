"""Backfill missing model version serving package fields for migrated databases."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260421_0007"
down_revision = "20260421_0006"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("model_versions", "serving_package_status"):
        op.add_column(
            "model_versions",
            sa.Column(
                "serving_package_status",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'missing'"),
            ),
        )
    if not _has_column("model_versions", "serving_package_built_at"):
        op.add_column("model_versions", sa.Column("serving_package_built_at", sa.DateTime(timezone=True)))
    if not _has_column("model_versions", "serving_package_error"):
        op.add_column("model_versions", sa.Column("serving_package_error", sa.Text()))
    if not _has_column("model_versions", "serving_manifest_uri"):
        op.add_column("model_versions", sa.Column("serving_manifest_uri", sa.String(length=512)))
    if not _has_column("model_versions", "serving_manifest"):
        op.add_column(
            "model_versions",
            sa.Column(
                "serving_manifest",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )


def downgrade() -> None:
    if _has_column("model_versions", "serving_manifest"):
        op.drop_column("model_versions", "serving_manifest")
    if _has_column("model_versions", "serving_manifest_uri"):
        op.drop_column("model_versions", "serving_manifest_uri")
    if _has_column("model_versions", "serving_package_error"):
        op.drop_column("model_versions", "serving_package_error")
    if _has_column("model_versions", "serving_package_built_at"):
        op.drop_column("model_versions", "serving_package_built_at")
    if _has_column("model_versions", "serving_package_status"):
        op.drop_column("model_versions", "serving_package_status")
