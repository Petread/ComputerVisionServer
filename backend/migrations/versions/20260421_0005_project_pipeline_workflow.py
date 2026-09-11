"""Add project automation policy and logical model metadata for pipeline-first workflows."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260421_0005"
down_revision = "20260420_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "label_projects",
        sa.Column("auto_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "label_projects",
        sa.Column("auto_train_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "label_projects",
        sa.Column("auto_deploy_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("label_projects", sa.Column("default_model_template", sa.String(length=120)))
    op.add_column("label_projects", sa.Column("default_training_profile", sa.String(length=120)))
    op.add_column(
        "label_projects",
        sa.Column(
            "preferred_endpoint_mode",
            sa.String(length=32),
            nullable=False,
            server_default="realtime",
        ),
    )

    op.add_column("model_families", sa.Column("label_project_id", sa.String(length=64)))
    op.add_column("model_families", sa.Column("display_name", sa.String(length=160)))
    op.add_column("model_families", sa.Column("description", sa.Text()))
    op.add_column("model_families", sa.Column("template_key", sa.String(length=120)))
    op.add_column("model_families", sa.Column("training_profile_key", sa.String(length=120)))
    op.create_index("ix_model_families_label_project_id", "model_families", ["label_project_id"])


def downgrade() -> None:
    op.drop_index("ix_model_families_label_project_id", table_name="model_families")
    op.drop_column("model_families", "training_profile_key")
    op.drop_column("model_families", "template_key")
    op.drop_column("model_families", "description")
    op.drop_column("model_families", "display_name")
    op.drop_column("model_families", "label_project_id")

    op.drop_column("label_projects", "preferred_endpoint_mode")
    op.drop_column("label_projects", "default_training_profile")
    op.drop_column("label_projects", "default_model_template")
    op.drop_column("label_projects", "auto_deploy_enabled")
    op.drop_column("label_projects", "auto_train_enabled")
    op.drop_column("label_projects", "auto_sync_enabled")
