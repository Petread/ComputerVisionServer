from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import numpy as np

from edge_agent.engine import _build_with_tensorrt_python
from edge_agent.inference import TensorRtEngineSession

_IDENTITY_ONNX_BASE64 = (
    "CAgSCHliYi1lZGdlOmIKGQoFaW5wdXQSBm91dHB1dCIISWRlbnRpdHkSEnliYl90ZW5z"
    "b3JydF9zbW9rZVoXCgVpbnB1dBIOCgwIARIICgIIAQoCCARiGAoGb3V0cHV0Eg4KDAgB"
    "EggKAggBCgIIBEIECgAQEQ=="
)


def run_smoke_test() -> dict[str, object]:
    """Build, deserialize, and execute a minimal TensorRT engine on the local GPU."""
    try:
        import tensorrt as trt  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Bundled TensorRT Python package cannot be imported.") from exc

    with tempfile.TemporaryDirectory(prefix="ybb-tensorrt-smoke-") as temporary:
        onnx_path = Path(temporary) / "identity.onnx"
        engine_path = Path(temporary) / "identity.plan"
        onnx_path.write_bytes(base64.b64decode(_IDENTITY_ONNX_BASE64))
        _build_with_tensorrt_python(
            onnx_path=onnx_path,
            destination=engine_path,
            precision="fp32",
            input_shapes={},
            calibration_path=None,
        )
        session = TensorRtEngineSession(engine_path)
        try:
            expected = np.asarray([[1.25, -2.5, 3.75, 8.0]], dtype=np.float32)
            outputs = session.infer({"input": expected})
        finally:
            session.close()
    actual = outputs.get("output")
    if actual is None or not np.array_equal(actual, expected):
        raise RuntimeError("TensorRT smoke inference returned an unexpected result.")
    return {
        "status": "passed",
        "tensorrt_version": str(trt.__version__),
        "input_shape": list(expected.shape),
        "output_shape": list(actual.shape),
    }


def main() -> int:
    print(json.dumps(run_smoke_test(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
