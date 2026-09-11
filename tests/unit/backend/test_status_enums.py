from cv_status import DeploymentPackageStatus, ModelLifecycleStatus, QueueName, ServiceName, TargetPath


def test_queue_names_match_isolation_contract() -> None:
    assert {queue.value for queue in QueueName} == {
        "webhook",
        "dataset",
        "training",
        "deploy",
        "batch",
        "runtime-control",
        "runtime-capture",
        "runtime-connector",
        "maintenance",
    }


def test_target_paths_are_split() -> None:
    assert {target.value for target in TargetPath} == {"realtime", "batch"}


def test_required_service_names_exist() -> None:
    assert ServiceName.ADMIN_API.value == "admin-api"
    assert ServiceName.TRAINING_WORKER.value == "training-worker"


def test_model_lifecycle_states_cover_registry_workflow() -> None:
    assert {status.value for status in ModelLifecycleStatus} == {
        "draft",
        "trained",
        "validated",
        "staged",
        "production",
        "archived",
        "rejected",
    }


def test_deployment_package_states_cover_browser_build_flow() -> None:
    assert {status.value for status in DeploymentPackageStatus} == {
        "missing",
        "building",
        "ready",
        "failed",
    }
