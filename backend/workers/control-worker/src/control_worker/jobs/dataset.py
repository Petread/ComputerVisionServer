from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path, PurePosixPath
from shutil import which
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from cv_contracts import LabelProjectProvisionRequest
from cv_core import load_platform_settings
from cv_datasets import (
    DatasetAssetRecord,
    LabelStudioClient,
    LabelStudioError,
    get_annotation_schema,
    materialize_snapshot,
    materialize_temporal_snapshot,
)
from cv_db import (
    create_audit_event,
    create_dataset_asset,
    find_ready_snapshot_by_checksum,
    get_dataset_asset_by_sha256,
    get_dataset_record,
    get_dataset_snapshot_record,
    get_label_project_by_external_id,
    get_label_project_for_dataset_schema,
    get_label_project_record,
    get_upload_session_record,
    list_dataset_assets_for_dataset,
    session_scope,
    update_dataset_snapshot,
    update_upload_session,
    upsert_discovered_label_project,
    upsert_provisioned_label_project,
)
from cv_db.models import (
    Dataset,
    DatasetAsset,
    LabelProject,
    RuntimeCaptureRequest,
)
from cv_status import (
    DatasetStatus,
    QueueName,
    SnapshotStatus,
    TaskType,
    UploadSessionStatus,
    ValidationStatus,
)
from cv_utils import promote_staged_directory, quarantine_path, utc_now_iso
from redis import Redis
from rq import Queue, Retry
from sqlalchemy import select
from sqlalchemy.orm import Session

from control_worker.jobs.pipeline_automation import (
    request_snapshot_after_project_sync,
    request_training_after_snapshot_ready,
)

_PROVISIONING_MARKER_PREFIX = "cv-platform-provisioning:v1:"


def provision_labelstudio_project(
    request_payload: dict[str, Any],
    *,
    requested_by: str = "unknown",
) -> dict[str, Any]:
    payload = LabelProjectProvisionRequest.model_validate(request_payload)
    if payload.train_ratio + payload.val_ratio + payload.test_ratio != 100:
        raise ValueError("Train, validation, and test split ratios must add up to 100.")

    settings = load_platform_settings("control-worker")
    client: LabelStudioClient | None = None
    created_project_id: int | None = None
    recovered_project = False
    marker = _provisioning_marker(payload)
    label_project_id: str | None = None
    stage = "validate_dataset_and_schema"

    try:
        stage = "connect_label_studio"
        client = _label_studio_client(settings)
        stage = "validate_dataset_and_schema"
        with session_scope(settings) as session:
            dataset = session.scalar(
                select(Dataset).where(Dataset.id == payload.dataset_id).with_for_update()
            )
            if dataset is None:
                raise ValueError(f"Dataset {payload.dataset_id} does not exist.")
            try:
                schema = get_annotation_schema(payload.annotation_schema_id)
            except KeyError as exc:
                raise ValueError(
                    f"Annotation schema {payload.annotation_schema_id!r} is not registered."
                ) from exc
            if dataset.task_type != schema.definition.task_type:
                raise ValueError(
                    f"Dataset task type {dataset.task_type!r} is incompatible with annotation "
                    f"schema task type {schema.definition.task_type!r}."
                )
            if dataset.label_schema_version != payload.annotation_schema_id:
                raise ValueError(
                    f"Dataset schema {dataset.label_schema_version!r} does not match requested "
                    f"annotation schema {payload.annotation_schema_id!r}."
                )

            label_config_xml = schema.label_studio_config()
            label_config_sha256 = hashlib.sha256(label_config_xml.encode("utf-8")).hexdigest()
            description = _provisioned_project_description(
                marker=marker,
                dataset_id=dataset.id,
                annotation_schema_id=payload.annotation_schema_id,
            )
            stored_label_config = {
                "raw": label_config_xml,
                "annotation_schema_id": payload.annotation_schema_id,
                "annotation_schema_title": schema.definition.title,
                "target_builder_id": schema.definition.target_builder_id,
                "target_builder_version": schema.definition.target_builder_version,
                "config_sha256": label_config_sha256,
                "provisioning": {
                    "marker_version": "v1",
                    "marker": marker,
                },
            }
            existing_mapping = get_label_project_for_dataset_schema(
                session,
                dataset_id=dataset.id,
                annotation_schema_id=payload.annotation_schema_id,
                include_deleted=True,
            )

            project: dict[str, Any] | None = None
            if existing_mapping is not None:
                stage = "read_existing_project"
                try:
                    project = client.get_project(existing_mapping.label_studio_project_id)
                except LabelStudioError as exc:
                    if exc.status_code != 404:
                        raise

            if project is None and existing_mapping is None:
                stage = "find_recoverable_project"
                matching_projects = [
                    candidate
                    for candidate in client.list_projects()
                    if marker in str(candidate.get("description") or "")
                ]
                if len(matching_projects) > 1:
                    raise ValueError(
                        "More than one Label Studio project carries the provisioning marker; "
                        "automatic recovery is ambiguous."
                    )
                if matching_projects:
                    recovered_project = True
                    project = client.get_project(int(matching_projects[0]["id"]))

            if project is None:
                stage = "create_project"
                project = client.create_project(
                    title=payload.title,
                    label_config=label_config_xml,
                    description=description,
                )
                created_project_id = int(project["id"])
            else:
                stage = "update_project"
                project = client.update_project(
                    int(project["id"]),
                    title=payload.title,
                    label_config=label_config_xml,
                    description=description,
                )

            stage = "persist_mapping"
            summary = upsert_provisioned_label_project(
                session,
                payload=payload,
                label_studio_project_id=int(project["id"]),
                label_config=stored_label_config,
                qualifying_actions=(
                    list(settings.dataset_workflow.automatic_snapshot_actions)
                    if payload.auto_snapshot_enabled
                    else []
                ),
            )
            label_project_id = summary.label_project_id
            create_audit_event(
                session,
                event_type="label_project.provisioned.v1",
                aggregate_type="label_project",
                aggregate_id=summary.label_project_id,
                source_service="control-worker",
                actor_type="user",
                actor_id=requested_by,
                payload={
                    "dataset_id": payload.dataset_id,
                    "annotation_schema_id": payload.annotation_schema_id,
                    "label_config_sha256": label_config_sha256,
                    "label_studio_project_id": summary.label_studio_project_id,
                    "created": created_project_id is not None,
                    "recovered": recovered_project,
                    "auto_sync_enabled": payload.auto_sync_enabled,
                    "auto_snapshot_enabled": payload.auto_snapshot_enabled,
                    "split": {
                        "train": payload.train_ratio,
                        "validation": payload.val_ratio,
                        "test": payload.test_ratio,
                    },
                },
            )

        return {
            "label_project_id": label_project_id,
            "label_studio_project_id": int(project["id"]),
            "dataset_id": payload.dataset_id,
            "annotation_schema_id": payload.annotation_schema_id,
            "created": created_project_id is not None,
            "recovered": recovered_project,
            "status": "provisioned",
            "finished_at": utc_now_iso(),
        }
    except Exception as exc:
        compensation_status = "not_required"
        compensation_error: str | None = None
        if created_project_id is not None and client is not None:
            try:
                client.delete_project(created_project_id)
                compensation_status = "deleted_created_project"
            except Exception as compensation_exc:
                compensation_status = "failed"
                compensation_error = _snapshot_processing_error(compensation_exc)
        _audit_project_provisioning_failure(
            settings,
            payload=payload,
            requested_by=requested_by,
            stage=stage,
            error=exc,
            created_project_id=created_project_id,
            compensation_status=compensation_status,
            compensation_error=compensation_error,
        )
        raise


def sync_labelstudio_projects() -> dict[str, Any]:
    settings = load_platform_settings("control-worker")
    client = _label_studio_client(settings)
    projects = client.list_projects()
    discovered = 0
    updated = 0
    snapshot_ids: list[str] = []
    with session_scope(settings) as session:
        for project in projects:
            existing = get_label_project_by_external_id(session, int(project["id"]))
            row = upsert_discovered_label_project(
                session,
                project_payload=project,
                automatic_snapshot_actions=settings.dataset_workflow.automatic_snapshot_actions,
                minimum_annotation_count=settings.dataset_workflow.minimum_annotation_count,
                required_classes=settings.dataset_workflow.required_classes,
                split_ratios=(
                    settings.dataset_workflow.default_train_ratio,
                    settings.dataset_workflow.default_val_ratio,
                    settings.dataset_workflow.default_test_ratio,
                ),
            )
            if existing is None:
                discovered += 1
            else:
                updated += 1
            snapshot_id = request_snapshot_after_project_sync(
                session,
                label_project=row,
                trigger="catalog_sync",
                reason="Automatic snapshot after project catalog sync",
                created_by="control-worker-sync",
                actor_id="control-worker",
            )
            if snapshot_id:
                snapshot_ids.append(snapshot_id)
        create_audit_event(
            session,
            event_type="labelstudio.projects.synced.v1",
            aggregate_type="system",
            aggregate_id="labelstudio",
            actor_type="system",
            actor_id="control-worker",
            payload={
                "project_count": len(projects),
                "discovered": discovered,
                "updated": updated,
                "snapshot_requests": len(snapshot_ids),
            },
        )
    for snapshot_id in snapshot_ids:
        _dataset_queue(settings).enqueue(
            "control_worker.jobs.dataset.create_labelstudio_snapshot_export",
            snapshot_id=snapshot_id,
            retry=Retry(max=3, interval=[5, 30, 120]),
        )
    return {
        "project_count": len(projects),
        "discovered": discovered,
        "updated": updated,
        "snapshot_requests": len(snapshot_ids),
    }


def finalize_upload_session(upload_session_id: str) -> dict[str, Any]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        row = get_upload_session_record(session, upload_session_id)
        if row is None:
            raise ValueError(f"Upload session {upload_session_id} does not exist.")
        dataset = get_dataset_record(session, row.dataset_id)
        if dataset is None:
            raise ValueError(f"Dataset {row.dataset_id} does not exist.")
        manifest_path = settings.storage.upload_session_manifest(row.dataset_id, row.id)
        if not manifest_path.exists():
            update_upload_session(
                session,
                upload_session_id,
                status=UploadSessionStatus.FAILED.value,
            )
            raise ValueError(f"Upload session {upload_session_id} has no manifest to finalize.")

    manifest = _read_json(manifest_path)
    parts = manifest.get("parts") if isinstance(manifest, dict) else None
    if not isinstance(parts, dict) or not parts:
        raise ValueError(f"Upload session {upload_session_id} has no uploaded parts.")

    with session_scope(settings) as session:
        row = get_upload_session_record(session, upload_session_id)
        assert row is not None
        dataset = get_dataset_record(session, row.dataset_id)
        assert dataset is not None
        asset_root = settings.storage.dataset_asset_root(row.dataset_id)
        asset_root.mkdir(parents=True, exist_ok=True)
        imported_count = 0
        for item in sorted(parts.values(), key=lambda candidate: int(candidate["part_no"])):
            sha256 = str(item["sha256"])
            stored_name = str(item["stored_name"])
            assembled_path = settings.storage.upload_session_assembled_root(row.dataset_id, row.id) / stored_name
            if not assembled_path.exists():
                raise ValueError(
                    f"Upload session {upload_session_id} is missing assembled file {stored_name}."
                )
            suffix = Path(stored_name).suffix.lower()
            relative_path = Path(sha256[:2]) / f"{sha256}{suffix}"
            final_path = asset_root / relative_path
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if not final_path.exists():
                shutil.copy2(assembled_path, final_path)
            existing = get_dataset_asset_by_sha256(session, dataset_id=row.dataset_id, sha256=sha256)
            if existing is None:
                create_dataset_asset(
                    session,
                    dataset_id=row.dataset_id,
                    upload_session_id=row.id,
                    relative_path=relative_path.as_posix(),
                    sha256=sha256,
                    size_bytes=int(item["size_bytes"]),
                    mime_type=str(item["mime_type"]),
                    width=int(item["width"]) if item.get("width") is not None else None,
                    height=int(item["height"]) if item.get("height") is not None else None,
                )
                imported_count += 1

        dataset.status = DatasetStatus.READY.value
        update_upload_session(
            session,
            upload_session_id,
            status=UploadSessionStatus.COMPLETED.value,
            content_manifest_sha256=_manifest_sha256(manifest),
            mark_completed=True,
        )
        create_audit_event(
            session,
            event_type="upload.session.completed.v1",
            aggregate_type="upload_session",
            aggregate_id=upload_session_id,
            actor_type="system",
            actor_id="control-worker",
            payload={
                "dataset_id": row.dataset_id,
                "imported_asset_count": imported_count,
                "received_file_count": row.received_file_count,
            },
        )
        has_label_project = session.scalar(
            select(LabelProject.id).where(LabelProject.dataset_id == row.dataset_id)
        )

    if has_label_project:
        _dataset_queue(settings).enqueue(
            "control_worker.jobs.dataset.sync_uploaded_assets_to_labelstudio",
            upload_session_id=upload_session_id,
            retry=Retry(max=3, interval=[5, 30, 120]),
        )
    return {
        "upload_session_id": upload_session_id,
        "status": UploadSessionStatus.COMPLETED.value,
        "finished_at": utc_now_iso(),
    }


def sync_uploaded_assets_to_labelstudio(upload_session_id: str) -> dict[str, Any]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        row = get_upload_session_record(session, upload_session_id)
        if row is None:
            raise ValueError(f"Upload session {upload_session_id} does not exist.")
        manifest_path = settings.storage.upload_session_manifest(row.dataset_id, row.id)
        labeling_metadata = _upload_labeling_metadata(manifest_path)
        label_projects = session.scalars(
            select(LabelProject).where(LabelProject.dataset_id == row.dataset_id)
        ).all()
        target_label_project_id = labeling_metadata.get("target_label_project_id")
        if target_label_project_id:
            label_projects = [
                project for project in label_projects if project.id == target_label_project_id
            ]
        if not label_projects:
            status = "no_target_label_project" if target_label_project_id else "no_active_label_project"
            return {
                "upload_session_id": upload_session_id,
                "status": status,
                "finished_at": utc_now_iso(),
            }
        assets = _assets_for_upload_session(
            session,
            dataset_id=row.dataset_id,
            upload_session_id=upload_session_id,
            manifest_path=manifest_path,
        )

    imported = 0
    for label_project in label_projects:
        tasks = []
        classification_label = labeling_metadata.get("classification_label")
        classification_control = (
            _classification_control(label_project.label_config)
            if classification_label and label_project.task_type == TaskType.CLASSIFICATION.value
            else None
        )
        for asset in assets:
            image_url = _label_studio_local_file_url(
                settings,
                dataset_id=asset.dataset_id,
                relative_path=asset.relative_path,
            )
            task = {
                "data": {"image": image_url},
                "meta": {
                    "asset_id": asset.id,
                    "dataset_id": asset.dataset_id,
                    "upload_session_id": upload_session_id,
                },
            }
            if classification_label and classification_control is not None:
                task["annotations"] = [
                    _classification_annotation(
                        label=classification_label,
                        from_name=classification_control[0],
                        to_name=classification_control[1],
                    )
                ]
                task["meta"]["classification_label"] = classification_label
                task["meta"]["source_folder_name"] = labeling_metadata.get(
                    "source_folder_name",
                    classification_label,
                )
            tasks.append(task)
        if not tasks:
            continue
        client = _label_studio_client(settings)
        client.import_tasks(label_project.label_studio_project_id, tasks)
        imported += len(tasks)

    with session_scope(settings) as session:
        create_audit_event(
            session,
            event_type="upload.session.synced_to_labelstudio.v1",
            aggregate_type="upload_session",
            aggregate_id=upload_session_id,
            actor_type="system",
            actor_id="control-worker",
            payload={
                "imported_task_count": imported,
                "classification_label": labeling_metadata.get("classification_label"),
                "target_label_project_id": labeling_metadata.get("target_label_project_id"),
            },
        )
    return {
        "upload_session_id": upload_session_id,
        "status": "synced",
        "imported_task_count": imported,
        "finished_at": utc_now_iso(),
    }


def create_labelstudio_snapshot_export(snapshot_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        snapshot = get_dataset_snapshot_record(session, snapshot_id)
        if snapshot is None:
            raise ValueError(f"Dataset snapshot {snapshot_id} does not exist.")
        if snapshot.status == SnapshotStatus.READY.value:
            return {"snapshot_id": snapshot_id, "status": "ready", "finished_at": utc_now_iso()}
        label_project = get_label_project_record(session, snapshot.label_project_id or "")
        dataset = get_dataset_record(session, snapshot.dataset_id)
        if label_project is None or dataset is None:
            update_dataset_snapshot(
                session,
                snapshot_id,
                status=SnapshotStatus.FAILED,
                processing_error="The dataset or Label Studio project mapping required for this snapshot no longer exists.",
            )
            raise ValueError(
                f"Snapshot {snapshot_id} is missing its dataset or Label Studio mapping."
            )

        export_root = settings.storage.labelstudio_project_export_root(
            label_project.id, snapshot_id
        )
        export_root.mkdir(parents=True, exist_ok=True)
        metadata_path = export_root / "export_metadata.json"
        client = _label_studio_client(settings)
        if metadata_path.exists():
            metadata = _read_json(metadata_path)
        else:
            metadata = client.create_export_snapshot(label_project.label_studio_project_id)
            _write_json(metadata_path, metadata)

        update_dataset_snapshot(
            session,
            snapshot_id,
            status=SnapshotStatus.EXPORTING,
            export_storage_uri=str(export_root),
            processing_error=None,
        )
        create_audit_event(
            session,
            event_type="dataset.snapshot.export.requested.v1",
            aggregate_type="dataset_snapshot",
            aggregate_id=snapshot_id,
            actor_type="system",
            actor_id="control-worker",
            payload={"label_project_id": label_project.id, "export_root": str(export_root)},
        )

    _dataset_queue(settings).enqueue(
        "control_worker.jobs.dataset.poll_labelstudio_snapshot_export",
        snapshot_id=snapshot_id,
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    return {"snapshot_id": snapshot_id, "status": "export_requested", "finished_at": utc_now_iso()}


def poll_labelstudio_snapshot_export(snapshot_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        snapshot = get_dataset_snapshot_record(session, snapshot_id)
        if snapshot is None:
            raise ValueError(f"Dataset snapshot {snapshot_id} does not exist.")
        label_project = get_label_project_record(session, snapshot.label_project_id or "")
        if label_project is None:
            update_dataset_snapshot(
                session,
                snapshot_id,
                status=SnapshotStatus.FAILED,
                processing_error="The Label Studio project mapping required for this snapshot no longer exists.",
            )
            raise ValueError(f"Snapshot {snapshot_id} is missing its Label Studio mapping.")
        export_root = Path(
            snapshot.export_storage_uri
            or settings.storage.labelstudio_project_export_root(label_project.id, snapshot_id)
        )
        metadata_path = export_root / "export_metadata.json"
        if not metadata_path.exists():
            raise ValueError(f"Snapshot {snapshot_id} has no export metadata to poll.")
        metadata = _read_json(metadata_path)
        export_id = int(metadata["id"])

    client = _label_studio_client(settings)
    metadata = client.get_export_snapshot(label_project.label_studio_project_id, export_id)
    _write_json(metadata_path, metadata)
    export_status = str(metadata.get("status") or "").lower()
    if export_status in {"created", "in_progress"}:
        _dataset_queue(settings).enqueue_in(
            timedelta(seconds=settings.label_studio.export_poll_interval_seconds),
            "control_worker.jobs.dataset.poll_labelstudio_snapshot_export",
            snapshot_id=snapshot_id,
            retry=Retry(max=3, interval=[5, 30, 120]),
        )
        return {"snapshot_id": snapshot_id, "status": export_status, "finished_at": utc_now_iso()}
    if export_status == "failed":
        error_message = _extract_export_error(metadata)
        with session_scope(settings) as session:
            update_dataset_snapshot(
                session,
                snapshot_id,
                status=SnapshotStatus.FAILED,
                processing_error=error_message,
            )
        return {"snapshot_id": snapshot_id, "status": "failed", "finished_at": utc_now_iso()}

    export_format = str(snapshot.export_format).upper()
    if export_format != "JSON":
        with session_scope(settings) as session:
            update_dataset_snapshot(
                session,
                snapshot_id,
                status=SnapshotStatus.FAILED,
                processing_error="Only Label Studio JSON exports are currently supported for immutable snapshot creation.",
            )
        return {"snapshot_id": snapshot_id, "status": "failed", "finished_at": utc_now_iso()}

    export_bytes = client.download_export_snapshot(
        label_project.label_studio_project_id,
        export_id,
        export_type=export_format,
    )
    export_root.mkdir(parents=True, exist_ok=True)
    temp_export_path = export_root / ".labelstudio-export.json.part"
    if temp_export_path.exists():
        quarantine_path(
            temp_export_path,
            quarantine_root=settings.storage.quarantine_category_root("snapshot-exports"),
            label="partial_export_download",
        )
    temp_export_path.write_bytes(export_bytes)
    temp_export_path.replace(export_root / "labelstudio-export.json")
    _dataset_queue(settings).enqueue(
        "control_worker.jobs.dataset.materialize_dataset_snapshot",
        snapshot_id=snapshot_id,
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    return {"snapshot_id": snapshot_id, "status": "downloaded", "finished_at": utc_now_iso()}


def materialize_dataset_snapshot(snapshot_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        snapshot = get_dataset_snapshot_record(session, snapshot_id)
        if snapshot is None:
            raise ValueError(f"Dataset snapshot {snapshot_id} does not exist.")
        dataset = get_dataset_record(session, snapshot.dataset_id)
        label_project = get_label_project_record(session, snapshot.label_project_id or "")
        if dataset is None or label_project is None:
            update_dataset_snapshot(
                session,
                snapshot_id,
                status=SnapshotStatus.FAILED,
                processing_error="The dataset or Label Studio project mapping required for this snapshot no longer exists.",
            )
            raise ValueError(
                f"Snapshot {snapshot_id} is missing its dataset or Label Studio mapping."
            )
        update_dataset_snapshot(session, snapshot_id, status=SnapshotStatus.MATERIALIZING)

        export_root = Path(
            snapshot.export_storage_uri
            or settings.storage.labelstudio_project_export_root(label_project.id, snapshot_id)
        )
        export_path = export_root / "labelstudio-export.json"
        if not export_path.exists():
            update_dataset_snapshot(
                session,
                snapshot_id,
                status=SnapshotStatus.FAILED,
                processing_error="The Label Studio export file is missing from durable storage.",
            )
            raise ValueError(f"Snapshot {snapshot_id} has no downloaded export file.")
        temporal_manifest_paths = (
            _temporal_manifest_paths_for_project(session, label_project)
            if dataset.task_type == TaskType.TEMPORAL_SEQUENCE.value
            else {}
        )

    snapshot_root = settings.storage.snapshot_root(snapshot_id)
    temp_root = snapshot_root.parent / f".tmp-{snapshot_root.name}"
    export_payload = _load_export_tasks(export_path)
    try:
        if dataset.task_type != TaskType.TEMPORAL_SEQUENCE.value:
            _hydrate_label_studio_assets(
                settings,
                dataset_id=dataset.id,
                export_payload=export_payload,
            )
            with session_scope(settings) as session:
                assets = _resolve_asset_paths(
                    list_dataset_assets_for_dataset(session, dataset.id),
                    settings,
                    dataset_id=dataset.id,
                )
        else:
            assets = []
        if temp_root.exists():
            quarantine_path(
                temp_root,
                quarantine_root=settings.storage.quarantine_category_root(
                    "snapshot-materialization"
                ),
                label="partial_snapshot_materialization",
            )
        if snapshot_root.exists():
            _dataset_queue(settings).enqueue(
                "control_worker.jobs.dataset.validate_dataset_snapshot",
                snapshot_id=snapshot_id,
                retry=Retry(max=3, interval=[5, 30, 120]),
            )
            return {
                "snapshot_id": snapshot_id,
                "status": "already_materialized",
                "finished_at": utc_now_iso(),
            }

        if dataset.task_type == TaskType.TEMPORAL_SEQUENCE.value:
            artifacts = materialize_temporal_snapshot(
                snapshot_id=snapshot_id,
                dataset_id=dataset.id,
                export_format=snapshot.export_format,
                annotation_schema_id=dataset.label_schema_version,
                project_label_schema_version=label_project.label_schema_version,
                minimum_annotation_count=label_project.minimum_annotation_count,
                train_ratio=label_project.train_ratio,
                val_ratio=label_project.val_ratio,
                test_ratio=label_project.test_ratio,
                export_payload=export_payload,
                manifest_paths_by_task_id=temporal_manifest_paths,
                target_root=temp_root,
            )
        else:
            artifacts = materialize_snapshot(
                snapshot_id=snapshot_id,
                dataset_id=dataset.id,
                task_type=dataset.task_type,
                export_format=snapshot.export_format,
                label_schema_version=dataset.label_schema_version,
                project_label_schema_version=label_project.label_schema_version,
                label_config=label_project.label_config,
                required_classes=label_project.required_classes or [],
                minimum_annotation_count=label_project.minimum_annotation_count,
                train_ratio=label_project.train_ratio,
                val_ratio=label_project.val_ratio,
                test_ratio=label_project.test_ratio,
                export_payload=export_payload,
                assets=assets,
                target_root=temp_root,
            )
        snapshot_root.parent.mkdir(parents=True, exist_ok=True)
        promote_staged_directory(source=temp_root, destination=snapshot_root)
    except Exception as exc:
        with session_scope(settings) as session:
            update_dataset_snapshot(
                session,
                snapshot_id,
                status=SnapshotStatus.FAILED,
                processing_error=_snapshot_processing_error(exc),
            )
        quarantine_path(
            temp_root,
            quarantine_root=settings.storage.quarantine_category_root("snapshot-materialization"),
            label="failed_snapshot_materialization",
        )
        raise

    with session_scope(settings) as session:
        update_dataset_snapshot(
            session,
            snapshot_id,
            status=SnapshotStatus.VALIDATING,
            item_count=len(artifacts.canonical_dataset.items),
            annotation_count=artifacts.canonical_dataset.annotation_count,
            eligible_item_count=artifacts.canonical_dataset.eligible_item_count,
            submitted_labeled_item_count=artifacts.canonical_dataset.submitted_labeled_item_count,
            submitted_background_item_count=artifacts.canonical_dataset.submitted_background_item_count,
            draft_item_count=artifacts.canonical_dataset.draft_item_count,
            invalid_item_count=artifacts.canonical_dataset.invalid_item_count,
            manifest_uri=str(snapshot_root / "snapshot_manifest.json"),
            content_sha256=artifacts.content_sha256,
            split_metadata=artifacts.split_summary.model_dump(),
            validation_report={
                **artifacts.validation.model_dump(),
                "checked_at": utc_now_iso(),
            },
            validation_status=ValidationStatus(artifacts.validation.status),
            statistics={
                "class_distribution": artifacts.canonical_dataset.class_distribution,
                "source_task_count": artifacts.canonical_dataset.source_task_count,
                "eligible_item_count": artifacts.canonical_dataset.eligible_item_count,
                "submitted_labeled_item_count": artifacts.canonical_dataset.submitted_labeled_item_count,
                "submitted_background_item_count": artifacts.canonical_dataset.submitted_background_item_count,
                "draft_item_count": artifacts.canonical_dataset.draft_item_count,
                "invalid_item_count": artifacts.canonical_dataset.invalid_item_count,
            },
            processing_error=None,
        )
        label_project = get_label_project_record(session, snapshot.label_project_id or "")
        if label_project is not None:
            label_project.task_count = int(artifacts.canonical_dataset.source_task_count or 0)
            label_project.annotated_task_count = int(
                artifacts.canonical_dataset.submitted_labeled_item_count
                + artifacts.canonical_dataset.submitted_background_item_count
            )
            label_project.annotation_count = int(artifacts.canonical_dataset.annotation_count or 0)
            label_project.draft_task_count = int(artifacts.canonical_dataset.draft_item_count or 0)
            label_project.eligible_task_count = int(artifacts.canonical_dataset.eligible_item_count or 0)
            label_project.submitted_labeled_task_count = int(
                artifacts.canonical_dataset.submitted_labeled_item_count or 0
            )
            label_project.submitted_background_task_count = int(
                artifacts.canonical_dataset.submitted_background_item_count or 0
            )
            label_project.invalid_task_count = int(artifacts.canonical_dataset.invalid_item_count or 0)
            label_project.class_distribution = dict(artifacts.canonical_dataset.class_distribution or {})
    _dataset_queue(settings).enqueue(
        "control_worker.jobs.dataset.validate_dataset_snapshot",
        snapshot_id=snapshot_id,
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    return {"snapshot_id": snapshot_id, "status": "materialized", "finished_at": utc_now_iso()}


def validate_dataset_snapshot(snapshot_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        snapshot = get_dataset_snapshot_record(session, snapshot_id)
        if snapshot is None:
            raise ValueError(f"Dataset snapshot {snapshot_id} does not exist.")
        validation_status = ValidationStatus(
            snapshot.validation_status or ValidationStatus.PENDING.value
        )
        if validation_status == ValidationStatus.FAILED:
            update_dataset_snapshot(
                session,
                snapshot_id,
                status=SnapshotStatus.FAILED,
                processing_error="This snapshot failed one or more dataset quality gates. Review the validation report before training.",
            )
            create_audit_event(
                session,
                event_type="dataset.snapshot.validation_failed.v1",
                aggregate_type="dataset_snapshot",
                aggregate_id=snapshot_id,
                actor_type="system",
                actor_id="control-worker",
                payload={"validation_status": validation_status.value},
            )
            return {"snapshot_id": snapshot_id, "status": "failed", "finished_at": utc_now_iso()}

    _dataset_queue(settings).enqueue(
        "control_worker.jobs.dataset.dvc_track_dataset_snapshot",
        snapshot_id=snapshot_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=Retry(max=3, interval=[5, 30, 120]),
    )
    return {"snapshot_id": snapshot_id, "status": "validated", "finished_at": utc_now_iso()}


def dvc_track_dataset_snapshot(snapshot_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    automatic_training_job_id = None
    with session_scope(settings) as session:
        snapshot = get_dataset_snapshot_record(session, snapshot_id)
        if snapshot is None:
            raise ValueError(f"Dataset snapshot {snapshot_id} does not exist.")
        snapshot_root = settings.storage.snapshot_root(snapshot_id)
        if not snapshot_root.exists():
            update_dataset_snapshot(
                session,
                snapshot_id,
                status=SnapshotStatus.FAILED,
                processing_error="The materialized snapshot directory is missing from storage.",
            )
            raise ValueError(f"Snapshot {snapshot_id} has no materialized snapshot directory.")

    tracked_root = Path(settings.dvc.repo_root) / "snapshots" / snapshot_root.name
    tracked_root.parent.mkdir(parents=True, exist_ok=True)
    if tracked_root.exists():
        shutil.rmtree(tracked_root)
    shutil.copytree(snapshot_root, tracked_root)

    dvc_rev = snapshot.content_sha256 or snapshot_id
    manifest_root = Path(settings.dvc.repo_root) / "manifests"
    manifest_root.mkdir(parents=True, exist_ok=True)
    tracking_manifest = {
        "snapshot_id": snapshot_id,
        "snapshot_root": str(snapshot_root),
        "tracked_root": str(tracked_root),
        "content_checksum": snapshot.content_sha256,
        "tracked_at": utc_now_iso(),
    }
    _write_json(manifest_root / f"{snapshot_id}.json", tracking_manifest)

    if which("dvc"):
        _dvc_track_path(settings, tracked_root)

    with session_scope(settings) as session:
        duplicate = find_ready_snapshot_by_checksum(
            session,
            dataset_id=snapshot.dataset_id,
            content_sha256=snapshot.content_sha256 or "",
            exclude_snapshot_id=snapshot_id,
        )
        update_dataset_snapshot(
            session,
            snapshot_id,
            status=SnapshotStatus.READY,
            dvc_rev=dvc_rev,
            processing_error=None,
        )
        create_audit_event(
            session,
            event_type="dataset.snapshot.created.v1",
            aggregate_type="dataset_snapshot",
            aggregate_id=snapshot_id,
            actor_type="system",
            actor_id="control-worker",
            payload={
                "content_checksum": snapshot.content_sha256,
                "dvc_rev": dvc_rev,
                "duplicate_of_snapshot_id": duplicate.id if duplicate else None,
            },
        )
        automatic_training_job_id = request_training_after_snapshot_ready(
            session,
            snapshot_id=snapshot_id,
        )
    if automatic_training_job_id:
        Queue(QueueName.TRAINING.value, connection=Redis.from_url(settings.redis.url())).enqueue(
            "training_worker.jobs.run_training_job",
            training_job_id=automatic_training_job_id,
            job_timeout=settings.training.job_timeout_seconds,
            retry=Retry(max=settings.training.safe_retry_limit, interval=[5, 30, 120]),
        )
    return {
        "snapshot_id": snapshot_id,
        "status": "ready",
        "automatic_training_job_id": automatic_training_job_id or "",
        "finished_at": utc_now_iso(),
    }


def _dataset_queue(settings) -> Queue:
    return Queue(QueueName.DATASET.value, connection=Redis.from_url(settings.redis.url()))


def _label_studio_client(settings) -> LabelStudioClient:
    token = settings.label_studio.token()
    if not token:
        raise LabelStudioError("The Label Studio API token is not configured.")
    return LabelStudioClient(
        base_url=settings.label_studio.base_url,
        token=token,
        timeout_seconds=settings.label_studio.request_timeout_seconds,
    )


def _provisioning_marker(payload: LabelProjectProvisionRequest) -> str:
    fingerprint = {
        "dataset_id": payload.dataset_id,
        "annotation_schema_id": payload.annotation_schema_id,
        "idempotency_key": payload.idempotency_key or "dataset-schema-default",
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{_PROVISIONING_MARKER_PREFIX}{digest}"


def _provisioned_project_description(
    *,
    marker: str,
    dataset_id: str,
    annotation_schema_id: str,
) -> str:
    return (
        "Provisioned by the inference server. The marker is used for idempotent recovery; "
        "do not remove it.\n\n"
        f"{marker}\n"
        f"dataset={dataset_id}\n"
        f"annotation_schema={annotation_schema_id}"
    )


def _audit_project_provisioning_failure(
    settings,
    *,
    payload: LabelProjectProvisionRequest,
    requested_by: str,
    stage: str,
    error: Exception,
    created_project_id: int | None,
    compensation_status: str,
    compensation_error: str | None,
) -> None:
    try:
        with session_scope(settings) as session:
            create_audit_event(
                session,
                event_type="label_project.provisioning_failed.v1",
                aggregate_type="dataset",
                aggregate_id=payload.dataset_id,
                source_service="control-worker",
                actor_type="user",
                actor_id=requested_by,
                payload={
                    "annotation_schema_id": payload.annotation_schema_id,
                    "stage": stage,
                    "error": _snapshot_processing_error(error),
                    "created_label_studio_project_id": created_project_id,
                    "compensation_status": compensation_status,
                    "compensation_error": compensation_error,
                },
            )
    except Exception:
        # The original provisioning error must remain the RQ retry cause. Audit export/database
        # failures are already surfaced by the platform audit persistence path.
        return


def _label_studio_local_file_url(settings, *, dataset_id: str, relative_path: str) -> str:
    asset_path = settings.storage.dataset_asset_root(dataset_id) / relative_path
    configured_root = Path(settings.label_studio.local_files_root or settings.storage.state_root).resolve()
    state_root = Path(settings.storage.state_root).resolve()
    local_root = configured_root if asset_path.resolve().is_relative_to(configured_root) else state_root
    relative_to_local_root = asset_path.resolve().relative_to(local_root)
    return f"{settings.label_studio.local_files_url_prefix}{relative_to_local_root.as_posix()}"


def _upload_labeling_metadata(manifest_path: Path) -> dict[str, str]:
    if not manifest_path.exists():
        return {}
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        return {}
    labeling = manifest.get("labeling")
    if not isinstance(labeling, dict):
        return {}
    return {
        str(key): str(value).strip()
        for key, value in labeling.items()
        if isinstance(key, str) and value is not None and str(value).strip()
    }


def _assets_for_upload_session(
    session: Any,
    *,
    dataset_id: str,
    upload_session_id: str,
    manifest_path: Path,
) -> list[DatasetAsset]:
    manifest_assets: list[DatasetAsset] = []
    seen_asset_ids: set[str] = set()
    if manifest_path.exists():
        manifest = _read_json(manifest_path)
        parts = manifest.get("parts") if isinstance(manifest, dict) else None
        if isinstance(parts, dict):
            for item in sorted(parts.values(), key=lambda candidate: int(candidate["part_no"])):
                if not isinstance(item, dict):
                    continue
                sha256 = item.get("sha256")
                if not isinstance(sha256, str):
                    continue
                asset = get_dataset_asset_by_sha256(
                    session,
                    dataset_id=dataset_id,
                    sha256=sha256,
                )
                if asset is None or asset.id in seen_asset_ids:
                    continue
                manifest_assets.append(asset)
                seen_asset_ids.add(asset.id)
    if manifest_assets:
        return manifest_assets
    return list(
        session.scalars(
            select(DatasetAsset).where(DatasetAsset.upload_session_id == upload_session_id)
        ).all()
    )


def _classification_annotation(*, label: str, from_name: str, to_name: str) -> dict[str, Any]:
    return {
        "ground_truth": True,
        "was_cancelled": False,
        "result": [
            {
                "from_name": from_name,
                "to_name": to_name,
                "type": "choices",
                "value": {"choices": [label]},
            }
        ],
    }


def _classification_control(label_config: dict[str, Any] | None) -> tuple[str, str]:
    if isinstance(label_config, dict):
        parsed = _classification_control_from_dict(label_config)
        if parsed is not None:
            return parsed
        raw = label_config.get("raw")
        if isinstance(raw, str):
            parsed = _classification_control_from_xml(raw)
            if parsed is not None:
                return parsed
    return "choice", "image"


def _classification_control_from_dict(label_config: dict[str, Any]) -> tuple[str, str] | None:
    stack: list[tuple[str | None, Any]] = [(None, label_config)]
    while stack:
        key, value = stack.pop()
        if isinstance(value, dict):
            current_type = str(value.get("type") or value.get("$type") or "").lower()
            if current_type == "choices" or (key and key.lower() == "choices"):
                return (
                    str(value.get("name") or key or "choice"),
                    _first_to_name(
                        value.get("toName") or value.get("to_name") or value.get("inputs")
                    ),
                )
            stack.extend((str(child_key), child_value) for child_key, child_value in value.items())
        elif isinstance(value, list):
            stack.extend((None, item) for item in value)
    return None


def _classification_control_from_xml(label_config: str) -> tuple[str, str] | None:
    match = re.search(r"<Choices\b(?P<attrs>[^>]*)>", label_config, flags=re.IGNORECASE)
    if not match:
        return None
    attrs = match.group("attrs")
    return _xml_attr(attrs, "name") or "choice", _xml_attr(attrs, "toName") or "image"


def _first_to_name(value: Any) -> str:
    if isinstance(value, list):
        first = next((item for item in value if item), None)
        if isinstance(first, dict):
            return str(first.get("name") or first.get("value") or "image")
        return str(first or "image")
    if isinstance(value, dict):
        return str(value.get("name") or value.get("value") or "image")
    return str(value or "image")


def _xml_attr(attrs: str, name: str) -> str | None:
    match = re.search(rf"\b{name}=(['\"])(.*?)\1", attrs, flags=re.IGNORECASE)
    return match.group(2) if match else None


def _extract_export_error(metadata: dict[str, Any]) -> str:
    converted_formats = metadata.get("converted_formats")
    if isinstance(converted_formats, list):
        for candidate in converted_formats:
            if isinstance(candidate, dict) and candidate.get("traceback"):
                return str(candidate["traceback"])
    return "Label Studio could not complete the snapshot export."


def _load_export_tasks(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("tasks"), list):
            return [item for item in payload["tasks"] if isinstance(item, dict)]
        if isinstance(payload.get("annotations"), list):
            return [item for item in payload["annotations"] if isinstance(item, dict)]
    raise ValueError("The downloaded Label Studio export does not contain a supported task list.")


def _temporal_manifest_paths_for_project(
    session: Session,
    label_project: LabelProject,
) -> dict[str, Path]:
    rows = session.scalars(
        select(RuntimeCaptureRequest).where(
            RuntimeCaptureRequest.target_label_project_id == label_project.id,
            RuntimeCaptureRequest.label_studio_project_id
            == label_project.label_studio_project_id,
            RuntimeCaptureRequest.source_type == "temporal_sample",
            RuntimeCaptureRequest.asset_uri.is_not(None),
        )
    ).all()
    manifests: dict[str, Path] = {}
    for row in rows:
        if not row.asset_uri:
            continue
        manifest_path = Path(row.asset_uri)
        for task_id in row.label_studio_task_ids or []:
            key = str(task_id)
            previous = manifests.get(key)
            if previous is not None and previous != manifest_path:
                raise ValueError(
                    "More than one temporal sample manifest is mapped to Label Studio task "
                    f"{key}."
                )
            manifests[key] = manifest_path
    return manifests


def _resolve_asset_paths(assets: list[Any], settings, *, dataset_id: str) -> list[Any]:
    resolved = []
    asset_root = settings.storage.dataset_asset_root(dataset_id)
    for asset in assets:
        relative_path = Path(asset.relative_path)
        source_path = relative_path if relative_path.is_absolute() else asset_root / relative_path
        resolved.append(asset.model_copy(update={"source_path": str(source_path)}))
    return resolved


def _hydrate_label_studio_assets(settings, *, dataset_id: str, export_payload: list[dict[str, Any]]) -> None:
    with session_scope(settings) as session:
        existing_assets = _resolve_asset_paths(
            list_dataset_assets_for_dataset(session, dataset_id),
            settings,
            dataset_id=dataset_id,
        )

    unique_asset_refs = []
    seen_refs: set[str] = set()
    for task in export_payload:
        asset_ref = _task_asset_reference(task)
        if not asset_ref or asset_ref in seen_refs:
            continue
        seen_refs.add(asset_ref)
        unique_asset_refs.append(asset_ref)

    if not unique_asset_refs:
        return

    exact_assets = {
        PurePosixPath(asset.relative_path).as_posix(): asset for asset in existing_assets
    }
    assets_by_basename: dict[str, list[DatasetAssetRecord]] = {}
    for asset in existing_assets:
        assets_by_basename.setdefault(Path(asset.relative_path).name, []).append(asset)

    client = None
    asset_root = settings.storage.dataset_asset_root(dataset_id)
    asset_root.mkdir(parents=True, exist_ok=True)

    for asset_ref in unique_asset_refs:
        normalized_ref = _normalize_label_studio_asset_reference(asset_ref)
        matched_asset = _match_cached_asset(
            normalized_ref=normalized_ref,
            exact_assets=exact_assets,
            assets_by_basename=assets_by_basename,
        )
        if matched_asset is not None and Path(matched_asset.source_path).exists():
            continue

        local_source_path = _local_label_studio_asset_path(settings, asset_ref)
        if local_source_path is not None:
            content = local_source_path.read_bytes()
            content_type = None
        else:
            client = client or _label_studio_client(settings)
            try:
                content, content_type = client.download_asset(asset_ref)
            except LabelStudioError as exc:
                raise LabelStudioError(
                    "Unable to hydrate Label Studio asset "
                    f"{asset_ref!r} (normalized: {normalized_ref!r}): {exc}"
                ) from exc
        relative_path = _cached_asset_relative_path(normalized_ref)
        final_path = asset_root / Path(relative_path)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(content)

        sha256 = hashlib.sha256(content).hexdigest()
        cached_asset = DatasetAssetRecord(
            asset_id=f"cached:{dataset_id}:{relative_path}",
            relative_path=relative_path,
            sha256=sha256,
            source_path=str(final_path),
        )

        with session_scope(settings) as session:
            current_assets = {
                item.relative_path: item
                for item in list_dataset_assets_for_dataset(session, dataset_id)
            }
            if relative_path not in current_assets:
                create_dataset_asset(
                    session,
                    dataset_id=dataset_id,
                    upload_session_id=None,
                    relative_path=relative_path,
                    sha256=sha256,
                    size_bytes=len(content),
                    mime_type=_asset_mime_type(relative_path=relative_path, content_type=content_type),
                    width=None,
                    height=None,
                )

        exact_assets[relative_path] = cached_asset
        assets_by_basename.setdefault(Path(relative_path).name, []).append(cached_asset)


def _task_asset_reference(task: dict[str, Any]) -> str | None:
    data = task.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("image", "image_url", "image_path", "img", "file", "file_upload"):
        candidate = data.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    for candidate in data.values():
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _match_cached_asset(
    *,
    normalized_ref: str,
    exact_assets: dict[str, DatasetAssetRecord],
    assets_by_basename: dict[str, list[DatasetAssetRecord]],
) -> DatasetAssetRecord | None:
    if normalized_ref in exact_assets:
        return exact_assets[normalized_ref]
    basename_matches = assets_by_basename.get(Path(normalized_ref).name, [])
    if len(basename_matches) == 1:
        return basename_matches[0]
    return None


def _normalize_label_studio_asset_reference(asset_ref: str) -> str:
    parsed = urlparse(asset_ref)
    candidate = asset_ref
    if parsed.scheme or parsed.query:
        query_path = parse_qs(parsed.query).get("d", [None])[0]
        candidate = str(query_path or parsed.path)
    normalized = unquote(candidate).replace("\\", "/")
    if "/data/upload/" in normalized:
        normalized = normalized.split("/data/upload/", maxsplit=1)[1]
    return PurePosixPath(normalized.lstrip("/")).as_posix()


def _local_label_studio_asset_path(settings, asset_ref: str) -> Path | None:
    parsed = urlparse(asset_ref)
    if "/data/local-files/" not in parsed.path:
        return None
    query_path = parse_qs(parsed.query).get("d", [None])[0]
    if not query_path:
        return None

    local_root = Path(settings.label_studio.local_files_root).resolve()
    candidate = Path(unquote(query_path).replace("\\", "/"))
    source_path = candidate if candidate.is_absolute() else local_root / candidate
    resolved_source = source_path.resolve()
    try:
        resolved_source.relative_to(local_root)
    except ValueError:
        return None
    return resolved_source if resolved_source.is_file() else None


def _cached_asset_relative_path(normalized_ref: str) -> str:
    candidate = normalized_ref.strip("/")
    if not candidate:
        return "label-studio/asset.bin"
    path = PurePosixPath(candidate)
    safe_parts = [
        part.replace(":", "-") for part in path.parts if part not in {"", ".", ".."}
    ]
    if not safe_parts:
        return "label-studio/asset.bin"
    return PurePosixPath(*safe_parts).as_posix()


def _asset_mime_type(*, relative_path: str, content_type: str | None) -> str:
    if content_type:
        return content_type.split(";", maxsplit=1)[0].strip()
    guessed, _ = mimetypes.guess_type(relative_path)
    return guessed or "application/octet-stream"


def _snapshot_processing_error(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return message[:1000]


def _manifest_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _dvc_track_path(settings, tracked_root: Path) -> None:
    repo_root = Path(settings.dvc.repo_root)
    repo_root.mkdir(parents=True, exist_ok=True)
    if not (repo_root / ".dvc").exists():
        subprocess.run(
            ["dvc", "init", "--no-scm"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    if settings.dvc.remote_name and settings.dvc.remote_url:
        remote_list = subprocess.run(
            ["dvc", "remote", "list"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        if settings.dvc.remote_name not in remote_list.stdout:
            subprocess.run(
                ["dvc", "remote", "add", "-d", settings.dvc.remote_name, settings.dvc.remote_url],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
    subprocess.run(
        ["dvc", "add", str(tracked_root)],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    if settings.dvc.remote_name:
        subprocess.run(
            ["dvc", "push", f"{tracked_root}.dvc", "-r", settings.dvc.remote_name],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
