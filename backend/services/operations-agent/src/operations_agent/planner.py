from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv_contracts import (
    OfflineUpdateChange,
    OfflineUpdatePlanResponse,
    OfflineUpdateServicePlan,
    ResponseMeta,
)
from cv_utils import utc_now_iso

from operations_agent.docker_api import DockerSocketClient

STATE_VERSION = 1
DEFAULT_REPO_ROOT = "/host/repo"
DEFAULT_STATE_FILE = "/srv/cv-platform/operations/offline-update-state.json"
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_COMPOSE_PROJECT = "cv-platform-prod"

PYTHON_SERVICES = [
    "admin-api",
    "dataset-service",
    "training-orchestrator",
    "model-registry-service",
    "deployment-controller",
    "webhook-receiver",
    "inference-gateway-rt",
    "inference-gateway-batch",
    "label-studio-ml-adapter",
    "runtime-admin-api",
    "channel-service",
    "pipeline-definition-service",
    "runtime-orchestrator",
    "realtime-executor",
    "trigger-executor",
    "overlay-service",
    "capture-service",
    "result-service",
    "connector-service",
    "control-worker",
    "training-worker",
    "batch-worker",
    "maintenance-worker",
    "operations-agent",
]

RUNTIME_SERVICES = [
    "runtime-admin-api",
    "channel-service",
    "pipeline-definition-service",
    "runtime-orchestrator",
    "realtime-executor",
    "trigger-executor",
    "overlay-service",
    "capture-service",
    "result-service",
    "connector-service",
]

SERVICE_PROFILES: dict[str, dict[str, object]] = {
    "admin-web": {
        "risk": "low",
        "impact": "UI reconnects; background jobs and inference keep running.",
        "follow_up": "Reload the browser and confirm the status header refreshes.",
    },
    "admin-api": {
        "risk": "medium",
        "impact": "The admin UI pauses briefly; queued worker jobs keep running.",
        "follow_up": "Confirm /health/ready and refresh the Operations page.",
    },
    "training-worker": {
        "risk": "high",
        "impact": "An active training run can be interrupted and must resume from a checkpoint.",
        "follow_up": "Check the training job state and reconcile stale training jobs if needed.",
    },
    "batch-worker": {
        "risk": "medium",
        "impact": "Batch chunk processing pauses; completed chunks remain durable.",
        "follow_up": "Confirm incomplete chunks continue after the heartbeat returns.",
    },
    "control-worker": {
        "risk": "medium",
        "impact": "Dataset, deployment, webhook, and maintenance queue throughput pauses.",
        "follow_up": "Confirm the worker heartbeat returns and queue depth drains.",
    },
    "triton-rt": {
        "risk": "high",
        "impact": "Realtime inference is interrupted until Triton reloads.",
        "follow_up": "Run a small realtime inference smoke test.",
    },
    "triton-batch": {
        "risk": "high",
        "impact": "Batch inference execution is interrupted until Triton reloads.",
        "follow_up": "Confirm batch gateway readiness and retry incomplete chunks if needed.",
    },
    "reverse-proxy": {
        "risk": "medium",
        "impact": "Browser and API routing pause while Caddy reloads.",
        "follow_up": "Open the platform URL and Label Studio route.",
    },
    "prometheus": {
        "risk": "low",
        "impact": "Metrics scraping pauses briefly.",
        "follow_up": "Confirm Prometheus starts and targets are visible.",
    },
    "grafana": {
        "risk": "low",
        "impact": "Dashboard access pauses briefly.",
        "follow_up": "Open Grafana and confirm dashboards load.",
    },
}

for service_name in PYTHON_SERVICES:
    SERVICE_PROFILES.setdefault(
        service_name,
        {
            "risk": "medium",
            "impact": "This service pauses briefly; dependent UI actions may retry.",
            "follow_up": "Confirm readiness and clear any new health alert.",
        },
    )

SERVICE_MODULE_MAP = {
    "admin-api": "admin-api",
    "dataset-service": "dataset-service",
    "training-orchestrator": "training-orchestrator",
    "model-registry-service": "model-registry-service",
    "deployment-controller": "deployment-controller",
    "webhook-receiver": "webhook-receiver",
    "inference-gateway": ["inference-gateway-rt", "inference-gateway-batch", "label-studio-ml-adapter"],
    "runtime-admin-api": "runtime-admin-api",
    "channel-service": "channel-service",
    "pipeline-definition-service": "pipeline-definition-service",
    "runtime-orchestrator": "runtime-orchestrator",
    "realtime-executor": ["runtime-orchestrator", "realtime-executor"],
    "trigger-executor": "trigger-executor",
    "overlay-service": "overlay-service",
    "capture-service": "capture-service",
    "result-service": "result-service",
    "connector-service": "connector-service",
    "operations-agent": "operations-agent",
}

WORKER_MODULE_MAP = {
    "control-worker": "control-worker",
    "training-worker": "training-worker",
    "batch-worker": "batch-worker",
    "maintenance-worker": "maintenance-worker",
}

PACKAGE_SERVICE_MAP = {
    "cv-core": PYTHON_SERVICES,
    "cv-contracts": PYTHON_SERVICES,
    "cv-db": PYTHON_SERVICES,
    "cv-status": PYTHON_SERVICES,
    "cv-utils": PYTHON_SERVICES,
    "cv-observability": PYTHON_SERVICES,
    "cv-events": PYTHON_SERVICES,
    "cv-datasets": ["admin-api", "dataset-service", "webhook-receiver", "control-worker"],
    "cv-training": ["training-orchestrator", "training-worker"],
    "cv-deploy": [
        "deployment-controller",
        "model-registry-service",
        "control-worker",
        "inference-gateway-rt",
        "inference-gateway-batch",
    ],
    "cv-runtime": RUNTIME_SERVICES,
}

DEPENDENCY_FILES = {
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
}


@dataclass(frozen=True)
class AgentConfig:
    repo_root: Path
    state_file: Path
    docker_socket: str
    compose_project: str


def read_agent_config() -> AgentConfig:
    return AgentConfig(
        repo_root=Path(os.getenv("OPERATIONS_REPO_ROOT", DEFAULT_REPO_ROOT)),
        state_file=Path(os.getenv("OPERATIONS_STATE_FILE", DEFAULT_STATE_FILE)),
        docker_socket=os.getenv("OPERATIONS_DOCKER_SOCKET", DEFAULT_DOCKER_SOCKET),
        compose_project=os.getenv("OPERATIONS_COMPOSE_PROJECT", DEFAULT_COMPOSE_PROJECT),
    )


def build_update_plan(config: AgentConfig | None = None) -> OfflineUpdatePlanResponse:
    config = config or read_agent_config()
    generated_at = utc_now_iso()
    meta = ResponseMeta(generated_at=generated_at)
    repo_root = config.repo_root
    docker_client = DockerSocketClient(config.docker_socket)
    apply_available = docker_client.available
    warnings: list[str] = []

    if not repo_root.exists():
        return OfflineUpdatePlanResponse(
            enabled=False,
            apply_available=False,
            repo_root=str(repo_root),
            blocked=True,
            blockers=[f"Repository mount is missing: {repo_root}"],
            warnings=[],
            meta=meta,
        )

    if not (repo_root / ".git").exists():
        return OfflineUpdatePlanResponse(
            enabled=False,
            apply_available=False,
            repo_root=str(repo_root),
            blocked=True,
            blockers=[f"Repository mount is not a Git working tree: {repo_root}"],
            warnings=[],
            meta=meta,
        )

    current_git_sha = _git(repo_root, "rev-parse", "HEAD")
    current_git_ref = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    state = _load_state(config.state_file)
    last_applied_git_sha = _string_or_none(state.get("git_sha"))
    last_applied_at = _string_or_none(state.get("applied_at"))

    raw_changes: list[tuple[str, str]] = []
    if last_applied_git_sha:
        try:
            raw_changes.extend(_git_changed_files(repo_root, last_applied_git_sha, "HEAD"))
        except RuntimeError as exc:
            warnings.append(f"Could not compare with last applied commit: {exc}")
    else:
        warnings.append("No applied baseline exists yet. Mark the current version as baseline once the stack is known-good.")

    raw_changes.extend(_git_worktree_changes(repo_root))
    deduped_changes = _dedupe_changes(raw_changes)
    changes = [classify_change(path, change_type) for path, change_type in deduped_changes]
    service_plans = _build_service_plans(changes)
    blockers = sorted(
        {
            blocker
            for change in changes
            for blocker in _change_blockers(change)
        }
    )
    if not apply_available:
        warnings.append(f"Docker socket is not mounted at {config.docker_socket}; apply is disabled.")

    apply_token = _apply_token(
        current_git_sha=current_git_sha,
        changes=changes,
        last_applied_git_sha=last_applied_git_sha,
    )
    blocked = bool(blockers)
    return OfflineUpdatePlanResponse(
        enabled=True,
        apply_available=apply_available,
        repo_root=str(repo_root),
        current_git_ref=current_git_ref,
        current_git_sha=current_git_sha,
        last_applied_git_sha=last_applied_git_sha,
        last_applied_at=last_applied_at,
        pending_change_count=len(changes),
        changed_files=changes,
        service_plans=service_plans,
        blocked=blocked,
        blockers=blockers,
        warnings=warnings,
        apply_token=apply_token,
        meta=meta,
    )


def mark_current_applied(config: AgentConfig | None = None) -> OfflineUpdatePlanResponse:
    config = config or read_agent_config()
    repo_root = config.repo_root
    if not repo_root.exists():
        raise RuntimeError(f"Repository mount is missing: {repo_root}")
    current_git_sha = _git(repo_root, "rev-parse", "HEAD")
    current_git_ref = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    payload = {
        "version": STATE_VERSION,
        "git_sha": current_git_sha,
        "git_ref": current_git_ref,
        "applied_at": utc_now_iso(),
        "applied_by": "operations-agent",
    }
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    config.state_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return build_update_plan(config)


def record_applied_state(config: AgentConfig, *, services: list[str]) -> None:
    current_git_sha = _git(config.repo_root, "rev-parse", "HEAD")
    current_git_ref = _git(config.repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    payload = {
        "version": STATE_VERSION,
        "git_sha": current_git_sha,
        "git_ref": current_git_ref,
        "applied_at": utc_now_iso(),
        "applied_by": "operations-agent",
        "services": services,
    }
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    config.state_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def classify_change(path: str, change_type: str = "unknown") -> OfflineUpdateChange:
    normalized = path.replace("\\", "/").lstrip("./")
    affected_services: list[str] = []
    category = "other"
    restart_required = False
    rebuild_required = False
    migration_required = False
    hot_apply_supported = change_type not in {"deleted", "renamed"}
    reason = "Tracked file changed."

    if change_type in {"deleted", "renamed"}:
        reason = "Deleted or renamed files are not hot-applied because stale files could remain inside running containers."

    if normalized.startswith("docs/") or normalized.startswith("tests/") or normalized in {"README.md", ".gitignore", ".gitattributes"}:
        category = "documentation"
        reason = "Documentation or test-only change; no runtime restart is required."
    elif normalized in DEPENDENCY_FILES or normalized.endswith("/package.json") or normalized.endswith("/pyproject.toml"):
        category = "dependencies"
        affected_services = ["admin-web", *PYTHON_SERVICES]
        restart_required = True
        rebuild_required = True
        hot_apply_supported = False
        reason = "Dependency metadata changed; a prebuilt image or controlled rebuild is required."
    elif normalized.startswith("backend/migrations/"):
        category = "database_migration"
        affected_services = PYTHON_SERVICES
        restart_required = True
        migration_required = True
        hot_apply_supported = False
        reason = "Database migrations must run in a coordinated maintenance step."
    elif normalized.startswith("infra/docker/"):
        category = "image_build"
        affected_services = ["admin-web", *PYTHON_SERVICES, "mlflow"]
        restart_required = True
        rebuild_required = True
        hot_apply_supported = False
        reason = "Dockerfile changes require a rebuilt image."
    elif normalized in {"infra/compose/compose.prod.yaml", "compose.prod.yaml"}:
        category = "compose"
        affected_services = ["reverse-proxy", "admin-web", *PYTHON_SERVICES]
        restart_required = True
        hot_apply_supported = False
        reason = "Compose topology changed; review mounts, images, and dependencies before applying."
    elif normalized.startswith("infra/reverse-proxy/"):
        category = "reverse_proxy"
        affected_services = ["reverse-proxy"]
        restart_required = True
        reason = "Reverse proxy configuration changed."
    elif normalized.startswith("infra/observability/prometheus"):
        category = "observability"
        affected_services = ["prometheus"]
        restart_required = True
        reason = "Prometheus configuration changed."
    elif normalized.startswith("infra/observability/grafana"):
        category = "observability"
        affected_services = ["grafana"]
        restart_required = True
        reason = "Grafana provisioning changed."
    elif normalized.startswith("config/"):
        category = "runtime_config"
        affected_services = PYTHON_SERVICES
        restart_required = True
        reason = "Platform settings are loaded by Python services at startup."
    elif normalized.startswith("frontend/"):
        category = "frontend"
        affected_services = ["admin-web"]
        restart_required = True
        reason = "Admin frontend source changed; restarting admin-web rebuilds the preview bundle."
    elif normalized.startswith("backend/services/"):
        category = "backend_service"
        service = normalized.split("/")[2] if len(normalized.split("/")) > 2 else ""
        mapped = SERVICE_MODULE_MAP.get(service)
        affected_services = _as_list(mapped)
        restart_required = bool(affected_services)
        reason = "Service source changed."
    elif normalized.startswith("backend/workers/"):
        category = "worker"
        service = normalized.split("/")[2] if len(normalized.split("/")) > 2 else ""
        mapped = WORKER_MODULE_MAP.get(service)
        affected_services = _as_list(mapped)
        restart_required = bool(affected_services)
        reason = "Worker source changed."
    elif normalized.startswith("backend/packages/"):
        category = "shared_backend_package"
        package_name = normalized.split("/")[2] if len(normalized.split("/")) > 2 else ""
        affected_services = list(PACKAGE_SERVICE_MAP.get(package_name, PYTHON_SERVICES))
        restart_required = True
        reason = "Shared backend package changed."
    elif normalized.startswith("contracts/"):
        category = "contracts"
        affected_services = ["admin-web", *PYTHON_SERVICES]
        restart_required = True
        reason = "Shared contract files changed."
    elif normalized.startswith("deployment/"):
        category = "deployment_tooling"
        reason = "Deployment tooling changed; running containers are not affected."
    else:
        category = "unknown_runtime"
        affected_services = PYTHON_SERVICES
        restart_required = True
        reason = "Unknown runtime-facing path changed; using conservative Python service restart scope."

    if change_type in {"deleted", "renamed"} and restart_required:
        hot_apply_supported = False

    return OfflineUpdateChange(
        path=normalized,
        change_type=_change_type(change_type),
        category=category,
        affected_services=sorted(set(affected_services)),
        restart_required=restart_required,
        rebuild_required=rebuild_required,
        migration_required=migration_required,
        hot_apply_supported=hot_apply_supported,
        reason=reason,
    )


def _build_service_plans(changes: list[OfflineUpdateChange]) -> list[OfflineUpdateServicePlan]:
    files_by_service: dict[str, list[str]] = {}
    blockers_by_service: dict[str, set[str]] = {}
    for change in changes:
        for service_name in change.affected_services:
            files_by_service.setdefault(service_name, []).append(change.path)
            blockers = _change_blockers(change)
            if blockers:
                blockers_by_service.setdefault(service_name, set()).update(blockers)

    plans: list[OfflineUpdateServicePlan] = []
    for service_name in sorted(files_by_service):
        profile = SERVICE_PROFILES.get(service_name, {})
        blockers = sorted(blockers_by_service.get(service_name, set()))
        risk = "blocked" if blockers else str(profile.get("risk", "medium"))
        plans.append(
            OfflineUpdateServicePlan(
                service_name=service_name,
                action="manual" if blockers else "restart",
                risk=risk,  # type: ignore[arg-type]
                changed_files=sorted(set(files_by_service[service_name])),
                safe_to_restart_independently=service_name not in {"postgres", "redis", "triton-rt", "triton-batch"},
                expected_impact=str(profile.get("impact", "The service pauses briefly.")),
                required_follow_up=str(profile.get("follow_up", "Confirm service readiness.")),
                blockers=blockers,
            )
        )
    return plans


def _change_blockers(change: OfflineUpdateChange) -> list[str]:
    blockers: list[str] = []
    if change.rebuild_required:
        blockers.append(f"{change.path}: requires a rebuilt/preloaded image")
    if change.migration_required:
        blockers.append(f"{change.path}: requires database migration coordination")
    if not change.hot_apply_supported:
        blockers.append(f"{change.path}: is not safe for hot file copy")
    return blockers


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _git_changed_files(repo_root: Path, left_ref: str, right_ref: str) -> list[tuple[str, str]]:
    output = _git(repo_root, "diff", "--name-status", f"{left_ref}..{right_ref}")
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            changes.append((parts[2], "renamed"))
        elif status == "A" and len(parts) >= 2:
            changes.append((parts[1], "added"))
        elif status == "D" and len(parts) >= 2:
            changes.append((parts[1], "deleted"))
        elif len(parts) >= 2:
            changes.append((parts[1], "modified"))
    return changes


def _git_worktree_changes(repo_root: Path) -> list[tuple[str, str]]:
    output = _git(repo_root, "status", "--porcelain")
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
            change_type = "renamed"
        elif "D" in status:
            change_type = "deleted"
        elif status == "??" or "A" in status:
            change_type = "added"
        else:
            change_type = "modified"
        changes.append((path, change_type))
    return changes


def _dedupe_changes(changes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    by_path: dict[str, str] = {}
    for path, change_type in changes:
        by_path[path.replace("\\", "/").lstrip("./")] = change_type
    return sorted((path, change_type) for path, change_type in by_path.items())


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _apply_token(
    *,
    current_git_sha: str | None,
    changes: list[OfflineUpdateChange],
    last_applied_git_sha: str | None,
) -> str:
    payload = {
        "current_git_sha": current_git_sha,
        "last_applied_git_sha": last_applied_git_sha,
        "changes": [(change.path, change.change_type) for change in changes],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:32]


def _as_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _change_type(value: str) -> str:
    if value in {"added", "modified", "deleted", "renamed", "unknown"}:
        return value
    return "unknown"
