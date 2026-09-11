# OpenAPI Contracts

The canonical OpenAPI documents are generated from the FastAPI service definitions in `backend/services/`.

Generate or refresh them with:

```powershell
uv run python scripts/dev/export_openapi.py
```

Expected outputs:

- `admin-api.v1.yaml`
- `dataset-service.v1.yaml`
- `training-orchestrator.v1.yaml`
- `model-registry-service.v1.yaml`
- `deployment-controller.v1.yaml`
- `webhook-receiver.v1.yaml`
- `inference-realtime.v1.yaml`
- `inference-batch.v1.yaml`
- `runtime-admin-api.v1.yaml`
- `channel-service.v1.yaml`
- `pipeline-definition-service.v1.yaml`
- `runtime-orchestrator.v1.yaml`
- `realtime-executor.v1.yaml`
- `trigger-executor.v1.yaml`
- `overlay-service.v1.yaml`
- `capture-service.v1.yaml`
- `result-service.v1.yaml`
- `connector-service.v1.yaml`
- `edge-agent.v1.yaml`
- `edge-plant.v1.yaml`
