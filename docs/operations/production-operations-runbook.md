# Production Operations Runbook

This runbook applies to the single-node on-prem deployment defined in [the architecture spec](../architecture/cv-platform-spec.md). It assumes the platform runs on one workstation, inside WSL2, with one NVIDIA GPU, one browser-first operator workflow, and strict separation between control, training, realtime inference, and batch inference planes.

Security and exposure policy are documented in:

- [Internal LAN Security Model](../architecture/internal-lan-security-model.md)
- [Endpoint Exposure Map](../architecture/endpoint-exposure-map.md)
- [Go-Live Checklist](./go-live-checklist.md)
- [Operator Guide](./operator-guide.md)
- [Troubleshooting Guide](./troubleshooting-guide.md)
- [Curvature-Minimum production and rollback runbook](./stem-curvature-minimum-production-runbook.md)

## Operational Model

- Normal workflows stay inside the admin UI. Operators use the `Overview`, `Operational Health`, `Deployment`, `Training Jobs`, `Batch Inference`, and `Audit Log` screens.
- Recovery and maintenance jobs run through the maintenance queue. The relevant implemented jobs are:
  - `maintenance_worker.jobs.backup_metadata`
  - `maintenance_worker.jobs.checkpoint_pruning`
  - `maintenance_worker.jobs.artifact_retention`
  - `maintenance_worker.jobs.reconcile_training_jobs`
  - `maintenance_worker.jobs.cleanup_stale_locks`
- Every Python HTTP service exposes:
  - `GET /health/live`
  - `GET /health/ready`
  - `GET /health/startup`
  - `GET /metrics`
- Workers report heartbeat rows into `service_heartbeats`.
- Audit events are append-only in PostgreSQL and mirrored to `/srv/cv-platform/audit/events/*.jsonl`.
- MLflow-aware workers must resolve the global tracking URI to the in-cluster MLflow service. A fallback such as `file:///workspace/mlruns` is not a valid runtime state for artifact-backed model registration.

## Health Semantics

- `live` means the process is up and the event loop can still respond.
- `ready` means startup finished, shutdown is not in progress, runtime paths exist, and required dependencies for that service are reachable.
- `startup` confirms config validation and runtime path preparation completed.
- The admin UI is the only supported aggregation surface for operators. It combines:
  - service readiness
  - dependency reachability
  - worker heartbeats
  - queue backlog and oldest-job age
  - disk usage
  - GPU memory and utilization
  - deployment repository integrity alerts

## Startup And Restart Behavior

1. Start the base infrastructure first: PostgreSQL, Redis, Label Studio, MLflow, Triton RT, Triton Batch, reverse proxy.
2. Start Python HTTP services next: `webhook-receiver`, `dataset-service`, `training-orchestrator`, `model-registry-service`, `deployment-controller`, `admin-api`, `inference-gateway-rt`, `inference-gateway-batch`.
3. Start workers after the control plane is healthy: `control-worker`, `training-worker`, `batch-worker`, `maintenance-worker`.
4. Confirm:
   - `Operational Health` shows no critical alerts.
   - worker heartbeats are current.
   - realtime and batch deployment targets both point to explicit immutable releases.
   - disk usage and GPU headroom stay inside thresholds.
   - the shared Python runtime used by `control-worker` and `training-worker` exposes both `git` and `dvc` on `PATH` before snapshot or model lineage work begins.
   - `control-worker` and `training-worker` both resolve `mlflow.get_tracking_uri()` to `http://mlflow:5000`.

Recommended verification commands:

```powershell
docker compose -f infra/compose/compose.prod.yaml exec -T training-worker sh -lc "which git && which dvc && dvc version | head -n 1"
docker compose -f infra/compose/compose.prod.yaml exec -T control-worker sh -lc "which git && which dvc && dvc version | head -n 1"
docker compose -f infra/compose/compose.prod.yaml exec -T training-worker uv run --package training-worker python -c "import mlflow; print(mlflow.get_tracking_uri())"
docker compose -f infra/compose/compose.prod.yaml exec -T control-worker uv run --package control-worker python -c "import mlflow; print(mlflow.get_tracking_uri())"
```

## Service Restart Matrix

| Component | Safe To Restart Independently | Expected User Impact | Required Follow-Up |
| --- | --- | --- | --- |
| `admin-web` | Yes | UI sessions reconnect; no jobs stop | Confirm the page reloads and the status header updates |
| `admin-api` | Yes | UI status/actions pause briefly; background jobs continue | Confirm `/health/ready`, job feed reconnect, and `Operational Health` refresh |
| `webhook-receiver` | Yes | Webhook ingress pauses briefly; queued/background work unaffected | Check webhook backlog and retry delivery from Label Studio if needed |
| `dataset-service` | Yes | Dataset creation and snapshot submission pause briefly | Confirm dataset routes are ready and snapshot queue drains |
| `training-orchestrator` | Yes | New training submissions pause briefly; active training keeps running | Verify `Training Jobs` page can submit and resume commands again |
| `model-registry-service` | Yes | Registry reads and lifecycle actions pause briefly | Confirm model detail and approvals load |
| `deployment-controller` | Yes | New package/promote/rollback actions pause briefly | Confirm deployment actions and smoke tests are ready again |
| `inference-gateway-rt` | Yes | Realtime API reconnect required; Triton RT stays active | Run a small realtime inference test from the admin UI |
| `inference-gateway-batch` | Yes | Batch submission and chunk execution pause briefly | Verify queued batch jobs resume and no chunks are stranded |
| `control-worker` | Yes | Webhook, dataset, deployment, and maintenance queue throughput pauses | Confirm worker heartbeat resumes and backlog shrinks |
| `training-worker` | Yes, with recovery expectations | Active training run may fail over and resume from latest checkpoint | Run `reconcile_training_jobs` and confirm checkpoint-based resume or safe restart |
| `batch-worker` | Yes | Chunk processing pauses; completed chunks remain durable | Confirm incomplete chunks are marked recoverable and rerun only missing work |
| `maintenance-worker` | Yes | Backups, retention, and stale-lock cleanup pause | Resume maintenance queue and verify alerts clear |
| `triton-rt` | Restart only with rollout awareness | Realtime inference interrupted until ready | Confirm repository integrity, readiness, and active release after restart |
| `triton-batch` | Restart only with rollout awareness | Batch chunk execution interrupted until ready | Confirm repository integrity and rerun incomplete chunks if necessary |
| PostgreSQL | No, coordinate full control-plane outage | Control plane pauses; workers cannot persist state | Restore service first, then check migrations, heartbeats, and admin UI |
| Redis | No, coordinate queue outage | Queue enqueue/dequeue pauses; durable DB state remains | Restart Redis, then run stale-lock cleanup and reconcile pending work |

## Backup Matrix

| Asset | Source Of Truth | Backup Method | Frequency | Restore Target |
| --- | --- | --- | --- | --- |
| `cv_platform` PostgreSQL database | PostgreSQL | `pg_dump` via `maintenance_worker.jobs.backup_metadata` when available | Hourly or before rollout | PostgreSQL `cv_platform` database |
| Audit JSONL export | Filesystem `/srv/cv-platform/audit/events/` | Copied by `backup_metadata` | Hourly | Audit root on the same node or recovery node |
| Snapshot manifests and validation reports | Filesystem `/srv/cv-platform/control/snapshots/...` | Copied by `backup_metadata` | Hourly | Control snapshot root |
| Serving history and smoke reports | Filesystem `/srv/cv-platform/serving/history/...` | Copied by `backup_metadata` | After each deployment and hourly | Serving history root |
| DVC manifest metadata | Filesystem `<dvc_repo>/manifests/` | Copied by `backup_metadata` | Hourly | DVC repo metadata root |
| Immutable serving packages | Filesystem `/srv/cv-platform/serving/packages/...` | Filesystem snapshot or storage replication | Before promotion and nightly | Serving packages root |
| Training checkpoints for active runs | Filesystem `/srv/cv-platform/training/trn_*/checkpoints/` | Filesystem snapshot | Every checkpoint interval plus nightly snapshot | Training root |
| Batch output manifests and bundles | Filesystem `/srv/cv-platform/inference/batch/...` | Filesystem snapshot | Nightly | Batch root |

## Restore Plan

1. Restore PostgreSQL first.
2. Restore `/srv/cv-platform/audit`, `/srv/cv-platform/control/snapshots`, `/srv/cv-platform/serving/history`, and DVC manifest metadata.
3. Restore immutable serving packages before trying to reattach deployment targets.
4. Restore active training checkpoints before restarting `training-worker`.
5. Start infrastructure, then Python services, then workers.
6. Run:
   - `reconcile_training_jobs`
   - `cleanup_stale_locks`
   - targeted batch reruns for incomplete chunks
7. Verify:
   - `Operational Health` has no critical alerts
   - active realtime release is intact and passes smoke validation
   - audit log shows resumed maintenance and recovery events

## Retention Matrix

| Asset Class | Managed Path | Retention | Enforced By |
| --- | --- | --- | --- |
| Operational logs | `/srv/cv-platform/logs/` | `log_retention.operational_days` | `maintenance_worker.jobs.artifact_retention` |
| Audit events | `/srv/cv-platform/audit/` | `log_retention.audit_days` | external archival policy plus audit export retention |
| Training checkpoints | `/srv/cv-platform/training/trn_*/checkpoints/` | `artifact_retention.checkpoints_days`, preserving newest checkpoints per inactive run | `maintenance_worker.jobs.checkpoint_pruning` |
| Label Studio exports | `/srv/cv-platform/control/labelstudio/exports/` | `artifact_retention.exports_days` | `maintenance_worker.jobs.artifact_retention` |
| Batch outputs and bundles | `/srv/cv-platform/inference/batch/` | `artifact_retention.batch_outputs_days` | `maintenance_worker.jobs.artifact_retention` |
| Quarantined partial builds and exports | `/srv/cv-platform/quarantine/` | `artifact_retention.quarantine_days` | `maintenance_worker.jobs.artifact_retention` |

## Crash Recovery Procedures

### Web UI or Admin API Restart

- Restart `admin-web` or `admin-api`.
- Do not restart workers just because the UI restarted.
- Verify `Training Jobs` still show the active run and its latest checkpoint.

### Training Worker Crash

1. Check `Operational Health` for stale `training-worker` heartbeat and GPU lock alerts.
2. Run `cleanup_stale_locks`.
3. Run `reconcile_training_jobs`.
4. Expected behavior:
   - if a resume-safe checkpoint exists, the job moves to `resuming` and requeues `resume_training_job`
   - otherwise the job moves back to `queued` and requeues `run_training_job`
5. Confirm the latest checkpoint stays immutable and the training job keeps the same `training_job_id` and audit trail.

### Training Reaches 100 Percent Then Fails

1. Open the training job detail and inspect `last_error`.
2. If the failure references `git` or `dvc`, verify the worker runtime directly:

   ```powershell
   docker compose -f infra/compose/compose.prod.yaml exec -T training-worker sh -lc "which git && which dvc && dvc version | head -n 1"
   ```

3. If the failure references `mlflow-artifacts` or `file:///workspace/mlruns`, verify the worker runtime resolves the tracking URI to the running MLflow server:

   ```powershell
   docker compose -f infra/compose/compose.prod.yaml exec -T training-worker uv run --package training-worker python -c "import mlflow; print(mlflow.get_tracking_uri())"
   ```

4. If the CLI or MLflow tracking URI is wrong after a dependency or compose rollout, rebuild the affected Python service image, restart `training-worker`, and rerun the training job.
5. Do not treat this as a model-quality or GPU-training failure; the run failed in the post-training registration path.

### Batch Worker Crash

1. Check failed or missing chunk outputs on the selected batch job.
2. Confirm the job detail exposes `artifacts.recoverable=true` and lists `incomplete_chunk_nos`.
3. Restart `batch-worker`.
4. Rerun only incomplete or failed chunks; do not rebuild completed chunk outputs.

### Deployment Failure

- A failed stage or smoke test must not change the active production pointer.
- Review the deployment release detail and repository integrity alerts.
- If partial repository content exists, it remains quarantined or isolated from the active pointer.
- Roll back only if the previous release is healthy and still present in the immutable repository.

### Redis Restart Or Queue Stall

1. Restart Redis.
2. Run `cleanup_stale_locks`.
3. Run `reconcile_training_jobs`.
4. Inspect webhook and maintenance backlog before reopening new submissions.

## Disk And GPU Monitoring Plan

- Disk usage is sampled for:
  - platform state root
  - training root
  - serving root
  - audit root
  - logs root
- GPU telemetry is sampled from `nvidia-smi`:
  - utilization percent
  - memory used
  - memory total
  - memory used percent
  - temperature
- Alert thresholds are configured in `observability.*`.
- Operational response order on pressure:
  1. pause non-critical batch submissions
  2. inspect active training and checkpoint age
  3. apply retention and clear quarantine growth
  4. if realtime inference headroom is threatened, pause or reschedule training at the next checkpoint boundary

## Failure Injection Test Plan

Run these drills on the production-like compose profile before declaring the node operational:

1. Restart `admin-api` during an active training run. Expected result: the run continues, UI reconnects, no checkpoint loss.
2. Kill `training-worker` during active training after at least one checkpoint. Expected result: stale heartbeat alert, stale GPU lock cleanup, checkpoint-based resume.
3. Stop Redis briefly during webhook delivery burst. Expected result: webhook rows remain durable, backlog becomes visible, follow-up processing resumes after recovery.
4. Corrupt a staged Triton repository `model.plan` copy. Expected result: repository integrity alert appears before promotion.
5. Fail a deployment smoke test. Expected result: active production release remains unchanged.
6. Fill the logs or quarantine filesystem until the warning threshold triggers. Expected result: disk alert appears in the admin UI.
7. Force one batch chunk to fail. Expected result: completed chunks remain durable, incomplete chunk numbers are exposed, rerun only missing work.
8. Remove a required secret in a production-like config. Expected result: service startup validation fails fast and the service does not advertise readiness.

## Disaster Recovery Checklist

- Restore PostgreSQL backup and verify schema accessibility.
- Restore audit JSONL, snapshot manifests, DVC manifests, and serving history.
- Restore immutable serving packages and active training checkpoints.
- Start infrastructure services.
- Start Python HTTP services and confirm `startup` and `ready` probes.
- Start workers and confirm fresh heartbeats in `Operational Health`.
- Run stale-lock cleanup.
- Run training reconciliation.
- Verify deployment targets and repository integrity.
- Run a realtime smoke inference and a small batch job.
- Confirm audit events are being written both to PostgreSQL and JSONL export.
