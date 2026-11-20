"""Cached access to the privileged snapshot (nftables + wireless stations)."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from .. import collect, shell, util

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None
_CACHE_AT: float = 0.0
_LAST_ERROR: str | None = None
DEFAULT_TTL = 8.0


def _merge_meta(snapshot: dict[str, Any], *, stale: bool, age: float, error: str | None) -> dict[str, Any]:
    enriched = dict(snapshot)
    enriched["_meta"] = {
        "stale": stale,
        "age_seconds": round(age, 1),
        "error": error,
    }
    return enriched


def snapshot(cfg, *, max_age: float = DEFAULT_TTL, live: bool = True) -> dict[str, Any]:
    """Return the snapshot, refreshing it through the privileged CLI when stale.

    Failure is never fatal: the last good snapshot (memory or disk) is used and
    flagged as stale so the UI can say so instead of inventing values.
    """
    global _CACHE, _CACHE_AT, _LAST_ERROR

    now = time.monotonic()
    with _LOCK:
        cached = _CACHE
        age = now - _CACHE_AT
        if cached is not None and age < max_age:
            return _merge_meta(cached, stale=False, age=age, error=_LAST_ERROR)

    if live:
        try:
            result = shell.ctl("state", timeout=25)
            if result.ok and result.stdout.strip():
                parsed = json.loads(result.stdout)
                if isinstance(parsed, dict):
                    with _LOCK:
                        _CACHE = parsed
                        _CACHE_AT = time.monotonic()
                        _LAST_ERROR = None
                    collect.write_state_file(parsed)
                    return _merge_meta(parsed, stale=False, age=0.0, error=None)
            _LAST_ERROR = (result.stderr or result.stdout).strip()[:200] or "state refresh failed"
        except (json.JSONDecodeError, OSError) as exc:
            _LAST_ERROR = str(exc)[:200]

    # Fall back to the on-disk snapshot written by the health check.
    if cached is None:
        cached = collect.read_state_file()
        if cached:
            stamp = util.to_float(cached.get("generated_epoch"))
            age = max(0.0, util.epoch_now() - stamp) if stamp else 3600.0
    if cached is not None:
        with _LOCK:
            _CACHE = cached
            _CACHE_AT = time.monotonic() - age
        return _merge_meta(cached, stale=True, age=age, error=_LAST_ERROR)

    empty = {
        "generated_at": None,
        "nft": {"available": False, "table_present": False, "blocked_macs": [], "chains": [], "detail": "No snapshot available"},
        "stations": {"available": False, "stations": [], "reason": "No snapshot available"},
        "sysctl": {"ipv4_forward": None},
    }
    return _merge_meta(empty, stale=True, age=0.0, error=_LAST_ERROR or "No snapshot available")


def firewall(snapshot_data: dict[str, Any], cfg) -> dict[str, Any]:
    nft = snapshot_data.get("nft") or {}
    forwarding = (snapshot_data.get("sysctl") or {}).get("ipv4_forward")
    chains = nft.get("chains") or []
    input_chain = next((chain for chain in chains if chain.get("hook") == "input"), None)
    return {
        "available": bool(nft.get("available")),
        "enabled": bool(nft.get("table_present")) and cfg.network.firewall_enabled,
        "configured": cfg.network.firewall_enabled,
        "table_present": bool(nft.get("table_present")),
        "nat": bool(nft.get("nat")) and cfg.network.nat_enabled,
        "nat_configured": cfg.network.nat_enabled,
        "forwarding": forwarding,
        "input_policy": (input_chain or {}).get("policy"),
        "blocked_macs": list(nft.get("blocked_macs") or []),
        "counters": nft.get("counters") or {"packets": 0, "bytes": 0},
        "detail": nft.get("detail"),
    }


def stations(snapshot_data: dict[str, Any]) -> dict[str, Any]:
    return snapshot_data.get("stations") or {"available": False, "stations": [], "reason": "Unavailable"}
