# Runtime Operations Runbook

## Scope
This runbook covers W7 runtime-plane operations for the existing platform.
It keeps the control plane, runtime plane, and inference plane separated:
operators use the admin UI and documented APIs, while Prometheus scrapes
runtime metrics from runtime services.

## Health And Readiness
Every runtime service exposes the shared service probes:

| Service | Port | Liveness | Readiness | Generic metrics |
| --- | ---: | --- | --- | --- |
| runtime-admin-api | 8090 | `/health/live` | `/health/ready` | `/metrics` |
| channel-service | 8091 | `/health/live` | `/health/ready` | `/metrics` |
| pipeline-definition-service | 8092 | `/health/live` | `/health/ready` | `/metrics` |
| runtime-orchestrator | 8093 | `/health/live` | `/health/ready` | `/metrics` |
| realtime-executor | 8094 | `/health/live` | `/health/ready` | `/metrics` |
| trigger-executor | 8095 | `/health/live` | `/health/ready` | `/metrics` |
| overlay-service | 8096 | `/health/live` | `/health/ready` | `/metrics` |
| capture-service | 8097 | `/health/live` | `/health/ready` | `/metrics` |
| result-service | 8098 | `/health/live` | `/health/ready` | `/metrics` |
| connector-service | 8099 | `/health/live` | `/health/ready` | `/metrics` |

Readiness means the service process is up and its declared dependencies are
ready enough to serve traffic. Liveness means the process should not be
restarted by the supervisor. Runtime-channel health is separate from service
health and is aggregated from persisted channel state, executor heartbeats, and
runtime result metrics.

## Runtime Metrics
Runtime operational aggregation is exposed by `runtime-admin-api`:

`GET /api/v1/runtime/metrics`

The endpoint is intended for the internal Prometheus scrape target
`runtime-plane`. It emits:

- channel counts by mode, observed state, and health state
- active, stale, and failed runtime instances
- realtime FPS, p95 latency, frame count, drop count, and drop rate
- trigger invocation count and latest trigger latency
- executor heartbeat age and recovery attempts

The admin system status API includes the same aggregation in the `runtime`
object and compact cards for runtime channels, runtime attention, FPS, and
trigger p95 latency.

## Recovery Matrix
| Observed state | Usual cause | Operator signal | Preferred action |
| --- | --- | --- | --- |
| starting | Runtime-control command accepted but not consumed yet | Channel badge says starting or start requested | Wait briefly, then check runtime-control worker if it remains stuck |
| running | Executor heartbeat and runtime metrics are current | Green badge, FPS or trigger metrics visible | No action |
| degraded | Source reconnects, high drop rate, stale heartbeat below fail threshold, or retryable executor issue | Warning badge and remediation hint on channel card/detail | Verify source connectivity and model route, then restart if the cause is corrected |
| failed | Terminal executor failure or stale heartbeat beyond fail threshold | Red badge, last error, and failed runtime count | Fix source/pipeline/model issue before restart |
| recovering | Reconciliation requested or orchestrator restart is binding persisted runtime state | Recovering badge and recovery hint | Wait for reconciliation, then restart if heartbeat does not refresh |
| stopped | Channel intentionally stopped or terminal runtime pointer cleaned | Stopped badge | Start only when live execution is required |

Idempotent behavior:
- Duplicate pending start, stop, restart, and reconcile commands for the same
  channel are coalesced while queued or processing.
- Idempotency keys still take precedence when supplied by clients.
- Cleanup only clears dangling or terminal active-runtime references. Failed
  evidence remains visible until the operator acts.

## Restart And Reconciliation
Use the channel detail page for normal runtime controls:

1. Check the compact status card, last error, heartbeat age, FPS, latency, and
   drop rate.
2. Follow the remediation hint. Correct source, network, model route, or
   pipeline issues before restarting.
3. Request Restart from the channel detail controls.
4. Confirm the channel returns to running and heartbeat age resets.
5. Confirm FPS, trigger latency, and drop rate are plausible for the source.

After a runtime-orchestrator restart, reconciliation should:
- recover running persisted runtime instances
- update assigned runtime worker identity
- clear terminal stale active-runtime references
- keep degraded and failed states visible to operators

## Performance Checklist
Before go-live or after significant change:

- Realtime channels show nonzero FPS when a live source is expected.
- Realtime p95 latency is within the configured production target.
- Drop rate is acceptable for the source and does not trend upward.
- Trigger p95 latency is within endpoint timeout.
- Trigger timeout count is zero during nominal PLC tests.
- Executor heartbeat age stays below the stale threshold.
- Prometheus has targets for runtime services and `runtime-plane`.
- Admin UI Runtime Plane panel remains compact and shows only actionable
  attention items by default.

## Failure Injection Plan
Run these drills in a controlled environment before production acceptance:

| Drill | Injection | Expected result |
| --- | --- | --- |
| Orchestrator restart | Restart `runtime-orchestrator` while a realtime channel is running | Reconcile binds the persisted runtime and heartbeat resumes |
| Executor stale | Stop or block a realtime executor heartbeat | Channel degrades, stale count increases, remediation hint is visible |
| Source loss | Disconnect RTSP or snapshot source | Drop/source errors rise, channel health degrades without hiding last error |
| Trigger timeout | Force trigger execution beyond endpoint timeout | Trigger result records timeout, trigger latency/timeout indicators update |
| Terminal failure | Raise an executor error | Channel becomes failed, last error and restart hint are visible |
| Terminal cleanup | Leave a stopped runtime execution as active pointer | Cleanup clears active pointer and marks stopped/degraded as appropriate |

## Go-Live Acceptance
The runtime plane is acceptable for W7 only when:

- runtime service liveness, readiness, and metrics endpoints respond
- `GET /api/v1/runtime/metrics` exposes meaningful runtime aggregation
- `GET /api/v1/system/status` includes runtime summary and runtime status cards
- stale, degraded, failed, recovering, running, and stopped states are visible in
  the operator UI with concise remediation hints
- restart and reconciliation behavior matches the recovery matrix
- Prometheus scrapes runtime service and runtime-plane metrics
- targeted backend and UI tests for recovery, stale cleanup, aggregation,
  metrics exposure, and compact status rendering pass
