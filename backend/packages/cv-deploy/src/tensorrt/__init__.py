"""Compatibility frontend for NVIDIA's TensorRT binding wheel.

The offline updater installs ``tensorrt-cu12-bindings`` because it is available
as a Linux wheel. NVIDIA's ``tensorrt`` frontend package is source-only for the
10.x versions we use, so this module exposes the binding wheel under the import
name expected by MMDeploy.
"""

from __future__ import annotations

try:
    from tensorrt_bindings import *  # noqa: F403
    from tensorrt_bindings import __version__  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised on deployment hosts
    raise ImportError(
        "TensorRT Python bindings are not installed or their native libraries "
        "are not on LD_LIBRARY_PATH. Install tensorrt-cu12-bindings in the "
        "deployment runtime and expose libnvinfer/libnvonnxparser."
    ) from exc
