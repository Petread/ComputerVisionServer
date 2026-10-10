from __future__ import annotations

import json
import sys
import types
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cv_contracts import TrainingJobCreateRequest
from cv_core.settings import PlatformSettings, ServiceRuntimeConfig, StorageRoots
from cv_db import (
    Base,
    Dataset,
    DatasetSnapshot,
    create_promotion_request,
    create_training_job_request,
    record_deployment_approval,
    register_trained_model_version,
)
from cv_db.model_catalog import architecture_summary
from cv_db.models import DeploymentRelease, DeploymentTarget
from cv_deploy.packaging import (
    TENSORRT_TOPK_K_LIMIT,
    _build_tensorrt_engine_from_onnx,
    _deployment_io_schema,
    _deployment_preprocessing,
    _existing_package_matches,
    _load_mmdeploy_tensorrt_api,
    _prepare_tensorrt_compatible_onnx,
    _trtexec_failure_hint,
    _validate_model_artifact,
)
from cv_deploy.triton import TritonRepositoryManager, render_config_pbtxt
from cv_status import (
    DatasetStatus,
    DeploymentApprovalScope,
    DeploymentApprovalStatus,
    DeploymentReleaseStatus,
    EnvironmentName,
    SnapshotStatus,
    TargetPath,
    TaskType,
)
from control_worker.jobs.deployment import (
    activate_deployment_model,
    build_deployment_package,
    deactivate_deployment_model,
    promote_model_release,
    rollback_model_release,
    stage_model_release,
)


class FakeTritonClient:
    def __init__(self) -> None:
        self.loaded_models: list[str] = []

    def load_model(self, model_name: str) -> None:
        self.loaded_models.append(model_name)

    def unload_model(self, model_name: str) -> None:
        return None

    def repository_index(self, *, ready_only: bool = False):  # noqa: ANN001
        _ = ready_only
        return []

    def is_server_ready(self) -> bool:
        return True

    def is_model_ready(self, model_name: str, model_version: int) -> bool:
        _ = (model_name, model_version)
        return True


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def override_session_scope(engine):
    @contextmanager
    def _scope(_settings) -> Generator[Session, None, None]:
        session = Session(engine, expire_on_commit=False)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


@pytest.fixture()
def settings(tmp_path: Path) -> PlatformSettings:
    return PlatformSettings(
        service=ServiceRuntimeConfig(name="control-worker"),
        storage=StorageRoots(
            state_root=str(tmp_path / "state"),
            training_root=str(tmp_path / "state" / "training"),
            serving_root=str(tmp_path / "state" / "serving"),
            audit_root=str(tmp_path / "state" / "audit"),
            logs_root=str(tmp_path / "state" / "logs"),
        ),
    )


@pytest.fixture()
def fake_triton_client():
    return FakeTritonClient()


@pytest.fixture(autouse=True)
def patch_worker_runtime(monkeypatch, override_session_scope, settings, fake_triton_client):
    monkeypatch.setattr("control_worker.jobs.deployment.load_platform_settings", lambda _service: settings)
    monkeypatch.setattr("control_worker.jobs.deployment.session_scope", override_session_scope)
    monkeypatch.setattr("control_worker.jobs.deployment._triton_client", lambda _settings, _target: fake_triton_client)


def test_build_deployment_package_creates_immutable_package_structure(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(session, tmp_path=tmp_path, suffix="pkg")
        record_deployment_approval(
            session,
            model_version_id=version.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.STAGING.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="package build",
        )
        session.commit()

    result = build_deployment_package(version.id)

    package_root = settings.storage.serving_package_root(version.id)
    manifest_path = package_root / "manifest.json"
    assert result["status"] == "ready"
    assert package_root.name == f"mvr_{version.id}"
    assert manifest_path.exists()
    assert (package_root / "realtime" / "model.plan").exists()
    assert (package_root / "realtime" / "config.pbtxt").exists()
    assert (package_root / "batch" / "model.plan").exists()
    assert (package_root / "batch" / "config.pbtxt").exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["model_version_id"] == version.id
    assert manifest["trainer_backend"] == "unit-test-backend"
    assert manifest["compatible_targets"] == ["realtime", "batch"]
    assert manifest["class_count"] == 2
    assert len(manifest["variants"]) == 2


def test_realtime_instance_group_count_follows_serving_configuration(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
) -> None:
    settings.triton.realtime.instance_group_count = 2
    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(session, tmp_path=tmp_path, suffix="instances")
        session.commit()

    build_deployment_package(version.id)

    package_root = settings.storage.serving_package_root(version.id)
    manifest = json.loads((package_root / "manifest.json").read_text(encoding="utf-8"))
    realtime_variant = next(
        item for item in manifest["variants"] if item["target_path"] == "realtime"
    )
    config_text = (package_root / "realtime" / "config.pbtxt").read_text(
        encoding="utf-8"
    )
    assert realtime_variant["instance_group_count"] == 2
    assert "    count: 2" in config_text


def test_legacy_classification_package_is_rebuilt_with_model_class_order(
    engine: Engine,
    settings: PlatformSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cv_deploy.packaging import build_deployment_package as build_package
    from cv_deploy.packaging import sha256_file
    from inference_gateway.runtime.resolver import _class_labels

    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(session, tmp_path=tmp_path, suffix="classmap")
        session.commit()
    # First reproduce a previously built package with the full snapshot class map.
    build_deployment_package(version.id)
    copy_manifest = json.loads(json.dumps(version.export_manifest))
    copy_manifest["trainer_backend"] = "ultralytics-yolo"
    monkeypatch.setattr(
        "cv_deploy.packaging._onnx_graph_io_schema",
        lambda _path: {
            "input_name": "images", "input_shape": [-1, 3, 640, 640],
            "outputs": [{"name": "output0", "shape": [-1, 1], "data_type": "FP32"}],
            "metadata": {"names": "{0: '001_rework'}"},
        },
    )
    manifest = build_package(
        settings=settings, model_version_id=version.id, model_family_id=version.model_family_id,
        model_family_slug="classification", version_no=1, task_type="classification",
        contract_major=1, immutable_metadata_sha256=None, export_manifest=copy_manifest,
        artifact_manifest=version.artifact_manifest,
        export_manifest_uri=version.export_manifest_uri,
        artifact_manifest_uri=version.artifact_manifest_uri,
    )
    class_map_path = Path(manifest["package_root"]) / "source" / "class_map.json"
    assert json.loads(class_map_path.read_text(encoding="utf-8")) == ["rework"]
    assert manifest["class_count"] == 1
    assert manifest["class_map_sha256"] == sha256_file(class_map_path)
    assert manifest["postprocessing"]["class_map"] == ["rework"]
    assert manifest["io_schema"]["output_tensors"][0]["dims"] == [1]
    assert _class_labels(manifest) == ["rework"]
    exported = json.loads((class_map_path.parent / "export_manifest.json").read_text("utf-8"))
    assert exported["class_count"] == 1
    assert exported["postprocessing"]["class_map"] == ["rework"]
    assert copy_manifest["postprocessing"]["class_map"] == ["accepted", "rework"]


def test_deployment_runtime_engine_build_normalizes_hardware_profile(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_tensorrt_build(**kwargs) -> Path:  # noqa: ANN003
        target_root = kwargs["target_root"]
        plan_path = target_root / "model.plan"
        plan_path.write_bytes(b"deployment-runtime-tensorrt-plan")
        return plan_path

    monkeypatch.setattr("cv_deploy.packaging._build_tensorrt_engine_from_onnx", _fake_tensorrt_build)

    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(
            session,
            tmp_path=tmp_path,
            suffix="runtimegpu",
            gpu_profile="legacy-training-profile",
            include_tensorrt=False,
        )
        session.commit()

    result = build_deployment_package(version.id)

    package_root = settings.storage.serving_package_root(version.id)
    manifest = json.loads((package_root / "manifest.json").read_text(encoding="utf-8"))
    assert result["status"] == "ready"
    assert manifest["hardware_compatibility"]["target_gpu_profile"] == settings.triton.realtime.gpu_profile
    assert manifest["hardware_compatibility"]["validated_devices"] == [settings.triton.realtime.gpu_profile]

    with Session(engine, expire_on_commit=False) as session:
        promotion_id = approve_and_request_staging(session, model_version_id=version.id)
        session.commit()

    stage_model_release(promotion_id)

    with Session(engine, expire_on_commit=False) as session:
        release = session.scalar(
            select(DeploymentRelease).where(DeploymentRelease.model_version_id == version.id)
        )

    assert release is not None
    assert release.status == DeploymentReleaseStatus.ACTIVATED.value


def test_mmdeploy_tensorrt_loader_supports_backend_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    package = types.ModuleType("mmdeploy")
    package.__path__ = []  # type: ignore[attr-defined]
    apis = types.ModuleType("mmdeploy.apis")
    apis.__path__ = []  # type: ignore[attr-defined]
    backend = types.ModuleType("mmdeploy.backend")
    backend.__path__ = []  # type: ignore[attr-defined]
    api_tensorrt = types.ModuleType("mmdeploy.apis.tensorrt")
    backend_tensorrt = types.ModuleType("mmdeploy.backend.tensorrt")

    def is_available() -> bool:
        return True

    def from_onnx(*_args: object, **_kwargs: object) -> object:
        return object()

    def load(_path: str) -> object:
        return object()

    def save(_engine: object, _path: str) -> None:
        return None

    api_tensorrt.is_available = is_available  # type: ignore[attr-defined]
    backend_tensorrt.from_onnx = from_onnx  # type: ignore[attr-defined]
    backend_tensorrt.load = load  # type: ignore[attr-defined]
    backend_tensorrt.save = save  # type: ignore[attr-defined]

    for module in (package, apis, backend, api_tensorrt, backend_tensorrt):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    resolved_from_onnx, resolved_is_available, resolved_load, resolved_save = (
        _load_mmdeploy_tensorrt_api()
    )

    assert resolved_from_onnx is from_onnx
    assert resolved_is_available is is_available
    assert resolved_load is load
    assert resolved_save is save


def test_mmdeploy_tensorrt_loader_supports_backend_utils_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = types.ModuleType("mmdeploy")
    package.__path__ = []  # type: ignore[attr-defined]
    apis = types.ModuleType("mmdeploy.apis")
    apis.__path__ = []  # type: ignore[attr-defined]
    backend = types.ModuleType("mmdeploy.backend")
    backend.__path__ = []  # type: ignore[attr-defined]
    api_tensorrt = types.ModuleType("mmdeploy.apis.tensorrt")
    backend_tensorrt = types.ModuleType("mmdeploy.backend.tensorrt")
    backend_tensorrt.__path__ = []  # type: ignore[attr-defined]
    backend_tensorrt_utils = types.ModuleType("mmdeploy.backend.tensorrt.utils")

    def is_available() -> bool:
        return True

    def from_onnx(*_args: object, **_kwargs: object) -> object:
        return object()

    def load(_path: str) -> object:
        return object()

    def save(_engine: object, _path: str) -> None:
        return None

    api_tensorrt.is_available = is_available  # type: ignore[attr-defined]
    backend_tensorrt_utils.from_onnx = from_onnx  # type: ignore[attr-defined]
    backend_tensorrt_utils.load = load  # type: ignore[attr-defined]
    backend_tensorrt_utils.save = save  # type: ignore[attr-defined]

    for module in (
        package,
        apis,
        backend,
        api_tensorrt,
        backend_tensorrt,
        backend_tensorrt_utils,
    ):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    resolved_from_onnx, resolved_is_available, resolved_load, resolved_save = (
        _load_mmdeploy_tensorrt_api()
    )

    assert resolved_from_onnx is from_onnx
    assert resolved_is_available is is_available
    assert resolved_load is load
    assert resolved_save is save


def test_deployment_tensorrt_build_prefers_trtexec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trtexec = tmp_path / "trtexec"
    trtexec.write_text("#!/bin/sh\n", encoding="utf-8")
    onnx_path = tmp_path / "model.onnx"
    onnx_path.write_bytes(b"onnx")
    target_root = tmp_path / "package"
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> object:
        captured["command"] = command
        captured["kwargs"] = kwargs
        save_arg = next(item for item in command if item.startswith("--saveEngine="))
        Path(save_arg.split("=", 1)[1]).write_bytes(b"tensorrt-engine")
        return types.SimpleNamespace(returncode=0, stdout="trtexec ok")

    monkeypatch.setattr("cv_deploy.packaging._find_trtexec_binary", lambda: trtexec)
    monkeypatch.setattr("cv_deploy.packaging.subprocess.run", fake_run)

    engine_path = _build_tensorrt_engine_from_onnx(
        export_manifest={
            "build_manifest": {"stage": "deployment"},
            "artifact_validation": {"checks": {"onnx_valid": True}},
            "io_schema": {"input_tensors": [{"name": "images", "dims": [3, 320, 640]}]},
            "hardware_compatibility": {
                "max_batch_size": {"realtime": 1, "batch": 4},
                "precision": "fp16",
            },
        },
        onnx_source=onnx_path,
        target_root=target_root,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert engine_path == target_root / "model.plan"
    assert engine_path.read_bytes() == b"tensorrt-engine"
    assert str(trtexec) == command[0]
    assert f"--onnx={onnx_path}" in command
    assert f"--saveEngine={target_root / 'model.plan'}" in command
    assert "--minShapes=images:1x3x320x640" in command
    assert "--optShapes=images:1x3x320x640" in command
    assert "--maxShapes=images:4x3x320x640" in command
    assert "--fp16" in command


def test_temporal_deployment_tensorrt_build_uses_sequence_feature_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trtexec = tmp_path / "trtexec"
    trtexec.write_text("#!/bin/sh\n", encoding="utf-8")
    onnx_path = tmp_path / "temporal.onnx"
    onnx_path.write_bytes(b"onnx")
    target_root = tmp_path / "temporal-package"
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> object:
        captured["command"] = command
        captured["kwargs"] = kwargs
        save_arg = next(item for item in command if item.startswith("--saveEngine="))
        Path(save_arg.split("=", 1)[1]).write_bytes(b"tensorrt-engine")
        return types.SimpleNamespace(returncode=0, stdout="trtexec ok")

    monkeypatch.setattr("cv_deploy.packaging._find_trtexec_binary", lambda: trtexec)
    monkeypatch.setattr("cv_deploy.packaging.subprocess.run", fake_run)

    engine_path = _build_tensorrt_engine_from_onnx(
        export_manifest={
            "task_type": "temporal_sequence",
            "build_manifest": {"stage": "deployment"},
            "artifact_validation": {"checks": {"onnx_valid": True}},
            "io_schema": {
                "input_tensors": [{"name": "features", "dims": [32, 17]}]
            },
            "hardware_compatibility": {
                "precision": "fp16",
                "max_batch_size": {"realtime": 1, "batch": 4},
            },
        },
        onnx_source=onnx_path,
        target_root=target_root,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert engine_path.read_bytes() == b"tensorrt-engine"
    assert "--minShapes=features:1x32x17" in command
    assert "--optShapes=features:1x32x17" in command
    assert "--maxShapes=features:4x32x17" in command
    assert "--fp16" in command


def test_tensorrt_onnx_preparation_caps_static_topk_initializers(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper

    onnx_path = tmp_path / "model.onnx"
    graph = helper.make_graph(
        [
            helper.make_node(
                "TopK",
                ["scores", "topk_limit"],
                ["values", "indices"],
                axis=1,
            )
        ],
        "topk-test",
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, [1, 5000])],
        [
            helper.make_tensor_value_info("values", TensorProto.FLOAT, [1, TENSORRT_TOPK_K_LIMIT]),
            helper.make_tensor_value_info("indices", TensorProto.INT64, [1, TENSORRT_TOPK_K_LIMIT]),
        ],
        [
            numpy_helper.from_array(
                np.array([TENSORRT_TOPK_K_LIMIT + 1200], dtype=np.int64),
                name="topk_limit",
            )
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    onnx.save(model, str(onnx_path))

    patched_path = _prepare_tensorrt_compatible_onnx(
        onnx_source=onnx_path,
        target_root=tmp_path / "package",
    )

    assert patched_path != onnx_path
    patched = onnx.load(str(patched_path))
    topk_limit = next(item for item in patched.graph.initializer if item.name == "topk_limit")
    assert int(numpy_helper.to_array(topk_limit).reshape(-1)[0]) == TENSORRT_TOPK_K_LIMIT


def test_tensorrt_onnx_preparation_clamps_dynamic_topk_limit(tmp_path: Path) -> None:
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper

    onnx_path = tmp_path / "model.onnx"
    graph = helper.make_graph(
        [
            helper.make_node(
                "TopK",
                ["scores", "requested_k"],
                ["values", "indices"],
                axis=1,
                name="postprocess_topk",
            )
        ],
        "dynamic-topk-test",
        [
            helper.make_tensor_value_info("scores", TensorProto.FLOAT, [1, 5000]),
            helper.make_tensor_value_info("requested_k", TensorProto.INT64, [1]),
        ],
        [
            helper.make_tensor_value_info("values", TensorProto.FLOAT, [1, TENSORRT_TOPK_K_LIMIT]),
            helper.make_tensor_value_info("indices", TensorProto.INT64, [1, TENSORRT_TOPK_K_LIMIT]),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    onnx.save(model, str(onnx_path))

    patched_path = _prepare_tensorrt_compatible_onnx(
        onnx_source=onnx_path,
        target_root=tmp_path / "package",
    )

    assert patched_path != onnx_path
    patched = onnx.load(str(patched_path))
    topk_node = next(node for node in patched.graph.node if node.op_type == "TopK")
    assert topk_node.input[1] != "requested_k"
    min_node = next(node for node in patched.graph.node if node.output and node.output[0] == topk_node.input[1])
    assert min_node.op_type == "Min"
    assert "requested_k" in list(min_node.input)
    limit_input = next(item for item in min_node.input if item != "requested_k")
    topk_limit = next(item for item in patched.graph.initializer if item.name == limit_input)
    assert int(numpy_helper.to_array(topk_limit).reshape(-1)[0]) == TENSORRT_TOPK_K_LIMIT


def test_tensorrt_onnx_preparation_keeps_small_static_topk_constant(tmp_path: Path) -> None:
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    onnx_path = tmp_path / "model.onnx"
    graph = helper.make_graph(
        [
            helper.make_node(
                "Constant",
                [],
                ["small_k"],
                value_int=100,
            ),
            helper.make_node(
                "TopK",
                ["scores", "small_k"],
                ["values", "indices"],
                axis=1,
            ),
        ],
        "small-topk-test",
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, [1, 5000])],
        [
            helper.make_tensor_value_info("values", TensorProto.FLOAT, [1, 100]),
            helper.make_tensor_value_info("indices", TensorProto.INT64, [1, 100]),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    onnx.save(model, str(onnx_path))

    patched_path = _prepare_tensorrt_compatible_onnx(
        onnx_source=onnx_path,
        target_root=tmp_path / "package",
    )

    assert patched_path == onnx_path


def test_trtexec_failure_hint_explains_topk_limit() -> None:
    hint = _trtexec_failure_hint(
        "IBuilder::buildSerializedNetwork: Error Code 4: API Usage Error "
        "(ITopKLayer /TopK: /TopK: K exceeds the maximum value allowed (3840).)"
    )

    assert "TopK" in hint
    assert str(TENSORRT_TOPK_K_LIMIT) in hint


def test_dev_build_deployment_package_uses_onnx_when_tensorrt_tooling_is_missing(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing_tensorrt(**_kwargs: object) -> None:
        raise RuntimeError("MMDeploy TensorRT tooling is required to build deployment engines.")

    monkeypatch.setattr("cv_deploy.packaging._build_tensorrt_engine_from_onnx", _missing_tensorrt)
    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(
            session,
            tmp_path=tmp_path,
            suffix="onnxfallback",
            include_tensorrt=False,
        )
        record_deployment_approval(
            session,
            model_version_id=version.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.STAGING.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="onnx fallback package build",
        )
        session.commit()

    result = build_deployment_package(version.id)

    package_root = settings.storage.serving_package_root(version.id)
    manifest = json.loads((package_root / "manifest.json").read_text(encoding="utf-8"))
    assert result["status"] == "ready"
    assert (package_root / "realtime" / "model.onnx").exists()
    assert not (package_root / "realtime" / "model.plan").exists()
    assert manifest["variants"][0]["artifact_file"] == "model.onnx"
    assert manifest["variants"][0]["artifact_format"] == "onnx"
    assert manifest["source_manifests"]["deployment_artifact"]["format"] == "onnx"
    assert "tensorrt" not in manifest["source_manifests"]
    config_text = (package_root / "realtime" / "config.pbtxt").read_text(encoding="utf-8")
    assert 'platform: "onnxruntime_onnx"' in config_text
    assert "version_policy {" in config_text
    assert 'default_model_filename: "model.onnx"' in config_text


def test_prod_build_deployment_package_uses_onnx_when_fallback_is_enabled(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing_tensorrt(**_kwargs: object) -> None:
        raise RuntimeError("MMDeploy TensorRT tooling is required to build deployment engines.")

    settings.env = EnvironmentName.PROD
    settings.training.allow_onnx_deployment_fallback = True
    monkeypatch.setattr("cv_deploy.packaging._build_tensorrt_engine_from_onnx", _missing_tensorrt)
    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(
            session,
            tmp_path=tmp_path,
            suffix="prodonnxfallback",
            include_tensorrt=False,
        )
        record_deployment_approval(
            session,
            model_version_id=version.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.STAGING.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="prod onnx fallback package build",
        )
        session.commit()

    result = build_deployment_package(version.id)

    package_root = settings.storage.serving_package_root(version.id)
    manifest = json.loads((package_root / "manifest.json").read_text(encoding="utf-8"))
    assert result["status"] == "ready"
    assert (package_root / "realtime" / "model.onnx").exists()
    assert manifest["variants"][0]["artifact_format"] == "onnx"
    assert manifest["source_manifests"]["deployment_artifact"]["format"] == "onnx"


def test_prod_package_reuse_rejects_stale_onnx_fallback(tmp_path: Path) -> None:
    io_schema = {
        "input_tensors": [],
        "output_tensors": [],
    }
    hardware_compatibility = {"accelerator": "nvidia_gpu"}
    manifest = {
        "io_schema": io_schema,
        "preprocessing": {},
        "hardware_compatibility": hardware_compatibility,
        "package_root": str(tmp_path),
        "variants": [
            {
                "target_path": "realtime",
                "artifact_format": "onnx",
            }
        ],
    }

    assert not _existing_package_matches(
        manifest,
        io_schema=io_schema,
        preprocessing={},
        hardware_compatibility=hardware_compatibility,
        required_artifact_format="tensorrt_plan",
    )


def test_triton_config_renderer_separates_multiple_output_tensors() -> None:
    config_text = render_config_pbtxt(
        stable_model_name="rt__detector_v1",
        target_path=TargetPath.REALTIME,
        task_type="detection",
        io_schema={
            "input_tensors": [
                {"name": "input", "data_type": "FP32", "dims": [3, 768, 768], "layout": "CHW"}
            ],
            "output_tensors": [
                {"name": "dets", "data_type": "FP32", "dims": [2]},
                {"name": "labels", "data_type": "INT64", "dims": [2]},
            ],
        },
        preferred_batch_sizes=[],
        max_batch_size=1,
        instance_group_count=1,
        platform="onnxruntime_onnx",
        default_model_filename="model.onnx",
    )

    assert 'name: "dets"' in config_text
    assert 'name: "labels"' in config_text
    assert "  },\n  {\n    name: \"labels\"" in config_text


def test_packaging_rejects_json_disguised_as_model_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "model.onnx"
    artifact.write_text('{"this": "is metadata"}', encoding="utf-8")

    with pytest.raises(ValueError, match="metadata JSON"):
        _validate_model_artifact(artifact, artifact_format="onnx")


def test_stage_release_writes_triton_repository_and_config(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(session, tmp_path=tmp_path, suffix="stage")
        promotion_id = approve_and_request_staging(session, model_version_id=version.id)
        session.commit()

    result = stage_model_release(promotion_id)

    assert result["status"] == "staged"
    with Session(engine, expire_on_commit=False) as session:
        release = session.get(DeploymentRelease, result["deployment_release_id"])

    assert release is not None
    model_root = settings.storage.triton_model_root(release.target_path, release.triton_model_name)
    version_dir = model_root / str(release.triton_model_version)
    config_path = model_root / "config.pbtxt"

    assert version_dir.exists()
    assert (version_dir / "model.plan").exists()
    assert config_path.exists()
    config_text = config_path.read_text(encoding="utf-8")
    assert f'name: "{release.triton_model_name}"' in config_text
    assert 'platform: "tensorrt_plan"' in config_text
    assert "version_policy {" in config_text


def test_stage_release_replaces_stale_shared_config_for_new_version_in_dev(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        version_a = seed_model_version(session, tmp_path=tmp_path, suffix="stage-a")
        promotion_a = approve_and_request_staging(session, model_version_id=version_a.id)
        session.commit()

    stage_a = stage_model_release(promotion_a)
    with Session(engine, expire_on_commit=False) as session:
        release_a = session.get(DeploymentRelease, stage_a["deployment_release_id"])
    assert release_a is not None
    model_root = settings.storage.triton_model_root(release_a.target_path, release_a.triton_model_name)
    config_path = model_root / "config.pbtxt"
    config_path.write_text('name: "stale"\noutput [ { dims: [ -1, -1 ] } ]\n', encoding="utf-8")

    with Session(engine, expire_on_commit=False) as session:
        version_b = seed_model_version(session, tmp_path=tmp_path, suffix="stage-b")
        promotion_b = approve_and_request_staging(session, model_version_id=version_b.id)
        session.commit()

    stage_b = stage_model_release(promotion_b)
    with Session(engine, expire_on_commit=False) as session:
        release_b = session.get(DeploymentRelease, stage_b["deployment_release_id"])

    assert release_b is not None
    assert release_b.triton_model_version == release_a.triton_model_version + 1
    assert not (model_root / str(release_a.triton_model_version)).exists()
    assert (model_root / str(release_b.triton_model_version) / "model.plan").exists()
    assert f'name: "{release_b.triton_model_name}"' in config_path.read_text(encoding="utf-8")


def test_stage_release_replaces_stale_shared_config_for_inactive_model_in_prod(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
) -> None:
    settings.env = EnvironmentName.PROD
    with Session(engine, expire_on_commit=False) as session:
        version_a = seed_model_version(session, tmp_path=tmp_path, suffix="prod-stale-a")
        promotion_a = approve_and_request_staging(session, model_version_id=version_a.id)
        session.commit()

    stage_a = stage_model_release(promotion_a)
    with Session(engine, expire_on_commit=False) as session:
        release_a = session.get(DeploymentRelease, stage_a["deployment_release_id"])
    assert release_a is not None
    model_root = settings.storage.triton_model_root(release_a.target_path, release_a.triton_model_name)
    config_path = model_root / "config.pbtxt"
    config_path.write_text('name: "stale"\noutput [ { dims: [ -1, -1 ] } ]\n', encoding="utf-8")

    with Session(engine, expire_on_commit=False) as session:
        version_b = seed_model_version(session, tmp_path=tmp_path, suffix="prod-stale-b")
        promotion_b = approve_and_request_staging(session, model_version_id=version_b.id)
        session.commit()

    stage_b = stage_model_release(promotion_b)
    with Session(engine, expire_on_commit=False) as session:
        release_b = session.get(DeploymentRelease, stage_b["deployment_release_id"])

    assert release_b is not None
    assert release_b.status == DeploymentReleaseStatus.ACTIVATED.value
    assert release_b.compatibility_report["compatible"] is True
    assert any(
        item["code"] == "config_pbtxt_mismatch" and item["level"] == "warning"
        for item in release_b.compatibility_report["checks"]
    )
    assert not (model_root / str(release_a.triton_model_version)).exists()
    assert (model_root / str(release_b.triton_model_version) / "model.plan").exists()
    assert f'name: "{release_b.triton_model_name}"' in config_path.read_text(encoding="utf-8")


def test_stage_release_blocks_stale_shared_config_for_active_model_in_prod(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
) -> None:
    settings.env = EnvironmentName.PROD
    with Session(engine, expire_on_commit=False) as session:
        version_a = seed_model_version(session, tmp_path=tmp_path, suffix="prod-active-a")
        promotion_a = approve_and_request_staging(session, model_version_id=version_a.id)
        session.commit()

    stage_a = stage_model_release(promotion_a)
    with Session(engine, expire_on_commit=False) as session:
        release_a = session.get(DeploymentRelease, stage_a["deployment_release_id"])
        assert release_a is not None
        session.add(
            DeploymentTarget(
                target_path=release_a.target_path,
                active_release_id=release_a.id,
                previous_release_id=None,
                updated_at=release_a.activated_at,
            )
        )
        session.commit()

    model_root = settings.storage.triton_model_root(release_a.target_path, release_a.triton_model_name)
    config_path = model_root / "config.pbtxt"
    config_path.write_text('name: "stale"\noutput [ { dims: [ -1, -1 ] } ]\n', encoding="utf-8")

    with Session(engine, expire_on_commit=False) as session:
        version_b = seed_model_version(session, tmp_path=tmp_path, suffix="prod-active-b")
        promotion_b = approve_and_request_staging(session, model_version_id=version_b.id)
        session.commit()

    with pytest.raises(ValueError, match="config_pbtxt_mismatch"):
        stage_model_release(promotion_b)

    assert config_path.read_text(encoding="utf-8").startswith('name: "stale"')


def test_stage_release_quarantines_sibling_versions_with_stale_config_metadata_in_dev(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(session, tmp_path=tmp_path, suffix="stage-stale-sibling")
        promotion_id = approve_and_request_staging(session, model_version_id=version.id)
        session.commit()

    stage = stage_model_release(promotion_id)
    with Session(engine, expire_on_commit=False) as session:
        release = session.get(DeploymentRelease, stage["deployment_release_id"])

    assert release is not None
    model_root = settings.storage.triton_model_root(release.target_path, release.triton_model_name)
    stale_version_dir = model_root / str(release.triton_model_version + 1)
    stale_version_dir.mkdir(parents=True)
    (stale_version_dir / "model.plan").write_bytes(b"stale-model")
    (stale_version_dir / "deployment_metadata.json").write_text(
        json.dumps({"variant": {"config_sha256": "old-shared-config"}}, indent=2),
        encoding="utf-8",
    )

    manifest_path = settings.storage.serving_package_root(release.model_version_id) / "manifest.json"
    package_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = TritonRepositoryManager(settings).stage_release(
        target_path=TargetPath(release.target_path),
        stable_model_name=release.triton_model_name,
        triton_model_version=release.triton_model_version,
        package_manifest=package_manifest,
    )

    quarantine_root = settings.storage.quarantine_category_root("triton-repository")
    assert result.version_dir.exists()
    assert not stale_version_dir.exists()
    assert any(path.name.startswith("stale_repository_version") for path in quarantine_root.iterdir())


def test_stage_release_blocks_incompatible_gpu_profile(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(
            session,
            tmp_path=tmp_path,
            suffix="badgpu",
            gpu_profile="different-gpu-profile",
        )
        promotion_id = approve_and_request_staging(session, model_version_id=version.id)
        session.commit()

    with pytest.raises(ValueError, match="gpu_profile_mismatch"):
        stage_model_release(promotion_id)

    with Session(engine, expire_on_commit=False) as session:
        release = session.scalar(
            select(DeploymentRelease).where(DeploymentRelease.model_version_id == version.id)
        )
        target = session.get(DeploymentTarget, "realtime")

    assert release is not None
    assert release.status == DeploymentReleaseStatus.ACTIVATION_FAILED.value
    assert target is None or target.active_release_id is None
    model_root = settings.storage.triton_model_root("realtime", release.triton_model_name)
    assert not (model_root / str(release.triton_model_version)).exists()


def test_stage_release_defers_model_load_until_channel_start(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
    fake_triton_client: FakeTritonClient,
) -> None:
    def _fail_load(_model_name: str) -> None:
        raise RuntimeError("simulated triton load failure")

    fake_triton_client.load_model = _fail_load

    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(session, tmp_path=tmp_path, suffix="devfallback")
        promotion_id = approve_and_request_staging(session, model_version_id=version.id)
        session.commit()

    stage_model_release(promotion_id)

    with Session(engine, expire_on_commit=False) as session:
        release = session.scalar(
            select(DeploymentRelease).where(DeploymentRelease.model_version_id == version.id)
        )

    assert release is not None
    assert release.status == DeploymentReleaseStatus.ACTIVATED.value
    assert release.smoke_report["passed"] is False
    assert release.smoke_report["runtime_validation_pending"] is True
    assert release.smoke_report["package_verified"] is True
    assert fake_triton_client.loaded_models == []


def test_packaging_derives_onnx_schema_and_ultralytics_preprocessing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    onnx_path = tmp_path / "model.onnx"
    onnx_path.write_bytes(b"onnx")
    export_manifest = {
        "trainer_backend": "ultralytics-yolo",
        "preprocessing": {
            "resize": 960,
            "normalize": {"mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225]},
        },
        "io_schema": {
            "input_tensors": [
                {"name": "input__image", "data_type": "FP32", "dims": [3, 960, 960], "layout": "CHW"}
            ],
            "output_tensors": [{"name": "output__predictions", "data_type": "FP32", "dims": ["N", "C"]}],
            "contract_major": 1,
            "schema_digest": "old",
        },
    }
    monkeypatch.setattr(
        "cv_deploy.packaging._onnx_graph_io_schema",
        lambda _path: {
            "input_name": "images",
            "input_shape": [-1, 3, 960, 960],
            "input_data_type": "FP32",
            "outputs": [{"name": "output0", "shape": [-1, 4], "data_type": "FP32"}],
        },
    )

    schema = _deployment_io_schema(
        export_manifest=export_manifest,
        onnx_source=onnx_path,
        class_count=4,
    )
    preprocessing = _deployment_preprocessing(export_manifest)

    assert schema["input_tensors"][0]["name"] == "images"
    assert schema["output_tensors"][0]["name"] == "output0"
    assert schema["output_tensors"][0]["dims"] == [4]
    assert schema["schema_digest"] != "old"
    assert preprocessing["normalize"] == {"mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]}
    assert preprocessing["scale"] == pytest.approx(1 / 255)


def test_packaging_overrides_openmmlab_mmyolo_preprocessing() -> None:
    export_manifest = {
        "trainer_backend": "openmmlab-mmyolo",
        "preprocessing": {
            "resize": 960,
            "normalize": {"mean": [123.675, 116.28, 103.53], "std": [58.395, 57.12, 57.375]},
            "color_format": "rgb",
        },
    }

    preprocessing = _deployment_preprocessing(export_manifest)

    assert preprocessing["resize"] == 960
    assert preprocessing["color_format"] == "rgb"
    assert preprocessing["normalize"] == {
        "mean": [0.0, 0.0, 0.0],
        "std": [255.0, 255.0, 255.0],
    }


def test_packaging_keeps_fixed_serving_shape_for_dynamic_detection_onnx(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    onnx_path = tmp_path / "model.onnx"
    onnx_path.write_bytes(b"onnx")
    export_manifest = {
        "task_type": "detection",
        "io_schema": {
            "input_tensors": [
                {"name": "input", "data_type": "FP32", "dims": [3, 960, 960], "layout": "CHW"}
            ],
            "output_tensors": [
                {"name": "dets", "data_type": "FP32", "dims": [-1, -1]},
                {"name": "labels", "data_type": "INT64", "dims": [-1]},
            ],
            "contract_major": 1,
            "schema_digest": "old",
        },
    }
    monkeypatch.setattr(
        "cv_deploy.packaging._onnx_graph_io_schema",
        lambda _path: {
            "input_name": "input",
            "input_shape": [-1, 3, -1, -1],
            "input_data_type": "FP32",
            "outputs": [
                {"name": "dets", "shape": [-1, -1, -1], "data_type": "FP32"},
                {"name": "labels", "shape": [-1, 2], "data_type": "INT64"},
            ],
        },
    )

    schema = _deployment_io_schema(
        export_manifest=export_manifest,
        onnx_source=onnx_path,
        class_count=2,
    )

    assert schema["input_tensors"][0]["dims"] == [3, 960, 960]
    assert schema["output_tensors"][0]["dims"] == [-1, -1]
    assert schema["output_tensors"][1]["dims"] == [-1]
    assert schema["schema_digest"] != "old"


def test_packaging_normalizes_symbolic_classification_output_when_onnx_schema_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    onnx_path = tmp_path / "model.onnx"
    onnx_path.write_bytes(b"onnx")
    export_manifest = {
        "io_schema": {
            "input_tensors": [
                {"name": "input__image", "data_type": "FP32", "dims": [3, 960, 960], "layout": "CHW"}
            ],
            "output_tensors": [{"name": "output__predictions", "data_type": "FP32", "dims": ["N", "C"]}],
            "contract_major": 1,
            "schema_digest": "old",
        },
    }
    monkeypatch.setattr("cv_deploy.packaging._onnx_graph_io_schema", lambda _path: None)

    schema = _deployment_io_schema(
        export_manifest=export_manifest,
        onnx_source=onnx_path,
        class_count=4,
    )

    assert schema["input_tensors"][0]["dims"] == [3, 960, 960]
    assert schema["output_tensors"][0]["dims"] == [4]
    assert schema["schema_digest"] != "old"


def test_packaging_normalizes_detection_outputs_when_onnx_schema_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    onnx_path = tmp_path / "model.onnx"
    onnx_path.write_bytes(b"onnx")
    export_manifest = {
        "task_type": "detection",
        "io_schema": {
            "input_tensors": [
                {"name": "input", "data_type": "FP32", "dims": [3, 768, 768], "layout": "CHW"}
            ],
            "output_tensors": [
                {"name": "dets", "data_type": "FP32", "dims": [2]},
                {"name": "labels", "data_type": "INT64", "dims": [2]},
            ],
            "contract_major": 1,
            "schema_digest": "old",
        },
    }
    monkeypatch.setattr("cv_deploy.packaging._onnx_graph_io_schema", lambda _path: None)

    schema = _deployment_io_schema(
        export_manifest=export_manifest,
        onnx_source=onnx_path,
        class_count=2,
    )

    assert schema["output_tensors"][0]["dims"] == [-1, -1]
    assert schema["output_tensors"][1]["dims"] == [-1]
    assert schema["schema_digest"] != "old"


def test_rollback_reactivates_previous_release_without_rebuilding(
    engine,
    settings: PlatformSettings,
    tmp_path: Path,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        version_a = seed_model_version(session, tmp_path=tmp_path, suffix="a")
        promotion_a = approve_and_request_staging(session, model_version_id=version_a.id)
        session.commit()

    stage_a = stage_model_release(promotion_a)
    with Session(engine, expire_on_commit=False) as session:
        record_deployment_approval(
            session,
            model_version_id=version_a.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.PRODUCTION.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="promote a",
        )
        create_promotion_request(
            session,
            model_version_id=version_a.id,
            target_path="realtime",
            requested_by="tester",
            requested_state="production",
        )
        session.commit()
    promote_model_release(promotion_a)

    with Session(engine, expire_on_commit=False) as session:
        version_b = seed_model_version(session, tmp_path=tmp_path, suffix="b")
        promotion_b = approve_and_request_staging(session, model_version_id=version_b.id)
        session.commit()

    stage_b = stage_model_release(promotion_b)
    with Session(engine, expire_on_commit=False) as session:
        record_deployment_approval(
            session,
            model_version_id=version_b.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.PRODUCTION.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="promote b",
        )
        create_promotion_request(
            session,
            model_version_id=version_b.id,
            target_path="realtime",
            requested_by="tester",
            requested_state="production",
        )
        session.commit()
    promote_model_release(promotion_b)

    rollback_model_release("realtime")
    with Session(engine, expire_on_commit=False) as session:
        target = session.get(DeploymentTarget, "realtime")

    assert target is not None
    assert target.active_release_id == stage_a["deployment_release_id"]
    assert target.previous_release_id == stage_b["deployment_release_id"]

    realtime_root = settings.storage.triton_repository_root("realtime")
    version_dirs = sorted(str(path) for path in realtime_root.rglob("model.plan"))
    assert len(version_dirs) == 2


def test_deployment_worker_reactivation_does_not_load_without_channel(
    engine,
    tmp_path: Path,
    fake_triton_client: FakeTritonClient,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        version = seed_model_version(session, tmp_path=tmp_path, suffix="serving-toggle")
        promotion_id = approve_and_request_staging(session, model_version_id=version.id)
        session.commit()

    staged = stage_model_release(promotion_id)
    release_id = staged["deployment_release_id"]
    with Session(engine) as session:
        model_name = session.get(DeploymentRelease, release_id).triton_model_name
    unloaded: list[str] = []
    fake_triton_client.repository_index = lambda *, ready_only=False: [
        {"name": model_name, "version": "1", "state": "READY"}
    ]
    fake_triton_client.unload_model = lambda unloaded_name: unloaded.append(unloaded_name)

    deactivated = deactivate_deployment_model(release_id)

    assert deactivated["status"] == DeploymentReleaseStatus.DEACTIVATED.value
    assert unloaded == [model_name]
    with Session(engine, expire_on_commit=False) as session:
        release = session.get(DeploymentRelease, release_id)
    assert release is not None
    assert release.status == DeploymentReleaseStatus.DEACTIVATED.value

    activated = activate_deployment_model(release_id)

    assert activated["status"] == DeploymentReleaseStatus.ACTIVATED.value
    assert fake_triton_client.loaded_models == []
    with Session(engine, expire_on_commit=False) as session:
        release = session.get(DeploymentRelease, release_id)
    assert release is not None
    assert release.status == DeploymentReleaseStatus.ACTIVATED.value


def seed_model_version(
    session: Session,
    *,
    tmp_path: Path,
    suffix: str,
    gpu_profile: str = "nvidia-rtx-4500-ada",
    include_tensorrt: bool = True,
):
    dataset = Dataset(
        id=f"dst_{suffix}",
        slug=f"deployment-{suffix}",
        name=f"Deployment Dataset {suffix}",
        task_type=TaskType.CLASSIFICATION.value,
        label_schema_version="1",
        status=DatasetStatus.SNAPSHOTTED.value,
        created_by="tester",
    )
    snapshot = DatasetSnapshot(
        id=f"dss_deploy_{suffix}",
        dataset_id=dataset.id,
        label_project_id=None,
        snapshot_no=1,
        status=SnapshotStatus.READY.value,
        item_count=24,
        annotation_count=24,
        manifest_uri=str(tmp_path / f"snapshot_{suffix}" / "snapshot_manifest.json"),
        content_sha256=f"checksum-{suffix}",
        dvc_rev=f"dvc-{suffix}",
        created_by="tester",
        export_format="JSON",
        label_schema_version="1",
        split_metadata={},
        validation_report={},
        validation_status="passed",
        statistics={"item_count": 24},
        idempotency_key=f"snapshot-key-{suffix}",
    )
    session.add_all([dataset, snapshot])
    session.flush()

    job = create_training_job_request(
        session,
        payload=TrainingJobCreateRequest.model_validate(
            {
                "model_family_slug": "widget-inspection-cls",
                "dataset_snapshot_id": snapshot.id,
                "task_type": "classification",
                "architecture": architecture_summary(
                    task_type=TaskType.CLASSIFICATION,
                    family="resnet",
                    version="v1",
                    size="18",
                ).model_dump(mode="json"),
                "recipe_name": "baseline_cls",
                "recipe_version": "1.0.0",
                "max_epochs": 5,
                "checkpoint_interval_steps": 5,
                "parameter_overrides": {"epochs": 5},
                "best_checkpoint_rule": {
                    "metric_name": "accuracy_top1",
                    "mode": "max",
                    "phase": "evaluation",
                    "minimum_epoch": 1,
                },
                "promotion_criteria": [
                    {
                        "metric_name": "accuracy_top1",
                        "comparison": "gte",
                        "threshold": 0.9,
                        "phase": "evaluation",
                    }
                ],
                "allow_duplicate_snapshot_training": False,
            }
        ),
        requested_by="tester",
        source_service="test-suite",
        idempotency_key=f"training-{suffix}",
    )

    artifact_root = tmp_path / f"artifacts_{suffix}"
    artifact_root.mkdir(parents=True, exist_ok=True)
    onnx_path = artifact_root / "model.onnx"
    plan_path = artifact_root / "model.plan"
    artifact_manifest_path = artifact_root / "artifact_manifest.json"
    export_manifest_path = artifact_root / "export_manifest.json"

    onnx_path.write_bytes(b"onnx-binary-placeholder-for-packaging-tests")
    if include_tensorrt:
        plan_path.write_bytes(b"tensorrt-binary-placeholder-for-packaging-tests")

    artifact_manifest = {
        "training_job_id": job.id,
        "artifacts": {
            "onnx": {"path": str(onnx_path), "sha256": f"onnx-{suffix}"},
        },
    }
    if include_tensorrt:
        artifact_manifest["artifacts"]["tensorrt"] = {
            "path": str(plan_path),
            "sha256": f"plan-{suffix}",
        }
    export_manifest = {
        "training_job_id": job.id,
        "model_family_slug": job.model_family_slug,
        "task_type": "classification",
        "recipe_name": "baseline_cls",
        "recipe_version": "1.0.0",
        "class_map_version": f"{snapshot.id}:classes",
        "class_map_sha256": f"class-map-{suffix}",
        "class_count": 2,
        "confidence_threshold_default": 0.5,
        "preprocessing": {"resize": {"width": 224, "height": 224}},
        "postprocessing": {
            "class_map": ["accepted", "rework"],
            "confidence_threshold_default": 0.5,
        },
        "nms": {"enabled": False},
        "io_schema": {
            "input_tensors": [
                {
                    "name": "input__image",
                    "data_type": "fp32",
                    "dims": [3, 224, 224],
                    "layout": "NCHW",
                }
            ],
            "output_tensors": [
                {
                    "name": "output__probabilities",
                    "data_type": "fp32",
                    "dims": [2],
                }
            ],
            "input_schema_ref": "contracts/classification/input.v1.json",
            "output_schema_ref": "contracts/classification/output.v1.json",
            "contract_major": 1,
            "schema_digest": f"schema-{suffix}",
        },
        "hardware_compatibility": {
            "accelerator": "nvidia_gpu",
            "target_gpu_profile": gpu_profile,
            "min_compute_capability": "8.9",
            "cuda_version": "12.4",
            "tensorrt_version": "10.0",
            "precision": "fp16",
            "max_batch_size": {"realtime": 1, "batch": 64},
            "validated_devices": ["cuda:0"],
        },
        "onnx": {
            "uri": str(onnx_path),
            "sha256": f"onnx-{suffix}",
            "size_bytes": onnx_path.stat().st_size,
            "format": "onnx",
            "file_name": onnx_path.name,
        },
        "tensorrt": None,
        "build_manifest": {
            "stage": "deployment",
            "precision": "fp16",
            "workspace_size_mb": 4096,
        },
        "trainer_mode": "real",
        "trainer_backend": "unit-test-backend",
        "artifact_validation": {
            "status": "passed",
            "checks": {"checkpoint_valid": True, "onnx_valid": True},
            "details": {"checkpoint": {"size_bytes": 1024}, "onnx": {"size_bytes": onnx_path.stat().st_size}},
        },
    }
    if include_tensorrt:
        export_manifest["tensorrt"] = {
            "uri": str(plan_path),
            "sha256": f"plan-{suffix}",
            "size_bytes": plan_path.stat().st_size,
            "format": "tensorrt_plan",
            "file_name": plan_path.name,
        }
    artifact_manifest_path.write_text(json.dumps(artifact_manifest, indent=2), encoding="utf-8")
    export_manifest_path.write_text(json.dumps(export_manifest, indent=2), encoding="utf-8")

    return register_trained_model_version(
        session,
        training_job_id=job.id,
        metrics_summary={"accuracy_top1": 0.95, "f1_macro": 0.94},
        evaluation_summary={
            "primary_metric_name": "accuracy_top1",
            "primary_metric_value": 0.95,
            "metrics": {"accuracy_top1": 0.95, "f1_macro": 0.94},
            "promotion_ready": True,
            "promotion_results": [],
        },
        artifact_manifest_uri=str(artifact_manifest_path),
        artifact_manifest=artifact_manifest,
        export_manifest_uri=str(export_manifest_path),
        export_manifest=export_manifest,
        onnx_uri=str(onnx_path),
        tensorrt_uri=str(plan_path) if include_tensorrt else None,
        dvc_rev=snapshot.dvc_rev,
        dvc_references={"bundle_dvc_file": f"registry/{job.id}.dvc"},
        reproducibility_metadata={"code_revision": "git-test-rev"},
        code_revision="git-test-rev",
        mlflow_run_id=f"mlf-{suffix}",
        mlflow_registered_model=job.model_family_slug,
        mlflow_model_version="1",
        alias_names=["latest-trained", "candidate"],
        registry_tags={
            "cv_platform.lifecycle": "trained",
            "cv_platform.trainer_mode": "real",
            "cv_platform.trainer_backend": "unit-test-backend",
        },
        immutable_metadata={
            "dataset_snapshot_id": snapshot.id,
            "training_job_id": job.id,
            "trainer_mode": "real",
            "trainer_backend": "unit-test-backend",
            "artifact_validation": {
                "status": "passed",
                "checks": {"checkpoint_valid": True, "onnx_valid": True},
                "details": {"checkpoint": {"size_bytes": 1024}, "onnx": {"size_bytes": onnx_path.stat().st_size}},
            },
        },
        immutable_metadata_sha256=f"sha-{suffix}",
    )


def approve_and_request_staging(session: Session, *, model_version_id: str) -> str:
    record_deployment_approval(
        session,
        model_version_id=model_version_id,
        target_path="realtime",
        approval_scope=DeploymentApprovalScope.STAGING.value,
        status=DeploymentApprovalStatus.APPROVED.value,
        decided_by="tester",
        notes="stage",
    )
    return create_promotion_request(
        session,
        model_version_id=model_version_id,
        target_path="realtime",
        requested_by="tester",
        requested_state="staged",
    )
