#!/usr/bin/env bash
#
# XPDevs NetworkOS - uninstaller
# ==============================
#   sudo bash uninstall.sh                # remove the product, keep data and Pi-hole
#   sudo bash uninstall.sh --purge        # also remove configuration and state
#   sudo bash uninstall.sh --purge --purge-pihole
#
# The wired uplink (eth0) is never touched, so management access survives.

set -Eeuo pipefail

readonly PRODUCT="XPDevs NetworkOS"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly APP_DIR="/opt/xpdevs-networkos"
readonly ETC_DIR="/etc/xpdevs-networkos"
readonly STATE_DIR="/var/lib/xpdevs-networkos"
readonly LOG_DIR="/var/log/xpdevs-networkos"
readonly SERVICE_USER="xpdevs-networkos"
readonly CTL="/usr/local/sbin/networkosctl"
readonly UNITS=(
  xpdevs-networkos-web.service
  xpdevs-networkos-health.timer
  xpdevs-networkos-health.service
  xpdevs-networkos-dnsmasq.service
  xpdevs-networkos-ap.service
  xpdevs-networkos-net.service
  xpdevs-networkos.target
)

ASSUME_YES=0
PURGE=0
PURGE_PIHOLE=0
RESTORE_SERVICES=0

for arg in "$@"; do
  case "${arg}" in
    --yes|-y) ASSUME_YES=1 ;;
    --purge) PURGE=1 ;;
    --purge-pihole) PURGE_PIHOLE=1; PURGE=1 ;;
    --restore-distro-services) RESTORE_SERVICES=1 ;;
    --help|-h)
      cat <<EOF
${PRODUCT} uninstaller

  sudo bash uninstall.sh [--yes] [--purge] [--purge-pihole] [--restore-distro-services]

  --yes                       do not ask for confirmation
  --purge                     also delete configuration, database and logs
  --purge-pihole              also remove Pi-hole (implies --purge)
  --restore-distro-services   unmask hostapd/dnsmasq and re-enable avahi defaults
EOF
      exit 0 ;;
    *) printf 'Unknown option: %s\n' "${arg}" >&2; exit 2 ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || { printf 'Run as root: sudo bash uninstall.sh\n' >&2; exit 1; }

if [[ "${ASSUME_YES}" -eq 0 ]]; then
  printf '%s\n' "This removes ${PRODUCT}: services, sudo policy, dashboard and helper."
  [[ "${PURGE}" -eq 1 ]] && printf '%s\n' "Configuration, state and logs will also be deleted."
  [[ "${PURGE_PIHOLE}" -eq 1 ]] && printf '%s\n' "Pi-hole will be removed as well."
  read -r -p "Continue? [y/N]: " answer
  [[ "${answer}" =~ ^[Yy]$ ]] || { printf 'Aborted.\n'; exit 1; }
fi

say() { printf '%s\n' "$*"; }

say "Stopping services"
for unit in "${UNITS[@]}"; do
  systemctl stop "${unit}" 2>/dev/null || true
  systemctl disable "${unit}" 2>/dev/null || true
done

say "Removing systemd units"
for unit in "${UNITS[@]}"; do
  rm -f "/etc/systemd/system/${unit}"
done
systemctl daemon-reload

say "Removing the firewall table"
if command -v nft >/dev/null 2>&1; then
  nft delete table inet networkos 2>/dev/null || true
fi

say "Releasing the wireless interface"
LAN_IF=""
if [[ -f "${ETC_DIR}/config.toml" ]]; then
  LAN_IF="$(sed -n 's/^lan_interface = "\(.*\)"$/\1/p' "${ETC_DIR}/config.toml" | head -n1)"
fi
LAN_IF="${LAN_IF:-wlan0}"
ip addr flush dev "${LAN_IF}" 2>/dev/null || true
sysctl -w net.ipv4.ip_forward=0 >/dev/null 2>&1 || true

say "Removing helper, sudo policy and log rotation"
rm -f "${CTL}"
rm -f /etc/sudoers.d/xpdevs-networkos
rm -f /etc/logrotate.d/xpdevs-networkos
rm -f /etc/avahi/services/xpdevs-networkos.service

if [[ "${RESTORE_SERVICES}" -eq 1 ]]; then
  systemctl unmask hostapd.service dnsmasq.service 2>/dev/null || true
fi

if [[ "${PURGE_PIHOLE}" -eq 1 ]]; then
  say "Removing Pi-hole"
  if command -v pihole >/dev/null 2>&1; then
    yes | pihole uninstall 2>/dev/null || true
  fi
fi

say "Removing the application"
rm -rf "${APP_DIR}"

if [[ "${PURGE}" -eq 1 ]]; then
  say "Removing configuration, state and logs"
  rm -rf "${ETC_DIR}" "${STATE_DIR}"
  rm -rf "${LOG_DIR}/networkos.log"* "${LOG_DIR}/audit.log"* "${LOG_DIR}/install.log"
  rmdir "${LOG_DIR}" 2>/dev/null || true
fi

say "Restoring distribution networking"
systemctl restart dhcpcd 2>/dev/null || systemctl restart NetworkManager 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  say "Service account already removed"
else
  say "Service account ${SERVICE_USER} left in place (it owns no files after --purge)"
fi

say ""
say "${PRODUCT} has been removed."
say "Separate files kept for reference:"
say "  /var/backups/xpdevs-networkos   pre-install network configuration snapshots"
[[ "${PURGE}" -eq 0 ]] && say "  ${ETC_DIR}   configuration (delete with --purge)"
exit 0
