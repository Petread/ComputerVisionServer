# Persistence Model

## Operational Source of Truth

- `PostgreSQL`: platform business truth, state machines, audit records, heartbeats, job rows
- `Redis`: queueing backbone and transient worker coordination
- `Filesystem`: artifact roots, serving packages, audit exports, and logs
- `MLflow`: experiment lineage and model metadata mirror
- `DVC`: dataset and artifact version references
- `Label Studio`: active project/task/annotation UI state

## Implemented Schema Coverage

The Alembic migration set covers:

- users and API keys
- datasets, upload sessions, dataset assets, label projects, and snapshots
- webhook events
- training jobs and checkpoints
- model families, versions, promotions, and deployment releases
- batch jobs and chunks
- audit events, idempotency keys, and service heartbeats

## Access Management Schema

- `users` stores human browser accounts. The canonical login identifier is `username`, which is unique and operator-managed through the admin UI.
- `users` includes `display_name`, optional `description`, `password_hash`, compatibility `role`, `is_active`, `created_at`, `updated_at`, `created_by`, `last_login_at`, `password_changed_at`, and `session_version`.
- `session_version` is incremented on password change and user deactivation so older browser sessions become invalid on the next authenticated request.
- `api_keys` stores machine credentials for inference access. The table keeps two identifiers:
  - `id`: the public `key_id` used in `Authorization: Bearer <key_id>.<secret>`
  - `record_id`: the internal admin/UI identifier used for list and detail operations
- `api_keys` also stores `label`, `description`, `intended_usage`, `subject_type`, `subject_id`, `endpoint_mode_hint`, `source_type`, `created_by`, `last_used_at`, `expires_at`, `revoked_at`, and `revoked_by`.
- `hashed_secret` stores only the derived secret verifier. Plaintext API key secrets are never persisted.
- `secret_prefix` stores a short non-sensitive prefix so the UI can help operators distinguish keys after the one-time reveal step.
- Bootstrap/config-managed keys and UI-managed keys share the same table and are differentiated by `source_type` set to `bootstrap` or `managed`.
- Scopes are stored as an extensible space-delimited string in `api_keys.scope` and parsed in application code. This keeps future scopes additive without forcing a schema redesign or a key-format change.

## Index And Lookup Strategy

- `users.username` is unique and used for login resolution.
- `api_keys.id` is the primary lookup key for gateway authentication.
- `api_keys.record_id` has a unique index for stable UI/admin lookup.
- `api_keys.source_type`, `api_keys.last_used_at`, and `api_keys.revoked_at` are indexed for administration, usage inspection, and revocation visibility.
- Revocation and expiration enforcement is a direct database-backed check with a bounded in-process cache in the gateway runtime. The cache TTL is intentionally short at approximately two seconds for single-node on-prem safety.
- `last_used_at` updates are write-throttled to approximately once per minute per key to avoid high write amplification on hot credentials.
- Expiration handling is row-local: authentication resolves the key by `key_id`, then rejects if `expires_at <= now`.
- Revocation handling is row-local: authentication resolves the key by `key_id`, then rejects if `revoked_at` is set. Because the cache is bounded, revocation becomes effective immediately or within the documented short cache window.
