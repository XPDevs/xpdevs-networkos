"""Server-rendered dashboard pages.

Pages render their fixed structure and static values on the server; live values
arrive from the JSON API so the interface stays current without reloading.
"""

from __future__ import annotations

from flask import Blueprint, g, render_template

from .. import PRODUCT_NAME, __version__, settings
from ..services import events, systeminfo
from . import common

views_bp = Blueprint("views", __name__)


@views_bp.app_context_processor
def _add_branding():
    return {"product": PRODUCT_NAME, "version": __version__}


def _base(page: str, **extra):
    config = settings.load()
    context = common.page_context(
        page,
        ssid=config.wifi.ssid,
        lan_address=config.network.lan_address,
        lan_interface=config.network.lan_interface,
        wan_interface=config.network.wan_interface,
        wifi_enabled=config.wifi.enabled,
        address=common.client_ip(),
        **extra,
    )
    return context


@views_bp.route("/")
@common.login_required
def overview():
    from .api.overview import build_overview

    model = systeminfo.model()
    return render_template(
        "overview.html",
        **_base(
            "overview",
            data=build_overview(),
            device_model=model["name"],
            recent_events=events.recent(6),
        ),
    )


@views_bp.route("/wireless")
@common.login_required
def wireless():
    config = settings.load()
    return render_template(
        "wireless.html",
        **_base(
            "wireless",
            channel=config.wifi.channel,
            country=config.wifi.country,
            hidden=config.wifi.hidden,
            isolate=config.wifi.isolate_clients,
            max_clients=config.wifi.max_clients,
        ),
    )


@views_bp.route("/devices")
@common.login_required
def devices():
    return render_template("devices.html", **_base("devices"))


@views_bp.route("/adblock")
@common.login_required
def adblock():
    return render_template("adblock.html", **_base("adblock"))


@views_bp.route("/network")
@common.login_required
def network_page():
    config = settings.load()
    return render_template(
        "network.html",
        **_base(
            "network",
            lan_prefix=config.network.lan_prefix,
            dhcp_start=config.network.dhcp_start,
            dhcp_end=config.network.dhcp_end,
            lease_time=config.network.lease_time,
            domain=config.network.domain,
            dns_servers=", ".join(config.network.dns_servers),
            nat_enabled=config.network.nat_enabled,
            firewall_enabled=config.network.firewall_enabled,
            wan_ssh=config.network.wan_ssh,
        ),
    )


@views_bp.route("/security")
@common.login_required
def security_page():
    return render_template(
        "security.html",
        **_base("security", argon2=__import__("networkos.security", fromlist=["ARGON2_AVAILABLE"]).ARGON2_AVAILABLE),
    )


@views_bp.route("/system")
@common.login_required
def system_page():
    return render_template("system.html", **_base("system"))


@views_bp.route("/settings")
@common.login_required
def settings_page():
    config = settings.load()
    return render_template(
        "settings.html",
        **_base(
            "settings",
            dashboard_hostname=config.system.dashboard_hostname,
            retention_days=config.pihole.retention_days,
            theme_default=config.ui.theme_default,
            idle_minutes=config.security.session_idle_minutes,
            session_hours=config.security.session_absolute_hours,
            login_attempts=config.security.login_max_attempts,
            allow_public=config.security.allow_public_access,
            api_base=config.pihole.api_base,
            username=g.session.username,
        ),
    )
