from __future__ import annotations

from typing import Any

from cv_contracts.runtime import (
    RuntimeNodePort,
    RuntimeNodePropertyOption,
    RuntimeNodePropertySpec,
    RuntimeNodeTypeSummary,
)
from cv_status import RuntimeMode, RuntimeNodeCategory, TargetPath, TaskType

from cv_runtime.bark_registry import bark_node_types


def port(key: str, data_type: str, *, required: bool = True) -> RuntimeNodePort:
    return RuntimeNodePort(key=key, data_type=data_type, required=required)


def prop(
    key: str,
    label: str,
    field_type: str,
    *,
    required: bool = False,
    default_value: Any | None = None,
    options: list[tuple[str, str]] | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    step_value: float | None = None,
    section: str | None = None,
    help_text: str | None = None,
) -> RuntimeNodePropertySpec:
    return RuntimeNodePropertySpec(
        key=key,
        label=label,
        field_type=field_type,  # type: ignore[arg-type]
        required=required,
        default_value=default_value,
        options=[
            RuntimeNodePropertyOption(value=value, label=label) for value, label in (options or [])
        ],
        min_value=min_value,
        max_value=max_value,
        step_value=step_value,
        section=section,
        help_text=help_text,
    )


ALL_MODES = [RuntimeMode.REALTIME, RuntimeMode.TRIGGER, RuntimeMode.INTERVAL]
FRAME_MODES = [RuntimeMode.REALTIME, RuntimeMode.INTERVAL]
TRIGGER_MODES = [RuntimeMode.TRIGGER]
BOARD_CUT_TEST_MODES = [RuntimeMode.REALTIME, RuntimeMode.TRIGGER]
INTERVAL_MODES = [RuntimeMode.INTERVAL]
SNAPSHOT_MODES = [RuntimeMode.TRIGGER, RuntimeMode.INTERVAL]
SOURCE_OUTPUTS = [port("frame", "frame")]
FRAME_INPUTS = [port("frame", "frame")]
RESULT_INPUTS = [port("result", "result")]
CAPTURE_OUTPUTS = [port("capture", "capture_intents", required=False)]
DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS = 1_500


def _node(
    *,
    node_type: str,
    category: RuntimeNodeCategory,
    display_name: str,
    description: str,
    supported_modes: list[RuntimeMode],
    input_ports: list[RuntimeNodePort] | None = None,
    output_ports: list[RuntimeNodePort] | None = None,
    default_config: dict[str, Any] | None = None,
    property_schema: list[RuntimeNodePropertySpec] | None = None,
    handler: str | None = None,
    constraints: dict[str, Any] | None = None,
) -> RuntimeNodeTypeSummary:
    input_ports = input_ports or []
    output_ports = output_ports or []
    property_schema = _property_schema_with_authoring_defaults(property_schema or [])
    compatibility_constraints = {
        **_default_authoring_constraints(
            node_type=node_type,
            category=category,
            input_ports=input_ports,
            output_ports=output_ports,
        ),
        **(constraints or {}),
    }
    return RuntimeNodeTypeSummary(
        node_type=node_type,
        node_category=category,
        contract_version=1,
        display_name=display_name,
        description=description,
        supported_modes=supported_modes,
        input_ports=input_ports,
        output_ports=output_ports,
        config_schema_ref=f"runtime.node.{node_type}.config.v1",
        property_schema=property_schema,
        default_config=default_config or {},
        runtime_handler_ref=handler or f"cv_runtime.nodes.builtin:{node_type.replace('.', '_')}",
        compatibility_constraints=compatibility_constraints,
    )


def _property_schema_with_authoring_defaults(
    fields: list[RuntimeNodePropertySpec],
) -> list[RuntimeNodePropertySpec]:
    return [
        field.model_copy(
            update={
                "section": field.section or _default_property_section(field),
                "help_text": field.help_text or _default_property_help(field),
            }
        )
        for field in fields
    ]


def _default_property_section(field: RuntimeNodePropertySpec) -> str:
    if field.field_type in {"model_ref", "label_project_list"}:
        return "Bindings"
    if field.field_type in {
        "zone_list",
        "roi",
        "direction_reference",
        "board_surface_calibration",
        "camera_intrinsic_calibration",
        "image_view_profile",
        "stem_curvature_process_calibration",
        "stem_surface_phase_calibration",
        "stem_minimum_principle_validation",
        "board_surface_class_mapping",
    }:
        return "Geometry"
    if field.field_type in {
        "response_mapping",
        "result_projection_table",
        "edge_role_mapping",
        "measurement_rule_table",
        "clearance_rule_table",
        "cv_line_selection_rules",
        "metric_apply_to",
        "quality_rule_table",
        "spatial_rule_table",
        "color_profile_table",
        "board_cut_class_rules",
        "board_surface_class_mapping",
        "plc_write_mapping_table",
    }:
        return "Rules"
    if field.field_type == "plc_connection":
        return "Connection"
    return "Configuration"


def _default_property_help(field: RuntimeNodePropertySpec) -> str:
    return (
        f"Controls the runtime value for {field.label}; validation stores it as "
        f"the node config key `{field.key}`."
    )


def _default_authoring_constraints(
    *,
    node_type: str,
    category: RuntimeNodeCategory,
    input_ports: list[RuntimeNodePort],
    output_ports: list[RuntimeNodePort],
) -> dict[str, Any]:
    required_input_types = [
        port.data_type for port in input_ports if port.required and port.data_type != "trigger"
    ]
    blocks: list[str] = []
    singleton = False
    auxiliary_source = node_type == "source.stem_csv_measurements"
    if category == RuntimeNodeCategory.SOURCE and not auxiliary_source:
        singleton = True
        blocks = ["source.*"]
    if node_type == "trigger.http_put":
        singleton = True
        blocks = ["trigger.interval"]
    elif node_type == "trigger.interval":
        singleton = True
        blocks = ["trigger.http_put"]
    return {
        **({"auxiliary_source": True} if auxiliary_source else {}),
        "authoring": {
            "starter": category in {RuntimeNodeCategory.TRIGGER, RuntimeNodeCategory.SOURCE}
            or not required_input_types,
            "singleton": singleton,
            "blocks_with_node_types": blocks,
            "requires_upstream_data_types": required_input_types,
            "terminal": category in {RuntimeNodeCategory.OUTPUT, RuntimeNodeCategory.CAPTURE}
            or not output_ports,
            "palette_reason": "Available when required upstream data is present.",
        }
    }


BUILTIN_NODE_TYPES: tuple[RuntimeNodeTypeSummary, ...] = (
    _node(
        node_type="trigger.http_put",
        category=RuntimeNodeCategory.TRIGGER,
        display_name="HTTP trigger",
        description="Execution entrypoint for an external HTTP PUT request.",
        supported_modes=TRIGGER_MODES,
        output_ports=[port("trigger", "trigger")],
        default_config={
            "method": "PUT",
            "timeout_ms": 2500,
            "auth_required": False,
            "require_idempotency_key": False,
            "endpoint_enabled": True,
            "response_mode": "configured_fields",
        },
        property_schema=[
            prop(
                "method",
                "Method",
                "select",
                default_value="PUT",
                options=[("PUT", "PUT")],
            ),
            prop(
                "timeout_ms",
                "Default timeout",
                "integer",
                default_value=2500,
                min_value=50,
                max_value=120000,
            ),
            prop("auth_required", "Require API key", "boolean", default_value=False),
            prop(
                "require_idempotency_key",
                "Idempotenzschlüssel am Edge verlangen",
                "boolean",
                default_value=False,
                section="Edge HTTP-Trigger",
                help_text=(
                    "Aus: Jeder PUT ohne X-Idempotency-Key startet eine neue Verarbeitung; "
                    "auch ein leerer Body ist erlaubt. Ein freiwillig mitgesendeter Schlüssel "
                    "verhindert fünf Minuten lang doppelte Ausführungen, auch nach einem Neustart. "
                    "Für neue Aufträge immer neue Schlüssel verwenden. Ein: Ohne Schlüssel wird "
                    "die Anfrage vor der Verarbeitung abgelehnt."
                ),
            ),
            prop("endpoint_enabled", "Endpoint enabled", "boolean", default_value=True),
            prop(
                "response_mode",
                "Response mapping",
                "select",
                default_value="configured_fields",
                options=[
                    ("configured_fields", "Configured fields"),
                    ("all_final_response", "All final response fields"),
                ],
            ),
        ],
    ),
    _node(
        node_type="trigger.interval",
        category=RuntimeNodeCategory.TRIGGER,
        display_name="Interval trigger",
        description="Start pipeline execution periodically for the channel using this pipeline.",
        supported_modes=INTERVAL_MODES,
        output_ports=[port("trigger", "trigger")],
        default_config={
            "interval_ms": 1000,
            "jitter_ms": 0,
            "enabled": True,
        },
        property_schema=[
            prop(
                "interval_ms",
                "Interval",
                "integer",
                required=True,
                default_value=1000,
                min_value=50,
                max_value=86_400_000,
            ),
            prop(
                "jitter_ms",
                "Jitter",
                "integer",
                default_value=0,
                min_value=0,
                max_value=60_000,
            ),
            prop("enabled", "Enabled", "boolean", default_value=True),
        ],
    ),
    _node(
        node_type="source.channel_snapshot",
        category=RuntimeNodeCategory.SOURCE,
        display_name="Legacy channel snapshot",
        description=(
            "Legacy trigger source binding. New pipelines should use Channel frame input; "
            "the selected channel supplies the frame when the pipeline is applied to it."
        ),
        supported_modes=TRIGGER_MODES,
        input_ports=[port("trigger", "trigger")],
        output_ports=[port("frame", "frame"), port("trigger", "trigger", required=False)],
        default_config={
            "channel_id": "",
            "acquisition": "snapshot_or_live_frame",
        },
        property_schema=[
            prop("channel_id", "Channel", "channel_ref", required=True),
            prop(
                "acquisition",
                "Acquisition",
                "select",
                required=True,
                default_value="snapshot_or_live_frame",
                options=[
                    ("snapshot_or_live_frame", "Snapshot URL or latest live frame"),
                    ("snapshot_url", "Configured snapshot URL"),
                    ("latest_live_frame", "Latest live frame"),
                ],
            ),
        ],
        constraints={
            "deprecated": True,
            "hidden_from_palette": True,
            "replacement_node_type": "source.channel_frame",
        },
    ),
    _node(
        node_type="source.channel_frame",
        category=RuntimeNodeCategory.SOURCE,
        display_name="Channel frame input",
        description="Frame supplied by the runtime channel that selected this pipeline.",
        supported_modes=ALL_MODES,
        input_ports=[port("trigger", "trigger", required=False)],
        output_ports=[port("frame", "frame"), port("trigger", "trigger", required=False)],
        default_config={"authoring_channel_id": ""},
        property_schema=[
            prop(
                "authoring_channel_id",
                "Calibration camera",
                "channel_ref",
                help_text=(
                    "Select the camera channel used for a single graphical calibration frame. "
                    "The channel may remain stopped; the camera is opened once and immediately "
                    "released. Realtime execution still uses the channel to which the pipeline "
                    "is applied."
                ),
            )
        ],
    ),
    _node(
        node_type="source.test_photo",
        category=RuntimeNodeCategory.SOURCE,
        display_name="Test photo",
        description=(
            "Use uploaded photos as a repeatable input for realtime and triggered "
            "pipeline tests. Photos are stored with the pipeline version."
        ),
        supported_modes=ALL_MODES,
        input_ports=[port("trigger", "trigger", required=False)],
        output_ports=[port("frame", "frame"), port("trigger", "trigger", required=False)],
        default_config={"photo": None, "photos": [], "fps": 5, "photo_duration_seconds": 1},
        property_schema=[
            prop(
                "photo",
                "Photo",
                "image_upload",
                help_text=(
                    "Choose one JPEG, PNG, WebP, or BMP image up to 5 MB, or use Photo sequence "
                    "below. Test photos are saved in this pipeline version for repeatable tests."
                ),
            ),
            prop(
                "photos",
                "Photo sequence",
                "image_upload_list",
                help_text=(
                    "Optionally upload up to 100 JPEG, PNG, WebP, or BMP images. The sequence "
                    "may total 100 MB (for example, 100 photos at 1 MB each), and repeats in a loop."
                ),
            ),
            prop(
                "photo_duration_seconds",
                "Duration per photo (seconds)",
                "number",
                default_value=1,
                min_value=0.1,
                max_value=300,
                help_text=(
                    "How long one photo remains active before the next photo in the sequence is emitted. "
                    "FPS still controls how often frames are emitted."
                ),
            ),
            prop(
                "fps",
                "Realtime test FPS",
                "number",
                default_value=5,
                min_value=0.1,
                max_value=30,
                help_text="How often the current test photo is emitted during realtime tests.",
            ),
        ],
        constraints={"test_only": True, "embedded_test_asset": True},
    ),
    _node(
        node_type="source.synchronized_camera_frames",
        category=RuntimeNodeCategory.SOURCE,
        display_name="Synchronized board cameras",
        description=(
            "Capture the required upper, lower, and end-face camera roles under one "
            "workpiece trigger and reject missing or unsynchronized views."
        ),
        supported_modes=TRIGGER_MODES,
        input_ports=[port("trigger", "trigger", required=False)],
        output_ports=[
            port("upper_frame", "frame"),
            port("lower_frame", "frame"),
            port("end_face_frame", "frame"),
            port("frame_set", "synchronized_frame_set"),
            port("trigger", "trigger", required=False),
        ],
        default_config={
            "upper_channel_id": "",
            "lower_channel_id": "",
            "end_face_channel_id": "",
            "max_capture_skew_ms": 50,
            "capture_timeout_ms": 1500,
        },
        property_schema=[
            prop("upper_channel_id", "Upper camera", "channel_ref", required=True),
            prop("lower_channel_id", "Lower camera", "channel_ref", required=True),
            prop("end_face_channel_id", "End-face camera", "channel_ref", required=True),
            prop(
                "max_capture_skew_ms",
                "Maximum capture skew [ms]",
                "number",
                required=True,
                default_value=50,
                min_value=0,
                max_value=1000,
            ),
            prop(
                "capture_timeout_ms",
                "Capture timeout [ms]",
                "integer",
                required=True,
                default_value=1500,
                min_value=50,
                max_value=30000,
            ),
        ],
        handler=("cv_runtime.nodes.board_cut:source_synchronized_camera_frames"),
        constraints={
            "fixed_camera_roles": ["upper", "lower", "end_face"],
            "required_camera_roles": ["upper", "lower", "end_face"],
        },
    ),
    _node(
        node_type="source.single_board_camera_test",
        category=RuntimeNodeCategory.SOURCE,
        display_name="Single board camera test",
        description=(
            "Commission one upper, lower, or end-face camera independently while "
            "keeping the production three-camera workflow unchanged."
        ),
        supported_modes=TRIGGER_MODES,
        input_ports=[port("trigger", "trigger", required=False)],
        output_ports=[
            port("frame", "frame"),
            port("frame_set", "synchronized_frame_set"),
            port("trigger", "trigger", required=False),
        ],
        default_config={"camera_channel_id": "", "camera_role": "upper"},
        property_schema=[
            prop(
                "camera_channel_id",
                "Test camera",
                "channel_ref",
                required=True,
            ),
            prop(
                "camera_role",
                "Board camera role",
                "select",
                required=True,
                default_value="upper",
                options=[
                    ("upper", "Upper"),
                    ("lower", "Lower"),
                    ("end_face", "End face"),
                ],
            ),
        ],
        handler="cv_runtime.nodes.board_cut:source_single_board_camera_test",
        constraints={"test_only": True, "captured_view_count": 1},
    ),
    _node(
        node_type="source.rtsp",
        category=RuntimeNodeCategory.SOURCE,
        display_name="Legacy RTSP source",
        description=(
            "Legacy passthrough for realtime frames. Configure RTSP on the channel "
            "and use Channel frame input for new pipelines."
        ),
        supported_modes=FRAME_MODES,
        output_ports=SOURCE_OUTPUTS,
        default_config={"drop_strategy": "latest", "reconnect_seconds": 5},
        property_schema=[
            prop("url", "Stream URL", "text", required=True),
            prop(
                "drop_strategy",
                "Drop strategy",
                "select",
                required=True,
                default_value="latest",
                options=[("latest", "Keep latest frame"), ("sample", "Sample frames")],
            ),
            prop(
                "reconnect_seconds",
                "Reconnect seconds",
                "integer",
                default_value=5,
                min_value=1,
                max_value=60,
            ),
        ],
        constraints={
            "deprecated": True,
            "hidden_from_palette": True,
            "replacement_node_type": "source.channel_frame",
        },
    ),
    _node(
        node_type="source.trigger_snapshot",
        category=RuntimeNodeCategory.SOURCE,
        display_name="Legacy trigger snapshot source",
        description=(
            "Legacy trigger passthrough. Configure trigger capture on the channel "
            "and use Channel frame input for new pipelines."
        ),
        supported_modes=TRIGGER_MODES,
        output_ports=[port("frame", "frame"), port("trigger", "trigger")],
        default_config={"timeout_ms": 1200},
        property_schema=[
            prop("camera", "Camera", "text", required=True),
            prop(
                "timeout_ms",
                "Capture timeout",
                "integer",
                default_value=1200,
                min_value=50,
                max_value=10000,
            ),
        ],
        constraints={
            "deprecated": True,
            "hidden_from_palette": True,
            "replacement_node_type": "source.channel_frame",
        },
    ),
    _node(
        node_type="source.file_test",
        category=RuntimeNodeCategory.SOURCE,
        display_name="Test/file source",
        description=(
            "Development-only static file source. Use Channel frame input for "
            "operator pipelines assigned to channels."
        ),
        supported_modes=ALL_MODES,
        output_ports=SOURCE_OUTPUTS,
        default_config={"loop": False},
        property_schema=[
            prop("path", "Path", "text", required=True),
            prop("loop", "Loop input", "boolean", default_value=False),
        ],
        constraints={
            "hidden_from_palette": True,
            "development_only": True,
            "replacement_node_type": "source.channel_frame",
        },
    ),
    _node(
        node_type="preprocessing.camera_undistort",
        category=RuntimeNodeCategory.PREPROCESSING,
        display_name="Camera undistort",
        description=(
            "Correct intrinsic radial and tangential lens distortion without changing "
            "the camera pose or rectifying perspective."
        ),
        supported_modes=ALL_MODES,
        input_ports=FRAME_INPUTS,
        output_ports=[port("frame", "frame")],
        default_config={
            "calibration": None,
            "resolution_policy": "exact",
            "interpolation": "linear",
        },
        property_schema=[
            prop(
                "calibration",
                "Intrinsic camera calibration",
                "camera_intrinsic_calibration",
                required=True,
                default_value=None,
                help_text=(
                    "Capture several Checkerboard or ChArUco observations, calculate a "
                    "valid profile, and apply it to this versioned node configuration."
                ),
            ),
            prop(
                "resolution_policy",
                "Calibration resolution policy",
                "select",
                required=True,
                default_value="exact",
                options=[("exact", "Require exact resolution")],
                help_text=(
                    "Frames must match the calibration image dimensions; implicit "
                    "intrinsic scaling is intentionally disabled."
                ),
            ),
            prop(
                "interpolation",
                "Remap interpolation",
                "select",
                default_value="linear",
                options=[
                    ("nearest", "Nearest"),
                    ("linear", "Linear"),
                    ("cubic", "Cubic"),
                ],
            ),
        ],
        handler="cv_runtime.nodes.camera_undistort:preprocessing_camera_undistort",
        constraints={
            "intrinsic_only": True,
            "perspective_rectification": False,
            "resolution_policy": "exact",
        },
    ),
    _node(
        node_type="preprocessing.process_coordinate_rectify",
        category=RuntimeNodeCategory.PREPROCESSING,
        display_name="Canonical image view + X/Y calibration",
        description=(
            "Create the exact model/Label Studio corridor after intrinsic undistortion, "
            "then attach the metric X/Y process system and documented Z reference plane."
        ),
        supported_modes=ALL_MODES,
        input_ports=FRAME_INPUTS,
        output_ports=[port("frame", "frame")],
        default_config={"view_profile_id": "", "view_profile": None, "calibration": None},
        property_schema=[
            prop(
                "view_profile",
                "Canonical image view",
                "image_view_profile",
                required=False,
                default_value=None,
                section="1. Model and Label Studio corridor",
                help_text=(
                    "Draw the four corridor corners on the intrinsically undistorted frame. "
                    "The recommended polygon-masked crop preserves the camera perspective and "
                    "becomes the exact model and Label Studio image."
                ),
            ),
            prop(
                "view_profile_id",
                "Expected view profile ID",
                "text",
                required=False,
                default_value="",
                section="1. Model and Label Studio corridor",
                help_text="Must match the embedded profile when set.",
            ),
            prop(
                "calibration",
                "Stem curvature process calibration",
                "stem_curvature_process_calibration",
                required=False,
                default_value=None,
                section="2. Metric X/Y and reference plane",
                help_text=(
                    "Use the guided O/X/Y/XY coordinate-frame assistant on the undistorted "
                    "preview. X follows the longitudinal conveyor, Y is horizontal transverse, "
                    "and Z is the documented vertical plane normal. The canonical image view is "
                    "automatically reused as the measurement corridor: entering starts tracking "
                    "and measurement, while leaving freezes curvature and makes the decision due. "
                    "Separate ROIs and gates remain an advanced legacy option."
                ),
            ),
        ],
        handler=(
            "cv_runtime.nodes.stem_process_coordinates:"
            "preprocessing_process_coordinate_rectify"
        ),
        constraints={
            "requires_intrinsic_undistort": True,
            "perspective_rectification": True,
            "perspective_preserving_masked_crop": True,
            "default_image_view_transform": "POLYGON_MASKED_CROP",
            "canonical_image_view": True,
            "legacy_process_calibration_without_view_profile_supported": True,
            "resolution_policy": "exact",
            "stateful": False,
            "no_3d_reconstruction": True,
        },
    ),
    _node(
        node_type="preprocessing.resize",
        category=RuntimeNodeCategory.PREPROCESSING,
        display_name="Resize",
        description="Resize frames before inference or analysis.",
        supported_modes=ALL_MODES,
        input_ports=FRAME_INPUTS,
        output_ports=[port("frame", "frame")],
        default_config={"width": 1280, "height": 720, "keep_aspect": True},
        property_schema=[
            prop("width", "Width", "integer", required=True, default_value=1280, min_value=16),
            prop("height", "Height", "integer", required=True, default_value=720, min_value=16),
            prop("keep_aspect", "Keep aspect", "boolean", default_value=True),
        ],
    ),
    _node(
        node_type="preprocessing.crop",
        category=RuntimeNodeCategory.PREPROCESSING,
        display_name="Crop",
        description="Crop a fixed frame region and create a derived image context.",
        supported_modes=ALL_MODES,
        input_ports=FRAME_INPUTS,
        output_ports=[port("frame", "frame")],
        default_config={"x": 0, "y": 0, "width": 640, "height": 480},
        property_schema=[
            prop("x", "X", "integer", required=True, default_value=0, min_value=0),
            prop("y", "Y", "integer", required=True, default_value=0, min_value=0),
            prop("width", "Width", "integer", required=True, default_value=640, min_value=1),
            prop("height", "Height", "integer", required=True, default_value=480, min_value=1),
        ],
    ),
    _node(
        node_type="preprocessing.roi_extract",
        category=RuntimeNodeCategory.PREPROCESSING,
        display_name="ROI extract",
        description=(
            "Extract configured regions or detection ROIs as derived image contexts "
            "with source-object provenance."
        ),
        supported_modes=ALL_MODES,
        input_ports=[
            port("frame", "frame"),
            port("regions", "region_set", required=False),
        ],
        output_ports=[port("rois", "roi_set")],
        default_config={"padding_percent": 5},
        property_schema=[
            prop("regions", "Configured ROIs", "roi", default_value=[]),
            prop(
                "padding_percent",
                "Padding percent",
                "number",
                default_value=5,
                min_value=0,
                max_value=100,
            ),
        ],
        constraints={"port_aliases": {"region_set": ["detections", "segments", "zones"]}},
    ),
    _node(
        node_type="preprocessing.frame_sampler",
        category=RuntimeNodeCategory.PREPROCESSING,
        display_name="Frame sampler",
        description="Sample every Nth frame or limit FPS before downstream work.",
        supported_modes=FRAME_MODES,
        input_ports=FRAME_INPUTS,
        output_ports=[port("frame", "frame")],
        default_config={"sample_every_n": 5, "max_fps": 10},
        property_schema=[
            prop(
                "sample_every_n",
                "Every N frames",
                "integer",
                required=True,
                default_value=5,
                min_value=1,
            ),
            prop("max_fps", "Max FPS", "number", default_value=10, min_value=0.1, max_value=120),
        ],
    ),
    _node(
        node_type="preprocessing.zone_filter",
        category=RuntimeNodeCategory.PREPROCESSING,
        display_name="Zone filter",
        description="Keep only objects or pixels inside configured zones.",
        supported_modes=ALL_MODES,
        input_ports=[port("items", "result")],
        output_ports=[port("items", "result")],
        default_config={"zones": [], "mode": "include"},
        property_schema=[
            prop("zones", "Zones", "zone_list", required=True, default_value=[]),
            prop(
                "mode",
                "Mode",
                "select",
                default_value="include",
                options=[("include", "Include zones"), ("exclude", "Exclude zones")],
            ),
        ],
    ),
    _node(
        node_type="computer_vision.canny_edge_detection",
        category=RuntimeNodeCategory.COMPUTER_VISION,
        display_name="Canny edge detection",
        description=(
            "Detect crisp image edges with classical OpenCV Canny. Use this for "
            "visible geometry and board edges; use Feature Detection when the target "
            "is semantic, variable, or learned from labeled examples."
        ),
        supported_modes=ALL_MODES,
        input_ports=[
            port("frame", "frame"),
            port("mask", "frame", required=False),
        ],
        output_ports=[port("edges", "cv_edges")],
        default_config={
            "grayscale_conversion": "auto",
            "blur_kernel_size": 5,
            "lower_threshold": 50,
            "upper_threshold": 150,
            "aperture_size": "3",
            "l2_gradient": False,
            "output_mode": "both",
        },
        property_schema=[
            prop(
                "grayscale_conversion",
                "Grayscale conversion",
                "select",
                default_value="auto",
                options=[
                    ("auto", "Automatic"),
                    ("yes", "Yes"),
                    ("no", "No"),
                ],
                help_text="Automatic keeps grayscale frames as-is and converts color frames before Canny.",
            ),
            prop(
                "blur_kernel_size",
                "Blur kernel",
                "integer",
                default_value=5,
                min_value=0,
                max_value=31,
                help_text="Use 0 or 1 for no blur; otherwise use an odd kernel size.",
            ),
            prop("lower_threshold", "Lower threshold", "number", default_value=50, min_value=0),
            prop("upper_threshold", "Upper threshold", "number", default_value=150, min_value=0),
            prop(
                "aperture_size",
                "Aperture size",
                "select",
                default_value="3",
                options=[("3", "3"), ("5", "5"), ("7", "7")],
            ),
            prop("l2_gradient", "L2 gradient", "boolean", default_value=False),
            prop(
                "output_mode",
                "Output",
                "select",
                default_value="both",
                options=[
                    ("edge_mask", "Edge mask"),
                    ("edge_contours", "Contours"),
                    ("both", "Mask and contours"),
                ],
            ),
        ],
        constraints={
            "image_context": {
                "consumes": ["frame"],
                "produces": ["cv_edges"],
                "preserves_source_context": True,
            },
            "overlay_outputs": ["cv_edge_mask", "cv_edge_contours"],
        },
    ),
    _node(
        node_type="computer_vision.color_object_segmentation",
        category=RuntimeNodeCategory.COMPUTER_VISION,
        display_name="Color object segmentation",
        description=(
            "Detect visually distinct colored objects with classical OpenCV "
            "thresholding, contours, oriented boxes, and optional operator-drawn "
            "inspection fields. This node does not call AI inference."
        ),
        supported_modes=ALL_MODES,
        input_ports=[port("frame", "frame")],
        output_ports=[
            port("objects", "region_set"),
            port("counts", "measurements", required=False),
            port("overlay", "overlay", required=False),
            port("mask", "frame", required=False),
        ],
        default_config={
            "profiles": [
                {
                    "label": "light_object",
                    "enabled": True,
                    "color_space": "hsv",
                    "hue_min": 0,
                    "hue_max": 179,
                    "saturation_min": 0,
                    "saturation_max": 180,
                    "value_min": 128,
                    "value_max": 255,
                    "color_tolerance_percent": 55,
                    "min_area_percent": 0.05,
                    "max_area_percent": None,
                    "min_aspect_ratio": 1.0,
                    "display_color": "#f3d7a5",
                }
            ],
            "zones": [],
            "zone_match": "box_overlaps",
            "blur_kernel_size": 3,
            "morphology_kernel_size": 5,
            "morphology_iterations": 1,
            "polygon_epsilon_percent": 1.5,
            "contour_display_mode": "oriented_box",
            "max_objects": 80,
            "exclusive_profile_matching": True,
            "split_touching_objects": True,
            "split_distance_threshold": 0.38,
            "overlay_output_mode": "objects",
        },
        property_schema=[
            prop(
                "profiles",
                "Color profiles",
                "color_profile_table",
                required=True,
                default_value=[
                    {
                        "label": "light_object",
                        "enabled": True,
                        "color_space": "hsv",
                        "hue_min": 0,
                        "hue_max": 179,
                        "saturation_min": 0,
                        "saturation_max": 180,
                        "value_min": 128,
                        "value_max": 255,
                        "color_tolerance_percent": 55,
                        "min_area_percent": 0.05,
                        "min_aspect_ratio": 1.0,
                        "display_color": "#f3d7a5",
                    }
                ],
                help_text=(
                    "Each profile becomes an object label. The UI derives HSV "
                    "bounds from the selected target color and tolerance."
                ),
            ),
            prop("zones", "Inspection fields", "zone_list", default_value=[]),
            prop(
                "zone_match",
                "Zone match",
                "select",
                default_value="box_overlaps",
                options=[
                    ("center_inside", "Center inside"),
                    ("box_overlaps", "Box overlaps"),
                    ("fully_inside", "Fully inside"),
                ],
            ),
            prop(
                "blur_kernel_size",
                "Blur kernel",
                "integer",
                default_value=3,
                min_value=0,
                max_value=31,
            ),
            prop(
                "morphology_kernel_size",
                "Morphology kernel",
                "integer",
                default_value=5,
                min_value=0,
                max_value=51,
            ),
            prop(
                "morphology_iterations",
                "Morphology iterations",
                "integer",
                default_value=1,
                min_value=0,
                max_value=10,
            ),
            prop(
                "polygon_epsilon_percent",
                "Polygon epsilon %",
                "number",
                default_value=1.5,
                min_value=0,
                max_value=20,
            ),
            prop(
                "contour_display_mode",
                "Contour overlay",
                "select",
                default_value="oriented_box",
                options=[
                    ("oriented_box", "Rotated object outline"),
                    ("convex_hull", "Convex hull"),
                    ("raw_contour", "Raw contour"),
                ],
                help_text=(
                    "Choose the polygon used by Color object contours. Rotated object "
                    "outline is the most stable view for long parts such as boards."
                ),
            ),
            prop("max_objects", "Max objects", "integer", default_value=80, min_value=1),
            prop(
                "exclusive_profile_matching",
                "Exclusive profiles",
                "boolean",
                default_value=True,
                help_text=(
                    "Assign matching pixels to the first matching color profile to avoid "
                    "double-counting objects when HSV ranges overlap."
                ),
            ),
            prop(
                "split_touching_objects",
                "Split touching objects",
                "boolean",
                default_value=True,
                help_text=(
                    "Use distance-transform watershed inside each color blob so touching "
                    "same-color parts can still be counted as separate objects."
                ),
            ),
            prop(
                "split_distance_threshold",
                "Split sensitivity",
                "number",
                default_value=0.38,
                min_value=0.05,
                max_value=0.95,
                help_text=(
                    "Lower values create more watershed seeds; higher values split only "
                    "clearly separated peaks."
                ),
            ),
            prop(
                "overlay_output_mode",
                "Overlay output",
                "select",
                default_value="objects",
                options=[
                    ("objects", "Contours, boxes, axes"),
                    ("mask", "Mask only"),
                    ("both", "Mask and object overlays"),
                ],
                help_text=(
                    "Choose whether the overlay output exposes the accepted-object mask "
                    "for validation or the derived contour/object primitives for runtime display."
                ),
            ),
        ],
        constraints={
            "image_context": {
                "consumes": ["frame"],
                "produces": ["region_set", "measurements", "overlay", "frame"],
                "preserves_source_context": True,
            },
            "overlay_outputs": [
                "color_object_mask",
                "color_object_contour",
                "color_object_box",
                "color_object_axis",
            ],
        },
    ),
    _node(
        node_type="computer_vision.reference_edge_from_cv_edges",
        category=RuntimeNodeCategory.COMPUTER_VISION,
        display_name="Reference edge from CV edges",
        description=(
            "Extract reference lines from Canny edge output and emit the same "
            "ReferenceEdge objects used by Coordinate Frame From Two Edges. Use this "
            "when board edges are visually clear; use Reference Edge From Features "
            "when edges come from AI Feature Detection classes."
        ),
        supported_modes=ALL_MODES,
        input_ports=[port("edges", "cv_edges")],
        output_ports=[port("reference_edges", "reference_edges")],
        default_config={
            "line_extraction_method": "hough_line_segments",
            "hough_threshold": 30,
            "max_line_gap_px": 8,
            "minimum_line_length_px": 25,
            "fallback_behavior": "fail",
            "line_selection_rules": [
                {
                    "edge_role": "start_edge",
                    "target_angle_degrees": 90,
                    "angle_tolerance_degrees": 20,
                    "minimum_line_length_px": 25,
                    "region_preference": "left",
                    "nearest_to_image_side": "left",
                    "ranking": "nearest_to_side",
                    "required": True,
                },
                {
                    "edge_role": "side_edge",
                    "target_angle_degrees": 0,
                    "angle_tolerance_degrees": 20,
                    "minimum_line_length_px": 25,
                    "region_preference": "top",
                    "nearest_to_image_side": "top",
                    "ranking": "nearest_to_side",
                    "required": True,
                },
            ],
        },
        property_schema=[
            prop(
                "line_extraction_method",
                "Line extraction",
                "select",
                default_value="hough_line_segments",
                options=[("hough_line_segments", "Hough line segments")],
            ),
            prop("hough_threshold", "Hough threshold", "integer", default_value=30, min_value=1),
            prop("max_line_gap_px", "Max line gap px", "number", default_value=8, min_value=0),
            prop(
                "minimum_line_length_px",
                "Minimum line length px",
                "number",
                default_value=25,
                min_value=1,
            ),
            prop(
                "fallback_behavior",
                "Fallback",
                "select",
                default_value="fail",
                options=[("fail", "Fail clearly"), ("skip_optional", "Skip optional roles")],
            ),
            prop(
                "line_selection_rules",
                "Reference edge roles",
                "cv_line_selection_rules",
                required=True,
                default_value=[
                    {
                        "edge_role": "start_edge",
                        "target_angle_degrees": 90,
                        "angle_tolerance_degrees": 20,
                        "minimum_line_length_px": 25,
                        "region_preference": "left",
                        "nearest_to_image_side": "left",
                        "ranking": "nearest_to_side",
                        "required": True,
                    },
                    {
                        "edge_role": "side_edge",
                        "target_angle_degrees": 0,
                        "angle_tolerance_degrees": 20,
                        "minimum_line_length_px": 25,
                        "region_preference": "top",
                        "nearest_to_image_side": "top",
                        "ranking": "nearest_to_side",
                        "required": True,
                    },
                ],
                help_text="Choose which detected line becomes each reference edge role.",
            ),
        ],
        constraints={
            "reference_edge_source": "classic_cv",
            "line_extraction_backend": "opencv_hough_lines_p",
        },
    ),
    _node(
        node_type="inference.detection",
        category=RuntimeNodeCategory.INFERENCE,
        display_name="Detection",
        description=(
            "Run an object detection model and emit object references tied to the "
            "input image context."
        ),
        supported_modes=ALL_MODES,
        input_ports=FRAME_INPUTS,
        output_ports=[port("detections", "detections")],
        default_config={
            "score_threshold": 0.35,
            "max_detections": 100,
            "timeout_ms": DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS,
        },
        property_schema=[
            prop("model", "Model", "model_ref", required=True),
            prop(
                "timeout_ms",
                "Timeout",
                "integer",
                default_value=DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS,
                min_value=50,
                max_value=30_000,
            ),
            prop(
                "score_threshold",
                "Score threshold",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
            ),
            prop(
                "max_detections",
                "Max detections",
                "integer",
                default_value=100,
                min_value=1,
                max_value=1000,
            ),
        ],
        constraints={
            "required_task_type": TaskType.DETECTION.value,
            "allowed_target_paths": [TargetPath.REALTIME.value],
        },
    ),
    _node(
        node_type="inference.classification",
        category=RuntimeNodeCategory.INFERENCE,
        display_name="Classification",
        description=(
            "Run an image classification model. If the image came from an ROI, "
            "the labels are emitted as enrichment for the source object."
        ),
        supported_modes=ALL_MODES,
        input_ports=FRAME_INPUTS,
        output_ports=[port("classifications", "classifications")],
        default_config={
            "top_k": 1,
            "timeout_ms": DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS,
        },
        property_schema=[
            prop("model", "Model", "model_ref", required=True),
            prop(
                "timeout_ms",
                "Timeout",
                "integer",
                default_value=DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS,
                min_value=50,
                max_value=30_000,
            ),
            prop("top_k", "Top K", "integer", default_value=1, min_value=1, max_value=25),
        ],
        constraints={
            "required_task_type": TaskType.CLASSIFICATION.value,
            "allowed_target_paths": [TargetPath.REALTIME.value],
        },
    ),
    _node(
        node_type="inference.segmentation",
        category=RuntimeNodeCategory.INFERENCE,
        display_name="Segmentation",
        description=(
            "Run a segmentation model and emit segment object references tied to "
            "the input image context."
        ),
        supported_modes=ALL_MODES,
        input_ports=FRAME_INPUTS,
        output_ports=[port("segments", "segments")],
        default_config={
            "score_threshold": 0.35,
            "max_segments": 50,
            "return_masks": False,
            "timeout_ms": DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS,
            "require_image_view_compatibility": False,
        },
        property_schema=[
            prop("model", "Model", "model_ref", required=True),
            prop(
                "timeout_ms",
                "Timeout",
                "integer",
                default_value=DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS,
                min_value=50,
                max_value=30_000,
            ),
            prop(
                "score_threshold",
                "Score threshold",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
            ),
            prop("max_segments", "Max segments", "integer", default_value=50, min_value=1),
            prop(
                "return_masks", "Vollständige Klassenmasken", "boolean", default_value=False,
                help_text="Verlustfreie Masken mit Innenlöchern für metrische Messungen anfordern.",
            ),
            prop(
                "require_image_view_compatibility",
                "Report training-view differences",
                "boolean",
                default_value=False,
                help_text=(
                    "Compare the current camera/view metadata with the view recorded during "
                    "training. Differences are diagnostic only and never block segmentation "
                    "or drop frames."
                ),
            ),
        ],
        constraints={
            "required_task_type": TaskType.SEGMENTATION.value,
            "allowed_target_paths": [TargetPath.REALTIME.value],
        },
    ),
    _node(
        node_type="inference.anomaly",
        category=RuntimeNodeCategory.INFERENCE,
        display_name="Anomaly inference",
        description=(
            "Run an anomaly model and emit score, thresholded decision, and "
            "context-bound heatmap output."
        ),
        supported_modes=ALL_MODES,
        input_ports=FRAME_INPUTS,
        output_ports=[port("anomaly", "anomaly")],
        default_config={
            "threshold": None,
            "return_mask": False,
            "heatmap_opacity": 0.55,
            "timeout_ms": DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS,
        },
        property_schema=[
            prop("model", "Model", "model_ref", required=True),
            prop(
                "timeout_ms",
                "Timeout",
                "integer",
                default_value=DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS,
                min_value=50,
                max_value=30_000,
            ),
            prop(
                "threshold",
                "Threshold",
                "number",
                default_value=None,
                min_value=0,
                max_value=1,
            ),
            prop("return_mask", "Mask", "boolean", default_value=False),
            prop(
                "heatmap_opacity",
                "Heatmap opacity",
                "number",
                default_value=0.55,
                min_value=0,
                max_value=1,
            ),
        ],
        constraints={
            "required_task_type": TaskType.ANOMALY.value,
            "allowed_target_paths": [TargetPath.REALTIME.value],
            "overlay_outputs": ["heatmap"],
        },
    ),
    _node(
        node_type="inference.temporal_sequence",
        category=RuntimeNodeCategory.INFERENCE,
        display_name="Temporal sequence inference",
        description=(
            "Run a versioned causal feature sequence through the normal deployment "
            "and inference-gateway path."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[port("sequence", "temporal_sequence")],
        output_ports=[port("prediction", "temporal_prediction")],
        default_config={"timeout_ms": DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS},
        property_schema=[
            prop("model", "Model", "model_ref", required=True),
            prop(
                "timeout_ms",
                "Timeout",
                "integer",
                default_value=DEFAULT_REALTIME_INFERENCE_TIMEOUT_MS,
                min_value=50,
                max_value=30_000,
            ),
        ],
        handler="cv_runtime.nodes.temporal_inference:inference_temporal_sequence",
        constraints={
            "required_task_type": TaskType.TEMPORAL_SEQUENCE.value,
            "allowed_target_paths": [TargetPath.REALTIME.value],
            "causal": True,
            "post_decision_frames_allowed": False,
        },
    ),
    _node(
        node_type="analysis.position_determination",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Position determination",
        description="Compute object positions from detections or segments.",
        supported_modes=ALL_MODES,
        input_ports=[port("objects", "region_set")],
        output_ports=[port("positions", "positions")],
        default_config={"coordinate_system": "normalized_xyxy"},
        property_schema=[
            prop(
                "coordinate_system",
                "Coordinate system",
                "select",
                default_value="normalized_xyxy",
                options=[
                    ("normalized_xyxy", "Normalized XYXY"),
                    ("pixel_xyxy", "Pixel XYXY"),
                ],
            )
        ],
        constraints={"port_aliases": {"region_set": ["detections", "segments"]}},
    ),
    _node(
        node_type="analysis.zone_entry_exit",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Zone entry/exit",
        description="Track whether positions entered or left configured zones.",
        supported_modes=FRAME_MODES,
        input_ports=[port("positions", "positions")],
        output_ports=[port("events", "events")],
        default_config={"zones": [], "min_dwell_ms": 0},
        property_schema=[
            prop("zones", "Zones", "zone_list", required=True, default_value=[]),
            prop("min_dwell_ms", "Minimum dwell", "integer", default_value=0, min_value=0),
        ],
    ),
    _node(
        node_type="analysis.optical_flow_motion",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Optical flow / motion value",
        description="Compute motion magnitude from adjacent frames.",
        supported_modes=FRAME_MODES,
        input_ports=FRAME_INPUTS,
        output_ports=[port("motion", "measurements")],
        default_config={"window": 3},
        property_schema=[prop("window", "Window", "integer", default_value=3, min_value=2)],
    ),
    _node(
        node_type="analysis.detection_size",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Detection size",
        description="Compute area and dimensions for detections.",
        supported_modes=ALL_MODES,
        input_ports=[port("detections", "detections")],
        output_ports=[port("sizes", "measurements")],
        default_config={"unit": "percent"},
        property_schema=[
            prop(
                "unit",
                "Unit",
                "select",
                default_value="percent",
                options=[("percent", "Percent of frame"), ("pixels", "Pixels")],
            )
        ],
    ),
    _node(
        node_type="analysis.reference_edge_from_features",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Reference edge from features",
        description=(
            "Map edge-like Feature Detection classes into typed reference edges for "
            "board coordinate-frame construction."
        ),
        supported_modes=ALL_MODES,
        input_ports=[port("features", "detections")],
        output_ports=[port("reference_edges", "reference_edges")],
        default_config={
            "class_role_mappings": [
                {
                    "feature_class": "board_start_edge",
                    "edge_role": "start_edge",
                    "bbox_side": "left",
                },
                {
                    "feature_class": "board_side_edge",
                    "edge_role": "side_edge",
                    "bbox_side": "top",
                },
            ],
            "minimum_confidence": 0.35,
            "line_extraction_mode": "from_bbox_edge",
            "bbox_side": "longest_side",
            "required_roles": ["start_edge", "side_edge"],
        },
        property_schema=[
            prop(
                "class_role_mappings",
                "Edge role mappings",
                "edge_role_mapping",
                required=True,
                default_value=[
                    {
                        "feature_class": "board_start_edge",
                        "edge_role": "start_edge",
                        "bbox_side": "left",
                    },
                    {
                        "feature_class": "board_side_edge",
                        "edge_role": "side_edge",
                        "bbox_side": "top",
                    },
                ],
                help_text="Map detected model classes to board edge roles.",
            ),
            prop(
                "minimum_confidence",
                "Minimum confidence",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
            ),
            prop(
                "line_extraction_mode",
                "Line extraction",
                "select",
                default_value="from_bbox_edge",
                options=[
                    ("from_bbox_edge", "From bbox edge"),
                    ("from_polygon_line", "From polygon line"),
                    ("from_mask_contour", "From mask contour"),
                ],
            ),
            prop(
                "bbox_side",
                "Default bbox side",
                "select",
                default_value="longest_side",
                options=[
                    ("left", "Left"),
                    ("right", "Right"),
                    ("top", "Top"),
                    ("bottom", "Bottom"),
                    ("longest_side", "Longest side"),
                ],
            ),
        ],
        constraints={"port_aliases": {"detections": ["region_set"]}},
    ),
    _node(
        node_type="analysis.coordinate_frame_from_edges",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Coordinate frame from two edges",
        description="Build a board inspection coordinate frame from a start edge and a direction edge.",
        supported_modes=ALL_MODES,
        input_ports=[port("reference_edges", "reference_edges")],
        output_ports=[port("coordinate_frame", "coordinate_frame")],
        default_config={
            "start_edge_role": "start_edge",
            "direction_edge_role": "side_edge",
            "origin_rule": "intersection_with_side_edge",
            "longitudinal_axis_rule": "along_side_edge",
            "axis_orientation": "auto",
            "confidence_aggregation": "min",
        },
        property_schema=[
            prop(
                "start_edge_role",
                "Start edge role",
                "text",
                required=True,
                default_value="start_edge",
            ),
            prop(
                "direction_edge_role",
                "Direction edge role",
                "text",
                required=True,
                default_value="side_edge",
            ),
            prop(
                "origin_rule",
                "Origin rule",
                "select",
                default_value="intersection_with_side_edge",
                options=[
                    ("center_of_start_edge", "Center of start edge"),
                    ("intersection_with_side_edge", "Intersection with side edge"),
                    ("configured_endpoint", "Configured endpoint"),
                ],
            ),
            prop(
                "longitudinal_axis_rule",
                "Longitudinal axis",
                "select",
                default_value="along_side_edge",
                options=[
                    ("along_side_edge", "Along side edge"),
                    ("perpendicular_to_start_edge", "Perpendicular to start edge"),
                ],
            ),
            prop(
                "axis_orientation",
                "Axis orientation",
                "select",
                default_value="auto",
                options=[("auto", "Auto"), ("invert", "Invert")],
            ),
            prop(
                "confidence_aggregation",
                "Confidence aggregation",
                "select",
                default_value="min",
                options=[("min", "Minimum"), ("mean", "Mean")],
            ),
        ],
    ),
    _node(
        node_type="analysis.feature_measurement",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Feature measurement",
        description="Measure Feature Detection objects using bbox, polygon, mask, and optional board-axis projection.",
        supported_modes=ALL_MODES,
        input_ports=[
            port("features", "detections"),
            port("coordinate_frame", "coordinate_frame", required=False),
        ],
        output_ports=[port("feature_measurements", "feature_measurements")],
        default_config={
            "measurement_rules": [
                {
                    "feature_type": "*",
                    "bases": ["bbox_width", "bbox_height", "approximate_diameter"],
                },
                {
                    "feature_type": "knot",
                    "bases": [
                        "bbox_width",
                        "bbox_height",
                        "approximate_diameter",
                        "axis_projected_width",
                    ],
                },
            ],
            "fallback_behavior": "use_bbox",
            "unit_output": "pixels_only",
        },
        property_schema=[
            prop(
                "measurement_rules",
                "Measurement rules",
                "measurement_rule_table",
                required=True,
                default_value=[
                    {
                        "feature_type": "*",
                        "bases": ["bbox_width", "bbox_height", "approximate_diameter"],
                    },
                    {
                        "feature_type": "knot",
                        "bases": [
                            "bbox_width",
                            "bbox_height",
                            "approximate_diameter",
                            "axis_projected_width",
                        ],
                    },
                ],
            ),
            prop(
                "fallback_behavior",
                "Fallback",
                "select",
                default_value="use_bbox",
                options=[("use_bbox", "Use bbox"), ("skip", "Skip feature"), ("fail", "Fail")],
            ),
            prop(
                "unit_output",
                "Units",
                "select",
                default_value="pixels_only",
                options=[
                    ("pixels_only", "Pixels only"),
                    ("pixels_and_mm", "Pixels and mm if calibrated"),
                ],
            ),
        ],
        constraints={"port_aliases": {"detections": ["region_set", "segments"]}},
    ),
    _node(
        node_type="analysis.segmentation_polygon_simplification",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Simplify segmentation polygons",
        description=(
            "Deterministically simplifies normalized instance-segmentation polygons while "
            "preserving image context, object references, route, and model provenance. Invalid "
            "polygons are reported as non-fatal frame diagnostics."
        ),
        supported_modes=ALL_MODES,
        input_ports=[port("segments", "segments")],
        output_ports=[
            port("segments", "segments"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "epsilon_ratio": 0.005,
            "maximum_points": 12,
            "minimum_area_ratio": 0.0005,
            "debug_overlay": True,
        },
        property_schema=[
            prop(
                "epsilon_ratio",
                "Simplification tolerance",
                "number",
                required=True,
                default_value=0.005,
                min_value=0.00001,
                max_value=0.25,
                help_text="Douglas-Peucker tolerance relative to the polygon perimeter.",
            ),
            prop(
                "maximum_points",
                "Maximum polygon points",
                "integer",
                required=True,
                default_value=12,
                min_value=4,
                max_value=12,
            ),
            prop(
                "minimum_area_ratio",
                "Minimum image-area ratio",
                "number",
                required=True,
                default_value=0.0005,
                min_value=0,
                max_value=1,
            ),
            prop("debug_overlay", "Show diagnostics", "boolean", default_value=True),
        ],
        handler=(
            "cv_runtime.nodes.segmentation_polygon:"
            "analysis_segmentation_polygon_simplification"
        ),
    ),
    _node(
        node_type="analysis.board_end_coordinate_frame",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Board end and longitudinal axis",
        description=(
            "Use either selected main/side segmentation polygons or the existing image-edge "
            "method to find the board end and positive longitudinal direction. The image-edge "
            "path finds the board first from its adaptive wood colour, brightness, connected area, "
            "and elongated shape; then locates the board end and positive longitudinal direction "
            "only on that board region. The robust mode combines the main face and visible side "
            "face, and can recover a short bevel-, shadow-, or contrast-related gap at their "
            "junction. A stable line-segment detector confirms the geometry. "
            "A legacy Canny/Hough mode remains available for existing configurations. "
            "The node emits both a coordinate frame and typed reference edges for following "
            "logic. Missing geometry produces zero confidence and an empty edge list so the cut "
            "workflow returns board_end_not_found."
        ),
        supported_modes=BOARD_CUT_TEST_MODES,
        input_ports=[
            port("frame", "frame"),
            port("segments", "segments", required=False),
        ],
        output_ports=[
            port("coordinate_frame", "coordinate_frame"),
            port(
                "reference_edges",
                "reference_edges",
                required=False,
            ),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "geometry_source": "image_edges",
            "view_role": "upper",
            "board_surface_class_mapping": {},
            "board_end_side": "left",
            "line_detector_mode": "robust_lsd",
            "board_color_tolerance_percent": 75.0,
            "blur_kernel_size": 5,
            "lower_threshold": 50,
            "upper_threshold": 150,
            "hough_threshold": 30,
            "minimum_line_length_px": 25,
            "max_line_gap_px": 8,
            "board_end_angle_tolerance_degrees": 30.0,
            "longitudinal_angle_tolerance_degrees": 12.0,
            "maximum_reference_join_gap_px": 15.0,
            "minimum_edge_support_percent": 15.0,
            "minimum_board_end_span_percent": 25.0,
            "minimum_longitudinal_span_percent": 30.0,
            "leading_edge_score_tolerance": 0.20,
            "processing_max_dimension_px": 640,
            "max_axis_segments": 200,
            "debug_overlay": True,
        },
        property_schema=[
            prop(
                "geometry_source",
                "Geometry source",
                "select",
                required=True,
                default_value="image_edges",
                options=[
                    ("image_edges", "Image edges (existing)"),
                    ("segmentation", "Segmentation polygons"),
                ],
            ),
            prop(
                "view_role",
                "Camera view",
                "select",
                required=True,
                default_value="upper",
                options=[("upper", "Upper / main face"), ("lower", "Lower / main face")],
            ),
            prop(
                "board_surface_class_mapping",
                "Board surface classes",
                "board_surface_class_mapping",
                default_value={},
                help_text=(
                    "Assign the selected segmentation model classes to the main-face and "
                    "side-face semantic roles."
                ),
            ),
            prop(
                "board_end_side",
                "Board end in image",
                "select",
                required=True,
                default_value="left",
                options=[("left", "Left"), ("right", "Right")],
                help_text=(
                    "Select where the physical end of the board appears in the image. "
                    "Choose Left when the board continues from the end toward the right; "
                    "choose Right when it continues toward the left. A wrong selection makes "
                    "the node search on the wrong side and reverses the measured board direction."
                ),
            ),
            prop(
                "line_detector_mode",
                "Line detection",
                "select",
                required=True,
                default_value="robust_lsd",
                options=[
                    ("robust_lsd", "Robust (recommended)"),
                    ("legacy_hough", "Legacy Canny/Hough"),
                ],
                help_text=(
                    "Use Robust for normal commissioning and production. It evaluates the same "
                    "image deterministically and treats the angle sliders as gradual quality "
                    "preferences, so a one-degree change does not make an otherwise valid board "
                    "corner disappear. Legacy Canny/Hough is only intended for existing setups "
                    "that were tuned with the older threshold controls."
                ),
            ),
            prop(
                "board_color_tolerance_percent",
                "Board-region tolerance [%]",
                "slider",
                default_value=75.0,
                min_value=20,
                max_value=90,
                step_value=1,
                help_text=(
                    "Controls how far the securely detected wood area may expand through connected "
                    "paler colour and wood-grain texture. Increase it when only the darker side "
                    "face is purple or the bright top face has holes. Decrease it when the purple "
                    "board area grows into a similarly coloured or textured table, wall, or other "
                    "background. Adjust this first; the edge-angle sliders only act inside the "
                    "resulting purple board area."
                ),
            ),
            prop(
                "blur_kernel_size",
                "Blur kernel",
                "slider",
                default_value=5,
                min_value=0,
                max_value=31,
                step_value=1,
                help_text=(
                    "Smooths wood grain, camera noise, and small reflections before the board "
                    "edges are searched. Increase it in steps of two (for example 5, 7, 9) when "
                    "the debug view contains many short, restless lines. Decrease it when the "
                    "real board edge becomes soft, moves, or disappears; 0 or 1 disables smoothing."
                ),
            ),
            prop(
                "lower_threshold",
                "Canny lower threshold",
                "slider",
                default_value=50,
                min_value=0,
                max_value=255,
                step_value=1,
                help_text=(
                    "Legacy Canny/Hough only. Controls whether faint parts of a board edge are "
                    "kept. Lower it when the "
                    "correct edge is visible but broken or incomplete in the debug view. Raise it "
                    "when wood grain or background texture extends into many false lines. This "
                    "value must remain below the upper threshold."
                ),
            ),
            prop(
                "upper_threshold",
                "Canny upper threshold",
                "slider",
                default_value=150,
                min_value=0,
                max_value=255,
                step_value=1,
                help_text=(
                    "Legacy Canny/Hough only. Controls how much image contrast is needed to "
                    "start a reliable edge. Lower "
                    "it when the board outline produces no candidates, especially with weak "
                    "lighting contrast. Raise it when grain, glare, or the background creates too "
                    "many candidates. Keep it above the lower threshold."
                ),
            ),
            prop(
                "hough_threshold",
                "Hough threshold",
                "slider",
                default_value=30,
                min_value=1,
                max_value=300,
                step_value=1,
                help_text=(
                    "Legacy Canny/Hough only. Controls how much straight-edge evidence is "
                    "required before a line is "
                    "considered. Lower it when the real board end or long board edge is not drawn "
                    "as a candidate. Raise it when many false straight lines are shown. A value "
                    "that is too high can lead to board_end_confidence_too_low."
                ),
            ),
            prop(
                "minimum_line_length_px",
                "Minimum line length [px]",
                "slider",
                default_value=25,
                min_value=1,
                max_value=1000,
                step_value=1,
                help_text=(
                    "Legacy Canny/Hough only. Sets how long a visible straight section must be "
                    "before it can help locate "
                    "the board. Lower it when the board edge is partly hidden or appears in "
                    "several short pieces. Raise it when short grain, scratches, or reflections "
                    "are being mistaken for board edges. Keep it below the visible board-edge "
                    "length."
                ),
            ),
            prop(
                "max_line_gap_px",
                "Maximum line gap [px]",
                "slider",
                default_value=8,
                min_value=0,
                max_value=200,
                step_value=1,
                help_text=(
                    "Sets the largest interruption that may be bridged to form one continuous "
                    "board edge. Increase it when a shadow, knot, glare, or obstruction splits the "
                    "correct edge into pieces. Decrease it when separate, unrelated lines are "
                    "being joined into a false board edge."
                ),
            ),
            prop(
                "board_end_angle_tolerance_degrees",
                "Board-end tilt from vertical [deg]",
                "slider",
                default_value=30.0,
                min_value=1,
                max_value=44,
                step_value=1,
                help_text=(
                    "Sets which tilt away from vertical is still rated as an ideal board end. In "
                    "Robust mode this is a gradual preference, not a hard on/off limit: changing "
                    "it by one degree must not make the same valid corner disappear. Increase it "
                    "when the real slanted end is visible but loses to a straighter false line. "
                    "Decrease it when diagonal background lines rank too strongly. This does not "
                    "limit where the board may appear in the image."
                ),
            ),
            prop(
                "longitudinal_angle_tolerance_degrees",
                "Long-edge tilt from horizontal [deg]",
                "slider",
                default_value=12.0,
                min_value=1,
                max_value=30,
                step_value=1,
                help_text=(
                    "Sets which tilt away from horizontal is still rated as an ideal long board "
                    "edge. In Robust mode this is a gradual preference, not a hard on/off limit. "
                    "Increase it when the real tilted top, seam, or bottom edge loses to a "
                    "straighter false line. Decrease it when diagonal grain, tools, or background "
                    "edges rank too strongly. Keep it substantially lower than the board-end tilt."
                ),
            ),
            prop(
                "maximum_reference_join_gap_px",
                "Maximum corner gap [px]",
                "slider",
                default_value=15.0,
                min_value=1,
                max_value=100,
                step_value=1,
                help_text=(
                    "Sets how close a board-end line and a long edge must meet before they form a "
                    "visible corner. Robust mode may project a larger interrupted gap only when "
                    "the purple board silhouette and a cyan surface boundary confirm it. Increase "
                    "this when the correct edges stop short at a bevel or shadow. Decrease it when "
                    "unrelated lines form a false corner. This is independent of the line-gap "
                    "setting above."
                ),
            ),
            prop(
                "minimum_edge_support_percent",
                "Minimum continuous edge support [%]",
                "slider",
                default_value=15.0,
                min_value=5,
                max_value=80,
                step_value=1,
                help_text=(
                    "Legacy Canny/Hough only. Checks how much of a fitted candidate is backed "
                    "by real edge pixels. Raise "
                    "it when grey lines float through the wood instead of following a visible "
                    "edge. Lower it when the correct board outline is interrupted by glare, "
                    "shadow, rough grain, or a bevel. Start at 15 percent and normally stay "
                    "between 10 and 30 percent."
                ),
            ),
            prop(
                "minimum_board_end_span_percent",
                "Minimum board-end height [%]",
                "slider",
                default_value=25.0,
                min_value=5,
                max_value=90,
                step_value=1,
                help_text=(
                    "Rejects vertical or slanted candidates that are too short compared with the "
                    "whole image height. Raise it when small cross-shaped line groups inside the "
                    "wood are mistaken for a board end. Lower it only when the complete visible "
                    "board end is genuinely small in the image."
                ),
            ),
            prop(
                "minimum_longitudinal_span_percent",
                "Minimum long-edge width [%]",
                "slider",
                default_value=30.0,
                min_value=5,
                max_value=95,
                step_value=1,
                help_text=(
                    "Rejects horizontal candidates that do not continue far enough through the "
                    "image. Raise it when small coordinate-like line groups or wood grain are "
                    "accepted. Lower it only when the visible board length occupies a small part "
                    "of the image."
                ),
            ),
            prop(
                "leading_edge_score_tolerance",
                "Outer-edge preference",
                "slider",
                default_value=0.20,
                min_value=0,
                max_value=0.75,
                step_value=0.01,
                help_text=(
                    "Controls how strongly the outermost plausible corner is preferred over a "
                    "slightly stronger inner line. Increase it when the node selects a seam or "
                    "feature just inside the board instead of the real end. Decrease it when a "
                    "weak line outside the board wins against the clearly stronger real end."
                ),
            ),
            prop(
                "processing_max_dimension_px",
                "Analysis resolution limit [px]",
                "slider",
                default_value=640,
                min_value=128,
                max_value=4096,
                step_value=64,
                help_text=(
                    "Sets how much image detail is used for the edge search. Increase it when the "
                    "board is small in the image or its thin edge disappears during analysis; "
                    "this uses more CPU and can reduce the realtime frame rate. Decrease it for "
                    "faster processing when the detected board end remains stable."
                ),
            ),
            prop(
                "max_axis_segments",
                "Maximum line candidates per axis",
                "slider",
                default_value=200,
                min_value=16,
                max_value=1000,
                step_value=16,
                help_text=(
                    "Limits how many line pieces per orientation are examined so realtime "
                    "processing stays responsive. Normally leave this value unchanged. Increase "
                    "it only in a very busy image when valid board-edge pieces are being omitted; "
                    "decrease it when analysis becomes slow because the debug view is full of "
                    "candidates. Robust mode ranks confirmation from separate main- and side-face "
                    "boundaries before selecting the end."
                ),
            ),
            prop(
                "debug_overlay",
                "Show board-end debug overlay",
                "boolean",
                default_value=True,
                help_text=(
                    "The purple outline is the board area supported by colour and shape. Cyan "
                    "lines are sustained surface changes inside that board area. Thin grey lines "
                    "are remaining geometric edge candidates; wood "
                    "grain, knots, shadows, and reflections may create them even when nothing "
                    "moves. They are not accepted automatically. The orange line is the selected "
                    "preliminary edge, while the calibrated cut node makes the final selection. "
                    "This switch only changes the display and does not change the cut result."
                ),
            ),
        ],
        handler="cv_runtime.nodes.board_cut:analysis_board_end_coordinate_frame",
        constraints={"fail_closed_coordinate_output": True},
    ),
    _node(
        node_type="analysis.board_view_cut_constraints",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Board-view cut constraints",
        description=(
            "Track both visible board surfaces from the detected board end and project "
            "detected or segmented features into a longitudinal millimetre coordinate."
        ),
        supported_modes=BOARD_CUT_TEST_MODES,
        input_ports=[
            port("features", "detections"),
            port("coordinate_frame", "coordinate_frame", required=False),
            port("frame", "frame", required=False),
        ],
        output_ports=[
            port("constraints", "board_cut_constraints"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "view_role": "upper",
            "inspection_length_mm": 300.0,
            "safe_visibility_margin_mm": 100.0,
            "calibration_rms_mm": 0.0,
            "max_calibration_rms_mm": 2.0,
            "uncertainty_multiplier": 3.0,
            "minimum_board_end_confidence": 0.5,
            "follow_board_position": True,
            "use_calibration_pose_reference": True,
            "realtime_pose_stabilization": True,
            "realtime_stability_profile": "balanced",
            "calibration_pose_angle_tolerance_degrees": 15.0,
            "calibration_surface_end_angle_tolerance_degrees": 18.0,
            "calibration_surface_end_tolerance_px": 24.0,
            "calibration_boundary_merge_tolerance_px": 15.0,
            "calibration_candidate_merge_distance_px": 8.0,
            "calibration_candidate_merge_gap_px": 50.0,
            "calibration_pose_join_tolerance_px": 30.0,
            "calibration_pose_boundary_tolerance_px": 30.0,
            "calibration_pose_minimum_boundary_matches": 2,
            "calibration_pose_cluster_distance_px": 18.0,
            "calibration_pose_cluster_angle_degrees": 3.0,
            "calibration_pose_ambiguity_margin": 0.6,
            "calibration_leading_edge_score_tolerance": 1.25,
            "calibration_preliminary_gate_min_confidence": 0.55,
            "calibration_preliminary_gate_tolerance_px": 30.0,
            "calibration_preliminary_junction_fraction_tolerance": 0.22,
            "show_dynamic_surface_overlay": True,
            "surfaces": [],
            "class_rules": [],
            "board_width_mm": 0.0,
            "board_thickness_mm": 0.0,
        },
        property_schema=[
            prop(
                "view_role",
                "Camera role",
                "select",
                required=True,
                default_value="upper",
                options=[("upper", "Upper"), ("lower", "Lower")],
            ),
            prop(
                "inspection_length_mm",
                "Inspection length [mm]",
                "number",
                required=True,
                default_value=300.0,
                min_value=1,
            ),
            prop(
                "safe_visibility_margin_mm",
                "Safety visibility beyond inspection [mm]",
                "number",
                required=True,
                default_value=100.0,
                min_value=0,
                help_text=(
                    "Additional calibrated length available for the largest configured "
                    "feature clearance."
                ),
            ),
            prop(
                "board_width_mm",
                "Test board width [mm]",
                "number",
                default_value=0.0,
                min_value=0,
                help_text="Realtime test fallback; trigger pipelines use board_format.width_mm.",
            ),
            prop(
                "board_thickness_mm",
                "Test board thickness [mm]",
                "number",
                default_value=0.0,
                min_value=0,
                help_text=(
                    "Realtime test fallback; trigger pipelines use board_format.thickness_mm."
                ),
            ),
            prop(
                "surfaces",
                "Surface calibration",
                "board_surface_calibration",
                required=True,
                default_value=[],
                help_text=(
                    "Use one commissioning board as the metric reference. Per surface, point 1 "
                    "is at the board end, point 2 follows the board, point 3 is across at the far "
                    "image boundary, and point 4 returns to the board end. Line 4-to-1 defines "
                    "that surface's board-end tilt; 1-to-2 and 4-to-3 define its board direction. "
                    "Line 2-to-3 is only a visible limit. Adjacent surfaces share their corner "
                    "but retain separate 4-to-1 angles."
                ),
            ),
            prop(
                "follow_board_position",
                "Follow the current board position",
                "boolean",
                required=True,
                default_value=True,
                help_text=(
                    "Keep this enabled for production. The evaluated surfaces then follow a "
                    "board that appears farther left, right, up, or down than the commissioning "
                    "board. Disable it only for comparison tests where the board is guaranteed "
                    "to remain exactly inside the original calibration polygons."
                ),
            ),
            prop(
                "use_calibration_pose_reference",
                "Match board pose to calibration",
                "boolean",
                required=True,
                default_value=True,
                help_text=(
                    "Keep this enabled. It prevents straight grain, reflections, and the far "
                    "edge of the inspection polygon from moving the evaluated surfaces. The "
                    "final board end and direction must match the lines drawn during metric "
                    "calibration."
                ),
            ),
            prop(
                "calibration_pose_angle_tolerance_degrees",
                "Allowed rotation from calibration [deg]",
                "number",
                required=True,
                default_value=15.0,
                min_value=1,
                max_value=45,
                help_text=(
                    "Sets how much the production board may rotate away from the commissioned "
                    "board angle. Increase it when a real board can arrive more rotated and is "
                    "rejected. Decrease it when grain or background lines are still accepted. "
                    "This value is measured from lines 4-to-1, 1-to-2, and 4-to-3, not from the "
                    "vertical or horizontal image axes."
                ),
            ),
            prop(
                "realtime_pose_stabilization",
                "Stabilize board geometry in realtime",
                "boolean",
                required=True,
                default_value=True,
                help_text=(
                    "Keep this enabled for live cameras. A single weak or incorrect frame then "
                    "cannot immediately move or remove the board end and cut line. Disable it "
                    "only when checking the raw edge result frame by frame. Triggered single "
                    "images are never delayed by this setting."
                ),
            ),
            prop(
                "realtime_stability_profile",
                "Realtime geometry response",
                "select",
                required=True,
                default_value="balanced",
                options=[
                    ("responsive", "Responsive"),
                    ("balanced", "Balanced"),
                    ("stable", "Stable"),
                ],
                help_text=(
                    "Balanced is the production default. Choose Stable when a stationary board "
                    "end still trembles or briefly disappears; changes then need more matching "
                    "frames. Choose Responsive when boards move often and the overlay follows "
                    "too slowly. This affects only realtime operation."
                ),
            ),
            prop(
                "show_dynamic_surface_overlay",
                "Show currently evaluated surfaces",
                "boolean",
                default_value=True,
                help_text=(
                    "Draws the surfaces that are being evaluated in the current frame. Use this "
                    "to verify that a moved board is still covered. This display setting does "
                    "not change the cut result."
                ),
            ),
            prop(
                "class_rules",
                "Cut rules per model class",
                "board_cut_class_rules",
                required=True,
                default_value=[],
                help_text=(
                    "Rules are generated from the upstream deployed model. Class names "
                    "and internal identifiers cannot be entered manually."
                ),
            ),
            prop(
                "calibration_rms_mm",
                "Calibration RMS [mm]",
                "number",
                required=True,
                default_value=0.0,
                min_value=0,
            ),
            prop(
                "max_calibration_rms_mm",
                "Maximum calibration RMS [mm]",
                "number",
                required=True,
                default_value=2.0,
                min_value=0.1,
            ),
            prop(
                "uncertainty_multiplier",
                "Calibration uncertainty factor",
                "number",
                required=True,
                default_value=3.0,
                min_value=0,
            ),
            prop(
                "minimum_board_end_confidence",
                "Minimum board-end confidence",
                "number",
                required=True,
                default_value=0.5,
                min_value=0,
                max_value=1,
            ),
        ],
        handler=("cv_runtime.nodes.board_cut:analysis_board_view_cut_constraints"),
        constraints={"port_aliases": {"detections": ["region_set", "segments"]}},
    ),
    _node(
        node_type="analysis.end_face_cut_constraints",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="End-face cut constraints",
        description=(
            "Measure end-face features metrically and convert them into conservative "
            "longitudinal cut constraints."
        ),
        supported_modes=BOARD_CUT_TEST_MODES,
        input_ports=[
            port("features", "detections"),
            port("frame", "frame", required=False),
            port("segments", "segments", required=False),
        ],
        output_ports=[
            port("constraints", "board_cut_constraints"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "inspection_length_mm": 300.0,
            "calibration_rms_mm": 0.0,
            "max_calibration_rms_mm": 2.0,
            "surface": {},
            "class_rules": [],
            "board_surface_class_mapping": {},
            "board_width_mm": 0.0,
            "board_thickness_mm": 0.0,
        },
        property_schema=[
            prop(
                "inspection_length_mm",
                "Inspection length [mm]",
                "number",
                required=True,
                default_value=300.0,
                min_value=1,
            ),
            prop(
                "board_surface_class_mapping",
                "Board end-face class",
                "board_surface_class_mapping",
                default_value={},
                help_text=(
                    "Assign the selected segmentation-model class that represents the board end face."
                ),
            ),
            prop(
                "board_width_mm",
                "Test board width [mm]",
                "number",
                default_value=0.0,
                min_value=0,
                help_text=(
                    "Realtime test fallback. Trigger pipelines use board_format.width_mm."
                ),
            ),
            prop(
                "board_thickness_mm",
                "Test board thickness [mm]",
                "number",
                default_value=0.0,
                min_value=0,
                help_text=(
                    "Realtime test fallback. Trigger pipelines use board_format.thickness_mm."
                ),
            ),
            prop(
                "surface",
                "End-face calibration",
                "board_surface_calibration",
                required=False,
                default_value={},
                help_text=(
                    "Legacy image calibration fallback. Segmentation pipelines derive the current "
                    "end-face homography from the selected polygon and board_format."
                ),
            ),
            prop(
                "class_rules",
                "Cut rules per model class",
                "board_cut_class_rules",
                required=True,
                default_value=[],
                help_text=(
                    "Rules are generated from the upstream deployed model. Class names "
                    "and internal identifiers cannot be entered manually."
                ),
            ),
            prop(
                "calibration_rms_mm",
                "Calibration RMS [mm]",
                "number",
                required=True,
                default_value=0.0,
                min_value=0,
            ),
            prop(
                "max_calibration_rms_mm",
                "Maximum calibration RMS [mm]",
                "number",
                required=True,
                default_value=2.0,
                min_value=0.1,
            ),
        ],
        handler="cv_runtime.nodes.board_cut:analysis_end_face_cut_constraints",
        constraints={"port_aliases": {"detections": ["region_set", "segments"]}},
    ),
    _node(
        node_type="analysis.multi_view_board_cut_fusion",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Multi-view board cut fusion",
        description=(
            "Fail-closed fusion of upper, lower, and end-face constraints. The largest "
            "safe cut value wins; an entirely clear inspection returns a valid 0 mm."
        ),
        supported_modes=TRIGGER_MODES,
        input_ports=[
            port("upper", "board_cut_constraints"),
            port("lower", "board_cut_constraints"),
            port("end_face", "board_cut_constraints"),
            port("frame_set", "synchronized_frame_set"),
        ],
        output_ports=[
            port("decision", "board_cut_decision"),
            port("result", "result"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "inspection_length_mm": 300.0,
            "show_cut_line_overlay": True,
        },
        property_schema=[
            prop(
                "inspection_length_mm",
                "Inspection length [mm]",
                "number",
                required=True,
                default_value=300.0,
                min_value=1,
            ),
            prop(
                "show_cut_line_overlay",
                "Show virtual cut line",
                "boolean",
                default_value=True,
                help_text=(
                    "Draws the calculated cut position in every usable longitudinal camera "
                    "view. The line is shown only for a valid cut greater than 0 mm and does "
                    "not change the machine value."
                ),
            ),
        ],
        handler="cv_runtime.nodes.board_cut:analysis_multi_view_board_cut_fusion",
        constraints={
            "required_semantic_inputs": ["upper", "lower", "end_face", "frame_set"],
            "standalone_function": True,
        },
    ),
    _node(
        node_type="analysis.single_view_board_cut_decision",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Single-view board cut decision (test)",
        description=(
            "Produce the board.cut-decision.v1 result from exactly one calibrated "
            "camera view for commissioning and model tests."
        ),
        supported_modes=BOARD_CUT_TEST_MODES,
        input_ports=[
            port("constraints", "board_cut_constraints"),
            port("frame_set", "synchronized_frame_set", required=False),
        ],
        output_ports=[
            port("decision", "board_cut_decision"),
            port("result", "result"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "inspection_length_mm": 300.0,
            "show_cut_line_overlay": True,
        },
        property_schema=[
            prop(
                "inspection_length_mm",
                "Inspection length [mm]",
                "number",
                required=True,
                default_value=300.0,
                min_value=1,
            ),
            prop(
                "show_cut_line_overlay",
                "Show virtual cut line",
                "boolean",
                default_value=True,
                help_text=(
                    "Draws the calculated cut position in the current longitudinal test view. "
                    "The line is shown only for a valid cut greater than 0 mm and does not "
                    "change the result. An end-face-only view cannot display a longitudinal "
                    "cut line."
                ),
            ),
        ],
        handler="cv_runtime.nodes.board_cut:analysis_single_view_board_cut_decision",
        constraints={"test_only": True, "required_view_count": 1},
    ),
    _node(
        node_type="analysis.clearance_zone_generator",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Clearance zone generator",
        description="Generate blocked intervals along the board axis from measured feature rules.",
        supported_modes=ALL_MODES,
        input_ports=[
            port("measurements", "feature_measurements"),
            port("coordinate_frame", "coordinate_frame"),
        ],
        output_ports=[port("clearance_zones", "clearance_zones")],
        default_config={
            "clearance_rules": [
                {
                    "feature_type": "knot",
                    "enabled": True,
                    "measurement_basis": "bbox_width",
                    "multiplier": 1.0,
                    "minimum_clearance_px": 0,
                    "maximum_clearance_px": None,
                    "apply_before": True,
                    "apply_after": True,
                    "merge_overlapping_zones": True,
                    "severity": "warning",
                    "category": "defect_clearance",
                }
            ],
            "feature_confidence_threshold": 0.35,
        },
        property_schema=[
            prop(
                "clearance_rules",
                "Clearance rules",
                "clearance_rule_table",
                required=True,
                default_value=[
                    {
                        "feature_type": "knot",
                        "enabled": True,
                        "measurement_basis": "bbox_width",
                        "multiplier": 1.0,
                        "minimum_clearance_px": 0,
                        "apply_before": True,
                        "apply_after": True,
                        "merge_overlapping_zones": True,
                        "severity": "warning",
                        "category": "defect_clearance",
                    }
                ],
            ),
            prop(
                "feature_confidence_threshold",
                "Minimum feature confidence",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
            ),
        ],
    ),
    _node(
        node_type="analysis.first_free_region_finder",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="First free region finder",
        description="Search from the board origin and return the first clear interval that satisfies the minimum length.",
        supported_modes=ALL_MODES,
        input_ports=[
            port("coordinate_frame", "coordinate_frame"),
            port("clearance_zones", "clearance_zones"),
        ],
        output_ports=[port("free_region", "free_region")],
        default_config={
            "search_direction": "positive_longitudinal",
            "start_offset_px": 0,
            "minimum_free_length_px": 1,
            "end_limit_px": None,
            "treat_outside_known_board_region_as_invalid": False,
            "merge_blocked_intervals": True,
        },
        property_schema=[
            prop(
                "search_direction",
                "Search direction",
                "select",
                default_value="positive_longitudinal",
                options=[
                    ("positive_longitudinal", "Positive longitudinal"),
                    ("negative_longitudinal", "Negative longitudinal"),
                ],
            ),
            prop("start_offset_px", "Start offset px", "number", default_value=0, min_value=0),
            prop(
                "minimum_free_length_px",
                "Minimum free length px",
                "number",
                default_value=1,
                min_value=0,
            ),
            prop("end_limit_px", "End limit px", "number", default_value=None, min_value=0),
            prop(
                "treat_outside_known_board_region_as_invalid",
                "Require end limit",
                "boolean",
                default_value=False,
            ),
            prop(
                "merge_blocked_intervals", "Merge blocked intervals", "boolean", default_value=True
            ),
        ],
    ),
    _node(
        node_type="analysis.pixel_to_metric",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Pixel to metric conversion",
        description="Apply simple mm-per-pixel calibration to measured board geometry and selected result fields.",
        supported_modes=ALL_MODES,
        input_ports=[
            port("free_region", "free_region", required=False),
            port("measurements", "feature_measurements", required=False),
            port("clearance_zones", "clearance_zones", required=False),
            port("coordinate_frame", "coordinate_frame", required=False),
            port("reference_edges", "reference_edges", required=False),
            port("decision", "quality_decision", required=False),
        ],
        output_ports=[
            port("feature_measurements", "feature_measurements", required=False),
            port("clearance_zones", "clearance_zones", required=False),
            port("free_region", "free_region", required=False),
            port("result", "result"),
        ],
        default_config={
            "mm_per_pixel": 1.0,
            "apply_to": [
                "feature_measurements",
                "clearance_zones",
                "free_region",
                "selected_result_fields",
            ],
        },
        property_schema=[
            prop(
                "mm_per_pixel",
                "mm per pixel",
                "number",
                required=True,
                default_value=1.0,
                min_value=0.000001,
            ),
            prop(
                "apply_to",
                "Apply to",
                "metric_apply_to",
                default_value=[
                    "feature_measurements",
                    "clearance_zones",
                    "free_region",
                    "selected_result_fields",
                ],
            ),
        ],
    ),
    _node(
        node_type="analysis.quality_rule_evaluation",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Quality rule evaluation",
        description="Evaluate simple count, size, total, and free-region rules over measured features.",
        supported_modes=ALL_MODES,
        input_ports=[
            port("measurements", "feature_measurements", required=False),
            port("free_region", "free_region", required=False),
        ],
        output_ports=[port("decision", "quality_decision"), port("result", "result")],
        default_config={
            "rules": [
                {
                    "rule_type": "feature_count_by_type",
                    "feature_type": "knot",
                    "operator": "<=",
                    "threshold": 3,
                    "status_on_fail": "warning",
                }
            ],
            "result_label": "Board quality",
        },
        property_schema=[
            prop(
                "rules",
                "Quality rules",
                "quality_rule_table",
                default_value=[
                    {
                        "rule_type": "feature_count_by_type",
                        "feature_type": "knot",
                        "operator": "<=",
                        "threshold": 3,
                        "status_on_fail": "warning",
                    }
                ],
            ),
            prop("result_label", "Result label", "text", default_value="Board quality"),
        ],
    ),
    _node(
        node_type="analysis.active_stem_tracker",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Active stem tracker",
        description=(
            "Select exactly one relevant segmented stem and maintain a stable track ID "
            "across serialized realtime frames. By default the largest mask per process "
            "region is used, while an already active track keeps temporal priority. When a "
            "rectified process frame is connected, calibrated process motion classifies "
            "Y-dominant transfer and X-dominant longitudinal transport."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[
            port("segments", "segments"),
            port("frame", "frame", required=False),
            port("events", "events", required=False),
            port("trigger", "trigger", required=False),
        ],
        output_ports=[
            port("segments", "segments"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "transfer_rois": [],
            "longitudinal_conveyor_rois": [],
            "class_filter": "",
            "minimum_score": 0.35,
            "handover_reference_mode": "transfer_roi_center",
            "expected_handover_x": 0.5,
            "expected_handover_y": 0.5,
            "expected_motion_direction": "any",
            "minimum_roi_overlap": 0.05,
            "expected_full_length_normalized": 0.55,
            "minimum_visible_length_fraction": 0.25,
            "maximum_centroid_distance": 0.25,
            "minimum_acquisition_score": 0.35,
            "minimum_continuity_score": 0.5,
            "ambiguity_margin": 0.08,
            "candidate_selection_policy": "largest_per_process_region",
            "minimum_region_area_dominance_ratio": 1.10,
            "prefer_longitudinal_on_acquisition": True,
            "complete_at_decision_gate": True,
            "max_lost_frames": 12,
            "reacquisition_minimum_score": 0.65,
            "reacquisition_ambiguity_margin": 0.12,
            "preserve_upstream_identity_during_occlusion": True,
            "completed_exclusion_frames": 90,
            "completed_exclusion_iou": 0.5,
            "max_completed_tracks": 32,
            "motion_smoothing_alpha": 0.6,
            "process_motion_axis_dominance_ratio": 1.75,
            "transfer_y_motion_sensitivity": 0.727273,
            "minimum_process_motion_mm_per_frame": 2.0,
            "longitudinal_motion_confirmation_frames": 3,
            "longitudinal_motion_window_frames": 6,
            "longitudinal_minimum_forward_displacement_mm": 120.0,
            "longitudinal_minimum_forward_progress_ratio": 0.75,
            "longitudinal_entry_centroid_required": True,
            "maximum_time_gap_seconds": 2.0,
            "completion_event_types": "",
            "reset_event_types": "",
            "debug_overlay": True,
        },
        property_schema=[
            prop(
                "transfer_rois",
                "Transfer ROIs",
                "roi",
                required=True,
                default_value=[],
                help_text=(
                    "Draw the actual transverse movement corridor from the release edge of "
                    "the waiting position into a small overlap with the longitudinal conveyor. "
                    "Do not include the complete resting stem package. Convex polygons follow "
                    "oblique conveyor edges; existing rectangular ROIs remain supported."
                ),
            ),
            prop(
                "longitudinal_conveyor_rois",
                "Longitudinal conveyor ROIs",
                "roi",
                required=True,
                default_value=[],
                help_text=(
                    "Draw the downstream longitudinal-conveyor area on the canonical model "
                    "image. Use a convex polygon when perspective makes the belt edges oblique. "
                    "Let it overlap the transfer polygon slightly so temporal handover has no "
                    "untracked gap."
                ),
            ),
            prop(
                "class_filter",
                "Stem classes",
                "text",
                default_value="",
                help_text=(
                    "Optional comma-separated segmentation labels. Empty means every "
                    "segmentation class is accepted; enter the exact model label only when "
                    "other classes must be ignored."
                ),
            ),
            prop(
                "minimum_score",
                "Minimum segmentation score",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
            ),
            prop(
                "handover_reference_mode",
                "Handover reference",
                "select",
                default_value="transfer_roi_center",
                options=[
                    ("transfer_roi_center", "Automatic from transfer polygon"),
                    ("manual_point", "Manual normalized image point"),
                ],
                help_text=(
                    "Automatic is recommended and removes a duplicate setup step. The tracker "
                    "derives its acquisition reference from the largest transfer polygon."
                ),
            ),
            prop(
                "expected_handover_x",
                "Expected handover X",
                "number",
                default_value=0.5,
                min_value=0,
                max_value=1,
                help_text=(
                    "Legacy/manual normalized image X coordinate. Hidden when the transfer "
                    "polygon supplies the reference automatically."
                ),
            ),
            prop(
                "expected_handover_y",
                "Expected handover Y",
                "number",
                default_value=0.5,
                min_value=0,
                max_value=1,
                help_text=(
                    "Legacy/manual normalized image Y coordinate. Hidden when the transfer "
                    "polygon supplies the reference automatically."
                ),
            ),
            prop(
                "expected_motion_direction",
                "Expected motion direction",
                "select",
                default_value="any",
                options=[
                    ("any", "Any direction"),
                    ("left", "Left"),
                    ("right", "Right"),
                    ("up", "Up"),
                    ("down", "Down"),
                ],
            ),
            prop(
                "minimum_roi_overlap",
                "Minimum ROI overlap",
                "number",
                default_value=0.05,
                min_value=0,
                max_value=1,
            ),
            prop(
                "expected_full_length_normalized",
                "Expected visible full length",
                "number",
                default_value=0.55,
                min_value=0.01,
                max_value=1.414214,
                help_text=(
                    "Expected projected stem length in normalized image coordinates. "
                    "This value is not a metric length."
                ),
            ),
            prop(
                "minimum_visible_length_fraction",
                "Minimum visible length fraction",
                "number",
                default_value=0.25,
                min_value=0,
                max_value=1,
            ),
            prop(
                "maximum_centroid_distance",
                "Maximum centroid distance",
                "number",
                default_value=0.25,
                min_value=0.01,
                max_value=1.414214,
                help_text="Maximum normalized prediction distance used for continuity scoring.",
            ),
            prop(
                "minimum_acquisition_score",
                "Minimum acquisition confidence",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
            ),
            prop(
                "minimum_continuity_score",
                "Minimum continuity confidence",
                "number",
                default_value=0.5,
                min_value=0,
                max_value=1,
            ),
            prop(
                "ambiguity_margin",
                "Ambiguity margin",
                "number",
                default_value=0.08,
                min_value=0,
                max_value=1,
                help_text=(
                    "Fail closed when the two best candidate scores differ by no more "
                    "than this value."
                ),
            ),
            prop(
                "complete_at_decision_gate",
                "Complete track at decision gate",
                "boolean",
                default_value=True,
                help_text=(
                    "Recommended for the stem minimum pipeline. The current stem is emitted "
                    "for its final decision frame and then closed, allowing an already visible "
                    "successor in the transfer corridor to receive a new track ID."
                ),
            ),
            prop(
                "max_lost_frames",
                "Occlusion grace frames",
                "integer",
                default_value=12,
                min_value=0,
                max_value=120,
                help_text=(
                    "Keep the physical active-stem identity through short segmentation "
                    "dropouts. At 10 processed frames/s, 12 frames bridge about 1.2 seconds."
                ),
            ),
            prop(
                "reacquisition_minimum_score",
                "Reacquisition confidence",
                "number",
                default_value=0.65,
                min_value=0,
                max_value=1,
                help_text=(
                    "After a dropout, require stronger spatial and motion continuity than "
                    "during uninterrupted tracking before accepting a segment again."
                ),
            ),
            prop(
                "reacquisition_ambiguity_margin",
                "Reacquisition ambiguity margin",
                "number",
                default_value=0.12,
                min_value=0,
                max_value=1,
            ),
            prop(
                "preserve_upstream_identity_during_occlusion",
                "Preserve upstream identity",
                "boolean",
                default_value=True,
                help_text=(
                    "When inference supplies stable upstream track IDs, do not switch to a "
                    "different upstream stem while reacquiring after an occlusion."
                ),
            ),
            prop(
                "completed_exclusion_frames",
                "Completed-track exclusion frames",
                "integer",
                default_value=90,
                min_value=0,
                max_value=10000,
            ),
            prop(
                "completed_exclusion_iou",
                "Completed-track exclusion IoU",
                "number",
                default_value=0.5,
                min_value=0,
                max_value=1,
            ),
            prop(
                "max_completed_tracks",
                "Maximum remembered completed tracks",
                "integer",
                default_value=32,
                min_value=1,
                max_value=256,
            ),
            prop(
                "motion_smoothing_alpha",
                "Motion smoothing alpha",
                "number",
                default_value=0.6,
                min_value=0,
                max_value=1,
            ),
            prop(
                "process_motion_axis_dominance_ratio",
                "Longitudinal X/Y dominance ratio",
                "number",
                default_value=1.75,
                min_value=1,
                max_value=10,
                step_value=0.05,
                help_text=(
                    "Minimum calibrated X-to-Y velocity ratio for one observation to count "
                    "as conveyor-directed longitudinal evidence."
                ),
            ),
            prop(
                "transfer_y_motion_sensitivity",
                "Transfer Y motion sensitivity",
                "number",
                default_value=0.727273,
                min_value=0,
                max_value=1,
                step_value=0.05,
                help_text=(
                    "Controls how readily mixed mask motion is still treated as the "
                    "transverse Y transfer. Higher values react to smaller Y components "
                    "and delay the switch to longitudinal X transport; lower values ignore "
                    "minor lateral rolling sooner. Strong conveyor-directed X motion still "
                    "takes priority."
                ),
            ),
            prop(
                "minimum_process_motion_mm_per_frame",
                "Minimum process motion [mm/frame]",
                "number",
                default_value=2.0,
                min_value=0,
                max_value=1000,
                step_value=0.5,
            ),
            prop(
                "longitudinal_motion_confirmation_frames",
                "Longitudinal evidence observations",
                "integer",
                default_value=3,
                min_value=1,
                max_value=30,
                help_text=(
                    "Required conveyor-directed X observations inside the temporal window. "
                    "Calibrated tracking always requires at least three."
                ),
            ),
            prop(
                "longitudinal_motion_window_frames",
                "Longitudinal evidence window",
                "integer",
                default_value=6,
                min_value=3,
                max_value=60,
                help_text=(
                    "Number of recent tracked observations used for signed net-X and "
                    "direction-consistency evidence."
                ),
            ),
            prop(
                "longitudinal_minimum_forward_displacement_mm",
                "Minimum longitudinal net X [mm]",
                "number",
                default_value=120.0,
                min_value=0,
                max_value=2000,
                step_value=5,
                help_text=(
                    "Minimum signed net displacement in conveyor direction before transfer "
                    "can switch to longitudinal transport. Set above the expected transfer "
                    "X drift."
                ),
            ),
            prop(
                "longitudinal_minimum_forward_progress_ratio",
                "Minimum longitudinal progress ratio",
                "number",
                default_value=0.75,
                min_value=0,
                max_value=1,
                step_value=0.05,
                help_text=(
                    "Required signed net-X divided by total absolute X travel. Oscillating "
                    "mask-centroid motion therefore cannot confirm longitudinal transport."
                ),
            ),
            prop(
                "longitudinal_entry_centroid_required",
                "Require centroid in longitudinal ROI",
                "boolean",
                default_value=True,
                help_text=(
                    "Require the tracked stem centroid to enter the configured longitudinal "
                    "ROI in addition to temporal X-motion evidence."
                ),
            ),
            prop(
                "maximum_time_gap_seconds",
                "Maximum observation gap [s]",
                "number",
                default_value=2.0,
                min_value=0.01,
                max_value=300,
            ),
            prop(
                "completion_event_types",
                "Completion event types",
                "text",
                default_value="",
                help_text=(
                    "Optional comma-separated EventsPayload or TriggerPayload event names "
                    "that complete the current track."
                ),
            ),
            prop(
                "reset_event_types",
                "Reset event types",
                "text",
                default_value="",
                help_text=(
                    "Optional comma-separated EventsPayload or TriggerPayload event names "
                    "that clear the tracker state."
                ),
            ),
            prop(
                "debug_overlay",
                "Show tracking overlay",
                "boolean",
                default_value=True,
            ),
        ],
        handler="cv_runtime.nodes.stem_orientation:analysis_active_stem_tracker",
        constraints={
            "stateful": True,
            "fail_closed_single_active_instance": True,
            "preserves_segment_geometry": True,
            "accepts_stem_process_geometry_from_frame": True,
        },
    ),
    _node(
        node_type="analysis.stem_measurement_eligibility",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem measurement corridor eligibility",
        description=(
            "Fail-closed decision for canonical segmentation capture and curvature "
            "observation frames. Rejected frames remain diagnostic results and are not "
            "normal Label Studio segmentation tasks."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[
            port("frame", "frame"),
            port("segments", "segments"),
            port("active_stem", "segments", required=False),
        ],
        output_ports=[
            port("result", "result"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "view_profile_id": "",
            "measurement_corridor": [
                {"label": "Measurement corridor", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
            ],
            "crowd_boundary": {"start": {"x": 0.98, "y": 0.0}, "end": {"x": 0.98, "y": 1.0}},
            "min_visible_axis_ratio": 0.55,
            "min_stable_frames": 3,
            "reject_if_touching_crowd_boundary": True,
            "reject_if_multiple_stems_in_view": False,
            "max_blur_score": 0.55,
            "crowd_boundary_tolerance": 0.01,
            "allow_negative_frames": True,
            "negative_frame_kind": "EMPTY_CORRIDOR",
            "debug_overlay": True,
        },
        property_schema=[
            prop("view_profile_id", "View profile ID", "text", required=True),
            prop(
                "crowd_boundary",
                "Crowd boundary",
                "direction_reference",
                required=True,
                section="1. Corridor and waiting-stem boundary",
                help_text=(
                    "The measurement corridor is already the complete canonical image view. "
                    "Only adjust this rejection line on the side facing the waiting stem package "
                    "when that package can still touch the view boundary."
                ),
            ),
            prop("min_visible_axis_ratio", "Minimum visible axis ratio", "number", default_value=0.55, min_value=0, max_value=1, section="2. Acceptance rules"),
            prop("min_stable_frames", "Minimum stable track frames", "integer", default_value=3, min_value=1, max_value=120, section="2. Acceptance rules"),
            prop("reject_if_touching_crowd_boundary", "Reject crowd-boundary contact", "boolean", default_value=True, section="2. Acceptance rules"),
            prop(
                "reject_if_multiple_stems_in_view",
                "Reject multiple stems in view",
                "boolean",
                default_value=False,
                section="2. Acceptance rules",
                help_text=(
                    "Enable only when every additional visible stem must invalidate the frame. "
                    "Leave disabled when the Active Stem Tracker is expected to select one "
                    "isolated process candidate while predecessor or waiting stems remain visible."
                ),
            ),
            prop("max_blur_score", "Maximum normalized blur score", "number", default_value=0.55, min_value=0, max_value=1, section="2. Acceptance rules"),
            prop("crowd_boundary_tolerance", "Crowd boundary tolerance", "number", default_value=0.01, min_value=0, max_value=0.25, section="2. Acceptance rules"),
            prop("allow_negative_frames", "Allow configured negative frames", "boolean", default_value=True, section="3. Label Studio negatives"),
            prop(
                "negative_frame_kind",
                "Negative frame kind",
                "select",
                default_value="EMPTY_CORRIDOR",
                options=[
                    ("EMPTY_CORRIDOR", "Empty corridor"),
                    ("MACHINE_MOTION", "Machine motion"),
                    ("LIGHT_CHANGE", "Shadow / light change"),
                    ("WOOD_DEBRIS", "Small wood debris"),
                ],
                section="3. Label Studio negatives",
            ),
            prop("debug_overlay", "Show eligibility overlay", "boolean", default_value=True, section="4. Diagnostics"),
        ],
        handler=(
            "cv_runtime.nodes.stem_measurement_eligibility:"
            "analysis_stem_measurement_eligibility"
        ),
        constraints={
            "stateful": True,
            "requires_canonical_image_view": True,
            "fail_closed": True,
            "no_waiting_log_class": True,
        },
    ),
    _node(
        node_type="analysis.stem_mask_geometry",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem mask geometry",
        description=(
            "Select one matching segmented stem and derive deterministic projected "
            "reference-axis, centerline, residual, width, and quality profiles."
        ),
        supported_modes=ALL_MODES,
        input_ports=[
            port("segments", "segments"),
            port("frame", "frame", required=False),
        ],
        output_ports=[
            port("stem_geometry", "stem_geometry"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "class_filter": "",
            "minimum_score": 0.35,
            "selection_strategy": "largest_matching_segment",
            "analysis_mode": "robust_full",
            "centerline_sample_count": 32,
            "width_sample_count": 16,
            "smoothing_passes": 2,
            "end_trim_percent": 0.2,
            "clipped_end_window_percent": 8.0,
            "min_component_area_px": 300,
            "min_visible_length_px": 60.0,
            "debug_overlay": True,
        },
        property_schema=[
            prop(
                "analysis_mode",
                "Geometry workload",
                "select",
                required=True,
                default_value="robust_full",
                options=[
                    ("robust_full", "Full centerline analysis"),
                    ("phase_tracking_fast", "Fast axis for surface phase"),
                ],
                help_text=(
                    "Use the fast axis path when this node only supplies stem axis and width "
                    "to Surface phase. Horizontal curvature performs its own calibrated full "
                    "measurement, so calculating a second full profile wastes frame time."
                ),
            ),
            prop(
                "class_filter",
                "Stem classes",
                "text",
                default_value="",
                help_text=(
                    "Optional comma-separated segmentation labels. Leave empty to accept "
                    "all labels before applying the score and selection rules."
                ),
            ),
            prop(
                "minimum_score",
                "Minimum segmentation score",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
            ),
            prop(
                "selection_strategy",
                "Instance selection",
                "select",
                required=True,
                default_value="largest_matching_segment",
                options=[
                    ("largest_matching_segment", "Largest matching segment"),
                    ("single_matching_segment", "Require exactly one match"),
                ],
                help_text=(
                    "Largest matching segment is the temporary stateless development rule. "
                    "Equal largest areas remain ambiguous; active-stem tracking follows later."
                ),
            ),
            prop(
                "centerline_sample_count",
                "Centerline / residual samples",
                "integer",
                required=True,
                default_value=32,
                min_value=8,
                max_value=128,
            ),
            prop(
                "width_sample_count",
                "Width samples",
                "integer",
                required=True,
                default_value=16,
                min_value=4,
                max_value=64,
            ),
            prop(
                "smoothing_passes",
                "Low-frequency smoothing passes",
                "integer",
                default_value=2,
                min_value=0,
                max_value=8,
            ),
            prop(
                "end_trim_percent",
                "Robust end trim [%]",
                "number",
                default_value=0.2,
                min_value=0,
                max_value=9,
                help_text=(
                    "Symmetrically excludes this projected fraction at each visible end when "
                    "estimating the robust longitudinal extent; it does not infer hidden length."
                ),
            ),
            prop(
                "clipped_end_window_percent",
                "Clipped-end uncertainty window [%]",
                "number",
                default_value=8.0,
                min_value=0,
                max_value=25,
            ),
            prop(
                "min_component_area_px",
                "Minimum mask area [px]",
                "integer",
                default_value=300,
                min_value=1,
            ),
            prop(
                "min_visible_length_px",
                "Minimum visible projected length [px]",
                "number",
                default_value=60.0,
                min_value=1,
            ),
            prop(
                "debug_overlay",
                "Show stem geometry overlay",
                "boolean",
                default_value=True,
                help_text=(
                    "Draw the selected contour, straight reference axis, robust centerline, "
                    "profile support points, and accepted or uncertain cross-sections."
                ),
            ),
        ],
        handler="cv_runtime.nodes.stem_orientation:analysis_stem_mask_geometry",
        constraints={"fail_closed_single_instance": True},
    ),
    _node(
        node_type="analysis.stem_horizontal_curvature",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem horizontal curvature",
        description=(
            "Measure robust horizontal projected stem curvature in the calibrated X/Y "
            "reference plane for the single active tracked stem. Observations stop at "
            "the configured curvature freeze gate."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[
            port("segments", "segments"),
            port("frame", "frame"),
            port("eligibility", "result", required=False),
        ],
        output_ports=[
            port("curvature", "stem_horizontal_curvature"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "minimum_observation_interval_ms": 0,
            "station_count": 96,
            "inner_longitudinal_fraction": 0.8,
            "minimum_visible_length_ratio": 0.55,
            "minimum_common_segment_ratio": 0.4,
            "minimum_valid_station_ratio": 0.65,
            "minimum_valid_station_count": 24,
            "morphology_kernel_px": 3,
            "minimum_component_area_px": 120,
            "meaningful_component_fraction": 0.08,
            "minimum_selected_component_fraction": 0.85,
            "minimum_width_mm": 10.0,
            "maximum_local_width_deviation_ratio": 0.15,
            "width_outlier_mad_scale": 4.0,
            "centerline_outlier_mad_scale": 4.5,
            "smoothing_window": 7,
            "endpoint_window_fraction": 0.12,
            "straight_bow_threshold_mm": 2.0,
            "s_bow_secondary_lobe_ratio": 0.35,
            "minimum_tracking_confidence": 0.35,
            "minimum_segmentation_score": 0.35,
            "minimum_geometry_confidence": 0.4,
            "debug_overlay": True,
        },
        property_schema=[
            prop(
                "minimum_observation_interval_ms",
                "Minimum observation interval [ms]",
                "integer",
                default_value=0,
                min_value=0,
                max_value=5000,
                section="Performance",
                help_text=(
                    "Zero measures every eligible pipeline frame. A nonzero value caps the "
                    "authoritative silhouette observation rate; 50 ms permits at most 20 Hz."
                ),
            ),
            prop(
                "station_count",
                "X stations",
                "integer",
                default_value=96,
                min_value=16,
                max_value=256,
                section="Measurement",
            ),
            prop(
                "inner_longitudinal_fraction",
                "Comparable inner length fraction",
                "number",
                default_value=0.8,
                min_value=0.1,
                max_value=1,
                step_value=0.01,
                section="Comparable segment",
                help_text=(
                    "Use the centered physical fraction of the currently visible X span; "
                    "hidden ends are never reconstructed."
                ),
            ),
            prop(
                "minimum_visible_length_ratio",
                "Minimum visible ROI length ratio",
                "number",
                default_value=0.55,
                min_value=0.05,
                max_value=1,
                step_value=0.01,
                section="Comparable segment",
            ),
            prop(
                "minimum_common_segment_ratio",
                "Minimum common segment ratio",
                "number",
                default_value=0.4,
                min_value=0.05,
                max_value=1,
                step_value=0.01,
                section="Comparable segment",
                help_text=(
                    "Measurements are marked non-comparable below this common physical "
                    "coverage."
                ),
            ),
            prop(
                "minimum_valid_station_ratio",
                "Minimum valid station ratio",
                "number",
                default_value=0.65,
                min_value=0.1,
                max_value=1,
                step_value=0.01,
                section="Quality",
            ),
            prop(
                "minimum_valid_station_count",
                "Minimum valid station count",
                "integer",
                default_value=24,
                min_value=8,
                max_value=256,
                section="Quality",
            ),
            prop(
                "morphology_kernel_px",
                "Mask cleanup kernel [px]",
                "integer",
                default_value=3,
                min_value=1,
                max_value=31,
                section="Robustness",
            ),
            prop(
                "minimum_component_area_px",
                "Minimum component area [px]",
                "integer",
                default_value=120,
                min_value=1,
                section="Robustness",
            ),
            prop(
                "meaningful_component_fraction",
                "Meaningful fragment fraction",
                "number",
                default_value=0.08,
                min_value=0.01,
                max_value=1,
                step_value=0.01,
                section="Robustness",
                help_text=(
                    "A second component above this fraction invalidates the observation; "
                    "V1 never merges machine-occluded fragments."
                ),
            ),
            prop(
                "minimum_selected_component_fraction",
                "Minimum selected component fraction",
                "number",
                default_value=0.85,
                min_value=0.1,
                max_value=1,
                step_value=0.01,
                section="Robustness",
            ),
            prop(
                "minimum_width_mm",
                "Minimum stem width [mm]",
                "number",
                default_value=10,
                min_value=0.1,
                section="Robustness",
            ),
            prop(
                "maximum_local_width_deviation_ratio",
                "Maximum local width deviation ratio",
                "number",
                default_value=0.15,
                min_value=0.01,
                max_value=1,
                step_value=0.01,
                section="Robustness",
            ),
            prop(
                "width_outlier_mad_scale",
                "Width outlier MAD scale",
                "number",
                default_value=4,
                min_value=0.1,
                section="Robustness",
            ),
            prop(
                "centerline_outlier_mad_scale",
                "Centerline outlier MAD scale",
                "number",
                default_value=4.5,
                min_value=0.1,
                section="Robustness",
            ),
            prop(
                "smoothing_window",
                "Centerline smoothing window",
                "integer",
                default_value=7,
                min_value=1,
                max_value=31,
                section="Measurement",
            ),
            prop(
                "endpoint_window_fraction",
                "Reference endpoint window fraction",
                "number",
                default_value=0.12,
                min_value=0.01,
                max_value=0.5,
                step_value=0.01,
                section="Measurement",
            ),
            prop(
                "straight_bow_threshold_mm",
                "Straight bow threshold [mm]",
                "number",
                default_value=2,
                min_value=0,
                section="Classification",
            ),
            prop(
                "s_bow_secondary_lobe_ratio",
                "S-bow secondary lobe ratio",
                "number",
                default_value=0.35,
                min_value=0.01,
                max_value=1,
                step_value=0.01,
                section="Classification",
            ),
            prop(
                "minimum_tracking_confidence",
                "Minimum tracking confidence",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Quality",
            ),
            prop(
                "minimum_segmentation_score",
                "Minimum segmentation score",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Quality",
            ),
            prop(
                "minimum_geometry_confidence",
                "Minimum geometry confidence",
                "number",
                default_value=0.4,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Quality",
            ),
            prop(
                "debug_overlay",
                "Show curvature overlay",
                "boolean",
                default_value=True,
                section="Output",
            ),
        ],
        handler=(
            "cv_runtime.nodes.stem_horizontal_curvature:"
            "analysis_stem_horizontal_curvature"
        ),
        constraints={
            "stateful": True,
            "requires_single_active_track": True,
            "requires_process_rectified_frame": True,
            "supports_perspective_preserved_model_view": True,
            "measurement_grid_is_internal": True,
            "requires_stem_curvature_process_calibration": True,
            "supports_measurement_eligibility_gate": True,
            "measurement_scope": "curvature_observation_roi_until_freeze_gate",
            "fragment_reconstruction": False,
            "comparison_strategy": "INNER_VISIBLE_LENGTH_FRACTION_V1",
        },
    ),
    _node(
        node_type="analysis.stem_surface_rotation",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem surface rotation",
        description=(
            "Track multiple bark features inside an eroded active-stem mask and convert "
            "their robust relative transverse motion with a validated angle calibration."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[
            port("frame", "frame"),
            port("segments", "segments"),
            port("stem_geometry", "stem_geometry"),
        ],
        output_ports=[
            port("stem_rotation", "stem_rotation"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "rotation_calibration_id": "",
            "rotation_calibration_version": "",
            "rotation_calibration_method": "known_angle_positions",
            "rotation_calibration_intrinsic_hash": "",
            "rotation_viewpoint_convention": "viewed_from_configured_log_end_a",
            "positive_rotation_direction": ("clockwise_viewed_from_configured_log_end"),
            "rotation_degrees_per_normalized_displacement": None,
            "rotation_calibration_sample_count": 0,
            "rotation_calibration_mean_abs_error_deg": None,
            "rotation_calibration_max_abs_error_deg": None,
            "rotation_calibration_mean_error_limit_deg": 3.0,
            "rotation_calibration_max_error_limit_deg": 7.5,
            "rotation_calibration_width_min": None,
            "rotation_calibration_width_max": None,
            "rotation_calibration_center_x_min": None,
            "rotation_calibration_center_x_max": None,
            "rotation_calibration_center_y_min": None,
            "rotation_calibration_center_y_max": None,
            "rotation_calibration_hash": "",
            "maximum_features": 160,
            "feature_quality_level": 0.02,
            "feature_minimum_distance_px": 7.0,
            "feature_block_size": 7,
            "lk_window_size": 21,
            "lk_max_level": 3,
            "mask_erosion_fraction": 0.18,
            "minimum_blur_score": 20.0,
            "maximum_frame_gap": 2,
            "maximum_time_gap_seconds": 0.5,
            "minimum_matched_features": 8,
            "minimum_inlier_ratio": 0.55,
            "maximum_tracking_error_px": 20.0,
            "maximum_longitudinal_shift_fraction": 0.18,
            "absolute_inlier_tolerance_normalized": 0.025,
            "inlier_mad_multiplier": 3.5,
            "maximum_abs_delta_rotation_deg": 60.0,
            "minimum_rotation_confidence": 0.35,
            "debug_overlay": True,
        },
        property_schema=[
            prop(
                "rotation_calibration_id",
                "Rotation calibration ID",
                "text",
                required=True,
                default_value="",
                section="Calibration",
            ),
            prop(
                "rotation_calibration_version",
                "Rotation calibration version",
                "text",
                required=True,
                default_value="",
                section="Calibration",
            ),
            prop(
                "rotation_calibration_method",
                "Calibration method",
                "select",
                required=True,
                default_value="known_angle_positions",
                options=[
                    ("known_angle_positions", "Known angle positions"),
                    (
                        "temporary_circumference_markers",
                        "Temporary circumference markers",
                    ),
                ],
                section="Calibration",
            ),
            prop(
                "rotation_calibration_intrinsic_hash",
                "Intrinsic calibration hash",
                "text",
                required=True,
                default_value="",
                section="Calibration",
                help_text=(
                    "Hash of the intrinsic camera profile used while recording the "
                    "known-angle calibration dataset."
                ),
            ),
            prop(
                "rotation_viewpoint_convention",
                "Rotation viewpoint",
                "select",
                required=True,
                default_value="viewed_from_configured_log_end_a",
                options=[
                    ("viewed_from_configured_log_end_a", "Viewed from configured log end A"),
                    ("viewed_from_configured_log_end_b", "Viewed from configured log end B"),
                ],
                section="Direction convention",
            ),
            prop(
                "positive_rotation_direction",
                "Positive rotation direction",
                "select",
                required=True,
                default_value="clockwise_viewed_from_configured_log_end",
                options=[
                    (
                        "clockwise_viewed_from_configured_log_end",
                        "Clockwise from configured log end",
                    ),
                    (
                        "counter_clockwise_viewed_from_configured_log_end",
                        "Counter-clockwise from configured log end",
                    ),
                ],
                section="Direction convention",
            ),
            prop(
                "rotation_degrees_per_normalized_displacement",
                "Degrees per normalized displacement",
                "number",
                required=True,
                default_value=None,
                section="Calibration",
            ),
            prop(
                "rotation_calibration_sample_count",
                "Validated calibration samples",
                "integer",
                required=True,
                default_value=0,
                min_value=0,
                section="Calibration",
            ),
            prop(
                "rotation_calibration_mean_abs_error_deg",
                "Calibration mean error [deg]",
                "number",
                required=True,
                default_value=None,
                min_value=0,
                section="Calibration",
            ),
            prop(
                "rotation_calibration_max_abs_error_deg",
                "Calibration maximum error [deg]",
                "number",
                required=True,
                default_value=None,
                min_value=0,
                section="Calibration",
            ),
            prop(
                "rotation_calibration_mean_error_limit_deg",
                "Allowed mean error [deg]",
                "number",
                default_value=3.0,
                min_value=0,
                section="Calibration",
            ),
            prop(
                "rotation_calibration_max_error_limit_deg",
                "Allowed maximum error [deg]",
                "number",
                default_value=7.5,
                min_value=0,
                section="Calibration",
            ),
            prop(
                "rotation_calibration_width_min",
                "Valid stem width fraction minimum",
                "number",
                required=True,
                default_value=None,
                min_value=0,
                max_value=1,
                section="Calibration range",
            ),
            prop(
                "rotation_calibration_width_max",
                "Valid stem width fraction maximum",
                "number",
                required=True,
                default_value=None,
                min_value=0,
                max_value=1,
                section="Calibration range",
            ),
            prop(
                "rotation_calibration_center_x_min",
                "Valid image center X minimum",
                "number",
                required=True,
                default_value=None,
                min_value=0,
                max_value=1,
                section="Calibration range",
            ),
            prop(
                "rotation_calibration_center_x_max",
                "Valid image center X maximum",
                "number",
                required=True,
                default_value=None,
                min_value=0,
                max_value=1,
                section="Calibration range",
            ),
            prop(
                "rotation_calibration_center_y_min",
                "Valid image center Y minimum",
                "number",
                required=True,
                default_value=None,
                min_value=0,
                max_value=1,
                section="Calibration range",
            ),
            prop(
                "rotation_calibration_center_y_max",
                "Valid image center Y maximum",
                "number",
                required=True,
                default_value=None,
                min_value=0,
                max_value=1,
                section="Calibration range",
            ),
            prop(
                "rotation_calibration_hash",
                "Rotation calibration hash",
                "text",
                required=True,
                default_value="",
                section="Calibration",
                help_text="SHA-256 hash calculated by the offline calibration core.",
            ),
            prop(
                "maximum_features",
                "Maximum surface features",
                "integer",
                default_value=160,
                min_value=16,
                max_value=1000,
                section="Feature tracking",
            ),
            prop(
                "feature_quality_level",
                "Feature quality level",
                "number",
                default_value=0.02,
                min_value=0.001,
                max_value=0.5,
                section="Feature tracking",
            ),
            prop(
                "feature_minimum_distance_px",
                "Feature minimum distance [px]",
                "number",
                default_value=7.0,
                min_value=1,
                section="Feature tracking",
            ),
            prop(
                "feature_block_size",
                "Feature block size",
                "integer",
                default_value=7,
                min_value=3,
                max_value=31,
                section="Feature tracking",
            ),
            prop(
                "lk_window_size",
                "Optical-flow window size",
                "integer",
                default_value=21,
                min_value=5,
                max_value=61,
                section="Feature tracking",
            ),
            prop(
                "lk_max_level",
                "Optical-flow pyramid levels",
                "integer",
                default_value=3,
                min_value=0,
                max_value=8,
                section="Feature tracking",
            ),
            prop(
                "mask_erosion_fraction",
                "Mask erosion fraction",
                "number",
                default_value=0.18,
                min_value=0.02,
                max_value=0.45,
                section="Feature tracking",
            ),
            prop(
                "minimum_blur_score",
                "Minimum blur score",
                "number",
                default_value=20.0,
                min_value=0,
                section="Quality gates",
            ),
            prop(
                "maximum_frame_gap",
                "Maximum frame gap",
                "integer",
                default_value=2,
                min_value=1,
                max_value=30,
                section="Quality gates",
            ),
            prop(
                "maximum_time_gap_seconds",
                "Maximum time gap [s]",
                "number",
                default_value=0.5,
                min_value=0.01,
                max_value=30,
                section="Quality gates",
            ),
            prop(
                "minimum_matched_features",
                "Minimum matched features",
                "integer",
                default_value=8,
                min_value=3,
                max_value=500,
                section="Quality gates",
            ),
            prop(
                "minimum_inlier_ratio",
                "Minimum feature inlier ratio",
                "number",
                default_value=0.55,
                min_value=0,
                max_value=1,
                section="Quality gates",
            ),
            prop(
                "maximum_tracking_error_px",
                "Maximum tracking error [px]",
                "number",
                default_value=20.0,
                min_value=0.1,
                section="Quality gates",
            ),
            prop(
                "maximum_longitudinal_shift_fraction",
                "Maximum longitudinal feature shift",
                "number",
                default_value=0.18,
                min_value=0.01,
                max_value=1,
                section="Quality gates",
            ),
            prop(
                "absolute_inlier_tolerance_normalized",
                "Absolute transverse inlier tolerance",
                "number",
                default_value=0.025,
                min_value=0.001,
                max_value=0.5,
                section="Quality gates",
            ),
            prop(
                "inlier_mad_multiplier",
                "Inlier MAD multiplier",
                "number",
                default_value=3.5,
                min_value=1,
                max_value=10,
                section="Quality gates",
            ),
            prop(
                "maximum_abs_delta_rotation_deg",
                "Maximum rotation step [deg]",
                "number",
                default_value=60.0,
                min_value=1,
                max_value=179,
                section="Quality gates",
            ),
            prop(
                "minimum_rotation_confidence",
                "Minimum rotation confidence",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
                section="Quality gates",
            ),
            prop(
                "debug_overlay",
                "Show surface rotation overlay",
                "boolean",
                default_value=True,
                section="Diagnostics",
            ),
        ],
        handler=("cv_runtime.nodes.stem_temporal:analysis_stem_surface_rotation"),
        constraints={
            "stateful": True,
            "calibrated_relative_angle_only": True,
            "single_active_track_required": True,
            "requires_undistorted_frame": True,
            "projected_surface_motion_only": True,
        },
    ),
    _node(
        node_type="analysis.stem_surface_phase",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem surface phase",
        description=(
            "Integrate translation-compensated bark-feature motion as raw roll phase, "
            "re-lock to distinctive track-local keyframes after impact, and expose degrees "
            "only when a bound commissioning calibration is valid."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[
            port("frame", "frame"),
            port("segments", "segments"),
            port("stem_geometry", "stem_geometry"),
            port("events", "events", required=False),
        ],
        output_ports=[
            port("surface_phase", "stem_surface_phase"),
            port("relock_events", "events", required=False),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "surface_phase_calibration": None,
            "phase_estimator_version": "LK_KEYFRAME_RELOCK_V1",
            "execution_mode": "SYNCHRONOUS",
            "target_phase_fps": 25.0,
            "tracking_max_width_px": 960,
            "phase_frame_queue_size": 3,
            "phase_replay_window_ms": 500,
            "phase_replay_max_frames": 16,
            "maximum_seed_age_ms": 300,
            "maximum_features": 240,
            "feature_quality_level": 0.015,
            "feature_minimum_distance_px": 6.0,
            "feature_block_size": 7,
            "lk_window_size": 21,
            "lk_max_level": 3,
            "maximum_forward_backward_error_px": 1.5,
            "minimum_blur_score": 18.0,
            "minimum_feature_span_ratio": 0.35,
            "mask_erosion_fraction": 0.15,
            "maximum_frame_gap": 3,
            "maximum_time_gap_seconds": 2.0,
            "minimum_matched_features": 12,
            "minimum_inlier_ratio": 0.70,
            "maximum_tracking_error_px": 12.0,
            "maximum_longitudinal_shift_fraction": 0.18,
            "absolute_inlier_tolerance_normalized": 0.025,
            "inlier_mad_multiplier": 3.5,
            "maximum_abs_delta_raw_phase": 0.70,
            "maximum_abs_delta_phase_deg": 60.0,
            "minimum_rotation_confidence": 0.45,
            "require_active_stem_motion_for_phase": False,
            "minimum_phase_motion_mm_per_frame": 3.0,
            "maximum_keyframes": 24,
            "keyframe_minimum_features": 15,
            "keyframe_minimum_raw_separation": 0.15,
            "relock_minimum_score": 0.60,
            "relock_uniqueness_margin": 0.15,
            "relock_maximum_raw_jump": 0.70,
            "relock_maximum_phase_jump_deg": 90.0,
            "debug_overlay": True,
        },
        property_schema=[
            prop(
                "phase_estimator_version",
                "Phase estimator",
                "select",
                default_value="LK_KEYFRAME_RELOCK_V1",
                options=[
                    ("CYLINDRICAL_SURFACE_V2", "Cylindrical surface V2"),
                    ("LK_KEYFRAME_RELOCK_V1", "Legacy LK V1"),
                ],
                help_text=(
                    "V2 separates transverse stem motion from cylindrical bark rotation. "
                    "A calibration is valid only for the estimator that produced its raw phase."
                ),
                section="Execution",
            ),
            prop(
                "execution_mode",
                "Execution mode",
                "select",
                default_value="SYNCHRONOUS",
                options=[
                    ("HIGH_RATE_SEEDED", "High-rate seeded"),
                    ("SYNCHRONOUS", "Pipeline frame rate"),
                ],
                help_text=(
                    "High-rate seeded tracks only the active stem supplied by segmentation "
                    "while processing camera frames independently at the camera cadence."
                ),
                section="Execution",
            ),
            prop(
                "target_phase_fps",
                "Target phase rate [fps]",
                "number",
                default_value=25.0,
                min_value=1,
                max_value=120,
                step_value=1,
                section="High-rate performance",
            ),
            prop(
                "tracking_max_width_px",
                "Tracking width limit [px]",
                "integer",
                default_value=960,
                min_value=160,
                max_value=4096,
                section="High-rate performance",
                help_text="Only the phase working copy is scaled; model and label pixels stay unchanged.",
            ),
            prop(
                "phase_frame_queue_size",
                "Fast-path queue frames",
                "integer",
                default_value=3,
                min_value=1,
                max_value=16,
                section="High-rate performance",
            ),
            prop(
                "phase_replay_window_ms",
                "Seed replay window [ms]",
                "integer",
                default_value=500,
                min_value=50,
                max_value=3000,
                section="High-rate performance",
            ),
            prop(
                "maximum_seed_age_ms",
                "Maximum segmentation seed age [ms]",
                "integer",
                default_value=300,
                min_value=50,
                max_value=3000,
                section="High-rate performance",
            ),
            prop(
                "surface_phase_calibration",
                "Surface phase calibration",
                "stem_surface_phase_calibration",
                help_text=(
                    "Versioned raw-phase to known-angle commissioning profile. Without "
                    "a valid bound profile the node deliberately emits raw phase only."
                ),
            ),
            prop(
                "maximum_features",
                "Maximum tracked features",
                "integer",
                default_value=240,
                min_value=16,
                max_value=1000,
            ),
            prop(
                "minimum_blur_score",
                "Minimum masked sharpness",
                "number",
                default_value=18.0,
                min_value=0,
                max_value=100000,
                step_value=1,
                section="Tracking quality",
                help_text=(
                    "Laplacian variance inside the eroded active-stem mask. Blurred frames "
                    "hold the last safe phase and never become a new tracking baseline."
                ),
            ),
            prop(
                "maximum_forward_backward_error_px",
                "Maximum flow roundtrip error [px]",
                "number",
                default_value=1.5,
                min_value=0.1,
                max_value=20,
                step_value=0.1,
                section="Tracking quality",
            ),
            prop(
                "minimum_feature_span_ratio",
                "Minimum transverse feature coverage",
                "number",
                default_value=0.35,
                min_value=0.05,
                max_value=1,
                step_value=0.01,
                section="Tracking quality",
            ),
            prop(
                "mask_erosion_fraction",
                "Stem-mask erosion fraction",
                "number",
                default_value=0.18,
                min_value=0.02,
                max_value=0.45,
                step_value=0.01,
            ),
            prop(
                "maximum_frame_gap",
                "Maximum frame gap",
                "integer",
                default_value=3,
                min_value=1,
                max_value=30,
            ),
            prop(
                "minimum_matched_features",
                "Minimum matched features",
                "integer",
                default_value=12,
                min_value=3,
                max_value=500,
            ),
            prop(
                "minimum_inlier_ratio",
                "Minimum inlier ratio",
                "number",
                default_value=0.70,
                min_value=0,
                max_value=1,
                step_value=0.01,
            ),
            prop(
                "maximum_abs_delta_raw_phase",
                "Maximum raw phase step",
                "number",
                default_value=0.70,
                min_value=0.01,
                max_value=5,
                step_value=0.01,
            ),
            prop(
                "maximum_abs_delta_phase_deg",
                "Maximum calibrated phase step [deg]",
                "number",
                default_value=60.0,
                min_value=1,
                max_value=179,
            ),
            prop(
                "require_active_stem_motion_for_phase",
                "Require active-stem motion for phase",
                "boolean",
                default_value=True,
                help_text=(
                    "Hold cumulative phase while the Active Stem Tracker reports a stationary "
                    "mask. This prevents optical-flow noise from inventing rotation before "
                    "the transverse throw starts."
                ),
            ),
            prop(
                "minimum_phase_motion_mm_per_frame",
                "Minimum motion for phase [mm/frame]",
                "number",
                default_value=3.0,
                min_value=0,
                max_value=500,
                step_value=0.5,
                help_text=(
                    "Minimum calibrated X/Y mask-centroid speed that opens phase integration. "
                    "Increase it when a resting stem still accumulates phase; reduce it only "
                    "when genuine slow rolling is being held."
                ),
            ),
            prop(
                "maximum_keyframes",
                "Maximum track-local keyframes",
                "integer",
                default_value=24,
                min_value=2,
                max_value=128,
            ),
            prop(
                "relock_minimum_score",
                "Minimum re-lock score",
                "number",
                default_value=0.60,
                min_value=0,
                max_value=1,
                step_value=0.01,
            ),
            prop(
                "relock_uniqueness_margin",
                "Re-lock uniqueness margin",
                "number",
                default_value=0.15,
                min_value=0,
                max_value=1,
                step_value=0.01,
            ),
            prop("debug_overlay", "Show surface phase overlay", "boolean", default_value=True),
        ],
        handler="cv_runtime.nodes.stem_surface_phase:analysis_stem_surface_phase",
        constraints={
            "stateful": True,
            "single_active_track_required": True,
            "requires_process_rectified_frame": True,
            "supports_perspective_preserved_model_view": True,
            "requires_stem_curvature_process_calibration": True,
            "raw_phase_without_angle_calibration": True,
            "calibrated_degrees_fail_closed": True,
            "keyframes_track_local_only": True,
            "high_rate_seeded_tracking": True,
            "high_rate_never_selects_tracks": True,
            "projected_surface_motion_only": True,
        },
    ),
    _node(
        node_type="analysis.stem_slice_model",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem slice model",
        description=(
            "Fit material-fixed y-z ellipse slices from the existing calibrated horizontal "
            "curvature profile and calibrated surface phase during natural transfer rolling. "
            "Signed tracked x drift is registered onto a material-fixed grid; acquisition "
            "freezes before longitudinal conveyor transport."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[
            port("curvature", "stem_horizontal_curvature"),
            port("surface_phase", "stem_surface_phase"),
            port("active_stem", "segments"),
            port("events", "events", required=False),
        ],
        output_ports=[port("slice_model", "stem_slice_model")],
        default_config={
            "slice_spacing_mm": 50.0,
            "minimum_observations_per_slice": 7,
            "minimum_unique_phases": 6,
            "minimum_phase_span_deg": 90.0,
            "phase_separation_deg": 8.0,
            "minimum_observation_quality": 0.1,
            "maximum_fit_condition_number": 100000.0,
            "maximum_center_residual_rms_mm": 5.0,
            "maximum_width_residual_rms_mm": 5.0,
            "minimum_slice_quality": 0.25,
            "minimum_valid_slice_count": 3,
            "minimum_valid_slice_ratio": 0.6,
            "minimum_model_quality": 0.25,
            "degraded_model_quality": 0.6,
            "maximum_interpolation_gap_mm": 100.0,
            "smoothing_window_slices": 3,
            "smoothing_strength": 0.25,
            "minimum_geometry_confidence": 0.4,
            "minimum_phase_confidence": 0.45,
            "minimum_tracking_confidence": 0.35,
            "maximum_frame_sequence_gap": 2,
            "maximum_material_x_step_mm": 75.0,
            "maximum_material_x_excursion_mm": 150.0,
            "maximum_observation_frames": 180,
            "include_raw_observations": False,
        },
        property_schema=[
            prop(
                "slice_spacing_mm",
                "Slice spacing [mm]",
                "number",
                default_value=50,
                min_value=5,
                max_value=500,
                step_value=5,
                section="Measurement grid",
            ),
            prop(
                "minimum_observations_per_slice",
                "Minimum observations per slice",
                "integer",
                default_value=7,
                min_value=3,
                max_value=180,
                section="Fit support",
            ),
            prop(
                "minimum_unique_phases",
                "Minimum separated phases",
                "integer",
                default_value=6,
                min_value=3,
                max_value=180,
                section="Fit support",
            ),
            prop(
                "minimum_phase_span_deg",
                "Minimum phase span [deg]",
                "number",
                default_value=90,
                min_value=1,
                max_value=360,
                step_value=1,
                section="Fit support",
            ),
            prop(
                "maximum_center_residual_rms_mm",
                "Maximum center fit RMS [mm]",
                "number",
                default_value=5,
                min_value=0.01,
                max_value=100,
                step_value=0.1,
                section="Fit quality",
            ),
            prop(
                "maximum_width_residual_rms_mm",
                "Maximum width fit RMS [mm]",
                "number",
                default_value=5,
                min_value=0.01,
                max_value=100,
                step_value=0.1,
                section="Fit quality",
            ),
            prop(
                "minimum_valid_slice_ratio",
                "Minimum valid slice ratio",
                "number",
                default_value=0.6,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Model quality",
            ),
            prop(
                "maximum_interpolation_gap_mm",
                "Maximum interpolation gap [mm]",
                "number",
                default_value=100,
                min_value=0,
                max_value=1000,
                step_value=10,
                section="Preprocessing",
            ),
            prop(
                "maximum_material_x_step_mm",
                "Maximum registered x step [mm]",
                "number",
                default_value=75,
                min_value=0,
                max_value=300,
                step_value=1,
                section="Material registration",
            ),
            prop(
                "maximum_material_x_excursion_mm",
                "Maximum transfer x excursion [mm]",
                "number",
                default_value=150,
                min_value=0,
                max_value=500,
                step_value=5,
                section="Material registration",
            ),
            prop(
                "include_raw_observations",
                "Include raw fit observations",
                "boolean",
                default_value=False,
                section="Diagnostics",
                help_text=(
                    "Keep disabled during normal realtime operation; the bounded raw values "
                    "are intended for commissioning replay only."
                ),
            ),
        ],
        handler="cv_runtime.nodes.stem_slice_model:analysis_stem_slice_model",
        constraints={
            "stateful": True,
            "single_active_track_required": True,
            "requires_calibrated_surface_phase": True,
            "requires_process_rectified_metric_profile": True,
            "measurement_phase": "NATURAL_ROLL_TRANSFER_ONLY",
            "material_registration_mode": "TRACKED_SIGNED_MATERIAL_X_V1",
            "calibrated_pose_adapter_available": True,
            "no_parallel_segmentation_or_phase_tracker": True,
        },
    ),
    _node(
        node_type="analysis.stem_rotation_priority_adapter",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem rotation priority",
        description=(
            "Resolve the one active geometric priority from the revisioned channel control. "
            "Frontend mode is available now; PLC selection remains fail-closed until a real "
            "PLC read adapter is connected."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[port("frame", "frame")],
        output_ports=[port("priority", "stem_rotation_priority")],
        default_config={"priority_source": "FRONTEND"},
        property_schema=[
            prop(
                "priority_source",
                "Default priority source",
                "select",
                default_value="FRONTEND",
                options=[
                    ("FRONTEND", "Frontend (test/commissioning)"),
                    ("PLC", "PLC (adapter not connected)"),
                ],
                help_text=(
                    "Used only when no channel control snapshot exists. The channel control "
                    "always identifies exactly one active source."
                ),
            )
        ],
        handler=(
            "cv_runtime.nodes.stem_rotation_priority:"
            "analysis_stem_rotation_priority_adapter"
        ),
        constraints={
            "plc_read_adapter_available": False,
            "plc_writes_allowed": False,
            "source_switch_fail_closed": True,
        },
    ),
    _node(
        node_type="analysis.stem_rotation_optimizer",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem rotation optimizer",
        description=(
            "Optimize the symmetric available y/z geometry of a frozen ellipse-slice model. "
            "The result is a shadow recommendation and never performs a PLC write."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[
            port("slice_model", "stem_slice_model"),
            port("priority", "stem_rotation_priority"),
        ],
        output_ports=[port("result", "result")],
        default_config={
            "slice_spacing_mm": 50.0,
            "x_eval_start_mm": None,
            "x_eval_end_mm": None,
            "smoothing_window_slices": 3,
            "coarse_angle_step_deg": 1.0,
            "fine_angle_step_deg": 0.1,
            "fine_search_half_width_deg": 2.0,
            "score_tolerance_mm": 1.0,
            "min_gain_mm": 3.0,
            "min_rotation_deg": 1.0,
            "min_valid_slice_ratio": 0.6,
            "min_valid_slice_count": 3,
            "min_model_quality": 0.25,
            "max_interpolation_gap_mm": 100.0,
            "rotation_negative_limit_deg": -90.0,
            "rotation_positive_limit_deg": 90.0,
        },
        property_schema=[
            prop(
                "x_eval_start_mm",
                "Evaluation start x [mm]",
                "number",
                default_value=None,
                section="Evaluation range",
            ),
            prop(
                "x_eval_end_mm",
                "Evaluation end x [mm]",
                "number",
                default_value=None,
                section="Evaluation range",
            ),
            prop(
                "coarse_angle_step_deg",
                "Coarse angle step [deg]",
                "number",
                default_value=1,
                min_value=0.1,
                max_value=180,
                step_value=0.1,
                section="Angle search",
            ),
            prop(
                "fine_angle_step_deg",
                "Fine angle step [deg]",
                "number",
                default_value=0.1,
                min_value=0.01,
                max_value=10,
                step_value=0.01,
                section="Angle search",
            ),
            prop(
                "fine_search_half_width_deg",
                "Fine search half-width [deg]",
                "number",
                default_value=2,
                min_value=0,
                max_value=90,
                step_value=0.1,
                section="Angle search",
            ),
            prop(
                "score_tolerance_mm",
                "Equal-score tolerance [mm]",
                "number",
                default_value=1,
                min_value=0,
                max_value=100,
                step_value=0.1,
                section="Decision",
            ),
            prop(
                "min_gain_mm",
                "Minimum geometry gain [mm]",
                "number",
                default_value=3,
                min_value=0,
                max_value=500,
                step_value=0.1,
                section="Decision",
            ),
            prop(
                "min_rotation_deg",
                "Minimum correction angle [deg]",
                "number",
                default_value=1,
                min_value=0,
                max_value=90,
                step_value=0.1,
                section="Decision",
            ),
            prop(
                "min_valid_slice_ratio",
                "Minimum valid slice ratio",
                "number",
                default_value=0.6,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Quality gates",
            ),
            prop(
                "min_model_quality",
                "Minimum model quality",
                "number",
                default_value=0.25,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Quality gates",
            ),
            prop(
                "rotation_negative_limit_deg",
                "Negative rotation limit [deg]",
                "number",
                default_value=-90,
                min_value=-180,
                max_value=0,
                step_value=0.1,
                section="Mechanical limits",
            ),
            prop(
                "rotation_positive_limit_deg",
                "Positive rotation limit [deg]",
                "number",
                default_value=90,
                min_value=0,
                max_value=180,
                step_value=0.1,
                section="Mechanical limits",
            ),
        ],
        handler=(
            "cv_runtime.nodes.stem_rotation_optimizer:analysis_stem_rotation_optimizer"
        ),
        constraints={
            "stateful": False,
            "frozen_slice_model_required": True,
            "positive_rotation_axis": "machine_x",
            "plc_writes_allowed": False,
            "product_geometry_forbidden": True,
        },
    ),
    _node(
        node_type="analysis.stem_curvature_phase_solver",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem curvature phase solver",
        description=(
            "Robustly fit signed bow and curvature energy over calibrated cumulative "
            "surface phase, freeze geometry observations at the curvature gate, and "
            "publish a unique 180-degree minimum only when support and fit are sufficient."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[
            port("curvature", "stem_horizontal_curvature"),
            port("surface_phase", "stem_surface_phase"),
            port("active_stem", "segments", required=False),
            port("events", "events", required=False),
        ],
        output_ports=[
            port("solution", "stem_curvature_phase_solution"),
            port("overlay", "overlay"),
        ],
        default_config={
            "minimum_observations": 7,
            "minimum_phase_span_deg": 90.0,
            "minimum_geometry_confidence": 0.4,
            "minimum_phase_confidence": 0.4,
            "minimum_common_coverage": 0.4,
            "maximum_observations": 240,
            "robust_huber_delta": 1.5,
            "robust_iterations": 8,
            "maximum_fit_condition": 100000000.0,
            "maximum_signed_residual_ratio": 0.45,
            "maximum_energy_residual_ratio": 0.4,
            "minimum_curvature_span_mm": 1.5,
            "bow_relevance_threshold_mm": 3.0,
            "target_observation_tolerance_deg": 12.0,
            "maximum_target_extrapolation_deg": 30.0,
            "s_bow_residual_minimum_mm": 2.0,
            "s_bow_residual_ratio": 0.3,
            "minimum_active_stem_confidence": 0.55,
            "minimum_decision_phase_confidence": 0.65,
            "minimum_secure_tracking_confidence": 0.8,
            "minimum_phase_inlier_ratio": 0.55,
            "settled_phase_window_frames": 3,
            "settled_phase_span_deg": 2.0,
            "no_rotation_tolerance_deg": 2.0,
            "minimum_overall_confidence": 0.55,
            "result_expiry_seconds": 5.0,
            "first_roller_distance_from_decision_gate_mm": 0.0,
            "conveyor_speed_mm_per_second": 0.0,
            "record_session_artifacts": False,
            "record_rectified_video": True,
            "record_overlay_video": True,
            "session_recording_fps": 15.0,
            "session_recording_maximum_frames": 900,
            "session_recording_retention_days": 30,
            "minimum_principle_validation": None,
            "shadow_mode_enabled": False,
            "shadow_evaluation_approved": False,
            "shadow_drift_window_sessions": 50,
            "shadow_confidence_drop_threshold": 0.15,
            "shadow_uncertain_rate_increase_threshold": 0.15,
        },
        property_schema=[
            prop(
                "minimum_observations",
                "Minimum observations",
                "integer",
                default_value=7,
                min_value=3,
                max_value=100,
                section="Fit support",
            ),
            prop(
                "minimum_phase_span_deg",
                "Minimum observed phase span [deg]",
                "number",
                default_value=90,
                min_value=1,
                max_value=180,
                section="Fit support",
            ),
            prop(
                "minimum_geometry_confidence",
                "Minimum geometry confidence",
                "number",
                default_value=0.4,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Observation quality",
            ),
            prop(
                "minimum_phase_confidence",
                "Minimum phase confidence",
                "number",
                default_value=0.4,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Observation quality",
            ),
            prop(
                "minimum_common_coverage",
                "Minimum common coverage",
                "number",
                default_value=0.4,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Observation quality",
            ),
            prop(
                "maximum_observations",
                "Maximum track observations",
                "integer",
                default_value=240,
                min_value=7,
                max_value=2000,
                section="Fit support",
            ),
            prop(
                "robust_huber_delta",
                "Huber delta",
                "number",
                default_value=1.5,
                min_value=0.1,
                max_value=10,
                step_value=0.1,
                section="Robust fit",
            ),
            prop(
                "maximum_signed_residual_ratio",
                "Maximum signed fit residual ratio",
                "number",
                default_value=0.45,
                min_value=0,
                max_value=2,
                step_value=0.01,
                section="Fit validation",
            ),
            prop(
                "maximum_energy_residual_ratio",
                "Maximum energy fit residual ratio",
                "number",
                default_value=0.4,
                min_value=0,
                max_value=2,
                step_value=0.01,
                section="Fit validation",
            ),
            prop(
                "minimum_curvature_span_mm",
                "Minimum curvature span [mm]",
                "number",
                default_value=1.5,
                min_value=0,
                section="Decision validity",
            ),
            prop(
                "bow_relevance_threshold_mm",
                "Bow relevance threshold [mm]",
                "number",
                default_value=3,
                min_value=0,
                section="Decision validity",
            ),
            prop(
                "target_observation_tolerance_deg",
                "Observed target tolerance [deg]",
                "number",
                default_value=12,
                min_value=0,
                max_value=90,
                section="Extrapolation",
            ),
            prop(
                "maximum_target_extrapolation_deg",
                "Maximum target extrapolation [deg]",
                "number",
                default_value=30,
                min_value=0,
                max_value=90,
                section="Extrapolation",
            ),
            prop(
                "s_bow_residual_minimum_mm",
                "S-bow residual minimum [mm]",
                "number",
                default_value=2,
                min_value=0,
                section="Classification",
            ),
            prop(
                "minimum_active_stem_confidence",
                "Minimum active stem confidence",
                "number",
                default_value=0.55,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Session decision",
            ),
            prop(
                "minimum_decision_phase_confidence",
                "Minimum decision phase confidence",
                "number",
                default_value=0.65,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Session decision",
            ),
            prop(
                "minimum_secure_tracking_confidence",
                "Secure tracking confidence",
                "number",
                default_value=0.8,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Session decision",
            ),
            prop(
                "minimum_phase_inlier_ratio",
                "Minimum phase inlier ratio",
                "number",
                default_value=0.55,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Session decision",
            ),
            prop(
                "settled_phase_window_frames",
                "Settled phase window [frames]",
                "integer",
                default_value=3,
                min_value=1,
                max_value=120,
                section="Session decision",
            ),
            prop(
                "settled_phase_span_deg",
                "Settled phase span [deg]",
                "number",
                default_value=2,
                min_value=0,
                max_value=90,
                section="Session decision",
            ),
            prop(
                "no_rotation_tolerance_deg",
                "No rotation tolerance [deg]",
                "number",
                default_value=2,
                min_value=0,
                max_value=90,
                section="Session decision",
            ),
            prop(
                "minimum_overall_confidence",
                "Minimum result confidence",
                "number",
                default_value=0.55,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Session decision",
            ),
            prop(
                "result_expiry_seconds",
                "Result expiry [s]",
                "number",
                default_value=5,
                min_value=0,
                section="Deadline",
            ),
            prop(
                "first_roller_distance_from_decision_gate_mm",
                "Decision gate to first roller [mm]",
                "number",
                default_value=0,
                min_value=0,
                section="Deadline",
            ),
            prop(
                "conveyor_speed_mm_per_second",
                "Conveyor speed [mm/s]",
                "number",
                default_value=0,
                min_value=0,
                section="Deadline",
            ),
            prop(
                "record_session_artifacts",
                "Record diagnostic session artifacts",
                "boolean",
                default_value=False,
                section="Diagnostics recording",
            ),
            prop(
                "record_rectified_video",
                "Record rectified video",
                "boolean",
                default_value=True,
                section="Diagnostics recording",
            ),
            prop(
                "record_overlay_video",
                "Record overlay video",
                "boolean",
                default_value=True,
                section="Diagnostics recording",
            ),
            prop(
                "session_recording_fps",
                "Recording FPS",
                "number",
                default_value=15,
                min_value=1,
                max_value=120,
                section="Diagnostics recording",
            ),
            prop(
                "session_recording_maximum_frames",
                "Maximum recorded frames",
                "integer",
                default_value=900,
                min_value=2,
                max_value=100000,
                section="Diagnostics recording",
            ),
            prop(
                "session_recording_retention_days",
                "Recording retention [days]",
                "integer",
                default_value=30,
                min_value=1,
                max_value=3650,
                section="Diagnostics recording",
            ),
            prop(
                "minimum_principle_validation",
                "Objective minimum-principle validation",
                "stem_minimum_principle_validation",
                required=False,
                section="Objective validation",
                help_text=(
                    "Versioned reference-cylinder, curved-body and marked-stem acceptance "
                    "artifact. Reports remain objective and never select a target frame."
                ),
            ),
            prop(
                "shadow_mode_enabled",
                "Enable production shadow mode",
                "boolean",
                default_value=False,
                section="Shadow evaluation",
                help_text=(
                    "Retain one compact decision result per stem for this immutable "
                    "pipeline version and suppress all Siemens PLC writes at runtime."
                ),
            ),
            prop(
                "shadow_evaluation_approved",
                "Shadow evaluation approved for PLC dry run",
                "boolean",
                default_value=False,
                section="Shadow evaluation",
                help_text=(
                    "Explicit technical approval recorded in this immutable pipeline "
                    "version. It does not enable or authorize PLC writes."
                ),
            ),
            prop(
                "shadow_drift_window_sessions",
                "Drift comparison window [sessions]",
                "integer",
                default_value=50,
                min_value=10,
                max_value=1000,
                section="Shadow evaluation",
            ),
            prop(
                "shadow_confidence_drop_threshold",
                "Confidence drop warning threshold",
                "number",
                default_value=0.15,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Shadow evaluation",
            ),
            prop(
                "shadow_uncertain_rate_increase_threshold",
                "Uncertain-rate increase warning threshold",
                "number",
                default_value=0.15,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Shadow evaluation",
            ),
        ],
        handler=(
            "cv_runtime.nodes.stem_curvature_phase_solver:"
            "analysis_stem_curvature_phase_solver"
        ),
        constraints={
            "stateful": True,
            "single_active_track_required": True,
            "requires_calibrated_surface_phase": True,
            "curvature_observations_stop_at_freeze_gate": True,
            "phase_updates_continue_after_freeze_gate": True,
            "periodicity_deg": 180,
            "correction_angle_output": True,
            "result_contract": "stem.orientation.v3",
            "shadow_mode_forces_plc_write_suppression": True,
            "legacy_solver_without_active_stem_supported": True,
        },
    ),
    _node(
        node_type="analysis.stem_temporal_orientation",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem temporal orientation",
        description=(
            "Freeze a fail-closed projected pre-entry rotation recommendation from "
            "stem geometry over calibrated cumulative surface rotation."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[
            port("stem_geometry", "stem_geometry"),
            port("stem_rotation", "stem_rotation"),
            port("active_stem", "segments", required=False),
            port("events", "events", required=False),
        ],
        output_ports=[
            port("result", "result"),
            port("sequence", "temporal_sequence", required=False),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "observation_entry_roi": [],
            "throw_in_roi": [],
            "decision_roi": [],
            "first_roller_roi": [],
            "process_zone_calibration_id": "",
            "process_zone_calibration_version": "",
            "process_zone_intrinsic_hash": "",
            "rotation_viewpoint_convention": "viewed_from_configured_log_end_a",
            "positive_rotation_direction": ("clockwise_viewed_from_configured_log_end"),
            "maximum_observations": 128,
            "minimum_observation_count": 4,
            "minimum_observed_rotation_deg": 20.0,
            "minimum_rotation_confidence": 0.45,
            "minimum_geometry_quality": 0.5,
            "minimum_visible_fraction": 0.3,
            "minimum_bow_relevance": 0.12,
            "minimum_target_improvement": 0.04,
            "target_ambiguity_margin": 0.08,
            "target_merge_distance_deg": 5.0,
            "minimum_result_confidence": 0.5,
            "deadband_deg": 5.0,
            "record_temporal_samples": True,
            "temporal_sample_fps": 15.0,
            "record_diagnostic_video": True,
            "verification_frame_count": 0,
            "temporal_sample_maximum_frames": 900,
            "minimum_recording_geometry_quality": 0.5,
            "minimum_recording_visible_fraction": 0.3,
            "minimum_recording_rotation_confidence": 0.0,
            "temporal_sample_deduplication": "sequence_id",
            "temporal_sample_retention_days": 30,
            "temporal_sample_split": "train",
            "temporal_annotation_schema_id": "stem_orientation_angle.v1",
            "temporal_dataset_id": "",
            "temporal_auto_import_enabled": True,
            "temporal_import_sample_every_n": 1,
            "temporal_import_maximum_per_hour": 120,
            "debug_overlay": True,
        },
        property_schema=[
            prop(
                "observation_entry_roi",
                "Observation entry ROI",
                "roi",
                required=True,
                default_value=[],
                section="Process zones",
            ),
            prop(
                "throw_in_roi",
                "Throw-in ROI",
                "roi",
                required=True,
                default_value=[],
                section="Process zones",
            ),
            prop(
                "decision_roi",
                "Decision deadline ROI",
                "roi",
                required=True,
                default_value=[],
                section="Process zones",
            ),
            prop(
                "first_roller_roi",
                "First roller diagnostic ROI",
                "roi",
                required=True,
                default_value=[],
                section="Process zones",
            ),
            prop(
                "process_zone_calibration_id",
                "Process-zone calibration ID",
                "text",
                required=True,
                default_value="",
                section="Process zones",
            ),
            prop(
                "process_zone_calibration_version",
                "Process-zone calibration version",
                "text",
                required=True,
                default_value="",
                section="Process zones",
            ),
            prop(
                "process_zone_intrinsic_hash",
                "Process-zone intrinsic hash",
                "text",
                required=True,
                default_value="",
                section="Process zones",
            ),
            prop(
                "rotation_viewpoint_convention",
                "Rotation viewpoint",
                "select",
                required=True,
                default_value="viewed_from_configured_log_end_a",
                options=[
                    ("viewed_from_configured_log_end_a", "Viewed from configured log end A"),
                    ("viewed_from_configured_log_end_b", "Viewed from configured log end B"),
                ],
                section="Direction convention",
            ),
            prop(
                "positive_rotation_direction",
                "Positive rotation direction",
                "select",
                required=True,
                default_value="clockwise_viewed_from_configured_log_end",
                options=[
                    (
                        "clockwise_viewed_from_configured_log_end",
                        "Clockwise from configured log end",
                    ),
                    (
                        "counter_clockwise_viewed_from_configured_log_end",
                        "Counter-clockwise from configured log end",
                    ),
                ],
                section="Direction convention",
            ),
            prop(
                "maximum_observations",
                "Maximum buffered observations",
                "integer",
                default_value=128,
                min_value=8,
                max_value=4096,
                section="Temporal baseline",
            ),
            prop(
                "minimum_observation_count",
                "Minimum observations",
                "integer",
                default_value=4,
                min_value=3,
                max_value=64,
                section="Temporal baseline",
            ),
            prop(
                "minimum_observed_rotation_deg",
                "Minimum observed rotation [deg]",
                "number",
                default_value=20.0,
                min_value=1,
                max_value=179,
                section="Temporal baseline",
            ),
            prop(
                "minimum_rotation_confidence",
                "Minimum rotation confidence",
                "number",
                default_value=0.45,
                min_value=0,
                max_value=1,
                section="Quality gates",
            ),
            prop(
                "minimum_geometry_quality",
                "Minimum geometry quality",
                "number",
                default_value=0.5,
                min_value=0,
                max_value=1,
                section="Quality gates",
            ),
            prop(
                "minimum_visible_fraction",
                "Minimum visible stem fraction",
                "number",
                default_value=0.3,
                min_value=0,
                max_value=1,
                section="Quality gates",
            ),
            prop(
                "minimum_bow_relevance",
                "Minimum projected bow relevance",
                "number",
                default_value=0.12,
                min_value=0,
                section="Temporal baseline",
            ),
            prop(
                "minimum_target_improvement",
                "Minimum target improvement",
                "number",
                default_value=0.04,
                min_value=0,
                section="Temporal baseline",
            ),
            prop(
                "target_ambiguity_margin",
                "Target ambiguity margin",
                "number",
                default_value=0.08,
                min_value=0,
                max_value=1,
                section="Temporal baseline",
            ),
            prop(
                "target_merge_distance_deg",
                "Target merge distance [deg]",
                "number",
                default_value=5.0,
                min_value=0,
                max_value=45,
                section="Temporal baseline",
            ),
            prop(
                "minimum_result_confidence",
                "Minimum result confidence",
                "number",
                default_value=0.5,
                min_value=0,
                max_value=1,
                section="Quality gates",
            ),
            prop(
                "deadband_deg",
                "No-rotation deadband [deg]",
                "number",
                default_value=5.0,
                min_value=0,
                max_value=45,
                section="Temporal baseline",
            ),
            prop(
                "record_temporal_samples",
                "Record temporal samples",
                "boolean",
                default_value=True,
                section="Temporal recording",
            ),
            prop(
                "temporal_sample_fps",
                "Temporal sample FPS",
                "number",
                default_value=15.0,
                min_value=1,
                max_value=120,
                section="Temporal recording",
            ),
            prop(
                "record_diagnostic_video",
                "Record diagnostic video",
                "boolean",
                default_value=True,
                section="Temporal recording",
            ),
            prop(
                "verification_frame_count",
                "Verification frames",
                "integer",
                default_value=0,
                min_value=0,
                max_value=600,
                section="Temporal recording",
            ),
            prop(
                "temporal_sample_maximum_frames",
                "Maximum primary frames",
                "integer",
                default_value=900,
                min_value=2,
                max_value=100000,
                section="Temporal recording",
            ),
            prop(
                "minimum_recording_geometry_quality",
                "Minimum recording geometry quality",
                "number",
                default_value=0.5,
                min_value=0,
                max_value=1,
                section="Temporal recording",
            ),
            prop(
                "minimum_recording_visible_fraction",
                "Minimum recording visible fraction",
                "number",
                default_value=0.3,
                min_value=0,
                max_value=1,
                section="Temporal recording",
            ),
            prop(
                "minimum_recording_rotation_confidence",
                "Minimum recording rotation confidence",
                "number",
                default_value=0.0,
                min_value=0,
                max_value=1,
                section="Temporal recording",
            ),
            prop(
                "temporal_sample_deduplication",
                "Temporal sample deduplication",
                "select",
                default_value="sequence_id",
                options=[
                    ("sequence_id", "Suppress duplicate sequence IDs"),
                    ("keep", "Keep every recording"),
                ],
                section="Temporal recording",
            ),
            prop(
                "temporal_sample_retention_days",
                "Temporal sample retention days",
                "integer",
                default_value=30,
                min_value=1,
                max_value=3650,
                section="Temporal recording",
            ),
            prop(
                "temporal_sample_split",
                "Initial dataset split",
                "select",
                default_value="train",
                options=[("train", "Train"), ("val", "Validation"), ("test", "Test")],
                section="Temporal recording",
            ),
            prop(
                "temporal_annotation_schema_id",
                "Temporal annotation schema",
                "text",
                default_value="stem_orientation_angle.v1",
                section="Temporal recording",
            ),
            prop(
                "temporal_dataset_id",
                "Temporal dataset ID",
                "text",
                default_value="",
                section="Temporal import",
            ),
            prop(
                "temporal_auto_import_enabled",
                "Automatically import recorded samples",
                "boolean",
                default_value=True,
                section="Temporal import",
            ),
            prop(
                "temporal_import_sample_every_n",
                "Import every Nth sample",
                "integer",
                default_value=1,
                min_value=1,
                max_value=100000,
                section="Temporal import",
            ),
            prop(
                "temporal_import_maximum_per_hour",
                "Maximum imports per hour",
                "integer",
                default_value=120,
                min_value=0,
                max_value=100000,
                section="Temporal import",
            ),
            prop(
                "debug_overlay",
                "Show temporal orientation overlay",
                "boolean",
                default_value=True,
                section="Diagnostics",
            ),
        ],
        handler=("cv_runtime.nodes.stem_temporal:analysis_stem_temporal_orientation"),
        constraints={
            "stateful": True,
            "shadow_mode_only": True,
            "plc_write": False,
            "projected_baseline": True,
            "decision_frozen_before_first_roller": True,
        },
    ),
    _node(
        node_type="analysis.stem_orientation_model_decision",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Stem orientation model decision",
        description=(
            "Apply fail-closed stem-orientation gates to a causal temporal model "
            "prediction while preserving the deterministic pre-entry baseline."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[
            port("baseline", "result"),
            port("prediction", "temporal_prediction"),
        ],
        output_ports=[
            port("result", "result"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "minimum_bow_relevance_probability": 0.6,
            "maximum_bow_relevance_probability_for_no_rotation": 0.35,
            "minimum_target_observed_probability": 0.6,
            "minimum_angle_confidence": 0.5,
            "maximum_baseline_disagreement_deg": 20.0,
            "validity_seconds": 2.0,
        },
        property_schema=[
            prop(
                "minimum_bow_relevance_probability",
                "Minimum bow relevance probability",
                "number",
                default_value=0.6,
                min_value=0,
                max_value=1,
            ),
            prop(
                "maximum_bow_relevance_probability_for_no_rotation",
                "Maximum bow probability for no rotation",
                "number",
                default_value=0.35,
                min_value=0,
                max_value=1,
            ),
            prop(
                "minimum_target_observed_probability",
                "Minimum target-observed probability",
                "number",
                default_value=0.6,
                min_value=0,
                max_value=1,
            ),
            prop(
                "minimum_angle_confidence",
                "Minimum angle confidence",
                "number",
                default_value=0.5,
                min_value=0,
                max_value=1,
            ),
            prop(
                "maximum_baseline_disagreement_deg",
                "Maximum baseline disagreement [deg]",
                "number",
                default_value=20.0,
                min_value=0,
                max_value=90,
            ),
            prop(
                "validity_seconds",
                "Decision validity [s]",
                "number",
                default_value=2.0,
                min_value=0,
                max_value=60,
            ),
        ],
        handler=(
            "cv_runtime.nodes.temporal_inference:analysis_stem_orientation_model_decision"
        ),
        constraints={
            "stateful": True,
            "shadow_mode_only": True,
            "plc_write": False,
            "preserves_deterministic_baseline": True,
            "decision_frozen_before_first_roller": True,
        },
    ),
    _node(
        node_type="analysis.object_counting",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Object counting",
        description="Count objects by class, zone, or total result set.",
        supported_modes=ALL_MODES,
        input_ports=[port("objects", "region_set")],
        output_ports=[port("counts", "measurements")],
        default_config={"group_by": "label", "zones": []},
        property_schema=[
            prop(
                "group_by",
                "Group by",
                "select",
                default_value="label",
                options=[("label", "Label"), ("zone", "Zone"), ("total", "Total")],
            ),
            prop("zones", "Zones", "zone_list", default_value=[]),
        ],
        constraints={"port_aliases": {"region_set": ["detections", "segments"]}},
    ),
    _node(
        node_type="analysis.zone_detection",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Zone detection",
        description=(
            "Determine whether detected, segmented, or CV objects are inside "
            "operator-drawn zones and optionally require class counts per zone."
        ),
        supported_modes=ALL_MODES,
        input_ports=[port("objects", "region_set")],
        output_ports=[
            port("objects", "region_set"),
            port("result", "result"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "zones": [],
            "zone_match": "center_inside",
            "match_policy": "any",
            "min_match_count": 1,
            "rules": [
                {
                    "rule_key": "zone_occupancy",
                    "relation": "zone_occupancy",
                    "enabled": True,
                    "primary_labels": [],
                    "secondary_labels": [],
                    "label_pairing": "any",
                    "max_distance_percent": 10,
                    "min_distance_percent": None,
                    "min_overlap_percent": 1,
                    "min_closing_percent": 1,
                    "zone_key": "",
                    "zone_match": "center_inside",
                    "min_count": 1,
                    "max_count": None,
                    "signal_label": "Zone occupied",
                }
            ],
        },
        property_schema=[
            prop("zones", "Zones", "zone_list", required=True, default_value=[]),
            prop(
                "zone_match",
                "Zone match",
                "select",
                default_value="center_inside",
                options=[
                    ("center_inside", "Center inside"),
                    ("box_overlaps", "Touching / overlaps"),
                    ("fully_inside", "Fully inside"),
                ],
                help_text=(
                    "Default zone test used for zone summaries. Individual "
                    "requirements can override this and can add an overlap percent."
                ),
            ),
            prop(
                "match_policy",
                "Requirement policy",
                "select",
                default_value="any",
                options=[("any", "Any requirement"), ("all", "All requirements")],
                help_text="Use All when several rows must be true together.",
            ),
            prop(
                "min_match_count",
                "Minimum matching requirements",
                "integer",
                default_value=1,
                min_value=1,
                help_text="Minimum number of requirement matches before the node emits true.",
            ),
            prop(
                "rules",
                "Zone requirements",
                "spatial_rule_table",
                default_value=[
                    {
                        "rule_key": "zone_occupancy",
                        "relation": "zone_occupancy",
                        "enabled": True,
                        "primary_labels": [],
                        "secondary_labels": [],
                        "label_pairing": "any",
                        "max_distance_percent": 10,
                        "min_distance_percent": None,
                        "min_overlap_percent": 1,
                        "min_closing_percent": 1,
                        "zone_key": "",
                        "zone_match": "center_inside",
                        "min_count": 1,
                        "max_count": None,
                        "signal_label": "Zone occupied",
                    }
                ],
                help_text=(
                    "Use multiple zone_occupancy rows with policy All to require "
                    "combinations such as one LKW and one Lader in Zone A."
                ),
            ),
        ],
        constraints={"port_aliases": {"region_set": ["detections", "segments"]}},
    ),
    _node(
        node_type="analysis.close_detection",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Close detection",
        description=(
            "Detect objects that are close to each other, overlap, or fully cover "
            "another object, including same-class or different-class pairs."
        ),
        supported_modes=ALL_MODES,
        input_ports=[
            port("objects", "region_set"),
            port("secondary_objects", "region_set", required=False),
        ],
        output_ports=[
            port("result", "result"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "match_policy": "any",
            "min_match_count": 1,
            "rules": [
                {
                    "rule_key": "overlapping_objects",
                    "relation": "overlap",
                    "enabled": True,
                    "primary_labels": [],
                    "secondary_labels": [],
                    "label_pairing": "any",
                    "max_distance_percent": 10,
                    "min_distance_percent": None,
                    "min_overlap_percent": 1,
                    "min_closing_percent": 1,
                    "zone_key": "",
                    "zone_match": "center_inside",
                    "min_count": 1,
                    "max_count": None,
                    "signal_label": "Objects overlap",
                }
            ],
        },
        property_schema=[
            prop(
                "match_policy",
                "Match policy",
                "select",
                default_value="any",
                options=[("any", "Any rule"), ("all", "All rules")],
                help_text="Use All when every close rule must be true.",
            ),
            prop(
                "min_match_count",
                "Minimum matching pairs",
                "integer",
                default_value=1,
                min_value=1,
                help_text="Minimum number of object pairs before the node emits true.",
            ),
            prop(
                "rules",
                "Close rules",
                "spatial_rule_table",
                required=True,
                default_value=[
                    {
                        "rule_key": "overlapping_objects",
                        "relation": "overlap",
                        "enabled": True,
                        "primary_labels": [],
                        "secondary_labels": [],
                        "label_pairing": "any",
                        "max_distance_percent": 10,
                        "min_distance_percent": None,
                        "min_overlap_percent": 1,
                        "min_closing_percent": 1,
                        "zone_key": "",
                        "zone_match": "center_inside",
                        "min_count": 1,
                        "max_count": None,
                        "signal_label": "Objects overlap",
                    }
                ],
                help_text=(
                    "Use overlap for intersecting boxes, overlap 100% for full "
                    "coverage, or distance for near-but-not-touching objects."
                ),
            ),
        ],
        constraints={"port_aliases": {"region_set": ["detections", "segments"]}},
    ),
    _node(
        node_type="analysis.object_spatial_signal",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Object spatial signal",
        description=(
            "Emit a boolean signal when detected or segmented objects approach, "
            "overlap, meet a distance rule, or occupy an operator-drawn field."
        ),
        supported_modes=ALL_MODES,
        input_ports=[
            port("primary_objects", "region_set"),
            port("secondary_objects", "region_set", required=False),
        ],
        output_ports=[
            port("signal", "result"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "match_policy": "any",
            "min_match_count": 1,
            "zones": [],
            "rules": [
                {
                    "rule_key": "near_objects",
                    "relation": "distance",
                    "enabled": True,
                    "primary_labels": [],
                    "secondary_labels": [],
                    "label_pairing": "any",
                    "max_distance_percent": 10,
                    "min_distance_percent": None,
                    "min_overlap_percent": 1,
                    "min_closing_percent": 1,
                    "zone_key": "",
                    "zone_match": "center_inside",
                    "min_count": 1,
                    "max_count": None,
                    "signal_label": "Objects near each other",
                }
            ],
        },
        property_schema=[
            prop(
                "match_policy",
                "Match policy",
                "select",
                default_value="any",
                options=[("any", "Any rule"), ("all", "All rules")],
            ),
            prop(
                "min_match_count",
                "Minimum active matches",
                "integer",
                default_value=1,
                min_value=1,
            ),
            prop("zones", "Fields", "zone_list", default_value=[]),
            prop(
                "rules",
                "Spatial rules",
                "spatial_rule_table",
                required=True,
                default_value=[
                    {
                        "rule_key": "near_objects",
                        "relation": "distance",
                        "enabled": True,
                        "primary_labels": [],
                        "secondary_labels": [],
                        "label_pairing": "any",
                        "max_distance_percent": 10,
                        "min_distance_percent": None,
                        "min_overlap_percent": 1,
                        "min_closing_percent": 1,
                        "zone_key": "",
                        "zone_match": "center_inside",
                        "min_count": 1,
                        "max_count": None,
                        "signal_label": "Objects near each other",
                    }
                ],
                help_text=(
                    "Configure object relations by class labels, frame-relative "
                    "percent thresholds, and fields drawn on the live frame."
                ),
            ),
        ],
        constraints={
            "port_aliases": {"region_set": ["detections", "segments"]},
            "hidden_from_palette": True,
            "replacement_node_types": ["analysis.zone_detection", "analysis.close_detection"],
        },
    ),
    _node(
        node_type="analysis.object_orientation_signal",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Object orientation signal",
        description=(
            "Evaluate elongated object orientation and shape against a drawn or "
            "coordinate-frame direction inside configured inspection fields."
        ),
        supported_modes=ALL_MODES,
        input_ports=[
            port("objects", "region_set"),
            port("coordinate_frame", "coordinate_frame", required=False),
        ],
        output_ports=[
            port("signal", "result"),
            port("overlay", "overlay", required=False),
        ],
        default_config={
            "labels": [],
            "zones": [],
            "zone_match": "center_inside",
            "orientation_source": "drawn_direction",
            "reference_direction": {
                "label": "Expected direction",
                "start": {"x": 0.15, "y": 0.50},
                "end": {"x": 0.85, "y": 0.50},
            },
            "reference_angle_degrees": 0,
            "angle_tolerance_degrees": 10,
            "min_aspect_ratio": 2.0,
            "min_major_axis_percent": 0,
            "max_minor_axis_percent": None,
            "minimum_object_count": 1,
            "maximum_object_count": None,
            "emit_signal_on": "violation",
            "signal_label": "Object orientation violation",
        },
        property_schema=[
            prop("labels", "Object labels", "text", default_value=""),
            prop("zones", "Inspection fields", "zone_list", default_value=[]),
            prop(
                "zone_match",
                "Zone match",
                "select",
                default_value="center_inside",
                options=[
                    ("center_inside", "Center inside"),
                    ("box_overlaps", "Box overlaps"),
                    ("fully_inside", "Fully inside"),
                ],
            ),
            prop(
                "orientation_source",
                "Reference source",
                "select",
                default_value="drawn_direction",
                options=[
                    ("drawn_direction", "Drawn direction"),
                    ("configured_angle", "Configured angle"),
                    ("coordinate_frame_longitudinal", "Coordinate frame longitudinal"),
                    ("coordinate_frame_transverse", "Coordinate frame transverse"),
                ],
            ),
            prop(
                "reference_direction",
                "Reference direction",
                "direction_reference",
                default_value={
                    "label": "Expected direction",
                    "start": {"x": 0.15, "y": 0.50},
                    "end": {"x": 0.85, "y": 0.50},
                },
            ),
            prop(
                "reference_angle_degrees",
                "Reference angle deg",
                "number",
                default_value=0,
                min_value=0,
                max_value=179,
            ),
            prop(
                "angle_tolerance_degrees",
                "Angle tolerance deg",
                "number",
                default_value=10,
                min_value=0,
                max_value=90,
            ),
            prop(
                "min_aspect_ratio", "Minimum aspect ratio", "number", default_value=2.0, min_value=1
            ),
            prop(
                "min_major_axis_percent",
                "Minimum length %",
                "number",
                default_value=0,
                min_value=0,
                max_value=100,
            ),
            prop(
                "max_minor_axis_percent",
                "Maximum width %",
                "number",
                default_value=None,
                min_value=0,
                max_value=100,
            ),
            prop(
                "minimum_object_count",
                "Minimum objects",
                "integer",
                default_value=1,
                min_value=0,
            ),
            prop(
                "maximum_object_count",
                "Maximum objects",
                "integer",
                default_value=None,
                min_value=0,
            ),
            prop(
                "emit_signal_on",
                "Signal mode",
                "select",
                default_value="violation",
                options=[
                    ("violation", "Signal on violation"),
                    ("pass", "Signal on pass"),
                ],
            ),
            prop(
                "signal_label",
                "Signal label",
                "text",
                default_value="Object orientation violation",
            ),
        ],
        constraints={"port_aliases": {"region_set": ["detections", "segments"]}},
    ),
    _node(
        node_type="analysis.movement_direction",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Movement direction",
        description=(
            "Compute movement direction from repeated position observations "
            "for the same object keys."
        ),
        supported_modes=FRAME_MODES,
        input_ports=[port("motion", "motion_basis")],
        output_ports=[port("direction", "measurements")],
        default_config={"smoothing": 0.4},
        property_schema=[
            prop("smoothing", "Smoothing", "number", default_value=0.4, min_value=0, max_value=1)
        ],
        constraints={"port_aliases": {"motion_basis": ["positions"]}},
    ),
    _node(
        node_type="analysis.rule_evaluation",
        category=RuntimeNodeCategory.ANALYSIS,
        display_name="Rule evaluation / result aggregation",
        description="Combine model and computation outputs into a normalized business result.",
        supported_modes=ALL_MODES,
        input_ports=RESULT_INPUTS,
        output_ports=[port("result", "result")],
        default_config={"expression": "pass"},
        property_schema=[
            prop("expression", "Rule expression", "expression", required=True, default_value="pass")
        ],
    ),
    _node(
        node_type="capture.full_frame_labelstudio",
        category=RuntimeNodeCategory.CAPTURE,
        display_name="Full-frame capture to Label Studio",
        description="Capture the current frame into one Label Studio project.",
        supported_modes=ALL_MODES,
        input_ports=[
            port("frame", "frame"),
            port("eligibility", "result", required=False),
        ],
        output_ports=CAPTURE_OUTPUTS,
        default_config={
            "target_project_ids": [],
            "rate_limit_per_minute": 30,
            "require_eligibility": False,
        },
        property_schema=[
            prop("target_project_ids", "Projects", "label_project_list", required=True),
            prop(
                "rate_limit_per_minute",
                "Rate limit",
                "integer",
                default_value=30,
                min_value=1,
            ),
            prop(
                "require_eligibility",
                "Require frame eligibility",
                "boolean",
                default_value=False,
                help_text=(
                    "When enabled, only accepted pipeline-image outputs are imported; "
                    "rejected eligibility results remain available for diagnosis."
                ),
            ),
        ],
    ),
    _node(
        node_type="capture.roi_labelstudio",
        category=RuntimeNodeCategory.CAPTURE,
        display_name="ROI capture to Label Studio",
        description="Capture extracted ROIs into a Label Studio project.",
        supported_modes=ALL_MODES,
        input_ports=[
            port("rois", "roi_set", required=False),
            port("frame", "frame", required=False),
            port("detections", "detections", required=False),
        ],
        output_ports=CAPTURE_OUTPUTS,
        default_config={"target_project_ids": [], "include_context": True},
        property_schema=[
            prop("target_project_ids", "Projects", "label_project_list", required=True),
            prop("include_context", "Include context", "boolean", default_value=True),
        ],
    ),
    _node(
        node_type="capture.conditional",
        category=RuntimeNodeCategory.CAPTURE,
        display_name="Conditional capture",
        description="Capture frames or ROIs only when a rule result matches.",
        supported_modes=ALL_MODES,
        input_ports=[port("result", "result"), port("frame", "frame", required=False)],
        output_ports=CAPTURE_OUTPUTS,
        default_config={"condition": "result == true", "target_project_ids": []},
        property_schema=[
            prop(
                "condition",
                "Condition",
                "expression",
                required=True,
                default_value="result == true",
            ),
            prop("target_project_ids", "Projects", "label_project_list", required=True),
        ],
    ),
    _node(
        node_type="capture.multi_project",
        category=RuntimeNodeCategory.CAPTURE,
        display_name="Multi-project capture",
        description="Route captured images or ROIs into multiple Label Studio projects.",
        supported_modes=ALL_MODES,
        input_ports=[port("asset", "capture_asset")],
        output_ports=CAPTURE_OUTPUTS,
        default_config={"target_project_ids": []},
        property_schema=[
            prop("target_project_ids", "Projects", "label_project_list", required=True)
        ],
        constraints={"port_aliases": {"capture_asset": ["frame", "roi_set", "result"]}},
    ),
    _node(
        node_type="output.overlay",
        category=RuntimeNodeCategory.OUTPUT,
        display_name="Overlay output",
        description=(
            "Create context-bound overlay primitives from visual or object results. "
            "Rendering is selected by Display output or the simple-pipeline default."
        ),
        supported_modes=ALL_MODES,
        input_ports=RESULT_INPUTS,
        output_ports=[],
        default_config={
            "layers": [
                "boxes",
                "labels",
                "zones",
                "lines",
                "polygons",
                "measurements",
                "masks",
                "heatmaps",
                "color_object_box",
                "color_object_contour",
                "color_object_axis",
            ]
        },
        property_schema=[
            prop(
                "layers",
                "Layers",
                "select",
                default_value="boxes",
                options=[
                    ("boxes", "Boxes"),
                    ("labels", "Labels"),
                    ("zones", "Zones"),
                    ("lines", "Lines / axes"),
                    ("polygons", "Polygons"),
                    ("measurements", "Measurements"),
                    ("masks", "Masks"),
                    ("heatmaps", "Heatmaps"),
                    ("color_object_box", "Color object boxes"),
                    ("color_object_contour", "Color object contours"),
                    ("color_object_axis", "Color object axes"),
                ],
            )
        ],
        constraints={
            "port_aliases": {
                "result": [
                    "detections",
                    "segments",
                    "region_set",
                    "object_set",
                    "overlay",
                    "positions",
                    "measurements",
                    "events",
                    "board_cut_decision",
                ]
            }
        },
    ),
    _node(
        node_type="output.display_output",
        category=RuntimeNodeCategory.OUTPUT,
        display_name="Display output",
        description=(
            "Select the image context for the channel live view and declare which "
            "object, overlay, and enriched-label layers may render on it."
        ),
        supported_modes=ALL_MODES,
        input_ports=[
            port("frame", "frame"),
            port("objects", "object_set", required=False),
            port("result", "result", required=False),
        ],
        output_ports=[],
        default_config={
            "display_name": "Live view",
            "overlay_layers": "detections,segmentation,labels,values,zones,rois",
            "object_layers": "detections,segments,enriched_labels",
            "show_enriched_labels": True,
        },
        property_schema=[
            prop("display_name", "Display name", "text", default_value="Live view"),
            prop(
                "overlay_layers",
                "Overlay layers",
                "text",
                default_value="detections,segmentation,labels,values,zones,rois",
                help_text="Comma-separated operator layer names to render on this live view.",
            ),
            prop(
                "object_layers",
                "Object layers",
                "text",
                default_value="detections,segments,enriched_labels",
                help_text="Comma-separated object/enrichment layers allowed on this display.",
            ),
            prop(
                "show_enriched_labels",
                "Show enriched labels",
                "boolean",
                default_value=True,
            ),
        ],
        constraints={
            "live_view_display_selector": True,
            "port_aliases": {
                "object_set": ["detections", "segments", "region_set"],
                "result": [
                    "detections",
                    "classifications",
                    "segments",
                    "anomaly",
                    "positions",
                    "measurements",
                    "events",
                    "roi_set",
                    "frame",
                    "overlay",
                    "region_set",
                ],
            },
        },
    ),
    _node(
        node_type="output.result",
        category=RuntimeNodeCategory.OUTPUT,
        display_name="Result output",
        description=(
            "Publish selected pipeline result fields for live display and trigger "
            "responses. Database history is opt-in per channel and only stores "
            "fields projected to the history destination."
        ),
        supported_modes=ALL_MODES,
        input_ports=RESULT_INPUTS,
        output_ports=[],
        default_config={
            "result_fields": [],
            "projections": [],
            "fields": "",
            "include_decision": True,
            "include_runtime_metadata": True,
            "wrap_key": "",
            "edge_response_mode": "full",
            "edge_response_metadata_fields": [],
        },
        property_schema=[
            prop(
                "edge_response_mode",
                "Umfang der Edge-HTTP-Antwort",
                "select",
                default_value="full",
                section="Edge HTTP-Antwort",
                options=[
                    ("result_only", "Nur konfigurierte Ergebnisfelder"),
                    ("result_with_metadata", "Ergebnisfelder + ausgewählte Metadaten"),
                    ("full", "Vollständiger Ausführungsbericht (bisheriges Verhalten)"),
                ],
                help_text=(
                    'Nur Ergebnisfelder liefert z. B. {"result":"LKW wartend"} direkt als '
                    "HTTP-Body. Die Result projections mit Ziel response bestimmen die Felder. "
                    "Mit Metadaten lautet die Struktur {data: Ergebnisfelder, meta: Auswahl}. "
                    "Historie und Gerätestatistiken bleiben unabhängig davon verfügbar."
                ),
            ),
            prop(
                "edge_response_metadata_fields",
                "Zusätzliche Metadaten",
                "multi_select",
                default_value=[],
                section="Edge HTTP-Antwort",
                options=[
                    ("correlation_id", "Korrelations-ID"),
                    ("channel_id", "Kanal-ID"),
                    ("pipeline_version_id", "Pipeline-Version"),
                    ("workload_release_id", "Edge-Release"),
                    ("completed_at", "Abschlusszeitpunkt"),
                    ("result_sequence", "Ergebnis-Sequenznummer"),
                    ("duplicate", "Bereits ausgeführter Auftrag"),
                    ("status", "Ausführungsstatus"),
                    ("duration_ms", "Gesamtlaufzeit (ms)"),
                    ("timing", "Detaillierte Laufzeiten aller Verarbeitungsschritte"),
                ],
                help_text="Nur ausgewählte Felder werden im Objekt meta mitgeliefert.",
            ),
            prop(
                "projections",
                "Result projections",
                "result_projection_table",
                default_value=[],
                help_text=(
                    "Define exactly which values are calculated from pipeline outputs "
                    "and whether each value goes to response, history, live view, PLC, "
                    "or connectors. History also requires the channel history policy "
                    "to be enabled."
                ),
            ),
            prop(
                "result_fields",
                "Legacy result fields",
                "response_mapping",
                default_value=[],
                help_text=("Legacy mapping. New pipeline versions should use Result projections."),
            ),
            prop("include_decision", "Include decision", "boolean", default_value=True),
            prop(
                "include_runtime_metadata",
                "Metadaten in der Historie",
                "boolean",
                default_value=True,
                help_text=(
                    "Ergänzt bei Result projections Kanal-ID, Pipeline-Version und "
                    "Korrelations-ID in der Historie. Der Umfang der Edge-HTTP-Antwort "
                    "wird separat eingestellt."
                ),
            ),
            prop(
                "wrap_key",
                "Wrap result under key",
                "text",
                default_value="",
                help_text=(
                    "Optional. Empty returns fields at the top level. Example "
                    'wrap key result returns {"result": { ...fields }}.'
                ),
            ),
        ],
        constraints={
            "port_aliases": {
                "result": [
                    "detections",
                    "classifications",
                    "segments",
                    "anomaly",
                    "positions",
                    "measurements",
                    "stem_horizontal_curvature",
                    "stem_surface_phase",
                    "stem_curvature_phase_solution",
                    "cv_edges",
                    "reference_edges",
                    "coordinate_frame",
                    "feature_measurements",
                    "clearance_zones",
                    "free_region",
                    "quality_decision",
                    "events",
                    "roi_set",
                    "frame",
                    "overlay",
                    "object_set",
                    "region_set",
                ],
            },
            "primary_result_output": True,
        },
    ),
    _node(
        node_type="output.siemens_sps_write",
        category=RuntimeNodeCategory.OUTPUT,
        display_name="Write to Siemens SPS",
        description=(
            "Write selected pipeline result values to Siemens S7/SPS DB addresses "
            "using explicit mapping, conversion, and safety behavior."
        ),
        supported_modes=ALL_MODES,
        input_ports=RESULT_INPUTS,
        output_ports=[],
        handler="cv_runtime.siemens_sps:output_siemens_sps_write",
        default_config={
            "enabled": False,
            "dry_run": True,
            "connection": {
                "name": "Siemens SPS",
                "host": "",
                "port": 102,
                "rack": 0,
                "slot": 1,
                "connect_timeout_ms": 1000,
                "write_timeout_ms": 1000,
                "reconnect": "on_failure",
                "reuse_policy": "per_execution",
                "connection_ref": None,
            },
            "mappings": [],
            "failure_behavior": "fail_pipeline",
            "realtime_min_interval_ms": 1000,
            "stem_orientation_command_enabled": False,
            "stem_orientation_minimum_confidence": 0.55,
            "stem_orientation_maximum_abs_angle_deg": 90.0,
            "closed_phase_control_enabled": False,
            "phase_control_stop_tolerance_deg": 2.0,
            "sps_rotation_limits_confirmed": False,
            "sps_maximum_rotation_deg": 90.0,
            "sps_maximum_rotation_time_ms": 5000,
        },
        property_schema=[
            prop("enabled", "Enable PLC writes", "boolean", default_value=False),
            prop("dry_run", "Dry run", "boolean", default_value=True),
            prop(
                "connection",
                "PLC connection",
                "plc_connection",
                default_value={
                    "name": "Siemens SPS",
                    "host": "",
                    "port": 102,
                    "rack": 0,
                    "slot": 1,
                    "connect_timeout_ms": 1000,
                    "write_timeout_ms": 1000,
                    "reconnect": "on_failure",
                    "reuse_policy": "per_execution",
                    "connection_ref": None,
                },
                help_text=(
                    "Configure the Siemens S7 endpoint. Operators do not need internal IDs; "
                    "named PLC connectors can be introduced later through connection_ref."
                ),
            ),
            prop(
                "mappings",
                "PLC write mappings",
                "plc_write_mapping_table",
                default_value=[],
                help_text=(
                    "Map upstream result fields to Siemens DB addresses with explicit "
                    "conversion and failure behavior."
                ),
            ),
            prop(
                "failure_behavior",
                "Failure behavior",
                "select",
                default_value="fail_pipeline",
                options=[
                    ("fail_pipeline", "Fail pipeline"),
                    ("warn_continue", "Warn and continue"),
                    ("skip_mapping", "Skip failed mapping"),
                ],
            ),
            prop(
                "realtime_min_interval_ms",
                "Realtime write throttle",
                "integer",
                default_value=1000,
                min_value=100,
                max_value=60000,
                help_text="Minimum interval between writes when used in realtime pipelines.",
            ),
            prop(
                "stem_orientation_command_enabled",
                "Enable stem-orientation command gate",
                "boolean",
                default_value=False,
                section="Stem orientation dry run",
                help_text=(
                    "Accept only validated, approved, unexpired stem.orientation.v3 "
                    "rotation results. PLC writes remain governed by enabled, dry run, "
                    "the runtime environment, and explicit mappings."
                ),
            ),
            prop(
                "stem_orientation_minimum_confidence",
                "Minimum stem command confidence",
                "number",
                default_value=0.55,
                min_value=0,
                max_value=1,
                step_value=0.01,
                section="Stem orientation dry run",
            ),
            prop(
                "stem_orientation_maximum_abs_angle_deg",
                "Maximum process angle [deg]",
                "number",
                default_value=90,
                min_value=0.1,
                max_value=90,
                section="Stem orientation dry run",
            ),
            prop(
                "closed_phase_control_enabled",
                "Enable optional closed phase guidance",
                "boolean",
                default_value=False,
                section="Optional phase control",
                help_text=(
                    "Continue surface-phase tracking after the frozen decision and expose "
                    "a stop request. Curvature remains frozen and the PLC remains the "
                    "independent safety and limit authority."
                ),
            ),
            prop(
                "phase_control_stop_tolerance_deg",
                "Target phase stop tolerance [deg]",
                "number",
                default_value=2,
                min_value=0,
                max_value=30,
                section="Optional phase control",
            ),
            prop(
                "sps_rotation_limits_confirmed",
                "PLC angle/time limits confirmed",
                "boolean",
                default_value=False,
                section="Optional phase control",
            ),
            prop(
                "sps_maximum_rotation_deg",
                "PLC maximum rotation [deg]",
                "number",
                default_value=90,
                min_value=0.1,
                max_value=180,
                section="Optional phase control",
            ),
            prop(
                "sps_maximum_rotation_time_ms",
                "PLC maximum rotation time [ms]",
                "integer",
                default_value=5000,
                min_value=100,
                max_value=120000,
                section="Optional phase control",
            ),
        ],
        constraints={
            "side_effect": "industrial_plc_write",
            "requires_runtime_dependency": "python-snap7>=3,<4",
            "palette_group": "Industrial / PLC",
            "port_aliases": {
                "result": [
                    "detections",
                    "classifications",
                    "segments",
                    "anomaly",
                    "positions",
                    "measurements",
                    "stem_curvature_phase_solution",
                    "cv_edges",
                    "reference_edges",
                    "coordinate_frame",
                    "feature_measurements",
                    "clearance_zones",
                    "free_region",
                    "quality_decision",
                    "events",
                    "roi_set",
                    "frame",
                    "overlay",
                    "object_set",
                    "region_set",
                ],
            },
        },
    ),
    _node(
        node_type="output.http_trigger_response",
        category=RuntimeNodeCategory.OUTPUT,
        display_name="Response output",
        description="Return selected result fields in the trigger response body.",
        supported_modes=TRIGGER_MODES,
        input_ports=RESULT_INPUTS,
        output_ports=[],
        default_config={
            "response_fields": [
                {"source_field": "result", "response_key": "result", "required": False},
                {"source_field": "count", "response_key": "count", "required": False},
            ],
            "status_on_false": 200,
        },
        property_schema=[
            prop(
                "response_fields",
                "Response fields",
                "response_mapping",
                default_value=[
                    {"source_field": "result", "response_key": "result", "required": False},
                    {"source_field": "count", "response_key": "count", "required": False},
                ],
            ),
            prop("status_on_false", "False status", "integer", default_value=200),
        ],
        constraints={
            "deprecated": True,
            "hidden_from_palette": True,
            "replacement_node_type": "output.result",
        },
    ),
    _node(
        node_type="output.persisted_result",
        category=RuntimeNodeCategory.OUTPUT,
        display_name="Persisted result",
        description="Store normalized results for history and status views.",
        supported_modes=ALL_MODES,
        input_ports=RESULT_INPUTS,
        output_ports=[],
        default_config={"retention_days": 30},
        property_schema=[
            prop("retention_days", "Retention days", "integer", default_value=30, min_value=1)
        ],
        constraints={
            "deprecated": True,
            "hidden_from_palette": True,
            "replacement_node_type": "output.result",
        },
    ),
    _node(
        node_type="output.internal_event",
        category=RuntimeNodeCategory.OUTPUT,
        display_name="Internal event output",
        description="Publish an internal runtime event for downstream services.",
        supported_modes=ALL_MODES,
        input_ports=RESULT_INPUTS,
        output_ports=[],
        default_config={"event_type": "runtime.pipeline.result.v1"},
        property_schema=[
            prop(
                "event_type",
                "Event type",
                "text",
                required=True,
                default_value="runtime.pipeline.result.v1",
            )
        ],
        constraints={
            "deprecated": True,
            "hidden_from_palette": True,
            "replacement_node_type": "output.result",
        },
    ),
)


BUILTIN_NODE_TYPES += bark_node_types(_node, port, prop)
BUILTIN_NODE_TYPE_BY_KEY = {item.node_type: item for item in BUILTIN_NODE_TYPES}


def builtin_node_types() -> tuple[RuntimeNodeTypeSummary, ...]:
    return BUILTIN_NODE_TYPES
