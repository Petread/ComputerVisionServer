"""Extend training persistence for resumable orchestration and audit envelopes."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260420_0003"
down_revision = "20260420_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("training_jobs", sa.Column("model_family_slug", sa.String(length=120)))
    op.add_column(
        "training_jobs",
        sa.Column("task_type", sa.String(length=32), nullable=False, server_default="classification"),
    )
    op.add_column("training_jobs", sa.Column("idempotency_key", sa.String(length=200)))
    op.add_column("training_jobs", sa.Column("submission_hash", sa.String(length=128)))
    op.add_column(
        "training_jobs",
        sa.Column(
            "allow_duplicate_snapshot_training",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "training_jobs",
        sa.Column(
            "best_checkpoint_rule",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "training_jobs",
        sa.Column(
            "promotion_criteria",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column(
        "training_jobs",
        sa.Column("progress", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column("training_jobs", sa.Column("best_checkpoint_id", sa.String(length=64)))
    op.add_column("training_jobs", sa.Column("worker_instance_id", sa.String(length=64)))
    op.add_column("training_jobs", sa.Column("worker_claimed_at", sa.DateTime(timezone=True)))
    op.add_column("training_jobs", sa.Column("worker_heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("training_jobs", sa.Column("gpu_device", sa.String(length=64)))
    op.add_column(
        "training_jobs",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("training_jobs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
    op.add_column("training_jobs", sa.Column("cancel_requested_by", sa.String(length=64)))
    op.add_column(
        "training_jobs",
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("training_jobs", sa.Column("artifact_manifest_uri", sa.String(length=512)))
    op.add_column(
        "training_jobs",
        sa.Column(
            "evaluation_summary",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "training_jobs",
        sa.Column(
            "metrics_summary",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column("training_jobs", sa.Column("model_version_id", sa.String(length=64)))
    op.add_column("training_jobs", sa.Column("failure_class", sa.String(length=64)))
    op.add_column("training_jobs", sa.Column("last_error", sa.Text()))
    op.add_column(
        "training_jobs",
        sa.Column(
            "runtime_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.create_index(
        "ix_training_jobs_dataset_snapshot_id",
        "training_jobs",
        ["dataset_snapshot_id"],
    )
    op.execute(
        """
        UPDATE training_jobs
        SET
          model_family_slug = COALESCE(model_family_id, 'unknown-family'),
          task_type = COALESCE(config_snapshot->>'task_type', 'classification'),
          status = CASE status
            WHEN 'accepted' THEN 'queued'
            WHEN 'running' THEN 'training'
            WHEN 'paused' THEN 'training'
            WHEN 'paused_slo' THEN 'training'
            WHEN 'succeeded' THEN 'completed'
            WHEN 'canceled' THEN 'cancelled'
            ELSE status
          END
        """
    )
    op.alter_column("training_jobs", "model_family_slug", nullable=False)

    op.add_column(
        "training_checkpoints",
        sa.Column("checkpoint_kind", sa.String(length=32), nullable=False, server_default="interval"),
    )
    op.add_column(
        "training_checkpoints",
        sa.Column("is_best", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "training_checkpoints",
        sa.Column("metrics", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )

    op.create_table(
        "training_metrics",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("training_job_id", sa.String(length=64), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64)),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("metric_name", sa.String(length=120), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=False),
        sa.Column("epoch_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("step_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_training_metrics_training_job_id", "training_metrics", ["training_job_id"])

    op.add_column(
        "audit_events",
        sa.Column("source_service", sa.String(length=64), nullable=False, server_default="legacy"),
    )
    op.add_column(
        "audit_events",
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("UPDATE audit_events SET occurred_at = created_at WHERE occurred_at IS NULL")
    op.alter_column("audit_events", "source_service", server_default=None)
    op.alter_column("audit_events", "occurred_at", server_default=None)


def downgrade() -> None:
    op.drop_column("audit_events", "occurred_at")
    op.drop_column("audit_events", "source_service")

    op.drop_index("ix_training_metrics_training_job_id", table_name="training_metrics")
    op.drop_table("training_metrics")

    op.drop_column("training_checkpoints", "metrics")
    op.drop_column("training_checkpoints", "is_best")
    op.drop_column("training_checkpoints", "checkpoint_kind")

    op.drop_index("ix_training_jobs_dataset_snapshot_id", table_name="training_jobs")
    op.drop_column("training_jobs", "runtime_metadata")
    op.drop_column("training_jobs", "last_error")
    op.drop_column("training_jobs", "failure_class")
    op.drop_column("training_jobs", "model_version_id")
    op.drop_column("training_jobs", "metrics_summary")
    op.drop_column("training_jobs", "evaluation_summary")
    op.drop_column("training_jobs", "artifact_manifest_uri")
    op.drop_column("training_jobs", "resume_count")
    op.drop_column("training_jobs", "cancel_requested_by")
    op.drop_column("training_jobs", "cancel_requested_at")
    op.drop_column("training_jobs", "cancel_requested")
    op.drop_column("training_jobs", "gpu_device")
    op.drop_column("training_jobs", "worker_heartbeat_at")
    op.drop_column("training_jobs", "worker_claimed_at")
    op.drop_column("training_jobs", "worker_instance_id")
    op.drop_column("training_jobs", "best_checkpoint_id")
    op.drop_column("training_jobs", "progress")
    op.drop_column("training_jobs", "promotion_criteria")
    op.drop_column("training_jobs", "best_checkpoint_rule")
    op.drop_column("training_jobs", "allow_duplicate_snapshot_training")
    op.drop_column("training_jobs", "submission_hash")
    op.drop_column("training_jobs", "idempotency_key")
    op.drop_column("training_jobs", "task_type")
    op.drop_column("training_jobs", "model_family_slug")
