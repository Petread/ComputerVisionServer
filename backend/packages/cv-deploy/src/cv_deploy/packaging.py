from __future__ import annotations

import hashlib
from importlib import import_module
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from cv_core import PlatformSettings
from cv_status import EnvironmentName, TargetPath
from cv_utils import promote_staged_directory, quarantine_path, utc_now_iso

from cv_deploy.manifests import (
    DeploymentPackageManifest,
    ExportFormatArtifact,
    ExportManifest,
    HardwareCompatibility,
    ModelIoSchema,
    NmsConfig,
    ServingTargetVariant,
)
from cv_deploy.triton import render_config_pbtxt

TENSORRT_TOPK_K_LIMIT = 3840
LOGGER = logging.getLogger(__name__)


def load_json_file(path_or_uri: str | Path | None) -> dict[str, Any]:
    if path_or_uri is None:
        return {}
    path = Path(path_or_uri)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def build_training_artifact_manifest_payload(
    *,
    training_job_id: str,
    task_type: str,
    training_root: Path,
    evaluation_summary_path: Path,
    onnx_path: Path,
    tensorrt_path: Path | None,
    export_manifest_path: Path,
    export_manifest_payload: dict[str, Any],
    additional_artifacts: dict[str, Path] | None = None,
) -> dict[str, Any]:
    payload = {
        "training_job_id": training_job_id,
        "task_type": task_type,
        "generated_at": utc_now_iso(),
        "training_root": str(training_root),
        "artifacts": {
            "evaluation_summary": _file_descriptor(evaluation_summary_path),
            "export_manifest": _file_descriptor(export_manifest_path),
            "onnx": _file_descriptor(onnx_path),
        },
        "metadata": {
            "class_map_version": export_manifest_payload.get("class_map_version"),
            "schema_digest": export_manifest_payload.get("io_schema", {}).get("schema_digest"),
            "hardware_profile": export_manifest_payload.get("hardware_compatibility", {}).get(
                "target_gpu_profile"
            ),
            "artifact_validation": export_manifest_payload.get("artifact_validation") or {},
        },
    }
    if tensorrt_path is not None and tensorrt_path.exists():
        payload["artifacts"]["tensorrt"] = _file_descriptor(tensorrt_path)
    for name, path in sorted((additional_artifacts or {}).items()):
        if path.exists():
            payload["artifacts"][name] = _file_descriptor(path)
    return payload


def build_export_manifest_payload(
    *,
    training_job_id: str,
    model_family_slug: str,
    task_type: str,
    recipe_name: str,
    recipe_version: str,
    image_size: int | None,
    snapshot_id: str,
    snapshot_manifest_uri: str | None,
    class_map: list[str] | None,
    class_map_sha256: str | None,
    io_schema: dict[str, Any],
    preprocessing: dict[str, Any],
    postprocessing: dict[str, Any],
    nms: dict[str, Any],
    hardware_compatibility: dict[str, Any],
    build_manifest: dict[str, Any],
    onnx_path: Path,
    tensorrt_path: Path | None,
    artifact_validation: dict[str, Any],
    temporal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = ExportManifest(
        training_job_id=training_job_id,
        model_family_slug=model_family_slug,
        task_type=task_type,
        recipe_name=recipe_name,
        recipe_version=recipe_version,
        image_size=image_size,
        generated_at=utc_now_iso(),
        source_snapshot_id=snapshot_id,
        source_snapshot_manifest_uri=snapshot_manifest_uri,
        class_map_version=(
            f"{snapshot_id}:{class_map_sha256[:12]}" if class_map_sha256 else None
        ),
        class_map_sha256=class_map_sha256,
        class_count=len(class_map or []),
        confidence_threshold_default=(
            float(postprocessing["confidence_threshold_default"])
            if postprocessing.get("confidence_threshold_default") is not None
            else None
        ),
        preprocessing=preprocessing,
        postprocessing=postprocessing,
        nms=NmsConfig.model_validate(nms),
        io_schema=ModelIoSchema.model_validate(io_schema),
        hardware_compatibility=HardwareCompatibility.model_validate(hardware_compatibility),
        onnx=ExportFormatArtifact(
            uri=str(onnx_path),
            sha256=sha256_file(onnx_path),
            size_bytes=onnx_path.stat().st_size,
            format="onnx",
            file_name=onnx_path.name,
        ),
        build_manifest=build_manifest,
        artifact_validation=artifact_validation,
        temporal=temporal or {},
    )
    if tensorrt_path is not None and tensorrt_path.exists():
        manifest.tensorrt = ExportFormatArtifact(
            uri=str(tensorrt_path),
            sha256=sha256_file(tensorrt_path),
            size_bytes=tensorrt_path.stat().st_size,
            format="tensorrt_plan",
            file_name=tensorrt_path.name,
        )
    return manifest.model_dump(mode="json")


def build_deployment_package(
    *,
    settings: PlatformSettings,
    model_version_id: str,
    model_family_id: str,
    model_family_slug: str,
    version_no: int,
    task_type: str,
    contract_major: int,
    immutable_metadata_sha256: str | None,
    export_manifest: dict[str, Any],
    artifact_manifest: dict[str, Any],
    export_manifest_uri: str | None,
    artifact_manifest_uri: str | None,
) -> dict[str, Any]:
    package_root = settings.storage.serving_package_root(model_version_id)
    manifest_path = package_root / "manifest.json"
    onnx_meta = export_manifest.get("onnx") or {}
    tensorrt_meta = export_manifest.get("tensorrt") or {}
    onnx_uri = str(onnx_meta.get("uri") or "").strip()
    tensorrt_uri = str(tensorrt_meta.get("uri") or "").strip()
    onnx_source = Path(onnx_uri) if onnx_uri else Path("")
    tensorrt_source = Path(tensorrt_uri) if tensorrt_uri else Path("")
    prebuilt_tensorrt_available = bool(tensorrt_uri and tensorrt_source.exists())
    package_hardware_compatibility = (
        dict(export_manifest.get("hardware_compatibility") or {})
        if prebuilt_tensorrt_available
        else _deployment_runtime_hardware_compatibility(
            settings=settings,
            export_manifest=export_manifest,
        )
    )
    if not onnx_source.exists():
        raise FileNotFoundError(
            f"Expected ONNX export for model version {model_version_id} at {onnx_source}."
        )
    _validate_model_artifact(onnx_source, artifact_format="onnx")
    class_map = _class_map_from_export(export_manifest)
    deployment_io_schema = _deployment_io_schema(
        export_manifest=export_manifest,
        onnx_source=onnx_source,
        class_count=len(class_map),
    )
    deployment_preprocessing = _deployment_preprocessing(export_manifest)
    instance_group_counts = {
        TargetPath.REALTIME: settings.triton.realtime.instance_group_count,
        TargetPath.BATCH: settings.triton.batch.instance_group_count,
    }
    if manifest_path.exists():
        existing_manifest = load_json_file(manifest_path)
        if _existing_package_matches(
            existing_manifest,
            io_schema=deployment_io_schema,
            preprocessing=deployment_preprocessing,
            hardware_compatibility=package_hardware_compatibility,
            temporal=dict(export_manifest.get("temporal") or {}),
            instance_group_counts=instance_group_counts,
            required_artifact_format=(
                "tensorrt_plan"
                if (
                    settings.env != EnvironmentName.DEV
                    and not settings.training.allow_onnx_deployment_fallback
                )
                else None
            ),
        ):
            return existing_manifest
        quarantine_path(
            package_root,
            quarantine_root=settings.storage.quarantine_category_root("deployment-packages"),
            label="stale_package_rebuild",
        )

    temp_root = package_root.with_name(
        f"{package_root.name}.tmp_{hashlib.sha256(model_version_id.encode()).hexdigest()[:8]}"
    )
    if temp_root.exists():
        quarantine_path(
            temp_root,
            quarantine_root=settings.storage.quarantine_category_root("deployment-packages"),
            label="partial_package_build",
        )
    temp_root.mkdir(parents=True, exist_ok=True)

    try:
        source_root = temp_root / "source"
        source_root.mkdir(parents=True, exist_ok=True)

        trainer_mode = str(export_manifest.get("trainer_mode") or "unknown").lower()
        if trainer_mode == "simulation":
            raise ValueError(
                "Simulation-trained model versions cannot be packaged for deployment."
            )
        serving_source = tensorrt_source if tensorrt_uri and tensorrt_source.exists() else None
        serving_artifact_format = "tensorrt_plan"
        if serving_source is not None:
            _validate_model_artifact(serving_source, artifact_format=serving_artifact_format)
        if serving_source is None:
            try:
                serving_source = _build_tensorrt_engine_from_onnx(
                    export_manifest=export_manifest,
                    onnx_source=onnx_source,
                    target_root=source_root,
                )
            except RuntimeError:
                if (
                    settings.env != EnvironmentName.DEV
                    and not settings.training.allow_onnx_deployment_fallback
                ):
                    raise
                _ensure_onnx_deployment_fallback_allowed(export_manifest)
                serving_source = onnx_source
                serving_artifact_format = "onnx"
        _validate_model_artifact(serving_source, artifact_format=serving_artifact_format)

        class_map_path = source_root / "class_map.json"
        _write_json(class_map_path, class_map)

        deployment_export_manifest = dict(export_manifest)
        deployment_export_manifest["io_schema"] = deployment_io_schema
        deployment_export_manifest["preprocessing"] = deployment_preprocessing
        deployment_export_manifest["hardware_compatibility"] = package_hardware_compatibility

        copied_export_manifest = source_root / "export_manifest.json"
        copied_artifact_manifest = source_root / "artifact_manifest.json"
        _write_json(copied_export_manifest, deployment_export_manifest)
        _write_json(copied_artifact_manifest, artifact_manifest)

        onnx_copy = source_root / onnx_source.name
        shutil.copy2(onnx_source, onnx_copy)
        temporal_artifact_copies = _copy_temporal_artifacts(
            export_manifest=deployment_export_manifest,
            source_root=source_root,
        )
        serving_copy = source_root / serving_source.name
        if serving_source != serving_copy:
            shutil.copy2(serving_source, serving_copy)

        variants: list[ServingTargetVariant] = []
        for target_path in (TargetPath.REALTIME, TargetPath.BATCH):
            variant_root = temp_root / target_path.value
            variant_root.mkdir(parents=True, exist_ok=True)
            artifact_name = (
                "model.plan" if serving_artifact_format == "tensorrt_plan" else "model.onnx"
            )
            variant_artifact = variant_root / artifact_name
            shutil.copy2(serving_copy, variant_artifact)
            config_text = render_config_pbtxt(
                stable_model_name="package-preview",
                target_path=target_path,
                task_type=task_type,
                io_schema=deployment_io_schema,
                preferred_batch_sizes=[] if target_path == TargetPath.REALTIME else [4, 8, 16],
                max_batch_size=int(
                    deployment_export_manifest["hardware_compatibility"]["max_batch_size"][
                        target_path.value
                    ]
                ),
                instance_group_count=instance_group_counts[target_path],
                platform=_triton_platform(serving_artifact_format),
                default_model_filename=artifact_name,
            )
            config_path = variant_root / "config.pbtxt"
            config_path.write_text(config_text, encoding="utf-8")
            variants.append(
                ServingTargetVariant(
                    target_path=target_path,
                    purpose="low_latency" if target_path == TargetPath.REALTIME else "throughput",
                    max_batch_size=int(
                        deployment_export_manifest["hardware_compatibility"]["max_batch_size"][
                            target_path.value
                        ]
                    ),
                    artifact_file=artifact_name,
                    artifact_format=serving_artifact_format,
                    artifact_sha256=sha256_file(variant_artifact),
                    artifact_size_bytes=variant_artifact.stat().st_size,
                    dynamic_batching=target_path == TargetPath.BATCH,
                    preferred_batch_sizes=[] if target_path == TargetPath.REALTIME else [4, 8, 16],
                    max_queue_delay_microseconds=None
                    if target_path == TargetPath.REALTIME
                    else 2_000,
                    instance_group_count=instance_group_counts[target_path],
                    config_sha256=sha256_bytes(config_text.encode("utf-8")),
                    config_path=str(config_path.relative_to(temp_root)).replace("\\", "/"),
                )
            )

        package_manifest = DeploymentPackageManifest(
            model_version_id=model_version_id,
            model_family_id=model_family_id,
            model_family_slug=model_family_slug,
            version_no=version_no,
            task_type=task_type,
            contract_major=contract_major,
            built_at=utc_now_iso(),
            training_artifact_manifest_uri=artifact_manifest_uri,
            training_export_manifest_uri=export_manifest_uri,
            package_root=str(package_root),
            package_digest="",
            immutable_metadata_sha256=immutable_metadata_sha256,
            class_map_version=str(
                deployment_export_manifest.get("class_map_version") or f"{model_version_id}:class-map"
            ),
            class_map_sha256=str(
                deployment_export_manifest.get("class_map_sha256") or sha256_file(class_map_path)
            ),
            class_count=int(deployment_export_manifest.get("class_count") or len(class_map)),
            confidence_threshold_default=float(
                deployment_export_manifest.get("confidence_threshold_default") or 0.5
            ),
            preprocessing=deployment_preprocessing,
            postprocessing=dict(deployment_export_manifest.get("postprocessing") or {}),
            anomaly=dict(deployment_export_manifest.get("anomaly") or {}),
            temporal=dict(deployment_export_manifest.get("temporal") or {}),
            nms=NmsConfig.model_validate(deployment_export_manifest.get("nms") or {}),
            io_schema=ModelIoSchema.model_validate(deployment_io_schema),
            hardware_compatibility=HardwareCompatibility.model_validate(
                deployment_export_manifest["hardware_compatibility"]
            ),
            compatible_targets=[TargetPath.REALTIME, TargetPath.BATCH],
            variants=variants,
            source_manifests=_source_manifests(
                copied_artifact_manifest=copied_artifact_manifest,
                copied_export_manifest=copied_export_manifest,
                class_map_path=class_map_path,
                onnx_copy=onnx_copy,
                serving_copy=serving_copy,
                serving_artifact_format=serving_artifact_format,
                temporal_artifact_copies=temporal_artifact_copies,
            ),
        )
        package_manifest.package_digest = sha256_bytes(
            json.dumps(package_manifest.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        )
        _write_json(temp_root / "manifest.json", package_manifest.model_dump(mode="json"))

        package_root.parent.mkdir(parents=True, exist_ok=True)
        promote_staged_directory(source=temp_root, destination=package_root)
        return package_manifest.model_dump(mode="json")
    except Exception:
        quarantine_path(
            temp_root,
            quarantine_root=settings.storage.quarantine_category_root("deployment-packages"),
            label="failed_package_build",
        )
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_descriptor(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _class_map_from_export(export_manifest: dict[str, Any]) -> list[str]:
    postprocessing = export_manifest.get("postprocessing") or {}
    class_map = postprocessing.get("class_map")
    if isinstance(class_map, list):
        return [str(item) for item in class_map]
    class_count = int(export_manifest.get("class_count") or 0)
    return [f"class_{index}" for index in range(class_count)]


def _validate_model_artifact(path: Path, *, artifact_format: str) -> None:
    if path.stat().st_size <= 0:
        raise ValueError(f"Deployment artifact {path} is empty.")
    prefix = path.read_bytes()[:64].lstrip()
    if prefix.startswith(b"{") or prefix.startswith(b"["):
        raise ValueError(
            f"Deployment artifact {path} is metadata JSON, not a {artifact_format} model."
        )


def _existing_package_matches(
    manifest: dict[str, Any],
    *,
    io_schema: dict[str, Any],
    preprocessing: dict[str, Any],
    hardware_compatibility: dict[str, Any],
    temporal: dict[str, Any] | None = None,
    instance_group_counts: dict[TargetPath, int] | None = None,
    required_artifact_format: str | None = None,
) -> bool:
    if not (
        bool(manifest)
        and manifest.get("io_schema") == io_schema
        and manifest.get("preprocessing") == preprocessing
        and manifest.get("hardware_compatibility") == hardware_compatibility
        and manifest.get("temporal", {}) == (temporal or {})
    ):
        return False
    package_root = Path(str(manifest.get("package_root") or ""))
    if str(manifest.get("task_type") or "") == "temporal_sequence":
        for name in (
            "temporal_contract",
            "scaler",
            "angle_contract",
            "head_class_order",
            "confidence_calibration",
            "temporal_evaluation",
        ):
            descriptor = (manifest.get("source_manifests") or {}).get(name)
            if not isinstance(descriptor, dict):
                return False
            path = package_root / str(descriptor.get("path") or "")
            if not path.is_file() or sha256_file(path) != str(descriptor.get("sha256") or ""):
                return False
    for variant in manifest.get("variants") or []:
        artifact_format = str(variant.get("artifact_format") or "tensorrt_plan")
        if required_artifact_format and artifact_format != required_artifact_format:
            return False
        try:
            target_path = TargetPath(str(variant["target_path"]))
        except (KeyError, ValueError):
            return False
        if instance_group_counts is not None and int(
            variant.get("instance_group_count") or 1
        ) != int(instance_group_counts[target_path]):
            return False
        config_path = package_root / str(variant.get("config_path") or "")
        expected_config = render_config_pbtxt(
            stable_model_name="package-preview",
            target_path=target_path,
            task_type=str(manifest.get("task_type") or ""),
            io_schema=io_schema,
            preferred_batch_sizes=list(variant.get("preferred_batch_sizes") or []),
            max_batch_size=int(variant.get("max_batch_size") or 1),
            instance_group_count=int(variant.get("instance_group_count") or 1),
            platform=_triton_platform(artifact_format),
            default_model_filename=str(variant.get("artifact_file") or "model.plan"),
        )
        if not config_path.exists() or config_path.read_text(encoding="utf-8") != expected_config:
            return False
    return True


def _deployment_runtime_hardware_compatibility(
    *,
    settings: PlatformSettings,
    export_manifest: dict[str, Any],
) -> dict[str, Any]:
    source = dict(export_manifest.get("hardware_compatibility") or {})
    raw_batch_sizes = source.get("max_batch_size")
    batch_sizes = raw_batch_sizes if isinstance(raw_batch_sizes, dict) else {}
    target_gpu_profile = settings.triton.realtime.gpu_profile
    return {
        "accelerator": "nvidia_gpu",
        "target_gpu_profile": target_gpu_profile,
        "min_compute_capability": str(source.get("min_compute_capability") or "7.5"),
        "cuda_version": settings.triton.realtime.cuda_version,
        "tensorrt_version": settings.triton.realtime.tensorrt_version,
        "precision": str(source.get("precision") or "fp16"),
        "max_batch_size": {
            "realtime": _positive_int(batch_sizes.get(TargetPath.REALTIME.value), 1),
            "batch": _positive_int(batch_sizes.get(TargetPath.BATCH.value), 16),
        },
        "validated_devices": [target_gpu_profile],
    }


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _deployment_io_schema(
    *,
    export_manifest: dict[str, Any],
    onnx_source: Path,
    class_count: int,
) -> dict[str, Any]:
    schema = json.loads(json.dumps(export_manifest.get("io_schema") or {}))
    graph_schema = _onnx_graph_io_schema(onnx_source)
    if graph_schema is None:
        return _fallback_deployment_io_schema(
            schema,
            class_count=class_count,
            task_type=str(export_manifest.get("task_type") or ""),
        )
    task_type = str(export_manifest.get("task_type") or "")
    input_tensors = list(schema.get("input_tensors") or [])
    fallback_input = dict(input_tensors[0]) if input_tensors else {}
    fallback_input_dims = list(fallback_input.get("dims") or [])
    input_shape = _manifest_dims_from_onnx_shape(graph_schema.get("input_shape") or [])
    if _has_dynamic_dim(input_shape) and _positive_int_dims(fallback_input_dims):
        input_shape = fallback_input_dims
    input_tensor = {
        "name": graph_schema["input_name"],
        "data_type": graph_schema.get("input_data_type") or fallback_input.get("data_type") or "FP32",
        "dims": input_shape or fallback_input.get("dims") or [3, 640, 640],
        "layout": fallback_input.get("layout") or "CHW",
    }
    output_tensors = []
    for output in graph_schema.get("outputs") or []:
        output_name = str(output["name"])
        single_dynamic_dim = None if _is_detection_like_task(task_type) else class_count
        dims = _manifest_dims_from_onnx_shape(
            output.get("shape") or [],
            single_dynamic_dim=single_dynamic_dim,
        )
        fallback_dims = _fallback_output_dims(
            tensor={"name": output_name},
            class_count=class_count,
            task_type=task_type,
        )
        if _is_detection_tensor_shape_ambiguous(
            {"name": output_name, "dims": dims},
            task_type=task_type,
            class_count=class_count,
        ):
            dims = fallback_dims
        output_tensors.append(
            {
                "name": output_name,
                "data_type": str(output.get("data_type") or "FP32"),
                "dims": dims or fallback_dims,
            }
        )
    if not output_tensors:
        output_tensors = list(schema.get("output_tensors") or [])
    schema.pop("schema_digest", None)
    schema.update(
        {
            "input_tensors": [input_tensor],
            "output_tensors": output_tensors,
            "contract_major": int(schema.get("contract_major") or 1),
        }
    )
    schema["schema_digest"] = sha256_bytes(json.dumps(schema, sort_keys=True).encode("utf-8"))
    return schema


def _fallback_deployment_io_schema(
    schema: dict[str, Any],
    *,
    class_count: int,
    task_type: str = "",
) -> dict[str, Any]:
    normalized = json.loads(json.dumps(schema or {}))
    output_tensors: list[dict[str, Any]] = []
    for tensor in normalized.get("output_tensors") or []:
        output_tensor = dict(tensor)
        fallback_dims = _fallback_output_dims(
            tensor=output_tensor,
            class_count=class_count,
            task_type=task_type,
        )
        output_tensor["dims"] = _manifest_dims_from_onnx_shape(
            output_tensor.get("dims") or [],
            single_dynamic_dim=class_count,
        ) or fallback_dims
        if fallback_dims and _is_detection_tensor_shape_ambiguous(
            output_tensor,
            task_type=task_type,
            class_count=class_count,
        ):
            output_tensor["dims"] = fallback_dims
        output_tensors.append(output_tensor)
    normalized.pop("schema_digest", None)
    if output_tensors:
        normalized["output_tensors"] = output_tensors
    normalized["contract_major"] = int(normalized.get("contract_major") or 1)
    normalized["schema_digest"] = sha256_bytes(json.dumps(normalized, sort_keys=True).encode("utf-8"))
    return normalized


def _fallback_output_dims(
    *,
    tensor: dict[str, Any],
    class_count: int,
    task_type: str,
) -> list[Any]:
    if _is_detection_like_task(task_type):
        kind = _detection_output_kind(str(tensor.get("name") or ""))
        if kind in {"dets", "boxes"}:
            return [-1, -1]
        if kind in {"labels", "scores"}:
            return [-1]
    return [class_count]


def _is_detection_tensor_shape_ambiguous(
    tensor: dict[str, Any],
    *,
    task_type: str,
    class_count: int,
) -> bool:
    if not _is_detection_like_task(task_type):
        return False
    if _detection_output_kind(str(tensor.get("name") or "")) is None:
        return False
    dims = list(tensor.get("dims") or [])
    return len(dims) <= 1 and (not dims or dims == [class_count] or dims == [str(class_count)])


def _is_detection_like_task(task_type: str) -> bool:
    return str(task_type).lower() in {"detection", "oriented_detection"}


def _has_dynamic_dim(dims: list[Any]) -> bool:
    return any(not isinstance(item, int) or item <= 0 for item in dims)


def _positive_int_dims(dims: list[Any]) -> bool:
    return bool(dims) and all(isinstance(item, int) and item > 0 for item in dims)


def _detection_output_kind(name: str) -> str | None:
    normalized = name.lower()
    if any(token in normalized for token in ("label", "class")):
        return "labels"
    if any(token in normalized for token in ("score", "conf")):
        return "scores"
    if any(token in normalized for token in ("box", "bbox")):
        return "boxes"
    if "det" in normalized:
        return "dets"
    return None


def _deployment_preprocessing(export_manifest: dict[str, Any]) -> dict[str, Any]:
    preprocessing = dict(export_manifest.get("preprocessing") or {})
    trainer_backend = str(export_manifest.get("trainer_backend") or "").lower()
    if trainer_backend == "ultralytics-yolo":
        return {
            **preprocessing,
            "normalize": {"mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]},
            "color_format": "rgb",
            "scale": 0.00392156862745098,
        }
    if trainer_backend == "openmmlab-mmyolo":
        return {
            **preprocessing,
            "normalize": {"mean": [0.0, 0.0, 0.0], "std": [255.0, 255.0, 255.0]},
            "color_format": "rgb",
        }
    return preprocessing


def _onnx_graph_io_schema(onnx_path: Path) -> dict[str, Any] | None:
    try:
        import onnx

        model = onnx.load(str(onnx_path), load_external_data=False)
    except Exception:
        return _onnxruntime_graph_io_schema(onnx_path)
    inputs = list(model.graph.input)
    outputs = list(model.graph.output)
    if not inputs or not outputs:
        return None
    first_input = inputs[0]
    return {
        "input_name": str(first_input.name),
        "input_shape": _onnx_value_shape(first_input),
        "input_data_type": _onnx_elem_type_to_manifest_dtype(first_input),
        "outputs": [
            {
                "name": str(item.name),
                "shape": _onnx_value_shape(item),
                "data_type": _onnx_elem_type_to_manifest_dtype(item),
            }
            for item in outputs
        ],
    }


def _onnxruntime_graph_io_schema(onnx_path: Path) -> dict[str, Any] | None:
    try:
        import onnxruntime as ort

        session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    except Exception:
        return None
    inputs = list(session.get_inputs())
    outputs = list(session.get_outputs())
    if not inputs or not outputs:
        return None
    first_input = inputs[0]
    return {
        "input_name": str(first_input.name),
        "input_shape": list(first_input.shape or []),
        "input_data_type": _onnx_type_to_manifest_dtype(str(first_input.type)),
        "outputs": [
            {
                "name": str(item.name),
                "shape": list(item.shape or []),
                "data_type": _onnx_type_to_manifest_dtype(str(item.type)),
            }
            for item in outputs
        ],
    }


def _onnx_value_shape(value_info: Any) -> list[Any]:
    tensor_type = value_info.type.tensor_type
    if not tensor_type.HasField("shape"):
        return []
    shape: list[Any] = []
    for dim in tensor_type.shape.dim:
        if dim.HasField("dim_value"):
            shape.append(int(dim.dim_value))
        elif dim.HasField("dim_param"):
            shape.append(str(dim.dim_param))
        else:
            shape.append(-1)
    return shape


def _onnx_elem_type_to_manifest_dtype(value_info: Any) -> str:
    elem_type = int(value_info.type.tensor_type.elem_type)
    mapping = {
        1: "FP32",
        10: "FP16",
        6: "INT32",
        7: "INT64",
    }
    return mapping.get(elem_type, "FP32")


def _manifest_dims_from_onnx_shape(
    shape: list[Any],
    *,
    single_dynamic_dim: int | None = None,
) -> list[Any]:
    dims = list(shape)
    if len(dims) > 1:
        dims = dims[1:]
    normalized: list[Any] = []
    for item in dims:
        if isinstance(item, int) and item > 0:
            normalized.append(item)
        else:
            normalized.append(-1)
    if single_dynamic_dim is not None and len(normalized) == 1 and normalized[0] <= 0:
        return [int(single_dynamic_dim)]
    return normalized


def _onnx_type_to_manifest_dtype(value: str) -> str:
    value = value.lower()
    if "int64" in value:
        return "INT64"
    if "int32" in value:
        return "INT32"
    if "float16" in value or "fp16" in value:
        return "FP16"
    return "FP32"


def _ensure_onnx_deployment_fallback_allowed(export_manifest: dict[str, Any]) -> None:
    artifact_validation = export_manifest.get("artifact_validation") or {}
    checks = artifact_validation.get("checks") or {}
    if not bool(checks.get("onnx_valid")):
        raise ValueError(
            "The ONNX export has not passed artifact validation and cannot be packaged "
            "for deployment."
        )


def _triton_platform(artifact_format: str) -> str:
    if artifact_format == "onnx":
        return "onnxruntime_onnx"
    return "tensorrt_plan"


def _source_manifests(
    *,
    copied_artifact_manifest: Path,
    copied_export_manifest: Path,
    class_map_path: Path,
    onnx_copy: Path,
    serving_copy: Path,
    serving_artifact_format: str,
    temporal_artifact_copies: dict[str, Path] | None = None,
) -> dict[str, Any]:
    payload = {
        "artifact_manifest": {
            "path": "source/artifact_manifest.json",
            "sha256": sha256_file(copied_artifact_manifest),
        },
        "export_manifest": {
            "path": "source/export_manifest.json",
            "sha256": sha256_file(copied_export_manifest),
        },
        "class_map": {
            "path": "source/class_map.json",
            "sha256": sha256_file(class_map_path),
        },
        "onnx": {
            "path": f"source/{onnx_copy.name}",
            "sha256": sha256_file(onnx_copy),
        },
        "deployment_artifact": {
            "path": f"source/{serving_copy.name}",
            "format": serving_artifact_format,
            "sha256": sha256_file(serving_copy),
        },
    }
    if serving_artifact_format == "tensorrt_plan":
        payload["tensorrt"] = {
            "path": f"source/{serving_copy.name}",
            "sha256": sha256_file(serving_copy),
        }
    for name, path in sorted((temporal_artifact_copies or {}).items()):
        payload[name] = {
            "path": f"source/{path.name}",
            "sha256": sha256_file(path),
        }
    return payload


def _copy_temporal_artifacts(
    *,
    export_manifest: dict[str, Any],
    source_root: Path,
) -> dict[str, Path]:
    if str(export_manifest.get("task_type") or "") != "temporal_sequence":
        return {}
    temporal = export_manifest.get("temporal") or {}
    artifacts = temporal.get("artifacts") if isinstance(temporal, dict) else None
    required = {
        "temporal_contract": "temporal-contract.json",
        "scaler": "scaler.json",
        "angle_contract": "angle-contract.json",
        "head_class_order": "head-class-order.json",
        "confidence_calibration": "confidence-calibration.json",
        "temporal_evaluation": "temporal-evaluation.json",
    }
    if not isinstance(artifacts, dict):
        raise ValueError("Temporal deployment export is missing versioned contract artifacts.")
    copies: dict[str, Path] = {}
    for name, file_name in required.items():
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, dict):
            raise ValueError(f"Temporal deployment export is missing artifact {name}.")
        source = Path(str(descriptor.get("uri") or ""))
        expected_sha256 = str(descriptor.get("sha256") or "").removeprefix("sha256:")
        if not source.is_file():
            raise FileNotFoundError(f"Temporal deployment artifact is missing: {source}.")
        if not expected_sha256 or sha256_file(source) != expected_sha256:
            raise ValueError(f"Temporal deployment artifact checksum changed: {name}.")
        destination = source_root / file_name
        shutil.copy2(source, destination)
        copies[name] = destination
    return copies


def _build_tensorrt_engine_from_onnx(
    *,
    export_manifest: dict[str, Any],
    onnx_source: Path,
    target_root: Path,
) -> Path:
    build_manifest = export_manifest.get("build_manifest") or {}
    stage = str(build_manifest.get("stage") or "")
    if stage != "deployment":
        raise FileNotFoundError(
            "The export manifest does not include a real TensorRT engine and does not declare "
            "deployment-stage engine building."
        )
    artifact_validation = export_manifest.get("artifact_validation") or {}
    if not bool((artifact_validation.get("checks") or {}).get("onnx_valid")):
        raise ValueError(
            "The ONNX export has not passed artifact validation and cannot be packaged for deployment."
        )

    io_schema = export_manifest.get("io_schema") or {}
    input_tensor = (io_schema.get("input_tensors") or [{}])[0]
    input_name = str(input_tensor.get("name") or "input")
    dims = input_tensor.get("dims") or [3, 640, 640]
    try:
        input_dims = [int(value) for value in dims]
    except (TypeError, ValueError) as exc:
        raise ValueError("TensorRT packaging requires fixed positive input dimensions.") from exc
    if not input_dims or any(value <= 0 for value in input_dims):
        raise ValueError("TensorRT packaging requires fixed positive input dimensions.")
    max_batch_map = (export_manifest.get("hardware_compatibility") or {}).get("max_batch_size", {})
    max_batch = int(
        max(
            int(max_batch_map.get("realtime") or 0),
            int(max_batch_map.get("batch") or 0),
            1,
        )
    )
    engine_path = target_root / "model.plan"
    tensorrt_onnx_source = _prepare_tensorrt_compatible_onnx(
        onnx_source=onnx_source,
        target_root=target_root,
    )
    trtexec_binary = _find_trtexec_binary()
    if trtexec_binary is not None:
        return _build_tensorrt_engine_with_trtexec(
            trtexec_binary=trtexec_binary,
            onnx_source=tensorrt_onnx_source,
            engine_path=engine_path,
            input_name=input_name,
            input_dims=input_dims,
            max_batch=max_batch,
            fp16_mode=(
                str(
                    (export_manifest.get("hardware_compatibility") or {}).get("precision") or ""
                ).lower()
                == "fp16"
            ),
        )

    from_onnx, is_available, load, save = _load_mmdeploy_tensorrt_api()
    if not is_available():
        raise RuntimeError("MMDeploy TensorRT backend is not available in this runtime.")

    engine = from_onnx(
        str(tensorrt_onnx_source),
        str(target_root / "engine_build"),
        {
            input_name: {
                "min_shape": [1, *input_dims],
                "opt_shape": [1, *input_dims],
                "max_shape": [max(max_batch, 1), *input_dims],
            }
        },
        fp16_mode=(
            str(
                (export_manifest.get("hardware_compatibility") or {}).get("precision") or ""
            ).lower()
            == "fp16"
        ),
        device_id=0,
    )
    save(engine, str(engine_path))
    load(str(engine_path))
    return engine_path


def _prepare_tensorrt_compatible_onnx(
    *,
    onnx_source: Path,
    target_root: Path,
) -> Path:
    try:
        import numpy as np
        import onnx
        from onnx import helper as onnx_helper, numpy_helper
    except ImportError as exc:
        LOGGER.warning("Skipping TensorRT ONNX preparation; ONNX tooling is unavailable: %s", exc)
        return onnx_source
    try:
        model = onnx.load(str(onnx_source))
    except Exception as exc:
        LOGGER.warning("Skipping TensorRT ONNX preparation; could not load %s: %s", onnx_source, exc)
        return onnx_source

    initializers = {initializer.name: initializer for initializer in model.graph.initializer}
    changed = False
    existing_names = _onnx_graph_names(model)
    capped_topk_inputs = 0
    clamped_topk_inputs = 0
    for node_index, node in enumerate(list(model.graph.node)):
        if node.op_type != "TopK" or len(node.input) < 2:
            continue
        k_input = node.input[1]
        initializer = initializers.get(k_input)
        if initializer is not None:
            array = numpy_helper.to_array(initializer)
            current_limit = _static_topk_value(np, array)
            if current_limit is None:
                _clamp_topk_k_input_to_limit(
                    np,
                    onnx_helper,
                    numpy_helper,
                    model=model,
                    node=node,
                    node_index=node_index,
                    existing_names=existing_names,
                )
                clamped_topk_inputs += 1
                changed = True
                continue
            capped = _cap_static_topk_array(np, array)
            if capped is not None:
                initializer.CopyFrom(numpy_helper.from_array(capped, name=initializer.name))
                capped_topk_inputs += 1
                changed = True
            continue
        constant_node = next(
            (candidate for candidate in model.graph.node if k_input in set(candidate.output)),
            None,
        )
        if constant_node is not None:
            constant_cap_result = _cap_topk_constant_node(np, numpy_helper, constant_node)
            if constant_cap_result is not None:
                if constant_cap_result:
                    capped_topk_inputs += 1
                    changed = True
                continue
        _clamp_topk_k_input_to_limit(
            np,
            onnx_helper,
            numpy_helper,
            model=model,
            node=node,
            node_index=node_index,
            existing_names=existing_names,
        )
        clamped_topk_inputs += 1
        changed = True

    if not changed:
        return onnx_source
    target_root.mkdir(parents=True, exist_ok=True)
    patched_path = target_root / f"{onnx_source.stem}.tensorrt.onnx"
    onnx.save(model, str(patched_path))
    LOGGER.info(
        "Prepared TensorRT-compatible ONNX copy %s from %s; capped_topk_inputs=%s, clamped_topk_inputs=%s",
        patched_path,
        onnx_source,
        capped_topk_inputs,
        clamped_topk_inputs,
    )
    return patched_path


def _clamp_topk_k_input_to_limit(
    np,  # noqa: ANN001
    onnx_helper,  # noqa: ANN001
    numpy_helper,  # noqa: ANN001
    *,
    model: Any,
    node: Any,
    node_index: int,
    existing_names: set[str],
) -> str:
    limit_name = _unique_onnx_name(
        existing_names,
        f"{node.name or f'topk_{node_index}'}_tensorrt_k_cap",
    )
    output_name = _unique_onnx_name(
        existing_names | {limit_name},
        f"{node.name or f'topk_{node_index}'}_tensorrt_k",
    )
    min_node_name = _unique_onnx_name(
        existing_names | {limit_name, output_name},
        f"{node.name or f'topk_{node_index}'}_tensorrt_k_min",
    )
    model.graph.initializer.append(
        numpy_helper.from_array(
            np.array([TENSORRT_TOPK_K_LIMIT], dtype=np.int64),
            name=limit_name,
        )
    )
    min_node = onnx_helper.make_node(
        "Min",
        [node.input[1], limit_name],
        [output_name],
        name=min_node_name,
    )
    model.graph.node.insert(_onnx_node_index(model, node), min_node)
    node.input[1] = output_name
    existing_names.update({limit_name, output_name, min_node_name})
    return output_name


def _onnx_node_index(model: Any, target_node: Any) -> int:
    target_outputs = list(target_node.output)
    for index, candidate in enumerate(model.graph.node):
        if candidate is target_node or list(candidate.output) == target_outputs:
            return index
    return len(model.graph.node)


def _onnx_graph_names(model: Any) -> set[str]:
    names: set[str] = set()
    for value_info in list(model.graph.input) + list(model.graph.output) + list(model.graph.value_info):
        if value_info.name:
            names.add(value_info.name)
    for initializer in model.graph.initializer:
        if initializer.name:
            names.add(initializer.name)
    for node in model.graph.node:
        if node.name:
            names.add(node.name)
        names.update(item for item in node.output if item)
    return names


def _unique_onnx_name(existing_names: set[str], base_name: str) -> str:
    candidate = "".join(char if char.isalnum() or char in "._-" else "_" for char in base_name)
    candidate = candidate.strip("_") or "tensorrt_topk_limit"
    if candidate not in existing_names:
        return candidate
    index = 1
    while f"{candidate}_{index}" in existing_names:
        index += 1
    return f"{candidate}_{index}"


def _static_topk_value(np, array: Any) -> int | None:  # noqa: ANN001
    values = np.asarray(array)
    if values.size != 1 or not np.issubdtype(values.dtype, np.integer):
        return None
    return int(values.reshape(-1)[0])


def _cap_static_topk_array(np, array: Any):  # noqa: ANN001
    current = _static_topk_value(np, array)
    if current is None or current <= TENSORRT_TOPK_K_LIMIT:
        return None
    capped = np.asarray(array).copy()
    capped[...] = TENSORRT_TOPK_K_LIMIT
    return capped


def _cap_topk_constant_node(np, numpy_helper, node) -> bool | None:  # noqa: ANN001
    for attribute in node.attribute:
        if attribute.name == "value":
            array = numpy_helper.to_array(attribute.t)
            current_limit = _static_topk_value(np, array)
            if current_limit is None:
                return None
            if current_limit <= TENSORRT_TOPK_K_LIMIT:
                return False
            capped = _cap_static_topk_array(np, array)
            if capped is None:
                return False
            attribute.t.CopyFrom(numpy_helper.from_array(capped, name=attribute.t.name))
            return True
        if attribute.name == "value_int":
            if int(attribute.i) <= TENSORRT_TOPK_K_LIMIT:
                return False
            attribute.i = TENSORRT_TOPK_K_LIMIT
            return True
        if attribute.name == "value_ints" and list(attribute.ints):
            values = list(attribute.ints)
            if len(values) != 1:
                return None
            if int(values[0]) <= TENSORRT_TOPK_K_LIMIT:
                return False
            del attribute.ints[:]
            attribute.ints.append(TENSORRT_TOPK_K_LIMIT)
            return True
    return None


def _find_trtexec_binary() -> Path | None:
    env_path = os.environ.get("CV_PLATFORM_TRTEXEC_BIN", "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    triton_bin = os.environ.get("TRITONSERVER_BIN", "").strip()
    if triton_bin:
        candidates.append(Path(triton_bin).with_name("trtexec"))
    candidates.extend(
        [
            Path("/opt/tritonserver/bin/trtexec"),
            Path("/usr/src/tensorrt/bin/trtexec"),
            Path("/usr/local/tensorrt/bin/trtexec"),
        ]
    )
    found = shutil.which("trtexec")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _build_tensorrt_engine_with_trtexec(
    *,
    trtexec_binary: Path,
    onnx_source: Path,
    engine_path: Path,
    input_name: str,
    input_dims: list[int],
    max_batch: int,
    fp16_mode: bool,
) -> Path:
    engine_path.parent.mkdir(parents=True, exist_ok=True)
    min_shape = _format_trtexec_shape(input_name, [1, *input_dims])
    opt_shape = _format_trtexec_shape(input_name, [1, *input_dims])
    max_shape = _format_trtexec_shape(input_name, [max(max_batch, 1), *input_dims])
    command = [
        str(trtexec_binary),
        f"--onnx={onnx_source}",
        f"--saveEngine={engine_path}",
        f"--minShapes={min_shape}",
        f"--optShapes={opt_shape}",
        f"--maxShapes={max_shape}",
    ]
    if fp16_mode:
        command.append("--fp16")

    result = subprocess.run(
        command,
        cwd=engine_path.parent,
        env=_trtexec_environment(trtexec_binary),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=1800,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "TensorRT engine build failed via trtexec. "
            f"Exit code: {result.returncode}. "
            f"{_trtexec_failure_hint(result.stdout)}"
            f"Output tail:\n{_tail_text(result.stdout)}"
        )
    if not engine_path.exists() or engine_path.stat().st_size <= 0:
        raise RuntimeError(
            "TensorRT engine build via trtexec completed but did not create a non-empty engine: "
            f"{engine_path}. Output tail:\n{_tail_text(result.stdout)}"
        )
    return engine_path


def _format_trtexec_shape(input_name: str, dims: list[int]) -> str:
    return f"{input_name}:{'x'.join(str(dim) for dim in dims)}"


def _trtexec_environment(trtexec_binary: Path) -> dict[str, str]:
    env = dict(os.environ)
    triton_root = trtexec_binary.parent.parent
    candidates = [
        triton_root / "external-libs",
        triton_root / "lib",
        triton_root / "backends" / "tensorrt",
        triton_root / "backends" / "onnxruntime",
        Path("/opt/tritonserver/external-libs"),
        Path("/opt/tritonserver/lib"),
        Path("/usr/lib/wsl/lib"),
        Path("/usr/local/cuda/lib64"),
        Path("/usr/local/cuda/compat"),
        Path("/usr/lib/x86_64-linux-gnu"),
    ]
    existing = [str(path) for path in candidates if path.exists()]
    if existing:
        current = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(existing + ([current] if current else []))
    return env


def _tail_text(value: str | None, *, max_lines: int = 80) -> str:
    lines = (value or "").splitlines()
    return "\n".join(lines[-max_lines:])


def _trtexec_failure_hint(output: str | None) -> str:
    text = output or ""
    if "ITopKLayer" in text and "K exceeds the maximum value allowed" in text:
        return (
            f"TensorRT rejected a TopK operator above its static K limit "
            f"({TENSORRT_TOPK_K_LIMIT}); TopK constants are capped before the build, "
            "and unresolved TopK K inputs are clamped with min(original_k, limit) in "
            "the deployment-only ONNX copy. "
        )
    return ""


def _load_mmdeploy_tensorrt_api():
    return (
        _resolve_mmdeploy_tensorrt_symbol("from_onnx"),
        _resolve_mmdeploy_tensorrt_symbol("is_available"),
        _resolve_mmdeploy_tensorrt_symbol("load"),
        _resolve_mmdeploy_tensorrt_symbol("save"),
    )


def _resolve_mmdeploy_tensorrt_symbol(name: str):
    errors: list[str] = []
    for module_name in (
        "mmdeploy.apis.tensorrt",
        "mmdeploy.backend.tensorrt",
        "mmdeploy.backend.tensorrt.utils",
    ):
        try:
            module = import_module(module_name)
        except ImportError as exc:
            errors.append(f"{module_name}: {exc}")
            continue
        value = getattr(module, name, None)
        if value is not None:
            return value
        errors.append(f"{module_name}: missing {name}")
    raise RuntimeError(
        "MMDeploy TensorRT tooling is required to build deployment engines. "
        f"Could not resolve {name}: {'; '.join(errors)}"
    )
