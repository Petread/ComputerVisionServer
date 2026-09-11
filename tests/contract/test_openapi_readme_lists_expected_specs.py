from pathlib import Path


def test_openapi_readme_lists_expected_specs() -> None:
    content = Path("contracts/openapi/README.md").read_text(encoding="utf-8")
    assert "admin-api.v1.yaml" in content
    assert "inference-batch.v1.yaml" in content
    assert "edge-agent.v1.yaml" in content
