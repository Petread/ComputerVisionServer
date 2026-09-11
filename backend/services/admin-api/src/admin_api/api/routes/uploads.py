from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from redis import Redis
from rq import Queue, Retry
from sqlalchemy.orm import Session

from admin_api.application.audit import write_admin_audit_event
from admin_api.dependencies import get_settings, require_permission
from cv_contracts import (
    AcceptedJob,
    SessionUser,
    UploadPartReceipt,
    UploadSessionCompleteRequest,
    UploadSessionCreateRequest,
    UploadSessionSummary,
)
from cv_db import (
    create_upload_session,
    get_dataset_record,
    get_label_project_record,
    get_upload_session_record,
    session_scope,
    update_upload_session,
)
from cv_status import QueueName, TaskType, UploadSessionStatus, UserRole
from cv_utils import PlatformError, inspect_image_bytes, sanitize_filename

router = APIRouter(tags=["uploads"])

UploadWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "uploads.write",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.LABEL_MANAGER,
        )
    ),
]
UploadReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "uploads.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.LABEL_MANAGER,
            UserRole.VIEWER,
        )
    ),
]

_UPLOAD_FILENAME_HEADER = "X-Upload-Filename"


def _queue(settings) -> Queue:
    return Queue(QueueName.DATASET.value, connection=Redis.from_url(settings.redis.url()))


@router.post("/datasets/{dataset_id}/upload-sessions", response_model=UploadSessionSummary)
def create_dataset_upload_session(
    dataset_id: str,
    payload: UploadSessionCreateRequest,
    request: Request,
    current_user: UploadWriter,
    settings=Depends(get_settings),
) -> UploadSessionSummary:
    labeling_metadata: dict[str, str] = {}
    with session_scope(settings) as session:
        labeling_metadata = _validate_labeling_metadata(
            session,
            dataset_id=dataset_id,
            payload=payload,
        )
        row = create_upload_session(
            session,
            dataset_id=dataset_id,
            expected_file_count=payload.expected_file_count,
        )
    _part_root, _assembled_root, manifest_path = _upload_paths(
        settings,
        dataset_id=dataset_id,
        upload_session_id=row.id,
    )
    if labeling_metadata:
        _write_manifest(manifest_path, {"parts": {}, "labeling": labeling_metadata})
    item = _summary(row)
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="upload.session.created.v1",
        aggregate_type="upload_session",
        aggregate_id=item.upload_session_id,
        payload={
            "dataset_id": dataset_id,
            "expected_file_count": payload.expected_file_count,
            "labeling": labeling_metadata,
        },
    )
    return item


@router.get("/upload-sessions/{upload_session_id}", response_model=UploadSessionSummary)
def get_upload_session(
    upload_session_id: str,
    _current_user: UploadReader,
    settings=Depends(get_settings),
) -> UploadSessionSummary:
    with session_scope(settings) as session:
        row = get_upload_session_record(session, upload_session_id)
    if row is None:
        raise PlatformError(
            code="upload_session_not_found",
            message="The upload session was not found.",
            detail=upload_session_id,
            status_code=404,
        )
    return _summary(row)


@router.put("/upload-sessions/{upload_session_id}/parts/{part_no}", response_model=UploadPartReceipt)
async def upload_session_part(
    upload_session_id: str,
    part_no: int,
    request: Request,
    current_user: UploadWriter,
    settings=Depends(get_settings),
) -> UploadPartReceipt:
    if part_no < 1:
        raise PlatformError(
            code="upload_part_invalid",
            message="Upload part numbers must start at 1.",
            status_code=422,
        )
    file_name = request.headers.get(_UPLOAD_FILENAME_HEADER)
    if not file_name:
        raise PlatformError(
            code="upload_filename_missing",
            message=f"The {_UPLOAD_FILENAME_HEADER} header is required for upload parts.",
            status_code=422,
        )

    with session_scope(settings) as session:
        row = get_upload_session_record(session, upload_session_id)
    if row is None:
        raise PlatformError(
            code="upload_session_not_found",
            message="The upload session was not found.",
            detail=upload_session_id,
            status_code=404,
        )
    if row.status != UploadSessionStatus.OPEN.value:
        raise PlatformError(
            code="upload_session_closed",
            message="The upload session is no longer accepting new parts.",
            detail=row.status,
            status_code=409,
        )

    raw_body = await request.body()
    if not raw_body:
        raise PlatformError(
            code="upload_part_empty",
            message="Upload parts must not be empty.",
            status_code=422,
        )
    if len(raw_body) > settings.request_limits.upload_body_limit_bytes:
        raise PlatformError(
            code="upload_part_too_large",
            message="The upload part exceeds the configured size limit.",
            status_code=413,
        )

    inspection = inspect_image_bytes(raw_body)
    if inspection.mime_type not in settings.uploads.allowed_mime_types:
        raise PlatformError(
            code="upload_mime_not_allowed",
            message="The uploaded image type is not allowed by the platform policy.",
            detail=inspection.mime_type,
            status_code=415,
        )
    requested_extension = Path(file_name).suffix.lower()
    if requested_extension and requested_extension not in settings.uploads.allowed_extensions:
        raise PlatformError(
            code="upload_extension_not_allowed",
            message="The uploaded filename uses an extension that is not allowed by the platform policy.",
            detail=requested_extension,
            status_code=415,
        )
    declared_content_type = request.headers.get("content-type")
    if declared_content_type and declared_content_type.split(";", maxsplit=1)[0].strip() != inspection.mime_type:
        raise PlatformError(
            code="upload_content_type_mismatch",
            message="The declared Content-Type does not match the uploaded image payload.",
            status_code=415,
        )

    safe_name = sanitize_filename(file_name, max_length=settings.uploads.max_file_name_length)
    suffix = Path(safe_name).suffix.lower() or inspection.extension
    stored_name = f"p{part_no:06d}_{Path(safe_name).stem}{suffix}"
    part_path, assembled_path, manifest_path = _upload_paths(
        settings,
        dataset_id=row.dataset_id,
        upload_session_id=row.id,
        part_no=part_no,
        stored_name=stored_name,
    )
    temp_path = assembled_path.with_suffix(f"{assembled_path.suffix}.part")
    temp_path.write_bytes(raw_body)
    part_path.write_bytes(raw_body)
    temp_path.replace(assembled_path)

    sha256 = hashlib.sha256(raw_body).hexdigest()
    manifest = _read_manifest(manifest_path)
    manifest["parts"][str(part_no)] = {
        "part_no": part_no,
        "file_name": file_name,
        "stored_name": stored_name,
        "size_bytes": len(raw_body),
        "sha256": sha256,
        "mime_type": inspection.mime_type,
        "width": inspection.width,
        "height": inspection.height,
    }
    _write_manifest(manifest_path, manifest)
    received_file_count = len(manifest["parts"])
    total_bytes = sum(int(item["size_bytes"]) for item in manifest["parts"].values())
    if received_file_count > settings.uploads.max_files_per_session:
        raise PlatformError(
            code="upload_session_file_limit_exceeded",
            message="The upload session exceeded the configured file-count limit.",
            status_code=413,
        )
    if total_bytes > settings.uploads.max_total_bytes:
        raise PlatformError(
            code="upload_session_size_limit_exceeded",
            message="The upload session exceeded the configured total-size limit.",
            status_code=413,
        )

    with session_scope(settings) as session:
        update_upload_session(
            session,
            upload_session_id,
            status=UploadSessionStatus.OPEN.value,
            received_file_count=received_file_count,
            total_bytes=total_bytes,
        )

    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="upload.part.received.v1",
        aggregate_type="upload_session",
        aggregate_id=upload_session_id,
        payload={
            "dataset_id": row.dataset_id,
            "part_no": part_no,
            "stored_name": stored_name,
            "size_bytes": len(raw_body),
            "sha256": sha256,
        },
    )
    return UploadPartReceipt(
        upload_session_id=upload_session_id,
        part_no=part_no,
        file_name=file_name,
        stored_name=stored_name,
        mime_type=inspection.mime_type,
        size_bytes=len(raw_body),
        sha256=sha256,
        width=inspection.width,
        height=inspection.height,
    )


@router.post("/upload-sessions/{upload_session_id}:complete", response_model=AcceptedJob, status_code=202)
def complete_upload_session(
    upload_session_id: str,
    payload: UploadSessionCompleteRequest,
    request: Request,
    current_user: UploadWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        row = get_upload_session_record(session, upload_session_id)
        if row is None:
            raise PlatformError(
                code="upload_session_not_found",
                message="The upload session was not found.",
                detail=upload_session_id,
                status_code=404,
            )
        if row.received_file_count != row.expected_file_count:
            raise PlatformError(
                code="upload_session_incomplete",
                message="Not all expected files have been uploaded yet.",
                detail=f"expected={row.expected_file_count}, received={row.received_file_count}",
                status_code=409,
            )
        manifest_checksum = payload.content_manifest_sha256 or _manifest_checksum(
            settings,
            dataset_id=row.dataset_id,
            upload_session_id=row.id,
        )
        update_upload_session(
            session,
            upload_session_id,
            status=UploadSessionStatus.FINALIZING.value,
            content_manifest_sha256=manifest_checksum,
        )
    _queue(settings).enqueue(
        "control_worker.jobs.dataset.finalize_upload_session",
        upload_session_id=upload_session_id,
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    accepted = AcceptedJob(
        job_id=upload_session_id,
        queue=QueueName.DATASET,
        status="accepted",
        accepted_at=_accepted_at(),
    )
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="upload.session.finalize.requested.v1",
        aggregate_type="upload_session",
        aggregate_id=upload_session_id,
        payload={"content_manifest_sha256": payload.content_manifest_sha256},
    )
    return accepted


def _upload_paths(
    settings,
    *,
    dataset_id: str,
    upload_session_id: str,
    part_no: int | None = None,
    stored_name: str | None = None,
) -> tuple[Path, Path, Path] | tuple[Path, Path, Path]:
    parts_root = settings.storage.upload_session_parts_root(dataset_id, upload_session_id)
    assembled_root = settings.storage.upload_session_assembled_root(dataset_id, upload_session_id)
    manifests_root = settings.storage.upload_session_manifests_root(dataset_id, upload_session_id)
    parts_root.mkdir(parents=True, exist_ok=True)
    assembled_root.mkdir(parents=True, exist_ok=True)
    manifests_root.mkdir(parents=True, exist_ok=True)
    if part_no is None or stored_name is None:
        return parts_root, assembled_root, settings.storage.upload_session_manifest(
            dataset_id,
            upload_session_id,
        )
    return (
        parts_root / f"part_{part_no:06d}.bin",
        assembled_root / stored_name,
        settings.storage.upload_session_manifest(dataset_id, upload_session_id),
    )


def _validate_labeling_metadata(
    session: Session,
    *,
    dataset_id: str,
    payload: UploadSessionCreateRequest,
) -> dict[str, str]:
    classification_label = (payload.classification_label or "").strip()
    target_label_project_id = (payload.target_label_project_id or "").strip()
    source_folder_name = (payload.source_folder_name or "").strip()
    if not classification_label and not target_label_project_id:
        return {}
    if not classification_label or not target_label_project_id:
        raise PlatformError(
            code="classified_upload_metadata_incomplete",
            message=(
                "Classified uploads require both a target Label Studio project "
                "and a classification label."
            ),
            status_code=422,
        )
    dataset = get_dataset_record(session, dataset_id)
    label_project = get_label_project_record(session, target_label_project_id)
    if dataset is None:
        raise PlatformError(
            code="dataset_not_found",
            message="The target dataset was not found.",
            detail=dataset_id,
            status_code=404,
        )
    if label_project is None or label_project.dataset_id != dataset_id:
        raise PlatformError(
            code="label_project_not_mapped",
            message="The target Label Studio project is not mapped to this dataset.",
            detail=target_label_project_id,
            status_code=422,
        )
    dataset_is_classification = dataset.task_type == TaskType.CLASSIFICATION.value
    project_is_classification = label_project.task_type == TaskType.CLASSIFICATION.value
    if not dataset_is_classification or not project_is_classification:
        raise PlatformError(
            code="classified_upload_task_type_invalid",
            message="Pre-classified folder uploads are only supported for classification datasets.",
            detail=f"dataset={dataset.task_type}, label_project={label_project.task_type}",
            status_code=422,
        )
    return {
        "mode": "classification",
        "target_label_project_id": target_label_project_id,
        "classification_label": classification_label,
        "source_folder_name": source_folder_name or classification_label,
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"parts": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload.setdefault("parts", {})
        return payload
    return {"parts": {}}


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _manifest_checksum(settings, *, dataset_id: str, upload_session_id: str) -> str:
    manifest_path = settings.storage.upload_session_manifest(dataset_id, upload_session_id)
    manifest_payload = _read_manifest(manifest_path)
    return hashlib.sha256(
        json.dumps(manifest_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _summary(row) -> UploadSessionSummary:
    created_at = row.created_at.isoformat().replace("+00:00", "Z")
    completed_at = row.completed_at.isoformat().replace("+00:00", "Z") if row.completed_at else None
    return UploadSessionSummary(
        upload_session_id=row.id,
        dataset_id=row.dataset_id,
        status=UploadSessionStatus(row.status),
        expected_file_count=row.expected_file_count,
        received_file_count=row.received_file_count,
        total_bytes=row.total_bytes,
        content_manifest_sha256=row.content_manifest_sha256,
        created_at=created_at,
        completed_at=completed_at,
    )


def _accepted_at() -> str:
    from cv_utils import utc_now_iso

    return utc_now_iso()
