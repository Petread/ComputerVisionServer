# Endpoint Exposure Map

This map is the concrete exposure policy for the reverse proxy and the internal service network.

## Externally Exposed

| Path | Backend | Auth Model | Notes |
| --- | --- | --- | --- |
| `/` | `admin-web` | Session cookie for authenticated screens | Single operator UI entry point |
| `/api/*` | `admin-api` | Session cookie + CSRF for browser mutations | Aggregated control-plane workflow API |
| `/health/*` | `admin-api` | Internal health visibility through proxy | Keeps health checks behind the same entry point |
| `/inference-rt/*` | `inference-gateway-rt` | Open when `api_keys.enabled=false`; otherwise public routes require scoped API keys and internal operator routes may use browser session auth | Keeps external app auth configurable while preserving browser-based operator testing |
| `/inference-batch/*` | `inference-gateway-batch` | Open when `api_keys.enabled=false`; otherwise public routes require scoped API keys and internal routes may use browser session auth or internal service keys | Separates external clients from operator and worker-only batch flows |
| `/runtime-api/*` | `runtime-admin-api` | Session cookie + CSRF for browser mutations | Runtime channel, pipeline, derived trigger endpoint metadata, manual capture, connector, live-view, and metrics administration |
| `WebRTC/WHEP :8889` | `mediamtx` on Windows in offline installs | Stream auth disabled by default: LAN-open `unauthenticated_lan`; explicit stream credentials/tokens when enabled | Browser-compatible low-latency viewing of the same runtime-published streams |
| `TURN :3478 TCP` | `coturn` in WSL | Ephemeral HMAC credentials are issued by MediaMTX; exposed by the installer firewall rules | The single browser media transport; TURN relays locally to MediaMTX ICE |
| `WebRTC ICE :8189 UDP/TCP` | `mediamtx` on Windows in offline installs | Used by the local TURN relay and for diagnostics | Not selected as an alternative player protocol |
| `/stream-webrtc/*` | `caddy -> Windows mediamtx:8889` | Same LAN-open stream policy as MediaMTX; keep behind trusted LAN or explicit stream auth | HTTPS browser WHEP entrypoint for MediaMTX WebRTC so the admin UI uses a native video client without insecure `http://:8889` frames |
| `/runtime-trigger/*` | `trigger-executor` | Open by default while `auth_required=false`; scoped machine API key when the HTTP Trigger Input node enables auth | PLC/control-system PUT path derived from published pipelines with HTTP Trigger Input; authentication can be enabled per pipeline before production exposure |
| `/runtime-overlay/*` | `overlay-service` | Internal/session-routed reads | Latest runtime overlay state for browser live view and support tooling |
| `/runtime-results/*` | `result-service` | Session-routed reads | Runtime result browsing and detail retrieval |
| `/label-studio/*` | `label-studio` | Label Studio local auth | Exposed for annotation UI under the shared origin |
| `/mlflow/*` | `mlflow` | Internal LAN access policy | Exposed for experiment inspection, not for deployment control |

## Explicitly Blocked At Reverse Proxy

| Path Prefix | Reason |
| --- | --- |
| `/datasets-service*` | Internal service path not required for browser workflows |
| `/training-orchestrator*` | Internal service path not required for browser workflows |
| `/model-registry*` | Internal service path not required for browser workflows |
| `/deployment-controller*` | Internal service path not required for browser workflows |
| `/webhook-receiver*` | Webhook ingress should stay private to Label Studio and the control network |
| `/runtime-admin-api*` | Runtime admin is exposed only through `/runtime-api/*` |
| `/runtime-orchestrator*` | Runtime lifecycle worker/control path stays internal |
| `/realtime-executor*` | Realtime executor handoff stays internal |
| `/trigger-executor*` | Trigger executor is exposed only through `/runtime-trigger/*` |
| `/overlay-service*` | Overlay service is exposed only through `/runtime-overlay/*` |
| `/capture-service*` | Capture drain/delivery path stays internal |
| `/result-service*` | Result service is exposed only through `/runtime-results/*` |
| `/connector-service*` | Connector drain/delivery path stays internal |
| `/inference-batch/api/v1/batch/internal/*` | Internal chunk execution path must remain service-to-service only |

## Internal Only

| Surface | Access Policy |
| --- | --- |
| PostgreSQL | Control-network only; no browser exposure |
| Redis | Control-network only; no browser exposure |
| Triton HTTP and gRPC ports | Data-network only; gateways talk to Triton directly |
| Worker processes | No direct external exposure |
| Label Studio webhook ingress network | Shared-secret validation and optional CIDR restriction |
| Batch chunk execution API | Internal service token with `internal.batch.execute` scope and `batch-worker` identity |
| Capture drain API | Internal service token with `runtime.capture.drain` scope |
| Connector drain API | Internal service token with `runtime.connector.drain` scope |
| MediaMTX API and metrics (`:9997`, `:9998`) | Localhost/internal network only; never browser-exposed |

## Browser Routing Rule

- The browser talks to the reverse proxy.
- The admin UI talks to `admin-api` for control-plane workflows.
- The admin UI talks to `runtime-admin-api` through `/runtime-api/*` for
  runtime-plane configuration and operator runtime workflows.
- Authenticated trigger tests and external PLC clients talk to
  `trigger-executor` through `/runtime-trigger/*`; production inference still
  uses the inference gateways.
- The browser never needs direct access to internal control-plane microservice URLs.
- The browser may consume MediaMTX WebRTC stream URLs, but it does not generate
  livestream frames. Runtime stream metadata is fetched from `/runtime-api/*`;
  production stream bytes are fanned out by MediaMTX.
- Realtime and batch inference stay on separate path prefixes and separate backend services.
- External applications use the public inference contract. Machine API keys are required only when `api_keys.enabled=true`; browser session cookies are not a substitute for external application authentication in that mode.
