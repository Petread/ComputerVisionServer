from __future__ import annotations

import re
import shutil
from pathlib import Path


def quarantine_path(source: Path, *, quarantine_root: Path, label: str) -> Path | None:
    if not source.exists():
        return None
    quarantine_root.mkdir(parents=True, exist_ok=True)
    destination = quarantine_root / _unique_name(source.name, label=label, parent=quarantine_root)
    shutil.move(str(source), str(destination))
    return destination


def remove_path_within_root(*, root: Path, target: Path) -> None:
    if not target.exists():
        return
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise ValueError(f"Refusing to remove path outside managed root: {resolved_target}")
    if target.is_dir():
        shutil.rmtree(target)
        return
    target.unlink()


def ensure_path_within_root(*, root: Path, target: Path) -> Path:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise ValueError(f"Path escapes managed root: {resolved_target}")
    return resolved_target


def sanitize_filename(name: str, *, max_length: int = 160) -> str:
    cleaned = Path(name).name.strip().replace(" ", "_")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", cleaned)
    cleaned = cleaned.strip(".-_" ) or "upload"
    stem = Path(cleaned).stem[:max_length]
    suffix = Path(cleaned).suffix[:16]
    return f"{stem}{suffix}"


def promote_staged_directory(*, source: Path, destination: Path) -> Path:
    if not source.exists():
        raise FileNotFoundError(f"Staged directory does not exist: {source}")
    if not source.is_dir():
        raise ValueError(f"Expected a staged directory, got file: {source}")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        return source.rename(destination)
    except PermissionError:
        # Windows dev/test runs can refuse freshly-written directory renames that succeed in WSL2.
        shutil.move(str(source), str(destination))
        return destination


def _unique_name(name: str, *, label: str, parent: Path) -> str:
    stem = f"{label}_{name}"
    candidate = stem
    counter = 1
    while (parent / candidate).exists():
        candidate = f"{stem}_{counter}"
        counter += 1
    return candidate
