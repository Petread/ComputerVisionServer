"""Normalize legacy training image sizes to the model stride."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260724_0034"
down_revision = "20260723_0033"
branch_labels = None
depends_on = None


def _normalized_image_size(value: Any) -> Any:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 32 <= value <= 4_096
        and value % 32
    ):
        return min(max(((value + 16) // 32) * 32, 32), 4_096)
    return value


def _normalized_defaults(payload: Any) -> tuple[Any, bool]:
    if not isinstance(payload, dict):
        return payload, False
    normalized = dict(payload)
    current = normalized.get("image_size")
    replacement = _normalized_image_size(current)
    if replacement == current:
        return payload, False
    normalized["image_size"] = replacement
    return normalized, True


def _normalized_template_defaults(payload: Any) -> tuple[Any, bool]:
    if not isinstance(payload, dict):
        return payload, False
    training, changed = _normalized_defaults(payload.get("training"))
    if not changed:
        return payload, False
    normalized = dict(payload)
    normalized["training"] = training
    return normalized, True


def _normalized_config_snapshot(payload: Any) -> tuple[Any, bool]:
    if not isinstance(payload, dict):
        return payload, False
    normalized = dict(payload)
    changed = False
    current = normalized.get("image_size")
    replacement = _normalized_image_size(current)
    if replacement != current:
        normalized["image_size"] = replacement
        changed = True
    for key in ("resolved_training_defaults", "parameter_overrides"):
        nested, nested_changed = _normalized_defaults(normalized.get(key))
        if nested_changed:
            normalized[key] = nested
            changed = True
    return (normalized, True) if changed else (payload, False)


def _repair_json_column(
    table_name: str,
    column_name: str,
    normalizer,
) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return
    columns = {str(item["name"]) for item in inspector.get_columns(table_name)}
    if column_name not in columns:
        return
    table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    for row in bind.execute(sa.select(table.c.id, table.c[column_name])).mappings():
        normalized, changed = normalizer(row[column_name])
        if changed:
            bind.execute(
                table.update().where(table.c.id == row["id"]).values(
                    {column_name: normalized}
                )
            )


def _repair_training_jobs() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "training_jobs" not in inspector.get_table_names():
        return
    columns = {str(item["name"]) for item in inspector.get_columns("training_jobs")}
    table = sa.Table("training_jobs", sa.MetaData(), autoload_with=bind)
    selected = [table.c.id]
    for name in ("image_size", "config_snapshot"):
        if name in columns:
            selected.append(table.c[name])
    for row in bind.execute(sa.select(*selected)).mappings():
        values: dict[str, Any] = {}
        if "image_size" in columns:
            replacement = _normalized_image_size(row["image_size"])
            if replacement != row["image_size"]:
                values["image_size"] = replacement
        if "config_snapshot" in columns:
            snapshot, changed = _normalized_config_snapshot(row["config_snapshot"])
            if changed:
                values["config_snapshot"] = snapshot
        if values:
            bind.execute(table.update().where(table.c.id == row["id"]).values(values))


def upgrade() -> None:
    _repair_json_column(
        "model_templates",
        "training_defaults",
        _normalized_template_defaults,
    )
    _repair_json_column(
        "model_families",
        "training_overrides",
        _normalized_defaults,
    )
    _repair_json_column(
        "training_jobs",
        "resolved_training_defaults",
        _normalized_defaults,
    )
    _repair_json_column(
        "model_versions",
        "resolved_training_defaults",
        _normalized_defaults,
    )
    _repair_training_jobs()


def downgrade() -> None:
    # Data normalization is intentionally irreversible.
    pass
