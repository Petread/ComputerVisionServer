"""Add managed user and API key administration metadata."""

from __future__ import annotations

from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "20260421_0008"
down_revision = "20260421_0007"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column("users", "display_name"):
        op.add_column("users", sa.Column("display_name", sa.String(length=160)))
    if not _has_column("users", "description"):
        op.add_column("users", sa.Column("description", sa.Text()))
    if not _has_column("users", "created_by"):
        op.add_column("users", sa.Column("created_by", sa.String(length=128)))
    if not _has_column("users", "updated_at"):
        op.add_column("users", sa.Column("updated_at", sa.DateTime(timezone=True)))
    if not _has_column("users", "last_login_at"):
        op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True)))
    if not _has_column("users", "password_changed_at"):
        op.add_column("users", sa.Column("password_changed_at", sa.DateTime(timezone=True)))
    if not _has_column("users", "session_version"):
        op.add_column("users", sa.Column("session_version", sa.Integer()))

    users_table = sa.table(
        "users",
        sa.column("id", sa.String()),
        sa.column("username", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("display_name", sa.String()),
        sa.column("created_by", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("password_changed_at", sa.DateTime(timezone=True)),
        sa.column("session_version", sa.Integer()),
    )
    for row in bind.execute(
        sa.select(
            users_table.c.id,
            users_table.c.username,
            users_table.c.created_at,
            users_table.c.display_name,
            users_table.c.created_by,
            users_table.c.updated_at,
            users_table.c.password_changed_at,
            users_table.c.session_version,
        )
    ):
        bind.execute(
            sa.update(users_table)
            .where(users_table.c.id == row.id)
            .values(
                display_name=row.display_name or row.username,
                created_by=row.created_by or "bootstrap",
                updated_at=row.updated_at or row.created_at,
                password_changed_at=row.password_changed_at or row.created_at,
                session_version=row.session_version or 1,
            )
        )

    op.alter_column("users", "display_name", existing_type=sa.String(length=160), nullable=False)
    op.alter_column("users", "updated_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.alter_column("users", "session_version", existing_type=sa.Integer(), nullable=False)

    if not _has_column("api_keys", "record_id"):
        op.add_column("api_keys", sa.Column("record_id", sa.String(length=64)))
    if not _has_column("api_keys", "label"):
        op.add_column("api_keys", sa.Column("label", sa.String(length=160)))
    if not _has_column("api_keys", "description"):
        op.add_column("api_keys", sa.Column("description", sa.Text()))
    if not _has_column("api_keys", "intended_usage"):
        op.add_column("api_keys", sa.Column("intended_usage", sa.Text()))
    if not _has_column("api_keys", "secret_prefix"):
        op.add_column("api_keys", sa.Column("secret_prefix", sa.String(length=16)))
    if not _has_column("api_keys", "endpoint_mode_hint"):
        op.add_column("api_keys", sa.Column("endpoint_mode_hint", sa.String(length=32)))
    if not _has_column("api_keys", "source_type"):
        op.add_column("api_keys", sa.Column("source_type", sa.String(length=32)))
    if not _has_column("api_keys", "created_by"):
        op.add_column("api_keys", sa.Column("created_by", sa.String(length=128)))
    if not _has_column("api_keys", "updated_at"):
        op.add_column("api_keys", sa.Column("updated_at", sa.DateTime(timezone=True)))
    if not _has_column("api_keys", "last_used_at"):
        op.add_column("api_keys", sa.Column("last_used_at", sa.DateTime(timezone=True)))
    if not _has_column("api_keys", "revoked_at"):
        op.add_column("api_keys", sa.Column("revoked_at", sa.DateTime(timezone=True)))
    if not _has_column("api_keys", "revoked_by"):
        op.add_column("api_keys", sa.Column("revoked_by", sa.String(length=128)))
    op.alter_column("api_keys", "subject_id", existing_type=sa.String(length=64), type_=sa.String(length=128))
    op.alter_column("api_keys", "scope", existing_type=sa.String(length=128), type_=sa.String(length=512))

    api_keys_table = sa.table(
        "api_keys",
        sa.column("id", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("record_id", sa.String()),
        sa.column("label", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("intended_usage", sa.Text()),
        sa.column("source_type", sa.String()),
        sa.column("created_by", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for row in bind.execute(
        sa.select(
            api_keys_table.c.id,
            api_keys_table.c.created_at,
            api_keys_table.c.record_id,
            api_keys_table.c.label,
            api_keys_table.c.description,
            api_keys_table.c.intended_usage,
            api_keys_table.c.source_type,
            api_keys_table.c.created_by,
            api_keys_table.c.updated_at,
        )
    ):
        bind.execute(
            sa.update(api_keys_table)
            .where(api_keys_table.c.id == row.id)
            .values(
                record_id=row.record_id or f"apk_{uuid4().hex}",
                label=row.label or row.id,
                description=row.description
                or "Bootstrap/config-managed key. Modify through configuration and mounted secret files.",
                intended_usage=row.intended_usage or f"Bootstrap identity for key {row.id}.",
                source_type=row.source_type or "bootstrap",
                created_by=row.created_by or "bootstrap",
                updated_at=row.updated_at or row.created_at,
            )
        )

    op.alter_column("api_keys", "record_id", existing_type=sa.String(length=64), nullable=False)
    op.alter_column("api_keys", "label", existing_type=sa.String(length=160), nullable=False)
    op.alter_column("api_keys", "source_type", existing_type=sa.String(length=32), nullable=False)
    op.alter_column("api_keys", "updated_at", existing_type=sa.DateTime(timezone=True), nullable=False)

    if not _has_index("api_keys", "ix_api_keys_record_id"):
        op.create_index("ix_api_keys_record_id", "api_keys", ["record_id"], unique=True)
    if not _has_index("api_keys", "ix_api_keys_source_type"):
        op.create_index("ix_api_keys_source_type", "api_keys", ["source_type"], unique=False)
    if not _has_index("api_keys", "ix_api_keys_last_used_at"):
        op.create_index("ix_api_keys_last_used_at", "api_keys", ["last_used_at"], unique=False)
    if not _has_index("api_keys", "ix_api_keys_revoked_at"):
        op.create_index("ix_api_keys_revoked_at", "api_keys", ["revoked_at"], unique=False)


def downgrade() -> None:
    if _has_index("api_keys", "ix_api_keys_revoked_at"):
        op.drop_index("ix_api_keys_revoked_at", table_name="api_keys")
    if _has_index("api_keys", "ix_api_keys_last_used_at"):
        op.drop_index("ix_api_keys_last_used_at", table_name="api_keys")
    if _has_index("api_keys", "ix_api_keys_source_type"):
        op.drop_index("ix_api_keys_source_type", table_name="api_keys")
    if _has_index("api_keys", "ix_api_keys_record_id"):
        op.drop_index("ix_api_keys_record_id", table_name="api_keys")

    for column_name in (
        "revoked_by",
        "revoked_at",
        "last_used_at",
        "updated_at",
        "created_by",
        "source_type",
        "endpoint_mode_hint",
        "secret_prefix",
        "intended_usage",
        "description",
        "label",
        "record_id",
    ):
        if _has_column("api_keys", column_name):
            op.drop_column("api_keys", column_name)

    for column_name in (
        "session_version",
        "password_changed_at",
        "last_login_at",
        "updated_at",
        "created_by",
        "description",
        "display_name",
    ):
        if _has_column("users", column_name):
            op.drop_column("users", column_name)
