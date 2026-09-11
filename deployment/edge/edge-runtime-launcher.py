"""Stable service entrypoint. Keep outside versioned environments during software updates."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from cv_edge.software import runtime_platform_version_matches, verify_software_package


class RuntimeLauncher:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.root = self.data_root / "software"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "requests").mkdir(exist_ok=True)
        (self.root / "completed").mkdir(exist_ok=True)
        self.child = None
        self.stopping = False
        self.state = self.read_state()
        self.state["protocol_version"] = 1

    def read_state(self) -> dict:
        path = self.root / "status.json"
        return (
            json.loads(path.read_text())
            if path.exists()
            else {
                "phase": "idle",
                "active_release": "bootstrap",
                "previous_release": None,
            }
        )

    def save(self, **values: object) -> None:
        self.state.update(values, updated_at=datetime.now(UTC).isoformat())
        temporary = self.root / "status.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.root / "status.json")

    def release_root(self, release: str) -> Path:
        # Release slots are content addressed; bootstrap is the immutable initial environment.
        if len(release) != 64 or any(c not in "0123456789abcdef" for c in release):
            raise ValueError("Invalid software slot digest.")
        return self.root / "releases" / release

    def python(self, release: str) -> str:
        if release == "bootstrap":
            return sys.executable
        folder = self.release_root(release) / "venv"
        return str(folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))

    def start(self) -> None:
        (self.root / "stop").unlink(missing_ok=True)
        environment = dict(os.environ, YBB_EDGE_SOFTWARE_LAUNCHER="1")
        self.child = subprocess.Popen(
            [self.python(self.state["active_release"]), "-m", "edge_agent.server"],
            env=environment,
            cwd=self.data_root,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def stop(self) -> None:
        if self.child is None:
            return
        (self.root / "stop").touch()
        try:
            self.child.wait(timeout=40)
        except subprocess.TimeoutExpired:
            self.child.terminate()
            self.child.wait(timeout=20)
        self.child = None

    def healthy(self, digest: str | None = None, timeout: int = 90) -> None:
        base = "http://{}:{}".format(
            os.environ["YBB_EDGE_MANAGEMENT_IP"], os.getenv("YBB_EDGE_CONTROL_PORT", "9443")
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        healthy_observations = 0
        while time.monotonic() < deadline and not self.stopping:
            if self.child is None or self.child.poll() is not None:
                raise RuntimeError("Candidate runtime process exited before health confirmation.")
            try:
                with opener.open(base + "/edge/v1/capabilities", timeout=3) as response:
                    capabilities = json.load(response)
                with opener.open(base + "/edge/v1/health", timeout=3) as response:
                    health = json.load(response)
                with opener.open(base + "/edge/v1/runtime", timeout=3) as response:
                    runtime = json.load(response)
                active = set(health.get("active_deployment_ids", []))
                running = {
                    process["deployment_id"]
                    for process in runtime.get("processes", [])
                    if process["state"] == "running" and process.get("last_heartbeat_at")
                }
                if (
                    (not digest or capabilities.get("runtime_code_digest") == digest)
                    and health["state"] == "ok"
                    and runtime.get("supervisor_state") != "degraded"
                    and active <= running
                ):
                    healthy_observations += 1
                    if healthy_observations >= 3:
                        return
                else:
                    healthy_observations = 0
            except (OSError, ValueError, KeyError):
                healthy_observations = 0
            time.sleep(1)
        raise RuntimeError("Runtime health or implementation verification timed out.")

    def prepare(self, digest: str) -> str:
        release_root = self.release_root(digest)
        package = self.data_root / "artifacts" / "sha256" / digest[:2] / digest
        with package.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("Runtime package differs from its uploaded digest.")
        self.save(phase="verifying", candidate_release=digest)
        trusted = Path(
            os.environ.get(
                "YBB_EDGE_TRUSTED_RELEASE_KEY",
                "C:/ProgramData/YBB/Trust/release-signing-public.pem"
                if os.name == "nt"
                else "/etc/ybb-edge/release-signing-public.pem",
            )
        )
        manifest = verify_software_package(package, trusted)
        if manifest.platform != os.environ.get("YBB_EDGE_PLATFORM", "jetson_orin_nano"):
            raise ValueError("Runtime package targets a different platform.")
        if manifest.architecture != os.environ.get(
            "YBB_EDGE_ARCHITECTURE", "x86_64" if os.name == "nt" else "aarch64"
        ):
            raise ValueError("Runtime package targets a different CPU architecture.")
        if manifest.python_version != f"{sys.version_info.major}.{sys.version_info.minor}":
            raise ValueError("Python base runtime update requires platform maintenance.")
        for field, variable in (
            ("cuda_version", "YBB_EDGE_CUDA_VERSION"),
            ("tensorrt_version", "YBB_EDGE_TENSORRT_VERSION"),
        ):
            if not runtime_platform_version_matches(
                getattr(manifest, field), os.environ.get(variable)
            ):
                raise ValueError(f"{field} differs; install the matching platform package first.")
        with zipfile.ZipFile(package) as archive:
            required_bytes = sum(info.file_size for info in archive.infolist()) * 3
        if shutil.disk_usage(self.root).free < required_bytes + 1024**3:
            raise ValueError("Insufficient space for runtime preparation and rollback reserve.")
        complete = release_root / "ready.json"
        if not complete.exists():
            self.save(phase="installing")
            release_root.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(package) as archive:
                archive.extractall(
                    release_root
                )  # exact signed, path-checked allowlist verified above
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "venv",
                    "--system-site-packages",
                    str(release_root / "venv"),
                ],
                check=True,
                timeout=120,
            )
            wheels = sorted(str(path) for path in (release_root / "wheelhouse").glob("*.whl"))
            subprocess.run(
                [
                    self.python(digest),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--force-reinstall",
                    *wheels,
                ],
                check=True,
                timeout=1800,
            )
            self.check_dependencies(digest)
            probe = subprocess.run(
                [
                    self.python(digest),
                    "-c",
                    "from cv_runtime.distribution import runtime_code_digest, edge_node_contracts; "
                    "from cv_runtime.registry import builtin_node_types; "
                    "from cv_runtime.loader import load_runtime_handler; "
                    "[load_runtime_handler(n.runtime_handler_ref, expected_node_type=n.node_type) "
                    "for n in builtin_node_types() if n.node_type in edge_node_contracts()]; "
                    "print(runtime_code_digest())",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if probe.stdout.strip().splitlines()[-1] != manifest.runtime_code_digest:
                raise ValueError("Installed runtime digest differs from signed manifest.")
            complete.write_text(manifest.model_dump_json(), encoding="utf-8")
        return manifest.runtime_code_digest

    def check_dependencies(self, digest: str) -> None:
        result = subprocess.run(
            [
                self.python(digest),
                str(Path(__file__).with_name("check-runtime-dependencies.py")),
                "--manifest",
                str(self.release_root(digest) / "release.json"),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
        if result.returncode:
            details = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            # Keep the package conflict in last_error for the central UI. The
            # candidate must pass this check before the active runtime is stopped.
            raise RuntimeError(
                f"Runtime dependency check failed (exit {result.returncode}): "
                + (details or "No diagnostic output from the runtime dependency checker.")[:1800]
            )

    def apply(self, command: dict) -> None:
        previous = self.state["active_release"]
        standby = self.state.get("previous_release")
        previous_digest = self.state.get("runtime_code_digest")
        self.save(request_id=command["request_id"], phase="queued", last_error=None)
        try:
            if command["action"] == "rollback":
                candidate = self.state.get("previous_release")
                if not candidate:
                    raise ValueError("No previous software release is available.")
                expected = None
            else:
                candidate = command["package_sha256"]
                expected = self.prepare(candidate)
            if candidate == previous:
                self.save(phase="succeeded", candidate_release=None)
                return
            self.save(phase="activating", candidate_release=candidate)
            self.stop()
            self.save(active_release=candidate, previous_release=previous, phase="checking")
            self.start()
            self.healthy(expected)
            self.save(
                phase="rolled_back" if command["action"] == "rollback" else "succeeded",
                candidate_release=None,
                runtime_code_digest=expected,
            )
        except Exception as exc:
            if self.state["active_release"] != previous:
                self.stop()
                failed = self.state["active_release"]
                self.save(
                    active_release=previous,
                    previous_release=standby,
                    candidate_release=failed,
                    runtime_code_digest=previous_digest,
                )
                self.start()
                try:
                    self.healthy(previous_digest)
                except Exception as recovery_error:
                    exc = RuntimeError(f"{exc}; previous runtime recovery: {recovery_error}")
            self.save(phase="failed", last_error=str(exc)[:2000])

    def run(self) -> None:
        # Power loss during activation restores the last confirmed executable environment.
        if self.state["phase"] == "checking" and self.state.get("previous_release"):
            self.save(
                active_release=self.state["previous_release"],
                previous_release=None,
                phase="failed",
                last_error="Interrupted activation recovered previous runtime.",
            )
        self.save()
        self.start()
        try:
            while not self.stopping:
                for path in sorted((self.root / "requests").glob("*.json")):
                    if (self.root / "completed" / path.name).exists():
                        continue
                    command = json.loads(path.read_text())
                    if not (
                        self.state.get("request_id") == command["request_id"]
                        and self.state["phase"] in {"succeeded", "failed", "rolled_back"}
                    ):
                        self.apply(command)
                    (self.root / "completed" / path.name).write_text("done")
                if self.child is not None and self.child.poll() is not None:
                    self.start()
                time.sleep(1)
        finally:
            self.stop()


if __name__ == "__main__":
    launcher = RuntimeLauncher(Path(os.environ["YBB_EDGE_DATA_ROOT"]))

    def request_stop(*_args: object) -> None:
        launcher.stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    launcher.run()
