"""Serving resources follow the existing runtime execution bindings."""

from __future__ import annotations

from collections import defaultdict
import logging
from pathlib import Path

from sqlalchemy import select

from cv_db import get_deployment_package_detail
from cv_db.models import DeploymentRelease
from cv_db.runtime_model_store import demanded_release_ids, lock_model_lifecycle
from cv_deploy.triton import TritonHttpControlClient, TritonRepositoryManager
from cv_status import TargetPath
from cv_utils import utc_now_iso


def _allows_local_onnx(settings, manifest, target):
    return bool(settings.training.allow_onnx_deployment_fallback) and any(
        item.get("target_path") == target.value and item.get("artifact_format") == "onnx"
        for item in (manifest or {}).get("variants", [])
    )


def _validate_local_model(release, manifest) -> None:
    sources = manifest.get("source_manifests") or {}
    keys = (
        ("openvino", "onnx", "deployment_artifact")
        if manifest.get("task_type") == "anomaly"
        else ("deployment_artifact", "onnx")
    )
    artifact = next(
        (
            sources[key]
            for key in keys
            if isinstance(sources.get(key), dict) and sources[key].get("path")
        ),
        None,
    )
    if artifact is None:
        raise ValueError(f"Required local model {release.id} has no inference artifact.")
    path = Path(str(manifest.get("package_root") or "")) / str(artifact["path"])
    if manifest.get("task_type") == "anomaly":
        from anomalib.deploy import OpenVINOInferencer

        model = OpenVINOInferencer(path=str(path))
    else:
        import onnxruntime as ort

        model = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        if not model.get_inputs() or not model.get_outputs():
            raise ValueError(f"Local model {release.id} has no input/output tensors.")
    # The gateway owns the serving cache; this is only the start-time load check.
    del model
    release.smoke_report = {
        "deployment_release_id": release.id,
        "target_path": release.target_path,
        "checked_at": utc_now_iso(),
        "passed": True,
        "checks": [
            {
                "name": "local_model_load",
                "passed": True,
                "detail": "Local inference artifact loaded successfully at channel start.",
            }
        ],
    }


def reconcile_runtime_models(
    session,
    settings,
    *,
    validate_local: bool = False,
    extra_demanded_release_ids: set[str] | None = None,
) -> None:
    """Caller commits demand and resource changes under the same PostgreSQL lock.

    Never unload a model merely because one channel stopped. Draining executions
    retain demand until their executor and in-flight trigger/gateway calls finish.
    """
    lock_model_lifecycle(session)
    session.flush()
    demanded = demanded_release_ids(session) | set(extra_demanded_release_ids or ())
    releases = session.scalars(select(DeploymentRelease)).all()
    groups = defaultdict(list)
    known = defaultdict(set)
    manifests_by_release = {}
    for release in releases:
        if (
            release.status == "activation_failed"
            and release.activated_at is not None
            and not release.deleted_at
            and not release.retired_at
            and (release.smoke_report or {}).get("passed")
            and (release.compatibility_report or {}).get("compatible", True)
        ):
            # Repair historical passive-reload failures without loading unused models.
            release.status = "activated"
        target = TargetPath(release.target_path)
        detail = get_deployment_package_detail(session, model_version_id=release.model_version_id)
        manifest = detail.serving_manifest if detail else {}
        manifests_by_release[release.id] = manifest
        if release.id in demanded and (
            release.deleted_at
            or release.retired_at
            or release.status not in {"activated", "rollback_requested"}
        ):
            raise ValueError(f"Required deployment release {release.id} is unavailable.")
        if manifest and (
            manifest.get("simulation_only") is True or manifest.get("trainer_mode") == "simulation"
        ):
            continue
        known[target].add(release.triton_model_name)
        if manifest and manifest.get("task_type") == "anomaly":
            if release.id in demanded and validate_local:
                _validate_local_model(release, manifest)
            continue
        if release.id in demanded:
            if (
                release.deleted_at
                or release.retired_at
                or release.status not in {"activated", "rollback_requested"}
            ):
                raise ValueError(f"Required deployment release {release.id} is unavailable.")
            groups[(target, release.triton_model_name)].append(release)
    missing = demanded - {release.id for release in releases}
    if missing:
        raise ValueError(f"Required deployment releases do not exist: {sorted(missing)}")
    manager = TritonRepositoryManager(settings)
    for target, names in known.items():
        endpoint = (
            settings.triton.realtime if target == TargetPath.REALTIME else settings.triton.batch
        )
        client = TritonHttpControlClient(
            endpoint_url=endpoint.http_url, timeout_seconds=endpoint.timeout_seconds
        )
        try:
            index = client.repository_index(ready_only=False)
        except Exception:
            required = [
                release for key, values in groups.items() if key[0] == target for release in values
            ]
            if required and not all(
                _allows_local_onnx(settings, manifests_by_release[item.id], target)
                for item in required
            ):
                raise
            if not required:
                logging.getLogger(__name__).warning(
                    "Could not release unused %s models; will retry.", target
                )
                continue
            index = []
        ready = defaultdict(set)
        loaded = set()
        for item in index:
            if item.get("state") in {"READY", "LOADING", "UNLOADING"}:
                loaded.add(item["name"])
            if item.get("state") == "READY" and str(item.get("version", "")).isdigit():
                ready[item["name"]].add(int(item["version"]))
        # Free unused models first, including when loading another model fails.
        # This also avoids holding their GPU memory during a channel restart.
        for name in sorted(names & loaded):
            if not groups.get((target, name)):
                client.unload_model(name)
        for name in sorted(names):
            needed = groups.get((target, name), [])
            if not needed:
                continue
            versions = {release.triton_model_version for release in needed}
            if ready[name] == versions and all(
                (release.smoke_report or {}).get("passed") for release in needed
            ):
                continue
            manifests = []
            for release in sorted(needed, key=lambda item: item.triton_model_version):
                manifest = manifests_by_release[release.id]
                if not manifest:
                    raise ValueError(f"Required release {release.id} has no serving package.")
                manifests.append(manifest)
            configs = {
                manager._render_target_config(
                    stable_model_name=name, target_path=target, package_manifest=manifest
                )
                for manifest in manifests
            }
            if len(configs) != 1:
                raise ValueError(f"Running channels require incompatible versions of {name}.")
            for release, manifest in zip(
                sorted(needed, key=lambda item: item.triton_model_version), manifests
            ):
                manager.stage_release(
                    target_path=target,
                    stable_model_name=name,
                    triton_model_version=release.triton_model_version,
                    package_manifest=manifest,
                    allow_repository_config_replacement=True,
                )
            try:
                manager.load_required_versions(
                    target_path=target,
                    stable_model_name=name,
                    versions=list(versions),
                    package_manifest=manifests[-1],
                    triton_client=client,
                )
                for version in versions:
                    if not client.is_model_ready(name, version):
                        raise RuntimeError(f"Triton model {name}:{version} is not ready.")
            except Exception:
                if all(_allows_local_onnx(settings, manifest, target) for manifest in manifests):
                    if validate_local:
                        for release, manifest in zip(
                            sorted(needed, key=lambda item: item.triton_model_version), manifests
                        ):
                            _validate_local_model(release, manifest)
                    logging.getLogger(__name__).warning(
                        "Using configured local ONNX fallback for %s.", name
                    )
                    continue
                raise
            for release, manifest in zip(
                sorted(needed, key=lambda item: item.triton_model_version), manifests
            ):
                report = manager.smoke_test_report(
                    deployment_release_id=release.id,
                    target_path=target,
                    stable_model_name=name,
                    triton_model_version=release.triton_model_version,
                    package_manifest=manifest,
                    triton_client=client,
                )
                if not report.get("passed"):
                    raise RuntimeError(f"Runtime model validation failed for {release.id}.")
                release.smoke_report = report
