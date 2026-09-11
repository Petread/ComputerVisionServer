# Startup Sequence

This sequence keeps the architecture boundaries intact and avoids bringing workers or inference traffic online before the dependencies they rely on are ready.

## 1. Preflight

1. Confirm WSL2, CUDA, and the target GPU profile are available.
2. Confirm `/srv/cv-platform` exists for production, or `.local/state` for development.
3. Confirm mounted secret files exist and match the environment profile.
4. Run Alembic migrations before starting application services:

   ```powershell
   uv run alembic -c backend/migrations/alembic.ini upgrade head
   ```

## 2. Infrastructure Services

Start these first:

1. `postgres`
2. `redis`
3. `label-studio`
4. `mlflow`
5. `triton-rt`
6. `triton-batch`

Do not start workers yet.

## 3. Control-Plane And Gateway Services

Start these second:

1. `webhook-receiver`
2. `dataset-service`
3. `training-orchestrator`
4. `model-registry-service`
5. `deployment-controller`
6. `admin-api`
7. `inference-gateway-rt`
8. `inference-gateway-batch`
9. `admin-web`
10. `reverse-proxy`

## 4. Workers

Start workers only after the previous section is healthy:

1. `control-worker`
2. `training-worker`
3. `batch-worker`
4. `maintenance-worker`

Queue ownership must remain:

- `training-worker`: `training`
- `batch-worker`: `batch`
- `control-worker`: `webhook`, `dataset`, `deploy`, `maintenance`

## 5. Bring-Up Validation

Confirm from the admin UI:

1. `Overview` shows no critical service outages.
2. `Operational Health` shows current worker heartbeats.
3. `Settings` shows storage and dependency reachability.
4. Both inference gateways are ready.
5. Realtime and batch deployment targets point to explicit releases or show the expected empty-state before first promotion.

## 6. First Functional Smoke

Run, in order:

1. Admin login
2. Dataset creation
3. Browser upload session
4. Label Studio project sync or mapping
5. Dataset snapshot request
6. Training job submission
7. Deployment package build
8. Stage and smoke-test release
9. Promote or roll back
10. Realtime request and small batch job

## 7. Shutdown Guidance

For planned shutdown:

1. Stop accepting new operator commands.
2. Let active deployment actions finish.
3. Pause or let active batch work drain if possible.
4. Allow training to checkpoint before stopping the training worker.
5. Stop workers.
6. Stop control-plane and gateway services.
7. Stop infrastructure last.
