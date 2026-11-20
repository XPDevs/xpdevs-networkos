"""Connected device inventory and per-device block/unblock."""

from __future__ import annotations

import logging

from flask import g

from ... import util
from ...errors import NotFoundError
from ...services import devices as device_service
from ...services import events, systemstate
from .. import common
from ..common import json_ok, payload
from . import api_bp

log = logging.getLogger(__name__)


@api_bp.get("/devices")
def device_list():
    cfg = common.cfg()
    snapshot = systemstate.snapshot(cfg)
    catalogue = device_service.collect(cfg, snapshot)
    return json_ok(
        {
            "devices": catalogue["devices"],
            "summary": catalogue["summary"],
            "leases": catalogue["leases"],
            "snapshot": snapshot.get("_meta", {}),
        }
    )


@api_bp.get("/devices/<mac>")
def device_detail(mac: str):
    cfg = common.cfg()
    device = device_service.find(mac, cfg)
    if not device:
        raise NotFoundError("That device is not on the network.")
    return json_ok({"device": device})


@api_bp.post("/devices/<mac>/block")
def device_block(mac: str):
    normalised = util.normalize_mac(mac)
    if not normalised:
        raise NotFoundError("That device identifier is not valid.")
    result = device_service.set_blocked(normalised, True, username=getattr(g.session, "username", ""))
    common.audit("device-block", target=normalised, detail="Device blocked at the firewall")
    events.record("devices", "warning", f"Device {normalised} blocked", details={"mac": normalised})
    return json_ok(result)


@api_bp.post("/devices/<mac>/unblock")
def device_unblock(mac: str):
    normalised = util.normalize_mac(mac)
    if not normalised:
        raise NotFoundError("That device identifier is not valid.")
    result = device_service.set_blocked(normalised, False, username=getattr(g.session, "username", ""))
    common.audit("device-unblock", target=normalised, detail="Device unblocked")
    events.record("devices", "success", f"Device {normalised} unblocked", details={"mac": normalised})
    return json_ok(result)
