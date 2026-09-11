from __future__ import annotations

from pathlib import Path

from cv_status import TaskType

from cv_datasets.models import (
    CanonicalDataset,
    CanonicalSequenceDataset,
    SplitSummary,
    ValidationIssue,
    ValidationResult,
)


def validate_canonical_dataset(
    *,
    dataset: CanonicalDataset,
    split_summary: SplitSummary,
    issues: list[ValidationIssue],
    declared_classes: list[str],
    required_classes: list[str],
    minimum_annotation_count: int,
    task_type: str,
    dataset_label_schema_version: str,
    project_label_schema_version: str | None,
) -> ValidationResult:
    collected_issues = list(issues)
    seen_paths: set[str] = set()
    training_target_names: set[str] = set()
    normalized_task_type = TaskType(task_type)

    eligible_items = [
        item for item in dataset.items if item.eligibility_status in {"eligible_labeled", "eligible_background"}
    ]

    for item in eligible_items:
        path_key = Path(item.relative_path).as_posix()
        if path_key in seen_paths:
            collected_issues.append(
                ValidationIssue(
                    severity="error",
                    code="duplicate_file_collision",
                    message="The same dataset file appears more than once in the snapshot.",
                    item_ref=item.item_id,
                    metadata={"relative_path": item.relative_path},
                )
            )
        seen_paths.add(path_key)

        if normalized_task_type in {
            TaskType.DETECTION,
            TaskType.ORIENTED_DETECTION,
            TaskType.SEGMENTATION,
        }:
            target_name = f"{item.split}/{Path(item.relative_path).stem}.txt"
            if target_name in training_target_names:
                collected_issues.append(
                    ValidationIssue(
                        severity="error",
                        code="duplicate_file_collision",
                        message="Two eligible items would produce the same training label file name.",
                        item_ref=item.item_id,
                        metadata={"target_name": target_name},
                    )
                )
            training_target_names.add(target_name)

    declared = set(declared_classes)
    observed = set(dataset.class_distribution)
    normalized_required_classes = list(required_classes)
    if normalized_task_type == TaskType.ANOMALY:
        declared = {
            value
            for item in declared_classes
            for value in [_normalize_anomaly_label(item)]
            if value is not None
        }
        observed = set(dataset.anomaly_distribution or dataset.class_distribution)
        normalized_required_classes = [
            value
            for item in required_classes
            for value in [_normalize_anomaly_label(item)]
            if value is not None
        ]
    if declared and not observed.issubset(declared):
        extra = sorted(observed - declared)
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="class_map_mismatch",
                message="The snapshot contains labels that are not present in the configured Label Studio schema.",
                metadata={"unexpected_classes": extra},
            )
        )

    missing_required = sorted(set(normalized_required_classes) - observed)
    if missing_required:
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="required_classes_missing",
                message="One or more required classes are absent from the eligible snapshot items.",
                metadata={"missing_classes": missing_required},
            )
        )

    if dataset.eligible_item_count < minimum_annotation_count:
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="minimum_annotation_count",
                message="The snapshot does not meet the minimum eligible item count for training readiness.",
                metadata={
                    "required": minimum_annotation_count,
                    "observed": dataset.eligible_item_count,
                },
            )
        )

    split_total = split_summary.train_count + split_summary.val_count + split_summary.test_count
    if split_total != len(eligible_items):
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="split_integrity",
                message="The split counts do not add up to the number of eligible materialized items.",
                metadata={"eligible_item_count": len(eligible_items), "split_total": split_total},
            )
        )
    if eligible_items and split_summary.train_count == 0:
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="split_integrity",
                message="The snapshot has no training items after split assignment.",
            )
        )
    if not eligible_items:
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="no_eligible_items",
                message="No eligible finalized items are available for training.",
            )
        )
    anomaly_checks: dict[str, bool] = {}
    if normalized_task_type == TaskType.ANOMALY:
        eligible_labeled = [
            item for item in dataset.items if item.eligibility_status == "eligible_labeled"
        ]
        normal_items = [
            item
            for item in eligible_labeled
            if (item.anomaly_label or (item.labels[0] if item.labels else None)) == "normal"
        ]
        anomalous_items = [
            item
            for item in eligible_labeled
            if (item.anomaly_label or (item.labels[0] if item.labels else None)) == "anomalous"
        ]
        train_normal = [item for item in normal_items if item.split == "train"]
        eval_normal = [item for item in normal_items if item.split in {"val", "test"}]
        eval_anomalous = [item for item in anomalous_items if item.split in {"val", "test"}]
        anomaly_checks = {
            "anomaly_normal_present": bool(normal_items),
            "anomaly_abnormal_present": bool(anomalous_items),
            "anomaly_train_normal_present": bool(train_normal),
            "anomaly_eval_normal_present": bool(eval_normal),
            "anomaly_eval_abnormal_present": bool(eval_anomalous),
            "anomaly_no_background_samples": dataset.submitted_background_item_count == 0,
        }
        if not normal_items:
            collected_issues.append(
                ValidationIssue(
                    severity="error",
                    code="anomaly_normal_missing",
                    message="Anomaly training requires at least one good/normal image.",
                )
            )
        if not anomalous_items:
            collected_issues.append(
                ValidationIssue(
                    severity="error",
                    code="anomaly_abnormal_missing",
                    message="Anomaly training requires at least one bad/anomalous image.",
                )
            )
        if not train_normal:
            collected_issues.append(
                ValidationIssue(
                    severity="error",
                    code="anomaly_train_normal_missing",
                    message="PatchCore anomaly training requires normal images in the train split.",
                )
            )
        if not eval_normal or not eval_anomalous:
            collected_issues.append(
                ValidationIssue(
                    severity="error",
                    code="anomaly_eval_split_missing",
                    message=(
                        "Anomaly evaluation requires both normal and anomalous images in "
                        "val or test splits."
                    ),
                )
            )

    schema_compatible = (
        project_label_schema_version is None
        or project_label_schema_version == dataset_label_schema_version
    )
    if not schema_compatible:
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="schema_incompatible",
                message="The project mapping schema version does not match the dataset schema version.",
                metadata={
                    "dataset_label_schema_version": dataset_label_schema_version,
                    "project_label_schema_version": project_label_schema_version,
                },
            )
        )

    checks = {
        "minimum_annotation_count": dataset.eligible_item_count >= minimum_annotation_count,
        "required_classes_present": not missing_required,
        "no_duplicate_file_collisions": not any(
            issue.code == "duplicate_file_collision" and issue.severity == "error"
            for issue in collected_issues
        ),
        "schema_compatible": schema_compatible,
        "split_integrity": not any(issue.code == "split_integrity" for issue in collected_issues),
        "eligible_items_present": bool(eligible_items),
        "missing_files": not any(issue.code == "missing_file" for issue in collected_issues),
        "invalid_annotations": not any(
            issue.code == "invalid_annotation" for issue in collected_issues
        ),
        "broken_references": not any(
            issue.code == "broken_reference" for issue in collected_issues
        ),
        "class_map_matches": not any(
            issue.code == "class_map_mismatch" for issue in collected_issues
        ),
        **anomaly_checks,
    }
    error_count = sum(1 for issue in collected_issues if issue.severity == "error")
    warning_count = sum(1 for issue in collected_issues if issue.severity == "warning")
    return ValidationResult(
        status="passed" if error_count == 0 else "failed",
        checks=checks,
        issues=collected_issues,
        warning_count=warning_count,
        error_count=error_count,
    )


def validate_temporal_sequence_dataset(
    *,
    dataset: CanonicalSequenceDataset,
    split_summary: SplitSummary,
    issues: list[ValidationIssue],
    minimum_annotation_count: int,
    dataset_label_schema_version: str,
    project_label_schema_version: str | None,
) -> ValidationResult:
    collected_issues = list(issues)
    eligible_items = [item for item in dataset.items if item.exclusion_reason is None]
    asset_owners: dict[str, list[tuple[str, str]]] = {}
    for item in dataset.items:
        normalized_hash = item.primary_video.sha256.removeprefix("sha256:").lower()
        asset_owners.setdefault(normalized_hash, []).append((item.sequence_id, item.split))
        if item.frames[-1].frame_index != item.decision_frame_index:
            collected_issues.append(
                ValidationIssue(
                    severity="error",
                    code="post_decision_frame_present",
                    message="The primary temporal input contains a frame after the decision frame.",
                    item_ref=item.sequence_id,
                )
            )

    for asset_hash, owners in asset_owners.items():
        if len(owners) < 2:
            continue
        splits = {split for _, split in owners}
        collected_issues.append(
            ValidationIssue(
                severity="error" if len(splits) > 1 else "warning",
                code=(
                    "duplicate_asset_hash_across_splits"
                    if len(splits) > 1
                    else "duplicate_asset_hash"
                ),
                message=(
                    "The same primary video hash occurs in more than one split."
                    if len(splits) > 1
                    else "The same primary video hash occurs more than once in the snapshot."
                ),
                metadata={
                    "sha256": asset_hash,
                    "sequence_ids": sorted(sequence_id for sequence_id, _ in owners),
                    "splits": sorted(splits),
                },
            )
        )

    _append_time_overlap_issues(dataset, collected_issues)
    schema_compatible = (
        project_label_schema_version is None
        or project_label_schema_version == dataset_label_schema_version
    )
    if not schema_compatible:
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="schema_incompatible",
                message=(
                    "The project mapping schema version does not match the temporal dataset "
                    "schema version."
                ),
                metadata={
                    "dataset_label_schema_version": dataset_label_schema_version,
                    "project_label_schema_version": project_label_schema_version,
                },
            )
        )
    if len(eligible_items) < minimum_annotation_count:
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="minimum_annotation_count",
                message=(
                    "The temporal snapshot does not meet the minimum eligible sequence count."
                ),
                metadata={
                    "required": minimum_annotation_count,
                    "observed": len(eligible_items),
                },
            )
        )
    split_total = split_summary.train_count + split_summary.val_count + split_summary.test_count
    if split_total != len(eligible_items):
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="split_integrity",
                message="Temporal split counts do not match the eligible sequence count.",
            )
        )
    if eligible_items and split_summary.train_count == 0:
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="split_integrity",
                message="The temporal snapshot has no training sequences.",
            )
        )
    if not eligible_items:
        collected_issues.append(
            ValidationIssue(
                severity="error",
                code="no_eligible_items",
                message="No eligible finalized temporal sequences are available for training.",
            )
        )

    error_count = sum(issue.severity == "error" for issue in collected_issues)
    warning_count = sum(issue.severity == "warning" for issue in collected_issues)
    checks = {
        "minimum_annotation_count": len(eligible_items) >= minimum_annotation_count,
        "schema_compatible": schema_compatible,
        "group_split_integrity": True,
        "no_post_decision_frames": not any(
            issue.code == "post_decision_frame_present" for issue in collected_issues
        ),
        "no_duplicate_asset_hashes_across_splits": not any(
            issue.code == "duplicate_asset_hash_across_splits"
            for issue in collected_issues
        ),
        "no_time_overlap_across_splits": not any(
            issue.code == "source_time_overlap_across_splits"
            for issue in collected_issues
        ),
        "checksums_valid": not any(
            "checksum" in issue.code and issue.severity == "error"
            for issue in collected_issues
        ),
        "split_integrity": not any(
            issue.code == "split_integrity" for issue in collected_issues
        ),
        "eligible_items_present": bool(eligible_items),
        "fit_statistics_train_only": True,
    }
    return ValidationResult(
        status="passed" if error_count == 0 else "failed",
        checks=checks,
        issues=collected_issues,
        warning_count=warning_count,
        error_count=error_count,
    )


def _append_time_overlap_issues(
    dataset: CanonicalSequenceDataset,
    issues: list[ValidationIssue],
) -> None:
    windows: list[tuple[str, str, float, float, str]] = []
    for item in dataset.items:
        attributes = item.provenance.attributes
        channel = item.provenance.source_channel_id
        start = attributes.get("source_start_epoch_seconds")
        end = attributes.get("source_end_epoch_seconds")
        if (
            not channel
            or isinstance(start, bool)
            or not isinstance(start, int | float)
            or isinstance(end, bool)
            or not isinstance(end, int | float)
        ):
            continue
        windows.append((item.sequence_id, channel, float(start), float(end), item.split))
    for index, left in enumerate(windows):
        for right in windows[index + 1 :]:
            if left[1] != right[1] or left[4] == right[4]:
                continue
            if max(left[2], right[2]) <= min(left[3], right[3]):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="source_time_overlap_across_splits",
                        message=(
                            "Source time windows overlap across temporal dataset splits."
                        ),
                        metadata={
                            "sequence_ids": sorted([left[0], right[0]]),
                            "channel_id": left[1],
                            "splits": sorted([left[4], right[4]]),
                        },
                    )
                )


def _normalize_anomaly_label(value: str) -> str | None:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"good", "normal", "ok", "okay", "pass", "passed", "accepted", "healthy"}:
        return "normal"
    if normalized in {
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
        "rejected",
        "rework",
    }:
        return "anomalous"
    return None
