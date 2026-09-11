"""Check a signed runtime's dependency closure in its candidate Python environment.

System site packages remain accessible for NVIDIA/GStreamer bindings. Unrelated
OS applications are not part of the runtime release's dependency contract.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

# pip is required by the launcher to create/install every candidate environment.
# Use its bundled parser if packaging is not separately installed on the Edge.
try:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.utils import canonicalize_name, parse_wheel_filename
    from packaging.version import Version
except ImportError:
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
    from pip._vendor.packaging.version import Version


def dependency_errors(
    wheel_names: list[str],
    installed: dict[str, dict],
    environment: dict[str, str] | None = None,
) -> list[str]:
    environment = environment or default_environment()
    errors: set[str] = set()
    pending: list[tuple[str, str]] = []
    for wheel in wheel_names:
        name, version, _, _ = parse_wheel_filename(wheel)
        pending.append((name, ""))
        package = installed.get(name)
        if package is not None and Version(package["version"]) != version:
            errors.add(f"{name}: signed wheel requires {version}, installed {package['version']}.")
    if not pending:
        return ["Runtime manifest contains no wheels."]
    # These bindings are provided by the platform instead of the wheelhouse.
    # Check them when installed, including their transitive requirements.
    pending.extend(
        (name, "")
        for name in installed
        if name == "pygobject" or name == "tensorrt" or name.startswith("tensorrt-")
    )
    visited: set[tuple[str, str]] = set()
    while pending:
        name, extra = pending.pop()
        if (name, extra) in visited:
            continue
        visited.add((name, extra))
        package = installed.get(name)
        if package is None:
            errors.add(f"{name} is required by the runtime but is not installed.")
            continue
        python_requirement = package.get("requires_python")
        if python_requirement and not SpecifierSet(python_requirement).contains(
            environment["python_full_version"], prereleases=True
        ):
            errors.add(f"{name} requires Python {python_requirement}.")
        for raw in package.get("requires", []):
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate(
                {**environment, "extra": extra}
            ):
                continue
            dependency = canonicalize_name(requirement.name)
            found = installed.get(dependency)
            if found is None:
                errors.add(f"{name} requires {requirement}, which is not installed.")
                continue
            if requirement.url:
                errors.add(f"{name}: cannot verify direct URL requirement offline: {dependency}.")
            elif not requirement.specifier.contains(found["version"], prereleases=True):
                errors.add(
                    f"{name} requires {requirement}, "
                    f"but {dependency} {found['version']} is installed."
                )
            pending.append((dependency, ""))
            pending.extend((dependency, requested) for requested in requirement.extras)
    return sorted(errors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    installed = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            # Preserve sys.path precedence: candidate packages shadow OS packages.
            installed.setdefault(
                canonicalize_name(name),
                {
                    "version": distribution.version,
                    "requires": distribution.requires or [],
                    "requires_python": distribution.metadata.get("Requires-Python"),
                },
            )
    errors = dependency_errors(list(manifest["wheels"]), installed)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Runtime dependencies OK (Python {platform.python_version()}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
