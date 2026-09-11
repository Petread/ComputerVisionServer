from __future__ import annotations

import json

import pytest
from cv_contracts import DatasetCreateRequest
from cv_datasets import (
    AnnotationFieldSpec,
    AnnotationSchemaDefinition,
    AnnotationSchemaError,
    CanonicalDataset,
    CanonicalSequenceDataset,
    CanonicalSequenceItem,
    HumanAnnotationSet,
    RegisteredAnnotationSchema,
    RequiredWhen,
    RotationTraceSample,
    SequenceArtifactReference,
    SequenceFrame,
    SequenceLabelQuality,
    SequenceProvenance,
    SequenceSchemaVersions,
    TargetBuildContext,
    TargetBuildResult,
    convert_label_studio_export,
    get_annotation_schema,
    list_annotation_schemas,
)
from cv_db import create_dataset, get_dataset_record, list_datasets
from cv_status import TaskType
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _frames(count: int = 5) -> list[SequenceFrame]:
    return [SequenceFrame(frame_index=index, timestamp=index / 10.0) for index in range(count)]


def _label_studio_results(
    *,
    sequence_valid: str = "usable",
    bow_relevance: str = "relevant",
    target_visible: str = "yes",
    target_range: tuple[int, int] | None = (1, 2),
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = [
        {"from_name": "sequence_valid", "value": {"choices": [sequence_valid]}},
        {"from_name": "bow_relevance", "value": {"choices": [bow_relevance]}},
        {"from_name": "target_visible", "value": {"choices": [target_visible]}},
        {
            "from_name": "final_orientation_quality",
            "value": {"choices": ["acceptable"]},
        },
        {
            "from_name": "special_case",
            "value": {"choices": ["motion_blur", "s_bow"]},
        },
        {"from_name": "comment", "value": {"text": ["reviewed"]}},
    ]
    if target_range is not None:
        results.append(
            {
                "from_name": "target_orientation",
                "value": {
                    "ranges": [
                        {
                            "start": target_range[0],
                            "end": target_range[1],
                        }
                    ],
                    "timelinelabels": ["target_orientation"],
                },
            }
        )
    return results


def _sequence_item(
    *,
    human_labels: HumanAnnotationSet,
    frames: list[SequenceFrame] | None = None,
) -> CanonicalSequenceItem:
    sequence_frames = frames or _frames()
    return CanonicalSequenceItem(
        sequence_id="sequence-001",
        group_id="physical-stem-001",
        primary_video=SequenceArtifactReference(
            uri="dataset://sequence-001/sequence.mp4",
            sha256=SHA_A,
            media_type="video/mp4",
        ),
        diagnostic_assets=[
            SequenceArtifactReference(
                uri="dataset://sequence-001/diagnostic.mp4",
                sha256=SHA_B,
                media_type="video/mp4",
            )
        ],
        frames=sequence_frames,
        decision_frame_index=sequence_frames[-1].frame_index,
        decision_timestamp=sequence_frames[-1].timestamp,
        feature_manifest=SequenceArtifactReference(
            uri="dataset://sequence-001/features.jsonl",
            sha256=SHA_B,
            media_type="application/x-ndjson",
        ),
        rotation_trace=SequenceArtifactReference(
            uri="dataset://sequence-001/rotation-trace.jsonl",
            sha256=SHA_C,
            media_type="application/x-ndjson",
        ),
        schemas=SequenceSchemaVersions(
            feature_schema="stem_features.v1",
            rotation_schema="stem_surface_rotation.v1",
            calibration_schema="stem_rotation_calibration.v1",
            annotation_schema="stem_orientation_angle.v1",
        ),
        human_labels=human_labels,
        derived_targets=[],
        label_source="label_studio",
        label_quality=SequenceLabelQuality(status="verified", score=0.95),
        split="train",
        provenance=SequenceProvenance(
            source_system="runtime_recorder",
            recording_id="recording-001",
            created_at="2026-08-04T10:00:00Z",
            software_version="test",
            source_channel_id="channel-001",
            pipeline_version_id="pipeline-version-001",
            runtime_session_id="session-001",
            track_id="track-001",
            component_versions={"geometry": "v1", "rotation": "v1"},
            attributes={
                "decision_line_version": "v1",
                "rotation_viewpoint_convention": "viewed_from_configured_log_end_a",
                "positive_rotation_direction": (
                    "clockwise_viewed_from_configured_log_end"
                ),
            },
        ),
    )


def _empty_target_builder(
    _item: CanonicalSequenceItem,
    _context: TargetBuildContext,
) -> TargetBuildResult:
    return TargetBuildResult()


def test_temporal_sequence_task_type_can_be_created_read_and_listed(
    sqlite_engine: Engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        created = create_dataset(
            session,
            DatasetCreateRequest(
                name="General Temporal Sequences",
                slug="general-temporal-sequences",
                task_type=TaskType.TEMPORAL_SEQUENCE,
                label_schema_version="stem_orientation_angle.v1",
            ),
            created_by="test",
        )
        session.commit()

        stored = get_dataset_record(session, created.dataset_id)
        listed = list_datasets(session)

    assert stored is not None
    assert stored.task_type == TaskType.TEMPORAL_SEQUENCE.value
    assert [item.dataset_id for item in listed] == [created.dataset_id]
    assert listed[0].task_type == TaskType.TEMPORAL_SEQUENCE


def test_image_export_converter_rejects_temporal_sequence_import() -> None:
    with pytest.raises(
        ValueError, match="Temporal sequence Label Studio import is not implemented"
    ):
        convert_label_studio_export(
            dataset_id="dataset-temporal-001",
            task_type=TaskType.TEMPORAL_SEQUENCE.value,
            export_payload=[],
            assets=[],
            label_config=None,
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
        )


def test_stem_schema_registry_and_label_studio_config_are_deterministic() -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")

    first = schema.label_studio_config()
    second = get_annotation_schema("stem_orientation_angle.v1").label_studio_config()

    assert [item.definition.schema_id for item in list_annotation_schemas()] == [
        "stem_orientation_angle.v1"
    ]
    assert first == second
    assert '<Video name="video" value="$video"' in first
    assert '<TimelineLabels name="target_orientation" toName="video">' in first
    assert 'name="special_case"' in first
    assert "degree" not in first.casefold()
    assert "grad" not in first.casefold()


def test_timeline_intervals_and_sequence_choices_are_parsed_and_validated() -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")
    frames = _frames()

    annotations = schema.parse_label_studio_results(
        _label_studio_results(),
        frames=frames,
        decision_frame_index=4,
    )

    values = {item.name: getattr(item, "value", None) for item in annotations.sequence_labels}
    interval = annotations.timeline_labels[0]
    assert values["sequence_valid"] == "usable"
    assert values["bow_relevance"] == "relevant"
    assert interval.name == "target_orientation"
    assert interval.start_frame_index == 1
    assert interval.end_frame_index == 2
    assert interval.start_timestamp == 0.1
    assert interval.end_timestamp == 0.2


def test_general_registry_supports_boolean_points_multiple_choices_and_text() -> None:
    schema = RegisteredAnnotationSchema(
        definition=AnnotationSchemaDefinition(
            schema_id="general_sequence_review.v1",
            title="General sequence review",
            description="Exercises the reusable temporal annotation field types.",
            target_builder_id="empty",
            target_builder_version="v1",
            fields=[
                AnnotationFieldSpec(
                    name="accepted",
                    label="Accepted",
                    kind="boolean",
                    required=True,
                ),
                AnnotationFieldSpec(
                    name="event",
                    label="Event",
                    kind="timeline_point",
                    required_when=[RequiredWhen(field="accepted", equals=True)],
                ),
                AnnotationFieldSpec(
                    name="tags",
                    label="Tags",
                    kind="multiple_choice",
                    choices=["one", "two"],
                ),
                AnnotationFieldSpec(name="note", label="Note", kind="text"),
            ],
        ),
        target_builder=_empty_target_builder,
    )

    annotations = schema.parse_label_studio_results(
        [
            {"from_name": "accepted", "value": {"choices": ["true"]}},
            {"from_name": "event", "value": {"ranges": [{"start": 2, "end": 2}]}},
            {"from_name": "tags", "value": {"choices": ["two", "one"]}},
            {"from_name": "note", "value": {"text": "typed"}},
        ],
        frames=_frames(),
        decision_frame_index=4,
    )

    assert [annotation.kind for annotation in annotations.sequence_labels] == [
        "boolean",
        "multiple_choice",
        "text",
    ]
    assert annotations.timeline_labels[0].kind == "timeline_point"
    assert annotations.timeline_labels[0].frame_index == 2


def test_relevant_visible_sequence_requires_exactly_one_target_interval() -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")

    with pytest.raises(AnnotationSchemaError, match="target_orientation.*missing"):
        schema.parse_label_studio_results(
            _label_studio_results(target_range=None),
            frames=_frames(),
            decision_frame_index=4,
        )


def test_target_interval_after_decision_frame_is_rejected() -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")

    with pytest.raises(AnnotationSchemaError, match="beyond decision_frame_index"):
        schema.parse_label_studio_results(
            _label_studio_results(target_range=(2, 4)),
            frames=_frames(),
            decision_frame_index=3,
        )


def test_canonical_sequence_manifest_references_assets_and_separates_targets() -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")
    frames = _frames()
    labels = schema.parse_label_studio_results(
        _label_studio_results(),
        frames=frames,
        decision_frame_index=4,
    )
    item = _sequence_item(human_labels=labels, frames=frames)
    dataset = CanonicalSequenceDataset(
        dataset_id="dataset-temporal-001",
        annotation_schema="stem_orientation_angle.v1",
        items=[item],
    )

    serialized = dataset.model_dump(mode="json")
    assert dataset.task_type == TaskType.TEMPORAL_SEQUENCE.value
    assert serialized["items"][0]["primary_video"]["uri"].endswith("sequence.mp4")
    assert serialized["items"][0]["human_labels"]["timeline_labels"]
    assert serialized["items"][0]["derived_targets"] == []
    assert "video_bytes" not in json.dumps(serialized)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CanonicalSequenceItem.model_validate(
            {
                **item.model_dump(),
                "inline_video": "not-allowed",
            }
        )


def test_versioned_target_builder_derives_angle_from_interval_and_rotation_trace() -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")
    frames = _frames()
    labels = schema.parse_label_studio_results(
        _label_studio_results(),
        frames=frames,
        decision_frame_index=4,
    )
    item = _sequence_item(human_labels=labels, frames=frames)
    trace = [
        RotationTraceSample(
            frame_index=frame.frame_index,
            timestamp=frame.timestamp,
            cumulative_rotation_deg_unwrapped=frame.frame_index * 10.0,
            rotation_confidence=0.9,
            calibration_id="rotation-calibration-001",
            calibration_method="known_test_angles",
            calibration_valid=True,
        )
        for frame in frames
    ]

    result = schema.build_targets(
        item,
        trace,
        minimum_rotation_confidence=0.8,
    )

    angle_target = next(
        target for target in result.targets if target.name == "recommended_rotation_deg_signed"
    )
    assert result.angle_target_generated is True
    assert angle_target.builder_id == "stem_orientation_angle"
    assert angle_target.builder_version == "v1"
    assert angle_target.value == pytest.approx(-25.0)
    final_quality_target = next(
        target for target in result.targets if target.name == "final_orientation_quality"
    )
    assert final_quality_target.value == "acceptable"
    assert final_quality_target.scope == "decision_window"


def test_target_builder_interpolates_sparse_rotation_trace_at_labeled_frame() -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")
    frames = _frames()
    labels = schema.parse_label_studio_results(
        _label_studio_results(target_range=(1, 1)),
        frames=frames,
        decision_frame_index=4,
    )
    trace = [
        RotationTraceSample(
            frame_index=index,
            timestamp=frames[index].timestamp,
            cumulative_rotation_deg_unwrapped=index * 10.0,
            rotation_confidence=0.9,
            calibration_id="rotation-calibration-001",
            calibration_method="known_test_angles",
            calibration_valid=True,
        )
        for index in (0, 2, 4)
    ]

    result = schema.build_targets(
        _sequence_item(human_labels=labels, frames=frames),
        trace,
        minimum_rotation_confidence=0.8,
    )

    targets = {target.name: target for target in result.targets}
    assert targets["signed_correction_deg"].value == pytest.approx(-30.0)
    assert targets["target_angle_interval_deg"].value == pytest.approx([-30.0, -30.0])


@pytest.mark.parametrize(
    ("target_rotation", "expected"),
    [(89.0, 89.0), (91.0, -89.0), (-89.0, -89.0), (-91.0, 89.0)],
)
def test_target_builder_wraps_half_turn_without_boundary_jump(
    target_rotation: float,
    expected: float,
) -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")
    frames = _frames()
    labels = schema.parse_label_studio_results(
        _label_studio_results(target_range=(1, 1)),
        frames=frames,
        decision_frame_index=4,
    )
    angles = [0.0, target_rotation, target_rotation / 2.0, 0.0, 0.0]
    trace = [
        RotationTraceSample(
            frame_index=frame.frame_index,
            timestamp=frame.timestamp,
            cumulative_rotation_deg_unwrapped=angles[frame.frame_index],
            rotation_confidence=0.95,
            calibration_id="rotation-calibration-001",
            calibration_method="known_test_angles",
            calibration_valid=True,
        )
        for frame in frames
    ]

    result = schema.build_targets(
        _sequence_item(human_labels=labels, frames=frames),
        trace,
        minimum_rotation_confidence=0.8,
    )

    target = next(target for target in result.targets if target.name == "signed_correction_deg")
    assert target.value == pytest.approx(expected)


def test_target_builder_emits_no_angle_for_unreliable_rotation_trace() -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")
    frames = _frames()
    labels = schema.parse_label_studio_results(
        _label_studio_results(),
        frames=frames,
        decision_frame_index=4,
    )
    item = _sequence_item(human_labels=labels, frames=frames)
    trace = [
        RotationTraceSample(
            frame_index=frame.frame_index,
            timestamp=frame.timestamp,
            cumulative_rotation_deg_unwrapped=frame.frame_index * 10.0,
            rotation_confidence=0.4 if frame.frame_index == 4 else 0.9,
            calibration_id="rotation-calibration-001",
            calibration_method="known_test_angles",
            calibration_valid=True,
        )
        for frame in frames
    ]

    result = schema.build_targets(
        item,
        trace,
        minimum_rotation_confidence=0.8,
    )

    assert result.angle_target_generated is False
    assert "rotation_confidence_too_low" in result.reason_codes
    assert all(target.kind != "regression" for target in result.targets)


def test_target_builder_emits_no_angle_without_direction_convention() -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")
    frames = _frames()
    labels = schema.parse_label_studio_results(
        _label_studio_results(),
        frames=frames,
        decision_frame_index=4,
    )
    item = _sequence_item(human_labels=labels, frames=frames)
    item = item.model_copy(
        update={
            "provenance": item.provenance.model_copy(
                update={"attributes": {"decision_line_version": "v1"}}
            )
        }
    )

    result = schema.build_targets(
        item,
        [],
        minimum_rotation_confidence=0.8,
    )

    targets = {target.name: target for target in result.targets}
    assert targets["angle_target_valid"].value is False
    assert "rotation_direction_convention_missing" in result.reason_codes
    assert "signed_correction_deg" not in targets


@pytest.mark.parametrize(
    ("bow_relevance", "target_visible", "expected_reason"),
    [
        ("nearly_straight", "yes", "angle_target_not_required_nearly_straight"),
        ("relevant", "no", "target_orientation_not_visible"),
        ("relevant", "unclear", "target_orientation_not_visible"),
    ],
)
def test_target_builder_keeps_non_angle_samples_without_inventing_an_angle(
    bow_relevance: str,
    target_visible: str,
    expected_reason: str,
) -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")
    frames = _frames()
    labels = schema.parse_label_studio_results(
        _label_studio_results(
            bow_relevance=bow_relevance,
            target_visible=target_visible,
            target_range=None,
        ),
        frames=frames,
        decision_frame_index=4,
    )

    result = schema.build_targets(
        _sequence_item(human_labels=labels, frames=frames),
        [],
        minimum_rotation_confidence=0.8,
    )

    assert result.exclusion_reason is None
    assert result.angle_target_generated is False
    assert expected_reason in result.reason_codes
    assert any(target.name == "bow_relevance" for target in result.targets)
    assert all(target.kind != "regression" for target in result.targets)


def test_unusable_sequence_is_fully_excluded_by_target_builder() -> None:
    schema = get_annotation_schema("stem_orientation_angle.v1")
    frames = _frames()
    labels = schema.parse_label_studio_results(
        _label_studio_results(sequence_valid="unusable", target_range=None),
        frames=frames,
        decision_frame_index=4,
    )

    result = schema.build_targets(
        _sequence_item(human_labels=labels, frames=frames),
        [],
        minimum_rotation_confidence=0.8,
    )

    assert result.exclusion_reason == "sequence_unusable"
    assert result.targets == []
    assert result.angle_target_generated is False


def test_existing_image_dataset_serialization_has_no_temporal_fields() -> None:
    dataset = CanonicalDataset(
        dataset_id="dataset-image-001",
        task_type="classification",
        source_task_count=0,
        annotation_count=0,
    )

    assert set(dataset.model_dump()) == {
        "dataset_id",
        "task_type",
        "source_task_count",
        "annotation_count",
        "eligible_item_count",
        "submitted_labeled_item_count",
        "submitted_background_item_count",
        "draft_item_count",
        "invalid_item_count",
        "rotated_rectangle_count",
        "items",
        "class_distribution",
        "anomaly_distribution",
        "label_map",
    }
