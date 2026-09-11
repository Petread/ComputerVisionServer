from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_core import generate_prefixed_id
from cv_utils import utc_now

from cv_db.models import Dataset, DatasetAsset, UploadSession


def create_upload_session(
    session: Session,
    *,
    dataset_id: str,
    expected_file_count: int,
) -> UploadSession:
    dataset = session.get(Dataset, dataset_id)
    if dataset is None:
        raise ValueError("Dataset does not exist.")
    row = UploadSession(
        id=generate_prefixed_id("upl"),
        dataset_id=dataset_id,
        expected_file_count=expected_file_count,
        received_file_count=0,
        total_bytes=0,
    )
    session.add(row)
    session.flush()
    return row


def get_upload_session_record(session: Session, upload_session_id: str) -> UploadSession | None:
    return session.get(UploadSession, upload_session_id)


def update_upload_session(
    session: Session,
    upload_session_id: str,
    *,
    status: str | None = None,
    received_file_count: int | None = None,
    total_bytes: int | None = None,
    content_manifest_sha256: str | None = None,
    mark_completed: bool = False,
) -> UploadSession:
    row = get_upload_session_record(session, upload_session_id)
    if row is None:
        raise ValueError("Upload session does not exist.")
    if status is not None:
        row.status = status
    if received_file_count is not None:
        row.received_file_count = received_file_count
    if total_bytes is not None:
        row.total_bytes = total_bytes
    if content_manifest_sha256 is not None:
        row.content_manifest_sha256 = content_manifest_sha256
    if mark_completed:
        row.completed_at = utc_now()
    session.flush()
    return row


def get_dataset_asset_by_sha256(
    session: Session,
    *,
    dataset_id: str,
    sha256: str,
) -> DatasetAsset | None:
    return session.scalar(
        select(DatasetAsset).where(
            DatasetAsset.dataset_id == dataset_id,
            DatasetAsset.sha256 == sha256,
        )
    )


def create_dataset_asset(
    session: Session,
    *,
    dataset_id: str,
    upload_session_id: str | None,
    relative_path: str,
    sha256: str,
    size_bytes: int,
    mime_type: str,
    width: int | None,
    height: int | None,
) -> DatasetAsset:
    row = DatasetAsset(
        id=generate_prefixed_id("ast"),
        dataset_id=dataset_id,
        upload_session_id=upload_session_id,
        relative_path=relative_path,
        sha256=sha256,
        size_bytes=size_bytes,
        mime_type=mime_type,
        width=width,
        height=height,
    )
    session.add(row)
    session.flush()
    return row
