from __future__ import annotations

from dataclasses import dataclass

from cv_contracts import (
    EdgeCapabilitySnapshot,
    EdgeDeviceDetail,
    EdgeDeviceHealth,
    EdgeDeviceIdentity,
)
from cv_edge import EdgeAgentClient, EdgeNetworkPolicy


@dataclass(frozen=True)
class EdgeProbeResult:
    identity: EdgeDeviceIdentity
    health: EdgeDeviceHealth
    capabilities: EdgeCapabilitySnapshot


def probe_edge_device(device: EdgeDeviceDetail) -> EdgeProbeResult:
    policy = EdgeNetworkPolicy.from_strings(
        allowed_cidrs=[str(device.allowed_cidr)],
        registered_addresses=[str(device.address.management_ip)],
    )
    with EdgeAgentClient(address=device.address, network_policy=policy) as client:
        identity = client.identity()
        health = client.health()
        capabilities = client.capabilities()
    return EdgeProbeResult(
        identity=identity,
        health=health,
        capabilities=capabilities,
    )
