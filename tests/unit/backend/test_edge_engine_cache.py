from __future__ import annotations

import hashlib
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cv_contracts import EdgeDeploymentManifest, EdgeDeviceAddress
from edge_agent.engine import EdgeEngineAdmissionError, EdgeEngineManager
from edge_agent.state import EdgeAgentConfig, EdgeAgentState


def _manifest(release: str) -> EdgeDeploymentManifest:
    return EdgeDeploymentManifest(
        deployment_id="deployment-" + release,
        workload_release_id=release,
        channel_id="channel-cache",
        pipeline_version_id="pipeline-cache",
        idempotency_key="prepare-" + release,
        created_at="2026-09-11T13:32:58Z",
    )


def test_identical_engine_is_reused_across_workload_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"model")
    item = {
        "node_key": "classification",
        "onnx_sha256": hashlib.sha256(onnx.read_bytes()).hexdigest(),
        "precision": "fp16",
        "input_shapes": {"images": [1, 3, 960, 960]},
    }
    manager = EdgeEngineManager(
        engine_root=tmp_path / "engines",
        artifact_resolver=lambda digest: onnx,
        builder_mode="trtexec",
        cache_namespace="orin-nano:trt10.16",
    )
    calls = []

    def build(command, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(command)
        destination = next(
            arg.removeprefix("--saveEngine=") for arg in command if arg.startswith("--saveEngine=")
        )
        Path(destination).write_bytes(b"compiled-engine")
        kwargs["stdout"].write("Building engine\nEngine saved\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", build)
    first = manager._build_engine(_manifest("release-old"), item)
    original = Path(first["engine_path"])
    second = manager._build_engine(_manifest("release-new"), item)
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert len(calls) == 1
    assert original != Path(second["engine_path"])
    assert original.read_bytes() == Path(second["engine_path"]).read_bytes()
    assert first["engine_sha256"] == second["engine_sha256"]
    assert original.with_suffix(".build.log").read_text() == "Building engine\nEngine saved\n"
    assert not list((tmp_path / "engines").rglob("*.tmp"))

    # Precision, shapes, model content and platform changes must still rebuild.
    variants = [
        {**item, "precision": "fp32"},
        {**item, "input_shapes": {"images": [1, 3, 640, 640]}},
    ]
    for index, changed in enumerate(variants):
        assert not manager._build_engine(_manifest(f"release-variant-{index}"), changed)[
            "cache_hit"
        ]
    manager.cache_namespace = "other-platform"
    assert not manager._build_engine(_manifest("release-other-platform"), item)["cache_hit"]
    onnx.write_bytes(b"other-model")
    item["onnx_sha256"] = hashlib.sha256(onnx.read_bytes()).hexdigest()
    assert not manager._build_engine(_manifest("release-other-model"), item)["cache_hit"]
    assert len(calls) == 5


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_engine_build_keeps_log_and_removes_partial_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout: bool,
) -> None:
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"model")
    manager = EdgeEngineManager(
        engine_root=tmp_path / "engines",
        artifact_resolver=lambda digest: onnx,
        builder_mode="trtexec",
    )

    def fail(command, **kwargs):  # noqa: ANN001, ANN003, ANN202
        destination = next(
            arg.removeprefix("--saveEngine=") for arg in command if arg.startswith("--saveEngine=")
        )
        Path(destination).write_bytes(b"incomplete")
        kwargs["stdout"].write("fixture TensorRT error\n")
        if timeout:
            raise subprocess.TimeoutExpired(command, 900)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(EdgeEngineAdmissionError, match="Log:") as failure:
        manager._build_engine(
            _manifest("release-failed"),
            {
                "node_key": "classification",
                "onnx_sha256": hashlib.sha256(onnx.read_bytes()).hexdigest(),
            },
        )
    if not timeout:
        assert "fixture TensorRT error" in str(failure.value)
    logs = list((tmp_path / "engines").rglob("*.build.log"))
    assert len(logs) == 1
    assert "fixture TensorRT error" in logs[0].read_text()
    assert not list((tmp_path / "engines").rglob("*.tmp"))
    assert not list((tmp_path / "engines").rglob("*.plan"))


def test_persistent_cache_survives_release_cleanup_and_repairs_corrupt_copies(
    tmp_path: Path,
) -> None:
    onnx = tmp_path / "model.onnx"
    onnx.write_bytes(b"model")
    item = {"node_key": "model", "onnx_sha256": hashlib.sha256(b"model").hexdigest()}

    def manager() -> EdgeEngineManager:
        return EdgeEngineManager(engine_root=tmp_path / "engines", artifact_resolver=lambda _: onnx)

    first = manager()._build_engine(_manifest("release-first"), item)
    original = Path(first["engine_path"])
    shared = tmp_path / "engines" / "cache" / f"{first['cache_key']}.plan"
    expected = original.read_bytes()
    original.unlink()
    second = manager()._build_engine(_manifest("release-next"), item)
    assert second["cache_hit"]
    current = Path(second["engine_path"])
    current.write_bytes(b"damaged")
    repaired = manager()._build_engine(_manifest("release-next"), item)
    assert repaired["cache_hit"]
    assert current.read_bytes() == expected
    current.write_bytes(b"")
    shared.write_bytes(b"damaged")
    rebuilt = manager()._build_engine(_manifest("release-next"), item)
    assert not rebuilt["cache_hit"]
    assert rebuilt["reason"] == "invalid_cache"
    assert shared.read_bytes() == current.read_bytes() == expected


def test_agent_restart_and_concurrent_requests_do_not_rebuild_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def build(command, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(command)
        path = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--saveEngine="))
        Path(path).write_bytes(b"compiled-plan")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", build)
    config = EdgeAgentConfig(
        address=EdgeDeviceAddress(management_ip="192.168.50.21"),
        data_root=tmp_path,
        minimum_disk_free_bytes=0,
        engine_builder_mode="trtexec",
    )
    item = {"node_key": "model", "onnx_sha256": hashlib.sha256(b"model").hexdigest()}
    manifest = _manifest("release-restart").model_copy(
        update={
            "runtime_requirements": {
                "engine_builds": [item],
                "source": {"type": "passive", "interval_ms": 10},
            }
        }
    )
    first = EdgeAgentState(config)
    try:
        onnx = first._blob_path(item["onnx_sha256"])
        onnx.parent.mkdir(parents=True, exist_ok=True)
        onnx.write_bytes(b"model")
        first.prepare(manifest)
        first.commit(manifest.deployment_id, "commit-restart")
    finally:
        first.close()
    restarted = EdgeAgentState(config)
    try:
        deadline = time.monotonic() + 3
        while (
            manifest.deployment_id not in restarted._runtime_engine_builds
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        processes = restarted.runtime_status().processes
        assert processes[0].channel_id == manifest.channel_id
        assert processes[0].engines[0].cache_hit
        assert processes[0].metrics is not None
        assert len(calls) == 1
        changed = {**item, "precision": "fp32"}
        with ThreadPoolExecutor(max_workers=4) as executor:
            builds = list(
                executor.map(
                    lambda _: restarted.engine_manager._build_engine(manifest, changed), range(4)
                )
            )
        assert len(calls) == 2
        assert sum(build["cache_hit"] for build in builds) == 3
    finally:
        restarted.close()
