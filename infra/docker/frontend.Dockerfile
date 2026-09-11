FROM node:20-bookworm-slim

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
  && apt-get install -y --no-install-recommends curl \
  && rm -rf /var/lib/apt/lists/* \
  && corepack enable

WORKDIR /workspace

COPY package.json pnpm-workspace.yaml ./
COPY frontend ./frontend

RUN pnpm install --no-frozen-lockfile

WORKDIR /workspace/frontend/apps/admin-web
