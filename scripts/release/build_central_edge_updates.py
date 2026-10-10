"""Build signed Edge updates alongside the existing central offline bundle (development only)."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit

try:
    from . import build_edge_offline_bundle as builder
    from . import build_edge_runtime_update as exporter
except ImportError:
    import build_edge_offline_bundle as builder
    import build_edge_runtime_update as exporter

ROOT = Path(__file__).resolve().parents[2]
INTERNAL = ("cv_status", "cv_contracts", "cv_utils", "cv_edge", "cv_runtime", "edge_agent")


def _digest_files(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0")
        digest.update(content.decode("utf-8-sig").replace("\r\n", "\n").encode() + b"\0")
    return digest.hexdigest()


def archive_code_digests(archive_path: Path) -> dict[str, str]:
    sources: dict[str, dict[str, bytes]] = {name: {} for name in INTERNAL}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            for name in INTERNAL:
                project = (
                    "services/edge-agent"
                    if name == "edge_agent"
                    else f"packages/{name.replace('_', '-')}"
                )
                prefix = f"backend/{project}/src/{name}/"
                path = member.name.removeprefix("./")
                if member.isfile() and path.startswith(prefix) and path.endswith(".py"):
                    sources[name][path.removeprefix(prefix)] = archive.extractfile(member).read()
    if any(not files for files in sources.values()):
        raise ValueError("Central app archive is missing internal Edge Python sources.")
    return {name: _digest_files(files) for name, files in sources.items()}


def validate_target_wheels(wheelhouse: Path, profile: dict) -> None:
    """Use the launcher's dependency checker with wheel metadata and target markers."""
    spec = importlib.util.spec_from_file_location(
        "edge_dependency_check", ROOT / "deployment/edge/check-runtime-dependencies.py"
    )
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    installed = {}
    wheels = list(wheelhouse.glob("*.whl"))
    for path in wheels:
        with zipfile.ZipFile(path) as wheel:
            names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError(f"Missing wheel metadata: {path.name}")
            metadata = BytesParser().parsebytes(wheel.read(names[0]))
        name = checker.canonicalize_name(metadata["Name"])
        if name in installed:
            raise ValueError(f"Multiple versions of {name} in Edge wheelhouse.")
        installed[name] = {
            "version": metadata["Version"],
            "requires": metadata.get_all("Requires-Dist", []),
            "requires_python": metadata.get("Requires-Python"),
        }
        _, _, _, tags = checker.parse_wheel_filename(path.name)
        platforms = set(profile["pip_platforms"]) | {"any"}
        python_tag = profile["python_tag"]
        if not any(
            tag.platform in platforms
            and (
                (
                    tag.interpreter in {"py3", f"py{python_tag}", f"cp{python_tag}"}
                    and tag.abi in {"none", profile.get("python_abi", f"cp{python_tag}")}
                )
                or (
                    tag.abi == "abi3"
                    and tag.interpreter.startswith("cp")
                    and int(tag.interpreter[2:]) <= int(python_tag)
                )
            )
            for tag in tags
        ):
            raise ValueError(f"Wheel does not match target Python/platform: {path.name}")
    environment = checker.default_environment()
    python_version = f"{profile['python_tag'][0]}.{profile['python_tag'][1:]}"
    windows = profile["platform"] == "windows_11"
    environment.update(
        python_version=python_version,
        python_full_version=python_version + ".0",
        implementation_version=python_version + ".0",
        implementation_name="cpython",
        platform_python_implementation="CPython",
        platform_machine="AMD64" if windows else "aarch64",
        os_name="nt" if windows else "posix",
        sys_platform="win32" if windows else "linux",
        platform_system="Windows" if windows else "Linux",
    )
    errors = checker.dependency_errors([p.name for p in wheels], installed, environment)
    if errors:
        raise ValueError("Incomplete target dependencies:\n" + "\n".join(errors))


def refresh_dependencies(wheelhouse: Path, profile: dict, config: dict) -> None:
    try:
        validate_target_wheels(wheelhouse, profile)
        return
    except ValueError as exc:
        print(f"Refreshing target dependencies: {exc}", flush=True)
    with tempfile.TemporaryDirectory(prefix="ybb-edge-dependencies-") as temporary:
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            temporary,
            "--find-links",
            str(wheelhouse),
            "--only-binary=:all:",
            "--implementation",
            "cp",
            "--python-version",
            profile["python_tag"],
            "--abi",
            profile["python_abi"],
            "--abi",
            "abi3",
            "--index-url",
            config["package_index_url"],
        ]
        for platform in profile["pip_platforms"]:
            command.extend(["--platform", platform])
        if config.get("additional_wheel_directory"):
            command.extend(["--find-links", config["additional_wheel_directory"]])
        for name in INTERNAL:
            command.extend(str(p) for p in wheelhouse.glob(name + "-*.whl"))
        command.extend(builder._target_marker_distributions(profile))
        subprocess.run(command, check=True, cwd=ROOT)
        for wheel in Path(temporary).glob("*.whl"):
            distribution = builder._normalized_distribution(wheel.name)
            for old in wheelhouse.glob("*.whl"):
                if builder._normalized_distribution(old.name) == distribution:
                    old.unlink()
            builder._normalize_wheel(wheel, wheelhouse / wheel.name)
    validate_target_wheels(wheelhouse, profile)


def build_updates(
    config: dict, output: Path, central_release_id: str, app_archive: Path, passphrase: str = ""
) -> dict:
    expected_code = archive_code_digests(app_archive)
    bundles = config.get("bundles", [])
    if not isinstance(bundles, list) or len(bundles) > 32:
        raise ValueError("Expected at most 32 Edge base bundles.")
    index = urlsplit(config.get("package_index_url") or "https://pypi.org/simple")
    if index.scheme != "https" or index.username or index.password:
        raise ValueError("Edge package index must use HTTPS and must not contain credentials.")
    openssl = exporter._resolve_openssl(config.get("openssl", "openssl")) if bundles else "openssl"
    key = Path(config.get("signing_key", ""))
    if bundles:
        exporter._validate_signing_key(openssl, key, passphrase)
    output.mkdir(parents=True, exist_ok=True)
    targets = {
        "schema_version": "edge.software.targets.v1",
        "central_release_id": central_release_id,
        "runtime_code_digest": expected_code["cv_runtime"],
        "packages": [],
    }
    profiles = set()
    for source in bundles:
        with tempfile.TemporaryDirectory(prefix="ybb-central-edge-") as temporary:
            stage = Path(temporary)
            bundle = Path(source).resolve()
            if bundle.is_file():
                exporter._extract_runtime_inputs(bundle, stage)
            else:
                shutil.copy2(bundle / "platform-profile.json", stage / "platform-profile.json")
                shutil.copytree(bundle / "wheelhouse", stage / "wheelhouse")
            profile = json.loads((stage / "platform-profile.json").read_text(encoding="utf-8-sig"))
            identity = tuple(
                profile.get(k)
                for k in (
                    "platform",
                    "architecture",
                    "python_tag",
                    "cuda_version",
                    "tensorrt_version",
                )
            )
            if identity in profiles:
                raise ValueError(
                    "Select only one base bundle per Edge platform/Python/CUDA/TensorRT profile."
                )
            profiles.add(identity)
            wheelhouse = stage / "wheelhouse"
            build_config = {
                "target_platform": profile["platform"],
                "package_index_url": config.get("package_index_url") or "https://pypi.org/simple",
                "additional_wheel_directory": config.get("additional_wheel_directory", ""),
            }
            print(
                f"Edge-Update vorbereiten: {profile['platform']} / Python {profile['python_tag']}",
                flush=True,
            )
            builder._ensure_basler_wheel(build_config, profile, wheelhouse)
            builder._refresh_internal_wheels(build_config, wheelhouse)
            builder._validate_wheelhouse(wheelhouse, profile, build_config)
            refresh_dependencies(wheelhouse, profile, build_config)
            for name in INTERNAL:
                paths = list(wheelhouse.glob(name + "-*.whl"))
                if len(paths) != 1:
                    raise ValueError(f"Expected exactly one {name} wheel.")
                with zipfile.ZipFile(paths[0]) as wheel:
                    files = {
                        p.removeprefix(name + "/"): wheel.read(p)
                        for p in wheel.namelist()
                        if p.startswith(name + "/") and p.endswith(".py")
                    }
                if _digest_files(files) != expected_code[name]:
                    raise ValueError(
                        f"{name} changed after central app archive creation. Restart the export."
                    )
            # Content-derived release identity avoids unnecessary updates for central-only changes.
            payload = {
                "profile": profile,
                "wheels": {p.name: builder._sha256(p) for p in sorted(wheelhouse.glob("*.whl"))},
                "system_packages": builder.edge_system_packages.stage(profile, stage, download=True),
            }
            release_id = (
                "edge-"
                + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]
            )
            candidate = output / (release_id + ".zip")
            exporter.export_runtime_update(
                stage,
                candidate,
                release_id=release_id,
                signing_key=key,
                signing_passphrase=passphrase,
                openssl_executable=openssl,
            )
            with zipfile.ZipFile(candidate) as archive:
                release = json.loads(archive.read("release.json"))
            digest = builder._sha256(candidate)
            size = candidate.stat().st_size
            if size > 8 * 1024**3:
                raise ValueError("Edge runtime update exceeds the supported 8 GiB package limit.")
            candidate.replace(output / f"{digest}.zip")
            targets["packages"].append(
                {"package_sha256": digest, "size_bytes": size, "release": release}
            )
    (output / "targets.json").write_text(
        json.dumps(targets, indent=2, sort_keys=True), encoding="utf-8"
    )
    return targets


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--central-release-id", required=True)
    parser.add_argument("--app-archive", required=True, type=Path)
    arguments = parser.parse_args()
    secret = sys.stdin.buffer.readline().decode("utf-8").removesuffix("\n").removesuffix("\r")
    try:
        result = build_updates(
            json.loads(arguments.config.read_text(encoding="utf-8-sig")),
            arguments.output,
            arguments.central_release_id,
            arguments.app_archive,
            secret,
        )
        print(f"{len(result['packages'])} Edge-Zielpakete im zentralen Update vorbereitet.")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Edge-Export fehlgeschlagen: {exc}\n")
