#!/usr/bin/env bash
set -euo pipefail

BUNDLE_DIR=""
APP_VERSION=""
APP_NAME=""
APP_HOST_NAME="127.0.0.1"
MEDIAMTX_API_USER=""
MEDIAMTX_API_PASSWORD=""
TURN_AUTH_SECRET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)
      BUNDLE_DIR="$2"
      shift 2
      ;;
    --version)
      APP_VERSION="$2"
      shift 2
      ;;
    --app-name)
      APP_NAME="$2"
      shift 2
      ;;
    --host-name)
      APP_HOST_NAME="$2"
      shift 2
      ;;
    --mediamtx-api-user)
      MEDIAMTX_API_USER="$2"
      shift 2
      ;;
    --mediamtx-api-password)
      MEDIAMTX_API_PASSWORD="$2"
      shift 2
      ;;
    --turn-auth-secret)
      TURN_AUTH_SECRET="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$BUNDLE_DIR" || -z "$APP_VERSION" || -z "$APP_NAME" ]]; then
  echo "Usage: install-offline.sh --bundle <dir> --version <version> --app-name <name> [--host-name <host>] [--mediamtx-api-user <user>] [--mediamtx-api-password <password>] [--turn-auth-secret <secret>]" >&2
  exit 2
fi

normalize_public_authority() {
  local value="$1"
  value="${value#https://}"
  value="${value#http://}"
  value="${value%%/*}"
  printf '%s\n' "$value"
}

public_host_without_port() {
  local authority="$1"
  authority="$(normalize_public_authority "$authority")"
  if [[ "$authority" =~ ^\[([0-9A-Fa-f:.]+)\](:[0-9]+)?$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return
  fi
  if [[ "$authority" =~ ^([^:]+):[0-9]+$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return
  fi
  printf '%s\n' "$authority"
}

is_ipv4() {
  [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]
}

tls_san_ext() {
  local host
  host="$(public_host_without_port "$APP_HOST_NAME")"
  local sans=("DNS:localhost" "IP:127.0.0.1")
  if [[ -n "$host" && "$host" != "localhost" && "$host" != "127.0.0.1" ]]; then
    if is_ipv4 "$host" || [[ "$host" == *:* ]]; then
      sans+=("IP:$host")
    else
      sans+=("DNS:$host")
    fi
  fi
  local IFS=,
  printf '%s\n' "${sans[*]}"
}

APP_HOST_NAME="$(normalize_public_authority "$APP_HOST_NAME")"

STATE_ROOT="/srv/cv-platform"
SECRETS_DIR="$STATE_ROOT/secrets"
CONFIG_DIR="/etc/ybb-inferenzserver"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

ensure_systemd() {
  if [[ -d /run/systemd/system ]]; then
    return
  fi

  log "systemd ist in dieser WSL-Distribution noch nicht aktiv. Aktiviere systemd und fordere Neustart an."
  cat >/etc/wsl.conf <<'EOF'
[boot]
systemd=true
EOF
  exit 42
}

detect_python_bin() {
  if [[ -x /opt/ybb-runtime/venv/bin/python ]]; then
    echo "/opt/ybb-runtime/venv/bin/python"
  elif [[ -x /opt/cv-platform-venv/bin/python ]]; then
    echo "/opt/cv-platform-venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    echo "python3"
  fi
}

ensure_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Required command is missing in the prepared WSL runtime: $name" >&2
    exit 1
  fi
}

secret_hex() {
  openssl rand -hex "$1"
}

ensure_secret_file() {
  local path="$1"
  local value="$2"
  local mode="${3:-600}"
  if [[ ! -s "$path" ]]; then
    umask 077
    printf '%s\n' "$value" >"$path"
  fi
  chmod "$mode" "$path"
}

ensure_tls_files() {
  local cert_path="$SECRETS_DIR/tls_cert.pem"
  local key_path="$SECRETS_DIR/tls_key.pem"
  if [[ -s "$cert_path" && -s "$key_path" ]]; then
    return
  fi

  local public_host
  local san
  public_host="$(public_host_without_port "$APP_HOST_NAME")"
  san="$(tls_san_ext)"
  log "Erzeuge lokales TLS-Zertifikat fuer $APP_HOST_NAME."
  openssl req \
    -x509 \
    -nodes \
    -newkey rsa:2048 \
    -keyout "$key_path" \
    -out "$cert_path" \
    -days 3650 \
    -subj "/CN=$public_host" \
    -addext "subjectAltName=$san" >/dev/null 2>&1
  chmod 644 "$cert_path" "$key_path"
}

ensure_bootstrap_admin_secret() {
  local python_bin="$1"
  local hash_path="$SECRETS_DIR/bootstrap_admin_password_hash"
  local initial_password_path="$SECRETS_DIR/bootstrap_admin_initial_password.txt"

  if [[ -s "$hash_path" ]]; then
    return
  fi

  local password
  password="Admin-$(secret_hex 8)"
  log "Erzeuge initiales Admin-Passwort."
  "$python_bin" - "$password" >"$hash_path" <<'PY'
import sys
from argon2 import PasswordHasher

print(PasswordHasher().hash(sys.argv[1]))
PY
  chmod 600 "$hash_path"
  umask 077
  printf '%s\n' "$password" >"$initial_password_path"
  chmod 600 "$initial_password_path"
}

ensure_run_secrets() {
  rm -rf /run/secrets
  ln -s "$SECRETS_DIR" /run/secrets
}

install_secret_service() {
  cat >/etc/systemd/system/ybb-secrets.service <<EOF
[Unit]
Description=YBB Inferenzserver secret mount
DefaultDependencies=no
Before=postgresql.service redis-server.service ybb-platform.target

[Service]
Type=oneshot
ExecStart=/bin/bash -lc 'rm -rf /run/secrets && ln -s $SECRETS_DIR /run/secrets'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now ybb-secrets.service
}

ensure_postgres() {
  local password
  password="$(tr -d '\r\n' <"$SECRETS_DIR/postgres_app_password")"

  systemctl enable --now postgresql

  log "Initialisiere PostgreSQL-Rollen und Datenbanken."
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 <<SQL
ALTER USER postgres WITH PASSWORD '$password';
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cv_platform') THEN
    CREATE ROLE cv_platform LOGIN PASSWORD '$password';
  ELSE
    ALTER ROLE cv_platform WITH LOGIN PASSWORD '$password';
  END IF;
END
\$\$;
SELECT 'CREATE DATABASE cv_platform OWNER cv_platform'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'cv_platform')\gexec
SELECT 'CREATE DATABASE labelstudio OWNER postgres'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'labelstudio')\gexec
SELECT 'CREATE DATABASE mlflow OWNER postgres'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mlflow')\gexec
GRANT ALL PRIVILEGES ON DATABASE cv_platform TO cv_platform;
SQL
}

ensure_redis() {
  systemctl enable --now redis-server
}

ensure_systemd
ensure_command openssl
ensure_command tar
ensure_command runuser
ensure_command psql

PYTHON_BIN="$(detect_python_bin)"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python runtime was not found at $PYTHON_BIN. Prepare the WSL runtime before exporting the installer." >&2
  exit 1
fi

mkdir -p "$STATE_ROOT" "$SECRETS_DIR" "$CONFIG_DIR" "$STATE_ROOT/control/pretrained-checkpoints"

ensure_secret_file "$SECRETS_DIR/postgres_app_password" "$(secret_hex 32)"
ensure_secret_file "$SECRETS_DIR/session_secret" "$(secret_hex 48)"
ensure_secret_file "$SECRETS_DIR/labelstudio_api_token" ""
ensure_secret_file "$SECRETS_DIR/labelstudio_webhook_secret" "$(secret_hex 32)"
ensure_tls_files
ensure_bootstrap_admin_secret "$PYTHON_BIN"
ensure_run_secrets
install_secret_service
ensure_postgres
ensure_redis

log "Installiere Anwendung und starte Services."
bash "$BUNDLE_DIR/wsl/apply-release.sh" \
  --bundle "$BUNDLE_DIR" \
  --version "$APP_VERSION" \
  --app-name "$APP_NAME" \
  --host-name "$APP_HOST_NAME" \
  --mediamtx-api-user "$MEDIAMTX_API_USER" \
  --mediamtx-api-password "$MEDIAMTX_API_PASSWORD" \
  --turn-auth-secret "$TURN_AUTH_SECRET" \
  --mode install

log "Offline-Installation in WSL abgeschlossen."
