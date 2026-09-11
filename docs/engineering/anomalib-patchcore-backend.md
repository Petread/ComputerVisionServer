# Anomalib PatchCore Backend

The first supported anomaly backend is PatchCore through Anomalib.

Implementation boundary:

- Task type: `anomaly`
- Family/version/size: `patchcore` / `anomalib` / `wide_resnet50_2`
- Training backend id: `anomalib-patchcore`
- Dataset staging format: `anomalib_folder_image_level`
- Export: ONNX artifact produced by the Anomalib engine path
- Runtime: inference-gateway anomaly endpoint using the Anomalib OpenVINO
  inferencer path
- Runtime outputs: anomaly score, thresholded normal/anomalous decision,
  heatmap, optional mask only when supported

The backend capability matrix in `cv_db.model_catalog` is the source of truth.
Frontend selectors and templates must not hardcode additional anomaly methods.

Dataset staging creates:

- `anomalib-folder/normal` for normal train images
- `anomalib-folder/normal_test` for normal validation/test images
- `anomalib-folder/abnormal` for anomalous validation/test images
- JSONL manifests under `annotations/anomaly_{split}.jsonl`

Validation requires at least one normal image, at least one anomalous image,
normal images in train, and both normal/anomalous images in validation or test.

Do not add PaDiM, EfficientAD, FastFlow, Reverse Distillation, STFPM, or mask
localization catalog entries until the complete platform path is implemented
and tested.
