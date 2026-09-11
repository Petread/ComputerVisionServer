"""Signed, offline runtime packages shared by the release builder and orchestration API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from cv_contracts.edge import EdgeSoftwareRelease


def runtime_platform_version_matches(required: str | None, installed: str | None) -> bool:
    """Match the numeric precision declared by the signed platform profile.

    Profiles such as CUDA 13.2 or TensorRT 10.16 specify a release series.
    Explicit patch/build components remain constraints, not minimum versions.
    """
    if not required:
        return True
    version_pattern = r"[0-9]+\.[0-9]+(?:\.[0-9]+)*"
    if (
        not installed
        or not re.fullmatch(version_pattern, required)
        or not re.fullmatch(version_pattern, installed)
    ):
        return False
    required_parts = tuple(int(part) for part in required.split("."))
    installed_parts = tuple(int(part) for part in installed.split("."))
    return installed_parts[: len(required_parts)] == required_parts


def verify_software_package(path: Path, trusted_key: Path) -> EdgeSoftwareRelease:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 2000:
            raise ValueError("Duplicate files or excessive file count in runtime package.")
        if sum(i.file_size for i in archive.infolist()) > 16 * 1024**3:
            raise ValueError("Runtime package exceeds the 16 GiB extracted size limit.")
        if archive.getinfo("release.json").file_size > 1024**2:
            raise ValueError("Runtime manifest is too large.")
        if archive.getinfo("release.sig").file_size > 16384:
            raise ValueError("Runtime signature is too large.")
        raw = archive.read("release.json")
        manifest = EdgeSoftwareRelease.model_validate_json(raw)
        expected = {"release.json", "release.sig"} | {f"wheelhouse/{n}" for n in manifest.wheels}
        if set(names) != expected:
            raise ValueError("Runtime package contains undeclared files.")
        try:
            with trusted_key.open("rb") as key_file:
                key_file.read(1)
        except FileNotFoundError as exc:
            raise ValueError(
                f"Trusted Edge release public key is missing: {trusted_key}. "
                "Install the public key matching the package signing key at this path, "
                "or configure YBB_EDGE_TRUSTED_RELEASE_KEY. Do not copy the private key."
            ) from exc
        except PermissionError as exc:
            raise ValueError(
                f"Trusted Edge release public key is not readable: {trusted_key}. "
                "Grant the runtime service read access to this public key."
            ) from exc
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "release.json").write_bytes(raw)
            (root / "release.sig").write_bytes(archive.read("release.sig"))
            result = subprocess.run(
                [
                    os.getenv("YBB_EDGE_OPENSSL_EXECUTABLE", "openssl"),
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(trusted_key),
                    "-signature",
                    str(root / "release.sig"),
                    str(root / "release.json"),
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if result.returncode:
                raise ValueError(
                    f"Runtime package signature could not be verified with {trusted_key}. "
                    "Check that this is a valid public release key matching the signing "
                    "key used by the builder, and that the package is unchanged. "
                    f"OpenSSL exit status: {result.returncode}."
                )
        for name, expected_hash in manifest.wheels.items():
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", name):
                raise ValueError("Runtime wheel filename is invalid.")
            with archive.open(f"wheelhouse/{name}") as wheel:
                digest = hashlib.file_digest(wheel, "sha256").hexdigest()
            if digest != expected_hash:
                raise ValueError(f"Wheel checksum mismatch: {name}")
        return manifest


def canonical_manifest(manifest: EdgeSoftwareRelease) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
