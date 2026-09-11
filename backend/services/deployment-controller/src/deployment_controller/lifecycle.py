from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from sqlalchemy import select

from cv_db import get_deployment_package_detail, session_scope
from cv_db.models import DeploymentRelease, DeploymentTarget
from cv_deploy import TritonHttpControlClient, TritonRepositoryManager
from cv_status import DeploymentReleaseStatus, TargetPath

logger = logging.getLogger(__name__)

_STARTUP_RELOAD_ATTEMPTS = 8
_STARTUP_RELOAD_RETRY_SECONDS = 5.0
_RECONCILE_INTERVAL_SECONDS = 60.0
_RECONCILE_RELOAD_ATTEMPTS = 3
_RECONCILE_RELOAD_RETRY_SECONDS = 5.0


async def start_active_release_reloader(app: FastAPI) -> None:
    await reload_active_releases_until_ready(
        app,
        attempts=_STARTUP_RELOAD_ATTEMPTS,
        retry_delay_seconds=_STARTUP_RELOAD_RETRY_SECONDS,
    )
    app.state.active_release_reloader_task = asyncio.create_task(
        _active_release_reconcile_loop(app)
    )


async def stop_active_release_reloader(app: FastAPI) -> None:
    task = getattr(app.state, "active_release_reloader_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def reload_active_releases_until_ready(
    app: FastAPI,
    *,
    attempts: int = 1,
    retry_delay_seconds: float = 0.0,
) -> bool:
    attempts = max(int(attempts or 1), 1)
    retry_delay_seconds = max(float(retry_delay_seconds or 0.0), 0.0)
    missing_after_reload = True
    for attempt in range(1, attempts + 1):
        await reload_active_releases(app)
        missing_after_reload = _model_serving_state_drift(app.state.settings)
        if not missing_after_reload:
            return False
        logger.warning(
            "Triton serving state still differs from deployment state after reload "
            "attempt %s/%s.",
            attempt,
            attempts,
        )
        if attempt < attempts and retry_delay_seconds > 0:
            await asyncio.sleep(retry_delay_seconds)
    return missing_after_reload


async def _active_release_reconcile_loop(app: FastAPI) -> None:
    while not getattr(app.state, "shutting_down", False):
        await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)
        if getattr(app.state, "shutting_down", False):
            return
        try:
            if not _model_serving_state_drift(app.state.settings):
                continue
            logger.warning("Triton serving state differs from deployment state; reconciling.")
            await reload_active_releases_until_ready(
                app,
                attempts=_RECONCILE_RELOAD_ATTEMPTS,
                retry_delay_seconds=_RECONCILE_RELOAD_RETRY_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to reconcile target-active Triton deployment releases.")


async def reload_active_releases(app: FastAPI) -> None:
    settings = app.state.settings
    manager = TritonRepositoryManager(settings)
    with session_scope(settings) as session:
        target_release_rows = session.execute(
            select(
                DeploymentTarget.active_release_id,
                DeploymentTarget.previous_release_id,
            )
        ).all()
        target_active_release_ids = {
            release_id for release_id, _ in target_release_rows if release_id
        }
        target_bound_release_ids = set(target_active_release_ids)
        target_bound_release_ids.update(
            release_id for _, release_id in target_release_rows if release_id
        )
        reload_candidate_releases = session.scalars(
            select(DeploymentRelease)
            .where(
                DeploymentRelease.status.in_(
                    [
                        DeploymentReleaseStatus.ACTIVATED.value,
                        DeploymentReleaseStatus.ACTIVATION_FAILED.value,
                    ]
                ),
                DeploymentRelease.retired_at.is_(None),
                DeploymentRelease.deleted_at.is_(None),
            )
            .order_by(DeploymentRelease.created_at.asc())
        ).all()
        recovered_release_ids: list[str] = []
        for release in reload_candidate_releases:
            if _is_recoverable_passive_reload_failure(release):
                release.status = DeploymentReleaseStatus.ACTIVATED.value
                recovered_release_ids.append(release.id)
        if recovered_release_ids:
            session.flush()
            logger.warning(
                "Recovered deployment releases previously failed by passive reload: %s.",
                ", ".join(sorted(recovered_release_ids)),
            )
        activated_releases = [
            release
            for release in reload_candidate_releases
            if release.status == DeploymentReleaseStatus.ACTIVATED.value
        ]
        latest_releases_by_model: dict[tuple[str, str], DeploymentRelease] = {}
        for release in activated_releases:
            key = (release.target_path, release.triton_model_name)
            current = latest_releases_by_model.get(key)
            if current is None or _deployment_release_order_key(release) > _deployment_release_order_key(current):
                latest_releases_by_model[key] = release
        latest_release_ids = {release.id for release in latest_releases_by_model.values()}
        selected_release_ids = set(latest_release_ids)
        selected_release_ids.update(target_bound_release_ids)
        releases = sorted(
            [
                release
                for release in activated_releases
                if release.id in selected_release_ids
            ],
            key=_deployment_release_order_key,
        )
        release_payloads = []
        for release in releases:
            try:
                target_path = TargetPath(release.target_path)
            except ValueError:
                logger.warning(
                    "Skipping deployment release %s with unknown target path %s.",
                    release.id,
                    release.target_path,
                )
                continue
            package_detail = get_deployment_package_detail(
                session,
                model_version_id=release.model_version_id,
            )
            if package_detail is None or not package_detail.serving_manifest:
                logger.warning(
                    "Skipping activated Triton release %s (%s:%s) because its serving "
                    "package manifest is missing.",
                    release.id,
                    release.triton_model_name,
                    release.triton_model_version,
                )
                continue
            release_payloads.append(
                (
                    release.id,
                    target_path,
                    release.triton_model_name,
                    release.triton_model_version,
                    package_detail.serving_manifest,
                    release.id in target_active_release_ids
                    or release.id in latest_release_ids
                    or _can_replace_triton_repository_config(
                        session,
                        target_path=target_path,
                        stable_model_name=release.triton_model_name,
                    ),
                )
            )
    load_requests: dict[tuple[TargetPath, str], dict[str, object]] = {}
    for (
        release_id,
        target_path,
        model_name,
        model_version,
        package_manifest,
        allow_repository_config_replacement,
    ) in release_payloads:
        try:
            manager.stage_release(
                target_path=target_path,
                stable_model_name=model_name,
                triton_model_version=model_version,
                package_manifest=package_manifest,
                allow_repository_config_replacement=allow_repository_config_replacement,
            )
            load_request = load_requests.setdefault(
                (target_path, model_name),
                {
                    "release_ids": [],
                    "target_path": target_path,
                    "model_name": model_name,
                    "model_version": model_version,
                    "package_manifest": package_manifest,
                },
            )
            release_ids = load_request["release_ids"]
            if isinstance(release_ids, list):
                release_ids.append(release_id)
            load_request["model_version"] = model_version
            load_request["package_manifest"] = package_manifest
        except Exception:
            logger.exception(
                "Failed to stage activated Triton release %s (%s:%s). Package hint: %s.",
                release_id,
                model_name,
                model_version,
                _package_manifest_hint(package_manifest, target_path),
            )
    for load_request in load_requests.values():
        release_ids = [
            str(item)
            for item in load_request["release_ids"]
            if isinstance(item, str)
        ]
        release_id = release_ids[-1] if release_ids else ""
        target_path = load_request["target_path"]
        model_name = str(load_request["model_name"])
        model_version = int(load_request["model_version"])
        package_manifest = load_request["package_manifest"]
        if not isinstance(target_path, TargetPath) or not isinstance(package_manifest, dict):
            continue
        endpoint = (
            settings.triton.realtime
            if target_path == TargetPath.REALTIME
            else settings.triton.batch
        )
        client = TritonHttpControlClient(
            endpoint_url=endpoint.http_url,
            timeout_seconds=endpoint.timeout_seconds,
        )
        try:
            manager.load_release(
                target_path=target_path,
                stable_model_name=model_name,
                triton_model_version=model_version,
                triton_client=client,
            )
        except Exception:
            logger.exception(
                "Failed to load activated Triton model for release %s (%s:%s). "
                "Repository integrity: %s.",
                release_id,
                model_name,
                model_version,
                _repository_integrity_report_or_error(
                    manager=manager,
                    target_path=target_path,
                    stable_model_name=model_name,
                    triton_model_version=model_version,
                    package_manifest=package_manifest,
                ),
            )
            try:
                client.unload_model(model_name)
            except Exception:
                logger.exception(
                    "Failed to unload unavailable Triton model after reload failure %s (%s:%s).",
                    release_id,
                    model_name,
                    model_version,
                )
            logger.warning(
                "Keeping activated deployment release status after passive reload failure "
                "for %s (%s:%s); reconcile will retry instead of undeploying the model.",
                release_id,
                model_name,
                model_version,
            )
    _unload_deactivated_models(settings)


def _model_serving_state_drift(settings) -> bool:
    return _target_active_releases_missing(settings) or _deactivated_models_loaded(settings)


def _deactivated_models_loaded(settings) -> bool:
    specs = _deactivated_model_specs(settings)
    if not specs:
        return False
    loaded_by_target: dict[TargetPath, set[str]] = {}
    for target_path, model_name in specs:
        loaded_names = loaded_by_target.get(target_path)
        if loaded_names is None:
            endpoint = (
                settings.triton.realtime
                if target_path == TargetPath.REALTIME
                else settings.triton.batch
            )
            client = TritonHttpControlClient(
                endpoint_url=endpoint.http_url,
                timeout_seconds=endpoint.timeout_seconds,
            )
            try:
                loaded_names = _loaded_model_names(client)
            except Exception:
                logger.warning(
                    "Could not inspect Triton repository state for deactivated %s models.",
                    target_path.value,
                    exc_info=True,
                )
                return True
            loaded_by_target[target_path] = loaded_names
        if model_name in loaded_names:
            logger.warning(
                "Deactivated Triton model is still loaded: target=%s model=%s.",
                target_path.value,
                model_name,
            )
            return True
    return False


def _unload_deactivated_models(settings) -> None:
    specs = _deactivated_model_specs(settings)
    if not specs:
        return
    clients: dict[TargetPath, TritonHttpControlClient] = {}
    loaded_by_target: dict[TargetPath, set[str]] = {}
    for target_path, model_name in specs:
        client = clients.get(target_path)
        if client is None:
            endpoint = (
                settings.triton.realtime
                if target_path == TargetPath.REALTIME
                else settings.triton.batch
            )
            client = TritonHttpControlClient(
                endpoint_url=endpoint.http_url,
                timeout_seconds=endpoint.timeout_seconds,
            )
            clients[target_path] = client
        loaded_names = loaded_by_target.get(target_path)
        if loaded_names is None:
            try:
                loaded_names = _loaded_model_names(client)
            except Exception:
                logger.warning(
                    "Could not inspect Triton repository before unloading deactivated models "
                    "for %s.",
                    target_path.value,
                    exc_info=True,
                )
                continue
            loaded_by_target[target_path] = loaded_names
        if model_name not in loaded_names:
            continue
        try:
            client.unload_model(model_name)
            loaded_names.discard(model_name)
            logger.info(
                "Unloaded deactivated Triton model: target=%s model=%s.",
                target_path.value,
                model_name,
            )
        except Exception:
            logger.exception(
                "Failed to unload deactivated Triton model: target=%s model=%s.",
                target_path.value,
                model_name,
            )


def _deactivated_model_specs(settings) -> list[tuple[TargetPath, str]]:
    with session_scope(settings) as session:
        rows = session.execute(
            select(
                DeploymentRelease.target_path,
                DeploymentRelease.triton_model_name,
                DeploymentRelease.status,
            ).where(
                DeploymentRelease.retired_at.is_(None),
                DeploymentRelease.deleted_at.is_(None),
            )
        ).all()
    grouped: dict[tuple[TargetPath, str], set[str]] = {}
    for raw_target_path, model_name, status in rows:
        try:
            target_path = TargetPath(str(raw_target_path))
        except ValueError:
            continue
        grouped.setdefault((target_path, str(model_name)), set()).add(str(status))
    serving_or_transitioning = {
        DeploymentReleaseStatus.REQUESTED.value,
        DeploymentReleaseStatus.PACKAGING.value,
        DeploymentReleaseStatus.REPOSITORY_SYNC.value,
        DeploymentReleaseStatus.LOADING.value,
        DeploymentReleaseStatus.SMOKE_TESTING.value,
        DeploymentReleaseStatus.ACTIVATED.value,
        DeploymentReleaseStatus.ROLLBACK_REQUESTED.value,
    }
    return sorted(
        [
            key
            for key, statuses in grouped.items()
            if DeploymentReleaseStatus.DEACTIVATED.value in statuses
            and not statuses.intersection(serving_or_transitioning)
        ],
        key=lambda item: (item[0].value, item[1]),
    )


def _target_active_releases_missing(settings) -> bool:
    specs = _target_active_release_specs(settings)
    if not specs:
        return False
    ready_by_target: dict[TargetPath, set[tuple[str, int]]] = {}
    for target_path, model_name, model_version in specs:
        ready_versions = ready_by_target.get(target_path)
        if ready_versions is None:
            endpoint = (
                settings.triton.realtime
                if target_path == TargetPath.REALTIME
                else settings.triton.batch
            )
            client = TritonHttpControlClient(
                endpoint_url=endpoint.http_url,
                timeout_seconds=endpoint.timeout_seconds,
            )
            try:
                ready_versions = _ready_model_versions(client)
            except Exception:
                logger.warning(
                    "Could not inspect Triton repository readiness for %s.",
                    target_path.value,
                    exc_info=True,
                )
                return True
            ready_by_target[target_path] = ready_versions
        if (model_name, model_version) not in ready_versions:
            logger.warning(
                "Target-active Triton release is not READY: target=%s model=%s version=%s.",
                target_path.value,
                model_name,
                model_version,
            )
            return True
    return False


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


def _package_manifest_hint(
    package_manifest: dict[str, object],
    target_path: TargetPath,
) -> dict[str, object]:
    hint: dict[str, object] = {
        "package_root": package_manifest.get("package_root"),
        "model_version_id": package_manifest.get("model_version_id"),
        "target_path": target_path.value,
    }
    variant = _variant_for_target(package_manifest, target_path)
    if variant is not None:
        artifact_file = str(variant.get("artifact_file") or "model.plan")
        hint.update(
            {
                "artifact_file": artifact_file,
                "artifact_format": variant.get("artifact_format"),
                "artifact_sha256": variant.get("artifact_sha256"),
                "variant_root": f"{package_manifest.get('package_root')}/{target_path.value}",
                "artifact_path": f"{package_manifest.get('package_root')}/{target_path.value}/{artifact_file}",
            }
        )
    return hint


def _repository_integrity_report_or_error(
    *,
    manager: TritonRepositoryManager,
    target_path: TargetPath,
    stable_model_name: str,
    triton_model_version: int,
    package_manifest: dict[str, object],
) -> dict[str, object]:
    try:
        return manager.repository_integrity_report(
            target_path=target_path,
            stable_model_name=stable_model_name,
            triton_model_version=triton_model_version,
            package_manifest=package_manifest,
        )
    except Exception as exc:
        return {"error": str(exc), "package_hint": _package_manifest_hint(package_manifest, target_path)}


def _variant_for_target(
    package_manifest: dict[str, object],
    target_path: TargetPath,
) -> dict[str, object] | None:
    variants = package_manifest.get("variants")
    if not isinstance(variants, list):
        return None
    for item in variants:
        if isinstance(item, dict) and item.get("target_path") == target_path.value:
            return item
    return None


def _target_active_release_specs(settings) -> list[tuple[TargetPath, str, int]]:
    with session_scope(settings) as session:
        rows = session.execute(
            select(
                DeploymentTarget.target_path,
                DeploymentRelease.triton_model_name,
                DeploymentRelease.triton_model_version,
            )
            .join(DeploymentRelease, DeploymentRelease.id == DeploymentTarget.active_release_id)
            .where(
                DeploymentTarget.active_release_id.is_not(None),
                DeploymentRelease.status == DeploymentReleaseStatus.ACTIVATED.value,
                DeploymentRelease.retired_at.is_(None),
                DeploymentRelease.deleted_at.is_(None),
            )
        ).all()
    specs: list[tuple[TargetPath, str, int]] = []
    for raw_target_path, model_name, model_version in rows:
        try:
            target_path = TargetPath(str(raw_target_path))
            version = int(model_version)
        except (TypeError, ValueError):
            continue
        specs.append((target_path, str(model_name), version))
    return specs


def _ready_model_versions(client: TritonHttpControlClient) -> set[tuple[str, int]]:
    ready: set[tuple[str, int]] = set()
    for item in client.repository_index(ready_only=True):
        if not isinstance(item, dict) or item.get("state") != "READY":
            continue
        model_name = item.get("name")
        model_version = item.get("version")
        if model_name is None or model_version is None:
            continue
        try:
            ready.add((str(model_name), int(model_version)))
        except (TypeError, ValueError):
            continue
    return ready


def _loaded_model_names(client: TritonHttpControlClient) -> set[str]:
    return {
        str(item.get("name"))
        for item in client.repository_index(ready_only=False)
        if isinstance(item, dict)
        and item.get("name") is not None
        and bool(item.get("version") or item.get("state"))
    }


def _deployment_release_order_key(release: DeploymentRelease) -> tuple[int, str, str, str]:
    activated_at = release.activated_at or release.created_at
    activated_text = activated_at.isoformat() if activated_at is not None else ""
    created_at = getattr(release, "created_at", None)
    created_text = created_at.isoformat() if created_at is not None else ""
    return (
        int(release.triton_model_version or 0),
        activated_text,
        created_text,
        str(release.id or ""),
    )


def _is_recoverable_passive_reload_failure(release: DeploymentRelease) -> bool:
    if release.status != DeploymentReleaseStatus.ACTIVATION_FAILED.value:
        return False
    if release.activated_at is None or release.retired_at is not None or release.deleted_at is not None:
        return False
    smoke_report = release.smoke_report if isinstance(release.smoke_report, dict) else {}
    if not bool(smoke_report.get("passed")):
        return False
    compatibility_report = release.compatibility_report if isinstance(release.compatibility_report, dict) else {}
    return bool(compatibility_report.get("compatible", True))
