# Edge Orchestration M1 Follow-Up Plan

## Outcome

M1 turns the M0 protocol slice into an operator-visible Fleet foundation. Devices, observations,
capability history, commands, result cursors, and channel placement survive service restarts in
the central PostgreSQL database. The Edge runtime itself remains the M0 simulator until M2.

M1 is software-complete when migration, stores, API, Fleet UI, channel targeting, OpenAPI, and
their automated tests pass. Physical Jetson acceptance remains governed by the M0 HIL report.

## Scope And Non-Scope

M1 includes:

- fixed-IP device registration, edit, archive, list, and detail views;
- server-initiated identity/health/capability reconciliation;
- persisted connectivity freshness, last reported health, and capability snapshots;
- persisted idempotent Fleet commands and boot-specific result offsets;
- `server|edge_device` placement on every runtime channel;
- Fleet navigation and operator workflows in the Admin UI;
- audit events and strict versioned API contracts.

M1 does not yet activate a real workload on an Edge device. Durable Edge-local SQLite state,
camera execution, TensorRT activation, A/B slots, and automatic deployment reconciliation are
M2/M3 work. Starting an Edge-placed channel is therefore rejected explicitly instead of silently
running it on the central server.

## Data Model

### `edge_devices`

Exactly one historical row exists per fixed management IPv4 address. It stores separate ports,
allowed CIDR, fixed server IP, enabled state, connectivity freshness, latest reported health,
current boot ID, identity generation, latest capability pointer, and deployment-revalidation flag.
An archived IP cannot be registered as a second identity in M1; a physical replacement under the
same IP continues the existing logical device and is detected through boot/capability changes.

The management IP is immutable. An IP change creates a new logical device. A boot-ID change under
the same IP increments `identity_generation`; a boot-ID or capability change marks deployments for
revalidation. This deliberately favors a conservative recheck because the selected trust model
has no cryptographic hardware identity.

### `edge_capability_snapshots`

Capabilities are canonicalized and SHA-256-digested. A new immutable snapshot is written only
when the payload changes. The device row points at the latest snapshot while history remains
available for diagnostics.

### `edge_fleet_commands`

Every mutating Fleet operation has `(edge_device_id, idempotency_key)` uniqueness, request and
response payloads, attempt count, terminal status, timestamps, and last error. M1 uses this for
manual reconciliation; deployment command types are already reserved for M3.

### `edge_result_cursors`

Each `(edge_device_id, boot_id)` owns monotonic persisted and acknowledged offsets. The store
rejects backwards movement and acknowledgements beyond durable persistence. Result payload
ingestion into the existing result service follows in M4.

### Runtime channel placement

`runtime_channels.execution_target` is `server` or `edge_device`; `edge_device_id` is present only
for Edge placement. Assignment requires an active, enabled registered device. Placement changes
require a stopped channel. Existing channels migrate to `server` without behavior changes.

## Operator API

The Runtime Admin API owns the Fleet control plane:

- `GET /api/v1/runtime/edge-devices`
- `POST /api/v1/runtime/edge-devices`
- `GET /api/v1/runtime/edge-devices/{edge_device_id}`
- `PUT /api/v1/runtime/edge-devices/{edge_device_id}`
- `DELETE /api/v1/runtime/edge-devices/{edge_device_id}`
- `POST /api/v1/runtime/edge-devices/{edge_device_id}:reconcile`

Reconciliation builds the M0 client exclusively from the registered address and CIDR, polls the
three read endpoints, validates reported IP/boot consistency, persists the observation, and ends
the command. A connection failure changes connectivity to `offline` while retaining the last
reported health and capability information.

Fleet reads are available to authenticated runtime readers. Registration, edit, archive, and
reconciliation use the runtime Fleet write permission and emit audit records.

## Fleet UI

The primary navigation gains an `Edge Devices` workspace. It provides:

- status counts and connectivity filters;
- registration with name, place, management IP, server IP, CIDR, and fixed ports;
- editing the mutable network metadata, enabling/disabling, and guarded archiving;
- device cards with connectivity, reported health, platform, last contact, channel count, and
  revalidation state;
- manual status reconciliation;
- a detail view of identity, capabilities, recent commands, and result cursors.

Channel creation loads enabled devices and stores a placement selection. Channel cards show the
target independently of runtime health, so server and Edge channels remain visible in the same
overview.

## Reconciliation And Failure Semantics

1. The server creates or reuses an idempotent `reconcile` command.
2. The registered IP is checked against its stored allowed CIDR before opening a socket.
3. Identity, health, and capabilities are fetched over the M0 HTTP contract.
4. A complete valid observation records connectivity `online` and separately stores reported
   health.
5. Failed contact records `offline`, the attempt time, and error without overwriting reported
   runtime data.
6. An otherwise-online observation becomes `stale` in API read models after 90 seconds without a
   successful poll.

No background poll scheduler is introduced in M1; the API and persistence boundary are ready for
the M2 reconciler loop.

## Acceptance Gates

- Migration upgrades an existing schema and defaults all existing channels to `server`.
- Duplicate management IP registration is rejected.
- Device list/detail and connectivity freshness survive new database sessions.
- Capability snapshots are written only on change.
- Boot changes increment the identity generation and require deployment revalidation.
- Offline reconciliation retains last reported health.
- Command idempotency conflicts are rejected and retries return the stored outcome.
- Result cursors cannot move backwards or acknowledge unpersisted sequences.
- Edge channel assignment requires an enabled registered device.
- Active channel placement changes and central start of an Edge-placed channel are rejected.
- Fleet API, OpenAPI, API client, navigation, Fleet page, and channel target selector agree on the
  same versioned contracts.
- Backend tests, frontend tests/typecheck, Ruff, and relevant Contract tests pass.

## Follow-Up To M2

M2 adds the continuous reconciliation loop, durable Edge-local SQLite WAL state, offline installer,
watchdog/process supervision, quotas, real camera/source execution, and recovery of the last
committed workload. M1 deliberately leaves `deployment_revalidation_required` set until that
deployment controller can validate and clear it.
