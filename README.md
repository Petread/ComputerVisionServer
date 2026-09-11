# On-Prem Computer Vision Platform

This repository contains the completed production-ready implementation of the on-prem computer vision platform defined in [docs/architecture/cv-platform-spec.md](docs/architecture/cv-platform-spec.md).

The platform runs on a Windows workstation while hosting the runtime stack inside `Ubuntu 24.04 LTS on WSL2`, and it keeps the required boundaries intact:

- browser-first operator workflows with no normal-workflow CLI dependency
- strict separation of control plane, training plane, realtime inference, and batch inference
- immutable dataset snapshots, model versions, deployment packages, and Triton release directories
- safe stage, promote, smoke-test, and rollback flows
- MLflow OSS and DVC hidden behind internal services for normal operators
- deterministic paths, artifact names, and resumable background jobs

## Platform Shape

- `admin-web`: the single operator UI for dataset upload, project mapping, snapshots, training, model review, deployment, realtime testing, batch inference, audit, and health.
- `admin-api`: the control-plane front door used by the admin UI for authenticated workflow commands, durable state transitions, queue submission, and health aggregation.
- `webhook-receiver`: fast Label Studio ingress that persists raw events before queueing follow-up work.
- `dataset-service`, `training-orchestrator`, `model-registry-service`, `deployment-controller`: bounded service surfaces that preserve the architecture split while sharing contracts and persistence packages.
- `control-worker`, `training-worker`, `batch-worker`, `maintenance-worker`: background execution isolated by queue ownership.
- `inference-gateway-rt`, `inference-gateway-batch`: separate runtime entrypoints backed by separate Triton instances and target-specific deployment pointers.

## Repository Layout

- `backend/`: Python services, workers, shared packages, and Alembic migrations
- `frontend/`: React/TypeScript admin UI and shared packages
- `contracts/`: exported OpenAPI and AsyncAPI contracts
- `config/`: typed defaults, environment overlays, and JSON schema exports
- `infra/`: compose stacks, reverse proxy, observability, secrets layout, and systemd support
- `ml/`: recipes, manifests, compatibility metadata, and runtime benchmark defaults
- `docs/`: architecture, operations, engineering guides, checklists, and generated tree
- `scripts/`: config export, migrations, backup, benchmark, and release utilities
- `tests/`: contract, unit, integration, e2e, performance, and acceptance coverage
- `examples/`: representative API request payloads

## Verification

Repository verification completed on `2026-04-21`:

- `pytest -q`: `57 passed`
- `frontend/apps/admin-web`: `tsc --noEmit` passed
- `frontend/apps/admin-web`: `vite build` passed

See the full repository acceptance mapping in [docs/operations/acceptance-report.md](docs/operations/acceptance-report.md).

## Documentation

- Architecture specification: [docs/architecture/cv-platform-spec.md](docs/architecture/cv-platform-spec.md)
- Edge orchestration setup and operations guide (German): [docs/runtime/edge-orchestration-operations-guide.md](docs/runtime/edge-orchestration-operations-guide.md)
- Real training integration: [docs/architecture/real-training-backend-integration.md](docs/architecture/real-training-backend-integration.md)
- Service catalog: [docs/architecture/service-catalog.md](docs/architecture/service-catalog.md)
- API contracts overview: [docs/architecture/api-contracts.md](docs/architecture/api-contracts.md)
- Operator guide: [docs/operations/operator-guide.md](docs/operations/operator-guide.md)
- Label Studio predictions integration: [docs/integrations/label-studio-predictions.md](docs/integrations/label-studio-predictions.md)
- Real vs simulation note: [docs/operations/real-vs-simulation-training.md](docs/operations/real-vs-simulation-training.md)
- Engineer guide: [docs/engineering/engineer-guide.md](docs/engineering/engineer-guide.md)
- WSL2/CUDA training setup: [docs/engineering/wsl2-cuda-openmmlab-setup.md](docs/engineering/wsl2-cuda-openmmlab-setup.md)
- Environment/config matrix: [docs/operations/environment-config-matrix.md](docs/operations/environment-config-matrix.md)
- Startup sequence: [docs/operations/startup-sequence.md](docs/operations/startup-sequence.md)
- Operator workflow map: [docs/operations/operator-workflow-map.md](docs/operations/operator-workflow-map.md)
- Troubleshooting guide: [docs/operations/troubleshooting-guide.md](docs/operations/troubleshooting-guide.md)
- Go-live checklist: [docs/operations/go-live-checklist.md](docs/operations/go-live-checklist.md)
- Production runbook: [docs/operations/production-operations-runbook.md](docs/operations/production-operations-runbook.md)
- Generated file tree: [docs/generated/monorepo-file-tree.md](docs/generated/monorepo-file-tree.md)

## Tooling

- Python workspace: `uv`
- Frontend workspace: `pnpm`
- Linting and formatting: `ruff`, `eslint`
- Runtime orchestration: `docker compose`

## Notes

- The admin UI uses `admin-api` for operator workflows and only reaches the inference gateways directly for realtime and batch testing/status views.
- Production inference access is authenticated; anonymous direct gateway access is not part of the production design.
- Realtime and batch traffic stay isolated even though the gateway codebase is shared.
