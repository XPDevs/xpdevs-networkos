"""Wireless access point configuration and live client view."""

from __future__ import annotations

import json
import logging
import threading
import time

from flask import request

from ... import shell, util
from ...errors import ApiError
from ...services import devices as device_service
from ...services import events, systemstate, wireless as wireless_service
from .. import common
from ..common import json_ok, payload, want_bool
from . import api_bp

log = logging.getLogger(__name__)

_CAP_LOCK = threading.Lock()
_CAP_CACHE: dict = {}
_CAP_AT = 0.0


def _capabilities(cfg) -> dict:
    global _CAP_CACHE, _CAP_AT
    now = time.monotonic()
    with _CAP_LOCK:
        if _CAP_CACHE and now - _CAP_AT < 300:
            return _CAP_CACHE
    capability = wireless_service.supports_ap(cfg.network.lan_interface)
    with _CAP_LOCK:
        _CAP_CACHE, _CAP_AT = capability, now
    return capability


def _cli_failure(result, action: str) -> ApiError:
    detail = (result.stderr or result.stdout).strip()
    for line in detail.splitlines():
        if line.strip():
            detail = line.strip()
            break
    detail = detail.replace("networkosctl: ", "")[:400]
    return ApiError(f"{action}: {detail}" if detail else f"{action} failed.", status=502, code="apply_failed")


@api_bp.get("/wireless")
def wireless_state():
    cfg = common.cfg()
    snapshot = systemstate.snapshot(cfg)
    stations = systemstate.stations(snapshot)
    ap = wireless_service.ap_status(cfg, stations=stations.get("stations", []))
    catalogue = device_service.collect(cfg, snapshot)
    clients = [device for device in catalogue["devices"] if device["connection"] == "wireless"]
    return json_ok(
        {
            "ap": ap,
            "clients": clients,
            "capabilities": _capabilities(cfg),
            "interfaces": wireless_service.live_interfaces(),
            "stations_reason": stations.get("reason"),
            "config": {
                "ssid": cfg.wifi.ssid,
                "ssid_quoted": cfg.wifi.ssid != util.strip_ssid_quotes(cfg.wifi.ssid),
                "channel": cfg.wifi.channel,
                "country": cfg.wifi.country,
                "hidden": cfg.wifi.hidden,
                "isolate": cfg.wifi.isolate_clients,
                "max_clients": cfg.wifi.max_clients,
                "enabled": cfg.wifi.enabled,
            },
            "snapshot": snapshot.get("_meta", {}),
        }
    )


@api_bp.post("/wireless/config")
def wireless_config():
    cfg = common.cfg()
    data = payload()
    raw_ssid = str(data.get("ssid", cfg.wifi.ssid)).strip()
    try:
        ssid = util.valid_ssid(raw_ssid)
    except util.ValidationError as exc:
        raise ApiError(exc.message, code=exc.field)
    password = str(data.get("password") or "")
    if password:
        try:
            util.valid_passphrase(password)
        except util.ValidationError as exc:
            raise ApiError(exc.message, code=exc.field)
    if ssid != cfg.wifi.ssid and not password:
        raise ApiError(
            "Enter the Wi-Fi password again when you change the network name.",
            code="password_required",
        )

    hw_mode = str(data.get("hw_mode") or cfg.wifi.hw_mode or "g").lower()
    if hw_mode == "a":
        raise ApiError(
            "The Raspberry Pi 3 Model B wireless radio is 2.4 GHz only, so the 5 GHz band is not available.",
            status=409,
            code="unsupported_band",
        )
    try:
        channel = util.valid_channel(data.get("channel", cfg.wifi.channel), hw_mode)
    except util.ValidationError as exc:
        raise ApiError(exc.message, code="channel")
    country = str(data.get("country", cfg.wifi.country) or "").strip().upper()
    if country and (len(country) != 2 or not country.isalpha()):
        raise ApiError("The country code must be two letters, for example GB or DE.", code="country")

    hidden = want_bool(data, "hidden", cfg.wifi.hidden)
    isolate = want_bool(data, "isolate", cfg.wifi.isolate_clients)
    max_clients = util.to_int(data.get("max_clients"), cfg.wifi.max_clients)
    if not 1 <= max_clients <= 200:
        raise ApiError("The client limit must be between 1 and 200.", code="max_clients")

    args = [
        "wifi-apply",
        "--ssid",
        ssid,
        "--channel",
        str(channel),
        "--hw-mode",
        hw_mode,
        "--country",
        country,
        "--hidden",
        "1" if hidden else "0",
        "--isolate",
        "1" if isolate else "0",
        "--max-clients",
        str(max_clients),
    ]
    if password:
        args.append("--password-stdin")
    result = shell.ctl(*args, timeout=240, input_text=f"{password}\n" if password else None)
    if not result.ok:
        raise _cli_failure(result, "The wireless settings were rejected")
    common.audit(
        "wireless-config",
        target=ssid,
        detail=f"channel {channel}, hidden={hidden}, isolate={isolate}, password {'changed' if password else 'unchanged'}",
    )
    events.record("wireless", "success", f"Wireless configuration applied: {ssid} on channel {channel}")
    return json_ok(
        {
            "ssid": ssid,
            "channel": channel,
            "country": country,
            "hidden": hidden,
            "isolate": isolate,
            "max_clients": max_clients,
            "detail": result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "Applied",
        }
    )


@api_bp.post("/wireless/power")
def wireless_power():
    data = payload()
    enabled = want_bool(data, "enabled")
    result = shell.ctl("wifi-power", "enable" if enabled else "disable", timeout=180)
    if not result.ok:
        raise _cli_failure(result, "The access point could not be toggled")
    common.audit("wireless-power", detail="enabled" if enabled else "disabled")
    events.record(
        "wireless",
        "info" if enabled else "warning",
        "Access point enabled" if enabled else "Access point disabled",
    )
    return json_ok({"enabled": enabled})


@api_bp.post("/wireless/restart")
def wireless_restart():
    result = shell.ctl("ap-restart", timeout=120)
    if not result.ok:
        raise _cli_failure(result, "The access point did not restart")
    common.audit("wireless-restart", detail="Access point restarted")
    events.record("wireless", "info", "Access point restarted")
    return json_ok({"restarted": True})


@api_bp.post("/wireless/scan")
def wireless_scan():
    data = payload()
    pause = want_bool(data, "pause_ap", True)
    args = ["scan", "--json"]
    if pause:
        args.append("--pause-ap")
    result = shell.ctl(*args, timeout=90)
    if not result.ok:
        raise _cli_failure(result, "The channel scan failed")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ApiError("The scan returned no readable data.", status=502, code="scan_failed")
    common.audit("wireless-scan", detail=f"scanned {len(report.get('networks', []))} networks")
    return json_ok(report)
