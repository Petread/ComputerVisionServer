#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE="${1:-/etc/ybb-edge/device.env}"
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this firewall helper as root." >&2
  exit 2
fi
if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Configuration not found: ${CONFIG_FILE}" >&2
  exit 3
fi
set -a
# shellcheck source=/dev/null
source "${CONFIG_FILE}"
set +a
: "${YBB_EDGE_SERVER_IP:?YBB_EDGE_SERVER_IP is required}"
: "${YBB_EDGE_CONTROL_PORT:=9443}"
: "${YBB_EDGE_PLANT_API_PORT:=9444}"
: "${YBB_EDGE_PLANT_ALLOWED_IPS:=}"

ufw allow from "${YBB_EDGE_SERVER_IP}" to any port "${YBB_EDGE_CONTROL_PORT}" proto tcp \
  comment "YBB Edge control from orchestration server"
ufw deny "${YBB_EDGE_CONTROL_PORT}/tcp"
IFS=',' read -r -a plant_clients <<< "${YBB_EDGE_PLANT_ALLOWED_IPS}"
for client in "${plant_clients[@]}"; do
  client="${client//[[:space:]]/}"
  if [[ -n "${client}" ]]; then
    ufw allow from "${client}" to any port "${YBB_EDGE_PLANT_API_PORT}" proto tcp \
      comment "YBB Edge plant trigger client"
  fi
done
ufw deny "${YBB_EDGE_PLANT_API_PORT}/tcp"
echo "Firewall rule staged. Confirm maintenance access before enabling or reloading UFW."
