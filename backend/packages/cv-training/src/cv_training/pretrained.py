from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

PRETRAINED_CHECKPOINT_ROOT_ENV = "CV_PLATFORM_PRETRAINED_CHECKPOINT_ROOT"
ALLOW_REMOTE_PRETRAINED_DOWNLOADS_ENV = "CV_PLATFORM_ALLOW_REMOTE_PRETRAINED_DOWNLOADS"

DEFAULT_PRETRAINED_CHECKPOINT_ROOTS = (
    Path("/srv/cv-platform/control/pretrained-checkpoints"),
    Path("/opt/ybb-runtime/pretrained-checkpoints"),
)


class PretrainedCheckpointMissingError(RuntimeError):
    """Raised when transfer learning needs a local checkpoint that is not present."""


def pretrained_checkpoint_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    configured = os.getenv(PRETRAINED_CHECKPOINT_ROOT_ENV, "")
    for item in configured.split(os.pathsep):
        stripped = item.strip()
        if stripped:
            roots.append(Path(stripped))
    roots.extend(DEFAULT_PRETRAINED_CHECKPOINT_ROOTS)
    return tuple(dict.fromkeys(roots))


def remote_pretrained_downloads_allowed() -> bool:
    value = os.getenv(ALLOW_REMOTE_PRETRAINED_DOWNLOADS_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_pretrained_checkpoint(
    requested_checkpoint: str,
    *,
    aliases: Iterable[str] = (),
    allow_remote: bool | None = None,
) -> tuple[str | None, dict[str, object]]:
    requested = str(requested_checkpoint or "").strip()
    candidate_filenames = _candidate_filenames(requested, aliases)
    roots = pretrained_checkpoint_roots()
    base_metadata: dict[str, object] = {
        "requested_checkpoint": requested,
        "candidate_filenames": candidate_filenames,
        "pretrained_checkpoint_roots": [str(root) for root in roots],
    }

    if not requested:
        return None, {"checkpoint_source": "not_configured", **base_metadata}

    checkpoint_path = Path(requested)
    if checkpoint_path.is_absolute():
        if checkpoint_path.is_file():
            return str(checkpoint_path), {"checkpoint_source": "absolute_path", **base_metadata}
        return None, {"checkpoint_source": "absolute_path_missing", **base_metadata}
    if checkpoint_path.is_file():
        return str(checkpoint_path), {"checkpoint_source": "relative_path", **base_metadata}

    for root in roots:
        for filename in candidate_filenames:
            candidate = root / filename
            if candidate.is_file():
                return str(candidate), {
                    "checkpoint_source": "local_cache",
                    "checkpoint_cache_path": str(candidate),
                    **base_metadata,
                }

    if allow_remote is None:
        allow_remote = remote_pretrained_downloads_allowed()
    if allow_remote:
        source = "remote_download_allowed" if _is_remote_checkpoint(requested) else "named_model_allowed"
        return requested, {"checkpoint_source": source, **base_metadata}

    return None, {
        "checkpoint_source": "local_cache_missing",
        "allow_remote_pretrained_downloads_env": ALLOW_REMOTE_PRETRAINED_DOWNLOADS_ENV,
        **base_metadata,
    }


def require_local_pretrained_checkpoint(
    requested_checkpoint: str,
    *,
    aliases: Iterable[str] = (),
    context: str,
) -> tuple[str, dict[str, object]]:
    resolved, metadata = resolve_pretrained_checkpoint(
        requested_checkpoint,
        aliases=aliases,
        allow_remote=False,
    )
    if resolved is not None:
        return resolved, metadata
    filenames = ", ".join(str(item) for item in metadata.get("candidate_filenames", []))
    roots = ", ".join(str(item) for item in metadata.get("pretrained_checkpoint_roots", []))
    raise PretrainedCheckpointMissingError(
        f"Transfer learning for {context} requires a local pretrained checkpoint. "
        f"Expected one of [{filenames}] in one of [{roots}]."
    )


def stage_torch_hub_checkpoint(checkpoint_path: Path, *, filename: str | None = None) -> dict[str, str]:
    import torch

    checkpoints_dir = Path(torch.hub.get_dir()) / "checkpoints"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    target = checkpoints_dir / (filename or checkpoint_path.name)
    if not target.exists():
        shutil.copy2(checkpoint_path, target)
    return {
        "torch_hub_checkpoint_path": str(target),
        "torch_hub_checkpoint_source": str(checkpoint_path),
    }


def _candidate_filenames(requested: str, aliases: Iterable[str]) -> list[str]:
    names: list[str] = []
    primary = _checkpoint_filename(requested)
    if primary:
        names.append(primary)
    for alias in aliases:
        filename = _checkpoint_filename(str(alias))
        if filename:
            names.append(filename)
    return list(dict.fromkeys(names))


def _checkpoint_filename(value: str) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None
    if _is_remote_checkpoint(stripped):
        parsed = urlparse(stripped)
        filename = Path(parsed.path).name
    else:
        filename = Path(stripped).name
    return filename or None


def _is_remote_checkpoint(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.startswith("http://") or normalized.startswith("https://")
