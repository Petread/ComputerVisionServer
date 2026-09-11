from __future__ import annotations

from importlib import util as importlib_util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_stem_orientation_review_migration_is_idempotent_and_rollback_preserves_reviews(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'stem-orientation-review.db'}")
    path = Path(
        "backend/migrations/versions/20260803_0035_stem_orientation_reviews.py"
    )
    spec = importlib_util.spec_from_file_location("stem_orientation_review_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        module.op = operations
        module.upgrade()
        module.upgrade()

        connection.execute(
            sa.text(
                """
                INSERT INTO stem_orientation_reviews (
                    id,
                    result_record_id,
                    revision,
                    review_schema_version,
                    proposal_plausibility,
                    direction_assessment,
                    angle_assessment,
                    bow_relevance,
                    final_position,
                    comment,
                    special_case,
                    reviewer_user_id,
                    reviewer_username
                ) VALUES (
                    'sor_legacy',
                    'rrc_orientation',
                    1,
                    'stem.orientation.review.v1',
                    'yes',
                    'correct',
                    'matching',
                    'relevant',
                    'good',
                    '',
                    '',
                    'usr_operator',
                    'operator'
                )
                """
            )
        )

        module.downgrade()

        inspector = sa.inspect(connection)
        tables = set(inspector.get_table_names())
        indexes = {
            item["name"] for item in inspector.get_indexes("stem_orientation_reviews")
        }
        retained = connection.execute(
            sa.text(
                "SELECT review_schema_version FROM stem_orientation_reviews "
                "WHERE id = 'sor_legacy'"
            )
        ).scalar_one()

    assert "stem_orientation_reviews" in tables
    assert "ix_stem_orientation_reviews_result_created" in indexes
    assert retained == "stem.orientation.review.v1"
