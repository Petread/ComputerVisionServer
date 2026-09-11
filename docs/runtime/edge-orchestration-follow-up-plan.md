# Edge Orchestration Follow-Up Plan

## Purpose And Current Status

This document follows the server-local runtime architecture and defines the transition to a
central orchestration server with Windows 11 and NVIDIA Jetson edge execution targets.

M0 is the protocol and feasibility slice. Its software status is `software_complete` when the
contract, simulator, client, transfer, replay, documentation, and automated tests pass. Physical
Jetson acceptance remains `pending_hardware` until an Orin Nano is available.

## Fixed Decisions

- Deployment network: isolated machine VLAN.
- Device identity: unique registered fixed IPv4 address.
- Trust profile: `isolated_vlan_fixed_ip`; no per-device key or certificate.
- Direction: the orchestration server initiates all management calls.
- Control API: HTTP on TCP `9443`.
- Plant trigger API: separate TCP `9444` surface.
- Edge RTSP: TCP `8554`, readable only by the central media relay.
- Placement: operator-selected target with automatic compatibility checks.
- Failure behavior: edge workloads continue without the server and buffer results locally.
- Failover: no automatic move to another edge device in V1.
- Plant I/O: camera, trigger, PLC, and output connectors execute on the edge.
- Live media: central relay pulls a stream only for an active viewer lease.
- Standard video envelope: capture up to 1080p/30; inference may sample frames.
- Runtime SLO: P95 100 ms live and 500 ms trigger for workloads declaring those profiles.
- Result policy: per-channel `events`, `sampled`, or `all`; default is business events plus
  aggregated metrics.
- Functional safety: outside this platform; certified PLC/safety hardware owns safe states.

The security and protocol rationale is normative in ADR 0001 and ADR 0002.

## M0 Architecture Slice

### Contracts

`cv-contracts` owns strict versioned DTOs for device address, identity, health, capability,
artifact, deployment, result batch, acknowledgement, and control error payloads. IPv4 address
and port are separate values. Unknown fields are rejected. OpenAPI describes the private edge
surface; platform AsyncAPI contains central edge state/result events.

### Central Edge Client

`cv-edge` owns:

- allow-listed CIDR and registered-address validation before network access;
- URL construction from validated IP and fixed protocol paths only;
- bounded connect, read, and transfer timeouts;
- typed health/capability reads and deployment commands;
- resumable SHA-256 blob transfer;
- boot-specific result replay and retry-safe central deduplication;
- stable timeout, offline, compatibility, hash, remote, and protocol error categories.

### Edge Agent Simulator

`edge-agent` is the executable M0 protocol implementation and the base for the later Jetson
agent. It provides:

- configurable Orin Nano capability data;
- filesystem-backed content-addressed blobs and partial uploads;
- idempotent prepare, commit, status, and rollback;
- local sequenced result outbox;
- restart with a new boot identity;
- simulated offline, degraded, disk-full, and activation-failure modes.

M0 deliberately excludes a real camera, TensorRT build, PLC write, device installer, Fleet
database, and Fleet UI.

### Running The M0 Simulator

From the repository root, install the M0 packages into a dedicated environment and start the
private control API:

```bash
python -m pip install -e backend/packages/cv-status -e backend/packages/cv-contracts \
  -e backend/packages/cv-edge -e backend/services/edge-agent
python -m uvicorn edge_agent.app:app --host 0.0.0.0 --port 9443
```

`YBB_EDGE_MANAGEMENT_IP`, `YBB_EDGE_CONTROL_PORT`, `YBB_EDGE_PLANT_API_PORT`,
`YBB_EDGE_RTSP_PORT`, `YBB_EDGE_DATA_ROOT`, and `YBB_EDGE_HOSTNAME` override the simulator
defaults. The advertised management IP is the device identity; it is not accepted as a free
request URL.

## Protocol Flows

### Device Reconciliation

1. Fleet configuration provides a registered fixed IP and allowed VLAN CIDR.
2. The server validates the address before opening a connection.
3. `identity`, `health`, and `capabilities` are polled.
4. Identity or capability changes at an existing IP mark deployments for revalidation.
5. A failed poll changes connectivity freshness only; it does not stop the edge workload.

### Artifact And Deployment

1. Server computes a content SHA-256 and asks the edge for the current offset.
2. Missing bytes are transferred in bounded chunks.
3. Edge fsyncs the partial file, checks final size and digest, then atomically commits it.
4. Prepare validates platform, required nodes, and all referenced artifacts.
5. Commit activates exactly once for its idempotency key.
6. Failed activation remains observable and can be rolled back idempotently.

### Result Replay

1. Edge assigns sequence numbers within its current boot identity.
2. Server reads after the last centrally confirmed contiguous sequence.
3. Central persistence deduplicates by fixed IP, boot ID, and sequence.
4. Server acknowledges only after persistence.
5. If the acknowledgement response is lost, the edge repeats the batch and central persistence
   skips duplicates before retrying the acknowledgement.

## Jetson Orin Nano Provisioning Baseline

- Target profile: JetPack 7.2 / L4T 39.2 on NVMe.
- A universal offline image carries NVIDIA runtime components, Python wheels, YBB agent/runtime,
  camera adapters, systemd units, and watchdog.
- A device-specific `device.yaml` supplies hostname, fixed IP, server IP, VLAN parameters, ports,
  and storage quotas. It contains no enrollment credential.
- Bootstrap applies network and firewall configuration, enables services, records inventory, and
  reboots before the server begins polling.
- Proprietary camera SDKs enter the image only after redistribution and target-version approval.

## Roadmap After M0

### M1: Fleet Foundations

- Persist devices, capability snapshots, connectivity freshness, commands, and result offsets.
- Add operator API and Fleet UI for fixed-IP device registration and status.
- Extend channels and executions with `server|edge_device` target references.

### M2: Durable Edge Runtime

- Replace simulator deployment/result memory with SQLite WAL state.
- Add A/B runtime slots, process supervision, quotas, watchdog, and offline installer.
- Integrate real source, local inference, trigger, and connector execution.

### M3: Workload Releases

- Build immutable channel/pipeline/model manifests.
- Add local ONNX-to-TensorRT optimization, golden tests, admission, atomic activation, and rollback.
- Prevent target moves until the old reachable target confirms stop.

### M4: Result And Media Integration

- Ingest edge results into the existing result service.
- Add configurable result policy, thumbnails, viewer leases, and on-demand MediaMTX relay.

### M5: Operator Workflows

- Device list/detail, deployment wizard, channel target badges, diagnostics, and rollback.
- Implemented in `edge-orchestration-m5-follow-up-plan.md` with software status
  `software_complete`; physical Orin Nano acceptance remains `pending_hardware`.

### M6: Fleet Updates And Hardening

- A/B agent/runtime updates, pilot waves, automatic rollout halt, recovery drills, and production
  pilot up to the agreed 50-device/eight-channel envelope.

## M0 Acceptance Checklist

- [x] Fixed-IP/VLAN and server-initiated protocol ADRs defined.
- [x] Strict Edge DTOs defined.
- [x] OpenAPI/AsyncAPI contract extension defined.
- [x] Central CIDR-restricted Edge client defined.
- [x] Resumable content-addressed transfer implemented.
- [x] Idempotent simulated deployment and rollback implemented.
- [x] Result replay and deduplication implemented.
- [x] Simulator fault modes implemented.
- [x] Automated contract/unit/integration coverage added.
- [x] Jetson target and benchmark harness specified.
- [ ] Physical Orin Nano HIL report completed (`pending_hardware`).
