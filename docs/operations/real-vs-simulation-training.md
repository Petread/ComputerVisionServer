# Real vs Simulation Training

This operator note explains how to distinguish production-ready real training runs from simulation-only runs.

## What Real Training Looks Like

A real training run shows:

- `trainer_mode: real`
- a concrete backend such as `torchvision-classification`, `torchvision-segmentation`, `openmmlab-mmyolo`, or `ultralytics-yolo`
- real checkpoint count greater than zero
- framework checkpoint files such as `.pth` or `.pt`
- evaluation metrics for the task, such as `accuracy_top1`, `f1_macro`, `map_50_95`, `map_50`, `mask_map_50_95`, or `mask_map_50`
- export validation status that reports passed checks
- an ONNX artifact reference in the export manifest

## What Simulation Looks Like

A simulation run is explicitly marked:

- `trainer_mode: simulation`
- `simulation_only: true`
- simulation-specific checkpoint metadata files
- simulation validation status instead of passed real-artifact checks
- no deployable ONNX/TensorRT path in the normal production flow

Simulation must never be treated as equivalent to a real trained model.

## UI Fields to Check

In training history and job detail views, verify:

- `Mode`
- `Backend`
- `Checkpoint Count`
- `Artifact Validation`

In model and deployment views, verify:

- `trainer_mode`
- `trainer_backend`
- export-manifest `artifact_validation`
- deployment package readiness

## Deployment Policy

By default the platform rejects simulation-trained model versions at approval and promotion time.

An operator should not attempt to deploy a version when:

- `trainer_mode` is `simulation`
- artifact validation is missing
- `checkpoint_valid` is false
- `onnx_valid` is false
- the export manifest is missing a real ONNX artifact

## Typical Real Training Flow

1. Queue training from the app.
2. Watch the job move through data prep, training, evaluation, export, and registration.
3. Confirm real checkpoints exist.
4. Confirm evaluation metrics are present.
5. Confirm export validation passes.
6. Review the registered model version.
7. Approve and package only after the real-artifact checks pass.

## If a Run Looks Wrong

Treat the run as non-deployable and hand it to engineering when:

- the mode is simulation but the operator expected real training
- the backend field is empty or unexpected
- checkpoints exist but are not real framework checkpoint files
- metrics look absent or obviously placeholder-like
- export validation failed

See [troubleshooting-guide.md](./troubleshooting-guide.md) and the engineering setup note for environment-level failures.
