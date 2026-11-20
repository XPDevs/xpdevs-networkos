"""Pi-hole statistics and blocking controls."""

from __future__ import annotations

import logging
import urllib.parse

from ... import util
from ...errors import ApiError, PiholeError
from ...services import events, pihole as pihole_service
from .. import common
from ..common import json_ok, payload, require_text, want_bool
from . import api_bp

log = logging.getLogger(__name__)


def _wrap(exc: PiholeError) -> ApiError:
    return ApiError(str(exc), status=502, code="pihole_error")


@api_bp.get("/adblock")
def adblock_status():
    cfg = common.cfg()
    force = request_refresh()
    try:
        status = pihole_service.status(cfg, force=force, include_details=True)
    except PiholeError as exc:
        raise _wrap(exc)
    return json_ok(status)


def request_refresh() -> bool:
    from flask import request

    return str(request.args.get("refresh", "")).lower() in ("1", "true", "yes")


@api_bp.post("/adblock/blocking")
def adblock_blocking():
    cfg = common.cfg()
    enabled = want_bool(payload(), "enabled")
    try:
        result = pihole_service.set_blocking(cfg, enabled)
    except PiholeError as exc:
        raise _wrap(exc)
    cfg.pihole.blocking_desired = enabled
    from ... import settings as settings_module

    settings_module.save(cfg)
    common.audit("pihole-blocking", detail="enabled" if enabled else "disabled")
    events.record(
        "adblock",
        "info" if enabled else "warning",
        "Ad blocking enabled" if enabled else "Ad blocking paused",
    )
    verification = pihole_service.verify_blocking(force=enabled)
    return json_ok({**result, "verification": verification})


@api_bp.post("/adblock/verify")
def adblock_verify():
    cfg = common.cfg()
    try:
        api = pihole_service.client(cfg)
        verification = pihole_service.verify_blocking(api, force=True)
    except PiholeError as exc:
        raise _wrap(exc)
    common.audit("pihole-verify", detail=f"blocking verified: {verification.get('blocking')}")
    return json_ok(verification)


@api_bp.get("/adblock/queries")
def adblock_queries():
    cfg = common.cfg()
    limit = util.to_int(__import__("flask").request.args.get("limit"), 25)
    try:
        queries = pihole_service.recent_queries(cfg, limit)
    except PiholeError as exc:
        raise _wrap(exc)
    return json_ok({"queries": queries})


@api_bp.post("/adblock/domains")
def adblock_domains():
    cfg = common.cfg()
    data = payload()
    kind = str(data.get("kind", "")).lower()
    if kind not in ("allow", "deny"):
        raise ApiError("Choose whether to allow or block the domain.", code="invalid_kind")
    action = str(data.get("action", "add")).lower()
    if action not in ("add", "remove"):
        raise ApiError("Unsupported domain action.", code="invalid_action")
    raw = data.get("domain")
    if isinstance(raw, list):
        domains = [str(item).strip().lower() for item in raw if str(item).strip()]
    else:
        text = require_text(data, "domain", "a domain name", maximum=253)
        domains = [part.strip().lower() for part in text.replace(",", "\n").splitlines() if part.strip()]
    if not domains:
        raise ApiError("Enter at least one domain.", code="missing_domain")
    if len(domains) > 50:
        raise ApiError("Add at most 50 domains at a time.", code="too_many_domains")
    for domain in domains:
        try:
            util.valid_domain_name(domain)
        except util.ValidationError:
            raise ApiError(f"'{domain}' is not a valid domain name.", code="invalid_domain")

    results = []
    for domain in domains:
        try:
            if action == "add":
                pihole_service.add_domain(cfg, kind, domain)
            else:
                pihole_service.remove_domain(cfg, kind, domain)
            results.append({"domain": domain, "ok": True})
        except PiholeError as exc:
            results.append({"domain": domain, "ok": False, "error": str(exc)})
    failures = [item for item in results if not item["ok"]]
    common.audit(
        f"pihole-{kind}-{action}",
        target=",".join(domains)[:200],
        detail=f"{len(domains) - len(failures)} succeeded, {len(failures)} failed",
    )
    events.record(
        "adblock",
        "success" if not failures else "warning",
        f"{'Allowed' if kind == 'allow' else 'Blocked'} {len(domains) - len(failures)} domain(s)",
        details={"domains": domains[:20]},
    )
    if failures and len(failures) == len(results):
        raise ApiError(failures[0]["error"], status=502, code="pihole_error")
    return json_ok({"results": results})


@api_bp.post("/adblock/lists")
def adblock_lists():
    cfg = common.cfg()
    data = payload()
    action = str(data.get("action", "add")).lower()
    address = require_text(data, "address", "a blocklist address", maximum=500)
    parsed = urllib.parse.urlparse(address)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ApiError("Enter a full blocklist URL starting with http:// or https://.", code="invalid_url")
    if action not in ("add", "remove"):
        raise ApiError("Unsupported blocklist action.", code="invalid_action")
    try:
        if action == "add":
            pihole_service.add_list(cfg, address)
        else:
            pihole_service.remove_list(cfg, address)
    except PiholeError as exc:
        raise _wrap(exc)
    common.audit(f"pihole-list-{action}", target=address[:200])
    events.record("adblock", "info", f"Blocklist {'added' if action == 'add' else 'removed'}", details={"address": address})
    return json_ok({"address": address, "action": action})


@api_bp.post("/adblock/gravity")
def adblock_gravity():
    try:
        result = pihole_service.update_gravity()
    except Exception as exc:  # CommandError and PiholeError share a base
        raise ApiError(str(exc), status=502, code="gravity_failed")
    common.audit("pihole-gravity", detail="Gravity database rebuilt")
    events.record("adblock", "success", "Blocklists updated (gravity rebuild)")
    return json_ok(result)
