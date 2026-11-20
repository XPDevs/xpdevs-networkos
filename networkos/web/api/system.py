"""System information and destructive maintenance actions."""

from __future__ import annotations

import json
import logging
import threading
import time

from flask import request

from ... import PRODUCT_NAME, VENDOR_URL, __version__, netapply, shell, util
from ...errors import ApiError
from ...services import events, systemd, systeminfo
from .. import common
from ..common import json_ok, payload, require_confirmation
from . import api_bp

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_SUMMARY_CACHE: dict = {}
_SUMMARY_AT = 0.0
SUMMARY_TTL = 20.0

ACTIONS = {
    "reboot": ("CONFIRM-REBOOT", "system-reboot"),
    "shutdown": ("CONFIRM-SHUTDOWN", "system-shutdown"),
    "update": ("CONFIRM-UPDATE", "system-update"),
}


def _summary(cfg) -> dict:
    global _SUMMARY_CACHE, _SUMMARY_AT
    now = time.monotonic()
    with _LOCK:
        if _SUMMARY_CACHE and now - _SUMMARY_AT < SUMMARY_TTL:
            return _SUMMARY_CACHE
    data = systeminfo.summary()
    with _LOCK:
        _SUMMARY_CACHE, _SUMMARY_AT = data, now
    return data


@api_bp.get("/system")
def system_state():
    cfg = common.cfg()
    summary = _summary(cfg)
    summary["services"] = systemd.status_map()
    summary["last_apply"] = netapply.last_apply()
    summary["safe_mode"] = netapply.safe_mode_reason()
    summary["product"] = {
        "name": PRODUCT_NAME,
        "version": __version__,
        "vendor_url": VENDOR_URL,
        "documents": "docs/",
    }
    summary["events"] = events.recent(12)
    summary["audit"] = events.audit_entries(8)
    return json_ok(summary)


@api_bp.get("/system/journal")
def system_journal():
    unit = str(request.args.get("unit", "web"))
    if unit not in systemd.UNITS:
        raise ApiError("Unknown service.", code="unknown_service")
    lines = systemd.journal(unit, util.to_int(request.args.get("lines"), 60))
    return json_ok({"unit": systemd.UNITS[unit], "lines": lines})


@api_bp.post("/system/action")
def system_action():
    data = payload()
    action = str(data.get("action", "")).lower()
    if action not in ACTIONS:
        raise ApiError("Unsupported system action.", code="invalid_action")
    phrase, command = ACTIONS[action]
    require_confirmation(data, action)
    if action == "update":
        result = shell.ctl(command, "--json", timeout=900)
    else:
        result = shell.ctl(command, timeout=60)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ApiError(
            f"The {action} request failed: " + (detail[0] if detail else "unknown error"),
            status=502,
            code="action_failed",
        )
    common.audit(f"system-{action}", detail=f"Confirmed with {phrase}")
    events.record("system", "warning", f"System {action} requested by the administrator")
    extra = {}
    if action == "update":
        try:
            extra = json.loads(result.stdout)
        except json.JSONDecodeError:
            extra = {"output": result.stdout.strip()[-1000:]}
    return json_ok({"action": action, **extra})


@api_bp.post("/system/service-restart")
def system_service_restart():
    service = str(payload().get("service", ""))
    if service not in systemd.RESTARTABLE:
        raise ApiError("That service cannot be restarted from the dashboard.", code="not_allowed")
    result = shell.ctl("service-restart", service, timeout=150)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ApiError(
            "Restarting the service failed: " + (detail[0] if detail else "unknown error"),
            status=502,
            code="restart_failed",
        )
    common.audit("service-restart", target=service)
    events.record("system", "info", f"Service restarted: {systemd.UNITS[service]}", details={"unit": systemd.UNITS[service]})
    return json_ok({"service": service, "unit": systemd.UNITS[service], "restarted": True})


@api_bp.get("/system/backups")
def system_backups():
    result = shell.ctl("backup-list", "--json", timeout=30)
    if not result.ok:
        raise ApiError("The configuration snapshots could not be listed.", status=502, code="backup_failed")
    try:
        items = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        items = []
    return json_ok({"backups": items})


@api_bp.post("/system/backup")
def system_backup():
    data = payload()
    action = str(data.get("action", "create")).lower()
    if action == "create":
        result = shell.ctl("backup-create", "--label", "manual", timeout=120)
        if not result.ok:
            raise ApiError("The snapshot could not be created.", status=502, code="backup_failed")
        common.audit("backup-create", detail="Manual configuration snapshot")
        return json_ok({"created": True, "detail": result.stdout.strip()})
    if action == "restore":
        name = str(data.get("name", "")).strip()
        if not name:
            raise ApiError("Choose a snapshot to restore.", code="missing_name")
        require_confirmation({"confirm": data.get("confirm")}, "RESTORE")
        result = shell.ctl("backup-restore", name, timeout=300)
        if not result.ok:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise ApiError(
                "The restore failed: " + (detail[0] if detail else "unknown error"),
                status=502,
                code="restore_failed",
            )
        common.audit("backup-restore", target=name)
        events.record("system", "warning", f"Configuration snapshot restored: {name}")
        return json_ok({"restored": name})
    raise ApiError("Unsupported backup action.", code="invalid_action")
