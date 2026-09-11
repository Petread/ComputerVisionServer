"""Add append-only stem-orientation review revisions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260803_0035"
down_revision = "20260724_0034"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("stem_orientation_reviews"):
        return
    op.create_table(
        "stem_orientation_reviews",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("result_record_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "review_schema_version",
            sa.String(length=80),
            nullable=False,
            server_default="stem.orientation.review.v1",
        ),
        sa.Column("proposal_plausibility", sa.String(length=16), nullable=False),
        sa.Column("direction_assessment", sa.String(length=16), nullable=False),
        sa.Column("angle_assessment", sa.String(length=24), nullable=False),
        sa.Column("bow_relevance", sa.String(length=24), nullable=False),
        sa.Column("final_position", sa.String(length=16), nullable=False),
        sa.Column("target_frame_start", sa.Integer(), nullable=True),
        sa.Column("target_frame_end", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("special_case", sa.Text(), nullable=False, server_default=""),
        sa.Column("supersedes_review_id", sa.String(length=64), nullable=True),
        sa.Column("reviewer_user_id", sa.String(length=64), nullable=False),
        sa.Column("reviewer_username", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "result_record_id",
            "revision",
            name="uq_stem_orientation_review_result_revision",
        ),
    )
    op.create_index(
        "ix_stem_orientation_reviews_result_record_id",
        "stem_orientation_reviews",
        ["result_record_id"],
    )
    op.create_index(
        "ix_stem_orientation_reviews_reviewer_user_id",
        "stem_orientation_reviews",
        ["reviewer_user_id"],
    )
    op.create_index(
        "ix_stem_orientation_reviews_supersedes_review_id",
        "stem_orientation_reviews",
        ["supersedes_review_id"],
    )
    op.create_index(
        "ix_stem_orientation_reviews_result_created",
        "stem_orientation_reviews",
        ["result_record_id", "created_at"],
    )


def downgrade() -> None:
    # Review revisions are operator-entered durable data. Keeping the table on
    # application rollback lets the previous release ignore it without data loss;
    # the idempotent upgrade reuses it on a later roll-forward.
    pass
