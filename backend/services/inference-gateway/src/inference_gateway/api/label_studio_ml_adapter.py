from __future__ import annotations

import asyncio
import base64
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Body, Depends, Request

from cv_core import PlatformSettings
from cv_db import resolve_project_active_realtime_deployment, session_scope
from cv_datasets import LabelStudioClient, extract_declared_classes
from cv_status import TargetPath, TaskType
from cv_utils import PlatformError
from inference_gateway.dependencies import get_settings
from inference_gateway.runtime.public_service import PublicRealtimeGatewayService

IMAGE_DATA_KEYS = ("image", "image_url", "image_path", "img", "file", "file_upload")
NORMAL_LABELS = {"good", "normal", "ok", "okay", "pass", "accepted", "healthy"}
ANOMALOUS_LABELS = {"bad", "abnormal", "anomaly", "anomalous", "defect", "defective", "fail", "rejected"}

router = APIRouter(tags=["label-studio-ml-adapter"])


@dataclass(frozen=True)
class ActiveProjectModel:
    label_project_id: str
    label_studio_project_id: int
    task_type: TaskType
    label_config: dict[str, Any] | str | None
    model_id: str
    model_version_id: str
    model_version_no: int
    declared_classes: list[str]


@dataclass(frozen=True)
class LabelControl:
    from_name: str
    to_name: str
    result_type: str
    value_key: str


def get_adapter_service(
    request: Request,
    settings: PlatformSettings = Depends(get_settings),
) -> PublicRealtimeGatewayService:
    service = getattr(request.app.state, "label_studio_ml_adapter_service", None)
    if (
        service is None
        or not isinstance(service, PublicRealtimeGatewayService)
        or getattr(service, "settings", None) is not settings
    ):
        service = PublicRealtimeGatewayService(
            settings,
            service_name="label-studio-ml-adapter",
            target_path=TargetPath.REALTIME,
        )
        request.app.state.label_studio_ml_adapter_service = service
    return service


@router.get("/health")
async def root_health() -> dict[str, str]:
    return {"status": "UP"}


@router.get("/projects/{label_project_id}")
@router.get("/projects/{label_project_id}/health")
async def project_health(
    label_project_id: str,
    settings: PlatformSettings = Depends(get_settings),
) -> dict[str, Any]:
    active = _active_project_model(settings, label_project_id, required=False)
    return {
        "status": "UP" if active is not None else "NO_MODEL",
        "label_project_id": label_project_id,
        "model_version": _model_version(active) if active is not None else None,
    }


@router.post("/projects/{label_project_id}/setup")
async def setup_project(
    label_project_id: str,
    _payload: dict[str, Any] | None = Body(default=None),
    settings: PlatformSettings = Depends(get_settings),
) -> dict[str, Any]:
    active = _active_project_model(settings, label_project_id, required=False)
    return {
        "model_version": _model_version(active) if active is not None else "unavailable",
        "label_project_id": label_project_id,
        "supports_interactive": False,
    }


@router.post("/projects/{label_project_id}/webhook")
async def project_webhook(label_project_id: str) -> dict[str, str]:
    return {"status": "ok", "label_project_id": label_project_id}


@router.post("/projects/{label_project_id}/predict")
async def predict_project(
    label_project_id: str,
    request: Request,
    settings: PlatformSettings = Depends(get_settings),
    service: PublicRealtimeGatewayService = Depends(get_adapter_service),
) -> dict[str, Any]:
    payload = await request.json()
    tasks = _tasks_from_payload(payload)
    active = _active_project_model(settings, label_project_id, required=True)
    assert active is not None
    predictions = [
        await _predict_task(settings=settings, service=service, active=active, task=task)
        for task in tasks
    ]
    return _prediction_response(predictions=predictions, model_version=_model_version(active))


async def _predict_task(
    *,
    settings: PlatformSettings,
    service: PublicRealtimeGatewayService,
    active: ActiveProjectModel,
    task: dict[str, Any],
) -> dict[str, Any]:
    image_ref = _task_image_ref(task)
    if not image_ref:
        raise PlatformError(
            code="labelstudio_task_image_missing",
            message="The Label Studio task does not contain an image reference.",
            status_code=422,
        )
    image_bytes, content_type = await _load_image(settings, image_ref)
    file_name = _file_name_from_ref(image_ref)
    control = _label_control(active.label_config, active.task_type)

    if active.task_type == TaskType.CLASSIFICATION:
        response = await service.classify_public(
            model_id=active.model_id,
            file_name=file_name,
            content_type=content_type,
            image_bytes=image_bytes,
            timeout_ms=_prediction_timeout_ms(settings),
            top_k=1,
            parameters={"top_k": 1},
        )
        result = _classification_result(control, response.predictions)
        score = response.predictions[0].score if response.predictions else 0.0
    elif active.task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
        response = await service.detect_public(
            model_id=active.model_id,
            file_name=file_name,
            content_type=content_type,
            image_bytes=image_bytes,
            timeout_ms=_prediction_timeout_ms(settings),
            threshold=None,
            max_detections=None,
            parameters={},
        )
        result = _detection_result(
            control,
            response.detections,
            image_width=response.input.width,
            image_height=response.input.height,
        )
        score = _average_score([item.confidence for item in response.detections])
    elif active.task_type == TaskType.SEGMENTATION:
        response = await service.segment_public(
            model_id=active.model_id,
            file_name=file_name,
            content_type=content_type,
            image_bytes=image_bytes,
            timeout_ms=_prediction_timeout_ms(settings),
            threshold=None,
            max_segments=None,
            parameters={},
        )
        result = _segmentation_result(control, response.segments)
        score = _average_score([item.confidence for item in response.segments])
    elif active.task_type == TaskType.ANOMALY:
        response = await service.anomaly_public(
            model_id=active.model_id,
            file_name=file_name,
            content_type=content_type,
            image_bytes=image_bytes,
            timeout_ms=_prediction_timeout_ms(settings),
            threshold=None,
            return_mask=False,
            parameters={},
        )
        label = _anomaly_label(response.predicted_label, active.declared_classes)
        result = _choice_result(control, label=label, score=response.anomaly_score)
        score = response.anomaly_score
    else:
        raise PlatformError(
            code="labelstudio_task_type_unsupported",
            message="The project task type is not supported by the Label Studio prediction adapter.",
            detail=active.task_type.value,
            status_code=409,
        )

    return {
        "result": result,
        "score": round(float(score), 6),
        "model_version": _model_version(active),
    }


def _prediction_timeout_ms(settings: PlatformSettings) -> int:
    return min(max(int(settings.label_studio.ml_backend_timeout_seconds * 1000), 1000), 30_000)


def _active_project_model(
    settings: PlatformSettings,
    label_project_id: str,
    *,
    required: bool,
) -> ActiveProjectModel | None:
    with session_scope(settings) as session:
        active = resolve_project_active_realtime_deployment(
            session,
            label_project_id=label_project_id,
        )
        if active is None:
            if required:
                raise PlatformError(
                    code="labelstudio_model_unavailable",
                    message="No active realtime deployment is available for this Label Studio project.",
                    detail=label_project_id,
                    status_code=503,
                    retryable=True,
                )
            return None
        project, family, version, _release = active
        label_config = project.label_config
        return ActiveProjectModel(
            label_project_id=project.id,
            label_studio_project_id=project.label_studio_project_id,
            task_type=TaskType(project.task_type),
            label_config=label_config,
            model_id=family.slug,
            model_version_id=version.id,
            model_version_no=version.version_no,
            declared_classes=extract_declared_classes(label_config),
        )


def _model_version(active: ActiveProjectModel) -> str:
    return f"{active.model_id}:v{active.model_version_no}:{active.model_version_id}"


def _prediction_response(*, predictions: list[dict[str, Any]], model_version: str) -> dict[str, Any]:
    return {
        "predictions": predictions,
        "results": predictions,
        "model_version": model_version,
    }


def _tasks_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            return [item for item in tasks if isinstance(item, dict)]
        if isinstance(tasks, dict):
            return [tasks]
        task = payload.get("task")
        if isinstance(task, dict):
            return [task]
        if isinstance(payload.get("data"), dict):
            return [payload]
    return []


def _task_image_ref(task: dict[str, Any]) -> str | None:
    data = task.get("data")
    if not isinstance(data, dict):
        return None
    for key in IMAGE_DATA_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    for value in data.values():
        if isinstance(value, str) and _looks_like_image_ref(value):
            return value
    return None


def _looks_like_image_ref(value: str) -> bool:
    path = urlparse(value).path.lower()
    return any(path.endswith(suffix) for suffix in (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"))


async def _load_image(settings: PlatformSettings, image_ref: str) -> tuple[bytes, str | None]:
    if image_ref.startswith("data:image/"):
        _header, encoded = image_ref.split(",", 1)
        return base64.b64decode(encoded), None
    if _should_fetch_directly(settings, image_ref):
        try:
            async with httpx.AsyncClient(timeout=settings.label_studio.request_timeout_seconds) as client:
                response = await client.get(image_ref)
                response.raise_for_status()
                return response.content, response.headers.get("content-type")
        except httpx.HTTPError as exc:
            raise PlatformError(
                code="labelstudio_image_download_failed",
                message="The Label Studio prediction adapter could not download the task image.",
                detail=str(exc),
                status_code=502,
                retryable=True,
            ) from exc
    return await asyncio.to_thread(_download_label_studio_asset, settings, image_ref)


def _download_label_studio_asset(settings: PlatformSettings, image_ref: str) -> tuple[bytes, str | None]:
    client = LabelStudioClient(
        base_url=settings.label_studio.base_url,
        token=settings.label_studio.token(),
        timeout_seconds=settings.label_studio.request_timeout_seconds,
    )
    return client.download_asset(image_ref)


def _should_fetch_directly(settings: PlatformSettings, image_ref: str) -> bool:
    parsed = urlparse(image_ref)
    if parsed.scheme not in {"http", "https"}:
        return False
    label_studio_host = urlparse(settings.label_studio.base_url).hostname
    if parsed.hostname in {label_studio_host, "localhost", "127.0.0.1"}:
        return False
    if parsed.path.startswith(("/data/", "/label-studio/data/")):
        return False
    return True


def _file_name_from_ref(image_ref: str) -> str | None:
    path = urlparse(image_ref).path or image_ref
    name = PurePosixPath(path).name
    return name or None


def _label_control(label_config: dict[str, Any] | str | None, task_type: TaskType) -> LabelControl:
    tag_type = {
        TaskType.CLASSIFICATION: "Choices",
        TaskType.ANOMALY: "Choices",
        TaskType.DETECTION: "RectangleLabels",
        TaskType.ORIENTED_DETECTION: "RectangleLabels",
        TaskType.SEGMENTATION: "PolygonLabels",
    }.get(task_type, "Choices")
    result_type = {
        "Choices": "choices",
        "RectangleLabels": "rectanglelabels",
        "PolygonLabels": "polygonlabels",
    }[tag_type]
    value_key = result_type
    default_from_name = {
        TaskType.CLASSIFICATION: "choice",
        TaskType.ANOMALY: "choice",
        TaskType.DETECTION: "label",
        TaskType.ORIENTED_DETECTION: "label",
        TaskType.SEGMENTATION: "polygon",
    }.get(task_type, "choice")
    parsed = _control_from_dict(label_config, tag_type) if isinstance(label_config, dict) else None
    if parsed is not None:
        return LabelControl(parsed[0], parsed[1], result_type, value_key)
    parsed = _control_from_xml(label_config, tag_type) if isinstance(label_config, str) else None
    if parsed is not None:
        return LabelControl(parsed[0], parsed[1], result_type, value_key)
    return LabelControl(default_from_name, "image", result_type, value_key)


def _control_from_dict(label_config: dict[str, Any], tag_type: str) -> tuple[str, str] | None:
    stack: list[tuple[str | None, Any]] = [(None, label_config)]
    while stack:
        key, value = stack.pop()
        if isinstance(value, dict):
            current_type = str(value.get("type") or value.get("$type") or "").lower()
            if current_type == tag_type.lower() or key_matches_tag(key, tag_type):
                name = str(value.get("name") or key or _default_from_name(tag_type))
                to_name = _first_to_name(
                    value.get("toName") or value.get("to_name") or value.get("to_name_list")
                )
                if "," in to_name:
                    to_name = to_name.split(",", 1)[0].strip()
                return name, to_name
            stack.extend((str(child_key), child_value) for child_key, child_value in value.items())
        elif isinstance(value, list):
            stack.extend((None, item) for item in value)
    return None


def _first_to_name(value: Any) -> str:
    if isinstance(value, list):
        first = next((item for item in value if item), None)
        if isinstance(first, dict):
            return str(first.get("name") or first.get("value") or "image")
        return str(first or "image")
    if isinstance(value, dict):
        return str(value.get("name") or value.get("value") or "image")
    return str(value or "image")


def key_matches_tag(key: str | None, tag_type: str) -> bool:
    return bool(key and key.lower() == tag_type.lower())


def _default_from_name(tag_type: str) -> str:
    if tag_type == "RectangleLabels":
        return "label"
    if tag_type == "PolygonLabels":
        return "polygon"
    return "choice"


def _control_from_xml(label_config: str, tag_type: str) -> tuple[str, str] | None:
    pattern = rf"<{tag_type}\b(?P<attrs>[^>]*)>"
    match = re.search(pattern, label_config, flags=re.IGNORECASE)
    if not match:
        return None
    attrs = match.group("attrs")
    name = _xml_attr(attrs, "name") or _default_from_name(tag_type)
    to_name = _xml_attr(attrs, "toName") or "image"
    return name, to_name


def _xml_attr(attrs: str, name: str) -> str | None:
    match = re.search(rf"\b{name}=(['\"])(.*?)\1", attrs, flags=re.IGNORECASE)
    return match.group(2) if match else None


def _classification_result(control: LabelControl, predictions: list[Any]) -> list[dict[str, Any]]:
    if not predictions:
        return []
    top = predictions[0]
    return _choice_result(control, label=str(top.label), score=float(top.score))


def _choice_result(control: LabelControl, *, label: str, score: float) -> list[dict[str, Any]]:
    return [
        {
            "id": _region_id(),
            "from_name": control.from_name,
            "to_name": control.to_name,
            "type": "choices",
            "value": {"choices": [label]},
            "score": round(float(score), 6),
        }
    ]


def _detection_result(
    control: LabelControl,
    detections: list[Any],
    *,
    image_width: int | None,
    image_height: int | None,
) -> list[dict[str, Any]]:
    result = []
    for item in detections:
        value = _rectangle_value(
            item,
            control=control,
            image_width=image_width,
            image_height=image_height,
        )
        result.append(
            {
                "id": _region_id(),
                "from_name": control.from_name,
                "to_name": control.to_name,
                "type": "rectanglelabels",
                "value": value,
                "score": round(float(item.confidence), 6),
            }
        )
    return result


def _rectangle_value(
    item: Any,
    *,
    control: LabelControl,
    image_width: int | None,
    image_height: int | None,
) -> dict[str, Any]:
    oriented_box = getattr(item, "oriented_box", None)
    if oriented_box is not None and len(getattr(oriented_box, "points", []) or []) >= 4:
        width = max(int(image_width or 1), 1)
        height = max(int(image_height or 1), 1)
        points = list(oriented_box.points[:4])
        p0, p1, p2 = points[0], points[1], points[2]
        edge_width = math.dist((p0.x * width, p0.y * height), (p1.x * width, p1.y * height))
        edge_height = math.dist((p1.x * width, p1.y * height), (p2.x * width, p2.y * height))
        rotation = math.degrees(math.atan2((p1.y - p0.y) * height, (p1.x - p0.x) * width))
        return {
            "x": _pct(p0.x),
            "y": _pct(p0.y),
            "width": round(max(edge_width / width, 0.0) * 100.0, 4),
            "height": round(max(edge_height / height, 0.0) * 100.0, 4),
            "rotation": round(_normalize_degrees(rotation), 4),
            control.value_key: [item.label],
        }

    box = item.bounding_box
    return {
        "x": _pct(box.x_min),
        "y": _pct(box.y_min),
        "width": _pct(max(box.x_max - box.x_min, 0.0)),
        "height": _pct(max(box.y_max - box.y_min, 0.0)),
        "rotation": 0,
        control.value_key: [item.label],
    }


def _segmentation_result(control: LabelControl, segments: list[Any]) -> list[dict[str, Any]]:
    result = []
    for item in segments:
        result.append(
            {
                "id": _region_id(),
                "from_name": control.from_name,
                "to_name": control.to_name,
                "type": "polygonlabels",
                "value": {
                    "points": [[_pct(point.x), _pct(point.y)] for point in item.polygon],
                    control.value_key: [item.label],
                },
                "score": round(float(item.confidence), 6),
            }
        )
    return result


def _anomaly_label(predicted_label: str, declared_classes: list[str]) -> str:
    candidates = NORMAL_LABELS if predicted_label == "normal" else ANOMALOUS_LABELS
    for declared in declared_classes:
        if declared.lower() in candidates:
            return declared
    return predicted_label


def _average_score(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pct(value: float) -> float:
    return round(max(0.0, min(float(value), 1.0)) * 100.0, 4)


def _normalize_degrees(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _region_id() -> str:
    return uuid.uuid4().hex[:10]
