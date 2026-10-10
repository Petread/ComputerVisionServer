from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cv_db import RuntimeChannel, RuntimeExecution, session_scope, request_runtime_channel_start
from cv_db.runtime_model_store import (
    demanded_release_ids,
    lock_runtime_use,
    pipeline_release_ids,
    require_runtime_pipeline,
)
from cv_deploy import TritonHttpControlClient, TritonRepositoryManager
from cv_status import RuntimeMode, TargetPath
from cv_utils import PlatformError
from inference_gateway.runtime.admission import require_bound_release, runtime_inference_releases
from tests.unit.backend.test_runtime_lifecycle_core import _seed_channel, _orchestrator


def test_failed_model_start_never_marks_channel_running(sqlite_engine, tmp_path, monkeypatch):
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    def fail(*args, **kwargs):
        raise RuntimeError("Triton model is not ready")

    monkeypatch.setattr("runtime_orchestrator.supervisor.reconcile_runtime_models", fail)
    with session_scope(settings) as session:
        request_runtime_channel_start(session, channel_id, requested_by="tester")
    try:
        command = orchestrator.process_one()
        assert command.status == "failed"
        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel.observed_state == "failed"
            assert demanded_release_ids(session) == set()
    finally:
        supervisor.shutdown()


@pytest.mark.parametrize("state", ["stopped", "starting", "draining", "failed_terminal"])
def test_trigger_admission_requires_started_ready_channel(sqlite_engine, tmp_path, state):
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        execution = RuntimeExecution(
            id="rex_gate",
            channel_id=channel_id,
            pipeline_version_id=channel.pipeline_version_id,
            status=state,
            actual_state=state,
            executor_service="trigger-executor",
            runtime_plan={},
        )
        session.add(execution)
        channel.active_runtime_execution_id = execution.id
        channel.desired_state = "running"
        channel.observed_state = state if state != "failed_terminal" else "failed"
        session.flush()
        with pytest.raises(PlatformError, match="Start the channel"):
            require_runtime_pipeline(
                session, execution_id=execution.id, pipeline_version_id=channel.pipeline_version_id
            )


def test_gateway_allows_only_model_versions_bound_to_pipeline():
    with pytest.raises(PlatformError) as error:
        require_bound_release("rel_deployed")
    assert error.value.code == "runtime_pipeline_required"
    token = runtime_inference_releases.set(frozenset({"rel_bound"}))
    try:
        require_bound_release("rel_bound")
        with pytest.raises(PlatformError):
            require_bound_release("rel_deployed")

        async def in_thread():
            await asyncio.to_thread(require_bound_release, "rel_bound")

        asyncio.run(in_thread())
    finally:
        runtime_inference_releases.reset(token)


def test_additional_trigger_pipeline_does_not_authorize_other_pipeline_models():
    execution = RuntimeExecution(
        pipeline_version_id="plv_live",
        runtime_plan={
            "binding_signature": {"model_bindings": [{"deployment_release_id": "rel_live"}]},
            "trigger_pipelines": [
                {
                    "pipeline_version_id": "plv_trigger",
                    "model_bindings": [{"deployment_release_id": "rel_trigger"}],
                }
            ],
        },
    )
    assert pipeline_release_ids(execution, "plv_live") == {"rel_live"}
    assert pipeline_release_ids(execution, "plv_trigger") == {"rel_trigger"}
    assert pipeline_release_ids(execution, "plv_other") == set()


def test_running_pipeline_resolves_production_reference_to_its_pinned_release(monkeypatch):
    from cv_contracts.runtime import RuntimeLogicalModelRef
    from cv_db.runtime_model_store import build_execution_model_resolver

    model_ref = RuntimeLogicalModelRef(
        model_family_slug="test-detection",
        target_path="realtime",
        required_task_type="detection",
        contract_major=1,
        selection_policy="active_production",
    )
    execution = RuntimeExecution(
        pipeline_version_id="plv_live",
        runtime_plan={
            "binding_signature": {
                "model_bindings": [
                    {
                        "logical_model_ref": model_ref.model_dump(mode="json"),
                        "deployment_release_id": "rel_selected_v3",
                        "model_family_slug": "test-detection",
                    }
                ]
            }
        },
    )
    resolved_refs = []
    monkeypatch.setattr(
        "cv_db.runtime_execution_store.build_runtime_model_resolver",
        lambda session: lambda ref: resolved_refs.append(ref) or ref.deployment_release_id,
    )
    resolver = build_execution_model_resolver(None, execution, "plv_live")
    assert resolver(model_ref) == "rel_selected_v3"
    assert resolved_refs[0].selection_policy == "explicit_release"
    with pytest.raises(ValueError, match="not part of the started runtime"):
        resolver(model_ref.model_copy(update={"deployment_release_id": "rel_unselected_v2"}))


def test_gateway_admission_rejects_unbound_release_before_backend_access(tmp_path):
    from inference_gateway.runtime.service import RealtimeGatewayService
    from tests.conftest import build_test_settings

    settings = build_test_settings("inference-realtime", tmp_path)
    service = RealtimeGatewayService(
        settings, service_name=settings.service.name, target_path=TargetPath.REALTIME
    )

    async def invoke():
        async with service._admission(
            release=SimpleNamespace(deployment_release_id="rel_unbound"),
            operation="test",
            wait=False,
        ):
            pytest.fail("Unbound release was admitted")

    try:
        with pytest.raises(PlatformError) as error:
            asyncio.run(invoke())
        assert error.value.code == "runtime_pipeline_required"
    finally:
        service.close()


def test_stop_cannot_release_execution_while_gateway_or_trigger_holds_shared_lock():
    calls = []

    class SessionStub:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def scalar(self, statement, params):
            calls.append((str(statement), params))
            return False

    session = SessionStub()
    assert lock_runtime_use(session, "rex_active") is True
    assert lock_runtime_use(session, "rex_active", exclusive=True) is False
    assert "pg_advisory_xact_lock_shared" in calls[0][0]
    assert "pg_try_advisory_xact_lock" in calls[1][0]
    assert calls[0][1] == calls[1][1]


def test_channel_stop_retains_demand_while_invocation_drains(sqlite_engine, tmp_path, monkeypatch):
    from cv_db import stop_runtime_execution

    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)
    with session_scope(settings) as session:
        request_runtime_channel_start(session, channel_id, requested_by="tester")
    try:
        orchestrator.process_one()
        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            monkeypatch.setattr(
                "cv_db.runtime_model_store.lock_runtime_use", lambda *a, **kw: False
            )
            stop_runtime_execution(session, runtime_execution_id=execution.id)
            assert execution.status == "draining"
            assert demanded_release_ids(session)
    finally:
        supervisor.shutdown()


def test_triton_load_uses_specific_versions_without_rewriting_repository(tmp_path, monkeypatch):
    from tests.conftest import build_test_settings

    settings = build_test_settings("deployment-controller", tmp_path)
    manager = TritonRepositoryManager(settings)
    captured = []
    client = TritonHttpControlClient(endpoint_url="http://unused", timeout_seconds=1)
    monkeypatch.setattr(client, "load_model", lambda name, **kw: captured.append((name, kw)))
    manager.load_required_versions(
        target_path=TargetPath.REALTIME,
        stable_model_name="rt__model",
        versions=[3, 1, 3],
        triton_client=client,
        package_manifest={
            "task_type": "classification",
            "io_schema": {
                "input_tensors": [
                    {"name": "images", "data_type": "fp32", "dims": [3, 32, 32], "layout": "NCHW"}
                ],
                "output_tensors": [{"name": "scores", "data_type": "fp32", "dims": [2]}],
            },
            "variants": [
                {"target_path": "realtime", "artifact_format": "onnx", "max_batch_size": 1}
            ],
        },
    )
    config = captured[0][1]["config"]
    assert config["version_policy"] == {"specific": {"versions": [1, 3]}}
    assert config["platform"] == "onnxruntime_onnx"
    assert config["input"][0]["format"] == "FORMAT_NCHW"
    assert not list(tmp_path.rglob("config.pbtxt"))


def test_gateway_http_requires_runtime_context_even_when_model_is_deployed(sqlite_engine, tmp_path):
    from fastapi import Depends, FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from inference_gateway.dependencies import require_runtime_inference

    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    settings.api_keys.enabled = False
    app = FastAPI()
    app.state.settings = settings

    @app.exception_handler(PlatformError)
    async def on_error(request, error):
        return JSONResponse({"code": error.code}, status_code=error.status_code)

    @app.post("/infer", dependencies=[Depends(require_runtime_inference)])
    async def infer():
        require_bound_release("rel_detection")
        return {"ok": True}

    supervisor, orchestrator = _orchestrator(settings)
    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="tester")
        orchestrator.process_one()
        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            headers = {
                "X-Runtime-Execution-Id": execution.id,
                "X-Runtime-Pipeline-Version-Id": execution.pipeline_version_id,
            }
        with TestClient(app) as client:
            assert client.post("/infer").status_code == 409
            assert client.post("/infer", headers=headers).status_code == 200
            with session_scope(settings) as session:
                execution = session.query(RuntimeExecution).one()
                execution.status = "stopped"
            assert client.post("/infer", headers=headers).status_code == 409
    finally:
        supervisor.shutdown()


def test_local_model_caches_release_only_unused_models(sqlite_engine, tmp_path):
    from inference_gateway.runtime.service import RealtimeGatewayService

    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)
    service = RealtimeGatewayService(
        settings, service_name=settings.service.name, target_path=TargetPath.REALTIME
    )
    with session_scope(settings) as session:
        request_runtime_channel_start(session, channel_id, requested_by="tester")
    try:
        orchestrator.process_one()
        service.anomaly_backend._inferencers.update(
            {"rel_detection": object(), "rel_unused": object()}
        )
        service.backend._onnx_sessions["unused.onnx"] = object()
        service.release_unused_local_models()
        assert set(service.anomaly_backend._inferencers) == {"rel_detection"}
        assert service.backend._onnx_sessions == {}
    finally:
        service.close()
        supervisor.shutdown()


def test_stopped_snapshot_trigger_rejects_request_before_inference(
    sqlite_engine, tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from tests.conftest import build_test_settings, configure_sqlite_factories
    from tests.unit.backend.test_runtime_execution_core import (
        _create_snapshot_channel,
        _create_published_pipeline,
        _trigger_pipeline_graph,
        _seed_runtime_capability,
    )
    from trigger_executor.main import app
    from cv_db.models import TriggerEndpoint

    configure_sqlite_factories(sqlite_engine, "trigger-executor")
    settings = build_test_settings("trigger-executor", tmp_path)
    app.state.settings = settings
    with session_scope(settings) as session:
        _seed_runtime_capability(session)
        channel = _create_snapshot_channel(session)
        _create_published_pipeline(
            session,
            name="Stopped trigger",
            mode=RuntimeMode.TRIGGER,
            graph=_trigger_pipeline_graph(channel.channel_id),
            channel_id=channel.channel_id,
        )
        session.get(RuntimeChannel, channel.channel_id).desired_state = "stopped"
        endpoint = session.query(TriggerEndpoint).one()
        endpoint.auth_required = False
        slug = endpoint.slug

    def unexpected_client(**kwargs):
        pytest.fail("Stopped trigger attempted to create an inference client")

    monkeypatch.setattr("trigger_executor.api.routes.HttpInferenceGatewayClient", unexpected_client)
    with TestClient(app) as client:
        response = client.put(f"/api/v1/runtime/triggers/{slug}", json={})
    assert response.status_code == 503
    assert response.json()["code"] == "runtime_trigger_runtime_unavailable"
