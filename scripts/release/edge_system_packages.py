"""Pinned Ubuntu prerequisites, downloaded only on the development/builder machine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
LOCK_FILE = ROOT / "deployment/edge/ubuntu-noble-arm64-ufw.lock.json"
CACHE_ROOT = ROOT / "dist/edge-inputs/ubuntu-noble-arm64-ufw"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(profile: dict, *, download: bool = True) -> Path | None:
    if profile["platform"] == "windows_11":
        return None
    if profile["architecture"] != "aarch64" or profile.get("ubuntu_codename", "noble") != "noble":
        raise ValueError("Edge-Systempakete sind für Ubuntu 24.04 Noble / ARM64 vorgesehen.")
    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    for package in lock["packages"]:
        url = urlsplit(package["url"])
        filename = Path(url.path).name
        if (url.scheme, url.netloc) != ("https", "ports.ubuntu.com") or not re.fullmatch(
            r"[A-Za-z0-9_.+~%-]+\.deb", filename
        ):
            raise ValueError("Invalid pinned Ubuntu package URL.")
        target = CACHE_ROOT / filename
        if target.is_file() and sha256(target) == package["sha256"]:
            continue
        if not download:
            raise ValueError(
                f"Ubuntu-Offlinepaket fehlt oder ist beschädigt: {filename}. "
                "Am Entwicklungsrechner zuerst 'Online-Inhalte vorbereiten (Internet)' ausführen."
            )
        print(f"Ubuntu-Offlinepaket laden: {filename}", flush=True)
        with tempfile.NamedTemporaryFile(dir=CACHE_ROOT, suffix=".part", delete=False) as output:
            temporary = Path(output.name)
            try:
                with urlopen(package["url"], timeout=90) as source:
                    shutil.copyfileobj(source, output)
            except BaseException:
                output.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            if (
                temporary.stat().st_size != package["size_bytes"]
                or sha256(temporary) != package["sha256"]
            ):
                raise ValueError(f"Ubuntu-Paket unvollständig oder beschädigt: {filename}")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return CACHE_ROOT


def stage(profile: dict, destination: Path, *, download: bool = False) -> dict[str, str]:
    cache = prepare(profile, download=download)
    if cache is None:
        return {}
    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    target = destination / "system-packages"
    target.mkdir(parents=True, exist_ok=True)
    records = []
    files = {}
    for package in lock["packages"]:
        name = Path(urlsplit(package["url"]).path).name
        shutil.copyfile(cache / name, target / name)
        files[name] = package["sha256"]
        fields = {
            "Package": package["name"],
            "Version": package["version"],
            "Architecture": package["architecture"],
            **package["control"],
            "Filename": "./" + name,
            "Size": package["size_bytes"],
            "SHA256": package["sha256"],
        }
        records.append("".join(f"{key}: {value}\n" for key, value in fields.items()))
    (target / "Packages").write_text("\n".join(records) + "\n", encoding="utf-8", newline="\n")
    files["Packages"] = sha256(target / "Packages")
    manifest = {
        "schema_version": "ybb.edge.system-packages.v1",
        "ubuntu_codename": lock["ubuntu_codename"],
        "architecture": lock["architecture"],
        "install": lock["install"],
        "files": files,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return {**files, "manifest.json": sha256(target / "manifest.json")}
