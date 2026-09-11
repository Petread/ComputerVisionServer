# Mounted Secrets Layout

The stack expects mounted files under `/run/secrets` for at least:

- `postgres_app_password`
- `session_secret`
- `bootstrap_admin_password_hash`
- `labelstudio_api_token`
- `labelstudio_webhook_secret` when webhook authentication is enabled
- one secret file per configured bootstrap API key
- `tls_cert.pem`
- `tls_key.pem`

Recommended local development approach:

- create `infra/secrets/dev/`
- bind mount it into containers as `/run/secrets:ro`
- never commit the secret files themselves
- keep the secret filenames stable so `config/base/platform.yaml` and `config/prod/platform.yaml` do not need code changes for routine rotation

For the repository-local compose profile, create untracked files directly under
`infra/secrets/` or adjust the compose mount to your local secret directory.
The filenames are part of the config contract; the values are not.

For Label Studio, platform administrators can also update the
`labelstudio_api_token` from the admin UI under `Settings`. The UI tests the
token against Label Studio before writing it, and it never displays the saved
token value again.
