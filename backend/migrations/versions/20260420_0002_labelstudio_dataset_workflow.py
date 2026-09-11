"""Extend schema for Label Studio integration and immutable dataset snapshots."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260420_0002"
down_revision = "20260420_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("label_projects", "dataset_id", existing_type=sa.String(length=64), nullable=True)
    op.create_unique_constraint(
        "uq_label_project_external_id",
        "label_projects",
        ["label_studio_project_id"],
    )
    op.create_index("ix_label_projects_dataset_id", "label_projects", ["dataset_id"])
    op.add_column("label_projects", sa.Column("label_schema_version", sa.String(length=32)))
    op.add_column("label_projects", sa.Column("training_pipeline_slug", sa.String(length=120)))
    op.add_column(
        "label_projects",
        sa.Column("auto_snapshot_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "label_projects",
        sa.Column("minimum_annotation_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "label_projects",
        sa.Column(
            "required_classes",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column(
        "label_projects",
        sa.Column("train_ratio", sa.Integer(), nullable=False, server_default="80"),
    )
    op.add_column(
        "label_projects",
        sa.Column("val_ratio", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "label_projects",
        sa.Column("test_ratio", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "label_projects",
        sa.Column(
            "qualifying_actions",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column(
        "label_projects",
        sa.Column("task_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "label_projects",
        sa.Column("annotated_task_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "label_projects",
        sa.Column("annotation_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "label_projects",
        sa.Column("skipped_annotation_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "label_projects",
        sa.Column(
            "class_distribution",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column("label_projects", sa.Column("last_activity_at", sa.DateTime(timezone=True)))

    op.add_column(
        "webhook_events",
        sa.Column("headers", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column("webhook_events", sa.Column("remote_addr", sa.String(length=128)))
    op.add_column(
        "webhook_events",
        sa.Column("payload_sha256", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column("webhook_events", sa.Column("signature_valid", sa.Boolean()))
    op.add_column("webhook_events", sa.Column("label_studio_project_id", sa.Integer()))
    op.add_column("webhook_events", sa.Column("label_project_id", sa.String(length=64)))
    op.add_column("webhook_events", sa.Column("internal_event_type", sa.String(length=160)))
    op.add_column("webhook_events", sa.Column("processing_error", sa.Text()))
    op.create_index(
        "ix_webhook_events_label_studio_project_id",
        "webhook_events",
        ["label_studio_project_id"],
    )

    op.create_index("ix_dataset_snapshots_dataset_id", "dataset_snapshots", ["dataset_id"])
    op.add_column(
        "dataset_snapshots",
        sa.Column("export_format", sa.String(length=64), nullable=False, server_default="JSON"),
    )
    op.add_column("dataset_snapshots", sa.Column("label_schema_version", sa.String(length=32)))
    op.add_column(
        "dataset_snapshots",
        sa.Column(
            "split_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "dataset_snapshots",
        sa.Column(
            "validation_report",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column("dataset_snapshots", sa.Column("validation_status", sa.String(length=32)))
    op.add_column(
        "dataset_snapshots",
        sa.Column("statistics", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column("dataset_snapshots", sa.Column("source_captured_at", sa.DateTime(timezone=True)))
    op.add_column("dataset_snapshots", sa.Column("source_label_studio_project_id", sa.Integer()))
    op.add_column("dataset_snapshots", sa.Column("source_project_title", sa.String(length=200)))
    op.add_column("dataset_snapshots", sa.Column("source_event_key", sa.String(length=128)))
    op.add_column("dataset_snapshots", sa.Column("idempotency_key", sa.String(length=200)))
    op.add_column("dataset_snapshots", sa.Column("export_storage_uri", sa.String(length=512)))
    op.add_column("dataset_snapshots", sa.Column("processing_error", sa.Text()))
    op.execute("UPDATE dataset_snapshots SET idempotency_key = id WHERE idempotency_key IS NULL")
    op.alter_column("dataset_snapshots", "idempotency_key", nullable=False)
    op.create_unique_constraint(
        "uq_dataset_snapshot_idempotency",
        "dataset_snapshots",
        ["dataset_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_dataset_snapshot_idempotency", "dataset_snapshots", type_="unique")
    op.drop_column("dataset_snapshots", "processing_error")
    op.drop_column("dataset_snapshots", "export_storage_uri")
    op.drop_column("dataset_snapshots", "idempotency_key")
    op.drop_column("dataset_snapshots", "source_event_key")
    op.drop_column("dataset_snapshots", "source_project_title")
    op.drop_column("dataset_snapshots", "source_label_studio_project_id")
    op.drop_column("dataset_snapshots", "source_captured_at")
    op.drop_column("dataset_snapshots", "statistics")
    op.drop_column("dataset_snapshots", "validation_status")
    op.drop_column("dataset_snapshots", "validation_report")
    op.drop_column("dataset_snapshots", "split_metadata")
    op.drop_column("dataset_snapshots", "label_schema_version")
    op.drop_column("dataset_snapshots", "export_format")
    op.drop_index("ix_dataset_snapshots_dataset_id", table_name="dataset_snapshots")

    op.drop_index("ix_webhook_events_label_studio_project_id", table_name="webhook_events")
    op.drop_column("webhook_events", "processing_error")
    op.drop_column("webhook_events", "internal_event_type")
    op.drop_column("webhook_events", "label_project_id")
    op.drop_column("webhook_events", "label_studio_project_id")
    op.drop_column("webhook_events", "signature_valid")
    op.drop_column("webhook_events", "payload_sha256")
    op.drop_column("webhook_events", "remote_addr")
    op.drop_column("webhook_events", "headers")

    op.drop_column("label_projects", "last_activity_at")
    op.drop_column("label_projects", "class_distribution")
    op.drop_column("label_projects", "skipped_annotation_count")
    op.drop_column("label_projects", "annotation_count")
    op.drop_column("label_projects", "annotated_task_count")
    op.drop_column("label_projects", "task_count")
    op.drop_column("label_projects", "qualifying_actions")
    op.drop_column("label_projects", "test_ratio")
    op.drop_column("label_projects", "val_ratio")
    op.drop_column("label_projects", "train_ratio")
    op.drop_column("label_projects", "required_classes")
    op.drop_column("label_projects", "minimum_annotation_count")
    op.drop_column("label_projects", "auto_snapshot_enabled")
    op.drop_column("label_projects", "training_pipeline_slug")
    op.drop_column("label_projects", "label_schema_version")
    op.drop_index("ix_label_projects_dataset_id", table_name="label_projects")
    op.drop_constraint("uq_label_project_external_id", "label_projects", type_="unique")
    op.alter_column("label_projects", "dataset_id", existing_type=sa.String(length=64), nullable=False)
