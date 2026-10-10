from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass

from cv_contracts import (
    EdgeDeviceDetail,
    EdgeMediaLeaseRequest,
    EdgeResultAck,
    EdgeResultSyncDeviceStatus,
    EdgeResultSyncStatus,
    RuntimeLiveDiagnostics,
    RuntimeLiveFrame,
    RuntimeLiveMetrics,
    RuntimeLiveSourceHealth,
    RuntimeLiveViewState,
    RuntimeOverlayPrimitiveState,
)
from cv_core import PlatformSettings
from cv_db import (
    acknowledge_edge_result_cursor,
    active_edge_viewer_leases,
    edge_live_view_runtime_id,
    edge_result_cursor_sequences,
    get_edge_device_detail,
    list_edge_devices,
    persist_edge_result_batch,
    session_scope,
    write_runtime_live_view_state,
)
from cv_db.models import RuntimeChannel
from cv_edge import EdgeAgentClient, EdgeNetworkPolicy
from cv_status import HealthState, RuntimeObservedState
from cv_utils import utc_now_iso

ClientFactory = Callable[[EdgeDeviceDetail], EdgeAgentClient]


@dataclass
class _DeviceCounters:
    persisted: int = 0
    acknowledged: int = 0
    previews: int = 0


class EdgeResultSynchronizer:
    """Pulls durable Edge outboxes and relays previews only while viewers hold leases."""

    def __init__(
        self,
        settings: PlatformSettings,
        *,
        interval_seconds: float = 1.0,
        batch_limit: int = 100,
        max_batches_per_stream: int = 10,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        if not 1 <= batch_limit <= 1000:
            raise ValueError("batch_limit must be between 1 and 1000")
        self.settings = settings
        self.interval_seconds = interval_seconds
        self.batch_limit = batch_limit
        self.max_batches_per_stream = max(max_batches_per_stream, 1)
        self.client_factory = client_factory or _edge_client
        self.running = False
        self.cycle_count = 0
        self.persisted_results = 0
        self.acknowledged_results = 0
        self.preview_frames = 0
        self.last_cycle_started_at: str | None = None
        self.last_cycle_finished_at: str | None = None
        self.last_error: str | None = None
        self._devices: dict[str, EdgeResultSyncDeviceStatus] = {}
        self._preview_sequences: dict[tuple[str, str], int] = {}
        self._preview_boot_ids: dict[tuple[str, str], str] = {}
        self._remote_leases: set[tuple[str, str]] = set()

    @classmethod
    def from_environment(cls, settings: PlatformSettings) -> EdgeResultSynchronizer:
        return cls(
            settings,
            interval_seconds=float(os.getenv("YBB_EDGE_RESULT_SYNC_INTERVAL_SECONDS", "1")),
            batch_limit=int(os.getenv("YBB_EDGE_RESULT_SYNC_BATCH_LIMIT", "100")),
            max_batches_per_stream=int(
                os.getenv("YBB_EDGE_RESULT_SYNC_MAX_BATCHES_PER_STREAM", "10")
            ),
        )

    def status(self) -> EdgeResultSyncStatus:
        return EdgeResultSyncStatus(
            running=self.running,
            interval_seconds=self.interval_seconds,
            batch_limit=self.batch_limit,
            cycle_count=self.cycle_count,
            persisted_results=self.persisted_results,
            acknowledged_results=self.acknowledged_results,
            preview_frames=self.preview_frames,
            last_cycle_started_at=self.last_cycle_started_at,
            last_cycle_finished_at=self.last_cycle_finished_at,
            last_error=self.last_error,
            devices=sorted(self._devices.values(), key=lambda item: item.edge_device_id),
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        self.running = True
        try:
            while not stop_event.is_set():
                await asyncio.to_thread(self.sync_once)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    pass
        finally:
            self.running = False

    def sync_once(self) -> EdgeResultSyncStatus:
        self.cycle_count += 1
        self.last_cycle_started_at = utc_now_iso()
        self.last_cycle_finished_at = None
        self.last_error = None
        with session_scope(self.settings) as session:
            devices = [
                detail
                for summary in list_edge_devices(session, enabled=True)
                if (detail := get_edge_device_detail(session, summary.edge_device_id)) is not None
            ]
        failures: list[str] = []
        for device in devices:
            counters = _DeviceCounters()
            error: str | None = None
            try:
                self._sync_device(device, counters)
            except Exception as exc:  # noqa: BLE001 - one device must not stop fleet sync
                error = str(exc)
                failures.append(f"{device.edge_device_id}: {exc}")
            previous = self._devices.get(device.edge_device_id)
            self._devices[device.edge_device_id] = EdgeResultSyncDeviceStatus(
                edge_device_id=device.edge_device_id,
                last_cycle_at=utc_now_iso(),
                last_success_at=utc_now_iso() if error is None else (
                    previous.last_success_at if previous else None
                ),
                persisted_results=(previous.persisted_results if previous else 0)
                + counters.persisted,
                acknowledged_results=(previous.acknowledged_results if previous else 0)
                + counters.acknowledged,
                preview_frames=(previous.preview_frames if previous else 0) + counters.previews,
                last_error=error,
            )
            self.persisted_results += counters.persisted
            self.acknowledged_results += counters.acknowledged
            self.preview_frames += counters.previews
        self.last_error = "; ".join(failures)[:2000] if failures else None
        self.last_cycle_finished_at = utc_now_iso()
        return self.status()

    def _sync_device(
        self,
        device: EdgeDeviceDetail,
        counters: _DeviceCounters,
    ) -> None:
        with self.client_factory(device) as client:
            streams = client.result_streams()
            if streams.management_ip != device.address.management_ip:
                raise ValueError("Edge result-stream identity does not match the registered IP.")
            for stream in streams.items:
                self._sync_stream(
                    client,
                    device,
                    stream.boot_id,
                    counters,
                )
            counters.previews += self._sync_previews(client, device)

    def _sync_stream(
        self,
        client: EdgeAgentClient,
        device: EdgeDeviceDetail,
        boot_id: str,
        counters: _DeviceCounters,
    ) -> None:
        for _batch_index in range(self.max_batches_per_stream):
            with session_scope(self.settings) as session:
                persisted_sequence, acknowledged_sequence = edge_result_cursor_sequences(
                    session,
                    edge_device_id=device.edge_device_id,
                    boot_id=boot_id,
                )
            if persisted_sequence > acknowledged_sequence:
                self._ack_through(
                    client,
                    device,
                    boot_id=boot_id,
                    sequence=persisted_sequence,
                )
                counters.acknowledged += persisted_sequence - acknowledged_sequence
            batch = client.results(
                boot_id=boot_id,
                after_sequence=persisted_sequence,
                limit=self.batch_limit,
            )
            if batch.boot_id != boot_id or batch.management_ip != device.address.management_ip:
                raise ValueError("Edge returned a result batch for another identity domain.")
            if not batch.items:
                break
            with session_scope(self.settings) as session:
                outcome = persist_edge_result_batch(
                    session,
                    edge_device_id=device.edge_device_id,
                    batch=batch,
                )
            counters.persisted += outcome.inserted_count
            self._ack_through(
                client,
                device,
                boot_id=boot_id,
                sequence=outcome.persisted_through_sequence,
            )
            counters.acknowledged += len(batch.items)
            if not batch.has_more:
                break

    def _ack_through(
        self,
        client: EdgeAgentClient,
        device: EdgeDeviceDetail,
        *,
        boot_id: str,
        sequence: int,
    ) -> None:
        ack = EdgeResultAck(
            management_ip=device.address.management_ip,
            boot_id=boot_id,
            acknowledged_through_sequence=sequence,
            idempotency_key=(
                f"result-sync:{device.edge_device_id}:{boot_id}:{sequence}"
            )[:160],
        )
        confirmed = client.acknowledge_results(ack)
        if confirmed.acknowledged_through_sequence != ack.acknowledged_through_sequence:
            raise ValueError("Edge acknowledged a different result sequence than requested.")
        with session_scope(self.settings) as session:
            acknowledge_edge_result_cursor(
                session,
                edge_device_id=device.edge_device_id,
                boot_id=boot_id,
                acknowledged_sequence=confirmed.acknowledged_through_sequence,
            )

    def _sync_previews(self, client: EdgeAgentClient, device: EdgeDeviceDetail) -> int:
        with session_scope(self.settings) as session:
            leases = active_edge_viewer_leases(
                session,
                edge_device_id=device.edge_device_id,
            )
        by_channel: dict[str, list[object]] = {}
        for lease in leases:
            by_channel.setdefault(lease.channel_id, []).append(lease)
        active_keys = {(device.edge_device_id, channel_id) for channel_id in by_channel}
        expired_keys = {
            key for key in self._remote_leases if key[0] == device.edge_device_id
        } - active_keys
        for key in expired_keys:
            try:
                client.release_media_lease(_relay_lease_id(*key))
            finally:
                self._remote_leases.discard(key)

        preview_count = 0
        for channel_id, channel_leases in by_channel.items():
            max_fps = max(float(lease.max_fps) for lease in channel_leases)
            max_width = max(int(lease.max_width) for lease in channel_leases)
            include_overlay = any(bool(lease.include_overlay) for lease in channel_leases)
            relay_id = _relay_lease_id(device.edge_device_id, channel_id)
            client.acquire_media_lease(
                EdgeMediaLeaseRequest(
                    lease_id=relay_id,
                    channel_id=channel_id,
                    ttl_seconds=min(120, max(10, round(self.interval_seconds * 4))),
                    max_fps=max_fps,
                    max_width=max_width,
                    include_overlay=include_overlay,
                )
            )
            self._remote_leases.add((device.edge_device_id, channel_id))
            key = (device.edge_device_id, channel_id)
            known_boot_id = self._preview_boot_ids.get(key)
            preview = client.preview_frame(
                lease_id=relay_id,
                channel_id=channel_id,
                after_sequence=(self._preview_sequences.get(key, 0) if known_boot_id else 0),
            )
            if known_boot_id is not None and preview.boot_id != known_boot_id:
                preview = client.preview_frame(
                    lease_id=relay_id,
                    channel_id=channel_id,
                    after_sequence=0,
                )
            if not preview.available or not preview.image_base64:
                self._preview_boot_ids[key] = preview.boot_id
                if known_boot_id is not None and preview.boot_id != known_boot_id:
                    self._preview_sequences[key] = 0
                continue
            self._write_preview(device, preview)
            self._preview_boot_ids[key] = preview.boot_id
            self._preview_sequences[key] = preview.sequence
            preview_count += 1
        return preview_count

    def _write_preview(self, device: EdgeDeviceDetail, preview) -> None:  # noqa: ANN001
        with session_scope(self.settings) as session:
            channel = session.get(RuntimeChannel, preview.channel_id)
            if (
                channel is None
                or channel.deleted_at is not None
                or channel.edge_device_id != device.edge_device_id
                or channel.execution_target != "edge_device"
            ):
                raise ValueError("Edge preview references a channel outside the target device.")
            if (
                preview.workload_release_id
                and channel.edge_workload_release_id
                and preview.workload_release_id != channel.edge_workload_release_id
            ):
                raise ValueError("Edge preview belongs to an inactive workload release.")
            actual_state = RuntimeObservedState(channel.observed_state)
        metadata = dict(preview.metadata or {})
        overlay_payload = metadata.get("overlay_primitives") or []
        overlays: list[RuntimeOverlayPrimitiveState] = []
        for item in overlay_payload if isinstance(overlay_payload, list) else []:
            overlay = _edge_overlay_primitive(item, preview=preview)
            if overlay is not None:
                overlays.append(overlay)
        state = RuntimeLiveViewState(
            channel_id=preview.channel_id,
            runtime_instance_id=edge_live_view_runtime_id(preview.channel_id),
            actual_state=actual_state,
            available=True,
            stale=False,
            display_context_id=(
                f"edge:{device.edge_device_id}:{preview.boot_id}:{preview.sequence}"
            ),
            display_context={
                "kind": "edge_preview",
                "width": preview.width,
                "height": preview.height,
            },
            frame=RuntimeLiveFrame(
                image_base64=preview.image_base64,
                mime_type=preview.mime_type,
                width=preview.width,
                height=preview.height,
                source_timestamp=preview.captured_at,
                captured_at=preview.captured_at,
                sequence=preview.sequence,
                metadata={
                    **metadata,
                    "edge_device_id": device.edge_device_id,
                    "edge_boot_id": preview.boot_id,
                    "workload_release_id": preview.workload_release_id,
                },
            ),
            overlay=overlays,
            layers_available=sorted({item.layer for item in overlays}),
            metrics=RuntimeLiveMetrics(
                source_fps=_optional_float(metadata.get("source_fps")),
                processed_fps=_optional_float(metadata.get("processed_fps")),
                latency_ms=_optional_float(metadata.get("latency_ms")),
                frame_count=_nonnegative_count(metadata.get("processed_frames"), preview.sequence),
                acquired_frame_count=_nonnegative_count(metadata.get("acquired_frames")),
                last_frame_at=preview.captured_at,
                connected_clients=1,
            ),
            source_health=RuntimeLiveSourceHealth(
                state=HealthState.OK,
                status_text="Edge preview relayed over the management VLAN",
                connected=True,
            ),
            diagnostics=RuntimeLiveDiagnostics(
                source_type="edge_preview",
                stream_transport="edge-http-lease-relay",
                pipeline_node_timings=(
                    metadata.get("pipeline_node_timings")
                    if isinstance(metadata.get("pipeline_node_timings"), list)
                    else []
                ),
                warnings=(
                    []
                    if metadata.get("overlay_included")
                    else ["The Edge preview does not contain a composited overlay."]
                ),
            ),
            updated_at=utc_now_iso(),
        )
        write_runtime_live_view_state(self.settings, state, split_frame_payload=True)


def _relay_lease_id(edge_device_id: str, channel_id: str) -> str:
    digest = hashlib.sha256(f"{edge_device_id}:{channel_id}".encode()).hexdigest()
    return f"relay-{digest[:32]}"


def _edge_overlay_primitive(
    item: object,
    *,
    preview: object,
) -> RuntimeOverlayPrimitiveState | None:
    """Accept shared-runtime provenance fields without dropping the whole primitive."""
    if not isinstance(item, dict):
        return None
    payload = {
        key: value
        for key, value in item.items()
        if key in RuntimeOverlayPrimitiveState.model_fields
    }
    geometry = payload.get("geometry")
    if isinstance(geometry, dict):
        payload.setdefault("coordinate_system", geometry.get("coordinate_system"))
    payload["coordinate_system"] = payload.get("coordinate_system") or "image_pixels"
    payload.setdefault(
        "resolution",
        {"width": getattr(preview, "width", None), "height": getattr(preview, "height", None)},
    )
    producer = {
        key: item[key]
        for key in ("node_key", "node_type", "output_port")
        if item.get(key) is not None
    }
    if producer:
        metadata = payload.get("metadata")
        payload["metadata"] = {
            **(metadata if isinstance(metadata, dict) else {}),
            "producer": producer,
        }
    try:
        return RuntimeOverlayPrimitiveState.model_validate(payload)
    except Exception:
        return None


def _nonnegative_count(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value)) if value is not None else default
    except (TypeError, ValueError, OverflowError):
        return default


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _edge_client(device: EdgeDeviceDetail) -> EdgeAgentClient:
    policy = EdgeNetworkPolicy.from_strings(
        allowed_cidrs=[str(device.allowed_cidr)],
        registered_addresses=[str(device.address.management_ip)],
    )
    return EdgeAgentClient(
        address=device.address,
        network_policy=policy,
        connect_timeout_seconds=float(
            os.getenv("YBB_EDGE_RESULT_SYNC_CONNECT_TIMEOUT_SECONDS", "2")
        ),
        read_timeout_seconds=float(os.getenv("YBB_EDGE_RESULT_SYNC_READ_TIMEOUT_SECONDS", "10")),
    )
