from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from cv_datasets.models import CanonicalSequenceItem

TEMPORAL_IMPORT_SCHEMA = "temporal.label-studio-import.v1"
TEMPORAL_IMPORTER_VERSION = "temporal-label-studio-importer.v1"


class TemporalSampleImportError(ValueError):
    pass


@dataclass(frozen=True)
class TemporalLabelStudioTask:
    dataset_id: str
    label_project_id: str
    sequence_id: str
    primary_asset_sha256: str
    idempotency_key: str
    task: dict[str, Any]
    manifest: dict[str, Any]


def build_temporal_label_studio_task(
    manifest_path: Path,
    *,
    dataset_id: str,
    label_project_id: str,
    local_files_root: Path,
    local_files_url_prefix: str,
    include_baseline_prediction: bool = True,
) -> TemporalLabelStudioTask:
    manifest_path = manifest_path.resolve()
    manifest = _read_manifest(manifest_path)
    if manifest.get("sample_status") != "recorded":
        raise TemporalSampleImportError(
            "Only released temporal samples with sample_status='recorded' can be imported."
        )
    try:
        item = CanonicalSequenceItem.model_validate(manifest["canonical_sequence_item"])
    except (KeyError, ValueError) as exc:
        raise TemporalSampleImportError(
            "Temporal sample manifest does not contain a valid canonical sequence item."
        ) from exc
    if item.sequence_id != manifest.get("sequence_id"):
        raise TemporalSampleImportError(
            "Temporal sample sequence identity differs between manifest sections."
        )
    raw_fps = manifest.get("constant_fps")
    if (
        not isinstance(raw_fps, int | float)
        or not math.isfinite(raw_fps)
        or raw_fps <= 0
    ):
        raise TemporalSampleImportError("Temporal sample FPS is missing or invalid.")
    fps = float(raw_fps)
    primary_path = _artifact_path(
        manifest_path.parent,
        item.primary_video.uri,
        expected_sha256=item.primary_video.sha256,
        label="primary video",
    )
    _artifact_path(
        manifest_path.parent,
        item.feature_manifest.uri,
        expected_sha256=item.feature_manifest.sha256,
        label="feature manifest",
    )
    _artifact_path(
        manifest_path.parent,
        item.rotation_trace.uri,
        expected_sha256=item.rotation_trace.sha256,
        label="rotation trace",
    )
    diagnostic_url = _diagnostic_video_url(
        manifest_path,
        manifest,
        local_files_root=local_files_root,
        local_files_url_prefix=local_files_url_prefix,
    )
    primary_url = label_studio_local_file_url(
        primary_path,
        local_files_root=local_files_root,
        local_files_url_prefix=local_files_url_prefix,
    )
    idempotency_key = temporal_import_idempotency_key(
        dataset_id=dataset_id,
        label_project_id=label_project_id,
        sequence_id=item.sequence_id,
        primary_asset_sha256=item.primary_video.sha256,
    )
    decision_time = item.decision_timestamp
    schemas = item.schemas.model_dump(mode="json")
    quality = {
        "angle_label_eligible": bool(manifest.get("angle_label_eligible")),
        "ground_truth_status": manifest.get("ground_truth_status"),
        "label_quality": item.label_quality.model_dump(mode="json"),
    }
    provenance = item.provenance.model_dump(mode="json")
    direction = manifest.get("direction_provenance") or {}
    task: dict[str, Any] = {
        "data": {
            "video": primary_url,
            "frame_rate": fps,
            "temporal_import_key": idempotency_key,
            "dataset_id": dataset_id,
            "label_project_id": label_project_id,
            "sequence_id": item.sequence_id,
            "group_id": item.group_id,
            "frame_count": len(item.frames),
            "decision_frame_index": item.decision_frame_index,
            "decision_video_timestamp_seconds": decision_time,
            "decision_source_timestamp": manifest.get("decision_source_timestamp"),
            "schemas": schemas,
            "feature_manifest": item.feature_manifest.model_dump(mode="json"),
            "rotation_trace": item.rotation_trace.model_dump(mode="json"),
            "direction_provenance": direction,
            "quality": quality,
            "provenance": provenance,
            **({"diagnostic_video": diagnostic_url} if diagnostic_url else {}),
        },
        "meta": {
            "import_schema": TEMPORAL_IMPORT_SCHEMA,
            "importer_version": TEMPORAL_IMPORTER_VERSION,
            "temporal_import_key": idempotency_key,
            "dataset_id": dataset_id,
            "label_project_id": label_project_id,
            "sequence_id": item.sequence_id,
            "group_id": item.group_id,
            "primary_asset_sha256": _normalized_sha256(item.primary_video.sha256),
            "frame_count": len(item.frames),
            "fps": fps,
            "decision_frame_index": item.decision_frame_index,
            "decision_video_timestamp_seconds": decision_time,
            "decision_source_timestamp": manifest.get("decision_source_timestamp"),
            "feature_manifest": item.feature_manifest.model_dump(mode="json"),
            "rotation_trace": item.rotation_trace.model_dump(mode="json"),
            "feature_schema": item.schemas.feature_schema,
            "rotation_schema": item.schemas.rotation_schema,
            "calibration_schema": item.schemas.calibration_schema,
            "annotation_schema": item.schemas.annotation_schema,
            "direction_provenance": direction,
            "quality": quality,
            "provenance": provenance,
            "diagnostic_video_url": diagnostic_url,
        },
    }
    prediction = (
        _baseline_prediction(manifest)
        if include_baseline_prediction
        else None
    )
    if prediction is not None:
        task["predictions"] = [prediction]
        task["meta"]["baseline_prediction"] = {
            "present": True,
            "is_ground_truth": False,
            "method": prediction["model_version"],
        }
    else:
        task["meta"]["baseline_prediction"] = {
            "present": False,
            "is_ground_truth": False,
        }
    return TemporalLabelStudioTask(
        dataset_id=dataset_id,
        label_project_id=label_project_id,
        sequence_id=item.sequence_id,
        primary_asset_sha256=_normalized_sha256(item.primary_video.sha256),
        idempotency_key=idempotency_key,
        task=task,
        manifest=manifest,
    )


def temporal_import_idempotency_key(
    *,
    dataset_id: str,
    label_project_id: str,
    sequence_id: str,
    primary_asset_sha256: str,
) -> str:
    identity = {
        "dataset_id": dataset_id,
        "label_project_id": label_project_id,
        "sequence_id": sequence_id,
        "primary_asset_sha256": _normalized_sha256(primary_asset_sha256),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"temporal-task:v1:{digest}"


def label_studio_local_file_url(
    asset_path: Path,
    *,
    local_files_root: Path,
    local_files_url_prefix: str,
) -> str:
    root = local_files_root.resolve()
    resolved = asset_path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise TemporalSampleImportError(
            "Temporal sample media is outside the configured Label Studio local-files root."
        ) from exc
    encoded = quote(relative.as_posix(), safe="/")
    separator = "" if local_files_url_prefix.endswith(("=", "/")) else "/"
    return f"{local_files_url_prefix}{separator}{encoded}"


def find_temporal_task_id(
    tasks: list[dict[str, Any]],
    *,
    idempotency_key: str,
) -> int | None:
    matches: set[int] = set()
    for task in tasks:
        metadata = task.get("meta")
        data = task.get("data")
        task_id = task.get("id")
        if (
            (
                isinstance(metadata, Mapping)
                and metadata.get("temporal_import_key") == idempotency_key
            )
            or (
                isinstance(data, Mapping)
                and data.get("temporal_import_key") == idempotency_key
            )
        ) and isinstance(task_id, int | str) and str(task_id).isdigit():
            matches.add(int(task_id))
    if len(matches) > 1:
        raise TemporalSampleImportError(
            "Label Studio contains multiple tasks for one temporal import key."
        )
    return next(iter(matches), None)


def task_ids_from_import_response(value: Mapping[str, Any]) -> list[int]:
    for key in ("task_ids", "ids"):
        raw = value.get(key)
        if isinstance(raw, list):
            return [
                int(item)
                for item in raw
                if isinstance(item, int | str) and str(item).isdigit()
            ]
    return []


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise TemporalSampleImportError("Temporal sample manifest does not exist.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemporalSampleImportError("Temporal sample manifest is not readable JSON.") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "temporal.sample.manifest.v1":
        raise TemporalSampleImportError("Unsupported temporal sample manifest schema.")
    return value


def _artifact_path(
    root: Path,
    uri: str,
    *,
    expected_sha256: str,
    label: str,
) -> Path:
    candidate = (root / uri).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise TemporalSampleImportError(f"Temporal {label} path escapes its sample root.") from exc
    if not candidate.is_file():
        raise TemporalSampleImportError(f"Temporal {label} is missing.")
    digest = _sha256_file(candidate)
    if digest != _normalized_sha256(expected_sha256):
        raise TemporalSampleImportError(f"Temporal {label} checksum does not match its manifest.")
    return candidate


def _diagnostic_video_url(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    local_files_root: Path,
    local_files_url_prefix: str,
) -> str | None:
    artifacts = manifest.get("artifacts")
    diagnostics = artifacts.get("diagnostics") if isinstance(artifacts, Mapping) else None
    for item in diagnostics if isinstance(diagnostics, list) else []:
        if not isinstance(item, Mapping) or item.get("role") == "verification":
            continue
        uri = item.get("uri")
        sha256 = item.get("sha256")
        if not isinstance(uri, str) or not isinstance(sha256, str):
            continue
        path = _artifact_path(
            manifest_path.parent,
            uri,
            expected_sha256=sha256,
            label="diagnostic video",
        )
        return label_studio_local_file_url(
            path,
            local_files_root=local_files_root,
            local_files_url_prefix=local_files_url_prefix,
        )
    return None


def _baseline_prediction(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    baseline = manifest.get("baseline_prediction")
    if not isinstance(baseline, Mapping) or not bool(baseline.get("available")):
        return None
    start = baseline.get("frame_start_index")
    end = baseline.get("frame_end_index")
    if not isinstance(start, int) or not isinstance(end, int) or end < start:
        return None
    confidence = baseline.get("confidence")
    score = float(confidence) if isinstance(confidence, int | float) else 0.0
    method = str(baseline.get("estimate_method") or "projected_baseline")
    return {
        "model_version": f"deterministic:{method}",
        "score": max(0.0, min(score, 1.0)),
        "result": [
            {
                "id": hashlib.sha256(
                    f"{manifest.get('sequence_id')}:{start}:{end}".encode("utf-8")
                ).hexdigest()[:16],
                "from_name": "target_orientation",
                "to_name": "video",
                "type": "timelinelabels",
                "origin": "prediction",
                "value": {
                    "ranges": [{"start": start, "end": end}],
                    "timelinelabels": ["target_orientation"],
                },
            }
        ],
    }


def _normalized_sha256(value: str) -> str:
    normalized = value.removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise TemporalSampleImportError("Temporal asset SHA256 is malformed.")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
