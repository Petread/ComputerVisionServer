# Local Bring-Up

## Runtime Assumptions

- Windows workstation host
- `Ubuntu 24.04 LTS` inside WSL2 for the container stack
- `docker compose` or compatible compose runtime
- NVIDIA GPU with working Docker Desktop or WSL2 GPU passthrough for `training-worker`, `triton-rt`, and `triton-batch`
- `uv` for Python workspace commands
- `pnpm` for frontend workspace commands

## Bring-Up Sequence

1. Create mounted secret files using the layout in `infra/secrets/README.md`.
2. Review `infra/env/platform.dev.env.example` and copy non-secret environment values into your local compose environment file.
3. Generate the config schema if desired:

   ```powershell
   uv run python scripts/dev/export_config_schema.py
   ```

4. Start the development stack:

   ```powershell
   docker compose -f infra/compose/compose.dev.yaml up --build
   ```

5. Open the reverse proxy entrypoint at `http://localhost`.

## Python Dependency Changes In Dev

- The development compose stack bind-mounts the repository into `/workspace`.
- Most Python services execute the shared project environment at `/workspace/.venv`.
- `training-worker` now uses a dedicated image-local environment at `/opt/cv-platform-training-venv` so the real OpenMMLab runtime is not shadowed by the repo bind mount.
- When a non-training Python dependency changes, sync the mounted environment before relying on a container restart alone.
- When a real-training dependency changes, rebuild the `training-worker` image instead of only restarting the container.
- MLflow-aware workers also expect the runtime tracking URI to stay aligned with the in-stack `mlflow` service at `http://mlflow:5000`.
- The development Triton services bind-mount the shared repository paths `.local/state/serving/triton/realtime` and `.local/state/serving/triton/batch` so deployment staging and Triton load the same model repository contents.
- The development inference gateway currently uses the deterministic gateway backend. When placeholder export artifacts are not loadable by Triton, the dev-only deployment smoke report records the load failure but still allows endpoint testing through the gateway. Production remains strict.

1. Sync the shared workspace environment for the regular Python services:

   ```powershell
   uv sync --all-packages --group dev
   ```

2. Rebuild the dedicated training worker when the OpenMMLab runtime or training dependencies change:

   ```powershell
   docker compose -f infra/compose/compose.dev.yaml build training-worker
   docker compose -f infra/compose/compose.dev.yaml up -d training-worker
   ```

3. Restart the affected Python workers or services.

   For training and dataset lineage tooling changes, restart at least:

   ```powershell
   docker compose -f infra/compose/compose.dev.yaml restart training-worker control-worker
   ```

4. Validate that the required CLIs and MLflow tracking URI are available inside the running worker:

   ```powershell
   docker compose -f infra/compose/compose.dev.yaml exec -T training-worker sh -lc "which git && which dvc && dvc version | head -n 1"
   docker compose -f infra/compose/compose.dev.yaml exec -T training-worker uv run --package training-worker python -c "import mlflow; print(mlflow.get_tracking_uri())"
   docker compose -f infra/compose/compose.dev.yaml exec -T training-worker uv run --package training-worker python -c "import torch, mmcv, mmengine, mmdet, mmyolo, mmdeploy, onnx, onnxruntime; print({'torch': torch.__version__, 'cuda_available': torch.cuda.is_available(), 'mmcv': mmcv.__version__, 'mmdet': mmdet.__version__, 'mmyolo': mmyolo.__version__})"
   docker compose -f infra/compose/compose.dev.yaml exec -T triton-rt sh -lc "nvidia-smi | head -n 5"
   ```

## Validation

- Admin UI: `http://localhost/`
- Admin API health: `http://localhost/api/health/ready`
- Dataset service health: `http://localhost/datasets/health/ready`
- Training orchestrator health: `http://localhost/training/health/ready`
- Model registry health: `http://localhost/registry/health/ready`
- Deployment controller health: `http://localhost/deployments/health/ready`
- Training worker DVC CLI: `docker compose -f infra/compose/compose.dev.yaml exec -T training-worker sh -lc "which dvc"`
- Training worker MLflow tracking URI: `docker compose -f infra/compose/compose.dev.yaml exec -T training-worker uv run --package training-worker python -c "import mlflow; print(mlflow.get_tracking_uri())"`
- Training worker real-training imports: `docker compose -f infra/compose/compose.dev.yaml exec -T training-worker uv run --package training-worker python -c "import torch, mmcv, mmengine, mmdet, mmyolo, mmdeploy, onnx, onnxruntime; print(torch.cuda.is_available())"`
- Triton GPU visibility: `docker compose -f infra/compose/compose.dev.yaml exec -T triton-rt sh -lc "nvidia-smi | head -n 5"`
