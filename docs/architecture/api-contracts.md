# API Contracts

## Operator-Facing API Entry Points

The admin UI uses:

- `admin-api` for authentication, datasets, uploads, snapshots, training, registry, deployments, batch jobs, audit, settings, and aggregated system status
- `runtime-admin-api` for channel-driven runtime configuration, runtime lifecycle commands, runtime pipeline authoring, derived trigger endpoint metadata reads, capture rules, output connectors, runtime status, and recent result views
- `result-service` for runtime result history, detail views, latest channel results, and typed result payload sections
- `inference-gateway-rt` for realtime testing and model-status views
- `inference-gateway-batch` for batch capability and model-status views

## Key Admin API Routes

- Auth:
  - `POST /api/v1/auth/login`
  - `POST /api/v1/auth/logout`
  - `GET /api/v1/auth/me`
- System:
  - `GET /api/v1/system/status`
  - `WS /api/v1/jobs/events/ws`
- Datasets and uploads:
  - `GET /api/v1/datasets`
  - `POST /api/v1/datasets`
  - `POST /api/v1/datasets/{dataset_id}/upload-sessions`
  - `PUT /api/v1/upload-sessions/{upload_session_id}/parts/{part_no}`
  - `POST /api/v1/upload-sessions/{upload_session_id}:complete`
  - `GET /api/v1/label-projects`
  - `POST /api/v1/label-projects`
  - `POST /api/v1/label-projects/sync`
  - `GET /api/v1/dataset-snapshots`
  - `POST /api/v1/dataset-snapshots`
- Training:
  - `GET /api/v1/model-capabilities`
  - `GET /api/v1/training-jobs`
  - `GET /api/v1/training-jobs/{training_job_id}`
  - `POST /api/v1/training-jobs`
  - `POST /api/v1/training-jobs/{training_job_id}:cancel`
  - `POST /api/v1/training-jobs/{training_job_id}:resume`
- Registry:
  - `GET /api/v1/registry/model-families`
  - `GET /api/v1/registry/model-versions`
  - `GET /api/v1/registry/model-versions/{model_version_id}`
  - lifecycle and approval commands under `/api/v1/registry/model-versions/...`
- Deployments:
  - `GET /api/v1/deployments/packages`
  - `POST /api/v1/deployments/packages`
  - `GET /api/v1/deployments/targets`
  - `GET /api/v1/deployments/releases`
  - `POST /api/v1/deployments/promotions`
  - `POST /api/v1/deployments/{target_path}:rollback`
- Batch jobs:
  - `POST /api/v1/batch-jobs`
  - `GET /api/v1/batch-jobs`
  - `GET /api/v1/batch-jobs/{batch_job_id}`
  - `GET /api/v1/batch-jobs/{batch_job_id}/results`
  - `GET /api/v1/batch-jobs/{batch_job_id}/download`
- Audit and settings:
  - `GET /api/v1/audit-events`
  - `GET /api/v1/webhook-events`
  - `GET /api/v1/settings/integration-status`

## Inference Gateway Routes

- Public consumer contract:
  - `GET /api/v1/health`
  - `GET /api/v1/ready`
  - `GET /api/v1/models`
  - `GET /api/v1/models/{model_id}`
- Realtime:
  - `POST /api/v1/infer/classify`
  - `POST /api/v1/infer/detect`
  - `POST /api/v1/infer/segment`
  - `POST /api/v1/realtime/classify`
  - `POST /api/v1/realtime/detect`
  - `POST /api/v1/realtime/segment`
  - `POST /api/v1/realtime/predict`
  - `GET /api/v1/realtime/models/capabilities`
  - `GET /api/v1/realtime/models/status`
- Batch:
  - `POST /api/v1/batch/jobs`
  - `GET /api/v1/batch/jobs/{job_id}`
  - `GET /api/v1/batch/jobs/{job_id}/results`
  - `DELETE /api/v1/batch/jobs/{job_id}`
  - `POST /api/v1/batch/internal/jobs`
  - `GET /api/v1/batch/internal/jobs`
  - `GET /api/v1/batch/internal/jobs/{batch_job_id}`
  - `GET /api/v1/batch/internal/jobs/{batch_job_id}/results`
  - `GET /api/v1/batch/internal/jobs/{batch_job_id}/download`
  - `POST /api/v1/batch/internal/chunks/{batch_job_id}/{chunk_no}:execute`
  - `GET /api/v1/batch/models/capabilities`
  - `GET /api/v1/batch/models/status`

## Runtime API Routes

- Runtime admin:
  - `GET /api/v1/runtime/channels`
  - `POST /api/v1/runtime/channels`
  - `GET /api/v1/runtime/channels/{channel_id}`
  - `PATCH /api/v1/runtime/channels/{channel_id}`
  - `POST /api/v1/runtime/channels/{channel_id}:start`
  - `POST /api/v1/runtime/channels/{channel_id}:stop`
  - `POST /api/v1/runtime/channels/{channel_id}:restart`
  - `GET /api/v1/runtime/channels/{channel_id}/state`
  - `GET /api/v1/runtime/channels/{channel_id}/streams`
  - `GET /api/v1/runtime/channels/{channel_id}/live-view/latest`
  - `GET /api/v1/runtime/channels/{channel_id}/results/latest`
  - `GET /api/v1/runtime/channels/{channel_id}/manual-capture/targets`
  - `POST /api/v1/runtime/channels/{channel_id}/manual-capture/single`
  - `POST /api/v1/runtime/channels/{channel_id}/manual-capture/interval-session`
  - `POST /api/v1/runtime/channels/{channel_id}/manual-capture/interval-session:stop`
  - `GET /api/v1/runtime/channels/{channel_id}/manual-capture/session`
  - `GET /api/v1/runtime/channels/{channel_id}/manual-capture/history`
  - `GET /api/v1/runtime/sources`
  - `POST /api/v1/runtime/sources`
  - `POST /api/v1/runtime/sources/{source_id}:validate`
  - `GET /api/v1/runtime/pipeline-definitions`
  - `POST /api/v1/runtime/pipeline-definitions`
  - `POST /api/v1/runtime/pipeline-definitions/{pipeline_definition_id}/versions`
  - `POST /api/v1/runtime/pipeline-versions/{pipeline_version_id}:validate`
  - `POST /api/v1/runtime/pipeline-versions/{pipeline_version_id}:publish`
  - `GET /api/v1/runtime/node-types`
  - `GET /api/v1/runtime/model-capabilities`
  - `GET /api/v1/runtime/trigger-endpoints`
  - `GET /api/v1/runtime/trigger-endpoints/{trigger_endpoint_id}`
  - `GET /api/v1/runtime/capture-rules`
  - `POST /api/v1/runtime/capture-rules`
  - `GET /api/v1/runtime/output-connectors`
  - `POST /api/v1/runtime/output-connectors`
  - `GET /api/v1/runtime/output-connectors/types`
  - `GET /api/v1/runtime/output-connectors/{output_connector_id}`
  - `PUT /api/v1/runtime/output-connectors/{output_connector_id}`
  - `GET /api/v1/runtime/channels/{channel_id}/output-connectors`
  - `GET /api/v1/runtime/channels/{channel_id}/connector-deliveries`
  - `GET /api/v1/runtime/executions`

Runtime channel `source_type` values include `rtsp`, `aravis_gige`,
`flir_blackfly`, `snapshot`, `trigger_capture`, and `file_test`.
`aravis_gige` remains a legacy Aravis/GStreamer bridge type and is rejected by
the server `camera-media` path because it adds a CPU encoder. `flir_blackfly` represents
Teledyne FLIR Blackfly/Blackfly S GigE Vision cameras acquired through the
Spinnaker GenTL producer and the realtime executor's GenTL adapter.
`GET /api/v1/runtime/channels/{channel_id}/streams` returns runtime-owned stream
schema-v2 metadata by channel identity: independent desired/actual state,
camera/encoder/publication/browser-reader components, negotiated profile, and
exactly one `raw` WHEP/WebRTC endpoint. It does not mint internal stream ids for
operators and does not cause pipeline, inference, overlay rendering, or encoding
work to run per client. No alternate browser video protocol is exposed.
- Result service:
  - `GET /api/v1/runtime/results`
  - `GET /api/v1/runtime/results/{result_record_id}`
  - `GET /api/v1/runtime/channels/{channel_id}/results/latest`
- Trigger executor:
  - `PUT /api/v1/runtime/triggers/{endpoint_slug}`
- Overlay service:
  - `GET /api/v1/runtime/channels/{channel_id}/overlay/latest`

Canonical OpenAPI exports remain under `contracts/openapi/`.

Realtime live view and overlay latest-state APIs return
`RuntimeLiveViewResponse`; the payload is bound to the active
`runtime_execution_id`, not only to a channel identifier.

Manual live capture is documented in
[../runtime/manual-live-capture.md](../runtime/manual-live-capture.md). It is a
runtime-admin API feature and remains separate from pipeline capture rules.

Runtime output connectors are documented in
[../runtime/output-connector-architecture.md](../runtime/output-connector-architecture.md).
Connector configuration is a runtime-admin API workflow; delivery execution is
owned by `connector-service`.

Trigger endpoint integration for PLC/control-system clients is derived from
published runtime pipelines containing HTTP Trigger Input, Channel frame input,
and Response Output nodes, applied to a channel. It is documented in
[../runtime/trigger-runtime-plc-contract.md](../runtime/trigger-runtime-plc-contract.md).
