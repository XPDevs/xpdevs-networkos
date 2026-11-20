"""Overview, live metrics, health and the event feed."""

from __future__ import annotations

import json
import logging

from flask import request

from ... import __version__ as VERSION
from ... import health as health_module
from ... import shell, util
from ...errors import ApiError
from ...services import devices as device_service
from ...services import events, network, pihole as pihole_service, resources, systemd, systeminfo
from ...services import systemstate, wireless as wireless_service
from .. import common
from ..common import json_ok
from . import api_bp

log = logging.getLogger(__name__)


def _interfaces(cfg) -> list[str]:
    return [cfg.network.wan_interface, cfg.network.lan_interface]


def build_overview(cfg=None) -> dict:
    """Single source of truth for the overview page and its API endpoint."""
    cfg = cfg or common.cfg()
    snapshot = systemstate.snapshot(cfg)
    wan = network.wan_summary(cfg)
    lan = network.lan_summary(cfg)
    pihole = pihole_service.status(cfg, include_details=False)
    catalogue = device_service.collect(cfg, snapshot)
    stations = systemstate.stations(snapshot).get("stations", [])
    ap = wireless_service.ap_status(cfg, stations=stations)
    report = health_module.cached() or health_module.quick_summary(cfg)
    hardware = systeminfo.model()
    return {
        "system": {
            "model": hardware["name"],
            "is_pi": hardware["is_raspberry_pi"],
            "os": systeminfo.os_release()["name"],
            "version": VERSION,
        },
        "resources": resources.snapshot(_interfaces(cfg)),
        "wan": wan,
        "lan": lan,
        "ap": ap,
        "pihole": pihole,
        "devices": {**catalogue["summary"], "recent": catalogue["devices"][:6]},
        "firewall": systemstate.firewall(snapshot, cfg),
        "health": report,
        "events": events.recent(6),
        "services": systemd.status_map(),
        "snapshot": snapshot.get("_meta", {}),
    }


@api_bp.get("/overview")
def overview():
    return json_ok(build_overview())


@api_bp.get("/metrics")
def metrics():
    cfg = common.cfg()
    snapshot = resources.snapshot(_interfaces(cfg))
    return json_ok(
        {
            "cpu": snapshot["cpu"],
            "memory": snapshot["memory"],
            "storage": snapshot["storage"],
            "temperature": snapshot["temperature"],
            "uptime": snapshot["uptime"],
            "traffic": snapshot["traffic"],
            "history": snapshot["history"],
            "interfaces": _interfaces(cfg),
        }
    )


@api_bp.get("/events")
def event_feed():
    limit = util.to_int(request.args.get("limit"), 20)
    category = request.args.get("category")
    return json_ok({"events": events.recent(limit, category)})


@api_bp.get("/health")
def health_status():
    cfg = common.cfg()
    report = health_module.cached()
    if not report:
        report = health_module.quick_summary(cfg)
        report["partial"] = True
    return json_ok(report)


@api_bp.post("/health/run")
def health_run():
    if not shell.available("systemctl"):
        raise ApiError("systemd is not available on this system.", status=409, code="unsupported")
    result = shell.ctl("healthcheck", "--remediate", "--json", timeout=180)
    if not result.ok:
        raise ApiError(
            "The diagnostic run failed: " + (result.stderr or result.stdout).strip()[:300],
            status=502,
            code="diagnostics_failed",
        )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ApiError("The diagnostic run returned no readable report.", status=502, code="diagnostics_failed")
    common.audit("run-diagnostics", detail="Ran the full health check with repairs enabled")
    return json_ok(report)
