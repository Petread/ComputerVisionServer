from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from importlib import util as importlib_util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cv_contracts import (
    EdgeArtifactDescriptor,
    EdgeCapabilitySnapshot,
    EdgeDeploymentManifest,
    EdgeDeploymentState,
    EdgeDeviceAddress,
    EdgeDeviceHealth,
    EdgeDeviceIdentity,
    EdgeDeviceRegistrationRequest,
)
from cv_contracts.runtime import RuntimeChannelCreateRequest, RuntimeGraphEdge, RuntimeNodeSpec
from cv_db import (
    EdgeDeploymentAttemptRecord,
    EdgeWorkloadReleaseRecord,
    PipelineDefinition,
    PipelineVersion,
    RuntimeChannel,
    RuntimeSource,
    create_edge_device,
    create_runtime_channel,
    delete_runtime_channel,
    ensure_builtin_node_types,
    get_edge_deployment_attempt,
    list_edge_deployment_attempts,
    list_edge_workload_releases,
    record_edge_observation,
    request_runtime_channel_restart,
    request_runtime_channel_start,
    request_runtime_channel_stop,
    session_scope,
)
from cv_runtime import BUILTIN_NODE_TYPES
from cv_runtime.distribution import runtime_code_digest
from cv_status import RuntimeMode, RuntimeSourceType
from edge_agent.state import EdgeAgentConfig, EdgeAgentState, EdgeStateError
from runtime_admin_api.edge_deployment_controller import EdgeDeploymentController
from runtime_admin_api.edge_workload_builder import _edge_source, build_edge_workload_release
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories


def test_edge_workload_maps_usb_channel_to_released_opencv_adapter() -> None:
    source = RuntimeSource(
        id="src_usb",
        slug="usb-camera",
        name="USB camera",
        source_type=RuntimeSourceType.USB_OPENCV.value,
        config={
            "device_index": 0,
            "backend": "dshow",
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "fourcc": "MJPG",
        },
    )

    assert _edge_source(channel_source=source) == {
        "type": "usb_opencv",
        "adapter": "usb_opencv",
        "uri": 0,
        "device_index": 0,
        "backend": "dshow",
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "fourcc": "MJPG",
    }


def test_local_tensorrt_admission_cache_golden_input_and_stop(tmp_path: Path) -> None:
    state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path,
            minimum_disk_free_bytes=0,
            engine_builder_mode="simulated",
        )
    )
    onnx = b"simulated-onnx-model"
    golden = b"golden-frame"
    onnx_digest = _upload(state, onnx, "upload-onnx")
    golden_digest = _upload(state, golden, "upload-golden")
    manifest = EdgeDeploymentManifest(
        deployment_id="deployment-m3",
        workload_release_id="release-m3",
        channel_id="channel-m3",
        pipeline_version_id="pipeline-m3",
        idempotency_key="prepare-m3",
        artifacts=[],
        runtime_requirements={
            "source": {"type": "passive", "interval_ms": 10},
            "engine_builds": [
                {
                    "node_key": "detect",
                    "onnx_sha256": onnx_digest,
                    "precision": "fp16",
                    "input_shapes": {"images": [1, 3, 640, 640]},
                }
            ],
            "golden_tests": [
                {
                    "node_key": "detect",
                    "input_sha256": golden_digest,
                    "comparison": "artifact_integrity",
                }
            ],
        },
        created_at=datetime.now(UTC).isoformat(),
    )
    try:
        first = state.prepare(manifest)
        assert first.admission_report["status"] == "admitted"
        assert first.engine_builds[0]["cache_hit"] is False
        assert Path(first.engine_builds[0]["engine_path"]).is_file()
        state.commit(manifest.deployment_id, "commit-m3")
        stopped = state.stop(manifest.deployment_id, "stop-m3")
        assert stopped.status == "stopped"
        assert state.stop(manifest.deployment_id, "stop-m3").status == "stopped"
        assert state.runtime_status().active_slot is None
        incompatible = manifest.model_copy(
            update={
                "deployment_id": "deployment-incompatible",
                "idempotency_key": "prepare-incompatible",
                "runtime_requirements": {
                    **manifest.runtime_requirements,
                    "target_profile": {"tensorrt_version": "0.0-incompatible"},
                },
            }
        )
        with pytest.raises(EdgeStateError) as rejected:
            state.prepare(incompatible)
        assert rejected.value.code == "incompatible_runtime"
        assert "release='0.0-incompatible', device='10.16'" in str(rejected.value)
    finally:
        state.close()


def test_m3_models_and_migration_extend_edge_runtime(tmp_path: Path) -> None:
    assert EdgeWorkloadReleaseRecord.__tablename__ == "edge_workload_releases"
    assert EdgeDeploymentAttemptRecord.__tablename__ == "edge_deployment_attempts"
    assert "edge_workload_release_id" in RuntimeChannel.__table__.columns
    assert "edge_deployment_id" in RuntimeChannel.__table__.columns

    engine = create_engine(f"sqlite:///{tmp_path / 'edge-m3-migration.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE runtime_channels (id VARCHAR(64) PRIMARY KEY)"))
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        path = Path("backend/migrations/versions/20260721_0029_edge_workload_releases.py")
        spec = importlib_util.spec_from_file_location("edge_m3_migration", path)
        assert spec is not None and spec.loader is not None
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = operations
        module.upgrade()

        schema = inspect(connection)
        assert {"edge_workload_releases", "edge_deployment_attempts"} <= set(
            schema.get_table_names()
        )
        channel_columns = {item["name"] for item in schema.get_columns("runtime_channels")}
        assert {"edge_workload_release_id", "edge_deployment_id"} <= channel_columns


def test_controller_builds_immutable_release_deploys_and_stops(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path / "server")
    edge_state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path / "edge",
            minimum_disk_free_bytes=0,
            supported_node_types=("source.channel_frame", "output.persisted_result"),
        )
    )
    try:
        with session_scope(settings) as session:
            channel_id, device_id = _seed_edge_channel(session)
            _channel, command = request_runtime_channel_start(
                session,
                channel_id,
                requested_by="operator",
                idempotency_key="start-edge-m3",
            )
            command_id = command.command_id

        controller = EdgeDeploymentController(
            settings,
            client_factory=lambda _device: _StateClient(edge_state),
            worker_id="test-controller",
        )
        assert controller.run_once() is True

        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert channel.observed_state == "running"
            assert channel.edge_deployment_id is not None
            assert channel.edge_workload_release_id is not None
            deployment_id = channel.edge_deployment_id
            first_release_id = channel.edge_workload_release_id
            attempt = get_edge_deployment_attempt(session, deployment_id)
            assert attempt is not None
            assert attempt.command_id == command_id
            assert attempt.status == "running"
            assert attempt.progress["local_admission"]["status"] == "admitted"
            releases = list_edge_workload_releases(session, edge_device_id=device_id)
            assert [item.workload_release_id for item in releases] == [first_release_id]
            rebuilt = build_edge_workload_release(
                session,
                settings=settings,
                channel_id=channel_id,
                created_by="operator",
            )
            assert rebuilt.workload_release_id == first_release_id
            # A replay after completion must not regress the channel to starting.
            replayed_channel, replayed_command = request_runtime_channel_start(
                session, channel_id, requested_by="operator", idempotency_key="start-edge-m3"
            )
            assert replayed_command.command_id == command_id
            assert replayed_channel.desired_state.value == "running"
            assert replayed_channel.observed_state.value == "running"
            # Simulate the persisted state left behind by the old replay bug.
            channel.desired_state = "start_requested"
            channel.observed_state = "starting"
            channel.health_summary = {"state": "degraded", "status_text": "Start requested"}

        # Idle controller repairs only the state; no prepare/commit is repeated.
        processed_before = controller.processed_commands
        assert controller.run_once() is False
        assert controller.processed_commands == processed_before
        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel.desired_state == "running"
            assert channel.observed_state == "running"
            assert channel.edge_deployment_id == deployment_id
            request_runtime_channel_restart(
                session,
                channel_id,
                requested_by="operator",
                idempotency_key="restart-edge-m3-failed",
            )
            from cv_db.edge_deployment_store import _repair_completed_edge_start_states

            _repair_completed_edge_start_states(session)
            assert channel.desired_state == "start_requested"
            assert channel.observed_state == "starting"
            # Replaying an older successful start must not overwrite this new request.
            request_runtime_channel_start(
                session, channel_id, requested_by="operator", idempotency_key="start-edge-m3"
            )
            assert channel.desired_state == "start_requested"

        edge_state.faults.activation_failure = True
        assert controller.run_once() is True
        edge_state.faults.activation_failure = False
        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert channel.observed_state == "running"
            assert channel.health_state == "degraded"
            assert channel.edge_deployment_id == deployment_id
            # A failed command replay preserves its degraded/error information.
            previous_error = channel.last_error
            request_runtime_channel_restart(
                session,
                channel_id,
                requested_by="operator",
                idempotency_key="restart-edge-m3-failed",
            )
            assert channel.observed_state == "running"
            assert channel.last_error == previous_error
            latest = list_edge_deployment_attempts(session, channel_id=channel_id)[0]
            assert latest.status == "rolled_back"
            assert edge_state.runtime_status().active_slot == "A"
            assert edge_state.deployment(deployment_id).status == "running"
            _channel, stop_command = request_runtime_channel_stop(
                session,
                channel_id,
                requested_by="operator",
                idempotency_key="stop-edge-m3",
            )

        assert controller.run_once() is True
        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert channel.observed_state == "stopped"
            assert channel.edge_deployment_id is None
            stopped = get_edge_deployment_attempt(session, deployment_id)
            assert stopped is not None
            assert stopped.status == "stopped"
            assert stopped.desired_state == "stopped"
            assert stop_command.command_id != command_id
            request_runtime_channel_stop(
                session, channel_id, requested_by="operator", idempotency_key="stop-edge-m3"
            )
            assert channel.observed_state == "stopped"
            assert channel.desired_state == "stopped"
    finally:
        edge_state.close()


def test_controller_rolls_back_failed_activation(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path / "server-failed")
    edge_state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path / "edge-failed",
            minimum_disk_free_bytes=0,
            supported_node_types=("source.channel_frame", "output.persisted_result"),
        )
    )
    edge_state.faults.activation_failure = True
    try:
        with session_scope(settings) as session:
            channel_id, _device_id = _seed_edge_channel(session)
            request_runtime_channel_start(
                session,
                channel_id,
                requested_by="operator",
                idempotency_key="start-edge-failed",
            )
        controller = EdgeDeploymentController(
            settings,
            client_factory=lambda _device: _StateClient(edge_state),
            worker_id="test-controller-failed",
        )
        assert controller.run_once() is True
        assert controller.failed_commands == 1
        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert channel.observed_state == "failed"
            assert channel.desired_state == "stopped"
            releases = list_edge_workload_releases(session, channel_id=channel_id)
            assert len(releases) == 1
            deployment = list_edge_deployment_attempts(session, channel_id=channel_id)[0]
            assert deployment.status == "rolled_back"
            assert edge_state.deployment(deployment.edge_deployment_id).status == "rolled_back"
            # Legacy failed first deployments could retain start_requested. They
            # remain safe to remove when no deployment was ever activated.
            channel.desired_state = "start_requested"
            delete_runtime_channel(
                session,
                channel_id,
                deleted_by="operator",
                reason="Remove failed commissioning channel",
            )
            assert channel.deleted_at is not None
    finally:
        edge_state.close()


class _StateClient:
    def __init__(self, state: EdgeAgentState) -> None:
        self.state = state

    def __enter__(self) -> _StateClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def upload_blob_file(
        self,
        path: Path,
        *,
        descriptor: EdgeArtifactDescriptor,
        idempotency_key: str,
    ) -> EdgeArtifactDescriptor:
        data = path.read_bytes()
        digest = descriptor.sha256
        self.state.append_blob(
            digest,
            data,
            offset=0,
            total_size=len(data),
            idempotency_key=idempotency_key,
        )
        return descriptor

    def prepare_deployment(self, manifest: EdgeDeploymentManifest) -> EdgeDeploymentState:
        return self.state.prepare(manifest)

    def commit_deployment(self, deployment_id: str, *, idempotency_key: str) -> EdgeDeploymentState:
        return self.state.commit(deployment_id, idempotency_key)

    def rollback_deployment(
        self, deployment_id: str, *, idempotency_key: str
    ) -> EdgeDeploymentState:
        return self.state.rollback(deployment_id, idempotency_key)

    def stop_deployment(self, deployment_id: str, *, idempotency_key: str) -> EdgeDeploymentState:
        return self.state.stop(deployment_id, idempotency_key)


def _seed_edge_channel(session) -> tuple[str, str]:  # noqa: ANN001
    device = create_edge_device(
        session,
        EdgeDeviceRegistrationRequest(
            name="Orin Nano M3",
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            allowed_cidr="192.168.50.0/24",
            server_ip="192.168.50.10",
        ),
        created_by="operator",
    )
    observed_at = datetime.now(UTC).isoformat()
    record_edge_observation(
        session,
        device.edge_device_id,
        identity=EdgeDeviceIdentity(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            hostname="edge-m3",
            platform="jetson_orin_nano",
            architecture="aarch64",
            agent_version="0.3.0-m3",
            runtime_version="0.3.0-m3",
            boot_id="boot-m3",
        ),
        health=EdgeDeviceHealth(
            state="ok",
            detail="ready",
            boot_id="boot-m3",
            observed_at=observed_at,
            disk_free_bytes=100 * 1024**3,
        ),
        capabilities=EdgeCapabilitySnapshot(
            runtime_code_digest=runtime_code_digest(),
            management_ip="192.168.50.21",
            platform="jetson_orin_nano",
            architecture="aarch64",
            os_version="Jetson Linux 39.2",
            jetpack_version="7.2",
            l4t_version="39.2",
            cuda_version="13.2",
            tensorrt_version="10.16",
            gpu_model="Jetson Orin Nano",
            gpu_memory_bytes=8 * 1024**3,
            ram_total_bytes=8 * 1024**3,
            storage_total_bytes=256 * 1024**3,
            storage_free_bytes=100 * 1024**3,
            agent_version="0.3.0-m3",
            runtime_version="0.3.0-m3",
            supported_node_types=["source.channel_frame", "output.persisted_result"],
            camera_adapters=["rtsp"],
            observed_at=observed_at,
        ),
    )
    channel = create_runtime_channel(
        session,
        RuntimeChannelCreateRequest(
            name="Edge camera M3",
            technical_place="line-m3",
            source_type=RuntimeSourceType.RTSP,
            source_config={"url": "rtsp://camera.local/live", "sample_every_n": 2},
            placement={"target": "edge_device", "edge_device_id": device.edge_device_id},
        ),
        created_by="operator",
    )
    ensure_builtin_node_types(session)
    source = _node("channel_input", "source.channel_frame")
    output = _node("persist", "output.persisted_result")
    graph = {
        "nodes": [source.model_dump(mode="json"), output.model_dump(mode="json")],
        "edges": [
            RuntimeGraphEdge(
                from_node_key="channel_input",
                from_port="frame",
                to_node_key="persist",
                to_port="result",
            ).model_dump(mode="json")
        ],
    }
    definition = PipelineDefinition(
        id="pipe_edge_m3",
        slug="edge-m3",
        name="Edge M3",
        scope="channel_instance",
        owner_channel_id=channel.channel_id,
        status="published",
        published_version_id="pver_edge_m3",
    )
    version = PipelineVersion(
        id="pver_edge_m3",
        pipeline_definition_id=definition.id,
        version_no=1,
        status="published",
        mode=RuntimeMode.REALTIME.value,
        graph=graph,
        validation_report={"valid": True},
        model_capability_constraints={},
        node_contract_versions={},
        config_digest="m3-config-digest",
        published_at=datetime.now(UTC),
        published_by="operator",
    )
    session.add_all([definition, version])
    row = session.get(RuntimeChannel, channel.channel_id)
    assert row is not None
    row.pipeline_version_id = version.id
    row.assigned_pipeline = definition.name
    session.flush()
    return channel.channel_id, device.edge_device_id


def _node(node_key: str, node_type: str) -> RuntimeNodeSpec:
    definition = {item.node_type: item for item in BUILTIN_NODE_TYPES}[node_type]
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=definition.node_category,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config=dict(definition.default_config),
    )


def _upload(state: EdgeAgentState, payload: bytes, key: str) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    state.append_blob(
        digest,
        payload,
        offset=0,
        total_size=len(payload),
        idempotency_key=key,
    )
    return digest
