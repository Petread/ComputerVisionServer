from __future__ import annotations

import pytest
from cv_edge import EdgeNetworkPolicy, EdgeNetworkPolicyError


def _policy() -> EdgeNetworkPolicy:
    return EdgeNetworkPolicy.from_strings(
        allowed_cidrs=["192.168.50.0/24"],
        registered_addresses=["192.168.50.21"],
    )


def test_network_policy_accepts_only_registered_vlan_hosts() -> None:
    assert str(_policy().validate("192.168.50.21")) == "192.168.50.21"

    with pytest.raises(EdgeNetworkPolicyError, match="not registered"):
        _policy().validate("192.168.50.22")

    with pytest.raises(EdgeNetworkPolicyError, match="outside"):
        _policy().validate("192.168.60.21")


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "169.254.10.1", "224.0.0.1", "0.0.0.0"],
)
def test_network_policy_rejects_non_machine_lan_addresses(address: str) -> None:
    policy = EdgeNetworkPolicy.from_strings(
        allowed_cidrs=["0.0.0.0/0"],
        registered_addresses=[address],
    )

    with pytest.raises(EdgeNetworkPolicyError, match="not a routable"):
        policy.validate(address)


def test_network_policy_rejects_network_and_broadcast_addresses() -> None:
    policy = EdgeNetworkPolicy.from_strings(
        allowed_cidrs=["192.168.50.0/24"],
        registered_addresses=["192.168.50.0", "192.168.50.255"],
    )

    with pytest.raises(EdgeNetworkPolicyError, match="network or broadcast"):
        policy.validate("192.168.50.0")
    with pytest.raises(EdgeNetworkPolicyError, match="network or broadcast"):
        policy.validate("192.168.50.255")
