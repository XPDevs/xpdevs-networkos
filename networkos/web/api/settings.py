"""Product preferences: theme default, Pi-hole retention, mDNS name."""

from __future__ import annotations

import logging
import os

from ... import PRODUCT_NAME, __version__, paths, shell, util
from ... import settings as settings_module
from ...errors import ApiError
from ...services import events
from .. import common
from ..common import json_ok, payload, want_bool
from . import api_bp

log = logging.getLogger(__name__)

THEMES = ("light", "dark", "system")


def _size_of(path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


@api_bp.get("/settings")
def settings_state():
    cfg = common.cfg()
    retention = None
    try:
        from ...services import pihole as pihole_service

        retention = pihole_service.status(cfg, include_details=False).get("stats", {}).get("gravity_domain_count")
    except Exception:  # pragma: no cover - display only
        retention = None
    return json_ok(
        {
            "ui": {"theme_default": cfg.ui.theme_default, "product_name": PRODUCT_NAME},
            "system": {
                "dashboard_hostname": cfg.system.dashboard_hostname,
                "version": __version__,
                "config_path": str(paths.CONFIG_FILE),
                "state_path": str(paths.STATE_DIR),
            },
            "pihole": {"retention_days": cfg.pihole.retention_days, "api_base": cfg.pihole.api_base, "extra": retention},
            "security": {
                "allow_public_access": cfg.security.allow_public_access,
                "session_idle_minutes": cfg.security.session_idle_minutes,
                "session_absolute_hours": cfg.security.session_absolute_hours,
                "login_max_attempts": cfg.security.login_max_attempts,
            },
            "network": {
                "wan_interface": cfg.network.wan_interface,
                "lan_interface": cfg.network.lan_interface,
                "enable_mdns": cfg.network.enable_mdns,
            },
            "storage": {
                "database_bytes": _size_of(paths.DB_FILE),
                "log_bytes": _size_of(paths.APP_LOG),
                "backup_dir": str(paths.BACKUP_DIR),
            },
        }
    )


@api_bp.post("/settings")
def settings_update():
    cfg = common.cfg()
    data = payload()
    changed: list[str] = []

    if "theme_default" in data:
        theme = str(data.get("theme_default", "")).lower()
        if theme not in THEMES:
            raise ApiError("Choose light, dark or system as the default appearance.", code="invalid_theme")
        cfg.ui.theme_default = theme
        changed.append("theme_default")

    if "retention_days" in data:
        days = util.to_int(data.get("retention_days"), cfg.pihole.retention_days)
        if not 1 <= days <= 365:
            raise ApiError("Query retention must be between 1 and 365 days.", code="invalid_retention")
        result = shell.ctl("pihole-retention", str(days), timeout=60)
        if not result.ok:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise ApiError(
                "Pi-hole did not accept the retention change: " + (detail[0] if detail else "unknown error"),
                status=502,
                code="retention_failed",
            )
        cfg.pihole.retention_days = days
        changed.append("retention_days")

    if "dashboard_hostname" in data:
        hostname = str(data.get("dashboard_hostname", "")).strip().lower()
        if not hostname.endswith(".local"):
            hostname = f"{hostname}.local"
        try:
            util.valid_hostname(hostname[:-6])
        except util.ValidationError as exc:
            raise ApiError(exc.message, code="invalid_hostname")
        cfg.system.dashboard_hostname = hostname
        result = shell.ctl("mdns-configure", timeout=60)
        if not result.ok:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise ApiError(
                "The .local name could not be published: " + (detail[0] if detail else "Avahi unavailable"),
                status=502,
                code="mdns_failed",
            )
        changed.append("dashboard_hostname")

    if "allow_public_access" in data:
        enabled = want_bool(data, "allow_public_access")
        if enabled:
            common.audit("security-warning", detail="Public access to the dashboard was enabled")
        cfg.security.allow_public_access = enabled
        changed.append("allow_public_access")

    if "enable_mdns" in data:
        cfg.network.enable_mdns = want_bool(data, "enable_mdns")
        changed.append("enable_mdns")

    if not changed:
        raise ApiError("There was nothing to update.", code="no_changes")

    settings_module.save(cfg)
    common.audit("settings-update", detail=", ".join(changed))
    events.record("system", "info", "Settings updated: " + ", ".join(changed))
    return json_ok({"changed": changed})


@api_bp.post("/settings/mdns")
def republish_mdns():
    result = shell.ctl("mdns-configure", timeout=60)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ApiError(
            "The .local name could not be published: " + (detail[0] if detail else "Avahi unavailable"),
            status=502,
            code="mdns_failed",
        )
    hostname = common.cfg().system.dashboard_hostname
    events.record("network", "info", f"Published {hostname} through Avahi")
    return json_ok({"hostname": hostname})
