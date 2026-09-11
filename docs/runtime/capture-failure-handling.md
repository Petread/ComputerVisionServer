# Capture Failure Handling

Capture delivery is durable and at-least-once. Realtime and trigger execution
persist capture requests and continue; Label Studio delivery happens in
`capture-service`.

## Statuses

- `queued`: waiting for capture-service
- `processing`: claimed by capture-service
- `rate_limited`: delayed by per-rule/project rate limits
- `delivered`: accepted by Label Studio
- `failed_retryable`: transient failure; retry is scheduled
- `failed_terminal`: configuration or payload failure that needs operator action
- `skipped_duplicate`: duplicate image/ROI/project delivery suppressed

## Project Availability

Projects must exist in the platform Label Studio mapping and be in
`discovered`, `mapped`, or `active` state. Missing, deleted, or unavailable
projects fail terminally and do not call Label Studio.

## Retry Policy

Label Studio API errors are retryable up to the request `max_attempts`.
Unsupported images, missing frames, oversized images, and invalid local-file
storage configuration fail terminally. Retry delays use increasing intervals so
Label Studio outages do not create tight loops.

If a capture-service instance exits after claiming work, stale `processing`
claims are treated as abandoned, counted against the retry budget, and made
claimable again. This keeps delivery rows from staying in `processing`
indefinitely after a worker restart or unexpected client failure.

## Rate Limiting And Backpressure

Capture-service enforces a per-rule/project rate limit. Manual interval sessions
allow one in-flight request per session, so a slow Label Studio project cannot
build an unbounded interval backlog.

## Duplicate Handling

Pipeline captures suppress duplicate image/ROI/project deliveries by digest.
Manual captures keep duplicates because the operator may intentionally collect
similar live samples. Duplicate pipeline rows remain visible as
`skipped_duplicate` for auditability.

## Audit And History

Operator manual capture commands write runtime audit events. Delivery state is
stored in `runtime_capture_requests`; manual UX history is stored in
`runtime_manual_capture_requests` and linked back to the durable delivery row.
