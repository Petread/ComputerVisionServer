from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("status", "installed", "bundle", "expected_install"),
    [
        ("install ok installed", "3", "2", False),
        ("install ok installed", "2", "2", False),
        ("hold ok installed", "3", "2", False),
        ("install ok installed", "1", "2", True),
        ("", "", "2", True),
        ("install ok unpacked", "2", "2", True),
    ],
)
def test_offline_bundle_preserves_configured_equal_or_newer_packages(
    tmp_path: Path, status: str, installed: str, bundle: str, expected_install: bool,
) -> None:
    _run_bundle(tmp_path, status, installed, bundle, expected_install)


def test_offline_bundle_keeps_architecture_specific_package_identity(tmp_path: Path) -> None:
    _run_bundle(tmp_path, "install ok installed", "3", "2", False, architecture="arm64")


def test_offline_bundle_handles_architecture_independent_packages(tmp_path: Path) -> None:
    _run_bundle(tmp_path, "install ok installed", "3", "2", False, architecture="all")


def test_offline_dependency_failure_prevents_success(tmp_path: Path) -> None:
    _run_bundle(tmp_path, "", "", "2", True, apt_exit=100)


def _run_bundle(
    tmp_path: Path, status: str, installed: str, bundle: str, expected_install: bool,
    *, architecture: str = "amd64", apt_exit: int = 0,
) -> None:
    bash = (
        str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
        if os.name == "nt" else shutil.which("bash")
    )
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash is required for offline installer behavior tests")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with tarfile.open(runtime / "gstreamer-debs.tar.gz", "w:gz") as archive:
        entry = tarfile.TarInfo("debs/library.deb")
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"x"))
    source = (ROOT / "deployment/wsl/apply-release.sh").read_text(encoding="utf-8")
    function = source.split("install_gstreamer_deb_bundle_from_bundle() {", 1)[1].split(
        "\ninstall_python_wheel_bundle_from_bundle()", 1,
    )[0]
    script = r'''set -euo pipefail
BUNDLE_DIR="$PWD"
log() { printf '%s\n' "$*"; }
turnserver() { :; }
dpkg-deb() {
  case "$3" in
    Package) echo library ;;
    Architecture) echo "$TEST_ARCH" ;;
    Version) echo "$TEST_BUNDLE" ;;
    *) return 2 ;;
  esac
}
dpkg-query() {
  printf '%s' "${@: -1}" > queried-package
  if [[ -z "$TEST_STATUS" ]]; then return 1; fi
  printf '%s\t%s' "$TEST_STATUS" "$TEST_INSTALLED"
}
dpkg() {
  [[ "$1" == --compare-versions && "$3" == ge ]]
  (( $2 >= $4 ))
}
apt-get() {
  printf '%s\n' "$@" > apt-arguments
  return "$TEST_APT_EXIT"
}
'''
    script += "\ninstall_gstreamer_deb_bundle_from_bundle() {" + function
    script += "\ninstall_gstreamer_deb_bundle_from_bundle\n"
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", script], cwd=tmp_path,
        env={**os.environ, "TEST_STATUS": status, "TEST_INSTALLED": installed,
             "TEST_BUNDLE": bundle, "TEST_ARCH": architecture,
             "TEST_APT_EXIT": str(apt_exit)},
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert (result.returncode == 0) == (apt_exit == 0), result.stdout + result.stderr
    expected_package = "library" if architecture == "all" else f"library:{architecture}"
    assert (tmp_path / "queried-package").read_text() == expected_package
    apt_arguments = tmp_path / "apt-arguments"
    assert apt_arguments.exists() == expected_install
    if expected_install:
        arguments = apt_arguments.read_text().splitlines()
        assert "--no-remove" in arguments
        assert "--allow-downgrades" not in arguments
        assert sum(arg.endswith("/library.deb") for arg in arguments) == 1
        assert "Dir::Etc::sourcelist=/dev/null" in arguments
