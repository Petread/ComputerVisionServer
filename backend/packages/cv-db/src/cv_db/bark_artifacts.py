"""Retain paired inspection evidence in the existing runtime results storage."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any

from cv_contracts.bark import StemBarkJob
from cv_core import PlatformSettings
from cv_runtime.payloads import FramePayload, RuntimeExecutionResult

from cv_db.models import ResultRecord


def store_bark_artifacts(
    settings: PlatformSettings,
    row: ResultRecord,
    result: RuntimeExecutionResult,
    frame: FramePayload,
    job: StemBarkJob,
) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", row.id):
        raise ValueError("Invalid generated result identifier")
    root = settings.storage.runtime_results_root().resolve()
    directory = (root / row.id).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("Result artifact path escapes storage root")
    directory.mkdir(parents=True, exist_ok=False)
    manifest: list[dict[str, Any]] = []

    def write(name: str, data: bytes, mime: str) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            raise ValueError("Invalid artifact filename")
        (directory / name).write_bytes(data)
        manifest.append(
            {
                "name": name,
                "mime_type": mime,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )

    write("measurement.csv", job.csv_text.encode("utf-8"), "text/csv")
    write(
        "original.png" if frame.mime_type == "image/png" else "original.jpg",
        frame.image_bytes(),
        frame.mime_type,
    )
    for key, outputs in result.node_outputs.items():
        for port, payload in outputs.items():
            if not isinstance(payload, dict):
                continue
            if payload.get("payload_type") == "frame" and payload.get("image_base64"):
                mime = payload.get("mime_type", "image/png")
                write(
                    f"{key}.{port}.{'png' if mime == 'image/png' else 'jpg'}",
                    base64.b64decode(payload["image_base64"], validate=True),
                    mime,
                )
            if payload.get("payload_type") == "segments":
                for index, segment in enumerate(payload.get("segments", [])):
                    mask = segment.get("mask")
                    if isinstance(mask, dict) and mask.get("data"):
                        write(
                            f"{key}.class-{index}.png",
                            base64.b64decode(mask["data"], validate=True),
                            "image/png",
                        )
    # Full context/transform/class labels and model provenance, outside PostgreSQL.
    write("execution.json", result.model_dump_json().encode("utf-8"), "application/json")
    row.raw_outputs_ref = str(directory)
    row.runtime_metadata = {
        **row.runtime_metadata,
        "bark_artifacts": manifest,
        "bark_job": job.model_dump(mode="json", exclude={"csv_text"}),
    }


def bark_artifact_path(settings: PlatformSettings, row: ResultRecord, name: str) -> Path:
    entries = row.runtime_metadata.get("bark_artifacts", [])
    if not any(entry.get("name") == name for entry in entries):
        raise ValueError("Artifact not in result manifest")
    root = settings.storage.runtime_results_root().resolve()
    directory = (root / row.id).resolve()
    path = (directory / name).resolve()
    if not directory.is_relative_to(root) or path.parent != directory or not path.is_file():
        raise ValueError("Result artifact unavailable")
    return path


def remove_bark_artifacts(settings: PlatformSettings, row: ResultRecord) -> None:
    """Retention removes only manifest-owned files inside this result's directory."""
    if not row.runtime_metadata.get("bark_artifacts"):
        return
    root = settings.storage.runtime_results_root().resolve()
    directory = (root / row.id).resolve()
    if directory.parent != root:
        raise ValueError("Result artifact path escapes storage root")
    for entry in row.runtime_metadata["bark_artifacts"]:
        path = (directory / entry["name"]).resolve()
        if path.parent != directory:
            raise ValueError("Artifact path escapes result directory")
        path.unlink(missing_ok=True)
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
