from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv_core import PlatformSettings


@dataclass(frozen=True)
class TrainingJobPaths:
    job_root: Path
    workspace_dir: Path
    configs_dir: Path
    logs_dir: Path
    outputs_dir: Path
    checkpoints_dir: Path
    evaluation_dir: Path
    onnx_dir: Path
    tensorrt_dir: Path
    temp_dir: Path
    log_file: Path
    metrics_file: Path
    artifact_manifest_file: Path
    export_manifest_file: Path
    engine_build_manifest_file: Path
    evaluation_summary_file: Path

    def ensure(self) -> "TrainingJobPaths":
        for directory in (
            self.job_root,
            self.workspace_dir,
            self.configs_dir,
            self.logs_dir,
            self.outputs_dir,
            self.checkpoints_dir,
            self.evaluation_dir,
            self.onnx_dir,
            self.tensorrt_dir,
            self.temp_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def build_training_job_paths(settings: PlatformSettings, training_job_id: str) -> TrainingJobPaths:
    job_root = Path(settings.storage.training_root) / f"trn_{training_job_id}"
    return TrainingJobPaths(
        job_root=job_root,
        workspace_dir=job_root / "workspace",
        configs_dir=job_root / "configs",
        logs_dir=job_root / "logs",
        outputs_dir=job_root / "outputs",
        checkpoints_dir=job_root / "checkpoints",
        evaluation_dir=job_root / "evaluation",
        onnx_dir=job_root / "onnx",
        tensorrt_dir=job_root / "tensorrt",
        temp_dir=job_root / ".tmp",
        log_file=job_root / "logs" / "events.jsonl",
        metrics_file=job_root / "outputs" / "metrics.jsonl",
        artifact_manifest_file=job_root / "outputs" / "artifact_manifest.json",
        export_manifest_file=job_root / "outputs" / "export_manifest.json",
        engine_build_manifest_file=job_root / "outputs" / "engine_build_manifest.json",
        evaluation_summary_file=job_root / "evaluation" / "summary.json",
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit is None:
        return lines
    return lines[-limit:]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()
