# ADR 0002: Server-Initiated Edge Control, Deployment, And Result Synchronization

## Status

Accepted

## Context

Edge channels must continue processing when the orchestration server is unavailable. The
server still needs deterministic health reads, resumable artifact delivery, idempotent workload
activation, and replay of locally buffered results after a network interruption.

The fixed-IP network decision makes a simple request/response protocol preferable to an edge
initiated broker or permanent reverse tunnel.

## Decision

- The server polls identity, health, and capabilities through versioned REST endpoints.
- Artifacts are content-addressed by SHA-256. `HEAD` reports the committed or partial offset;
  bounded `PUT` chunks resume from that offset. Completion requires exact size and digest before
  atomic promotion from the temporary file.
- Workload prepare, commit, and rollback are separate idempotent commands. A partial transfer or
  lost response can be retried without applying a workload twice.
- Results are stored in a local outbox with `(management_ip, boot_id, sequence)` identity.
- The server requests results after its last contiguous sequence and acknowledges only after
  durable central persistence. Unacknowledged results may be delivered repeatedly.
- A reboot creates a new `boot_id` and a new sequence domain while older unacknowledged domains
  remain queryable.
- Connectivity is independent of workload state. An unreachable device becomes stale/unknown in
  the control plane; it is not instructed to stop and continues its last committed workload.

## Consequences

- No Redis, PostgreSQL, or central queue is required on the edge device.
- Transport is at-least-once; central persistence becomes effectively-once through the result
  identity tuple.
- Every mutating request needs an idempotency key and a persisted command outcome in the
  production agent.
- M0 uses a simulator and in-memory deployment/result state around a real filesystem blob store.
  Durable SQLite state, Fleet persistence, and placement reconciliation are M1 work.
