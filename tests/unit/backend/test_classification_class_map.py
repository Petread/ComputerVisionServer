from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from cv_deploy.packaging import (
    _classification_labels_from_model_names,
    _existing_package_matches,
    normalize_classification_export_manifest,
    sha256_bytes,
)


def _model(tmp_path: Path, *, names: str | None, count: int = 4) -> Path:
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    graph = helper.make_graph(
        [helper.make_node("Identity", ["images"], ["scores"])],
        "classification",
        [helper.make_tensor_value_info("images", TensorProto.FLOAT, ["batch", count])],
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, ["batch", count])],
    )
    model = helper.make_model(graph)
    if names is not None:
        helper.set_model_props(model, {"names": names})
    path = tmp_path / "model.onnx"
    onnx.save(model, str(path))
    return path


def _manifest() -> dict[str, Any]:
    return {
        "task_type": "classification",
        "trainer_backend": "ultralytics-yolo",
        "source_snapshot_id": "snapshot-original",
        "class_count": 5,
        "class_map_sha256": "old-hash",
        "class_map_version": "snapshot-original:old-hash",
        "postprocessing": {"class_map": ["OK", "new class", "A/B", "A_B", "bad"]},
    }


def test_model_class_order_excludes_unused_middle_class_and_preserves_original_labels(
    tmp_path: Path,
) -> None:
    path = _model(
        tmp_path, names="{3: '004_bad', 1: '002_A_B', 0: '000_OK', 2: '003_A_B'}",
    )
    original = _manifest()
    before = copy.deepcopy(original)
    result = normalize_classification_export_manifest(original, path)

    assert original == before
    assert result["postprocessing"]["class_map"] == ["OK", "A/B", "A_B", "bad"]
    assert result["postprocessing"]["training_class_map"] == before["postprocessing"]["class_map"]
    assert result["class_count"] == 4
    digest = sha256_bytes(json.dumps(["OK", "A/B", "A_B", "bad"], indent=2).encode())
    assert result["class_map_sha256"] == digest
    assert result["class_map_version"] == f"snapshot-original:{digest[:12]}"
    # Repackaging an already corrected export must not reinterpret the original folder indices.
    assert normalize_classification_export_manifest(result, path) == result


def test_equal_class_counts_still_follow_model_output_order(tmp_path: Path) -> None:
    manifest = _manifest()
    path = _model(tmp_path, names="['bad', 'A_B', 'A/B', 'new class', 'OK']", count=5)
    result = normalize_classification_export_manifest(manifest, path)
    assert result["postprocessing"]["class_map"] == ["bad", "A_B", "A/B", "new class", "OK"]
    assert result["class_map_sha256"] != manifest["class_map_sha256"]


@pytest.mark.parametrize("names", [None, "['OK', 'new class', 'A/B', 'A_B', 'bad']"])
def test_output_mismatch_without_proven_mapping_is_rejected(
    tmp_path: Path, names: str | None,
) -> None:
    with pytest.raises(ValueError, match="4 outputs.*5 labels"):
        normalize_classification_export_manifest(_manifest(), _model(tmp_path, names=names))


@pytest.mark.parametrize(
    "names",
    ["{1: 'OK'}", "['unknown']", "['OK', 'OK']", "[]", "{0: 123}", "not metadata"],
)
def test_invalid_model_labels_are_rejected(names: str) -> None:
    with pytest.raises(ValueError):
        _classification_labels_from_model_names(names, ["OK", "bad"])


def test_string_indices_are_sorted_numerically() -> None:
    assert _classification_labels_from_model_names(
        '{"1": "001_bad", "0": "000_OK"}', ["OK", "bad"],
    ) == ["OK", "bad"]


def test_other_classification_backends_keep_their_training_map(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["trainer_backend"] = "torchvision-classification"
    path = _model(tmp_path, names="['unrelated metadata']", count=5)
    assert normalize_classification_export_manifest(manifest, path) == manifest


def test_cached_package_with_same_shape_but_wrong_labels_is_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "class_map.json"
    path.write_text(json.dumps(["bad", "OK"]), encoding="utf-8")
    manifest = {
        "package_root": str(tmp_path),
        "io_schema": {},
        "preprocessing": {},
        "hardware_compatibility": {},
        "class_count": 2,
        "postprocessing": {"class_map": ["OK", "bad"]},
        "source_manifests": {"class_map": {"path": path.name}},
    }
    kwargs = {"io_schema": {}, "preprocessing": {}, "hardware_compatibility": {}}
    assert not _existing_package_matches(manifest, class_map=["OK", "bad"], **kwargs)
    path.write_text(json.dumps(["OK", "bad"]), encoding="utf-8")
    assert _existing_package_matches(manifest, class_map=["OK", "bad"], **kwargs)
    assert not _existing_package_matches(manifest, class_map=["bad", "OK"], **kwargs)
