"""Expand model registry persistence for lifecycle, lineage, approvals, and rollback metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260420_0004"
down_revision = "20260420_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_versions", sa.Column("dataset_snapshot_id", sa.String(length=64)))
    op.add_column("model_versions", sa.Column("validated_at", sa.DateTime(timezone=True)))
    op.add_column("model_versions", sa.Column("validated_by", sa.String(length=64)))
    op.add_column("model_versions", sa.Column("code_revision", sa.String(length=128)))
    op.add_column(
        "model_versions",
        sa.Column("evaluation_summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column(
        "model_versions",
        sa.Column("artifact_manifest", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column("model_versions", sa.Column("export_manifest_uri", sa.String(length=512)))
    op.add_column(
        "model_versions",
        sa.Column("export_manifest", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column(
        "model_versions",
        sa.Column("dvc_references", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column(
        "model_versions",
        sa.Column("reproducibility_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column(
        "model_versions",
        sa.Column("immutable_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column("model_versions", sa.Column("immutable_metadata_sha256", sa.String(length=128)))
    op.add_column(
        "model_versions",
        sa.Column("alias_names", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )
    op.add_column(
        "model_versions",
        sa.Column("registry_tags", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column("model_versions", sa.Column("mlflow_run_id", sa.String(length=128)))
    op.add_column("model_versions", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.add_column("model_versions", sa.Column("archived_by", sa.String(length=64)))
    op.add_column("model_versions", sa.Column("archive_reason", sa.Text()))
    op.add_column("model_versions", sa.Column("rejected_at", sa.DateTime(timezone=True)))
    op.add_column("model_versions", sa.Column("rejected_by", sa.String(length=64)))
    op.add_column("model_versions", sa.Column("rejection_reason", sa.Text()))
    op.create_index("ix_model_versions_dataset_snapshot_id", "model_versions", ["dataset_snapshot_id"])
    op.execute(
        """
        UPDATE model_versions mv
        SET dataset_snapshot_id = tj.dataset_snapshot_id,
            mlflow_run_id = tj.mlflow_run_id
        FROM training_jobs tj
        WHERE mv.training_job_id = tj.id
        """
    )
    op.execute(
        """
        UPDATE model_versions
        SET status = CASE status
            WHEN 'failed' THEN 'rejected'
            ELSE status
        END
        """
    )
    op.alter_column("model_versions", "status", server_default="draft")

    op.add_column(
        "model_promotions",
        sa.Column("rollback_candidate", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("model_promotions", sa.Column("rollback_marked_by", sa.String(length=64)))
    op.add_column("model_promotions", sa.Column("rollback_marked_at", sa.DateTime(timezone=True)))
    op.add_column("model_promotions", sa.Column("notes", sa.Text()))
    op.add_column("model_promotions", sa.Column("retired_at", sa.DateTime(timezone=True)))
    op.execute(
        """
        UPDATE model_promotions
        SET status = CASE status
            WHEN 'requested' THEN 'staging_requested'
            WHEN 'running' THEN 'production_requested'
            WHEN 'succeeded' THEN 'production'
            WHEN 'failed' THEN 'rejected'
            ELSE status
        END
        """
    )
    op.create_unique_constraint(
        "uq_model_promotion_target",
        "model_promotions",
        ["model_version_id", "target_path"],
    )

    op.create_table(
        "deployment_approval_records",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("target_path", sa.String(length=32), nullable=False),
        sa.Column("approval_scope", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decided_by", sa.String(length=64)),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("promotion_id", sa.String(length=64)),
        sa.Column("deployment_release_id", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_deployment_approval_records_model_version_id",
        "deployment_approval_records",
        ["model_version_id"],
    )

    op.add_column("deployment_releases", sa.Column("rollback_source_release_id", sa.String(length=64)))
    op.execute(
        """
        UPDATE deployment_releases
        SET status = CASE status
            WHEN 'staging' THEN 'packaging'
            WHEN 'ready' THEN 'activated'
            WHEN 'active' THEN 'activated'
            WHEN 'failed' THEN 'activation_failed'
            ELSE status
        END
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE deployment_releases
        SET status = CASE status
            WHEN 'packaging' THEN 'staging'
            WHEN 'repository_sync' THEN 'staging'
            WHEN 'loading' THEN 'staging'
            WHEN 'smoke_testing' THEN 'ready'
            WHEN 'activated' THEN 'active'
            WHEN 'activation_failed' THEN 'failed'
            WHEN 'rollback_requested' THEN 'failed'
            ELSE status
        END
        """
    )
    op.drop_column("deployment_releases", "rollback_source_release_id")

    op.drop_index(
        "ix_deployment_approval_records_model_version_id",
        table_name="deployment_approval_records",
    )
    op.drop_table("deployment_approval_records")

    op.drop_constraint("uq_model_promotion_target", "model_promotions", type_="unique")
    op.execute(
        """
        UPDATE model_promotions
        SET status = CASE status
            WHEN 'staging_requested' THEN 'requested'
            WHEN 'production_requested' THEN 'running'
            WHEN 'production' THEN 'succeeded'
            WHEN 'rejected' THEN 'failed'
            WHEN 'retired' THEN 'succeeded'
            ELSE status
        END
        """
    )
    op.drop_column("model_promotions", "retired_at")
    op.drop_column("model_promotions", "notes")
    op.drop_column("model_promotions", "rollback_marked_at")
    op.drop_column("model_promotions", "rollback_marked_by")
    op.drop_column("model_promotions", "rollback_candidate")

    op.alter_column("model_versions", "status", server_default="trained")
    op.drop_index("ix_model_versions_dataset_snapshot_id", table_name="model_versions")
    op.drop_column("model_versions", "rejection_reason")
    op.drop_column("model_versions", "rejected_by")
    op.drop_column("model_versions", "rejected_at")
    op.drop_column("model_versions", "archive_reason")
    op.drop_column("model_versions", "archived_by")
    op.drop_column("model_versions", "archived_at")
    op.drop_column("model_versions", "mlflow_run_id")
    op.drop_column("model_versions", "registry_tags")
    op.drop_column("model_versions", "alias_names")
    op.drop_column("model_versions", "immutable_metadata_sha256")
    op.drop_column("model_versions", "immutable_metadata")
    op.drop_column("model_versions", "reproducibility_metadata")
    op.drop_column("model_versions", "dvc_references")
    op.drop_column("model_versions", "export_manifest")
    op.drop_column("model_versions", "export_manifest_uri")
    op.drop_column("model_versions", "artifact_manifest")
    op.drop_column("model_versions", "evaluation_summary")
    op.drop_column("model_versions", "code_revision")
    op.drop_column("model_versions", "validated_by")
    op.drop_column("model_versions", "validated_at")
    op.drop_column("model_versions", "dataset_snapshot_id")
