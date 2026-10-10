"""Model demand and admission derived from the existing runtime execution records."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from cv_db.models import RuntimeChannel, RuntimeExecution
from cv_utils import PlatformError


def lock_model_lifecycle(session: Session) -> None:
    """Serialize demand changes and Triton reconciliation across service processes."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(173529, 1)"))


def lock_runtime_use(session: Session, execution_id: str, *, exclusive: bool = False) -> bool:
    # Shared locks cover whole trigger jobs / gateway requests, including timeouts
    # whose worker is still running. A stop uses a nonblocking exclusive lock.
    if session.get_bind().dialect.name != "postgresql":
        return True  # The repository's in-memory unit-test backend.
    key = int.from_bytes(hashlib.sha256(execution_id.encode()).digest()[:8], "big", signed=True)
    function = "pg_try_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    result = session.scalar(text(f"SELECT {function}(:key)"), {"key": key})
    return bool(result) if exclusive else True


def execution_model_bindings(execution: RuntimeExecution) -> list[dict[str, Any]]:
    plan = execution.runtime_plan or {}
    bindings = list((plan.get("binding_signature") or {}).get("model_bindings") or [])
    for pipeline in plan.get("trigger_pipelines") or []:
        bindings.extend(pipeline.get("model_bindings") or [])
    return bindings


def demanded_release_ids(session: Session) -> set[str]:
    executions = session.scalars(
        select(RuntimeExecution)
        .join(
            RuntimeChannel,
            RuntimeChannel.id == RuntimeExecution.channel_id,
        )
        .where(
            RuntimeChannel.deleted_at.is_(None),
            or_(
                RuntimeChannel.active_runtime_execution_id == RuntimeExecution.id,
                RuntimeExecution.status == "draining",
            ),
            RuntimeExecution.stopped_at.is_(None),
            RuntimeExecution.status.in_(
                [
                    "validating",
                    "loading_pipeline",
                    "starting",
                    "running",
                    "draining",
                    "failed_retryable",
                ]
            ),
        )
    ).all()
    return {
        str(binding["deployment_release_id"])
        for execution in executions
        for binding in execution_model_bindings(execution)
        if binding.get("deployment_release_id")
    }


def require_runtime_pipeline(
    session: Session,
    *,
    execution_id: str,
    pipeline_version_id: str,
    allow_draining: bool = False,
) -> RuntimeExecution:
    execution = session.get(RuntimeExecution, execution_id, populate_existing=True)
    channel = (
        session.get(RuntimeChannel, execution.channel_id, populate_existing=True)
        if execution
        else None
    )
    allowed = {"running", "draining"} if allow_draining else {"running"}
    pipeline_ids = {execution.pipeline_version_id} if execution else set()
    if execution:
        pipeline_ids.update(
            item["pipeline_version_id"]
            for item in (execution.runtime_plan or {}).get("trigger_pipelines", [])
        )
    if (
        execution is None
        or channel is None
        or channel.deleted_at is not None
        or execution.stopped_at is not None
        or execution.status not in allowed
        or pipeline_version_id not in pipeline_ids
        or (execution.status == "running" and channel.active_runtime_execution_id != execution.id)
        or (
            not allow_draining
            and (
                channel.active_runtime_execution_id != execution.id
                or channel.desired_state != "running"
                or channel.observed_state != "running"
            )
        )
    ):
        raise PlatformError(
            code="runtime_channel_not_running",
            status_code=409,
            message="Start the channel and wait for its pipeline to become ready before inference.",
        )
    return execution


def pipeline_release_ids(execution: RuntimeExecution, pipeline_version_id: str) -> set[str]:
    plan = execution.runtime_plan or {}
    bindings = (
        ((plan.get("binding_signature") or {}).get("model_bindings") or [])
        if execution.pipeline_version_id == pipeline_version_id
        else []
    )
    for pipeline in plan.get("trigger_pipelines") or []:
        if pipeline.get("pipeline_version_id") == pipeline_version_id:
            bindings = pipeline.get("model_bindings") or []
    return {
        str(item["deployment_release_id"]) for item in bindings if item.get("deployment_release_id")
    }


def build_execution_model_resolver(
    session: Session, execution: RuntimeExecution, pipeline_version_id: str
):
    from cv_db.runtime_execution_store import build_runtime_model_resolver

    resolver = build_runtime_model_resolver(session)
    release_ids = pipeline_release_ids(execution, pipeline_version_id)
    bindings = [
        item
        for item in execution_model_bindings(execution)
        if item.get("deployment_release_id") in release_ids
    ]

    def resolve(model_ref):
        for binding in bindings:
            if binding.get("logical_model_ref") == model_ref.model_dump(mode="json"):
                return resolver(
                    model_ref.model_copy(
                        update={
                            "selection_policy": "explicit_release",
                            "deployment_release_id": binding["deployment_release_id"],
                            "model_family_slug": binding["model_family_slug"],
                        }
                    )
                )
        raise ValueError("Model binding is not part of the started runtime; restart the channel.")

    return resolve
