# Internal LAN Security Model

This document records the production-minded security decisions implemented for the internal LAN deployment defined in [the platform specification](./cv-platform-spec.md). It preserves the platform's service boundaries and does not assume direct internet exposure.

## Scope

- The deployment is internal-only and browser-first.
- Control, training, realtime inference, and batch inference remain separate planes.
- Reverse proxy exposure is intentionally narrower than the internal service topology.
- Security controls are designed to prevent the platform from becoming effectively unauthenticated while avoiding internet-grade complexity that is unnecessary for a workstation deployment.

## Authentication Decision

### Admin UI and Control Plane

- Browser operators authenticate with username and password.
- Passwords are verified with Argon2id hashes and are never stored in plaintext.
- Users are persisted in PostgreSQL and managed through the admin UI and `admin-api`.
- The bootstrap admin credential remains available only for initial bring-up and recovery; if the bootstrap user already exists in PostgreSQL, the database record remains authoritative for normal operation.
- Successful login issues:
  - an HTTP-only signed session cookie
  - a readable CSRF cookie used with a double-submit CSRF header
- Session cookies carry a `session_version`; password changes and user deactivation invalidate older browser sessions on the next request.
- Unsafe browser requests require:
  - valid session cookie
  - matching CSRF cookie and `X-CSRF-Token` header
  - an active user record in PostgreSQL

### Inference Clients

- The gateway-level `api_keys.enabled` setting controls whether inference routes enforce machine credentials. The current base/local configuration may run open with `api_keys.enabled=false`; production-oriented deployments should keep it enabled.
- Realtime and batch inference support scoped machine credentials using:
  - `Authorization: Bearer <key_id>.<secret>`
  - legacy compatibility: `X-API-Key: <key_id>.<secret>`
- Managed API keys are created in the UI and stored in PostgreSQL with:
  - public `key_id`
  - internal DB record id
  - hashed secret only
  - key metadata and secret prefix for masked display
  - `last_used_at`, `expires_at`, and `revoked_at`
- The full generated secret is shown exactly once at creation time and is never returned again.
- Supported scopes are intentionally separated:
  - `realtime.predict`
  - `batch.submit`
  - `internal.batch.execute`
- Internal chunk-execution endpoints additionally require a service credential bound to the `batch-worker` service identity.
- Public inference endpoints are machine-authenticated only. Browser sessions remain valid for operator-only internal inference routes used by the admin UI.
- Gateway validation uses direct PostgreSQL lookups with a short bounded in-process cache. Revocation therefore takes effect immediately or within the small cache window.

### Service-to-Service Calls

- Internal service authentication still supports mounted secret files that seed bootstrap API key records on startup.
- Bootstrap keys and UI-managed keys coexist during migration. Bootstrap keys are synchronized into PostgreSQL so the gateway can validate both sources with one runtime path.
- Credentials are scoped per subject and per capability instead of sharing one global internal secret.
- Bootstrap keys are visible in the admin UI as read-only `bootstrap` credentials and must be changed or removed through configuration and mounted secret files.
- Label Studio webhook ingress validates a shared secret header and can also restrict requests to approved source CIDRs.

## Authorization Model

- The platform does not use RBAC for administration.
- Any authenticated active user can:
  - create users
  - change passwords
  - deactivate or reactivate users
  - create and revoke managed API keys
  - inspect user and API key metadata
- Route handlers still record permission names in request state for audit context, but enforcement is authenticated-user-only rather than role-based.
- The legacy `role` field remains as a compatibility attribute for older session and UI contracts; it is no longer used as the authority for administration decisions.

## Secret And Config Separation

- Runtime configuration stays in versioned YAML under `config/`.
- Secrets are mounted as files under `/run/secrets` and loaded at runtime.
- Production startup validation fails fast when required secret files are missing.
- Required mounted secrets are:
  - database password
  - session signing secret
  - bootstrap admin password hash
  - Label Studio API token
  - Label Studio webhook secret when webhook auth is enabled
  - per-client API key secret files for configured machine credentials
  - TLS certificate and key
- No application code or checked-in config contains hardcoded production secrets.

## Network Exposure

- Browser-facing entry stays at the reverse proxy.
- Direct exposure is limited to:
  - `https://<host>/`
  - `https://<host>/api/*`
  - `https://<host>/health/*`
  - `https://<host>/inference-rt/*`
  - `https://<host>/inference-batch/*` when external batch submission is needed
  - `https://<host>/label-studio/*`
  - `https://<host>/mlflow/*`
- The reverse proxy explicitly blocks:
  - internal control-plane service path prefixes
  - internal batch execution endpoints
- PostgreSQL, Redis, Triton ports, and worker processes remain internal-only.

## Request And Upload Hardening

- Global request middleware enforces:
  - per-route request timeouts
  - request body size limits
  - stricter limits for inference and upload paths
- Upload sessions enforce:
  - explicit filename header
  - file-count and total-size limits
  - content sniffing for supported image types
  - MIME and extension allow-lists
  - filename sanitization
  - atomic file assembly
  - deterministic content-addressed asset placement
- Batch input manifests are constrained to the configured batch root and maximum manifest size.

## CORS And Browser Session Policy

- CORS allow-lists are explicit and environment-specific.
- Credentialed cross-origin requests are permitted only for configured admin UI origins.
- Browser sessions use:
  - signed cookies
  - `SameSite` policy
  - HTTP-only session cookie
  - double-submit CSRF protection for unsafe methods

## Audit Coverage

- Audit records are written for privileged actions, including:
  - login success and failure
  - user creation, deactivation, reactivation, and password changes
  - API key creation, bootstrap visibility, revocation, and failed machine-auth attempts
  - logout
  - dataset upload session creation, part receipt, and completion
  - snapshot requests
  - training create, cancel, and resume
  - model approval, rejection, archive, and rollback-target marking
  - deployment package build, stage, promote, smoke test, and rollback requests
  - batch submissions
- Audit events remain append-only in PostgreSQL and are mirrored to filesystem export.

## Safe Deployment Defaults

- Deployment artifacts are immutable, versioned serving packages.
- Triton repositories are updated through staged release directories, explicit model control, and smoke-test gating.
- Production pointer changes happen only after compatibility and smoke checks pass.
- Failed activation leaves the previous production release active.
- Rollback switches back to the previous immutable release without rebuilding artifacts.
