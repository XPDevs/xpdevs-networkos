"""Small, dependency-free helpers shared by the backend and the CLI.

All validation that protects the system from user supplied input lives here so
that the web layer and the privileged CLI agree on exactly one definition of a
valid value.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import time
from datetime import datetime, timezone
from typing import Iterable

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*$")
UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9@._-]{1,63}$")
SSID_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")

#: Channels usable by the Raspberry Pi 3 Model B radios.
CHANNELS_2GHZ = tuple(range(1, 14))
CHANNELS_5GHZ = tuple(range(36, 166, 4))

DEFAULT_MAX_MESSAGE = 240


class ValidationError(ValueError):
    """Raised when user input fails validation."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


# --------------------------------------------------------------------------- #
# Identity / hashing helpers
# --------------------------------------------------------------------------- #
def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def wpa_psk(ssid: str, passphrase: str) -> str:
    """Derive the 256-bit WPA2 pre-shared key exactly as wpa_supplicant does.

    Storing only the derived key means the plain-text Wi-Fi passphrase never
    has to be written to disk at all.
    """
    return hashlib.pbkdf2_hmac("sha1", passphrase.encode("utf-8"), ssid.encode("utf-8"), 4096, 32).hex()


# --------------------------------------------------------------------------- #
# IP address helpers
# --------------------------------------------------------------------------- #
def parse_ipv4(value: object) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(str(value).strip())
    except (ipaddress.AddressValueError, ValueError):
        raise ValidationError("ip", "Enter a valid IPv4 address.")


def valid_ipv4(value: object) -> str | None:
    try:
        return str(parse_ipv4(value))
    except ValidationError:
        return None


def valid_prefix(value: object, minimum: int = 8, maximum: int = 30) -> int | None:
    try:
        prefix = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return prefix if minimum <= prefix <= maximum else None


def valid_cidr(value: object) -> str | None:
    try:
        return str(ipaddress.IPv4Network(str(value).strip(), strict=True))
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
        return None


def network_of(address: str, prefix: int) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network(f"{address}/{prefix}", strict=False)


def ip_in_network(address: str, network: str, prefix: int) -> bool:
    try:
        return ipaddress.IPv4Address(address) in network_of(network, prefix)
    except (ipaddress.AddressValueError, ValueError):
        return False


#: Ranges from which the administration interface may be reached.
LOCAL_NETWORKS = tuple(
    ipaddress.IPv4Network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16")
)


def is_private_address(address: str) -> bool:
    """True for loopback, RFC1918 and link-local addresses (the admin LAN).

    The definition is written out explicitly rather than using
    ``ipaddress.is_private`` so that documentation and test ranges are never
    treated as trusted.
    """
    try:
        ip = ipaddress.IPv4Address(address)
    except (ipaddress.AddressValueError, ValueError):
        return False
    return any(ip in network for network in LOCAL_NETWORKS)


def ip_sort_key(address: str) -> tuple:
    try:
        return (0, int(ipaddress.IPv4Address(address)))
    except (ipaddress.AddressValueError, ValueError):
        return (1, 0)


def check_lan_plan(gateway: str, prefix: int, dhcp_start: str, dhcp_end: str) -> None:
    """Validate the LAN addressing plan as a whole.

    Raises ValidationError naming the offending field when the plan would
    produce an unroutable network, an overlapping range, or no usable leases.
    """
    gw = parse_ipv4(gateway)
    net = network_of(str(gw), prefix)
    start = parse_ipv4(dhcp_start)
    end = parse_ipv4(dhcp_end)

    if gw not in net:
        raise ValidationError("lan_address", "The gateway address is not inside the LAN subnet.")
    if start not in net or end not in net:
        raise ValidationError("dhcp_range", "The DHCP range must be inside the LAN subnet.")
    if int(start) > int(end):
        raise ValidationError("dhcp_range", "The DHCP range start must not be after the end.")
    if int(gw) == int(start) or int(gw) == int(end):
        raise ValidationError("dhcp_range", "The DHCP range must not include the gateway address.")
    if prefix <= 30 and int(start) == int(net.network_address):
        raise ValidationError("dhcp_range", "The DHCP range must not include the network address.")
    if prefix <= 30 and int(end) == int(net.broadcast_address):
        raise ValidationError("dhcp_range", "The DHCP range must not include the broadcast address.")
    if int(end) - int(start) < 1:
        raise ValidationError("dhcp_range", "The DHCP range must contain at least two addresses.")


# --------------------------------------------------------------------------- #
# Wireless helpers
# --------------------------------------------------------------------------- #
def normalize_mac(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", ":")
    if ":" not in text and len(text) == 12:
        text = ":".join(text[i : i + 2] for i in range(0, 12, 2))
    return text if MAC_RE.match(text) else None


def valid_mac(value: object) -> bool:
    return normalize_mac(value) is not None


_QUOTE_CHARS = "\"'\u201c\u201d\u2018\u2019"


def strip_ssid_quotes(value: str) -> str:
    """Remove decorative quotes from an SSID.

    Users copy the quoted name the dashboard shows, so names arrive wrapped in
    straight or typographic quotes. Those characters become part of the beacon
    and of the PSK salt, which is why a client then reports a wrong password:
    it derives the key from the name without the quotes.
    """
    text = value.strip()
    changed = True
    while changed and len(text) >= 2:
        changed = False
        if text[0] in _QUOTE_CHARS and text[-1] in _QUOTE_CHARS:
            text = text[1:-1].strip()
            changed = True
    return text


def valid_ssid(value: object) -> str:
    text = strip_ssid_quotes(str(value or ""))
    if not text:
        raise ValidationError("ssid", "The Wi-Fi network name cannot be empty.")
    if SSID_FORBIDDEN.search(text):
        raise ValidationError("ssid", "The Wi-Fi network name contains control characters.")
    if '"' in text or "\\" in text:
        raise ValidationError("ssid", "The Wi-Fi network name cannot contain quotes or backslashes.")
    if len(text.encode("utf-8")) > 32:
        raise ValidationError("ssid", "The Wi-Fi network name must be 32 bytes or fewer.")
    return text


def valid_passphrase(value: object) -> str:
    text = str(value or "")
    if len(text) < 8 or len(text) > 63:
        raise ValidationError("password", "The Wi-Fi password must be between 8 and 63 characters.")
    if not all(32 <= ord(ch) <= 126 for ch in text):
        raise ValidationError("password", "Use printable ASCII characters for the Wi-Fi password.")
    return text


def valid_channel(value: object, hw_mode: str = "g") -> int:
    try:
        channel = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError("channel", "Choose a valid wireless channel.")
    allowed = CHANNELS_5GHZ if hw_mode == "a" else CHANNELS_2GHZ
    if channel not in allowed:
        raise ValidationError(
            "channel",
            f"Channel {channel} is not available for {hw_mode.upper()}-band operation.",
        )
    return channel


def valid_hostname(value: object, field: str = "hostname") -> str:
    text = str(value or "").strip().lower()
    if not HOSTNAME_RE.match(text) or len(text) > 63:
        raise ValidationError(field, "Use 1-63 letters, digits or hyphens (no leading or trailing hyphen).")
    return text


def valid_domain_name(value: object, field: str = "domain") -> str:
    text = str(value or "").strip().lower()
    if not text or not DOMAIN_RE.match(text) or len(text) > 253:
        raise ValidationError(field, "Enter a valid domain name, for example example.com.")
    return text


def valid_label(value: object, field: str = "name") -> str:
    text = str(value or "").strip().lower()
    if not UNIT_RE.match(text):
        raise ValidationError(field, "Use letters, digits, dots, hyphens or underscores.")
    return text


# --------------------------------------------------------------------------- #
# Durations, sizes, timestamps
# --------------------------------------------------------------------------- #
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
LEASE_RE = re.compile(r"^(infinite|(\d+)([smhdw]))$")


def parse_duration(value: object, field: str = "duration") -> int:
    """Parse '30m', '12h', '2d' or plain seconds into seconds."""
    if isinstance(value, (int, float)):
        seconds = int(value)
        if seconds <= 0:
            raise ValidationError(field, "Enter a duration greater than zero.")
        return seconds
    text = str(value or "").strip().lower()
    match = LEASE_RE.match(text)
    if not match:
        raise ValidationError(field, "Use a duration such as 30m, 12h or 2d.")
    if text == "infinite":
        return 0
    amount, unit = int(match.group(2)), match.group(3)
    return amount * _DURATION_UNITS[unit]


def valid_lease_time(value: object) -> str:
    """Validate a dnsmasq lease time such as '12h', '30m' or 'infinite'."""
    text = str(value or "").strip().lower()
    if text == "infinite":
        return text
    if not re.match(r"^\d{1,4}[smhdw]$", text):
        raise ValidationError("lease_time", "Use a lease time such as 30m, 12h or 1d.")
    seconds = parse_duration(text, "lease_time")
    if seconds < 120 or seconds > 604800 * 4:
        raise ValidationError("lease_time", "Lease time must be between 2 minutes and 4 weeks.")
    return text


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if sec < 30 else f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def human_bytes(value: float | int | None, precision: int = 1) -> str:
    if value is None:
        return "—"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(amount) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(amount)} B"
            return f"{amount:.{precision}f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def epoch_now() -> int:
    return int(time.time())


def iso(ts: float | int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def percent(part: float, whole: float, digits: int = 1) -> float:
    if not whole:
        return 0.0
    return round(clamp(part / whole * 100.0, 0.0, 100.0), digits)


def first_present(values: Iterable, default=None):
    for value in values:
        if value not in (None, ""):
            return value
    return default
