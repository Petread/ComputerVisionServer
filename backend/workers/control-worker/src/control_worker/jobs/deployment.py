from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cv_core import load_platform_settings
from cv_db import (
    activate_deployment_model_release,
    create_deployment_release_for_promotion,
    deactivate_deployment_model_releases,
    get_deployment_package_detail,
    mark_promotion_staged,
    promote_staged_release_to_production,
    rollback_target_to_previous_release,
    session_scope,
    update_deployment_release_status,
    update_model_serving_package,
    validate_deployment_model_serving_command,
)
from cv_db.models import DeploymentRelease, DeploymentTarget, ModelPromotion, ModelVersion
from cv_deploy import (
    TritonHttpControlClient,
    TritonRepositoryManager,
    build_deployment_package as build_serving_package,
)
from cv_status import (
    DeploymentPackageStatus,
    DeploymentReleaseStatus,
    ModelLifecycleStatus,
    TargetPath,
)
from cv_utils import utc_now, utc_now_iso


def build_deployment_package(model_version_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        model_version = session.get(ModelVersion, model_version_id)
        detail = get_deployment_package_detail(session, model_version_id=model_version_id)
        if model_version is None:
            raise ValueError(f"Model version {model_version_id} does not exist.")
        if detail is None:
            raise ValueError(f"Model version {model_version_id} does not exist.")
        if model_version.status not in {
            ModelLifecycleStatus.TRAINED.value,
            ModelLifecycleStatus.VALIDATED.value,
            ModelLifecycleStatus.STAGED.value,
            ModelLifecycleStatus.PRODUCTION.value,
        }:
            raise ValueError(
                f"Model version {model_version_id} must be approved before a deployment package can be built."
            )
        if not detail.export_manifest:
            raise ValueError(
                f"Model version {model_version_id} does not have a validated export manifest to package."
            )
        with session_scope(settings) as inner_session:
            update_model_serving_package(
                inner_session,
                model_version_id=model_version_id,
                status=DeploymentPackageStatus.BUILDING,
                error=None,
            )
    try:
        package_manifest = build_serving_package(
            settings=settings,
            model_version_id=detail.model_version_id,
            model_family_id=detail.model_family_id,
            model_family_slug=detail.model_family_slug,
            version_no=detail.version_no,
            task_type=detail.task_type.value,
            contract_major=int(detail.export_manifest.get("io_schema", {}).get("contract_major") or 1),
            immutable_metadata_sha256=None,
            export_manifest=detail.export_manifest,
            artifact_manifest=detail.artifact_manifest,
            export_manifest_uri=detail.export_manifest_uri,
            artifact_manifest_uri=detail.artifact_manifest_uri,
        )
        manifest_uri = str(settings.storage.serving_package_root(model_version_id) / "manifest.json")
        with session_scope(settings) as session:
            update_model_serving_package(
                session,
                model_version_id=model_version_id,
                status=DeploymentPackageStatus.READY,
                built_at=utc_now(),
                serving_manifest_uri=manifest_uri,
                serving_manifest=package_manifest,
                error=None,
            )
        return {
            "model_version_id": model_version_id,
            "status": DeploymentPackageStatus.READY.value,
            "serving_manifest_uri": manifest_uri,
            "finished_at": utc_now_iso(),
        }
    except Exception as exc:
        with session_scope(settings) as session:
            update_model_serving_package(
                session,
                model_version_id=model_version_id,
                status=DeploymentPackageStatus.FAILED,
                error=str(exc),
            )
        raise


def stage_model_release(promotion_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        promotion = session.get(ModelPromotion, promotion_id)
        if promotion is None:
            raise ValueError(f"Promotion {promotion_id} does not exist.")
        release = create_deployment_release_for_promotion(session, promotion_id=promotion_id)
        model_version_id = release.model_version_id
        target_path = TargetPath(release.target_path)

    build_deployment_package(model_version_id)
    manager = TritonRepositoryManager(settings)
    with session_scope(settings) as session:
        package_detail = get_deployment_package_detail(session, model_version_id=model_version_id)
        if package_detail is None or not package_detail.serving_manifest:
            raise ValueError(
                f"Model version {model_version_id} does not have an immutable serving package to stage."
            )
        allow_repository_config_replacement = _can_replace_triton_repository_config(
            session,
            target_path=target_path,
            stable_model_name=release.triton_model_name,
        )
    with session_scope(settings) as session:
        update_deployment_release_status(
            session,
            deployment_release_id=release.id,
            status=DeploymentReleaseStatus.PACKAGING,
        )
    compatibility_report = manager.compatibility_report(
        target_path=target_path,
        model_version_id=model_version_id,
        stable_model_name=release.triton_model_name,
        package_manifest=package_detail.serving_manifest,
        allow_repository_config_replacement=allow_repository_config_replacement,
    )
    with session_scope(settings) as session:
        update_deployment_release_status(
            session,
            deployment_release_id=release.id,
            status=DeploymentReleaseStatus.REPOSITORY_SYNC,
            compatibility_report=compatibility_report,
        )
    if not bool(compatibility_report.get("compatible")):
        with session_scope(settings) as session:
            update_deployment_release_status(
                session,
                deployment_release_id=release.id,
                status=DeploymentReleaseStatus.ACTIVATION_FAILED,
                compatibility_report=compatibility_report,
            )
        raise ValueError(
            "Serving package for model version "
            f"{model_version_id} is incompatible with target {target_path.value}: "
            f"{_summarize_compatibility_errors(compatibility_report)}"
        )

    manager.stage_release(
        target_path=target_path,
        stable_model_name=release.triton_model_name,
        triton_model_version=release.triton_model_version,
        package_manifest=package_detail.serving_manifest,
        allow_repository_config_replacement=allow_repository_config_replacement,
    )
    client = _triton_client(settings, target_path)
    with session_scope(settings) as session:
        update_deployment_release_status(
            session,
            deployment_release_id=release.id,
            status=DeploymentReleaseStatus.LOADING,
        )
    try:
        manager.load_release(
            target_path=target_path,
            stable_model_name=release.triton_model_name,
            triton_model_version=release.triton_model_version,
            triton_client=client,
        )
        smoke_report = manager.smoke_test_report(
            deployment_release_id=release.id,
            target_path=target_path,
            stable_model_name=release.triton_model_name,
            triton_model_version=release.triton_model_version,
            package_manifest=package_detail.serving_manifest,
            triton_client=client,
        )
        smoke_report_uri = _write_smoke_report(
            settings=settings,
            deployment_release_id=release.id,
            smoke_report=smoke_report,
        )
    except Exception as exc:
        simulated_runtime = _uses_simulated_runtime(package_detail.serving_manifest)
        onnx_fallback_runtime = _uses_local_onnx_fallback_runtime(
            settings=settings,
            package_manifest=package_detail.serving_manifest,
            target_path=target_path,
        )
        if onnx_fallback_runtime:
            smoke_report = _build_local_onnx_fallback_smoke_report(
                settings=settings,
                deployment_release_id=release.id,
                target_path=target_path,
                stable_model_name=release.triton_model_name,
                triton_model_version=release.triton_model_version,
                package_manifest=package_detail.serving_manifest,
                triton_error=str(exc),
            )
        else:
            smoke_report = _build_load_failure_smoke_report(
                deployment_release_id=release.id,
                target_path=target_path,
                error=str(exc),
                simulated=simulated_runtime,
            )
        smoke_report_uri = _write_smoke_report(
            settings=settings,
            deployment_release_id=release.id,
            smoke_report=smoke_report,
        )
        with session_scope(settings) as session:
            update_deployment_release_status(
                session,
                deployment_release_id=release.id,
                status=(
                    DeploymentReleaseStatus.SMOKE_TESTING
                    if simulated_runtime or onnx_fallback_runtime
                    else DeploymentReleaseStatus.ACTIVATION_FAILED
                ),
                smoke_report_uri=smoke_report_uri,
                smoke_report=smoke_report,
            )
        if not simulated_runtime and not onnx_fallback_runtime:
            raise
    with session_scope(settings) as session:
        update_deployment_release_status(
            session,
            deployment_release_id=release.id,
            status=DeploymentReleaseStatus.SMOKE_TESTING,
            smoke_report_uri=smoke_report_uri,
            smoke_report=smoke_report,
        )
    if not bool(smoke_report.get("passed")):
        with session_scope(settings) as session:
            update_deployment_release_status(
                session,
                deployment_release_id=release.id,
                status=DeploymentReleaseStatus.ACTIVATION_FAILED,
                smoke_report_uri=smoke_report_uri,
                smoke_report=smoke_report,
            )
        raise ValueError(
            f"Smoke verification failed for deployment release {release.id}; production traffic remains unchanged."
        )
    with session_scope(settings) as session:
        mark_promotion_staged(session, promotion_id=promotion_id)
    return {
        "promotion_id": promotion_id,
        "deployment_release_id": release.id,
        "status": "staged",
        "finished_at": utc_now_iso(),
    }


def run_release_smoke_test(deployment_release_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    manager = TritonRepositoryManager(settings)
    with session_scope(settings) as session:
        release = session.get(DeploymentRelease, deployment_release_id)
        if release is None:
            raise ValueError(f"Deployment release {deployment_release_id} does not exist.")
        package_detail = get_deployment_package_detail(session, model_version_id=release.model_version_id)
        if package_detail is None or not package_detail.serving_manifest:
            raise ValueError(
                f"Deployment release {deployment_release_id} does not have an immutable "
                "serving package manifest."
            )
    target_path = TargetPath(release.target_path)
    client = _triton_client(settings, target_path)
    smoke_report = manager.smoke_test_report(
        deployment_release_id=release.id,
        target_path=target_path,
        stable_model_name=release.triton_model_name,
        triton_model_version=release.triton_model_version,
        package_manifest=package_detail.serving_manifest,
        triton_client=client,
    )
    smoke_report_uri = _write_smoke_report(
        settings=settings,
        deployment_release_id=release.id,
        smoke_report=smoke_report,
    )
    with session_scope(settings) as session:
        update_deployment_release_status(
            session,
            deployment_release_id=release.id,
            status=release.status if isinstance(release.status, DeploymentReleaseStatus) else DeploymentReleaseStatus(release.status),
            smoke_report_uri=smoke_report_uri,
            smoke_report=smoke_report,
        )
    return {
        "deployment_release_id": deployment_release_id,
        "status": "passed" if smoke_report.get("passed") else "failed",
        "finished_at": utc_now_iso(),
    }


def deactivate_deployment_model(deployment_release_id: str) -> dict[str, object]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        release, changed_release_ids = deactivate_deployment_model_releases(
            session,
            deployment_release_id=deployment_release_id,
        )
        target_path = TargetPath(release.target_path)
        model_name = release.triton_model_name

    client = _triton_client(settings, target_path)
    if _triton_model_is_loaded(client, model_name=model_name):
        client.unload_model(model_name)
    return {
        "deployment_release_id": deployment_release_id,
        "triton_model_name": model_name,
        "target_path": target_path.value,
        "status": DeploymentReleaseStatus.DEACTIVATED.value,
        "deactivated_release_ids": changed_release_ids,
        "finished_at": utc_now_iso(),
    }


def activate_deployment_model(deployment_release_id: str) -> dict[str, object]:
    settings = load_platform_settings("control-worker")
    manager = TritonRepositoryManager(settings)
    with session_scope(settings) as session:
        release = validate_deployment_model_serving_command(
            session,
            deployment_release_id=deployment_release_id,
            enable=True,
        )
        target_path = TargetPath(release.target_path)
        model_name = release.triton_model_name
        model_version = release.triton_model_version
        package_detail = get_deployment_package_detail(
            session,
            model_version_id=release.model_version_id,
        )
        if package_detail is None or not package_detail.serving_manifest:
            raise ValueError(
                f"Deployment release {deployment_release_id} does not have an immutable "
                "serving package manifest."
            )
        package_manifest = package_detail.serving_manifest
        allow_repository_config_replacement = _can_replace_triton_repository_config(
            session,
            target_path=target_path,
            stable_model_name=model_name,
        )
        update_deployment_release_status(
            session,
            deployment_release_id=deployment_release_id,
            status=DeploymentReleaseStatus.LOADING,
        )
    try:
        manager.stage_release(
            target_path=target_path,
            stable_model_name=model_name,
            triton_model_version=model_version,
            package_manifest=package_manifest,
            allow_repository_config_replacement=allow_repository_config_replacement,
        )
        client = _triton_client(settings, target_path)
        manager.load_release(
            target_path=target_path,
            stable_model_name=model_name,
            triton_model_version=model_version,
            triton_client=client,
        )
        if not client.is_model_ready(model_name, model_version):
            raise RuntimeError(
                f"Triton did not mark {model_name}:{model_version} ready after activation."
            )
    except Exception:
        with session_scope(settings) as session:
            update_deployment_release_status(
                session,
                deployment_release_id=deployment_release_id,
                status=DeploymentReleaseStatus.DEACTIVATED,
            )
        raise
    with session_scope(settings) as session:
        activate_deployment_model_release(
            session,
            deployment_release_id=deployment_release_id,
        )
    return {
        "deployment_release_id": deployment_release_id,
        "triton_model_name": model_name,
        "triton_model_version": model_version,
        "target_path": target_path.value,
        "status": DeploymentReleaseStatus.ACTIVATED.value,
        "finished_at": utc_now_iso(),
    }


def promote_model_release(promotion_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        target = promote_staged_release_to_production(session, promotion_id=promotion_id)
    return {
        "promotion_id": promotion_id,
        "target_path": target.target_path,
        "active_release_id": target.active_release_id or "",
        "status": ModelLifecycleStatus.PRODUCTION.value,
        "finished_at": utc_now_iso(),
    }


def rollback_model_release(target_path: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    manager = TritonRepositoryManager(settings)
    resolved_target_path = TargetPath(target_path)
    with session_scope(settings) as session:
        target = session.get(DeploymentTarget, target_path)
        if target is None or target.previous_release_id is None:
            raise ValueError(f"Target {target_path} does not have a previous release to roll back to.")
        previous_release = session.get(DeploymentRelease, target.previous_release_id)
        if previous_release is None:
            raise ValueError(
                f"Target {target_path} references a previous release that no longer exists."
            )
        if not manager.ensure_release_present(
            target_path=resolved_target_path,
            stable_model_name=previous_release.triton_model_name,
            triton_model_version=previous_release.triton_model_version,
        ):
            raise ValueError(
                f"Rollback candidate {previous_release.id} is missing from the Triton repository and cannot be activated."
            )
        package_detail = get_deployment_package_detail(session, model_version_id=previous_release.model_version_id)
        if not _uses_local_onnx_fallback_runtime(
            settings=settings,
            package_manifest=package_detail.serving_manifest if package_detail else None,
            target_path=resolved_target_path,
        ):
            client = _triton_client(settings, resolved_target_path)
            manager.load_release(
                target_path=resolved_target_path,
                stable_model_name=previous_release.triton_model_name,
                triton_model_version=previous_release.triton_model_version,
                triton_client=client,
            )
        target = rollback_target_to_previous_release(session, target_path=target_path)
    return {
        "target_path": target.target_path,
        "active_release_id": target.active_release_id or "",
        "status": "rolled_back",
        "finished_at": utc_now_iso(),
    }


def _triton_client(settings, target_path: TargetPath) -> TritonHttpControlClient:
    endpoint = settings.triton.realtime if target_path == TargetPath.REALTIME else settings.triton.batch
    return TritonHttpControlClient(
        endpoint_url=endpoint.http_url,
        timeout_seconds=endpoint.timeout_seconds,
    )


def _triton_model_is_loaded(
    client: TritonHttpControlClient,
    *,
    model_name: str,
) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("name") or "") == model_name
        and bool(item.get("version") or item.get("state"))
        for item in client.repository_index(ready_only=False)
    )


def _can_replace_triton_repository_config(
    session,
    *,
    target_path: TargetPath,
    stable_model_name: str,
) -> bool:
    target = session.get(DeploymentTarget, target_path.value)
    if target is None or not target.active_release_id:
        return True
    active_release = session.get(DeploymentRelease, target.active_release_id)
    return active_release is None or active_release.triton_model_name != stable_model_name


def _summarize_compatibility_errors(report: dict[str, Any]) -> str:
    checks = report.get("checks") if isinstance(report, dict) else None
    failed = [
        item
        for item in checks or []
        if isinstance(item, dict) and str(item.get("level") or "").lower() == "error"
    ]
    if not failed:
        return "no failing compatibility check was reported"
    return "; ".join(
        f"{item.get('code') or 'compatibility_error'}: {item.get('message') or 'no detail'}"
        for item in failed
    )


def _write_smoke_report(
    *,
    settings,
    deployment_release_id: str,
    smoke_report: dict[str, Any],
) -> str:
    report_path = Path(settings.storage.serving_root) / "history" / f"rel_{deployment_release_id}" / "smoke_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(smoke_report, indent=2, sort_keys=True), encoding="utf-8")
    return str(report_path)


def _uses_simulated_runtime(package_manifest: dict[str, Any] | None) -> bool:
    if not package_manifest:
        return False
    if package_manifest.get("simulation_only") is True:
        return True
    source_manifests = package_manifest.get("source_manifests") or {}
    artifact = source_manifests.get("deployment_artifact") or source_manifests.get("onnx")
    return not (isinstance(artifact, dict) and artifact.get("path"))


def _uses_local_onnx_fallback_runtime(
    *,
    settings,
    package_manifest: dict[str, Any] | None,
    target_path: TargetPath,
) -> bool:
    if not package_manifest or not bool(settings.training.allow_onnx_deployment_fallback):
        return False
    variant = _target_variant(package_manifest, target_path)
    if str(variant.get("artifact_format") or "").lower() == "onnx":
        return True
    source_manifests = package_manifest.get("source_manifests") or {}
    artifact = source_manifests.get("deployment_artifact") or source_manifests.get("onnx")
    return isinstance(artifact, dict) and str(artifact.get("format") or "").lower() == "onnx"


def _target_variant(package_manifest: dict[str, Any], target_path: TargetPath) -> dict[str, Any]:
    variants = package_manifest.get("variants") or []
    for item in variants:
        if isinstance(item, dict) and item.get("target_path") == target_path.value:
            return item
    return {}


def _deployment_artifact_path(package_manifest: dict[str, Any]) -> Path | None:
    package_root = Path(str(package_manifest.get("package_root") or ""))
    source_manifests = package_manifest.get("source_manifests") or {}
    for key in ("deployment_artifact", "onnx"):
        entry = source_manifests.get(key) or {}
        relative = entry.get("path") if isinstance(entry, dict) else None
        if relative:
            candidate = package_root / str(relative)
            if candidate.exists():
                return candidate
    return None


def _build_local_onnx_fallback_smoke_report(
    *,
    settings,
    deployment_release_id: str,
    target_path: TargetPath,
    stable_model_name: str,
    triton_model_version: int,
    package_manifest: dict[str, Any] | None,
    triton_error: str,
) -> dict[str, Any]:
    package_manifest = package_manifest or {}
    version_dir = settings.storage.triton_model_root(target_path, stable_model_name) / str(
        triton_model_version
    )
    artifact_path = _deployment_artifact_path(package_manifest)
    checks: list[dict[str, Any]] = [
        {
            "name": "triton_load",
            "passed": False,
            "detail": "Triton could not load the staged release; local ONNX fallback is enabled.",
            "metadata": {"error": triton_error},
        },
        {
            "name": "repository_version_present",
            "passed": version_dir.exists(),
            "detail": "Immutable repository version directory exists on disk.",
            "metadata": {"version_dir": str(version_dir)},
        },
        {
            "name": "onnx_artifact_present",
            "passed": artifact_path is not None,
            "detail": "Deployment package contains a local ONNX artifact.",
            "metadata": {"artifact_path": str(artifact_path) if artifact_path else None},
        },
    ]
    session_loads = False
    session_error = None
    if artifact_path is not None:
        try:
            import onnxruntime as ort

            ort.InferenceSession(str(artifact_path), providers=["CPUExecutionProvider"])
            session_loads = True
        except Exception as exc:  # pragma: no cover - depends on target runtime packages.
            session_error = str(exc)
    checks.append(
        {
            "name": "onnxruntime_session_loads",
            "passed": session_loads,
            "detail": "Local ONNX fallback can load the deployment artifact.",
            "metadata": {"error": session_error} if session_error else {},
        }
    )
    return {
        "deployment_release_id": deployment_release_id,
        "target_path": target_path.value,
        "checked_at": utc_now_iso(),
        "passed": all(item["passed"] for item in checks if item["name"] != "triton_load"),
        "checks": checks,
        "fallback": "local_onnx",
    }


def _build_load_failure_smoke_report(
    *,
    deployment_release_id: str,
    target_path: TargetPath,
    error: str,
    simulated: bool,
) -> dict[str, Any]:
    detail = (
        "Simulation-only release accepted without a Triton load because it does not carry a real model artifact."
        if simulated
        else "Triton could not load the staged release."
    )
    return {
        "deployment_release_id": deployment_release_id,
        "target_path": target_path.value,
        "checked_at": utc_now_iso(),
        "passed": simulated,
        "checks": [
            {
                "name": "triton_load",
                "passed": simulated,
                "detail": detail,
                "metadata": {"error": error},
            }
        ],
    }
