from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from batch_worker import jobs as batch_jobs
from cv_contracts import ModelArchitectureSummary
from cv_core.settings import (
    ApiKeyBootstrapConfig,
    ApiKeyConfig,
    InferenceLimitsConfig,
    PlatformSettings,
    ServiceRuntimeConfig,
    StorageRoots,
)
from cv_db import Base
from cv_db.models import DeploymentRelease, DeploymentTarget, ModelFamily, ModelVersion, RuntimeChannel, RuntimeExecution
from cv_db.session import _session_factories
from cv_status import DeploymentReleaseStatus, TargetPath, TaskType
from fastapi.testclient import TestClient
from inference_gateway.dependencies import (
    RequestIdentity,
    _is_internal_runtime_identity,
    require_runtime_inference,
)
from inference_gateway.main_batch import app as batch_app
from inference_gateway.main_realtime import app as realtime_app
from inference_gateway.runtime.admission import (
    operator_direct_inference,
    runtime_inference_releases,
)
from inference_gateway.runtime.public_service import (
    PublicBatchGatewayService,
    PublicRealtimeGatewayService,
)
from inference_gateway.runtime.resolver import ReleaseContext
from inference_gateway.runtime.service import (
    RealtimeGatewayService,
    TritonInferenceBackend,
    _BackendPhaseTiming,
    _OutputTensor,
    _anomaly_artifact_path,
    _backend_phase_timing,
    _classification_tensor,
    _classification_labels_from_outputs,
    _classification_labels_from_scores,
    _classification_image_size,
    _deployment_artifact_path,
    _detection_tensor,
    _detection_predictions_from_outputs,
    _rows_from_model_output,
    _segmentation_predictions_from_outputs,
    _tensor_byte_size,
    _triton_binary_output_tensors,
    _triton_model_statistics,
    _triton_response_output_specs,
    _triton_shared_memory_layout,
    _uses_simulated_inference,
)
from cv_utils import PlatformError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


class FakeQueue:
    def __init__(self, *_args, **_kwargs) -> None:
        self.enqueued = ENQUEUED

    def enqueue(self, job_name: str, **kwargs) -> None:
        self.enqueued.append((job_name, kwargs))


ENQUEUED: list[tuple[str, dict[str, object]]] = []
REALTIME_AUTH_HEADERS = {"Authorization": "Bearer rt-client.rt-secret"}
BATCH_AUTH_HEADERS = {"Authorization": "Bearer batch-client.batch-secret"}


def test_only_internal_runtime_identity_bypasses_realtime_rate_limit() -> None:
    internal = RequestIdentity(
        auth_mode="machine",
        principal_id="ybb-inference-runtime",
        machine_subject_type="service",
        machine_subject="runtime-inference-client",
    )
    external = RequestIdentity(
        auth_mode="machine",
        principal_id="managed-client",
        machine_subject_type="application",
        machine_subject="runtime-inference-client",
    )

    assert _is_internal_runtime_identity(internal) is True
    assert _is_internal_runtime_identity(external) is False


def test_browser_session_can_bind_an_active_release_for_direct_operator_inference(
    engine,
    tmp_path: Path,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        release_id = seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="operator-test-classifier",
            task_type=TaskType.CLASSIFICATION,
        )
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )
    request = SimpleNamespace(
        method="POST",
        query_params={"deployment_release_id": release_id},
        headers={},
    )
    identity = RequestIdentity(auth_mode="session", principal_id="operator")

    async def assert_direct_binding() -> None:
        dependency = require_runtime_inference(request, settings, identity)
        await anext(dependency)
        assert runtime_inference_releases.get() == frozenset({release_id})
        assert operator_direct_inference.get() is True
        with pytest.raises(StopAsyncIteration):
            await anext(dependency)

    asyncio.run(assert_direct_binding())


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    _session_factories.clear()
    yield engine
    _session_factories.clear()


def build_settings(
    service_name: str,
    tmp_path: Path,
    *,
    api_keys: list[tuple[str, str, list[str], str, str]] | None = None,
) -> PlatformSettings:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(parents=True, exist_ok=True)
    bootstrap_keys: list[ApiKeyBootstrapConfig] = []
    if service_name == "inference-gateway-rt" and api_keys:
        api_keys = [*api_keys, ("ybb-inference-runtime", "runtime-secret", ["realtime.predict"],
                               "service", "runtime-inference-client")]
    for index, (key_id, secret_value, scopes, subject_type, subject_id) in enumerate(api_keys or []):
        secret_path = secret_root / f"api_key_{index}_{key_id}"
        secret_path.write_text(secret_value, encoding="utf-8")
        bootstrap_keys.append(
            ApiKeyBootstrapConfig(
                key_id=key_id,
                subject_type=subject_type,
                subject_id=subject_id,
                scopes=scopes,
                secret_file=str(secret_path),
            )
        )
    return PlatformSettings(
        service=ServiceRuntimeConfig(name=service_name),
        storage=StorageRoots(
            state_root=str(tmp_path / "state"),
            training_root=str(tmp_path / "state" / "training"),
            serving_root=str(tmp_path / "state" / "serving"),
            audit_root=str(tmp_path / "state" / "audit"),
            logs_root=str(tmp_path / "state" / "logs"),
        ),
        api_keys=ApiKeyConfig(bootstrap_keys=bootstrap_keys),
        inference_limits=InferenceLimitsConfig(
            realtime_timeout_ms=750,
            batch_timeout_seconds=30,
            batch_chunk_size=4,
            realtime_max_concurrency=1,
            batch_max_concurrency=1,
            metrics_window_size=50,
            routing_cache_ttl_seconds=1,
            warm_ttl_seconds=60,
        ),
    )


def configure_session_factories(engine) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _session_factories["inference-gateway-rt"] = factory
    _session_factories["inference-gateway-batch"] = factory
    _session_factories["batch-worker"] = factory


def runtime_auth_headers(engine) -> dict[str, str]:
    """Seed a started channel so backend/transport tests exercise real admission."""
    with Session(engine) as session:
        if session.get(RuntimeExecution, "rex_gateway_test") is None:
            session.add(RuntimeChannel(
                id="chn_gateway_test", slug="gateway-test", name="Gateway test",
                technical_place="test", desired_state="running", observed_state="running",
                pipeline_version_id="plv_gateway_test", active_runtime_execution_id="rex_gateway_test",
            ))
            session.add(RuntimeExecution(
                id="rex_gateway_test", channel_id="chn_gateway_test",
                pipeline_version_id="plv_gateway_test", executor_service="realtime-executor",
                status="running", actual_state="running", desired_state="running",
                runtime_plan={"binding_signature": {"model_bindings": [
                    {"deployment_release_id": release.id}
                    for release in session.query(DeploymentRelease).all()
                ]}},
            ))
            session.commit()
    return {
        "Authorization": "Bearer ybb-inference-runtime.runtime-secret",
        "X-Runtime-Execution-Id": "rex_gateway_test",
        "X-Runtime-Pipeline-Version-Id": "plv_gateway_test",
    }


def seed_release(
    session: Session,
    *,
    tmp_path: Path,
    target_path: TargetPath,
    task_type: TaskType,
    release_suffix: str,
    active: bool,
    previous_release_id: str | None = None,
) -> str:
    family = ModelFamily(
        id=f"mfy_{release_suffix}",
        slug=f"{release_suffix}-{task_type.value}",
        task_type=task_type.value,
        contract_major=1,
        input_schema_ref=f"contracts/{task_type.value}/input.v1.json",
        output_schema_ref=f"contracts/{task_type.value}/output.v1.json",
        active=True,
    )
    labels = ["accepted", "rework"] if task_type == TaskType.CLASSIFICATION else ["defect", "scratch"]
    package_root = tmp_path / f"package_{release_suffix}"
    class_map_path = package_root / "source" / "class_map.json"
    class_map_path.parent.mkdir(parents=True, exist_ok=True)
    class_map_path.write_text('["' + '","'.join(labels) + '"]', encoding="utf-8")
    version = ModelVersion(
        id=f"mvr_{release_suffix}",
        model_family_id=family.id,
        version_no=1,
        status="production" if active else "staged",
        serving_manifest_uri=str(package_root / "manifest.json"),
        serving_manifest={
            "package_root": str(package_root),
            "class_count": len(labels),
            "confidence_threshold_default": 0.5,
            "compatible_targets": [target_path.value],
            "preprocessing": {"resize": {"width": 224, "height": 224}},
            "postprocessing": {"class_map": labels},
            "io_schema": {
                "input_schema_ref": family.input_schema_ref,
                "output_schema_ref": family.output_schema_ref,
                "contract_major": 1,
                "schema_digest": f"schema-{release_suffix}",
            },
            "hardware_compatibility": {
                "max_batch_size": {"realtime": 1, "batch": 8},
            },
            "source_manifests": {
                "class_map": {"path": "source/class_map.json"},
            },
        },
    )
    release = DeploymentRelease(
        id=f"rel_{release_suffix}",
        target_path=target_path.value,
        model_family_id=family.id,
        model_version_id=version.id,
        triton_model_name=f"{target_path.value}_{release_suffix}",
        triton_model_version=1,
        status="activated",
    )
    session.add_all([family, version, release])
    session.flush()
    target = session.get(DeploymentTarget, target_path.value)
    if target is None:
        target = DeploymentTarget(
            target_path=target_path.value,
            active_release_id=release.id if active else None,
            previous_release_id=previous_release_id,
            updated_at=datetime.now(timezone.utc),
        )
        session.add(target)
    if active:
        target.active_release_id = release.id
        target.previous_release_id = previous_release_id
        target.updated_at = datetime.now(timezone.utc)
    elif previous_release_id is None:
        current = session.get(DeploymentTarget, target_path.value)
        if current is not None:
            current.previous_release_id = release.id
            current.updated_at = datetime.now(timezone.utc)
    session.flush()
    return release.id


def seed_realtime_releases(session: Session, *, tmp_path: Path) -> tuple[str, str]:
    active_release_id = seed_release(
        session,
        tmp_path=tmp_path,
        target_path=TargetPath.REALTIME,
        task_type=TaskType.CLASSIFICATION,
        release_suffix="rt_primary",
        active=True,
    )
    comparison_release_id = seed_release(
        session,
        tmp_path=tmp_path,
        target_path=TargetPath.REALTIME,
        task_type=TaskType.DETECTION,
        release_suffix="rt_detect",
        active=False,
    )
    target = session.get(DeploymentTarget, TargetPath.REALTIME.value)
    assert target is not None
    target.previous_release_id = comparison_release_id
    session.flush()
    return active_release_id, comparison_release_id


def seed_batch_release(session: Session, *, tmp_path: Path) -> str:
    return seed_release(
        session,
        tmp_path=tmp_path,
        target_path=TargetPath.BATCH,
        task_type=TaskType.CLASSIFICATION,
        release_suffix="batch_primary",
        active=True,
    )


def test_classification_scores_are_ranked_from_model_outputs() -> None:
    route = _classification_route(
        manifest={},
        class_labels=["bad", "good", "unclear"],
    )

    labels = _classification_labels_from_scores(route=route, scores=[0.02, 0.95, 0.03], top_k=2)

    assert [(item.label, item.rank) for item in labels] == [("good", 1), ("unclear", 2)]
    assert labels[0].score == pytest.approx(0.95)


def test_classification_topk_outputs_map_labels_by_index() -> None:
    route = _classification_route(
        manifest={},
        class_labels=["LKW wartend", "LKW wird beladen", "keine Verladung"],
    )
    outputs = {
        "topk_scores": _OutputTensor(
            name="topk_scores",
            shape=[1, 3],
            values=[0.89, 0.109, 0.001],
        ),
        "topk_indices": _OutputTensor(
            name="topk_indices",
            shape=[1, 3],
            values=[1.0, 2.0, 0.0],
        ),
    }

    labels = _classification_labels_from_outputs(
        route=route,
        outputs=outputs,
        fallback_output_name="topk_scores",
        top_k=3,
    )

    assert [(item.label, item.rank) for item in labels] == [
        ("LKW wird beladen", 1),
        ("keine Verladung", 2),
        ("LKW wartend", 3),
    ]
    assert labels[0].score == pytest.approx(0.89)


def test_real_artifact_routes_do_not_use_hash_simulation(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    artifact = package_root / "source" / "model.onnx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"onnx")

    real_route = _classification_route(
        manifest={
            "package_root": str(package_root),
            "source_manifests": {
                "deployment_artifact": {"path": "source/model.onnx", "format": "onnx"}
            },
        }
    )
    fixture_route = _classification_route(
        manifest={"source_manifests": {"class_map": {"path": "source/class_map.json"}}}
    )

    assert _uses_simulated_inference(real_route) is False
    assert _uses_simulated_inference(fixture_route) is True


def test_capabilities_keep_deployed_models_selectable_before_channel_start(tmp_path: Path) -> None:
    settings = build_settings("inference-gateway-rt", tmp_path)
    service = RealtimeGatewayService(
        settings,
        service_name="inference-gateway-rt",
        target_path=TargetPath.REALTIME,
    )
    real_manifest = {
        "source_manifests": {
            "deployment_artifact": {"path": "source/model.onnx", "format": "onnx"}
        },
        "compatible_targets": ["realtime"],
        "hardware_compatibility": {"max_batch_size": {"realtime": 1}},
    }
    ready_route = replace(
        _classification_route(manifest=real_manifest),
        deployment_release_id="rel_ready",
        triton_model_name="rt__ready",
        triton_model_version=1,
    )
    unready_route = replace(
        _classification_route(manifest=real_manifest),
        deployment_release_id="rel_unready",
        triton_model_name="rt__unready",
        triton_model_version=1,
    )
    service.resolver.list_release_contexts = lambda *, target_path: [unready_route, ready_route]
    service.backend.ready_model_versions = lambda: {("rt__ready", 1)}

    response = asyncio.run(service.list_capabilities())

    assert [item.deployment_release_id for item in response.items] == ["rel_unready", "rel_ready"]


def test_status_probe_does_not_block_gateway_event_loop(tmp_path: Path) -> None:
    settings = build_settings("inference-gateway-rt", tmp_path)
    service = RealtimeGatewayService(
        settings,
        service_name="inference-gateway-rt",
        target_path=TargetPath.REALTIME,
    )

    def slow_statistics() -> dict:
        time.sleep(0.25)
        return {}

    service.backend.model_statistics = slow_statistics  # type: ignore[method-assign]
    service._servable_release_contexts = lambda: []  # type: ignore[method-assign]

    async def run_probe_with_heartbeat() -> float:
        task = asyncio.create_task(service.list_status())
        started_at = time.perf_counter()
        await asyncio.sleep(0.02)
        heartbeat_delay = time.perf_counter() - started_at
        await task
        return heartbeat_delay

    assert asyncio.run(run_probe_with_heartbeat()) < 0.1


def test_dynamic_input_dims_use_preprocessing_resize_for_image_size() -> None:
    route = _classification_route(
        manifest={
            "io_schema": {"input_tensors": [{"dims": [3, -1, -1]}]},
            "preprocessing": {"resize": 960},
        }
    )

    assert _classification_image_size(route) == (960, 960)


def test_classification_tensor_uses_rgb_channel_order() -> None:
    Image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), (10, 20, 30)).save(buffer, format="PNG")
    route = _classification_route(
        manifest={"io_schema": {"input_tensors": [{"dims": [3, 1, 1]}]}},
    )

    tensor = _classification_tensor(buffer.getvalue(), route)

    assert tensor.shape == (1, 3, 1, 1)
    assert tensor[0, :, 0, 0].tolist() == pytest.approx(
        [10 / 255.0, 20 / 255.0, 30 / 255.0],
        abs=1e-6,
    )


def test_detection_outputs_are_decoded_from_model_tensors() -> None:
    route = _classification_route(
        task_type=TaskType.DETECTION,
        manifest={"io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]}},
        class_labels=["hand", "background"],
    )

    detections = _detection_predictions_from_outputs(
        route=route,
        outputs={
            "output__boxes": _OutputTensor(
                name="output__boxes",
                shape=[2, 4],
                values=[0.1, 0.2, 0.4, 0.5, 10, 20, 40, 50],
            ),
            "output__scores": _OutputTensor(
                name="output__scores",
                shape=[2],
                values=[0.91, 0.12],
            ),
            "output__labels": _OutputTensor(
                name="output__labels",
                shape=[2],
                values=[0, 1],
            ),
        },
        max_detections=5,
        score_threshold=0.5,
    )

    assert len(detections) == 1
    assert detections[0].label == "hand"
    assert detections[0].box.x_min == pytest.approx(0.1)
    assert detections[0].box.x_max == pytest.approx(0.4)


def test_ultralytics_two_class_raw_detection_output_uses_xywh_and_class_scores() -> None:
    route = _classification_route(
        task_type=TaskType.DETECTION,
        manifest={
            "io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]},
            "nms": {"enabled": False},
        },
        class_labels=["Eine Spur", "Zwei Spuren"],
    )
    rows = [
        [50.0, 40.0, 80.0, 5.0, 0.52, 0.20],
        [50.0, 70.0, 60.0, 8.0, 0.10, 0.90],
        *([[0.0] * 6] * 8),
    ]
    values = [
        row[channel]
        for channel in range(6)
        for row in rows
    ]

    detections = _detection_predictions_from_outputs(
        route=route,
        outputs={"output0": _OutputTensor("output0", [1, 6, 10], values)},
        max_detections=10,
        score_threshold=0.5,
    )

    assert [(item.label, item.score) for item in detections] == [
        ("Zwei Spuren", 0.9),
        ("Eine Spur", 0.52),
    ]
    assert detections[0].box.x_min == pytest.approx(0.2)
    assert detections[0].box.y_min == pytest.approx(0.66)
    assert detections[0].box.x_max == pytest.approx(0.8)
    assert detections[0].box.y_max == pytest.approx(0.74)
    assert detections[1].box.x_min == pytest.approx(0.1)
    assert detections[1].box.y_min == pytest.approx(0.375)
    assert detections[1].box.x_max == pytest.approx(0.9)
    assert detections[1].box.y_max == pytest.approx(0.425)


def test_ultralytics_one_class_raw_detection_output_is_not_discarded() -> None:
    route = _classification_route(
        task_type=TaskType.DETECTION,
        manifest={
            "io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]},
        },
        class_labels=["Eine Spur"],
    )
    rows = [[50.0, 40.0, 80.0, 5.0, 0.8], *([[0.0] * 5] * 5)]
    values = [row[channel] for channel in range(5) for row in rows]

    detections = _detection_predictions_from_outputs(
        route=route,
        outputs={"output0": _OutputTensor("output0", [1, 5, 6], values)},
        max_detections=10,
        score_threshold=0.5,
    )

    assert len(detections) == 1
    assert detections[0].label == "Eine Spur"
    assert detections[0].box.y_min == pytest.approx(0.375)
    assert detections[0].box.y_max == pytest.approx(0.425)


def test_ultralytics_post_nms_detection_output_remains_xyxy() -> None:
    route = _classification_route(
        task_type=TaskType.DETECTION,
        manifest={
            "io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]},
        },
        class_labels=["Eine Spur", "Zwei Spuren"],
    )
    rows = [
        [10.0, 20.0, 90.0, 30.0, 0.8, 1.0],
        *([[0.0] * 6] * 9),
    ]

    detections = _detection_predictions_from_outputs(
        route=route,
        outputs={
            "output0": _OutputTensor(
                "output0",
                [1, 10, 6],
                [value for row in rows for value in row],
            )
        },
        max_detections=10,
        score_threshold=0.5,
    )

    assert len(detections) == 1
    assert detections[0].label == "Zwei Spuren"
    assert detections[0].box.x_min == pytest.approx(0.1)
    assert detections[0].box.y_min == pytest.approx(0.2)
    assert detections[0].box.x_max == pytest.approx(0.9)
    assert detections[0].box.y_max == pytest.approx(0.3)


def test_detection_nms_keeps_overlapping_different_classes() -> None:
    route = _classification_route(
        task_type=TaskType.DETECTION,
        manifest={
            "io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]},
            "nms": {"enabled": True, "iou_threshold": 0.5},
        },
        class_labels=["Eine Spur", "Zwei Spuren"],
    )
    rows = [
        [50.0, 50.0, 80.0, 10.0, 0.9, 0.1],
        [50.0, 50.0, 80.0, 10.0, 0.1, 0.8],
        *([[0.0] * 6] * 8),
    ]
    values = [row[channel] for channel in range(6) for row in rows]

    detections = _detection_predictions_from_outputs(
        route=route,
        outputs={"output0": _OutputTensor("output0", [1, 6, 10], values)},
        max_detections=10,
        score_threshold=0.5,
    )

    assert [(item.label, item.score) for item in detections] == [
        ("Eine Spur", 0.9),
        ("Zwei Spuren", 0.8),
    ]


def test_detection_letterbox_is_applied_and_removed_from_returned_coordinates() -> None:
    Image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    Image.new("RGB", (200, 100), (255, 0, 0)).save(buffer, format="PNG")
    route = _classification_route(
        task_type=TaskType.DETECTION,
        manifest={
            "io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]},
            "preprocessing": {
                "resize": 100,
                "resize_mode": "letterbox",
                "letterbox": {"enabled": True, "padding_value": 114, "center": True},
            },
        },
        class_labels=["Eine Spur", "Zwei Spuren"],
    )

    tensor, transform = _detection_tensor(buffer.getvalue(), route)
    rows = [[50.0, 50.0, 80.0, 20.0, 0.9, 0.1], *([[0.0] * 6] * 9)]
    values = [row[channel] for channel in range(6) for row in rows]
    detections = _detection_predictions_from_outputs(
        route=route,
        outputs={"output0": _OutputTensor("output0", [1, 6, 10], values)},
        max_detections=10,
        score_threshold=0.5,
        preprocessing_transform=transform,
    )

    assert tensor.shape == (1, 3, 100, 100)
    assert transform.resized_width == 100
    assert transform.resized_height == 50
    assert transform.pad_left == 0
    assert transform.pad_top == 25
    assert tensor[0, :, 0, 0].tolist() == pytest.approx([114 / 255.0] * 3)
    assert tensor[0, :, 50, 50].tolist() == pytest.approx([1.0, 0.0, 0.0])
    assert len(detections) == 1
    assert detections[0].box.x_min == pytest.approx(0.1)
    assert detections[0].box.y_min == pytest.approx(0.3)
    assert detections[0].box.x_max == pytest.approx(0.9)
    assert detections[0].box.y_max == pytest.approx(0.7)


def test_segmentation_outputs_are_decoded_from_model_tensors() -> None:
    route = _classification_route(
        task_type=TaskType.SEGMENTATION,
        manifest={"io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]}},
        class_labels=["hand", "background"],
    )

    segments = _segmentation_predictions_from_outputs(
        route=route,
        outputs={
            "output__segments": _OutputTensor(
                name="output__segments",
                shape=[1, 4, 2],
                values=[10, 10, 40, 10, 40, 50, 10, 50],
            ),
            "output__scores": _OutputTensor(
                name="output__scores",
                shape=[1],
                values=[0.88],
            ),
            "output__labels": _OutputTensor(
                name="output__labels",
                shape=[1],
                values=[0],
            ),
        },
        max_segments=5,
        score_threshold=0.5,
    )

    assert len(segments) == 1
    assert segments[0].label == "hand"
    assert segments[0].polygon[0].x == pytest.approx(0.1)
    assert segments[0].polygon[2].y == pytest.approx(0.5)


def test_binary_triton_outputs_remain_numpy_backed() -> None:
    source = np.arange(48, dtype=np.float32)
    tensors = _triton_binary_output_tensors(
        {
            "outputs": [
                {
                    "name": "output0",
                    "shape": [1, 6, 8],
                    "datatype": "FP32",
                    "parameters": {"binary_data_size": source.nbytes},
                }
            ]
        },
        source.tobytes(),
    )

    tensor = tensors["output0"]
    assert isinstance(tensor.values, np.ndarray)
    assert tensor.values.flags.owndata is False
    np.testing.assert_array_equal(tensor.values, source)

    rows = _rows_from_model_output(tensor)
    assert isinstance(rows, np.ndarray)
    assert rows.shape == (8, 6)
    assert np.shares_memory(rows, tensor.values)
    np.testing.assert_array_equal(rows[0], source.reshape(6, 8)[:, 0])


def test_triton_model_statistics_report_average_backend_phases() -> None:
    statistics = _triton_model_statistics(
        {
            "model_stats": [
                {
                    "name": "segmenter",
                    "version": "1",
                    "inference_count": "4",
                    "execution_count": "4",
                    "inference_stats": {
                        "success": {"count": "4", "ns": "400000000"},
                        "queue": {"count": "4", "ns": "8000000"},
                        "compute_input": {"count": "4", "ns": "40000000"},
                        "compute_infer": {"count": "4", "ns": "320000000"},
                        "compute_output": {"count": "4", "ns": "32000000"},
                    },
                }
            ]
        }
    )

    model = statistics[("segmenter", 1)]
    assert model["inference_count"] == 4
    assert model["queue_avg_ms"] == 2.0
    assert model["compute_input_avg_ms"] == 10.0
    assert model["compute_infer_avg_ms"] == 80.0
    assert model["compute_output_avg_ms"] == 8.0


def test_triton_shared_memory_layout_reserves_static_model_tensors() -> None:
    route = _classification_route(
        task_type=TaskType.SEGMENTATION,
        manifest={
            "io_schema": {
                "input_tensors": [
                    {"name": "images", "data_type": "FP32", "dims": [3, 960, 960]}
                ],
                "output_tensors": [
                    {"name": "output0", "data_type": "FP32", "dims": [39, 18900]},
                    {"name": "output1", "data_type": "FP32", "dims": [32, 240, 240]},
                ],
            }
        },
        class_labels=["defect"],
    )

    layout = _triton_shared_memory_layout(
        route=route,
        input_name="images",
        input_shape=[1, 3, 960, 960],
        input_byte_size=1 * 3 * 960 * 960 * 4,
    )

    assert layout is not None
    assert [item.name for item in layout] == ["images", "output0", "output1"]
    assert layout[1].byte_size == 39 * 18900 * 4
    assert layout[2].byte_size == 32 * 240 * 240 * 4
    assert layout[1].offset % 64 == 0
    assert layout[2].offset % 64 == 0
    assert _tensor_byte_size([32, 240, 240], "TYPE_FP16") == 32 * 240 * 240 * 2


def test_triton_shared_memory_layout_falls_back_for_dynamic_outputs() -> None:
    route = _classification_route(
        task_type=TaskType.DETECTION,
        manifest={
            "io_schema": {
                "input_tensors": [
                    {"name": "images", "data_type": "FP32", "dims": [3, 640, 640]}
                ],
                "output_tensors": [
                    {"name": "detections", "data_type": "FP32", "dims": [-1, 6]}
                ],
            }
        },
        class_labels=["object"],
    )

    assert (
        _triton_shared_memory_layout(
            route=route,
            input_name="images",
            input_shape=[1, 3, 640, 640],
            input_byte_size=1 * 3 * 640 * 640 * 4,
        )
        is None
    )


def test_triton_binary_response_exposes_actual_shapes_for_shared_memory_cache() -> None:
    class Response:
        def __init__(self) -> None:
            header = json.dumps(
                {
                    "outputs": [
                        {
                            "name": "output0",
                            "shape": [1, 39, 18900],
                            "datatype": "FP32",
                            "parameters": {"binary_data_size": 39 * 18900 * 4},
                        }
                    ]
                }
            ).encode("utf-8")
            self.headers = {"Inference-Header-Content-Length": str(len(header))}
            self.content = header + bytes(39 * 18900 * 4)

    specs = _triton_response_output_specs(Response())

    assert specs == [
        {
            "name": "output0",
            "data_type": "FP32",
            "dims": [1, 39, 18900],
        }
    ]


def test_triton_shared_memory_request_exchanges_tensors_without_binary_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiprocessing import shared_memory

    class FakeResponse:
        def __init__(self, payload: dict[str, object] | None = None) -> None:
            self.status_code = 200
            self.text = ""
            self._payload = payload or {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self) -> None:
            self.infer_payload: dict[str, object] | None = None
            self.register_count = 0
            self.infer_count = 0
            self.unregistered = False

        def post(self, url: str, *, json=None, timeout=None):  # noqa: ANN001, ANN202
            _ = timeout
            if url.endswith("/register"):
                self.register_count += 1
                assert json["key"].startswith("/ybb_")
                return FakeResponse()
            if url.endswith("/infer"):
                self.infer_count += 1
                self.infer_payload = json
                input_parameters = json["inputs"][0]["parameters"]
                region = shared_memory.SharedMemory(
                    name=input_parameters["shared_memory_region"]
                )
                try:
                    input_start = input_parameters["shared_memory_offset"]
                    input_end = input_start + input_parameters["shared_memory_byte_size"]
                    input_values = np.frombuffer(
                        region.buf[input_start:input_end],
                        dtype=np.float32,
                    )
                    np.testing.assert_array_equal(
                        input_values,
                        np.asarray([9.0], dtype=np.float32),
                    )
                    output_parameters = json["outputs"][0]["parameters"]
                    output_bytes = np.asarray([1.0, 2.0], dtype=np.float32).tobytes()
                    start = output_parameters["shared_memory_offset"]
                    region.buf[start : start + len(output_bytes)] = output_bytes
                finally:
                    del input_values
                    region.close()
                return FakeResponse(
                    {
                        "outputs": [
                            {
                                "name": "scores",
                                "shape": [1, 2],
                                "datatype": "FP32",
                            }
                        ]
                    }
                )
            if url.endswith("/unregister"):
                self.unregistered = True
                return FakeResponse()
            raise AssertionError(url)

    settings = build_settings("inference-gateway-rt", tmp_path)
    backend = TritonInferenceBackend(
        settings=settings,
        target_path=TargetPath.REALTIME,
        warm_ttl_seconds=120,
    )
    fake_client = FakeClient()
    monkeypatch.setattr(backend, "_triton_client", lambda _endpoint: fake_client)
    route = _classification_route(
        manifest={
            "io_schema": {
                "input_tensors": [
                    {"name": "images", "data_type": "FP32", "dims": [1]}
                ],
                "output_tensors": [
                    {"name": "scores", "data_type": "FP32", "dims": [2]}
                ],
            }
        }
    )
    layout = _triton_shared_memory_layout(
        route=route,
        input_name="images",
        input_shape=[1, 1],
        input_byte_size=4,
    )
    assert layout is not None

    outputs = backend._post_triton_shared_memory_request(
        endpoint=settings.triton.realtime,
        route=route,
        tensor_bytes=np.asarray([9.0], dtype=np.float32).tobytes(),
        layout=layout,
        timeout_seconds=1.0,
    )
    second_outputs = backend._post_triton_shared_memory_request(
        endpoint=settings.triton.realtime,
        route=route,
        tensor_bytes=np.asarray([9.0], dtype=np.float32).tobytes(),
        layout=layout,
        timeout_seconds=1.0,
    )

    assert fake_client.infer_payload is not None
    assert fake_client.register_count == 1
    assert fake_client.infer_count == 2
    assert fake_client.unregistered is False
    np.testing.assert_array_equal(outputs["scores"].values, np.asarray([1.0, 2.0]))
    np.testing.assert_array_equal(second_outputs["scores"].values, np.asarray([1.0, 2.0]))

    backend.close()

    assert fake_client.unregistered is True


def test_yolo_segmentation_outputs_are_decoded_from_box_and_proto_tensors() -> None:
    route = _classification_route(
        task_type=TaskType.SEGMENTATION,
        manifest={"io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]}},
        class_labels=["hand"],
    )
    detection_row = [0.5, 0.5, 0.4, 0.4, 0.93, 12.0, *([0.0] * 31)]
    background_row = [0.1, 0.1, 0.1, 0.1, 0.05, 0.0, *([0.0] * 31)]
    detection_rows = [detection_row, *([background_row] * 39)]
    detection_values = [
        row[channel_index]
        for channel_index in range(len(detection_row))
        for row in detection_rows
    ]
    proto_values = [1.0] * 16 + [0.0] * (31 * 16)

    segments = _segmentation_predictions_from_outputs(
        route=route,
        outputs={
            "output0": _OutputTensor(
                name="output0",
                shape=[1, len(detection_row), len(detection_rows)],
                values=detection_values,
            ),
            "output1": _OutputTensor(
                name="output1",
                shape=[1, 32, 4, 4],
                values=proto_values,
            ),
        },
        max_segments=5,
        score_threshold=0.5,
    )

    assert len(segments) == 1
    assert segments[0].label == "hand"
    assert segments[0].score == pytest.approx(0.93)
    assert segments[0].polygon[0].x == pytest.approx(0.25)
    assert segments[0].polygon[2].x == pytest.approx(0.75)


def test_legacy_yolo_segmentation_release_suppresses_same_class_duplicates() -> None:
    route = _classification_route(
        task_type=TaskType.SEGMENTATION,
        manifest={
            "io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]},
            "nms": {
                "enabled": False,
                "iou_threshold": 0.65,
                "max_detections": 300,
            },
        },
        class_labels=["Ast", "Riss"],
    )

    segments = _segmentation_predictions_from_outputs(
        route=route,
        outputs=_yolo_segmentation_outputs(
            [
                [0.5, 0.5, 0.4, 0.4, 0.90, 0.05],
                [0.5, 0.5, 0.4, 0.4, 0.80, 0.05],
            ]
        ),
        max_segments=10,
        score_threshold=0.1,
    )

    assert [(segment.label, segment.score) for segment in segments] == [("Ast", 0.9)]


def test_yolo_segmentation_nms_keeps_overlapping_different_classes() -> None:
    route = _classification_route(
        task_type=TaskType.SEGMENTATION,
        manifest={
            "io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]},
            "nms": {"enabled": True, "iou_threshold": 0.65},
        },
        class_labels=["Ast", "Riss"],
    )

    segments = _segmentation_predictions_from_outputs(
        route=route,
        outputs=_yolo_segmentation_outputs(
            [
                [0.5, 0.5, 0.4, 0.4, 0.90, 0.05],
                [0.5, 0.5, 0.4, 0.4, 0.05, 0.85],
            ]
        ),
        max_segments=10,
        score_threshold=0.1,
    )

    assert [(segment.label, segment.score) for segment in segments] == [
        ("Ast", 0.9),
        ("Riss", 0.85),
    ]


def test_non_yolo_segmentation_honors_disabled_nms() -> None:
    route = _classification_route(
        task_type=TaskType.SEGMENTATION,
        manifest={
            "io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]},
            "nms": {"enabled": False},
        },
        class_labels=["Ast", "Riss"],
    )
    route = replace(
        route,
        architecture=route.architecture.model_copy(update={"family": "torchvision"}),
    )

    segments = _segmentation_predictions_from_outputs(
        route=route,
        outputs=_yolo_segmentation_outputs(
            [
                [0.5, 0.5, 0.4, 0.4, 0.90, 0.05],
                [0.5, 0.5, 0.4, 0.4, 0.80, 0.05],
            ]
        ),
        max_segments=10,
        score_threshold=0.1,
    )

    assert [segment.score for segment in segments] == [0.9, 0.8]


def test_yolo_segmentation_limits_candidates_before_mask_reconstruction() -> None:
    route = _classification_route(
        task_type=TaskType.SEGMENTATION,
        manifest={
            "io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]},
            "nms": {"enabled": True, "max_detections": 1},
        },
        class_labels=["Ast", "Riss"],
    )

    segments = _segmentation_predictions_from_outputs(
        route=route,
        outputs=_yolo_segmentation_outputs(
            [
                [0.25, 0.5, 0.2, 0.2, 0.80, 0.05],
                [0.75, 0.5, 0.2, 0.2, 0.90, 0.05],
            ]
        ),
        max_segments=10,
        score_threshold=0.1,
    )

    assert [(segment.label, segment.score) for segment in segments] == [("Ast", 0.9)]


def _yolo_segmentation_outputs(
    prediction_rows: list[list[float]],
) -> dict[str, _OutputTensor]:
    padded_prediction_rows = [
        *prediction_rows,
        *(
            [[0.1, 0.1, 0.1, 0.1, 0.01, 0.01]]
            * max(40 - len(prediction_rows), 0)
        ),
    ]
    rows = [
        [*prediction_row, 12.0, *([0.0] * 31)]
        for prediction_row in padded_prediction_rows
    ]
    detection_values = [
        row[channel_index]
        for channel_index in range(len(rows[0]))
        for row in rows
    ]
    return {
        "output0": _OutputTensor(
            name="output0",
            shape=[1, len(rows[0]), len(rows)],
            values=detection_values,
        ),
        "output1": _OutputTensor(
            name="output1",
            shape=[1, 32, 4, 4],
            values=[1.0] * 16 + [0.0] * (31 * 16),
        ),
    }


def test_segmentation_mask_outputs_keep_non_rectangular_contours() -> None:
    route = _classification_route(
        task_type=TaskType.SEGMENTATION,
        manifest={"io_schema": {"input_tensors": [{"dims": [3, 100, 100]}]}},
        class_labels=["hand"],
    )

    segments = _segmentation_predictions_from_outputs(
        route=route,
        outputs={
            "mask_logits": _OutputTensor(
                name="mask_logits",
                shape=[1, 4, 4],
                values=[
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                    1.0,
                    0.0,
                    0.0,
                    1.0,
                    1.0,
                    1.0,
                    0.0,
                    1.0,
                    1.0,
                    1.0,
                    1.0,
                ],
            )
        },
        max_segments=5,
        score_threshold=0.5,
    )

    assert len(segments) == 1
    assert segments[0].label == "hand"
    polygon = segments[0].polygon
    assert len(polygon) >= 3
    polygon_area = abs(
        sum(
            point.x * polygon[(index + 1) % len(polygon)].y
            - polygon[(index + 1) % len(polygon)].x * point.y
            for index, point in enumerate(polygon)
        )
    ) / 2
    bounding_area = (
        max(point.x for point in polygon) - min(point.x for point in polygon)
    ) * (max(point.y for point in polygon) - min(point.y for point in polygon))
    assert polygon_area < bounding_area


def test_json_model_artifacts_are_rejected_before_inference(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    artifact = package_root / "source" / "model.onnx"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"not": "a-model"}', encoding="utf-8")
    route = _classification_route(
        manifest={
            "package_root": str(package_root),
            "source_manifests": {
                "deployment_artifact": {"path": "source/model.onnx", "format": "onnx"}
            },
        }
    )

    with pytest.raises(PlatformError) as exc_info:
        _deployment_artifact_path(route)

    assert exc_info.value.code == "invalid_model_artifact"


def test_anomaly_json_artifacts_are_rejected_before_inference(tmp_path: Path) -> None:
    package_root = tmp_path / "anomaly-package"
    artifact = package_root / "source" / "model.onnx"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"not": "an-openvino-model"}', encoding="utf-8")
    route = _classification_route(
        task_type=TaskType.ANOMALY,
        manifest={
            "package_root": str(package_root),
            "source_manifests": {"onnx": {"path": "source/model.onnx"}},
        },
    )

    with pytest.raises(PlatformError) as exc_info:
        _anomaly_artifact_path(route)

    assert exc_info.value.code == "invalid_model_artifact"


def seed_public_release(
    session: Session,
    *,
    tmp_path: Path,
    target_path: TargetPath,
    family_slug: str,
    task_type: TaskType,
    version_no: int = 1,
    labels: list[str] | None = None,
    public_parameters: list[dict[str, object]] | None = None,
) -> str:
    family = session.query(ModelFamily).filter(ModelFamily.slug == family_slug).one_or_none()
    if family is None:
        family = ModelFamily(
            id=f"mfy_{family_slug}",
            slug=family_slug,
            task_type=task_type.value,
            contract_major=1,
            input_schema_ref=f"contracts/{family_slug}/input.v1.json",
            output_schema_ref=f"contracts/{family_slug}/output.v1.json",
            active=True,
        )
    existing_version = (
        session.query(ModelVersion)
        .filter(ModelVersion.model_family_id == family.id, ModelVersion.version_no == version_no)
        .one_or_none()
    )
    if existing_version is not None:
        version_no = (
            session.query(ModelVersion)
            .filter(ModelVersion.model_family_id == family.id)
            .count()
            + 1
        )
    class_labels = labels or (
        ["good", "bad"] if task_type == TaskType.CLASSIFICATION else ["car", "log"]
    )
    package_root = tmp_path / f"package_{family_slug}_{target_path.value}"
    class_map_path = package_root / "source" / "class_map.json"
    class_map_path.parent.mkdir(parents=True, exist_ok=True)
    class_map_path.write_text('["' + '","'.join(class_labels) + '"]', encoding="utf-8")
    version = ModelVersion(
        id=f"mvr_{family_slug}_{target_path.value}",
        model_family_id=family.id,
        version_no=version_no,
        status="production",
        serving_manifest_uri=str(package_root / "manifest.json"),
        serving_manifest={
            "package_root": str(package_root),
            "class_count": len(class_labels),
            "confidence_threshold_default": 0.5,
            "compatible_targets": [target_path.value],
            "preprocessing": {"resize": {"width": 640, "height": 640}},
            "postprocessing": {"class_map": class_labels},
            "public_inference": {
                "display_name": family_slug.replace("-", " ").title(),
                "description": f"Public endpoint for {family_slug}.",
                "supported_parameters": public_parameters
                if public_parameters is not None
                else (
                    [
                        {
                            "name": "top_k",
                            "parameter_type": "integer",
                            "required": False,
                            "default": 3,
                            "minimum": 1,
                            "maximum": 20,
                            "description": "Number of top labels to return.",
                        }
                    ]
                    if task_type == TaskType.CLASSIFICATION
                    else [
                        {
                            "name": "threshold",
                            "parameter_type": "number",
                            "required": False,
                            "default": 0.5,
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "description": "Confidence threshold.",
                        },
                        {
                            "name": "max_detections",
                            "parameter_type": "integer",
                            "required": False,
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100,
                            "description": "Maximum detections returned.",
                        },
                    ]
                ),
            },
            "io_schema": {
                "input_schema_ref": family.input_schema_ref,
                "output_schema_ref": family.output_schema_ref,
                "contract_major": 1,
                "schema_digest": f"schema-{family_slug}-{target_path.value}",
            },
            "hardware_compatibility": {
                "max_batch_size": {"realtime": 1, "batch": 8},
            },
            "source_manifests": {
                "class_map": {"path": "source/class_map.json"},
            },
        },
    )
    release = DeploymentRelease(
        id=f"rel_{family_slug}_{target_path.value}",
        target_path=target_path.value,
        model_family_id=family.id,
        model_version_id=version.id,
        triton_model_name=f"{target_path.value}__{family_slug}_v1",
        triton_model_version=1,
        status="activated",
    )
    session.add_all([family, version, release])
    session.flush()
    return release.id


def _classification_route(
    *,
    manifest: dict[str, object],
    class_labels: list[str] | None = None,
    task_type: TaskType = TaskType.CLASSIFICATION,
) -> ReleaseContext:
    return ReleaseContext(
        deployment_release_id="rel_quality",
        target_path=TargetPath.REALTIME,
        active=True,
        rollback_candidate=False,
        release_status=DeploymentReleaseStatus.ACTIVATED,
        model_family_id="mfy_quality",
        model_family_slug="quality-classifier",
        model_version_id="mvr_quality",
        model_version_no=1,
        task_type=task_type,
        architecture=ModelArchitectureSummary(
            task_type=task_type,
            family="yolo",
            version="v11",
            size="s",
            display_name="YOLOv11 Small",
            internal_key=f"{task_type.value}:yolo:v11:s",
        ),
        triton_model_name="rt__quality_classifier_v1",
        triton_model_version=1,
        contract_major=1,
        manifest=manifest,
        class_labels=class_labels or ["bad", "good"],
        confidence_threshold_default=0.5,
        input_schema_ref=None,
        output_schema_ref=None,
    )


def test_oriented_detection_postprocessing_returns_corners_and_axis_box() -> None:
    route = _classification_route(
        manifest={
            "preprocessing": {"resize": {"width": 100, "height": 100}},
            "nms": {"enabled": False},
        },
        class_labels=["scratch"],
        task_type=TaskType.ORIENTED_DETECTION,
    )
    outputs = {
        "output__oriented_detections": _OutputTensor(
            name="output__oriented_detections",
            shape=[1, 6],
            values=[0.5, 0.5, 0.4, 0.2, 0.5, 0.9],
        )
    }

    detections = _detection_predictions_from_outputs(
        route=route,
        outputs=outputs,
        max_detections=5,
        score_threshold=0.5,
    )

    assert len(detections) == 1
    assert detections[0].oriented_box is not None
    assert len(detections[0].oriented_box.points) == 4
    assert detections[0].box.x_min < detections[0].box.x_max
    assert detections[0].box.y_min < detections[0].box.y_max


def test_realtime_classify_uses_channel_selected_release_and_exposes_status(engine, tmp_path: Path, monkeypatch) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        active_release_id, _ = seed_realtime_releases(session, tmp_path=tmp_path)
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )
    monkeypatch.setattr(realtime_app.state, "settings", settings, raising=False)

    runtime_headers = runtime_auth_headers(engine)
    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        realtime_app.state.realtime_gateway_service = RealtimeGatewayService(
            settings,
            service_name="inference-gateway-rt",
            target_path=TargetPath.REALTIME,
        )
        response = client.post(
            "/api/v1/realtime/classify",
            json={
                "deployment_target": "widget-inspection-rt",
                "deployment_release_id": "rel_rt_primary",
                "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAEElEQVR42mP8/5+hHgAHggJ/l8LojQAAAABJRU5ErkJggg==",
                "top_k": 2,
            },
            headers=runtime_headers,
        )
        status = client.get("/api/v1/realtime/models/status", headers=runtime_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "inference.classification.response.v1"
    assert body["route"]["deployment_release_id"] == active_release_id
    assert len(body["labels"]) == 2
    assert body["timing"]["total_ms"] >= 0

    assert status.status_code == 200
    status_items = status.json()["items"]
    assert any(
        item["route"]["deployment_release_id"] == active_release_id for item in status_items
    )


def test_realtime_detect_routes_to_explicit_release(engine, tmp_path: Path) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        _, detect_release_id = seed_realtime_releases(session, tmp_path=tmp_path)
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )

    runtime_headers = runtime_auth_headers(engine)
    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        realtime_app.state.realtime_gateway_service = RealtimeGatewayService(
            settings,
            service_name="inference-gateway-rt",
            target_path=TargetPath.REALTIME,
        )
        response = client.post(
            "/api/v1/realtime/detect",
            json={
                "deployment_target": "widget-inspection-rt",
                "deployment_release_id": detect_release_id,
                "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAEElEQVR42mP8/5+hHgAHggJ/l8LojQAAAABJRU5ErkJggg==",
                "max_detections": 2,
            },
            headers=runtime_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "inference.detection.response.v1"
    assert body["route"]["deployment_release_id"] == detect_release_id
    assert body["route"]["task_type"] == "detection"


def test_realtime_detect_accepts_binary_image_transport(
    engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        _, detect_release_id = seed_realtime_releases(session, tmp_path=tmp_path)
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )

    runtime_headers = runtime_auth_headers(engine)
    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        realtime_app.state.realtime_gateway_service = RealtimeGatewayService(
            settings,
            service_name="inference-gateway-rt",
            target_path=TargetPath.REALTIME,
        )
        response = client.post(
            "/api/v1/realtime/detect-bytes",
            params={
                "deployment_target": "widget-inspection-rt",
                "deployment_release_id": detect_release_id,
                "max_detections": 2,
                "score_threshold": 0.35,
            },
            content=sample_png_bytes,
            headers={
                **runtime_headers,
                "Content-Type": "image/png",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "inference.detection.response.v1"
    assert body["route"]["deployment_release_id"] == detect_release_id


def test_realtime_overload_returns_explicit_error_model(engine, tmp_path: Path) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_realtime_releases(session, tmp_path=tmp_path)
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )
    service = PublicRealtimeGatewayService(
        settings,
        service_name="inference-gateway-rt",
        target_path=TargetPath.REALTIME,
    )
    service.gate.current_inflight = service.gate.limit

    runtime_headers = runtime_auth_headers(engine)
    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        realtime_app.state.realtime_gateway_service = service
        response = client.post(
            "/api/v1/realtime/classify",
            json={
                "deployment_target": "widget-inspection-rt",
                "deployment_release_id": "rel_rt_primary",
                "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAEElEQVR42mP8/5+hHgAHggJ/l8LojQAAAABJRU5ErkJggg==",
            },
            headers=runtime_headers,
        )

    assert response.status_code == 429
    assert response.json()["code"] == "gateway_overloaded"


def test_realtime_triton_request_uses_realtime_timeout_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = build_settings("inference-gateway-rt", tmp_path)
    backend = TritonInferenceBackend(
        settings=settings,
        target_path=TargetPath.REALTIME,
        warm_ttl_seconds=120,
    )
    captured: dict[str, object] = {}

    def fake_post(
        self,
        *,
        endpoint,
        route,
        payload_bytes,
        tensor_bytes,
        timeout_seconds=None,
    ):
        captured["timeout_seconds"] = timeout_seconds
        captured["payload_len"] = len(payload_bytes)
        captured["tensor_len"] = len(tensor_bytes)
        from types import SimpleNamespace
        return SimpleNamespace(headers={}, json=lambda: {"outputs": []})

    monkeypatch.setattr(TritonInferenceBackend, "_post_triton_binary_request", fake_post)
    monkeypatch.setattr(
        "inference_gateway.runtime.service._triton_response_output_tensors",
        lambda _response: {},
    )

    route = ReleaseContext(
        deployment_release_id="rel_timeout",
        target_path=TargetPath.REALTIME,
        active=True,
        rollback_candidate=False,
        release_status=DeploymentReleaseStatus.ACTIVATED,
        model_family_id="mf_timeout",
        model_family_slug="timeout-classifier",
        model_version_id="mvr_timeout",
        model_version_no=1,
        task_type=TaskType.CLASSIFICATION,
        architecture=ModelArchitectureSummary(
            task_type=TaskType.CLASSIFICATION,
            family="resnet",
            version="v1",
            size="s",
            display_name="ResNet Small",
            internal_key="resnet-s",
        ),
        triton_model_name="timeout_classifier",
        triton_model_version=1,
        contract_major=1,
        manifest={
            "io_schema": {
                "input_tensors": [{"name": "images", "dims": [1, 3, 1, 1]}],
                "output_tensors": [{"name": "scores", "dims": [1, 2]}],
            }
        },
        class_labels=["ok", "nok"],
        confidence_threshold_default=0.35,
        input_schema_ref=None,
        output_schema_ref=None,
    )
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAEElEQVR42mP8/5+hHgAHggJ/l8LojQAAAABJRU5ErkJggg=="
    )

    asyncio.run(
        backend._model_outputs_via_triton(
            route=route,
            image_bytes=image_bytes,
            timeout_seconds=0.25,
        )
    )

    assert captured["timeout_seconds"] == 0.25
    assert captured["payload_len"] > 0
    assert captured["tensor_len"] > 0


def test_realtime_invalid_base64_returns_api_error(engine, tmp_path: Path) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_realtime_releases(session, tmp_path=tmp_path)
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )

    runtime_headers = runtime_auth_headers(engine)
    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        realtime_app.state.realtime_gateway_service = RealtimeGatewayService(
            settings,
            service_name="inference-gateway-rt",
            target_path=TargetPath.REALTIME,
        )
        response = client.post(
            "/api/v1/realtime/classify",
            json={
                "deployment_target": "widget-inspection-rt",
                "deployment_release_id": "rel_rt_primary",
                "image_base64": "not-base64-payload###",
            },
            headers=runtime_headers,
        )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_image_payload"


def test_batch_job_submission_without_channel_is_rejected(
    engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_batch_release(session, tmp_path=tmp_path)
        session.commit()

    ENQUEUED.clear()
    gateway_settings = build_settings(
        "inference-gateway-batch",
        tmp_path,
        api_keys=[("batch-client", "batch-secret", ["batch.submit"], "inference_client", "batch-unit")],
    )
    worker_settings = build_settings("batch-worker", tmp_path)

    monkeypatch.setattr("inference_gateway.runtime.service.Redis.from_url", lambda _url: object())
    monkeypatch.setattr("inference_gateway.runtime.service.Queue", FakeQueue)
    monkeypatch.setattr(batch_jobs, "load_platform_settings", lambda _service: worker_settings)

    with TestClient(batch_app) as client:
        batch_app.state.settings = gateway_settings
        batch_app.state.batch_gateway_service = PublicBatchGatewayService(
            gateway_settings,
            service_name="inference-gateway-batch",
            target_path=TargetPath.BATCH,
        )
        response = client.post(
            "/api/v1/batch/jobs",
            json={
                "model_id": "batch_primary-classification",
                "inputs": [
                    {
                        "item_id": "img-1",
                        "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAEElEQVR42mP8/5+hHgAHggJ/l8LojQAAAABJRU5ErkJggg==",
                        "external_ref": "first.png",
                        "metadata": {},
                    },
                    {
                        "item_id": "img-2",
                        "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAEElEQVR42mP8/5+hHgAHggJ/l8LojQAAAABJRU5ErkJggg==",
                        "external_ref": "second.png",
                        "metadata": {},
                    },
                ],
            },
            headers=BATCH_AUTH_HEADERS,
        )
        assert response.status_code == 409
        assert response.json()["code"] == "runtime_pipeline_required"
    assert ENQUEUED == []


def test_public_model_discovery_lists_parallel_realtime_and_batch_models(
    engine,
    tmp_path: Path,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="car-detector",
            task_type=TaskType.DETECTION,
            labels=["car"],
        )
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="quality-classifier",
            task_type=TaskType.CLASSIFICATION,
            labels=["good", "bad"],
        )
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.BATCH,
            family_slug="quality-classifier",
            task_type=TaskType.CLASSIFICATION,
            labels=["good", "bad"],
        )
        session.commit()

    realtime_settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )
    batch_settings = build_settings(
        "inference-gateway-batch",
        tmp_path,
        api_keys=[("batch-client", "batch-secret", ["batch.submit"], "inference_client", "batch-unit")],
    )

    with TestClient(realtime_app) as rt_client, TestClient(batch_app) as bt_client:
        realtime_app.state.settings = realtime_settings
        batch_app.state.settings = batch_settings

        realtime_response = rt_client.get("/api/v1/models", headers=REALTIME_AUTH_HEADERS)
        batch_response = bt_client.get("/api/v1/models", headers=BATCH_AUTH_HEADERS)

    assert realtime_response.status_code == 200
    realtime_items = {item["model_id"]: item for item in realtime_response.json()["items"]}
    assert realtime_items["car-detector"]["availability"]["realtime"]["available"] is True
    assert realtime_items["car-detector"]["availability"]["batch"]["available"] is False
    assert realtime_items["quality-classifier"]["availability"]["realtime"]["available"] is True
    assert realtime_items["quality-classifier"]["availability"]["realtime"]["timeout_default_ms"] == 750
    assert realtime_items["quality-classifier"]["availability"]["realtime"]["timeout_max_ms"] == 30000
    assert realtime_items["quality-classifier"]["availability"]["batch"]["available"] is True

    assert batch_response.status_code == 200
    batch_items = {item["model_id"]: item for item in batch_response.json()["items"]}
    assert batch_items["quality-classifier"]["availability"]["batch"]["available"] is True


def test_public_model_discovery_hides_unready_realtime_release(
    engine,
    tmp_path: Path,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="quality-classifier",
            task_type=TaskType.CLASSIFICATION,
            labels=["good", "bad"],
        )
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )
    service = PublicRealtimeGatewayService(
        settings,
        service_name="inference-gateway-rt",
        target_path=TargetPath.REALTIME,
    )
    service.backend.ready_model_versions = lambda: set()  # type: ignore[method-assign]

    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        realtime_app.state.realtime_gateway_service = service
        response = client.get("/api/v1/models", headers=REALTIME_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["model_id"] == "quality-classifier"
    assert item["availability"]["realtime"]["available"] is False


def test_public_model_discovery_is_open_when_api_key_auth_is_disabled(
    engine,
    tmp_path: Path,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="quality-classifier",
            task_type=TaskType.CLASSIFICATION,
            labels=["good", "bad"],
        )
        session.commit()

    settings = build_settings("inference-gateway-rt", tmp_path)
    settings.api_keys.enabled = False

    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        response = client.get("/api/v1/models")

    assert response.status_code == 200
    assert response.json()["items"][0]["model_id"] == "quality-classifier"


def test_public_anomaly_model_detail_uses_anomaly_schema_and_parameters(
    engine,
    tmp_path: Path,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="surface-anomaly",
            task_type=TaskType.ANOMALY,
            labels=[],
            public_parameters=[],
        )
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )

    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        response = client.get("/api/v1/models/surface-anomaly", headers=REALTIME_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["task_type"] == "anomaly"
    assert item["response_schema"]["schema_id"] == "inference.public.anomaly-response.v1"
    assert [parameter["name"] for parameter in item["supported_parameters"]] == [
        "threshold",
        "return_mask",
    ]


def test_public_realtime_classify_requires_channel_pipeline(
    engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="quality-classifier",
            task_type=TaskType.CLASSIFICATION,
            labels=["good", "bad"],
        )
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )

    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        response = client.post(
            "/api/v1/infer/classify",
            data={"model_id": "quality-classifier", "top_k": "2"},
            files={"image": ("sample.png", sample_png_bytes, "image/png")},
            headers=REALTIME_AUTH_HEADERS,
        )

    assert response.status_code == 409
    assert response.json()["code"] == "runtime_pipeline_required"


def test_internal_realtime_timeout_is_retryable(
    engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="timeout-segmenter",
            task_type=TaskType.SEGMENTATION,
            labels=["defect"],
        )
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )
    service = PublicRealtimeGatewayService(
        settings,
        service_name="inference-gateway-rt",
        target_path=TargetPath.REALTIME,
    )

    async def timeout_segment(**_kwargs: object) -> list[object]:
        raise TimeoutError

    service.backend.segment = timeout_segment  # type: ignore[method-assign]

    runtime_headers = runtime_auth_headers(engine)
    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        realtime_app.state.realtime_gateway_service = service
        response = client.post(
            "/api/v1/realtime/segment-bytes",
            params={"deployment_target": "realtime", "deployment_release_id": "rel_timeout-segmenter_realtime"},
            content=sample_png_bytes,
            headers={**runtime_headers, "Content-Type": "image/png"},
        )

    assert response.status_code == 504
    assert response.json()["code"] == "inference_timeout"
    assert response.json()["retryable"] is True
    assert response.json()["metadata"]["timeout_ms"] == 750
    assert (
        response.json()["metadata"]["phase"]
        == "backend_inference_and_postprocessing"
    )


def test_internal_segmentation_reports_backend_phase_timings(
    engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="timed-segmenter",
            task_type=TaskType.SEGMENTATION,
            labels=["defect"],
        )
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )
    service = PublicRealtimeGatewayService(
        settings,
        service_name="inference-gateway-rt",
        target_path=TargetPath.REALTIME,
    )

    async def timed_segment(**_kwargs: object) -> list[object]:
        timing = _backend_phase_timing.get()
        assert isinstance(timing, _BackendPhaseTiming)
        timing.preprocessing_ms = 12.0
        timing.inference_ms = 34.0
        timing.postprocessing_ms = 56.0
        timing.measured = True
        return []

    service.backend.segment = timed_segment  # type: ignore[method-assign]

    runtime_headers = runtime_auth_headers(engine)
    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        realtime_app.state.realtime_gateway_service = service
        response = client.post(
            "/api/v1/realtime/segment-bytes",
            params={"deployment_target": "realtime", "deployment_release_id": "rel_timed-segmenter_realtime"},
            content=sample_png_bytes,
            headers={**runtime_headers, "Content-Type": "image/png"},
        )

    assert response.status_code == 200
    timing = response.json()["timing"]
    assert timing["preprocessing_ms"] >= 12.0
    assert timing["inference_ms"] == 34.0
    assert timing["postprocessing_ms"] == 56.0


def test_public_realtime_detect_rejects_model_specific_unsupported_parameter(
    engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="wood-log-detector",
            task_type=TaskType.DETECTION,
            labels=["log"],
        )
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )

    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        response = client.post(
            "/api/v1/infer/detect",
            data={"model_id": "wood-log-detector", "iou_threshold": "0.5"},
            files={"image": ("sample.png", sample_png_bytes, "image/png")},
            headers=REALTIME_AUTH_HEADERS,
        )

    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_parameter"


def test_public_realtime_segment_requires_channel_pipeline(
    engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            family_slug="wood-segmenter",
            task_type=TaskType.SEGMENTATION,
            labels=["ring"],
            public_parameters=[
                {
                    "name": "threshold",
                    "parameter_type": "number",
                    "required": False,
                    "default": 0.5,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Confidence threshold.",
                },
                {
                    "name": "max_segments",
                    "parameter_type": "integer",
                    "required": False,
                    "default": 10,
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum segments returned.",
                },
            ],
        )
        session.commit()

    settings = build_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-unit")],
    )

    with TestClient(realtime_app) as client:
        realtime_app.state.settings = settings
        response = client.post(
            "/api/v1/infer/segment",
            data={"model_id": "wood-segmenter", "threshold": "0.1", "max_segments": "2"},
            files={"image": ("sample.png", sample_png_bytes, "image/png")},
            headers=REALTIME_AUTH_HEADERS,
        )

    assert response.status_code == 409
    assert response.json()["code"] == "runtime_pipeline_required"


def test_public_batch_job_submission_requires_channel_pipeline(
    engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_session_factories(engine)
    with Session(engine, expire_on_commit=False) as session:
        seed_public_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.BATCH,
            family_slug="quality-classifier",
            task_type=TaskType.CLASSIFICATION,
            labels=["good", "bad"],
        )
        session.commit()

    ENQUEUED.clear()
    gateway_settings = build_settings(
        "inference-gateway-batch",
        tmp_path,
        api_keys=[("batch-client", "batch-secret", ["batch.submit"], "inference_client", "batch-unit")],
    )
    worker_settings = build_settings("batch-worker", tmp_path)

    monkeypatch.setattr("inference_gateway.runtime.service.Redis.from_url", lambda _url: object())
    monkeypatch.setattr("inference_gateway.runtime.service.Queue", FakeQueue)
    monkeypatch.setattr(batch_jobs, "load_platform_settings", lambda _service: worker_settings)

    with TestClient(batch_app) as client:
        batch_app.state.settings = gateway_settings
        response = client.post(
            "/api/v1/batch/jobs",
            json={
                "model_id": "quality-classifier",
                "inputs": [
                    {
                        "item_id": "img-1",
                        "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAEElEQVR42mP8/5+hHgAHggJ/l8LojQAAAABJRU5ErkJggg==",
                        "external_ref": "first.png",
                        "metadata": {},
                    }
                ],
            },
            headers={**BATCH_AUTH_HEADERS, "Idempotency-Key": "job-quality-1"},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "runtime_pipeline_required"
    assert ENQUEUED == []
