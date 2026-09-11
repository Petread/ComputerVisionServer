FROM python:3.11-slim

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
  && apt-get install -y --no-install-recommends build-essential curl git libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libxcb1 \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /workspace

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

ARG UV_SYNC_ARGS="--all-packages --group training-runtime"

ENV UV_PROJECT_ENVIRONMENT="/opt/cv-platform-training-venv"
ENV UV_PYTHON_DOWNLOADS="never"
ENV PATH="/opt/cv-platform-training-venv/bin:${PATH}"

RUN python -m venv /opt/cv-platform-training-venv

RUN python - <<'PY'
from pathlib import Path

path = Path("pyproject.toml")
text = path.read_text(encoding="utf-8")
start = text.index("[tool.uv]\n")
end = text.index("\n[tool.uv.workspace]", start)
replacement = """[tool.uv]
environments = [
  "sys_platform == 'linux' and python_version == '3.11'",
]
"""
path.write_text(text[:start] + replacement + text[end + 1 :], encoding="utf-8")
PY

RUN uv sync --python /usr/local/bin/python3.11 ${UV_SYNC_ARGS}

RUN python - <<'PY'
from pathlib import Path

torch_lib = Path("/opt/cv-platform-training-venv/lib/python3.11/site-packages/torch/lib")
candidates = sorted(torch_lib.glob("libnvrtc-*.so.*"))
if not candidates:
    raise SystemExit("No bundled libnvrtc library was found in torch/lib.")
target = candidates[0]
system_lib = Path("/usr/lib/x86_64-linux-gnu")
for link_name in ("libnvrtc.so", f"libnvrtc.so.{target.name.split('.so.', maxsplit=1)[1]}"):
    link_path = system_lib / link_name
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(target)
builtins = sorted(torch_lib.glob("libnvrtc-builtins.so.*"))
if not builtins:
    raise SystemExit("No bundled libnvrtc-builtins library was found in torch/lib.")
builtins_target = builtins[0]
for link_name in ("libnvrtc-builtins.so", builtins_target.name):
    link_path = system_lib / link_name
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(builtins_target)
print({"libnvrtc_target": str(target), "libnvrtc_builtins_target": str(builtins_target)})
PY

RUN /opt/cv-platform-training-venv/bin/python -c "import numpy, pkg_resources; from torch.utils.cpp_extension import CUDA_HOME; assert int(numpy.__version__.split('.', 1)[0]) < 2, numpy.__version__; print({'numpy': numpy.__version__, 'pkg_resources': pkg_resources.__file__, 'cuda_home': CUDA_HOME})"
