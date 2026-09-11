#!/usr/bin/env bash
set -euo pipefail

HOSTNAME_VALUE=""
ADDRESS=""
PREFIX="24"
GATEWAY=""
DNS=""
SERVER_IP=""
CONNECTION=""
NVME_MOUNT="/var/lib/ybb-edge"
RELEASE_PUBLIC_KEY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname) HOSTNAME_VALUE="$2"; shift 2 ;;
    --address) ADDRESS="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --gateway) GATEWAY="$2"; shift 2 ;;
    --dns) DNS="$2"; shift 2 ;;
    --server-ip) SERVER_IP="$2"; shift 2 ;;
    --connection) CONNECTION="$2"; shift 2 ;;
    --state-root) NVME_MOUNT="$2"; shift 2 ;;
    --release-public-key) RELEASE_PUBLIC_KEY="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then echo "Run as root." >&2; exit 3; fi
if [[ ! -f /etc/nv_tegra_release ]]; then echo "This is not a provisioned NVIDIA Jetson." >&2; exit 4; fi
if [[ -z "${HOSTNAME_VALUE}" || -z "${ADDRESS}" || -z "${GATEWAY}" || -z "${DNS}" || -z "${SERVER_IP}" || -z "${CONNECTION}" ]]; then
  echo "hostname, address, gateway, dns, server-ip and connection are required." >&2; exit 5
fi
if [[ ! "${HOSTNAME_VALUE}" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$ ]]; then echo "Invalid hostname." >&2; exit 6; fi
if [[ ! "${PREFIX}" =~ ^([8-9]|[12][0-9]|3[0-2])$ ]]; then echo "Invalid IPv4 prefix." >&2; exit 7; fi
for value in "${ADDRESS}" "${GATEWAY}" "${SERVER_IP}"; do
  python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "${value}"
done
if ! mountpoint -q "${NVME_MOUNT}"; then
  echo "State root must already be a mounted, formatted NVMe filesystem: ${NVME_MOUNT}" >&2
  echo "The provisioner intentionally never partitions or formats disks." >&2
  exit 8
fi

hostnamectl set-hostname "${HOSTNAME_VALUE}"
timedatectl set-timezone Europe/Vienna
timedatectl set-ntp true
nmcli connection modify "${CONNECTION}" \
  ipv4.method manual ipv4.addresses "${ADDRESS}/${PREFIX}" ipv4.gateway "${GATEWAY}" \
  ipv4.dns "${DNS}" ipv6.method disabled connection.autoconnect yes
nmcli connection up "${CONNECTION}"
getent group ybb-edge >/dev/null || groupadd --system ybb-edge
id ybb-edge >/dev/null 2>&1 || useradd --system --gid ybb-edge --home-dir "${NVME_MOUNT}" --shell /usr/sbin/nologin ybb-edge
install -d -o ybb-edge -g ybb-edge -m 0750 "${NVME_MOUNT}" "${NVME_MOUNT}/artifacts" "${NVME_MOUNT}/engines" "${NVME_MOUNT}/state" "${NVME_MOUNT}/uploads"
install -d -o root -g root -m 0755 /etc/ybb-edge
if [[ -n "${RELEASE_PUBLIC_KEY}" ]]; then
  [[ -f "${RELEASE_PUBLIC_KEY}" ]] || { echo "Release public key not found." >&2; exit 9; }
  install -o root -g root -m 0644 "${RELEASE_PUBLIC_KEY}" /etc/ybb-edge/release-signing-public.pem
fi

if command -v nvpmodel >/dev/null 2>&1; then nvpmodel -m 0; fi
if command -v jetson_clocks >/dev/null 2>&1; then jetson_clocks --store >/dev/null 2>&1 || true; fi
echo "Jetson base provisioning complete. Apply the offline release bundle next."
