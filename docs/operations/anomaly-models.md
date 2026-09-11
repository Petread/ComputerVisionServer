# Anomaly Models

Anomaly is a first-class task type. Operators should choose `Anomaly` when the
goal is to learn normal surface or part appearance and flag deviations. Do not
create a binary classification model just because the labels are good and bad.

## Supported Methods

Current exposed support:

| Method | Backend | Labels | Outputs |
| --- | --- | --- | --- |
| PatchCore | Anomalib | image-level good/bad | anomaly score, normal/anomalous decision, heatmap |

Not exposed yet: PaDiM, EfficientAD, FastFlow, Reverse Distillation, STFPM, and
mask-supervised anomaly localization. They must stay out of templates and UI
selectors until dataset preparation, training, evaluation, export, deployment,
runtime inference, and overlay rendering are all implemented.

## Label Studio Dataset Expectations

Use image-level choices for good and bad examples. The normal Label Studio
Classification/Choices template is acceptable for this. Label Studio itself
does not know that the project is an anomaly project; it only stores the chosen
labels. The platform records the operator's intent when a model is created:
for choice-only projects the Create Model dialog asks whether the model is
`Classification` or `Anomaly`.

When the project/model type is `Anomaly`, snapshot conversion interprets the
image-level choices as anomaly semantics:

- accepted good labels become canonical `normal`
- accepted bad labels become canonical `anomalous`
- boxes and polygons are ignored for the current image-level PatchCore path

When the project/model type is `Classification`, the same `good` and `bad`
labels remain ordinary classification classes. This is the operator decision
point. Choice-only Label Studio projects are discovered as `Classification` by
default because the Label Studio schema does not expose an anomaly-specific
template type. If the operator selects `Anomaly` while creating the first model,
the platform retargets the project and dataset to `Anomaly`; later Label Studio
sync and reconciliation preserve that task mapping.

Accepted normal labels:

- `good`
- `normal`
- `ok`
- `pass`

Accepted anomalous labels:

- `bad`
- `abnormal`
- `anomaly`
- `defect`
- `fail`

Snapshot validation fails when an image has conflicting good and bad labels.
PatchCore training requires normal images in the train split and both normal
and anomalous images in validation or test splits.

## Operator Flow

1. Create or discover a Label Studio project with the normal
   Classification/Choices labeling interface.
2. Label images as good/normal or bad/anomalous.
3. Open the project pipeline and choose `Create model`.
4. Select model type `Anomaly`, then select the `PatchCore Anomaly Detector`
   template.
5. Create a fresh immutable dataset snapshot and review anomaly readiness.
6. Train from the UI. The training worker uses Anomalib and records
   anomaly-specific metrics such as `image_auroc`, `image_f1`, precision,
   recall, and threshold.
7. Stage and promote the model like other supported model types.
8. Add an `Anomaly inference` node to a runtime pipeline and select a deployed
   anomaly model.
9. View `anomaly_score`, decision, and heatmap overlay in live/runtime views.

Heatmaps are returned by the deployed anomaly model. The runtime does not fake
or draw heatmaps from scores.
