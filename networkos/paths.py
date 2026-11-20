"""Filesystem locations used by XPDevs NetworkOS.

Every path can be relocated with an environment variable, which keeps the
backend testable on a development machine without touching the real system
configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_SLUG = "xpdevs-networkos"


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


#: Primary configuration directory (root owned, group readable).
ETC_DIR = _env_path("NETWORKOS_ETC", f"/etc/{APP_SLUG}")
CONFIG_FILE = _env_path("NETWORKOS_CONFIG", str(ETC_DIR / "config.toml"))
GENERATED_DIR = ETC_DIR / "generated"
BACKUP_DIR = ETC_DIR / "backups"
SECRET_DIR = ETC_DIR / "secrets"
PIHOLE_PASSWORD_FILE = _env_path(
    "NETWORKOS_PIHOLE_PASSWORD_FILE", str(SECRET_DIR / "pihole-api.key")
)
SESSION_KEY_FILE = SECRET_DIR / "session.key"

#: Mutable state (SQLite database, health snapshot).
STATE_DIR = _env_path("NETWORKOS_STATE", f"/var/lib/{APP_SLUG}")
DB_FILE = _env_path("NETWORKOS_DB", str(STATE_DIR / "networkos.db"))
HEALTH_FILE = STATE_DIR / "health.json"
UPDATE_STATE_FILE = STATE_DIR / "update.state"

#: Logs.
LOG_DIR = _env_path("NETWORKOS_LOG", f"/var/log/{APP_SLUG}")
APP_LOG = LOG_DIR / "networkos.log"
AUDIT_LOG = LOG_DIR / "audit.log"

#: Code locations.
INSTALL_ROOT = _env_path("NETWORKOS_ROOT", "/opt/xpdevs-networkos")
VENV_DIR = INSTALL_ROOT / "venv"
CTL_PATH = _env_path("NETWORKOS_CTL", "/usr/local/sbin/networkosctl")

#: System files we render into (written by the privileged CLI only).
HOSTAPD_CONF = GENERATED_DIR / "hostapd.conf"
DNSMASQ_CONF = GENERATED_DIR / "dnsmasq.conf"
NFT_RULESET = GENERATED_DIR / "networkos.nft"
SYSCTL_CONF = GENERATED_DIR / "networkos.conf"
HOSTS_FILE = GENERATED_DIR / "hosts"
DHCP_LEASES = STATE_DIR / "dnsmasq.leases"
DHCP_HOSTS = GENERATED_DIR / "dhcp-hosts"
NFT_TABLE = "networkos"

#: systemd unit names managed by the product.
UNIT_AP = "xpdevs-networkos-ap.service"
UNIT_DNSMASQ = "xpdevs-networkos-dnsmasq.service"
UNIT_NET = "xpdevs-networkos-net.service"
UNIT_WEB = "xpdevs-networkos-web.service"
UNIT_HEALTH = "xpdevs-networkos-health.timer"
UNIT_PIHOLE = "pihole-FTL.service"
UNIT_LIGHTTPD = "lighttpd.service"
UNIT_MDNS = "xpdevs-networkos-mdns.service"


def ensure_dirs() -> None:
    """Create the directories the service user must be able to write to."""
    for path in (STATE_DIR, LOG_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
