from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cv_datasets.models import (
    BooleanAnnotation,
    CanonicalSequenceItem,
    HumanAnnotationSet,
    MultiChoiceAnnotation,
    SequenceChoiceAnnotation,
    SequenceFrame,
    TextAnnotation,
    TimelineIntervalAnnotation,
    TimelinePointAnnotation,
)
from cv_datasets.target_builders.base import (
    RotationTraceSample,
    TargetBuildContext,
    TargetBuilder,
    TargetBuildResult,
)
from cv_datasets.target_builders.stem_orientation import (
    BUILDER_ID as STEM_ORIENTATION_BUILDER_ID,
)
from cv_datasets.target_builders.stem_orientation import (
    BUILDER_VERSION as STEM_ORIENTATION_BUILDER_VERSION,
)
from cv_datasets.target_builders.stem_orientation import build_stem_orientation_targets

AnnotationFieldKind = Literal[
    "sequence_choice",
    "boolean",
    "timeline_point",
    "timeline_interval",
    "multiple_choice",
    "text",
]


class StrictSchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequiredWhen(StrictSchemaModel):
    field: str = Field(min_length=1, max_length=128)
    equals: str | bool


class AnnotationFieldSpec(StrictSchemaModel):
    name: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=256)
    kind: AnnotationFieldKind
    description: str | None = Field(default=None, max_length=1_000)
    choices: list[str] = Field(default_factory=list, max_length=128)
    required: bool = False
    required_when: list[RequiredWhen] = Field(default_factory=list, max_length=16)
    max_occurrences: int = Field(default=1, ge=1, le=256)

    @model_validator(mode="after")
    def validate_choices(self) -> AnnotationFieldSpec:
        choice_kinds = {"sequence_choice", "multiple_choice"}
        if self.kind in choice_kinds and not self.choices:
            raise ValueError(f"{self.kind} field {self.name!r} requires choices")
        if self.kind not in choice_kinds and self.choices:
            raise ValueError(f"field {self.name!r} does not accept choices")
        if len(self.choices) != len(set(self.choices)):
            raise ValueError(f"field {self.name!r} contains duplicate choices")
        return self


class AnnotationSchemaDefinition(StrictSchemaModel):
    schema_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+\.v[1-9][0-9]*$")
    task_type: Literal["temporal_sequence"] = "temporal_sequence"
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=2_000)
    fields: list[AnnotationFieldSpec] = Field(min_length=1, max_length=256)
    target_builder_id: str = Field(min_length=1, max_length=128)
    target_builder_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_field_references(self) -> AnnotationSchemaDefinition:
        field_names = [field.name for field in self.fields]
        if len(field_names) != len(set(field_names)):
            raise ValueError("annotation schema field names must be unique")
        known = set(field_names)
        for field in self.fields:
            unknown = sorted({rule.field for rule in field.required_when} - known)
            if unknown:
                raise ValueError(
                    f"field {field.name!r} has unknown required_when dependencies: "
                    f"{', '.join(unknown)}"
                )
        return self


class AnnotationSchemaError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(issues))


@dataclass(frozen=True)
class RegisteredAnnotationSchema:
    definition: AnnotationSchemaDefinition
    target_builder: TargetBuilder

    def label_studio_config(self) -> str:
        return _render_label_studio_config(self.definition)

    def parse_label_studio_results(
        self,
        results: list[dict[str, Any]],
        *,
        frames: list[SequenceFrame],
        decision_frame_index: int,
    ) -> HumanAnnotationSet:
        annotations = _parse_label_studio_results(
            definition=self.definition,
            results=results,
            frames=frames,
        )
        self.validate_annotations(
            annotations,
            frames=frames,
            decision_frame_index=decision_frame_index,
        )
        return annotations

    def validate_annotations(
        self,
        annotations: HumanAnnotationSet,
        *,
        frames: list[SequenceFrame],
        decision_frame_index: int,
    ) -> None:
        _validate_annotations(
            definition=self.definition,
            annotations=annotations,
            frames=frames,
            decision_frame_index=decision_frame_index,
        )

    def validate_item(self, item: CanonicalSequenceItem) -> None:
        if item.schemas.annotation_schema != self.definition.schema_id:
            raise AnnotationSchemaError(
                [
                    f"item annotation schema {item.schemas.annotation_schema!r} does not match "
                    f"registry schema {self.definition.schema_id!r}"
                ]
            )
        self.validate_annotations(
            item.human_labels,
            frames=item.frames,
            decision_frame_index=item.decision_frame_index,
        )

    def build_targets(
        self,
        item: CanonicalSequenceItem,
        rotation_trace: list[RotationTraceSample],
        *,
        minimum_rotation_confidence: float,
    ) -> TargetBuildResult:
        if not 0.0 <= minimum_rotation_confidence <= 1.0:
            raise ValueError("minimum_rotation_confidence must be between 0 and 1")
        return self.build_targets_with_context(
            item,
            TargetBuildContext(
                resources={"rotation_trace": tuple(rotation_trace)},
                numeric_options={
                    "minimum_rotation_confidence": minimum_rotation_confidence,
                },
            ),
        )

    def build_targets_with_context(
        self,
        item: CanonicalSequenceItem,
        context: TargetBuildContext,
    ) -> TargetBuildResult:
        self.validate_item(item)
        result = self.target_builder(item, context)
        mismatched_targets = [
            target.name
            for target in result.targets
            if target.builder_id != self.definition.target_builder_id
            or target.builder_version != self.definition.target_builder_version
        ]
        if mismatched_targets:
            raise ValueError(
                "target builder returned targets with mismatched provenance: "
                f"{', '.join(sorted(mismatched_targets))}"
            )
        return result


class AnnotationSchemaRegistry:
    def __init__(self) -> None:
        self._schemas: dict[str, RegisteredAnnotationSchema] = {}

    def register(self, schema: RegisteredAnnotationSchema) -> None:
        schema_id = schema.definition.schema_id
        if schema_id in self._schemas:
            raise ValueError(f"annotation schema {schema_id!r} is already registered")
        self._schemas[schema_id] = schema

    def get(self, schema_id: str) -> RegisteredAnnotationSchema:
        try:
            return self._schemas[schema_id]
        except KeyError as exc:
            raise KeyError(f"unknown annotation schema {schema_id!r}") from exc

    def list(self) -> tuple[RegisteredAnnotationSchema, ...]:
        return tuple(self._schemas[key] for key in sorted(self._schemas))


def _render_label_studio_config(definition: AnnotationSchemaDefinition) -> str:
    lines = [
        "<View>",
        f'  <Header value="{_xml_attribute(definition.title)}"/>',
        '  <Video name="video" value="$video" frameRate="$frame_rate" timelineHeight="120"/>',
    ]
    for field in definition.fields:
        lines.append(f'  <Header value="{_xml_attribute(field.label)}"/>')
        if field.kind == "sequence_choice":
            lines.append(
                f'  <Choices name="{_xml_attribute(field.name)}" toName="video" choice="single">'
            )
            lines.extend(
                f'    <Choice value="{_xml_attribute(choice)}"/>' for choice in field.choices
            )
            lines.append("  </Choices>")
        elif field.kind == "boolean":
            lines.extend(
                [
                    f'  <Choices name="{_xml_attribute(field.name)}" toName="video" '
                    'choice="single">',
                    '    <Choice value="true"/>',
                    '    <Choice value="false"/>',
                    "  </Choices>",
                ]
            )
        elif field.kind == "multiple_choice":
            lines.append(
                f'  <Choices name="{_xml_attribute(field.name)}" toName="video" choice="multiple">'
            )
            lines.extend(
                f'    <Choice value="{_xml_attribute(choice)}"/>' for choice in field.choices
            )
            lines.append("  </Choices>")
        elif field.kind in {"timeline_point", "timeline_interval"}:
            lines.extend(
                [
                    f'  <TimelineLabels name="{_xml_attribute(field.name)}" toName="video">',
                    f'    <Label value="{_xml_attribute(field.name)}"/>',
                    "  </TimelineLabels>",
                ]
            )
        else:
            lines.append(
                f'  <TextArea name="{_xml_attribute(field.name)}" toName="video" rows="3" '
                'maxSubmissions="1"/>'
            )
    lines.append("</View>")
    return "\n".join(lines) + "\n"


def _xml_attribute(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def _parse_label_studio_results(
    *,
    definition: AnnotationSchemaDefinition,
    results: list[dict[str, Any]],
    frames: list[SequenceFrame],
) -> HumanAnnotationSet:
    fields = {field.name: field for field in definition.fields}
    timestamps = {frame.frame_index: frame.timestamp for frame in frames}
    sequence_labels: list[Any] = []
    timeline_labels: list[Any] = []
    issues: list[str] = []

    for result in results:
        field_name = result.get("from_name")
        if not isinstance(field_name, str) or field_name not in fields:
            issues.append(f"unknown annotation field {field_name!r}")
            continue
        field = fields[field_name]
        value = result.get("value")
        if not isinstance(value, dict):
            issues.append(f"annotation field {field_name!r} has no value object")
            continue
        try:
            if field.kind == "sequence_choice":
                choices = _choice_values(value)
                if len(choices) != 1:
                    raise ValueError("requires exactly one choice")
                sequence_labels.append(SequenceChoiceAnnotation(name=field_name, value=choices[0]))
            elif field.kind == "boolean":
                choices = _choice_values(value)
                if len(choices) != 1 or choices[0].casefold() not in {
                    "true",
                    "false",
                    "yes",
                    "no",
                }:
                    raise ValueError("requires exactly one boolean choice")
                sequence_labels.append(
                    BooleanAnnotation(
                        name=field_name,
                        value=choices[0].casefold() in {"true", "yes"},
                    )
                )
            elif field.kind == "multiple_choice":
                choices = _choice_values(value)
                if not choices:
                    raise ValueError("requires at least one choice")
                sequence_labels.append(
                    MultiChoiceAnnotation(name=field_name, values=sorted(set(choices)))
                )
            elif field.kind == "text":
                text_value = value.get("text")
                if isinstance(text_value, list):
                    text_value = "\n".join(str(entry) for entry in text_value)
                if not isinstance(text_value, str):
                    raise ValueError("requires text")
                sequence_labels.append(TextAnnotation(name=field_name, value=text_value))
            else:
                ranges = value.get("ranges")
                if not isinstance(ranges, list) or not ranges:
                    raise ValueError("requires at least one timeline range")
                timeline_values = value.get("timelinelabels")
                if timeline_values is not None and (
                    not isinstance(timeline_values, list) or field_name not in timeline_values
                ):
                    raise ValueError("contains an unexpected timeline label")
                for range_value in ranges:
                    if not isinstance(range_value, dict):
                        raise ValueError("contains a malformed timeline range")
                    start = _frame_number(range_value.get("start"))
                    end = _frame_number(range_value.get("end"))
                    if start not in timestamps or end not in timestamps:
                        raise ValueError("references a frame outside the sequence")
                    if field.kind == "timeline_point":
                        if start != end:
                            raise ValueError("timeline point must use an equal start and end frame")
                        timeline_labels.append(
                            TimelinePointAnnotation(
                                name=field_name,
                                frame_index=start,
                                timestamp=timestamps[start],
                            )
                        )
                    else:
                        timeline_labels.append(
                            TimelineIntervalAnnotation(
                                name=field_name,
                                start_frame_index=start,
                                end_frame_index=end,
                                start_timestamp=timestamps[start],
                                end_timestamp=timestamps[end],
                            )
                        )
        except ValueError as exc:
            issues.append(f"annotation field {field_name!r} {exc}")

    if issues:
        raise AnnotationSchemaError(issues)
    return HumanAnnotationSet(
        sequence_labels=sequence_labels,
        timeline_labels=timeline_labels,
    )


def _choice_values(value: dict[str, Any]) -> list[str]:
    choices = value.get("choices")
    if not isinstance(choices, list):
        return []
    return [choice for choice in choices if isinstance(choice, str) and choice]


def _frame_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("contains a non-numeric frame boundary")
    frame_number = int(value)
    if float(value) != float(frame_number) or frame_number < 0:
        raise ValueError("contains a non-integral frame boundary")
    return frame_number


def _validate_annotations(
    *,
    definition: AnnotationSchemaDefinition,
    annotations: HumanAnnotationSet,
    frames: list[SequenceFrame],
    decision_frame_index: int,
) -> None:
    issues: list[str] = []
    known_frames = {frame.frame_index for frame in frames}
    if not frames:
        issues.append("annotation validation requires at least one sequence frame")
    if decision_frame_index not in known_frames:
        issues.append("decision_frame_index is not part of the sequence")

    annotations_by_name: dict[str, list[Any]] = {}
    for annotation in [*annotations.sequence_labels, *annotations.timeline_labels]:
        annotations_by_name.setdefault(annotation.name, []).append(annotation)

    fields = {field.name: field for field in definition.fields}
    unknown_names = sorted(set(annotations_by_name) - set(fields))
    if unknown_names:
        issues.append(f"unknown annotation fields: {', '.join(unknown_names)}")

    scalar_values = {
        annotation.name: annotation.value
        for annotation in annotations.sequence_labels
        if isinstance(annotation, SequenceChoiceAnnotation | BooleanAnnotation)
    }
    for field in definition.fields:
        values = annotations_by_name.get(field.name, [])
        required_by_dependency = bool(field.required_when) and all(
            scalar_values.get(rule.field) == rule.equals for rule in field.required_when
        )
        if (field.required or required_by_dependency) and not values:
            issues.append(f"required annotation field {field.name!r} is missing")
        if len(values) > field.max_occurrences:
            issues.append(
                f"annotation field {field.name!r} exceeds max_occurrences={field.max_occurrences}"
            )
        for value in values:
            if not _annotation_matches_field(value, field.kind):
                issues.append(f"annotation field {field.name!r} does not match kind {field.kind!r}")
                continue
            if isinstance(value, SequenceChoiceAnnotation) and value.value not in field.choices:
                issues.append(
                    f"annotation field {field.name!r} has unsupported choice {value.value!r}"
                )
            if isinstance(value, MultiChoiceAnnotation):
                unsupported = sorted(set(value.values) - set(field.choices))
                if unsupported:
                    issues.append(
                        f"annotation field {field.name!r} has unsupported choices: "
                        f"{', '.join(unsupported)}"
                    )
            if isinstance(value, TimelinePointAnnotation):
                _validate_frame_reference(
                    issues,
                    field.name,
                    value.frame_index,
                    known_frames,
                    decision_frame_index,
                )
            if isinstance(value, TimelineIntervalAnnotation):
                _validate_frame_reference(
                    issues,
                    field.name,
                    value.start_frame_index,
                    known_frames,
                    decision_frame_index,
                )
                _validate_frame_reference(
                    issues,
                    field.name,
                    value.end_frame_index,
                    known_frames,
                    decision_frame_index,
                )

    if issues:
        raise AnnotationSchemaError(issues)


def _annotation_matches_field(annotation: object, field_kind: AnnotationFieldKind) -> bool:
    expected_type = {
        "sequence_choice": SequenceChoiceAnnotation,
        "boolean": BooleanAnnotation,
        "multiple_choice": MultiChoiceAnnotation,
        "text": TextAnnotation,
        "timeline_point": TimelinePointAnnotation,
        "timeline_interval": TimelineIntervalAnnotation,
    }[field_kind]
    return isinstance(annotation, expected_type)


def _validate_frame_reference(
    issues: list[str],
    field_name: str,
    frame_index: int,
    known_frames: set[int],
    decision_frame_index: int,
) -> None:
    if frame_index not in known_frames:
        issues.append(f"annotation field {field_name!r} references unknown frame {frame_index}")
    if frame_index > decision_frame_index:
        issues.append(
            f"annotation field {field_name!r} extends beyond decision_frame_index "
            f"{decision_frame_index}"
        )


STEM_ORIENTATION_ANGLE_V1 = RegisteredAnnotationSchema(
    definition=AnnotationSchemaDefinition(
        schema_id="stem_orientation_angle.v1",
        title="Stammausrichtung bis zum visuellen Entscheidungsframe",
        description=(
            "Markiert Sequenzqualität und den beobachteten Zeitbereich der besten vertikalen "
            "Bogenlage; der Labeler gibt keinen Winkelwert ein."
        ),
        target_builder_id=STEM_ORIENTATION_BUILDER_ID,
        target_builder_version=STEM_ORIENTATION_BUILDER_VERSION,
        fields=[
            AnnotationFieldSpec(
                name="sequence_valid",
                label="Sequenz verwendbar?",
                kind="sequence_choice",
                choices=["usable", "unusable"],
                required=True,
            ),
            AnnotationFieldSpec(
                name="bow_relevance",
                label="Bogenrelevanz",
                kind="sequence_choice",
                choices=["relevant", "nearly_straight", "unclear"],
                required_when=[RequiredWhen(field="sequence_valid", equals="usable")],
            ),
            AnnotationFieldSpec(
                name="target_visible",
                label="Gute Zielstellung in der Sequenz sichtbar?",
                kind="sequence_choice",
                choices=["yes", "no", "unclear"],
                required_when=[RequiredWhen(field="sequence_valid", equals="usable")],
            ),
            AnnotationFieldSpec(
                name="target_orientation",
                label="Kurzer Bereich der besten vertikalen Bogenlage",
                kind="timeline_interval",
                required_when=[
                    RequiredWhen(field="sequence_valid", equals="usable"),
                    RequiredWhen(field="bow_relevance", equals="relevant"),
                    RequiredWhen(field="target_visible", equals="yes"),
                ],
                max_occurrences=1,
            ),
            AnnotationFieldSpec(
                name="final_orientation_quality",
                label="Lage ausschließlich im finalen Entscheidungsfenster",
                kind="sequence_choice",
                choices=["good", "acceptable", "bad"],
                required_when=[RequiredWhen(field="sequence_valid", equals="usable")],
            ),
            AnnotationFieldSpec(
                name="special_case",
                label="Sonderfall (optional, mehrfach möglich)",
                kind="multiple_choice",
                choices=[
                    "occluded",
                    "motion_blur",
                    "tracking_failed",
                    "rotation_trace_invalid",
                    "insufficient_rotation",
                    "multiple_stems",
                    "incomplete_sequence",
                    "ambiguous_target",
                    "s_bow",
                ],
            ),
            AnnotationFieldSpec(
                name="comment",
                label="Kommentar (optional)",
                kind="text",
            ),
        ],
    ),
    target_builder=build_stem_orientation_targets,
)


ANNOTATION_SCHEMA_REGISTRY = AnnotationSchemaRegistry()
ANNOTATION_SCHEMA_REGISTRY.register(STEM_ORIENTATION_ANGLE_V1)


def get_annotation_schema(schema_id: str) -> RegisteredAnnotationSchema:
    return ANNOTATION_SCHEMA_REGISTRY.get(schema_id)


def list_annotation_schemas() -> tuple[RegisteredAnnotationSchema, ...]:
    return ANNOTATION_SCHEMA_REGISTRY.list()
