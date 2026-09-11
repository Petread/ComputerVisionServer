# Access Management Guide

This guide covers the production access-management workflow for human users and machine API keys. It is the supported operator path for normal day-to-day administration and replaces config-file-only key creation as the default operating mode.

## 1. Identity Separation

- Human users authenticate to the admin UI with username and password.
- Machine API keys authenticate external applications against the inference gateways when `api_keys.enabled=true`; the base/local configuration can run those gateways open while the project is not scaled.
- Browser session cookies are not used as machine credentials.
- API key secrets are shown once at creation time and are never displayed again.

## 2. Managing Users

1. Open `Operations -> Access & Security`.
2. Select the `Users` panel.
3. Create a user with:
   - unique `username`
   - display name
   - optional description or notes
   - initial password
4. Open the user detail panel to review status, creator, timestamps, and recent audit events.
5. Use `Change Password` to reset another user's password.
6. Use `Change My Password` for self-service password changes.
7. Use `Deactivate` or `Reactivate` to control login eligibility.

Operational rules:

- `username` is the canonical login identifier and must be unique.
- Deactivated users cannot log in.
- Password changes increment the session version, so older browser sessions become invalid on the next request.
- User creation, password changes, deactivation, and reactivation are all audited.

## 3. Managing API Keys

This workflow is required only for gateways running with `api_keys.enabled=true`.

1. Open `Operations -> Access & Security`.
2. Select the `API Keys` panel.
3. Choose `Create API Key`.
4. Enter:
   - label
   - description
   - intended usage
   - subject or application name
   - allowed scopes
   - optional expiration timestamp
   - optional endpoint mode hint
5. Confirm the one-time reveal screen and copy the full token into the target application or secret manager immediately.
6. Use the key detail view later to inspect masked metadata, scopes, source type, timestamps, and recent usage.
7. Use `Revoke` when the credential should stop working.

Operational rules:

- Labels are informational and may be duplicated. Use the masked token, subject, and timestamps for unambiguous identification.
- The canonical token format is `Authorization: Bearer <key_id>.<secret>`.
- Managed key secrets are generated server-side with secure randomness and stored only as hashes.
- Revocation stops access immediately or within the short gateway cache window.
- Expired keys fail authentication automatically.

## 4. Managed Keys Vs Bootstrap Keys

- `managed`
  - created and revoked in the UI
  - stored in PostgreSQL as the normal operating mode
  - intended for external applications and ongoing administration
- `bootstrap`
  - seeded from configuration plus mounted secret files
  - visible in the UI for migration awareness
  - read-only in the UI
  - changed or removed through configuration and secret-file management

Use managed keys for all normal application onboarding. Keep bootstrap keys only for initial bring-up, recovery, or staged migration.

## 5. PLC Write Access

`Operations -> Access & Security` also contains the PLC write access block for
runtime pipelines that use `Write to Siemens SPS`.

The block shows:

- the active runtime write gate loaded by the currently running services
- the persisted deployment env value from
  `CV_PLATFORM__RUNTIME__PLC_WRITES_ALLOWED`
- whether `python-snap7` is available
- whether a service restart is required
- every Siemens SPS target discovered from latest or published runtime pipeline
  graphs, including host, rack, slot, mapping count, sample DB addresses, and
  whether each target is blocked, dry-run, disabled, dependency-missing, or
  ready
- the configured connection reuse and reconnect policy for each target,
  including whether runtime reuse is bounded to one persistent connection slot
  for the PLC endpoint or whether connections are opened per execution

Changing `Allow PLC writes in deployment env` writes only the deployment safety
gate. It does not connect to a PLC and does not perform a test write. Running
runtime services must reload the env before the active runtime gate changes;
the page shows `restart required` while the persisted env differs from the
active setting.

Operational rules:

- Platform administrators can change the global PLC write gate.
- Operators can inspect configured PLC targets and status.
- Real writes still require the pipeline node to be enabled, `dry_run=false`,
  enabled mapping rows, valid conversion, valid DB address configuration, and
  successful runtime execution.
- The page is not a substitute for PLC-side commissioning or address
  validation.

## 6. External Application Example

Example realtime request with a UI-created key:

```bash
curl -X POST "https://127.0.0.1:8443/inference-rt/api/v1/infer/classify" \
  -H "Authorization: Bearer key_7f8c9d0e1a2b3c4d5e6f7890.S3cureTokenFromOneTimeReveal" \
  -H "X-Request-ID: 6b9bfc9f-2399-4269-b31d-3e0bbcfb2d6a" \
  -F "model_id=quality-classifier" \
  -F "image=@photo.png;type=image/png"
```

Use:

- `realtime.predict` for realtime routes
- `batch.submit` for batch submission and retrieval routes

## 7. Migration From Config-Only Keys

1. Leave existing bootstrap keys in place during transition.
2. Create a managed replacement key in the UI for each external application.
3. Update the client application to use the managed key.
4. Confirm successful requests and `last_used_at` activity for the managed key.
5. Revoke the old managed key in the UI, or remove the bootstrap key from configuration and mounted secret files once the client has cut over.
6. Repeat until bootstrap keys are no longer needed for routine operations.

The bootstrap admin account remains available for initial bring-up and recovery, but once the bootstrap user exists in PostgreSQL, normal password and session lifecycle is database-managed.
