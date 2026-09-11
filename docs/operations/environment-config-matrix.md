# Environment And Config Matrix

The platform uses typed Pydantic settings resolved from code defaults, `config/base/platform.yaml`, environment overlays, mounted secret files, and environment variables.

## Frontend Runtime Variables

| Variable | Dev Example | Prod Example | Purpose |
| --- | --- | --- | --- |
| `VITE_ADMIN_API_URL` | `http://localhost` | `https://127.0.0.1:8443` | Control-plane API base URL for operator workflows |
| `VITE_RUNTIME_ADMIN_API_URL` | `http://localhost/runtime-api` | `https://127.0.0.1:8443/runtime-api` | Runtime-admin API for channel, pipeline, capture, connector, and live-view workflows |
| `VITE_RESULT_SERVICE_URL` | `http://localhost/runtime-results` | `https://127.0.0.1:8443/runtime-results` | Runtime result-service URL for historical result browsing |
| `VITE_TRIGGER_EXECUTOR_URL` | `http://localhost/runtime-trigger` | `https://127.0.0.1:8443/runtime-trigger` | Trigger executor URL for authenticated operator test PUTs |
| `VITE_INFERENCE_GATEWAY_RT_URL` | `http://localhost/inference-rt` | `https://127.0.0.1:8443/inference-rt` | Realtime gateway base URL for test console and status |
| `VITE_INFERENCE_GATEWAY_BATCH_URL` | `http://localhost/inference-batch` | `https://127.0.0.1:8443/inference-batch` | Batch gateway base URL for capability and status views |
| `VITE_ADMIN_WS_URL` | `ws://localhost` | `wss://127.0.0.1:8443` | Job/status feed websocket origin |

## Backend Configuration Domains

| Domain | Dev Profile | Prod Profile | Notes |
| --- | --- | --- | --- |
| `database` | local/dev PostgreSQL connection | mounted secret-backed production PostgreSQL connection | control-plane source of truth |
| `redis` | local/dev Redis | production Redis | queue and transient coordination only |
| `storage` | repo-local `.local/state/...` | `/srv/cv-platform/...` | must stay deterministic and typed |
| `label_studio` | dev URL and token file | production URL and token file | operators still use the UI path, not CLI |
| `mlflow` | local/dev MLflow URL | production MLflow URL | hidden behind internal services for normal operators |
| `dvc` | local/dev repo and remote path | production repo and remote path | hidden behind internal services for normal operators |
| `training` | reduced concurrency and developer-friendly timings | production-safe queue/retry/heartbeat settings | worker ownership remains unchanged |
| `triton.realtime` | dev RT endpoint | prod RT endpoint | separate from batch |
| `triton.batch` | dev batch endpoint | prod batch endpoint | separate from realtime |
| `inference_limits` | lower scale acceptable | production limits and headroom | direct effect on gateways |
| `session` | dev secret allowed | mounted secret required | secure cookies enforced in prod |
| `api_keys` | disabled by default for open local inference, bootstrap keys optional when re-enabled | scoped production keys enabled | controls whether inference clients must authenticate |
| `tls` | optional | required | reverse proxy and public base URL must match |
| `observability` | lower thresholds acceptable | production thresholds required | drives UI health and alerting |

## Runtime Plane Configuration

| Setting | Dev Default | Prod Default | Purpose |
| --- | --- | --- | --- |
| `runtime.realtime_frame_buffer_size` | `2` | `2` | bounded live frame buffer; old frames drop before backlog grows |
| `runtime.realtime_drop_policy` | `drop_oldest` | `drop_oldest` | low-latency realtime behavior |
| `runtime.realtime_target_latency_ms` | `350` | `250` | operator-visible SLO target and health signal |
| `runtime.realtime_max_channels_per_executor` | `4` | `8` | executor admission guardrail |
| `runtime.trigger_timeout_ms` | `3000` | `2500` | default deterministic PLC trigger timeout |
| `runtime.trigger_max_concurrency` | `2` | `4` | trigger executor admission limit |
| `runtime.executor_heartbeat_interval_seconds` | `2` | `3` | executor heartbeat cadence |
| `runtime.executor_stale_after_seconds` | `8` | `15` | stale runtime detection threshold |
| `runtime.model_capability_cache_ttl_seconds` | `10` | `10` | short-lived projection from deployment releases to runtime model options |
| `runtime.capture_queue_name` | `runtime-capture` | `runtime-capture` | durable capture delivery queue name |
| `runtime.connector_queue_name` | `runtime-connector` | `runtime-connector` | durable output connector delivery queue name |
| `runtime.capture_max_image_bytes` | `5242880` | `5242880` | max captured full-frame or ROI image size |
| `runtime.capture_default_rate_per_minute` | `60` | `120` | capture-service default rate control |
| `runtime.label_studio_submission_mode` | `local_file_url` | `local_file_url` | Label Studio delivery abstraction |
| `runtime.inference_gateway_realtime_url` | `http://inference-gateway-rt:8087` | `http://inference-gateway-rt:8087` | runtime inference-node gateway path |
| `runtime.inference_gateway_bearer_token_file` | `null` | mounted file when required | optional service credential for runtime-to-gateway calls |

## Runtime Service Routes

| Route Prefix | Service | Normal Caller |
| --- | --- | --- |
| `/runtime-api/api/v1/runtime/*` | `runtime-admin-api` | admin UI browser session |
| `/runtime-trigger/api/v1/runtime/triggers/{slug}` | `trigger-executor` | PLC/control system with `runtime.trigger.invoke`; admin UI test uses a pasted machine token |
| `/runtime-overlay/api/v1/runtime/channels/{channel_id}/overlay/latest` | `overlay-service` | live-view/support reads |
| `/runtime-results/api/v1/runtime/results*` | `result-service` | result history UI |
| internal `/api/v1/runtime/captures:drain` | `capture-service` | service identity with `runtime.capture.drain` |
| internal `/api/v1/runtime/connector-deliveries:drain` | `connector-service` | service identity with `runtime.connector.drain` |

## Deterministic Storage Roots

| Root | Purpose |
| --- | --- |
| `storage.state_root` | umbrella root |
| `storage.control_root()` | control-plane state and uploads |
| `storage.snapshots_root()` | immutable dataset snapshots |
| `storage.training_root` | training workspaces, checkpoints, exports |
| `storage.serving_packages_root()` | immutable deployment packages |
| `storage.triton_repository_root("realtime")` | realtime Triton repository |
| `storage.triton_repository_root("batch")` | batch Triton repository |
| `storage.batch_root()` | batch manifests, chunk outputs, bundles |
| `storage.audit_root` | JSONL audit exports |
| `storage.logs_root` | operational logs |
| `storage.backups_root()` | metadata backup outputs |
| `storage.quarantine_root()` | failed or partial artifacts isolated from active paths |

## Required Production Secret Files

- `postgres_app_password`
- `session_secret`
- `bootstrap_admin_password_hash`
- `labelstudio_api_token`
- `labelstudio_webhook_secret` when webhook auth is enabled
- bootstrap API key secrets for all configured machine identities when `api_keys.enabled` is true
- `tls_cert.pem`
- `tls_key.pem`

The repository tracks only [../../infra/secrets/README.md](../../infra/secrets/README.md).
Secret values are local mounted files and must not be committed.

Reference configuration files:

- [../../config/base/platform.yaml](../../config/base/platform.yaml)
- [../../config/dev/platform.yaml](../../config/dev/platform.yaml)
- [../../config/prod/platform.yaml](../../config/prod/platform.yaml)
- [../../config/schemas/platform-settings.schema.json](../../config/schemas/platform-settings.schema.json)
