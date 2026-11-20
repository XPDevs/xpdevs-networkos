"""LAN addressing, DHCP, firewall and connectivity checks."""

from __future__ import annotations

import logging

from ... import shell, util
from ...errors import ApiError
from ...services import events, network, systemd, systemstate
from .. import common
from ..common import json_ok, payload, want_bool
from . import api_bp

log = logging.getLogger(__name__)


def _network_payload(cfg) -> dict:
    snapshot = systemstate.snapshot(cfg)
    return {
        "wan": network.wan_summary(cfg),
        "lan": network.lan_summary(cfg),
        "firewall": systemstate.firewall(snapshot, cfg),
        "dns": network.dns_service(),
        "dhcp": network.dhcp_service(cfg.network.lan_address),
        "services": systemd.status_map(),
        "config": {
            "lan_address": cfg.network.lan_address,
            "lan_prefix": cfg.network.lan_prefix,
            "dhcp_start": cfg.network.dhcp_start,
            "dhcp_end": cfg.network.dhcp_end,
            "lease_time": cfg.network.lease_time,
            "domain": cfg.network.domain,
            "dns_servers": cfg.dns_servers(),
            "nat_enabled": cfg.network.nat_enabled,
            "firewall_enabled": cfg.network.firewall_enabled,
            "mgmt_ssh": cfg.network.mgmt_ssh,
            "wan_interface": cfg.network.wan_interface,
            "lan_interface": cfg.network.lan_interface,
        },
        "snapshot": snapshot.get("_meta", {}),
    }


@api_bp.get("/network")
def network_state():
    return json_ok(_network_payload(common.cfg()))


@api_bp.post("/network/lan")
def network_lan():
    cfg = common.cfg()
    data = payload()
    try:
        address = str(util.parse_ipv4(data.get("lan_address")))
        prefix = util.valid_prefix(data.get("lan_prefix"))
        if prefix is None:
            raise util.ValidationError("lan_prefix", "The subnet prefix must be between 8 and 30.")
        start = str(util.parse_ipv4(data.get("dhcp_start")))
        end = str(util.parse_ipv4(data.get("dhcp_end")))
        util.check_lan_plan(address, prefix, start, end)
        lease = util.valid_lease_time(data.get("lease_time"))
        domain = util.valid_domain_name(data.get("domain", cfg.network.domain), "domain")
    except util.ValidationError as exc:
        raise ApiError(exc.message, code=exc.field)

    dns_value = data.get("dns_servers")
    if isinstance(dns_value, list):
        dns_list = [str(item).strip() for item in dns_value if str(item).strip()]
    else:
        dns_list = [part.strip() for part in str(dns_value or "").replace(";", ",").split(",") if part.strip()]
    for server in dns_list:
        if not util.valid_ipv4(server):
            raise ApiError(f"'{server}' is not a valid DNS server address.", code="dns_servers")

    args = [
        "lan-apply",
        "--address",
        address,
        "--prefix",
        str(prefix),
        "--dhcp-start",
        start,
        "--dhcp-end",
        end,
        "--lease",
        lease,
        "--domain",
        domain,
        "--dns",
        ",".join(dns_list),
    ]
    result = shell.ctl(*args, timeout=240)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ApiError(
            "The network change was rejected: " + (detail[0] if detail else "unknown error"),
            status=502,
            code="apply_failed",
        )
    common.audit(
        "network-lan",
        target=f"{address}/{prefix}",
        detail=f"DHCP {start}-{end}, lease {lease}, DNS {','.join(dns_list) or address}",
    )
    events.record("network", "success", f"LAN changed to {address}/{prefix}, DHCP {start}–{end}")
    payload_data = _network_payload(common.cfg())
    payload_data["applied"] = True
    return json_ok(payload_data)


@api_bp.post("/network/firewall")
def network_firewall():
    enabled = want_bool(payload(), "enabled")
    result = shell.ctl("firewall", "enable" if enabled else "disable", timeout=120)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ApiError(
            "The firewall change failed: " + (detail[0] if detail else "unknown error"),
            status=502,
            code="apply_failed",
        )
    common.audit("network-firewall", detail="enabled" if enabled else "disabled")
    events.record(
        "network",
        "warning" if not enabled else "success",
        "Firewall enabled" if enabled else "Firewall disabled",
    )
    return json_ok({"enabled": enabled})


@api_bp.post("/network/nat")
def network_nat():
    enabled = want_bool(payload(), "enabled")
    result = shell.ctl("nat", "enable" if enabled else "disable", timeout=120)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ApiError(
            "The NAT change failed: " + (detail[0] if detail else "unknown error"),
            status=502,
            code="apply_failed",
        )
    common.audit("network-nat", detail="enabled" if enabled else "disabled")
    events.record("network", "success" if enabled else "warning", f"NAT {'enabled' if enabled else 'disabled'}")
    return json_ok({"enabled": enabled})


@api_bp.post("/network/test")
def network_test():
    cfg = common.cfg()
    network.invalidate_cache()
    report = network.connectivity_report(cfg)
    common.audit("network-test", detail=f"connectivity ok={report['ok']}")
    return json_ok(report)
