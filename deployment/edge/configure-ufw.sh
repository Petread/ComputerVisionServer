#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this firewall helper as root." >&2
  exit 2
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Read device.env as data, including OS names with spaces, without shell execution.
exec python3 -I "${SCRIPT_DIR}/edge-system-packages.py" configure \
  --config "${1:-/etc/ybb-edge/device.env}"
