"""Track Label Studio prediction backend links."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260511_0024"
down_revision = "20260509_0023"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("label_projects", "prediction_backend_enabled"):
        op.add_column(
            "label_projects",
            sa.Column(
                "prediction_backend_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if not _has_column("label_projects", "prediction_backend_id"):
        op.add_column("label_projects", sa.Column("prediction_backend_id", sa.Integer(), nullable=True))
    if not _has_column("label_projects", "prediction_backend_url"):
        op.add_column(
            "label_projects",
            sa.Column("prediction_backend_url", sa.String(length=512), nullable=True),
        )
    if not _has_column("label_projects", "prediction_backend_status"):
        op.add_column(
            "label_projects",
            sa.Column(
                "prediction_backend_status",
                sa.String(length=32),
                nullable=False,
                server_default="not_configured",
            ),
        )
    if not _has_column("label_projects", "prediction_model_family_id"):
        op.add_column(
            "label_projects",
            sa.Column("prediction_model_family_id", sa.String(length=64), nullable=True),
        )
    if not _has_column("label_projects", "prediction_model_version_id"):
        op.add_column(
            "label_projects",
            sa.Column("prediction_model_version_id", sa.String(length=64), nullable=True),
        )
    if not _has_column("label_projects", "prediction_linked_at"):
        op.add_column(
            "label_projects",
            sa.Column("prediction_linked_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("label_projects", "prediction_last_error"):
        op.add_column("label_projects", sa.Column("prediction_last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    for column_name in [
        "prediction_last_error",
        "prediction_linked_at",
        "prediction_model_version_id",
        "prediction_model_family_id",
        "prediction_backend_status",
        "prediction_backend_url",
        "prediction_backend_id",
        "prediction_backend_enabled",
    ]:
        if _has_column("label_projects", column_name):
            op.drop_column("label_projects", column_name)
