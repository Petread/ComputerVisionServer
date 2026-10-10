from __future__ import annotations

from dataclasses import dataclass

from cv_contracts import (
    EdgeCapabilitySnapshot,
    EdgeDeviceDetail,
    EdgeDeviceHealth,
    EdgeDeviceIdentity,
)
from cv_contracts.edge import EdgeSoftwareStatus
from cv_edge import EdgeAgentClient, EdgeNetworkPolicy


@dataclass(frozen=True)
class EdgeProbeResult:
    identity: EdgeDeviceIdentity
    health: EdgeDeviceHealth
    capabilities: EdgeCapabilitySnapshot
    software: EdgeSoftwareStatus | None = None

    def software_observation(self) -> dict:
        # Fleet command responses are extensible; health/capability JSON must remain
        # readable by older central releases after rollback.
        return {
            "software": self.software.model_dump(mode="json") if self.software else None,
            "software_observed_at": self.health.observed_at if self.software else None,
        }


def probe_edge_device(device: EdgeDeviceDetail) -> EdgeProbeResult:
    policy = EdgeNetworkPolicy.from_strings(
        allowed_cidrs=[str(device.allowed_cidr)],
        registered_addresses=[str(device.address.management_ip)],
    )
    with EdgeAgentClient(address=device.address, network_policy=policy) as client:
        identity = client.identity()
        health = client.health()
        capabilities = client.capabilities()
        # Read the existing endpoint even for agents predating central bundle imports.
        software = client.software_status() if capabilities.software_update_protocol >= 1 else None
    return EdgeProbeResult(
        identity=identity,
        health=health,
        capabilities=capabilities,
        software=software,
    )
