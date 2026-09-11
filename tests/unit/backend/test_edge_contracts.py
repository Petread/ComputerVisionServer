from __future__ import annotations

from ipaddress import IPv4Address

import pytest
from cv_contracts import (
    EdgeArtifactDescriptor,
    EdgeDeviceAddress,
    EdgeDeviceIdentity,
    EdgeDeviceRegistrationRequest,
    EdgeResultBatch,
    EdgeResultEnvelope,
    RuntimeChannelPlacementRequest,
)
from pydantic import ValidationError


def test_edge_address_is_ipv4_and_serializes_canonically() -> None:
    address = EdgeDeviceAddress(management_ip="192.168.50.21")

    assert address.management_ip == IPv4Address("192.168.50.21")
    assert address.model_dump(mode="json")["management_ip"] == "192.168.50.21"

    with pytest.raises(ValidationError):
        EdgeDeviceAddress(management_ip="2001:db8::21")


def test_edge_contracts_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        EdgeDeviceIdentity.model_validate(
            {
                "address": {"management_ip": "192.168.50.21"},
                "hostname": "edge-01",
                "platform": "jetson_orin_nano",
                "architecture": "aarch64",
                "agent_version": "0.1.0",
                "runtime_version": "0.1.0",
                "boot_id": "boot-1",
                "unexpected": True,
            }
        )


def test_artifact_file_name_cannot_escape_storage_root() -> None:
    with pytest.raises(ValidationError):
        EdgeArtifactDescriptor(
            sha256="a" * 64,
            size_bytes=12,
            kind="onnx_model",
            file_name="../model.onnx",
        )


def test_result_batch_rejects_mixed_device_or_boot_identity() -> None:
    item = EdgeResultEnvelope(
        management_ip="192.168.50.22",
        boot_id="boot-2",
        sequence=1,
        channel_id="channel-1",
        workload_release_id="release-1",
        correlation_id="correlation-1",
        occurred_at="2026-07-21T12:00:00Z",
    )

    with pytest.raises(ValidationError):
        EdgeResultBatch(
            management_ip="192.168.50.21",
            boot_id="boot-1",
            after_sequence=0,
            items=[item],
            highest_sequence=1,
        )


def test_edge_registration_requires_device_and_server_in_allowed_cidr() -> None:
    with pytest.raises(ValidationError):
        EdgeDeviceRegistrationRequest(
            name="Orin Nano Press Line",
            address={"management_ip": "192.168.51.21"},
            allowed_cidr="192.168.50.0/24",
            server_ip="192.168.50.10",
        )

    with pytest.raises(ValidationError):
        EdgeDeviceRegistrationRequest(
            name="Orin Nano Press Line",
            address={"management_ip": "192.168.50.21"},
            allowed_cidr="192.168.50.0/24",
            server_ip="192.168.50.21",
        )


def test_edge_channel_placement_requires_exactly_one_device_target() -> None:
    placement = RuntimeChannelPlacementRequest(
        target="edge_device",
        edge_device_id="edge-press-01",
    )
    assert placement.edge_device_id == "edge-press-01"

    with pytest.raises(ValidationError):
        RuntimeChannelPlacementRequest(target="edge_device")
    with pytest.raises(ValidationError):
        RuntimeChannelPlacementRequest(target="server", edge_device_id="edge-press-01")
