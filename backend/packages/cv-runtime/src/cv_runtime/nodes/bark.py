"""Stateless end-face inspection nodes using existing runtime frame and model paths."""

# ruff: noqa: ANN401
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import numpy as np
from cv_contracts.bark import (
    BarkMeasurementResult,
    BarkSideMeasurement,
    EndfaceReference,
    StemBarkJob,
)
from cv_contracts.runtime import RuntimeNodeSpec
from PIL import Image

from cv_runtime.bark_measurement import (
    parse_stem_csv,
    polygon_centroid,
    project,
    radial_samples,
    register_contours,
    summarize_samples,
)
from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.nodes.builtin import (
    _ensure_frame_context,
    _frame_from_image,
    _open_frame,
    _required_frame,
)
from cv_runtime.payloads import (
    BarkMeasurementPayload,
    EndfaceCalibrationPayload,
    EndfaceReferencePayload,
    FramePayload,
    ResultPayload,
    RuntimeImageContext,
    RuntimeImageTransform,
    SegmentPayload,
    StemCsvPayload,
)
from cv_runtime.segmentation_masks import decode_mask, encode_mask


def _error(node: RuntimeNodeSpec, message: str) -> RuntimeNodeExecutionError:
    return RuntimeNodeExecutionError(
        code="bark_input_invalid", message=message, node_key=node.node_key, node_type=node.node_type
    )


def _derived(
    source: FramePayload,
    node: RuntimeNodeSpec,
    image: Any,
    suffix: str,
    matrix: Any,
    metadata: dict[str, Any] | None = None,
) -> FramePayload:
    image = Image.fromarray(np.asarray(image).astype(np.uint8))
    parent = source.image_context.context_id if source.image_context else "original"
    return _frame_from_image(
        image,
        source=source,
        image_format="PNG",
        mime_type="image/png",
        metadata={"bark_stage": suffix, **(metadata or {})},
        image_context=RuntimeImageContext(
            context_id=f"{node.node_key}-{suffix}-{hashlib.sha256(parent.encode()).hexdigest()[:12]}",
            kind="transformed",
            width=image.width,
            height=image.height,
            source_node_key=node.node_key,
            source_image_context_id=parent,
            parent_image_context_id=parent,
            transform=RuntimeImageTransform(
                transform_type="perspective_rectify",
                coordinate_projection="homography",
                parameters={
                    "source_to_view_homography": np.asarray(matrix).reshape(-1).tolist(),
                    "view_to_source_homography": np.linalg.inv(matrix).reshape(-1).tolist(),
                },
            ),
            metadata=metadata or {},
        ),
    )


class StemCsvHandler:
    node_type = "source.stem_csv_measurements"

    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        body = context.trigger.body if context.trigger else {}
        try:
            if node.config.get("format", "HFA_KRUEMMUNG_V1") != "HFA_KRUEMMUNG_V1":
                raise ValueError("csv_format_unsupported")
            job = StemBarkJob.model_validate(body.get("bark_job"))
            measurement = parse_stem_csv(job.csv_text, job.csv_filename)
        except ValueError as exc:
            raise _error(node, str(exc)) from exc
        return NodeExecutionResult(
            outputs={
                "measurement": StemCsvPayload(
                    measurement=measurement,
                    side_mapping_override=job.side_mapping,
                )
            }
        )


class EndfaceSplitHandler:
    node_type = "preprocessing.endface_pair_split"

    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        import cv2

        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        csv = inputs.get("measurement")
        if not isinstance(csv, StemCsvPayload):
            raise _error(node, "CSV-Messdaten fehlen.")
        mapping = csv.side_mapping_override or node.config.get("side_mapping")
        if mapping not in {"left_first", "left_last"}:
            raise _error(
                node, "Zuordnung wählen: Links = erste Scheibe oder Links = letzte Scheibe."
            )
        image = np.asarray(_open_frame(frame))
        height, width = image.shape[:2]
        outputs = {}
        boxes = {}
        for side in ("left", "right"):
            regions = node.config.get(f"{side}_region")
            if not isinstance(regions, list) or len(regions) != 1:
                raise _error(node, f"Genau einen Bildbereich für {side} angeben.")
            region = regions[0]
            x, y, w, h = (float(region.get(k, 0)) for k in ("x", "y", "width", "height"))
            if not all(math.isfinite(v) for v in (x, y, w, h)) or (
                x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.000001 or y + h > 1.000001
            ):
                raise _error(node, "Bildbereich liegt außerhalb des Eingabefotos.")
            x0, y0, x1, y1 = (
                round(x * width),
                round(y * height),
                round((x + w) * width),
                round((y + h) * height),
            )
            if x1 - x0 < 8 or y1 - y0 < 8:
                raise _error(node, "Bildbereich ist zu klein.")
            boxes[side] = (x0, y0, x1, y1)
            cropped = image[y0:y1, x0:x1]
            transform = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]], dtype=float)
            rotation = int(node.config.get(f"{side}_rotation", 0))
            if rotation not in {0, 90, 180, 270}:
                raise _error(node, "Drehung muss 0, 90, 180 oder 270 Grad sein.")
            for _ in range(rotation // 90):
                ch = cropped.shape[0]
                cropped = cv2.rotate(cropped, cv2.ROTATE_90_CLOCKWISE)
                transform = np.array([[0, -1, ch - 1], [1, 0, 0], [0, 0, 1]]) @ transform
            if node.config.get(f"{side}_mirror", False):
                cropped = cropped[:, ::-1]
                transform = (
                    np.array([[-1, 0, cropped.shape[1] - 1], [0, 1, 0], [0, 0, 1]]) @ transform
                )
            first = (side == "left") == (mapping == "left_first")
            reference = EndfaceReference(
                side=side,
                slice_role="first" if first else "last",
                side_mapping=mapping,
                mapping_source="job" if csv.side_mapping_override else "pipeline",
                workpiece_id=csv.measurement.workpiece_id,
                leading_end=csv.measurement.leading_end,
                csv_sha256=csv.measurement.csv_sha256,
                slice=csv.measurement.slices[0 if first else -1],
            )
            outputs[f"{side}_frame"] = _derived(
                frame,
                node,
                cropped,
                side,
                transform,
                {"endface_reference": reference.model_dump(mode="json")},
            )
            outputs[f"{side}_reference"] = EndfaceReferencePayload(reference=reference)
        left, right = boxes["left"], boxes["right"]
        if left[2] > right[0]:
            raise _error(
                node, "Linker und rechter Bildbereich müssen von links nach rechts getrennt sein."
            )
        return NodeExecutionResult(outputs=outputs)


class EndfaceRectifyHandler:
    node_type = "preprocessing.endface_metric_rectify"

    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        import cv2

        frame = _required_frame(node, inputs, "frame")
        reference, segments = inputs.get("reference"), inputs.get("segments")
        if not isinstance(reference, EndfaceReferencePayload) or not isinstance(
            segments, SegmentPayload
        ):
            raise _error(node, "Stirnflächenreferenz und Segmentierung werden benötigt.")
        mpp = float(node.config.get("mm_per_pixel", 0.25))
        if not math.isfinite(mpp) or mpp < 0.05 or mpp > 5:
            raise _error(node, "Rastermaßstab muss zwischen 0,05 und 5 mm/Pixel liegen.")
        maximum_error = float(node.config.get("maximum_error_mm", 0.5))
        maximum_stability = float(node.config.get("maximum_stability_mm", 0.25))
        if not (0.01 <= maximum_error <= 0.5 and 0.01 <= maximum_stability <= 0.25):
            raise _error(node, "Registrierungsgrenzen liegen außerhalb des zulässigen Bereichs.")
        calibration = EndfaceCalibrationPayload(
            reference=reference.reference,
            mm_per_pixel=mpp,
            metadata={"outline_model": segments.metadata},
        )
        image = np.asarray(_open_frame(frame))
        diagnostic = frame
        isolated = frame
        try:
            if (
                not frame.image_context
                or segments.image_context_id != frame.image_context.context_id
            ):
                raise ValueError("segmentation_image_context_mismatch")
            selected = [
                s
                for s in segments.segments
                if s.label == node.config.get("endface_label", "endface")
            ]
            if len(selected) != 1 or selected[0].mask is None:
                raise ValueError("exactly_one_endface_with_full_mask_required")
            mask = decode_mask(selected[0].mask)
            if mask.shape != image.shape[:2]:
                raise ValueError("endface_mask_shape_mismatch")
            if mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any():
                raise ValueError("endface_clipped")
            contours, _ = cv2.findContours(
                mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
            )
            substantial = [c for c in contours if cv2.contourArea(c) > max(4, mask.sum() * 0.01)]
            if len(substantial) != 1:
                raise ValueError("endface_outline_disconnected")
            contour = substantial[0].reshape(-1, 2)
            isolated = _derived(
                frame, node, np.where(mask[:, :, None], image, 0), "isolated", np.eye(3)
            )
            fit = register_contours(
                contour,
                reference.reference.slice.points_mm,
                maximum_error_mm=maximum_error,
                maximum_stability_mm=maximum_stability,
            )
            calibration.error_mm, calibration.stability_mm = fit.error_mm, fit.stability_mm
            if fit.image_to_mm is not None:
                outline_px = project(
                    reference.reference.slice.points_mm, np.linalg.inv(fit.image_to_mm)
                )
                preview = image.copy()
                cv2.polylines(preview, [np.rint(outline_px).astype(np.int32)], True, (0, 255, 0), 2)
                diagnostic = _derived(frame, node, preview, "csv-overlay", np.eye(3))
            if not fit.valid:
                raise ValueError(";".join(fit.reasons))
            metric = project(contour, fit.image_to_mm)
            origin = (
                np.minimum(metric.min(axis=0), np.min(reference.reference.slice.points_mm, axis=0))
                - 2
            )
            extent = (
                np.maximum(metric.max(axis=0), np.max(reference.reference.slice.points_mm, axis=0))
                + 2
            )
            size = np.ceil((extent - origin) / mpp).astype(int) + 1
            if max(size) > 8192 or np.prod(size.astype(float)) > 16_000_000:
                raise ValueError("metric_raster_too_large")
            mm_to_view = np.array(
                [[1 / mpp, 0, -origin[0] / mpp], [0, 1 / mpp, -origin[1] / mpp], [0, 0, 1]]
            )
            warp = mm_to_view @ fit.image_to_mm
            warped_mask = (
                cv2.warpPerspective(
                    mask.astype(np.uint8), warp, tuple(size), flags=cv2.INTER_NEAREST
                )
                > 0
            )
            warped_image = cv2.warpPerspective(image, warp, tuple(size), flags=cv2.INTER_LINEAR)
            warped_image[~warped_mask] = 0
            hbytes = json.dumps(
                {
                    "reference": reference.reference.model_dump(mode="json"),
                    "matrix": fit.image_to_mm.reshape(-1).tolist(),
                    "mpp": mpp,
                },
                sort_keys=True,
            ).encode()
            digest = hashlib.sha256(hbytes).hexdigest()
            output = _derived(
                frame,
                node,
                warped_image,
                "metric",
                warp,
                {
                    "registration_hash": digest,
                    "mm_per_pixel": mpp,
                    "endface_reference": reference.reference.model_dump(mode="json"),
                    "metric_origin_mm": origin.tolist(),
                    "metrology_status": "not_validated",
                },
            )
            # Worst local source pixel scale, including the actual model mask resolution.
            source_scale = max(
                image.shape[1] / (selected[0].mask.native_width or image.shape[1]),
                image.shape[0] / (selected[0].mask.native_height or image.shape[0]),
            )
            probes = contour[:: max(1, len(contour) // 64)]
            base = project(probes, fit.image_to_mm)
            effective = max(
                float(
                    np.max(np.linalg.norm(project(probes + delta, fit.image_to_mm) - base, axis=1))
                )
                for delta in ([source_scale, 0], [0, source_scale])
            )
            calibration.valid = True
            calibration.image_context_id = output.image_context.context_id
            calibration.image_to_mm = fit.image_to_mm.reshape(-1).tolist()
            calibration.image_to_view = warp.reshape(-1).tolist()
            calibration.origin_mm = tuple(origin)
            calibration.center_mm = tuple(polygon_centroid(reference.reference.slice.points_mm))
            calibration.outer_mask = encode_mask(warped_mask).model_dump(mode="json")
            calibration.registration_hash = digest
            calibration.effective_source_mm_per_pixel = effective
            calibration.alternative_view_to_mm = [
                (h @ np.linalg.inv(warp)).reshape(-1).tolist() for h in fit.alternatives
            ]
        except ValueError as exc:
            calibration.reason_codes = str(exc).split(";")
            # Preserve a usable visual input for diagnostics and the other branch.
            output = frame
        return NodeExecutionResult(
            outputs={
                "frame": output,
                "calibration": calibration,
                "eligibility": ResultPayload(
                    result={
                        "eligible": calibration.valid,
                        "result": calibration.valid,
                        "image_context_id": output.image_context.context_id
                        if output.image_context
                        else None,
                        "reason_codes": calibration.reason_codes,
                        "registration_hash": calibration.registration_hash,
                    }
                ),
                "isolated": isolated,
                "diagnostic": diagnostic,
            }
        )


class BarkRadialHandler:
    node_type = "analysis.bark_radial_thickness"

    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        import cv2

        frame = _required_frame(node, inputs, "frame")
        calibration, segments = inputs.get("calibration"), inputs.get("segments")
        if not isinstance(calibration, EndfaceCalibrationPayload) or not isinstance(
            segments, SegmentPayload
        ):
            raise _error(node, "Metrische Kalibrierung und Klassenmasken fehlen.")
        result = BarkSideMeasurement(
            reference=calibration.reference,
            registration_hash=calibration.registration_hash,
            registration_error_mm=calibration.error_mm,
            registration_stability_mm=calibration.stability_mm,
            effective_source_mm_per_pixel=calibration.effective_source_mm_per_pixel,
            model_versions={
                "outline": str(
                    calibration.metadata.get("outline_model", {}).get("model_version_id", "")
                ),
                "semantic": str(segments.metadata.get("model_version_id", "")),
            },
        )
        diagnostic = frame
        try:
            fraction = float(node.config.get("minimum_valid_fraction", 0.9))
            gap = float(node.config.get("maximum_invalid_gap_deg", 10))
            source_limit = float(node.config.get("maximum_source_mm_per_pixel", 0.5))
            if not (0.9 <= fraction <= 1 and 0 <= gap <= 10 and 0.01 <= source_limit <= 0.5):
                raise ValueError("measurement_quality_limits_out_of_range")
            if not calibration.valid:
                raise ValueError(";".join(calibration.reason_codes))
            if segments.image_context_id != calibration.image_context_id:
                raise ValueError("semantic_image_context_mismatch")
            outer = decode_mask(calibration.outer_mask)
            labels = {
                role: str(node.config.get(f"{role}_label", role))
                for role in ("wood", "bark", "background", "unknown")
            }
            if len(set(labels.values())) != 4 or not all(labels.values()):
                raise ValueError("semantic_labels_must_be_distinct")
            masks = {role: np.zeros_like(outer) for role in labels}
            native_scale = 1.0
            for segment in segments.segments:
                role = next((k for k, v in labels.items() if segment.label == v), None)
                if role is None:
                    continue
                if segment.mask is None:
                    raise ValueError("lossless_semantic_masks_required")
                mask = decode_mask(segment.mask)
                if mask.shape != outer.shape:
                    raise ValueError("semantic_mask_shape_mismatch")
                masks[role] |= mask
                native_scale = max(
                    native_scale,
                    outer.shape[1] / (segment.mask.native_width or outer.shape[1]),
                    outer.shape[0] / (segment.mask.native_height or outer.shape[0]),
                )
            effective = max(
                calibration.effective_source_mm_per_pixel or 0,
                native_scale * calibration.mm_per_pixel,
            )
            result.effective_source_mm_per_pixel = effective
            if effective > source_limit:
                raise ValueError("insufficient_native_image_resolution")
            occupied = sum(mask.astype(np.uint8) for mask in masks.values())
            unknown = masks["unknown"] | (occupied != 1) | (masks["background"] & outer)
            center = (
                np.asarray(calibration.center_mm) - calibration.origin_mm
            ) / calibration.mm_per_pixel
            count = int(node.config.get("sample_count", 360))
            if count < 36 or count > 1440:
                raise ValueError("sample_count_out_of_range")
            samples = radial_samples(
                outer,
                masks["wood"],
                masks["bark"],
                unknown,
                center_px=tuple(center),
                mm_per_pixel=calibration.mm_per_pixel,
                count=count,
            )
            summary = summarize_samples(
                samples,
                minimum_valid_fraction=fraction,
                maximum_invalid_gap_deg=gap,
                include_extrema=bool(node.config.get("include_extrema", False)),
            )
            result = result.model_copy(update={**summary, "samples": samples})
            # Measure sensitivity of the reported mean to accepted alternative transforms.
            deviations = []
            good = [s for s in samples if s.valid]
            if good and result.mean_mm is not None:
                directions = np.array(
                    [
                        [math.cos(math.radians(s.angle_deg)), math.sin(math.radians(s.angle_deg))]
                        for s in good
                    ]
                )
                inner = (
                    center
                    + directions
                    * np.array([s.wood_radius_mm for s in good])[:, None]
                    / calibration.mm_per_pixel
                )
                exterior = (
                    center
                    + directions
                    * np.array([s.outer_radius_mm for s in good])[:, None]
                    / calibration.mm_per_pixel
                )
                for matrix in calibration.alternative_view_to_mm:
                    lengths = np.linalg.norm(
                        project(exterior, matrix) - project(inner, matrix), axis=1
                    )
                    deviations.append(abs(float(lengths.mean()) - result.mean_mm))
            result.sampling_stability_mm = max(deviations, default=0)
            if result.sampling_stability_mm > 0.25:
                result.valid, result.mean_mm, result.min_mm, result.max_mm = False, None, None, None
                result.reason_codes.append("radial_mean_registration_unstable")
            overlay = np.asarray(_open_frame(frame)).copy()
            overlay[masks["bark"] & outer] = (
                0.45 * overlay[masks["bark"] & outer] + np.array([255, 140, 0]) * 0.55
            ).astype(np.uint8)
            overlay[unknown & outer] = [255, 0, 80]
            for sample in samples[:: max(1, count // 72)]:
                if sample.outer_radius_mm is None or sample.wood_radius_mm is None:
                    continue
                direction = np.array(
                    [
                        math.cos(math.radians(sample.angle_deg)),
                        math.sin(math.radians(sample.angle_deg)),
                    ]
                )
                a = np.rint(
                    center + direction * sample.wood_radius_mm / calibration.mm_per_pixel
                ).astype(int)
                b = np.rint(
                    center + direction * sample.outer_radius_mm / calibration.mm_per_pixel
                ).astype(int)
                cv2.line(overlay, tuple(a), tuple(b), (0, 255, 100), 1)
            diagnostic = _derived(frame, node, overlay, "measurement", np.eye(3))
        except ValueError as exc:
            result.valid, result.mean_mm, result.min_mm, result.max_mm = False, None, None, None
            result.reason_codes = str(exc).split(";")
        return NodeExecutionResult(
            outputs={
                "measurement": BarkMeasurementPayload(measurement=result),
                "diagnostic": diagnostic,
            }
        )


class BarkPairHandler:
    node_type = "analysis.bark_pair_result"

    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        if node.config.get("aggregation", "equal_valid_sides") != "equal_valid_sides":
            raise _error(node, "Der Stammwert benötigt beide gültigen, gleich gewichteten Seiten.")
        left, right = inputs.get("left"), inputs.get("right")
        if not isinstance(left, BarkMeasurementPayload) or not isinstance(
            right, BarkMeasurementPayload
        ):
            raise _error(node, "Beide Seitenergebnisse werden benötigt.")
        left_value, right_value = left.measurement, right.measurement
        if (
            left_value.reference.side != "left"
            or right_value.reference.side != "right"
            or left_value.reference.csv_sha256 != right_value.reference.csv_sha256
            or left_value.reference.workpiece_id != right_value.reference.workpiece_id
            or left_value.reference.side_mapping != right_value.reference.side_mapping
            or left_value.reference.slice_role == right_value.reference.slice_role
        ):
            raise _error(node, "Seitenergebnisse gehören nicht zum selben Foto-/CSV-Auftrag.")
        valid = (
            left_value.valid
            and right_value.valid
            and left_value.mean_mm is not None
            and right_value.mean_mm is not None
        )
        result = BarkMeasurementResult(
            workpiece_id=left_value.reference.workpiece_id,
            valid=valid,
            mean_mm=(left_value.mean_mm + right_value.mean_mm) / 2 if valid else None,
            left=left_value,
            right=right_value,
        )
        return NodeExecutionResult(
            outputs={"result": ResultPayload(result=result.model_dump(mode="json"))}
        )


source_stem_csv_measurements = StemCsvHandler()
preprocessing_endface_pair_split = EndfaceSplitHandler()
preprocessing_endface_metric_rectify = EndfaceRectifyHandler()
analysis_bark_radial_thickness = BarkRadialHandler()
analysis_bark_pair_result = BarkPairHandler()
