from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from cv_contracts import EdgeDeploymentManifest
from edge_agent.engine import EdgeEngineAdmissionError, EdgeEngineManager


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
