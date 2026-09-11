# ADR 0001: Edge Device Identity By Fixed IP In An Isolated VLAN

## Status

Accepted

## Context

YBB edge devices run inside machine-local networks. Each installation provides an isolated
VLAN in which the orchestration server, edge devices, and machine PLCs are the only permitted
participants. The first fleet version should avoid per-device enrollment, certificates, and
API-key lifecycle work.

A fixed IP address identifies the logical edge device. Replacing hardware and assigning the
same address intentionally replaces that logical device, subject to a new capability and
deployment validation.

An IP address is not cryptographic authentication. Without an authenticated transport, a host
with access to the VLAN can observe configuration and artifact traffic or impersonate another
host.

## Decision

- Edge management uses server-initiated HTTP over the isolated machine VLAN.
- The canonical identity is the registered fixed IPv4 address. Ports are stored separately;
  callers cannot submit arbitrary control URLs.
- M0 uses no device certificate, API key, enrollment token, or outbound control connection.
- The central client connects only to explicitly registered addresses inside configured edge
  CIDRs. Loopback, link-local, multicast, unspecified, network, broadcast, and out-of-scope
  addresses are rejected before a socket is opened.
- Network enforcement is mandatory: switch/VLAN ACLs and the device firewall allow control
  port `9443` only from the fixed server IP. Plant API `9444` and RTSP `8554` receive separate
  allow-lists.
- Product documentation and the UI must describe this trust profile as
  `isolated_vlan_fixed_ip`, not as authenticated or encrypted.
- Product secrets are excluded from M0. Later secret delivery under this trust profile is
  plaintext on the wire and requires a separate explicit operational acceptance.

## Consequences

- Provisioning and replacement remain simple and do not require a PKI.
- The server can reconcile every device with deterministic, server-originated requests.
- Routed office networks, Wi-Fi, internet exposure, and shared or untrusted LANs are unsupported.
- IP conflicts and address takeover are security and availability incidents.
- Moving to shared networks later requires a new ADR and authenticated transport; fixed-IP
  identity alone must not be silently presented as secure authentication.
