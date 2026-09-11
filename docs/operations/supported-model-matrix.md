# Supported Model Matrix

The admin UI shows only models returned by the backend capability API. A visible model means the full lifecycle is implemented: Label Studio snapshot conversion, trainer-ready dataset preparation, real training, evaluation, checkpointing, real ONNX export, deployment packaging, deployment compatibility, and realtime/batch inference contracts.

## Operator Matrix

| Task | Supported models |
| --- | --- |
| Classification | ResNet-18; ResNet-50; MobileNetV3 Small; MobileNetV3 Large; EfficientNet-B0; YOLOv11 n/s/m/l/x; YOLOv26 n/s/m/l/x |
| Detection | YOLOv8 n/s/m/l/x; YOLOv11 n/s/m/l/x; YOLOv26 n/s/m/l/x |
| Segmentation | DeepLabV3 ResNet-50; FCN ResNet-50; LRASPP MobileNetV3 Large; YOLOv8 n/s/m/l/x; YOLOv11 n/s/m/l/x; YOLOv26 n/s/m/l/x |

## What Selection Means

When an operator creates a model from a template, the platform stores the exact task, family, version, size, trainer backend, concrete architecture, training profile, and resolved promotion criteria policy. Operators do not enter internal IDs.

The backend rejects unsupported combinations before a training job is queued. Template administration and model creation use the same backend capability source, so a model cannot be template-visible while being non-trainable or non-deployable.

## Promotion Criteria Policy

Each template defines default deployment gating criteria. During model creation, the operator sees the inherited criteria and may override them only when the template allows model-level overrides.

The effective policy is stored on the model and copied into training job and model version metadata. Deployment approval, automatic deployment, and promotion use that resolved policy. Deployment views show whether the selected version passed or failed the configured criteria.

Default criteria:

| Task | Default criteria |
| --- | --- |
| Classification | `accuracy_top1 >= 0.9`, `f1_macro >= 0.85` |
| Detection | `map_50_95 >= 0.55`, `map_50 >= 0.72` |
| Segmentation | `mask_map_50_95 >= 0.5`, `mask_map_50 >= 0.7` |

## Hidden Entries

Unsupported, experimental, train-only, deploy-only, and catalog-only model combinations must not appear in the normal operator workflow. Engineers add future entries through the backend capability matrix only after the end-to-end path is implemented and tested.
