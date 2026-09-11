from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_script(name: str):  # noqa: ANN201
    spec = importlib.util.spec_from_file_location(name, ROOT / "deployment/edge" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_system_scipy_and_pynacl_do_not_block_unrelated_runtime() -> None:
    checker = load_script("check-runtime-dependencies")
    installed = {
        "edge-agent": {"version": "0.8.0", "requires": ["numpy>=2", "uvicorn[standard]"]},
        "numpy": {"version": "2.5.3"},
        "uvicorn": {
            "version": "0.52.4",
            "requires": ['uvloop; extra == "standard" and sys_platform == "linux"'],
        },
        "uvloop": {"version": "0.22.1"},
        "scipy": {"version": "1.11.4", "requires": ["numpy<1.28.0,>=1.21.6"]},
        "pynacl": {"version": "1.5.0", "requires": ["cffi"]},
    }
    environment = {**checker.default_environment(), "sys_platform": "linux"}
    wheels = ["edge_agent-0.8.0-py3-none-any.whl", "numpy-2.5.3-py3-none-any.whl"]
    assert checker.dependency_errors(wheels, installed, environment) == []
    installed["edge-agent"]["requires"].append("scipy")
    assert any(
        "scipy requires numpy" in item
        for item in checker.dependency_errors(wheels, installed, environment)
    )
    installed["edge-agent"]["requires"].append("pynacl")
    assert any("cffi" in item for item in checker.dependency_errors(wheels, installed, environment))


def test_runtime_extras_markers_cycles_and_platform_bindings_are_checked() -> None:
    checker = load_script("check-runtime-dependencies")
    installed = {
        "edge-agent": {"version": "0.8.0", "requires": ["uvicorn[standard]"]},
        "uvicorn": {
            "version": "1.0",
            "requires": ['uvloop; extra == "standard" and sys_platform == "linux"', "edge-agent"],
        },
        "tensorrt": {"version": "10.16", "requires": ["numpy<3"]},
        "numpy": {"version": "2.5.3"},
    }
    wheels = ["edge_agent-0.8.0-py3-none-any.whl"]
    environment = {**checker.default_environment(), "sys_platform": "linux"}
    assert any(
        "uvloop" in item for item in checker.dependency_errors(wheels, installed, environment)
    )
    environment["sys_platform"] = "win32"
    assert checker.dependency_errors(wheels, installed, environment) == []
    installed["numpy"]["version"] = "3.0"
    assert any(
        "tensorrt requires numpy" in item
        for item in checker.dependency_errors(wheels, installed, environment)
    )


def test_manifest_versions_missing_roots_and_python_are_enforced() -> None:
    checker = load_script("check-runtime-dependencies")
    wheels = ["edge_agent-0.8.0-py3-none-any.whl"]
    assert checker.dependency_errors(wheels, {})
    assert checker.dependency_errors([], {})
    assert checker.dependency_errors(wheels, {"edge-agent": {"version": "0.7.0"}})
    assert checker.dependency_errors(
        wheels, {"edge-agent": {"version": "0.8.0", "requires_python": "<3"}}
    )


def test_existing_launcher_patch_preserves_imports_and_has_backup(tmp_path: Path) -> None:
    repair = load_script("repair-runtime-dependency-check")
    launcher = tmp_path / "edge-runtime-launcher.py"
    original = (
        "from cv_edge.software import verify_software_package\n"
        "def check(self, digest):\n"
        '    subprocess.run([self.python(digest), "-m", "pip", "check"], check=True, timeout=60)\n'
    )
    launcher.write_text(original)
    helper = ROOT / "deployment/edge/check-runtime-dependencies.py"
    repair.repair(tmp_path, helper)
    patched = launcher.read_text()
    assert patched.startswith("from cv_edge.software import verify_software_package\n")
    assert "runtime_platform_version_matches" not in patched
    assert "check-runtime-dependencies.py" in patched
    assert '"--manifest"' in patched
    assert (tmp_path / "edge-runtime-launcher.before-dependency-check.py").read_text() == original
    assert (tmp_path / helper.name).read_bytes() == helper.read_bytes()
    repair.repair(tmp_path, helper)
    assert launcher.read_text() == patched


def test_launcher_patch_rejects_unknown_source_without_changes(tmp_path: Path) -> None:
    repair = load_script("repair-runtime-dependency-check")
    launcher = tmp_path / "edge-runtime-launcher.py"
    launcher.write_text("# unknown launcher\n")
    with pytest.raises(ValueError, match="Unrecognized launcher"):
        repair.repair(tmp_path, ROOT / "deployment/edge/check-runtime-dependencies.py")
    assert launcher.read_text() == "# unknown launcher\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [launcher.name]
