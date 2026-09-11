from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SEQUENCE_FRAMES = 100_000


class DatasetAssetRecord(BaseModel):
    asset_id: str
    relative_path: str
    sha256: str
    source_path: str
    width: int | None = None
    height: int | None = None


class BoundingBox(BaseModel):
    class_name: str
    x: float
    y: float
    width: float
    height: float
    original_width: int | None = None
    original_height: int | None = None


class PolygonPoint(BaseModel):
    x: float
    y: float


class OrientedBoundingBox(BaseModel):
    class_name: str
    points: list[PolygonPoint] = Field(default_factory=list)
    rotation: float | None = None
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    original_width: int | None = None
    original_height: int | None = None


class SegmentationPolygon(BaseModel):
    class_name: str
    points: list[PolygonPoint] = Field(default_factory=list)
    original_width: int | None = None
    original_height: int | None = None


class CanonicalItem(BaseModel):
    item_id: str
    task_id: str
    relative_path: str
    source_path: str
    asset_sha256: str
    split: Literal["train", "val", "test"]
    eligibility_status: Literal["eligible_labeled", "eligible_background", "draft", "invalid"]
    submitted: bool = False
    background: bool = False
    exclusion_reason: str | None = None
    labels: list[str] = Field(default_factory=list)
    anomaly_label: Literal["normal", "anomalous"] | None = None
    boxes: list[BoundingBox] = Field(default_factory=list)
    oriented_boxes: list[OrientedBoundingBox] = Field(default_factory=list)
    polygons: list[SegmentationPolygon] = Field(default_factory=list)
    raw_annotation_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class StrictSequenceModel(BaseModel):
    """Strict base for new manifest-only temporal dataset contracts."""

    model_config = ConfigDict(extra="forbid")


class SequenceArtifactReference(StrictSequenceModel):
    uri: str = Field(min_length=1, max_length=2_048)
    sha256: str = Field(pattern=r"^(?:sha256:)?[0-9a-fA-F]{64}$")
    media_type: str | None = Field(default=None, min_length=1, max_length=128)


class SequenceFrame(StrictSequenceModel):
    frame_index: int = Field(ge=0)
    timestamp: float = Field(ge=0.0)


class SequenceChoiceAnnotation(StrictSequenceModel):
    kind: Literal["sequence_choice"] = "sequence_choice"
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=256)


class BooleanAnnotation(StrictSequenceModel):
    kind: Literal["boolean"] = "boolean"
    name: str = Field(min_length=1, max_length=128)
    value: bool


class MultiChoiceAnnotation(StrictSequenceModel):
    kind: Literal["multiple_choice"] = "multiple_choice"
    name: str = Field(min_length=1, max_length=128)
    values: list[str] = Field(min_length=1, max_length=64)


class TextAnnotation(StrictSequenceModel):
    kind: Literal["text"] = "text"
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=10_000)


class TimelinePointAnnotation(StrictSequenceModel):
    kind: Literal["timeline_point"] = "timeline_point"
    name: str = Field(min_length=1, max_length=128)
    frame_index: int = Field(ge=0)
    timestamp: float | None = Field(default=None, ge=0.0)


class TimelineIntervalAnnotation(StrictSequenceModel):
    kind: Literal["timeline_interval"] = "timeline_interval"
    name: str = Field(min_length=1, max_length=128)
    start_frame_index: int = Field(ge=0)
    end_frame_index: int = Field(ge=0)
    start_timestamp: float | None = Field(default=None, ge=0.0)
    end_timestamp: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_bounds(self) -> TimelineIntervalAnnotation:
        if self.end_frame_index < self.start_frame_index:
            raise ValueError("timeline interval end_frame_index must not precede start_frame_index")
        timestamps = (self.start_timestamp, self.end_timestamp)
        if (timestamps[0] is None) != (timestamps[1] is None):
            raise ValueError(
                "timeline interval timestamps must either both be set or both be omitted"
            )
        if (
            timestamps[0] is not None
            and timestamps[1] is not None
            and timestamps[1] < timestamps[0]
        ):
            raise ValueError("timeline interval end_timestamp must not precede start_timestamp")
        return self


SequenceLevelAnnotation: TypeAlias = Annotated[  # noqa: UP040 - Python 3.11 support
    SequenceChoiceAnnotation | BooleanAnnotation | MultiChoiceAnnotation | TextAnnotation,
    Field(discriminator="kind"),
]
TimelineAnnotation: TypeAlias = Annotated[  # noqa: UP040 - Python 3.11 support
    TimelinePointAnnotation | TimelineIntervalAnnotation,
    Field(discriminator="kind"),
]


class HumanAnnotationSet(StrictSequenceModel):
    sequence_labels: list[SequenceLevelAnnotation] = Field(default_factory=list, max_length=256)
    timeline_labels: list[TimelineAnnotation] = Field(default_factory=list, max_length=256)
    source_annotation_id: str | None = Field(default=None, max_length=128)
    source_revision: str | None = Field(default=None, max_length=128)
    source_updated_at: str | None = Field(default=None, max_length=64)
    source_annotator: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_unique_names(self) -> HumanAnnotationSet:
        names = [item.name for item in self.sequence_labels]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"sequence-level annotation names must be unique: {', '.join(duplicates)}"
            )
        return self


DerivedTargetValue: TypeAlias = (  # noqa: UP040
    str | bool | int | float | list[str | bool | int | float]
)
DerivedTargetMetadataValue: TypeAlias = (  # noqa: UP040
    str | bool | int | float | None | list[str] | list[float]
)


class DerivedTarget(StrictSequenceModel):
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["classification", "boolean", "regression", "timeline_point", "timeline_interval"]
    value: DerivedTargetValue
    scope: Literal["sequence", "decision_frame", "decision_window", "timeline"]
    valid: bool = True
    builder_id: str = Field(min_length=1, max_length=128)
    builder_version: str = Field(min_length=1, max_length=64)
    source_annotation_schema: str = Field(min_length=1, max_length=128)
    quality: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list, max_length=64)
    metadata: dict[str, DerivedTargetMetadataValue] = Field(default_factory=dict)


class SequenceLabelQuality(StrictSequenceModel):
    status: Literal["unreviewed", "reviewed", "verified", "rejected"]
    tier: Literal["weak", "silver", "gold"] | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list, max_length=64)


class SequenceSchemaVersions(StrictSequenceModel):
    feature_schema: str = Field(min_length=1, max_length=128)
    rotation_schema: str = Field(min_length=1, max_length=128)
    calibration_schema: str = Field(min_length=1, max_length=128)
    annotation_schema: str = Field(min_length=1, max_length=128)


ProvenanceScalar: TypeAlias = str | int | float | bool | None  # noqa: UP040


class SequenceProvenance(StrictSequenceModel):
    source_system: str = Field(min_length=1, max_length=128)
    recording_id: str = Field(min_length=1, max_length=256)
    created_at: str = Field(min_length=1, max_length=64)
    software_version: str = Field(min_length=1, max_length=128)
    source_channel_id: str | None = Field(default=None, max_length=128)
    pipeline_version_id: str | None = Field(default=None, max_length=128)
    runtime_session_id: str | None = Field(default=None, max_length=128)
    track_id: str | None = Field(default=None, max_length=128)
    component_versions: dict[str, str] = Field(default_factory=dict)
    attributes: dict[str, ProvenanceScalar] = Field(default_factory=dict)


class CanonicalSequenceItem(StrictSequenceModel):
    """A bounded manifest item; media and traces are referenced, never embedded."""

    sequence_id: str = Field(min_length=1, max_length=256)
    group_id: str = Field(min_length=1, max_length=256)
    primary_video: SequenceArtifactReference
    diagnostic_assets: list[SequenceArtifactReference] = Field(default_factory=list, max_length=32)
    frames: list[SequenceFrame] = Field(min_length=1, max_length=MAX_SEQUENCE_FRAMES)
    decision_frame_index: int = Field(ge=0)
    decision_timestamp: float = Field(ge=0.0)
    feature_manifest: SequenceArtifactReference
    rotation_trace: SequenceArtifactReference
    schemas: SequenceSchemaVersions
    human_labels: HumanAnnotationSet = Field(default_factory=HumanAnnotationSet)
    derived_targets: list[DerivedTarget] = Field(default_factory=list, max_length=256)
    label_source: Literal["label_studio", "manual_review", "automatic", "imported"]
    label_quality: SequenceLabelQuality
    exclusion_reason: str | None = Field(default=None, max_length=512)
    split: Literal["train", "val", "test"]
    provenance: SequenceProvenance

    @model_validator(mode="after")
    def validate_timeline(self) -> CanonicalSequenceItem:
        frame_indices = [frame.frame_index for frame in self.frames]
        timestamps = [frame.timestamp for frame in self.frames]
        if frame_indices != sorted(set(frame_indices)):
            raise ValueError("sequence frame indices must be strictly increasing and unique")
        if any(
            current <= previous
            for previous, current in zip(timestamps, timestamps[1:], strict=False)
        ):
            raise ValueError("sequence frame timestamps must be strictly increasing")
        if self.decision_frame_index != frame_indices[-1]:
            raise ValueError("the sequence must end at decision_frame_index")
        if abs(self.decision_timestamp - timestamps[-1]) > 1e-6:
            raise ValueError("decision_timestamp must match the final sequence frame timestamp")

        timestamp_by_frame = {frame.frame_index: frame.timestamp for frame in self.frames}
        for annotation in self.human_labels.timeline_labels:
            if isinstance(annotation, TimelinePointAnnotation):
                self._validate_annotated_frame(
                    annotation.frame_index,
                    annotation.timestamp,
                    timestamp_by_frame,
                )
                continue
            self._validate_annotated_frame(
                annotation.start_frame_index,
                annotation.start_timestamp,
                timestamp_by_frame,
            )
            self._validate_annotated_frame(
                annotation.end_frame_index,
                annotation.end_timestamp,
                timestamp_by_frame,
            )
        return self

    def _validate_annotated_frame(
        self,
        frame_index: int,
        timestamp: float | None,
        timestamp_by_frame: dict[int, float],
    ) -> None:
        if frame_index not in timestamp_by_frame:
            raise ValueError(f"timeline annotation references unknown frame {frame_index}")
        if frame_index > self.decision_frame_index:
            raise ValueError("timeline annotation must not extend beyond decision_frame_index")
        if timestamp is not None and abs(timestamp - timestamp_by_frame[frame_index]) > 1e-6:
            raise ValueError(f"timeline timestamp does not match frame {frame_index}")


class CanonicalSequenceDataset(StrictSequenceModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    task_type: Literal["temporal_sequence"] = "temporal_sequence"
    annotation_schema: str = Field(min_length=1, max_length=128)
    items: list[CanonicalSequenceItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identity_and_group_splits(self) -> CanonicalSequenceDataset:
        sequence_ids = [item.sequence_id for item in self.items]
        duplicates = sorted(
            {item_id for item_id in sequence_ids if sequence_ids.count(item_id) > 1}
        )
        if duplicates:
            raise ValueError(f"sequence_id values must be unique: {', '.join(duplicates)}")
        split_by_group: dict[str, str] = {}
        for item in self.items:
            previous = split_by_group.setdefault(item.group_id, item.split)
            if previous != item.split:
                raise ValueError(
                    f"all sequences in group {item.group_id!r} must use the same split"
                )
            if item.schemas.annotation_schema != self.annotation_schema:
                raise ValueError(
                    f"sequence {item.sequence_id!r} uses a different annotation schema"
                )
        return self

    @property
    def source_task_count(self) -> int:
        return len(self.items)

    @property
    def annotation_count(self) -> int:
        return sum(
            len(item.human_labels.sequence_labels) + len(item.human_labels.timeline_labels)
            for item in self.items
        )

    @property
    def eligible_item_count(self) -> int:
        return sum(item.exclusion_reason is None for item in self.items)

    @property
    def submitted_labeled_item_count(self) -> int:
        return len(self.items)

    @property
    def submitted_background_item_count(self) -> int:
        return 0

    @property
    def draft_item_count(self) -> int:
        return 0

    @property
    def invalid_item_count(self) -> int:
        return 0

    @property
    def rotated_rectangle_count(self) -> int:
        return 0

    @property
    def class_distribution(self) -> dict[str, int]:
        distribution: dict[str, int] = {}
        for item in self.items:
            for annotation in item.human_labels.sequence_labels:
                if not isinstance(annotation, SequenceChoiceAnnotation):
                    continue
                key = f"{annotation.name}:{annotation.value}"
                distribution[key] = distribution.get(key, 0) + 1
        return dict(sorted(distribution.items()))

    @property
    def anomaly_distribution(self) -> dict[str, int]:
        return {}


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    item_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    status: Literal["passed", "failed"]
    checks: dict[str, bool] = Field(default_factory=dict)
    issues: list[ValidationIssue] = Field(default_factory=list)
    warning_count: int = 0
    error_count: int = 0


class CanonicalDataset(BaseModel):
    dataset_id: str
    task_type: str
    source_task_count: int
    annotation_count: int
    eligible_item_count: int = 0
    submitted_labeled_item_count: int = 0
    submitted_background_item_count: int = 0
    draft_item_count: int = 0
    invalid_item_count: int = 0
    rotated_rectangle_count: int = 0
    items: list[CanonicalItem] = Field(default_factory=list)
    class_distribution: dict[str, int] = Field(default_factory=dict)
    anomaly_distribution: dict[str, int] = Field(default_factory=dict)
    label_map: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SplitSummary(BaseModel):
    strategy: str
    train_ratio: int
    val_ratio: int
    test_ratio: int
    train_count: int
    val_count: int
    test_count: int


class SnapshotArtifacts(BaseModel):
    canonical_dataset: CanonicalDataset | CanonicalSequenceDataset
    split_summary: SplitSummary
    validation: ValidationResult
    content_sha256: str
    manifest_paths: dict[str, str] = Field(default_factory=dict)
