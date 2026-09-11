from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from sqlalchemy import select

from cv_contracts import ModelArchitectureSummary
from cv_core import PlatformSettings
from cv_db import session_scope
from cv_db.models import DeploymentRelease, DeploymentTarget, ModelFamily, ModelVersion
from cv_status import DeploymentReleaseStatus, TargetPath, TaskType


@dataclass(frozen=True)
class ReleaseContext:
    deployment_release_id: str
    target_path: TargetPath
    active: bool
    rollback_candidate: bool
    release_status: DeploymentReleaseStatus
    model_family_id: str
    model_family_slug: str
    model_version_id: str
    model_version_no: int
    task_type: TaskType
    architecture: ModelArchitectureSummary
    triton_model_name: str
    triton_model_version: int
    contract_major: int
    manifest: dict[str, Any]
    class_labels: list[str]
    confidence_threshold_default: float
    input_schema_ref: str | None
    output_schema_ref: str | None


@dataclass(frozen=True)
class PublicModelContext:
    model_id: str
    display_name: str
    description: str | None
    model_family_id: str
    model_family_slug: str
    task_type: TaskType
    architecture: ModelArchitectureSummary
    contract_major: int
    input_schema_ref: str | None
    output_schema_ref: str | None
    class_labels: list[str]
    manifest: dict[str, Any]
    releases: dict[TargetPath, ReleaseContext]


@dataclass
class _CacheEntry:
    value: ReleaseContext | None
    expires_at: float


class ReleaseResolver:
    def __init__(self, settings: PlatformSettings) -> None:
        self._settings = settings
        self._ttl_seconds = settings.inference_limits.routing_cache_ttl_seconds
        self._release_ttl_seconds = max(float(self._ttl_seconds), 60.0)
        self._active_cache: dict[str, _CacheEntry] = {}
        self._release_cache: dict[str, _CacheEntry] = {}
        self._cache_lock = RLock()

    def resolve_active_release(self, *, target_path: TargetPath) -> ReleaseContext | None:
        cache_key = f"active:{target_path.value}"
        with self._cache_lock:
            cached = self._active_cache.get(cache_key)
            if cached and cached.expires_at > time.monotonic():
                return cached.value
            # Hold the lock through the miss so only one concurrent request
            # refreshes a target from PostgreSQL.
            with session_scope(self._settings) as session:
                target = session.get(DeploymentTarget, target_path.value)
                release_id = target.active_release_id if target else None
                value = self._build_context(
                    session,
                    deployment_release_id=release_id,
                    active_release_id=target.active_release_id if target else None,
                    previous_release_id=target.previous_release_id if target else None,
                )
            self._active_cache[cache_key] = _CacheEntry(
                value=value,
                expires_at=time.monotonic() + self._ttl_seconds,
            )
            if value is not None:
                self._release_cache[value.deployment_release_id] = _CacheEntry(
                    value=value,
                    expires_at=time.monotonic() + self._release_ttl_seconds,
                )
            return value

    def resolve_release(
        self,
        *,
        deployment_release_id: str,
        target_path: TargetPath,
    ) -> ReleaseContext | None:
        with self._cache_lock:
            cached = self._release_cache.get(deployment_release_id)
            if cached and cached.expires_at > time.monotonic():
                return cached.value
            with session_scope(self._settings) as session:
                target = session.get(DeploymentTarget, target_path.value)
                value = self._build_context(
                    session,
                    deployment_release_id=deployment_release_id,
                    active_release_id=target.active_release_id if target else None,
                    previous_release_id=target.previous_release_id if target else None,
                )
            self._release_cache[deployment_release_id] = _CacheEntry(
                value=value,
                # Release model content is immutable, so explicit routes can be
                # cached substantially longer than active-target selection. A
                # bounded TTL still propagates retirement/deletion without a
                # gateway restart.
                expires_at=(
                    time.monotonic() + self._release_ttl_seconds
                    if value is not None
                    else time.monotonic() + self._ttl_seconds
                ),
            )
            return value

    def list_release_contexts(self, *, target_path: TargetPath) -> list[ReleaseContext]:
        with session_scope(self._settings) as session:
            target = session.get(DeploymentTarget, target_path.value)
            rows = session.scalars(
                select(DeploymentRelease)
                .where(
                    DeploymentRelease.target_path == target_path.value,
                    DeploymentRelease.status == DeploymentReleaseStatus.ACTIVATED.value,
                    DeploymentRelease.retired_at.is_(None),
                    DeploymentRelease.deleted_at.is_(None),
                )
                .order_by(DeploymentRelease.created_at.desc())
            ).all()
            items = [
                self._build_context(
                    session,
                    deployment_release_id=row.id,
                    active_release_id=target.active_release_id if target else None,
                    previous_release_id=target.previous_release_id if target else None,
                )
                for row in rows
            ]
        return [item for item in items if item is not None]

    def invalidate(self) -> None:
        with self._cache_lock:
            self._active_cache.clear()
            self._release_cache.clear()

    def _build_context(
        self,
        session,
        *,
        deployment_release_id: str | None,
        active_release_id: str | None,
        previous_release_id: str | None,
    ) -> ReleaseContext | None:
        if deployment_release_id is None:
            return None
        release = session.get(DeploymentRelease, deployment_release_id)
        if release is None or release.deleted_at is not None:
            return None
        version = session.get(ModelVersion, release.model_version_id)
        family = session.get(ModelFamily, release.model_family_id)
        if (
            version is None
            or version.deleted_at is not None
            or family is None
            or family.deleted_at is not None
        ):
            return None
        task_type = TaskType(family.task_type)
        manifest = _manifest_payload(version)
        return ReleaseContext(
            deployment_release_id=release.id,
            target_path=TargetPath(release.target_path),
            active=release.id == active_release_id,
            rollback_candidate=release.id == previous_release_id,
            release_status=DeploymentReleaseStatus(release.status),
            model_family_id=release.model_family_id,
            model_family_slug=family.slug,
            model_version_id=version.id,
            model_version_no=version.version_no,
            task_type=task_type,
            architecture=_architecture_summary(
                task_type=task_type,
                family=family.architecture_family,
                version=family.architecture_version,
                size=family.architecture_size,
                display_name=family.architecture_display_name,
            ),
            triton_model_name=release.triton_model_name,
            triton_model_version=release.triton_model_version,
            contract_major=family.contract_major,
            manifest=manifest,
            class_labels=_class_labels(manifest),
            confidence_threshold_default=float(manifest.get("confidence_threshold_default") or 0.5),
            input_schema_ref=manifest.get("io_schema", {}).get("input_schema_ref") or family.input_schema_ref,
            output_schema_ref=manifest.get("io_schema", {}).get("output_schema_ref") or family.output_schema_ref,
        )

    def resolve_public_model_release(
        self,
        *,
        model_id: str,
        target_path: TargetPath,
    ) -> ReleaseContext | None:
        with session_scope(self._settings) as session:
            family = session.scalar(
                select(ModelFamily).where(
                    ModelFamily.slug == model_id,
                    ModelFamily.active.is_(True),
                    ModelFamily.deleted_at.is_(None),
                )
            )
            if family is None:
                return None
            release = session.scalar(
                select(DeploymentRelease)
                .where(
                    DeploymentRelease.model_family_id == family.id,
                    DeploymentRelease.target_path == target_path.value,
                    DeploymentRelease.status == DeploymentReleaseStatus.ACTIVATED.value,
                    DeploymentRelease.retired_at.is_(None),
                    DeploymentRelease.deleted_at.is_(None),
                )
                .order_by(DeploymentRelease.activated_at.desc(), DeploymentRelease.created_at.desc())
            )
            active_release = session.get(DeploymentTarget, target_path.value)
            return self._build_context(
                session,
                deployment_release_id=release.id if release is not None else None,
                active_release_id=active_release.active_release_id if active_release else None,
                previous_release_id=active_release.previous_release_id if active_release else None,
            )

    def get_public_model_context(self, *, model_id: str) -> PublicModelContext | None:
        with session_scope(self._settings) as session:
            family = session.scalar(
                select(ModelFamily).where(
                    ModelFamily.slug == model_id,
                    ModelFamily.active.is_(True),
                    ModelFamily.deleted_at.is_(None),
                )
            )
            if family is None:
                return None
            releases: dict[TargetPath, ReleaseContext] = {}
            for target_path in TargetPath:
                target = session.get(DeploymentTarget, target_path.value)
                release = session.scalar(
                    select(DeploymentRelease)
                    .where(
                        DeploymentRelease.model_family_id == family.id,
                        DeploymentRelease.target_path == target_path.value,
                        DeploymentRelease.status == DeploymentReleaseStatus.ACTIVATED.value,
                        DeploymentRelease.retired_at.is_(None),
                        DeploymentRelease.deleted_at.is_(None),
                    )
                    .order_by(DeploymentRelease.activated_at.desc(), DeploymentRelease.created_at.desc())
                )
                context = self._build_context(
                    session,
                    deployment_release_id=release.id if release is not None else None,
                    active_release_id=target.active_release_id if target else None,
                    previous_release_id=target.previous_release_id if target else None,
                )
                if context is not None:
                    releases[target_path] = context
            manifest = next(iter(releases.values())).manifest if releases else {}
            class_labels = next(iter(releases.values())).class_labels if releases else []
            public_config = manifest.get("public_inference") or {}
            task_type = TaskType(family.task_type)
            return PublicModelContext(
                model_id=family.slug,
                display_name=str(
                    public_config.get("display_name")
                    or family.display_name
                    or _display_name_from_slug(family.slug)
                ),
                description=(
                    str(public_config.get("description"))
                    if public_config.get("description") is not None
                    else family.description
                ),
                model_family_id=family.id,
                model_family_slug=family.slug,
                task_type=task_type,
                architecture=_architecture_summary(
                    task_type=task_type,
                    family=family.architecture_family,
                    version=family.architecture_version,
                    size=family.architecture_size,
                    display_name=family.architecture_display_name,
                ),
                contract_major=family.contract_major,
                input_schema_ref=(manifest.get("io_schema", {}).get("input_schema_ref") or family.input_schema_ref),
                output_schema_ref=(
                    manifest.get("io_schema", {}).get("output_schema_ref") or family.output_schema_ref
                ),
                class_labels=class_labels,
                manifest=manifest,
                releases=releases,
            )

    def list_public_models(self) -> list[PublicModelContext]:
        with session_scope(self._settings) as session:
            families = session.scalars(
                select(ModelFamily)
                .where(ModelFamily.active.is_(True), ModelFamily.deleted_at.is_(None))
                .order_by(ModelFamily.slug.asc())
            ).all()
        items = [
            self.get_public_model_context(model_id=family.slug)
            for family in families
        ]
        return [item for item in items if item is not None]


def _manifest_payload(version: ModelVersion) -> dict[str, Any]:
    if isinstance(version.serving_manifest, dict) and version.serving_manifest:
        return version.serving_manifest
    manifest_uri = version.serving_manifest_uri
    if manifest_uri:
        path = Path(manifest_uri)
        if path.exists():
            import json

            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
    return {}


def _class_labels(manifest: dict[str, Any]) -> list[str]:
    source_manifests = manifest.get("source_manifests") or {}
    class_map_meta = source_manifests.get("class_map") or {}
    class_map_path = class_map_meta.get("path")
    package_root = manifest.get("package_root")
    if class_map_path and package_root:
        path = Path(package_root) / class_map_path
        if path.exists():
            import json

            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                return [str(item) for item in payload]
    class_count = int(manifest.get("class_count") or 0)
    return [f"class_{index}" for index in range(class_count)]


def _display_name_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-"))


def _architecture_summary(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
    display_name: str,
) -> ModelArchitectureSummary:
    return ModelArchitectureSummary(
        task_type=task_type,
        family=family,
        version=version,
        size=size,
        display_name=display_name,
        internal_key=f"{task_type.value}:{family}:{version}:{size}",
    )
