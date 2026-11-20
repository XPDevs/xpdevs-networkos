"""Persistent product configuration (``/etc/xpdevs-networkos/config.toml``).

The configuration file is the single source of truth shared by the dashboard,
the privileged CLI and the rendered service configurations.  It is written
atomically so a crash can never leave a half-written file behind.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any

from . import paths

try:  # Python >= 3.11
    import tomllib as _toml_reader
except ModuleNotFoundError:  # pragma: no cover - older Raspberry Pi images
    try:
        import tomli as _toml_reader  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        _toml_reader = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_LOCK = threading.RLock()
_CACHE: "Config | None" = None
_CACHE_STAMP: tuple[int, int] | None = None


@dataclass
class SystemSection:
    dashboard_hostname: str = "networkos.local"
    timezone: str = ""


@dataclass
class NetworkSection:
    wan_interface: str = "eth0"
    lan_interface: str = "wlan0"
    lan_address: str = "10.42.0.1"
    lan_prefix: int = 24
    dhcp_start: str = "10.42.0.10"
    dhcp_end: str = "10.42.0.200"
    lease_time: str = "12h"
    domain: str = "lan"
    dns_servers: list[str] = field(default_factory=list)
    nat_enabled: bool = True
    firewall_enabled: bool = True
    wan_ssh: bool = False
    enable_mdns: bool = True


@dataclass
class WifiSection:
    enabled: bool = True
    ssid: str = "XPDevs NetworkOS"
    #: PBKDF2(WPA2) pre-shared key in hex. The passphrase itself is never stored.
    psk: str = ""
    channel: int = 6
    hw_mode: str = "g"
    country: str = ""
    hidden: bool = False
    max_clients: int = 32
    isolate_clients: bool = False
    ieee80211n: bool = True


@dataclass
class PiholeSection:
    api_base: str = "http://127.0.0.1:8081"
    manage: bool = True
    blocking_desired: bool = True
    retention_days: int = 7


@dataclass
class SecuritySection:
    bind_host: str = "0.0.0.0"
    bind_port: int = 80
    session_idle_minutes: int = 120
    session_absolute_hours: int = 24
    login_max_attempts: int = 5
    login_window_minutes: int = 15
    allow_public_access: bool = False
    cookie_secure: bool = False


@dataclass
class UiSection:
    theme_default: str = "light"
    product_name: str = "XPDevs NetworkOS"
    support_url: str = "https://xpdevs.github.io/"


@dataclass
class LoggingSection:
    level: str = "INFO"
    max_bytes: int = 524288
    backups: int = 2


@dataclass
class ProductSection:
    version: str = ""
    installed_at: str = ""
    install_channel: str = "release"


@dataclass
class Config:
    system: SystemSection = field(default_factory=SystemSection)
    network: NetworkSection = field(default_factory=NetworkSection)
    wifi: WifiSection = field(default_factory=WifiSection)
    pihole: PiholeSection = field(default_factory=PiholeSection)
    security: SecuritySection = field(default_factory=SecuritySection)
    ui: UiSection = field(default_factory=UiSection)
    logging: LoggingSection = field(default_factory=LoggingSection)
    product: ProductSection = field(default_factory=ProductSection)

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def dns_servers(self) -> list[str]:
        return list(self.network.dns_servers) or [self.network.lan_address]

    def lan_network(self) -> str:
        return f"{self.network.lan_address}/{self.network.lan_prefix}"


_SECTION_TYPES = {
    "system": SystemSection,
    "network": NetworkSection,
    "wifi": WifiSection,
    "pihole": PiholeSection,
    "security": SecuritySection,
    "ui": UiSection,
    "logging": LoggingSection,
    "product": ProductSection,
}

_TRUE = {"1", "true", "yes", "on", "enabled"}
_FALSE = {"0", "false", "no", "off", "disabled", ""}


def _coerce(value: Any, default: Any, name: str) -> Any:
    """Coerce an incoming value to the type of its default, raising on nonsense."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"setting {name} expects a boolean")
    if isinstance(default, int):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"setting {name} expects an integer")
    if isinstance(default, list):
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        text = str(value).strip()
        return [part.strip() for part in text.split(",") if part.strip()] if text else []
    return "" if value is None else str(value)


def _field_default(f: dataclasses.Field) -> Any:
    """The type template for a field, including fields built by a factory."""
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()
    return None


def _apply_section(obj: Any, data: dict[str, Any]) -> None:
    for f in dataclasses.fields(obj):
        if f.name in data:
            try:
                setattr(obj, f.name, _coerce(data[f.name], _field_default(f), f.name))
            except ValueError as exc:
                log.warning("ignoring invalid configuration value: %s", exc)


def from_dict(data: dict[str, Any]) -> Config:
    config = Config()
    for section_name, section_type in _SECTION_TYPES.items():
        raw = data.get(section_name)
        if isinstance(raw, dict):
            _apply_section(getattr(config, section_name), raw)
    # Unknown headers are reported once so a typo is not silently ignored.
    for key in data:
        if key not in _SECTION_TYPES:
            log.warning("unknown configuration section ignored: %s", key)
    return config


def load(use_cache: bool = True, path=None) -> Config:
    """Load the configuration, falling back to safe defaults when absent."""
    global _CACHE, _CACHE_STAMP
    target = path or paths.CONFIG_FILE
    try:
        stat = os.stat(target)
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None

    with _LOCK:
        if use_cache and _CACHE is not None and stamp is not None and stamp == _CACHE_STAMP:
            return copy.deepcopy(_CACHE)

        if stamp is None:
            config = Config()
        else:
            try:
                with open(target, "rb") as handle:
                    raw = _toml_reader.load(handle) if _toml_reader else {}
                config = from_dict(raw)
            except Exception as exc:  # malformed file: keep serving defaults
                log.error("could not parse %s: %s", target, exc)
                config = Config()
        _CACHE = copy.deepcopy(config)
        _CACHE_STAMP = stamp
        return config


def to_toml(config: Config) -> str:
    """Serialise the configuration to TOML (lists of strings inline)."""
    lines: list[str] = []
    for section_name in _SECTION_TYPES:
        section = getattr(config, section_name)
        lines.append(f"[{section_name}]")
        for f in dataclasses.fields(section):
            value = getattr(section, f.name)
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            elif isinstance(value, list):
                rendered = "[" + ", ".join(_quote(str(item)) for item in value) + "]"
            else:
                rendered = _quote(str(value))
            lines.append(f"{f.name} = {rendered}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save(config: Config, path=None) -> None:
    """Atomically persist the configuration with strict permissions."""
    global _CACHE, _CACHE_STAMP
    target = path or paths.CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = to_toml(config)
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o640)
        try:  # hand group ownership to the service group when running as root
            import grp

            os.chown(tmp_name, 0, grp.getgrnam("xpdevs-networkos").gr_gid)
        except (KeyError, PermissionError, OSError, ImportError):
            pass
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    with _LOCK:
        _CACHE = copy.deepcopy(config)
        try:
            stat = os.stat(target)
            _CACHE_STAMP = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            _CACHE_STAMP = None


def invalidate_cache() -> None:
    global _CACHE, _CACHE_STAMP
    with _LOCK:
        _CACHE = None
        _CACHE_STAMP = None
