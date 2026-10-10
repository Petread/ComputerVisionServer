"""Exercise the Windows offline installer's native signature verifier."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
VERIFIER = ROOT / "deployment/edge/WindowsReleaseSignatureVerifier.cs"
OPENSSL = shutil.which("openssl") or (
    str(Path(r"C:\Program Files\Git\usr\bin\openssl.exe"))
    if Path(r"C:\Program Files\Git\usr\bin\openssl.exe").is_file()
    else None
)


@pytest.mark.skipif(
    os.name != "nt" or OPENSSL is None,
    reason="Requires Windows and OpenSSL for test key generation",
)
def test_native_verifier_accepts_signature_and_rejects_tampering(tmp_path: Path) -> None:
    assert OPENSSL is not None
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    checksums = tmp_path / "SHA256SUMS"
    signature = tmp_path / "SHA256SUMS.sig"
    checksums.write_bytes(b"signed checksum manifest\n")
    subprocess.run(
        [
            OPENSSL,
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [OPENSSL, "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            OPENSSL,
            "dgst",
            "-sha256",
            "-sign",
            str(private_key),
            "-out",
            str(signature),
            str(checksums),
        ],
        check=True,
        capture_output=True,
    )
    script = tmp_path / "verify.ps1"
    script.write_text(
        "param([string]$Source, [string]$Key, [string]$Signature, [string]$Checksums)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "Add-Type -Path $Source\n"
        "if ([Ybb.Edge.WindowsReleaseSignatureVerifier]::Verify("
        "$Key, $Signature, $Checksums)) { exit 0 }\n"
        "exit 1\n",
        encoding="utf-8",
    )
    command = [
        str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        str(VERIFIER),
        str(public_key),
        str(signature),
        str(checksums),
    ]
    assert subprocess.run(command, capture_output=True, text=True).returncode == 0
    checksums.write_bytes(b"tampered checksum manifest\n")
    assert subprocess.run(command, capture_output=True, text=True).returncode == 1
