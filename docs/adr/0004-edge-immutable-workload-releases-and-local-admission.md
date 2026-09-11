# ADR 0004: Immutable Edge Workload Releases and Local Admission

- Status: Accepted
- Date: 2026-07-21

## Context

M1 can assign a channel to a fixed-IP Edge device. M2 can durably transfer and run a supplied
manifest with A/B recovery. Starting an Edge channel still needs a reproducible binding from the
published pipeline and current model releases to the exact files executed on a specific target.
TensorRT engines are hardware- and runtime-dependent and therefore cannot safely be treated as a
portable server artifact.

## Decision

The server builds an immutable, content-addressed workload release from one published,
channel-scoped pipeline version and the concrete model deployment releases resolved at build time.
Its digest covers pipeline configuration, artifact descriptors, source requirements, model
bindings, precision and the target capability digest. Equal content yields the same release ID.

Admission has two mandatory stages:

1. Central admission checks the registered device observation, supported nodes and source adapter,
   TensorRT inventory, storage reserve, Golden artifact addressing and referenced secret
   availability.
2. Local admission on the Edge verifies every transferred blob, builds or reuses TensorRT engines
   in a target-specific cache, and runs declared Golden checks before `prepare` succeeds.

The deployment controller consumes the existing channel commands but only for Edge placement.
The existing central runtime executor only consumes server placement. Remote operations derive
stable idempotency keys from the control command and release. Activation uses M2 A/B commit;
failure triggers M2 rollback. Stop must be acknowledged by the old target before placement can be
changed.

Source credentials are excluded from centrally readable release metadata. They are materialized
only into the in-flight deployment manifest and stored in the protected local Edge state. Under
ADR 0001 this transfer is HTTP without application credentials or encryption and is therefore
permitted only inside the isolated VLAN/ACL/firewall boundary.

## Consequences

- Operators can reproduce which pipeline, model and artifacts ran on a device.
- A device/capability change forces a new digest and local admission before revalidation clears.
- Large artifacts resume from confirmed offsets and do not need to be loaded fully into server
  memory.
- TensorRT engines cannot be copied between incompatible target profiles by accident.
- Server loss after activation does not stop the Edge workload.
- Release metadata and deployment history add database/storage retention requirements.
- Network isolation remains mandatory because management and source credentials are not protected
  by TLS in the selected trust model.
- Numeric, task-specific Golden comparisons require a matching offline runner; unknown comparison
  modes fail closed.
