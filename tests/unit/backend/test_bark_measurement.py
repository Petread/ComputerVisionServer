from __future__ import annotations

import base64
import io
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from cv_contracts.bark import BarkRadialSample, BarkSideMeasurement, StemBarkJob
from cv_contracts.inference import RealtimeSegmentRequest, SegmentationPrediction
from cv_contracts.runtime import (
    RuntimeLogicalModelRef,
    RuntimePipelinePreviewRequest,
    RuntimeTriggerExecutionRequest,
)
from cv_db.bark_artifacts import bark_artifact_path, remove_bark_artifacts, store_bark_artifacts
from cv_db.pipeline_store import (
    _bark_measurement_template_definition,
    _template_node,
    validate_pipeline_graph,
)
from cv_runtime.bark_measurement import (
    parse_stem_csv,
    project,
    radial_samples,
    register_contours,
    resample_contour,
    summarize_samples,
)
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.engine import ExecutablePipelineGraph
from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.nodes.bark import (
    BarkPairHandler,
    EndfaceRectifyHandler,
    EndfaceSplitHandler,
    StemCsvHandler,
)
from cv_runtime.nodes.builtin import PreparedInferenceImage, _restore_segmentation_geometry
from cv_runtime.payloads import BarkMeasurementPayload, FramePayload, SegmentPayload, TriggerPayload
from cv_runtime.registry import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.segmentation_masks import decode_mask, encode_mask
from cv_status import RuntimeMode, TaskType
from PIL import Image
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from tests.support.runtime_model_seed import seed_deployed_runtime_model


def contour(radius: float = 100) -> np.ndarray:
    angles = np.arange(36) * 2 * np.pi / 36
    radii = radius + 9 * np.sin(3 * angles) + 7 * np.cos(5 * angles) + 4 * np.sin(7 * angles)
    return np.c_[radii * np.cos(angles), radii * np.sin(angles)]


def csv_text(points: np.ndarray | None = None) -> str:
    points = contour() if points is None else points
    values = ";".join(f"{v:.4f}" for v in points.reshape(-1))
    return "\n".join(
        [
            "HFA_KRUEMMUNG_V1",
            "FIRMA;ME1",
            "11;18.10.2016;13:11:45",
            "1000.0;1000.0;Z;0",
            "305.0;310.0;347.0;8.0;27.9",
            "0.420;0.447",
            f"0.0;{values}",
            f"100.0;{values}",
        ]
    )


def context(job: dict | None = None) -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="test-bark",
        pipeline_version_id="test-version",
        mode=RuntimeMode.TRIGGER,
        trigger=TriggerPayload(
            trigger_id="test-job",
            body={"bark_job": job or StemBarkJob(csv_text=csv_text()).model_dump()},
        ),
    )


def split_outputs(
    mapping: str = "", override: str | None = None, mirror: bool = False, rotation: str = "0"
) -> dict:
    job = StemBarkJob(csv_text=csv_text(), side_mapping=override)
    ctx = context(job.model_dump())
    csv_node = _template_node("csv_input", "source.stem_csv_measurements", 0, 0)
    measurements = StemCsvHandler().execute(ctx, csv_node, {}).outputs["measurement"]
    node = _template_node("split", "preprocessing.endface_pair_split", 0, 0)
    node = node.model_copy(
        update={
            "config": {
                **node.config,
                "side_mapping": mapping,
                "left_mirror": mirror,
                "left_rotation": rotation,
            }
        }
    )
    pixels = np.zeros((80, 200, 3), dtype=np.uint8)
    pixels[:, :50] = [255, 0, 0]
    pixels[:, 100:] = [0, 255, 0]
    frame = FramePayload.from_image(
        Image.fromarray(pixels), image_format="PNG", mime_type="image/png"
    )
    return (
        EndfaceSplitHandler()
        .execute(ctx, node, {"frame": frame, "measurement": measurements})
        .outputs
    )


def test_csv_units_headers_and_exact_end_rows() -> None:
    data = parse_stem_csv("\ufeff" + csv_text())
    assert data.workpiece_id == "11"
    assert data.leading_end == "Z"
    assert data.slices[-1].position_mm == 1000
    assert [s.csv_line for s in data.slices] == [7, 8]
    assert len(data.slices[0].points_mm) == 36
    assert data.slices[0].points_mm[0][0] == pytest.approx(107)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda s: s.replace("100.0;", "0.0;"), "strictly_increasing"),
        (lambda s: s.replace("100.0;", "101.0;"), "outside_length"),
        (lambda s: s.replace("107.0000;", "nan;", 1), "non_finite"),
        (lambda s: s + ";1", "73_values"),
        (lambda s: s.replace("HFA_KRUEMMUNG_V1", "unknown"), "unsupported"),
    ],
)
def test_csv_rejects_invalid_data_without_substitution(
    mutation: Callable[[str], str], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_stem_csv(mutation(csv_text()))


def test_csv_rejects_self_intersection() -> None:
    points = contour()
    points[[3, 19]] = points[[19, 3]]
    with pytest.raises(ValueError, match="self_intersection"):
        parse_stem_csv(csv_text(points))


@pytest.mark.parametrize("mapping,first_side", [("left_first", "left"), ("left_last", "right")])
def test_explicit_side_mapping(mapping: str, first_side: str) -> None:
    outputs = split_outputs(mapping)
    for side in ("left", "right"):
        reference = outputs[f"{side}_reference"].reference
        assert reference.side == side
        assert reference.slice_role == ("first" if side == first_side else "last")
        assert reference.slice.csv_line == (7 if side == first_side else 8)
        assert reference.mapping_source == "pipeline"


def test_missing_mapping_fails_and_job_override_preserves_configuration() -> None:
    with pytest.raises(RuntimeNodeExecutionError, match="Zuordnung"):
        split_outputs()
    result = split_outputs("left_first", "left_last")
    assert result["left_reference"].reference.slice_role == "last"
    assert result["left_reference"].reference.mapping_source == "job"
    assert split_outputs("left_first")["left_reference"].reference.slice_role == "first"


def test_rotation_mirror_and_image_lineage() -> None:
    result = split_outputs("left_first", mirror=True, rotation="90")
    frame = result["left_frame"]
    assert (frame.width, frame.height) == (80, 100)
    matrix = frame.image_context.transform.parameters["source_to_view_homography"]
    assert np.allclose(project([[0, 0], [99, 79]], matrix), [[0, 0], [79, 99]])
    assert frame.image_context.parent_image_context_id is not None


def test_mask_holes_components_and_letterbox_restoration() -> None:
    mask = np.zeros((16, 20), dtype=bool)
    mask[3:13, 3:17] = True
    mask[6:10, 7:13] = False
    mask[1, 1] = True
    encoded = encode_mask(mask)
    assert np.array_equal(decode_mask(encoded), mask)
    prepared = PreparedInferenceImage(b"", "image/png", 16, 12, 20, 16, 1, 2, 2, True)
    restored = _restore_segmentation_geometry(
        [SegmentationPrediction(label="bark", score=1, mask=encoded)], prepared
    )[0]
    assert np.array_equal(decode_mask(restored.mask), mask[2:14, 2:18])
    assert restored.mask.native_width == 16


def test_stretched_upscale_restores_entire_mask_without_cropping() -> None:
    mask = np.zeros((32, 40), dtype=bool)
    mask[:, 20:] = True
    prepared = PreparedInferenceImage(b"", "image/png", 20, 16, 40, 32, letterboxed=False)
    restored = _restore_segmentation_geometry(
        [SegmentationPrediction(label="wood", score=1, mask=encode_mask(mask))], prepared
    )[0]
    actual = decode_mask(restored.mask)
    assert actual.shape == (16, 20)
    assert not actual[:, :10].any()
    assert actual[:, 10:].all()


def test_semantic_logits_keep_ring_and_background_offset() -> None:
    from inference_gateway.runtime.service import _lossless_semantic_predictions

    winners = np.zeros((24, 24), dtype=int)
    winners[3:21, 3:21] = 2
    winners[7:17, 7:17] = 1
    logits = np.full((1, 4, 24, 24), -10, dtype=np.float32)
    for index in range(4):
        logits[0, index][winners == index] = 10
    route = SimpleNamespace(class_labels=["wood", "bark", "unknown"])
    tensor = SimpleNamespace(shape=logits.shape, values=logits.reshape(-1).tolist())
    predictions = _lossless_semantic_predictions(route, tensor, 10, 0.8)
    by_label = {p.label: decode_mask(p.mask) for p in predictions}
    assert np.array_equal(by_label["wood"], winners == 1)
    assert np.array_equal(by_label["bark"], winners == 2)
    assert not by_label["bark"][12, 12]
    assert not np.any(by_label["bark"] & by_label["wood"])


def ring_masks(thickness: float = 5) -> tuple[np.ndarray, ...]:
    y, x = np.mgrid[:440, :440]
    radius = np.hypot(x - 220, y - 220) * 0.25
    outer, wood = radius <= 50, radius <= 50 - thickness
    return outer, wood, outer & ~wood, np.zeros_like(outer)


@pytest.mark.parametrize("thickness", [0, 1, 5, 12])
def test_radial_ring_and_true_zero(thickness: float) -> None:
    samples = radial_samples(*ring_masks(thickness), center_px=(220, 220), mm_per_pixel=0.25)
    result = summarize_samples(samples, include_extrema=True)
    assert result["valid"]
    assert result["mean_mm"] == pytest.approx(thickness, abs=0.15)
    if thickness == 0:
        assert result["min_mm"] == result["max_mm"] == 0


def test_occlusion_and_crack_are_not_zero_or_bark() -> None:
    outer, wood, bark, unknown = ring_masks()
    wood[200:210, 200:240] = False  # an interior crack must not become the wood edge
    samples = radial_samples(outer, wood, bark, unknown, center_px=(220, 220), mm_per_pixel=0.25)
    assert summarize_samples(samples)["mean_mm"] == pytest.approx(5, abs=0.15)
    unknown[:220, 380:] = outer[:220, 380:]
    result = summarize_samples(
        radial_samples(outer, wood, bark, unknown, center_px=(220, 220), mm_per_pixel=0.25)
    )
    assert not result["valid"]
    assert result["mean_mm"] is None


def test_pair_means_weight_sides_equally_and_require_both() -> None:
    refs = split_outputs("left_first")
    left = BarkSideMeasurement(reference=refs["left_reference"].reference, valid=True, mean_mm=2)
    right = BarkSideMeasurement(reference=refs["right_reference"].reference, valid=True, mean_mm=8)
    node = _template_node("pair", "analysis.bark_pair_result", 0, 0)
    inputs = {
        "left": BarkMeasurementPayload(measurement=left),
        "right": BarkMeasurementPayload(measurement=right),
    }
    assert (
        BarkPairHandler().execute(context(), node, inputs).outputs["result"].result["mean_mm"] == 5
    )
    inputs["right"].measurement = right.model_copy(update={"valid": False, "mean_mm": None})
    result = BarkPairHandler().execute(context(), node, inputs).outputs["result"].result
    assert result["mean_mm"] is None and result["left"]["mean_mm"] == 2


@pytest.mark.parametrize("scale", [2, 4])
def test_registration_known_projective_map(scale: float) -> None:
    metric = contour()
    matrix = np.array([[scale, 0.1, 440], [0.1, scale * 0.85, 410], [0.0006, -0.0003, 1]])
    fit = register_contours(resample_contour(project(metric, matrix), 600), metric)
    assert fit.valid, (fit.reasons, fit.error_mm, fit.stability_mm)
    probes = metric * 0.6
    assert (
        np.max(np.linalg.norm(project(project(probes, matrix), fit.image_to_mm) - probes, axis=1))
        < 0.5
    )


def test_unmarked_ellipse_is_not_a_metric_calibration() -> None:
    angles = np.arange(36) * 2 * np.pi / 36
    ellipse = np.c_[100 * np.cos(angles), 80 * np.sin(angles)]
    fit = register_contours(ellipse * 3 + 500, ellipse)
    assert not fit.valid and "registration_outline_ambiguous" in fit.reasons


def test_template_ports_and_preview_contract(sqlite_engine: Engine) -> None:
    template = _bark_measurement_template_definition()
    graph = template["graph"]
    by_key = {n.node_key: n for n in graph.nodes}
    assert template["mode"] == RuntimeMode.TRIGGER
    assert by_key["split_faces"].config["side_mapping"] == ""
    assert by_key["bark_trigger"].config["timeout_ms"] == 120000
    assert len([n for n in graph.nodes if n.node_type == "inference.segmentation"]) == 4
    for edge in graph.edges:
        source = {p.key: p.data_type for p in by_key[edge.from_node_key].output_ports}
        target = {p.key: p.data_type for p in by_key[edge.to_node_key].input_ports}
        assert source[edge.from_port] == target[edge.to_port]
    request = RuntimePipelinePreviewRequest(graph=graph, bark_job=StemBarkJob(csv_text=csv_text()))
    assert request.bark_job.csv_text == csv_text()
    for node in graph.nodes:
        assert node.node_type in BUILTIN_NODE_TYPE_BY_KEY
    with Session(sqlite_engine) as session:
        report = validate_pipeline_graph(session, mode=RuntimeMode.TRIGGER, graph=graph)
    assert not {
        "source_count_invalid",
        "trigger_entry_not_connected",
        "source_has_input",
        "unknown_output_port",
        "unknown_input_port",
        "model_binding_group_mismatch",
    } & {issue.code for issue in report.issues}, report
    bound = graph.model_copy(deep=True)
    with Session(sqlite_engine) as session:
        seed_deployed_runtime_model(session, task_type=TaskType.SEGMENTATION)
        for node in bound.nodes:
            if node.node_type == "preprocessing.endface_pair_split":
                node.config["side_mapping"] = "left_first"
            if node.node_type == "inference.segmentation":
                node.config.pop("template_model_unbound", None)
                node.logical_model_ref = RuntimeLogicalModelRef(
                    model_family_slug="defect-detector",
                    target_path="realtime",
                    required_task_type="segmentation",
                    selection_policy="active_production",
                )
        validated = validate_pipeline_graph(session, mode=RuntimeMode.TRIGGER, graph=bound)
        assert not [issue for issue in validated.issues if issue.severity == "error"], (
            validated.issues
        )
    assert RuntimeTriggerExecutionRequest(timeout_ms=120000).timeout_ms == 120000


def test_training_preview_can_stop_after_split_before_any_model_is_bound() -> None:
    from runtime_admin_api.api.routes import _preview_graph_through_selected_node

    graph = _bark_measurement_template_definition()["graph"].model_copy(deep=True)
    graph = _preview_graph_through_selected_node(graph, "split_faces")
    assert not any(node.node_type.startswith("inference.") for node in graph.nodes)
    ctx = context(StemBarkJob(csv_text=csv_text(), side_mapping="left_first").model_dump())
    ctx.input_frame = FramePayload.from_image(
        Image.new("RGB", (200, 80)), image_format="PNG", mime_type="image/png"
    )
    result = ExecutablePipelineGraph.build(
        pipeline_version_id="preview",
        graph=graph,
        handler_refs={
            key: node.runtime_handler_ref for key, node in BUILTIN_NODE_TYPE_BY_KEY.items()
        },
    ).execute(ctx)
    assert result.status == "completed", result.errors
    assert result.node_outputs["split_faces"]["left_frame"]["image_base64"]
    assert result.node_outputs["split_faces"]["right_frame"]["image_base64"]


def test_clipped_endface_returns_invalid_calibration_without_reusing_previous_job() -> None:
    outputs = split_outputs("left_first")
    frame = outputs["left_frame"]
    mask = np.ones((frame.height, frame.width), dtype=bool)
    segments = SegmentPayload(
        image_context_id=frame.image_context.context_id,
        segments=[SegmentationPrediction(label="endface", score=1, mask=encode_mask(mask))],
    )
    node = _template_node("metric", "preprocessing.endface_metric_rectify", 0, 0)
    result = EndfaceRectifyHandler().execute(
        context(),
        node,
        {
            "frame": frame,
            "reference": outputs["left_reference"],
            "segments": segments,
        },
    )
    assert not result.outputs["calibration"].valid
    assert result.outputs["calibration"].reason_codes == ["endface_clipped"]
    assert result.outputs["calibration"].registration_hash is None
    assert result.outputs["eligibility"].result["eligible"] is False
    assert result.outputs["eligibility"].result["reason_codes"] == ["endface_clipped"]


def test_wrong_csv_shape_cannot_be_accepted_by_scaling() -> None:
    metric = contour()
    wrong = metric.copy()
    wrong[4:11] *= 1.25
    fit = register_contours(resample_contour(metric * 4 + 500, 600), wrong)
    assert not fit.valid


def test_varying_bark_with_bare_sector_includes_zero_in_mean() -> None:
    y, x = np.mgrid[:440, :440]
    angles = np.arctan2(y - 220, x - 220)
    radius = np.hypot(x - 220, y - 220) * 0.25
    thickness = np.where(np.abs(angles) < np.pi / 6, 0, 4 + 2 * np.sin(angles))
    outer, wood = radius <= 50, radius <= 50 - thickness
    samples = radial_samples(
        outer, wood, outer & ~wood, np.zeros_like(outer), center_px=(220, 220), mm_per_pixel=0.25
    )
    result = summarize_samples(samples, include_extrema=True)
    assert result["valid"]
    assert result["min_mm"] == 0
    assert result["max_mm"] == pytest.approx(6, abs=0.2)
    assert result["mean_mm"] == pytest.approx(4 * 5 / 6, abs=0.15)


def test_cyclic_invalid_gap_is_enforced_even_above_ninety_percent_valid() -> None:
    samples = [BarkRadialSample(angle_deg=i, valid=True, thickness_mm=0) for i in range(360)]
    for index in [*range(355, 360), *range(6)]:
        samples[index] = BarkRadialSample(angle_deg=index, valid=False)
    result = summarize_samples(samples)
    assert result["valid_fraction"] > 0.9
    assert result["maximum_invalid_gap_deg"] == 11
    assert result["mean_mm"] is None
    assert result["reason_codes"] == ["excessive_invalid_angular_gap"]


def test_complete_photo_csv_pipeline_and_retained_evidence(tmp_path: Path) -> None:
    """Run the actual graph with deterministic image-based segmentation models."""
    metric = contour()
    photo = np.zeros((1100, 2200, 3), dtype=np.uint8)
    for offset, scale in ((0, 4), (1100, 3.5)):
        transform = np.array([[scale, 0.1, 550 + offset], [0.1, scale, 550], [0, 0, 1]])
        wood = metric * (1 - 5 / np.linalg.norm(metric, axis=1))[:, None]
        cv2.fillPoly(photo, [np.rint(project(metric, transform)).astype(np.int32)], (255, 0, 0))
        cv2.fillPoly(photo, [np.rint(project(wood, transform)).astype(np.int32)], (0, 255, 0))
    frame = FramePayload.from_image(
        Image.fromarray(photo), image_format="PNG", mime_type="image/png"
    )

    class ImageSegmentationClient:
        def segment(self, request: RealtimeSegmentRequest) -> SimpleNamespace:
            assert request.return_masks
            image = np.asarray(Image.open(io.BytesIO(base64.b64decode(request.image_base64))))
            wood_mask = image[:, :, 1] > image[:, :, 0]
            bark_mask = image[:, :, 0] > image[:, :, 1]
            masks = (
                {"endface": wood_mask | bark_mask}
                if request.deployment_release_id == "outline"
                else {
                    "wood": wood_mask,
                    "bark": bark_mask,
                }
            )
            return SimpleNamespace(
                segments=[
                    SegmentationPrediction(label=label, score=1, mask=encode_mask(mask))
                    for label, mask in masks.items()
                ],
                route=SimpleNamespace(model_dump=lambda **_: {}),
            )

    graph = _bark_measurement_template_definition()["graph"].model_copy(deep=True)
    for node in graph.nodes:
        if node.node_type == "preprocessing.endface_pair_split":
            node.config["side_mapping"] = "left_last"
        if node.node_type == "inference.segmentation":
            stage = "outline" if node.node_key.endswith("outline") else "semantic"
            node.config.pop("template_model_unbound", None)
            node.logical_model_ref = RuntimeLogicalModelRef(
                model_family_slug=stage,
                target_path="realtime",
                contract_major=1,
                selection_policy="explicit_release",
                deployment_release_id=stage,
                required_task_type="segmentation",
            )
    job = StemBarkJob(csv_text=csv_text())
    ctx = context(job.model_dump())
    ctx.input_frame = frame
    ctx.inference_client = ImageSegmentationClient()
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="test-version",
        graph=graph,
        handler_refs={
            key: node.runtime_handler_ref for key, node in BUILTIN_NODE_TYPE_BY_KEY.items()
        },
    )
    result = executable.execute(ctx)
    assert result.status == "completed", result.errors
    for node_key in ("left_metric", "right_metric"):
        outputs = result.node_outputs[node_key]
        assert outputs["eligibility"]["result"]["eligible"] is True
        assert (
            outputs["eligibility"]["result"]["image_context_id"]
            == outputs["frame"]["image_context"]["context_id"]
        )
    assert result.final_response["valid"], result.final_response
    assert result.final_response["mean_mm"] == pytest.approx(5, abs=0.5)
    assert result.final_response["left"]["reference"]["slice_role"] == "last"
    assert result.final_response["left"]["reference"]["slice"]["csv_line"] == 8
    assert result.final_response["right"]["model_versions"]["semantic"] == "semantic"
    assert result.final_response["metrology_status"] == "not_validated"
    assert len(result.final_response["left"]["samples"]) == 360
    assert result.persisted_results
    settings = SimpleNamespace(storage=SimpleNamespace(runtime_results_root=lambda: tmp_path))
    row = SimpleNamespace(id="bark-result-test", runtime_metadata={})
    store_bark_artifacts(settings, row, result, frame, job)
    assert bark_artifact_path(settings, row, "measurement.csv").read_text() == job.csv_text
    assert bark_artifact_path(settings, row, "left_classes.class-0.png").is_file()
    assert "csv_text" not in row.runtime_metadata["bark_job"]
    with pytest.raises(ValueError, match="manifest"):
        bark_artifact_path(settings, row, "../measurement.csv")
    foreign = tmp_path / "bark-result-test" / "foreign.txt"
    foreign.write_text("keep")
    remove_bark_artifacts(settings, row)
    assert foreign.read_text() == "keep"
