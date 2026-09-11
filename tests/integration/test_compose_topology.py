from pathlib import Path


def test_compose_dev_declares_expected_networks() -> None:
    content = Path("infra/compose/compose.dev.yaml").read_text(encoding="utf-8")
    assert "edge:" in content
    assert "control:" in content
    assert "data:" in content
    assert "observability:" in content


def test_compose_mlflow_aware_workers_export_tracking_uri() -> None:
    dev_content = Path("infra/compose/compose.dev.yaml").read_text(encoding="utf-8")
    prod_content = Path("infra/compose/compose.prod.yaml").read_text(encoding="utf-8")

    assert dev_content.count("MLFLOW_TRACKING_URI: http://mlflow:5000") >= 2
    assert prod_content.count("MLFLOW_TRACKING_URI: http://mlflow:5000") >= 2


def test_compose_control_worker_can_reach_deployment_targets_and_dev_triton_repo_is_shared() -> None:
    dev_content = Path("infra/compose/compose.dev.yaml").read_text(encoding="utf-8")
    prod_content = Path("infra/compose/compose.prod.yaml").read_text(encoding="utf-8")

    assert "../../.local/state/serving/triton/realtime:/models" in dev_content
    assert "../../.local/state/serving/triton/batch:/models" in dev_content
    assert "control-worker:" in dev_content and "\n      - data" in dev_content
    assert "control-worker:" in prod_content and "\n      - data" in prod_content


def test_compose_runs_database_migrations_before_pipeline_services() -> None:
    dev_content = Path("infra/compose/compose.dev.yaml").read_text(encoding="utf-8")
    prod_content = Path("infra/compose/compose.prod.yaml").read_text(encoding="utf-8")

    for content in (dev_content, prod_content):
        assert "migrations:" in content
        assert "alembic -c backend/migrations/alembic.ini upgrade head" in content
        assert "condition: service_completed_successfully" in content


def test_compose_gpu_bound_services_request_gpu_access() -> None:
    dev_content = Path("infra/compose/compose.dev.yaml").read_text(encoding="utf-8")
    prod_content = Path("infra/compose/compose.prod.yaml").read_text(encoding="utf-8")

    assert dev_content.count("gpus: all") >= 3
    assert prod_content.count("gpus: all") >= 3


def test_label_studio_can_serve_platform_local_files() -> None:
    dev_content = Path("infra/compose/compose.dev.yaml").read_text(encoding="utf-8")
    prod_content = Path("infra/compose/compose.prod.yaml").read_text(encoding="utf-8")

    for content in (dev_content, prod_content):
        assert 'LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED: "true"' in content
        assert "LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT: /srv/cv-platform" in content

    assert "../../.local/state/runtime:/srv/cv-platform/runtime:ro" in dev_content
    assert "../../.local/state/control/uploads:/srv/cv-platform/control/uploads:ro" in dev_content
    assert "/srv/cv-platform/runtime:/srv/cv-platform/runtime:ro" in prod_content
    assert "/srv/cv-platform/control/uploads:/srv/cv-platform/control/uploads:ro" in prod_content
