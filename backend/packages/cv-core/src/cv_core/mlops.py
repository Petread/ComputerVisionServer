from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv_core.settings import PlatformSettings
from cv_utils import utc_now_iso


def _resolve_cli(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found

    executable_name = f"{name}.exe" if sys.platform == "win32" else name
    candidate = Path(sys.executable).with_name(executable_name)
    if candidate.is_file():
        return str(candidate)
    return None


@dataclass(frozen=True)
class MlflowRunHandle:
    experiment_id: str
    experiment_name: str
    run_id: str


@dataclass(frozen=True)
class MlflowModelRegistration:
    registered_model_name: str
    model_version: str


class MlflowTrackingAdapter:
    def __init__(self, settings: PlatformSettings) -> None:
        self.settings = settings

    def start_training_run(
        self,
        *,
        model_family_slug: str,
        training_job_id: str,
        dataset_snapshot_id: str,
        parameters: dict[str, Any],
        tags: dict[str, Any],
    ) -> MlflowRunHandle:
        client, entities = self._client()
        experiment_name = f"cv-platform/{model_family_slug}"
        experiment = client.get_experiment_by_name(experiment_name)
        experiment_id = experiment.experiment_id if experiment else client.create_experiment(experiment_name)
        run = client.create_run(
            experiment_id=experiment_id,
            tags={
                "cv_platform.training_job_id": training_job_id,
                "cv_platform.dataset_snapshot_id": dataset_snapshot_id,
                **_stringify_mapping(tags),
            },
            run_name=training_job_id,
        )
        self.log_run_batch(
            run_id=run.info.run_id,
            parameters=parameters,
            metrics={},
            tags={
                "cv_platform.lifecycle": "training",
                "cv_platform.started_at": utc_now_iso(),
            },
            entities=entities,
        )
        return MlflowRunHandle(
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            run_id=run.info.run_id,
        )

    def log_run_batch(
        self,
        *,
        run_id: str,
        parameters: dict[str, Any],
        metrics: dict[str, float],
        tags: dict[str, Any],
        entities=None,  # noqa: ANN001
    ) -> None:
        client, imported_entities = self._client()
        entities = entities or imported_entities
        client.log_batch(
            run_id=run_id,
            metrics=[
                entities.Metric(key=key, value=float(value), timestamp=int(time.time() * 1000), step=0)
                for key, value in metrics.items()
            ],
            params=[
                entities.Param(key=key, value=_stringify_scalar(value))
                for key, value in parameters.items()
            ],
            tags=[
                entities.RunTag(key=key, value=_stringify_scalar(value))
                for key, value in tags.items()
            ],
        )

    def log_metrics(
        self,
        *,
        run_id: str,
        metrics: dict[str, float],
        step: int,
    ) -> None:
        if not metrics:
            return
        client, entities = self._client()
        timestamp_ms = int(time.time() * 1000)
        client.log_batch(
            run_id=run_id,
            metrics=[
                entities.Metric(key=key, value=float(value), timestamp=timestamp_ms, step=int(step))
                for key, value in metrics.items()
            ],
            params=[],
            tags=[],
        )

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        metrics: dict[str, float],
        tags: dict[str, Any],
    ) -> None:
        self.log_run_batch(run_id=run_id, parameters={}, metrics=metrics, tags=tags)
        client, _entities = self._client()
        client.set_terminated(run_id, status=status)

    def log_artifacts(
        self,
        *,
        run_id: str,
        artifact_path: str,
        file_paths: list[Path],
    ) -> str:
        client, _entities = self._client()
        for file_path in file_paths:
            if not file_path.exists():
                continue
            client.log_artifact(run_id, str(file_path), artifact_path=artifact_path)
        return artifact_path

    def register_model_version(
        self,
        *,
        registered_model_name: str,
        source_uri: str,
        run_id: str,
        aliases: list[str],
        tags: dict[str, Any],
    ) -> MlflowModelRegistration:
        client, _entities = self._client()
        try:
            client.create_registered_model(registered_model_name)
        except Exception:
            pass
        model_version = client.create_model_version(
            name=registered_model_name,
            source=source_uri,
            run_id=run_id,
            tags=_stringify_mapping(tags),
        )
        version = str(model_version.version)
        for key, value in _stringify_mapping(tags).items():
            client.set_model_version_tag(registered_model_name, version, key, value)
        for alias in _mlflow_supported_aliases(aliases):
            client.set_registered_model_alias(registered_model_name, alias, version)
        return MlflowModelRegistration(
            registered_model_name=registered_model_name,
            model_version=version,
        )

    def sync_registered_model_metadata(
        self,
        *,
        registered_model_name: str,
        model_version: str,
        aliases: list[str],
        tags: dict[str, Any],
    ) -> None:
        client, _entities = self._client()
        version = str(model_version)
        for key, value in _stringify_mapping(tags).items():
            client.set_model_version_tag(registered_model_name, version, key, value)
        for alias in _mlflow_supported_aliases(aliases):
            client.set_registered_model_alias(registered_model_name, alias, version)

    def _client(self):  # noqa: ANN202
        try:
            import mlflow
            from mlflow import MlflowClient
            from mlflow import entities
        except ImportError as exc:  # pragma: no cover - exercised through monkeypatched fakes in tests
            raise RuntimeError(
                "MLflow support requires the `mlflow` or `mlflow-skinny` package to be installed."
            ) from exc
        # MLflow artifact operations against `mlflow-artifacts:/...` resolve the
        # artifact repository via the process-global tracking URI, not only the
        # client-local constructor argument. Keep both aligned so training
        # workers do not fall back to `file:///...` and fail after training.
        mlflow.set_tracking_uri(self.settings.mlflow.tracking_uri)
        return MlflowClient(tracking_uri=self.settings.mlflow.tracking_uri), entities


class DvcRepositoryAdapter:
    def __init__(self, settings: PlatformSettings) -> None:
        self.settings = settings
        self.repo_root = Path(settings.dvc.repo_root)
        self.remote_root = Path(settings.dvc.remote_url)
        self.git_bin = _resolve_cli("git")
        self.dvc_bin = _resolve_cli("dvc")

    def verify_runtime_prerequisites(self) -> None:
        if not self.git_bin:
            raise RuntimeError("Git is required for DVC integration but is not available.")
        if not self.dvc_bin:
            raise RuntimeError("DVC CLI is required for DVC integration but is not available.")

    def _required_git_bin(self) -> str:
        if not self.git_bin:
            raise RuntimeError("Git is required for DVC integration but is not available.")
        return self.git_bin

    def _required_dvc_bin(self) -> str:
        if not self.dvc_bin:
            raise RuntimeError("DVC CLI is required for DVC integration but is not available.")
        return self.dvc_bin

    def current_code_revision(self, workspace_root: Path) -> str | None:
        return self._git(workspace_root, "rev-parse", "HEAD", check=False)

    def build_reproducibility_metadata(
        self,
        *,
        workspace_root: Path,
        training_job_id: str,
    ) -> dict[str, Any]:
        return {
            "recorded_at": utc_now_iso(),
            "training_job_id": training_job_id,
            "code_revision": self.current_code_revision(workspace_root),
            "git_dirty": bool(self._git(workspace_root, "status", "--porcelain", check=False)),
            "dvc_repo_root": str(self.repo_root),
            "dvc_remote_name": self.settings.dvc.remote_name,
            "dvc_remote_url": self.settings.dvc.remote_url,
        }

    def capture_model_artifact_bundle(
        self,
        *,
        training_job_id: str,
        files: dict[str, Path],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_repository()
        bundle_root = self.repo_root / "registry" / "training-jobs" / training_job_id
        if bundle_root.exists():
            shutil.rmtree(bundle_root)
        bundle_root.mkdir(parents=True, exist_ok=True)

        tracked_files: dict[str, Any] = {}
        for logical_name, source_path in files.items():
            if not source_path.exists():
                continue
            target_path = bundle_root / f"{logical_name}{source_path.suffix}"
            shutil.copy2(source_path, target_path)
            tracked_files[logical_name] = {
                "relative_path": str(target_path.relative_to(self.repo_root)).replace("\\", "/"),
                "sha256": _sha256_file(target_path),
                "source_path": str(source_path),
            }

        metadata_path = bundle_root / "reproducibility.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        tracked_files["reproducibility_metadata"] = {
            "relative_path": str(metadata_path.relative_to(self.repo_root)).replace("\\", "/"),
            "sha256": _sha256_file(metadata_path),
        }

        dvc_bin = self._required_dvc_bin()
        self._run(self.repo_root, dvc_bin, "add", str(bundle_root))
        dvc_file = bundle_root.with_suffix(".dvc")
        self._run(
            self.repo_root,
            dvc_bin,
            "push",
            "-r",
            self.settings.dvc.remote_name,
            str(dvc_file),
        )

        return {
            "repo_root": str(self.repo_root),
            "bundle_relative_path": str(bundle_root.relative_to(self.repo_root)).replace("\\", "/"),
            "bundle_dvc_file": str(dvc_file.relative_to(self.repo_root)).replace("\\", "/"),
            "remote_name": self.settings.dvc.remote_name,
            "remote_url": self.settings.dvc.remote_url,
            "tracked_files": tracked_files,
            "git_commit": self._git(self.repo_root, "rev-parse", "HEAD", check=False),
        }

    def _ensure_repository(self) -> None:
        self.repo_root.mkdir(parents=True, exist_ok=True)
        self.remote_root.mkdir(parents=True, exist_ok=True)
        self.verify_runtime_prerequisites()
        git_bin = self._required_git_bin()
        dvc_bin = self._required_dvc_bin()
        if not (self.repo_root / ".git").exists():
            self._run(self.repo_root, git_bin, "init")
        if not (self.repo_root / ".dvc").exists():
            self._run(self.repo_root, dvc_bin, "init", "--quiet")
        self._run(
            self.repo_root,
            dvc_bin,
            "remote",
            "add",
            "-f",
            self.settings.dvc.remote_name,
            self.settings.dvc.remote_url,
        )

    def _git(self, workdir: Path, *args: str, check: bool) -> str | None:
        if not self.git_bin:
            return None
        try:
            completed = subprocess.run(
                [self.git_bin, *args],
                cwd=workdir,
                check=check,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        stdout = completed.stdout.strip()
        return stdout or None

    def _run(self, workdir: Path, *args: str) -> None:
        completed = subprocess.run(
            list(args),
            cwd=workdir,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"Command `{' '.join(args)}` failed: {stderr}")


def build_mlflow_adapter(settings: PlatformSettings) -> MlflowTrackingAdapter:
    return MlflowTrackingAdapter(settings)


def build_dvc_adapter(settings: PlatformSettings) -> DvcRepositoryAdapter:
    return DvcRepositoryAdapter(settings)


def _stringify_mapping(payload: dict[str, Any]) -> dict[str, str]:
    return {key: _stringify_scalar(value) for key, value in payload.items()}


def _mlflow_supported_aliases(aliases: list[str]) -> list[str]:
    reserved = {"latest"}
    supported: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        normalized = str(alias).strip()
        key = normalized.casefold()
        if not normalized or key in reserved or key in seen:
            continue
        seen.add(key)
        supported.append(normalized)
    return supported


def _stringify_scalar(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    return json.dumps(value, sort_keys=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()
