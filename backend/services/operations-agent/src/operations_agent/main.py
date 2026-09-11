from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException

from cv_contracts import (
    OfflineUpdateApplyRequest,
    OfflineUpdateApplyResult,
    OfflineUpdatePlanResponse,
    ResponseMeta,
)
from cv_core import create_service_app
from cv_utils import utc_now_iso

from operations_agent.docker_api import DockerSocketClient
from operations_agent.planner import (
    AgentConfig,
    build_update_plan,
    mark_current_applied,
    read_agent_config,
    record_applied_state,
)

HOST_MOUNTED_SERVICES = {"reverse-proxy", "prometheus", "grafana", "operations-agent"}

router = APIRouter(prefix="/api/v1/operations-agent", tags=["operations-agent"])


@router.get("/offline-update/plan", response_model=OfflineUpdatePlanResponse)
def get_update_plan() -> OfflineUpdatePlanResponse:
    return build_update_plan()


@router.post("/offline-update:mark-current", response_model=OfflineUpdatePlanResponse)
def mark_current_version() -> OfflineUpdatePlanResponse:
    return mark_current_applied()


@router.post("/offline-update:apply", response_model=OfflineUpdateApplyResult)
def apply_update(
    payload: OfflineUpdateApplyRequest,
    background_tasks: BackgroundTasks,
) -> OfflineUpdateApplyResult:
    config = read_agent_config()
    plan = build_update_plan(config)
    if not plan.enabled:
        raise HTTPException(status_code=409, detail="Offline update planning is not enabled.")
    if not plan.apply_available:
        raise HTTPException(status_code=409, detail="Docker socket is not available to the operations agent.")
    if payload.apply_token != plan.apply_token:
        raise HTTPException(status_code=409, detail="The update plan changed. Refresh the page and try again.")
    if plan.blocked and not payload.force:
        return _result(
            operation_id=str(uuid.uuid4()),
            status="blocked",
            service_names=[],
            changed_files=[item.path for item in plan.changed_files],
            message="Update was not applied because the plan contains blockers.",
            blockers=plan.blockers,
            warnings=plan.warnings,
        )

    requested = set(payload.service_names or [])
    service_plans = [
        item
        for item in plan.service_plans
        if item.action == "restart"
        and not item.blockers
        and (not requested or item.service_name in requested)
    ]
    if not service_plans:
        return _result(
            operation_id=str(uuid.uuid4()),
            status="succeeded",
            service_names=[],
            changed_files=[],
            message="No restartable service changes are pending.",
            blockers=[],
            warnings=plan.warnings,
        )

    operation_id = str(uuid.uuid4())
    service_names = _restart_order([item.service_name for item in service_plans])
    changed_files = sorted({path for item in service_plans for path in item.changed_files})
    _write_run_result(
        config,
        _result(
            operation_id=operation_id,
            status="accepted",
            service_names=service_names,
            changed_files=changed_files,
            message="Update apply was accepted and will run in the operations agent.",
            blockers=[],
            warnings=plan.warnings,
        ),
    )
    background_tasks.add_task(
        _run_apply,
        config,
        operation_id,
        service_names,
        changed_files,
        plan.warnings,
    )
    return _result(
        operation_id=operation_id,
        status="accepted",
        service_names=service_names,
        changed_files=changed_files,
        message="Update apply accepted. Services will restart one at a time.",
        blockers=[],
        warnings=plan.warnings,
    )


def _run_apply(
    config: AgentConfig,
    operation_id: str,
    service_names: list[str],
    changed_files: list[str],
    warnings: list[str],
) -> None:
    client = DockerSocketClient(config.docker_socket)
    restart_self = "operations-agent" in service_names
    loop_services = [service_name for service_name in service_names if service_name != "operations-agent"]
    try:
        for service_name in loop_services:
            service_files = [
                path for path in changed_files if _path_should_copy_to_service(path, service_name)
            ]
            if service_files and service_name not in HOST_MOUNTED_SERVICES:
                client.copy_files_to_service(
                    project_name=config.compose_project,
                    service_name=service_name,
                    repo_root=config.repo_root,
                    relative_paths=service_files,
                )
            client.restart_service(
                project_name=config.compose_project,
                service_name=service_name,
                timeout_seconds=10,
            )
        record_applied_state(config, services=service_names)
        _write_run_result(
            config,
            _result(
                operation_id=operation_id,
                status="succeeded",
                service_names=service_names,
                changed_files=changed_files,
                message="Update was applied and selected services were restarted.",
                blockers=[],
                warnings=warnings,
            ),
        )
        if restart_self:
            client.restart_service(
                project_name=config.compose_project,
                service_name="operations-agent",
                timeout_seconds=10,
            )
    except Exception as exc:  # noqa: BLE001
        _write_run_result(
            config,
            _result(
                operation_id=operation_id,
                status="failed",
                service_names=service_names,
                changed_files=changed_files,
                message=f"Update apply failed: {exc}",
                blockers=[],
                warnings=warnings,
            ),
        )


def _path_should_copy_to_service(path: str, service_name: str) -> bool:
    if service_name == "admin-web":
        return path.startswith("frontend/") or path in {"package.json", "pnpm-workspace.yaml"}
    if service_name in {"reverse-proxy", "prometheus", "grafana"}:
        return path.startswith("infra/")
    return path.startswith("backend/") or path.startswith("config/") or path.startswith("contracts/")


def _restart_order(service_names: list[str]) -> list[str]:
    # Keep the control surface and the agent itself alive until the other restarts are triggered.
    tail = ["admin-api", "operations-agent"]
    ordered = [item for item in sorted(set(service_names)) if item not in tail]
    ordered.extend([item for item in tail if item in set(service_names)])
    return ordered


def _result(
    *,
    operation_id: str,
    status: str,
    service_names: list[str],
    changed_files: list[str],
    message: str,
    blockers: list[str],
    warnings: list[str],
) -> OfflineUpdateApplyResult:
    generated_at = utc_now_iso()
    return OfflineUpdateApplyResult(
        operation_id=operation_id,
        status=status,  # type: ignore[arg-type]
        accepted_at=generated_at,
        service_names=service_names,
        changed_files=changed_files,
        message=message,
        blockers=blockers,
        warnings=warnings,
        meta=ResponseMeta(generated_at=generated_at),
    )


def _write_run_result(config: AgentConfig, result: OfflineUpdateApplyResult) -> None:
    path = _run_result_path(config, result.operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    latest_path = path.parent / "latest.json"
    latest_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")


def _run_result_path(config: AgentConfig, operation_id: str) -> Path:
    return config.state_file.parent / "offline-update-runs" / f"{operation_id}.json"


app = create_service_app(
    title="CV Platform Operations Agent",
    service_name="operations-agent",
    api_router=router,
)
