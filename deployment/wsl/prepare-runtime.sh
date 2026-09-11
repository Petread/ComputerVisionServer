#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=""
INSTALL_APT=1
INSTALL_TRAINING_RUNTIME=1
SKIP_TRITON_CHECK=0
TRITON_ARCHIVE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO_DIR="$2"
      shift 2
      ;;
    --skip-apt)
      INSTALL_APT=0
      shift
      ;;
    --skip-training-runtime)
      INSTALL_TRAINING_RUNTIME=0
      shift
      ;;
    --skip-triton-check)
      SKIP_TRITON_CHECK=1
      shift
      ;;
    --triton-archive)
      TRITON_ARCHIVE="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$REPO_DIR" || ! -d "$REPO_DIR" ]]; then
  echo "Usage: prepare-runtime.sh --repo <repo-dir> [--skip-apt] [--skip-training-runtime] [--triton-archive <tar.gz>] [--skip-triton-check]" >&2
  exit 2
fi

RUNTIME_ROOT="/opt/ybb-runtime"
SOURCE_ROOT="$RUNTIME_ROOT/source"
SERVICE_VENV="$RUNTIME_ROOT/venv"
TRAINING_VENV="$RUNTIME_ROOT/training-venv"
WORKSPACE_REQUIREMENTS="$RUNTIME_ROOT/workspace-editables.txt"
TRAINING_REQUIREMENTS="$RUNTIME_ROOT/training-runtime-requirements.txt"
LOCK_FILE="/var/lock/ybb-runtime-prepare.lock"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

acquire_runtime_lock() {
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  if command -v flock >/dev/null 2>&1; then
    if ! flock -n 9; then
      echo "Runtime-Vorbereitung laeuft bereits in dieser WSL-Distribution. Bitte warten oder den alten Build beenden." >&2
      exit 73
    fi
    printf 'pid=%s\nstarted_at=%s\n' "$$" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" 1>&9
    log "Runtime-Sperre aktiv: $LOCK_FILE"
    return
  fi

  local fallback_lock="${LOCK_FILE}.d"
  if ! mkdir "$fallback_lock" 2>/dev/null; then
    echo "Runtime-Vorbereitung laeuft bereits in dieser WSL-Distribution. Bitte warten oder den alten Build beenden." >&2
    exit 73
  fi
  trap 'rmdir "$fallback_lock" 2>/dev/null || true' EXIT
  log "Runtime-Sperre aktiv: $fallback_lock"
}

clean_uv_cache_after_failed_install() {
  log "Bereinige uv-Cache nach fehlgeschlagener Paketinstallation."
  uv cache clean || true
  find "${HOME}/.cache/uv" -type f \( \
    -iname "*onnx*" -o \
    -iname "*onnxruntime*" -o \
    -iname "*nvidia_cudnn*" -o \
    -iname "*torch*" -o \
    -iname "*torchvision*" -o \
    -iname "*mmcv*" \
  \) -delete 2>/dev/null || true
}

install_with_retry() {
  local venv_path="$1"
  shift
  local python_path="$venv_path/bin/python"

  if uv pip install --python "$python_path" "$@"; then
    return 0
  fi

  clean_uv_cache_after_failed_install
  log "Wiederhole Paketinstallation mit frischem venv ohne uv-Cache."
  rm -rf "$venv_path"
  uv venv --python 3.11 "$venv_path"
  uv pip install --no-cache --python "$python_path" "$@"
}

ensure_apt_packages() {
  if [[ "$INSTALL_APT" -ne 1 ]]; then
    log "APT-Installation wurde uebersprungen."
    return
  fi

  export DEBIAN_FRONTEND=noninteractive
  log "Installiere Systempakete fuer Offline-Runtime."
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates \
    coturn \
    curl \
    ffmpeg \
    git \
    rsync \
    tar \
    gzip \
    openssl \
    build-essential \
    pkg-config \
    libpq-dev \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libxcb1 \
    python3-gi \
    python3-gst-1.0 \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-rtsp-server-1.0 \
    gir1.2-aravis-0.8 \
    libaravis-0.8-0 \
    aravis-tools-cli \
    gstreamer1.0-tools \
    gstreamer1.0-rtsp \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    postgresql \
    postgresql-contrib \
    redis-server \
    caddy
  rm -rf /var/lib/apt/lists/*
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi

  log "Installiere uv."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  if [[ -x "$HOME/.local/bin/uv" ]]; then
    ln -sf "$HOME/.local/bin/uv" /usr/local/bin/uv
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv installation failed." >&2
    exit 1
  fi
}

sync_source() {
  log "Synchronisiere Repo nach $SOURCE_ROOT."
  mkdir -p "$RUNTIME_ROOT"
  rm -rf "$SOURCE_ROOT"
  mkdir -p "$SOURCE_ROOT"
  rsync -a --delete \
    --exclude ".git" \
    --exclude ".venv" \
    --exclude "node_modules" \
    --exclude "dist" \
    --exclude ".runtime" \
    --exclude ".local" \
    --exclude ".pytest_cache" \
    --exclude ".ruff_cache" \
    --exclude ".mypy_cache" \
    --exclude "runs" \
    --exclude "mlruns" \
    --exclude "*.pt" \
    --exclude "*.pth" \
    --exclude "*.onnx" \
    --exclude "*.engine" \
    "$REPO_DIR/" "$SOURCE_ROOT/"
}

write_runtime_requirements() {
  log "Erzeuge Runtime-Requirements aus dem Workspace."
  python3 - <<'PY'
from pathlib import Path
import tomllib

runtime_root = Path("/opt/ybb-runtime")
source_root = runtime_root / "source"
pyproject = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))

members = pyproject["tool"]["uv"]["workspace"]["members"]
workspace_lines = [f"-e {source_root / member}" for member in members]
(runtime_root / "workspace-editables.txt").write_text(
    "\n".join(workspace_lines) + "\n",
    encoding="utf-8",
)

training_requirements = pyproject.get("dependency-groups", {}).get("training-runtime", [])
(runtime_root / "training-runtime-requirements.txt").write_text(
    "\n".join(training_requirements) + "\n",
    encoding="utf-8",
)
PY
}

install_service_venv() {
  log "Installiere Python-Service-Runtime in $SERVICE_VENV."
  cd "$SOURCE_ROOT"
  uv python install 3.11
  rm -rf "$SERVICE_VENV"
  uv venv --python 3.11 "$SERVICE_VENV"
  install_with_retry "$SERVICE_VENV" \
    -r "$WORKSPACE_REQUIREMENTS" \
    "mlflow>=2.16.2,<3" \
    "label-studio>=1.13,<2"
}

install_training_venv() {
  if [[ "$INSTALL_TRAINING_RUNTIME" -ne 1 ]]; then
    log "Training-Runtime wurde uebersprungen."
    return
  fi

  log "Installiere Python-Training-Runtime in $TRAINING_VENV."
  cd "$SOURCE_ROOT"
  rm -rf "$TRAINING_VENV"
  uv venv --python 3.11 "$TRAINING_VENV"
  install_with_retry "$TRAINING_VENV" \
    -r "$WORKSPACE_REQUIREMENTS" \
    -r "$TRAINING_REQUIREMENTS"
}

ensure_training_cuda_runtime_links() {
  if [[ ! -x "$TRAINING_VENV/bin/python" ]]; then
    return
  fi

  log "Repariere CUDA-NVRTC-Bibliothekslinks fuer die Training-Runtime."
  "$TRAINING_VENV/bin/python" - <<'PY'
from pathlib import Path
import importlib.util
import sys

if importlib.util.find_spec("torch") is None:
    raise SystemExit(0)

import torch

torch_lib = Path(torch.__file__).resolve().parent / "lib"
candidates = sorted(torch_lib.glob("libnvrtc-*.so.*"))
if not candidates:
    raise SystemExit(f"No bundled libnvrtc library was found in {torch_lib}.")
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
    raise SystemExit(f"No bundled libnvrtc-builtins library was found in {torch_lib}.")
builtins_target = builtins[0]
for link_name in ("libnvrtc-builtins.so", builtins_target.name):
    link_path = system_lib / link_name
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(builtins_target)

print({"libnvrtc_target": str(target), "libnvrtc_builtins_target": str(builtins_target)})
PY
}

install_triton_archive() {
  if [[ -z "$TRITON_ARCHIVE" ]]; then
    return
  fi
  if [[ ! -f "$TRITON_ARCHIVE" ]]; then
    echo "Triton archive was not found: $TRITON_ARCHIVE" >&2
    exit 1
  fi

  log "Installiere Triton Server aus Archiv: $TRITON_ARCHIVE"
  rm -rf /opt/tritonserver
  mkdir -p /opt/tritonserver
  tar -xf "$TRITON_ARCHIVE" -C /opt/tritonserver --strip-components=1
}

pythonpath_for_source() {
  local root="$SOURCE_ROOT"
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

smoke_test_runtime() {
  log "Pruefe Service-Runtime-Imports."
  local pythonpath
  pythonpath="$(pythonpath_for_source)"
  PYTHONPATH="$pythonpath" "$SERVICE_VENV/bin/python" - <<'PY'
import alembic
import fastapi
import redis
import rq
import sqlalchemy
import uvicorn
import yaml
import cv2
import numpy
import cv_core
import cv_db
import cv_edge
import cv_runtime
import admin_api.main
import runtime_admin_api.main
import inference_gateway.main_realtime
import realtime_executor.main
import result_service.main
print("service-runtime-imports-ok")
PY

  "$SERVICE_VENV/bin/python" - <<'PY'
import importlib.metadata

print("mlflow", importlib.metadata.version("mlflow"))
print("label-studio", importlib.metadata.version("label-studio"))
PY
  "$SERVICE_VENV/bin/dvc" --version >/dev/null

  if [[ "$INSTALL_TRAINING_RUNTIME" -eq 1 ]]; then
    log "Pruefe Training-Runtime-Basisimports."
    PYTHONPATH="$pythonpath" "$TRAINING_VENV/bin/python" - <<'PY'
import cv_training
import training_worker.worker
import numpy
import PIL
print("training-runtime-imports-ok")
PY
    "$TRAINING_VENV/bin/dvc" --version >/dev/null
  fi

  if [[ "$SKIP_TRITON_CHECK" -ne 1 ]]; then
    if [[ ! -x /opt/tritonserver/bin/tritonserver ]] && ! command -v tritonserver >/dev/null 2>&1; then
      echo "Triton Server wurde nicht gefunden. Setzen Sie TritonServerArchivePath oder PrepareRuntimeSkipTritonCheck=true." >&2
      exit 1
    fi
  fi
}

smoke_test_aravis_rtsp_bridge() {
  if ! command -v gst-inspect-1.0 >/dev/null 2>&1; then
    if [[ "$INSTALL_APT" -eq 1 ]]; then
      echo "gst-inspect-1.0 wurde nicht gefunden; die Aravis-RTSP-Bridge ist unvollstaendig." >&2
      exit 1
    fi
    log "Aravis-RTSP-Bridge-Pruefung uebersprungen, weil APT-Installation uebersprungen wurde."
    return
  fi

  log "Pruefe optionale Aravis-RTSP-Bridge-Komponenten."
  gst-inspect-1.0 appsrc >/dev/null
  gst-inspect-1.0 rawvideoparse >/dev/null
  gst-inspect-1.0 videoconvert >/dev/null
  gst-inspect-1.0 h264parse >/dev/null
  gst-inspect-1.0 aravissrc >/dev/null
  gst-inspect-1.0 bayer2rgb >/dev/null
  gst-inspect-1.0 rtph264pay >/dev/null
  gst-inspect-1.0 rtspclientsink >/dev/null
  if gst-inspect-1.0 nvcudah264enc >/dev/null 2>&1; then
    log "Moderner NVENC-GStreamer-Encoder ist verfuegbar: nvcudah264enc."
    if gst-inspect-1.0 cudaupload >/dev/null 2>&1 \
      && gst-inspect-1.0 cudaconvert >/dev/null 2>&1; then
      log "GStreamer CUDA-Upload fuer NVENC ist verfuegbar: cudaupload."
      local nvenc_info diagnostic_line
      local nvenc_last_attempt="" nvenc_last_exit_code="" nvenc_last_error=""
      nvenc_info="$(gst-inspect-1.0 nvcudah264enc 2>/dev/null || true)"

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

      if ! grep -Eq '(^|[[:space:]])p1([[:space:]]|-|$)' <<<"$nvenc_info" \
        || ! grep -Eq '(^|[[:space:]])ultra-low-latency([[:space:]]|-|$)' <<<"$nvenc_info" \
        || ! grep -Eq '^[[:space:]]+zero-reorder-delay[[:space:]]*:' <<<"$nvenc_info"; then
        log "WARNUNG: nvcudah264enc bietet das verpflichtende moderne Profil preset=p1/tune=ultra-low-latency/zero-reorder-delay nicht an; Live-Streams werden als failed markiert."
      elif run_nvenc_encoder_smoke preset=p1 tune=ultra-low-latency zero-reorder-delay=true; then
        log "NVENC-GStreamer-Smoke-Test erfolgreich: p1/ultra-low-latency/zero-reorder-delay."
      else
        log "WARNUNG: nvcudah264enc/cudaupload/cudaconvert sind verfuegbar, aber der verpflichtende GStreamer-NVENC-Smoke-Test ist fehlgeschlagen (Versuch: ${nvenc_last_attempt:-unbekannt}, Exit Code: ${nvenc_last_exit_code:-unbekannt}); Live-Streams werden als failed markiert."
        if [[ -n "$nvenc_last_error" ]]; then
          log "NVENC-Diagnose (letzte maximal 60 stderr-Zeilen):"
          while IFS= read -r diagnostic_line; do
            log "NVENC: $diagnostic_line"
          done <<<"$nvenc_last_error"
        else
          log "NVENC-Diagnose: gst-launch-1.0 lieferte keine stderr-Ausgabe."
        fi
      fi
    else
      log "WARNUNG: nvcudah264enc ist verfuegbar, aber cudaupload oder cudaconvert fehlt; Live-Streams werden als failed markiert."
    fi
  else
    log "WARNUNG: nvcudah264enc ist nicht verfuegbar; Live-Streams werden als failed markiert."
  fi

  /usr/bin/python3 - <<'PY'
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer  # noqa: F401

print("aravis-rtsp-bridge-imports-ok")
PY
}

write_runtime_marker() {
  mkdir -p "$RUNTIME_ROOT"
  cat >"$RUNTIME_ROOT/runtime-build-info.json" <<EOF
{
  "prepared_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "source_root": "$SOURCE_ROOT",
  "service_venv": "$SERVICE_VENV",
  "training_venv": "$TRAINING_VENV",
  "training_runtime_installed": $INSTALL_TRAINING_RUNTIME,
  "triton_checked": $((1 - SKIP_TRITON_CHECK))
}
EOF
}

acquire_runtime_lock
ensure_apt_packages
ensure_uv
sync_source
write_runtime_requirements
install_service_venv
install_training_venv
ensure_training_cuda_runtime_links
install_triton_archive
smoke_test_runtime
smoke_test_aravis_rtsp_bridge
write_runtime_marker

log "WSL-Runtime ist vorbereitet."
