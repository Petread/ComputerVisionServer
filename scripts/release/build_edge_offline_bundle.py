from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

try:
    from scripts.release import edge_system_packages
except ImportError:  # Direct script execution.
    import edge_system_packages

ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILES = {
    "jetson_orin_nano": "jetson-orin-nano.json",
    "jetson_agx_orin": "jetson-agx-orin.json",
    "windows_11": "windows-11-nvidia.json",
}
FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
WINDOWS_PAYLOAD_SOURCES = ROOT / "deployment/edge/windows-payload-sources.json"
WINDOWS_PAYLOAD_DIRECTORY = ROOT / "dist/edge-inputs/windows_11"
WINDOWS_TENSORRT_DISTRIBUTIONS = {
    "tensorrt-cu13",
    "tensorrt-cu13-bindings",
    "tensorrt-cu13-libs",
}
LARGE_WHEEL_NORMALIZATION_LIMIT = 128 * 1024 * 1024
INTERNAL_PROJECTS = (
    ROOT / "backend/packages/cv-status",
    ROOT / "backend/packages/cv-contracts",
    ROOT / "backend/packages/cv-utils",
    ROOT / "backend/packages/cv-edge",
    ROOT / "backend/packages/cv-runtime",
    ROOT / "backend/services/edge-agent",
)


class BundleError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BundleError(f"JSON object expected: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _https_url(value: object, field: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise BundleError(f"{field} must be an HTTPS URL without embedded credentials.")
    return url


def _download_verified(source: dict[str, Any], destination: Path) -> None:
    url = _https_url(source.get("url"), "payload source URL")
    expected = str(source.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise BundleError("Payload source must define a valid SHA-256 digest.")
    expected_size = int(source.get("size_bytes") or 0)
    offset = destination.stat().st_size if destination.is_file() else 0
    if expected_size and offset > expected_size:
        destination.unlink()
        offset = 0
    if expected_size and offset == expected_size:
        observed = _sha256(destination)
        if observed == expected:
            return
        destination.unlink()
        offset = 0
    headers = {"User-Agent": "YBB-Edge-Bundle-Builder/2"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=180) as response:
            resumed = offset > 0 and getattr(response, "status", None) == 206
            mode = "ab" if resumed else "wb"
            transferred = offset if resumed else 0
            report_at = transferred + 128 * 1024 * 1024
            with destination.open(mode) as output:
                while chunk := response.read(8 * 1024 * 1024):
                    output.write(chunk)
                    transferred += len(chunk)
                    if expected_size and transferred >= report_at:
                        print(
                            json.dumps(
                                {
                                    "download": destination.name,
                                    "downloaded_bytes": transferred,
                                    "total_bytes": expected_size,
                                },
                                sort_keys=True,
                            ),
                            flush=True,
                        )
                        report_at += 128 * 1024 * 1024
    except OSError as exc:
        raise BundleError(f"Payload download failed: {url}") from exc
    if expected_size and destination.stat().st_size != expected_size:
        raise BundleError(
            f"Payload size mismatch for {url}: expected {expected_size}, "
            f"observed {destination.stat().st_size}"
        )
    observed = _sha256(destination)
    if observed != expected:
        raise BundleError(
            f"Payload checksum mismatch for {url}: expected {expected}, observed {observed}"
        )


def _ensure_verified_payload(source: dict[str, Any], destination: Path) -> bool:
    expected = str(source.get("sha256") or "").strip().lower()
    if destination.is_file() and _sha256(destination) == expected:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    _download_verified(source, partial)
    os.replace(partial, destination)
    return True


def _extract_regular_tar_files(source: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with tarfile.open(source, "r:gz") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise BundleError(f"Unsafe Python runtime archive path: {member.name!r}")
            target = destination.joinpath(*relative.parts)
            resolved_target = target.resolve()
            if (
                resolved_target != destination_root
                and destination_root not in resolved_target.parents
            ):
                raise BundleError(f"Unsafe Python runtime archive path: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise BundleError(
                    f"Unsupported Python runtime archive entry: {member.name!r}"
                )
            stream = archive.extractfile(member)
            if stream is None:
                raise BundleError(f"Could not read Python runtime entry: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream, target.open("wb") as output:
                shutil.copyfileobj(stream, output, length=1024 * 1024)


def _runtime_python(root: Path) -> Path:
    candidates = sorted(
        (path for path in root.rglob("python.exe") if path.is_file()),
        key=lambda path: (len(path.relative_to(root).parts), path.as_posix().lower()),
    )
    if not candidates:
        raise BundleError("Downloaded Python runtime does not contain python.exe.")
    return candidates[0]


def _validate_windows_runtime_tree(root: Path, expected_version: str) -> None:
    python = _runtime_python(root)
    library = python.parent / "Lib"
    if not (library / "venv" / "__init__.py").is_file():
        raise BundleError("Downloaded Python runtime does not contain the venv module.")
    if not (library / "ensurepip" / "__init__.py").is_file():
        raise BundleError("Downloaded Python runtime does not contain ensurepip.")
    completed = subprocess.run(
        [
            str(python),
            "-c",
            "import ensurepip, sys, venv; "
            "print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    observed = completed.stdout.strip()
    if completed.returncode or observed != expected_version:
        detail = completed.stderr.strip() or observed or f"exit {completed.returncode}"
        raise BundleError(
            f"Downloaded Python runtime is not usable as Python {expected_version}: {detail}"
        )


def _write_reproducible_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def _safe_name(value: str, field: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}", value):
        raise BundleError(f"{field} contains unsupported characters: {value!r}")
    return value


def _require_path(config: dict[str, Any], name: str, *, directory: bool = False) -> Path:
    raw = str(config.get(name, "")).strip()
    if not raw:
        raise BundleError(f"Missing required configuration value: {name}")
    path = Path(raw).expanduser().resolve()
    if directory and not path.is_dir():
        raise BundleError(f"Directory not found for {name}: {path}")
    if not directory and not path.is_file():
        raise BundleError(f"File not found for {name}: {path}")
    return path


def _normalized_distribution(filename: str) -> str:
    return re.split(r"-(?=\d)", filename, maxsplit=1)[0].replace("_", "-").lower()


def _normalize_wheel(source: Path, target: Path) -> None:
    if source.stat().st_size >= LARGE_WHEEL_NORMALIZATION_LIMIT:
        _link_or_copy(source, target)
        return
    try:
        with (
            zipfile.ZipFile(source, "r") as wheel,
            zipfile.ZipFile(
                target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as output,
        ):
            for name in sorted(wheel.namelist()):
                info = zipfile.ZipInfo(name, FIXED_ZIP_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                with wheel.open(name) as input_stream, output.open(
                    info, "w", force_zip64=True
                ) as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, length=8 * 1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise BundleError(f"Invalid wheel archive: {source}") from exc


def _copy_tree_files(source: Path, target: Path) -> None:
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def _build_internal_wheels(
    config: dict[str, Any],
    destination: Path,
    *,
    build_isolation: bool = False,
) -> None:
    if not bool(config.get("build_internal_wheels", False)) and not build_isolation:
        return
    python = str(config.get("python_executable") or sys.executable)
    for project in INTERNAL_PROJECTS:
        with tempfile.TemporaryDirectory(prefix="ybb-edge-internal-wheel-") as temporary:
            built_directory = Path(temporary)
            command = [
                python,
                "-m",
                "pip",
                "wheel",
                "--no-cache-dir",
                "--no-deps",
                "--wheel-dir",
                str(built_directory),
                str(project),
            ]
            if not build_isolation:
                command.insert(6, "--no-build-isolation")
            completed = subprocess.run(command, cwd=ROOT, check=False, text=True)
            built = sorted(built_directory.glob("*.whl"))
            if completed.returncode or len(built) != 1:
                raise BundleError(f"Internal wheel build failed: {project}")
            distribution = _normalized_distribution(built[0].name)
            for previous in destination.glob("*.whl"):
                if _normalized_distribution(previous.name) == distribution:
                    previous.unlink()
            os.replace(built[0], destination / built[0].name)


def _validate_internal_wheel_sources(directory: Path) -> None:
    """Never combine current installer scripts with stale cached first-party Python code."""
    stale: list[str] = []
    wheels = list(directory.glob("*.whl"))
    for project in INTERNAL_PROJECTS:
        candidates = [
            wheel for wheel in wheels if _normalized_distribution(wheel.name) == project.name
        ]
        if len(candidates) != 1:
            stale.append(project.name)
            continue
        source_root = project / "src"
        sources = {
            path.relative_to(source_root).as_posix(): path.read_text(encoding="utf-8-sig")
            for path in source_root.rglob("*.py")
        }
        try:
            with zipfile.ZipFile(candidates[0]) as archive:
                names = [name for name in archive.namelist() if name.endswith(".py")]
                if len(names) != len(set(names)) or set(names) != set(sources):
                    stale.append(project.name)
                    continue
                if any(
                    archive.read(name).decode("utf-8-sig").replace("\r\n", "\n") != source
                    for name, source in sources.items()
                ):
                    stale.append(project.name)
        except (OSError, ValueError, zipfile.BadZipFile):
            stale.append(project.name)
    if stale:
        raise BundleError(
            "Veraltete oder unvollständige interne Python-Pakete im Wheelhouse: "
            + ", ".join(stale)
            + ". Die Installer-Dateien und diese Pakete müssen aus demselben Codestand stammen. "
            "In der Builder-UI zuerst 'Online-Inhalte vorbereiten (Internet)' ausführen und "
            "anschließend das Bundle neu bauen. Alternativ 'Interne Wheels im Offline-Build "
            "erneut bauen' mit lokal verfügbaren Build-Abhängigkeiten aktivieren."
        )


def _runtime_distribution_names(config: dict[str, Any]) -> list[str]:
    values = config.get("runtime_distributions", "edge-agent")
    if isinstance(values, str):
        values = [item.strip() for item in values.split(",") if item.strip()]
    if not isinstance(values, list) or not values:
        raise BundleError("runtime_distributions must contain at least edge-agent.")
    unsafe = [
        item
        for item in values
        if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", item)
    ]
    if unsafe:
        raise BundleError(f"Unsafe runtime distribution names: {unsafe}")
    if str(config.get("target_platform") or "") == "windows_11" and not any(
        item.replace("_", "-").lower() == "opencv-python-headless" for item in values
    ):
        values.append("opencv-python-headless")
    return values


def _target_marker_distributions(profile: dict[str, Any]) -> list[str]:
    """Dependencies whose environment markers pip evaluates for the build host.

    Jetson wheelhouses are prepared on Windows with pip's cross-platform download
    options. pip selects target-compatible wheels, but evaluates dependency
    environment markers such as ``sys_platform != 'win32'`` for the Windows host.
    Request those Linux-only dependencies explicitly so the offline target has a
    complete dependency closure.
    """
    if str(profile.get("platform") or "") in {
        "jetson_orin_nano",
        "jetson_agx_orin",
    }:
        return ["uvloop"]
    return []


def _validate_wheelhouse(
    directory: Path,
    profile: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    available = {_normalized_distribution(path.name) for path in directory.glob("*.whl")}
    required = {
        str(item).replace("_", "-").lower()
        for item in config.get("required_distributions", profile["required_distributions"])
    }
    if profile["platform"] == "windows_11":
        required.update(WINDOWS_TENSORRT_DISTRIBUTIONS)
    required.update(_target_marker_distributions(profile))
    required.add("pypylon")
    missing = sorted(required - available)
    if missing:
        raise BundleError(f"Wheelhouse is incomplete; missing distributions: {missing}")
    runtime_distributions = _runtime_distribution_names(config)
    runtime_names = {item.replace("_", "-").lower() for item in runtime_distributions}
    missing_runtime = sorted(runtime_names - available)
    if missing_runtime:
        raise BundleError(
            f"Runtime distributions are not present in the wheelhouse: {missing_runtime}"
        )
    return runtime_distributions


def _windows_payload_paths(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    runtime_raw = str(config.get("python_runtime_archive") or "").strip()
    winsw_raw = str(config.get("winsw_executable") or "").strip()
    runtime = (
        Path(runtime_raw).expanduser().resolve()
        if runtime_raw
        else WINDOWS_PAYLOAD_DIRECTORY / "python-runtime.zip"
    )
    winsw = (
        Path(winsw_raw).expanduser().resolve()
        if winsw_raw
        else WINDOWS_PAYLOAD_DIRECTORY / "winsw.exe"
    )
    lock = runtime.parent / "windows-payload-source.lock.json"
    return runtime, winsw, lock


def _windows_payload_lock_is_current(
    lock_path: Path,
    runtime: Path,
    winsw: Path,
    inference_payloads: dict[str, Path],
    sources: dict[str, Any],
) -> bool:
    if (
        not lock_path.is_file()
        or not runtime.is_file()
        or not winsw.is_file()
        or not all(path.is_file() for path in inference_payloads.values())
    ):
        return False
    try:
        lock = _json(lock_path)
    except (BundleError, OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        lock.get("schema_version") == "ybb.edge.windows-payload-source-lock.v2"
        and lock.get("python_runtime_source_sha256")
        == sources["python_runtime"]["sha256"]
        and lock.get("winsw_source_sha256") == sources["winsw"]["sha256"]
        and lock.get("cuda_installer_source_sha256")
        == sources["cuda_toolkit"]["sha256"]
        and lock.get("tensorrt_frontend_source_sha256")
        == sources["tensorrt"]["frontend"]["sha256"]
        and lock.get("tensorrt_runtime_source_sha256")
        == sources["tensorrt"]["runtime"]["sha256"]
        and lock.get("tensorrt_bindings_source_sha256")
        == sources["tensorrt"]["bindings"]["sha256"]
        and lock.get("python_runtime_zip_sha256") == _sha256(runtime)
        and lock.get("winsw_sha256") == _sha256(winsw)
        and lock.get("cuda_installer_sha256")
        == _sha256(inference_payloads["cuda_installer"])
        and lock.get("tensorrt_frontend_wheel_sha256")
        == _sha256(inference_payloads["tensorrt_frontend_wheel"])
        and lock.get("tensorrt_runtime_wheel_sha256")
        == _sha256(inference_payloads["tensorrt_runtime_wheel"])
        and lock.get("tensorrt_bindings_wheel_sha256")
        == _sha256(inference_payloads["tensorrt_bindings_wheel"])
    )


def _windows_inference_payload_paths(
    config: dict[str, Any], sources: dict[str, Any]
) -> dict[str, Path]:
    tensorrt = sources["tensorrt"]

    def selected(config_name: str, source: dict[str, Any], fallback_name: str) -> Path:
        raw = str(config.get(config_name) or "").strip()
        return (
            Path(raw).expanduser().resolve()
            if raw
            else WINDOWS_PAYLOAD_DIRECTORY / str(source.get("file_name") or fallback_name)
        )

    return {
        "cuda_installer": selected(
            "cuda_installer", sources["cuda_toolkit"], "cuda-runtime-installer.exe"
        ),
        "tensorrt_frontend_source": selected(
            "tensorrt_frontend_source",
            tensorrt["frontend"],
            "tensorrt-frontend.tar.gz",
        ),
        "tensorrt_frontend_wheel": selected(
            "tensorrt_frontend_wheel",
            {"file_name": tensorrt["frontend"]["wheel_file_name"]},
            "tensorrt-frontend.whl",
        ),
        "tensorrt_runtime_wheel": selected(
            "tensorrt_runtime_wheel", tensorrt["runtime"], "tensorrt-runtime.whl"
        ),
        "tensorrt_bindings_wheel": selected(
            "tensorrt_bindings_wheel", tensorrt["bindings"], "tensorrt-bindings.whl"
        ),
    }


def _build_tensorrt_frontend_wheel(
    config: dict[str, Any], source: Path, destination: Path
) -> None:
    python = str(config.get("python_executable") or sys.executable)
    with tempfile.TemporaryDirectory(prefix="ybb-edge-tensorrt-frontend-") as temporary:
        wheel_directory = Path(temporary)
        completed = subprocess.run(
            [
                python,
                "-m",
                "pip",
                "wheel",
                "--no-cache-dir",
                "--no-deps",
                "--wheel-dir",
                str(wheel_directory),
                str(source),
            ],
            cwd=ROOT,
            check=False,
            text=True,
        )
        wheels = sorted(wheel_directory.glob("*.whl"))
        if completed.returncode or len(wheels) != 1:
            raise BundleError("NVIDIA TensorRT frontend wheel build failed.")
        prepared = destination.with_name(destination.name + ".prepared")
        _normalize_wheel(wheels[0], prepared)
        os.replace(prepared, destination)


def prepare_windows_payloads(
    config: dict[str, Any],
    profile: dict[str, Any],
    *,
    sources_path: Path = WINDOWS_PAYLOAD_SOURCES,
) -> tuple[Path, Path]:
    """Download and verify every redistributable Windows inference payload."""
    sources = _json(sources_path)
    if sources.get("schema_version") != "ybb.edge.windows-payload-sources.v2":
        raise BundleError("Unsupported Windows payload source manifest.")
    if not all(
        isinstance(sources.get(name), dict)
        for name in ("python_runtime", "winsw", "cuda_toolkit", "tensorrt")
    ) or not all(
        isinstance(sources["tensorrt"].get(name), dict)
        for name in ("frontend", "runtime", "bindings")
    ):
        raise BundleError("Windows payload source manifest is incomplete.")
    runtime_source = sources["python_runtime"]
    winsw_source = sources["winsw"]
    expected_python = str(profile.get("python_tag") or "")
    configured_python = str(runtime_source.get("python_version") or "").replace(".", "")
    if configured_python != expected_python:
        raise BundleError(
            "Pinned Windows Python runtime does not match the platform profile ABI."
        )
    if str(sources["cuda_toolkit"].get("profile_version")) != str(
        profile.get("cuda_version")
    ) or not str(sources["tensorrt"].get("version") or "").startswith(
        str(profile.get("tensorrt_version") or "")
    ):
        raise BundleError("Pinned CUDA/TensorRT payloads do not match the Windows profile.")
    runtime, winsw, lock_path = _windows_payload_paths(config)
    inference_payloads = _windows_inference_payload_paths(config, sources)
    if _windows_payload_lock_is_current(
        lock_path, runtime, winsw, inference_payloads, sources
    ):
        print(
            json.dumps(
                {
                    "cuda_installer": str(inference_payloads["cuda_installer"]),
                    "python_runtime_archive": str(runtime),
                    "tensorrt_runtime_wheel": str(
                        inference_payloads["tensorrt_runtime_wheel"]
                    ),
                    "winsw_executable": str(winsw),
                    "windows_payloads": "reused",
                },
                sort_keys=True,
            )
        )
        return runtime, winsw

    runtime.parent.mkdir(parents=True, exist_ok=True)
    winsw.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous_lock = _json(lock_path) if lock_path.is_file() else {}
    except (BundleError, OSError, ValueError, json.JSONDecodeError):
        previous_lock = {}
    runtime_is_current = (
        runtime.is_file()
        and previous_lock.get("python_runtime_source_sha256")
        == runtime_source["sha256"]
        and previous_lock.get("python_runtime_zip_sha256") == _sha256(runtime)
    )
    if not runtime_is_current:
        with tempfile.TemporaryDirectory(prefix="ybb-edge-windows-python-") as temporary:
            staging = Path(temporary)
            source_archive = staging / "python-runtime-source.tar.gz"
            extracted = staging / "runtime"
            generated_runtime = staging / "python-runtime.zip"
            extracted.mkdir()
            _download_verified(runtime_source, source_archive)
            _extract_regular_tar_files(source_archive, extracted)
            _validate_windows_runtime_tree(
                extracted, str(runtime_source["python_version"])
            )
            _write_reproducible_zip(extracted, generated_runtime)
            staged_runtime = runtime.with_name(runtime.name + ".prepared")
            shutil.copyfile(generated_runtime, staged_runtime)
            os.replace(staged_runtime, runtime)

    _ensure_verified_payload(winsw_source, winsw)
    _ensure_verified_payload(
        sources["cuda_toolkit"], inference_payloads["cuda_installer"]
    )
    _ensure_verified_payload(
        sources["tensorrt"]["runtime"],
        inference_payloads["tensorrt_runtime_wheel"],
    )
    _ensure_verified_payload(
        sources["tensorrt"]["bindings"],
        inference_payloads["tensorrt_bindings_wheel"],
    )
    _ensure_verified_payload(
        sources["tensorrt"]["frontend"],
        inference_payloads["tensorrt_frontend_source"],
    )
    frontend_wheel = inference_payloads["tensorrt_frontend_wheel"]
    frontend_is_current = (
        frontend_wheel.is_file()
        and previous_lock.get("tensorrt_frontend_source_sha256")
        == sources["tensorrt"]["frontend"]["sha256"]
        and previous_lock.get("tensorrt_frontend_wheel_sha256")
        == _sha256(frontend_wheel)
    )
    if not frontend_is_current:
        _build_tensorrt_frontend_wheel(
            config,
            inference_payloads["tensorrt_frontend_source"],
            frontend_wheel,
        )

    _write_json(
        lock_path,
        {
            "schema_version": "ybb.edge.windows-payload-source-lock.v2",
            "python_runtime_version": runtime_source["version"],
            "python_runtime_source_url": runtime_source["url"],
            "python_runtime_source_sha256": runtime_source["sha256"],
            "python_runtime_zip": str(runtime),
            "python_runtime_zip_sha256": _sha256(runtime),
            "winsw_version": winsw_source["version"],
            "winsw_source_url": winsw_source["url"],
            "winsw_source_sha256": winsw_source["sha256"],
            "winsw_executable": str(winsw),
            "winsw_sha256": _sha256(winsw),
            "cuda_installer_version": sources["cuda_toolkit"]["version"],
            "cuda_installer_source_url": sources["cuda_toolkit"]["url"],
            "cuda_installer_source_sha256": sources["cuda_toolkit"]["sha256"],
            "cuda_installer": str(inference_payloads["cuda_installer"]),
            "cuda_installer_sha256": _sha256(
                inference_payloads["cuda_installer"]
            ),
            "tensorrt_version": sources["tensorrt"]["version"],
            "tensorrt_frontend_source_sha256": sources["tensorrt"]["frontend"][
                "sha256"
            ],
            "tensorrt_frontend_wheel": str(frontend_wheel),
            "tensorrt_frontend_wheel_sha256": _sha256(frontend_wheel),
            "tensorrt_runtime_source_sha256": sources["tensorrt"]["runtime"][
                "sha256"
            ],
            "tensorrt_runtime_wheel": str(
                inference_payloads["tensorrt_runtime_wheel"]
            ),
            "tensorrt_runtime_wheel_sha256": _sha256(
                inference_payloads["tensorrt_runtime_wheel"]
            ),
            "tensorrt_bindings_source_sha256": sources["tensorrt"]["bindings"][
                "sha256"
            ],
            "tensorrt_bindings_wheel": str(
                inference_payloads["tensorrt_bindings_wheel"]
            ),
            "tensorrt_bindings_wheel_sha256": _sha256(
                inference_payloads["tensorrt_bindings_wheel"]
            ),
        },
    )
    print(
        json.dumps(
            {
                "cuda_installer": str(inference_payloads["cuda_installer"]),
                "python_runtime_archive": str(runtime),
                "tensorrt_runtime_wheel": str(
                    inference_payloads["tensorrt_runtime_wheel"]
                ),
                "winsw_executable": str(winsw),
                "windows_payloads": "downloaded",
            },
            sort_keys=True,
        )
    )
    return runtime, winsw


def _add_windows_inference_wheels(
    destination: Path, config: dict[str, Any], profile: dict[str, Any]
) -> None:
    sources = _json(WINDOWS_PAYLOAD_SOURCES)
    payloads = _windows_inference_payload_paths(config, sources)
    wheels = (
        payloads["tensorrt_frontend_wheel"],
        payloads["tensorrt_runtime_wheel"],
        payloads["tensorrt_bindings_wheel"],
    )
    for source in wheels:
        if not source.is_file():
            raise BundleError(
                f"Pinned Windows TensorRT wheel is missing: {source}. "
                "Run prepare-wheelhouse while online first."
            )
        target = destination / source.name
        if target.is_file():
            if _sha256(target) != _sha256(source):
                raise BundleError(f"Existing TensorRT wheel differs from pinned payload: {target}")
            continue
        _link_or_copy(source, target)
    _validate_wheelhouse(destination, profile, config)


def _write_wheelhouse_source_lock(
    destination: Path,
    profile: dict[str, Any],
    platform_name: str,
    index_url: str,
) -> None:
    _write_json(
        destination / "wheelhouse-source.lock.json",
        {
            "schema_version": "ybb.edge.wheelhouse-source.v1",
            "platform": platform_name,
            "python_tag": profile["python_tag"],
            "python_abi": profile["python_abi"],
            "package_index": index_url,
            "wheels": [
                {"file_name": wheel.name, "sha256": _sha256(wheel)}
                for wheel in sorted(destination.glob("*.whl"))
            ],
        },
    )


def _ensure_basler_wheel(
    config: dict[str, Any], profile: dict[str, Any], destination: Path
) -> None:
    """Supplement existing wheelhouses without rebuilding the Jetson base or dependencies."""
    if any(destination.glob("pypylon-*.whl")):
        return
    index_url = str(config.get("package_index_url") or "https://pypi.org/simple")
    parsed = urlsplit(index_url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise BundleError("package_index_url must be HTTPS and must not contain credentials.")
    with tempfile.TemporaryDirectory(prefix="ybb-edge-basler-") as temporary:
        command = [
            str(config.get("python_executable") or sys.executable), "-m", "pip", "download",
            "--dest", temporary, "--only-binary=:all:", "--no-deps",
            "--implementation", "cp", "--python-version", str(profile["python_tag"]),
            "--abi", str(profile["python_abi"]), "--abi", "abi3",
            "--index-url", index_url,
        ]
        for platform in profile["pip_platforms"]:
            command.extend(["--platform", str(platform)])
        if config.get("additional_wheel_directory"):
            command.extend(["--find-links", str(config["additional_wheel_directory"])])
        command.append("pypylon>=3.0.0,<27")
        completed = subprocess.run(command, cwd=ROOT, check=False, text=True)
        wheels = list(Path(temporary).glob("pypylon-*.whl"))
        if completed.returncode or len(wheels) != 1:
            raise BundleError("Could not prepare the target-compatible Basler pypylon wheel.")
        _normalize_wheel(wheels[0], destination / wheels[0].name)


def _refresh_internal_wheels(config: dict[str, Any], destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="ybb-edge-internal-refresh-") as temporary:
        built_directory = Path(temporary)
        online_config = dict(config)
        online_config["build_internal_wheels"] = True
        _build_internal_wheels(online_config, built_directory, build_isolation=True)
        for built in sorted(built_directory.glob("*.whl")):
            distribution = _normalized_distribution(built.name)
            for previous in destination.glob("*.whl"):
                if _normalized_distribution(previous.name) == distribution:
                    previous.unlink()
            _normalize_wheel(built, destination / built.name)


def _generate_env(config: dict[str, Any], profile: dict[str, Any]) -> str:
    plant_ips = str(config.get("plant_allowed_ipv4s") or "").strip()
    adapters = config.get("camera_adapters") or profile["approved_camera_adapters"]
    if isinstance(adapters, str):
        adapters = [item.strip() for item in adapters.split(",") if item.strip()]
    invalid = sorted(set(adapters) - set(profile["approved_camera_adapters"]))
    if invalid:
        raise BundleError(f"Camera adapters are not approved for this profile: {invalid}")
    default_engine_builder = (
        "tensorrt_python" if profile["platform"] == "windows_11" else "trtexec"
    )
    values: dict[str, object] = {
        "YBB_EDGE_PLATFORM": profile["platform"],
        "YBB_EDGE_ARCHITECTURE": profile["architecture"],
        "YBB_EDGE_OS_VERSION": profile["base_os"],
        "YBB_EDGE_JETPACK_VERSION": profile.get("jetpack_version") or "",
        "YBB_EDGE_L4T_VERSION": profile.get("l4t_version") or "",
        "YBB_EDGE_CUDA_VERSION": profile.get("cuda_version") or "",
        "YBB_EDGE_TENSORRT_VERSION": profile.get("tensorrt_version") or "",
        "YBB_EDGE_DEEPSTREAM_VERSION": profile.get("deepstream_version") or "",
        "YBB_EDGE_HOSTNAME": config["device_hostname"],
        "YBB_EDGE_MANAGEMENT_IP": config["device_ipv4"],
        "YBB_EDGE_SERVER_IP": config["server_ipv4"],
        "YBB_EDGE_CONTROL_PORT": int(config.get("control_port", 9443)),
        "YBB_EDGE_PLANT_API_PORT": int(config.get("plant_api_port", 9444)),
        "YBB_EDGE_RTSP_PORT": int(config.get("rtsp_port", 8554)),
        "YBB_EDGE_PLANT_ALLOWED_IPS": plant_ips,
        "YBB_ALLOW_PLC_WRITES": str(bool(config.get("allow_plc_writes", False))).lower(),
        "YBB_EDGE_DATA_ROOT": config.get("state_root", "/var/lib/ybb-edge"),
        "YBB_EDGE_ARTIFACT_QUOTA_BYTES": int(config.get("artifact_quota_bytes", 34359738368)),
        "YBB_EDGE_OUTBOX_QUOTA_BYTES": int(config.get("outbox_quota_bytes", 67108864)),
        "YBB_EDGE_OUTBOX_MAX_ITEMS": int(config.get("outbox_max_items", 10000)),
        "YBB_EDGE_MINIMUM_DISK_FREE_BYTES": int(config.get("minimum_disk_free_bytes", 2147483648)),
        "YBB_EDGE_ENGINE_BUILDER_MODE": config.get(
            "engine_builder_mode", default_engine_builder
        ),
        "YBB_EDGE_TRTEXEC_PATH": config.get("trtexec_path", "trtexec"),
        "YBB_EDGE_CAMERA_ADAPTERS": ",".join(adapters),
        "YBB_EDGE_CAPABILITY_INVENTORY": "probe",
    }
    for key, value in values.items():
        if any(character in str(value) for character in ("\r", "\n", "\x00")):
            raise BundleError(f"Unsafe control character in {key}.")
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def _validate_device_configuration(config: dict[str, Any]) -> None:
    hostname = str(config["device_hostname"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", hostname):
        raise BundleError("device_hostname is invalid.")
    for name in ("device_ipv4", "server_ipv4", "gateway_ipv4"):
        value = str(config.get(name, "")).strip()
        if value:
            try:
                ipaddress.IPv4Address(value)
            except ipaddress.AddressValueError as exc:
                raise BundleError(f"{name} is not a valid IPv4 address.") from exc
    for name in ("plant_allowed_ipv4s", "dns_servers"):
        for value in str(config.get(name, "")).split(","):
            if value.strip():
                try:
                    ipaddress.IPv4Address(value.strip())
                except ipaddress.AddressValueError as exc:
                    raise BundleError(f"{name} contains an invalid IPv4 address.") from exc
    prefix = int(config.get("prefix_length", 24))
    if not 8 <= prefix <= 32:
        raise BundleError("prefix_length must be between 8 and 32.")
    for name in ("control_port", "plant_api_port", "rtsp_port"):
        port = int(
            config.get(
                name, {"control_port": 9443, "plant_api_port": 9444, "rtsp_port": 8554}[name]
            )
        )
        if not 1 <= port <= 65535:
            raise BundleError(f"{name} must be between 1 and 65535.")


def _stage_payload(config: dict[str, Any], profile: dict[str, Any], stage: Path) -> None:
    edge_system_packages.stage(profile, stage)
    wheel_source = _require_path(config, "wheelhouse_directory", directory=True)
    raw_wheels = Path(tempfile.mkdtemp(prefix="ybb-edge-wheels-"))
    try:
        for wheel in sorted(wheel_source.glob("*.whl")):
            _link_or_copy(wheel, raw_wheels / wheel.name)
        _build_internal_wheels(config, raw_wheels)
        _validate_internal_wheel_sources(raw_wheels)
        wheels = sorted(raw_wheels.glob("*.whl"))
        runtime_distributions = _validate_wheelhouse(raw_wheels, profile, config)
        target_wheels = stage / "wheelhouse"
        target_wheels.mkdir()
        for wheel in wheels:
            _normalize_wheel(wheel, target_wheels / wheel.name)
        _write_json(
            stage / "wheelhouse.lock.json",
            {
                "schema_version": "ybb.edge.wheelhouse-lock.v1",
                "platform": profile["platform"],
                "wheels": [
                    {
                        "file_name": wheel.name,
                        "size_bytes": wheel.stat().st_size,
                        "sha256": _sha256(wheel),
                    }
                    for wheel in sorted(target_wheels.glob("*.whl"))
                ],
            },
        )
        runtime_requirements = list(runtime_distributions)
        if profile["platform"] == "windows_11":
            sources = _json(WINDOWS_PAYLOAD_SOURCES)
            runtime_requirements.append(
                f"tensorrt-cu13=={sources['tensorrt']['version']}"
            )
        (stage / "runtime-requirements.txt").write_text(
            "".join(f"{item}\n" for item in sorted(runtime_requirements)),
            encoding="ascii",
            newline="\n",
        )
    finally:
        shutil.rmtree(raw_wheels, ignore_errors=True)

    deployment_target = stage / "deployment/edge"
    deployment_target.mkdir(parents=True)
    for name in (
        "install-jetson-offline.sh",
        "Install-JetsonOfflineUI.sh",
        "install-jetson-offline-ui.py",
        "Install-WindowsOffline.ps1",
        "validate-device-env.py",
        "configure-ufw.sh",
        "edge-watchdog.py",
        "edge-runtime-launcher.py",
        "edge-settings-helper.py",
        "edge-system-packages.py",
        "ybb-edge-settings.service",
        "ybb-edge-settings.timer",
        "check-runtime-dependencies.py",
        "ybb-edge-agent.service",
        "ybb-edge-watchdog.service",
        "ybb-edge-watchdog-recover.service",
        "ybb-edge-watchdog.timer",
    ):
        shutil.copyfile(ROOT / "deployment/edge" / name, deployment_target / name)
    shutil.copyfile(
        ROOT / "backend/packages/cv-contracts/src/cv_contracts/edge_environment.py",
        deployment_target / "edge_environment.py",
    )
    provisioning = ROOT / "deployment/edge/provisioning"
    if provisioning.exists():
        _copy_tree_files(provisioning, stage / "deployment/edge/provisioning")
    _write_json(stage / "platform-profile.json", profile)
    _write_json(
        stage / "provisioning.json",
        {
            "schema_version": "ybb.edge.provisioning.v1",
            "platform": profile["platform"],
            "device_hostname": config["device_hostname"],
            "device_ipv4": config["device_ipv4"],
            "prefix_length": int(config.get("prefix_length", 24)),
            "gateway_ipv4": config.get("gateway_ipv4", ""),
            "dns_servers": config.get("dns_servers", ""),
            "server_ipv4": config["server_ipv4"],
            "plant_allowed_ipv4s": config.get("plant_allowed_ipv4s", ""),
            "timezone": config.get("timezone", "Europe/Vienna"),
            "state_root": config.get("state_root", "/var/lib/ybb-edge"),
            "nvme_device": config.get("nvme_device", "/dev/nvme0n1p1"),
            "windows_interface_alias": config.get("windows_interface_alias", "Ethernet"),
        },
    )
    (stage / "device.env").write_text(
        _generate_env(config, profile), encoding="utf-8", newline="\n"
    )

    if profile["platform"] == "windows_11":
        shutil.copyfile(
            ROOT / "deployment/edge/WindowsReleaseSignatureVerifier.cs",
            deployment_target / "WindowsReleaseSignatureVerifier.cs",
        )
        runtime, winsw, _lock = _windows_payload_paths(config)
        sources = _json(WINDOWS_PAYLOAD_SOURCES)
        inference_payloads = _windows_inference_payload_paths(config, sources)
        if not runtime.is_file():
            raise BundleError(
                "Windows Python runtime is missing (python_runtime_archive). "
                "Run prepare-wheelhouse while online first."
            )
        if not winsw.is_file():
            raise BundleError(
                "WinSW is missing (winsw_executable). "
                "Run prepare-wheelhouse while online first."
            )
        cuda_installer = inference_payloads["cuda_installer"]
        custom_cuda_installer = bool(str(config.get("cuda_installer") or "").strip())
        if not cuda_installer.is_file() or (
            not custom_cuda_installer
            and _sha256(cuda_installer) != sources["cuda_toolkit"]["sha256"]
        ):
            raise BundleError(
                "Pinned CUDA runtime installer is missing or invalid. "
                "Run prepare-wheelhouse while online first."
            )
        _link_or_copy(winsw, stage / "winsw.exe")
        _link_or_copy(runtime, stage / "python-runtime.zip")
        _link_or_copy(cuda_installer, stage / "cuda-runtime-installer.exe")
        windows_installer = _require_path(config, "windows_installer_executable")
        _link_or_copy(windows_installer, stage / "YBB-Edge-Windows-Installer.exe")


def _manifest(stage: Path, config: dict[str, Any], profile: dict[str, Any], epoch: int) -> None:
    observed = datetime.fromtimestamp(epoch, UTC).isoformat()
    files = []
    for path in sorted(item for item in stage.rglob("*") if item.is_file()):
        relative = path.relative_to(stage).as_posix()
        files.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": _sha256(path)})
    _write_json(
        stage / "release-manifest.json",
        {
            "schema_version": "ybb.edge.offline-release.v1",
            "release_version": config["release_version"],
            "platform": profile["platform"],
            "architecture": profile["architecture"],
            "source_date_epoch": epoch,
            "created_at": observed,
            "signature_policy": "required" if config.get("require_signature") else "prepared",
            "files": files,
        },
    )


def _prepare_signature(stage: Path, config: dict[str, Any]) -> bool:
    private_raw = str(config.get("signing_private_key", "")).strip()
    public_raw = str(config.get("signing_public_key", "")).strip()
    required = bool(config.get("require_signature", False))
    if not private_raw and not public_raw and not required:
        (stage / "SIGNATURE_POLICY").write_text("prepared\n", encoding="ascii", newline="\n")
        return False
    if not private_raw or not public_raw:
        raise BundleError(
            "Both signing_private_key and signing_public_key are required for signing."
        )
    private_key = Path(private_raw).expanduser().resolve()
    public_key = Path(public_raw).expanduser().resolve()
    if not private_key.is_file() or not public_key.is_file():
        raise BundleError("Signing key file not found.")
    shutil.copyfile(public_key, stage / "release-signing-public.pem")
    _write_json(
        stage / "release-signing-key.json",
        {
            "schema_version": "ybb.edge.release-key.v1",
            "algorithm": "RSA-SHA256",
            "public_key_pem_sha256": _sha256(public_key),
        },
    )
    (stage / "SIGNATURE_POLICY").write_text(
        "required\n" if required else "signed\n", encoding="ascii", newline="\n"
    )
    return True


def _sign_checksums(stage: Path, config: dict[str, Any], passphrase: str = "") -> None:
    try:
        from build_edge_runtime_update import _passphrase_input, _resolve_openssl
    except ModuleNotFoundError:
        from scripts.release.build_edge_runtime_update import _passphrase_input, _resolve_openssl

    private_key = Path(str(config["signing_private_key"])).expanduser().resolve()
    openssl = _resolve_openssl(str(config.get("openssl_executable") or "openssl"))
    signature = stage / "SHA256SUMS.sig"
    completed = subprocess.run(
        [
            openssl,
            "dgst",
            "-sha256",
            "-sign",
            str(private_key),
            "-passin",
            "stdin",
            "-out",
            str(signature),
            str(stage / "SHA256SUMS"),
        ],
        input=_passphrase_input(passphrase),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode:
        raise BundleError("Release-Signatur fehlgeschlagen. RSA-Schlüssel und Passphrase prüfen.")
    verified = subprocess.run(
        [
            openssl,
            "dgst",
            "-sha256",
            "-verify",
            str(stage / "release-signing-public.pem"),
            "-signature",
            str(signature),
            str(stage / "SHA256SUMS"),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if verified.returncode:
        raise BundleError("Privater und öffentlicher Release-Schlüssel passen nicht zusammen.")


def _checksums(stage: Path) -> None:
    paths = sorted(
        path for path in stage.rglob("*") if path.is_file() and path.name != "SHA256SUMS"
    )
    lines = [f"{_sha256(path)}  {path.relative_to(stage).as_posix()}\n" for path in paths]
    (stage / "SHA256SUMS").write_text("".join(lines), encoding="ascii", newline="\n")


def _zip(stage: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in stage.rglob("*") if item.is_file()):
            info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), FIXED_ZIP_DATE)
            info.compress_type = (
                zipfile.ZIP_STORED
                if path.suffix.lower() in {".exe", ".whl", ".zip"}
                else zipfile.ZIP_DEFLATED
            )
            info.external_attr = 0o100644 << 16
            info.file_size = path.stat().st_size
            with path.open("rb") as input_stream, archive.open(
                info, "w", force_zip64=True
            ) as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=8 * 1024 * 1024)


def _tar_gz(stage: Path, output: Path, epoch: int) -> None:
    with (
        output.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=epoch, filename="") as compressed,
    ):
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path in sorted(item for item in stage.rglob("*") if item.is_file()):
                relative = path.relative_to(stage).as_posix()
                info = archive.gettarinfo(str(path), relative)
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = epoch
                info.mode = 0o755 if path.suffix.lower() in {".sh", ".py"} else 0o644
                with path.open("rb") as stream:
                    archive.addfile(info, stream)


def build(
    config_path: Path, output_directory: Path | None = None, *, signing_passphrase: str = ""
) -> Path:
    config = _json(config_path)
    platform_name = str(config.get("target_platform", ""))
    if platform_name not in PROFILE_FILES:
        raise BundleError(f"Unsupported target_platform: {platform_name!r}")
    for field in ("release_version", "device_hostname", "device_ipv4", "server_ipv4"):
        if not str(config.get(field, "")).strip():
            raise BundleError(f"Missing required configuration value: {field}")
    _validate_device_configuration(config)
    version = _safe_name(str(config["release_version"]), "release_version")
    epoch = int(config.get("source_date_epoch", os.environ.get("SOURCE_DATE_EPOCH", 1704067200)))
    if epoch < 315532800:
        raise BundleError("source_date_epoch must be at or after 1980-01-01.")
    profile = _json(ROOT / "deployment/edge/profiles" / PROFILE_FILES[platform_name])
    destination = (
        output_directory or Path(str(config.get("output_directory", "dist/edge")))
    ).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    extension = ".zip" if profile["archive_format"] == "zip" else ".tar.gz"
    output = destination / f"ybb-edge-{platform_name}-{version}{extension}"
    with tempfile.TemporaryDirectory(prefix="ybb-edge-bundle-") as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        _stage_payload(config, profile, stage)
        _manifest(stage, config, profile, epoch)
        signing_enabled = _prepare_signature(stage, config)
        _checksums(stage)
        if signing_enabled:
            _sign_checksums(stage, config, signing_passphrase)
        if extension == ".zip":
            _zip(stage, output)
        else:
            _tar_gz(stage, output, epoch)
    result = {"bundle": str(output), "sha256": _sha256(output)}
    if platform_name == "windows_11":
        companion_installer = destination / f"ybb-edge-{platform_name}-{version}-installer.exe"
        shutil.copyfile(_require_path(config, "windows_installer_executable"), companion_installer)
        result["installer"] = str(companion_installer)
        result["installer_sha256"] = _sha256(companion_installer)
    print(json.dumps(result, sort_keys=True))
    return output


def _validate_prepared_wheelhouse(
    destination: Path,
    profile: dict[str, Any],
    config: dict[str, Any],
    platform_name: str,
    *,
    validate_distributions: bool = True,
) -> None:
    lock_path = destination / "wheelhouse-source.lock.json"
    if not lock_path.is_file():
        raise BundleError(
            "Wheelhouse directory is not empty and has no wheelhouse-source.lock.json. "
            "Select an empty directory to avoid mixed releases."
        )
    lock = _json(lock_path)
    if (
        lock.get("schema_version") != "ybb.edge.wheelhouse-source.v1"
        or lock.get("platform") != platform_name
        or str(lock.get("python_tag")) != str(profile["python_tag"])
        or str(lock.get("python_abi")) != str(profile["python_abi"])
    ):
        raise BundleError("Existing wheelhouse source lock does not match the selected platform.")
    locked_wheels = lock.get("wheels")
    if not isinstance(locked_wheels, list):
        raise BundleError("Existing wheelhouse source lock has no valid wheel list.")
    expected: dict[str, str] = {}
    for entry in locked_wheels:
        if not isinstance(entry, dict):
            raise BundleError("Existing wheelhouse source lock contains an invalid entry.")
        name = str(entry.get("file_name") or "")
        digest = str(entry.get("sha256") or "")
        if not name or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise BundleError("Existing wheelhouse source lock contains an invalid entry.")
        expected[name] = digest
    actual = {wheel.name: _sha256(wheel) for wheel in destination.glob("*.whl")}
    if actual != expected:
        raise BundleError("Existing wheelhouse does not match wheelhouse-source.lock.json.")
    if validate_distributions:
        _validate_wheelhouse(destination, profile, config)


def prepare_wheelhouse(config_path: Path) -> Path:
    """Online staging step used by the GUI; produced wheels are then consumed fully offline."""
    config = _json(config_path)
    platform_name = str(config.get("target_platform", ""))
    if platform_name not in PROFILE_FILES:
        raise BundleError(f"Unsupported target_platform: {platform_name!r}")
    profile = _json(ROOT / "deployment/edge/profiles" / PROFILE_FILES[platform_name])
    destination_raw = str(config.get("wheelhouse_directory", "")).strip()
    if not destination_raw:
        raise BundleError("wheelhouse_directory is required.")
    destination = Path(destination_raw).expanduser().resolve()
    reuse_wheelhouse = destination.exists() and any(destination.iterdir())
    if reuse_wheelhouse and not (destination / "wheelhouse-source.lock.json").is_file():
        raise BundleError(
            "Wheelhouse directory is not empty and has no wheelhouse-source.lock.json. "
            "Select an empty directory to avoid mixed releases."
        )
    index_url = str(config.get("package_index_url") or "https://pypi.org/simple").strip()
    parsed_index = urlsplit(index_url)
    if parsed_index.scheme != "https" or parsed_index.username or parsed_index.password:
        raise BundleError("package_index_url must be HTTPS and must not contain credentials.")
    additional_raw = str(config.get("additional_wheel_directory") or "").strip()
    additional = Path(additional_raw).expanduser().resolve() if additional_raw else None
    if additional is not None and not additional.is_dir():
        raise BundleError(f"Additional wheel directory not found: {additional}")

    python = str(config.get("python_executable") or sys.executable)
    if platform_name == "windows_11":
        prepare_windows_payloads(config, profile)
    edge_system_packages.prepare(profile)
    if reuse_wheelhouse:
        _validate_prepared_wheelhouse(
            destination,
            profile,
            config,
            platform_name,
            validate_distributions=False,
        )
        _ensure_basler_wheel(config, profile, destination)
        _refresh_internal_wheels(config, destination)
        if platform_name == "windows_11":
            _add_windows_inference_wheels(destination, config, profile)
        _validate_wheelhouse(destination, profile, config)
        _write_wheelhouse_source_lock(destination, profile, platform_name, index_url)
        print(json.dumps({"wheelhouse": str(destination), "status": "reused"}, sort_keys=True))
    else:
        with tempfile.TemporaryDirectory(prefix="ybb-edge-wheelhouse-") as temporary:
            staging = Path(temporary)
            internal = staging / "internal"
            downloaded = staging / "downloaded"
            internal.mkdir()
            downloaded.mkdir()
            online_config = dict(config)
            online_config["build_internal_wheels"] = True
            _build_internal_wheels(online_config, internal, build_isolation=True)
            edge_wheels = sorted(internal.glob("edge_agent-*.whl"))
            if len(edge_wheels) != 1:
                raise BundleError("Exactly one internally built edge-agent wheel was expected.")
            command = [
                python,
                "-m",
                "pip",
                "download",
                "--dest",
                str(downloaded),
                "--find-links",
                str(internal),
                "--only-binary=:all:",
                "--implementation",
                "cp",
                "--python-version",
                str(profile["python_tag"]),
                "--abi",
                str(profile["python_abi"]),
                "--index-url",
                index_url,
            ]
            for pip_platform in profile["pip_platforms"]:
                command.extend(["--platform", str(pip_platform)])
            if additional is not None:
                command.extend(["--find-links", str(additional)])
            command.append(str(edge_wheels[0]))
            requested_distributions = [
                item
                for item in _runtime_distribution_names(config)
                if item.replace("_", "-").lower() != "edge-agent"
            ]
            requested_names = {
                item.replace("_", "-").lower() for item in requested_distributions
            }
            requested_distributions.extend(
                item
                for item in _target_marker_distributions(profile)
                if item.replace("_", "-").lower() not in requested_names
            )
            command.extend(requested_distributions)
            completed = subprocess.run(command, cwd=ROOT, check=False, text=True)
            if completed.returncode:
                raise BundleError("Platform wheel download failed; see pip output above.")
            for wheel in internal.glob("*.whl"):
                if not (downloaded / wheel.name).exists():
                    shutil.copyfile(wheel, downloaded / wheel.name)
            if platform_name == "windows_11":
                _add_windows_inference_wheels(downloaded, config, profile)
            _validate_wheelhouse(downloaded, profile, config)
            destination.mkdir(parents=True, exist_ok=True)
            for wheel in sorted(downloaded.glob("*.whl")):
                _normalize_wheel(wheel, destination / wheel.name)
            _write_wheelhouse_source_lock(
                destination, profile, platform_name, index_url
            )
        print(json.dumps({"wheelhouse": str(destination), "status": "prepared"}, sort_keys=True))
    return destination


def verify(
    directory: Path,
    openssl: str = "openssl",
    trusted_public_key: Path | None = None,
) -> None:
    root = directory.resolve()
    checksum_file = root / "SHA256SUMS"
    if not checksum_file.is_file():
        raise BundleError("Mandatory SHA256SUMS is missing.")
    for line in checksum_file.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  (.+)", line)
        if match is None:
            raise BundleError(f"Invalid SHA256SUMS line: {line!r}")
        relative = PurePosixPath(match.group(2))
        if relative.is_absolute() or ".." in relative.parts:
            raise BundleError(f"Unsafe checksum path: {relative}")
        path = root.joinpath(*relative.parts)
        if not path.is_file() or _sha256(path) != match.group(1):
            raise BundleError(f"Checksum mismatch: {relative}")
    policy = (root / "SIGNATURE_POLICY").read_text(encoding="ascii").strip()
    if policy in {"signed", "required"}:
        if trusted_public_key is None or not trusted_public_key.is_file():
            raise BundleError("A pinned trusted public key is required for signed releases.")
        completed = subprocess.run(
            [
                openssl,
                "dgst",
                "-sha256",
                "-verify",
                str(trusted_public_key.resolve()),
                "-signature",
                str(root / "SHA256SUMS.sig"),
                str(root / "SHA256SUMS"),
            ],
            check=False,
        )
        if completed.returncode:
            raise BundleError("Release signature verification failed.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or verify a reproducible YBB edge bundle.")
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--config", type=Path, required=True)
    build_parser.add_argument("--output-directory", type=Path)
    build_parser.add_argument("--signing-passphrase-stdin", action="store_true")
    prepare_parser = sub.add_parser("prepare-wheelhouse")
    prepare_parser.add_argument("--config", type=Path, required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--bundle-directory", type=Path, required=True)
    verify_parser.add_argument("--openssl", default="openssl")
    verify_parser.add_argument("--trusted-public-key", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            passphrase = (
                sys.stdin.buffer.readline().decode("utf-8").rstrip("\r\n")
                if args.signing_passphrase_stdin
                else ""
            )
            build(args.config, args.output_directory, signing_passphrase=passphrase)
        elif args.command == "prepare-wheelhouse":
            prepare_wheelhouse(args.config)
        else:
            verify(args.bundle_directory, args.openssl, args.trusted_public_key)
    except (BundleError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
