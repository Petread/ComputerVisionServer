#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/ybb-inferenzserver}"
STATE_ROOT="${STATE_ROOT:-/srv/cv-platform}"
RELEASE_KEEP_COUNT="${YBB_RELEASE_KEEP_COUNT:-3}"
REPORT_ONLY=false
MAINTENANCE_STATUS=0

log() {
  printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

usage() {
  cat <<'EOF'
Usage: storage-maintenance.sh [--report-only] [--keep-releases N]

Without --report-only, the script runs application retention, removes old
application releases and package caches, limits the system journal, and trims
free filesystem blocks for WSL.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-only)
      REPORT_ONLY=true
      shift
      ;;
    --keep-releases)
      [[ $# -ge 2 ]] || {
        usage >&2
        exit 2
      }
      RELEASE_KEEP_COUNT="$2"
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

if [[ ! "$RELEASE_KEEP_COUNT" =~ ^[0-9]+$ ]] || (( RELEASE_KEEP_COUNT < 2 )); then
  printf 'Release keep count must be an integer greater than or equal to 2.\n' >&2
  exit 2
fi

storage_report() {
  local detailed="${1:-false}"
  log "Filesystem usage"
  df -hT / "$STATE_ROOT" "$APP_ROOT" 2>/dev/null | awk '!seen[$1]++'

  if [[ "$detailed" == "true" ]]; then
    for root in "$STATE_ROOT" "$APP_ROOT" /var; do
      if [[ ! -d "$root" ]]; then
        continue
      fi
      log "Largest direct children below $root"
      du -x -B1 --max-depth=1 "$root" 2>/dev/null |
        sort -n |
        tail -n 25 |
        awk '{ printf "%14d  %s\n", $1, $2 }'
    done
  fi

  if command -v journalctl >/dev/null 2>&1; then
    journalctl --disk-usage || true
  fi
}

prune_application_releases() {
  local releases_dir="$APP_ROOT/releases"
  [[ -d "$releases_dir" ]] || return 0

  local current_target=""
  current_target="$(readlink -f "$APP_ROOT/current" 2>/dev/null || true)"
  local kept=0
  local entry release_dir
  mapfile -t release_entries < <(
    find "$releases_dir" \
      -mindepth 1 \
      -maxdepth 1 \
      -type d \
      ! -name '.tmp_*' \
      -printf '%T@\t%p\n' 2>/dev/null |
      sort -nr
  )
  for entry in "${release_entries[@]}"; do
    release_dir="${entry#*$'\t'}"
    if [[ -z "$release_dir" || "$release_dir" == "$releases_dir" ]]; then
      continue
    fi
    if [[ -n "$current_target" && "$(readlink -f "$release_dir")" == "$current_target" ]]; then
      ((kept += 1))
      continue
    fi
    if (( kept < RELEASE_KEEP_COUNT )); then
      ((kept += 1))
      continue
    fi
    log "Removing old application release: $release_dir"
    rm -rf -- "$release_dir"
  done

  find "$releases_dir" \
    -mindepth 1 \
    -maxdepth 1 \
    -type d \
    -name '.tmp_*' \
    -mtime +1 \
    -print \
    -exec rm -rf -- {} +
}

run_application_retention() {
  if [[ ! -x /usr/local/bin/ybb-python-worker ]]; then
    log "Python maintenance runner is not installed; application retention is skipped."
    return 0
  fi
  log "Running reference-aware application retention."
  if ! /usr/local/bin/ybb-python-worker maintenance_worker.scheduled; then
    log "WARNING: Application retention reported an error; continuing with system cleanup."
    MAINTENANCE_STATUS=1
  fi
}

prune_system_caches() {
  log "Removing dispensable package caches."
  find /var/cache/apt/archives \
    -maxdepth 1 \
    -type f \
    -name '*.deb' \
    -delete 2>/dev/null || true
  if command -v uv >/dev/null 2>&1; then
    uv cache prune >/dev/null 2>&1 || true
  fi
  if command -v journalctl >/dev/null 2>&1; then
    journalctl --vacuum-time=14d --vacuum-size=2G || true
  fi
}

trim_wsl_filesystem() {
  if command -v fstrim >/dev/null 2>&1; then
    log "Discarding free filesystem blocks for the WSL virtual disk."
    fstrim -av || true
  fi
}

if [[ "$REPORT_ONLY" == "true" ]]; then
  storage_report true
  exit 0
fi

run_application_retention
prune_application_releases
prune_system_caches
trim_wsl_filesystem
storage_report false
log "Storage maintenance completed."
exit "$MAINTENANCE_STATUS"
