from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("environment_present", [True, False])
def test_import_uses_service_environment_and_candidate_code(
    tmp_path: Path,
    environment_present: bool,
) -> None:
    bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash is required for installer verification")
    source = (ROOT / "deployment/wsl/apply-release.sh").read_text(encoding="utf-8")
    function = (
        "import_edge_runtime_packages() {"
        + source.split("import_edge_runtime_packages() {", 1)[1].split(
            "\nvalidate_service_runtime_imports()", 1
        )[0]
    )
    (tmp_path / "edge-runtime").mkdir()
    (tmp_path / "edge-runtime/targets.json").write_text("{}")
    if environment_present:
        (tmp_path / "runtime.env").write_text(
            "ENV=prod\nCV_PLATFORM__STORAGE__STATE_ROOT=/srv/cv-platform\n"
            "PYTHONPATH=old-release\nYBB_EDGE_TRUSTED_RELEASE_KEY=/pinned-public.pem\n"
        )
    script = r"""set -euo pipefail
export ENV=dev
export CV_PLATFORM__STORAGE__STATE_ROOT=.local/state
export PYTHONPATH=caller
ENV_FILE="$PWD/runtime.env"
BUNDLE_DIR="$PWD"
RELEASE_DIR="$PWD/candidate"
APP_VERSION=central-test
PYTHON_BIN=fake_python
log() { :; }
read_bundle_manifest_value() { printf '%s' 'edge-runtime/targets.json'; }
build_pythonpath() { [[ "$1" == "$RELEASE_DIR" ]]; printf '%s' 'candidate-code'; }
fake_python() {
  [[ "$ENV" == prod ]]
  [[ "$CV_PLATFORM__STORAGE__STATE_ROOT" == /srv/cv-platform ]]
  [[ "$YBB_EDGE_TRUSTED_RELEASE_KEY" == /pinned-public.pem ]]
  [[ "$(declare -p YBB_EDGE_TRUSTED_RELEASE_KEY)" == 'declare -x '* ]]
  [[ "$PYTHONPATH" == candidate-code ]]
  [[ "$1" == -m && "$2" == runtime_admin_api.edge_software ]]
  [[ "$3" == --bundle-directory && "$4" == "$BUNDLE_DIR/edge-runtime" ]]
  [[ "$5" == --release-root && "$6" == "$RELEASE_DIR" ]]
  [[ "$7" == --central-release-id && "$8" == central-test ]]
  printf '%s' 'imported' > invoked
}
"""
    script += (
        function
        + '\nimport_edge_runtime_packages\n[[ "$ENV" == dev && "$PYTHONPATH" == caller ]]\n'
    )
    path = tmp_path / "test-import.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    result = subprocess.run(
        [bash, path.as_posix()], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert (result.returncode == 0) is environment_present, result.stderr
    assert (tmp_path / "invoked").exists() is environment_present


def test_cli_rejects_development_storage_before_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import cv_core
    from runtime_admin_api import edge_software

    monkeypatch.setattr(
        cv_core, "load_platform_settings", lambda *args, **kwargs: SimpleNamespace(env="dev")
    )
    monkeypatch.setattr(
        edge_software,
        "import_bundle_targets",
        lambda *args, **kwargs: pytest.fail("Import must not run"),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "edge_software",
            "--bundle-directory",
            str(tmp_path),
            "--release-root",
            str(tmp_path),
            "--central-release-id",
            "central-test",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        edge_software.main()
    assert exc.value.code == 2
