"""Install the scoped dependency check into an existing Edge launcher, without rebuilding it."""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        candidate = Path(stream.name)
        stream.write(content)
    try:
        candidate.chmod(mode)
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def repair(install_root: Path, checker_source: Path) -> None:
    launcher = install_root.resolve() / "edge-runtime-launcher.py"
    original = launcher.read_bytes()
    source = original.decode("utf-8")
    old = '[self.python(digest), "-m", "pip", "check"]'
    replacement = (
        '[self.python(digest), str(Path(__file__).with_name("check-runtime-dependencies.py")), '
        '"--manifest", str(self.release_root(digest) / "release.json")]'
    )
    checker = checker_source.read_bytes()
    compile(checker, str(checker_source), "exec")
    if old not in source:
        if replacement not in source:
            raise ValueError(
                "Unrecognized launcher; no files changed. Use the matching release installer."
            )
        atomic_write(launcher.with_name("check-runtime-dependencies.py"), checker, 0o644)
        print("Launcher already patched.")
        return
    if source.count(old) != 1:
        raise ValueError("Ambiguous launcher dependency check; no files changed.")
    updated = source.replace(old, replacement)
    compile(updated, str(launcher), "exec")
    backup = launcher.with_name("edge-runtime-launcher.before-dependency-check.py")
    if backup.exists():
        if backup.read_bytes() != original:
            raise ValueError(f"Different backup already exists: {backup}. No files changed.")
    else:
        with backup.open("xb") as output:
            output.write(original)
        backup.chmod(stat.S_IMODE(launcher.stat().st_mode))
    atomic_write(launcher.with_name("check-runtime-dependencies.py"), checker, 0o644)
    atomic_write(launcher, updated.encode("utf-8"), stat.S_IMODE(launcher.stat().st_mode))
    print(f"Updated only the dependency check in {launcher}.")
    print(f"Original launcher retained: {backup}")
    print("Restart ybb-edge-agent.service, then retry the existing runtime update in the UI.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", type=Path, default=Path("/opt/ybb-edge"))
    args = parser.parse_args()
    repair(args.install_root, Path(__file__).with_name("check-runtime-dependencies.py"))
