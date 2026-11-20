"""Read systemd unit state and restart units from a fixed allowlist."""

from __future__ import annotations

import logging
from typing import Any

from .. import paths, shell
from ..errors import ApiError, CommandError

log = logging.getLogger(__name__)

#: Managed units, keyed by the identifier used in the UI.
UNITS: dict[str, str] = {
    "ap": paths.UNIT_AP,
    "dhcp": paths.UNIT_DNSMASQ,
    "net": paths.UNIT_NET,
    "web": paths.UNIT_WEB,
    "pihole": paths.UNIT_PIHOLE,
    "lighttpd": paths.UNIT_LIGHTTPD,
    "mdns": paths.UNIT_MDNS,
}

#: Units an administrator may restart from the dashboard.
RESTARTABLE = ("ap", "dhcp", "net", "pihole", "lighttpd", "web", "mdns")

PROPERTIES = "Id,LoadState,ActiveState,SubState,UnitFileState,ActiveEnterTimestamp"


def _parse_show(output: str) -> dict[str, dict[str, str]]:
    units: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            if current.get("Id"):
                units[current["Id"]] = current
            current = {}
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            current[key] = value
    if current.get("Id"):
        units[current["Id"]] = current
    return units


def show(unit_names: list[str] | None = None) -> dict[str, dict[str, str]]:
    names = unit_names or list(UNITS.values())
    if not shell.available("systemctl"):
        return {}
    result = shell.run(
        ["systemctl", "show", f"--property={PROPERTIES}", "--no-pager", "--", *names], timeout=8
    )
    if not result.ok:
        log.debug("systemctl show failed: %s", result.stderr.strip())
        return {}
    return _parse_show(result.stdout)


def _normalise(raw: dict[str, str] | None) -> dict[str, Any]:
    raw = raw or {}
    active = raw.get("ActiveState", "unknown")
    enabled = raw.get("UnitFileState", "unknown")
    load = raw.get("LoadState", "unknown")
    return {
        "active": active,
        "state": raw.get("SubState", "unknown"),
        "enabled": enabled in ("enabled", "enabled-runtime", "static", "generated"),
        "enabled_state": enabled,
        "loaded": load == "loaded",
        "since": raw.get("ActiveEnterTimestamp") or None,
        "running": active == "active",
        "healthy": active == "active",
    }


def status_map() -> dict[str, dict[str, Any]]:
    raw = show()
    return {key: _normalise(raw.get(unit)) for key, unit in UNITS.items()}


def status(key: str) -> dict[str, Any]:
    unit = UNITS.get(key)
    if not unit:
        raise ApiError("Unknown service.", status=404, code="not_found")
    raw = show([unit])
    return {"unit": unit, **_normalise(raw.get(unit))}


def restart(key: str) -> dict[str, Any]:
    if key not in RESTARTABLE:
        raise ApiError("That service cannot be restarted from the dashboard.", status=400, code="not_allowed")
    unit = UNITS[key]
    result = shell.ctl("service-restart", key, timeout=90)
    if not result.ok:
        raise CommandError(
            f"Restarting {unit} failed.",
            returncode=result.returncode,
            stderr=(result.stderr or result.stdout).strip()[:400],
            command=f"systemctl restart {unit}",
        )
    return {"unit": unit, "restarted": True}


def journal(unit_key: str, lines: int = 60) -> list[str]:
    """Recent journal lines for a managed unit (read-only, no shell)."""
    unit = UNITS.get(unit_key)
    if not unit or not shell.available("journalctl"):
        return []
    result = shell.run(
        ["journalctl", "--no-pager", "--unit", unit, "--lines", str(max(1, min(int(lines), 200))), "--output", "short-iso"],
        timeout=8,
    )
    if not result.ok:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]
