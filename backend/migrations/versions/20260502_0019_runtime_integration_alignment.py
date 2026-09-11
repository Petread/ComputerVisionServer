"""Align runtime integration with deployment and auth truth."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260502_0019"
down_revision = "20260502_0018"
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


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def _has_unique_constraint(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return constraint_name in {
        constraint["name"] for constraint in inspector.get_unique_constraints(table_name)
    }


def upgrade() -> None:
    if _has_table("trigger_endpoints") and _has_column("trigger_endpoints", "auth_required"):
        op.execute("UPDATE trigger_endpoints SET auth_required = true WHERE auth_required = false")
        with op.batch_alter_table("trigger_endpoints") as batch_op:
            batch_op.alter_column(
                "auth_required",
                existing_type=sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )

    if not _has_table("runtime_model_capability_cache"):
        return

    # This table is a short-lived projection. Clearing it removes legacy hidden
    # runtime model sources; services repopulate it from deployment releases.
    op.execute("DELETE FROM runtime_model_capability_cache")

    with op.batch_alter_table("runtime_model_capability_cache") as batch_op:
        if _has_unique_constraint(
            "runtime_model_capability_cache",
            "uq_runtime_model_capability_ref",
        ):
            batch_op.drop_constraint("uq_runtime_model_capability_ref", type_="unique")
        if not _has_unique_constraint(
            "runtime_model_capability_cache",
            "uq_runtime_model_capability_release",
        ):
            batch_op.create_unique_constraint(
                "uq_runtime_model_capability_release",
                ["deployment_release_id"],
            )

    if not _has_index("runtime_model_capability_cache", "ix_runtime_model_capability_ref"):
        op.create_index(
            "ix_runtime_model_capability_ref",
            "runtime_model_capability_cache",
            ["model_family_slug", "target_path", "contract_major"],
        )


def downgrade() -> None:
    if _has_table("runtime_model_capability_cache"):
        if _has_index("runtime_model_capability_cache", "ix_runtime_model_capability_ref"):
            op.drop_index(
                "ix_runtime_model_capability_ref",
                table_name="runtime_model_capability_cache",
            )
        with op.batch_alter_table("runtime_model_capability_cache") as batch_op:
            if _has_unique_constraint(
                "runtime_model_capability_cache",
                "uq_runtime_model_capability_release",
            ):
                batch_op.drop_constraint(
                    "uq_runtime_model_capability_release",
                    type_="unique",
                )
            if not _has_unique_constraint(
                "runtime_model_capability_cache",
                "uq_runtime_model_capability_ref",
            ):
                batch_op.create_unique_constraint(
                    "uq_runtime_model_capability_ref",
                    ["model_family_slug", "target_path", "contract_major"],
                )

    if _has_table("trigger_endpoints") and _has_column("trigger_endpoints", "auth_required"):
        with op.batch_alter_table("trigger_endpoints") as batch_op:
            batch_op.alter_column(
                "auth_required",
                existing_type=sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
