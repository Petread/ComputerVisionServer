# ADR 0003: Durable Edge runtime state and A/B slots

- Status: Accepted
- Date: 2026-07-21

## Context

An Edge device must continue its assigned workload while the orchestration server is unavailable
and recover after power loss without internet access. Deployment state and result replay therefore
cannot depend on central PostgreSQL or volatile process memory. A partially activated workload
must not destroy the last known executable version.

## Decision

The Edge Agent owns a local SQLite database in WAL mode with full synchronous writes. Deployment
manifests, command idempotency, A/B slot state, worker state, result envelopes and acknowledgements
are transactionally persisted. Content-addressed artifacts remain files and are atomically moved
from a partial-upload directory after SHA-256 verification.

Exactly one slot is active. Preparing writes the inactive slot; commit changes active/standby state
in one database transaction. Rollback restores the previous slot when it still contains a valid
deployment. On process start the supervisor recovers every deployment persisted as running.

The result outbox and artifact store have independent byte/item quotas plus a minimum disk-free
reserve. Exceeding a limit rejects the specific write and reports degraded health without stopping
an already running workload.

## Consequences

- Edge operation and replay survive server, agent and device restarts.
- A single-file database plus its WAL is locally serviceable and needs no database daemon.
- Deployment commits and result sequences are serialized on the device.
- SQLite and artifact directories must live on the NVMe data volume and be included in service
  diagnostics.
- A/B protects the last committed workload, but release construction and TensorRT compatibility
  admission remain the responsibility of M3.
