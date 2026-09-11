# ADR 0005: Edge Result Sync and Demand-Driven Preview

- Status: Accepted
- Date: 2026-07-21

## Context

M2 provides a durable sequenced result outbox and M3 provides immutable workload provenance. The
central platform must consume results without duplicates across network loss, server restarts and
multiple Edge boot IDs. Optional live images must not create permanent bandwidth or encoding load
and browsers must not connect directly to machine VLAN devices.

## Decision

The server is the only initiator. It discovers pending boot domains, pulls bounded batches and
persists each envelope into the existing `result_records` domain together with a unique receipt
for device, boot and sequence. It acknowledges only the highest contiguous sequence after the
database transaction commits. The Edge retains ACK state and idempotency responses while deleting
acknowledged outbox rows. ACK-only boot domains remain discoverable, so a lost response after a
successful remote acknowledgement can be replayed and the central cursor repaired.

Edge results use the channel's existing result-history policy. The receipt is durable regardless
of history selection, while `enabled` and event-time sampling decide whether the associated
`ResultRecord` is visible as retained history. Existing maintenance retention remains authoritative.
Inline image payloads are removed; channel-card thumbnails use the ephemeral preview path.

Preview transport is lease-driven. Authenticated browser views create short central leases. The
server aggregates these into one expiring Edge lease per channel and pulls bounded JPEG previews.
The existing central live-view state and MJPEG endpoint are the browser relay. The Edge encodes no
preview when no unexpired lease exists.

## Consequences

- Result replay is crash-safe without introducing a second result read model.
- Old unacknowledged boot domains remain recoverable after another device restart.
- ACK response loss remains recoverable after local outbox compaction.
- A sequence replay with different content fails closed.
- Result provenance includes device, fixed IP, boot, sequence and workload release.
- Browsers require no route to the machine VLAN.
- Preview bandwidth and encode cost disappear after lease expiry.
- JPEG/MJPEG is an operational preview path, not a high-efficiency production video fanout.
- VLAN isolation remains mandatory because the selected fixed-IP control plane uses HTTP.
