# Manual Capture Guide

Manual capture is the operator-driven full-frame capture path in channel live
view. It is intentionally simple: choose a Label Studio project, capture once,
or start/stop a backend-owned interval.

## Operator Workflow

1. Open a channel detail page.
2. In **Manual Live Capture**, choose a Label Studio project.
3. Click **Capture once**, or set an interval in seconds and click **Start**.
4. Watch **Capture Delivery** for queued, delivered, retrying, rate-limited,
   failed, or duplicate status.
5. Click **Stop** to end an interval session.

Operators never type internal IDs. The project selector is populated from the
existing platform Label Studio project mapping.

## Execution Model

- `runtime-admin-api` only accepts the operator command and writes durable
  capture work.
- `capture-service` owns frame resolution, interval ticks, materialization,
  Label Studio submission, retries, rate limiting, and duplicate handling.
- Manual capture records remain separate from pipeline capture records, but both
  delivery paths use `runtime_capture_requests` for durable status.
- The browser polls status and history; it does not own the timer.

## Frame Binding

Capture-service resolves manual frames in this order:

1. current non-stale live-view frame for the active runtime instance
2. latest persisted runtime result frame
3. configured file-test source frame for development

If none is available, the request fails visibly and no realtime inference path is
blocked.

## Routes

- `GET /api/v1/runtime/channels/{channel_id}/manual-capture/targets`
- `POST /api/v1/runtime/channels/{channel_id}/manual-capture/single`
- `POST /api/v1/runtime/channels/{channel_id}/manual-capture/interval-session`
- `POST /api/v1/runtime/channels/{channel_id}/manual-capture/interval-session:stop`
- `GET /api/v1/runtime/channels/{channel_id}/manual-capture/session`
- `GET /api/v1/runtime/channels/{channel_id}/manual-capture/history`
- `GET /api/v1/runtime/channels/{channel_id}/captures/history`

`Capture once` returns a queued manual record. Final Label Studio delivery is
shown in capture history once `capture-service` processes the durable request.
