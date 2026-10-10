from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cv_contracts import EdgeDeploymentManifest

from edge_agent.inference import (
    EdgeInferenceError,
    EdgeInferenceRuntime,
    InferenceSession,
    TensorRtEngineSession,
    compare_golden_output,
)


class EdgeEngineAdmissionError(RuntimeError):
    """Raised when a workload cannot be compiled or validated on the target Edge."""


class EdgeEngineManager:
    def __init__(
        self,
        *,
        engine_root: Path,
        artifact_resolver: Callable[[str], Path],
        builder_mode: str = "simulated",
        trtexec_path: str = "trtexec",
        build_timeout_seconds: float = 900.0,
        cache_namespace: str = "unknown-platform",
        session_factory: Callable[[Path], InferenceSession] = TensorRtEngineSession,
    ) -> None:
        if builder_mode not in {"simulated", "tensorrt_python", "trtexec"}:
            raise ValueError("builder_mode must be simulated, tensorrt_python, or trtexec")
        self.engine_root = engine_root
        self.artifact_resolver = artifact_resolver
        self.builder_mode = builder_mode
        self.trtexec_path = trtexec_path
        self.build_timeout_seconds = build_timeout_seconds
        self.cache_namespace = cache_namespace
        self.session_factory = session_factory
        self._build_lock = threading.RLock()
        self.engine_root.mkdir(parents=True, exist_ok=True)

    def admit(
        self, manifest: EdgeDeploymentManifest
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        started = time.monotonic()
        builds = self.ensure_engines(manifest)
        golden_items = self._items(manifest, "golden_tests")
        needs_runtime = any(
            str(item.get("comparison") or "artifact_integrity") != "artifact_integrity"
            for item in golden_items
        )
        inference_runtime: EdgeInferenceRuntime | None = None
        try:
            has_models = bool(manifest.runtime_requirements.get("models") or [])
            if self.builder_mode == "simulated":
                if needs_runtime:
                    raise EdgeEngineAdmissionError(
                        "Golden output comparison requires a real TensorRT engine builder."
                    )
            elif has_models:
                inference_runtime = self.create_inference_runtime(manifest, builds)
            golden = [
                self._run_golden_test(manifest, item, inference_runtime) for item in golden_items
            ]
        finally:
            if inference_runtime is not None:
                inference_runtime.close()
        report = {
            "schema_version": "edge.local-admission.v1",
            "status": "admitted",
            "builder_mode": self.builder_mode,
            "engine_build_count": len(builds),
            "engine_cache_hit_count": sum(bool(item["cache_hit"]) for item in builds),
            "golden_test_count": len(golden),
            "golden_tests": golden,
            "engine_runtime_validated": bool(inference_runtime) or not has_models,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }
        return report, builds

    def ensure_engines(self, manifest: EdgeDeploymentManifest) -> list[dict[str, Any]]:
        return [
            self._build_engine(manifest, item) for item in self._items(manifest, "engine_builds")
        ]

    def create_inference_runtime(
        self,
        manifest: EdgeDeploymentManifest,
        engine_builds: list[dict[str, Any]],
    ) -> EdgeInferenceRuntime | None:
        if not (manifest.runtime_requirements.get("models") or []):
            return None
        if self.builder_mode == "simulated":
            return None
        try:
            return EdgeInferenceRuntime.from_manifest(
                manifest,
                engine_builds,
                artifact_resolver=self.artifact_resolver,
                session_factory=self.session_factory,
            )
        except EdgeInferenceError as exc:
            raise EdgeEngineAdmissionError(str(exc)) from exc

    def _build_engine(
        self, manifest: EdgeDeploymentManifest, item: dict[str, Any]
    ) -> dict[str, Any]:
        # Admission and a recovering worker can request the same plan concurrently.
        with self._build_lock:
            return self._build_engine_locked(manifest, item)

    def _build_engine_locked(
        self, manifest: EdgeDeploymentManifest, item: dict[str, Any]
    ) -> dict[str, Any]:
        started = time.monotonic()
        node_key = str(item.get("node_key") or "").strip()
        onnx_sha256 = str(item.get("onnx_sha256") or "").strip()
        precision = str(item.get("precision") or "fp16").lower()
        if not node_key or len(onnx_sha256) != 64:
            raise EdgeEngineAdmissionError("Engine build entry lacks node_key or ONNX digest.")
        if precision not in {"fp32", "fp16", "int8"}:
            raise EdgeEngineAdmissionError(f"Unsupported TensorRT precision {precision!r}.")
        onnx_path = self.artifact_resolver(onnx_sha256)
        if not onnx_path.is_file() or _sha256_file(onnx_path) != onnx_sha256:
            raise EdgeEngineAdmissionError(f"ONNX artifact for node {node_key} is unavailable.")
        build_key = _digest_json(
            {
                "onnx_sha256": onnx_sha256,
                "precision": precision,
                "input_shapes": item.get("input_shapes") or {},
                "calibration_cache_sha256": item.get("calibration_cache_sha256"),
                "builder_mode": self.builder_mode,
                "cache_namespace": self.cache_namespace,
            }
        )
        destination = (
            self.engine_root / manifest.workload_release_id / node_key / f"{build_key}.plan"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shared_cache = self.engine_root / "cache" / f"{build_key}.plan"
        cache_hit = _valid_cached_plan(destination)
        invalid_cache = destination.exists() and not cache_hit
        if not cache_hit:
            # A workload release can change for camera/pipeline settings while
            # the model, shapes, precision and platform namespace remain equal.
            # Keep the per-release path, but reuse an existing identical build.
            for cached in [shared_cache, *sorted(self.engine_root.glob(f"*/*/{build_key}.plan"))]:
                if cached != destination and _valid_cached_plan(cached):
                    _copy_cached_plan(cached, destination)
                    cache_hit = True
                    break
        if not cache_hit:
            temporary = destination.with_suffix(".tmp")
            if self.builder_mode == "simulated":
                temporary.write_bytes(
                    b"YBB-SIMULATED-TENSORRT\0"
                    + json.dumps(item, sort_keys=True).encode("utf-8")
                    + onnx_path.read_bytes()
                )
            elif self.builder_mode == "tensorrt_python":
                calibration_path: Path | None = None
                if precision == "int8":
                    calibration_digest = str(item.get("calibration_cache_sha256") or "")
                    calibration_path = self.artifact_resolver(calibration_digest)
                    if len(calibration_digest) != 64 or not calibration_path.is_file():
                        raise EdgeEngineAdmissionError(
                            f"INT8 engine for node {node_key} has no calibration cache."
                        )
                try:
                    _build_with_tensorrt_python(
                        onnx_path=onnx_path,
                        destination=temporary,
                        precision=precision,
                        input_shapes=item.get("input_shapes") or {},
                        calibration_path=calibration_path,
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    temporary.unlink(missing_ok=True)
                    raise EdgeEngineAdmissionError(
                        f"TensorRT Python build failed for node {node_key}: {exc}"
                    ) from exc
            else:
                command = [self.trtexec_path, f"--onnx={onnx_path}", f"--saveEngine={temporary}"]
                if precision == "fp16":
                    command.append("--fp16")
                elif precision == "int8":
                    command.append("--int8")
                    calibration_digest = str(item.get("calibration_cache_sha256") or "")
                    calibration_path = self.artifact_resolver(calibration_digest)
                    if len(calibration_digest) != 64 or not calibration_path.is_file():
                        raise EdgeEngineAdmissionError(
                            f"INT8 engine for node {node_key} has no calibration cache."
                        )
                    command.append(f"--calib={calibration_path}")
                shapes = []
                for name, shape in sorted((item.get("input_shapes") or {}).items()):
                    dimensions = "x".join(str(max(int(value), 1)) for value in shape)
                    shapes.append(f"{name}:{dimensions}")
                if shapes:
                    command.append(f"--shapes={','.join(shapes)}")
                log_path = destination.with_suffix(".build.log")
                try:
                    # Preserve output while trtexec is running, not only when it
                    # exits. An operator can follow this file during preparation.
                    with log_path.open("w", encoding="utf-8") as build_log:
                        result = subprocess.run(
                            command,
                            check=False,
                            stdout=build_log,
                            stderr=subprocess.STDOUT,
                            text=True,
                            timeout=self.build_timeout_seconds,
                        )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    temporary.unlink(missing_ok=True)
                    raise EdgeEngineAdmissionError(
                        f"TensorRT build could not run for node {node_key}: {exc}. Log: {log_path}"
                    ) from exc
                if result.returncode != 0 or not temporary.is_file():
                    temporary.unlink(missing_ok=True)
                    with log_path.open("rb") as build_log:
                        build_log.seek(max(log_path.stat().st_size - 4096, 0))
                        detail = build_log.read().decode("utf-8", errors="replace")[-1500:]
                    raise EdgeEngineAdmissionError(
                        f"TensorRT build failed for node {node_key}: "
                        f"{detail or 'No trtexec output'}. Log: {log_path}"
                    )
            if not temporary.is_file() or not temporary.stat().st_size:
                temporary.unlink(missing_ok=True)
                raise EdgeEngineAdmissionError(
                    f"TensorRT produced an empty engine for node {node_key}."
                )
            temporary.replace(destination)
        _write_plan_digest(destination)
        if not _valid_cached_plan(shared_cache):
            _copy_cached_plan(destination, shared_cache)
        return {
            "node_key": node_key,
            "onnx_sha256": onnx_sha256,
            "precision": precision,
            "engine_path": str(destination),
            "engine_sha256": _sha256_file(destination),
            "engine_size_bytes": destination.stat().st_size,
            "cache_hit": cache_hit,
            "cache_key": build_key,
            "reason": "cache_hit"
            if cache_hit
            else "invalid_cache"
            if invalid_cache
            else "missing_or_changed",
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }

    def _run_golden_test(
        self,
        manifest: EdgeDeploymentManifest,
        item: dict[str, Any],
        inference_runtime: EdgeInferenceRuntime | None,
    ) -> dict[str, Any]:
        node_key = str(item.get("node_key") or "").strip()
        input_digest = str(item.get("input_sha256") or "").strip()
        if not node_key or len(input_digest) != 64:
            raise EdgeEngineAdmissionError("Golden test lacks node_key or input digest.")
        input_path = self.artifact_resolver(input_digest)
        if not input_path.is_file() or _sha256_file(input_path) != input_digest:
            raise EdgeEngineAdmissionError(f"Golden input for node {node_key} is unavailable.")
        expected_digest = item.get("expected_output_sha256")
        expected_path: Path | None = None
        if expected_digest:
            expected_path = self.artifact_resolver(str(expected_digest))
            if not expected_path.is_file() or _sha256_file(expected_path) != expected_digest:
                raise EdgeEngineAdmissionError(
                    f"Golden expected-output artifact for node {node_key} is unavailable."
                )
        comparison = str(item.get("comparison") or "artifact_integrity")
        comparison_detail: dict[str, Any] = {}
        if comparison != "artifact_integrity" and expected_path is None:
            raise EdgeEngineAdmissionError(
                f"Golden comparison {comparison!r} needs an expected-output artifact."
            )
        if comparison != "artifact_integrity":
            if inference_runtime is None or expected_path is None:
                raise EdgeEngineAdmissionError("Golden TensorRT runtime is unavailable.")
            try:
                actual = inference_runtime.infer_file(node_key, input_path)
                comparison_detail = compare_golden_output(
                    actual,
                    expected_path,
                    comparison=comparison,
                    atol=float(item.get("atol") or 0.0),
                    rtol=float(item.get("rtol") or 0.0),
                )
            except (EdgeInferenceError, OSError, ValueError) as exc:
                raise EdgeEngineAdmissionError(
                    f"Golden output comparison failed for node {node_key}: {exc}"
                ) from exc
        return {
            "node_key": node_key,
            "input_sha256": input_digest,
            "comparison": comparison,
            "status": "passed",
            "release_id": manifest.workload_release_id,
            **comparison_detail,
        }

    @staticmethod
    def _items(manifest: EdgeDeploymentManifest, key: str) -> list[dict[str, Any]]:
        value = manifest.runtime_requirements.get(key) or []
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise EdgeEngineAdmissionError(f"runtime_requirements.{key} must be an object list.")
        return [dict(item) for item in value]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_json(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _build_with_tensorrt_python(
    *,
    onnx_path: Path,
    destination: Path,
    precision: str,
    input_shapes: dict[str, Any],
    calibration_path: Path | None,
) -> None:
    try:
        import tensorrt as trt  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("the bundled TensorRT Python package is unavailable") from exc

    logger = trt.Logger(trt.Logger.ERROR)
    if hasattr(trt, "init_libnvinfer_plugins"):
        trt.init_libnvinfer_plugins(logger, "")
    builder = trt.Builder(logger)
    if builder is None:
        raise RuntimeError("TensorRT could not create a CUDA engine builder")
    explicit_batch = 1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
    network = builder.create_network(explicit_batch)
    parser = trt.OnnxParser(network, logger)
    if not parser.parse(onnx_path.read_bytes()):
        errors = [str(parser.get_error(index)) for index in range(parser.num_errors)]
        raise RuntimeError("ONNX parser rejected the model: " + " | ".join(errors[-8:]))

    config = builder.create_builder_config()
    if precision == "fp16":
        config.set_flag(trt.BuilderFlag.FP16)
    calibrator: Any | None = None
    if precision == "int8":
        if calibration_path is None:
            raise RuntimeError("INT8 requires a calibration cache")
        config.set_flag(trt.BuilderFlag.INT8)
        calibrator = _calibration_cache_reader(trt, calibration_path.read_bytes())
        config.int8_calibrator = calibrator

    configured_shapes = {
        str(name): tuple(max(int(value), 1) for value in shape)
        for name, shape in input_shapes.items()
    }
    profile = None
    for index in range(network.num_inputs):
        tensor = network.get_input(index)
        name = str(tensor.name)
        model_shape = tuple(int(value) for value in tensor.shape)
        selected = configured_shapes.get(name)
        if any(value < 0 for value in model_shape):
            if selected is None:
                raise RuntimeError(f"dynamic ONNX input {name!r} has no deployment shape")
            if len(selected) != len(model_shape):
                raise RuntimeError(
                    f"deployment shape {selected} does not match ONNX input {name!r}"
                )
            if profile is None:
                profile = builder.create_optimization_profile()
            accepted = profile.set_shape(name, selected, selected, selected)
            if accepted is False:
                raise RuntimeError(f"TensorRT rejected deployment shape {selected} for {name!r}")
        elif selected is not None and selected != model_shape:
            raise RuntimeError(
                f"deployment shape {selected} differs from static ONNX shape {model_shape} "
                f"for {name!r}"
            )
    unknown_shapes = sorted(
        set(configured_shapes)
        - {str(network.get_input(index).name) for index in range(network.num_inputs)}
    )
    if unknown_shapes:
        raise RuntimeError(f"deployment shapes reference unknown ONNX inputs: {unknown_shapes}")
    if profile is not None:
        config.add_optimization_profile(profile)

    serialized = builder.build_serialized_network(network, config)
    if serialized is None:
        raise RuntimeError("TensorRT returned no serialized engine")
    destination.write_bytes(bytes(serialized))
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("TensorRT produced an empty engine")
    del calibrator


def _valid_cached_plan(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        checksum = path.with_suffix(".sha256")
        # Existing releases predate the sidecar; validate by deserializing at admission.
        return not checksum.exists() or checksum.read_text(
            encoding="ascii"
        ).strip() == _sha256_file(path)
    except (OSError, UnicodeError):
        return False


def _write_plan_digest(path: Path) -> None:
    checksum = path.with_suffix(".sha256")
    digest = _sha256_file(path)
    try:
        if checksum.read_text(encoding="ascii").strip() == digest:
            return
    except (OSError, UnicodeError):
        pass
    temporary = checksum.with_suffix(".sha256.tmp")
    try:
        temporary.write_text(digest, encoding="ascii")
        temporary.replace(checksum)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_cached_plan(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
        _write_plan_digest(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _calibration_cache_reader(trt: Any, payload: bytes) -> Any:  # noqa: ANN401
    class CacheCalibrator(trt.IInt8EntropyCalibrator2):
        def __init__(self) -> None:
            trt.IInt8EntropyCalibrator2.__init__(self)

        def get_batch_size(self) -> int:
            return 1

        def get_batch(self, _names: list[str]) -> None:
            return None

        def read_calibration_cache(self) -> bytes:
            return payload

        def write_calibration_cache(self, _cache: bytes) -> None:
            return None

    return CacheCalibrator()
