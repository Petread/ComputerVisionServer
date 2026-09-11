from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cv_contracts import EdgeDeploymentManifest, EdgeDeviceAddress
from edge_agent.plant import create_plant_app
from edge_agent.runtime import SourceFrame
from edge_agent.state import EdgeAgentConfig, EdgeAgentState
from fastapi.testclient import TestClient


class _TriggerSupervisor:
    recovered_deployment_ids: set[str] = set()

    def __init__(self, state: EdgeAgentState, manifest: EdgeDeploymentManifest) -> None:
        self.state = state
        self.manifest = manifest
        self.calls = 0

    def active_channel_ids(self) -> list[str]:
        return [self.manifest.channel_id]

    def trigger(
        self,
        channel_id: str,
        *,
        payload: dict[str, object],
        correlation_id: str,
    ) -> tuple[EdgeDeploymentManifest, SourceFrame, dict[str, object], object]:
        assert channel_id == self.manifest.channel_id
        self.calls += 1
        frame = SourceFrame(1, 640, 480, datetime.now(UTC).isoformat(), image=object())
        execution: dict[str, object] = {
            "schema_version": "runtime.execution.result.v1",
            "status": "success",
            "correlation_id": correlation_id,
            "final_response": {"accepted": True, "part_id": payload.get("part_id")},
            "overlay_primitives": [],
        }
        envelope = self.state.enqueue_result(
            channel_id=channel_id,
            workload_release_id=self.manifest.workload_release_id,
            correlation_id=correlation_id,
            payload=execution,
        )
        return self.manifest, frame, execution, envelope

    def close(self) -> None:
        return None


def test_plant_api_is_bounded_durable_and_idempotent(tmp_path: Path) -> None:
    state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path,
            minimum_disk_free_bytes=0,
            engine_builder_mode="simulated",
            plant_max_request_bytes=512,
            plant_allowed_ipv4s=("192.168.50.10", "192.168.50.40"),
        )
    )
    state.supervisor.close()
    manifest = EdgeDeploymentManifest(
        deployment_id="deployment-e1-plant",
        workload_release_id="release-e1-plant",
        channel_id="channel-e1-plant",
        pipeline_version_id="pipeline-e1-plant",
        idempotency_key="prepare-e1-plant",
        created_at=datetime.now(UTC).isoformat(),
    )
    supervisor = _TriggerSupervisor(state, manifest)
    state.supervisor = supervisor  # type: ignore[assignment]
    try:
        with TestClient(create_plant_app(state=state)) as client:
            health = client.get("/edge-plant/v1/health")
            assert health.status_code == 200
            assert health.json()["active_channel_ids"] == [manifest.channel_id]

            path = f"/edge-plant/v1/channels/{manifest.channel_id}/trigger"
            first = client.put(
                path,
                headers={"X-Idempotency-Key": "plc-cycle-4711"},
                json={"payload": {"part_id": "4711"}, "timeout_ms": 1000},
            )
            replay = client.put(
                path,
                headers={"X-Idempotency-Key": "plc-cycle-4711"},
                json={"payload": {"part_id": "4711"}, "timeout_ms": 1000},
            )
            assert first.status_code == 200
            assert replay.status_code == 200
            assert replay.json()["duplicate"] is True
            assert replay.json()["result_sequence"] == first.json()["result_sequence"]
            assert supervisor.calls == 1

            last = client.get(
                f"/edge-plant/v1/channels/{manifest.channel_id}/last-result"
            )
            assert last.status_code == 200
            assert last.json()["result"]["execution_result"]["status"] == "success"

            oversized = client.put(
                path,
                headers={"X-Idempotency-Key": "plc-cycle-too-large"},
                json={"payload": {"value": "x" * 1024}},
            )
            assert oversized.status_code == 413
    finally:
        state.close()
