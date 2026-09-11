# ruff: noqa: ANN001, ANN205 - the fake mirrors TensorRT's dynamically typed API

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from edge_agent.engine import _build_with_tensorrt_python


def test_tensorrt_python_builder_parses_onnx_and_applies_dynamic_shape(
    tmp_path, monkeypatch
) -> None:
    observed: dict[str, object] = {}

    class FakeTensor:
        name = "images"
        shape = (-1, 3, -1, -1)

    class FakeNetwork:
        num_inputs = 1

        @staticmethod
        def get_input(_index: int) -> FakeTensor:
            return FakeTensor()

    class FakeParser:
        num_errors = 0

        def __init__(self, network, _logger) -> None:
            observed["network"] = network

        @staticmethod
        def parse(payload: bytes) -> bool:
            observed["onnx"] = payload
            return True

    class FakeProfile:
        @staticmethod
        def set_shape(name, minimum, optimum, maximum) -> bool:
            observed["shape"] = (name, minimum, optimum, maximum)
            return True

    class FakeConfig:
        def set_flag(self, flag) -> None:
            observed.setdefault("flags", []).append(flag)

        @staticmethod
        def add_optimization_profile(profile) -> None:
            observed["profile"] = profile

    class FakeBuilder:
        def __init__(self, _logger) -> None:
            return None

        @staticmethod
        def create_network(flags):
            observed["network_flags"] = flags
            return FakeNetwork()

        @staticmethod
        def create_builder_config() -> FakeConfig:
            return FakeConfig()

        @staticmethod
        def create_optimization_profile() -> FakeProfile:
            return FakeProfile()

        @staticmethod
        def build_serialized_network(network, config) -> bytes:
            observed["built"] = (network, config)
            return b"serialized-engine"

    class FakeLogger:
        ERROR = 1

        def __init__(self, severity) -> None:
            observed["severity"] = severity

    fake = ModuleType("tensorrt")
    fake.Logger = FakeLogger
    fake.Builder = FakeBuilder
    fake.OnnxParser = FakeParser
    fake.NetworkDefinitionCreationFlag = SimpleNamespace(EXPLICIT_BATCH=0)
    fake.BuilderFlag = SimpleNamespace(FP16="fp16", INT8="int8")
    fake.IInt8EntropyCalibrator2 = object
    fake.init_libnvinfer_plugins = lambda _logger, namespace: observed.update(
        {"plugin_namespace": namespace}
    )
    monkeypatch.setitem(sys.modules, "tensorrt", fake)

    onnx_path = tmp_path / "model.onnx"
    engine_path = tmp_path / "model.plan"
    onnx_path.write_bytes(b"onnx-fixture")
    shape = (1, 3, 640, 640)

    _build_with_tensorrt_python(
        onnx_path=onnx_path,
        destination=engine_path,
        precision="fp16",
        input_shapes={"images": shape},
        calibration_path=None,
    )

    assert observed["onnx"] == b"onnx-fixture"
    assert observed["flags"] == ["fp16"]
    assert observed["shape"] == ("images", shape, shape, shape)
    assert engine_path.read_bytes() == b"serialized-engine"
