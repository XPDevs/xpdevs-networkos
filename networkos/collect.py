"""Privileged state collection.

Reading the nftables ruleset and the nl80211 station list needs
CAP_NET_ADMIN.  The dashboard therefore never receives that capability: the
privileged CLI (``networkosctl state``) collects this snapshot and hands back
plain JSON, which is also cached on disk so the UI stays useful if a refresh
fails.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import paths, shell, util

log = logging.getLogger(__name__)


def _scan_json(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def nft_state() -> dict[str, Any]:
    """Describe the NetworkOS nftables table: policy, NAT rule, blocked MACs."""
    if not shell.available("nft"):
        return {"available": False, "reason": "nftables is not installed", "table_present": False}

    listing = shell.run(["nft", "-j", "list", "table", "inet", paths.NFT_TABLE], timeout=6)
    if not listing.ok:
        # A missing table is a valid state (firewall disabled), not an error.
        return {
            "available": True,
            "table_present": False,
            "blocked_macs": [],
            "chains": [],
            "nat": False,
            "forwarding": False,
            "detail": "The NetworkOS firewall table is not loaded",
        }
    document = _scan_json(listing.stdout)
    objects = document.get("nftables", [])
    blocked: list[str] = []
    chains: list[dict[str, Any]] = []
    nat_rule = False
    has_forward_chain = False
    counters = {"packets": 0, "bytes": 0}

    for entry in objects:
        if "set" in entry:
            table_set = entry["set"]
            if table_set.get("name") == "blocked_macs":
                for element in table_set.get("elem", []) or []:
                    if isinstance(element, str):
                        mac = util.normalize_mac(element)
                    elif isinstance(element, dict):
                        mac = util.normalize_mac(element.get("elem", {}).get("val") if isinstance(element.get("elem"), dict) else None)
                    else:
                        mac = None
                    if mac:
                        blocked.append(mac)
        elif "chain" in entry:
            chain = entry["chain"]
            chains.append(
                {
                    "name": chain.get("name"),
                    "type": chain.get("type"),
                    "hook": chain.get("hook"),
                    "policy": chain.get("policy"),
                }
            )
            if chain.get("name") == "forward":
                has_forward_chain = True
        elif "rule" in entry:
            rule = entry["rule"]
            for expression in rule.get("expr", []) or []:
                if "masquerade" in expression:
                    nat_rule = True
                if "counter" in expression:
                    counters["packets"] += util.to_int(expression["counter"].get("packets"))
                    counters["bytes"] += util.to_int(expression["counter"].get("bytes"))
                redirect = expression.get("redirect")
                if isinstance(redirect, dict) and redirect.get("port") == 53:
                    nat_rule = nat_rule or True

    return {
        "available": True,
        "table_present": True,
        "blocked_macs": sorted(set(blocked)),
        "chains": chains,
        "nat": nat_rule,
        "forwarding": has_forward_chain,
        "counters": counters,
        "detail": None,
    }


def station_state(interface: str) -> dict[str, Any]:
    from .services import wireless

    if not shell.available("iw"):
        return {"available": False, "reason": "iw is not installed", "stations": []}
    result = shell.run(["iw", "dev", interface, "station", "dump"], timeout=6)
    if not result.ok:
        return {
            "available": False,
            "reason": (result.stderr or result.stdout).strip()[:200] or "The wireless interface is not available",
            "stations": [],
            "interface": interface,
        }
    return {
        "available": True,
        "interface": interface,
        "stations": wireless.parse_station_dump(result.stdout),
    }


def sysctl_state() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, path in (
        ("ipv4_forward", "/proc/sys/net/ipv4/ip_forward"),
        ("ipv6_forward", "/proc/sys/net/ipv6/conf/all/forwarding"),
    ):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                values[key] = handle.read().strip() == "1"
        except OSError:
            values[key] = None
    return values


def privileged_snapshot(cfg) -> dict[str, Any]:
    """Full privileged snapshot consumed by the dashboard."""
    return {
        "generated_at": util.iso_now(),
        "generated_epoch": util.epoch_now(),
        "nft": nft_state(),
        "stations": station_state(cfg.network.lan_interface),
        "sysctl": sysctl_state(),
        "euid": __import__("os").geteuid(),
        "version": __import__("networkos").__version__,
    }


def write_state_file(snapshot: dict[str, Any]) -> None:
    try:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        temp = paths.STATE_DIR / ".state.json.tmp"
        temp.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
        temp.chmod(0o644)
        temp.replace(paths.STATE_DIR / "state.json")
    except OSError as exc:
        log.debug("could not persist privileged state: %s", exc)


def read_state_file() -> dict[str, Any] | None:
    try:
        raw = (paths.STATE_DIR / "state.json").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
