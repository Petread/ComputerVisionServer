from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cv_training.trainer_types import TrainerArtifactValidation


def validate_real_checkpoint(path: Path) -> tuple[bool, dict[str, Any]]:
    if not path.exists():
        return False, {"reason": "missing"}
    if path.stat().st_size == 0:
        return False, {"reason": "empty"}
    if _looks_like_json(path):
        return False, {"reason": "json_placeholder"}
    return True, {"size_bytes": path.stat().st_size}


def validate_real_onnx(path: Path) -> tuple[bool, dict[str, Any]]:
    if not path.exists():
        return False, {"reason": "missing"}
    if path.stat().st_size == 0:
        return False, {"reason": "empty"}
    if _looks_like_json(path):
        return False, {"reason": "json_placeholder"}
    try:
        import onnx
    except ImportError as exc:  # pragma: no cover - exercised in runtime validation
        return False, {"reason": "onnx_package_missing", "detail": str(exc)}
    try:
        model = onnx.load(str(path))
        onnx.checker.check_model(model)
    except Exception as exc:  # pragma: no cover - depends on optional runtime
        return False, {"reason": "onnx_load_failed", "detail": str(exc)}
    return True, {"size_bytes": path.stat().st_size}


def build_artifact_validation_summary(
    *,
    checkpoint_path: Path | None,
    onnx_path: Path | None,
    trainer_mode: str,
    tensorrt_deferred: bool,
) -> TrainerArtifactValidation:
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {
        "trainer_mode": trainer_mode,
        "tensorrt_deferred": tensorrt_deferred,
    }

    if checkpoint_path is not None:
        checkpoint_valid, checkpoint_detail = validate_real_checkpoint(checkpoint_path)
        checks["checkpoint_valid"] = checkpoint_valid
        details["checkpoint"] = checkpoint_detail
    else:
        checks["checkpoint_valid"] = False
        details["checkpoint"] = {"reason": "missing"}

    if onnx_path is not None:
        onnx_valid, onnx_detail = validate_real_onnx(onnx_path)
        checks["onnx_valid"] = onnx_valid
        details["onnx"] = onnx_detail
    else:
        checks["onnx_valid"] = False
        details["onnx"] = {"reason": "missing"}

    status = "passed" if all(checks.values()) and trainer_mode == "real" else "failed"
    return TrainerArtifactValidation(status=status, checks=checks, details=details)


def _looks_like_json(path: Path) -> bool:
    try:
        payload = path.read_bytes()[:256].lstrip()
    except OSError:
        return False
    if not payload:
        return False
    if payload[:1] not in {b"{", b"["}:
        return False
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return True
