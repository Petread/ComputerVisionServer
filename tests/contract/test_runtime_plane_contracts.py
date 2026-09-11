from pathlib import Path

import yaml

REQUIRED_RUNTIME_SERVICES = {
    "runtime-admin-api",
    "channel-service",
    "pipeline-definition-service",
    "runtime-orchestrator",
    "realtime-executor",
    "trigger-executor",
    "overlay-service",
    "capture-service",
    "result-service",
    "connector-service",
}


def test_runtime_architecture_extension_documents_required_boundaries() -> None:
    content = Path("docs/runtime/runtime-plane-architecture-extension.md").read_text(
        encoding="utf-8"
    )

    for service_name in REQUIRED_RUNTIME_SERVICES:
        assert f"`{service_name}`" in content
    assert "no queue before inference" in content
    assert "logical refs" in content
    assert "Label Studio" in content


def test_runtime_openapi_contracts_are_exported() -> None:
    for service_name in REQUIRED_RUNTIME_SERVICES:
        path = Path("contracts/openapi") / f"{service_name}.v1.yaml"
        assert path.exists(), f"missing OpenAPI export for {service_name}"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert document["info"]["title"].startswith("CV Platform")


def test_runtime_services_are_declared_in_compose_profiles() -> None:
    dev_content = Path("infra/compose/compose.dev.yaml").read_text(encoding="utf-8")
    prod_content = Path("infra/compose/compose.prod.yaml").read_text(encoding="utf-8")

    for service_name in REQUIRED_RUNTIME_SERVICES:
        assert f"  {service_name}:" in dev_content
        assert f"  {service_name}:" in prod_content
    assert "/runtime-api" in Path("infra/reverse-proxy/Caddyfile.prod").read_text(
        encoding="utf-8"
    )
    assert "/runtime-trigger" in Path("infra/reverse-proxy/Caddyfile.prod").read_text(
        encoding="utf-8"
    )
