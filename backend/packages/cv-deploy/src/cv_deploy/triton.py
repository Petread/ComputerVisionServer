from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

import httpx

from cv_core import PlatformSettings
from cv_status import EnvironmentName, TargetPath
from cv_utils import promote_staged_directory, quarantine_path, utc_now_iso

from cv_deploy.manifests import (
    CompatibilityCheck,
    CompatibilityReport,
    SmokeTestCheck,
    SmokeTestReport,
)


def triton_model_name(*, target_path: TargetPath, family_slug: str, contract_major: int) -> str:
    prefix = "rt" if target_path == TargetPath.REALTIME else "bt"
    safe_slug = re.sub(r"[^a-z0-9]+", "_", family_slug.lower()).strip("_")
    return f"{prefix}__{safe_slug}_v{contract_major}"


def triton_dtype(data_type: str) -> str:
    mapping = {
        "fp32": "TYPE_FP32",
        "float32": "TYPE_FP32",
        "fp16": "TYPE_FP16",
        "float16": "TYPE_FP16",
        "int64": "TYPE_INT64",
        "int32": "TYPE_INT32",
        "uint8": "TYPE_UINT8",
    }
    return mapping.get(data_type.lower(), "TYPE_FP32")


def render_config_pbtxt(
    *,
    stable_model_name: str,
    target_path: TargetPath,
    task_type: str,
    io_schema: dict[str, Any],
    preferred_batch_sizes: list[int],
    max_batch_size: int,
    instance_group_count: int,
    platform: str = "tensorrt_plan",
    default_model_filename: str = "model.plan",
) -> str:
    inputs = ",\n".join(
        _render_tensor_block("input", tensor) for tensor in io_schema["input_tensors"]
    )
    outputs = ",\n".join(
        _render_tensor_block("output", tensor) for tensor in io_schema["output_tensors"]
    )
    lines = [
        f'name: "{stable_model_name}"',
        f'platform: "{platform}"',
        "version_policy {",
        "  all { }",
        "}",
        f"max_batch_size: {max_batch_size}",
        "input [",
        inputs,
        "]",
        "output [",
        outputs,
        "]",
        "instance_group [",
        "  {",
        "    kind: KIND_GPU",
        f"    count: {instance_group_count}",
        "  }",
        "]",
        f'default_model_filename: "{default_model_filename}"',
        f'parameters: {{ key: "cv_platform_task_type" value: {{ string_value: "{task_type}" }} }}',
    ]
    if target_path == TargetPath.BATCH:
        batch_sizes = ", ".join(str(size) for size in preferred_batch_sizes)
        lines.extend(
            [
                "dynamic_batching {",
                f"  preferred_batch_size: [ {batch_sizes} ]" if batch_sizes else "",
                "  max_queue_delay_microseconds: 2000",
                "}",
            ]
        )
    return "\n".join(line for line in lines if line != "") + "\n"


def _render_tensor_block(kind: str, tensor: dict[str, Any]) -> str:
    dims = ", ".join(_render_dim(item) for item in tensor.get("dims", []))
    layout = tensor.get("layout")
    format_line = ""
    if kind == "input" and layout == "NCHW":
        format_line = "\n    format: FORMAT_NCHW"
    return (
        "  {\n"
        f'    name: "{tensor["name"]}"\n'
        f"    data_type: {triton_dtype(str(tensor['data_type']))}\n"
        f"    dims: [ {dims} ]{format_line}\n"
        "  }"
    )


def _render_dim(item: Any) -> str:
    if isinstance(item, int):
        return str(item)
    value = str(item).strip()
    return value if value.lstrip("-").isdigit() else "-1"


class TritonHttpControlClient:
    def __init__(self, *, endpoint_url: str, timeout_seconds: int) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def load_model(self, model_name: str) -> None:
        self._post(f"/v2/repository/models/{model_name}/load")

    def unload_model(self, model_name: str) -> None:
        self._post(f"/v2/repository/models/{model_name}/unload")

    def repository_index(self, *, ready_only: bool = False) -> list[dict[str, Any]]:
        response = self._post("/v2/repository/index", json={"ready": ready_only})
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def is_server_ready(self) -> bool:
        response = httpx.get(f"{self.endpoint_url}/v2/health/ready", timeout=self.timeout_seconds)
        return response.status_code == 200

    def is_model_ready(self, model_name: str, model_version: int) -> bool:
        response = httpx.get(
            f"{self.endpoint_url}/v2/models/{model_name}/versions/{model_version}/ready",
            timeout=self.timeout_seconds,
        )
        return response.status_code == 200

    def _post(self, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        response = httpx.post(
            f"{self.endpoint_url}{path}",
            json=json,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response


@dataclass(frozen=True)
class RepositoryStageResult:
    model_root: Path
    version_dir: Path
    config_path: Path


class TritonRepositoryManager:
    def __init__(self, settings: PlatformSettings) -> None:
        self.settings = settings

    def compatibility_report(
        self,
        *,
        target_path: TargetPath,
        model_version_id: str,
        stable_model_name: str,
        package_manifest: dict[str, Any],
        allow_repository_config_replacement: bool = False,
    ) -> dict[str, Any]:
        checks: list[CompatibilityCheck] = []
        target_settings = self._target_settings(target_path)
        hardware = package_manifest.get("hardware_compatibility") or {}
        expected_profile = target_settings.gpu_profile
        actual_profile = str(hardware.get("target_gpu_profile") or "")
        if actual_profile != expected_profile:
            checks.append(
                CompatibilityCheck(
                    code="gpu_profile_mismatch",
                    level="error",
                    message="The serving package was built for a different GPU profile.",
                    metadata={"expected": expected_profile, "actual": actual_profile},
                )
            )
        else:
            checks.append(
                CompatibilityCheck(
                    code="gpu_profile_match",
                    level="info",
                    message="Hardware profile matches the target Triton deployment.",
                    metadata={"target_gpu_profile": expected_profile},
                )
            )
        model_root = self.settings.storage.triton_model_root(target_path, stable_model_name)
        config_path = model_root / "config.pbtxt"
        if config_path.exists():
            existing_config = config_path.read_text(encoding="utf-8")
            expected_config = self._render_target_config(
                stable_model_name=stable_model_name,
                target_path=target_path,
                package_manifest=package_manifest,
            )
            if existing_config != expected_config:
                can_replace_config = (
                    self.settings.env == EnvironmentName.DEV
                    or allow_repository_config_replacement
                )
                checks.append(
                    CompatibilityCheck(
                        code="config_pbtxt_mismatch",
                        level="warning" if can_replace_config else "error",
                        message=(
                            "Existing Triton model config differs and will be replaced before staging."
                            if can_replace_config
                            else "Existing Triton model config differs from the immutable serving package."
                        ),
                        metadata={"config_path": str(config_path)},
                    )
                )
        checks.append(
            CompatibilityCheck(
                code="schema_digest_present",
                level="info",
                message="Serving package carries explicit input/output schema compatibility metadata.",
                metadata={"schema_digest": package_manifest["io_schema"]["schema_digest"]},
            )
        )
        report = CompatibilityReport(
            compatible=not any(item.level == "error" for item in checks),
            checked_at=utc_now_iso(),
            target_path=target_path,
            model_version_id=model_version_id,
            checks=checks,
        )
        return report.model_dump(mode="json")

    def stage_release(
        self,
        *,
        target_path: TargetPath,
        stable_model_name: str,
        triton_model_version: int,
        package_manifest: dict[str, Any],
        allow_repository_config_replacement: bool = False,
    ) -> RepositoryStageResult:
        model_root = self.settings.storage.triton_model_root(target_path, stable_model_name)
        version_dir = model_root / str(triton_model_version)
        config_path = model_root / "config.pbtxt"
        variant = self._variant_for_target(package_manifest, target_path)
        package_root = Path(package_manifest["package_root"])
        variant_root = package_root / target_path.value
        expected_config = self._render_target_config(
            stable_model_name=stable_model_name,
            target_path=target_path,
            package_manifest=package_manifest,
        )
        expected_config_sha256 = hashlib.sha256(expected_config.encode("utf-8")).hexdigest()
        quarantine_root = self.settings.storage.quarantine_category_root("triton-repository")
        can_replace_config = (
            self.settings.env == EnvironmentName.DEV
            or allow_repository_config_replacement
        )
        if version_dir.exists():
            artifact_file = str(variant.get("artifact_file") or "model.plan")
            artifact_path = version_dir / artifact_file
            config_matches = config_path.exists() and config_path.read_text(encoding="utf-8") == expected_config
            artifact_matches = (
                artifact_path.exists()
                and sha256_file(artifact_path) == str(variant["artifact_sha256"])
            )
            if config_matches and artifact_matches:
                if can_replace_config:
                    _quarantine_model_versions_with_config_mismatch(
                        model_root,
                        expected_config_sha256=expected_config_sha256,
                        quarantine_root=quarantine_root,
                        keep_version_dir=version_dir,
                    )
                return RepositoryStageResult(
                    model_root=model_root, version_dir=version_dir, config_path=config_path
                )
            if not can_replace_config:
                raise ValueError(
                    f"Triton repository entry for {stable_model_name}:{triton_model_version} "
                    "differs from the immutable serving package."
                )
            quarantine_path(
                version_dir,
                quarantine_root=quarantine_root,
                label="stale_repository_version",
            )
            if config_path.exists() and not config_matches:
                quarantine_path(
                    config_path,
                    quarantine_root=quarantine_root,
                    label="stale_repository_config",
                )
                _quarantine_model_versions(model_root, quarantine_root=quarantine_root)
        if version_dir.exists():
            return RepositoryStageResult(
                model_root=model_root, version_dir=version_dir, config_path=config_path
            )

        model_root.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            existing_config = config_path.read_text(encoding="utf-8")
            if existing_config != expected_config:
                if not can_replace_config:
                    raise ValueError(
                        f"Triton config mismatch for {stable_model_name} at {config_path}; refusing in-place overwrite."
                    )
                quarantine_path(
                    config_path,
                    quarantine_root=quarantine_root,
                    label="stale_repository_config",
                )
                _quarantine_model_versions(model_root, quarantine_root=quarantine_root)
                temp_config = config_path.with_suffix(".pbtxt.tmp")
                temp_config.write_text(expected_config, encoding="utf-8")
                temp_config.replace(config_path)
        else:
            temp_config = config_path.with_suffix(".pbtxt.tmp")
            temp_config.write_text(expected_config, encoding="utf-8")
            temp_config.replace(config_path)
            if can_replace_config:
                _quarantine_model_versions_with_config_mismatch(
                    model_root,
                    expected_config_sha256=expected_config_sha256,
                    quarantine_root=quarantine_root,
                    keep_version_dir=version_dir,
                )

        temp_version_dir = model_root / f".tmp_{triton_model_version}"
        if temp_version_dir.exists():
            shutil.rmtree(temp_version_dir)
        temp_version_dir.mkdir(parents=True, exist_ok=True)
        artifact_file = str(variant.get("artifact_file") or "model.plan")
        shutil.copy2(variant_root / artifact_file, temp_version_dir / artifact_file)
        metadata_path = temp_version_dir / "deployment_metadata.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "model_version_id": package_manifest["model_version_id"],
                    "target_path": target_path.value,
                    "variant": variant,
                    "artifact_file": artifact_file,
                    "packaged_at": package_manifest["built_at"],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        promote_staged_directory(source=temp_version_dir, destination=version_dir)
        return RepositoryStageResult(
            model_root=model_root, version_dir=version_dir, config_path=config_path
        )

    def smoke_test_report(
        self,
        *,
        deployment_release_id: str,
        target_path: TargetPath,
        stable_model_name: str,
        triton_model_version: int,
        package_manifest: dict[str, Any],
        triton_client: TritonHttpControlClient,
    ) -> dict[str, Any]:
        version_dir = self.settings.storage.triton_model_root(target_path, stable_model_name) / str(
            triton_model_version
        )
        variant = self._variant_for_target(package_manifest, target_path)
        artifact_file = str(variant.get("artifact_file") or "model.plan")
        artifact_path = version_dir / artifact_file
        checks = [
            SmokeTestCheck(
                name="repository_version_present",
                passed=version_dir.exists(),
                detail="Immutable Triton version directory exists on disk.",
                metadata={"version_dir": str(version_dir)},
            ),
            SmokeTestCheck(
                name="triton_server_ready",
                passed=triton_client.is_server_ready(),
                detail="Triton HTTP readiness probe succeeded.",
            ),
            SmokeTestCheck(
                name="triton_model_ready",
                passed=triton_client.is_model_ready(stable_model_name, triton_model_version),
                detail="Triton marked the requested model version ready.",
                metadata={"model_name": stable_model_name, "model_version": triton_model_version},
            ),
            SmokeTestCheck(
                name="artifact_checksum_matches_package",
                passed=(
                    version_dir.exists()
                    and artifact_path.exists()
                    and sha256_file(artifact_path) == str(variant["artifact_sha256"])
                ),
                detail="Repository artifact checksum matches the immutable serving package.",
            ),
        ]
        report = SmokeTestReport(
            deployment_release_id=deployment_release_id,
            target_path=target_path,
            checked_at=utc_now_iso(),
            passed=all(item.passed for item in checks),
            checks=checks,
        )
        return report.model_dump(mode="json")

    def load_release(
        self,
        *,
        target_path: TargetPath,
        stable_model_name: str,
        triton_model_version: int,
        triton_client: TritonHttpControlClient,
    ) -> None:
        _ = triton_model_version
        _ = target_path
        triton_client.load_model(stable_model_name)

    def ensure_release_present(
        self,
        *,
        target_path: TargetPath,
        stable_model_name: str,
        triton_model_version: int,
    ) -> bool:
        version_dir = self.settings.storage.triton_model_root(target_path, stable_model_name) / str(
            triton_model_version
        )
        return version_dir.exists()

    def repository_integrity_report(
        self,
        *,
        target_path: TargetPath,
        stable_model_name: str,
        triton_model_version: int,
        package_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        model_root = self.settings.storage.triton_model_root(target_path, stable_model_name)
        version_dir = model_root / str(triton_model_version)
        variant = self._variant_for_target(package_manifest, target_path)
        artifact_file = str(variant.get("artifact_file") or "model.plan")
        artifact_path = version_dir / artifact_file
        config_path = model_root / "config.pbtxt"
        expected_config = self._render_target_config(
            stable_model_name=stable_model_name,
            target_path=target_path,
            package_manifest=package_manifest,
        )
        checks = [
            {
                "name": "version_directory_present",
                "passed": version_dir.exists(),
                "detail": f"Expected Triton version directory at {version_dir}.",
            },
            {
                "name": "artifact_present",
                "passed": artifact_path.exists(),
                "detail": f"Expected deployment artifact at {artifact_path}.",
            },
            {
                "name": "config_present",
                "passed": config_path.exists(),
                "detail": f"Expected config.pbtxt at {config_path}.",
            },
        ]
        if config_path.exists():
            checks.append(
                {
                    "name": "config_matches_package",
                    "passed": config_path.read_text(encoding="utf-8") == expected_config,
                    "detail": "Triton config must match the immutable serving package.",
                }
            )
        if artifact_path.exists():
            checks.append(
                {
                    "name": "artifact_checksum_matches_package",
                    "passed": sha256_file(artifact_path) == str(variant["artifact_sha256"]),
                    "detail": "Triton artifact checksum must match the immutable serving package.",
                }
            )
        return {
            "healthy": all(bool(item["passed"]) for item in checks),
            "target_path": target_path.value,
            "stable_model_name": stable_model_name,
            "triton_model_version": triton_model_version,
            "checked_at": utc_now_iso(),
            "checks": checks,
        }

    def release_manifest(self, package_manifest_uri: str | None) -> dict[str, Any]:
        return load_json_file(package_manifest_uri)

    def _target_settings(self, target_path: TargetPath):
        return (
            self.settings.triton.realtime
            if target_path == TargetPath.REALTIME
            else self.settings.triton.batch
        )

    def _variant_for_target(
        self, package_manifest: dict[str, Any], target_path: TargetPath
    ) -> dict[str, Any]:
        for item in package_manifest.get("variants", []):
            if item["target_path"] == target_path.value:
                return item
        raise ValueError(f"No serving variant found for target {target_path.value}.")

    def _render_target_config(
        self,
        *,
        stable_model_name: str,
        target_path: TargetPath,
        package_manifest: dict[str, Any],
    ) -> str:
        variant = self._variant_for_target(package_manifest, target_path)
        return render_config_pbtxt(
            stable_model_name=stable_model_name,
            target_path=target_path,
            task_type=str(package_manifest["task_type"]),
            io_schema=package_manifest["io_schema"],
            preferred_batch_sizes=list(variant.get("preferred_batch_sizes") or []),
            max_batch_size=int(variant["max_batch_size"]),
            # Serving capacity is an installation concern.  This override also
            # upgrades packages created before instance-group tuning existed.
            instance_group_count=int(
                self._target_settings(target_path).instance_group_count
            ),
            platform=_platform_for_artifact_format(
                str(variant.get("artifact_format") or "tensorrt_plan")
            ),
            default_model_filename=str(variant.get("artifact_file") or "model.plan"),
        )


def load_json_file(path_or_uri: str | Path | None) -> dict[str, Any]:
    if path_or_uri is None:
        return {}
    path = Path(path_or_uri)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_for_artifact_format(artifact_format: str) -> str:
    if artifact_format == "onnx":
        return "onnxruntime_onnx"
    return "tensorrt_plan"


def _quarantine_model_versions(model_root: Path, *, quarantine_root: Path) -> None:
    for child in list(model_root.iterdir()) if model_root.exists() else []:
        if child.is_dir() and child.name.isdigit():
            quarantine_path(
                child,
                quarantine_root=quarantine_root,
                label="stale_repository_version",
            )


def _quarantine_model_versions_with_config_mismatch(
    model_root: Path,
    *,
    expected_config_sha256: str,
    quarantine_root: Path,
    keep_version_dir: Path,
) -> None:
    for child in list(model_root.iterdir()) if model_root.exists() else []:
        if not child.is_dir() or not child.name.isdigit() or child == keep_version_dir:
            continue
        metadata_path = child / "deployment_metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            actual_config_sha256 = str((metadata.get("variant") or {}).get("config_sha256") or "")
        except (OSError, json.JSONDecodeError):
            actual_config_sha256 = ""
        if actual_config_sha256 != expected_config_sha256:
            quarantine_path(
                child,
                quarantine_root=quarantine_root,
                label="stale_repository_version",
            )
