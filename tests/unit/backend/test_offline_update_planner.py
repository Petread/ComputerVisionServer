from operations_agent.planner import _build_service_plans, classify_change


def test_classification_training_package_targets_training_services():
    change = classify_change("backend/packages/cv-training/src/cv_training/backends/ultralytics_yolo.py")

    assert change.restart_required is True
    assert change.rebuild_required is False
    assert change.hot_apply_supported is True
    assert change.affected_services == ["training-orchestrator", "training-worker"]


def test_dependency_change_blocks_hot_apply():
    change = classify_change("pyproject.toml")
    plans = _build_service_plans([change])

    assert change.rebuild_required is True
    assert change.hot_apply_supported is False
    assert any(plan.action == "manual" for plan in plans)


def test_deleted_runtime_file_blocks_stale_hot_apply():
    change = classify_change("backend/services/admin-api/src/admin_api/api/routes/system.py", "deleted")
    plans = _build_service_plans([change])

    assert change.hot_apply_supported is False
    assert plans == [
        plan for plan in plans if "is not safe for hot file copy" in " ".join(plan.blockers)
    ]
