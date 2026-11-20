#!/usr/bin/env bash
#
# XPDevs NetworkOS - one-command installer
# =========================================
# Turns a Raspberry Pi into a wireless router with network-wide ad blocking.
#
#   sudo bash install.sh
#
# The installer is idempotent: running it again repairs or reconfigures an
# existing installation. It never reconfigures the wired uplink (eth0), so the
# management connection survives a failed wireless change.
#
# Tested target: Raspberry Pi OS Lite 64-bit (bookworm / trixie), Raspberry Pi 3
# Model B. See docs/HARDWARE.md for the supported matrix.

set -Eeuo pipefail

readonly PRODUCT="XPDevs NetworkOS"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PRODUCT_VERSION="$(tr -d '\n' < "${SCRIPT_DIR}/VERSION" 2>/dev/null || echo "1.0.0")"
readonly APP_DIR="/opt/xpdevs-networkos"
readonly ETC_DIR="/etc/xpdevs-networkos"
readonly STATE_DIR="/var/lib/xpdevs-networkos"
readonly LOG_DIR="/var/log/xpdevs-networkos"
readonly BACKUP_DIR="/var/backups/xpdevs-networkos"
readonly SERVICE_USER="xpdevs-networkos"
readonly CTL="/usr/local/sbin/networkosctl"
readonly VENV="${APP_DIR}/venv"
readonly INSTALL_LOG="${LOG_DIR}/install.log"
readonly PIHOLE_INSTALLER_URL="https://install.pi-hole.net"

WORK_DIR=""
KEEP_CONFIG=0
CONFIG_EXISTING=0
PIHOLE_SKIPPED=0
SKIP_PACKAGES=0

# ---------------------------------------------------------------- presentation
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
  C_BLUE=$'\033[38;5;33m'; C_GREEN=$'\033[38;5;35m'
  C_RED=$'\033[38;5;160m'; C_YELLOW=$'\033[38;5;178m'
else
  C_RESET=""; C_DIM=""; C_BOLD=""; C_BLUE=""; C_GREEN=""; C_RED=""; C_YELLOW=""
fi

say()  { printf '%s\n' "$*"; }
log()  { printf '%s\n' "$*"; printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "${INSTALL_LOG}" 2>/dev/null || true; }
info() { printf '%s•%s %s\n' "${C_BLUE}" "${C_RESET}" "$*"; log "INFO $*"; }
ok()   { printf '%s✓%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; log "OK   $*"; }
warn() { printf '%s!%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*"; log "WARN $*"; }
die()  { printf '%s✗%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; log "FAIL $*"; exit 1; }
step() { printf '\n%s%s▸ %s%s\n' "${C_BOLD}${C_BLUE}" "" "$*" "${C_RESET}"; log "STEP $*"; }

on_error() {
  local status=$?
  printf '\n%sInstallation stopped at line %s (exit %s).%s\n' "${C_RED}" "${BASH_LINENO[0]:-?}" "${status}" "${C_RESET}" >&2
  warn "Nothing on the wired connection was modified; Ethernet management access should still work."
  warn "Logs: ${INSTALL_LOG}   Recovery steps: docs/TROUBLESHOOTING.md"
  exit "${status}"
}
trap on_error ERR

cleanup() {
  if [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" ]]; then
    find "${WORK_DIR}" -type f -exec shred -u {} + 2>/dev/null || rm -rf "${WORK_DIR}"
    rm -rf "${WORK_DIR}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# --------------------------------------------------------------------- helpers
usage() {
  cat <<EOF
${PRODUCT} ${PRODUCT_VERSION} installer

  sudo bash install.sh [options]

Options:
  --yes                 Accept the defaults and do not prompt (automation)
  --skip-pihole         Do not install Pi-hole; NetworkOS runs without DNS filtering
  --skip-packages       Skip apt-get (dependencies already present)
  --ssid NAME           Wireless network name
  --channel N           Wireless channel (1-13)
  --country CC          Two letter country code (GB, US, DE ...)
  --lan-address IP      Access point address (default 10.42.0.1)
  --help                Show this help

Values can also be provided with environment variables:
  NETWORKOS_SSID, NETWORKOS_WIFI_PASSWORD, NETWORKOS_ADMIN_USER,
  NETWORKOS_ADMIN_PASSWORD, NETWORKOS_CHANNEL, NETWORKOS_COUNTRY,
  NETWORKOS_LAN_ADDRESS, NETWORKOS_DHCP_START, NETWORKOS_DHCP_END

Re-running the installer repairs or reconfigures an existing installation.
EOF
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run this installer as root: sudo bash install.sh"
}

# Refuse to proceed without root before creating system files or changing services.
require_root

# The first preflight messages are logged, so create the log directory first.
mkdir -p "${LOG_DIR}" "${STATE_DIR}" "${ETC_DIR}"
chmod 750 "${LOG_DIR}" "${STATE_DIR}"
chmod 755 "${ETC_DIR}"

ask() { # ask <prompt> <default> <variable>
  local prompt="$1" default="$2" __var="$3" answer=""
  if [[ "${ASSUME_YES}" -eq 1 ]]; then
    printf -v "${__var}" '%s' "${default}"
    return
  fi
  if [[ -n "${default}" ]]; then
    read -r -p "$(printf '%s [%s]: ' "${prompt}" "${default}")" answer || true
    printf -v "${__var}" '%s' "${answer:-${default}}"
  else
    read -r -p "$(printf '%s: ' "${prompt}")" answer || true
    printf -v "${__var}" '%s' "${answer}"
  fi
}

ask_secret() { # ask_secret <prompt> <variable>
  local prompt="$1" __var="$2" first="" second=""
  if [[ "${ASSUME_YES}" -eq 1 ]]; then
    printf -v "${__var}" '%s' ""
    return
  fi
  while true; do
    read -r -s -p "$(printf '%s: ' "${prompt}")" first || true
    printf '\n'
    read -r -s -p "$(printf 'Confirm: ')" second || true
    printf '\n'
    if [[ "${first}" != "${second}" ]]; then
      warn "The values do not match. Try again."
      continue
    fi
    printf -v "${__var}" '%s' "${first}"
    return
  done
}

confirm() { # confirm <question> [default-yes]
  local prompt="$1" default="${2:-no}" answer=""
  if [[ "${ASSUME_YES}" -eq 1 ]]; then return 0; fi
  read -r -p "$(printf '%s [%s]: ' "${prompt}" "${default}")" answer || true
  answer="${answer:-${default}}"
  [[ "${answer}" =~ ^[Yy]$ ]]
}

has_cmd() { command -v "$1" >/dev/null 2>&1; }

http_ready() { # http_ready <url> [attempts]
  local url="$1" attempts="${2:-30}" i
  for ((i = 0; i < attempts; i++)); do
    if curl -fsS -m 3 -o /dev/null "${url}" 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

detect_model() {
  local model=""
  for candidate in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    if [[ -r "${candidate}" ]]; then
      model="$(tr -d '\0' < "${candidate}")"
      break
    fi
  done
  printf '%s' "${model:-Unknown hardware}"
}

first_interface() { # first_interface <prefix...>
  local prefixes=("$@") iface path
  while read -r path; do
    iface="$(basename "${path}")"
    for prefix in "${prefixes[@]}"; do
      [[ "${iface}" == ${prefix}* ]] && { printf '%s' "${iface}"; return 0; }
    done
  done < <(ls -1d /sys/class/net/* 2>/dev/null)
  return 1
}

interface_ipv4() {
  ip -4 -o addr show dev "$1" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1
}

# ------------------------------------------------------------------- preflight
for required_path in \
  "${SCRIPT_DIR}/VERSION" \
  "${SCRIPT_DIR}/requirements.txt" \
  "${SCRIPT_DIR}/networkos" \
  "${SCRIPT_DIR}/packaging/networkosctl" \
  "${SCRIPT_DIR}/packaging/systemd" \
  "${SCRIPT_DIR}/packaging/sudoers/xpdevs-networkos" \
  "${SCRIPT_DIR}/packaging/logrotate/xpdevs-networkos" \
  "${SCRIPT_DIR}/packaging/avahi/xpdevs-networkos.service"; do
  [[ -e "${required_path}" ]] || die "Required project file is missing: ${required_path}"
done

step "Checking the hardware and operating system"

MODEL="$(detect_model)"
ok "Hardware: ${MODEL}"
log "model=${MODEL}"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  OS_NAME="${PRETTY_NAME:-unknown}"
  OS_ID="${ID:-unknown}"
else
  OS_NAME="unknown"; OS_ID="unknown"
fi
ok "Operating system: ${OS_NAME}"

case "${OS_ID}" in
  debian|raspbian|ubuntu) : ;;
  *) warn "This installer targets Raspberry Pi OS or Debian. '${OS_ID}' may need manual adjustments." ;;
esac

ARCH="$(uname -m)"
case "${ARCH}" in
  aarch64|arm64) ok "Architecture: ${ARCH} (64-bit)" ;;
  armv7l|armv6l) warn "Architecture: ${ARCH}. Raspberry Pi OS Lite 64-bit is recommended; continuing anyway." ;;
  x86_64)
    warn "Architecture: ${ARCH}. NetworkOS targets the Raspberry Pi; wireless features will be unavailable here."
    ;;
  *) warn "Architecture: ${ARCH} is not a supported target; continuing for testing purposes." ;;
esac

AVAIL_KB="$(df -Pk / | awk 'NR==2 {print $4}')"
if (( AVAIL_KB < 1500000 )); then
  die "Not enough free space on / (need about 1.5 GB, found $((AVAIL_KB/1024)) MB)."
fi
ok "Free space on /: $((AVAIL_KB / 1024)) MB"

WAN_IF="${NETWORKOS_WAN_INTERFACE:-}"
if [[ -z "${WAN_IF}" ]]; then
  WAN_IF="$(first_interface eth en oth || true)"
fi
[[ -n "${WAN_IF}" ]] || die "No wired Ethernet interface found. A wired uplink is required."
ok "Wired uplink: ${WAN_IF}"

WLAN_IF="${NETWORKOS_LAN_INTERFACE:-}"
if [[ -z "${WLAN_IF}" ]]; then
  WLAN_IF="$(first_interface wlan wl || true)"
fi
[[ -n "${WLAN_IF}" ]] || die "No wireless interface found. This build requires the built-in Wi-Fi radio."
ok "Wireless interface: ${WLAN_IF}"

WAN_IP="$(interface_ipv4 "${WAN_IF}")"
[[ -n "${WAN_IP}" ]] || die "${WAN_IF} has no IPv4 address. Connect the Ethernet cable and check the DHCP server first."
ok "Ethernet address: ${WAN_IP}"

if ! ip route show default | grep -q "dev ${WAN_IF}"; then
  die "No default route on ${WAN_IF}. Without internet access dependencies cannot be installed."
fi
ok "Default route present"

if ! ip link show "${WLAN_IF}" | grep -q "state UP\|state UNKNOWN"; then
  ip link set "${WLAN_IF}" up 2>/dev/null || true
fi

if command -v iw >/dev/null 2>&1 && iw list 2>/dev/null | grep -q "^\s*\* AP"; then
  ok "The wireless radio supports access point mode"
else
  if command -v iw >/dev/null 2>&1; then
    die "The wireless radio does not advertise AP mode. NetworkOS cannot create an access point."
  else
    warn "'iw' is not installed yet; access point support will be verified after installation."
  fi
fi

if curl -fsS -m 10 -o /dev/null "${PIHOLE_INSTALLER_URL}" 2>/dev/null; then
  ok "Internet reachability confirmed"
elif curl -fsS -m 10 -o /dev/null "https://deb.debian.org/" 2>/dev/null; then
  ok "Internet reachability confirmed (Pi-hole site not reachable yet)"
else
  die "No internet access from ${WAN_IF}. NetworkOS cannot install packages without it."
fi

if systemctl list-unit-files 2>/dev/null | grep -q "^systemd-networkd.service"; then
  if systemctl is-active --quiet systemd-networkd; then
    warn "systemd-networkd is active; this installer assumes DHCP-managed Ethernet (dhcpcd or NetworkManager)."
  fi
fi

# ------------------------------------------------------------------- arguments
ASSUME_YES=0
SKIP_PIHOLE=0
OPT_SSID=""; OPT_CHANNEL=""; OPT_COUNTRY=""; OPT_LAN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --skip-pihole) SKIP_PIHOLE=1 ;;
    --skip-packages) SKIP_PACKAGES=1 ;;
    --ssid) OPT_SSID="${2:-}"; shift ;;
    --channel) OPT_CHANNEL="${2:-}"; shift ;;
    --country) OPT_COUNTRY="${2:-}"; shift ;;
    --lan-address) OPT_LAN="${2:-}"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
  shift
done

mkdir -p "${LOG_DIR}" "${STATE_DIR}" "${ETC_DIR}"
chmod 750 "${LOG_DIR}" "${STATE_DIR}"
chmod 755 "${ETC_DIR}"
log "=== ${PRODUCT} ${PRODUCT_VERSION} installer started on ${MODEL} (${OS_NAME}, ${ARCH}) ==="

WORK_DIR="$(mktemp -d /root/.networkos-install.XXXXXX)"
chmod 700 "${WORK_DIR}"

# ---------------------------------------------------------------- configuration
step "Collecting the wireless and administrator settings"

if [[ -f "${ETC_DIR}/config.toml" ]]; then
  CONFIG_EXISTING=1
fi

DEFAULT_SSID="${NETWORKOS_SSID:-XPDevs NetworkOS}"
DEFAULT_CHANNEL="${NETWORKOS_CHANNEL:-6}"
DEFAULT_COUNTRY="${NETWORKOS_COUNTRY:-}"
if [[ -z "${DEFAULT_COUNTRY}" && -r /etc/default/crda ]]; then
  DEFAULT_COUNTRY="$(sed -n 's/^REGDOMAIN=//p' /etc/default/crda 2>/dev/null | head -n1)"
fi
if [[ -z "${DEFAULT_COUNTRY}" && -r /etc/wpa_supplicant/wpa_supplicant.conf ]]; then
  DEFAULT_COUNTRY="$(sed -n 's/^country=//p' /etc/wpa_supplicant/wpa_supplicant.conf 2>/dev/null | head -n1)"
fi
DEFAULT_COUNTRY="${DEFAULT_COUNTRY:-GB}"

if [[ "${CONFIG_EXISTING}" -eq 1 && "${ASSUME_YES}" -eq 0 ]]; then
  if confirm "An existing NetworkOS configuration was found. Keep its wireless network name and password?" yes; then
    KEEP_CONFIG=1
  else
    KEEP_CONFIG=0
  fi
else
  KEEP_CONFIG=0
fi

SSID=""; WIFI_PASSWORD=""; CHANNEL="${OPT_CHANNEL:-${DEFAULT_CHANNEL}}"; COUNTRY="${OPT_COUNTRY:-${DEFAULT_COUNTRY}}"
LAN_ADDRESS="${OPT_LAN:-${NETWORKOS_LAN_ADDRESS:-10.42.0.1}}"
DHCP_START="${NETWORKOS_DHCP_START:-10.42.0.10}"
DHCP_END="${NETWORKOS_DHCP_END:-10.42.0.200}"

if [[ "${KEEP_CONFIG}" -eq 1 ]]; then
  SSID="$(sed -n 's/^ssid = "\(.*\)"$/\1/p' "${ETC_DIR}/config.toml" | head -n1)"
  CHANNEL="$(sed -n 's/^channel = \([0-9]*\)$/\1/p' "${ETC_DIR}/config.toml" | head -n1)"
  LAN_ADDRESS="$(sed -n 's/^lan_address = "\(.*\)"$/\1/p' "${ETC_DIR}/config.toml" | head -n1)"
  ok "Keeping the existing wireless configuration (${SSID:-unknown})"
else
  if [[ -z "${NETWORKOS_SSID:-}" ]]; then
    ask "Wireless network name (SSID)" "${DEFAULT_SSID}" SSID
  else
    SSID="${NETWORKOS_SSID}"
  fi
  [[ -n "${SSID}" ]] || die "The network name cannot be empty."
  (( ${#SSID} <= 32 )) || die "The network name must be 32 characters or fewer."

  if [[ -n "${NETWORKOS_WIFI_PASSWORD:-}" ]]; then
    WIFI_PASSWORD="${NETWORKOS_WIFI_PASSWORD}"
  elif [[ "${CONFIG_EXISTING}" -eq 1 ]]; then
    say "Leave blank to keep the current Wi-Fi password."
    while true; do
      read -r -s -p "Wi-Fi password (8-63 characters): " WIFI_PASSWORD || true
      printf '\n'
      if [[ -z "${WIFI_PASSWORD}" ]]; then break; fi
      if (( ${#WIFI_PASSWORD} >= 8 && ${#WIFI_PASSWORD} <= 63 )); then break; fi
      warn "Use between 8 and 63 characters."
    done
  else
    while true; do
      ask_secret "Wi-Fi password (8-63 characters)" WIFI_PASSWORD
      if (( ${#WIFI_PASSWORD} >= 8 && ${#WIFI_PASSWORD} <= 63 )); then break; fi
      warn "Use between 8 and 63 characters."
    done
  fi
  if [[ -z "${WIFI_PASSWORD}" && "${CONFIG_EXISTING}" -eq 0 ]]; then
    die "A Wi-Fi password is required."
  fi

  if [[ -z "${OPT_CHANNEL}" && -z "${NETWORKOS_CHANNEL:-}" ]]; then
    ask "Wireless channel (1-13)" "${DEFAULT_CHANNEL}" CHANNEL
  fi
  [[ "${CHANNEL}" =~ ^[0-9]+$ ]] && (( CHANNEL >= 1 && CHANNEL <= 13 )) || die "Choose a channel between 1 and 13."
  if [[ -z "${OPT_COUNTRY}" && -z "${NETWORKOS_COUNTRY:-}" ]]; then
    ask "Country code for radio regulations" "${DEFAULT_COUNTRY}" COUNTRY
  fi
  COUNTRY="$(printf '%s' "${COUNTRY}" | tr '[:lower:]' '[:upper:]')"
  [[ "${COUNTRY}" =~ ^[A-Z]{2}$ ]] || die "The country code must be two letters, for example GB."
fi

# The administrator password is chosen in the browser on the first visit, so it is
# never typed into this script and never exists in a shell variable or a log. An
# unattended installation may pre-create the account through the environment.
ADMIN_USER="${NETWORKOS_ADMIN_USER:-admin}"
ADMIN_USER="$(printf '%s' "${ADMIN_USER}" | tr '[:upper:]' '[:lower:]')"
[[ "${ADMIN_USER}" =~ ^[a-z0-9._-]{3,32}$ ]] || die "Use 3-32 lowercase letters, digits, dots, hyphens or underscores."
ADMIN_PASSWORD="${NETWORKOS_ADMIN_PASSWORD:-}"
if [[ -n "${ADMIN_PASSWORD}" ]]; then
  (( ${#ADMIN_PASSWORD} >= 10 )) || die "NETWORKOS_ADMIN_PASSWORD must be at least 10 characters."
fi

# Secrets go to files with strict permissions and never to the installer log.
printf '%s' "${WIFI_PASSWORD}" > "${WORK_DIR}/wifi.pass"
chmod 600 "${WORK_DIR}/wifi.pass"
if [[ -n "${ADMIN_PASSWORD}" ]]; then
  printf '%s' "${ADMIN_PASSWORD}" > "${WORK_DIR}/admin.pass"
  chmod 600 "${WORK_DIR}/admin.pass"
fi
PIHOLE_API_PASSWORD="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
printf '%s' "${PIHOLE_API_PASSWORD}" > "${WORK_DIR}/pihole.pass"
chmod 600 "${WORK_DIR}/pihole.pass"
unset ADMIN_PASSWORD PIHOLE_API_PASSWORD

# --------------------------------------------------------------------- backup
step "Backing up the current network configuration"
mkdir -p "${BACKUP_DIR}"
BACKUP_FILE="${BACKUP_DIR}/pre-install-$(date -u +%Y%m%d-%H%M%S).tar.gz"
BACKUP_TARGETS=()
for candidate in /etc/dhcpcd.conf /etc/network/interfaces /etc/network/interfaces.d /etc/hostapd /etc/dnsmasq.conf /etc/nftables.conf /etc/avahi/avahi-daemon.conf /etc/systemd/network /etc/pihole; do
  [[ -e "${candidate}" ]] && BACKUP_TARGETS+=("${candidate}")
done
if (( ${#BACKUP_TARGETS[@]} > 0 )); then
  tar -czf "${BACKUP_FILE}" "${BACKUP_TARGETS[@]}" 2>/dev/null || warn "Backup was incomplete; continuing."
  chmod 600 "${BACKUP_FILE}"
  ok "Saved ${BACKUP_FILE}"
else
  warn "Nothing to back up yet (fresh system)."
fi

restore_network_config() {
  if [[ -s "${BACKUP_FILE}" ]]; then
    tar -xzf "${BACKUP_FILE}" -C / 2>/dev/null || true
    warn "Restored the pre-install network configuration from ${BACKUP_FILE}"
  fi
  systemctl restart dhcpcd 2>/dev/null || systemctl restart NetworkManager 2>/dev/null || true
}

# ------------------------------------------------------------------- packages
step "Installing system packages"
if [[ "${SKIP_PACKAGES}" -eq 0 ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || die "apt-get update failed. Check the uplink connection."
  PACKAGES=(
    hostapd dnsmasq nftables iptables iproute2 iw rfkill wireless-regdb crda
    avahi-daemon libnss-mdns curl ca-certificates gnupg
    python3 python3-venv python3-pip python3-dev libffi-dev libssl-dev
    sqlite3 logrotate rsync sudo util-linux procps
  )
  if ! apt-get install -y -qq --no-install-recommends "${PACKAGES[@]}"; then
    warn "Some optional packages were unavailable; retrying the essential set."
    apt-get install -y -qq --no-install-recommends \
      hostapd dnsmasq nftables iproute2 iw avahi-daemon curl ca-certificates \
      python3 python3-venv python3-pip sqlite3 logrotate sudo
  fi
  ok "Packages installed"
else
  ok "Package installation skipped on request"
fi

for binary in hostapd dnsmasq nft iw; do
  has_cmd "${binary}" || die "Required program '${binary}' is missing after installation."
done

# --------------------------------------------------------------- service user
step "Creating the service account and directories"
if ! getent group "${SERVICE_USER}" >/dev/null; then
  groupadd --system "${SERVICE_USER}"
  ok "Created group ${SERVICE_USER}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${SERVICE_USER}" --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
  ok "Created user ${SERVICE_USER}"
fi
# Read the journal without root privileges.
usermod -aG systemd-journal "${SERVICE_USER}" 2>/dev/null || true
usermod -aG adm "${SERVICE_USER}" 2>/dev/null || true

install -d -m 0755 -o root -g root "${APP_DIR}"
install -d -m 0750 -o root -g "${SERVICE_USER}" "${ETC_DIR}"
install -d -m 0750 -o root -g "${SERVICE_USER}" "${ETC_DIR}/generated" "${ETC_DIR}/backups"
install -d -m 0750 -o root -g "${SERVICE_USER}" "${ETC_DIR}/secrets"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${STATE_DIR}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${LOG_DIR}"
ok "Directories are in place"

# ----------------------------------------------------------------- application
step "Installing the NetworkOS application"
rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.git' --exclude 'tests' \
  "${SCRIPT_DIR}/networkos" "${APP_DIR}/"
install -m 0644 "${SCRIPT_DIR}/VERSION" "${APP_DIR}/VERSION"
install -m 0644 "${SCRIPT_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"
if [[ -d "${SCRIPT_DIR}/docs" ]]; then
  install -d -m 0755 "${APP_DIR}/docs"
  rsync -a "${SCRIPT_DIR}/docs/" "${APP_DIR}/docs/"
fi
ok "Application copied to ${APP_DIR}"

if [[ ! -x "${VENV}/bin/python" ]]; then
  python3 -m venv "${VENV}" || die "Could not create the virtual environment (install python3-venv)."
fi
"${VENV}/bin/pip" install --quiet --upgrade pip setuptools wheel || warn "pip could not be upgraded; continuing."
if ! "${VENV}/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"; then
  warn "Full dependency install failed; retrying without Argon2 (scrypt fallback is used)."
  "${VENV}/bin/pip" install --quiet "Flask==3.1.2" "waitress==3.0.2" || die "Python dependencies could not be installed."
fi
if "${VENV}/bin/python" -c "import argon2" >/dev/null 2>&1; then
  ok "Python dependencies installed (Argon2id available)"
else
  warn "Argon2id is unavailable; passwords use the built-in scrypt hash instead."
fi

install -m 0755 "${SCRIPT_DIR}/packaging/networkosctl" "${CTL}"
ok "Installed the privileged helper at ${CTL}"

# --------------------------------------------------------------- configuration
step "Writing the product configuration"

export APP_DIR="${APP_DIR}"
export NETWORKOS_SSID="${SSID}"
export NETWORKOS_CHANNEL="${CHANNEL}"
export NETWORKOS_COUNTRY="${COUNTRY}"
export NETWORKOS_LAN="${LAN_ADDRESS}"
export NETWORKOS_DHCP_START="${DHCP_START}"
export NETWORKOS_DHCP_END="${DHCP_END}"
export NETWORKOS_WAN="${WAN_IF}"
export NETWORKOS_LAN_IF="${WLAN_IF}"
export NETWORKOS_WIFI_PASS_FILE="${WORK_DIR}/wifi.pass"
export NETWORKOS_VERSION="${PRODUCT_VERSION}"
export NETWORKOS_ETC_DIR="${ETC_DIR}"
export NETWORKOS_KEEP_CONFIG="${KEEP_CONFIG}"

"${VENV}/bin/python" - <<'PYCONF'
import os
import sys

sys.path.insert(0, os.environ["APP_DIR"])
from networkos import settings, util  # noqa: E402

etc = os.environ["NETWORKOS_ETC_DIR"]
path = os.path.join(etc, "config.toml")
config = settings.load(path=path) if os.environ.get("NETWORKOS_KEEP_CONFIG") == "1" else settings.Config()

config.wifi.ssid = os.environ["NETWORKOS_SSID"]
config.wifi.channel = int(os.environ["NETWORKOS_CHANNEL"])
config.wifi.country = os.environ["NETWORKOS_COUNTRY"]
config.wifi.enabled = True
config.network.wan_interface = os.environ["NETWORKOS_WAN"]
config.network.lan_interface = os.environ["NETWORKOS_LAN_IF"]
config.network.lan_address = os.environ["NETWORKOS_LAN"]
config.network.dhcp_start = os.environ["NETWORKOS_DHCP_START"]
config.network.dhcp_end = os.environ["NETWORKOS_DHCP_END"]
config.network.dns_servers = [os.environ["NETWORKOS_LAN"]]
config.product.version = os.environ["NETWORKOS_VERSION"]
config.product.installed_at = util.iso_now()

password_file = os.environ.get("NETWORKOS_WIFI_PASS_FILE", "")
if password_file and os.path.getsize(password_file) > 0:
    with open(password_file, "r", encoding="utf-8") as handle:
        passphrase = handle.read().strip()
    util.valid_passphrase(passphrase)
    config.wifi.psk = util.wpa_psk(config.wifi.ssid, passphrase)
elif not config.wifi.psk:
    raise SystemExit("A Wi-Fi password is required for a new installation.")

util.check_lan_plan(
    config.network.lan_address,
    config.network.lan_prefix,
    config.network.dhcp_start,
    config.network.dhcp_end,
)
settings.save(config, path=path)
print(f"configuration written to {path}")
PYCONF
ok "Configuration saved (only the derived WPA2 key is stored)"

# Stop services that would fight for the wireless radio or port 53.
step "Disabling conflicting services"
for unit in hostapd.service dnsmasq.service lighttpd.service; do
  if systemctl list-unit-files "${unit}" >/dev/null 2>&1; then
    systemctl stop "${unit}" 2>/dev/null || true
    systemctl disable "${unit}" 2>/dev/null || true
  fi
done
systemctl mask dnsmasq.service hostapd.service 2>/dev/null || true
ok "Distribution hostapd/dnsmasq units are masked; NetworkOS owns them now"

# --------------------------------------------------------------------- Pi-hole
step "Installing Pi-hole (network-wide ad blocking)"
if [[ "${SKIP_PIHOLE}" -eq 1 || "${SKIP_PIHOLE}" == "1" ]]; then
  PIHOLE_SKIPPED=1
  warn "Pi-hole installation skipped on request; DNS filtering will be unavailable."
elif has_cmd pihole || [[ -x /usr/bin/pihole-FTL || -x /usr/local/bin/pihole ]]; then
  ok "Pi-hole is already installed"
else
  info "Downloading the official Pi-hole installer"
  if ! curl -fsSL -o "${WORK_DIR}/pihole-install.sh" "${PIHOLE_INSTALLER_URL}"; then
    warn "The Pi-hole installer could not be downloaded. NetworkOS will install without DNS filtering."
    PIHOLE_SKIPPED=1
  else
    install -d -m 0755 /etc/pihole
    cat > /etc/pihole/setupVars.conf <<EOF
PIHOLE_INTERFACE=${WAN_IF}
IPV4_ADDRESS=${WAN_IP}/24
IPV6_ADDRESS=
PIHOLE_DNS_1=1.1.1.1
PIHOLE_DNS_2=9.9.9.9
QUERY_LOGGING=true
INSTALL_WEB_SERVER=false
INSTALL_WEB_INTERFACE=true
LIGHTTPD_ENABLED=false
CACHE_SIZE=10000
DNS_FQDN_REQUIRED=true
DNS_BOGUS_PRIV=true
DNSSEC=false
REV_SERVER=false
BLOCKING_ENABLED=true
PIHOLE_SKIP_OS_CHECK=true
EOF
    chmod 640 /etc/pihole/setupVars.conf
    export PIHOLE_SKIP_OS_CHECK=true
    if bash "${WORK_DIR}/pihole-install.sh" --unattended; then
      ok "Pi-hole installed"
    else
      warn "The Pi-hole installer reported a problem; NetworkOS continues and will report Pi-hole as unavailable."
      PIHOLE_SKIPPED=1
    fi
    rm -f "${WORK_DIR}/pihole-install.sh"
  fi
fi

if [[ "${PIHOLE_SKIPPED}" -eq 0 ]]; then
  info "Pointing Pi-hole at loopback and issuing a local API credential"
  if "${CTL}" pihole-configure --password-stdin < "${WORK_DIR}/pihole.pass" >/dev/null 2>&1; then
    ok "Pi-hole API configured on 127.0.0.1:8081"
  else
    warn "Pi-hole configuration did not complete; open the dashboard to review the Pi-hole status."
  fi
  systemctl stop lighttpd 2>/dev/null || true
  systemctl disable lighttpd 2>/dev/null || true
  "${CTL}" pihole-retention 7 >/dev/null 2>&1 || true
fi

# ------------------------------------------------------------- systemd & sudo
step "Installing systemd units and the sudo policy"
install -m 0644 "${SCRIPT_DIR}/packaging/systemd/"*.service /etc/systemd/system/
install -m 0644 "${SCRIPT_DIR}/packaging/systemd/"*.timer /etc/systemd/system/
install -m 0644 "${SCRIPT_DIR}/packaging/systemd/xpdevs-networkos.target" /etc/systemd/system/
install -m 0644 "${SCRIPT_DIR}/packaging/sudoers/xpdevs-networkos" /etc/sudoers.d/xpdevs-networkos
chmod 0440 /etc/sudoers.d/xpdevs-networkos
if has_cmd visudo; then
  visudo -c -f /etc/sudoers.d/xpdevs-networkos >/dev/null || die "The sudo policy failed validation."
fi
install -m 0644 "${SCRIPT_DIR}/packaging/logrotate/xpdevs-networkos" /etc/logrotate.d/xpdevs-networkos
install -m 0644 "${SCRIPT_DIR}/packaging/avahi/xpdevs-networkos.service" /etc/avahi/services/xpdevs-networkos.service
systemctl daemon-reload
systemctl enable xpdevs-networkos.target >/dev/null 2>&1 || true
systemctl enable xpdevs-networkos-net.service xpdevs-networkos-ap.service \
  xpdevs-networkos-dnsmasq.service xpdevs-networkos-web.service >/dev/null 2>&1 || true
systemctl enable --now xpdevs-networkos-health.timer >/dev/null 2>&1 || true
ok "Units installed and enabled"

step "Publishing the dashboard name over mDNS"
if systemctl is-enabled avahi-daemon >/dev/null 2>&1; then
  "${CTL}" mdns-configure >/dev/null 2>&1 && ok "networkos.local is published" || warn "mDNS name could not be published; use the IP address instead."
else
  warn "avahi-daemon is not enabled; the .local name will not resolve."
fi

# --------------------------------------------------------------- administrator
step "Preparing the administrator account"
if [[ -s "${WORK_DIR}/admin.pass" ]]; then
  if "${CTL}" admin-create --username "${ADMIN_USER}" --password-stdin < "${WORK_DIR}/admin.pass" >/dev/null 2>&1; then
    ok "Administrator account '${ADMIN_USER}' created with the supplied password"
  else
    die "The administrator account could not be created. Check ${INSTALL_LOG}."
  fi
else
  ok "Waiting for you to choose the password: http://${LAN_ADDRESS}/setup"
fi

# ------------------------------------------------------------- network bringup
step "Applying the network configuration"
if "${CTL}" net-apply --reason install; then
  ok "Access point, DHCP and firewall applied"
else
  warn "The network configuration did not fully apply. The dashboard is still reachable on ${WAN_IF}."
  warn "Review ${ETC_DIR}/generated and run: sudo networkosctl net-apply --verbose"
fi

step "Starting the administration dashboard"
systemctl restart xpdevs-networkos-web.service
if http_ready "http://127.0.0.1:80/healthz" 40; then
  ok "Dashboard is responding on port 80"
else
  warn "The dashboard did not answer yet. Check: journalctl -u xpdevs-networkos-web -n 50"
fi

# --------------------------------------------------------------- verification
step "Running the connectivity and blocking verification"
if "${CTL}" healthcheck --json > "${WORK_DIR}/health.json" 2>/dev/null; then
  "${VENV}/bin/python" - "${WORK_DIR}/health.json" <<'PY' || true
import json, sys
try:
    report = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(0)
for check in report.get("checks", []):
    mark = {"ok": "✓", "warn": "!", "fail": "✗", "unknown": "?"}.get(check["status"], "?")
    print(f"  {mark} {check['label']}: {check['detail']}")
print(f"  overall: {report.get('overall')}")
PY
else
  warn "The health check could not run; open the dashboard to inspect each subsystem."
fi

if [[ "${PIHOLE_SKIPPED}" -eq 0 ]]; then
  BLOCK_STATE="$("${VENV}/bin/python" - <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, "/opt/xpdevs-networkos")
from networkos import settings
from networkos.services import pihole
status = pihole.status(settings.load(), force=True)
if status.get("available"):
    print("verified" if status.get("verified") else ("enabled" if status.get("enabled") else "disabled"))
else:
    print("unavailable")
PY
)"
  case "${BLOCK_STATE}" in
    verified) ok "Pi-hole blocking verified with a live DNS query" ;;
    enabled) warn "Pi-hole reports blocking enabled but the DNS verification did not confirm it yet." ;;
    disabled) warn "Pi-hole is installed but blocking is disabled." ;;
    *) warn "Pi-hole is not answering yet; the dashboard will keep reporting its real state." ;;
  esac
fi

# ------------------------------------------------------------------- summary
DASH_IP="${LAN_ADDRESS}"
printf '\n%s────────────────────────────────────────────────────────────%s\n' "${C_BLUE}" "${C_RESET}"
printf '%s%s %s installed%s\n' "${C_BOLD}" "${C_GREEN}" "${PRODUCT} ${PRODUCT_VERSION}" "${C_RESET}"
printf '%s────────────────────────────────────────────────────────────%s\n' "${C_BLUE}" "${C_RESET}"
say ""
say "  Dashboard:      http://networkos.local"
say "                  http://${DASH_IP}/   (from the wireless network)"
say "                  http://${WAN_IP}/   (from the wired network)"
if [[ -s "${WORK_DIR}/admin.pass" ]]; then
  say "  Administrator:  ${ADMIN_USER} (password set during installation)"
else
  say "  Administrator:  none yet — the first visit asks you to create one"
fi
say "  Pi-hole API:    internal, bound to 127.0.0.1:8081"
say ""
if [[ "${KEEP_CONFIG}" -eq 0 && -n "${WIFI_PASSWORD}" ]]; then
  # Printed to the console only; never written to a log file.
  printf '  Wireless name:  %s\n' "${SSID}"
  printf '  Wi-Fi password: %s\n' "${WIFI_PASSWORD}"
else
  printf '  Wireless name:  %s (password unchanged)\n' "${SSID}"
fi
say ""
say "  Raspberry Pi OS: ${OS_NAME}"
say "  Hardware:        ${MODEL}"
say "  Install log:     ${INSTALL_LOG}"
say ""
if [[ "${PIHOLE_SKIPPED}" -eq 1 ]]; then
  warn "Pi-hole was not installed, so DNS filtering is inactive."
fi
say "  Next: open http://networkos.local and choose your administrator password."
say "  Reboot persistence is enabled; configuration survives restarts."
printf '\n'

log "=== installation completed ==="
cleanup
trap - EXIT
exit 0