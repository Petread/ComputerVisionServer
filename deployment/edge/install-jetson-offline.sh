#!/usr/bin/env bash
set -euo pipefail

BUNDLE_ROOT="$(pwd)"
DEVICE_CONFIG=""
VERIFY_ONLY="false"
if [[ $# -gt 0 && "$1" != --* ]]; then
  BUNDLE_ROOT="$1"
  shift
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) DEVICE_CONFIG="$2"; shift 2 ;;
    --verify-only) VERIFY_ONLY="true"; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
INSTALL_ROOT="/opt/ybb-edge"
STATE_ROOT="/var/lib/ybb-edge"
CONFIG_ROOT="/etc/ybb-edge"
SERVICE_USER="ybb-edge"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 2
fi
if [[ ! -d "${BUNDLE_ROOT}/wheelhouse" ]]; then
  echo "Offline wheelhouse missing: ${BUNDLE_ROOT}/wheelhouse" >&2
  exit 3
fi
if [[ ! -f "${BUNDLE_ROOT}/device.env" ]]; then
  echo "Device configuration missing: ${BUNDLE_ROOT}/device.env" >&2
  exit 4
fi
if [[ ! -f "${BUNDLE_ROOT}/SHA256SUMS" ]]; then
  echo "Mandatory checksum manifest missing: ${BUNDLE_ROOT}/SHA256SUMS" >&2
  exit 5
fi
if [[ ! -f "${BUNDLE_ROOT}/SIGNATURE_POLICY" ]]; then
  echo "Release signature policy is missing." >&2
  exit 6
fi
(cd "${BUNDLE_ROOT}" && sha256sum --check SHA256SUMS)
SIGNATURE_POLICY="$(tr -d '\r\n' < "${BUNDLE_ROOT}/SIGNATURE_POLICY")"
TRUSTED_RELEASE_KEY="${YBB_EDGE_TRUSTED_RELEASE_KEY:-/etc/ybb-edge/release-signing-public.pem}"
case "${SIGNATURE_POLICY}" in
  prepared) ;;
  signed|required)
    command -v openssl >/dev/null 2>&1 || { echo "OpenSSL is required for signed releases." >&2; exit 7; }
    [[ -f "${BUNDLE_ROOT}/SHA256SUMS.sig" ]] || { echo "Release signature missing." >&2; exit 8; }
    [[ -f "${TRUSTED_RELEASE_KEY}" ]] || { echo "Pinned release public key missing: ${TRUSTED_RELEASE_KEY}" >&2; exit 9; }
    openssl dgst -sha256 -verify "${TRUSTED_RELEASE_KEY}" \
      -signature "${BUNDLE_ROOT}/SHA256SUMS.sig" "${BUNDLE_ROOT}/SHA256SUMS"
    ;;
  *) echo "Unsupported SIGNATURE_POLICY: ${SIGNATURE_POLICY}" >&2; exit 10 ;;
esac
if [[ "${VERIFY_ONLY}" == "true" ]]; then
  echo "Bundle checksums and release signature policy verified."
  exit 0
fi

getent group "${SERVICE_USER}" >/dev/null || groupadd --system "${SERVICE_USER}"
id "${SERVICE_USER}" >/dev/null 2>&1 || useradd \
  --system --gid "${SERVICE_USER}" --home-dir "${STATE_ROOT}" --shell /usr/sbin/nologin \
  "${SERVICE_USER}"
for device_group in video render; do
  if getent group "${device_group}" >/dev/null; then
    usermod -aG "${device_group}" "${SERVICE_USER}"
  fi
done
install -d -m 0755 "${INSTALL_ROOT}" "${CONFIG_ROOT}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 \
  "${STATE_ROOT}" "${STATE_ROOT}/artifacts" "${STATE_ROOT}/uploads" \
  "${STATE_ROOT}/state" "${STATE_ROOT}/engines"
CONFIG_SOURCE="${DEVICE_CONFIG:-${BUNDLE_ROOT}/device.env}"
if [[ -z "${DEVICE_CONFIG}" && -f "${CONFIG_ROOT}/device.env" ]]; then
  CONFIG_SOURCE="${CONFIG_ROOT}/device.env"
fi
[[ -f "${CONFIG_SOURCE}" ]] || { echo "UI-generated device configuration missing: ${CONFIG_SOURCE}" >&2; exit 11; }
python3 "${BUNDLE_ROOT}/deployment/edge/validate-device-env.py" \
  --input "${CONFIG_SOURCE}" \
  --output "${CONFIG_ROOT}/device.env" \
  --profile "${BUNDLE_ROOT}/platform-profile.json"
chown root:"${SERVICE_USER}" "${CONFIG_ROOT}/device.env"
chmod 0640 "${CONFIG_ROOT}/device.env"

systemctl stop ybb-edge-watchdog.timer ybb-edge-agent.service 2>/dev/null || true
python3 -m venv --system-site-packages "${INSTALL_ROOT}/venv"
"${INSTALL_ROOT}/venv/bin/python" -m pip install \
  --no-index --upgrade --force-reinstall --find-links "${BUNDLE_ROOT}/wheelhouse" --disable-pip-version-check \
  --requirement "${BUNDLE_ROOT}/runtime-requirements.txt"

install -m 0644 "${BUNDLE_ROOT}/deployment/edge/ybb-edge-agent.service" \
  /etc/systemd/system/ybb-edge-agent.service
install -m 0644 "${BUNDLE_ROOT}/deployment/edge/ybb-edge-watchdog.service" \
  /etc/systemd/system/ybb-edge-watchdog.service
install -m 0644 "${BUNDLE_ROOT}/deployment/edge/ybb-edge-watchdog-recover.service" \
  /etc/systemd/system/ybb-edge-watchdog-recover.service
install -m 0644 "${BUNDLE_ROOT}/deployment/edge/ybb-edge-watchdog.timer" \
  /etc/systemd/system/ybb-edge-watchdog.timer
install -m 0755 "${BUNDLE_ROOT}/deployment/edge/edge-watchdog.py" \
  "${INSTALL_ROOT}/edge-watchdog.py"
install -m 0755 "${BUNDLE_ROOT}/deployment/edge/edge-runtime-launcher.py" \
  "${INSTALL_ROOT}/edge-runtime-launcher.py"
install -m 0644 "${BUNDLE_ROOT}/deployment/edge/check-runtime-dependencies.py" \
  "${INSTALL_ROOT}/check-runtime-dependencies.py"

systemctl daemon-reload
systemctl enable --now ybb-edge-agent.service ybb-edge-watchdog.timer
echo "YBB Edge Runtime installed fully offline."
