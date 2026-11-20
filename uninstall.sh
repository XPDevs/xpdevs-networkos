#!/usr/bin/env bash
#
# XPDevs NetworkOS - uninstaller
# ==============================
#   sudo bash uninstall.sh                # remove the product, keep data and Pi-hole
#   sudo bash uninstall.sh --purge        # also remove configuration and state
#   sudo bash uninstall.sh --purge --purge-pihole
#   sudo bash uninstall.sh --dry-run      # print what would happen, change nothing
#
# The wired uplink (eth0) is never touched, so management access survives.
# Everything the installer changed on the host is put back: masked units are
# unmasked, the wireless interface is returned to NetworkManager, and the
# product's runtime sockets and firewall table are removed.

set -Eeuo pipefail

readonly PRODUCT="XPDevs NetworkOS"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Paths are overridable so the uninstaller can be exercised against a throw-away
# tree in tests without touching the machine running them.
readonly APP_DIR="${NETWORKOS_APP_DIR:-/opt/xpdevs-networkos}"
readonly ETC_DIR="${NETWORKOS_ETC_DIR:-/etc/xpdevs-networkos}"
readonly STATE_DIR="${NETWORKOS_STATE_DIR:-/var/lib/xpdevs-networkos}"
readonly LOG_DIR="${NETWORKOS_LOG_DIR:-/var/log/xpdevs-networkos}"
readonly BACKUP_DIR="${NETWORKOS_BACKUP_DIR:-/var/backups/xpdevs-networkos}"
readonly SYSTEMD_DIR="${NETWORKOS_SYSTEMD_DIR:-/etc/systemd/system}"
readonly NM_DIR="${NETWORKOS_NM_DIR:-/etc/NetworkManager/conf.d}"
readonly RUN_DIR="${NETWORKOS_RUN_DIR:-/run}"
readonly SERVICE_USER="${NETWORKOS_SERVICE_USER:-xpdevs-networkos}"
readonly CTL="${NETWORKOS_CTL:-/usr/local/sbin/networkosctl}"
readonly SUDOERS_FILE="${NETWORKOS_SUDOERS_FILE:-/etc/sudoers.d/xpdevs-networkos}"
readonly LOGROTATE_FILE="${NETWORKOS_LOGROTATE_FILE:-/etc/logrotate.d/xpdevs-networkos}"
readonly AVAHI_FILE="${NETWORKOS_AVAHI_FILE:-/etc/avahi/services/xpdevs-networkos.service}"

readonly UNITS=(
  xpdevs-networkos-web.service
  xpdevs-networkos-health.timer
  xpdevs-networkos-health.service
  xpdevs-networkos-dnsmasq.service
  xpdevs-networkos-ap.service
  xpdevs-networkos-net.service
  xpdevs-networkos-mdns.service
  xpdevs-networkos.target
)

ASSUME_YES=0
PURGE=0
PURGE_PIHOLE=0
RESTORE_SERVICES=0
DRY_RUN=0

usage() {
  cat <<EOF
${PRODUCT} uninstaller

  sudo bash uninstall.sh [--yes] [--purge] [--purge-pihole]
                         [--restore-distro-services] [--dry-run]

  --yes                       do not ask for confirmation
  --purge                     also delete configuration, database and logs
  --purge-pihole              also remove Pi-hole (implies --purge)
  --restore-distro-services   also re-enable the distribution hostapd/dnsmasq
                              units (unmasking happens either way)
  --dry-run                   show every action without performing it
EOF
}

for arg in "$@"; do
  case "${arg}" in
    --yes|-y) ASSUME_YES=1 ;;
    --purge) PURGE=1 ;;
    --purge-pihole) PURGE_PIHOLE=1; PURGE=1 ;;
    --restore-distro-services) RESTORE_SERVICES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "${arg}" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }

if [[ "${DRY_RUN}" -eq 0 ]]; then
  [[ "${EUID}" -eq 0 ]] || { printf 'Run as root: sudo bash uninstall.sh\n' >&2; exit 1; }
fi

if [[ "${ASSUME_YES}" -eq 0 && "${DRY_RUN}" -eq 0 ]]; then
  printf '%s\n' "This removes ${PRODUCT}: services, sudo policy, dashboard and helper."
  [[ "${PURGE}" -eq 1 ]] && printf '%s\n' "Configuration, state and logs will also be deleted."
  [[ "${PURGE_PIHOLE}" -eq 1 ]] && printf '%s\n' "Pi-hole will be removed as well."
  read -r -p "Continue? [y/N]: " answer
  [[ "${answer}" =~ ^[Yy]$ ]] || { printf 'Aborted.\n'; exit 1; }
fi

# Run a command, honouring --dry-run and never aborting the cleanup.
run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    say "  [dry-run] $*"
    return 0
  fi
  "$@" || true
}

# Delete a path, refusing anything that is not one of the product's own paths.
# An allowlist is used rather than a prefix test so a mistyped or injected value
# can never remove something the product does not own.
MANAGED_PATHS=(
  "${APP_DIR}"
  "${ETC_DIR}"
  "${STATE_DIR}"
  "${LOG_DIR}"
  "${RUN_DIR}/xpdevs-hostapd"
)
safe_rm() {
  local target="${1:-}"
  if [[ -z "${target}" || "${target}" == "/" ]]; then
    printf 'Refusing to remove an empty path or "/"\n' >&2
    return 1
  fi
  local managed=0
  for candidate in "${MANAGED_PATHS[@]}"; do
    [[ "${target}" == "${candidate}" ]] && managed=1
  done
  if [[ "${managed}" -eq 0 ]]; then
    printf 'Refusing to remove a path the product does not own: %s\n' "${target}" >&2
    return 1
  fi
  run rm -rf "${target}"
}

have() { command -v "$1" >/dev/null 2>&1; }

say "Stopping and disabling services"
for unit in "${UNITS[@]}"; do
  run systemctl stop "${unit}"
  run systemctl disable "${unit}"
done

say "Removing systemd units"
for unit in "${UNITS[@]}"; do
  run rm -f "${SYSTEMD_DIR}/${unit}"
done
run systemctl daemon-reload
run systemctl reset-failed

say "Removing the firewall table"
if have nft; then
  run nft delete table inet networkos
else
  say "  nftables is not installed; nothing to remove"
fi

say "Releasing the wireless interface"
LAN_IF=""
if [[ -f "${ETC_DIR}/config.toml" ]]; then
  LAN_IF="$(sed -n 's/^lan_interface = "\(.*\)"$/\1/p' "${ETC_DIR}/config.toml" | head -n1)"
fi
LAN_IF="${LAN_IF:-wlan0}"
if have ip; then
  run ip addr flush dev "${LAN_IF}"
  run ip link set "${LAN_IF}" up
fi
if have sysctl; then
  run sysctl -w net.ipv4.ip_forward=0
fi

say "Restoring host services the installer changed"
# The installer masks these to take ownership of the radio and port 67. Leaving
# them masked would leave the Pi with no Wi-Fi at all after the product is gone,
# so unmasking is unconditional; re-enabling them is opt-in.
run systemctl unmask hostapd.service
run systemctl unmask dnsmasq.service
run systemctl unmask "wpa_supplicant@${LAN_IF}.service"
if [[ "${RESTORE_SERVICES}" -eq 1 ]]; then
  run systemctl enable hostapd.service
  run systemctl enable dnsmasq.service
  run systemctl enable "wpa_supplicant@${LAN_IF}.service"
fi

# The installer marks the AP interface unmanaged in NetworkManager. Left in
# place, NetworkManager would never touch wlan0 again.
NM_CONF="${NM_DIR}/xpdevs-networkos.conf"
if [[ -f "${NM_CONF}" ]]; then
  say "Returning ${LAN_IF} to NetworkManager"
  run rm -f "${NM_CONF}"
fi

say "Removing helper, sudo policy and log rotation"
run rm -f "${CTL}"
run rm -f "${SUDOERS_FILE}"
run rm -f "${LOGROTATE_FILE}"
run rm -f "${AVAHI_FILE}"

say "Removing runtime files"
run rm -rf "${RUN_DIR}/xpdevs-hostapd"
run rm -f "${RUN_DIR}/xpdevs-hostapd.pid" "${RUN_DIR}/xpdevs-dnsmasq.pid"

if [[ "${PURGE_PIHOLE}" -eq 1 ]]; then
  say "Removing Pi-hole"
  if have pihole; then
    run systemctl stop pihole-FTL.service
    run bash -c 'yes | pihole uninstall'
  else
    say "  Pi-hole is not installed"
  fi
fi

say "Removing the application"
safe_rm "${APP_DIR}"

if [[ "${PURGE}" -eq 1 ]]; then
  say "Removing configuration, state and logs"
  safe_rm "${ETC_DIR}"
  safe_rm "${STATE_DIR}"
  run rm -f "${LOG_DIR}/networkos.log" "${LOG_DIR}/networkos.log.1"
  run rm -f "${LOG_DIR}/audit.log" "${LOG_DIR}/audit.log.1"
  run rm -f "${LOG_DIR}/install.log"
  run rmdir "${LOG_DIR}"
fi

say "Restoring distribution networking"
run systemctl restart dhcpcd
run systemctl restart NetworkManager
run systemctl restart wpa_supplicant
run systemctl restart avahi-daemon

say ""
say "Verification"
remaining=0
for unit in "${UNITS[@]}"; do
  if [[ -f "${SYSTEMD_DIR}/${unit}" ]]; then
    say "  still present: ${SYSTEMD_DIR}/${unit}"
    remaining=$((remaining + 1))
  fi
done
[[ -e "${CTL}" ]] && { say "  still present: ${CTL}"; remaining=$((remaining + 1)); }
[[ -e "${NM_CONF}" ]] && { say "  still present: ${NM_CONF}"; remaining=$((remaining + 1)); }
if [[ -d "${APP_DIR}" ]]; then
  say "  still present: ${APP_DIR}"
  remaining=$((remaining + 1))
fi
if [[ "${DRY_RUN}" -eq 1 ]]; then
  say "  dry run: nothing was changed"
elif [[ "${remaining}" -eq 0 ]]; then
  say "  no product files remain"
else
  say "  ${remaining} item(s) could not be removed; see above"
fi

if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  say ""
  say "Service account ${SERVICE_USER} was left in place. Remove it with:"
  say "  userdel ${SERVICE_USER}"
fi

say ""
say "${PRODUCT} has been removed."
say "Kept for reference:"
say "  ${BACKUP_DIR}   pre-install network configuration snapshots"
[[ "${PURGE}" -eq 0 ]] && say "  ${ETC_DIR}   configuration (delete with --purge)"
exit 0
