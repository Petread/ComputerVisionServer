from __future__ import annotations

from cv_core import build_mlflow_adapter, load_platform_settings
from cv_db import get_model_version_detail, list_model_versions, session_scope
from cv_utils import utc_now_iso


def register_model_version(model_version_id: str) -> dict[str, str]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        detail = get_model_version_detail(session, model_version_id)
    if detail is None:
        raise ValueError(f"Model version {model_version_id} does not exist.")
    if detail.mlflow_registered_model and detail.mlflow_model_version:
        build_mlflow_adapter(settings).sync_registered_model_metadata(
            registered_model_name=detail.mlflow_registered_model,
            model_version=detail.mlflow_model_version,
            aliases=detail.alias_names,
            tags=detail.registry_tags,
        )
    return {"model_version_id": model_version_id, "status": "registered", "finished_at": utc_now_iso()}


def reconcile_model_registry_state() -> dict[str, object]:
    settings = load_platform_settings("control-worker")
    mlflow_adapter = build_mlflow_adapter(settings)
    synced_model_version_ids: list[str] = []
    with session_scope(settings) as session:
        items = list_model_versions(session)
        for item in items:
            detail = get_model_version_detail(session, item.model_version_id)
            if detail is None:
                continue
            if detail.mlflow_registered_model and detail.mlflow_model_version:
                mlflow_adapter.sync_registered_model_metadata(
                    registered_model_name=detail.mlflow_registered_model,
                    model_version=detail.mlflow_model_version,
                    aliases=detail.alias_names,
                    tags=detail.registry_tags,
                )
                synced_model_version_ids.append(detail.model_version_id)
    return {
        "status": "reconciled",
        "synced_model_version_ids": synced_model_version_ids,
        "finished_at": utc_now_iso(),
    }
