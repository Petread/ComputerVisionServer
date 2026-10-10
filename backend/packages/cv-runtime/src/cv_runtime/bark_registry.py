"""Catalogue entries for the paired end-face inspection workflow."""

# ruff: noqa: ANN001, ANN201
from cv_contracts.runtime import RuntimeNodeTypeSummary
from cv_status import RuntimeMode, RuntimeNodeCategory


def bark_node_types(node, port, prop):
    def define(key, name, category, inputs, outputs, config, fields) -> RuntimeNodeTypeSummary:
        return node(
            node_type=key,
            display_name=name,
            description=name,
            category=category,
            supported_modes=[RuntimeMode.TRIGGER],
            input_ports=inputs,
            output_ports=outputs,
            default_config=config,
            property_schema=fields,
            handler=f"cv_runtime.nodes.bark:{key.replace('.', '_')}",
        )

    split_config = {
        "side_mapping": "",
        "left_region": [{"name": "Links", "x": 0, "y": 0, "width": 0.5, "height": 1}],
        "right_region": [{"name": "Rechts", "x": 0.5, "y": 0, "width": 0.5, "height": 1}],
    }
    split_fields = [
        prop(
            "side_mapping",
            "Zuordnung der Messscheiben",
            "select",
            required=True,
            default_value="",
            options=[
                ("left_first", "Links = erste Scheibe / Rechts = letzte"),
                ("left_last", "Links = letzte Scheibe / Rechts = erste"),
            ],
            help_text=(
                "Bezieht sich auf das unveränderte Inputfoto. "
                "Eine explizite Auftragsauswahl überschreibt nur diesen Auftrag."
            ),
        )
    ]
    for side, label in (("left", "Links"), ("right", "Rechts")):
        split_fields.extend(
            [
                prop(
                    f"{side}_region",
                    f"Bildbereich {label}",
                    "roi",
                    required=True,
                    default_value=split_config[f"{side}_region"],
                    help_text="Genau ein Rechteck im unveränderten Inputfoto.",
                ),
                prop(
                    f"{side}_rotation",
                    f"Drehung {label}",
                    "select",
                    default_value="0",
                    options=[(str(v), f"{v}° im Uhrzeigersinn") for v in (0, 90, 180, 270)],
                ),
                prop(f"{side}_mirror", f"Spiegelung {label}", "boolean", default_value=False),
            ]
        )
        split_config[f"{side}_rotation"] = "0"
        split_config[f"{side}_mirror"] = False
    metric_config = {
        "endface_label": "endface",
        "mm_per_pixel": 0.25,
        "maximum_error_mm": 0.5,
        "maximum_stability_mm": 0.25,
    }
    radial_config = {
        "sample_count": 360,
        "minimum_valid_fraction": 0.9,
        "maximum_invalid_gap_deg": 10,
        "maximum_source_mm_per_pixel": 0.5,
        "include_extrema": False,
        **{f"{k}_label": k for k in ("wood", "bark", "background", "unknown")},
    }
    return (
        define(
            "source.stem_csv_measurements",
            "Stamm-Messdatei (CSV)",
            RuntimeNodeCategory.SOURCE,
            [],
            [port("measurement", "stem_csv")],
            {"format": "HFA_KRUEMMUNG_V1"},
            [
                prop(
                    "format",
                    "Messdateiformat",
                    "select",
                    default_value="HFA_KRUEMMUNG_V1",
                    options=[("HFA_KRUEMMUNG_V1", "HFA Krümmung V1")],
                )
            ],
        ),
        define(
            "preprocessing.endface_pair_split",
            "Stirnflächen aufteilen und zuordnen",
            RuntimeNodeCategory.PREPROCESSING,
            [port("frame", "frame"), port("measurement", "stem_csv")],
            [
                port(f"{side}_{kind}", "frame" if kind == "frame" else "endface_reference")
                for side in ("left", "right")
                for kind in ("frame", "reference")
            ],
            split_config,
            split_fields,
        ),
        define(
            "preprocessing.endface_metric_rectify",
            "Stirnfläche metrisch ausrichten",
            RuntimeNodeCategory.PREPROCESSING,
            [
                port("frame", "frame"),
                port("reference", "endface_reference"),
                port("segments", "segments"),
            ],
            [
                port("frame", "frame"),
                port("calibration", "endface_calibration"),
                port("eligibility", "result", required=False),
                port("isolated", "frame"),
                port("diagnostic", "frame"),
            ],
            metric_config,
            [
                prop(
                    "endface_label",
                    "Klasse gesamte Schnittfläche",
                    "text",
                    required=True,
                    default_value="endface",
                ),
                prop(
                    "mm_per_pixel",
                    "Ausgaberaster [mm/Pixel]",
                    "number",
                    default_value=0.25,
                    min_value=0.05,
                    max_value=5,
                ),
                prop(
                    "maximum_error_mm",
                    "Max. Konturfehler [mm]",
                    "number",
                    default_value=0.5,
                    min_value=0.01,
                    max_value=0.5,
                ),
                prop(
                    "maximum_stability_mm",
                    "Max. Registrierungsinstabilität [mm]",
                    "number",
                    default_value=0.25,
                    min_value=0.01,
                    max_value=0.25,
                ),
            ],
        ),
        define(
            "analysis.bark_radial_thickness",
            "Rindenstärke radial messen",
            RuntimeNodeCategory.ANALYSIS,
            [
                port("frame", "frame"),
                port("calibration", "endface_calibration"),
                port("segments", "segments"),
            ],
            [port("measurement", "bark_measurement"), port("diagnostic", "frame")],
            radial_config,
            [
                prop(
                    f"{key}_label",
                    f"Modellklasse {label}",
                    "text",
                    required=True,
                    default_value=key,
                )
                for key, label in (
                    ("wood", "Holz"),
                    ("bark", "Rinde"),
                    ("background", "Hintergrund"),
                    ("unknown", "Unklar/verdeckt"),
                )
            ]
            + [
                prop(
                    "sample_count",
                    "Radiale Messstrahlen",
                    "integer",
                    default_value=360,
                    min_value=36,
                    max_value=1440,
                ),
                prop(
                    "minimum_valid_fraction",
                    "Mindestanteil gültiger Strahlen",
                    "number",
                    default_value=0.9,
                    min_value=0.9,
                    max_value=1,
                ),
                prop(
                    "maximum_invalid_gap_deg",
                    "Max. ungültige Winkellücke [°]",
                    "number",
                    default_value=10,
                    min_value=0,
                    max_value=10,
                ),
                prop(
                    "maximum_source_mm_per_pixel",
                    "Max. ursprüngliche Pixelgröße [mm]",
                    "number",
                    default_value=0.5,
                    min_value=0.01,
                    max_value=0.5,
                ),
                prop(
                    "include_extrema", "Min/Max zusätzlich ausgeben", "boolean", default_value=False
                ),
            ],
        ),
        define(
            "analysis.bark_pair_result",
            "Rindenmessung zusammenführen",
            RuntimeNodeCategory.ANALYSIS,
            [port("left", "bark_measurement"), port("right", "bark_measurement")],
            [port("result", "result")],
            {"aggregation": "equal_valid_sides"},
            [
                prop(
                    "aggregation",
                    "Stamm-Mittelwert",
                    "select",
                    default_value="equal_valid_sides",
                    options=[("equal_valid_sides", "Beide gültigen Seiten gleich gewichten")],
                )
            ],
        ),
    )
