from pathlib import Path


def test_asyncapi_contract_exists() -> None:
    path = Path("contracts/asyncapi/platform-events.v1.yaml")
    assert path.exists()
    assert "training.job.state-changed" in path.read_text(encoding="utf-8")

