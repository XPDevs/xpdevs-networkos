"""Access point state and wireless client inspection via nl80211 (iw)."""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import shell, util
from . import systemd

log = logging.getLogger(__name__)

_STATION_RE = re.compile(r"^Station\s+([0-9a-f:]{17})\s+\(on\s+(\S+)\)", re.IGNORECASE)


def parse_station_dump(text: str) -> list[dict[str, Any]]:
    """Parse ``iw dev <if> station dump`` output into structured clients."""
    stations: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = _STATION_RE.match(line)
        if match:
            if current:
                stations.append(current)
            current = {"mac": match.group(1).lower(), "interface": match.group(2), "raw": {}}
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        current["raw"][key] = value
        if key == "signal":
            current["signal_dbm"] = util.to_int(value.split()[0], 0) if value else None
        elif key == "signal avg":
            current["signal_avg_dbm"] = util.to_int(value.split()[0], 0) if value else None
        elif key == "rx bytes":
            current["rx_bytes"] = util.to_int(value)
        elif key == "tx bytes":
            current["tx_bytes"] = util.to_int(value)
        elif key == "rx packets":
            current["rx_packets"] = util.to_int(value)
        elif key == "tx packets":
            current["tx_packets"] = util.to_int(value)
        elif key == "connected time":
            current["connected_seconds"] = util.to_int(value.split()[0])
        elif key == "inactive time":
            current["inactive_ms"] = util.to_int(value.split()[0])
        elif key == "rx bitrate":
            current["rx_bitrate"] = value.split()[0] if value else None
        elif key == "tx bitrate":
            current["tx_bitrate"] = value.split()[0] if value else None
        elif key == "authorized":
            current["authorized"] = value.lower() == "yes"
    if current:
        stations.append(current)
    for station in stations:
        station.pop("raw", None)
        station.setdefault("rx_bytes", 0)
        station.setdefault("tx_bytes", 0)
        station["online"] = station.get("inactive_ms", 0) < 300_000
    return stations


def parse_iw_dev(text: str) -> list[dict[str, Any]]:
    interfaces: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("phy#"):
            current = None
            continue
        if line.startswith("Interface "):
            current = {"interface": line.split(" ", 1)[1].strip()}
            interfaces.append(current)
            continue
        if current is None:
            continue
        if line.startswith("type "):
            current["type"] = line.split(" ", 1)[1].strip()
        elif line.startswith("channel "):
            parts = line.split()
            current["channel"] = util.to_int(parts[1]) if len(parts) > 1 else None
            freq = re.search(r"\((\d+)\s*MHz\)", line)
            if freq:
                current["frequency_mhz"] = int(freq.group(1))
            width = re.search(r"width:\s*([0-9]+\s*MHz)", line)
            if width:
                current["width"] = width.group(1)
        elif line.startswith("addr "):
            current["mac"] = line.split(" ", 1)[1].strip().lower()
        elif line.startswith("ssid "):
            current["ssid"] = line.split(" ", 1)[1].strip()
        elif line.startswith("txpower "):
            current["txpower"] = line.split(" ", 1)[1].strip()
    return interfaces


def live_interfaces() -> list[dict[str, Any]]:
    """Can be called unprivileged on most kernels; returns [] if unsupported."""
    if not shell.available("iw"):
        return []
    result = shell.run(["iw", "dev"], timeout=5)
    if not result.ok:
        return []
    return parse_iw_dev(result.stdout)


def supports_ap(interface_name: str = "wlan0") -> dict[str, Any]:
    """Whether the radio advertises AP mode (``iw list``)."""
    if not shell.available("iw"):
        return {"supported": False, "reason": "iw is not installed", "modes": []}
    result = shell.run(["iw", "list"], timeout=8)
    if not result.ok:
        return {
            "supported": False,
            "reason": "The wireless driver did not report its capabilities",
            "modes": [],
        }
    modes: list[str] = []
    in_modes = False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("supported interface modes"):
            in_modes = True
            continue
        if in_modes:
            if stripped.startswith("*"):
                modes.append(stripped.lstrip("* ").strip())
            elif stripped and not stripped.startswith("*"):
                in_modes = False
    supported = any(mode.upper() == "AP" for mode in modes)
    return {
        "supported": supported,
        "reason": None if supported else "This radio cannot run as an access point",
        "modes": modes,
        "interface": interface_name,
    }


def ap_status(cfg, *, stations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Combined view of the configured and the actually running AP."""
    service = systemd.status("ap")
    running = service["running"]
    live = live_interfaces()
    lan = next((item for item in live if item["interface"] == cfg.network.lan_interface), None)
    station_list = stations if stations is not None else []
    online_clients = sum(1 for station in station_list if station.get("online"))
    return {
        "configured": cfg.wifi.enabled,
        "enabled": cfg.wifi.enabled,
        "running": running,
        "service": service,
        "ssid": cfg.wifi.ssid,
        "channel": cfg.wifi.channel,
        "band": "5 GHz" if cfg.wifi.hw_mode == "a" else "2.4 GHz",
        "hw_mode": cfg.wifi.hw_mode,
        "hidden": cfg.wifi.hidden,
        "isolate_clients": cfg.wifi.isolate_clients,
        "security": "WPA2-PSK (AES)" if cfg.wifi.psk else "Not configured",
        "country": cfg.wifi.country or None,
        "interface": cfg.network.lan_interface,
        "live": lan,
        "clients": len(station_list),
        "clients_online": online_clients,
        "broadcasting": bool(running and lan and lan.get("type") == "AP"),
        "max_clients": cfg.wifi.max_clients,
    }
