"""Add configurable model templates, cleanup metadata, and segmentation-ready fields."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260423_0009"
down_revision = "20260421_0008"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    _upgrade_label_projects()
    _upgrade_dataset_snapshots()
    _upgrade_training_jobs()
    _upgrade_model_families()
    _upgrade_model_versions()
    _upgrade_deployment_releases()
    _create_model_templates()


def downgrade() -> None:
    if _has_table("model_templates"):
        op.drop_table("model_templates")

    _drop_index_if_exists("deployment_releases", "ix_deployment_releases_deleted_at")
    _drop_columns(
        "deployment_releases",
        ("delete_reason", "deleted_by", "deleted_at"),
    )

    _drop_index_if_exists("model_versions", "ix_model_versions_deleted_at")
    _drop_columns(
        "model_versions",
        (
            "delete_reason",
            "deleted_by",
            "deleted_at",
            "resolved_training_defaults",
            "model_template_version",
            "model_template_key",
            "architecture_display_name",
            "architecture_size",
            "architecture_version",
            "architecture_family",
        ),
    )

    _drop_index_if_exists("model_families", "ix_model_families_deleted_at")
    _drop_columns(
        "model_families",
        (
            "delete_reason",
            "deleted_by",
            "deleted_at",
            "architecture_display_name",
            "architecture_size",
            "architecture_version",
            "architecture_family",
            "template_version",
            "template_key",
        ),
    )

    _drop_index_if_exists("training_jobs", "ix_training_jobs_deleted_at")
    _drop_columns(
        "training_jobs",
        (
            "delete_reason",
            "deleted_by",
            "deleted_at",
            "resolved_training_defaults",
            "optimizer_settings",
            "optimizer",
            "batch_size",
            "image_size",
            "learning_rate",
            "model_template_version",
            "model_template_key",
            "architecture_display_name",
            "architecture_size",
            "architecture_version",
            "architecture_family",
        ),
    )

    _drop_index_if_exists("dataset_snapshots", "ix_dataset_snapshots_deleted_at")
    _drop_columns(
        "dataset_snapshots",
        (
            "delete_reason",
            "deleted_by",
            "deleted_at",
            "invalid_item_count",
            "draft_item_count",
            "submitted_background_item_count",
            "submitted_labeled_item_count",
            "eligible_item_count",
        ),
    )

    _drop_index_if_exists("label_projects", "ix_label_projects_deleted_at")
    _drop_columns(
        "label_projects",
        (
            "delete_reason",
            "deleted_by",
            "deleted_at",
            "invalid_task_count",
            "submitted_background_task_count",
            "submitted_labeled_task_count",
            "eligible_task_count",
            "draft_task_count",
        ),
    )


def _upgrade_label_projects() -> None:
    if not _has_column("label_projects", "draft_task_count"):
        op.add_column(
            "label_projects",
            sa.Column("draft_task_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("label_projects", "eligible_task_count"):
        op.add_column(
            "label_projects",
            sa.Column("eligible_task_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("label_projects", "submitted_labeled_task_count"):
        op.add_column(
            "label_projects",
            sa.Column("submitted_labeled_task_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("label_projects", "submitted_background_task_count"):
        op.add_column(
            "label_projects",
            sa.Column("submitted_background_task_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("label_projects", "invalid_task_count"):
        op.add_column(
            "label_projects",
            sa.Column("invalid_task_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("label_projects", "deleted_at"):
        op.add_column("label_projects", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    if not _has_column("label_projects", "deleted_by"):
        op.add_column("label_projects", sa.Column("deleted_by", sa.String(length=64)))
    if not _has_column("label_projects", "delete_reason"):
        op.add_column("label_projects", sa.Column("delete_reason", sa.Text()))
    if not _has_index("label_projects", "ix_label_projects_deleted_at"):
        op.create_index("ix_label_projects_deleted_at", "label_projects", ["deleted_at"], unique=False)


def _upgrade_dataset_snapshots() -> None:
    if not _has_column("dataset_snapshots", "eligible_item_count"):
        op.add_column(
            "dataset_snapshots",
            sa.Column("eligible_item_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("dataset_snapshots", "submitted_labeled_item_count"):
        op.add_column(
            "dataset_snapshots",
            sa.Column("submitted_labeled_item_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("dataset_snapshots", "submitted_background_item_count"):
        op.add_column(
            "dataset_snapshots",
            sa.Column("submitted_background_item_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("dataset_snapshots", "draft_item_count"):
        op.add_column(
            "dataset_snapshots",
            sa.Column("draft_item_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("dataset_snapshots", "invalid_item_count"):
        op.add_column(
            "dataset_snapshots",
            sa.Column("invalid_item_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("dataset_snapshots", "deleted_at"):
        op.add_column("dataset_snapshots", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    if not _has_column("dataset_snapshots", "deleted_by"):
        op.add_column("dataset_snapshots", sa.Column("deleted_by", sa.String(length=64)))
    if not _has_column("dataset_snapshots", "delete_reason"):
        op.add_column("dataset_snapshots", sa.Column("delete_reason", sa.Text()))
    if not _has_index("dataset_snapshots", "ix_dataset_snapshots_deleted_at"):
        op.create_index("ix_dataset_snapshots_deleted_at", "dataset_snapshots", ["deleted_at"], unique=False)


def _upgrade_training_jobs() -> None:
    if not _has_column("training_jobs", "architecture_family"):
        op.add_column(
            "training_jobs",
            sa.Column(
                "architecture_family",
                sa.String(length=64),
                nullable=False,
                server_default="yolo",
            ),
        )
    if not _has_column("training_jobs", "architecture_version"):
        op.add_column(
            "training_jobs",
            sa.Column(
                "architecture_version",
                sa.String(length=32),
                nullable=False,
                server_default="v8",
            ),
        )
    if not _has_column("training_jobs", "architecture_size"):
        op.add_column(
            "training_jobs",
            sa.Column(
                "architecture_size",
                sa.String(length=16),
                nullable=False,
                server_default="n",
            ),
        )
    if not _has_column("training_jobs", "architecture_display_name"):
        op.add_column(
            "training_jobs",
            sa.Column(
                "architecture_display_name",
                sa.String(length=160),
                nullable=False,
                server_default="YOLOv8 Nano",
            ),
        )
    if not _has_column("training_jobs", "model_template_key"):
        op.add_column("training_jobs", sa.Column("model_template_key", sa.String(length=120)))
    if not _has_column("training_jobs", "model_template_version"):
        op.add_column("training_jobs", sa.Column("model_template_version", sa.Integer()))
    if not _has_column("training_jobs", "learning_rate"):
        op.add_column(
            "training_jobs",
            sa.Column("learning_rate", sa.Float(), nullable=False, server_default="0.001"),
        )
    if not _has_column("training_jobs", "image_size"):
        op.add_column("training_jobs", sa.Column("image_size", sa.Integer()))
    if not _has_column("training_jobs", "batch_size"):
        op.add_column("training_jobs", sa.Column("batch_size", sa.Integer()))
    if not _has_column("training_jobs", "optimizer"):
        op.add_column("training_jobs", sa.Column("optimizer", sa.String(length=120)))
    if not _has_column("training_jobs", "optimizer_settings"):
        op.add_column(
            "training_jobs",
            sa.Column("optimizer_settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        )
    if not _has_column("training_jobs", "resolved_training_defaults"):
        op.add_column(
            "training_jobs",
            sa.Column("resolved_training_defaults", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        )
    if not _has_column("training_jobs", "deleted_at"):
        op.add_column("training_jobs", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    if not _has_column("training_jobs", "deleted_by"):
        op.add_column("training_jobs", sa.Column("deleted_by", sa.String(length=64)))
    if not _has_column("training_jobs", "delete_reason"):
        op.add_column("training_jobs", sa.Column("delete_reason", sa.Text()))
    if not _has_index("training_jobs", "ix_training_jobs_deleted_at"):
        op.create_index("ix_training_jobs_deleted_at", "training_jobs", ["deleted_at"], unique=False)


def _upgrade_model_families() -> None:
    if not _has_column("model_families", "template_key"):
        op.add_column("model_families", sa.Column("template_key", sa.String(length=120)))
    if not _has_column("model_families", "template_version"):
        op.add_column("model_families", sa.Column("template_version", sa.Integer()))
    if not _has_column("model_families", "architecture_family"):
        op.add_column(
            "model_families",
            sa.Column(
                "architecture_family",
                sa.String(length=64),
                nullable=False,
                server_default="yolo",
            ),
        )
    if not _has_column("model_families", "architecture_version"):
        op.add_column(
            "model_families",
            sa.Column(
                "architecture_version",
                sa.String(length=32),
                nullable=False,
                server_default="v8",
            ),
        )
    if not _has_column("model_families", "architecture_size"):
        op.add_column(
            "model_families",
            sa.Column(
                "architecture_size",
                sa.String(length=16),
                nullable=False,
                server_default="n",
            ),
        )
    if not _has_column("model_families", "architecture_display_name"):
        op.add_column(
            "model_families",
            sa.Column(
                "architecture_display_name",
                sa.String(length=160),
                nullable=False,
                server_default="YOLOv8 Nano",
            ),
        )
    if not _has_column("model_families", "deleted_at"):
        op.add_column("model_families", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    if not _has_column("model_families", "deleted_by"):
        op.add_column("model_families", sa.Column("deleted_by", sa.String(length=64)))
    if not _has_column("model_families", "delete_reason"):
        op.add_column("model_families", sa.Column("delete_reason", sa.Text()))
    if not _has_index("model_families", "ix_model_families_deleted_at"):
        op.create_index("ix_model_families_deleted_at", "model_families", ["deleted_at"], unique=False)


def _upgrade_model_versions() -> None:
    if not _has_column("model_versions", "architecture_family"):
        op.add_column(
            "model_versions",
            sa.Column(
                "architecture_family",
                sa.String(length=64),
                nullable=False,
                server_default="yolo",
            ),
        )
    if not _has_column("model_versions", "architecture_version"):
        op.add_column(
            "model_versions",
            sa.Column(
                "architecture_version",
                sa.String(length=32),
                nullable=False,
                server_default="v8",
            ),
        )
    if not _has_column("model_versions", "architecture_size"):
        op.add_column(
            "model_versions",
            sa.Column(
                "architecture_size",
                sa.String(length=16),
                nullable=False,
                server_default="n",
            ),
        )
    if not _has_column("model_versions", "architecture_display_name"):
        op.add_column(
            "model_versions",
            sa.Column(
                "architecture_display_name",
                sa.String(length=160),
                nullable=False,
                server_default="YOLOv8 Nano",
            ),
        )
    if not _has_column("model_versions", "model_template_key"):
        op.add_column("model_versions", sa.Column("model_template_key", sa.String(length=120)))
    if not _has_column("model_versions", "model_template_version"):
        op.add_column("model_versions", sa.Column("model_template_version", sa.Integer()))
    if not _has_column("model_versions", "resolved_training_defaults"):
        op.add_column(
            "model_versions",
            sa.Column("resolved_training_defaults", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        )
    if not _has_column("model_versions", "deleted_at"):
        op.add_column("model_versions", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    if not _has_column("model_versions", "deleted_by"):
        op.add_column("model_versions", sa.Column("deleted_by", sa.String(length=64)))
    if not _has_column("model_versions", "delete_reason"):
        op.add_column("model_versions", sa.Column("delete_reason", sa.Text()))
    if not _has_index("model_versions", "ix_model_versions_deleted_at"):
        op.create_index("ix_model_versions_deleted_at", "model_versions", ["deleted_at"], unique=False)


def _upgrade_deployment_releases() -> None:
    if not _has_column("deployment_releases", "deleted_at"):
        op.add_column("deployment_releases", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    if not _has_column("deployment_releases", "deleted_by"):
        op.add_column("deployment_releases", sa.Column("deleted_by", sa.String(length=64)))
    if not _has_column("deployment_releases", "delete_reason"):
        op.add_column("deployment_releases", sa.Column("delete_reason", sa.Text()))
    if not _has_index("deployment_releases", "ix_deployment_releases_deleted_at"):
        op.create_index("ix_deployment_releases_deleted_at", "deployment_releases", ["deleted_at"], unique=False)


def _create_model_templates() -> None:
    if _has_table("model_templates"):
        return
    op.create_table(
        "model_templates",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("template_key", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("architecture_family", sa.String(length=64), nullable=False),
        sa.Column("architecture_version", sa.String(length=32), nullable=False),
        sa.Column("architecture_size", sa.String(length=16), nullable=False),
        sa.Column("architecture_display_name", sa.String(length=160), nullable=False),
        sa.Column("display_name_suffix", sa.String(length=80), nullable=False),
        sa.Column("default_training_profile", sa.String(length=120), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("training_defaults", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("recommended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=64)),
        sa.Column("updated_by", sa.String(length=64)),
        sa.UniqueConstraint("template_key", name="uq_model_templates_template_key"),
    )
    if not _has_index("model_templates", "ix_model_templates_template_key"):
        op.create_index("ix_model_templates_template_key", "model_templates", ["template_key"], unique=True)


def _drop_columns(table_name: str, columns: tuple[str, ...]) -> None:
    for column_name in columns:
        if _has_column(table_name, column_name):
            op.drop_column(table_name, column_name)


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    if _has_index(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)
