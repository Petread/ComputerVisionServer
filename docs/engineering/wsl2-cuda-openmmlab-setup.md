# WSL2 CUDA Real-Training Setup

This guide covers the engineering prerequisites for the real training backend on the architecture defined in [../architecture/cv-platform-spec.md](../architecture/cv-platform-spec.md).

## Target Runtime

- Windows host
- Ubuntu 24.04 on WSL2
- NVIDIA GPU exposed to WSL2
- Dedicated `training-worker` container image on Python 3.11
- PyTorch CUDA support in that worker image

## Required Python Packages

The real detection backend expects these packages to be importable at job launch:

- `torch`
- `mmcv`
- `mmengine`
- `mmdet`
- `mmyolo`
- `mmdeploy`
- `onnx`
- `onnxruntime`
- `Pillow`

The supported MMYOLO runtime is intentionally pinned to a worker-only compatibility set:

- Python `3.11`
- PyTorch `2.0.0+cu118`
- `mmcv 2.0.1`
- `mmdet >=3.0.0,<3.1.0`
- `mmyolo 0.6.0`

Rebuild the dedicated training worker image after changing the real-training dependency set:

```powershell
docker compose -f infra/compose/compose.dev.yaml build training-worker
docker compose -f infra/compose/compose.dev.yaml up -d training-worker
```

## Backend Expectations

The current real backend resolves MMYOLO model configs and deploy configs from the installed package or source tree. The runtime must therefore include MMYOLO configuration files, not only importable Python modules.

The deployment packaging stage expects MMDeploy TensorRT APIs to be available when it needs to build a real engine from ONNX.

## Environment Checks

At startup or job launch the worker should fail fast when:

- `torch.cuda.is_available()` is false for a CUDA-targeted run
- the worker is not running on Linux/Python 3.11
- MMCV or MMDetection versions are outside the supported MMYOLO range
- MMEngine/MMYOLO/MMDeploy imports fail
- snapshot conversion cannot materialize a valid staged dataset
- ONNX export tooling is unavailable

The platform intentionally does not fall back silently from real mode to simulation.

## Recommended Local Verification

1. Verify WSL2 CUDA visibility with PyTorch.
2. Verify the OpenMMLab stack imports in the training-worker environment.
3. Queue a tiny detection training run from the UI.
4. Confirm `.pth` checkpoints appear in the job checkpoint directory.
5. Confirm the export manifest references a real ONNX file.
6. Build a deployment package and confirm a real `model.plan` is produced in the serving package.

## Troubleshooting

### CUDA unavailable

- confirm the NVIDIA Windows driver supports CUDA in WSL2
- confirm the WSL2 distro can see the GPU
- confirm PyTorch was installed with the correct CUDA build

### Backend import failure

- verify the `training-worker` container was rebuilt from `infra/docker/training-worker.Dockerfile`
- verify the training-worker environment contains `torch`, `mmcv`, `mmengine`, `mmdet`, `mmyolo`, `mmdeploy`, `onnx`, and `onnxruntime`
- verify MMYOLO and MMDeploy were installed with their config files
- verify the worker is using Python 3.11 and not the shared `/workspace/.venv` Python 3.12 environment

### Dataset conversion failure

- check snapshot `validation_report.json`
- check `canonical_dataset.json`
- confirm required image files exist under the immutable snapshot root

### Checkpoint or export failure

- inspect training-worker structured logs for checkpoint and export events
- verify the checkpoint directory is writable
- verify ONNX and MMDeploy dependencies are installed in the same environment as the worker

### Unsupported model or task selection

The current production-safe support matrix is owned by the backend capability API and documented in [supported-model-matrix.md](../operations/supported-model-matrix.md).

OpenMMLab/MMYOLO is still used for YOLOv8 detection. Torchvision and Ultralytics backends cover the other supported classification, detection, and segmentation entries. Anomalib plus OpenVINO cover the supported PatchCore anomaly path. If a model is not returned by `GET /api/v1/model-capabilities`, it must remain unavailable to operators.
