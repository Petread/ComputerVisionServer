"""Add revisioned stem-rotation priority control to runtime channels.

Revision ID: 20260820_0036
Revises: 20260803_0035
Create Date: 2026-08-20
"""

import sqlalchemy as sa
from alembic import op

revision = "20260820_0036"
down_revision = "20260803_0035"
branch_labels = None
depends_on = None


_DEFAULT_CONTROL = (
    '{"schema_version":"runtime.stem-rotation-priority-control.v1",'
    '"priority_source":"FRONTEND","priority_mode":"NONE",'
    '"optimization_enable":true,"revision":0,'
    '"updated_at":null,"updated_by":null}'
)


def upgrade() -> None:
    op.add_column(
        "runtime_channels",
        sa.Column(
            "stem_rotation_priority_control",
            sa.JSON(),
            nullable=False,
            # Pass the JSON document as a literal default. A TextClause would
            # interpret JSON tokens such as ``:true`` and ``:null`` as bind
            # parameters and render them as SQL NULL in migration DDL.
            server_default=_DEFAULT_CONTROL,
        ),
    )


def downgrade() -> None:
    op.drop_column("runtime_channels", "stem_rotation_priority_control")
