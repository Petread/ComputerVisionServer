"""Model trigger execution as pipeline-derived endpoint metadata."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260502_0021"
down_revision = "20260502_0020"
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


def _json_object_default() -> sa.TextClause:
    return sa.text("'{}'::json")


def _json_default_for_bind() -> Any:
    return sa.text("'{}'") if op.get_bind().dialect.name == "sqlite" else _json_object_default()


def upgrade() -> None:
    if not _has_table("trigger_endpoints"):
        return

    columns = (
        sa.Column("description", sa.Text()),
        sa.Column("pipeline_definition_id", sa.String(length=64)),
        sa.Column("trigger_node_key", sa.String(length=64)),
        sa.Column(
            "derived_from_pipeline",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "trigger_type",
            sa.String(length=32),
            nullable=False,
            server_default="http_put",
        ),
        sa.Column(
            "auth_policy",
            sa.JSON(),
            nullable=False,
            server_default=_json_default_for_bind(),
        ),
    )
    for column in columns:
        if not _has_column("trigger_endpoints", column.name):
            op.add_column("trigger_endpoints", column)

    bind = op.get_bind()
    if _has_table("runtime_channels"):
        _migrate_legacy_trigger_channels(bind)


def downgrade() -> None:
    if not _has_table("trigger_endpoints"):
        return
    for column_name in (
        "auth_policy",
        "trigger_type",
        "derived_from_pipeline",
        "trigger_node_key",
        "pipeline_definition_id",
        "description",
    ):
        if _has_column("trigger_endpoints", column_name):
            op.drop_column("trigger_endpoints", column_name)


def _migrate_legacy_trigger_channels(bind) -> None:
    now = datetime.now(tz=UTC)
    existing_ids = {
        row[0]
        for row in bind.execute(sa.text("SELECT id FROM trigger_endpoints")).all()
    }
    existing_slugs = {
        row[0]
        for row in bind.execute(sa.text("SELECT slug FROM trigger_endpoints")).all()
    }

    legacy_channels = bind.execute(
        sa.text(
            """
            SELECT id, slug, name, pipeline_version_id, assigned_pipeline, created_by, updated_by
            FROM runtime_channels
            WHERE mode = 'trigger' AND deleted_at IS NULL
            """
        )
    ).mappings().all()

    for channel in legacy_channels:
        endpoint_count = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM trigger_endpoints WHERE channel_id = :channel_id"
            ),
            {"channel_id": channel["id"]},
        ).scalar_one()
        if endpoint_count:
            bind.execute(
                sa.text(
                    """
                    UPDATE trigger_endpoints
                    SET pipeline_version_id = COALESCE(pipeline_version_id, :pipeline_version_id),
                        trigger_type = COALESCE(trigger_type, 'http_put'),
                        auth_policy = COALESCE(auth_policy, :auth_policy),
                        description = COALESCE(description, :description),
                        updated_at = :updated_at
                    WHERE channel_id = :channel_id
                    """
                ).bindparams(sa.bindparam("auth_policy", type_=sa.JSON())),
                {
                    "channel_id": channel["id"],
                    "pipeline_version_id": channel["pipeline_version_id"],
                    "auth_policy": {
                        "type": "api_key",
                        "required": True,
                        "scope": "runtime.trigger.invoke",
                    },
                    "description": "Migrated from legacy trigger-channel configuration.",
                    "updated_at": now,
                },
            )
            continue

        endpoint_id = _unique_token(
            existing_ids,
            "tep_mig_",
            str(channel["id"]),
            max_length=64,
        )
        endpoint_slug = _unique_slug(
            existing_slugs,
            f"{channel['slug']}-pipeline-trigger",
        )
        enabled = channel["pipeline_version_id"] is not None
        bind.execute(
            sa.text(
                """
                INSERT INTO trigger_endpoints (
                    id,
                    channel_id,
                    pipeline_version_id,
                    display_name,
                    description,
                    slug,
                    trigger_type,
                    derived_from_pipeline,
                    method,
                    enabled,
                    auth_required,
                    auth_scope,
                    auth_policy,
                    timeout_ms,
                    rate_limit_per_minute,
                    burst_limit,
                    response_policy,
                    request_schema,
                    response_schema,
                    status,
                    invocation_count,
                    failure_count,
                    created_by,
                    updated_by,
                    created_at,
                    updated_at
                ) VALUES (
                    :id,
                    :channel_id,
                    :pipeline_version_id,
                    :display_name,
                    :description,
                    :slug,
                    'http_put',
                    :derived_from_pipeline,
                    'PUT',
                    :enabled,
                    :auth_required,
                    'runtime.trigger.invoke',
                    :auth_policy,
                    2500,
                    60,
                    4,
                    :response_policy,
                    :request_schema,
                    :response_schema,
                    :status,
                    0,
                    0,
                    :created_by,
                    :updated_by,
                    :created_at,
                    :updated_at
                )
                """
            ).bindparams(
                sa.bindparam("auth_policy", type_=sa.JSON()),
                sa.bindparam("response_policy", type_=sa.JSON()),
                sa.bindparam("request_schema", type_=sa.JSON()),
                sa.bindparam("response_schema", type_=sa.JSON()),
            ),
            {
                "id": endpoint_id,
                "channel_id": channel["id"],
                "pipeline_version_id": channel["pipeline_version_id"],
                "display_name": f"{channel['name']} pipeline trigger",
                "description": "Migrated from legacy trigger-channel configuration.",
                "slug": endpoint_slug,
                "derived_from_pipeline": False,
                "enabled": enabled,
                "auth_required": True,
                "auth_policy": {
                    "type": "api_key",
                    "required": True,
                    "scope": "runtime.trigger.invoke",
                    "source": "legacy_channel_migration",
                },
                "response_policy": {},
                "request_schema": {},
                "response_schema": {},
                "status": "degraded" if enabled else "disabled",
                "created_by": channel["created_by"] or "migration",
                "updated_by": channel["updated_by"] or "migration",
                "created_at": now,
                "updated_at": now,
            },
        )

    bind.execute(
        sa.text(
            """
            UPDATE runtime_channels
            SET mode = 'realtime',
                assigned_pipeline = NULL,
                pipeline_version_id = NULL,
                active_runtime_execution_id = NULL,
                desired_state = 'stopped',
                observed_state = 'stopped',
                health_state = 'down',
                updated_at = :updated_at
            WHERE mode = 'trigger'
            """
        ),
        {"updated_at": now},
    )


def _unique_token(existing: set[str], prefix: str, seed: str, *, max_length: int) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_]+", "_", seed).strip("_").lower()
    base = f"{prefix}{sanitized}"[:max_length]
    candidate = base
    suffix = 2
    while candidate in existing:
        suffix_text = f"_{suffix}"
        candidate = f"{base[: max_length - len(suffix_text)]}{suffix_text}"
        suffix += 1
    existing.add(candidate)
    return candidate


def _unique_slug(existing: set[str], value: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "pipeline-trigger"
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    existing.add(candidate)
    return candidate
