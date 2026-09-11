"""Add model capability policy metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260430_0010"
down_revision = "20260423_0009"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("model_templates", "default_promotion_policy"):
        op.add_column(
            "model_templates",
            sa.Column(
                "default_promotion_policy",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )
    if not _has_column("model_families", "promotion_policy"):
        op.add_column(
            "model_families",
            sa.Column(
                "promotion_policy",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )
    if not _has_column("training_jobs", "promotion_policy"):
        op.add_column(
            "training_jobs",
            sa.Column(
                "promotion_policy",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )
    if not _has_column("model_versions", "promotion_policy"):
        op.add_column(
            "model_versions",
            sa.Column(
                "promotion_policy",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
        )


def downgrade() -> None:
    for table_name, column_name in (
        ("model_versions", "promotion_policy"),
        ("training_jobs", "promotion_policy"),
        ("model_families", "promotion_policy"),
        ("model_templates", "default_promotion_policy"),
    ):
        if _has_column(table_name, column_name):
            op.drop_column(table_name, column_name)
