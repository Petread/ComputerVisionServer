from __future__ import annotations

import pytest
from cv_contracts.runtime import RuntimeLogicalModelRef, RuntimeNodeSpec
from cv_db.models import (
    DeploymentRelease,
    DeploymentTarget,
    LabelProject,
    ModelFamily,
    ModelPromotion,
)
from cv_db.pipeline_store import _validate_inference_node
from cv_db.registry_store import (
    promote_staged_release_to_production,
    rollback_target_to_previous_release,
)
from cv_db.runtime_execution_store import (
    build_runtime_model_resolver,
    list_runtime_model_capability_options,
)
from cv_runtime import BUILTIN_NODE_TYPES
from cv_status import DeploymentReleaseStatus, TargetPath, TaskType
from cv_utils import utc_now
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from tests.support.runtime_model_seed import seed_deployed_runtime_model


def _stage(session: Session, family: str, version: int) -> str:
    release_id = f"rel_{family}_{version}"
    target = session.get(DeploymentTarget, "realtime")
    previous_id = target.active_release_id if target else None
    seed_deployed_runtime_model(
        session,
        release_id=release_id,
        model_family_id=f"mf_{family}",
        model_family_slug=family,
        model_version_id=f"mvr_{family}_{version}",
        version_no=version,
        task_type=TaskType.CLASSIFICATION,
        active_release=False,
        staged=True,
    )
    session.get(DeploymentRelease, release_id).rollback_source_release_id = previous_id
    session.flush()
    return release_id


def _promote(session: Session, release_id: str) -> None:
    promote_staged_release_to_production(session, promotion_id=f"mpr_{release_id}")


def _production_ids(session: Session) -> set[str]:
    options = list_runtime_model_capability_options(
        session, task_type=TaskType.CLASSIFICATION, target_path=TargetPath.REALTIME
    )
    return {
        item.deployment_release_id for item in options if item.deployment_status == "production"
    }


def _resolve(
    session: Session, family: str, pinned: str | None = None, project: str | None = None
) -> str:
    return build_runtime_model_resolver(session)(
        RuntimeLogicalModelRef(
            model_family_slug=family,
            label_project_id=project,
            target_path=TargetPath.REALTIME,
            contract_major=1,
            required_task_type=TaskType.CLASSIFICATION,
            selection_policy="explicit_release" if pinned else "active_production",
            deployment_release_id=pinned,
        )
    ).deployment_release_id


def test_production_follows_each_family_across_promotions_and_rollback(
    sqlite_engine: Engine,
) -> None:
    with Session(sqlite_engine) as session:
        first = _stage(session, "first-classifier", 1)
        second = _stage(session, "second-classifier", 1)
        _promote(session, first)
        _promote(session, second)
        assert _production_ids(session) == {first, second}
        assert _resolve(session, "first-classifier") == first
        assert _resolve(session, "second-classifier") == second

        updated = _stage(session, "first-classifier", 2)
        assert _production_ids(session) == {first, second}
        _promote(session, updated)
        assert _production_ids(session) == {updated, second}
        assert _resolve(session, "first-classifier") == updated
        assert _resolve(session, "first-classifier", first) == first
        assert session.get(ModelPromotion, f"mpr_{first}").status == "staged"
        assert session.get(ModelPromotion, f"mpr_{second}").status == "production"

        target = session.get(DeploymentTarget, "realtime")
        target.previous_release_id = first
        rollback_target_to_previous_release(session, target_path="realtime")
        assert _production_ids(session) == {first, second}
        assert _resolve(session, "first-classifier") == first


def test_legacy_production_history_survives_deleted_successor(sqlite_engine: Engine) -> None:
    with Session(sqlite_engine) as session:
        first = _stage(session, "first-classifier", 1)
        _promote(session, first)
        second = _stage(session, "second-classifier", 1)
        _promote(session, second)
        # Before the fix, promoting another family demoted the first one.
        session.get(ModelPromotion, f"mpr_{first}").status = "staged"
        third = _stage(session, "third-classifier", 1)
        _promote(session, third)
        session.get(ModelPromotion, f"mpr_{second}").status = "staged"
        session.get(DeploymentRelease, second).deleted_at = utc_now()
        session.flush()
        assert _production_ids(session) == {first, third}
        assert _resolve(session, "first-classifier") == first


@pytest.mark.parametrize("status", ["retired", "rolled_back", "activation_failed"])
def test_unavailable_former_production_is_excluded(sqlite_engine: Engine, status: str) -> None:
    with Session(sqlite_engine) as session:
        first = _stage(session, "first-classifier", 1)
        _promote(session, first)
        second = _stage(session, "second-classifier", 1)
        _promote(session, second)
        session.get(DeploymentRelease, first).status = DeploymentReleaseStatus(status).value
        session.flush()
        assert _production_ids(session) == {second}
        with pytest.raises(ValueError):
            _resolve(session, "first-classifier")


def test_staged_rollback_candidate_is_not_production(sqlite_engine: Engine) -> None:
    with Session(sqlite_engine) as session:
        staged = _stage(session, "test-classifier", 1)
        session.get(ModelPromotion, f"mpr_{staged}").rollback_candidate = True
        active = _stage(session, "production-classifier", 1)
        _promote(session, active)
        assert _production_ids(session) == {active}
        with pytest.raises(ValueError):
            _resolve(session, "test-classifier")
        assert _resolve(session, "test-classifier", staged) == staged


def test_project_binding_follows_model_changes_without_affecting_other_projects(
    sqlite_engine: Engine,
) -> None:
    with Session(sqlite_engine) as session:
        for index, project in enumerate(["project-a", "project-b"], start=1):
            session.add(LabelProject(
                id=project,
                label_studio_project_id=index,
                title=project.upper(),
                task_type="classification",
            ))
        first = _stage(session, "first-classifier", 1)
        second = _stage(session, "second-classifier", 1)
        replacement = _stage(session, "replacement-classifier", 1)
        session.get(ModelFamily, "mf_first-classifier").label_project_id = "project-a"
        session.get(ModelFamily, "mf_replacement-classifier").label_project_id = "project-a"
        session.get(ModelFamily, "mf_second-classifier").label_project_id = "project-b"
        _promote(session, first)
        _promote(session, second)
        assert _resolve(session, "first-classifier", project="project-a") == first
        _promote(session, replacement)
        assert _resolve(session, "first-classifier", project="project-a") == replacement
        assert _resolve(session, "second-classifier", project="project-b") == second
        # Existing family bindings and explicit pins keep their original meaning.
        assert _resolve(session, "first-classifier") == first
        assert _resolve(session, "first-classifier", first) == first

        options = list_runtime_model_capability_options(session)
        project_active = {
            item.capability["label_project_id"]: item.deployment_release_id
            for item in options if item.capability["project_active_production"]
        }
        assert project_active == {"project-a": replacement, "project-b": second}
        assert next(item for item in options if item.deployment_release_id == second).capability[
            "label_project_title"
        ] == "PROJECT-B"
        with pytest.raises(ValueError):
            _resolve(session, "first-classifier", project="missing-project")
        # Publishing must resolve the same replacement even when the original
        # model release is no longer available.
        session.get(DeploymentRelease, first).status = "retired"
        session.flush()
        spec = next(
            item for item in BUILTIN_NODE_TYPES if item.node_type == "inference.classification"
        )
        node = RuntimeNodeSpec(
            node_key="classify",
            node_type=spec.node_type,
            node_category=spec.node_category,
            logical_model_ref=RuntimeLogicalModelRef(
                model_family_slug="first-classifier",
                label_project_id="project-a",
                required_task_type=TaskType.CLASSIFICATION,
            ),
        )
        assert _validate_inference_node(session, node, spec) == []
