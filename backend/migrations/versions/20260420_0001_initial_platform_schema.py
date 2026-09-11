"""Initial platform schema."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260420_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("username", sa.String(length=128), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("hashed_secret", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "datasets",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False, unique=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("label_schema_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("dataset_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expected_file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("received_file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_manifest_sha256", sa.String(length=128)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_upload_sessions_dataset_id", "upload_sessions", ["dataset_id"])
    op.create_table(
        "dataset_assets",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("dataset_id", sa.String(length=64), nullable=False),
        sa.Column("upload_session_id", sa.String(length=64)),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mime_type", sa.String(length=128)),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dataset_assets_dataset_id", "dataset_assets", ["dataset_id"])
    op.create_table(
        "label_projects",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("dataset_id", sa.String(length=64), nullable=False),
        sa.Column("label_studio_project_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("label_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "label_project_webhooks",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("label_project_id", sa.String(length=64), nullable=False),
        sa.Column("secret_ref", sa.String(length=256), nullable=False),
        sa.Column("target_url", sa.String(length=512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_event_key", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("processing_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source", "source_event_key", name="uq_webhook_source_event"),
    )
    op.create_table(
        "dataset_snapshots",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("dataset_id", sa.String(length=64), nullable=False),
        sa.Column("label_project_id", sa.String(length=64)),
        sa.Column("snapshot_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("annotation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manifest_uri", sa.String(length=512)),
        sa.Column("content_sha256", sa.String(length=128)),
        sa.Column("dvc_rev", sa.String(length=128)),
        sa.Column("created_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dataset_id", "snapshot_no", name="uq_dataset_snapshot_no"),
    )
    op.create_table(
        "training_jobs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("model_family_id", sa.String(length=64)),
        sa.Column("dataset_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("queue_name", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=64)),
        sa.Column("recipe_name", sa.String(length=120), nullable=False),
        sa.Column("recipe_version", sa.String(length=32), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("mlflow_run_id", sa.String(length=128)),
        sa.Column("latest_checkpoint_id", sa.String(length=64)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "training_checkpoints",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("training_job_id", sa.String(length=64), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("epoch_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metric_primary", sa.Float()),
        sa.Column("storage_uri", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=128), nullable=False),
        sa.Column("resume_safe", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_training_checkpoints_training_job_id", "training_checkpoints", ["training_job_id"])
    op.create_table(
        "model_families",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("slug", sa.String(length=120), nullable=False, unique=True),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("contract_major", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("input_schema_ref", sa.String(length=256), nullable=False),
        sa.Column("output_schema_ref", sa.String(length=256), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "model_versions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("model_family_id", sa.String(length=64), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("training_job_id", sa.String(length=64)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metrics_summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("artifact_manifest_uri", sa.String(length=512)),
        sa.Column("onnx_uri", sa.String(length=512)),
        sa.Column("tensorrt_uri", sa.String(length=512)),
        sa.Column("dvc_rev", sa.String(length=128)),
        sa.Column("mlflow_registered_model", sa.String(length=128)),
        sa.Column("mlflow_model_version", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("model_family_id", "version_no", name="uq_model_family_version"),
    )
    op.create_table(
        "model_promotions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("target_path", sa.String(length=32), nullable=False),
        sa.Column("requested_environment", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=64)),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("deployment_release_id", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "deployment_releases",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("target_path", sa.String(length=32), nullable=False),
        sa.Column("model_family_id", sa.String(length=64), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("triton_model_name", sa.String(length=160), nullable=False),
        sa.Column("triton_model_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("artifact_manifest_uri", sa.String(length=512)),
        sa.Column("smoke_report_uri", sa.String(length=512)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "deployment_targets",
        sa.Column("target_path", sa.String(length=32), primary_key=True),
        sa.Column("active_release_id", sa.String(length=64)),
        sa.Column("previous_release_id", sa.String(length=64)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "batch_jobs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("deployment_release_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_manifest_uri", sa.String(length=512), nullable=False),
        sa.Column("output_manifest_uri", sa.String(length=512)),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("submitted_by", sa.String(length=64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "batch_job_chunks",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("batch_job_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_uri", sa.String(length=512), nullable=False),
        sa.Column("output_uri", sa.String(length=512)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("batch_job_id", "chunk_no", name="uq_batch_chunk_no"),
    )
    op.create_index("ix_batch_job_chunks_batch_job_id", "batch_job_chunks", ["batch_job_id"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64)),
        sa.Column("correlation_id", sa.String(length=64)),
        sa.Column("causation_id", sa.String(length=64)),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )
    op.create_table(
        "service_heartbeats",
        sa.Column("service_name", sa.String(length=64), primary_key=True),
        sa.Column("instance_id", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )


def downgrade() -> None:
    op.drop_table("service_heartbeats")
    op.drop_table("idempotency_keys")
    op.drop_table("audit_events")
    op.drop_index("ix_batch_job_chunks_batch_job_id", table_name="batch_job_chunks")
    op.drop_table("batch_job_chunks")
    op.drop_table("batch_jobs")
    op.drop_table("deployment_targets")
    op.drop_table("deployment_releases")
    op.drop_table("model_promotions")
    op.drop_table("model_versions")
    op.drop_table("model_families")
    op.drop_index("ix_training_checkpoints_training_job_id", table_name="training_checkpoints")
    op.drop_table("training_checkpoints")
    op.drop_table("training_jobs")
    op.drop_table("dataset_snapshots")
    op.drop_table("webhook_events")
    op.drop_table("label_project_webhooks")
    op.drop_table("label_projects")
    op.drop_index("ix_dataset_assets_dataset_id", table_name="dataset_assets")
    op.drop_table("dataset_assets")
    op.drop_index("ix_upload_sessions_dataset_id", table_name="upload_sessions")
    op.drop_table("upload_sessions")
    op.drop_table("datasets")
    op.drop_table("api_keys")
    op.drop_table("users")
