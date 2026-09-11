"""Export a signed runtime update from an existing Edge bundle directory or archive."""

from __future__ import annotations

import argparse
import ast
import getpass
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def _resolve_openssl(executable: str) -> str:
    """Resolve the signer before unpacking or rebuilding a potentially large bundle."""
    requested = executable.strip() or "openssl"
    found = shutil.which(requested)
    if found:
        return str(Path(found).resolve())
    # Only the default name may use automatic discovery; explicit paths must be respected.
    if sys.platform == "win32" and requested.lower() in {"openssl", "openssl.exe"}:
        roots = [
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git",
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Git",
        ]
        if os.environ.get("LOCALAPPDATA"):
            roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git")
        git = shutil.which("git")
        if git:
            roots.insert(0, Path(git).resolve().parent.parent)
        for root in roots:
            candidate = root / "usr" / "bin" / "openssl.exe"
            if candidate.is_file():
                return str(candidate.resolve())
    raise ValueError(
        f"OpenSSL wurde nicht gefunden: {requested!r}. "
        "Im Edge Builder unter Signatur > OpenSSL den vollständigen Pfad zur openssl.exe "
        "eintragen, z. B. C:\\Program Files\\Git\\usr\\bin\\openssl.exe "
        "(Kommandozeile: --openssl). Es wurden noch keine Runtime-Pakete neu gebaut."
    )


def _extract_runtime_inputs(bundle: Path, destination: Path) -> None:
    """Read only the profile and wheels; never unpack OS installers or archive links."""
    seen: set[str] = set()

    def selected(name: str) -> Path | None:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError(f"Unsafe bundle archive path: {name!r}")
        if str(path) != "platform-profile.json" and not (
            len(path.parts) == 2 and path.parts[0] == "wheelhouse" and path.suffix == ".whl"
        ):
            return None
        normalized = str(path).casefold()
        if normalized in seen:
            raise ValueError(f"Duplicate runtime input in bundle: {name!r}")
        seen.add(normalized)
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    if zipfile.is_zipfile(bundle):
        with zipfile.ZipFile(bundle) as archive:
            for entry in archive.infolist():
                # ZipInfo normalizes backslashes on Windows; validate the original name.
                target = selected(entry.orig_filename)
                if target is None:
                    continue
                if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError(f"Runtime input must be a regular file: {entry.filename}")
                with archive.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    elif bundle.name.lower().endswith((".tar.gz", ".tgz")):
        with tarfile.open(bundle, "r|gz") as archive:
            for entry in archive:
                target = selected(entry.name)
                if target is None:
                    continue
                if not entry.isfile():
                    raise ValueError(f"Runtime input must be a regular file: {entry.name}")
                source = archive.extractfile(entry)
                if source is None:
                    raise ValueError(f"Unreadable runtime input: {entry.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    else:
        raise ValueError("Select an Edge bundle directory, .zip or .tar.gz archive.")
    if not (destination / "platform-profile.json").is_file() or not list(
        (destination / "wheelhouse").glob("*.whl")
    ):
        raise ValueError("The selected build lacks platform-profile.json or wheelhouse/*.whl.")


def _passphrase_input(passphrase: str) -> bytes:
    if any(char in passphrase for char in "\r\n\0"):
        raise ValueError("Die Schlüssel-Passphrase darf keine Zeilenumbrüche oder NUL enthalten.")
    return (passphrase + "\n").encode("utf-8")


def _validate_signing_key(openssl: str, key: Path, passphrase: str) -> None:
    # Never let a background GUI process attempt an interactive OpenSSL prompt.
    result = subprocess.run(
        [openssl, "rsa", "-in", str(key), "-passin", "stdin", "-check", "-noout"],
        input=_passphrase_input(passphrase),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        # Classify known errors without copying paths, key data or arbitrary stderr
        # into the GUI log. A decryption failure alone does not prove a typo.
        errors = result.stderr.lower()
        if b"bad decrypt" in errors or b"cipherfinal error" in errors:
            if passphrase:
                reason = (
                    "OpenSSL kann den Schlüssel mit der übergebenen Passphrase nicht "
                    "entschlüsseln (bad decrypt). Die richtige Schlüssel-Passphrase "
                    "für genau diese Datei verwenden. Falls die direkte OpenSSL-Abfrage "
                    "funktioniert, muss die Zeichenkodierung der Passphrase geprüft werden. "
                )
            else:
                reason = (
                    "OpenSSL kann den Schlüssel ohne Passphrase nicht entschlüsseln. "
                    "Es wurde eine leere Passphrase übergeben. Im Runtime-Update-Dialog "
                    "die richtige Schlüssel-Passphrase eingeben "
                    "(Kommandozeile: --ask-signing-passphrase). "
                )
        elif b"not an rsa key" in errors or b"expecting an rsa key" in errors:
            reason = "Die ausgewählte Datei enthält keinen privaten RSA-Schlüssel. "
        elif b"permission denied" in errors or b"access is denied" in errors:
            reason = "OpenSSL hat keine Leseberechtigung für die Schlüsseldatei. "
        elif b"unsupported" in errors:
            reason = (
                "OpenSSL meldet ein nicht unterstütztes Schlüsselformat oder "
                "Verschlüsselungsverfahren (unsupported). Die Datei und die "
                "OpenSSL-Konfiguration mit einer direkten Schlüsselprüfung prüfen. "
            )
        else:
            reason = (
                "Der private RSA-Schlüssel konnte nicht geöffnet oder geprüft werden "
                f"(OpenSSL-Exit {result.returncode}). Die Datei mit OpenSSL direkt "
                "prüfen: pkey -in <Schlüsseldatei> -check -noout. "
            )
        raise ValueError(
            reason + "Es wurden noch keine Runtime-Pakete neu gebaut."
        )


def export_runtime_update(
    bundle: Path,
    destination: Path,
    *,
    release_id: str,
    signing_key: Path,
    refresh_runtime: bool = False,
    openssl_executable: str = "openssl",
    package_index_url: str = "https://pypi.org/simple",
    additional_wheel_directory: Path | None = None,
    signing_passphrase: str = "",
) -> None:
    bundle = bundle.resolve()
    destination = destination.resolve()
    signing_key = signing_key.resolve()
    if not release_id.strip():
        raise ValueError("A release ID is required.")
    if not signing_key.is_file():
        raise ValueError("The private release signing key does not exist.")
    if (
        destination == signing_key
        or destination == bundle
        or (bundle.is_dir() and destination.is_relative_to(bundle))
    ):
        raise ValueError("The update output must be outside the input build and signing key.")
    openssl_executable = _resolve_openssl(openssl_executable)
    _validate_signing_key(openssl_executable, signing_key, signing_passphrase)
    if bundle.is_file():
        print(f"Reading runtime inputs from {bundle.name} ...", flush=True)
        with tempfile.TemporaryDirectory(prefix="ybb-edge-update-input-") as temporary:
            staging = Path(temporary)
            _extract_runtime_inputs(bundle, staging)
            export_runtime_update(
                staging,
                destination,
                release_id=release_id,
                signing_key=signing_key,
                refresh_runtime=refresh_runtime,
                openssl_executable=openssl_executable,
                package_index_url=package_index_url,
                additional_wheel_directory=additional_wheel_directory,
                signing_passphrase=signing_passphrase,
            )
        return
    profile_path = bundle / "platform-profile.json"
    if not profile_path.is_file() or not list((bundle / "wheelhouse").glob("*.whl")):
        raise ValueError("The selected build lacks platform-profile.json or wheelhouse/*.whl.")
    profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    if profile.get("platform") not in {"jetson_orin_nano", "jetson_agx_orin", "windows_11"}:
        raise ValueError("The selected build has an unsupported Edge platform.")
    if not re.fullmatch(r"3\d{2}", str(profile.get("python_tag", ""))):
        raise ValueError("The selected build has an invalid Python target tag.")
    if refresh_runtime:
        # Reuse the completed bundle, including its exact target profile and native wheels.
        # Never modify the input bundle (it may still be used by the Jetson installer).
        try:
            from .build_edge_offline_bundle import (
                _ensure_basler_wheel,
                _refresh_internal_wheels,
                _validate_wheelhouse,
            )
        except ImportError:  # Direct script execution.
            from build_edge_offline_bundle import (
                _ensure_basler_wheel,
                _refresh_internal_wheels,
                _validate_wheelhouse,
            )
        with tempfile.TemporaryDirectory(prefix="ybb-edge-runtime-update-") as temporary:
            staging = Path(temporary)
            shutil.copy2(bundle / "platform-profile.json", staging / "platform-profile.json")
            shutil.copytree(bundle / "wheelhouse", staging / "wheelhouse")
            config = {
                "target_platform": profile["platform"],
                "package_index_url": package_index_url,
                "additional_wheel_directory": str(additional_wheel_directory or ""),
            }
            print(f"Refreshing Python runtime for {profile['platform']} ...", flush=True)
            _ensure_basler_wheel(config, profile, staging / "wheelhouse")
            _refresh_internal_wheels(config, staging / "wheelhouse")
            _validate_wheelhouse(staging / "wheelhouse", profile, config)
            export_runtime_update(
                staging,
                destination,
                release_id=release_id,
                signing_key=signing_key,
                openssl_executable=openssl_executable,
                signing_passphrase=signing_passphrase,
            )
        return
    wheels = sorted((bundle / "wheelhouse").glob("*.whl"))
    runtime_wheels = [p for p in wheels if p.name.startswith("cv_runtime-")]
    if len(runtime_wheels) != 1:
        raise ValueError("Expected exactly one cv-runtime wheel in the prepared bundle.")
    if not any(p.name.startswith("cv_edge-") for p in wheels):
        raise ValueError("Rebuild the offline bundle with cv-edge and the software launcher.")
    digest = hashlib.sha256()
    with zipfile.ZipFile(runtime_wheels[0]) as wheel:
        for name in sorted(
            n for n in wheel.namelist() if n.startswith("cv_runtime/") and n.endswith(".py")
        ):
            digest.update(name.removeprefix("cv_runtime/").encode())
            digest.update(b"\0")
            digest.update(wheel.read(name).decode("utf-8-sig").replace("\r\n", "\n").encode())
            digest.update(b"\0")
        # Read literal registry metadata without importing target-native dependencies.
        tree = ast.parse(wheel.read("cv_runtime/registry.py"))
        distribution = ast.parse(wheel.read("cv_runtime/distribution.py"))
        unavailable = next(
            set(ast.literal_eval(item.value.args[0]))
            for item in distribution.body
            if isinstance(item, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "EDGE_UNAVAILABLE_NODES"
                for target in item.targets
            )
        )
        factory = next(
            item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_node"
        )
        version = next(
            ast.literal_eval(keyword.value)
            for item in ast.walk(factory)
            if isinstance(item, ast.Call)
            for keyword in item.keywords
            if keyword.arg == "contract_version"
        )
        contracts = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_node"
            ):
                arguments = {item.arg: item.value for item in node.keywords}
                name = ast.literal_eval(arguments["node_type"])
                category = arguments["category"]
                constraints = arguments.get("constraints")
                excluded = isinstance(category, ast.Attribute) and category.attr == "CAPTURE"
                if isinstance(constraints, ast.Dict):
                    excluded |= any(
                        isinstance(key, ast.Constant)
                        and key.value == "edge_supported"
                        and isinstance(value, ast.Constant)
                        and value.value is False
                        for key, value in zip(constraints.keys, constraints.values, strict=True)
                    )
                if name not in unavailable and not excluded:
                    contracts[name] = version
    manifest = {
        "schema_version": "edge.software.release.v1",
        "release_id": release_id,
        "platform": profile["platform"],
        "architecture": profile["architecture"],
        "python_version": f"{profile['python_tag'][0]}.{profile['python_tag'][1:]}",
        "cuda_version": profile.get("cuda_version"),
        "tensorrt_version": profile.get("tensorrt_version"),
        "runtime_code_digest": digest.hexdigest(),
        "node_contract_versions": contracts,
        "wheels": {},
    }
    for path in wheels:
        with path.open("rb") as stream:
            manifest["wheels"][path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        (root / "release.json").write_bytes(raw)
        subprocess.run(
            [
                openssl_executable,
                "dgst",
                "-sha256",
                "-sign",
                str(signing_key),
                "-passin",
                "stdin",
                "-out",
                str(root / "release.sig"),
                str(root / "release.json"),
            ],
            check=True,
            input=_passphrase_input(signing_passphrase),
        )
        _publish_update_archive(destination, raw, root / "release.sig", wheels)
    print(f"Runtime update created: {destination}", flush=True)


def _publish_update_archive(
    destination: Path, raw: bytes, signature: Path, wheels: list[Path]
) -> None:
    # Keep any previous output intact if writing the new package fails.
    # Create the archive directly in its destination directory so it inherits
    # that directory's Windows ACL. Moving it out of TemporaryDirectory would
    # retain the restrictive ACL applied by mkdir(mode=0o700).
    with tempfile.NamedTemporaryFile(
        prefix=".edge-update-", suffix=".zip", dir=destination.parent, delete=False
    ) as output_file:
        candidate = Path(output_file.name)
    try:
        _write_update_archive(candidate, raw, signature, wheels)
        os.replace(candidate, destination)
    finally:
        candidate.unlink(missing_ok=True)


def _write_update_archive(
    destination: Path, raw: bytes, signature: Path, wheels: list[Path]
) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(zipfile.ZipInfo("release.json"), raw)
        archive.writestr(zipfile.ZipInfo("release.sig"), signature.read_bytes())
        for path in wheels:
            with (
                path.open("rb") as source,
                archive.open(
                    zipfile.ZipInfo(f"wheelhouse/{path.name}"), "w", force_zip64=True
                ) as target,
            ):
                shutil.copyfileobj(source, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--signing-key", required=True, type=Path)
    parser.add_argument("--openssl", default="openssl")
    parser.add_argument("--package-index-url", default="https://pypi.org/simple")
    parser.add_argument("--additional-wheel-directory", type=Path)
    passphrase_options = parser.add_mutually_exclusive_group()
    passphrase_options.add_argument(
        "--signing-passphrase-stdin",
        action="store_true",
        help="Read the signing passphrase from a UTF-8 stdin line (used by the GUI).",
    )
    passphrase_options.add_argument(
        "--ask-signing-passphrase",
        action="store_true",
        help="Prompt for the signing passphrase without displaying it in an interactive terminal.",
    )
    parser.add_argument(
        "--refresh-runtime",
        action="store_true",
        help="Build current Python packages and add Basler SDK; reuse the completed bundle's base.",
    )
    args = parser.parse_args()
    passphrase = ""
    if args.signing_passphrase_stdin:
        passphrase = (
            sys.stdin.buffer.readline().decode("utf-8").removesuffix("\n").removesuffix("\r")
        )
    elif args.ask_signing_passphrase:
        passphrase = getpass.getpass("Schlüssel-Passphrase: ")
    try:
        export_runtime_update(
            args.bundle,
            args.output,
            release_id=args.release_id,
            signing_key=args.signing_key,
            refresh_runtime=args.refresh_runtime,
            openssl_executable=args.openssl,
            package_index_url=args.package_index_url,
            additional_wheel_directory=args.additional_wheel_directory,
            signing_passphrase=passphrase,
        )
    except ValueError as error:
        parser.exit(1, f"Runtime-Update fehlgeschlagen: {error}\n")
