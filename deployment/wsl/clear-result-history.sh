#!/usr/bin/env bash
set -euo pipefail

DATABASE_NAME="cv_platform"
CONFIRMED=false
STOPPED_UNITS=()
UNITS=(
  ybb-runtime-orchestrator.service
  ybb-realtime-executor.service
  ybb-trigger-executor.service
  ybb-capture-service.service
  ybb-connector-service.service
  ybb-result-service.service
)

usage() {
  cat <<'EOF'
Usage: ybb-clear-result-history --confirm [--database NAME]

Permanently removes runtime result history and connector delivery history.
Channel, pipeline, model, capture, training, and audit configuration is kept.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm)
      CONFIRMED=true
      shift
      ;;
    --database)
      [[ $# -ge 2 ]] || {
        usage >&2
        exit 2
      }
      DATABASE_NAME="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$CONFIRMED" != "true" ]]; then
  usage >&2
  printf '\nRefusing destructive cleanup without --confirm.\n' >&2
  exit 2
fi

if [[ ! "$DATABASE_NAME" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]; then
  printf 'Invalid database name.\n' >&2
  exit 2
fi

restart_units() {
  if [[ "${#STOPPED_UNITS[@]}" -gt 0 ]]; then
    systemctl start "${STOPPED_UNITS[@]}" || true
  fi
}
trap restart_units EXIT

for unit in "${UNITS[@]}"; do
  if systemctl is-active --quiet "$unit"; then
    STOPPED_UNITS+=("$unit")
  fi
done
if [[ "${#STOPPED_UNITS[@]}" -gt 0 ]]; then
  systemctl stop "${STOPPED_UNITS[@]}"
fi

runuser -u postgres -- psql --set ON_ERROR_STOP=1 --dbname "$DATABASE_NAME" <<'SQL'
BEGIN;
UPDATE runtime_executions SET last_result_id = NULL WHERE last_result_id IS NOT NULL;
UPDATE runtime_capture_requests SET result_record_id = NULL WHERE result_record_id IS NOT NULL;
TRUNCATE TABLE runtime_connector_deliveries, result_records;
COMMIT;
SQL

if command -v fstrim >/dev/null 2>&1; then
  fstrim / || true
fi

runuser -u postgres -- psql --dbname "$DATABASE_NAME" --tuples-only --no-align \
  --command "SELECT pg_size_pretty(pg_total_relation_size('public.result_records'::regclass));"
