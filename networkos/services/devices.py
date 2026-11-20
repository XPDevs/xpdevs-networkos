"""Connected device inventory.

Combines four real sources so the list reflects what is actually attached:

* ``iw station dump``      - wireless clients, signal and per-client byte counts
* dnsmasq leases           - DHCP hostname, IP and address lifetime
* kernel neighbour table   - devices that did not use DHCP
* the local database       - first seen, last seen and block intent
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from .. import db, paths, util
from . import network, systemstate

log = logging.getLogger(__name__)


def parse_leases(path=None) -> list[dict[str, Any]]:
    """Parse the dnsmasq lease file (expiry mac ip hostname client-id)."""
    leases: list[dict[str, Any]] = []
    try:
        with open(path or paths.DHCP_LEASES, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return leases
    now = util.epoch_now()
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        mac = util.normalize_mac(parts[1])
        if not mac:
            continue
        expires = util.to_int(parts[0])
        hostname = parts[3].strip()
        if hostname in ("*", "-"):
            hostname = ""
        leases.append(
            {
                "mac": mac,
                "ip": parts[2],
                "hostname": hostname,
                "expires_at": expires,
                "remaining_seconds": max(0, expires - now) if expires > 0 else 0,
                "infinite": expires == 0,
                "client_id": parts[4] if len(parts) > 4 else "",
            }
        )
    return leases


def _sync_database(seen: dict[str, dict[str, Any]]) -> dict[str, sqlite3.Row]:
    """Persist first/last seen values with minimal write amplification."""
    now = util.epoch_now()
    existing = {row["mac"]: row for row in db.query("SELECT * FROM devices")}
    for mac, device in seen.items():
        row = existing.get(mac)
        ip = device.get("ip") or ""
        hostname = device.get("hostname") or ""
        if row is None:
            try:
                db.execute(
                    "INSERT OR IGNORE INTO devices(mac, hostname, ip, first_seen, last_seen) VALUES(?,?,?,?,?)",
                    (mac, hostname, ip, now, now),
                )
            except sqlite3.Error as exc:
                log.debug("device insert failed: %s", exc)
        elif now - util.to_int(row["last_seen"]) > 300 or (ip and ip != row["ip"]) or (
            hostname and hostname != row["hostname"]
        ):
            try:
                db.execute(
                    "UPDATE devices SET hostname = COALESCE(NULLIF(?, ''), hostname), ip = COALESCE(NULLIF(?, ''), ip), "
                    "last_seen = ? WHERE mac = ?",
                    (hostname, ip, now, mac),
                )
            except sqlite3.Error as exc:
                log.debug("device update failed: %s", exc)
    if seen:
        try:
            return {row["mac"]: row for row in db.query("SELECT * FROM devices")}
        except sqlite3.Error:
            return existing
    return existing


def collect(cfg, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the merged device list plus summary counters."""
    snap = snapshot if snapshot is not None else systemstate.snapshot(cfg)
    stations = systemstate.stations(snap)
    firewall = systemstate.firewall(snap, cfg)
    blocked_macs = set(firewall["blocked_macs"])
    leases = parse_leases()
    neighbours = network.neighbours(cfg.network.lan_interface)
    station_map = {station["mac"]: station for station in stations.get("stations", [])}
    lease_map = {lease["mac"]: lease for lease in leases}
    neigh_map = {entry["mac"]: entry for entry in neighbours}

    merged: dict[str, dict[str, Any]] = {}

    def ensure(mac: str) -> dict[str, Any]:
        if mac not in merged:
            merged[mac] = {
                "mac": mac,
                "ip": None,
                "hostname": "",
                "online": False,
                "connection": "unknown",
                "rx_bytes": 0,
                "tx_bytes": 0,
                "signal_dbm": None,
                "connected_seconds": None,
                "lease_remaining": None,
                "first_seen": None,
                "last_seen": None,
                "blocked": mac in blocked_macs,
            }
        return merged[mac]

    for mac, station in station_map.items():
        device = ensure(mac)
        device["connection"] = "wireless"
        device["online"] = bool(station.get("online"))
        device["signal_dbm"] = station.get("signal_dbm")
        device["rx_bytes"] = station.get("rx_bytes", 0)
        device["tx_bytes"] = station.get("tx_bytes", 0)
        device["connected_seconds"] = station.get("connected_seconds")
        device["bitrate"] = station.get("tx_bitrate")

    for mac, lease in lease_map.items():
        device = ensure(mac)
        device["ip"] = device["ip"] or lease["ip"]
        device["hostname"] = device["hostname"] or lease["hostname"]
        device["lease_remaining"] = lease["remaining_seconds"]
        if device["connection"] == "unknown":
            device["connection"] = "wired" if cfg.network.lan_interface not in (lease.get("interface") or "") else "wireless"

    for mac, entry in neigh_map.items():
        device = ensure(mac)
        device["ip"] = device["ip"] or entry["ip"]
        if entry["state"] in ("REACHABLE", "DELAY", "PROBE"):
            device["online"] = True
        if device["connection"] == "unknown":
            device["connection"] = "wired"

    rows = _sync_database(merged)
    now = util.epoch_now()
    for mac, device in merged.items():
        row = rows.get(mac)
        if row:
            device["first_seen"] = util.iso(row["first_seen"])
            device["last_seen"] = util.iso(row["last_seen"])
            device["blocked"] = bool(row["blocked"]) or mac in blocked_macs
            device["first_seen_epoch"] = util.to_int(row["first_seen"])
            if not device["ip"]:
                device["ip"] = row["ip"]
            if not device["hostname"]:
                device["hostname"] = row["hostname"]
    for device in merged.values():
        device.setdefault("first_seen_epoch", now)
        if not device["online"] and device.get("lease_remaining"):
            device["online"] = device["lease_remaining"] > 0
        device["last_seen"] = device["last_seen"] or util.iso(now)

    items = sorted(
        merged.values(),
        key=lambda item: (not item["online"], item["hostname"] or "~", util.ip_sort_key(item["ip"] or "")),
    )
    for item in items:
        item["display_name"] = item["hostname"] or (f"Device {item['mac'][-5:].replace(':', '-')}" if item["mac"] else "Unknown device")
        item["signal_quality"] = signal_quality(item.get("signal_dbm"))

    summary = {
        "total": len(items),
        "online": sum(1 for item in items if item["online"]),
        "blocked": sum(1 for item in items if item["blocked"]),
        "wireless": sum(1 for item in items if item["connection"] == "wireless"),
        "wired": sum(1 for item in items if item["connection"] == "wired"),
        "stations_available": stations.get("available", False),
        "stations_reason": stations.get("reason"),
        "snapshot_stale": bool((snap.get("_meta") or {}).get("stale")),
    }
    return {"devices": items, "summary": summary, "leases": len(leases)}


def signal_quality(dbm: int | None) -> dict[str, Any]:
    if dbm is None:
        return {"label": "Wired", "level": None, "percent": None}
    percent = util.clamp(round(2 * (dbm + 100)), 0, 100)
    if dbm >= -50:
        label = "Excellent"
    elif dbm >= -60:
        label = "Good"
    elif dbm >= -70:
        label = "Fair"
    elif dbm >= -80:
        label = "Weak"
    else:
        label = "Poor"
    return {"label": label, "level": dbm, "percent": int(percent)}


def find(mac: str, cfg, snapshot: dict[str, Any] | None = None) -> dict[str, Any] | None:
    device_mac = util.normalize_mac(mac)
    if not device_mac:
        return None
    catalogue = collect(cfg, snapshot)
    return next((device for device in catalogue["devices"] if device["mac"] == device_mac), None)


def set_blocked(mac: str, blocked: bool, *, username: str = "") -> dict[str, Any]:
    """Block or unblock a device through the privileged CLI."""
    from .. import shell
    from ..errors import ApiError, CommandError

    device_mac = util.normalize_mac(mac)
    if not device_mac:
        raise ApiError("That device identifier is not valid.", code="invalid_mac")
    result = shell.ctl("device-block" if blocked else "device-unblock", device_mac, timeout=30)
    if not result.ok:
        raise CommandError(
            "The firewall rejected the change.",
            returncode=result.returncode,
            stderr=(result.stderr or result.stdout).strip()[:400],
            command=f"networkosctl device-{'block' if blocked else 'unblock'} {device_mac}",
        )
    db.execute(
        "UPDATE devices SET blocked = ?, blocked_at = ? WHERE mac = ?",
        (1 if blocked else 0, util.epoch_now() if blocked else None, device_mac),
    )
    return {"mac": device_mac, "blocked": blocked}


def uptime_string(seconds: int | None) -> str:
    return util.format_duration(seconds) if seconds is not None else "—"


def last_seen_text(iso_value: str | None) -> str:
    if not iso_value:
        return "—"
    try:
        from datetime import datetime, timezone

        when = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    delta = (datetime.now(timezone.utc) - when).total_seconds()
    if delta < 90:
        return "just now"
    return f"{util.format_duration(delta)} ago"


def clock() -> float:
    return time.time()
