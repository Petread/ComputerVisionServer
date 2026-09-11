# Failure Modes

## Active Protections

- Mutating operator actions write durable control-plane state before queueing long-running work.
- Label Studio webhook delivery is persisted before business processing and deduplicated by `source_event_key`.
- Upload finalization, snapshot export/materialization, training, deployment, rollback, and batch chunk execution all run outside the request path.
- Training artifacts never write directly into Triton repositories.
- Realtime and batch inference use separate gateways, separate Triton targets, and separate deployment pointers.
- Staged deployment directories are prepared before the active production pointer changes.
- Rollback reuses previous immutable releases instead of rebuilding artifacts.
- Health aggregation, queue depth, disk pressure, GPU pressure, and worker heartbeats are visible in the admin UI.

## Primary Failure Modes And Expected Outcome

- Worker crash during training:
  - latest resume-safe checkpoint is preserved
  - the same `training_job_id` remains authoritative
  - recovery continues through the training queue
- Worker crash during batch inference:
  - completed chunk outputs remain durable
  - incomplete chunk numbers stay visible on the batch job detail
  - reruns can process only missing chunks
- Failed dataset snapshot export or validation:
  - snapshot state moves to `failed`
  - dataset state is not promoted to `snapshotted`
  - the validation/error detail remains visible in the UI
- Failed deployment packaging, repository sync, or smoke test:
  - deployment release records the failure
  - active production traffic is unchanged
  - rollback remains available to the previous release
- Triton repository drift:
  - compatibility or repository integrity checks surface the mismatch
  - promotion is blocked until the immutable package and repository align
- Redis or queue disruption:
  - enqueue/dequeue pauses
  - durable PostgreSQL state remains authoritative
  - maintenance and reconciliation jobs can resume work after recovery
