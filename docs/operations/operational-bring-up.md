# Operational Bring-Up

Use `infra/compose/compose.prod.yaml` as the production-shaped reference stack for a single-node workstation deployment.

Canonical production bring-up references:

- Startup order: [startup-sequence.md](./startup-sequence.md)
- Environment/config matrix: [environment-config-matrix.md](./environment-config-matrix.md)
- Go-live checklist: [go-live-checklist.md](./go-live-checklist.md)
- Troubleshooting guide: [troubleshooting-guide.md](./troubleshooting-guide.md)
- Production runbook: [production-operations-runbook.md](./production-operations-runbook.md)

Minimum bring-up flow:

1. Provision WSL2 Ubuntu 24.04 with NVIDIA CUDA support and confirm the target GPU profile.
2. Mount `/srv/cv-platform` and place all required secret files under `/srv/cv-platform/secrets`.
3. Validate `config/prod/platform.yaml` plus mounted secret files.
4. Run Alembic migrations.
5. Start the production stack in the documented service order.
6. Confirm `admin-api`, both inference gateways, all workers, PostgreSQL, Redis, Label Studio, MLflow, and both Triton instances are healthy.
7. Run the full go-live checklist before opening the node for normal operators.
