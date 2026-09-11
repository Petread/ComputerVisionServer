from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from edge_agent.cameras import CameraAdapterInventory, inventory_camera_adapters


class CommandRunner(Protocol):
    def __call__(self, command: list[str]) -> str | None: ...


def _run(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or completed.stderr).strip()
    return text if completed.returncode == 0 and text else None


def _version(text: str | None, pattern: str = r"\d+(?:\.\d+){1,3}") -> str | None:
    match = re.search(pattern, text or "")
    return match.group(0) if match else None


def _l4t_version(text: str | None) -> str | None:
    match = re.search(r"R(\d+)\s+\(release\),\s+REVISION:\s*([0-9.]+)", text or "")
    return f"{match.group(1)}.{match.group(2)}" if match else None


def _linux_memory() -> int:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size)
    except (AttributeError, OSError, ValueError):
        return 0


def _windows_memory() -> int:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    try:
        return (
            int(status.total_physical)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            else 0
        )
    except (AttributeError, OSError):
        return 0


@dataclass(frozen=True)
class PlatformInventory:
    source: str
    architecture: str
    os_version: str
    jetpack_version: str | None = None
    l4t_version: str | None = None
    cuda_version: str | None = None
    tensorrt_version: str | None = None
    deepstream_version: str | None = None
    gpu_model: str | None = None
    gpu_driver_version: str | None = None
    gpu_memory_bytes: int = 0
    python_version: str | None = None
    ram_total_bytes: int = 0
    camera_adapters: list[CameraAdapterInventory] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PlatformInventoryProbe:
    """Reads the actually installed NVIDIA stack; no configured version is reported as probed."""

    def __init__(self, *, runner: CommandRunner = _run, root: Path = Path("/")) -> None:
        self._runner = runner
        self._root = root

    def probe(self) -> PlatformInventory:
        system = platform.system()
        warnings: list[str] = []
        l4t_text = self._read("etc/nv_tegra_release") if system != "Windows" else None
        l4t = _l4t_version(l4t_text)
        os_version = self._os_version(system)
        jetpack = self._package_version("nvidia-jetpack") if l4t_text else None
        cuda_text = self._runner(["nvcc", "--version"])
        if cuda_text is None:
            cuda_text = self._read("usr/local/cuda/version.json")
        tensorrt_text = self._python_version("tensorrt") or self._runner(["trtexec", "--version"])
        deepstream_text = self._runner(["deepstream-app", "--version"])
        gpu_model, gpu_bytes, gpu_driver = self._gpu(l4t_text is not None)
        if gpu_model is None:
            warnings.append("No NVIDIA GPU was detected by device-tree or nvidia-smi.")
        if _version(tensorrt_text) is None:
            warnings.append("TensorRT version could not be probed.")
        cameras = inventory_camera_adapters()
        if not any(item.available for item in cameras):
            warnings.append("No released camera adapter provider is installed.")
        return PlatformInventory(
            source="probed" if not warnings else "partial",
            architecture=platform.machine() or "unknown",
            os_version=os_version,
            jetpack_version=_version(jetpack),
            l4t_version=l4t,
            cuda_version=_version(cuda_text),
            tensorrt_version=_version(tensorrt_text),
            deepstream_version=_version(deepstream_text),
            gpu_model=gpu_model,
            gpu_driver_version=gpu_driver,
            gpu_memory_bytes=gpu_bytes,
            python_version=platform.python_version(),
            ram_total_bytes=_windows_memory() if system == "Windows" else _linux_memory(),
            camera_adapters=cameras,
            warnings=warnings,
        )

    def _read(self, relative: str) -> str | None:
        path = self._root / relative
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None

    def _os_version(self, system: str) -> str:
        if system == "Windows":
            return f"Windows {platform.version()} ({platform.release()})"
        os_release = self._read("etc/os-release")
        if os_release:
            values = dict(line.split("=", 1) for line in os_release.splitlines() if "=" in line)
            return values.get("PRETTY_NAME", "Linux").strip('"')
        return f"{system} {platform.release()}"

    def _package_version(self, package: str) -> str | None:
        return self._runner(["dpkg-query", "-W", "-f=${Version}", package])

    def _python_version(self, module: str) -> str | None:
        return self._runner(
            [
                os.fspath(Path(sys.executable)),
                "-c",
                f"import {module}; print(getattr({module}, '__version__', ''))",
            ]
        )

    def _gpu(self, jetson: bool) -> tuple[str | None, int, str | None]:
        if jetson:
            model = self._read("proc/device-tree/model")
            driver = _l4t_version(self._read("etc/nv_tegra_release"))
            return (model.rstrip("\x00") if model else None, _linux_memory(), driver)
        text = self._runner(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ]
        )
        if not text:
            return None, 0, None
        values = [value.strip() for value in text.splitlines()[0].split(",")]
        if len(values) < 3:
            return values[0], 0, None
        name, memory, driver = values[0], values[1], values[2]
        try:
            memory_bytes = int(memory) * 1024**2
        except ValueError:
            memory_bytes = 0
        return name, memory_bytes, driver
