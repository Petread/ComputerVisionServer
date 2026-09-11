from __future__ import annotations

# ruff: noqa: ANN401 - CUDA/TensorRT expose dynamically typed native handles
import json
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from cv_contracts import EdgeDeploymentManifest
from PIL import Image


class EdgeInferenceError(RuntimeError):
    """A deterministic model loading, preprocessing or inference failure."""


class InferenceSession(Protocol):
    input_names: tuple[str, ...]
    output_names: tuple[str, ...]

    def infer(self, inputs: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]: ...

    def close(self) -> None: ...


class _CudaRuntime:
    def __init__(self) -> None:
        try:
            from cuda.bindings import runtime as cudart  # type: ignore[import-not-found]
        except ImportError:
            try:
                from cuda import cudart  # type: ignore[import-not-found,no-redef]
            except ImportError as exc:
                raise EdgeInferenceError(
                    "TensorRT execution requires the cuda-python runtime bindings."
                ) from exc
        self.api = cudart

    @staticmethod
    def _checked(result: Any, operation: str) -> Any:
        values = result if isinstance(result, tuple) else (result,)
        status = values[0]
        status_value = int(getattr(status, "value", status))
        if status_value != 0:
            raise EdgeInferenceError(f"CUDA operation {operation} failed with status {status}.")
        if len(values) == 1:
            return None
        return values[1] if len(values) == 2 else values[1:]

    def stream_create(self) -> Any:
        return self._checked(self.api.cudaStreamCreate(), "cudaStreamCreate")

    def stream_destroy(self, stream: Any) -> None:
        self._checked(self.api.cudaStreamDestroy(stream), "cudaStreamDestroy")

    def malloc(self, size: int) -> Any:
        return self._checked(self.api.cudaMalloc(size), "cudaMalloc")

    def free(self, pointer: Any) -> None:
        self._checked(self.api.cudaFree(pointer), "cudaFree")

    def copy_host_to_device(self, pointer: Any, array: np.ndarray, stream: Any) -> None:
        kind = self.api.cudaMemcpyKind.cudaMemcpyHostToDevice
        self._checked(
            self.api.cudaMemcpyAsync(pointer, array.ctypes.data, array.nbytes, kind, stream),
            "cudaMemcpyAsync(H2D)",
        )

    def copy_device_to_host(self, array: np.ndarray, pointer: Any, stream: Any) -> None:
        kind = self.api.cudaMemcpyKind.cudaMemcpyDeviceToHost
        self._checked(
            self.api.cudaMemcpyAsync(array.ctypes.data, pointer, array.nbytes, kind, stream),
            "cudaMemcpyAsync(D2H)",
        )

    def synchronize(self, stream: Any) -> None:
        self._checked(self.api.cudaStreamSynchronize(stream), "cudaStreamSynchronize")


class TensorRtEngineSession:
    """TensorRT 10 execution session using the CUDA runtime, without a Triton dependency."""

    def __init__(self, engine_path: Path) -> None:
        try:
            import tensorrt as trt  # type: ignore[import-not-found]
        except ImportError as exc:
            raise EdgeInferenceError(
                "The productive edge runtime requires the platform TensorRT Python package."
            ) from exc
        self._trt = trt
        self._cuda = _CudaRuntime()
        self._logger = trt.Logger(trt.Logger.ERROR)
        self._runtime = trt.Runtime(self._logger)
        try:
            payload = engine_path.read_bytes()
        except OSError as exc:
            raise EdgeInferenceError(f"TensorRT engine cannot be read: {engine_path}") from exc
        self._engine = self._runtime.deserialize_cuda_engine(payload)
        if self._engine is None:
            raise EdgeInferenceError(f"TensorRT rejected engine {engine_path}.")
        self._context = self._engine.create_execution_context()
        if self._context is None:
            raise EdgeInferenceError(f"TensorRT could not create a context for {engine_path}.")
        self._tensor_api = hasattr(self._engine, "num_io_tensors")
        if self._tensor_api:
            names = tuple(
                str(self._engine.get_tensor_name(index))
                for index in range(self._engine.num_io_tensors)
            )
            self.input_names = tuple(
                name
                for name in names
                if self._engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT
            )
            self.output_names = tuple(name for name in names if name not in self.input_names)
        else:  # TensorRT 8 compatibility for existing Windows installations.
            names = tuple(
                str(self._engine.get_binding_name(i)) for i in range(self._engine.num_bindings)
            )
            self.input_names = tuple(
                name for index, name in enumerate(names) if self._engine.binding_is_input(index)
            )
            self.output_names = tuple(name for name in names if name not in self.input_names)
        if not self.input_names or not self.output_names:
            raise EdgeInferenceError("TensorRT engine has no usable input/output bindings.")
        self._lock = threading.Lock()

    def infer(self, inputs: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
        missing = sorted(set(self.input_names) - set(inputs))
        if missing:
            raise EdgeInferenceError(f"TensorRT input tensors are missing: {missing}")
        with self._lock:
            return self._infer_locked(inputs)

    def _infer_locked(self, inputs: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
        stream = self._cuda.stream_create()
        allocations: list[Any] = []
        host_outputs: dict[str, np.ndarray] = {}
        bindings: list[int] | None = None
        try:
            prepared: dict[str, np.ndarray] = {}
            for name in self.input_names:
                array = np.ascontiguousarray(inputs[name], dtype=self._dtype(name))
                prepared[name] = array
                self._set_input_shape(name, tuple(int(value) for value in array.shape))
            if not self._all_shapes_specified():
                raise EdgeInferenceError("TensorRT dynamic input shapes are not fully specified.")
            tensor_pointers: dict[str, Any] = {}
            for name, array in prepared.items():
                pointer = self._cuda.malloc(max(array.nbytes, 1))
                allocations.append(pointer)
                tensor_pointers[name] = pointer
                self._cuda.copy_host_to_device(pointer, array, stream)
            for name in self.output_names:
                shape = self._shape(name)
                if any(value < 0 for value in shape):
                    raise EdgeInferenceError(
                        f"TensorRT output {name!r} still has a dynamic shape {shape}."
                    )
                output = np.empty(shape, dtype=self._dtype(name))
                pointer = self._cuda.malloc(max(output.nbytes, 1))
                allocations.append(pointer)
                tensor_pointers[name] = pointer
                host_outputs[name] = output
            if self._tensor_api:
                for name, pointer in tensor_pointers.items():
                    if not self._context.set_tensor_address(name, int(pointer)):
                        raise EdgeInferenceError(f"TensorRT rejected tensor address for {name}.")
                ok = self._context.execute_async_v3(stream_handle=int(stream))
            else:
                bindings = [0] * self._engine.num_bindings
                for name, pointer in tensor_pointers.items():
                    bindings[self._engine.get_binding_index(name)] = int(pointer)
                ok = self._context.execute_async_v2(bindings=bindings, stream_handle=int(stream))
            if not ok:
                raise EdgeInferenceError("TensorRT execution returned failure.")
            for name, output in host_outputs.items():
                self._cuda.copy_device_to_host(output, tensor_pointers[name], stream)
            self._cuda.synchronize(stream)
            return host_outputs
        finally:
            for pointer in allocations:
                self._cuda.free(pointer)
            self._cuda.stream_destroy(stream)

    def _dtype(self, name: str) -> np.dtype[Any]:
        if self._tensor_api:
            value = self._engine.get_tensor_dtype(name)
        else:
            value = self._engine.get_binding_dtype(self._engine.get_binding_index(name))
        return np.dtype(self._trt.nptype(value))

    def _shape(self, name: str) -> tuple[int, ...]:
        if self._tensor_api:
            shape = self._context.get_tensor_shape(name)
        else:
            shape = self._context.get_binding_shape(self._engine.get_binding_index(name))
        return tuple(int(value) for value in shape)

    def _set_input_shape(self, name: str, shape: tuple[int, ...]) -> None:
        if self._tensor_api:
            if not self._context.set_input_shape(name, shape):
                raise EdgeInferenceError(f"TensorRT rejected input shape {shape} for {name}.")
        else:
            index = self._engine.get_binding_index(name)
            if any(int(value) < 0 for value in self._engine.get_binding_shape(index)):
                if not self._context.set_binding_shape(index, shape):
                    raise EdgeInferenceError(f"TensorRT rejected input shape {shape} for {name}.")

    def _all_shapes_specified(self) -> bool:
        value = getattr(self._context, "all_binding_shapes_specified", True)
        return bool(value() if callable(value) else value)

    def close(self) -> None:
        # TensorRT/CUDA objects use deterministic Python reference lifetimes.
        return None


@dataclass(frozen=True)
class PreprocessTransform:
    original_width: int
    original_height: int
    model_width: int
    model_height: int
    scale_x: float
    scale_y: float
    pad_x: float
    pad_y: float


@dataclass(frozen=True)
class ModelRuntimeSpec:
    node_key: str
    task_type: str
    input_size: tuple[int, int]
    preprocessing: dict[str, Any]
    postprocessing: dict[str, Any]
    nms: dict[str, Any]
    io_schema: dict[str, Any]
    class_map: tuple[str, ...]
    model_version_id: str | None
    deployment_release_id: str | None
    warmup_runs: int = 2


class EdgeInferenceRuntime:
    def __init__(
        self,
        *,
        specs: dict[str, ModelRuntimeSpec],
        sessions: dict[str, InferenceSession],
    ) -> None:
        self.specs = specs
        self.sessions = sessions
        self._warmed_nodes: set[str] = set()
        self._warmup_lock = threading.Lock()

    @classmethod
    def from_manifest(
        cls,
        manifest: EdgeDeploymentManifest,
        engine_builds: list[dict[str, Any]],
        *,
        artifact_resolver: Callable[[str], Path],
        session_factory: Callable[[Path], InferenceSession] = TensorRtEngineSession,
    ) -> EdgeInferenceRuntime:
        model_items = manifest.runtime_requirements.get("models") or []
        if not isinstance(model_items, list):
            raise EdgeInferenceError("runtime_requirements.models must be a list.")
        build_by_node = {str(item.get("node_key")): item for item in engine_builds}
        specs: dict[str, ModelRuntimeSpec] = {}
        sessions: dict[str, InferenceSession] = {}
        for raw in model_items:
            if not isinstance(raw, dict):
                raise EdgeInferenceError("Every runtime model description must be an object.")
            node_key = str(raw.get("node_key") or "")
            build = build_by_node.get(node_key)
            if not node_key or build is None:
                raise EdgeInferenceError(
                    f"No TensorRT engine build exists for model node {node_key!r}."
                )
            preprocessing = _object_field(raw, "preprocessing", node_key)
            postprocessing = _object_field(raw, "postprocessing", node_key)
            io_schema = _object_field(raw, "io_schema", node_key)
            nms = _object_field(raw, "nms", node_key)
            class_map = _read_class_map(raw.get("class_map_sha256"), artifact_resolver)
            if not class_map:
                fallback_map = postprocessing.get("class_map") or []
                if isinstance(fallback_map, dict):
                    class_map = tuple(
                        str(label)
                        for _, label in sorted(
                            fallback_map.items(), key=lambda item: int(item[0])
                        )
                    )
                elif isinstance(fallback_map, list):
                    class_map = tuple(str(label) for label in fallback_map)
            input_size = raw.get("input_size") or [640, 640]
            if not isinstance(input_size, list) or len(input_size) < 2:
                raise EdgeInferenceError(f"Model {node_key} has an invalid input_size.")
            specs[node_key] = ModelRuntimeSpec(
                node_key=node_key,
                task_type=str(raw.get("task_type") or "classification"),
                input_size=(int(input_size[-2]), int(input_size[-1])),
                preprocessing=preprocessing,
                postprocessing=postprocessing,
                nms=nms,
                io_schema=io_schema,
                class_map=class_map,
                model_version_id=_optional_string(raw.get("model_version_id")),
                deployment_release_id=_optional_string(raw.get("deployment_release_id")),
                warmup_runs=max(int(raw.get("warmup_runs") or 2), 0),
            )
            sessions[node_key] = session_factory(Path(str(build["engine_path"])))
        return cls(specs=specs, sessions=sessions)

    def infer(
        self,
        node_key: str,
        image: Any,
        *,
        node_config: dict[str, Any] | None = None,
        source_color_format: str = "bgr",
    ) -> dict[str, Any]:
        try:
            spec = self.specs[node_key]
            session = self.sessions[node_key]
        except KeyError as exc:
            raise EdgeInferenceError(f"Inference node {node_key!r} has no loaded engine.") from exc
        tensor, transform = preprocess_image(
            image,
            spec,
            input_name=session.input_names[0],
            source_color_format=source_color_format,
        )
        tensor_inputs = {session.input_names[0]: tensor}
        if node_key not in self._warmed_nodes:
            with self._warmup_lock:
                if node_key not in self._warmed_nodes:
                    for _ in range(spec.warmup_runs):
                        session.infer(tensor_inputs)
                    self._warmed_nodes.add(node_key)
        outputs = session.infer(tensor_inputs)
        normalized = postprocess_outputs(
            outputs,
            spec,
            transform,
            node_config=node_config or {},
        )
        return {
            "node_key": node_key,
            "task_type": spec.task_type,
            "model_version_id": spec.model_version_id,
            "deployment_release_id": spec.deployment_release_id,
            "normalized": normalized,
            "raw_tensors": {name: _tensor_summary(value) for name, value in outputs.items()},
            "_raw_arrays": outputs,
        }

    def infer_file(self, node_key: str, path: Path) -> dict[str, Any]:
        with Image.open(path) as image:
            rgb = np.asarray(image.convert("RGB"))
        return self.infer(node_key, rgb, source_color_format="rgb")

    def close(self) -> None:
        for session in self.sessions.values():
            session.close()


def preprocess_image(
    image: Any,
    spec: ModelRuntimeSpec,
    *,
    input_name: str,
    source_color_format: str,
) -> tuple[np.ndarray, PreprocessTransform]:
    del input_name  # Name remains part of the public preprocessing boundary.
    array = np.asarray(image)
    if array.ndim == 2:
        array = np.repeat(array[..., None], 3, axis=2)
    if array.ndim != 3 or array.shape[2] < 3:
        raise EdgeInferenceError("Model input must be an HxWx3 image.")
    array = array[..., :3]
    target_color = str(spec.preprocessing.get("color_format") or "rgb").lower()
    if source_color_format.lower() != target_color:
        if {source_color_format.lower(), target_color} == {"rgb", "bgr"}:
            array = array[..., ::-1]
        else:
            raise EdgeInferenceError(
                f"Unsupported color conversion {source_color_format!r} -> {target_color!r}."
            )
    original_height, original_width = int(array.shape[0]), int(array.shape[1])
    model_height, model_width = spec.input_size
    resize_mode = str(spec.preprocessing.get("resize_mode") or "stretch")
    pil = Image.fromarray(np.asarray(array, dtype=np.uint8))
    if resize_mode == "letterbox" or bool(
        (spec.preprocessing.get("letterbox") or {}).get("enabled")
    ):
        scale = min(model_width / original_width, model_height / original_height)
        resized_width = max(1, round(original_width * scale))
        resized_height = max(1, round(original_height * scale))
        resized = pil.resize((resized_width, resized_height), Image.Resampling.BILINEAR)
        letterbox = spec.preprocessing.get("letterbox") or {}
        pad_value = int(letterbox.get("padding_value", 114))
        canvas = Image.new("RGB", (model_width, model_height), (pad_value,) * 3)
        pad_x = (model_width - resized_width) // 2 if letterbox.get("center", True) else 0
        pad_y = (model_height - resized_height) // 2 if letterbox.get("center", True) else 0
        canvas.paste(resized, (pad_x, pad_y))
        resized_array = np.asarray(canvas, dtype=np.float32)
        scale_x = scale_y = scale
    else:
        resized_array = np.asarray(
            pil.resize((model_width, model_height), Image.Resampling.BILINEAR),
            dtype=np.float32,
        )
        scale_x = model_width / original_width
        scale_y = model_height / original_height
        pad_x = pad_y = 0
    scale_value = float(spec.preprocessing.get("scale", 1.0))
    resized_array *= scale_value
    normalize = spec.preprocessing.get("normalize") or {}
    mean = np.asarray(normalize.get("mean") or [0.0, 0.0, 0.0], dtype=np.float32)
    std = np.asarray(normalize.get("std") or [1.0, 1.0, 1.0], dtype=np.float32)
    if np.any(std == 0):
        raise EdgeInferenceError("Preprocessing standard deviation must not be zero.")
    resized_array = (resized_array - mean) / std
    layout = _input_layout(spec.io_schema)
    if layout == "CHW":
        resized_array = np.transpose(resized_array, (2, 0, 1))
    elif layout != "HWC":
        raise EdgeInferenceError(f"Unsupported model input layout {layout!r}.")
    tensor = np.ascontiguousarray(resized_array[None, ...], dtype=np.float32)
    return tensor, PreprocessTransform(
        original_width=original_width,
        original_height=original_height,
        model_width=model_width,
        model_height=model_height,
        scale_x=scale_x,
        scale_y=scale_y,
        pad_x=float(pad_x),
        pad_y=float(pad_y),
    )


def postprocess_outputs(
    outputs: Mapping[str, np.ndarray],
    spec: ModelRuntimeSpec,
    transform: PreprocessTransform,
    *,
    node_config: dict[str, Any],
) -> dict[str, Any]:
    if not outputs:
        raise EdgeInferenceError("TensorRT returned no output tensors.")
    task = spec.task_type.lower()
    first = next(iter(outputs.values()))
    if task == "detection":
        return _postprocess_detection_outputs(outputs, spec, transform, node_config)
    if task == "oriented_detection":
        return _postprocess_oriented_detection(first, spec, transform, node_config)
    if task == "segmentation":
        return _postprocess_segmentation(outputs, spec, transform)
    if task == "anomaly":
        return _postprocess_anomaly(outputs, spec, node_config)
    return _postprocess_classification(first, spec, node_config)


def compare_golden_output(
    actual: dict[str, Any],
    expected_path: Path,
    *,
    comparison: str,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    comparison = comparison.lower()
    if comparison in {"normalized_json", "task_output"}:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        normalized = actual["normalized"]
        if not _json_allclose(normalized, expected, atol=atol, rtol=rtol):
            raise EdgeInferenceError("Golden normalized task output differs from expected output.")
        return {"comparison": comparison, "max_abs_error": 0.0}
    expected = _read_expected_arrays(expected_path)
    actual_arrays = actual.get("_raw_arrays")
    if actual_arrays is None:
        raise EdgeInferenceError("Golden comparison has no raw TensorRT arrays.")
    actual_values = dict(actual_arrays)
    if len(expected) == 1 and len(actual_values) == 1:
        expected_array = next(iter(expected.values()))
        actual_array = next(iter(actual_values.values()))
        pairs = [(actual_array, expected_array)]
    else:
        missing = sorted(set(expected) - set(actual_values))
        if missing:
            raise EdgeInferenceError(f"Golden output tensors are missing: {missing}")
        pairs = [(actual_values[name], expected[name]) for name in expected]
    max_error = 0.0
    for current, wanted in pairs:
        if current.shape != wanted.shape:
            raise EdgeInferenceError(
                f"Golden output shape differs: actual={current.shape}, expected={wanted.shape}."
            )
        if comparison == "exact":
            passed = np.array_equal(current, wanted)
        elif comparison in {"numeric_allclose", "allclose"}:
            passed = np.allclose(current, wanted, atol=atol, rtol=rtol, equal_nan=True)
        else:
            raise EdgeInferenceError(f"Unsupported golden comparison {comparison!r}.")
        if current.size:
            max_error = max(max_error, float(np.nanmax(np.abs(current - wanted))))
        if not passed:
            raise EdgeInferenceError(
                f"Golden TensorRT output differs (max_abs_error={max_error:.8g})."
            )
    return {"comparison": comparison, "max_abs_error": max_error}


def _postprocess_classification(
    output: np.ndarray,
    spec: ModelRuntimeSpec,
    config: dict[str, Any],
) -> dict[str, Any]:
    values = np.asarray(output, dtype=np.float32).reshape(-1)
    if values.size == 0:
        return {"predictions": []}
    if (
        np.any(values < 0)
        or np.any(values > 1)
        or not math.isclose(float(values.sum()), 1.0, rel_tol=0.05, abs_tol=0.05)
    ):
        shifted = values - np.max(values)
        probabilities = np.exp(shifted) / np.sum(np.exp(shifted))
    else:
        probabilities = values
    top_k = min(max(int(config.get("top_k") or 5), 1), probabilities.size)
    indexes = np.argsort(probabilities)[::-1][:top_k]
    return {
        "predictions": [
            {
                "class_id": int(index),
                "label": _class_label(spec, int(index)),
                "score": float(probabilities[index]),
                "rank": rank,
            }
            for rank, index in enumerate(indexes, start=1)
        ]
    }


def _postprocess_detection(
    output: np.ndarray,
    spec: ModelRuntimeSpec,
    transform: PreprocessTransform,
    config: dict[str, Any],
) -> dict[str, Any]:
    values = np.asarray(output, dtype=np.float32)
    while values.ndim > 2 and values.shape[0] == 1:
        values = values[0]
    if values.ndim != 2:
        raise EdgeInferenceError(f"Detection output must be rank 2, received {values.shape}.")
    class_count = len(spec.class_map)
    feature_count = 4 + class_count
    if (
        values.shape[0] == feature_count
        and values.shape[1] != feature_count
        or values.shape[1] < 6 <= values.shape[0]
        or 6 <= values.shape[0] <= max(feature_count, 7) < values.shape[1]
    ):
        values = values.T
    threshold = float(
        config.get("confidence_threshold")
        or spec.nms.get("score_threshold")
        or spec.postprocessing.get("confidence_threshold_default")
        or 0.25
    )
    boxes: list[list[float]] = []
    scores: list[float] = []
    classes: list[int] = []
    for row in values:
        if row.size < 6:
            continue
        if row.size > 6 and (class_count == 0 or row.size >= 4 + class_count):
            class_scores = row[4 : 4 + class_count] if class_count else row[4:]
            class_id = int(np.argmax(class_scores))
            score = float(class_scores[class_id])
            cx, cy, width, height = (float(value) for value in row[:4])
            box = [cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2]
        else:
            box = [float(value) for value in row[:4]]
            score = float(row[4])
            class_id = int(row[5])
        if score < threshold:
            continue
        boxes.append(box)
        scores.append(score)
        classes.append(class_id)
    iou_threshold = float(config.get("nms_iou_threshold") or spec.nms.get("iou_threshold") or 0.65)
    keep = _nms(boxes, scores, iou_threshold)
    max_detections = int(config.get("max_detections") or spec.nms.get("max_detections") or 300)
    detections = []
    for index in keep[:max_detections]:
        x1, y1, x2, y2 = _restore_box(boxes[index], transform)
        detections.append(
            {
                "class_id": classes[index],
                "label": _class_label(spec, classes[index]),
                "confidence": scores[index],
                "box": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            }
        )
    return {"detections": detections}


def _postprocess_detection_outputs(
    outputs: Mapping[str, np.ndarray],
    spec: ModelRuntimeSpec,
    transform: PreprocessTransform,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Normalize either a raw YOLO tensor or TensorRT EfficientNMS outputs."""
    named = {name.lower(): np.asarray(value) for name, value in outputs.items()}
    boxes = next(
        (value for name, value in named.items() if "box" in name and value.shape[-1:] == (4,)),
        None,
    )
    scores = next(
        (value for name, value in named.items() if "score" in name and "box" not in name),
        None,
    )
    classes = next(
        (
            value
            for name, value in named.items()
            if "class" in name or "label" in name
        ),
        None,
    )
    if boxes is None or scores is None or classes is None:
        return _postprocess_detection(next(iter(outputs.values())), spec, transform, config)
    box_rows = boxes.reshape(-1, 4)
    score_rows = scores.reshape(-1)
    class_rows = classes.reshape(-1)
    count = min(len(box_rows), len(score_rows), len(class_rows))
    count_tensor = next(
        (
            value
            for name, value in named.items()
            if "num" in name and ("det" in name or "box" in name)
        ),
        None,
    )
    if count_tensor is not None and count_tensor.size:
        count = min(count, max(int(count_tensor.reshape(-1)[0]), 0))
    combined = np.column_stack(
        (box_rows[:count], score_rows[:count], class_rows[:count])
    ).astype(np.float32, copy=False)
    return _postprocess_detection(combined, spec, transform, config)


def _postprocess_oriented_detection(
    output: np.ndarray,
    spec: ModelRuntimeSpec,
    transform: PreprocessTransform,
    config: dict[str, Any],
) -> dict[str, Any]:
    values = np.asarray(output, dtype=np.float32)
    while values.ndim > 2 and values.shape[0] == 1:
        values = values[0]
    if values.ndim != 2:
        raise EdgeInferenceError(
            f"Oriented detection output must be rank 2, received {values.shape}."
        )
    class_count = len(spec.class_map)
    feature_count = 5 + class_count
    if values.shape[0] == feature_count and values.shape[1] != feature_count:
        values = values.T
    threshold = float(
        config.get("confidence_threshold")
        or config.get("score_threshold")
        or spec.nms.get("score_threshold")
        or spec.postprocessing.get("confidence_threshold_default")
        or 0.25
    )
    angle_unit = str(spec.postprocessing.get("angle_unit") or "radians").lower()
    candidates: list[dict[str, Any]] = []
    for row in values:
        if row.size < 7:
            continue
        if class_count and row.size >= feature_count:
            class_scores = row[4 : 4 + class_count]
            class_id = int(np.argmax(class_scores))
            score = float(class_scores[class_id])
            angle = float(row[4 + class_count])
        else:
            score = float(row[4])
            class_id = int(row[5])
            angle = float(row[6])
        if score < threshold:
            continue
        angle_radians = math.radians(angle) if angle_unit in {"degree", "degrees"} else angle
        model_points = _rotated_box_points(
            float(row[0]),
            float(row[1]),
            max(float(row[2]), 0.0),
            max(float(row[3]), 0.0),
            angle_radians,
        )
        points = [_restore_point(point, transform) for point in model_points]
        x_values = [point[0] for point in points]
        y_values = [point[1] for point in points]
        candidates.append(
            {
                "class_id": class_id,
                "label": _class_label(spec, class_id),
                "confidence": score,
                "box": {
                    "x1": min(x_values),
                    "y1": min(y_values),
                    "x2": max(x_values),
                    "y2": max(y_values),
                },
                "oriented_box": {
                    "points": [{"x": x, "y": y} for x, y in points],
                    "rotation_degrees": math.degrees(angle_radians),
                },
            }
        )
    boxes = [
        [item["box"]["x1"], item["box"]["y1"], item["box"]["x2"], item["box"]["y2"]]
        for item in candidates
    ]
    scores = [float(item["confidence"]) for item in candidates]
    iou_threshold = float(
        config.get("nms_iou_threshold") or spec.nms.get("iou_threshold") or 0.65
    )
    keep = _nms(boxes, scores, iou_threshold)
    max_detections = int(config.get("max_detections") or spec.nms.get("max_detections") or 300)
    return {"detections": [candidates[index] for index in keep[:max_detections]]}


def _postprocess_segmentation(
    outputs: Mapping[str, np.ndarray],
    spec: ModelRuntimeSpec,
    transform: PreprocessTransform,
) -> dict[str, Any]:
    prototype: np.ndarray | None = None
    prediction: np.ndarray | None = None
    for output in outputs.values():
        candidate = np.asarray(output)
        if candidate.ndim == 4 and candidate.shape[-2] > 1 and candidate.shape[-1] > 1:
            prototype = candidate[0]
        elif prediction is None:
            prediction = candidate
    if prototype is not None and prediction is not None and spec.class_map:
        return _postprocess_instance_segmentation(prediction, prototype, spec, transform)
    values = np.asarray(next(iter(outputs.values())))
    if values.ndim == 4:
        values = values[0]
    if values.ndim == 3 and values.shape[0] > 1:
        mask = np.argmax(values, axis=0)
    elif values.ndim == 3:
        mask = (values[0] > 0.5).astype(np.int32)
    elif values.ndim == 2:
        mask = values.astype(np.int32)
    else:
        raise EdgeInferenceError(f"Segmentation output has unsupported shape {values.shape}.")
    segments: list[dict[str, Any]] = []
    for class_id in sorted(int(value) for value in np.unique(mask) if int(value) != 0):
        ys, xs = np.nonzero(mask == class_id)
        if xs.size == 0:
            continue
        x1, y1, x2, y2 = _restore_box(
            [float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())], transform
        )
        segments.append(
            {
                "class_id": class_id,
                "label": _class_label(spec, class_id),
                "area_pixels": int(xs.size),
                "polygon": [
                    {"x": x1, "y": y1},
                    {"x": x2, "y": y1},
                    {"x": x2, "y": y2},
                    {"x": x1, "y": y2},
                ],
            }
        )
    return {"segments": segments}


def _postprocess_instance_segmentation(
    prediction: np.ndarray,
    prototype: np.ndarray,
    spec: ModelRuntimeSpec,
    transform: PreprocessTransform,
) -> dict[str, Any]:
    values = np.asarray(prediction, dtype=np.float32)
    while values.ndim > 2 and values.shape[0] == 1:
        values = values[0]
    if values.ndim != 2 or prototype.ndim != 3:
        raise EdgeInferenceError("Instance segmentation tensors have unsupported shapes.")
    mask_count, mask_height, mask_width = prototype.shape
    class_count = len(spec.class_map)
    expected_width = 4 + class_count + mask_count
    if values.shape[0] == expected_width and values.shape[1] != expected_width:
        values = values.T
    if values.shape[1] < expected_width:
        raise EdgeInferenceError(
            "Instance segmentation prediction does not contain class and mask coefficients."
        )
    threshold = float(
        spec.nms.get("score_threshold")
        or spec.postprocessing.get("confidence_threshold_default")
        or 0.25
    )
    boxes: list[list[float]] = []
    scores: list[float] = []
    classes: list[int] = []
    coefficients: list[np.ndarray] = []
    for row in values:
        class_scores = row[4 : 4 + class_count]
        class_id = int(np.argmax(class_scores))
        score = float(class_scores[class_id])
        if score < threshold:
            continue
        center_x, center_y, width, height = (float(value) for value in row[:4])
        boxes.append(
            [
                center_x - width / 2,
                center_y - height / 2,
                center_x + width / 2,
                center_y + height / 2,
            ]
        )
        scores.append(score)
        classes.append(class_id)
        coefficients.append(row[4 + class_count : expected_width])
    keep = _nms(boxes, scores, float(spec.nms.get("iou_threshold") or 0.65))
    max_segments = int(spec.nms.get("max_detections") or 300)
    flattened_prototype = prototype.reshape(mask_count, -1)
    segments: list[dict[str, Any]] = []
    for index in keep[:max_segments]:
        logits = coefficients[index] @ flattened_prototype
        mask = (1.0 / (1.0 + np.exp(-np.clip(logits, -30, 30)))).reshape(mask_height, mask_width)
        ys, xs = np.nonzero(mask > 0.5)
        if xs.size:
            model_box = [
                float(xs.min()) * transform.model_width / mask_width,
                float(ys.min()) * transform.model_height / mask_height,
                float(xs.max()) * transform.model_width / mask_width,
                float(ys.max()) * transform.model_height / mask_height,
            ]
        else:
            model_box = boxes[index]
        x1, y1, x2, y2 = _restore_box(model_box, transform)
        segments.append(
            {
                "class_id": classes[index],
                "label": _class_label(spec, classes[index]),
                "confidence": scores[index],
                "area_pixels": int(xs.size),
                "polygon": [
                    {"x": x1, "y": y1},
                    {"x": x2, "y": y1},
                    {"x": x2, "y": y2},
                    {"x": x1, "y": y2},
                ],
            }
        )
    return {"segments": segments}


def _postprocess_anomaly(
    outputs: Mapping[str, np.ndarray],
    spec: ModelRuntimeSpec,
    config: dict[str, Any],
) -> dict[str, Any]:
    flattened = [np.asarray(value, dtype=np.float32).reshape(-1) for value in outputs.values()]
    score = float(max((float(np.max(value)) for value in flattened if value.size), default=0.0))
    threshold = float(
        config.get("threshold")
        or spec.postprocessing.get("confidence_threshold_default")
        or 0.5
    )
    return {"score": score, "is_anomaly": score >= threshold, "threshold": threshold}


def _nms(boxes: list[list[float]], scores: list[float], threshold: float) -> list[int]:
    order = sorted(range(len(boxes)), key=lambda index: scores[index], reverse=True)
    keep: list[int] = []
    while order:
        current = order.pop(0)
        keep.append(current)
        order = [index for index in order if _iou(boxes[current], boxes[index]) <= threshold]
    return keep


def _iou(left: list[float], right: list[float]) -> float:
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _restore_box(box: list[float], transform: PreprocessTransform) -> tuple[float, ...]:
    x1 = (box[0] - transform.pad_x) / transform.scale_x
    y1 = (box[1] - transform.pad_y) / transform.scale_y
    x2 = (box[2] - transform.pad_x) / transform.scale_x
    y2 = (box[3] - transform.pad_y) / transform.scale_y
    return (
        float(min(max(x1, 0.0), transform.original_width)),
        float(min(max(y1, 0.0), transform.original_height)),
        float(min(max(x2, 0.0), transform.original_width)),
        float(min(max(y2, 0.0), transform.original_height)),
    )


def _restore_point(
    point: tuple[float, float], transform: PreprocessTransform
) -> tuple[float, float]:
    x = (point[0] - transform.pad_x) / transform.scale_x
    y = (point[1] - transform.pad_y) / transform.scale_y
    return (
        float(min(max(x, 0.0), transform.original_width)),
        float(min(max(y, 0.0), transform.original_height)),
    )


def _rotated_box_points(
    center_x: float,
    center_y: float,
    width: float,
    height: float,
    angle_radians: float,
) -> list[tuple[float, float]]:
    cosine, sine = math.cos(angle_radians), math.sin(angle_radians)
    points: list[tuple[float, float]] = []
    for local_x, local_y in (
        (-width / 2, -height / 2),
        (width / 2, -height / 2),
        (width / 2, height / 2),
        (-width / 2, height / 2),
    ):
        points.append(
            (
                center_x + local_x * cosine - local_y * sine,
                center_y + local_x * sine + local_y * cosine,
            )
        )
    return points


def _input_layout(io_schema: dict[str, Any]) -> str:
    tensors = io_schema.get("input_tensors") or io_schema.get("inputs") or []
    if tensors and isinstance(tensors[0], dict):
        return str(tensors[0].get("layout") or "CHW").upper()
    return "CHW"


def _class_label(spec: ModelRuntimeSpec, class_id: int) -> str:
    if 0 <= class_id < len(spec.class_map):
        return spec.class_map[class_id]
    return str(class_id)


def _read_class_map(digest: Any, artifact_resolver: Callable[[str], Path]) -> tuple[str, ...]:
    if not digest:
        return ()
    try:
        value = json.loads(artifact_resolver(str(digest)).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise EdgeInferenceError("Model class-map artifact is invalid.") from exc
    if isinstance(value, dict):
        value = value.get("classes") or value.get("class_map") or value.get("labels") or value
        if isinstance(value, dict):
            return tuple(
                str(label) for _, label in sorted(value.items(), key=lambda item: int(item[0]))
            )
    if not isinstance(value, list):
        raise EdgeInferenceError("Model class-map artifact must contain a label list.")
    return tuple(str(item) for item in value)


def _tensor_summary(value: np.ndarray) -> dict[str, Any]:
    array = np.asarray(value)
    return {
        "shape": list(array.shape),
        "dtype": str(array.dtype),
        "minimum": float(np.min(array)) if array.size else None,
        "maximum": float(np.max(array)) if array.size else None,
    }


def _read_expected_arrays(path: Path) -> dict[str, np.ndarray]:
    try:
        loaded = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise EdgeInferenceError(
            "Golden expected output must be a NumPy .npy/.npz artifact or normalized JSON."
        ) from exc
    if isinstance(loaded, np.lib.npyio.NpzFile):
        try:
            return {name: np.asarray(loaded[name]) for name in loaded.files}
        finally:
            loaded.close()
    return {"output": np.asarray(loaded)}


def _json_allclose(actual: Any, expected: Any, *, atol: float, rtol: float) -> bool:
    if isinstance(actual, dict) and isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            _json_allclose(actual[key], expected[key], atol=atol, rtol=rtol) for key in actual
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _json_allclose(left, right, atol=atol, rtol=rtol)
            for left, right in zip(actual, expected, strict=True)
        )
    if isinstance(actual, int | float) and isinstance(expected, int | float):
        return math.isclose(float(actual), float(expected), abs_tol=atol, rel_tol=rtol)
    return actual == expected


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _object_field(raw: dict[str, Any], key: str, node_key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise EdgeInferenceError(f"Model {node_key} field {key} must be an object.")
    return dict(value)
