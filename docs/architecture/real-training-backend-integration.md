# Real Training Backend Integration

This note extends [cv-platform-spec.md](./cv-platform-spec.md). The platform keeps the existing control-plane, queue, worker, registry, deployment, realtime inference, and batch inference boundaries. Normal operators use the browser UI; training and deployment stay durable background work.

## Source Of Truth

The backend capability matrix is authoritative:

- code: `backend/packages/cv-db/src/cv_db/model_catalog.py`
- API: `GET /api/v1/model-capabilities`
- contract: `SupportedModelArchitecture`

Frontend model creation, template administration, pipeline model selection, automation selectors, deployment compatibility views, and inference capability views must read this backend capability source. A model is `supported` only when the dataset conversion path, real trainer, checkpointing, evaluation, real ONNX export, deployment packaging, deployment compatibility, and public inference contract are implemented.

Unsupported or partial entries must stay hidden from normal operator selection.

## Supported Matrix

Classification:

- ResNet-18, ResNet-50
- MobileNetV3 Small, MobileNetV3 Large
- EfficientNet-B0
- YOLOv11 n/s/m/l/x
- YOLOv26 n/s/m/l/x

Detection:

- YOLOv8 n/s/m/l/x
- YOLOv11 n/s/m/l/x
- YOLOv26 n/s/m/l/x

Segmentation:

- DeepLabV3 ResNet-50
- FCN ResNet-50
- LRASPP MobileNetV3 Large
- YOLOv8 n/s/m/l/x
- YOLOv11 n/s/m/l/x
- YOLOv26 n/s/m/l/x

## Trainer Routing

Capability entries resolve to exact trainer inputs, not vague size labels:

| Task/family | Trainer backend | Concrete architecture examples |
| --- | --- | --- |
| torchvision classification | `torchvision-classification` | `resnet18`, `resnet50`, `mobilenet_v3_small`, `efficientnet_b0` |
| YOLO classification | `ultralytics-yolo` | `yolo11n-cls`, `yolo26x-cls` |
| YOLOv8 detection | `openmmlab-mmyolo` | `yolov8_n_syncbn_fast_8xb16-500e_coco` |
| YOLOv11/YOLOv26 detection | `ultralytics-yolo` | `yolo11n`, `yolo26x` |
| torchvision segmentation | `torchvision-segmentation` | `deeplabv3_resnet50`, `fcn_resnet50`, `lraspp_mobilenet_v3_large` |
| YOLO segmentation | `ultralytics-yolo` | `yolov8n-seg`, `yolo11n-seg`, `yolo26x-seg` |

Resolved trainer backend and concrete architecture are persisted in training runtime metadata, model metadata, export metadata, and deployment metadata.

## Dataset Preparation

The worker stages canonical snapshots into trainer-ready datasets:

- classification: image-folder layout with deterministic class directories
- detection: COCO and YOLO-label compatible layouts
- segmentation: COCO polygon/mask data and YOLO segmentation labels

Draft and unsubmitted Label Studio tasks are ignored according to snapshot validation rules. Dataset manifests record the resolved format, split counts, class map, image roots, and conversion metadata.

## Training And Export

Real mode is the production default. Simulation remains development-only and is blocked by normal deployment policy.

Every supported production trainer must produce:

- real GPU-backed training when `cuda:*` is requested
- real framework checkpoints
- real evaluation metrics
- resume-safe checkpoints where the backend supports resume
- structured dataset, checkpoint, evaluation, export, and artifact-validation manifests
- real ONNX export

TensorRT engines are built by deployment packaging when the target path requires them. Training must not emit fake `.plan` files.

## Transfer Learning

The shared policy is `auto_pretrained_le_2000`:

- datasets with 2000 or fewer eligible items use pretrained initialization when the backend has intended pretrained weights
- larger datasets follow the recipe scratch/pretrained policy
- the resolved mode is recorded in runtime metadata

## Promotion Criteria

Deployment promotion criteria are typed configuration:

- template default: `ModelTemplate.default_promotion_policy`
- effective model value: `ModelFamily.promotion_policy`
- run/deployment usage: training jobs and model versions persist the resolved policy

Deployment approval, automatic deployment, and promotion requests evaluate the model version against the resolved model-level policy. Once a model exists, deployment decisions must not depend on hidden global defaults.

## Adding Future Families

To add a model family without creating partial support:

1. Add the capability only after dataset conversion, real training, checkpointing, evaluation, export, deployment packaging, and inference contracts exist.
2. Add exact trainer backend and concrete architecture resolution in `model_catalog.py`.
3. Generate templates from the same capability source.
4. Add routing, export validation, deployment compatibility, and inference contract tests.
5. Keep experimental or incomplete combinations hidden by default.
