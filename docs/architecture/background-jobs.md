# Background Job Flow

## Request Path to Worker Path

1. A browser action or service callback hits an HTTP API.
2. The API validates input with typed contracts.
3. Durable state is written to PostgreSQL when the workflow requires state ownership.
4. An RQ job is enqueued onto an isolated queue.
5. The corresponding worker picks up the job and performs the long-running step.
6. Health, audit, and later state transitions are observable without holding the original HTTP request open.

## Queue Ownership

- `webhook`: Label Studio webhook follow-up processing
- `dataset`: upload finalization, snapshot materialization, and DVC work
- `training`: resumable training and later export/evaluation stages
- `deploy`: staging, promotion, rollback, and retirement jobs
- `batch`: chunk-based batch inference execution
- `maintenance`: reconciliation, retention, health collection, and backups

