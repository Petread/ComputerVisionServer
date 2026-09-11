# Label Studio Predictions

## Status

Label Studio predictions are integrated through the internal
`label-studio-ml-adapter` service.

Do not connect the raw CV Platform realtime gateway directly in Label Studio's
`Connect Model` dialog. Label Studio expects an ML backend root that exposes
health/setup/predict behavior and accepts Label Studio task JSON. The adapter is
that compatibility layer.

Current in-stack backend URL pattern:

```text
http://label-studio-ml-adapter:8089/api/v1/projects/<label_project_id>
```

The platform stores this URL in Label Studio through the Label Studio API. It is
created per project only after an operator links that project from the Pipeline
Detail page.

Useful upstream references:

- Label Studio ML backend overview: <https://labelstud.io/guide/ml>
- Create/connect ML backends through the API:
  <https://api.labelstud.io/api-reference/api-reference/ml/create>
- List project ML backends:
  <https://api.labelstud.io/api-reference/api-reference/ml/list>
- Update ML backends:
  <https://api.labelstud.io/api-reference/api-reference/ml/update>

## Operator Flow

1. Train and deploy a model for the project.
2. Ensure the deployment includes the realtime target. Label Studio predictions
   are synchronous and cannot use batch-only deployments.
3. Open `Pipelines -> <project>`.
4. In `Secondary and advanced actions`, select
   `Connect deployed model to Label Studio`.
5. The Admin API creates or updates one Label Studio ML backend for that Label
   Studio project.
6. In Label Studio, use predictions as usual. Predictions are proposed labels;
   they become submitted annotations only after a user accepts/submits them in
   Label Studio.

No predictions are uploaded by this action. The platform connects the project's
currently deployed realtime model as a Label Studio ML backend. No project is
linked automatically just because a model exists; the first connection is
intentionally project-scoped and operator-triggered.

## Auto-Update Behavior

After a project has been linked once, the connection is stable:

- Label Studio keeps pointing to the same adapter URL for that project.
- The adapter resolves the active realtime deployment for the project at
  prediction time.
- When a newer model is deployed to realtime for that project, the deployment
  worker refreshes the stored Label Studio backend metadata and the adapter
  immediately serves the new active version.
- If a project is not linked, deployments do not create a Label Studio backend.

This means the Pipeline Detail button is the opt-in switch; subsequent realtime
deployments keep the linked project current.

## What The Adapter Does

For each Label Studio prediction request the adapter:

1. Resolves the active realtime deployment for the requested
   `label_project_id`.
2. Reads the image reference from the Label Studio task data.
3. Downloads the image from Label Studio or fetches an external URL.
4. Calls the existing realtime inference service internally.
5. Converts the platform result into Label Studio prediction JSON.

Supported task mappings:

| Project task type | Label Studio result type |
| --- | --- |
| Classification | `choices` |
| Anomaly | `choices` (`normal`/`anomalous` mapped to project labels when possible) |
| Detection | `rectanglelabels` |
| Segmentation | `polygonlabels` |

The adapter infers `from_name` and `to_name` from the Label Studio config when
possible and falls back to common defaults such as `choice -> image` and
`label -> image`.

## Configuration

The relevant platform settings live under `label_studio`:

```yaml
label_studio:
  base_url: http://label-studio:8080
  token_file: /run/secrets/labelstudio_api_token
  ml_backend_adapter_url: http://label-studio-ml-adapter:8089
  ml_backend_timeout_seconds: 60
```

`ml_backend_adapter_url` must be reachable from the Label Studio container. In
the bundled compose setup, the service name
`http://label-studio-ml-adapter:8089` is the intended internal address.

The bundled dev/prod compose files include `label-studio-ml-adapter` as a
normal service. It starts with `docker compose up -d` after migrations and is
reachable only inside the compose networks unless exposed explicitly.

## API Surface

Admin API:

```text
POST /api/v1/project-pipelines/{label_project_id}:link-labelstudio-predictions
```

Adapter endpoints:

```text
GET  /health
GET  /api/v1/projects/{label_project_id}/health
POST /api/v1/projects/{label_project_id}/setup
POST /api/v1/projects/{label_project_id}/predict
POST /api/v1/projects/{label_project_id}/webhook
```

Label Studio API endpoints used by the platform:

```text
GET   /api/ml/?project=<label_studio_project_id>
POST  /api/ml/
PATCH /api/ml/<backend_id>
```

## Troubleshooting

- Link button is disabled: deploy a realtime model for the project first.
- Link fails with Label Studio connectivity errors: verify that Label Studio can
  reach `http://label-studio-ml-adapter:8089/api/v1/projects/<label_project_id>/health`
  inside the compose network.
- Predictions are empty or hidden: check that the Label Studio labeling config
  uses supported CV controls and that model labels match the project labels.
  Detection and segmentation predictions must use the same `from_name` and
  `to_name` as the labeling config, for example `label -> image`.
- Adapter returns `labelstudio_model_unavailable`: the project has no active
  realtime deployment, or the global realtime target currently points to another
  project's model.
