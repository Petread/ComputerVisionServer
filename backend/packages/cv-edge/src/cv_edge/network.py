from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network


class EdgeNetworkPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class EdgeNetworkPolicy:
    allowed_networks: tuple[IPv4Network, ...]
    registered_addresses: frozenset[IPv4Address]

    @classmethod
    def from_strings(
        cls,
        *,
        allowed_cidrs: Iterable[str],
        registered_addresses: Iterable[str],
    ) -> EdgeNetworkPolicy:
        networks: list[IPv4Network] = []
        for value in allowed_cidrs:
            network = ip_network(value, strict=False)
            if not isinstance(network, IPv4Network):
                raise EdgeNetworkPolicyError("Only IPv4 edge networks are supported in M0.")
            networks.append(network)
        if not networks:
            raise EdgeNetworkPolicyError("At least one allowed edge CIDR is required.")

        addresses: set[IPv4Address] = set()
        for value in registered_addresses:
            address = ip_address(value)
            if not isinstance(address, IPv4Address):
                raise EdgeNetworkPolicyError("Only IPv4 edge addresses are supported in M0.")
            addresses.add(address)
        return cls(tuple(networks), frozenset(addresses))

    def validate(self, value: str | IPv4Address) -> IPv4Address:
        address = value if isinstance(value, IPv4Address) else ip_address(value)
        if not isinstance(address, IPv4Address):
            raise EdgeNetworkPolicyError("Only IPv4 edge addresses are supported in M0.")
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise EdgeNetworkPolicyError(
                f"Edge address {address} is not a routable machine-LAN host."
            )
        matching_networks = [network for network in self.allowed_networks if address in network]
        if not matching_networks:
            raise EdgeNetworkPolicyError(
                f"Edge address {address} is outside the allowed VLAN CIDRs."
            )
        if any(
            address in {network.network_address, network.broadcast_address}
            for network in matching_networks
        ):
            raise EdgeNetworkPolicyError(
                f"Edge address {address} is a network or broadcast address."
            )
        if address not in self.registered_addresses:
            raise EdgeNetworkPolicyError(f"Edge address {address} is not registered.")
        return address
