from control_worker.jobs.dataset import (
    _cached_asset_relative_path,
    _normalize_label_studio_asset_reference,
)
from cv_db.orchestration_store import list_project_model_templates
from cv_status import TaskType


def test_list_project_model_templates_filters_for_detection() -> None:
    templates = list_project_model_templates(task_type=TaskType.DETECTION)

    assert [item.template_key for item in templates] == [
        "detection-yolov8-n",
        "detection-yolov8-s",
        "detection-yolov8-m",
        "detection-yolov8-l",
        "detection-yolov8-x",
        "detection-yolov11-n",
        "detection-yolov11-s",
        "detection-yolov11-m",
        "detection-yolov11-l",
        "detection-yolov11-x",
        "detection-yolov26-n",
        "detection-yolov26-s",
        "detection-yolov26-m",
        "detection-yolov26-l",
        "detection-yolov26-x",
    ]
    assert templates[0].recommended is True


def test_list_project_model_templates_exposes_yolo_ultralytics_sizes_for_all_tasks() -> None:
    classification_keys = {
        item.template_key
        for item in list_project_model_templates(task_type=TaskType.CLASSIFICATION)
    }
    segmentation_keys = {
        item.template_key for item in list_project_model_templates(task_type=TaskType.SEGMENTATION)
    }

    for version in ("v11", "v26"):
        for size in ("n", "s", "m", "l", "x"):
            assert f"classification-yolo{version}-{size}" in classification_keys

    for version in ("v8", "v11", "v26"):
        for size in ("n", "s", "m", "l", "x"):
            assert f"segmentation-yolo{version}-{size}" in segmentation_keys


def test_normalize_label_studio_asset_reference_handles_local_files_query() -> None:
    normalized = _normalize_label_studio_asset_reference(
        "/data/local-files/?d=/srv/cv-platform/control/uploads/dts_dst_1/assets/ab/cars.png"
    )

    assert normalized == "srv/cv-platform/control/uploads/dts_dst_1/assets/ab/cars.png"


def test_cached_asset_relative_path_drops_parent_segments_and_preserves_readable_names() -> None:
    relative_path = _cached_asset_relative_path("../Cars Inspection/Front View 01.png")

    assert relative_path == "Cars Inspection/Front View 01.png"
