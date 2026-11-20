"""Health checks and safe self-repair.

The health timer runs every minute.  It records a snapshot the dashboard can
read without privileges, and it repairs a small, well-understood set of
failures (access point stopped, firewall table missing, forwarding disabled)
with a throttle so it can never flap the radio.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from . import collect, paths, shell, util
from .services import events, network, resources, systemd, wireless
from .services import pihole as pihole_service

log = logging.getLogger(__name__)

REMEDIATION_THROTTLE = 300
_STATUS_ORDER = {"fail": 0, "warn": 1, "unknown": 2, "ok": 3}


def _check(identifier: str, label: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"id": identifier, "label": label, "status": status, "detail": detail, **extra}


def _read_forwarding() -> bool | None:
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "r", encoding="utf-8") as handle:
            return handle.read().strip() == "1"
    except OSError:
        return None


def run(cfg, *, remediate: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    services = systemd.status_map()
    wan = network.wan_summary(cfg)
    lan = network.lan_summary(cfg)
    internet = network.internet()
    dns = network.dns_service()
    dhcp = network.dhcp_service(cfg.network.lan_address)
    ap_snapshot = collect.station_state(cfg.network.lan_interface)
    nft = collect.nft_state()
    forwarding = _read_forwarding()
    storage = resources.storage("/")
    temp = resources.temperature()

    checks.append(
        _check(
            "wan_link",
            f"Ethernet link ({cfg.network.wan_interface})",
            "ok" if wan["link_up"] else "fail",
            f"{wan['state']}, {wan['ipv4'] or 'no IPv4 address'}",
        )
    )
    checks.append(
        _check(
            "wan_address",
            "WAN address",
            "ok" if wan["ipv4"] else "warn",
            wan["ipv4"] or "No IPv4 address obtained (DHCP may still be running)",
        )
    )
    checks.append(
        _check(
            "default_route",
            "Default route",
            "ok" if wan["default_route"] else "warn",
            f"via {wan['gateway']} on {wan['default_route'].get('dev') if wan['default_route'] else '—'}",
        )
    )
    checks.append(
        _check(
            "internet",
            "Internet reachability",
            "ok" if internet["online"] else ("fail" if wan["link_up"] else "unknown"),
            f"TCP probe {internet['latency_ms']} ms" if internet["online"] else "No upstream answer",
        )
    )
    ap_status = services.get("ap", {})
    checks.append(
        _check(
            "ap_service",
            "Access point service",
            "ok" if ap_status.get("running") else ("fail" if cfg.wifi.enabled else "warn"),
            f"{ap_status.get('active', 'unknown')}/{ap_status.get('state', 'unknown')}",
        )
    )
    stations = ap_snapshot.get("stations", []) if ap_snapshot.get("available") else []
    checks.append(
        _check(
            "ap_clients",
            "Wireless clients",
            "ok",
            f"{len(stations)} associated",
        )
    )
    checks.append(
        _check(
            "lan_address",
            f"Access point address ({cfg.network.lan_interface})",
            "ok" if lan["address_applied"] else ("fail" if cfg.wifi.enabled else "warn"),
            lan["ipv4"] or "Address not applied",
        )
    )
    dhcp_active = services.get("dhcp", {}).get("running", False)
    checks.append(
        _check(
            "dhcp",
            "DHCP server",
            "ok" if dhcp_active and dhcp["listening"] else ("warn" if dhcp_active else "fail"),
            f"dnsmasq {'running' if dhcp_active else 'stopped'}, UDP 67 {'bound' if dhcp['listening'] else 'not bound'}",
        )
    )
    fw_ok = bool(nft.get("table_present")) if cfg.network.firewall_enabled else True
    checks.append(
        _check(
            "firewall",
            "Firewall and NAT",
            "ok" if fw_ok and (cfg.network.nat_enabled is False or forwarding) else "fail",
            "Rules loaded, forwarding on"
            if fw_ok and forwarding
            else ("Firewall table missing" if not fw_ok else "IPv4 forwarding is disabled"),
        )
    )
    dns_ok = services.get("pihole", {}).get("running") and dns["listening"]
    checks.append(
        _check(
            "dns",
            "DNS resolver (Pi-hole)",
            "ok" if dns_ok else "fail",
            f"port 53 {'listening on ' + ', '.join(dns['addresses']) if dns['listening'] else 'not listening'}",
        )
    )
    checks.append(
        _check(
            "storage",
            "Storage",
            "ok" if storage["percent"] < 85 else ("warn" if storage["percent"] < 93 else "fail"),
            f"{storage['percent']}% used, {util.human_bytes(storage['free'])} free",
        )
    )
    celsius = temp.get("celsius")
    checks.append(
        _check(
            "temperature",
            "CPU temperature",
            "unknown" if celsius is None else ("ok" if celsius < 70 else ("warn" if celsius < 80 else "fail")),
            f"{celsius} °C" if celsius is not None else "Sensor not available",
        )
    )

    remediated: list[str] = []
    if remediate:
        remediated = _remediate(cfg, checks, services, nft, forwarding)

    worst = min(_STATUS_ORDER.get(check["status"], 2) for check in checks) if checks else 3
    overall = {3: "ok", 2: "unknown", 1: "warn", 0: "fail"}[worst]
    report = {
        "generated_at": util.iso_now(),
        "generated_epoch": util.epoch_now(),
        "overall": overall,
        "checks": sorted(checks, key=lambda item: _STATUS_ORDER.get(item["status"], 2)),
        "remediated": remediated,
        "wifi_enabled": cfg.wifi.enabled,
    }
    _write(report)
    if remediated:
        events.record("network", "warning", "Health check repaired the network", details={"actions": remediated})
    return report


def _remediate(cfg, checks: list[dict[str, Any]], services: dict[str, Any], nft: dict[str, Any], forwarding) -> list[str]:
    from . import netapply

    stamp_path = paths.STATE_DIR / "remediate.stamp"
    try:
        last = float(stamp_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        last = 0.0
    if time.time() - last < REMEDIATION_THROTTLE:
        return []
    if not shell.is_root():
        return []

    failures = {check["id"] for check in checks if check["status"] == "fail"}
    actions: list[str] = []
    if cfg.wifi.enabled and "ap_service" in failures:
        try:
            result = netapply.apply(cfg, reason="health-repair")
            actions.append(f"access point reapplied ({'ok' if result.get('ok') else 'failed'})")
        except Exception as exc:
            actions.append(f"access point repair failed: {exc}")
    if cfg.network.firewall_enabled and not nft.get("table_present"):
        try:
            netapply.apply_firewall(cfg)
            actions.append("firewall rules reloaded")
        except Exception as exc:
            actions.append(f"firewall repair failed: {exc}")
    if cfg.network.nat_enabled and forwarding is False:
        try:
            netapply.apply_sysctl()
            actions.append("IPv4 forwarding re-enabled")
        except Exception as exc:
            actions.append(f"forwarding repair failed: {exc}")
    if "dhcp" in failures and not services.get("dhcp", {}).get("running"):
        result = shell.run(["systemctl", "restart", paths.UNIT_DNSMASQ], timeout=45)
        actions.append("DHCP service restarted" if result.ok else "DHCP restart failed")
    if actions:
        try:
            paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
            stamp_path.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
    return actions


def _write(report: dict[str, Any]) -> None:
    try:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        temp = paths.HEALTH_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(report, separators=(",", ":")), encoding="utf-8")
        os.chmod(temp, 0o644)
        os.replace(temp, paths.HEALTH_FILE)
    except OSError as exc:
        log.debug("could not persist health report: %s", exc)


def cached(max_age: int = 300) -> dict[str, Any] | None:
    try:
        report = json.loads(paths.HEALTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    age = util.epoch_now() - util.to_int(report.get("generated_epoch"))
    if age > max_age:
        return None
    report["age_seconds"] = age
    return report


def quick_summary(cfg) -> dict[str, Any]:
    """Cheap checks the web process can run itself when no snapshot exists."""
    wan = network.wan_summary(cfg)
    internet = network.internet()
    services = systemd.status_map()
    return {
        "overall": "ok" if wan["online"] else ("warn" if wan["link_up"] else "fail"),
        "generated_at": util.iso_now(),
        "checks": [
            {"id": "wan_link", "label": "Ethernet", "status": "ok" if wan["link_up"] else "fail", "detail": wan["state"]},
            {
                "id": "internet",
                "label": "Internet",
                "status": "ok" if internet["online"] else "fail",
                "detail": f"{internet['latency_ms']} ms" if internet["online"] else "Unreachable",
            },
            {
                "id": "ap_service",
                "label": "Access point",
                "status": "ok" if services.get("ap", {}).get("running") else "fail",
                "detail": services.get("ap", {}).get("active", "unknown"),
            },
        ],
        "age_seconds": 0,
    }
