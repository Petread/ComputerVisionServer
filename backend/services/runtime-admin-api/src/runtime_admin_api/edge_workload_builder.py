from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from cv_contracts import (
    EdgeAdmissionCheck,
    EdgeAdmissionReport,
    EdgeArtifactDescriptor,
    EdgeCapabilitySnapshot,
    EdgeDeploymentManifest,
    EdgeWorkloadReleaseDetail,
)
from cv_core import PlatformSettings
from cv_db import (
    build_runtime_model_resolver,
    get_edge_device_detail,
    save_edge_workload_release,
    validate_runtime_channel_start_requirements,
)
from cv_db.models import DeploymentRelease, ModelVersion, RuntimeChannel, RuntimeSource
from cv_runtime.distribution import edge_node_contracts, runtime_code_digest
from cv_utils import utc_now_iso
from sqlalchemy.orm import Session


class EdgeWorkloadBuildError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        admission_report: EdgeAdmissionReport | None = None,
    ) -> None:
        super().__init__(message)
        self.admission_report = admission_report


def build_edge_workload_release(
    session: Session,
    *,
    settings: PlatformSettings,
    channel_id: str,
    created_by: str | None,
    precision: str = "fp16",
) -> EdgeWorkloadReleaseDetail:
    loaded = validate_runtime_channel_start_requirements(session, channel_id=channel_id)
    channel = loaded.channel
    if channel.execution_target != "edge_device" or not channel.edge_device_id:
        raise EdgeWorkloadBuildError("Channel is not assigned to an Edge device.")
    device = get_edge_device_detail(session, channel.edge_device_id)
    if device is None or device.latest_capability_snapshot is None:
        raise EdgeWorkloadBuildError(
            "Edge device must have a current capability snapshot before release construction."
        )
    capabilities = device.latest_capability_snapshot.capabilities
    capability_digest = device.latest_capability_snapshot.digest
    artifact_sources: list[dict[str, Any]] = []
    descriptors: dict[str, EdgeArtifactDescriptor] = {}

    graph_payload = {
        "schema_version": "edge.pipeline.graph.v1",
        "channel_id": channel.id,
        "pipeline_version_id": loaded.version.id,
        "config_digest": loaded.version.config_digest,
        "mode": loaded.version.mode,
        "graph": loaded.graph.model_dump(mode="json"),
    }
    graph_descriptor = _store_json_artifact(
        settings,
        graph_payload,
        file_name="pipeline-graph.json",
        kind="pipeline_graph",
        artifact_sources=artifact_sources,
    )
    descriptors[graph_descriptor.sha256] = graph_descriptor

    resolver = build_runtime_model_resolver(session)
    models: list[dict[str, Any]] = []
    engine_builds: list[dict[str, Any]] = []
    golden_tests: list[dict[str, Any]] = []
    for node in loaded.graph.nodes:
        if node.logical_model_ref is None:
            continue
        binding = resolver(node.logical_model_ref)
        version = session.get(ModelVersion, binding.model_version_id)
        release = session.get(DeploymentRelease, binding.deployment_release_id)
        if version is None or release is None:
            raise EdgeWorkloadBuildError(
                f"Resolved model for node {node.node_key} is missing registry data."
            )
        onnx_path = _resolve_onnx_path(version)
        onnx_descriptor = _store_file_artifact(
            settings,
            onnx_path,
            file_name=f"{node.node_key}.onnx",
            kind="onnx_model",
            artifact_sources=artifact_sources,
        )
        descriptors[onnx_descriptor.sha256] = onnx_descriptor
        calibration_descriptor = _optional_int8_calibration(
            settings,
            version,
            node.node_key,
            precision,
            artifact_sources,
        )
        if calibration_descriptor is not None:
            descriptors[calibration_descriptor.sha256] = calibration_descriptor
        class_map_descriptor = _optional_class_map(
            settings, version, node.node_key, artifact_sources
        )
        if class_map_descriptor is not None:
            descriptors[class_map_descriptor.sha256] = class_map_descriptor
        export_manifest = dict(version.export_manifest or {})
        model = {
            "node_key": node.node_key,
            "architecture": {
                "task_type": binding.task_type.value,
                "family": str(
                    getattr(version, "architecture_family", None) or binding.model_family_slug
                ),
                "version": str(getattr(version, "architecture_version", None) or "unknown"),
                "size": str(getattr(version, "architecture_size", None) or "unknown"),
                "display_name": binding.display_name or binding.model_family_slug,
                "internal_key": binding.model_family_slug,
            },
            "capability": binding.capability,
            "model_family_slug": binding.model_family_slug,
            "model_version_id": binding.model_version_id,
            "deployment_release_id": binding.deployment_release_id,
            "task_type": binding.task_type.value,
            "onnx_sha256": onnx_descriptor.sha256,
            "class_map_sha256": (
                class_map_descriptor.sha256 if class_map_descriptor is not None else None
            ),
            "input_size": _model_input_size(version),
            "io_schema": dict(export_manifest.get("io_schema") or {}),
            "preprocessing": dict(export_manifest.get("preprocessing") or {}),
            "postprocessing": dict(export_manifest.get("postprocessing") or {}),
            "nms": dict(export_manifest.get("nms") or {}),
            "warmup_runs": 2,
            "precision": precision,
            "calibration_cache_sha256": (
                calibration_descriptor.sha256 if calibration_descriptor is not None else None
            ),
        }
        models.append(model)
        if binding.task_type.value == "temporal_sequence":
            temporal = export_manifest.get("temporal") or {}
            model["temporal"] = {
                "runtime_contract_implemented": temporal.get("runtime_contract_implemented", False)
            }
            model["temporal_artifacts"] = {}
            for name in (
                "temporal_contract",
                "scaler",
                "angle_contract",
                "head_class_order",
                "confidence_calibration",
            ):
                artifact = (temporal.get("artifacts") or {}).get(name) or {}
                if not artifact.get("uri") or not artifact.get("sha256"):
                    raise EdgeWorkloadBuildError(f"Temporal model is missing the {name} artifact.")
                descriptor = _store_file_artifact(
                    settings,
                    Path(artifact["uri"]),
                    file_name=f"{node.node_key}-{name}.json",
                    kind="configuration",
                    artifact_sources=artifact_sources,
                    expected_sha256=str(artifact["sha256"]).removeprefix("sha256:"),
                )
                descriptors[descriptor.sha256] = descriptor
                model["temporal_artifacts"][name] = descriptor.sha256
        engine_builds.append(
            {
                "node_key": node.node_key,
                "onnx_sha256": onnx_descriptor.sha256,
                "precision": precision,
                "input_shapes": _engine_input_shapes(version),
                "calibration_cache_sha256": (
                    calibration_descriptor.sha256 if calibration_descriptor is not None else None
                ),
            }
        )
        for golden in _golden_inputs(version):
            input_descriptor = _store_file_artifact(
                settings,
                Path(golden["uri"]),
                file_name=f"{node.node_key}-{Path(golden['uri']).name}",
                kind="golden_input",
                artifact_sources=artifact_sources,
                expected_sha256=golden.get("sha256"),
            )
            descriptors[input_descriptor.sha256] = input_descriptor
            expected_uri = str(golden.get("expected_output_uri") or "").strip()
            test = {
                "node_key": node.node_key,
                "input_sha256": input_descriptor.sha256,
                "comparison": str(
                    golden.get("comparison")
                    or ("numeric_allclose" if expected_uri else "artifact_integrity")
                ),
                "atol": float(golden.get("atol") or 0.0),
                "rtol": float(golden.get("rtol") or 0.0),
            }
            if expected_uri:
                expected_descriptor = _store_file_artifact(
                    settings,
                    Path(expected_uri),
                    file_name=(
                        f"{node.node_key}-expected-output{Path(expected_uri).suffix or '.bin'}"
                    ),
                    kind="golden_input",
                    artifact_sources=artifact_sources,
                    expected_sha256=golden.get("expected_output_sha256"),
                )
                descriptors[expected_descriptor.sha256] = expected_descriptor
                test["expected_output_sha256"] = expected_descriptor.sha256
            golden_tests.append(test)

    edge_source = _edge_source(channel_source=loaded.source)
    runtime_requirements = {
        "executor": "cv_runtime_v1",
        "runtime_code_digest": runtime_code_digest(),
        "node_contract_versions": {
            node.node_type: edge_node_contracts().get(node.node_type, 1)
            for node in loaded.graph.nodes
        },
        "pipeline_graph_sha256": graph_descriptor.sha256,
        "execution_mode": loaded.version.mode,
        "source": edge_source,
        "frame_sample_every_n": _sample_every_n(loaded.source),
        "emit_source_results": True,
        "models": models,
        "engine_builds": engine_builds,
        "golden_tests": golden_tests,
        "target_profile": {
            "platform": capabilities.platform,
            "architecture": capabilities.architecture,
            "cuda_version": capabilities.cuda_version,
            "tensorrt_version": capabilities.tensorrt_version,
            "gpu_model": capabilities.gpu_model,
        },
    }
    config_descriptor = _store_json_artifact(
        settings,
        runtime_requirements,
        file_name="runtime-configuration.json",
        kind="configuration",
        artifact_sources=artifact_sources,
    )
    descriptors[config_descriptor.sha256] = config_descriptor
    artifacts = sorted(descriptors.values(), key=lambda item: (item.kind, item.file_name))
    required_nodes = sorted({node.node_type for node in loaded.graph.nodes})
    admission = _admit(
        device_id=device.edge_device_id,
        connectivity=device.connectivity_state,
        enabled=device.enabled,
        deployment_revalidation_required=device.deployment_revalidation_required,
        capability_digest=capability_digest,
        capabilities=capabilities,
        required_node_types=required_nodes,
        artifact_bytes=sum(item.size_bytes for item in artifacts),
        model_count=len(models),
        golden_tests=golden_tests,
        source_adapter=str(edge_source.get("adapter") or edge_source.get("type") or "passive"),
        source_secrets_available=_source_secrets_available(loaded.source),
    )
    release_payload = {
        "channel_id": channel.id,
        "pipeline_version_id": loaded.version.id,
        "pipeline_config_digest": loaded.version.config_digest,
        "edge_device_id": device.edge_device_id,
        "capability_digest": capability_digest,
        "precision": precision,
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "runtime_requirements": runtime_requirements,
    }
    release_digest = _digest_json(release_payload)
    release_id = f"ewrl_{release_digest[:24]}"
    manifest = EdgeDeploymentManifest(
        deployment_id="release-template",
        workload_release_id=release_id,
        channel_id=channel.id,
        pipeline_version_id=loaded.version.id,
        idempotency_key=f"release:{release_digest}",
        artifacts=artifacts,
        required_platforms=[capabilities.platform],
        required_node_types=required_nodes,
        runtime_requirements={
            **runtime_requirements,
            "release_digest": release_digest,
            "capability_digest": capability_digest,
            "configuration_sha256": config_descriptor.sha256,
        },
        created_at=utc_now_iso(),
    )
    detail = save_edge_workload_release(
        session,
        release_id=release_id,
        release_digest=release_digest,
        channel_id=channel.id,
        pipeline_version_id=loaded.version.id,
        edge_device_id=device.edge_device_id,
        capability_digest=capability_digest,
        platform=capabilities.platform,
        manifest=manifest,
        artifact_sources=artifact_sources,
        admission_report=admission,
        required_node_types=required_nodes,
        created_by=created_by,
    )
    return detail


def _admit(
    *,
    device_id: str,
    connectivity: str,
    enabled: bool,
    deployment_revalidation_required: bool,
    capability_digest: str,
    capabilities: EdgeCapabilitySnapshot,
    required_node_types: list[str],
    artifact_bytes: int,
    model_count: int,
    golden_tests: list[dict[str, Any]],
    source_adapter: str,
    source_secrets_available: bool,
) -> EdgeAdmissionReport:
    checks: list[EdgeAdmissionCheck] = []

    def add(key: str, ok: bool, success: str, failure: str, **detail: object) -> None:
        checks.append(
            EdgeAdmissionCheck(
                key=key,
                outcome="pass" if ok else "fail",
                message=success if ok else failure,
                detail=detail,
            )
        )

    add("device.enabled", enabled, "Device is enabled.", "Device is disabled.")
    add(
        "runtime.code",
        capabilities.runtime_code_digest == runtime_code_digest(),
        "Installed runtime matches the pipeline implementation.",
        "A central Edge runtime update is required before this workload can run.",
        required_digest=runtime_code_digest(),
        installed_digest=capabilities.runtime_code_digest,
    )
    add(
        "device.connectivity",
        connectivity in {"online", "degraded"},
        "Device has a current server observation.",
        f"Device connectivity is {connectivity}.",
    )
    missing_nodes = sorted(set(required_node_types) - set(capabilities.supported_node_types))
    add(
        "runtime.node_types",
        not missing_nodes,
        "Every pipeline node type is supported.",
        "The Edge runtime lacks required node types.",
        missing_node_types=missing_nodes,
    )
    add(
        "storage.capacity",
        capabilities.storage_free_bytes >= artifact_bytes + 1024**3,
        "Artifact set fits with a 1 GiB reserve.",
        "Artifact set would violate the 1 GiB admission reserve.",
        artifact_bytes=artifact_bytes,
        storage_free_bytes=capabilities.storage_free_bytes,
    )
    add(
        "runtime.tensorrt",
        model_count == 0 or bool(capabilities.tensorrt_version),
        "TensorRT inventory satisfies model-engine construction.",
        "TensorRT is required for model-engine construction.",
    )
    add(
        "golden.inputs",
        all(test.get("input_sha256") for test in golden_tests),
        "Every declared golden input is content-addressed.",
        "At least one golden test has no input artifact.",
        test_count=len(golden_tests),
    )
    comparable_golden = all(
        test.get("comparison") == "artifact_integrity" or bool(test.get("expected_output_sha256"))
        for test in golden_tests
    )
    add(
        "golden.outputs",
        comparable_golden,
        "Every output comparison has a content-addressed expected artifact.",
        "At least one output comparison has no expected-output artifact.",
        test_count=len(golden_tests),
    )
    add(
        "source.adapter",
        source_adapter == "passive" or source_adapter in capabilities.camera_adapters,
        "The Edge reports the required source adapter.",
        (
            "The Edge does not report Basler support. Install the Basler runtime update, "
            "approve basler_pylon in YBB_EDGE_CAMERA_ADAPTERS, then refresh device status."
            if source_adapter == "basler_pylon"
            else "The Edge does not report the required source adapter."
        ),
        required_adapter=source_adapter,
        available_adapters=capabilities.camera_adapters,
    )
    add(
        "source.secrets",
        source_secrets_available,
        "Every referenced source secret is readable for deployment materialization.",
        "At least one referenced source secret is unavailable.",
    )
    if deployment_revalidation_required:
        checks.append(
            EdgeAdmissionCheck(
                key="device.revalidation",
                outcome="warning",
                message=(
                    "Device identity changed; this admitted release clears revalidation only "
                    "after a successful commit."
                ),
            )
        )
    return EdgeAdmissionReport(
        status="rejected" if any(check.outcome == "fail" for check in checks) else "admitted",
        edge_device_id=device_id,
        capability_digest=capability_digest,
        checked_at=utc_now_iso(),
        checks=checks,
    )


def _store_json_artifact(
    settings: PlatformSettings,
    payload: dict[str, Any],
    *,
    file_name: str,
    kind: str,
    artifact_sources: list[dict[str, Any]],
) -> EdgeArtifactDescriptor:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _store_bytes(
        settings,
        data,
        file_name=file_name,
        kind=kind,
        media_type="application/json",
        artifact_sources=artifact_sources,
    )


def _store_file_artifact(
    settings: PlatformSettings,
    source: Path,
    *,
    file_name: str,
    kind: str,
    artifact_sources: list[dict[str, Any]],
    expected_sha256: str | None = None,
) -> EdgeArtifactDescriptor:
    if not source.is_file():
        raise EdgeWorkloadBuildError(f"Required workload artifact does not exist: {source}")
    digest = _sha256_file(source)
    if expected_sha256 and expected_sha256 != digest:
        raise EdgeWorkloadBuildError(f"Artifact hash mismatch for {source}.")
    destination = _cas_path(settings, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix(".tmp")
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    descriptor = EdgeArtifactDescriptor(
        sha256=digest,
        size_bytes=source.stat().st_size,
        kind=kind,
        file_name=file_name,
    )
    _record_source(artifact_sources, descriptor, destination)
    return descriptor


def _store_bytes(
    settings: PlatformSettings,
    data: bytes,
    *,
    file_name: str,
    kind: str,
    media_type: str,
    artifact_sources: list[dict[str, Any]],
) -> EdgeArtifactDescriptor:
    digest = hashlib.sha256(data).hexdigest()
    destination = _cas_path(settings, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(destination)
    descriptor = EdgeArtifactDescriptor(
        sha256=digest,
        size_bytes=len(data),
        kind=kind,
        file_name=file_name,
        media_type=media_type,
    )
    _record_source(artifact_sources, descriptor, destination)
    return descriptor


def _record_source(
    sources: list[dict[str, Any]], descriptor: EdgeArtifactDescriptor, path: Path
) -> None:
    if any(item["sha256"] == descriptor.sha256 for item in sources):
        return
    sources.append({**descriptor.model_dump(mode="json"), "source_path": str(path)})


def _cas_path(settings: PlatformSettings, digest: str) -> Path:
    return settings.storage.edge_workload_blob_root() / digest[:2] / digest


def _resolve_onnx_path(version: ModelVersion) -> Path:
    candidates = [version.onnx_uri, (version.export_manifest or {}).get("onnx", {}).get("uri")]
    for candidate in candidates:
        if candidate and Path(str(candidate)).is_file():
            return Path(str(candidate))
    raise EdgeWorkloadBuildError(
        f"Model version {version.id} has no readable ONNX artifact for Edge deployment."
    )


def _optional_class_map(
    settings: PlatformSettings,
    version: ModelVersion,
    node_key: str,
    artifact_sources: list[dict[str, Any]],
) -> EdgeArtifactDescriptor | None:
    candidate = settings.storage.serving_package_root(version.id) / "source" / "class_map.json"
    if not candidate.is_file():
        return None
    return _store_file_artifact(
        settings,
        candidate,
        file_name=f"{node_key}-class-map.json",
        kind="label_map",
        artifact_sources=artifact_sources,
    )


def _optional_int8_calibration(
    settings: PlatformSettings,
    version: ModelVersion,
    node_key: str,
    precision: str,
    artifact_sources: list[dict[str, Any]],
) -> EdgeArtifactDescriptor | None:
    export = version.export_manifest or {}
    tensorrt = export.get("tensorrt") or {}
    candidate = export.get("int8_calibration_cache_uri") or tensorrt.get("calibration_cache_uri")
    if not candidate:
        if precision == "int8":
            raise EdgeWorkloadBuildError(
                f"Model version {version.id} requires an INT8 calibration cache."
            )
        return None
    return _store_file_artifact(
        settings,
        Path(str(candidate)),
        file_name=f"{node_key}-int8-calibration.cache",
        kind="other",
        artifact_sources=artifact_sources,
    )


def _golden_inputs(version: ModelVersion) -> list[dict[str, Any]]:
    value = (version.export_manifest or {}).get("golden_inputs") or []
    return [dict(item) for item in value if isinstance(item, dict) and item.get("uri")]


def _model_input_size(version: ModelVersion) -> list[int]:
    preprocessing = (version.export_manifest or {}).get("preprocessing") or {}
    raw = preprocessing.get("input_size") or preprocessing.get("resize") or [640, 640]
    if isinstance(raw, int):
        return [raw, raw]
    return [int(item) for item in raw[-2:]]


def _engine_input_shapes(version: ModelVersion) -> dict[str, list[int]]:
    schema = (version.export_manifest or {}).get("io_schema") or {}
    inputs = schema.get("input_tensors") or schema.get("inputs") or []
    result: dict[str, list[int]] = {}
    for item in inputs:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        raw_shape = item.get("shape") or item.get("dims") or []
        shape = [int(value) for value in raw_shape if isinstance(value, int)]
        if shape and len(shape) == 3:
            shape.insert(0, 1)
        if shape and all(value > 0 for value in shape):
            result[str(item["name"])] = shape
    return result


def _edge_source(*, channel_source: RuntimeSource | None) -> dict[str, Any]:
    if channel_source is None:
        return {"type": "passive", "interval_ms": 1000}
    config = dict(channel_source.config or {})
    source_type = str(channel_source.source_type)
    if source_type == "rtsp":
        return {"type": "rtsp", "adapter": "rtsp", "uri": config.get("url")}
    if source_type == "usb_opencv":
        device = config.get("device_path")
        if device is None:
            device = config.get("device_index")
        return _compact_edge_source(
            {
                "type": "usb_opencv",
                "adapter": "usb_opencv",
                "uri": device,
                "device_path": config.get("device_path"),
                "device_index": config.get("device_index"),
                "backend": config.get("backend") or "auto",
                "width": config.get("width"),
                "height": config.get("height"),
                "fps": config.get("fps"),
                "fourcc": config.get("fourcc"),
                "buffer_size": config.get("buffer_size"),
            }
        )
    if source_type == "file_test":
        return {"type": "file", "adapter": "opencv", "uri": config.get("path")}
    if source_type == "snapshot":
        return {
            "type": "file",
            "adapter": "opencv",
            "uri": config.get("url") or config.get("snapshot_url"),
        }
    if source_type == "aravis_gige":
        return _compact_edge_source(
            {
                "type": "aravis_gige",
                "adapter": "aravis_gige",
                "camera_name": config.get("camera_name")
                or config.get("serial_number")
                or config.get("ip_address")
                or config.get("device_id"),
                "width": config.get("width"),
                "height": config.get("height"),
                "fps": config.get("fps"),
            }
        )
    if source_type == "basler_pylon":
        transport = str(config.get("transport_mode") or "auto").lower()
        if transport not in {"auto", "direct", "pylon", "pylon_direct"}:
            raise EdgeWorkloadBuildError(
                "Basler on Edge requires transport_mode=auto or pylon_direct; "
                "the server's Aravis RTSP bridge is not a local Edge source."
            )
        return _compact_edge_source(
            {
                **{
                    key: config[key]
                    for key in (
                        "serial_number", "user_defined_name", "camera_name", "ip_address",
                        "device_id", "id_", "model", "width", "height", "offset_x", "offset_y",
                        "fps", "acquisition_mode", "acquisition_frame_rate_enable",
                        "pixel_format", "exposure_time_us", "exposure_us", "exposure_auto",
                        "gain_db", "gain", "gain_auto", "packet_size", "inter_packet_delay",
                        "trigger_mode", "trigger_selector", "trigger_source", "trigger_activation",
                        "grab_strategy", "buffer_count", "max_num_buffer", "output_queue_size",
                        "frame_retention_ms", "fetch_timeout_seconds", "timeout_ms", "features",
                    )
                    if key in config
                },
                "type": "basler_pylon",
                "adapter": "basler_pylon",
                "transport_mode": "pylon_direct",
            }
        )
    if source_type == "flir_blackfly":
        trigger_mode = str(config.get("trigger_mode") or "free_run").lower()
        trigger_source = str(config.get("trigger_source") or "Software").lower()
        normalized_trigger = (
            "free_run"
            if trigger_mode in {"off", "false", "0", "free_run"}
            else "software"
            if trigger_source == "software"
            else "line0"
        )
        return _compact_edge_source(
            {
                "type": "flir_spinnaker",
                "adapter": "flir_spinnaker",
                "serial_number": config.get("serial_number"),
                "pixel_format": config.get("pixel_format"),
                "exposure_us": config.get("exposure_time_us"),
                "gain": config.get("gain_db"),
                "trigger_mode": normalized_trigger,
                "timeout_ms": int(float(config.get("fetch_timeout_seconds") or 2.0) * 1000),
            }
        )
    raise EdgeWorkloadBuildError(f"Source type {source_type} is not supported by Edge M3.")


def _compact_edge_source(source: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in source.items() if value is not None and value != ""}


def _sample_every_n(source: RuntimeSource | None) -> int:
    return max(int((source.config or {}).get("sample_every_n", 1)), 1) if source else 1


def materialize_edge_deployment_manifest(
    session: Session,
    manifest: EdgeDeploymentManifest,
) -> EdgeDeploymentManifest:
    """Add source credentials only to the in-flight Edge manifest, never to release metadata."""
    channel = session.get(RuntimeChannel, manifest.channel_id)
    source = (
        session.get(RuntimeSource, channel.source_id)
        if channel is not None and channel.source_id
        else None
    )
    requirements = dict(manifest.runtime_requirements)
    runtime_source = dict(requirements.get("source") or {})
    if source is not None and runtime_source.get("type") == "rtsp":
        refs = dict(source.secret_refs or {})
        username = _read_source_secret(refs.get("username_file") or refs.get("username"))
        password = _read_source_secret(refs.get("password_file") or refs.get("password"))
        uri = str(runtime_source.get("uri") or "")
        if username or password:
            parsed = urlsplit(uri)
            host = parsed.hostname or ""
            if parsed.port is not None:
                host = f"{host}:{parsed.port}"
            userinfo = quote(username or "", safe="")
            if password is not None:
                userinfo = f"{userinfo}:{quote(password, safe='')}"
            runtime_source["uri"] = urlunsplit(
                (parsed.scheme, f"{userinfo}@{host}", parsed.path, parsed.query, parsed.fragment)
            )
    requirements["source"] = runtime_source
    return manifest.model_copy(update={"runtime_requirements": requirements})


def _source_secrets_available(source: RuntimeSource | None) -> bool:
    if source is None:
        return True
    return all(Path(str(value)).is_file() for value in (source.secret_refs or {}).values() if value)


def _read_source_secret(reference: str | None) -> str | None:
    if not reference:
        return None
    path = Path(reference)
    if not path.is_file():
        raise EdgeWorkloadBuildError(f"Referenced source secret is unavailable: {path}")
    return path.read_text(encoding="utf-8").strip()


def _digest_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
