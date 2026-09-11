from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv4Network
from typing import Any

from cv_contracts import (
    EdgeCapabilitySnapshot,
    EdgeCapabilitySnapshotRecord,
    EdgeDeviceAddress,
    EdgeDeviceDetail,
    EdgeDeviceHealth,
    EdgeDeviceIdentity,
    EdgeDeviceRegistrationRequest,
    EdgeDeviceSummary,
    EdgeDeviceUpdateRequest,
    EdgeFleetCommandSummary,
    EdgeResultCursorSummary,
)
from cv_core import generate_prefixed_id
from cv_utils import utc_now
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cv_db.models import (
    EdgeCapabilitySnapshotRecord as EdgeCapabilitySnapshotRow,
)
from cv_db.models import (
    EdgeDevice,
    EdgeFleetCommandRecord,
    EdgeResultCursorRecord,
    RuntimeChannel,
)

_CONNECTIVITY_STALE_AFTER = timedelta(seconds=90)
_VOLATILE_CAPABILITY_FIELDS = {"observed_at", "storage_free_bytes"}


def create_edge_device(
    session: Session,
    payload: EdgeDeviceRegistrationRequest,
    *,
    created_by: str | None,
) -> EdgeDeviceDetail:
    management_ip = str(payload.address.management_ip)
    existing = session.scalar(select(EdgeDevice).where(EdgeDevice.management_ip == management_ip))
    if existing is not None:
        lifecycle = "archived" if existing.deleted_at is not None else "active"
        raise ValueError(
            f"Edge device IP {management_ip} is already registered ({lifecycle} record)."
        )
    row = EdgeDevice(
        id=generate_prefixed_id("edge"),
        name=payload.name,
        technical_place=payload.technical_place,
        management_ip=management_ip,
        control_port=payload.address.control_port,
        plant_api_port=payload.address.plant_api_port,
        rtsp_port=payload.address.rtsp_port,
        allowed_cidr=str(payload.allowed_cidr),
        server_ip=str(payload.server_ip),
        enabled=payload.enabled,
        connectivity_state="unknown",
        identity_generation=1,
        deployment_revalidation_required=True,
        created_by=created_by,
        updated_by=created_by,
        updated_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return get_edge_device_detail(session, row.id)  # type: ignore[return-value]


def list_edge_devices(
    session: Session,
    *,
    search: str | None = None,
    connectivity_state: str | None = None,
    enabled: bool | None = None,
) -> list[EdgeDeviceSummary]:
    rows = list(
        session.scalars(
            select(EdgeDevice)
            .where(EdgeDevice.deleted_at.is_(None))
            .order_by(EdgeDevice.name.asc(), EdgeDevice.management_ip.asc())
        ).all()
    )
    counts = _assigned_channel_counts(session, [row.id for row in rows])
    items = [_device_summary(row, assigned_channel_count=counts.get(row.id, 0)) for row in rows]
    if search:
        needle = search.strip().casefold()
        items = [
            item
            for item in items
            if needle
            in " ".join(
                [
                    item.name,
                    item.technical_place or "",
                    str(item.address.management_ip),
                    item.reported_health_state or "",
                ]
            ).casefold()
        ]
    if connectivity_state:
        items = [item for item in items if item.connectivity_state == connectivity_state]
    if enabled is not None:
        items = [item for item in items if item.enabled is enabled]
    return items


def get_edge_device_detail(session: Session, edge_device_id: str) -> EdgeDeviceDetail | None:
    row = session.get(EdgeDevice, edge_device_id)
    if row is None or row.deleted_at is not None:
        return None
    assigned_channel_count = (
        session.scalar(
            select(func.count(RuntimeChannel.id)).where(
                RuntimeChannel.edge_device_id == row.id,
                RuntimeChannel.deleted_at.is_(None),
            )
        )
        or 0
    )
    capability_row = (
        session.get(EdgeCapabilitySnapshotRow, row.latest_capability_snapshot_id)
        if row.latest_capability_snapshot_id
        else None
    )
    commands = list(
        session.scalars(
            select(EdgeFleetCommandRecord)
            .where(EdgeFleetCommandRecord.edge_device_id == row.id)
            .order_by(EdgeFleetCommandRecord.created_at.desc())
            .limit(20)
        ).all()
    )
    cursors = list(
        session.scalars(
            select(EdgeResultCursorRecord)
            .where(EdgeResultCursorRecord.edge_device_id == row.id)
            .order_by(EdgeResultCursorRecord.updated_at.desc())
        ).all()
    )
    summary = _device_summary(row, assigned_channel_count=int(assigned_channel_count))
    return EdgeDeviceDetail(
        **summary.model_dump(),
        identity=(
            EdgeDeviceIdentity.model_validate(row.identity_payload)
            if row.identity_payload
            else None
        ),
        health=(
            EdgeDeviceHealth.model_validate(row.health_payload) if row.health_payload else None
        ),
        latest_capability_snapshot=(
            _capability_snapshot_to_api(capability_row) if capability_row is not None else None
        ),
        recent_commands=[_command_to_api(command) for command in commands],
        result_cursors=[_cursor_to_api(cursor) for cursor in cursors],
    )


def update_edge_device(
    session: Session,
    edge_device_id: str,
    payload: EdgeDeviceUpdateRequest,
    *,
    updated_by: str | None,
) -> EdgeDeviceDetail:
    row = _get_edge_device(session, edge_device_id)
    network_changed = False
    if payload.name is not None:
        row.name = payload.name
    if "technical_place" in payload.model_fields_set:
        row.technical_place = payload.technical_place
    for field_name in ("control_port", "plant_api_port", "rtsp_port"):
        value = getattr(payload, field_name)
        if value is not None and value != getattr(row, field_name):
            setattr(row, field_name, value)
            network_changed = True
    allowed_cidr = payload.allowed_cidr or IPv4Network(row.allowed_cidr)
    server_ip = payload.server_ip or IPv4Address(row.server_ip)
    if IPv4Address(row.management_ip) not in allowed_cidr:
        raise ValueError("Existing management_ip must belong to allowed_cidr.")
    if server_ip not in allowed_cidr:
        raise ValueError("server_ip must belong to allowed_cidr.")
    if server_ip == IPv4Address(row.management_ip):
        raise ValueError("server_ip and management_ip must differ.")
    if str(allowed_cidr) != row.allowed_cidr or str(server_ip) != row.server_ip:
        row.allowed_cidr = str(allowed_cidr)
        row.server_ip = str(server_ip)
        network_changed = True
    if payload.enabled is not None:
        row.enabled = payload.enabled
        if not row.enabled:
            row.connectivity_state = "unknown"
    if network_changed:
        row.connectivity_state = "unknown"
        row.last_connectivity_error = None
        row.deployment_revalidation_required = True
    row.updated_by = updated_by
    row.updated_at = utc_now()
    session.flush()
    return get_edge_device_detail(session, edge_device_id)  # type: ignore[return-value]


def archive_edge_device(
    session: Session,
    edge_device_id: str,
    *,
    archived_by: str | None,
) -> EdgeDeviceDetail:
    row = _get_edge_device(session, edge_device_id)
    assigned = (
        session.scalar(
            select(func.count(RuntimeChannel.id)).where(
                RuntimeChannel.edge_device_id == row.id,
                RuntimeChannel.deleted_at.is_(None),
            )
        )
        or 0
    )
    if assigned:
        raise ValueError("Edge device cannot be archived while channels are assigned to it.")
    detail = get_edge_device_detail(session, edge_device_id)
    row.deleted_at = utc_now()
    row.updated_by = archived_by
    row.updated_at = utc_now()
    session.flush()
    assert detail is not None
    return detail


def record_edge_observation(
    session: Session,
    edge_device_id: str,
    *,
    identity: EdgeDeviceIdentity,
    health: EdgeDeviceHealth,
    capabilities: EdgeCapabilitySnapshot,
) -> EdgeDeviceDetail:
    row = _get_edge_device(session, edge_device_id)
    expected_ip = row.management_ip
    if str(identity.address.management_ip) != expected_ip:
        raise ValueError("Edge identity management_ip does not match the registered device.")
    if str(capabilities.management_ip) != expected_ip:
        raise ValueError("Capability management_ip does not match the registered device.")
    if health.boot_id != identity.boot_id:
        raise ValueError("Identity and health boot_id values do not match.")

    previous_boot_id = row.current_boot_id
    capability_payload = capabilities.model_dump(mode="json")
    # Observation time and currently free storage describe the sample, not the
    # device's compatibility. Excluding both keeps releases pinned to a stable
    # capability fingerprint while storage admission still uses the refreshed
    # payload from the latest observation.
    capability_digest = _payload_digest(
        {
            key: value
            for key, value in capability_payload.items()
            if key not in _VOLATILE_CAPABILITY_FIELDS
        }
    )
    boot_changed = previous_boot_id is not None and previous_boot_id != identity.boot_id
    capability_changed = (
        row.latest_capability_digest is not None
        and row.latest_capability_digest != capability_digest
    )
    if boot_changed:
        row.identity_generation += 1
    if boot_changed or capability_changed:
        row.deployment_revalidation_required = True

    observed_at = utc_now()
    if row.latest_capability_digest != capability_digest:
        snapshot = EdgeCapabilitySnapshotRow(
            id=generate_prefixed_id("ecap"),
            edge_device_id=row.id,
            digest=capability_digest,
            boot_id=identity.boot_id,
            observed_at=observed_at,
            payload=capability_payload,
        )
        session.add(snapshot)
        session.flush()
        row.latest_capability_snapshot_id = snapshot.id
        row.latest_capability_digest = capability_digest
    elif row.latest_capability_snapshot_id:
        # Refresh volatile inventory values without creating a new compatibility
        # version. In particular, storage admission must use current free space.
        snapshot = session.get(EdgeCapabilitySnapshotRow, row.latest_capability_snapshot_id)
        if snapshot is not None:
            snapshot.boot_id = identity.boot_id
            snapshot.observed_at = observed_at
            snapshot.payload = capability_payload

    now = observed_at
    row.identity_payload = identity.model_dump(mode="json")
    row.health_payload = health.model_dump(mode="json")
    row.reported_health_state = health.state
    row.connectivity_state = "online"
    row.last_seen_at = now
    row.last_poll_attempt_at = now
    row.last_connectivity_error = None
    row.current_boot_id = identity.boot_id
    row.updated_at = now
    session.flush()
    return get_edge_device_detail(session, edge_device_id)  # type: ignore[return-value]


def record_edge_connectivity_failure(
    session: Session,
    edge_device_id: str,
    *,
    message: str,
) -> EdgeDeviceDetail:
    row = _get_edge_device(session, edge_device_id)
    row.connectivity_state = "offline"
    row.last_poll_attempt_at = utc_now()
    row.last_connectivity_error = message[:2000]
    row.updated_at = utc_now()
    session.flush()
    return get_edge_device_detail(session, edge_device_id)  # type: ignore[return-value]


def create_edge_fleet_command(
    session: Session,
    edge_device_id: str,
    *,
    command_type: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
) -> EdgeFleetCommandSummary:
    _get_edge_device(session, edge_device_id)
    existing = session.scalar(
        select(EdgeFleetCommandRecord).where(
            EdgeFleetCommandRecord.edge_device_id == edge_device_id,
            EdgeFleetCommandRecord.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.command_type != command_type or existing.request_payload != request_payload:
            raise ValueError("Edge command idempotency key was reused with another request.")
        return _command_to_api(existing)
    row = EdgeFleetCommandRecord(
        id=generate_prefixed_id("ecmd"),
        edge_device_id=edge_device_id,
        command_type=command_type,
        idempotency_key=idempotency_key,
        status="queued",
        attempt_count=0,
        request_payload=request_payload,
        response_payload={},
    )
    session.add(row)
    session.flush()
    return _command_to_api(row)


def start_edge_fleet_command(
    session: Session,
    command_id: str,
) -> EdgeFleetCommandSummary:
    row = _get_edge_command(session, command_id)
    if row.status in {"succeeded", "failed"}:
        return _command_to_api(row)
    row.status = "running"
    row.attempt_count += 1
    row.started_at = row.started_at or utc_now()
    session.flush()
    return _command_to_api(row)


def finish_edge_fleet_command(
    session: Session,
    command_id: str,
    *,
    succeeded: bool,
    response_payload: dict[str, Any] | None = None,
    last_error: str | None = None,
) -> EdgeFleetCommandSummary:
    row = _get_edge_command(session, command_id)
    row.status = "succeeded" if succeeded else "failed"
    row.response_payload = response_payload or {}
    row.last_error = last_error
    row.finished_at = utc_now()
    session.flush()
    return _command_to_api(row)


def advance_edge_result_cursor(
    session: Session,
    edge_device_id: str,
    *,
    boot_id: str,
    persisted_sequence: int,
    acknowledged_sequence: int,
) -> EdgeResultCursorSummary:
    device = _get_edge_device(session, edge_device_id)
    if not boot_id or len(boot_id) > 128:
        raise ValueError("Result cursor boot_id must contain 1 to 128 characters.")
    if persisted_sequence < 0 or acknowledged_sequence < 0:
        raise ValueError("Result cursor sequences cannot be negative.")
    if acknowledged_sequence > persisted_sequence:
        raise ValueError("Acknowledged sequence cannot exceed the persisted sequence.")
    row = session.scalar(
        select(EdgeResultCursorRecord).where(
            EdgeResultCursorRecord.edge_device_id == edge_device_id,
            EdgeResultCursorRecord.boot_id == boot_id,
        )
    )
    if row is None:
        row = EdgeResultCursorRecord(
            id=generate_prefixed_id("ecur"),
            edge_device_id=edge_device_id,
            management_ip=device.management_ip,
            boot_id=boot_id,
            last_persisted_sequence=0,
            last_acknowledged_sequence=0,
            updated_at=utc_now(),
        )
        session.add(row)
    if persisted_sequence < row.last_persisted_sequence:
        raise ValueError("Persisted result sequence cannot move backwards.")
    if acknowledged_sequence < row.last_acknowledged_sequence:
        raise ValueError("Acknowledged result sequence cannot move backwards.")
    row.last_persisted_sequence = persisted_sequence
    row.last_acknowledged_sequence = acknowledged_sequence
    row.updated_at = utc_now()
    session.flush()
    return _cursor_to_api(row)


def validate_edge_channel_target(session: Session, edge_device_id: str) -> EdgeDevice:
    row = _get_edge_device(session, edge_device_id)
    if not row.enabled:
        raise ValueError("Edge device is disabled and cannot receive channel assignments.")
    return row


def _get_edge_device(session: Session, edge_device_id: str) -> EdgeDevice:
    row = session.get(EdgeDevice, edge_device_id)
    if row is None or row.deleted_at is not None:
        raise ValueError("Edge device does not exist.")
    return row


def _get_edge_command(session: Session, command_id: str) -> EdgeFleetCommandRecord:
    row = session.get(EdgeFleetCommandRecord, command_id)
    if row is None:
        raise ValueError("Edge fleet command does not exist.")
    return row


def _device_summary(row: EdgeDevice, *, assigned_channel_count: int) -> EdgeDeviceSummary:
    return EdgeDeviceSummary(
        edge_device_id=row.id,
        name=row.name,
        technical_place=row.technical_place,
        address=EdgeDeviceAddress(
            management_ip=row.management_ip,
            control_port=row.control_port,
            plant_api_port=row.plant_api_port,
            rtsp_port=row.rtsp_port,
        ),
        allowed_cidr=row.allowed_cidr,
        server_ip=row.server_ip,
        enabled=row.enabled,
        connectivity_state=_effective_connectivity_state(row),
        reported_health_state=row.reported_health_state,
        last_seen_at=_iso(row.last_seen_at),
        last_poll_attempt_at=_iso(row.last_poll_attempt_at),
        last_connectivity_error=row.last_connectivity_error,
        current_boot_id=row.current_boot_id,
        identity_generation=row.identity_generation,
        deployment_revalidation_required=row.deployment_revalidation_required,
        assigned_channel_count=assigned_channel_count,
        created_at=_iso(row.created_at) or utc_now().isoformat(),
        updated_at=_iso(row.updated_at) or utc_now().isoformat(),
    )


def _effective_connectivity_state(row: EdgeDevice) -> str:
    if not row.enabled:
        return "unknown"
    last_seen_at = _aware(row.last_seen_at)
    if row.connectivity_state == "online" and last_seen_at is not None:
        if datetime.now(UTC) - last_seen_at > _CONNECTIVITY_STALE_AFTER:
            return "stale"
    return row.connectivity_state


def _assigned_channel_counts(session: Session, device_ids: list[str]) -> dict[str, int]:
    if not device_ids:
        return {}
    rows = session.execute(
        select(RuntimeChannel.edge_device_id, func.count(RuntimeChannel.id))
        .where(
            RuntimeChannel.edge_device_id.in_(device_ids),
            RuntimeChannel.deleted_at.is_(None),
        )
        .group_by(RuntimeChannel.edge_device_id)
    ).all()
    return {str(device_id): int(count) for device_id, count in rows if device_id}


def _command_to_api(row: EdgeFleetCommandRecord) -> EdgeFleetCommandSummary:
    return EdgeFleetCommandSummary(
        command_id=row.id,
        edge_device_id=row.edge_device_id,
        command_type=row.command_type,
        idempotency_key=row.idempotency_key,
        status=row.status,
        attempt_count=row.attempt_count,
        request_payload=dict(row.request_payload or {}),
        response_payload=dict(row.response_payload or {}),
        last_error=row.last_error,
        created_at=_iso(row.created_at) or utc_now().isoformat(),
        started_at=_iso(row.started_at),
        finished_at=_iso(row.finished_at),
    )


def _cursor_to_api(row: EdgeResultCursorRecord) -> EdgeResultCursorSummary:
    return EdgeResultCursorSummary(
        edge_device_id=row.edge_device_id,
        management_ip=row.management_ip,
        boot_id=row.boot_id,
        last_persisted_sequence=row.last_persisted_sequence,
        last_acknowledged_sequence=row.last_acknowledged_sequence,
        updated_at=_iso(row.updated_at) or utc_now().isoformat(),
    )


def _capability_snapshot_to_api(
    row: EdgeCapabilitySnapshotRow,
) -> EdgeCapabilitySnapshotRecord:
    return EdgeCapabilitySnapshotRecord(
        snapshot_id=row.id,
        digest=row.digest,
        observed_at=_iso(row.observed_at) or utc_now().isoformat(),
        capabilities=EdgeCapabilitySnapshot.model_validate(row.payload),
    )


def _payload_digest(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")
