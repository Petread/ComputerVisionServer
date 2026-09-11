# Metric Definitions

The training plane persists every metric point per run and uses explicit, task-aware defaults for checkpoint
selection and promotion evaluation.

Classification defaults:

- training: `loss`
- validation: `accuracy_top1`, `precision_macro`, `recall_macro`, `f1_macro`
- evaluation: `accuracy_top1`, `precision_macro`, `recall_macro`, `f1_macro`
- default best-checkpoint rule: maximize `accuracy_top1`
- default promotion criteria: `accuracy_top1 >= 0.90` and `f1_macro >= 0.85`

Detection defaults:

- training: `loss_cls`, `loss_bbox`
- validation: `map_50`, `map_50_95`, `precision`, `recall`
- evaluation: `map_50`, `map_50_95`, `precision`, `recall`
- default best-checkpoint rule: maximize `map_50_95`
- default promotion criteria: `map_50_95 >= 0.55` and `precision >= 0.70`

These definitions are encoded in the shared training runtime so the UI, worker, and post-training registration
path all evaluate runs against the same metric vocabulary.
