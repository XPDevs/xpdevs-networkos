"""Interface, routing, DNS and connectivity inspection.

All reads are unprivileged (rtnetlink dumps and /proc), so the web service never
needs elevated rights to describe the network.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Callable

from .. import shell, util

log = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
INTERNET_TTL = 30.0


def _cached(key: str, ttl: float, producer: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and now - entry[0] < ttl:
            return entry[1]
    value = producer()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
    return value


def invalidate_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------- #
# Interfaces and addresses
# --------------------------------------------------------------------------- #
def _ip_json(*args: str) -> Any:
    result = shell.run(["ip", "-j", *args], timeout=6)
    if not result.ok:
        log.debug("ip %s failed: %s", " ".join(args), result.stderr.strip())
        return None
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None


def interfaces() -> list[dict[str, Any]]:
    """Interface table, cached for a few seconds to keep the dashboard cheap."""
    return _cached("interfaces", 5.0, _interfaces_uncached)


def _interfaces_uncached() -> list[dict[str, Any]]:
    data = _ip_json("addr", "show") or []
    result: list[dict[str, Any]] = []
    for entry in data:
        name = entry.get("ifname", "")
        if name == "lo":
            continue
        addr_info = entry.get("addr_info") or []
        ipv4 = [
            {"address": item.get("local"), "prefix": item.get("prefixlen"), "broadcast": item.get("broadcast")}
            for item in addr_info
            if item.get("family") == "inet"
        ]
        ipv6 = [
            {"address": item.get("local"), "prefix": item.get("prefixlen"), "scope": item.get("scope")}
            for item in addr_info
            if item.get("family") == "inet6" and item.get("scope") in ("global", "link")
        ]
        result.append(
            {
                "name": name,
                "mac": (entry.get("address") or "").lower() or None,
                "state": entry.get("operstate", "unknown"),
                "up": entry.get("operstate") in ("UP", "UNKNOWN"),
                "mtu": entry.get("mtu"),
                "ipv4": ipv4,
                "ipv6": ipv6,
                "primary_ipv4": ipv4[0]["address"] if ipv4 else None,
                "wireless": name.startswith(("wl", "wlan")),
            }
        )
    return result


def interface(name: str) -> dict[str, Any] | None:
    return next((item for item in interfaces() if item["name"] == name), None)


def default_route() -> dict[str, Any] | None:
    return _cached("default_route", 5.0, _default_route_uncached)


def _default_route_uncached() -> dict[str, Any] | None:
    data = _ip_json("route", "show", "default") or []
    best = None
    for entry in data:
        if entry.get("dst") != "default":
            continue
        metric = util.to_int(entry.get("metric"), 0)
        if best is None or metric < util.to_int(best.get("metric"), 0):
            best = entry
    if not best:
        return None
    return {"gateway": best.get("gateway"), "dev": best.get("dev"), "metric": best.get("metric")}


def neighbours(dev: str | None = None) -> list[dict[str, Any]]:
    key = f"neigh:{dev or 'all'}"
    return _cached(key, 4.0, lambda: _neighbours_uncached(dev))


def _neighbours_uncached(dev: str | None = None) -> list[dict[str, Any]]:
    args = ["neigh", "show"]
    if dev:
        args += ["dev", dev]
    data = _ip_json(*args) or []
    result = []
    for entry in data:
        mac = util.normalize_mac(entry.get("lladdr"))
        if not mac:
            continue
        result.append(
            {
                "ip": entry.get("dst"),
                "mac": mac,
                "dev": entry.get("dev"),
                "state": entry.get("state", ""),
            }
        )
    return result


# --------------------------------------------------------------------------- #
# Listening sockets (for DNS/NAT verification)
# --------------------------------------------------------------------------- #
def _hex_port(port: int) -> str:
    return f"{int(port):04X}"


def listening(port: int, proto: str = "udp") -> list[str]:
    """Return local addresses listening on a port, from /proc/net/{udp,tcp}."""
    files = ["/proc/net/udp", "/proc/net/udp6"] if proto == "udp" else ["/proc/net/tcp", "/proc/net/tcp6"]
    wanted = _hex_port(port)
    addresses: list[str] = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                continue
            local = fields[1]
            if ":" not in local:
                continue
            host_hex, port_hex = local.rsplit(":", 1)
            if port_hex.upper() != wanted:
                continue
            if host_hex.strip("0") == "":
                addresses.append("0.0.0.0" if len(host_hex) <= 8 else "::")
            else:
                try:
                    raw = bytes.fromhex(host_hex)
                    addresses.append(socket.inet_ntop(socket.AF_INET if len(raw) == 4 else socket.AF_INET6, raw[::-1] if len(raw) == 4 else raw))
                except (ValueError, OSError):
                    addresses.append(host_hex)
    return sorted(set(addresses))


def dns_service() -> dict[str, Any]:
    addresses = listening(53, "udp")
    return {
        "listening": bool(addresses),
        "addresses": addresses,
        "local": any(addr in ("0.0.0.0", "127.0.0.1", "::") for addr in addresses),
    }


def dhcp_service(server_ip: str) -> dict[str, Any]:
    """A DHCP server listens on UDP 67; confirm dnsmasq is bound."""
    addresses = listening(67, "udp")
    return {"listening": bool(addresses), "addresses": addresses, "expected_bind": server_ip}


# --------------------------------------------------------------------------- #
# Connectivity
# --------------------------------------------------------------------------- #
def _with_timeout(fn: Callable[[], Any], timeout: float, default: Any = None) -> Any:
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - reported as a failed probe
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return default
    return box.get("value", default)


def _tcp_probe(host: str, port: int, timeout: float = 2.0) -> float | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    started = time.monotonic()
    try:
        sock.connect((host, port))
        return round((time.monotonic() - started) * 1000, 1)
    except OSError:
        return None
    finally:
        sock.close()


def internet() -> dict[str, Any]:
    """Probe upstream reachability with a real TCP handshake (no ping needed)."""

    def probe() -> dict[str, Any]:
        attempts = (("1.1.1.1", 53), ("8.8.8.8", 53), ("9.9.9.9", 53))
        latencies = []
        for host, port in attempts:
            latency = _tcp_probe(host, port)
            if latency is not None:
                latencies.append(latency)
        online = bool(latencies)
        return {
            "online": online,
            "latency_ms": min(latencies) if latencies else None,
            "checked_at": util.iso_now(),
            "method": "tcp",
        }

    return _cached("internet", INTERNET_TTL, probe)


def dns_resolution() -> dict[str, Any]:
    """Resolve a well-known name through the Pi's configured resolver."""

    def probe() -> dict[str, Any]:
        started = time.monotonic()
        result = _with_timeout(lambda: socket.getaddrinfo("one.one.one.one", 443, socket.AF_INET), 4.0)
        if not result:
            return {"ok": False, "latency_ms": None, "checked_at": util.iso_now()}
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "checked_at": util.iso_now(),
        }

    return _cached("dns", INTERNET_TTL, probe)


def gateway_reachable(gateway: str | None, dev: str | None) -> dict[str, Any]:
    if not gateway or not dev:
        return {"reachable": False, "detail": "No default gateway"}
    entries = neighbours(dev)
    match = next((item for item in entries if item["ip"] == gateway), None)
    if match and match["state"] in ("REACHABLE", "STALE", "DELAY", "PROBE"):
        return {"reachable": True, "mac": match["mac"], "detail": f"Gateway {gateway} resolved on {dev}"}
    latency = _tcp_probe(gateway, 53, timeout=1.0)
    if latency is not None:
        return {"reachable": True, "mac": match["mac"] if match else None, "detail": f"Gateway answered on port 53 in {latency} ms"}
    return {"reachable": False, "mac": match["mac"] if match else None, "detail": "Gateway not answering"}


def wan_summary(cfg) -> dict[str, Any]:
    wan = interface(cfg.network.wan_interface)
    route = default_route()
    inet = internet()
    return {
        "interface": cfg.network.wan_interface,
        "present": wan is not None,
        "link_up": bool(wan and wan["up"]),
        "state": wan["state"] if wan else "missing",
        "ipv4": wan["primary_ipv4"] if wan else None,
        "mac": wan["mac"] if wan else None,
        "gateway": route["gateway"] if route and route["dev"] == cfg.network.wan_interface else (route or {}).get("gateway"),
        "default_route": route,
        "online": inet["online"],
        "latency_ms": inet["latency_ms"],
        "checked_at": inet["checked_at"],
    }


def lan_summary(cfg) -> dict[str, Any]:
    lan = interface(cfg.network.lan_interface)
    expected = cfg.network.lan_address
    configured = bool(lan and any(item["address"] == expected for item in lan["ipv4"]))
    return {
        "interface": cfg.network.lan_interface,
        "present": lan is not None,
        "link_up": bool(lan and lan["up"]),
        "state": lan["state"] if lan else "missing",
        "ipv4": lan["primary_ipv4"] if lan else None,
        "configured_address": expected,
        "address_applied": configured,
    }


def connectivity_report(cfg) -> dict[str, Any]:
    """Full connectivity test used by the Network page button and health check."""
    wan = wan_summary(cfg)
    lan = lan_summary(cfg)
    gateway = gateway_reachable(wan["gateway"], cfg.network.wan_interface)
    dns = dns_resolution()
    checks = [
        {
            "id": "wan_link",
            "label": f"Ethernet link ({cfg.network.wan_interface})",
            "ok": wan["link_up"],
            "detail": f"{wan['state']}, {wan['ipv4'] or 'no address'}",
        },
        {
            "id": "gateway",
            "label": "Default gateway",
            "ok": gateway["reachable"],
            "detail": gateway["detail"],
        },
        {
            "id": "internet",
            "label": "Internet reachability",
            "ok": wan["online"],
            "detail": f"TCP probe succeeded ({wan['latency_ms']} ms)" if wan["online"] else "No upstream answer",
        },
        {
            "id": "dns",
            "label": "DNS resolution",
            "ok": dns["ok"],
            "detail": f"Resolved in {dns['latency_ms']} ms" if dns["ok"] else "Resolver did not answer",
        },
        {
            "id": "lan_address",
            "label": "Access point address",
            "ok": lan["address_applied"],
            "detail": f"{cfg.network.lan_interface} carries {lan['ipv4'] or 'no address'}",
        },
    ]
    return {"checks": checks, "ok": all(check["ok"] for check in checks), "checked_at": util.iso_now()}
