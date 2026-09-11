FROM python:3.12-slim

RUN set -eux; \
  find /etc/apt -type f \( -name "*.list" -o -name "*.sources" \) -exec sed -i \
    -e 's|http://deb.debian.org|https://deb.debian.org|g' \
    -e 's|http://security.debian.org|https://security.debian.org|g' {} +; \
  printf '%s\n' \
    'Acquire::https::Verify-Peer "false";' \
    'Acquire::https::Verify-Host "false";' \
    > /etc/apt/apt.conf.d/99insecure-bootstrap; \
  apt-get update; \
  apt-get install -y --no-install-recommends ca-certificates; \
  update-ca-certificates; \
  rm -f /etc/apt/apt.conf.d/99insecure-bootstrap; \
  apt-get update \
  && apt-get install -y --no-install-recommends build-essential curl ffmpeg git libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libxcb1 python3-gi python3-gst-1.0 gir1.2-gstreamer-1.0 \
    gstreamer1.0-libav gstreamer1.0-plugins-bad gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly gstreamer1.0-rtsp gstreamer1.0-tools \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /workspace

ENV UV_PROJECT_ENVIRONMENT="/opt/cv-platform-venv"
ENV UV_PYTHON_DOWNLOADS="never"
ENV PATH="/opt/cv-platform-venv/bin:${PATH}"

COPY pyproject.toml README.md package.json pnpm-workspace.yaml ./
COPY backend ./backend
COPY config ./config
COPY contracts ./contracts
COPY docs ./docs
COPY examples ./examples
COPY infra ./infra
COPY ml ./ml
COPY scripts ./scripts
COPY tests ./tests
COPY frontend ./frontend

ARG UV_SYNC_ARGS="--package admin-api --package webhook-receiver --package dataset-service --package training-orchestrator --package model-registry-service --package inference-gateway --package deployment-controller --package runtime-admin-api --package channel-service --package pipeline-definition-service --package runtime-orchestrator --package realtime-executor --package camera-media --package trigger-executor --package overlay-service --package capture-service --package result-service --package connector-service --package cv-edge --package edge-agent --package control-worker --package batch-worker --package maintenance-worker"

RUN python -m venv /opt/cv-platform-venv

RUN python - <<'PY'
from pathlib import Path

path = Path("pyproject.toml")
text = path.read_text(encoding="utf-8")
text = text.replace('  "backend/workers/training-worker",\n', "")
text = text.replace("training-worker = { workspace = true }\n", "")
training_group = "training-runtime = [\n"
if training_group in text:
    start = text.index(training_group)
    end = text.index("\n]\n", start) + len("\n]\n")
    text = text[:start] + text[end:]
tool_group = "[tool.uv.dependency-groups]\n"
if tool_group in text:
    start = text.index(tool_group)
    end = text.find("\n[", start + len(tool_group))
    text = text[:start] if end == -1 else text[:start] + text[end + 1 :]
path.write_text(text, encoding="utf-8")
PY

RUN uv sync --python /usr/local/bin/python3.12 ${UV_SYNC_ARGS}
