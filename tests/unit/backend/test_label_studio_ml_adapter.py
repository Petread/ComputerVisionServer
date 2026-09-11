from __future__ import annotations

from types import SimpleNamespace

from cv_status import TaskType
from inference_gateway.api.label_studio_ml_adapter import (
    _anomaly_label,
    _detection_result,
    _label_control,
    _prediction_response,
    _tasks_from_payload,
)


def test_label_studio_adapter_parses_detection_control_from_xml() -> None:
    control = _label_control(
        """
        <View>
          <Image name="photo" value="$image"/>
          <RectangleLabels name="defect_box" toName="photo">
            <Label value="scratch"/>
          </RectangleLabels>
        </View>
        """,
        TaskType.DETECTION,
    )

    assert control.from_name == "defect_box"
    assert control.to_name == "photo"
    assert control.result_type == "rectanglelabels"


def test_label_studio_adapter_parses_detection_control_from_dict_with_to_name_list() -> None:
    control = _label_control(
        {
            "label": {
                "type": "RectangleLabels",
                "to_name": ["image"],
                "labels": ["Brett", "Ast"],
            }
        },
        TaskType.DETECTION,
    )

    assert control.from_name == "label"
    assert control.to_name == "image"
    assert control.result_type == "rectanglelabels"


def test_label_studio_adapter_formats_detection_boxes_as_percentages() -> None:
    control = _label_control({"labels": ["scratch"]}, TaskType.DETECTION)
    detection = SimpleNamespace(
        label="scratch",
        confidence=0.91,
        bounding_box=SimpleNamespace(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.6),
    )

    result = _detection_result(control, [detection], image_width=100, image_height=100)

    assert result == [
        {
            "id": result[0]["id"],
            "from_name": "label",
            "to_name": "image",
            "type": "rectanglelabels",
            "value": {
                "x": 10.0,
                "y": 20.0,
                "width": 30.0,
                "height": 40.0,
                "rotation": 0,
                "rectanglelabels": ["scratch"],
            },
            "score": 0.91,
        }
    ]


def test_label_studio_adapter_formats_oriented_boxes_with_rotation() -> None:
    control = _label_control({"labels": ["scratch"]}, TaskType.ORIENTED_DETECTION)
    detection = SimpleNamespace(
        label="scratch",
        confidence=0.88,
        bounding_box=SimpleNamespace(x_min=0.1, y_min=0.1, x_max=0.3, y_max=0.4),
        oriented_box=SimpleNamespace(
            points=[
                SimpleNamespace(x=0.1, y=0.1),
                SimpleNamespace(x=0.3, y=0.3),
                SimpleNamespace(x=0.2, y=0.4),
                SimpleNamespace(x=0.0, y=0.2),
            ],
            rotation_degrees=45,
        ),
    )

    result = _detection_result(control, [detection], image_width=100, image_height=100)

    assert result[0]["value"]["x"] == 10.0
    assert result[0]["value"]["y"] == 10.0
    assert result[0]["value"]["rotation"] == 45.0
    assert result[0]["value"]["rectanglelabels"] == ["scratch"]


def test_label_studio_adapter_maps_anomaly_labels_to_declared_classes() -> None:
    assert _anomaly_label("normal", ["good", "bad"]) == "good"
    assert _anomaly_label("anomalous", ["good", "bad"]) == "bad"
    assert _anomaly_label("normal", ["ok", "nok"]) == "ok"


def test_label_studio_adapter_reads_tasks_from_payload() -> None:
    assert _tasks_from_payload({"tasks": [{"id": 1}, {"id": 2}]}) == [{"id": 1}, {"id": 2}]
    assert _tasks_from_payload([{"id": 3}]) == [{"id": 3}]
    assert _tasks_from_payload({"tasks": {"id": 4}}) == [{"id": 4}]
    assert _tasks_from_payload({"task": {"id": 5}}) == [{"id": 5}]
    assert _tasks_from_payload({"id": 6, "data": {"image": "/data/local-files/?d=image.jpg"}}) == [
        {"id": 6, "data": {"image": "/data/local-files/?d=image.jpg"}}
    ]


def test_label_studio_adapter_response_includes_prediction_keys() -> None:
    predictions = [{"result": [], "score": 0.0, "model_version": "detector:v1:mvr_1"}]

    response = _prediction_response(predictions=predictions, model_version="detector:v1:mvr_1")

    assert response["predictions"] == predictions
    assert response["results"] == predictions
    assert response["model_version"] == "detector:v1:mvr_1"
