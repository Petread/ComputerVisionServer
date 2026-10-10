#!/usr/bin/env bash
set -euo pipefail

BUNDLE_DIR=""
APP_VERSION=""
APP_NAME=""
APP_HOST_NAME=""
MODE="update"
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
    --mode)
      MODE="$2"
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
  echo "Usage: apply-release.sh --bundle <dir> --version <version> --app-name <name> [--host-name <host>] [--mode install|update] [--mediamtx-api-user <user>] [--mediamtx-api-password <password>] [--turn-auth-secret <secret>]" >&2
  exit 2
fi

if [[ "$MODE" != "install" && "$MODE" != "update" ]]; then
  echo "Invalid mode: $MODE" >&2
  exit 2
fi

APP_ROOT="/opt/ybb-inferenzserver"
STATE_ROOT="/srv/cv-platform"
SECRETS_DIR="$STATE_ROOT/secrets"
CONFIG_DIR="/etc/ybb-inferenzserver"
ENV_FILE="$CONFIG_DIR/runtime.env"
INSTALL_ENV_FILE="$CONFIG_DIR/install.env"
RELEASES_DIR="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
APP_ARCHIVE="$BUNDLE_DIR/app.tar.gz"
RELEASE_DIR=""

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
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

detect_training_python_bin() {
  if [[ -x /opt/ybb-runtime/training-venv/bin/python ]]; then
    echo "/opt/ybb-runtime/training-venv/bin/python"
  elif [[ -x /opt/cv-platform-training-venv/bin/python ]]; then
    echo "/opt/cv-platform-training-venv/bin/python"
  else
    detect_python_bin
  fi
}

detect_bin() {
  local preferred="$1"
  local fallback_name="$2"
  if [[ -x "$preferred" ]]; then
    echo "$preferred"
  elif command -v "$fallback_name" >/dev/null 2>&1; then
    command -v "$fallback_name"
  else
    echo "$preferred"
  fi
}

ensure_training_cuda_runtime_links() {
  local training_python_bin="$1"
  if [[ -z "$training_python_bin" || ! -x "$training_python_bin" ]]; then
    return
  fi

  "$training_python_bin" - <<'PY'
from pathlib import Path
import importlib.util
import sys

if importlib.util.find_spec("torch") is None:
    raise SystemExit(0)

import torch

torch_lib = Path(torch.__file__).resolve().parent / "lib"
candidates = sorted(torch_lib.glob("libnvrtc-*.so.*"))
if not candidates:
    print(f"WARNUNG: Keine gebuendelte libnvrtc in {torch_lib} gefunden.", file=sys.stderr)
    raise SystemExit(0)
target = candidates[0]
system_lib = Path("/usr/lib/x86_64-linux-gnu")
system_lib.mkdir(parents=True, exist_ok=True)
for link_name in ("libnvrtc.so", f"libnvrtc.so.{target.name.split('.so.', maxsplit=1)[1]}"):
    link_path = system_lib / link_name
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(target)

builtins = sorted(torch_lib.glob("libnvrtc-builtins.so.*"))
if not builtins:
    print(f"WARNUNG: Keine gebuendelte libnvrtc-builtins in {torch_lib} gefunden.", file=sys.stderr)
    raise SystemExit(0)
builtins_target = builtins[0]
for link_name in ("libnvrtc-builtins.so", builtins_target.name):
    link_path = system_lib / link_name
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(builtins_target)

print({"libnvrtc_target": str(target), "libnvrtc_builtins_target": str(builtins_target)})
PY
}

ensure_service_onnxruntime() {
  local service_python_bin="$1"
  local training_python_bin="$2"
  if [[ -z "$service_python_bin" || ! -x "$service_python_bin" ]]; then
    return
  fi
  if "$service_python_bin" - <<'PY' >/dev/null 2>&1; then
import onnxruntime
PY
    return
  fi
  if [[ -z "$training_python_bin" || ! -x "$training_python_bin" ]]; then
    log "WARNUNG: onnxruntime fehlt in der Service-Runtime und es wurde keine Training-Runtime zum Kopieren gefunden."
    return
  fi
  if ! "$training_python_bin" - <<'PY' >/dev/null 2>&1; then
import onnxruntime
PY
    log "WARNUNG: onnxruntime fehlt in der Service-Runtime und ist auch in der Training-Runtime nicht importierbar."
    return
  fi

  log "Aktualisiere Service-Runtime fuer lokalen ONNX-Deployment-Fallback."
  local service_site
  local training_site
  service_site="$("$service_python_bin" - <<'PY'
import site

paths = site.getsitepackages()
print(paths[0] if paths else "")
PY
)"
  training_site="$("$training_python_bin" - <<'PY'
import site

paths = site.getsitepackages()
print(paths[0] if paths else "")
PY
)"
  if [[ -z "$service_site" || -z "$training_site" ]]; then
    log "WARNUNG: Python-site-packages fuer onnxruntime-Kopie konnten nicht ermittelt werden."
    return
  fi

  "$service_python_bin" - "$service_site" "$training_site" <<'PY'
from pathlib import Path
import shutil
import sys

service_site = Path(sys.argv[1])
training_site = Path(sys.argv[2])
copy_specs = [
    ("onnxruntime", "onnxruntime*.dist-info"),
    ("coloredlogs", "coloredlogs*.dist-info"),
    ("humanfriendly", "humanfriendly*.dist-info"),
    ("flatbuffers", "flatbuffers*.dist-info"),
]

def replace_copy(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)

copied: list[str] = []
for module_name, dist_glob in copy_specs:
    for src in training_site.glob(module_name):
        replace_copy(src, service_site / src.name)
        copied.append(src.name)
    for src in training_site.glob(dist_glob):
        replace_copy(src, service_site / src.name)
        copied.append(src.name)

service_google_protobuf = service_site / "google" / "protobuf"
training_google_protobuf = training_site / "google" / "protobuf"
if not service_google_protobuf.exists() and training_google_protobuf.exists():
    (service_site / "google").mkdir(exist_ok=True)
    replace_copy(training_google_protobuf, service_google_protobuf)
    copied.append("google/protobuf")
    for src in training_site.glob("protobuf*.dist-info"):
        replace_copy(src, service_site / src.name)
        copied.append(src.name)

print({"copied": copied})
PY
  if ! "$service_python_bin" - <<'PY' >/dev/null 2>&1; then
import onnxruntime
PY
    log "WARNUNG: onnxruntime konnte nach der Service-Runtime-Aktualisierung weiterhin nicht importiert werden."
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

ensure_internal_machine_credentials() {
  mkdir -p "$SECRETS_DIR"
  # Caddy runs as the unprivileged caddy user and reads TLS files through
  # /run/secrets -> $SECRETS_DIR. Keep the directory traversable, while
  # individual secret files stay locked down below.
  chmod 755 "$STATE_ROOT" "$SECRETS_DIR"

  local key_id="ybb-inference-runtime"
  local secret_path="$SECRETS_DIR/internal_inference_gateway_api_key_secret"
  local bearer_token_path="$SECRETS_DIR/internal_inference_gateway_bearer_token"

  ensure_secret_file "$secret_path" "$(secret_hex 32)"
  local secret
  secret="$(tr -d '\r\n' <"$secret_path")"
  umask 077
  printf '%s.%s\n' "$key_id" "$secret" >"$bearer_token_path"
  chmod 600 "$bearer_token_path"
  for tls_file in tls_cert.pem tls_key.pem; do
    if [[ -f "$SECRETS_DIR/$tls_file" ]]; then
      chmod 644 "$SECRETS_DIR/$tls_file"
    fi
  done
}

triton_available() {
  [[ -x /opt/tritonserver/bin/tritonserver ]] || command -v tritonserver >/dev/null 2>&1
}

install_triton_archive_from_bundle() {
  local triton_archive="$BUNDLE_DIR/runtime/tritonserver.tar.gz"
  if [[ ! -f "$triton_archive" ]]; then
    return
  fi

  log "Installiere Triton Server aus Offline-Bundle: $triton_archive"
  local tmp_dir
  tmp_dir="$(mktemp -d /opt/tritonserver.install.XXXXXX)"
  tar -xf "$triton_archive" -C "$tmp_dir" --strip-components=1
  if [[ ! -x "$tmp_dir/bin/tritonserver" ]]; then
    rm -rf "$tmp_dir"
    echo "Triton-Archiv ist ungueltig: bin/tritonserver wurde nach dem Entpacken nicht gefunden." >&2
    exit 1
  fi

  systemctl stop ybb-triton-rt.service ybb-triton-batch.service >/dev/null 2>&1 || true
  rm -rf /opt/tritonserver
  mv "$tmp_dir" /opt/tritonserver
}

read_bundle_manifest_value() {
  local key="$1"
  local default_value="${2:-}"
  local manifest_path="$BUNDLE_DIR/windows-bundle-manifest.json"
  if [[ ! -f "$manifest_path" ]]; then
    printf '%s\n' "$default_value"
    return
  fi
  python3 - "$manifest_path" "$key" "$default_value" <<'PY'
import json
import sys

try:
    data = json.loads(open(sys.argv[1], encoding="utf-8-sig").read())
except Exception:
    data = {}
value = data.get(sys.argv[2], sys.argv[3])
if isinstance(value, bool):
    print("true" if value else "false")
elif value is None:
    print(sys.argv[3])
else:
    print(value)
PY
}

install_mediamtx_archive_from_bundle() {
  if [[ "$(mediamtx_host_platform)" == "windows" ]]; then
    log "MediaMTX wird direkt auf Windows gehostet; Linux-MediaMTX in WSL wird uebersprungen."
    systemctl disable --now ybb-mediamtx.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/ybb-mediamtx.service
    return 0
  fi
  local media_archive="$BUNDLE_DIR/runtime/mediamtx.tar.gz"
  if [[ ! -f "$media_archive" ]]; then
    log "WARNUNG: MediaMTX Archiv fehlt im Bundle; RTSP/WebRTC-Fanout bleibt deaktiviert, bis ein Bundle mit runtime/mediamtx.tar.gz eingespielt wird."
    return
  fi

  local version
  version="$(read_bundle_manifest_value MediaMtxVersion "unknown")"
  log "Installiere MediaMTX $version aus Offline-Bundle: $media_archive"
  local tmp_dir install_dir binary
  tmp_dir="$(mktemp -d /opt/mediamtx.install.XXXXXX)"
  tar -xf "$media_archive" -C "$tmp_dir"
  binary="$(find "$tmp_dir" -type f -name mediamtx -perm -u+x | head -n 1)"
  if [[ -z "$binary" ]]; then
    rm -rf "$tmp_dir"
    echo "MediaMTX-Archiv ist ungueltig: ausfuehrbare Datei 'mediamtx' wurde nicht gefunden." >&2
    exit 1
  fi

  install_dir="/opt/mediamtx/$version"
  rm -rf "$install_dir"
  mkdir -p "$install_dir"
  install -m 0755 "$binary" "$install_dir/mediamtx"
  ln -sfn "$install_dir/mediamtx" /usr/local/bin/mediamtx
  rm -rf "$tmp_dir"
}

install_gstreamer_deb_bundle_from_bundle() {
  local gstreamer_archive="$BUNDLE_DIR/runtime/gstreamer-debs.tar.gz"
  if [[ ! -f "$gstreamer_archive" ]]; then
    if command -v gst-launch-1.0 >/dev/null 2>&1 && command -v turnserver >/dev/null 2>&1; then
      return 0
    fi
    log "FEHLER: Das Debian-Paketbundle fehlt oder enthaelt noch keinen TURN-Server. Exportiere das Update erneut mit runtime/gstreamer-debs.tar.gz."
    return 1
  fi

  log "Installiere GStreamer Debian-Pakete aus Offline-Bundle: $gstreamer_archive"
  local tmp_dir empty_sources_dir
  tmp_dir="$(mktemp -d /tmp/ybb-gstreamer-debs.XXXXXX)"
  empty_sources_dir="$tmp_dir/empty-sources"
  mkdir -p "$empty_sources_dir"
  tar -xzf "$gstreamer_archive" -C "$tmp_dir"
  if ! compgen -G "$tmp_dir/debs/*.deb" >/dev/null; then
    rm -rf "$tmp_dir"
    echo "GStreamer Debian-Paketbundle ist ungueltig: keine .deb Dateien gefunden." >&2
    return 1
  fi

  # Application rollback must not request older OS libraries just because the
  # archived dependency closure predates the installed runtime. Keep configured
  # equal/newer packages; still install missing, broken or newer bundle packages.
  local deb package architecture bundle_version installed status installed_version
  local retained=0
  local -a install_debs=()
  for deb in "$tmp_dir"/debs/*.deb; do
    package="$(dpkg-deb -f "$deb" Package)"
    architecture="$(dpkg-deb -f "$deb" Architecture)"
    bundle_version="$(dpkg-deb -f "$deb" Version)"
    if [[ "$architecture" != "all" ]]; then
      package="$package:$architecture"
    fi
    installed="$(dpkg-query -W -f='${Status}\t${Version}' "$package" 2>/dev/null || true)"
    status="${installed%%$'\t'*}"
    installed_version="${installed#*$'\t'}"
    if [[ ( "$status" == "install ok installed" || "$status" == "hold ok installed" ) && -n "$installed_version" ]] &&
      dpkg --compare-versions "$installed_version" ge "$bundle_version"; then
      retained=$((retained + 1))
    else
      install_debs+=("$deb")
    fi
  done
  log "Offline-Abhaengigkeiten: $retained installierte gleiche/neuere Pakete beibehalten; ${#install_debs[@]} Pakete zu installieren."
  if (( ${#install_debs[@]} > 0 )); then
    export DEBIAN_FRONTEND=noninteractive
    if ! apt-get \
      -o Dir::Etc::sourcelist=/dev/null \
      -o Dir::Etc::sourceparts="$empty_sources_dir" \
      -o APT::Get::List-Cleanup=0 \
      install -y --no-remove --no-install-recommends "${install_debs[@]}"; then
      rm -rf "$tmp_dir"
      log "FEHLER: Offline-Abhaengigkeiten konnten ohne Paketentfernung oder erzwungenes Downgrade nicht aufgeloest werden."
      return 1
    fi
  fi
  rm -rf "$tmp_dir"
  if ! command -v turnserver >/dev/null 2>&1; then
    log "FEHLER: Das Offline-Paketbundle enthaelt kein ausfuehrbares turnserver-Programm (Paket coturn)."
    return 1
  fi
}

install_python_wheel_bundle_from_bundle() {
  local service_python_bin="$1"
  local requirements_name="${2:-requirements.txt}"
  local runtime_label="${3:-Python-Runtime}"
  local wheel_archive="$BUNDLE_DIR/runtime/python-wheels.tar.gz"
  if [[ -z "$service_python_bin" || ! -x "$service_python_bin" ]]; then
    log "WARNUNG: $runtime_label wurde nicht gefunden; Python-Wheel-Bundle kann nicht installiert werden."
    return 0
  fi
  if [[ ! -f "$wheel_archive" ]]; then
    log "WARNUNG: Python-Wheel-Bundle fehlt im Bundle; neue Runtime-Abhaengigkeiten sind nur verfuegbar, wenn sie bereits in $runtime_label installiert sind."
    return 0
  fi

  log "Installiere Python-Wheels aus Offline-Bundle in $runtime_label: $wheel_archive"
  local tmp_dir requirements_file
  tmp_dir="$(mktemp -d /tmp/ybb-python-wheels.XXXXXX)"
  tar -xzf "$wheel_archive" -C "$tmp_dir"
  requirements_file="$tmp_dir/$requirements_name"
  if [[ ! -f "$requirements_file" ]]; then
    log "WARNUNG: Python-Wheel-Bundle enthaelt $requirements_name nicht; ueberspringe Installation fuer $runtime_label."
    rm -rf "$tmp_dir"
    return 0
  fi
  if install_python_wheels_from_dir "$service_python_bin" "$tmp_dir" "$requirements_file"; then
    rm -rf "$tmp_dir"
    return 0
  fi
  rm -rf "$tmp_dir"
  return 1
}

build_training_runtime_library_path() {
  local python_bin="$1"
  local python_site_packages=""
  if [[ -n "$python_bin" && -x "$python_bin" ]]; then
    python_site_packages="$("$python_bin" - <<'PY'
import site

paths = site.getsitepackages()
print(paths[0] if paths else "")
PY
)"
  fi

  local library_paths=()
  for candidate in \
    "$python_site_packages/torch/lib" \
    "$python_site_packages"/nvidia/*/lib \
    /opt/tritonserver/external-libs \
    /opt/tritonserver/lib \
    /opt/tritonserver/backends/tensorrt \
    /opt/tritonserver/backends/onnxruntime \
    /usr/lib/wsl/lib \
    /usr/local/cuda/lib64 \
    /usr/local/cuda/compat \
    /usr/lib/x86_64-linux-gnu
  do
    if [[ -d "$candidate" ]]; then
      library_paths+=("$candidate")
    fi
  done

  if [[ "${#library_paths[@]}" -gt 0 ]]; then
    local IFS=:
    echo "${library_paths[*]}"
  fi
}

validate_deployment_packaging_runtime() {
  local deploy_python_bin="$1"
  if [[ -z "$deploy_python_bin" || ! -x "$deploy_python_bin" ]]; then
    deploy_python_bin="$PYTHON_BIN"
  fi
  if [[ -z "$deploy_python_bin" || ! -x "$deploy_python_bin" ]]; then
    log "FEHLER: Keine Python-Runtime fuer Deployment-Packaging gefunden."
    return 1
  fi

  log "Pruefe Deployment-Packaging-Runtime: $deploy_python_bin"
  local deploy_library_path
  deploy_library_path="$(build_training_runtime_library_path "$deploy_python_bin")"
  if [[ -n "$deploy_library_path" ]]; then
    export LD_LIBRARY_PATH="$deploy_library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
  PYTHONPATH="$(build_pythonpath "$RELEASE_DIR")" "$deploy_python_bin" - <<'PY'
from importlib.util import find_spec

missing = [
    name
    for name in ("mmdeploy", "onnxruntime", "tensorrt", "tensorrt_bindings")
    if find_spec(name) is None
]
if missing:
    raise SystemExit(f"Deployment-Packaging-Abhaengigkeiten fehlen: {', '.join(missing)}")

from cv_deploy.packaging import _find_trtexec_binary, _load_mmdeploy_tensorrt_api

if _find_trtexec_binary() is None:
    raise SystemExit("Deployment-Packaging-Abhaengigkeit fehlt: trtexec wurde nicht gefunden.")
_load_mmdeploy_tensorrt_api()
print("deployment-packaging-runtime-imports-ok")
PY
}

install_python_wheels_from_dir() {
  local service_python_bin="$1"
  local wheel_dir="$2"
  local requirements_file="$3"
  local install_args=(--no-index --find-links "$wheel_dir")
  if [[ -f "$requirements_file" ]]; then
    install_args+=(-r "$requirements_file")
  else
    install_args+=(python-snap7 pypylon)
  fi

  if "$service_python_bin" -m pip --version >/dev/null 2>&1; then
    "$service_python_bin" -m pip install "${install_args[@]}"
    return 0
  fi

  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$service_python_bin" "${install_args[@]}"
    return 0
  fi

  if "$service_python_bin" -m ensurepip --upgrade >/dev/null 2>&1 && "$service_python_bin" -m pip --version >/dev/null 2>&1; then
    "$service_python_bin" -m pip install "${install_args[@]}"
    return 0
  fi

  echo "Python-Wheel-Bundle kann nicht installiert werden: weder pip noch uv pip noch ensurepip ist fuer $service_python_bin verfuegbar." >&2
  return 1
}

validate_basler_pypylon_package() {
  local service_python_bin="$1"
  if [[ -z "$service_python_bin" || ! -x "$service_python_bin" ]]; then
    log "WARNUNG: Service-Runtime wurde nicht gefunden; pypylon-Verfuegbarkeit kann nicht geprueft werden."
    return 0
  fi

  if ! "$service_python_bin" - <<'PY'
from importlib.util import find_spec

if find_spec("pypylon") is None:
    raise SystemExit("pypylon fehlt")
PY
  then
    log "FEHLER: pypylon fehlt in der Service-Runtime. Erzeuge das Update-Bundle neu; runtime/python-wheels.tar.gz muss ein pypylon-*.whl enthalten."
    return 1
  fi

  if "$service_python_bin" - <<'PY'
from pypylon import pylon

print("pypylon-import-ok")
PY
  then
    log "Basler Pylon Python-Paket ist in der Service-Runtime importierbar."
  else
    log "WARNUNG: pypylon ist installiert, aber der native Pylon-Import ist fehlgeschlagen. Pruefe die Basler Pylon Suite/Runtime auf dem Zielsystem, falls Basler-Kanaele nicht starten."
  fi
}

read_installed_host_name() {
  if [[ -n "$APP_HOST_NAME" ]]; then
    normalize_public_authority "$APP_HOST_NAME"
    return
  fi
  local bundle_host_name
  bundle_host_name="$(read_bundle_host_name)"
  if [[ -n "$bundle_host_name" ]] && ! is_local_public_authority "$bundle_host_name"; then
    normalize_public_authority "$bundle_host_name"
    return
  fi
  if [[ -f "$INSTALL_ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    . "$INSTALL_ENV_FILE"
    if [[ -n "${APP_HOST_NAME:-}" ]]; then
      normalize_public_authority "$APP_HOST_NAME"
      return
    fi
  fi
  echo "127.0.0.1"
}

read_bundle_host_name() {
  local manifest_path="$BUNDLE_DIR/windows-bundle-manifest.json"
  if [[ ! -f "$manifest_path" ]]; then
    return
  fi

  local python_reader
  python_reader="$(command -v python3 || command -v python || true)"
  if [[ -n "$python_reader" ]]; then
    "$python_reader" - "$manifest_path" <<'PY' || true
import json
import sys

try:
    value = json.loads(open(sys.argv[1], encoding="utf-8").read()).get("AppHostName", "")
except Exception:
    value = ""
if value:
    print(value)
PY
    return
  fi

  sed -nE 's/.*"AppHostName"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$manifest_path" | head -n 1
}

normalize_public_authority() {
  local value="$1"
  value="${value#https://}"
  value="${value#http://}"
  value="${value%%/*}"
  if [[ "$value" =~ ^(\[[^]]+\]|[^:]+):(443|8443)$ ]]; then
    value="${BASH_REMATCH[1]}"
  fi
  printf '%s\n' "$value"
}

is_local_public_authority() {
  local value host
  value="$(normalize_public_authority "$1")"
  host="$value"
  if [[ "$host" =~ ^\[([^]]+)\](:[0-9]+)?$ ]]; then
    host="${BASH_REMATCH[1]}"
  elif [[ "$host" =~ ^([^:]+):[0-9]+$ ]]; then
    host="${BASH_REMATCH[1]}"
  fi
  [[ "$host" == "127.0.0.1" || "$host" == "localhost" || "$host" == "::1" ]]
}

public_authority_host() {
  local value host
  value="$(normalize_public_authority "$1")"
  host="$value"
  if [[ "$host" =~ ^\[([^]]+)\](:[0-9]+)?$ ]]; then
    host="${BASH_REMATCH[1]}"
  elif [[ "$host" =~ ^([^:]+):[0-9]+$ ]]; then
    host="${BASH_REMATCH[1]}"
  fi
  printf '%s\n' "$host"
}

normalize_wsl_networking_mode() {
  local value
  value="$(printf '%s' "${1:-unchanged}" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    mirrored)
      echo "mirrored"
      ;;
    nat|default)
      echo "nat"
      ;;
    unchanged|"")
      echo "unchanged"
      ;;
    *)
      echo "unchanged"
      ;;
  esac
}

read_installed_wsl_networking_mode() {
  if [[ -f "$INSTALL_ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    . "$INSTALL_ENV_FILE"
    if [[ -n "${YBB_WSL_NETWORKING_MODE:-}" ]]; then
      normalize_wsl_networking_mode "$YBB_WSL_NETWORKING_MODE"
      return
    fi
  fi
  echo "unchanged"
}

wsl_networking_mode() {
  local mode
  if [[ -n "${YBB_WSL_NETWORKING_MODE:-}" ]]; then
    mode="$(normalize_wsl_networking_mode "$YBB_WSL_NETWORKING_MODE")"
    if [[ "$mode" != "unchanged" ]]; then
      echo "$mode"
      return
    fi
  fi
  mode="$(normalize_wsl_networking_mode "$(read_bundle_manifest_value WslNetworkingMode "unchanged")")"
  if [[ "$mode" != "unchanged" ]]; then
    echo "$mode"
    return
  fi
  read_installed_wsl_networking_mode
}

is_wsl_mirrored_networking() {
  [[ "$(wsl_networking_mode)" == "mirrored" ]]
}

windows_host_for_wsl() {
  local gateway nameserver
  if is_wsl_mirrored_networking; then
    echo "127.0.0.1"
    return
  fi
  gateway="$(ip route show default 2>/dev/null | awk '{ print $3; exit }' || true)"
  if [[ -n "$gateway" ]]; then
    printf '%s\n' "$gateway"
    return
  fi
  nameserver="$(awk '/^nameserver[[:space:]]+/ { print $2; exit }' /etc/resolv.conf 2>/dev/null || true)"
  if [[ -n "$nameserver" ]]; then
    printf '%s\n' "$nameserver"
    return
  fi
  echo "127.0.0.1"
}

mediamtx_public_host_for_wsl() {
  local bundle_host
  bundle_host="$(read_bundle_host_name)"
  if [[ -n "$bundle_host" ]] && ! is_local_public_authority "$bundle_host"; then
    public_authority_host "$bundle_host"
    return
  fi
  if [[ -n "${APP_HOST_NAME:-}" ]] && ! is_local_public_authority "$APP_HOST_NAME"; then
    public_authority_host "$APP_HOST_NAME"
  fi
}

mediamtx_host_platform() {
  read_bundle_manifest_value MediaMtxHostPlatform "linux"
}

mediamtx_runs_in_wsl() {
  [[ "$(mediamtx_host_platform)" != "windows" ]] && command -v mediamtx >/dev/null 2>&1
}

mediamtx_host_for_wsl() {
  if [[ "$(mediamtx_host_platform)" == "windows" ]]; then
    local host
    host="$(windows_host_for_wsl)"
    if [[ -z "$host" ]]; then
      host="$(mediamtx_public_host_for_wsl)"
    fi
    if [[ -n "$host" ]]; then
      printf '%s\n' "$host"
    else
      echo "127.0.0.1"
    fi
  else
    echo "127.0.0.1"
  fi
}

mediamtx_api_url_for_wsl() {
  local host="${1:-}"
  if [[ -z "$host" ]]; then
    host="$(mediamtx_host_for_wsl)"
  fi
  if [[ -z "$host" ]]; then
    host="127.0.0.1"
  fi
  if [[ -n "$MEDIAMTX_API_USER" && -n "$MEDIAMTX_API_PASSWORD" ]]; then
    printf 'http://%s:%s@%s:9997\n' "$MEDIAMTX_API_USER" "$MEDIAMTX_API_PASSWORD" "$host"
  else
    printf 'http://%s:9997\n' "$host"
  fi
}

build_pythonpath() {
  local root="${1:-$CURRENT_LINK}"
  local paths=(
    "backend/packages/cv-core/src"
    "backend/packages/cv-contracts/src"
    "backend/packages/cv-edge/src"
    "backend/packages/cv-db/src"
    "backend/packages/cv-deploy/src"
    "backend/packages/cv-runtime/src"
    "backend/packages/cv-datasets/src"
    "backend/packages/cv-events/src"
    "backend/packages/cv-observability/src"
    "backend/packages/cv-status/src"
    "backend/packages/cv-training/src"
    "backend/packages/cv-utils/src"
    "backend/services/admin-api/src"
    "backend/services/dataset-service/src"
    "backend/services/deployment-controller/src"
    "backend/services/inference-gateway/src"
    "backend/services/model-registry-service/src"
    "backend/services/training-orchestrator/src"
    "backend/services/webhook-receiver/src"
    "backend/services/runtime-admin-api/src"
    "backend/services/channel-service/src"
    "backend/services/pipeline-definition-service/src"
    "backend/services/runtime-orchestrator/src"
    "backend/services/realtime-executor/src"
    "backend/services/camera-media/src"
    "backend/services/trigger-executor/src"
    "backend/services/overlay-service/src"
    "backend/services/capture-service/src"
    "backend/services/result-service/src"
    "backend/services/connector-service/src"
    "backend/workers/batch-worker/src"
    "backend/workers/control-worker/src"
    "backend/workers/maintenance-worker/src"
    "backend/workers/training-worker/src"
  )
  local joined=""
  local item
  for item in "${paths[@]}"; do
    if [[ -z "$joined" ]]; then
      joined="$root/$item"
    else
      joined="$joined:$root/$item"
    fi
  done
  echo "$joined"
}

require_release_token() {
  local relative_path="$1"
  local token="$2"
  local path="$RELEASE_DIR/$relative_path"
  if [[ ! -f "$path" ]]; then
    log "FEHLER: Temporal-/Sequenz-Kompatibilitaetsdatei fehlt im Release-Kandidaten: $relative_path"
    return 1
  fi
  if ! grep -Fq -- "$token" "$path"; then
    log "FEHLER: Temporal-/Sequenz-Kompatibilitaetsvertrag fehlt in $relative_path: $token"
    return 1
  fi
}

validate_temporal_release_compatibility() {
  local compatibility_profile minimum_database_revision persistent_state_root
  local temporal_sample_storage temporal_snapshot_storage activation_policy
  compatibility_profile="$(read_bundle_manifest_value CompatibilityProfile '')"
  minimum_database_revision="$(read_bundle_manifest_value MinimumDatabaseRevision '')"
  persistent_state_root="$(read_bundle_manifest_value PersistentStateRoot '')"
  temporal_sample_storage="$(read_bundle_manifest_value TemporalSampleStorage '')"
  temporal_snapshot_storage="$(read_bundle_manifest_value TemporalSnapshotStorage '')"
  activation_policy="$(read_bundle_manifest_value StemOrientationActivation '')"
  if [[ "$compatibility_profile" != "temporal-sequence.v1" ]]; then
    log "FEHLER: Bundle-Kompatibilitaetsprofil fehlt oder ist unbekannt: $compatibility_profile"
    return 1
  fi
  if [[ "$persistent_state_root" != "$STATE_ROOT" ]]; then
    log "FEHLER: Bundle-Persistenzroot stimmt nicht mit der installierten Runtime ueberein: $persistent_state_root != $STATE_ROOT"
    return 1
  fi
  if [[ "$minimum_database_revision" != "20260803_0035" ]]; then
    log "FEHLER: Minimale Temporal-/Review-Datenbankrevision ist nicht dokumentiert: $minimum_database_revision"
    return 1
  fi
  if [[ "$temporal_sample_storage" != "$STATE_ROOT/runtime/captures/temporal-samples" ]]; then
    log "FEHLER: Temporal-Sample-Persistenzpfad ist inkonsistent: $temporal_sample_storage"
    return 1
  fi
  if [[ "$temporal_snapshot_storage" != "$STATE_ROOT/control/snapshots" ]]; then
    log "FEHLER: Temporal-Snapshot-Persistenzpfad ist inkonsistent: $temporal_snapshot_storage"
    return 1
  fi
  if [[ "$activation_policy" != "pipeline_configuration_required" ]]; then
    log "FEHLER: Stammausrichtung darf nach Installation nicht implizit aktiviert werden: $activation_policy"
    return 1
  fi

  require_release_token "backend/packages/cv-status/src/cv_status/enums.py" 'TEMPORAL_SEQUENCE = "temporal_sequence"'
  require_release_token "backend/packages/cv-datasets/src/cv_datasets/annotation_schemas.py" 'schema_id="stem_orientation_angle.v1"'
  require_release_token "backend/packages/cv-datasets/src/cv_datasets/annotation_schemas.py" '<Video name="video"'
  require_release_token "backend/packages/cv-datasets/src/cv_datasets/annotation_schemas.py" '<TimelineLabels'
  require_release_token "backend/packages/cv-datasets/src/cv_datasets/temporal_import.py" 'def temporal_import_idempotency_key'
  require_release_token "backend/packages/cv-datasets/src/cv_datasets/temporal_snapshot.py" 'def materialize_temporal_snapshot'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/registry.py" 'node_type="analysis.stem_temporal_orientation"'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/registry.py" 'node_type="analysis.stem_orientation_model_decision"'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/registry.py" 'node_type="preprocessing.process_coordinate_rectify"'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/registry.py" 'node_type="analysis.stem_measurement_eligibility"'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/registry.py" 'node_type="analysis.stem_horizontal_curvature"'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/registry.py" 'node_type="analysis.stem_surface_phase"'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/registry.py" 'node_type="analysis.stem_curvature_phase_solver"'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/registry.py" '"shadow_mode_enabled": False'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/registry.py" '"stem_orientation_command_enabled": False'
  require_release_token "backend/packages/cv-db/src/cv_db/pipeline_store.py" '"slug": "template-realtime-stem-curvature-minimum"'
  require_release_token "backend/packages/cv-db/src/cv_db/pipeline_store.py" '"require_image_view_compatibility": True'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/payloads.py" 'class StemOrientationResultV3'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/siemens_sps.py" 'def _stem_orientation_command_gate'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/image_view.py" 'def calculate_image_view_profile'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/image_view.py" 'def restore_view_mask'
  require_release_token "backend/packages/cv-runtime/src/cv_runtime/image_view.py" 'def view_overlay_geometry_to_source'
  require_release_token "backend/services/realtime-executor/src/realtime_executor/live.py" '_source_overlay_from_image_view'
  require_release_token "backend/services/capture-service/src/capture_service/executor.py" 'No current canonical pipeline image output'
  require_release_token "backend/services/runtime-admin-api/src/runtime_admin_api/api/routes.py" 'def _capture_calibration_frame_from_stream_ring'
  require_release_token "frontend/apps/admin-web/src/features/runtime-pipelines/runtime-pipeline-designer-page.tsx" 'export function commissioningPreviewTargetNodeKey'
  require_release_token "frontend/apps/admin-web/src/features/channels/channel-detail-page.tsx" 'Actual Label Studio / model input'
  require_release_token "backend/packages/cv-contracts/src/cv_contracts/runtime.py" 'class RuntimeImageViewProfile'
  require_release_token "backend/packages/cv-datasets/src/cv_datasets/conversion.py" 'def _dataset_image_view_metadata'
  require_release_token "backend/services/realtime-executor/src/realtime_executor/live.py" '_stem_shadow_mode_enabled'
  require_release_token "backend/packages/cv-db/src/cv_db/stem_shadow_store.py" 'def stem_shadow_evaluation_response'
  require_release_token "backend/services/realtime-executor/src/realtime_executor/live.py" 'analysis.stem_orientation_model_decision'
  require_release_token "backend/packages/cv-training/src/cv_training/backends/temporal_sequence.py" 'class TemporalSequenceTrainer'
  require_release_token "backend/migrations/versions/20260803_0035_stem_orientation_reviews.py" 'revision = "20260803_0035"'
  require_release_token "frontend/packages/schemas/src/index.ts" "'temporal_sequence'"
  require_release_token "frontend/packages/schemas/src/index.ts" 'export interface LabelProjectProvisionRequest'
  require_release_token "contracts/openapi/dataset-service.v1.yaml" '/api/v1/label-projects:provision:'
  require_release_token "contracts/openapi/result-service.v1.yaml" 'stem-orientation-reviews'
  require_release_token "contracts/openapi/result-service.v1.yaml" 'stem-shadow-evaluation'
  require_release_token "docs/operations/stem-curvature-minimum-production-runbook.md" '# Curvature-Minimum'
  log "Temporal-Sequence-, Minimumprinzip-, Runtime-, Migrations- und Frontend-Vertraege des Release-Kandidaten validiert."
}

import_edge_runtime_packages() {
  local manifest
  manifest="$(read_bundle_manifest_value EdgeRuntimeTargets '')"
  if [[ -z "$manifest" ]]; then
    # Older bundles retain their manual package import workflow.
    return 0
  fi
  if [[ "$manifest" != "edge-runtime/targets.json" || ! -f "$BUNDLE_DIR/$manifest" ]]; then
    log "FEHLER: Mitgelieferte Edge-Zuordnung fehlt oder hat einen ungueltigen Pfad."
    return 1
  fi
  log "Pruefe und importiere Edge-Runtime-Pakete ohne Internetzugriff."
  (
    # Use the same production storage and trust configuration as the running services.
    # The import still loads code/config from the candidate, before current is switched.
    set -a
    . "$ENV_FILE"
    set +a
    PYTHONPATH="$(build_pythonpath "$RELEASE_DIR")" "$PYTHON_BIN" -m runtime_admin_api.edge_software \
      --bundle-directory "$BUNDLE_DIR/edge-runtime" \
      --release-root "$RELEASE_DIR" --central-release-id "$APP_VERSION"
  )
}

validate_service_runtime_imports() {
  if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
    log "FEHLER: Keine Python-Runtime fuer den Service-Importtest gefunden."
    return 1
  fi
  if [[ -z "$RELEASE_DIR" || ! -d "$RELEASE_DIR" ]]; then
    log "FEHLER: Release-Kandidat fuer den Service-Importtest fehlt."
    return 1
  fi

  log "Pruefe Service-Runtime-Imports des Release-Kandidaten."
  PYTHONPATH="$(build_pythonpath "$RELEASE_DIR")" "$PYTHON_BIN" - <<'PY'
import cv2
import numpy
import cv_edge
import runtime_admin_api.main
from cv_datasets.annotation_schemas import get_annotation_schema
from cv_runtime.image_view import (
    apply_image_view,
    calculate_image_view_profile,
    restore_view_mask,
    view_overlay_geometry_to_source,
)
from cv_runtime.registry import builtin_node_types
from cv_status import TaskType
from cv_training.catalog import default_promotion_criteria
from cv_training.trainer import get_default_trainer

schema = get_annotation_schema("stem_orientation_angle.v1")
label_config = schema.label_studio_config()
if '<Video name="video"' not in label_config or "<TimelineLabels" not in label_config:
    raise SystemExit("Temporal Label-Studio-Video-/Timeline-Konfiguration fehlt.")

node_types = {item.node_type for item in builtin_node_types()}
required_node_types = {
    "inference.temporal_sequence",
    "analysis.stem_temporal_orientation",
    "analysis.stem_orientation_model_decision",
    "preprocessing.camera_undistort",
    "preprocessing.process_coordinate_rectify",
    "analysis.stem_measurement_eligibility",
    "analysis.active_stem_tracker",
    "analysis.stem_horizontal_curvature",
    "analysis.stem_surface_phase",
    "analysis.stem_curvature_phase_solver",
    "output.siemens_sps_write",
}
missing_node_types = sorted(required_node_types - node_types)
if missing_node_types:
    raise SystemExit(
        "Temporal Runtime-Nodes fehlen: " + ", ".join(missing_node_types)
    )

trainer = get_default_trainer(TaskType.TEMPORAL_SEQUENCE)
if trainer.trainer_backend != "pytorch-temporal-sequence":
    raise SystemExit("Temporal Trainer-Backend ist nicht registriert.")
if default_promotion_criteria(TaskType.TEMPORAL_SEQUENCE):
    raise SystemExit("Temporal Modelle duerfen nicht automatisch promotet werden.")

print("release-candidate-service-imports-ok")
print("release-candidate-temporal-contracts-ok")
PY
}

extract_release() {
  if [[ ! -f "$APP_ARCHIVE" ]]; then
    echo "app.tar.gz not found in bundle: $APP_ARCHIVE" >&2
    exit 1
  fi

  mkdir -p "$RELEASES_DIR"
  local release_dir="$RELEASES_DIR/$APP_VERSION"
  local tmp_dir="$RELEASES_DIR/.tmp_${APP_VERSION}_$$"
  rm -rf "$tmp_dir"
  mkdir -p "$tmp_dir"

  log "Entpacke Release $APP_VERSION nach $release_dir."
  tar -xzf "$APP_ARCHIVE" -C "$tmp_dir"
  rm -rf "$release_dir"
  mv "$tmp_dir" "$release_dir"
  chmod 755 "$APP_ROOT" "$RELEASES_DIR" "$release_dir"
  find "$release_dir" -type d -exec chmod 755 {} +
  if [[ -d "$release_dir/frontend/apps/admin-web/dist" ]]; then
    find "$release_dir/frontend/apps/admin-web/dist" -type f -exec chmod 644 {} +
  fi
  RELEASE_DIR="$release_dir"
}

validate_release_frontend_assets() {
  local dist_dir="$RELEASE_DIR/frontend/apps/admin-web/dist"
  local index_path="$dist_dir/index.html"
  local asset_path relative_path
  local -a asset_paths=()

  if [[ ! -f "$index_path" ]]; then
    log "FEHLER: Release-Kandidat enthaelt keine Admin-Frontend-index.html: $index_path"
    return 1
  fi

  mapfile -t asset_paths < <(
    grep -oE '/frontend/assets/[^"]+\.(css|js)' "$index_path" |
      sort -u || true
  )
  if ((${#asset_paths[@]} == 0)); then
    log "FEHLER: Release-Kandidat wurde nicht fuer /frontend/ gebaut. index.html muss CSS/JS unter /frontend/assets/ referenzieren."
    return 1
  fi

  for asset_path in "${asset_paths[@]}"; do
    relative_path="${asset_path#/frontend/}"
    if [[ ! -f "$dist_dir/$relative_path" ]]; then
      log "FEHLER: Referenziertes Admin-Frontend-Asset fehlt im Release-Kandidaten: $relative_path"
      return 1
    fi
  done
  log "Admin-Frontend-Release-Kandidat validiert: ${#asset_paths[@]} Asset(s) unter /frontend/assets/."
}

activate_release() {
  if [[ -z "$RELEASE_DIR" || ! -d "$RELEASE_DIR" ]]; then
    echo "Release kann nicht aktiviert werden: Kandidat fehlt." >&2
    return 1
  fi
  ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"
  printf '%s\n' "$APP_VERSION" >"$APP_ROOT/VERSION"
}

patch_release_config() {
  local host_name="$1"
  local python_bin="$2"
  "$python_bin" - "$RELEASE_DIR/config/prod/platform.yaml" "$host_name" <<'PY' || true
from __future__ import annotations

import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
host_name = sys.argv[2]
data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
data.setdefault("service", {})["public_base_url"] = f"https://{host_name}"
data.setdefault("cors", {})["allowed_origins"] = [f"https://{host_name}", f"http://{host_name}"]
data.setdefault("label_studio", {})["local_files_root"] = "/srv/cv-platform"
data.setdefault("label_studio", {})["local_files_url_prefix"] = "/data/local-files/?d="
data.setdefault("runtime", {})["inference_gateway_bearer_token_file"] = "/run/secrets/internal_inference_gateway_bearer_token"
api_keys = data.setdefault("api_keys", {})
api_keys["enabled"] = True
bootstrap_keys = api_keys.setdefault("bootstrap_keys", [])
if not isinstance(bootstrap_keys, list):
    bootstrap_keys = []
api_keys["bootstrap_keys"] = [
    item for item in bootstrap_keys if not isinstance(item, dict) or item.get("key_id") != "ybb-inference-runtime"
]
api_keys["bootstrap_keys"].append(
    {
        "key_id": "ybb-inference-runtime",
        "label": "YBB internal inference runtime",
        "description": "Bootstrap credential for runtime services calling the local inference gateway.",
        "intended_usage": "Service-to-service inference calls inside the local WSL runtime.",
        "subject_type": "service",
        "subject_id": "runtime-inference-client",
        "scopes": ["realtime.predict", "batch.submit"],
        "secret_file": "/run/secrets/internal_inference_gateway_api_key_secret",
        "endpoint_mode_hint": "both",
    }
)
data.setdefault("storage", {})["state_root"] = "/srv/cv-platform"
data["storage"]["training_root"] = "/srv/cv-platform/training"
data["storage"]["serving_root"] = "/srv/cv-platform/serving"
data["storage"]["audit_root"] = "/srv/cv-platform/audit"
data["storage"]["logs_root"] = "/srv/cv-platform/logs"
data.setdefault("training", {})["allow_onnx_deployment_fallback"] = False
path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY
}

write_runtime_env() {
  local host_name="$1"
  local python_bin="$2"
  local training_python_bin="$3"
  local label_studio_bin="$4"
  local mlflow_bin="$5"
  local triton_bin="$6"
  local trtexec_bin="$7"
  local pythonpath
  pythonpath="$(build_pythonpath)"
  local stream_rtsp_port stream_webrtc_port stream_webrtc_ice_tcp_port stream_auth_enabled stream_access_policy plc_writes_allowed stream_media_host mediamtx_api_url
  local effective_wsl_networking_mode
  stream_rtsp_port="$(read_bundle_manifest_value StreamingRtspPort 8554)"
  stream_webrtc_port="$(read_bundle_manifest_value StreamingWebRtcPort 8889)"
  stream_webrtc_ice_tcp_port="$(read_bundle_manifest_value StreamingWebRtcIceTcpPort 8189)"
  stream_auth_enabled="$(read_bundle_manifest_value StreamAuthEnabled false)"
  plc_writes_allowed="$(read_bundle_manifest_value PlcWritesAllowed false)"
  effective_wsl_networking_mode="$(wsl_networking_mode)"
  stream_media_host="$(mediamtx_host_for_wsl)"
  if [[ -z "$stream_media_host" ]]; then
    stream_media_host="127.0.0.1"
  fi
  mediamtx_api_url="$(mediamtx_api_url_for_wsl "$stream_media_host")"
  if [[ -z "$mediamtx_api_url" ]]; then
    mediamtx_api_url="http://${stream_media_host}:9997"
  fi
  stream_access_policy="unauthenticated_lan"
  if [[ "$stream_auth_enabled" == "true" ]]; then
    stream_access_policy="token"
  fi

  mkdir -p "$CONFIG_DIR"
cat >"$INSTALL_ENV_FILE" <<EOF
APP_HOST_NAME=$host_name
APP_NAME=$APP_NAME
YBB_WSL_NETWORKING_MODE=$effective_wsl_networking_mode
EOF

  cat >"$ENV_FILE" <<EOF
APP_NAME=$APP_NAME
APP_VERSION=$APP_VERSION
APP_HOST_NAME=$host_name
YBB_WSL_NETWORKING_MODE=$effective_wsl_networking_mode
YBB_MEDIAMTX_HOST_PLATFORM=$(mediamtx_host_platform)
APP_ROOT=$APP_ROOT
STATE_ROOT=$STATE_ROOT
ENV=prod
PYTHONPATH=$pythonpath
YBB_PYTHON_BIN=$python_bin
YBB_TRAINING_PYTHON_BIN=$training_python_bin
YBB_LABEL_STUDIO_BIN=$label_studio_bin
YBB_MLFLOW_BIN=$mlflow_bin
TRITONSERVER_BIN=$triton_bin
CV_PLATFORM_TRTEXEC_BIN=$trtexec_bin
MLFLOW_TRACKING_URI=http://127.0.0.1:5000
CV_PLATFORM__SERVICE__PUBLIC_BASE_URL=https://$host_name
CV_PLATFORM__DATABASE__HOST=127.0.0.1
CV_PLATFORM__DATABASE__PORT=5432
CV_PLATFORM__DATABASE__DATABASE=cv_platform
CV_PLATFORM__DATABASE__USER=cv_platform
CV_PLATFORM__DATABASE__PASSWORD_FILE=/run/secrets/postgres_app_password
CV_PLATFORM__REDIS__HOST=127.0.0.1
CV_PLATFORM__REDIS__PORT=6379
CV_PLATFORM__STORAGE__STATE_ROOT=/srv/cv-platform
CV_PLATFORM__STORAGE__TRAINING_ROOT=/srv/cv-platform/training
CV_PLATFORM__STORAGE__SERVING_ROOT=/srv/cv-platform/serving
CV_PLATFORM__STORAGE__AUDIT_ROOT=/srv/cv-platform/audit
CV_PLATFORM__STORAGE__LOGS_ROOT=/srv/cv-platform/logs
CV_PLATFORM_PRETRAINED_CHECKPOINT_ROOT=/srv/cv-platform/control/pretrained-checkpoints
CV_PLATFORM__LABEL_STUDIO__BASE_URL=http://127.0.0.1:8100
CV_PLATFORM__LABEL_STUDIO__ML_BACKEND_ADAPTER_URL=http://127.0.0.1:8089
CV_PLATFORM__LABEL_STUDIO__LOCAL_FILES_ROOT=/srv/cv-platform
CV_PLATFORM__LABEL_STUDIO__LOCAL_FILES_URL_PREFIX=/data/local-files/?d=
CV_PLATFORM__MLFLOW__BASE_URL=http://127.0.0.1:5000
CV_PLATFORM__MLFLOW__TRACKING_URI=http://127.0.0.1:5000
CV_PLATFORM__DVC__REPO_ROOT=/srv/cv-platform/control/dvc/repo
CV_PLATFORM__DVC__REMOTE_URL=/srv/cv-platform/control/dvc/remote
CV_PLATFORM__TRAINING__ALLOW_ONNX_DEPLOYMENT_FALLBACK=false
CV_PLATFORM__TRITON__REALTIME__HTTP_URL=http://127.0.0.1:8000
CV_PLATFORM__TRITON__REALTIME__GRPC_URL=127.0.0.1:8001
CV_PLATFORM__TRITON__BATCH__HTTP_URL=http://127.0.0.1:8010
CV_PLATFORM__TRITON__BATCH__GRPC_URL=127.0.0.1:8011
CV_PLATFORM__RUNTIME__INFERENCE_GATEWAY_REALTIME_URL=http://127.0.0.1:8087
CV_PLATFORM__RUNTIME__INFERENCE_GATEWAY_BEARER_TOKEN_FILE=/run/secrets/internal_inference_gateway_bearer_token
CV_PLATFORM__RUNTIME__PLC_WRITES_ALLOWED=$plc_writes_allowed
CV_PLATFORM__RUNTIME__STREAM_AUTH_ENABLED=$stream_auth_enabled
CV_PLATFORM__RUNTIME__STREAM_ACCESS_POLICY=$stream_access_policy
CV_PLATFORM__RUNTIME__STREAM_MEDIAMTX_API_URL=$mediamtx_api_url
CV_PLATFORM__RUNTIME__STREAM_PUBLIC_HOST=$host_name
CV_PLATFORM__RUNTIME__STREAM_RTSP_PORT=$stream_rtsp_port
CV_PLATFORM__RUNTIME__STREAM_WEBRTC_PORT=$stream_webrtc_port
CV_PLATFORM__RUNTIME__STREAM_WEBRTC_ICE_TCP_PORT=$stream_webrtc_ice_tcp_port
CV_PLATFORM__RUNTIME__STREAM_RTSP_PUBLISH_HOST=$stream_media_host
CV_PLATFORM_SERVICE_HEALTH_HOST=127.0.0.1
CV_PLATFORM_SERVICE_HEALTH_EXCLUDE=operations-agent
OPENCV_FFMPEG_CAPTURE_OPTIONS='rtsp_transport;tcp'
EOF
  chmod 600 "$ENV_FILE" "$INSTALL_ENV_FILE"
}

install_helpers() {
  install -m 755 "$CURRENT_LINK/deployment/wsl/aravis-rtsp-bridge.py" \
    /usr/local/bin/ybb-aravis-rtsp-bridge
  install -m 755 "$CURRENT_LINK/deployment/wsl/storage-maintenance.sh" \
    /usr/local/bin/ybb-storage-maintenance
  install -m 755 "$CURRENT_LINK/deployment/wsl/clear-result-history.sh" \
    /usr/local/sbin/ybb-clear-result-history

  cat >/usr/local/bin/ybb-python-service <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. /etc/ybb-inferenzserver/runtime.env
set +a
export OPENCV_FFMPEG_CAPTURE_OPTIONS="${OPENCV_FFMPEG_CAPTURE_OPTIONS:-rtsp_transport;tcp}"

# NumPy/OpenBLAS and OpenCV may otherwise each start a native worker per CPU
# core.  Small realtime preprocessing and segmentation postprocessing kernels
# become slower under that oversubscription and can consume the entire host.
# Keep this local to inference services so training and Triton retain their own
# independently tuned CPU/GPU parallelism.
case "${1:-}" in
  inference_gateway.*)
    inference_native_threads="${YBB_INFERENCE_GATEWAY_NATIVE_THREADS:-1}"
    if [[ ! "$inference_native_threads" =~ ^[1-9][0-9]*$ ]]; then
      echo "YBB_INFERENCE_GATEWAY_NATIVE_THREADS must be a positive integer." >&2
      exit 64
    fi
    export OPENBLAS_NUM_THREADS="$inference_native_threads"
    export GOTO_NUM_THREADS="$inference_native_threads"
    export BLIS_NUM_THREADS="$inference_native_threads"
    export OMP_NUM_THREADS="$inference_native_threads"
    export MKL_NUM_THREADS="$inference_native_threads"
    export NUMEXPR_NUM_THREADS="$inference_native_threads"
    export OPENCV_FOR_THREADS_NUM="$inference_native_threads"
    export OMP_WAIT_POLICY="${YBB_INFERENCE_GATEWAY_OMP_WAIT_POLICY:-PASSIVE}"
    export KMP_BLOCKTIME="${YBB_INFERENCE_GATEWAY_KMP_BLOCKTIME:-0}"
    ;;
esac

python_site_packages="$("$YBB_PYTHON_BIN" - <<'PY'
import site

paths = site.getsitepackages()
print(paths[0] if paths else "")
PY
)"
library_paths=()
for candidate in \
  "$python_site_packages/torch/lib" \
  "$python_site_packages"/nvidia/*/lib \
  /usr/lib/wsl/lib \
  /usr/local/cuda/lib64 \
  /usr/local/cuda/compat \
  /usr/lib/x86_64-linux-gnu
do
  if [[ -d "$candidate" ]]; then
    library_paths+=("$candidate")
  fi
done
if [[ "${#library_paths[@]}" -gt 0 ]]; then
  service_library_path="$(IFS=:; echo "${library_paths[*]}")"
  export LD_LIBRARY_PATH="$service_library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
cd /opt/ybb-inferenzserver/current
exec "$YBB_PYTHON_BIN" -m uvicorn "$1" --host 127.0.0.1 --port "$2"
EOF
  chmod 755 /usr/local/bin/ybb-python-service

  cat >/usr/local/bin/ybb-python-worker <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. /etc/ybb-inferenzserver/runtime.env
set +a
cd /opt/ybb-inferenzserver/current
module="$1"
python_bin="$YBB_PYTHON_BIN"
needs_training_runtime=false
case "$module" in
  control_worker.worker|training_worker.worker)
    needs_training_runtime=true
    ;;
esac
if [[ "$needs_training_runtime" == "true" && -n "${YBB_TRAINING_PYTHON_BIN:-}" ]]; then
  python_bin="$YBB_TRAINING_PYTHON_BIN"
fi
python_bin_dir="$(dirname "$python_bin")"
export PATH="$python_bin_dir:$PATH"
if [[ "$needs_training_runtime" == "true" ]]; then
  python_site_packages="$("$python_bin" - <<'PY'
import site

paths = site.getsitepackages()
print(paths[0] if paths else "")
PY
)"
  cuda_library_paths=()
  for candidate in \
    "$python_site_packages/torch/lib" \
    "$python_site_packages"/nvidia/*/lib \
    /opt/tritonserver/external-libs \
    /opt/tritonserver/lib \
    /opt/tritonserver/backends/tensorrt \
    /opt/tritonserver/backends/onnxruntime \
    /usr/lib/wsl/lib \
    /usr/local/cuda/lib64 \
    /usr/local/cuda/compat \
    /usr/lib/x86_64-linux-gnu
  do
    if [[ -d "$candidate" ]]; then
      cuda_library_paths+=("$candidate")
    fi
  done
  if [[ "${#cuda_library_paths[@]}" -gt 0 ]]; then
    cuda_library_path="$(IFS=:; echo "${cuda_library_paths[*]}")"
    export LD_LIBRARY_PATH="$cuda_library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
fi
exec "$python_bin" -m "$module"
EOF
  chmod 755 /usr/local/bin/ybb-python-worker

  cat >/usr/local/bin/ybb-triton-server <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. /etc/ybb-inferenzserver/runtime.env
set +a

library_paths=()
for candidate in \
  /opt/tritonserver/external-libs \
  /opt/tritonserver/lib \
  /opt/tritonserver/backends/onnxruntime \
  /usr/lib/wsl/lib \
  /usr/local/cuda/lib64 \
  /usr/local/cuda/compat \
  /usr/lib/x86_64-linux-gnu \
  /opt/ybb-runtime/venv/lib/python*/site-packages/torch/lib \
  /opt/ybb-runtime/venv/lib/python*/site-packages/nvidia/*/lib \
  /opt/ybb-runtime/training-venv/lib/python*/site-packages/torch/lib \
  /opt/ybb-runtime/training-venv/lib/python*/site-packages/nvidia/*/lib
do
  if [[ -d "$candidate" ]]; then
    library_paths+=("$candidate")
  fi
done
if [[ "${#library_paths[@]}" -gt 0 ]]; then
  triton_library_path="$(IFS=:; echo "${library_paths[*]}")"
  export LD_LIBRARY_PATH="$triton_library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

exec "$TRITONSERVER_BIN" "$@"
EOF
  chmod 755 /usr/local/bin/ybb-triton-server

  cat >/usr/local/bin/ybb-label-studio-service <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. /etc/ybb-inferenzserver/runtime.env
set +a

public_origin="https://${APP_HOST_NAME}"
public_label_studio_path="/label-studio"
public_label_studio_url="${public_origin}${public_label_studio_path}"

export HOST="$public_label_studio_url"
export LABEL_STUDIO_HOST="$public_label_studio_url"
export FRONTEND_HOSTNAME="$public_label_studio_url"
export DOMAIN_FROM_REQUEST=False
export CSRF_TRUSTED_ORIGINS="$public_origin,http://${APP_HOST_NAME},https://127.0.0.1,http://127.0.0.1,https://localhost,http://localhost"
export LABEL_STUDIO_PORT=8100
export BASE_DATA_DIR=/srv/cv-platform/labelstudio
export LABEL_STUDIO_BASE_DATA_DIR=/srv/cv-platform/labelstudio
export LOCAL_FILES_SERVING_ENABLED=true
export LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED=true
export LOCAL_FILES_DOCUMENT_ROOT=/srv/cv-platform
export LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT=/srv/cv-platform
export DJANGO_DB=default
export POSTGRE_NAME=labelstudio
export POSTGRE_USER=postgres
export POSTGRE_PASSWORD="$(cat /run/secrets/postgres_app_password)"
export POSTGRE_HOST=127.0.0.1
export POSTGRE_PORT=5432
export COLLECT_ANALYTICS=False
export LATEST_VERSION_CHECK=False
export SENTRY_DSN=
export FRONTEND_SENTRY_DSN=

label_studio_python="${YBB_LABEL_STUDIO_BIN%/label-studio}/python"
if [[ ! -x "$label_studio_python" ]]; then
  label_studio_python="${YBB_PYTHON_BIN:-python3}"
fi
"$label_studio_python" - <<'PY'
from importlib.util import find_spec
from pathlib import Path

spec = find_spec("label_studio")
if spec and spec.submodule_search_locations:
    package_root = Path(next(iter(spec.submodule_search_locations)))
    settings_path = package_root / "core" / "settings" / "base.py"
    if settings_path.exists():
        text = settings_path.read_text(encoding="utf-8")
        patched = text.replace("LATEST_VERSION_CHECK = True", "LATEST_VERSION_CHECK = False")
        original_host_block = """HOSTNAME = get_env('HOST', '')
if HOSTNAME:
    if not HOSTNAME.startswith('http://') and not HOSTNAME.startswith('https://'):
        logger.info(
            '! HOST variable found in environment, but it must start with http:// or https://, ignore it: %s', HOSTNAME
        )
        HOSTNAME = ''
    else:
        logger.info('=> Hostname correctly is set to: %s', HOSTNAME)
        if HOSTNAME.endswith('/'):
            HOSTNAME = HOSTNAME[0:-1]

        # for django url resolver
        if HOSTNAME:
            # http[s]://domain.com:8080/script_name => /script_name
            pattern = re.compile(r'^http[s]?:\\/\\/([^:\\/\\s]+(:\\d*)?)(.*)?')
            match = pattern.match(HOSTNAME)
            FORCE_SCRIPT_NAME = match.group(3)
            if FORCE_SCRIPT_NAME:
                logger.info('=> Django URL prefix is set to: %s', FORCE_SCRIPT_NAME)
"""
        patched_host_block = """HOSTNAME = get_env('HOST', '')
if HOSTNAME:
    if HOSTNAME.startswith('/'):
        if HOSTNAME.endswith('/') and HOSTNAME != '/':
            HOSTNAME = HOSTNAME[0:-1]
        logger.info('=> Hostname path prefix is set to: %s', HOSTNAME)
        FORCE_SCRIPT_NAME = HOSTNAME
    elif not HOSTNAME.startswith('http://') and not HOSTNAME.startswith('https://'):
        logger.info(
            '! HOST variable found in environment, but it must start with http://, https:// or /, ignore it: %s',
            HOSTNAME,
        )
        HOSTNAME = ''
    else:
        logger.info('=> Hostname correctly is set to: %s', HOSTNAME)
        if HOSTNAME.endswith('/'):
            HOSTNAME = HOSTNAME[0:-1]

        # for django url resolver
        if HOSTNAME:
            # http[s]://domain.com:8080/script_name => /script_name
            pattern = re.compile(r'^http[s]?:\\/\\/([^:\\/\\s]+(:\\d*)?)(.*)?')
            match = pattern.match(HOSTNAME)
            FORCE_SCRIPT_NAME = match.group(3)
            if FORCE_SCRIPT_NAME:
                logger.info('=> Django URL prefix is set to: %s', FORCE_SCRIPT_NAME)
"""
        patched = patched.replace(original_host_block, patched_host_block)
        if patched != text:
            settings_path.write_text(patched, encoding="utf-8")
PY

exec "$YBB_LABEL_STUDIO_BIN" start --port 8100 --no-browser
EOF
  chmod 755 /usr/local/bin/ybb-label-studio-service

  cat >/usr/local/bin/ybb-rtsp-probe <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: ybb-rtsp-probe <rtsp-url> [timeout-seconds]" >&2
  exit 2
fi

set -a
. /etc/ybb-inferenzserver/runtime.env
set +a
export OPENCV_FFMPEG_CAPTURE_OPTIONS="${OPENCV_FFMPEG_CAPTURE_OPTIONS:-rtsp_transport;tcp}"

"$YBB_PYTHON_BIN" - "$@" <<'PY'
from __future__ import annotations

import os
import sys
import time
from urllib.parse import urlsplit, urlunsplit


def _redact(url: str) -> str:
    parsed = urlsplit(url)
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = "***:***@" + netloc.rsplit("@", 1)[1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


if len(sys.argv) < 2:
    print("Usage: ybb-rtsp-probe <rtsp-url> [timeout-seconds]", file=sys.stderr)
    raise SystemExit(2)

url = sys.argv[1]
try:
    timeout_seconds = max(float(sys.argv[2]), 1.0) if len(sys.argv) > 2 else 10.0
except ValueError:
    print("FEHLER: timeout-seconds muss eine Zahl sein.", file=sys.stderr)
    raise SystemExit(2)

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp",
)

try:
    import cv2  # type: ignore[import-not-found]
except Exception as exc:
    print(f"FEHLER: OpenCV konnte nicht geladen werden: {exc}", file=sys.stderr)
    raise SystemExit(1)

capture = cv2.VideoCapture()
timeout_msec = int(timeout_seconds * 1000)
for prop_name, value in (
    ("CAP_PROP_BUFFERSIZE", 1),
    ("CAP_PROP_OPEN_TIMEOUT_MSEC", timeout_msec),
    ("CAP_PROP_READ_TIMEOUT_MSEC", timeout_msec),
):
    prop = getattr(cv2, prop_name, None)
    if prop is not None:
        try:
            capture.set(prop, value)
        except Exception:
            pass

backend = getattr(cv2, "CAP_FFMPEG", None)
try:
    opened = capture.open(url, backend) if backend is not None else capture.open(url)
except Exception as exc:
    print(f"FEHLER: RTSP-Quelle konnte nicht geoeffnet werden: {exc}", file=sys.stderr)
    print(f"Quelle: {_redact(url)}", file=sys.stderr)
    print(
        f"OpenCV-FFmpeg-Optionen: {os.environ.get('OPENCV_FFMPEG_CAPTURE_OPTIONS', '<unset>')}",
        file=sys.stderr,
    )
    raise SystemExit(1)

if not opened or not capture.isOpened():
    print("FEHLER: RTSP-Quelle konnte aus der WSL-Runtime nicht geoeffnet werden.", file=sys.stderr)
    print(f"Quelle: {_redact(url)}", file=sys.stderr)
    print(
        f"OpenCV-FFmpeg-Optionen: {os.environ.get('OPENCV_FFMPEG_CAPTURE_OPTIONS', '<unset>')}",
        file=sys.stderr,
    )
    raise SystemExit(1)

deadline = time.monotonic() + timeout_seconds
while time.monotonic() < deadline:
    ok, frame = capture.read()
    if ok and frame is not None:
        height = int(frame.shape[0]) if getattr(frame, "shape", None) is not None else 0
        width = int(frame.shape[1]) if getattr(frame, "shape", None) is not None else 0
        print(f"OK: RTSP-Frame empfangen ({width}x{height}) von {_redact(url)}")
        raise SystemExit(0)
    time.sleep(0.1)

print("FEHLER: RTSP-Quelle wurde geoeffnet, aber es kam kein Frame an.", file=sys.stderr)
print(f"Quelle: {_redact(url)}", file=sys.stderr)
raise SystemExit(1)
PY
EOF
  chmod 755 /usr/local/bin/ybb-rtsp-probe

  cat >/usr/local/bin/ybb-start-platform <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

start_systemd_units() {
  if ! systemctl start "$@"; then
    echo "WARNUNG: Mindestens eine systemd-Unit konnte nicht gestartet werden: $*" >&2
    systemctl --no-pager --failed || true
  fi
}

start_systemd_units ybb-secrets.service postgresql.service redis-server.service
start_systemd_units ybb-platform.target

mapfile -t ybb_units < <(
  systemctl list-unit-files 'ybb-*.service' --no-legend --no-pager |
    awk '$2 != "masked" && $1 !~ /@\.service$/ { print $1 }'
)
if ((${#ybb_units[@]} > 0)); then
  start_systemd_units "${ybb_units[@]}"
fi

start_systemd_units caddy
EOF
  chmod 755 /usr/local/bin/ybb-start-platform

  cat >/usr/local/bin/ybb-keepalive <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if command -v flock >/dev/null 2>&1; then
  exec 9>/run/ybb-platform.keepalive.lock
  if ! flock -n 9; then
    /usr/local/bin/ybb-start-platform || true
    trap 'exit 0' INT TERM
    while true; do
      sleep 3600 &
      wait $!
    done
  fi
fi

/usr/local/bin/ybb-start-platform || true
trap 'exit 0' INT TERM
while true; do
  sleep 3600 &
  wait $!
done
EOF
  chmod 755 /usr/local/bin/ybb-keepalive
}

write_mediamtx_config() {
  local stream_rtsp_port stream_webrtc_port stream_webrtc_ice_tcp_port stream_turn_tcp_port stream_auth_enabled stream_public_host
  stream_rtsp_port="$(read_bundle_manifest_value StreamingRtspPort 8554)"
  stream_webrtc_port="$(read_bundle_manifest_value StreamingWebRtcPort 8889)"
  stream_webrtc_ice_tcp_port="$(read_bundle_manifest_value StreamingWebRtcIceTcpPort 8189)"
  stream_turn_tcp_port="$(read_bundle_manifest_value StreamingTurnTcpPort 3478)"
  stream_auth_enabled="$(read_bundle_manifest_value StreamAuthEnabled false)"
  stream_public_host="$(public_authority_host "${HOST_NAME:-127.0.0.1}")"
  if [[ "$stream_auth_enabled" == "true" ]]; then
    log "FEHLER: StreamAuthEnabled=true ist im aktuellen Offline-Streaming-Bundle nicht unterstuetzt. Bitte StreamAuthEnabled=false belassen, bis explizite MediaMTX-Stream-Credentials konfiguriert werden."
    return 1
  fi

  mkdir -p /etc/mediamtx
  cat >/etc/mediamtx/mediamtx.yml <<EOF
# Generated by YBB offline updater. Streams are LAN-open by default.
logLevel: info
api: yes
apiAddress: 127.0.0.1:9997
metrics: yes
metricsAddress: 127.0.0.1:9998
rtspTransports: [tcp]
rtspAddress: :$stream_rtsp_port
hls: no
webrtcAddress: :$stream_webrtc_port
webrtcLocalUDPAddress: :$stream_webrtc_ice_tcp_port
webrtcLocalTCPAddress: :$stream_webrtc_ice_tcp_port
webrtcIPsFromInterfaces: false
webrtcAdditionalHosts:
  - "$stream_public_host"
webrtcICEServers2:
  - url: "turn:${stream_public_host}:${stream_turn_tcp_port}?transport=tcp"
    username: AUTH_SECRET
    password: "$TURN_AUTH_SECRET"
    clientOnly: true
authMethod: internal
paths:
  all_others:
    source: publisher
EOF
  log "MediaMTX Streams sind standardmaessig ohne Autorisierung im LAN erreichbar."
}

write_service_unit() {
  local unit_name="$1"
  local description="$2"
  local exec_start="$3"
  local after="${4:-network.target ybb-secrets.service postgresql.service redis-server.service}"
  local wants="${5:-ybb-secrets.service postgresql.service redis-server.service}"

  cat >"/etc/systemd/system/$unit_name.service" <<EOF
[Unit]
Description=$description
After=$after
Wants=$wants

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
WorkingDirectory=$CURRENT_LINK
ExecStart=$exec_start
Restart=always
RestartSec=5

[Install]
WantedBy=ybb-platform.target
EOF
}

write_turnserver_config() {
  local turn_tcp_port
  turn_tcp_port="$(read_bundle_manifest_value StreamingTurnTcpPort 3478)"
  if ! command -v turnserver >/dev/null 2>&1; then
    log "FEHLER: coturn/turnserver ist nicht installiert."
    return 1
  fi
  if [[ -z "$TURN_AUTH_SECRET" ]]; then
    log "FEHLER: Das TURN-Shared-Secret wurde vom Windows-Installer nicht uebergeben."
    return 1
  fi

  install -d -m 0750 /etc/ybb-inferenzserver
  cat >"$CONFIG_DIR/turnserver.conf" <<EOF
# Generated by YBB offline updater. Browser transport is TURN over TCP;
# the relay leg to the local Windows MediaMTX ICE listener remains UDP.
listening-port=$turn_tcp_port
listening-ip=0.0.0.0
no-udp
no-tls
no-dtls
no-tcp-relay
fingerprint
use-auth-secret
static-auth-secret=$TURN_AUTH_SECRET
realm=ybb-inferenzserver
min-port=49152
max-port=49251
no-cli
no-multicast-peers
stale-nonce
simple-log
log-file=stdout
EOF
  chmod 0600 "$CONFIG_DIR/turnserver.conf"

  # The coturn Debian package can enable its own service. Stop and mask each
  # possible distribution unit separately: a missing alias must not prevent
  # the real coturn.service from being disabled.
  systemctl disable --now coturn.service >/dev/null 2>&1 || true
  systemctl mask coturn.service >/dev/null 2>&1 || true
  systemctl disable --now turnserver.service >/dev/null 2>&1 || true
  systemctl mask turnserver.service >/dev/null 2>&1 || true
  cat >/etc/systemd/system/ybb-turnserver.service <<EOF
[Unit]
Description=YBB WebRTC TURN over TCP
After=network.target
Wants=network.target
Conflicts=coturn.service turnserver.service

[Service]
Type=simple
ExecStart=$(command -v turnserver) -c $CONFIG_DIR/turnserver.conf
Restart=always
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=ybb-platform.target
EOF
}

install_systemd_units() {
  install_helpers
  write_turnserver_config
  if mediamtx_runs_in_wsl; then
    write_mediamtx_config
  fi

  local inference_rt_after="network.target ybb-secrets.service"
  local inference_rt_wants="ybb-secrets.service"
  local inference_batch_after="network.target ybb-secrets.service"
  local inference_batch_wants="ybb-secrets.service"
  local label_adapter_after="network.target ybb-secrets.service postgresql.service"
  local label_adapter_wants="ybb-secrets.service postgresql.service"
  if triton_available; then
    inference_rt_after="$inference_rt_after ybb-triton-rt.service"
    inference_rt_wants="$inference_rt_wants ybb-triton-rt.service"
    inference_batch_after="$inference_batch_after ybb-triton-batch.service"
    inference_batch_wants="$inference_batch_wants ybb-triton-batch.service"
    label_adapter_after="$label_adapter_after ybb-triton-rt.service"
    label_adapter_wants="$label_adapter_wants ybb-triton-rt.service"
  fi

  write_service_unit "ybb-admin-api" "YBB admin-api" "/usr/local/bin/ybb-python-service admin_api.main:app 8080"
  write_service_unit "ybb-dataset-service" "YBB dataset-service" "/usr/local/bin/ybb-python-service dataset_service.main:app 8082"
  write_service_unit "ybb-training-orchestrator" "YBB training-orchestrator" "/usr/local/bin/ybb-python-service training_orchestrator.main:app 8083"
  write_service_unit "ybb-model-registry-service" "YBB model-registry-service" "/usr/local/bin/ybb-python-service model_registry_service.main:app 8084"
  write_service_unit "ybb-deployment-controller" "YBB deployment-controller" "/usr/local/bin/ybb-python-service deployment_controller.main:app 8085"
  write_service_unit "ybb-webhook-receiver" "YBB webhook-receiver" "/usr/local/bin/ybb-python-service webhook_receiver.main:app 8086"
  write_service_unit "ybb-inference-gateway-rt" "YBB inference-gateway realtime" "/usr/local/bin/ybb-python-service inference_gateway.main_realtime:app 8087" "$inference_rt_after" "$inference_rt_wants"
  write_service_unit "ybb-inference-gateway-batch" "YBB inference-gateway batch" "/usr/local/bin/ybb-python-service inference_gateway.main_batch:app 8088" "$inference_batch_after" "$inference_batch_wants"
  write_service_unit "ybb-label-studio-ml-adapter" "YBB Label Studio ML adapter" "/usr/local/bin/ybb-python-service inference_gateway.main_labelstudio_ml_adapter:app 8089" "$label_adapter_after" "$label_adapter_wants"
  write_service_unit "ybb-runtime-admin-api" "YBB runtime-admin-api" "/usr/local/bin/ybb-python-service runtime_admin_api.main:app 8090"
  write_service_unit "ybb-channel-service" "YBB channel-service" "/usr/local/bin/ybb-python-service channel_service.main:app 8091"
  write_service_unit "ybb-pipeline-definition-service" "YBB pipeline-definition-service" "/usr/local/bin/ybb-python-service pipeline_definition_service.main:app 8092"
  write_service_unit "ybb-runtime-orchestrator" "YBB runtime-orchestrator" "/usr/local/bin/ybb-python-service runtime_orchestrator.main:app 8093"
  local camera_after="network.target ybb-secrets.service postgresql.service redis-server.service"
  local camera_wants="ybb-secrets.service postgresql.service redis-server.service"
  if mediamtx_runs_in_wsl; then
    camera_after="$camera_after ybb-mediamtx.service"
    camera_wants="$camera_wants ybb-mediamtx.service"
  fi
  write_service_unit "ybb-camera-media" "YBB camera and media service" "/usr/local/bin/ybb-python-service camera_media.main:app 8102" "$camera_after" "$camera_wants"
  write_service_unit "ybb-realtime-executor" "YBB realtime-executor" "/usr/local/bin/ybb-python-service realtime_executor.main:app 8094" "network.target ybb-secrets.service postgresql.service redis-server.service ybb-camera-media.service" "ybb-secrets.service postgresql.service redis-server.service ybb-camera-media.service"
  write_service_unit "ybb-trigger-executor" "YBB trigger-executor" "/usr/local/bin/ybb-python-service trigger_executor.main:app 8095"
  write_service_unit "ybb-overlay-service" "YBB overlay-service" "/usr/local/bin/ybb-python-service overlay_service.main:app 8096"
  write_service_unit "ybb-capture-service" "YBB capture-service" "/usr/local/bin/ybb-python-service capture_service.main:app 8097"
  write_service_unit "ybb-result-service" "YBB result-service" "/usr/local/bin/ybb-python-service result_service.main:app 8098"
  write_service_unit "ybb-connector-service" "YBB connector-service" "/usr/local/bin/ybb-python-service connector_service.main:app 8099"
  write_service_unit "ybb-control-worker" "YBB control-worker" "/usr/local/bin/ybb-python-worker control_worker.worker"
  write_service_unit "ybb-training-worker" "YBB training-worker" "/usr/local/bin/ybb-python-worker training_worker.worker"
  write_service_unit "ybb-batch-worker" "YBB batch-worker" "/usr/local/bin/ybb-python-worker batch_worker.worker"
  write_service_unit "ybb-maintenance-worker" "YBB maintenance-worker" "/usr/local/bin/ybb-python-worker maintenance_worker.worker"

  mkdir -p /etc/systemd/journald.conf.d
  cat >/etc/systemd/journald.conf.d/ybb-storage-limits.conf <<'EOF'
[Journal]
SystemMaxUse=2G
RuntimeMaxUse=512M
MaxRetentionSec=14day
EOF

  cat >/etc/systemd/system/ybb-storage-maintenance.service <<'EOF'
[Unit]
Description=YBB storage retention and WSL trim
After=network.target postgresql.service redis-server.service ybb-secrets.service
Wants=postgresql.service redis-server.service ybb-secrets.service

[Service]
Type=oneshot
EnvironmentFile=/etc/ybb-inferenzserver/runtime.env
WorkingDirectory=/opt/ybb-inferenzserver/current
ExecStart=/usr/local/bin/ybb-storage-maintenance
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=6h
EOF

  cat >/etc/systemd/system/ybb-storage-maintenance.timer <<'EOF'
[Unit]
Description=Daily YBB storage retention

[Timer]
OnBootSec=20min
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=30min
Persistent=true
Unit=ybb-storage-maintenance.service

[Install]
WantedBy=timers.target
EOF

  cat >/etc/systemd/system/ybb-result-retention.service <<'EOF'
[Unit]
Description=YBB opt-in runtime result retention
After=postgresql.service ybb-secrets.service
Wants=postgresql.service ybb-secrets.service

[Service]
Type=oneshot
EnvironmentFile=/etc/ybb-inferenzserver/runtime.env
WorkingDirectory=/opt/ybb-inferenzserver/current
ExecStart=/usr/local/bin/ybb-python-worker maintenance_worker.result_retention
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=10min
EOF

  cat >/etc/systemd/system/ybb-result-retention.timer <<'EOF'
[Unit]
Description=Frequent YBB runtime result retention

[Timer]
OnBootSec=10min
OnUnitActiveSec=10min
RandomizedDelaySec=1min
Persistent=true
Unit=ybb-result-retention.service

[Install]
WantedBy=timers.target
EOF

  mkdir -p "$CONFIG_DIR/aravis-rtsp"
  if [[ ! -f "$CONFIG_DIR/aravis-rtsp/flir-blackfly.env.example" ]]; then
    cat >"$CONFIG_DIR/aravis-rtsp/flir-blackfly.env.example" <<'EOF'
# Beispiel fuer eine Teledyne FLIR Blackfly/Blackfly S GigE Kamera ueber Aravis.
# Support-Beispiel. Im Normalbetrieb erzeugt der Runtime-Executor diese Datei
# pro aravis_gige Channel automatisch und startet die Bridge im Hintergrund.
ARAVIS_RTSP_ADDRESS=0.0.0.0
ARAVIS_RTSP_PORT=8554
ARAVIS_RTSP_MOUNT=/flir-blackfly

# Ermitteln mit: arv-tool-0.8 list
# Beispiele: Point Grey Research-20373903 oder 192.168.10.42
ARAVIS_CAMERA_NAME=

# Fuer FLIR-Farbkameras haeufig BayerRG8/BayerGB8/BayerGR8/BayerBG8 verwenden.
# Fuer Monochrom-Kameras PixelFormat=Mono8 setzen und ARAVIS_CONVERSION_MODE=mono.
ARAVIS_FEATURES=PixelFormat=BayerRG8
ARAVIS_CONVERSION_MODE=bayer

# Jumbo Frames nur aktivieren, wenn NIC, Switch und Kamera entsprechend konfiguriert sind.
ARAVIS_SRC_PROPERTIES=packet-size=1500 packet-delay=1000
ARAVIS_H264_BITRATE_KBIT=6000
ARAVIS_H264_KEY_INT_MAX=30
ARAVIS_QUEUE_SIZE=2
EOF
  fi

  cat >/etc/systemd/system/ybb-aravis-rtsp@.service <<'EOF'
[Unit]
Description=YBB Aravis GigE RTSP bridge (%i)
After=network.target
Wants=network.target

[Service]
Type=simple
EnvironmentFile=/etc/ybb-inferenzserver/aravis-rtsp/%i.env
ExecStart=/usr/bin/python3 /usr/local/bin/ybb-aravis-rtsp-bridge
Restart=always
RestartSec=5

[Install]
WantedBy=ybb-platform.target
EOF

  cat >/etc/systemd/system/ybb-label-studio.service <<'EOF'
[Unit]
Description=YBB Label Studio
After=network.target ybb-secrets.service postgresql.service
Wants=ybb-secrets.service postgresql.service

[Service]
Type=simple
EnvironmentFile=/etc/ybb-inferenzserver/runtime.env
WorkingDirectory=/srv/cv-platform/labelstudio
ExecStart=/usr/local/bin/ybb-label-studio-service
Restart=always
RestartSec=5

[Install]
WantedBy=ybb-platform.target
EOF

  cat >/etc/systemd/system/ybb-mlflow.service <<'EOF'
[Unit]
Description=YBB MLflow
After=network.target ybb-secrets.service postgresql.service
Wants=ybb-secrets.service postgresql.service

[Service]
Type=simple
EnvironmentFile=/etc/ybb-inferenzserver/runtime.env
WorkingDirectory=/srv/cv-platform/mlflow
ExecStart=/bin/bash -lc 'set -a; . /etc/ybb-inferenzserver/runtime.env; set +a; exec "$YBB_MLFLOW_BIN" server --host 127.0.0.1 --port 5000 --backend-store-uri "postgresql://postgres:$(cat /run/secrets/postgres_app_password)@127.0.0.1:5432/mlflow" --artifacts-destination /srv/cv-platform/mlflow'
Restart=always
RestartSec=5

[Install]
WantedBy=ybb-platform.target
EOF

  if mediamtx_runs_in_wsl; then
    cat >/etc/systemd/system/ybb-mediamtx.service <<'EOF'
[Unit]
Description=YBB MediaMTX RTSP/WebRTC fanout
After=network.target
Wants=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml
Restart=always
RestartSec=3

[Install]
WantedBy=ybb-platform.target
EOF
  else
    log "MediaMTX laeuft nicht in WSL; ybb-mediamtx.service wird deaktiviert."
    systemctl disable --now ybb-mediamtx.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/ybb-mediamtx.service
  fi

  if triton_available; then
    cat >/etc/systemd/system/ybb-triton-rt.service <<'EOF'
[Unit]
Description=YBB Triton realtime
After=network.target

[Service]
Type=simple
EnvironmentFile=/etc/ybb-inferenzserver/runtime.env
ExecStart=/usr/local/bin/ybb-triton-server --model-repository=/srv/cv-platform/serving/triton/realtime --model-control-mode=explicit --http-port=8000 --grpc-port=8001 --metrics-port=8002
Restart=always
RestartSec=5

[Install]
WantedBy=ybb-platform.target
EOF

    cat >/etc/systemd/system/ybb-triton-batch.service <<'EOF'
[Unit]
Description=YBB Triton batch
After=network.target

[Service]
Type=simple
EnvironmentFile=/etc/ybb-inferenzserver/runtime.env
ExecStart=/usr/local/bin/ybb-triton-server --model-repository=/srv/cv-platform/serving/triton/batch --model-control-mode=explicit --http-port=8010 --grpc-port=8011 --metrics-port=8012
Restart=always
RestartSec=5

[Install]
WantedBy=ybb-platform.target
EOF
  else
    log "Triton Server wurde nicht gefunden; Triton-systemd-Units werden deaktiviert."
    systemctl disable --now ybb-triton-rt.service ybb-triton-batch.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/ybb-triton-rt.service /etc/systemd/system/ybb-triton-batch.service
  fi

  local platform_wants="ybb-turnserver.service ybb-secrets.service ybb-label-studio.service ybb-mlflow.service ybb-admin-api.service ybb-dataset-service.service ybb-training-orchestrator.service ybb-model-registry-service.service ybb-deployment-controller.service ybb-webhook-receiver.service ybb-inference-gateway-rt.service ybb-inference-gateway-batch.service ybb-label-studio-ml-adapter.service ybb-runtime-admin-api.service ybb-channel-service.service ybb-pipeline-definition-service.service ybb-runtime-orchestrator.service ybb-camera-media.service ybb-realtime-executor.service ybb-trigger-executor.service ybb-overlay-service.service ybb-capture-service.service ybb-result-service.service ybb-connector-service.service ybb-control-worker.service ybb-training-worker.service ybb-batch-worker.service ybb-maintenance-worker.service"
  if mediamtx_runs_in_wsl; then
    platform_wants="$platform_wants ybb-mediamtx.service"
  fi
  if triton_available; then
    platform_wants="$platform_wants ybb-triton-rt.service ybb-triton-batch.service"
  fi

  cat >/etc/systemd/system/ybb-platform.target <<EOF
[Unit]
Description=YBB Inferenzserver platform
Wants=$platform_wants
After=network.target postgresql.service redis-server.service ybb-secrets.service

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now ybb-storage-maintenance.timer >/dev/null 2>&1 || true
  systemctl enable --now ybb-result-retention.timer >/dev/null 2>&1 || true
  systemctl try-restart systemd-journald.service >/dev/null 2>&1 || true
}

write_caddyfile() {
  if ! command -v caddy >/dev/null 2>&1; then
    log "Caddy ist in der vorbereiteten Runtime nicht installiert; Reverse Proxy wird uebersprungen."
    return
  fi

  local stream_media_host stream_webrtc_port
  stream_media_host="$(mediamtx_host_for_wsl)"
  stream_webrtc_port="$(read_bundle_manifest_value StreamingWebRtcPort 8889)"
  mkdir -p /etc/caddy
  cat >/etc/caddy/Caddyfile <<EOF
{
  auto_https disable_redirects
  servers {
    protocols h1 h2
  }
}

(ybb_routes) {
  encode gzip zstd
  header {
    X-Content-Type-Options "nosniff"
    Referrer-Policy "same-origin"
    X-Frame-Options "SAMEORIGIN"
    -Alt-Svc
    -Server
  }

  handle /health/* {
    reverse_proxy 127.0.0.1:8080
  }

  handle /api/* {
    reverse_proxy 127.0.0.1:8080
  }

  handle_path /inference-rt/* {
    reverse_proxy 127.0.0.1:8087
  }

  handle_path /inference-batch/* {
    reverse_proxy 127.0.0.1:8088
  }

  handle_path /runtime-api/* {
    reverse_proxy 127.0.0.1:8090
  }

  handle_path /stream-webrtc/* {
    reverse_proxy ${stream_media_host}:${stream_webrtc_port} {
      header_down Location ^/ /stream-webrtc/
    }
  }

  handle_path /runtime-trigger/* {
    reverse_proxy 127.0.0.1:8095
  }

  handle_path /runtime-overlay/* {
    reverse_proxy 127.0.0.1:8096
  }

  handle_path /runtime-results/* {
    reverse_proxy 127.0.0.1:8098
  }

  handle /label-studio/data/local-files* {
    root * /srv/cv-platform
    rewrite * /{query.d}
    header Access-Control-Allow-Origin "*"
    file_server
  }

  handle /data/local-files* {
    root * /srv/cv-platform
    rewrite * /{query.d}
    header Access-Control-Allow-Origin "*"
    file_server
  }

  handle /data/upload/* {
    reverse_proxy 127.0.0.1:8100
  }

  # Label Studio serves frontend assets on absolute paths even when HOST uses /label-studio.
  handle /static/* {
    reverse_proxy 127.0.0.1:8100
  }

  handle /react-app/* {
    reverse_proxy 127.0.0.1:8100
  }

  redir /label-studio /label-studio/ 308
  handle /label-studio/ {
    uri strip_prefix /label-studio
    reverse_proxy 127.0.0.1:8100
  }

  handle_path /label-studio/* {
    reverse_proxy 127.0.0.1:8100
  }

  redir /mlflow /mlflow/ 308
  handle_path /mlflow/* {
    reverse_proxy 127.0.0.1:5000
  }

  redir / /frontend/ 308
  redir /frontend /frontend/ 308
  @frontend_assets path /frontend/assets/*
  handle @frontend_assets {
    uri strip_prefix /frontend/assets
    root * /opt/ybb-inferenzserver/current/frontend/apps/admin-web/dist/assets
    header Cache-Control "public, max-age=31536000, immutable"
    try_files {path} =404
    file_server
  }

  handle_path /frontend/* {
    root * /opt/ybb-inferenzserver/current/frontend/apps/admin-web/dist
    header Cache-Control "no-store"
    try_files {path} /index.html
    file_server
  }

  handle {
    root * /opt/ybb-inferenzserver/current/frontend/apps/admin-web/dist
    header Cache-Control "no-store"
    try_files {path} /index.html
    file_server
  }
}

:80 {
  redir https://{host}{uri} 308
}

:443 {
  tls /run/secrets/tls_cert.pem /run/secrets/tls_key.pem
  import ybb_routes
}
EOF

  if ! caddy validate --config /etc/caddy/Caddyfile; then
    log "FEHLER: Caddyfile ist ungueltig."
    log_caddy_diagnostics
    return 1
  fi

  systemctl enable caddy >/dev/null 2>&1 || true
  systemctl reset-failed caddy >/dev/null 2>&1 || true
  systemctl stop caddy >/dev/null 2>&1 || true
  if command -v pkill >/dev/null 2>&1; then
    pkill -x caddy >/dev/null 2>&1 || true
  fi
  if ! systemctl start caddy; then
    log "FEHLER: Caddy konnte nicht gestartet werden."
    log_caddy_diagnostics
    return 1
  fi
}

log_caddy_diagnostics() {
  log "Caddy-Diagnose:"
  caddy validate --config /etc/caddy/Caddyfile || true
  systemctl --no-pager status caddy || true
  journalctl -u caddy -n 160 --no-pager -l || true
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp | grep -E ':(80|443)\b' || true
  fi
  ls -l /run/secrets/tls_cert.pem /run/secrets/tls_key.pem 2>/dev/null || true
}

run_migrations() {
  log "Fuehre Alembic-Migrationen aus."
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  cd "$RELEASE_DIR"
  PYTHONPATH="$(build_pythonpath "$RELEASE_DIR")" \
    "$YBB_PYTHON_BIN" -m alembic -c backend/migrations/alembic.ini upgrade head
}

restart_platform() {
  log "Starte bzw. aktualisiere systemd-Services."
  systemctl enable ybb-platform.target
  local units=(
    ybb-turnserver.service
    ybb-secrets.service
    ybb-label-studio.service
    ybb-mlflow.service
    ybb-admin-api.service
    ybb-dataset-service.service
    ybb-training-orchestrator.service
    ybb-model-registry-service.service
    ybb-deployment-controller.service
    ybb-webhook-receiver.service
    ybb-inference-gateway-rt.service
    ybb-inference-gateway-batch.service
    ybb-label-studio-ml-adapter.service
    ybb-runtime-admin-api.service
    ybb-channel-service.service
    ybb-pipeline-definition-service.service
    ybb-runtime-orchestrator.service
    ybb-camera-media.service
    ybb-realtime-executor.service
    ybb-trigger-executor.service
    ybb-overlay-service.service
    ybb-capture-service.service
    ybb-result-service.service
    ybb-connector-service.service
    ybb-control-worker.service
    ybb-training-worker.service
    ybb-batch-worker.service
    ybb-maintenance-worker.service
  )
  if triton_available; then
    units+=(ybb-triton-rt.service ybb-triton-batch.service)
  fi
  if mediamtx_runs_in_wsl; then
    units=(ybb-mediamtx.service "${units[@]}")
  fi
  systemctl restart "${units[@]}"
  mapfile -t aravis_units < <(
    systemctl list-units 'ybb-aravis-rtsp@*.service' --all --no-legend --no-pager |
      awk '{ print $1 }'
  )
  if ((${#aravis_units[@]} > 0)); then
    systemctl restart "${aravis_units[@]}" || true
  fi
  systemctl start ybb-platform.target
}

tcp_port_ready() {
  local port="$1"
  timeout 1 bash -lc "</dev/tcp/127.0.0.1/$port" >/dev/null 2>&1
}

turnserver_ready() {
  local port="$1"
  local main_pid listeners
  if ! systemctl is-active --quiet ybb-turnserver.service; then
    return 1
  fi
  if systemctl is-active --quiet coturn.service \
    || systemctl is-active --quiet turnserver.service; then
    return 1
  fi
  main_pid="$(systemctl show ybb-turnserver.service --property=MainPID --value)"
  if [[ ! "$main_pid" =~ ^[1-9][0-9]*$ ]]; then
    return 1
  fi
  if ! tcp_port_ready "$port"; then
    return 1
  fi
  if ! command -v ss >/dev/null 2>&1; then
    log "FEHLER: ss fehlt; der Besitzer des TURN-Ports kann nicht validiert werden."
    return 1
  fi
  listeners="$(ss -H -ltnp 2>/dev/null | grep -F ":${port} " || true)"
  grep -Fq "pid=${main_pid}," <<<"$listeners"
}

log_startup_diagnostics() {
  log "Runtime-Status nach fehlgeschlagenem Start:"
  log "WSL Networking Mode: $(wsl_networking_mode); MediaMTX Host fuer WSL: $(mediamtx_host_for_wsl); MediaMTX Plattform: $(mediamtx_host_platform)"
  systemctl --no-pager --failed || true
  systemctl --no-pager status \
    caddy \
    coturn.service \
    turnserver.service \
    ybb-turnserver.service \
    ybb-mediamtx.service \
    ybb-admin-api.service \
    ybb-runtime-admin-api.service \
    ybb-label-studio.service \
    ybb-mlflow.service || true
  journalctl \
    -u coturn.service \
    -u turnserver.service \
    -u ybb-turnserver.service \
    -u ybb-mediamtx.service \
    -u ybb-admin-api.service \
    -u ybb-runtime-admin-api.service \
    -u ybb-label-studio.service \
    -u ybb-mlflow.service \
    -n 180 --no-pager -l || true
}

wait_runtime_ports() {
  local timeout_seconds="${1:-300}"
  local deadline=$((SECONDS + timeout_seconds))
  local stream_rtsp_port stream_webrtc_port stream_webrtc_ice_tcp_port stream_turn_tcp_port
  stream_rtsp_port="$(read_bundle_manifest_value StreamingRtspPort 8554)"
  stream_webrtc_port="$(read_bundle_manifest_value StreamingWebRtcPort 8889)"
  stream_webrtc_ice_tcp_port="$(read_bundle_manifest_value StreamingWebRtcIceTcpPort 8189)"
  stream_turn_tcp_port="$(read_bundle_manifest_value StreamingTurnTcpPort 3478)"
  local -a pending=(
    "Caddy HTTP:80"
    "Caddy HTTPS:443"
    "Admin-API:8080"
    "Runtime-API:8090"
    "Camera-Media:8102"
    "Label Studio:8100"
    "MLflow:5000"
    "TURN TCP:${stream_turn_tcp_port}"
  )
  if mediamtx_runs_in_wsl; then
    pending+=(
      "MediaMTX RTSP:${stream_rtsp_port}"
      "MediaMTX WHEP:${stream_webrtc_port}"
      "MediaMTX ICE TCP:${stream_webrtc_ice_tcp_port}"
      "MediaMTX API:9997"
    )
  fi
  local item name port
  local -a next_pending

  while ((${#pending[@]} > 0 && SECONDS < deadline)); do
    next_pending=()
    for item in "${pending[@]}"; do
      name="${item%%:*}"
      port="${item##*:}"
      if [[ "$name" == "TURN TCP" ]] && turnserver_ready "$port"; then
        log "Runtime-Port bereit: $name auf 127.0.0.1:$port (ybb-turnserver.service)"
      elif [[ "$name" != "TURN TCP" ]] && tcp_port_ready "$port"; then
        log "Runtime-Port bereit: $name auf 127.0.0.1:$port"
      else
        next_pending+=("$item")
      fi
    done
    pending=("${next_pending[@]}")
    if ((${#pending[@]} == 0)); then
      return 0
    fi
    sleep 2
  done

  log "FEHLER: Diese Runtime-Ports wurden nicht rechtzeitig bereit: ${pending[*]}"
  log_startup_diagnostics
  return 1
}

validate_internal_gateway_auth() {
  local timeout_seconds="${1:-120}"
  local deadline=$((SECONDS + timeout_seconds))
  local token_path="$SECRETS_DIR/internal_inference_gateway_bearer_token"
  local token status body_path
  body_path="/tmp/ybb-inference-gateway-auth-check.json"

  if [[ ! -s "$token_path" ]]; then
    log "FEHLER: Internes Inference-Gateway-Token fehlt: $token_path"
    return 1
  fi
  token="$(tr -d '\r\n' <"$token_path")"

  while ((SECONDS < deadline)); do
    status="$(
      curl -ksS \
        -o "$body_path" \
        -w "%{http_code}" \
        -H "Authorization: Bearer $token" \
        http://127.0.0.1:8087/api/v1/realtime/models/status 2>/dev/null || true
    )"
    if [[ "$status" == "200" ]]; then
      log "Interne Inference-Gateway-Authentifizierung ist bereit."
      rm -f "$body_path"
      return 0
    fi
    sleep 2
  done

  log "FEHLER: Interne Inference-Gateway-Authentifizierung fehlgeschlagen -> HTTP ${status:-none}"
  if [[ -s "$body_path" ]]; then
    head -c 1200 "$body_path" | sed 's/^/[inference-gateway-auth] /' || true
  fi
  return 1
}

validate_mediamtx_streaming() {
  if [[ "$(read_bundle_manifest_value MediaMtxSkipped false)" == "true" ]]; then
    log "WARNUNG: MediaMTX Smoke-Test uebersprungen, weil MediaMTX im Bundle uebersprungen wurde."
    return 0
  fi

  local stream_media_host mediamtx_api_url
  stream_media_host="$(mediamtx_host_for_wsl)"
  mediamtx_api_url="$(mediamtx_api_url_for_wsl "$stream_media_host")"
  if mediamtx_runs_in_wsl; then
    if ! systemctl is-active --quiet ybb-mediamtx.service; then
      log "FEHLER: ybb-mediamtx.service ist nicht aktiv."
      systemctl --no-pager status ybb-mediamtx.service || true
      return 1
    fi
    mediamtx --version || true
  fi
  if ! curl -fsS "${mediamtx_api_url}/v3/paths/list" >/dev/null; then
    log "FEHLER: MediaMTX API ist von WSL nicht erreichbar: http://${stream_media_host}:9997/v3/paths/list"
    return 1
  fi
  if ! command -v gst-launch-1.0 >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    log "WARNUNG: MediaMTX Publish/Read-Smoke-Test uebersprungen, weil gst-launch-1.0 oder ffprobe fehlt."
    return 0
  fi
  if ! gst-inspect-1.0 rtspclientsink >/dev/null 2>&1; then
    log "FEHLER: MediaMTX Smoke-Test braucht GStreamer-Element rtspclientsink; das Paket gstreamer1.0-rtsp fehlt im Runtime-System oder Offline-Bundle."
    return 1
  fi
  validate_nvenc_stream_encoder

  local stream_rtsp_port smoke_url publisher_pid
  stream_rtsp_port="$(read_bundle_manifest_value StreamingRtspPort 8554)"
  smoke_url="rtsp://${stream_media_host}:${stream_rtsp_port}/ybb-smoke/offline"
  timeout 45s gst-launch-1.0 -q \
    videotestsrc is-live=true num-buffers=675 \
    ! video/x-raw,width=320,height=240,framerate=15/1 \
    ! videoconvert \
    ! video/x-raw,format=BGR \
    ! cudaupload \
    ! cudaconvert \
    ! 'video/x-raw(memory:CUDAMemory),format=NV12' \
    ! nvcudah264enc preset=p1 tune=ultra-low-latency zero-reorder-delay=true \
      gop-size=15 b-frames=0 bitrate=512 \
    ! h264parse config-interval=1 \
    ! rtspclientsink location="$smoke_url" protocols=tcp &
  publisher_pid=$!
  sleep 2
  if ! timeout 8s ffprobe -v error -rtsp_transport tcp -select_streams v:0 -show_entries stream=codec_name -of default=nw=1:nk=1 "$smoke_url" | grep -q h264; then
    kill "$publisher_pid" >/dev/null 2>&1 || true
    wait "$publisher_pid" >/dev/null 2>&1 || true
    log "FEHLER: MediaMTX Smoke-Test konnte den RTSP-Teststream nicht lesen: $smoke_url"
    return 1
  fi
  kill "$publisher_pid" >/dev/null 2>&1 || true
  wait "$publisher_pid" >/dev/null 2>&1 || true
  log "MediaMTX NVENC/RTSP Publish/Read-Smoke-Test erfolgreich."
}

validate_nvenc_stream_encoder() {
  log "Pruefe verpflichtenden GStreamer-NVENC-Hardwarepfad."
  if [[ ! -x /usr/bin/python3 ]] || ! /usr/bin/python3 - <<'PY'
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: F401

print("system-python-gstreamer-import-ok")
PY
  then
    log "FEHLER: System-Python kann die GStreamer-GI-Bindings nicht importieren; der NVENC-Publisher kann nicht starten."
    return 1
  fi
  if ! gst-inspect-1.0 nvcudah264enc >/dev/null 2>&1; then
    log "FEHLER: nvcudah264enc ist nicht verfuegbar; der Produktionsstream hat keinen CPU-Fallback."
    return 1
  fi
  if ! gst-inspect-1.0 cudaupload >/dev/null 2>&1; then
    log "FEHLER: nvcudah264enc ist verfuegbar, aber cudaupload fehlt; der Produktionsstream kann nicht starten."
    return 1
  fi
  if ! gst-inspect-1.0 cudaconvert >/dev/null 2>&1; then
    log "FEHLER: nvcudah264enc ist verfuegbar, aber cudaconvert fehlt; der Produktionsstream kann nicht starten."
    return 1
  fi

  local nvenc_info diagnostic_line
  local nvenc_last_attempt="" nvenc_last_exit_code="" nvenc_last_error=""
  nvenc_info="$(gst-inspect-1.0 nvcudah264enc 2>/dev/null || true)"
  if ! grep -Eq '(^|[[:space:]])p1([[:space:]]|-|$)' <<<"$nvenc_info" \
    || ! grep -Eq '(^|[[:space:]])ultra-low-latency([[:space:]]|-|$)' <<<"$nvenc_info" \
    || ! grep -Eq '^[[:space:]]+zero-reorder-delay[[:space:]]*:' <<<"$nvenc_info"; then
    log "FEHLER: nvcudah264enc bietet das verpflichtende moderne Profil preset=p1/tune=ultra-low-latency/zero-reorder-delay nicht an."
    return 1
  fi

  run_nvenc_encoder_smoke() {
    local smoke_stderr smoke_exit_code attempt_description
    attempt_description="${*:-default-preset}"
    smoke_stderr="$(mktemp /tmp/ybb-nvenc-smoke.XXXXXX)"
    if GST_DEBUG=2 gst-launch-1.0 -q videotestsrc num-buffers=4 is-live=true \
      ! video/x-raw,width=320,height=240,framerate=15/1 \
      ! videoconvert \
      ! video/x-raw,format=BGR \
      ! cudaupload \
      ! cudaconvert \
      ! 'video/x-raw(memory:CUDAMemory),format=NV12' \
      ! nvcudah264enc bitrate=512 gop-size=15 b-frames=0 "$@" \
      ! h264parse \
      ! fakesink >/dev/null 2>"$smoke_stderr"; then
      if grep -Eiq 'WARNING: erroneous pipeline|could not set property|Selected preset not supported|Could not configure supporting library|ERROR:' "$smoke_stderr"; then
        smoke_exit_code=0
      else
        rm -f "$smoke_stderr"
        return 0
      fi
    else
      smoke_exit_code=$?
    fi

    nvenc_last_attempt="$attempt_description"
    nvenc_last_exit_code="$smoke_exit_code"
    nvenc_last_error="$(tail -n 60 "$smoke_stderr" 2>/dev/null || true)"
    rm -f "$smoke_stderr"
    return 1
  }

  if run_nvenc_encoder_smoke preset=p1 tune=ultra-low-latency zero-reorder-delay=true; then
    log "NVENC-GStreamer-Smoke-Test erfolgreich: p1/ultra-low-latency/zero-reorder-delay."
    return 0
  fi

  log "FEHLER: Der verpflichtende GStreamer-NVENC-Smoke-Test ist fehlgeschlagen (Versuch: ${nvenc_last_attempt:-unbekannt}, Exit Code: ${nvenc_last_exit_code:-unbekannt})."
  if [[ -n "$nvenc_last_error" ]]; then
    log "NVENC-Diagnose (letzte maximal 60 stderr-Zeilen):"
    while IFS= read -r diagnostic_line; do
      log "NVENC: $diagnostic_line"
    done <<<"$nvenc_last_error"
  else
    log "NVENC-Diagnose: gst-launch-1.0 lieferte keine stderr-Ausgabe."
  fi
  return 1
}

validate_reverse_proxy_routes() {
  local timeout_seconds="${1:-120}"
  local deadline=$((SECONDS + timeout_seconds))
  local index_path="/opt/ybb-inferenzserver/current/frontend/apps/admin-web/dist/index.html"
  local asset_path
  local response status content_type
  local label_html label_python asset_status asset_content_type
  local local_file_probe_relative local_file_probe_path local_file_probe_body local_file_probe_status
  local local_file_probe_output
  local video_probe_relative video_probe_path video_probe_output video_probe_status video_probe_body
  local -a label_assets

  if [[ ! -f "$index_path" ]]; then
    log "FEHLER: Admin-Frontend-index.html fehlt: $index_path"
    return 1
  fi

  asset_path="$(grep -oE '/frontend/assets/[^"]+\.(css|js)' "$index_path" | head -n 1 || true)"
  if [[ -z "$asset_path" ]]; then
    log "FEHLER: Admin-Frontend-index.html referenziert kein /frontend/assets/*.css oder *.js."
    return 1
  fi

  while ((SECONDS < deadline)); do
    response="$(curl -k -sS -o /dev/null -w '%{http_code} %{content_type}' "https://127.0.0.1${asset_path}" || true)"
    status="${response%% *}"
    content_type="${response#* }"
    if [[ "$status" == "200" && "$content_type" != text/html* ]]; then
      break
    fi
    sleep 2
  done

  if [[ "$status" != "200" || "$content_type" == text/html* ]]; then
    log "FEHLER: Frontend-Asset wird nicht korrekt ausgeliefert: ${asset_path} -> HTTP ${status}, Content-Type ${content_type}"
    return 1
  fi

  if mediamtx_runs_in_wsl || [[ "$(mediamtx_host_platform)" == "windows" ]]; then
    local webrtc_probe_body webrtc_probe_status webrtc_probe_content_type
    webrtc_probe_body="/tmp/ybb-stream-webrtc-proxy-probe.out"
    response="$(
      printf 'invalid-sdp-probe' | curl -k -sS \
        -H "Host: ${APP_HOST_NAME}" \
        -H "Content-Type: application/sdp" \
        -X POST \
        --data-binary @- \
        -o "$webrtc_probe_body" \
        -w '%{http_code} %{content_type}' \
        "https://127.0.0.1/stream-webrtc/__ybb_reverse_proxy_probe__/whep" || true
    )"
    webrtc_probe_status="${response%% *}"
    webrtc_probe_content_type="${response#* }"
    if grep -qE '/frontend/assets/|<div id="root"' "$webrtc_probe_body" 2>/dev/null; then
      rm -f "$webrtc_probe_body"
      log "FEHLER: /stream-webrtc/.../whep wird vom Admin-Frontend beantwortet statt von MediaMTX: HTTP ${webrtc_probe_status}, Content-Type ${webrtc_probe_content_type}"
      return 1
    fi
    if [[ "$webrtc_probe_status" == "000" || "$webrtc_probe_status" == 5* ]]; then
      rm -f "$webrtc_probe_body"
      log "FEHLER: /stream-webrtc/.../whep kann MediaMTX nicht erreichen: HTTP ${webrtc_probe_status}, Content-Type ${webrtc_probe_content_type}"
      return 1
    fi
    rm -f "$webrtc_probe_body"

  fi

  label_html="/tmp/ybb-label-studio-login.html"
  while ((SECONDS < deadline)); do
    response="$(curl -k -sS -o /dev/null -w '%{http_code} %{content_type}' "https://127.0.0.1/label-studio/" || true)"
    status="${response%% *}"
    content_type="${response#* }"
    if [[ "$status" == "200" && "$content_type" == text/html* ]]; then
      break
    fi
    if [[ "$status" == "302" || "$status" == "303" ]]; then
      break
    fi
    sleep 2
  done

  if [[ "$status" != "200" && "$status" != "302" && "$status" != "303" ]]; then
    log "FEHLER: Label Studio wird nicht korrekt ueber /label-studio/ ausgeliefert: HTTP ${status}, Content-Type ${content_type}"
    return 1
  fi

  if ! curl -k -sS -H "Host: ${APP_HOST_NAME}" "https://127.0.0.1/label-studio/user/login/" -o "$label_html"; then
    log "FEHLER: Label-Studio-Login-Seite konnte nicht fuer Asset-Pruefung geladen werden."
    return 1
  fi

  label_python="${PYTHON_BIN:-}"
  if [[ -z "$label_python" || ! -x "$label_python" ]]; then
    label_python="$(command -v python3 || command -v python || true)"
  fi
  if [[ -z "$label_python" ]]; then
    log "WARNUNG: Python fuer Label-Studio-Asset-Pruefung nicht gefunden."
  else
    mapfile -t label_assets < <("$label_python" - "$label_html" <<'PY'
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
import sys

class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name not in {"href", "src"} or not value:
                continue
            lower = value.lower().split("?", 1)[0]
            if not (lower.endswith(".css") or lower.endswith(".js")):
                continue
            parsed = urlparse(value)
            path = parsed.path if parsed.scheme else value.split("?", 1)[0]
            if path.startswith("/"):
                self.assets.append(path)

parser = AssetParser()
parser.feed(Path(sys.argv[1]).read_text(encoding="utf-8", errors="ignore"))
seen = set()
for path in parser.assets:
    if path in seen:
        continue
    seen.add(path)
    print(path)
PY
    )
  fi

  if ((${#label_assets[@]} == 0)); then
    log "WARNUNG: Keine Label-Studio-CSS/JS-Assets in der Login-Seite gefunden."
  fi

  for label_asset in "${label_assets[@]}"; do
    response="$(curl -k -sS -H "Host: ${APP_HOST_NAME}" -o /dev/null -w '%{http_code} %{content_type}' "https://127.0.0.1${label_asset}" || true)"
    asset_status="${response%% *}"
    asset_content_type="${response#* }"
    if [[ "$asset_status" != "200" || "$asset_content_type" == text/html* ]]; then
      log "FEHLER: Label-Studio-Asset wird nicht korrekt ausgeliefert: ${label_asset} -> HTTP ${asset_status}, Content-Type ${asset_content_type}"
      return 1
    fi
  done

  local_file_probe_relative="runtime/.label-studio-local-files-probe-${APP_VERSION}-$$.txt"
  local_file_probe_path="${STATE_ROOT}/${local_file_probe_relative}"
  local_file_probe_output="/tmp/ybb-label-studio-local-files-probe.out"
  mkdir -p "$(dirname "$local_file_probe_path")"
  chmod 755 "$STATE_ROOT" "$(dirname "$local_file_probe_path")"
  printf 'ybb-local-files-probe' >"$local_file_probe_path"
  chmod 644 "$local_file_probe_path"

  for local_file_route in "/data/local-files/:required" "/label-studio/data/local-files/:optional"; do
    local local_file_route_path local_file_route_required local_file_probe_preview
    local_file_route_path="${local_file_route%:*}"
    local_file_route_required="${local_file_route##*:}"
    local_file_probe_status="$(
      curl -k -sS \
        -H "Host: ${APP_HOST_NAME}" \
        -o "$local_file_probe_output" \
        -w '%{http_code}' \
        "https://127.0.0.1${local_file_route_path}?d=${local_file_probe_relative}" || true
    )"
    local_file_probe_body="$(cat "$local_file_probe_output" 2>/dev/null || true)"
    if [[ "$local_file_probe_status" != "200" || "$local_file_probe_body" != "ybb-local-files-probe" ]]; then
      local_file_probe_preview="$(head -c 160 "$local_file_probe_output" 2>/dev/null | tr '\r\n' '  ' || true)"
      if [[ "$local_file_route_required" == "required" ]]; then
        rm -f "$local_file_probe_path" "$local_file_probe_output"
        log "FEHLER: Label-Studio-Local-Files-Route liefert keine Capture-Dateien aus: ${local_file_route_path} -> HTTP ${local_file_probe_status}; Antwort: ${local_file_probe_preview}"
        return 1
      fi
      log "WARNUNG: Optionale Label-Studio-Local-Files-Route liefert keine Probe-Datei: ${local_file_route_path} -> HTTP ${local_file_probe_status}; Antwort: ${local_file_probe_preview}"
    fi
  done
  rm -f "$local_file_probe_path" "$local_file_probe_output"

  video_probe_relative="runtime/captures/.label-studio-video-range-probe-${APP_VERSION}-$$.mp4"
  video_probe_path="${STATE_ROOT}/${video_probe_relative}"
  video_probe_output="/tmp/ybb-label-studio-video-range-probe.out"
  mkdir -p "$(dirname "$video_probe_path")"
  chmod 755 "$STATE_ROOT" "$STATE_ROOT/runtime" "$(dirname "$video_probe_path")"
  printf '0123456789' >"$video_probe_path"
  chmod 644 "$video_probe_path"
  video_probe_status="$(
    curl -k -sS \
      -H "Host: ${APP_HOST_NAME}" \
      -H 'Range: bytes=2-5' \
      -o "$video_probe_output" \
      -w '%{http_code}' \
      "https://127.0.0.1/data/local-files/?d=${video_probe_relative}" || true
  )"
  video_probe_body="$(cat "$video_probe_output" 2>/dev/null || true)"
  rm -f "$video_probe_path" "$video_probe_output"
  if [[ "$video_probe_status" != "206" || "$video_probe_body" != "2345" ]]; then
    log "FEHLER: Label-Studio-Local-Files unterstuetzt keinen Byte-Range-Zugriff fuer Video-Timeline-Labeling: HTTP ${video_probe_status}"
    return 1
  fi

  log "Reverse-Proxy-Routen validiert: ${asset_path}, /label-studio/, ${#label_assets[@]} Label-Studio-Assets, Local-Files und Video-Byte-Ranges."
}

normalize_label_studio_capture_permissions() {
  local captures_dir="$STATE_ROOT/runtime/captures"
  mkdir -p "$STATE_ROOT/runtime"
  chmod 755 "$STATE_ROOT" "$STATE_ROOT/runtime"

  if [[ ! -d "$captures_dir" ]]; then
    return 0
  fi

  log "Normalisiere Dateirechte fuer Label-Studio-Captures."
  find "$captures_dir" -type d -exec chmod 755 {} +
  find "$captures_dir" -type f -exec chmod 644 {} +
}

HOST_NAME="$(read_installed_host_name)"
APP_HOST_NAME="$HOST_NAME"
PYTHON_BIN="$(detect_python_bin)"
TRAINING_PYTHON_BIN="$(detect_training_python_bin)"
LABEL_STUDIO_BIN="$(detect_bin /opt/ybb-runtime/venv/bin/label-studio label-studio)"
MLFLOW_BIN="$(detect_bin /opt/ybb-runtime/venv/bin/mlflow mlflow)"
install_triton_archive_from_bundle
install_mediamtx_archive_from_bundle
install_gstreamer_deb_bundle_from_bundle
install_python_wheel_bundle_from_bundle "$PYTHON_BIN" "requirements.txt" "Service-Runtime"
validate_basler_pypylon_package "$PYTHON_BIN"
install_python_wheel_bundle_from_bundle "$TRAINING_PYTHON_BIN" "deployment-requirements.txt" "Training-/Deployment-Runtime"
TRITON_BIN="$(detect_bin /opt/tritonserver/bin/tritonserver tritonserver)"
TRTEXEC_BIN="$(detect_bin /opt/tritonserver/bin/trtexec trtexec)"

mkdir -p "$APP_ROOT" "$RELEASES_DIR" "$STATE_ROOT" "$SECRETS_DIR" "$CONFIG_DIR"
mkdir -p \
  "$STATE_ROOT/training" \
  "$STATE_ROOT/serving/packages" \
  "$STATE_ROOT/serving/triton/realtime" \
  "$STATE_ROOT/serving/triton/batch" \
  "$STATE_ROOT/audit" \
  "$STATE_ROOT/logs" \
  "$STATE_ROOT/control/pretrained-checkpoints" \
  "$STATE_ROOT/control/uploads" \
  "$STATE_ROOT/control/labelstudio/exports" \
  "$STATE_ROOT/control/snapshots" \
  "$STATE_ROOT/control/dvc/repo" \
  "$STATE_ROOT/control/dvc/remote" \
  "$STATE_ROOT/runtime" \
  "$STATE_ROOT/runtime/captures" \
  "$STATE_ROOT/runtime/captures/.temporal-spool" \
  "$STATE_ROOT/runtime/captures/temporal-samples" \
  "$STATE_ROOT/runtime/results" \
  "$STATE_ROOT/mlflow" \
  "$STATE_ROOT/labelstudio"

extract_release
validate_release_frontend_assets
validate_temporal_release_compatibility
ensure_internal_machine_credentials
patch_release_config "$HOST_NAME" "$PYTHON_BIN"
ensure_training_cuda_runtime_links "$TRAINING_PYTHON_BIN"
ensure_service_onnxruntime "$PYTHON_BIN" "$TRAINING_PYTHON_BIN"
write_runtime_env "$HOST_NAME" "$PYTHON_BIN" "$TRAINING_PYTHON_BIN" "$LABEL_STUDIO_BIN" "$MLFLOW_BIN" "$TRITON_BIN" "$TRTEXEC_BIN"
validate_deployment_packaging_runtime "$TRAINING_PYTHON_BIN"
validate_service_runtime_imports
import_edge_runtime_packages
validate_nvenc_stream_encoder
run_migrations
activate_release
install_systemd_units
write_caddyfile
normalize_label_studio_capture_permissions
restart_platform
wait_runtime_ports 300
validate_mediamtx_streaming
validate_internal_gateway_auth 120
if ! validate_reverse_proxy_routes 120; then
  log "WARNUNG: Reverse-Proxy-Routen konnten nach dem Update nicht vollstaendig validiert werden; das Release bleibt aktiv. Pruefe Caddy-, Label-Studio- und MediaMTX-Logs, falls die Weboberflaeche nicht erreichbar ist."
  log_startup_diagnostics || true
fi

if ! /usr/local/bin/ybb-storage-maintenance; then
  log "WARNUNG: Die Speicherbereinigung nach dem Update war nicht vollstaendig erfolgreich. Pruefe systemctl status ybb-storage-maintenance.service und journalctl -u ybb-storage-maintenance.service."
fi

log "Release $APP_VERSION wurde angewendet. Persistente Daten bleiben unter $STATE_ROOT."
