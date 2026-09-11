from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from cv_contracts import EdgeDeviceDetail, EdgeFleetReconcilerStatus
from cv_core import PlatformSettings
from cv_db import (
    create_edge_fleet_command,
    finish_edge_fleet_command,
    get_edge_device_detail,
    list_edge_devices,
    record_edge_connectivity_failure,
    record_edge_observation,
    session_scope,
    start_edge_fleet_command,
)
from cv_edge import EdgeClientError

from runtime_admin_api.edge_reconciliation import EdgeProbeResult, probe_edge_device

Probe = Callable[[EdgeDeviceDetail], EdgeProbeResult]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class EdgeFleetReconciler:
    def __init__(
        self,
        *,
        settings: PlatformSettings,
        interval_seconds: float = 15.0,
        max_parallel_probes: int = 4,
        probe: Probe = probe_edge_device,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        self.settings = settings
        self.interval_seconds = interval_seconds
        self.max_parallel_probes = max(max_parallel_probes, 1)
        self.probe = probe
        self._running = False
        self._cycle_count = 0
        self._last_cycle_started_at: str | None = None
        self._last_cycle_finished_at: str | None = None
        self._last_cycle_device_count = 0
        self._last_cycle_success_count = 0
        self._last_cycle_failure_count = 0
        self._last_error: str | None = None

    @classmethod
    def from_environment(cls, settings: PlatformSettings) -> EdgeFleetReconciler:
        return cls(
            settings=settings,
            interval_seconds=float(os.getenv("YBB_EDGE_RECONCILE_INTERVAL_SECONDS", "15")),
            max_parallel_probes=int(os.getenv("YBB_EDGE_RECONCILE_MAX_PARALLEL", "4")),
        )

    def status(self) -> EdgeFleetReconcilerStatus:
        return EdgeFleetReconcilerStatus(
            running=self._running,
            interval_seconds=self.interval_seconds,
            cycle_count=self._cycle_count,
            last_cycle_started_at=self._last_cycle_started_at,
            last_cycle_finished_at=self._last_cycle_finished_at,
            last_cycle_device_count=self._last_cycle_device_count,
            last_cycle_success_count=self._last_cycle_success_count,
            last_cycle_failure_count=self._last_cycle_failure_count,
            last_error=self._last_error,
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        self._running = True
        try:
            while not stop_event.is_set():
                try:
                    await self.reconcile_once()
                except Exception as exc:  # noqa: BLE001 - keep the control loop alive
                    self._last_error = str(exc)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    continue
        finally:
            self._running = False

    async def reconcile_once(self) -> EdgeFleetReconcilerStatus:
        self._cycle_count += 1
        cycle_id = f"auto:{self._cycle_count}:{uuid.uuid4()}"
        self._last_cycle_started_at = _utc_now()
        self._last_cycle_finished_at = None
        self._last_error = None
        with session_scope(self.settings) as session:
            summaries = list_edge_devices(session, enabled=True)
            devices = [
                detail
                for summary in summaries
                if (detail := get_edge_device_detail(session, summary.edge_device_id)) is not None
            ]
        self._last_cycle_device_count = len(devices)
        semaphore = asyncio.Semaphore(self.max_parallel_probes)

        async def reconcile(device: EdgeDeviceDetail) -> bool:
            async with semaphore:
                return await asyncio.to_thread(self._reconcile_device, device, cycle_id)

        outcomes = await asyncio.gather(*(reconcile(device) for device in devices))
        self._last_cycle_success_count = sum(outcomes)
        self._last_cycle_failure_count = len(outcomes) - self._last_cycle_success_count
        self._last_cycle_finished_at = _utc_now()
        return self.status()

    def _reconcile_device(self, device: EdgeDeviceDetail, cycle_id: str) -> bool:
        idempotency_key = f"{cycle_id}:{device.edge_device_id}"
        with session_scope(self.settings) as session:
            command = create_edge_fleet_command(
                session,
                device.edge_device_id,
                command_type="reconcile",
                idempotency_key=idempotency_key,
                request_payload={"mode": "automatic", "cycle_id": cycle_id},
            )
            start_edge_fleet_command(session, command.command_id)
        try:
            observation = self.probe(device)
        except (EdgeClientError, OSError, ValueError) as exc:
            with session_scope(self.settings) as session:
                record_edge_connectivity_failure(
                    session, device.edge_device_id, message=str(exc)
                )
                finish_edge_fleet_command(
                    session,
                    command.command_id,
                    succeeded=False,
                    last_error=str(exc),
                )
            return False
        with session_scope(self.settings) as session:
            record_edge_observation(
                session,
                device.edge_device_id,
                identity=observation.identity,
                health=observation.health,
                capabilities=observation.capabilities,
            )
            finish_edge_fleet_command(
                session,
                command.command_id,
                succeeded=True,
                response_payload={
                    "mode": "automatic",
                    "boot_id": observation.identity.boot_id,
                    "health_state": observation.health.state,
                },
            )
        return True
