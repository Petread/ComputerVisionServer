from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cv_contracts import (
    EdgeArtifactDescriptor,
    EdgeBlobUploadStatus,
    EdgeCameraAdapterCapability,
    EdgeCapabilitySnapshot,
    EdgeDeploymentManifest,
    EdgeDeploymentState,
    EdgeDeviceAddress,
    EdgeDeviceHealth,
    EdgeDeviceIdentity,
    EdgeMediaLeaseRequest,
    EdgeMediaLeaseState,
    EdgePlantLastResultResponse,
    EdgePlantTriggerRequest,
    EdgePlantTriggerResponse,
    EdgePreviewFrame,
    EdgeResultAck,
    EdgeResultBatch,
    EdgeResultEnvelope,
    EdgeResultStreamList,
    EdgeResultStreamSummary,
    EdgeRuntimeStatus,
    EdgeStorageQuotaStatus,
)
from cv_runtime.distribution import edge_node_contracts, runtime_code_digest

from edge_agent.engine import EdgeEngineAdmissionError, EdgeEngineManager
from edge_agent.graph import (
    PRODUCTIVE_EDGE_NODE_TYPES,
    EdgeGraphError,
    EdgeGraphExecutor,
    build_productive_registry,
)
from edge_agent.inventory import PlatformInventoryProbe
from edge_agent.persistence import EdgeStateStore
from edge_agent.runtime import EdgeRuntimeSupervisor, EdgeRuntimeTriggerError, SourceFrame


class EdgeStateError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        retryable: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.detail = detail or {}


@dataclass(frozen=True)
class EdgeAgentConfig:
    address: EdgeDeviceAddress
    data_root: Path
    hostname: str = "ybb-edge-orin-01"
    platform: str = "jetson_orin_nano"
    architecture: str = "aarch64"
    agent_version: str = "0.8.0"
    runtime_version: str = "0.8.0"
    os_version: str = "Jetson Linux 39.2"
    jetpack_version: str | None = "7.2"
    l4t_version: str | None = "39.2"
    cuda_version: str | None = "13.2"
    tensorrt_version: str | None = "10.16"
    deepstream_version: str | None = "9.1"
    gpu_model: str | None = "Jetson Orin Nano"
    gpu_memory_bytes: int = 8 * 1024**3
    ram_total_bytes: int = 8 * 1024**3
    storage_total_bytes: int = 256 * 1024**3
    supported_node_types: tuple[str, ...] = tuple(
        sorted(set(PRODUCTIVE_EDGE_NODE_TYPES) | edge_node_contracts().keys())
    )
    camera_adapters: tuple[str, ...] = ("rtsp", "aravis_gige", "opencv", "gstreamer")
    capability_inventory: str = "configured"
    max_blob_chunk_bytes: int = 4 * 1024 * 1024
    max_result_batch_bytes: int = 512 * 1024
    artifact_quota_bytes: int = 32 * 1024**3
    result_outbox_quota_bytes: int = 2 * 1024**3
    result_outbox_max_items: int = 250_000
    minimum_disk_free_bytes: int = 2 * 1024**3
    supervisor_max_restarts: int = 3
    supervisor_base_backoff_seconds: float = 0.25
    engine_builder_mode: str = "trtexec"
    trtexec_path: str = "trtexec"
    engine_build_timeout_seconds: float = 900.0
    preview_jpeg_quality: int = 70
    preview_max_bytes: int = 1_048_576
    plant_allowed_ipv4s: tuple[str, ...] = ()
    plant_max_request_bytes: int = 65_536
    plant_max_concurrent_triggers: int = 4
    plant_trigger_timeout_ms: int = 2_500
    plant_rate_limit_per_second: int = 20

    def __post_init__(self) -> None:
        if self.capability_inventory not in {"configured", "probe"}:
            raise ValueError("capability_inventory must be configured or probe")
        positive_limits = {
            "plant_max_request_bytes": self.plant_max_request_bytes,
            "plant_max_concurrent_triggers": self.plant_max_concurrent_triggers,
            "plant_trigger_timeout_ms": self.plant_trigger_timeout_ms,
            "plant_rate_limit_per_second": self.plant_rate_limit_per_second,
        }
        invalid = [name for name, value in positive_limits.items() if value <= 0]
        if invalid:
            raise ValueError(f"Edge plant limits must be positive: {invalid}")

    @classmethod
    def from_environment(cls) -> EdgeAgentConfig:
        platform = os.getenv("YBB_EDGE_PLATFORM", "jetson_orin_nano")
        return cls(
            address=EdgeDeviceAddress(
                management_ip=os.getenv("YBB_EDGE_MANAGEMENT_IP", "192.168.50.21"),
                control_port=int(os.getenv("YBB_EDGE_CONTROL_PORT", "9443")),
                plant_api_port=int(os.getenv("YBB_EDGE_PLANT_API_PORT", "9444")),
                rtsp_port=int(os.getenv("YBB_EDGE_RTSP_PORT", "8554")),
            ),
            data_root=Path(os.getenv("YBB_EDGE_DATA_ROOT", ".local/state/edge-agent")),
            hostname=os.getenv("YBB_EDGE_HOSTNAME", "ybb-edge-orin-01"),
            platform=platform,
            architecture=os.getenv(
                "YBB_EDGE_ARCHITECTURE", "aarch64" if platform.startswith("jetson_") else "x86_64"
            ),
            os_version=os.getenv("YBB_EDGE_OS_VERSION", "Jetson Linux 39.2"),
            jetpack_version=_optional_environment("YBB_EDGE_JETPACK_VERSION", "7.2"),
            l4t_version=_optional_environment("YBB_EDGE_L4T_VERSION", "39.2"),
            cuda_version=_optional_environment("YBB_EDGE_CUDA_VERSION", "13.2"),
            tensorrt_version=_optional_environment("YBB_EDGE_TENSORRT_VERSION", "10.16"),
            deepstream_version=_optional_environment("YBB_EDGE_DEEPSTREAM_VERSION", "9.1"),
            gpu_model=_optional_environment("YBB_EDGE_GPU_MODEL", "Jetson Orin Nano"),
            camera_adapters=tuple(
                value.strip()
                for value in os.getenv(
                    "YBB_EDGE_CAMERA_ADAPTERS",
                    "rtsp,usb_opencv,gstreamer,aravis_gige,basler_pylon,flir_spinnaker",
                ).split(",")
                if value.strip()
            ),
            capability_inventory=os.getenv("YBB_EDGE_CAPABILITY_INVENTORY", "probe"),
            artifact_quota_bytes=int(os.getenv("YBB_EDGE_ARTIFACT_QUOTA_BYTES", str(32 * 1024**3))),
            result_outbox_quota_bytes=int(
                os.getenv("YBB_EDGE_OUTBOX_QUOTA_BYTES", str(2 * 1024**3))
            ),
            result_outbox_max_items=int(os.getenv("YBB_EDGE_OUTBOX_MAX_ITEMS", "250000")),
            minimum_disk_free_bytes=int(
                os.getenv("YBB_EDGE_MINIMUM_DISK_FREE_BYTES", str(2 * 1024**3))
            ),
            engine_builder_mode=os.getenv("YBB_EDGE_ENGINE_BUILDER_MODE", "trtexec"),
            trtexec_path=os.getenv("YBB_EDGE_TRTEXEC_PATH", "trtexec"),
            engine_build_timeout_seconds=float(
                os.getenv("YBB_EDGE_ENGINE_BUILD_TIMEOUT_SECONDS", "900")
            ),
            preview_jpeg_quality=int(os.getenv("YBB_EDGE_PREVIEW_JPEG_QUALITY", "70")),
            preview_max_bytes=int(os.getenv("YBB_EDGE_PREVIEW_MAX_BYTES", "1048576")),
            plant_allowed_ipv4s=tuple(
                value.strip()
                for value in os.getenv("YBB_EDGE_PLANT_ALLOWED_IPS", "").split(",")
                if value.strip()
            ),
            plant_max_request_bytes=int(os.getenv("YBB_EDGE_PLANT_MAX_REQUEST_BYTES", "65536")),
            plant_max_concurrent_triggers=int(
                os.getenv("YBB_EDGE_PLANT_MAX_CONCURRENT_TRIGGERS", "4")
            ),
            plant_trigger_timeout_ms=int(os.getenv("YBB_EDGE_PLANT_TRIGGER_TIMEOUT_MS", "2500")),
            plant_rate_limit_per_second=int(
                os.getenv("YBB_EDGE_PLANT_RATE_LIMIT_PER_SECOND", "20")
            ),
        )


@dataclass
class EdgeAgentFaults:
    offline: bool = False
    degraded: bool = False
    disk_full: bool = False
    activation_failure: bool = False


class EdgeAgentState:
    def __init__(
        self,
        config: EdgeAgentConfig,
        *,
        inventory_probe: PlatformInventoryProbe | None = None,
    ) -> None:
        self.config = config
        self.faults = EdgeAgentFaults()
        self._artifact_capacity_rejected = False
        self._outbox_capacity_rejected = False
        self._preview_last_emitted: dict[str, float] = {}
        self._trigger_locks: dict[tuple[str, str], threading.Lock] = {}
        self.boot_id = str(uuid.uuid4())
        self._inventory_probe = inventory_probe or PlatformInventoryProbe()
        self._lock = threading.RLock()
        self.blob_root = config.data_root / "artifacts" / "sha256"
        self.upload_root = config.data_root / "uploads"
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self.store = EdgeStateStore(config.data_root / "state" / "edge-agent.sqlite3")
        self.engine_manager = EdgeEngineManager(
            engine_root=config.data_root / "engines",
            artifact_resolver=self._blob_path,
            builder_mode=config.engine_builder_mode,
            trtexec_path=config.trtexec_path,
            build_timeout_seconds=config.engine_build_timeout_seconds,
            cache_namespace=(
                f"{config.platform}:{config.architecture}:"
                f"{config.tensorrt_version or 'no-tensorrt'}:{config.gpu_model or 'unknown-gpu'}"
            ),
        )
        self.supervisor = EdgeRuntimeSupervisor(
            store=self.store,
            result_sink=self._runtime_result_sink,
            preview_sink=self._runtime_preview_sink,
            preview_demand=self._preview_requested,
            graph_factory=self._create_graph_executor,
            max_restarts=config.supervisor_max_restarts,
            base_backoff_seconds=config.supervisor_base_backoff_seconds,
        )
        self._recover_committed_workloads()

    def close(self) -> None:
        self.supervisor.close()
        self.store.close()

    def ensure_online(self) -> None:
        if self.faults.offline:
            raise EdgeStateError(
                "simulated_offline",
                "The simulated edge device is offline.",
                status_code=503,
                retryable=True,
            )

    def identity(self) -> EdgeDeviceIdentity:
        self.ensure_online()
        return EdgeDeviceIdentity(
            address=self.config.address,
            hostname=self.config.hostname,
            platform=self.config.platform,
            architecture=self.config.architecture,
            agent_version=self.config.agent_version,
            runtime_version=self.config.runtime_version,
            boot_id=self.boot_id,
        )

    def health(self) -> EdgeDeviceHealth:
        self.ensure_online()
        runtime = self.runtime_status()
        state = (
            "degraded" if self.faults.degraded or runtime.supervisor_state == "degraded" else "ok"
        )
        detail = "Edge agent and durable runtime are ready."
        if runtime.storage.pressure or self.faults.disk_full:
            state = "degraded"
            detail = "Edge storage quota or free-space reserve is exhausted."
        elif runtime.supervisor_state == "degraded":
            detail = "One or more edge runtime workers are in a crash loop."
        elif self.faults.degraded:
            detail = "Simulated edge agent is degraded."
        return EdgeDeviceHealth(
            state=state,
            detail=detail,
            boot_id=self.boot_id,
            observed_at=_utc_now(),
            active_deployment_ids=[
                state.deployment_id
                for _manifest, state, _slot, _keys in self.store.deployments()
                if state.status == "running"
            ],
            result_backlog_count=runtime.storage.outbox_items,
            disk_free_bytes=runtime.storage.disk_free_bytes,
            runtime=runtime,
        )

    def runtime_status(self) -> EdgeRuntimeStatus:
        self.ensure_online()
        storage = self._storage_status()
        processes = self.store.processes()
        supervisor_state = "idle"
        if any(process.state == "crash_loop" for process in processes) or storage.pressure:
            supervisor_state = "degraded"
        elif any(process.state in {"starting", "running", "backoff"} for process in processes):
            supervisor_state = "running"
        return EdgeRuntimeStatus(
            boot_id=self.boot_id,
            supervisor_state=supervisor_state,
            active_slot=self.store.metadata("active_slot"),
            previous_slot=self.store.metadata("previous_slot"),
            slots=self.store.slots(),
            processes=processes,
            storage=storage,
            recovered_deployment_ids=sorted(self.supervisor.recovered_deployment_ids),
            observed_at=_utc_now(),
        )

    def capabilities(self) -> EdgeCapabilitySnapshot:
        self.ensure_online()
        disk = shutil.disk_usage(self.config.data_root)
        inventory = (
            self._inventory_probe.probe() if self.config.capability_inventory == "probe" else None
        )
        camera_inventory: list[EdgeCameraAdapterCapability] = []
        available_adapters = list(self.config.camera_adapters)
        if inventory is not None:
            camera_inventory = [
                EdgeCameraAdapterCapability(
                    adapter=item.adapter,
                    available=item.available,
                    provider=item.provider,
                    version=item.version,
                    device_count=item.device_count,
                    detail=item.detail,
                )
                for item in inventory.camera_adapters
            ]
            available_adapters = [
                item.adapter
                for item in inventory.camera_adapters
                if item.available and item.adapter in self.config.camera_adapters
            ]
        return EdgeCapabilitySnapshot(
            management_ip=self.config.address.management_ip,
            platform=self.config.platform,
            architecture=inventory.architecture if inventory else self.config.architecture,
            os_version=inventory.os_version if inventory else self.config.os_version,
            jetpack_version=inventory.jetpack_version if inventory else self.config.jetpack_version,
            l4t_version=inventory.l4t_version if inventory else self.config.l4t_version,
            cuda_version=inventory.cuda_version if inventory else self.config.cuda_version,
            tensorrt_version=(
                inventory.tensorrt_version if inventory else self.config.tensorrt_version
            ),
            deepstream_version=(
                inventory.deepstream_version if inventory else self.config.deepstream_version
            ),
            gpu_model=inventory.gpu_model if inventory else self.config.gpu_model,
            gpu_driver_version=inventory.gpu_driver_version if inventory else None,
            gpu_memory_bytes=(
                inventory.gpu_memory_bytes if inventory else self.config.gpu_memory_bytes
            ),
            python_version=inventory.python_version if inventory else None,
            ram_total_bytes=inventory.ram_total_bytes if inventory else self.config.ram_total_bytes,
            storage_total_bytes=max(self.config.storage_total_bytes, disk.total),
            storage_free_bytes=disk.free,
            agent_version=self.config.agent_version,
            runtime_version=self.config.runtime_version,
            supported_node_types=list(self._supported_node_types()),
            runtime_code_digest=runtime_code_digest(),
            node_contract_versions=edge_node_contracts(),
            software_update_protocol=1 if os.getenv("YBB_EDGE_SOFTWARE_LAUNCHER") == "1" else 0,
            camera_adapters=available_adapters,
            camera_inventory=camera_inventory,
            inventory_source=inventory.source if inventory else "configured_fallback",
            inventory_warnings=inventory.warnings if inventory else [],
            observed_at=_utc_now(),
        )

    def blob_status(self, sha256: str) -> EdgeBlobUploadStatus:
        self.ensure_online()
        final_path = self._blob_path(sha256)
        partial_path = self._partial_path(sha256)
        if final_path.exists():
            size = final_path.stat().st_size
            if _sha256_file(final_path) == sha256:
                return EdgeBlobUploadStatus(
                    sha256=sha256, size_bytes=size, received_bytes=size, complete=True
                )
            final_path.unlink(missing_ok=True)
        received = partial_path.stat().st_size if partial_path.exists() else 0
        return EdgeBlobUploadStatus(
            sha256=sha256,
            size_bytes=received,
            received_bytes=received,
            complete=False,
        )

    def append_blob(
        self,
        sha256: str,
        chunk: bytes,
        *,
        offset: int,
        total_size: int,
        idempotency_key: str,
    ) -> EdgeBlobUploadStatus:
        self.ensure_online()
        if len(chunk) > self.config.max_blob_chunk_bytes:
            raise EdgeStateError(
                "chunk_too_large",
                f"Blob chunks are limited to {self.config.max_blob_chunk_bytes} bytes.",
                status_code=413,
            )
        if offset < 0 or total_size < 0 or offset + len(chunk) > total_size:
            raise EdgeStateError("invalid_blob_range", "Blob upload range is invalid.")
        with self._lock:
            current = self.blob_status(sha256)
            if current.complete:
                if current.size_bytes != total_size:
                    raise EdgeStateError(
                        "blob_size_conflict",
                        "A complete blob with the same digest has a different size.",
                    )
                return current.model_copy(update={"idempotency_key": idempotency_key})
            if current.received_bytes != offset:
                raise EdgeStateError(
                    "blob_offset_conflict",
                    "Blob upload offset does not match the persisted partial artifact.",
                    retryable=True,
                    detail={"expected_offset": current.received_bytes, "received_offset": offset},
                )
            self._ensure_artifact_capacity(total_size - current.received_bytes)
            partial_path = self._partial_path(sha256)
            partial_path.parent.mkdir(parents=True, exist_ok=True)
            with partial_path.open("ab") as output:
                output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            received = partial_path.stat().st_size
            if received < total_size:
                return EdgeBlobUploadStatus(
                    sha256=sha256,
                    size_bytes=total_size,
                    received_bytes=received,
                    complete=False,
                    idempotency_key=idempotency_key,
                )
            if _sha256_file(partial_path) != sha256:
                partial_path.unlink(missing_ok=True)
                raise EdgeStateError(
                    "blob_hash_mismatch",
                    "Completed artifact does not match the requested SHA-256 digest.",
                    status_code=422,
                )
            final_path = self._blob_path(sha256)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            partial_path.replace(final_path)
            return EdgeBlobUploadStatus(
                sha256=sha256,
                size_bytes=total_size,
                received_bytes=received,
                complete=True,
                idempotency_key=idempotency_key,
            )

    def prepare(self, manifest: EdgeDeploymentManifest) -> EdgeDeploymentState:
        self.ensure_online()
        with self._lock:
            existing = self.store.deployment(manifest.deployment_id)
            if existing is not None:
                existing_manifest, state, _slot, _keys = existing
                if existing_manifest.idempotency_key != manifest.idempotency_key:
                    raise EdgeStateError(
                        "deployment_idempotency_conflict",
                        "Deployment ID already exists with a different idempotency key.",
                    )
                return state
            if (
                manifest.required_platforms
                and self.config.platform not in manifest.required_platforms
            ):
                raise EdgeStateError(
                    "incompatible_platform",
                    f"Platform {self.config.platform} is not allowed by this workload.",
                    status_code=422,
                )
            source = manifest.runtime_requirements.get("source", {})
            source_type = str(source.get("type", "passive")) if isinstance(source, dict) else ""
            normalized_source = {
                "usb": "usb_opencv",
                "opencv": "usb_opencv",
            }.get(source_type, source_type)
            if (
                normalized_source not in {"passive", "synthetic", "file"}
                and normalized_source not in self.config.camera_adapters
            ):
                raise EdgeStateError(
                    "camera_adapter_not_approved",
                    f"Camera adapter {normalized_source!r} is not approved on this edge device.",
                    status_code=422,
                    detail={"approved_camera_adapters": list(self.config.camera_adapters)},
                )
            self._validate_target_profile(manifest)
            expected_runtime = manifest.runtime_requirements.get("runtime_code_digest")
            if expected_runtime and expected_runtime != runtime_code_digest():
                raise EdgeStateError(
                    "runtime_update_required",
                    "Install the required runtime release before activating this pipeline.",
                    status_code=422,
                )
            required_contracts = manifest.runtime_requirements.get("node_contract_versions", {})
            if any(
                edge_node_contracts().get(key) != value for key, value in required_contracts.items()
            ):
                raise EdgeStateError(
                    "node_contract_incompatible", "Node contracts differ.", status_code=422
                )
            missing_nodes = sorted(
                set(manifest.required_node_types) - set(self._supported_node_types())
            )
            if missing_nodes:
                raise EdgeStateError(
                    "missing_node_types",
                    "Edge runtime does not provide every required node type.",
                    status_code=422,
                    detail={"missing_node_types": missing_nodes},
                )
            missing_artifacts = [
                artifact.sha256
                for artifact in manifest.artifacts
                if not self._artifact_exists(artifact)
            ]
            if missing_artifacts:
                raise EdgeStateError(
                    "missing_artifacts",
                    "Deployment cannot be prepared until every artifact is complete.",
                    status_code=422,
                    detail={"missing_artifacts": missing_artifacts},
                )
            active_slot = self.store.metadata("active_slot")
            slot = "B" if active_slot == "A" else "A"
            try:
                admission_report, engine_builds = self.engine_manager.admit(manifest)
                graph = EdgeGraphExecutor.from_manifest(
                    manifest,
                    artifact_resolver=self._blob_path,
                    inference_runtime=None,
                )
                if graph is not None:
                    graph.close()
            except (EdgeEngineAdmissionError, EdgeGraphError) as exc:
                raise EdgeStateError(
                    "local_admission_failed",
                    str(exc),
                    status_code=422,
                    detail={"workload_release_id": manifest.workload_release_id},
                ) from exc
            state = EdgeDeploymentState(
                deployment_id=manifest.deployment_id,
                workload_release_id=manifest.workload_release_id,
                channel_id=manifest.channel_id,
                status="prepared",
                idempotency_key=manifest.idempotency_key,
                prepared_at=_utc_now(),
                runtime_slot=slot,
                admission_report=admission_report,
                engine_builds=engine_builds,
            )
            self.store.prepare_deployment(manifest, state, slot, updated_at=_utc_now())
            return state

    def commit(self, deployment_id: str, idempotency_key: str) -> EdgeDeploymentState:
        self.ensure_online()
        with self._lock:
            manifest, state, slot, command_keys = self._deployment_record(deployment_id)
            prior = command_keys.get("commit")
            if prior is not None:
                if prior != idempotency_key:
                    raise EdgeStateError(
                        "deployment_idempotency_conflict",
                        "Commit was already requested with another idempotency key.",
                    )
                return state
            command_keys["commit"] = idempotency_key
            if self.faults.activation_failure or manifest.simulate_activation_failure:
                failed = state.model_copy(
                    update={
                        "status": "activation_failed",
                        "last_error": "Simulated workload activation failure.",
                    }
                )
                self.store.mark_activation_failed(
                    deployment_id, failed, command_keys, updated_at=_utc_now()
                )
                raise EdgeStateError("activation_failed", "Simulated workload activation failure.")
            running = state.model_copy(
                update={
                    "status": "running",
                    "activated_at": _utc_now(),
                    "last_error": None,
                    "runtime_slot": slot,
                }
            )
            previous = self.store.activate_deployment(
                deployment_id, running, command_keys, updated_at=_utc_now()
            )
            if previous is not None:
                self.supervisor.stop(previous.deployment_id)
            self.supervisor.start(manifest)
            return running

    def rollback(self, deployment_id: str, idempotency_key: str) -> EdgeDeploymentState:
        self.ensure_online()
        with self._lock:
            _manifest, state, _slot, command_keys = self._deployment_record(deployment_id)
            prior = command_keys.get("rollback")
            if prior is not None:
                if prior != idempotency_key:
                    raise EdgeStateError(
                        "deployment_idempotency_conflict",
                        "Rollback was already requested with another idempotency key.",
                    )
                return state
            command_keys["rollback"] = idempotency_key
            rolled_back = state.model_copy(
                update={"status": "rolled_back", "rolled_back_at": _utc_now()}
            )
            restored = self.store.rollback_deployment(
                deployment_id, rolled_back, command_keys, updated_at=_utc_now()
            )
            self.supervisor.stop(deployment_id)
            if restored is not None:
                restored_manifest, _state, _slot, _keys = self._deployment_record(
                    restored.deployment_id
                )
                self.supervisor.start(restored_manifest)
            return rolled_back

    def stop(self, deployment_id: str, idempotency_key: str) -> EdgeDeploymentState:
        self.ensure_online()
        with self._lock:
            _manifest, state, _slot, command_keys = self._deployment_record(deployment_id)
            prior = command_keys.get("stop")
            if prior is not None:
                if prior != idempotency_key:
                    raise EdgeStateError(
                        "deployment_idempotency_conflict",
                        "Stop was already requested with another idempotency key.",
                    )
                return state
            command_keys["stop"] = idempotency_key
            stopped = state.model_copy(update={"status": "stopped", "last_error": None})
            self.supervisor.stop(deployment_id)
            self.store.stop_deployment(deployment_id, stopped, command_keys, updated_at=_utc_now())
            return stopped

    def deployment(self, deployment_id: str) -> EdgeDeploymentState:
        self.ensure_online()
        return self._deployment_record(deployment_id)[1]

    def enqueue_result(
        self,
        *,
        channel_id: str,
        workload_release_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        result_type: str = "pipeline_execution",
    ) -> EdgeResultEnvelope:
        with self._lock:
            sequence = self.store.next_sequence(self.boot_id)
            envelope = EdgeResultEnvelope(
                management_ip=self.config.address.management_ip,
                boot_id=self.boot_id,
                sequence=sequence,
                channel_id=channel_id,
                workload_release_id=workload_release_id,
                correlation_id=correlation_id or str(uuid.uuid4()),
                occurred_at=_utc_now(),
                result_type=result_type,
                payload=payload,
            )
            size = _result_size(envelope)
            if size > self.config.max_result_batch_bytes:
                raise EdgeStateError(
                    "result_too_large",
                    "Result envelope exceeds the configured outbox batch size.",
                    status_code=413,
                )
            items, used_bytes = self.store.outbox_usage()
            if (
                items + 1 > self.config.result_outbox_max_items
                or used_bytes + size > self.config.result_outbox_quota_bytes
            ):
                self._outbox_capacity_rejected = True
                raise EdgeStateError(
                    "result_outbox_full",
                    "The durable result outbox quota is exhausted.",
                    status_code=507,
                    retryable=True,
                )
            self.store.insert_result(envelope, size_bytes=size)
            self._outbox_capacity_rejected = False
            return envelope

    def trigger_channel(
        self,
        channel_id: str,
        request: EdgePlantTriggerRequest,
        *,
        idempotency_key: str,
    ) -> EdgePlantTriggerResponse:
        self.ensure_online()
        request_digest = hashlib.sha256(
            request.model_dump_json(exclude_none=False).encode("utf-8")
        ).hexdigest()
        lock_key = (channel_id, idempotency_key)
        with self._lock:
            trigger_lock = self._trigger_locks.setdefault(lock_key, threading.Lock())
        with trigger_lock:
            existing = self.store.plant_trigger_response(channel_id, idempotency_key)
            if existing is not None:
                existing_digest, response = existing
                if existing_digest != request_digest:
                    raise EdgeStateError(
                        "trigger_idempotency_conflict",
                        "The trigger idempotency key was reused with another request.",
                    )
                return response.model_copy(update={"duplicate": True})
            correlation_id = request.correlation_id or f"plant:{uuid.uuid4()}"
            try:
                manifest, _frame, execution, envelope = self.supervisor.trigger(
                    channel_id,
                    payload=request.payload,
                    correlation_id=correlation_id,
                )
            except EdgeRuntimeTriggerError as exc:
                status_code = 404 if exc.code == "channel_not_running" else 503
                raise EdgeStateError(
                    exc.code,
                    str(exc),
                    status_code=status_code,
                    retryable=status_code == 503,
                ) from exc
            response = EdgePlantTriggerResponse(
                channel_id=channel_id,
                workload_release_id=manifest.workload_release_id,
                correlation_id=correlation_id,
                boot_id=self.boot_id,
                result_sequence=envelope.sequence,
                execution_result=(
                    execution
                    if request.include_overlay
                    else {**execution, "overlay_primitives": []}
                ),
                completed_at=_utc_now(),
            )
            self.store.save_plant_trigger_response(
                channel_id,
                idempotency_key,
                request_digest,
                response,
                created_at=response.completed_at,
            )
            return response

    def last_trigger_result(self, channel_id: str) -> EdgePlantLastResultResponse:
        self.ensure_online()
        result = self.store.latest_plant_trigger_response(channel_id)
        return EdgePlantLastResultResponse(
            channel_id=channel_id,
            available=result is not None,
            result=result,
        )

    def results(self, *, boot_id: str | None, after_sequence: int, limit: int) -> EdgeResultBatch:
        self.ensure_online()
        selected_boot_id = boot_id or self.boot_id
        acknowledged = self.store.acknowledged_sequence(selected_boot_id)
        effective_after = max(after_sequence, acknowledged)
        matching = self.store.results_after(selected_boot_id, effective_after)
        items: list[EdgeResultEnvelope] = []
        batch_bytes = 0
        for item in matching:
            if len(items) >= limit:
                break
            item_bytes = _result_size(item)
            if items and batch_bytes + item_bytes > self.config.max_result_batch_bytes:
                break
            items.append(item)
            batch_bytes += item_bytes
        return EdgeResultBatch(
            management_ip=self.config.address.management_ip,
            boot_id=selected_boot_id,
            after_sequence=effective_after,
            items=items,
            highest_sequence=self.store.highest_sequence(selected_boot_id),
            has_more=len(matching) > len(items),
        )

    def result_streams(self) -> EdgeResultStreamList:
        self.ensure_online()
        items = self.store.result_streams()
        if not any(item.boot_id == self.boot_id for item in items):
            items.append(
                EdgeResultStreamSummary(
                    boot_id=self.boot_id,
                    earliest_sequence=0,
                    highest_sequence=self.store.highest_sequence(self.boot_id),
                    acknowledged_sequence=self.store.acknowledged_sequence(self.boot_id),
                    pending_count=0,
                )
            )
        return EdgeResultStreamList(
            management_ip=self.config.address.management_ip,
            current_boot_id=self.boot_id,
            items=items,
        )

    def acknowledge(self, ack: EdgeResultAck) -> EdgeResultAck:
        self.ensure_online()
        if ack.management_ip != self.config.address.management_ip:
            raise EdgeStateError(
                "result_device_mismatch",
                "Result acknowledgement is addressed to another device.",
                status_code=422,
            )
        with self._lock:
            existing = self.store.ack_by_key(ack.idempotency_key)
            if existing is not None:
                if existing != ack:
                    raise EdgeStateError(
                        "result_ack_idempotency_conflict",
                        "Acknowledgement idempotency key was reused with another payload.",
                    )
                return existing
            current = self.store.acknowledged_sequence(ack.boot_id)
            target = ack.acknowledged_through_sequence
            if target < current:
                response = ack.model_copy(update={"acknowledged_through_sequence": current})
                self.store.acknowledge(response)
                return response
            if not self.store.has_contiguous_results(ack.boot_id, current + 1, target):
                raise EdgeStateError(
                    "result_ack_gap",
                    "Acknowledgement may only advance over a contiguous result sequence.",
                )
            self.store.acknowledge(ack)
            items, used_bytes = self.store.outbox_usage()
            if (
                items < self.config.result_outbox_max_items
                and used_bytes < self.config.result_outbox_quota_bytes
            ):
                self._outbox_capacity_rejected = False
            return ack

    def restart(self) -> str:
        with self._lock:
            self.boot_id = str(uuid.uuid4())
            return self.boot_id

    def acquire_media_lease(self, request: EdgeMediaLeaseRequest) -> EdgeMediaLeaseState:
        self.ensure_online()
        running = [
            manifest
            for manifest, state, _slot, _keys in self.store.deployments()
            if manifest.channel_id == request.channel_id and state.status == "running"
        ]
        if not running:
            raise EdgeStateError(
                "media_channel_not_running",
                "A preview lease requires an active deployment for the channel.",
                status_code=409,
                retryable=True,
            )
        lease = EdgeMediaLeaseState(
            lease_id=request.lease_id,
            channel_id=request.channel_id,
            expires_at=(datetime.now(UTC) + timedelta(seconds=request.ttl_seconds)).isoformat(),
            max_fps=request.max_fps,
            max_width=request.max_width,
            include_overlay=request.include_overlay,
        )
        self.store.save_media_lease(lease)
        return lease

    def release_media_lease(self, lease_id: str) -> None:
        self.ensure_online()
        self.store.release_media_lease(lease_id)

    def preview_frame(
        self,
        *,
        lease_id: str,
        channel_id: str,
        after_sequence: int,
    ) -> EdgePreviewFrame:
        self.ensure_online()
        lease = self.store.media_lease(lease_id, now=_utc_now())
        if lease is None or lease.channel_id != channel_id:
            raise EdgeStateError(
                "media_lease_invalid",
                "The preview lease is missing, expired, or belongs to another channel.",
                status_code=409,
                retryable=True,
            )
        metadata_path, image_path = self._preview_paths(channel_id)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            metadata = {}
        sequence = int(metadata.get("sequence") or 0)
        if (
            not metadata
            or metadata.get("boot_id") != self.boot_id
            or sequence <= after_sequence
            or not image_path.is_file()
        ):
            return EdgePreviewFrame(
                channel_id=channel_id,
                workload_release_id=metadata.get("workload_release_id"),
                boot_id=self.boot_id,
                sequence=max(sequence, 0),
            )
        image_bytes = image_path.read_bytes()
        return EdgePreviewFrame(
            channel_id=channel_id,
            workload_release_id=metadata.get("workload_release_id"),
            boot_id=self.boot_id,
            available=True,
            sequence=sequence,
            image_base64=base64.b64encode(image_bytes).decode("ascii"),
            width=metadata.get("width"),
            height=metadata.get("height"),
            captured_at=metadata.get("captured_at"),
            metadata=dict(metadata.get("metadata") or {}),
        )

    def publish_preview_bytes(
        self,
        *,
        channel_id: str,
        workload_release_id: str,
        image_bytes: bytes,
        width: int,
        height: int,
        captured_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EdgePreviewFrame:
        if not image_bytes or len(image_bytes) > self.config.preview_max_bytes:
            raise EdgeStateError(
                "preview_too_large",
                "The encoded preview exceeds the configured Edge preview limit.",
                status_code=413,
            )
        with self._lock:
            metadata_path, image_path = self._preview_paths(channel_id)
            try:
                previous = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                previous = {}
            sequence = int(previous.get("sequence") or 0) + 1
            payload = {
                "channel_id": channel_id,
                "workload_release_id": workload_release_id,
                "boot_id": self.boot_id,
                "sequence": sequence,
                "width": width,
                "height": height,
                "captured_at": captured_at or _utc_now(),
                "metadata": metadata or {},
            }
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_tmp = image_path.with_suffix(".jpg.tmp")
            metadata_tmp = metadata_path.with_suffix(".json.tmp")
            image_tmp.write_bytes(image_bytes)
            metadata_tmp.write_text(json.dumps(payload), encoding="utf-8")
            image_tmp.replace(image_path)
            metadata_tmp.replace(metadata_path)
        return EdgePreviewFrame(
            channel_id=channel_id,
            workload_release_id=workload_release_id,
            boot_id=self.boot_id,
            available=True,
            sequence=sequence,
            image_base64=base64.b64encode(image_bytes).decode("ascii"),
            width=width,
            height=height,
            captured_at=payload["captured_at"],
            metadata=metadata or {},
        )

    def _recover_committed_workloads(self) -> None:
        manifests: list[EdgeDeploymentManifest] = []
        for manifest, state, _slot, _keys in self.store.deployments():
            if state.status != "running":
                continue
            recovered = state.model_copy(update={"recovery_count": state.recovery_count + 1})
            self.store.save_deployment_state(state.deployment_id, recovered, updated_at=_utc_now())
            manifests.append(manifest)
        self.supervisor.recover(manifests)

    def _create_graph_executor(self, manifest: EdgeDeploymentManifest) -> EdgeGraphExecutor | None:
        record = self.store.deployment(manifest.deployment_id)
        if record is None:
            raise RuntimeError(f"Deployment {manifest.deployment_id} has no persisted state.")
        engine_builds = record[1].engine_builds
        inference_runtime = self.engine_manager.create_inference_runtime(manifest, engine_builds)
        try:
            return EdgeGraphExecutor.from_manifest(
                manifest,
                artifact_resolver=self._blob_path,
                inference_runtime=inference_runtime,
            )
        except Exception:
            if inference_runtime is not None:
                inference_runtime.close()
            raise

    def _runtime_result_sink(
        self,
        manifest: EdgeDeploymentManifest,
        frame: SourceFrame,
        payload: dict[str, Any],
    ) -> EdgeResultEnvelope:
        is_execution = payload.get("schema_version") == "runtime.execution.result.v1"
        return self.enqueue_result(
            channel_id=manifest.channel_id,
            workload_release_id=manifest.workload_release_id,
            correlation_id=str(
                payload.get("correlation_id") or f"edge-frame:{self.boot_id}:{frame.index}"
            ),
            result_type="pipeline_execution" if is_execution else "source_frame",
            payload=payload,
        )

    def _preview_requested(self, channel_id: str) -> bool:
        return bool(self.store.active_media_leases(channel_id, now=_utc_now()))

    def _runtime_preview_sink(
        self,
        manifest: EdgeDeploymentManifest,
        frame: SourceFrame,
        execution: dict[str, Any] | None,
    ) -> None:
        leases = self.store.active_media_leases(manifest.channel_id, now=_utc_now())
        if not leases or frame.image is None:
            return
        max_fps = max(lease.max_fps for lease in leases)
        now = time.monotonic()
        if now - self._preview_last_emitted.get(manifest.channel_id, 0.0) < 1.0 / max_fps:
            return
        try:
            import cv2  # type: ignore[import-not-found]

            image = frame.image.copy()
            overlays = list((execution or {}).get("overlay_primitives") or [])
            include_overlay = any(lease.include_overlay for lease in leases)
            if include_overlay and overlays:
                _draw_overlays(image, overlays, cv2)
            height, width = image.shape[:2]
            max_width = max(lease.max_width for lease in leases)
            if width > max_width:
                target_height = max(1, round(height * max_width / width))
                image = cv2.resize(image, (max_width, target_height), interpolation=cv2.INTER_AREA)
                height, width = image.shape[:2]
            ok, encoded = cv2.imencode(
                ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self.config.preview_jpeg_quality]
            )
            if not ok:
                return
            self.publish_preview_bytes(
                channel_id=manifest.channel_id,
                workload_release_id=manifest.workload_release_id,
                image_bytes=encoded.tobytes(),
                width=int(width),
                height=int(height),
                captured_at=frame.captured_at,
                metadata={
                    "source_frame_index": frame.index,
                    "overlay_included": bool(include_overlay and overlays),
                    "overlay_primitives": overlays if include_overlay else [],
                    "correlation_id": (execution or {}).get("correlation_id"),
                },
            )
            self._preview_last_emitted[manifest.channel_id] = now
        except (ImportError, EdgeStateError, ValueError):
            return

    def _preview_paths(self, channel_id: str) -> tuple[Path, Path]:
        key = hashlib.sha256(channel_id.encode("utf-8")).hexdigest()
        root = self.config.data_root / "previews"
        return root / f"{key}.json", root / f"{key}.jpg"

    def _storage_status(self) -> EdgeStorageQuotaStatus:
        disk = shutil.disk_usage(self.config.data_root)
        artifact_bytes = _directory_size(self.blob_root) + _directory_size(self.upload_root)
        outbox_items, outbox_bytes = self.store.outbox_usage()
        pressure = (
            self.faults.disk_full
            or self._artifact_capacity_rejected
            or self._outbox_capacity_rejected
            or artifact_bytes >= self.config.artifact_quota_bytes
            or outbox_items >= self.config.result_outbox_max_items
            or outbox_bytes >= self.config.result_outbox_quota_bytes
            or disk.free < self.config.minimum_disk_free_bytes
        )
        return EdgeStorageQuotaStatus(
            artifact_bytes=artifact_bytes,
            artifact_quota_bytes=self.config.artifact_quota_bytes,
            outbox_bytes=outbox_bytes,
            outbox_quota_bytes=self.config.result_outbox_quota_bytes,
            outbox_items=outbox_items,
            outbox_max_items=self.config.result_outbox_max_items,
            disk_free_bytes=disk.free,
            minimum_disk_free_bytes=self.config.minimum_disk_free_bytes,
            pressure=pressure,
        )

    def _ensure_artifact_capacity(self, additional_bytes: int) -> None:
        status = self._storage_status()
        if self.faults.disk_full:
            raise EdgeStateError(
                "disk_full",
                "Edge artifact storage is full.",
                status_code=507,
                retryable=True,
            )
        if (
            status.artifact_bytes + additional_bytes > status.artifact_quota_bytes
            or status.disk_free_bytes - additional_bytes < status.minimum_disk_free_bytes
        ):
            self._artifact_capacity_rejected = True
            raise EdgeStateError(
                "artifact_quota_exceeded",
                "Edge artifact quota or disk free-space reserve would be exceeded.",
                status_code=507,
                retryable=True,
            )
        self._artifact_capacity_rejected = False

    def _blob_path(self, sha256: str) -> Path:
        return self.blob_root / sha256[:2] / sha256

    def _partial_path(self, sha256: str) -> Path:
        return self.upload_root / f"{sha256}.part"

    def _artifact_exists(self, artifact: EdgeArtifactDescriptor) -> bool:
        path = self._blob_path(artifact.sha256)
        return (
            path.exists()
            and path.stat().st_size == artifact.size_bytes
            and _sha256_file(path) == artifact.sha256
        )

    def _validate_target_profile(self, manifest: EdgeDeploymentManifest) -> None:
        profile = manifest.runtime_requirements.get("target_profile") or {}
        if not isinstance(profile, dict):
            raise EdgeStateError(
                "incompatible_runtime",
                "Deployment target_profile must be an object.",
                status_code=422,
            )

        capabilities = self.capabilities()
        expected = {
            "platform": capabilities.platform,
            "architecture": capabilities.architecture,
            "cuda_version": capabilities.cuda_version,
            "tensorrt_version": capabilities.tensorrt_version,
            "gpu_model": capabilities.gpu_model,
        }
        mismatches = {
            key: {"release": value, "device": expected[key]}
            for key, value in profile.items()
            if key in expected and value is not None and value != expected[key]
        }
        if mismatches:
            summary = "; ".join(
                f"{key}: release={values['release']!r}, device={values['device']!r}"
                for key, values in sorted(mismatches.items())
            )
            raise EdgeStateError(
                "incompatible_runtime",
                f"Edge runtime profile changed after central admission ({summary}).",
                status_code=422,
                detail={"mismatches": mismatches},
            )

    def _supported_node_types(self) -> tuple[str, ...]:
        registered = (
            set(build_productive_registry(None).supported_node_types) | edge_node_contracts().keys()
        )
        return tuple(
            node_type for node_type in self.config.supported_node_types if node_type in registered
        )

    def _deployment_record(
        self, deployment_id: str
    ) -> tuple[EdgeDeploymentManifest, EdgeDeploymentState, str, dict[str, str]]:
        record = self.store.deployment(deployment_id)
        if record is None:
            raise EdgeStateError(
                "deployment_not_found",
                f"Deployment {deployment_id} does not exist.",
                status_code=404,
            )
        return record


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _result_size(result: EdgeResultEnvelope) -> int:
    return len(result.model_dump_json().encode("utf-8"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _optional_environment(name: str, default: str) -> str | None:
    value = os.getenv(name, default).strip()
    return value or None


def _draw_overlays(
    image: Any,  # noqa: ANN401 - optional OpenCV array type
    overlays: list[dict[str, Any]],
    cv2: Any,  # noqa: ANN401 - optional OpenCV module type
) -> None:
    """Render the bounded E1 primitive subset; full primitive data stays in metadata."""
    for primitive in overlays[:500]:
        geometry = primitive.get("geometry") or {}
        primitive_type = primitive.get("primitive_type")
        color = (0, 220, 0)
        if primitive_type == "box":
            point_a = (int(geometry.get("x1", 0)), int(geometry.get("y1", 0)))
            point_b = (int(geometry.get("x2", 0)), int(geometry.get("y2", 0)))
            cv2.rectangle(image, point_a, point_b, color, 2)
            origin = (point_a[0], max(point_a[1] - 6, 12))
        elif primitive_type == "polygon":
            try:
                import numpy as np

                points = np.asarray(
                    [
                        [int(point.get("x", 0)), int(point.get("y", 0))]
                        for point in geometry.get("points") or []
                    ],
                    dtype=np.int32,
                )
                if len(points) >= 2:
                    cv2.polylines(image, [points], True, color, 2)
                origin = tuple(points[0]) if len(points) else (12, 24)
            except (TypeError, ValueError):
                origin = (12, 24)
        else:
            origin = (int(geometry.get("x", 12)), int(geometry.get("y", 24)))
        label = str(primitive.get("label") or "")
        confidence = primitive.get("confidence")
        if confidence is not None:
            label = f"{label} {float(confidence):.2f}".strip()
        if label:
            cv2.putText(
                image,
                label[:120],
                (int(origin[0]), int(origin[1])),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                cv2.LINE_AA,
            )
