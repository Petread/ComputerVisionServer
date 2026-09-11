# ADR 0006: Edge Operator Preflight, Diagnostics and Manual Rollback

- Status: Accepted
- Date: 2026-07-22

## Context

M1–M4 provide fixed-IP fleet management, a durable A/B runtime, immutable workload releases and
crash-safe result/media synchronization. Directly exposing these individual primitives would make
operators coordinate release construction, channel commands, deployment state and recovery by
hand. A preflight result can also become stale between display and execution.

Operators need one auditable workflow, actionable diagnostics and a safe way to restore the
previous A/B slot without introducing a second deployment queue or allowing browsers to contact
machine-VLAN devices.

## Decision

Deployment is a two-step workflow. Preflight creates an immutable, capability-bound workload
release and returns strict admission and operational checks. Execution accepts only the selected
release ID. The existing durable runtime-control command stores the expected release ID and digest.
The deployment controller revalidates release digest, channel placement, pipeline, device state and
capability digest before transferring any artifact.

The device revalidation marker is not by itself a preflight failure. The immutable release is bound
to the newest observed capability snapshot, and a successful commit clears revalidation. This
supports first deployment and deliberate device replacement at the same fixed IP while still
failing closed on later capability drift.

The deployment controller marks the direct predecessor of a successful activation as `standby`.
Manual rollback uses the existing durable fleet-command store and the existing Edge rollback
endpoint. It is permitted only for the currently active deployment with a same-channel,
same-device standby predecessor. Central channel and deployment state changes only after the Edge
confirms both rollback and restoration. Idempotent retries return the original completed command.

Diagnostics are server-side, strict, persisted reports built from the latest reconciled device
state, result cursor watermarks, channel state and recent result timings. Fleet alerts are a
deterministic projection of failed or warning checks. Live channels use a 100 ms P95 target and
trigger channels use 500 ms until site-specific policy becomes necessary.

## Consequences

- Displayed preflight evidence and executed artifacts share one immutable release identity.
- Stale browser state cannot silently deploy a different release.
- The existing runtime-control worker remains the single deployment executor.
- Initial deployment and hardware replacement can clear revalidation through a verified commit.
- The previous A/B slot has an explicit central `standby` state and a constrained recovery path.
- Rollback retries are safe across lost HTTP responses.
- Diagnostics and Fleet alerts do not modify or stop local channel execution.
- P95 targets are currently platform defaults, not per-site policy.
- Browsers continue to communicate only with the central Runtime Admin API.
- Fixed-IP/VLAN trust and its lack of transport authentication remain unchanged and mandatory.
