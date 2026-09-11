# Engineer Guide

This guide is for ML engineers and platform engineers maintaining the repository and runtime. Normal operators should stay in the admin UI.

## Repository Responsibilities

- `backend/services/admin-api`: control-plane front door for operator workflows
- `backend/services/webhook-receiver`: persistence-first external webhook ingress
- `backend/services/inference-gateway`: isolated realtime and batch runtime entrypoints
- `backend/workers/control-worker`: webhook, dataset, deploy, and maintenance queues
- `backend/workers/training-worker`: training queue only
- `backend/workers/batch-worker`: batch queue only
- `backend/packages/cv-db`: persistence models and read/write stores
- `backend/packages/cv-training`: deterministic training runtime and artifact conventions
- `backend/packages/cv-deploy`: immutable serving package and Triton repository management
- `frontend/apps/admin-web`: operator UI

## Local Engineering Workflow

1. Validate Python dependencies and configuration.
2. Run migrations before integration work:

   ```powershell
   uv run alembic -c backend/migrations/alembic.ini upgrade head
   ```

3. Start the dev stack or the individual services you need.
4. Run backend verification:

   ```powershell
   pytest -q
   ```

5. Run frontend verification from `frontend/apps/admin-web`:

   ```powershell
   .\node_modules\.bin\tsc.cmd --noEmit
   .\node_modules\.bin\vite.cmd build
   ```

## Control-Plane Rules

- Keep `admin-api` authoritative for operator workflows.
- Do not move long-running work into request handlers.
- Persist control-plane intent before queueing background execution.
- Preserve the separation between:
  - control plane and training plane
  - training artifacts and serving artifacts
  - realtime and batch inference
  - control plane, runtime plane, and inference plane
- Keep MLflow and DVC behind internal services for normal operator workflows.

## Runtime Integration Rules

- Runtime pipeline model choices come from deployed model capabilities projected
  from deployment releases and targets. Do not add a separate model metadata
  source for runtime nodes.
- Channel create/edit flows should assign published runtime pipeline versions
  through selectors, not operator-entered internal IDs.
- Trigger endpoints and runtime service endpoints use the shared API-key model
  for machine calls; browser sessions stay on the control-plane side.
- Manual and pipeline capture use platform `LabelProject` records. Raw Label
  Studio project numbers are not a runtime configuration interface.
- Keep the channel -> runtime -> inference -> result -> overlay/capture/output
  path covered when changing node handlers, deployment discovery, or service
  auth.
- Keep livestream publication in the runtime plane. Production live video uses
  runtime-owned GStreamer publication and MediaMTX RTSP/WebRTC fanout; MJPEG and
  latest-frame APIs are debug/snapshot paths only.
- Treat `output.siemens_sps_write` as a runtime side-effect node. It must keep
  typed config validation, mockable S7 client abstraction, dry-run/write-allow
  safety gates, per-write status payloads, and offline `python-snap7` wheel
  deployment intact.

See [../runtime/end-to-end-runtime-integration.md](../runtime/end-to-end-runtime-integration.md).
See also [../runtime/livestream-streaming-architecture.md](../runtime/livestream-streaming-architecture.md).
See also [../runtime/siemens-sps-write-node.md](../runtime/siemens-sps-write-node.md).

## Deployment Rules

- Serving packages are immutable and versioned per model version.
- Triton repository updates must prepare staged content before the release becomes active.
- Gateways must route with exact Triton model version numbers.
- Production promotion must not switch traffic before compatibility and smoke checks pass.
- Rollback must reactivate an existing healthy release instead of rebuilding artifacts.

## Deterministic Path And Naming Rules

- Storage roots come from typed platform settings.
- Snapshot materialization uses stable snapshot IDs and deterministic split metadata.
- Deployment package paths are deterministic per model version.
- Triton stable model names are derived from target path, family slug, and contract major.
- Numeric Triton version directories are immutable once staged.

## Contract And Schema Maintenance

- Export canonical OpenAPI files to `contracts/openapi/`.
- Export the platform settings schema to `config/schemas/platform-settings.schema.json`.
- Keep frontend schema types aligned with backend contracts.
- When changing workflow contracts, update:
  - backend Pydantic models
  - frontend schema package
  - relevant UI consumers
  - operational documentation

## Supported Model Maintenance

- Treat `backend/packages/cv-db/src/cv_db/model_catalog.py` as the only source of truth for supported model capabilities.
- Do not add frontend-only, template-only, catalog-only, or train-only model entries.
- A normal selectable model must have dataset conversion, real trainer routing, checkpointing, evaluation, real ONNX export, deployment packaging, deployment compatibility, and inference contracts.
- Store exact trainer backend and concrete architecture names in metadata. Do not use ambiguous labels such as only `small` or `large` as trainer input.
- Keep template generation and UI selectors backend-driven through `GET /api/v1/model-capabilities`.
- Add routing, dataset conversion, export validation, deployment compatibility, inference contract, and UI/API consistency tests for every new supported family/version/size.

## Promotion Policy Maintenance

- Template defaults live on `ModelTemplate.default_promotion_policy`.
- The effective model policy lives on `ModelFamily.promotion_policy`.
- Training jobs and model versions persist the resolved model policy so deployment decisions never depend on hidden global defaults.
- Deployment approval, automatic deployment, and promotion must evaluate the resolved model-level policy unless an explicitly audited bypass is used.
- Keep criteria typed through `PromotionCriteriaPolicy`; do not introduce raw JSON or free-text promotion gates.

## Release Checklist For Engineers

1. `pytest -q` is green.
2. Admin-web typecheck and production build are green.
3. Docs reflect the current environment variables, workflow surfaces, and service boundaries.
4. Generated file tree is refreshed.
5. The acceptance report is updated if architecture-significant behavior changed.

Reference docs:

- [../operations/environment-config-matrix.md](../operations/environment-config-matrix.md)
- [../operations/startup-sequence.md](../operations/startup-sequence.md)
- [../operations/go-live-checklist.md](../operations/go-live-checklist.md)
- [../operations/acceptance-report.md](../operations/acceptance-report.md)
