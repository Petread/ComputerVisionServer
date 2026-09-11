from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from cv_datasets.models import (
    BoundingBox,
    CanonicalDataset,
    CanonicalItem,
    DatasetAssetRecord,
    OrientedBoundingBox,
    PolygonPoint,
    SegmentationPolygon,
    SplitSummary,
    ValidationIssue,
)
from cv_status import TaskType

ASSET_KEYS = ("image", "image_url", "image_path", "img", "file", "file_upload")
LABEL_CONFIG_VALUE_KEYS = ("labels", "choices", "rectanglelabels", "polygonlabels")
LABEL_CONFIG_ATTR_KEYS = ("labels_attrs", "choices_attrs")
ANOMALY_NORMAL_LABELS = {
    "good",
    "normal",
    "ok",
    "okay",
    "pass",
    "passed",
    "accepted",
    "healthy",
}
ANOMALY_ABNORMAL_LABELS = {
    "bad",
    "abnormal",
    "anomaly",
    "anomalous",
    "defect",
    "defective",
    "ng",
    "fail",
    "failed",
    "nok",
    "not_ok",
    "not ok",
    "rejected",
    "rework",
}


def extract_declared_classes(label_config: dict[str, Any] | str | None) -> list[str]:
    if not label_config:
        return []
    if isinstance(label_config, dict):
        classes: set[str] = set()
        stack: list[Any] = [label_config]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key in LABEL_CONFIG_VALUE_KEYS:
                    _collect_declared_label_values(current.get(key), classes)
                for key in LABEL_CONFIG_ATTR_KEYS:
                    _collect_declared_label_attrs(current.get(key), classes)
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        return sorted(classes)
    try:
        parsed = json.loads(label_config)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        return extract_declared_classes(parsed)
    values = [
        match.group(2)
        for match in re.finditer(
            r"<(?:Label|Choice)\b[^>]*\bvalue=(['\"])(.*?)\1",
            label_config,
            flags=re.IGNORECASE,
        )
    ]
    return sorted({value for value in values if value})


def _collect_declared_label_values(candidate: Any, classes: set[str]) -> None:
    if isinstance(candidate, str) and candidate:
        classes.add(candidate)
        return
    if isinstance(candidate, list):
        for entry in candidate:
            _collect_declared_label_values(entry, classes)
        return
    if isinstance(candidate, dict):
        value = candidate.get("value")
        if isinstance(value, str) and value:
            classes.add(value)


def _collect_declared_label_attrs(candidate: Any, classes: set[str]) -> None:
    if not isinstance(candidate, dict):
        return
    for key, value in candidate.items():
        if isinstance(key, str) and key:
            classes.add(key)
        if isinstance(value, dict):
            attr_value = value.get("value")
            if isinstance(attr_value, str) and attr_value:
                classes.add(attr_value)


def generate_content_checksum(
    *,
    dataset: CanonicalDataset,
    split_summary: SplitSummary,
    label_schema_version: str,
    export_format: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(label_schema_version.encode("utf-8"))
    digest.update(export_format.encode("utf-8"))
    digest.update(json.dumps(split_summary.model_dump(), sort_keys=True).encode("utf-8"))
    digest.update(
        json.dumps(
            {
                "eligible": dataset.eligible_item_count,
                "labeled": dataset.submitted_labeled_item_count,
                "background": dataset.submitted_background_item_count,
                "draft": dataset.draft_item_count,
                "invalid": dataset.invalid_item_count,
                "rotated_rectangles": dataset.rotated_rectangle_count,
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    for item in sorted(dataset.items, key=lambda candidate: candidate.item_id):
        digest.update(item.item_id.encode("utf-8"))
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(item.asset_sha256.encode("utf-8"))
        digest.update(item.split.encode("utf-8"))
        digest.update(item.eligibility_status.encode("utf-8"))
        digest.update(json.dumps(item.metadata, sort_keys=True).encode("utf-8"))
        digest.update(json.dumps(item.labels, sort_keys=True).encode("utf-8"))
        digest.update(str(item.anomaly_label or "").encode("utf-8"))
        digest.update(
            json.dumps([box.model_dump() for box in item.boxes], sort_keys=True).encode("utf-8")
        )
        digest.update(
            json.dumps(
                [box.model_dump() for box in item.oriented_boxes],
                sort_keys=True,
            ).encode("utf-8")
        )
        digest.update(
            json.dumps(
                [polygon.model_dump() for polygon in item.polygons],
                sort_keys=True,
            ).encode("utf-8")
        )
    return f"sha256:{digest.hexdigest()}"


def _task_image_view_metadata(task: dict[str, Any]) -> dict[str, Any]:
    meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    source_frame = (
        meta.get("source_frame") if isinstance(meta.get("source_frame"), dict) else {}
    )
    capture_metadata = (
        meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    )
    image_context = (
        source_frame.get("image_context")
        if isinstance(source_frame.get("image_context"), dict)
        else {}
    )
    transform = (
        image_context.get("transform")
        if isinstance(image_context.get("transform"), dict)
        else {}
    )
    parameters = (
        transform.get("parameters")
        if isinstance(transform.get("parameters"), dict)
        else {}
    )
    eligibility = (
        capture_metadata.get("frame_eligibility")
        if isinstance(capture_metadata.get("frame_eligibility"), dict)
        else {}
    )

    def first(*values: object) -> object:
        return next((value for value in values if value is not None and value != ""), None)

    profile_id = first(
        source_frame.get("view_profile_id"),
        capture_metadata.get("view_profile_id"),
        parameters.get("view_profile_id"),
        eligibility.get("view_profile_id"),
    )
    checksum = first(
        source_frame.get("view_profile_checksum"),
        capture_metadata.get("view_profile_checksum"),
        parameters.get("view_profile_checksum"),
        eligibility.get("view_profile_checksum"),
    )
    if not isinstance(profile_id, str) or not isinstance(checksum, str):
        return {}
    width = first(source_frame.get("width"), eligibility.get("output_width"), image_context.get("width"))
    height = first(source_frame.get("height"), eligibility.get("output_height"), image_context.get("height"))
    return {
        "schema_version": "dataset.image-view-provenance.v1",
        "capture_source": capture_metadata.get("capture_source") or "PIPELINE_IMAGE_OUTPUT",
        "view_profile_id": profile_id,
        "profile_checksum": checksum,
        "input_width": int(width) if isinstance(width, (int, float)) else None,
        "input_height": int(height) if isinstance(height, (int, float)) else None,
        "color_space": first(source_frame.get("color_space"), capture_metadata.get("color_space")),
        "undistort_preprocessing_version": first(
            source_frame.get("undistort_preprocessing_version"),
            capture_metadata.get("undistort_preprocessing_version"),
        ),
        "image_view_preprocessing_version": first(
            source_frame.get("image_view_preprocessing_version"),
            capture_metadata.get("image_view_preprocessing_version"),
        ),
        "intrinsic_calibration_hash": first(
            source_frame.get("camera_calibration_id"),
            capture_metadata.get("camera_calibration_id"),
            parameters.get("intrinsic_calibration_hash"),
        ),
        "source_to_view_homography": parameters.get("source_to_view_homography"),
        "view_to_source_homography": parameters.get("view_to_source_homography"),
        "source_frame_context_id": first(
            source_frame.get("source_frame_context_id"),
            capture_metadata.get("source_frame_context_id"),
        ),
        "source_frame_timestamp": first(
            source_frame.get("source_frame_timestamp"),
            capture_metadata.get("source_frame_timestamp"),
        ),
        "frame_eligibility": eligibility,
    }


def _dataset_image_view_metadata(
    items: list[CanonicalItem],
    *,
    issues: list[ValidationIssue],
) -> dict[str, Any]:
    eligible = [item for item in items if item.eligibility_status.startswith("eligible")]
    views = [item.metadata for item in eligible if item.metadata.get("view_profile_id")]
    if not views:
        return {}
    if len(views) != len(eligible):
        issues.append(
            ValidationIssue(
                severity="error",
                code="image_view_provenance_incomplete",
                message=(
                    "Canonical-view and legacy/full-frame items must not be mixed in one "
                    "training snapshot."
                ),
            )
        )
    signatures = {
        (
            item.get("view_profile_id"),
            item.get("profile_checksum"),
            item.get("input_width"),
            item.get("input_height"),
            item.get("color_space"),
            item.get("undistort_preprocessing_version"),
            item.get("image_view_preprocessing_version"),
        )
        for item in views
    }
    if len(signatures) != 1:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="image_view_profile_mixed",
                message=(
                    "Eligible items use more than one canonical image-view contract. "
                    "Training remains allowed and the complete accepted profile set is "
                    "recorded as model provenance."
                ),
                metadata={
                    "signature_count": len(signatures),
                    "view_profile_ids": sorted(
                        {
                            str(signature[0])
                            for signature in signatures
                            if signature[0] is not None
                        }
                    ),
                },
            )
        )
        profiles = [
            {
                "view_profile_id": signature[0],
                "profile_checksum": signature[1],
                "input_width": signature[2],
                "input_height": signature[3],
                "color_space": signature[4],
                "undistort_preprocessing_version": signature[5],
                "image_view_preprocessing_version": signature[6],
            }
            for signature in sorted(
                signatures,
                key=lambda item: tuple(str(value) for value in item),
            )
        ]
        return {
            "schema_version": "dataset.image-view-provenance-set.v1",
            "contract_mode": "MULTIPLE_CANONICAL_VIEWS",
            "profile_count": len(profiles),
            "profiles": profiles,
        }
    representative = dict(views[0])
    representative.pop("frame_eligibility", None)
    representative.pop("source_frame_context_id", None)
    representative.pop("source_frame_timestamp", None)
    return representative


def convert_label_studio_export(
    *,
    dataset_id: str,
    task_type: str,
    export_payload: list[dict[str, Any]],
    assets: list[DatasetAssetRecord],
    label_config: dict[str, Any] | str | None,
    train_ratio: int,
    val_ratio: int,
    test_ratio: int,
) -> tuple[CanonicalDataset, SplitSummary, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    asset_by_relative = {PurePosixPath(asset.relative_path).as_posix(): asset for asset in assets}
    assets_by_basename: dict[str, list[DatasetAssetRecord]] = defaultdict(list)
    for asset in assets:
        assets_by_basename[Path(asset.relative_path).name].append(asset)

    normalized_task_type = TaskType(task_type)
    if normalized_task_type == TaskType.TEMPORAL_SEQUENCE:
        raise ValueError(
            "Temporal sequence Label Studio import is not implemented in the image export "
            "converter. Use the schema-dependent temporal snapshot path when it is available."
        )
    declared_classes = extract_declared_classes(label_config)
    items: list[CanonicalItem] = []
    class_distribution: dict[str, int] = defaultdict(int)
    annotation_count = 0
    eligible_item_count = 0
    submitted_labeled_item_count = 0
    submitted_background_item_count = 0
    draft_item_count = 0
    invalid_item_count = 0
    rotated_rectangle_count = 0
    anomaly_distribution: dict[str, int] = defaultdict(int)

    for task in export_payload:
        task_id = str(task.get("id") or task.get("task_id") or len(items) + 1)
        asset_ref = _resolve_task_asset_reference(task)
        asset = _lookup_asset(asset_ref, asset_by_relative, assets_by_basename)
        if asset is None:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="broken_reference",
                    message="The exported task points to a file that is not registered in the dataset.",
                    item_ref=task_id,
                    metadata={"asset_ref": asset_ref or ""},
                )
            )
            invalid_item_count += 1
            continue
        if not Path(asset.source_path).exists():
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="missing_file",
                    message="A dataset asset referenced by the export is missing from storage.",
                    item_ref=task_id,
                    metadata={"relative_path": asset.relative_path},
                )
            )
            invalid_item_count += 1
            continue

        selected_annotation, submitted, draft_count = _selected_annotation(task)
        parse_result = _parse_annotation(
            task_type=normalized_task_type,
            annotation=selected_annotation,
            task_id=task_id,
        )
        annotation_count += parse_result.raw_annotation_count
        rotated_rectangle_count += parse_result.rotated_rectangle_count

        split = _assign_split(
            seed=f"{dataset_id}:{task_id}:{asset.relative_path}",
            train_ratio=train_ratio,
            val_ratio=val_ratio,
            test_ratio=test_ratio,
        )
        eligibility_status = "eligible_labeled"
        background = False
        exclusion_reason = None
        item_issues = list(parse_result.issues)

        if not submitted:
            eligibility_status = "draft"
            exclusion_reason = "draft_or_unsubmitted"
            draft_item_count += 1
        elif parse_result.invalid:
            eligibility_status = "invalid"
            exclusion_reason = "invalid_annotation"
            invalid_item_count += 1
        elif parse_result.has_foreground:
            eligible_item_count += 1
            submitted_labeled_item_count += 1
            if normalized_task_type == TaskType.CLASSIFICATION:
                for label in parse_result.labels:
                    class_distribution[label] += 1
            elif normalized_task_type == TaskType.ANOMALY:
                anomaly_label = parse_result.anomaly_label or (
                    parse_result.labels[0] if parse_result.labels else None
                )
                if anomaly_label:
                    class_distribution[anomaly_label] += 1
                    anomaly_distribution[anomaly_label] += 1
            elif normalized_task_type == TaskType.DETECTION:
                for box in parse_result.boxes:
                    class_distribution[box.class_name] += 1
            elif normalized_task_type == TaskType.ORIENTED_DETECTION:
                for box in parse_result.oriented_boxes:
                    class_distribution[box.class_name] += 1
            elif normalized_task_type == TaskType.SEGMENTATION:
                for polygon in parse_result.polygons:
                    class_distribution[polygon.class_name] += 1
        else:
            background = submitted
            eligibility_status = "eligible_background"
            eligible_item_count += 1
            submitted_background_item_count += 1
            item_issues.append(
                ValidationIssue(
                    severity="warning",
                    code="background_sample",
                    message="Submitted task contains no foreground labels and will be used as a background sample.",
                    item_ref=task_id,
                )
            )

        issues.extend(item_issues)
        items.append(
            CanonicalItem(
                item_id=f"{dataset_id}:{task_id}",
                task_id=task_id,
                relative_path=asset.relative_path,
                source_path=asset.source_path,
                asset_sha256=asset.sha256,
                split=split,
                eligibility_status=eligibility_status,
                submitted=submitted,
                background=background,
                exclusion_reason=exclusion_reason,
                labels=parse_result.labels,
                anomaly_label=parse_result.anomaly_label,
                boxes=parse_result.boxes,
                oriented_boxes=parse_result.oriented_boxes,
                polygons=parse_result.polygons,
                raw_annotation_count=parse_result.raw_annotation_count + draft_count,
                metadata=_task_image_view_metadata(task),
            )
        )

    _assign_balanced_splits(
        dataset_id=dataset_id,
        items=items,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        test_ratio=test_ratio,
    )
    if normalized_task_type == TaskType.ANOMALY:
        label_map = ["normal", "anomalous"]
    else:
        label_map = sorted(set(declared_classes) | set(class_distribution))
    image_view_metadata = _dataset_image_view_metadata(items, issues=issues)
    dataset = CanonicalDataset(
        dataset_id=dataset_id,
        task_type=normalized_task_type.value,
        source_task_count=len(export_payload),
        annotation_count=annotation_count,
        eligible_item_count=eligible_item_count,
        submitted_labeled_item_count=submitted_labeled_item_count,
        submitted_background_item_count=submitted_background_item_count,
        draft_item_count=draft_item_count,
        invalid_item_count=invalid_item_count,
        rotated_rectangle_count=rotated_rectangle_count,
        items=items,
        class_distribution=dict(sorted(class_distribution.items())),
        anomaly_distribution=dict(sorted(anomaly_distribution.items())),
        label_map=label_map,
        metadata={"image_view": image_view_metadata} if image_view_metadata else {},
    )
    split_summary = SplitSummary(
        strategy="deterministic_sha256_balanced",
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        test_ratio=test_ratio,
        train_count=sum(
            1
            for item in items
            if item.split == "train" and item.eligibility_status.startswith("eligible")
        ),
        val_count=sum(
            1
            for item in items
            if item.split == "val" and item.eligibility_status.startswith("eligible")
        ),
        test_count=sum(
            1
            for item in items
            if item.split == "test" and item.eligibility_status.startswith("eligible")
        ),
    )
    return dataset, split_summary, issues


def _resolve_task_asset_reference(task: dict[str, Any]) -> str | None:
    data = task.get("data")
    if not isinstance(data, dict):
        return None
    for key in ASSET_KEYS:
        candidate = data.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    for candidate in data.values():
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _lookup_asset(
    asset_ref: str | None,
    asset_by_relative: dict[str, DatasetAssetRecord],
    assets_by_basename: dict[str, list[DatasetAssetRecord]],
) -> DatasetAssetRecord | None:
    if not asset_ref:
        return None
    normalized = _normalize_asset_reference(asset_ref)
    if normalized in asset_by_relative:
        return asset_by_relative[normalized]
    basename_matches = assets_by_basename.get(Path(normalized).name, [])
    if len(basename_matches) == 1:
        return basename_matches[0]
    return None


def _normalize_asset_reference(asset_ref: str) -> str:
    parsed = urlparse(asset_ref)
    candidate = asset_ref
    if parsed.scheme or parsed.query:
        query_path = parse_qs(parsed.query).get("d", [None])[0]
        candidate = str(query_path or parsed.path)
    normalized = unquote(candidate).replace("\\", "/")
    if "/data/upload/" in normalized:
        normalized = normalized.split("/data/upload/", maxsplit=1)[1]
    return PurePosixPath(normalized.lstrip("/")).as_posix()


def _selected_annotation(task: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, int]:
    annotations = task.get("annotations")
    if not isinstance(annotations, list):
        return None, False, 0

    submitted_candidates: list[dict[str, Any]] = []
    draft_count = 0
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        if annotation.get("was_cancelled") or annotation.get("skipped"):
            continue
        result = annotation.get("result")
        if not isinstance(result, list):
            continue
        if _annotation_is_submitted(annotation):
            submitted_candidates.append(annotation)
        else:
            draft_count += 1
    submitted_candidates.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or item.get("id") or "")
    )
    return (submitted_candidates[-1] if submitted_candidates else None, bool(submitted_candidates), draft_count)


def _annotation_is_submitted(annotation: dict[str, Any]) -> bool:
    if annotation.get("was_cancelled") or annotation.get("skipped"):
        return False
    if annotation.get("completed_by") is not None:
        return True
    if annotation.get("completed_at") is not None:
        return True
    for key in ("is_labeled", "ground_truth", "accepted"):
        if annotation.get(key) is True:
            return True
    if annotation.get("draft_created_at"):
        return False
    result = annotation.get("result")
    return isinstance(result, list) and bool(result)


class _ParsedAnnotation:
    def __init__(
        self,
        *,
        labels: list[str],
        boxes: list[BoundingBox],
        oriented_boxes: list[OrientedBoundingBox],
        polygons: list[SegmentationPolygon],
        issues: list[ValidationIssue],
        raw_annotation_count: int,
        invalid: bool,
        anomaly_label: str | None = None,
        rotated_rectangle_count: int = 0,
    ) -> None:
        self.labels = labels
        self.boxes = boxes
        self.oriented_boxes = oriented_boxes
        self.polygons = polygons
        self.issues = issues
        self.raw_annotation_count = raw_annotation_count
        self.invalid = invalid
        self.anomaly_label = anomaly_label
        self.rotated_rectangle_count = rotated_rectangle_count

    @property
    def has_foreground(self) -> bool:
        return bool(self.labels or self.boxes or self.oriented_boxes or self.polygons)


def _parse_annotation(
    *,
    task_type: TaskType,
    annotation: dict[str, Any] | None,
    task_id: str,
) -> _ParsedAnnotation:
    if annotation is None:
        return _ParsedAnnotation(
            labels=[],
            boxes=[],
            oriented_boxes=[],
            polygons=[],
            issues=[],
            raw_annotation_count=0,
            invalid=False,
            anomaly_label=None,
        )

    results = annotation.get("result")
    if not isinstance(results, list):
        return _ParsedAnnotation(
            labels=[],
            boxes=[],
            oriented_boxes=[],
            polygons=[],
            issues=[
                ValidationIssue(
                    severity="error",
                    code="invalid_annotation",
                    message="The annotation payload is missing result entries.",
                    item_ref=task_id,
                )
            ],
            raw_annotation_count=1,
            invalid=True,
            anomaly_label=None,
        )

    labels: list[str] = []
    boxes: list[BoundingBox] = []
    oriented_boxes: list[OrientedBoundingBox] = []
    polygons: list[SegmentationPolygon] = []
    issues: list[ValidationIssue] = []
    invalid = False
    rotated_rectangle_count = _count_rotated_rectangle_results(results)
    for result in results:
        if not isinstance(result, dict):
            continue
        value = result.get("value")
        if not isinstance(value, dict):
            continue
        labels.extend(_extract_labels(value))
        try:
            boxes.extend(_extract_boxes(task_type, value))
            oriented_boxes.extend(_extract_oriented_boxes(task_type, value))
            polygons.extend(_extract_polygons(task_type, value))
        except ValueError as exc:
            invalid = True
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="invalid_annotation",
                    message=str(exc),
                    item_ref=task_id,
                )
            )

    if task_type == TaskType.CLASSIFICATION and len(labels) > 1:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="multi_label_classification",
                message="Multiple labels were found for a classification item.",
                item_ref=task_id,
            )
        )
    if task_type == TaskType.DETECTION and labels and not boxes:
        invalid = True
        issues.append(
            ValidationIssue(
                severity="error",
                code="invalid_annotation",
                message="Detection annotations must contain at least one bounding box when labels are present.",
                item_ref=task_id,
            )
        )
    if task_type == TaskType.ORIENTED_DETECTION and labels and not oriented_boxes:
        invalid = True
        issues.append(
            ValidationIssue(
                severity="error",
                code="invalid_annotation",
                message=(
                    "Oriented detection annotations must contain at least one "
                    "rotated bounding box when labels are present."
                ),
                item_ref=task_id,
            )
        )
    if task_type == TaskType.SEGMENTATION and labels and not polygons:
        invalid = True
        issues.append(
            ValidationIssue(
                severity="error",
                code="invalid_annotation",
                message="Segmentation annotations must contain at least one polygon when labels are present.",
                item_ref=task_id,
            )
        )
    anomaly_label = None
    if task_type == TaskType.ANOMALY:
        normalized_labels = sorted(
            {
                semantic
                for label in labels
                for semantic in [_normalize_anomaly_label(label)]
                if semantic is not None
            }
        )
        unknown_labels = sorted(
            {
                label
                for label in labels
                if _normalize_anomaly_label(label) is None
            }
        )
        if unknown_labels:
            invalid = True
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="invalid_anomaly_label",
                    message=(
                        "Anomaly annotations must use good/normal or bad/abnormal labels."
                    ),
                    item_ref=task_id,
                    metadata={"labels": unknown_labels},
                )
            )
        elif len(normalized_labels) != 1:
            invalid = True
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="invalid_anomaly_label",
                    message=(
                        "Anomaly annotations require exactly one good/normal or "
                        "bad/abnormal label."
                    ),
                    item_ref=task_id,
                    metadata={"labels": labels},
                )
            )
        else:
            anomaly_label = normalized_labels[0]
            labels = [anomaly_label]
        boxes = []
        oriented_boxes = []
        polygons = []

    return _ParsedAnnotation(
        labels=sorted(set(labels)),
        boxes=boxes,
        oriented_boxes=oriented_boxes,
        polygons=polygons,
        issues=issues,
        raw_annotation_count=len(results),
        invalid=invalid,
        anomaly_label=anomaly_label,
        rotated_rectangle_count=rotated_rectangle_count,
    )


def _extract_labels(value: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for key in ("choices", "labels", "rectanglelabels", "polygonlabels"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            labels.extend([entry for entry in candidate if isinstance(entry, str) and entry])
    return labels


def _count_rotated_rectangle_results(results: list[Any]) -> int:
    count = 0
    for result in results:
        if not isinstance(result, dict):
            continue
        value = result.get("value")
        if not isinstance(value, dict):
            continue
        rectangle_labels = value.get("rectanglelabels")
        if not isinstance(rectangle_labels, list) or not rectangle_labels:
            continue
        rotation = _coerce_float(value.get("rotation")) or 0.0
        if abs(rotation) > 1e-6:
            count += 1
    return count


def _normalize_anomaly_label(value: str) -> str | None:
    normalized = re.sub(r"[\s_-]+", " ", value.strip().casefold())
    underscored = normalized.replace(" ", "_")
    if normalized in ANOMALY_NORMAL_LABELS or underscored in ANOMALY_NORMAL_LABELS:
        return "normal"
    if normalized in ANOMALY_ABNORMAL_LABELS or underscored in ANOMALY_ABNORMAL_LABELS:
        return "anomalous"
    return None


def _extract_boxes(task_type: TaskType, value: dict[str, Any]) -> list[BoundingBox]:
    if task_type != TaskType.DETECTION:
        return []
    rectangle_labels = value.get("rectanglelabels")
    if not isinstance(rectangle_labels, list) or not rectangle_labels:
        return []
    label = next((entry for entry in rectangle_labels if isinstance(entry, str) and entry), None)
    if label is None:
        return []
    required_fields = ("x", "y", "width", "height")
    if any(field not in value for field in required_fields):
        raise ValueError("The detection annotation is missing one or more bounding-box coordinates.")
    rotation = _coerce_float(value.get("rotation")) or 0.0
    if abs(rotation) > 1e-6:
        raise ValueError(
            "The detection annotation contains rotated bounding boxes. "
            "Create an Oriented Detection model for this Label Studio project."
        )
    return [
        BoundingBox(
            class_name=label,
            x=float(value["x"]) / 100.0,
            y=float(value["y"]) / 100.0,
            width=float(value["width"]) / 100.0,
            height=float(value["height"]) / 100.0,
            original_width=_coerce_int(value.get("original_width")),
            original_height=_coerce_int(value.get("original_height")),
        )
    ]


def _extract_oriented_boxes(task_type: TaskType, value: dict[str, Any]) -> list[OrientedBoundingBox]:
    if task_type != TaskType.ORIENTED_DETECTION:
        return []
    rectangle_labels = value.get("rectanglelabels")
    if not isinstance(rectangle_labels, list) or not rectangle_labels:
        return []
    label = next((entry for entry in rectangle_labels if isinstance(entry, str) and entry), None)
    if label is None:
        return []
    required_fields = ("x", "y", "width", "height")
    if any(field not in value for field in required_fields):
        raise ValueError("The oriented detection annotation is missing one or more bounding-box coordinates.")

    original_width = _coerce_int(value.get("original_width"))
    original_height = _coerce_int(value.get("original_height"))
    normalized_x = float(value["x"]) / 100.0
    normalized_y = float(value["y"]) / 100.0
    normalized_width = float(value["width"]) / 100.0
    normalized_height = float(value["height"]) / 100.0
    rotation = _coerce_float(value.get("rotation")) or 0.0
    points = _rotated_rectangle_points(
        x=normalized_x,
        y=normalized_y,
        width=normalized_width,
        height=normalized_height,
        rotation_degrees=rotation,
        original_width=original_width,
        original_height=original_height,
    )
    return [
        OrientedBoundingBox(
            class_name=label,
            points=points,
            rotation=rotation,
            x=normalized_x,
            y=normalized_y,
            width=normalized_width,
            height=normalized_height,
            original_width=original_width,
            original_height=original_height,
        )
    ]


def _rotated_rectangle_points(
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    rotation_degrees: float,
    original_width: int | None,
    original_height: int | None,
) -> list[PolygonPoint]:
    image_width = max(int(original_width or 100), 1)
    image_height = max(int(original_height or 100), 1)
    origin_x = x * image_width
    origin_y = y * image_height
    box_width = width * image_width
    box_height = height * image_height
    angle = math.radians(rotation_degrees)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    points_px = []
    for offset_x, offset_y in (
        (0.0, 0.0),
        (box_width, 0.0),
        (box_width, box_height),
        (0.0, box_height),
    ):
        rotated_x = origin_x + (offset_x * cos_a) - (offset_y * sin_a)
        rotated_y = origin_y + (offset_x * sin_a) + (offset_y * cos_a)
        points_px.append((rotated_x, rotated_y))
    return [
        PolygonPoint(
            x=_clip01(point_x / image_width),
            y=_clip01(point_y / image_height),
        )
        for point_x, point_y in points_px
    ]


def _extract_polygons(task_type: TaskType, value: dict[str, Any]) -> list[SegmentationPolygon]:
    if task_type != TaskType.SEGMENTATION:
        return []
    polygon_labels = value.get("polygonlabels")
    if not isinstance(polygon_labels, list) or not polygon_labels:
        return []
    label = next((entry for entry in polygon_labels if isinstance(entry, str) and entry), None)
    points = value.get("points")
    if label is None or not isinstance(points, list) or not points:
        raise ValueError("The segmentation annotation is missing polygon labels or points.")
    normalized_points: list[PolygonPoint] = []
    for point in points:
        if not isinstance(point, list) or len(point) < 2:
            raise ValueError("A segmentation polygon point is malformed.")
        normalized_points.append(PolygonPoint(x=float(point[0]) / 100.0, y=float(point[1]) / 100.0))
    return [
        SegmentationPolygon(
            class_name=label,
            points=normalized_points,
            original_width=_coerce_int(value.get("original_width")),
            original_height=_coerce_int(value.get("original_height")),
        )
    ]


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clip01(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _assign_split(*, seed: str, train_ratio: int, val_ratio: int, test_ratio: int) -> str:
    bucket = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % 100
    if bucket < train_ratio:
        return "train"
    if bucket < train_ratio + val_ratio:
        return "val"
    return "test"


def _assign_balanced_splits(
    *,
    dataset_id: str,
    items: list[CanonicalItem],
    train_ratio: int,
    val_ratio: int,
    test_ratio: int,
) -> None:
    eligible_items = [
        item for item in items if item.eligibility_status.startswith("eligible")
    ]
    if not eligible_items:
        return
    split_counts = _balanced_split_counts(
        item_count=len(eligible_items),
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        test_ratio=test_ratio,
    )
    ordered_items = sorted(
        eligible_items,
        key=lambda item: hashlib.sha256(
            f"{dataset_id}:{item.task_id}:{item.relative_path}".encode("utf-8")
        ).hexdigest(),
    )
    offset = 0
    for split in ("train", "val", "test"):
        count = split_counts[split]
        for item in ordered_items[offset : offset + count]:
            item.split = split
        offset += count


def _balanced_split_counts(
    *,
    item_count: int,
    train_ratio: int,
    val_ratio: int,
    test_ratio: int,
) -> dict[str, int]:
    if item_count <= 0:
        return {"train": 0, "val": 0, "test": 0}
    ratios = {
        "train": max(int(train_ratio), 0),
        "val": max(int(val_ratio), 0),
        "test": max(int(test_ratio), 0),
    }
    total_ratio = sum(ratios.values())
    if total_ratio <= 0:
        return {"train": item_count, "val": 0, "test": 0}

    ideals = {
        split: item_count * (ratio / total_ratio)
        for split, ratio in ratios.items()
    }
    counts = {split: int(value) for split, value in ideals.items()}
    remainder = item_count - sum(counts.values())
    priority = {"train": 0, "val": 1, "test": 2}
    for split in sorted(
        ratios,
        key=lambda item: (-(ideals[item] - counts[item]), priority[item]),
    )[:remainder]:
        counts[split] += 1

    active_splits = [split for split, ratio in ratios.items() if ratio > 0]
    if item_count >= len(active_splits):
        for split in active_splits:
            if counts[split] > 0:
                continue
            donor = max(
                (candidate for candidate in active_splits if counts[candidate] > 1),
                key=lambda candidate: (counts[candidate], ratios[candidate]),
                default=None,
            )
            if donor is None:
                continue
            counts[donor] -= 1
            counts[split] = 1
    return counts
