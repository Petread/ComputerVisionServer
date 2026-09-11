"""Add channel-scoped runtime pipeline instances."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "20260511_0025"
down_revision = "20260511_0024"
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


def _columns(table_name: str) -> set[str]:
    if not _has_table(table_name):
        return set()
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


def _unique_slug(existing: set[str], base: str) -> str:
    normalized = "".join(ch if ch.isalnum() else "-" for ch in base.lower()).strip("-")
    normalized = normalized[:96] or "channel-pipeline"
    candidate = normalized
    suffix = 2
    while candidate in existing:
        tail = f"-{suffix}"
        candidate = f"{normalized[: 120 - len(tail)]}{tail}"
        suffix += 1
    existing.add(candidate)
    return candidate


def upgrade() -> None:
    if not _has_column("pipeline_definitions", "scope"):
        op.add_column(
            "pipeline_definitions",
            sa.Column(
                "scope",
                sa.String(length=32),
                nullable=False,
                server_default="template",
            ),
        )
        op.create_index("ix_pipeline_definitions_scope", "pipeline_definitions", ["scope"])
    if not _has_column("pipeline_definitions", "owner_channel_id"):
        op.add_column(
            "pipeline_definitions",
            sa.Column("owner_channel_id", sa.String(length=64), nullable=True),
        )
        op.create_index(
            "ix_pipeline_definitions_owner_channel_id",
            "pipeline_definitions",
            ["owner_channel_id"],
        )
    if not _has_column("pipeline_definitions", "source_template_definition_id"):
        op.add_column(
            "pipeline_definitions",
            sa.Column("source_template_definition_id", sa.String(length=64), nullable=True),
        )
        op.create_index(
            "ix_pipeline_definitions_source_template_definition_id",
            "pipeline_definitions",
            ["source_template_definition_id"],
        )
    if not _has_column("runtime_channels", "authoring_label_project_ids"):
        op.add_column(
            "runtime_channels",
            sa.Column(
                "authoring_label_project_ids",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )

    _backfill_channel_pipeline_instances()


def downgrade() -> None:
    if _has_column("runtime_channels", "authoring_label_project_ids"):
        op.drop_column("runtime_channels", "authoring_label_project_ids")
    if _has_column("pipeline_definitions", "source_template_definition_id"):
        op.drop_index(
            "ix_pipeline_definitions_source_template_definition_id",
            table_name="pipeline_definitions",
        )
        op.drop_column("pipeline_definitions", "source_template_definition_id")
    if _has_column("pipeline_definitions", "owner_channel_id"):
        op.drop_index("ix_pipeline_definitions_owner_channel_id", table_name="pipeline_definitions")
        op.drop_column("pipeline_definitions", "owner_channel_id")
    if _has_column("pipeline_definitions", "scope"):
        op.drop_index("ix_pipeline_definitions_scope", table_name="pipeline_definitions")
        op.drop_column("pipeline_definitions", "scope")


def _backfill_channel_pipeline_instances() -> None:
    required = {
        "runtime_channels",
        "pipeline_definitions",
        "pipeline_versions",
    }
    if not all(_has_table(table) for table in required):
        return
    bind = op.get_bind()
    metadata = sa.MetaData()
    runtime_channels = sa.Table("runtime_channels", metadata, autoload_with=bind)
    pipeline_definitions = sa.Table("pipeline_definitions", metadata, autoload_with=bind)
    pipeline_versions = sa.Table("pipeline_versions", metadata, autoload_with=bind)
    pipeline_nodes = (
        sa.Table("pipeline_nodes", metadata, autoload_with=bind)
        if _has_table("pipeline_nodes")
        else None
    )
    trigger_endpoints = (
        sa.Table("trigger_endpoints", metadata, autoload_with=bind)
        if _has_table("trigger_endpoints")
        else None
    )

    definition_cols = _columns("pipeline_definitions")
    version_cols = _columns("pipeline_versions")
    channel_cols = _columns("runtime_channels")
    node_cols = _columns("pipeline_nodes")
    endpoint_cols = _columns("trigger_endpoints")
    now = datetime.now(timezone.utc)

    existing_slugs = {
        row.slug for row in bind.execute(sa.select(pipeline_definitions.c.slug)).all()
    }
    channels = bind.execute(
        sa.select(runtime_channels).where(
            runtime_channels.c.pipeline_version_id.is_not(None),
            runtime_channels.c.deleted_at.is_(None),
        )
    ).all()
    for channel_row in channels:
        channel = dict(channel_row._mapping)
        current_version = bind.execute(
            sa.select(pipeline_versions).where(
                pipeline_versions.c.id == channel["pipeline_version_id"]
            )
        ).first()
        if current_version is None:
            continue
        version = dict(current_version._mapping)
        current_definition = bind.execute(
            sa.select(pipeline_definitions).where(
                pipeline_definitions.c.id == version["pipeline_definition_id"]
            )
        ).first()
        if current_definition is None:
            continue
        definition = dict(current_definition._mapping)
        if definition.get("scope") == "channel_instance" and definition.get("owner_channel_id") == channel["id"]:
            continue

        existing_instance = bind.execute(
            sa.select(pipeline_definitions).where(
                pipeline_definitions.c.scope == "channel_instance",
                pipeline_definitions.c.owner_channel_id == channel["id"],
            )
        ).first()
        if existing_instance is not None:
            instance = dict(existing_instance._mapping)
            target_version_id = instance.get("published_version_id") or instance.get("latest_version_id")
            if target_version_id:
                bind.execute(
                    runtime_channels.update()
                    .where(runtime_channels.c.id == channel["id"])
                    .values(
                        pipeline_version_id=target_version_id,
                        assigned_pipeline=instance["name"],
                    )
                )
            continue

        definition_id = _new_id("pln")
        version_id = _new_id("plv")
        definition_values = {
            "id": definition_id,
            "slug": _unique_slug(existing_slugs, f"{channel['slug']}-{definition['slug']}"),
            "name": f"{channel['name']} Pipeline",
            "description": definition.get("description"),
            "status": definition.get("status") or "draft",
            "latest_version_id": version_id,
            "published_version_id": version_id if version.get("status") == "published" else None,
            "template": False,
            "scope": "channel_instance",
            "owner_channel_id": channel["id"],
            "source_template_definition_id": definition["id"],
            "created_by": definition.get("created_by"),
            "created_at": now,
            "updated_at": now,
        }
        bind.execute(
            pipeline_definitions.insert().values(
                {key: value for key, value in definition_values.items() if key in definition_cols}
            )
        )

        version_values = {
            "id": version_id,
            "pipeline_definition_id": definition_id,
            "version_no": 1,
            "status": version.get("status") or "draft",
            "mode": version.get("mode") or "realtime",
            "graph": version.get("graph") or {"nodes": [], "edges": []},
            "validation_report": version.get("validation_report") or {},
            "model_capability_constraints": version.get("model_capability_constraints") or {},
            "node_contract_versions": version.get("node_contract_versions") or {},
            "config_digest": version.get("config_digest") or "backfilled",
            "change_note": "Backfilled channel-scoped pipeline instance",
            "created_by": version.get("created_by"),
            "published_at": version.get("published_at"),
            "published_by": version.get("published_by"),
            "created_at": now,
            "updated_at": now,
        }
        bind.execute(
            pipeline_versions.insert().values(
                {key: value for key, value in version_values.items() if key in version_cols}
            )
        )

        if pipeline_nodes is not None:
            node_rows = bind.execute(
                sa.select(pipeline_nodes).where(
                    pipeline_nodes.c.pipeline_version_id == version["id"]
                )
            ).all()
            for node_row in node_rows:
                node = dict(node_row._mapping)
                node["id"] = _new_id("pnd")
                node["pipeline_version_id"] = version_id
                node["created_at"] = now
                node["updated_at"] = now
                bind.execute(
                    pipeline_nodes.insert().values(
                        {key: value for key, value in node.items() if key in node_cols}
                    )
                )

        channel_values = {
            "pipeline_version_id": version_id,
            "assigned_pipeline": definition_values["name"],
            "updated_at": now,
        }
        bind.execute(
            runtime_channels.update()
            .where(runtime_channels.c.id == channel["id"])
            .values({key: value for key, value in channel_values.items() if key in channel_cols})
        )

        if trigger_endpoints is not None:
            updates = {
                "pipeline_definition_id": definition_id,
                "pipeline_version_id": version_id,
                "updated_at": now,
            }
            bind.execute(
                trigger_endpoints.update()
                .where(
                    trigger_endpoints.c.channel_id == channel["id"],
                    trigger_endpoints.c.pipeline_version_id == version["id"],
                )
                .values({key: value for key, value in updates.items() if key in endpoint_cols})
            )
