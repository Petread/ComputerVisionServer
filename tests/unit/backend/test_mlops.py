from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from cv_core.mlops import MlflowTrackingAdapter
from cv_core.settings import PlatformSettings


def _install_fake_mlflow(monkeypatch) -> ModuleType:  # noqa: ANN001
    fake_mlflow = ModuleType("mlflow")
    fake_mlflow.entities = SimpleNamespace(
        Metric=object,
        Param=object,
        RunTag=object,
    )
    fake_mlflow.set_tracking_uri = lambda _tracking_uri: None
    fake_mlflow.MlflowClient = lambda tracking_uri: object()
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)
    return fake_mlflow


def test_mlflow_adapter_sets_global_tracking_uri_before_creating_client(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    mlflow = _install_fake_mlflow(monkeypatch)

    class DummyClient:
        def __init__(self, *, tracking_uri: str) -> None:
            calls.append(("client", tracking_uri))

    monkeypatch.setattr(
        mlflow,
        "set_tracking_uri",
        lambda tracking_uri: calls.append(("set_tracking_uri", tracking_uri)),
    )
    monkeypatch.setattr(mlflow, "MlflowClient", DummyClient)

    settings = PlatformSettings.model_validate(
        {
            "service": {"name": "test-service"},
            "mlflow": {"tracking_uri": "http://mlflow:5000"},
        }
    )

    client, entities = MlflowTrackingAdapter(settings)._client()

    assert isinstance(client, DummyClient)
    assert entities is mlflow.entities
    assert calls == [
        ("set_tracking_uri", "http://mlflow:5000"),
        ("client", "http://mlflow:5000"),
    ]


def test_mlflow_adapter_logs_metrics_with_training_step(monkeypatch) -> None:
    calls: list[tuple[str, list[object], list[object], list[object]]] = []
    mlflow = _install_fake_mlflow(monkeypatch)

    class DummyMetric:
        def __init__(self, *, key: str, value: float, timestamp: int, step: int) -> None:
            self.key = key
            self.value = value
            self.timestamp = timestamp
            self.step = step

    class DummyClient:
        def log_batch(self, *, run_id: str, metrics: list[object], params: list[object], tags: list[object]) -> None:
            calls.append((run_id, metrics, params, tags))

    mlflow.entities = SimpleNamespace(Metric=DummyMetric, Param=object, RunTag=object)
    monkeypatch.setattr(mlflow, "set_tracking_uri", lambda _tracking_uri: None)
    monkeypatch.setattr(mlflow, "MlflowClient", lambda tracking_uri: DummyClient())

    settings = PlatformSettings.model_validate(
        {
            "service": {"name": "test-service"},
            "mlflow": {"tracking_uri": "http://mlflow:5000"},
        }
    )

    MlflowTrackingAdapter(settings).log_metrics(
        run_id="run-1",
        metrics={"training/loss": 0.42, "validation/map_50": 0.8},
        step=17,
    )

    assert len(calls) == 1
    run_id, metrics, params, tags = calls[0]
    assert run_id == "run-1"
    assert params == []
    assert tags == []
    assert [(metric.key, metric.value, metric.step) for metric in metrics] == [
        ("training/loss", 0.42, 17),
        ("validation/map_50", 0.8, 17),
    ]


def test_mlflow_adapter_syncs_registered_model_metadata_with_string_version(monkeypatch) -> None:
    calls: list[tuple[str, str, str | None, str | None]] = []
    mlflow = _install_fake_mlflow(monkeypatch)

    class DummyClient:
        def set_model_version_tag(self, name: str, version: str, key: str, value: str) -> None:
            calls.append(("tag", name, version, f"{key}={value}"))

        def set_registered_model_alias(self, name: str, alias: str, version: str) -> None:
            calls.append(("alias", name, version, alias))

    monkeypatch.setattr(mlflow, "set_tracking_uri", lambda _tracking_uri: None)
    monkeypatch.setattr(mlflow, "MlflowClient", lambda tracking_uri: DummyClient())

    settings = PlatformSettings.model_validate(
        {
            "service": {"name": "test-service"},
            "mlflow": {"tracking_uri": "http://mlflow:5000"},
        }
    )

    MlflowTrackingAdapter(settings).sync_registered_model_metadata(
        registered_model_name="cars-inspection-detector",
        model_version="7",
        aliases=["candidate"],
        tags={"cv_platform.model_version_id": "mvr_123"},
    )

    assert calls == [
        ("tag", "cars-inspection-detector", "7", "cv_platform.model_version_id=mvr_123"),
        ("alias", "cars-inspection-detector", "7", "candidate"),
    ]


def test_mlflow_adapter_skips_reserved_latest_alias(monkeypatch) -> None:
    calls: list[tuple[str, str, str | None, str | None]] = []
    mlflow = _install_fake_mlflow(monkeypatch)

    class DummyModelVersion:
        version = "8"

    class DummyClient:
        def create_registered_model(self, _name: str) -> None:
            calls.append(("registered_model", _name, None, None))

        def create_model_version(self, *, name: str, source: str, run_id: str, tags: dict[str, str]):
            calls.append(("model_version", name, run_id, source))
            return DummyModelVersion()

        def set_model_version_tag(self, name: str, version: str, key: str, value: str) -> None:
            calls.append(("tag", name, version, f"{key}={value}"))

        def set_registered_model_alias(self, name: str, alias: str, version: str) -> None:
            calls.append(("alias", name, version, alias))

    monkeypatch.setattr(mlflow, "set_tracking_uri", lambda _tracking_uri: None)
    monkeypatch.setattr(mlflow, "MlflowClient", lambda tracking_uri: DummyClient())

    settings = PlatformSettings.model_validate(
        {
            "service": {"name": "test-service"},
            "mlflow": {"tracking_uri": "http://mlflow:5000"},
        }
    )

    registration = MlflowTrackingAdapter(settings).register_model_version(
        registered_model_name="cars-inspection-detector",
        source_uri="runs:/run-1/registration-bundle",
        run_id="run-1",
        aliases=["latest", "LATEST", "latest-trained", "trained", "trained"],
        tags={"cv_platform.lifecycle": "trained"},
    )

    assert registration.model_version == "8"
    assert ("alias", "cars-inspection-detector", "8", "latest") not in calls
    assert ("alias", "cars-inspection-detector", "8", "LATEST") not in calls
    assert calls[-2:] == [
        ("alias", "cars-inspection-detector", "8", "latest-trained"),
        ("alias", "cars-inspection-detector", "8", "trained"),
    ]
