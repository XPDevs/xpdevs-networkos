#!/usr/bin/env python3
"""``networkosctl`` - the only privileged entry point for XPDevs NetworkOS.

The dashboard never runs a shell and never receives root: it calls this program
through a narrow sudo policy.  Every subcommand validates its own arguments, so
a request that reaches here with unexpected data is rejected before anything
touches the system.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import sys
import time
from typing import Any, Callable

from . import (
    __version__,
    backup,
    collect,
    health,
    netapply,
    paths,
    settings,
    shell,
    util,
)
from .errors import NetworkOSError
from .services import events, systemd, wireless
from .services import pihole as pihole_service

SERVICE_USER = "xpdevs-networkos"


def _out(payload: Any, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                continue
            print(f"{key}: {value}")
    elif isinstance(payload, list):
        for item in payload:
            print(item)


def _read_secret() -> str:
    """Read a password from stdin so it never appears in the process table."""
    if sys.stdin.isatty():
        raise NetworkOSError("This command reads the password from standard input.")
    value = sys.stdin.readline().strip()
    if not value:
        raise NetworkOSError("No password was provided on standard input.")
    return value


def _fix_permissions() -> None:
    """Give the service account ownership of its state directory."""
    try:
        account = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        return
    for target in (paths.STATE_DIR, paths.LOG_DIR):
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.chown(target, account.pw_uid, account.pw_gid)
        except (OSError, PermissionError):
            continue
    for name in ("networkos.db", "networkos.db-wal", "networkos.db-shm"):
        candidate = paths.STATE_DIR / name
        if candidate.exists():
            try:
                os.chown(candidate, account.pw_uid, account.pw_gid)
                os.chmod(candidate, 0o600)
            except (OSError, PermissionError):
                continue
    try:
        group = grp.getgrnam(SERVICE_USER)
        for path in (paths.CONFIG_FILE, paths.SECRET_DIR, paths.PIHOLE_PASSWORD_FILE):
            if path.exists():
                os.chown(path, 0, group.gr_gid)
    except (KeyError, OSError):
        pass


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_state(args) -> dict[str, Any]:
    cfg = settings.load()
    snapshot = collect.privileged_snapshot(cfg)
    collect.write_state_file(snapshot)
    return snapshot


def cmd_net_apply(args) -> dict[str, Any]:
    cfg = settings.load()
    return netapply.apply(cfg, reason=args.reason or "manual", restart=not args.no_restart)


def cmd_wifi_apply(args) -> dict[str, Any]:
    cfg = settings.load()
    previous_ssid = cfg.wifi.ssid
    ssid = util.valid_ssid(args.ssid) if args.ssid else cfg.wifi.ssid
    hw_mode = (args.hw_mode or cfg.wifi.hw_mode or "g").lower()
    if hw_mode not in ("g", "a", "b"):
        raise NetworkOSError("Unsupported wireless band.")
    channel = util.valid_channel(args.channel if args.channel is not None else cfg.wifi.channel, hw_mode)
    country = (args.country or cfg.wifi.country or "").strip().upper()
    if country and (len(country) != 2 or not country.isalpha()):
        raise NetworkOSError("The country code must be two letters, for example GB.")

    password = _read_secret() if args.password_stdin else ""
    if password:
        util.valid_passphrase(password)
        psk = util.wpa_psk(ssid, password)
    else:
        if ssid != previous_ssid:
            raise NetworkOSError(
                "Changing the network name requires the Wi-Fi password again, because the "
                "stored key is derived from both."
            )
        psk = cfg.wifi.psk
    if not psk:
        raise NetworkOSError("Set a Wi-Fi password before enabling the access point.")

    cfg.wifi.ssid = ssid
    cfg.wifi.psk = psk
    cfg.wifi.channel = channel
    cfg.wifi.hw_mode = hw_mode
    cfg.wifi.country = country
    if args.hidden is not None:
        cfg.wifi.hidden = args.hidden == "1"
    if args.isolate is not None:
        cfg.wifi.isolate_clients = args.isolate == "1"
    if args.max_clients is not None:
        cfg.wifi.max_clients = max(1, min(int(args.max_clients), 200))
    cfg.wifi.enabled = True
    settings.save(cfg)
    result = netapply.apply(cfg, reason="wifi-config")
    result["ssid"] = ssid
    result["channel"] = channel
    return result


def cmd_wifi_power(args) -> dict[str, Any]:
    cfg = settings.load()
    return netapply.set_wifi_enabled(cfg, args.state == "enable")


def cmd_ap_restart(args) -> dict[str, Any]:
    cfg = settings.load()
    if not cfg.wifi.enabled:
        raise NetworkOSError("The access point is disabled. Enable Wi-Fi first.")
    netapply.restart_services(cfg, restart_ap=True)
    verification = netapply.verify_ap(cfg)
    if not verification["ok"]:
        raise NetworkOSError("The access point did not come back up: " + verification["detail"])
    return {"ok": True, "detail": verification["detail"], "restarted_at": util.iso_now()}


def cmd_device_block(args) -> dict[str, Any]:
    from . import db

    result = netapply.block_device(args.mac)
    db.execute(
        "INSERT INTO devices(mac, blocked, blocked_at, first_seen, last_seen) VALUES(?,1,?,?,?) "
        "ON CONFLICT(mac) DO UPDATE SET blocked = 1, blocked_at = excluded.blocked_at",
        (result["mac"], util.epoch_now(), util.epoch_now(), util.epoch_now()),
    )
    return result


def cmd_device_unblock(args) -> dict[str, Any]:
    from . import db

    result = netapply.unblock_device(args.mac)
    db.execute("UPDATE devices SET blocked = 0, blocked_at = NULL WHERE mac = ?", (result["mac"],))
    return result


def cmd_lan_apply(args) -> dict[str, Any]:
    cfg = settings.load()
    address = util.parse_ipv4(args.address)
    prefix = util.valid_prefix(args.prefix)
    if prefix is None:
        raise NetworkOSError("The subnet prefix must be between 8 and 30.")
    start = util.parse_ipv4(args.dhcp_start)
    end = util.parse_ipv4(args.dhcp_end)
    util.check_lan_plan(str(address), prefix, str(start), str(end))
    lease = util.valid_lease_time(args.lease)
    domain = util.valid_domain_name(args.domain, "domain")
    dns_servers: list[str] = []
    if args.dns:
        for item in str(args.dns).split(","):
            text = item.strip()
            if not text:
                continue
            if not util.valid_ipv4(text):
                raise NetworkOSError(f"'{text}' is not a valid DNS server address.")
            dns_servers.append(text)

    old_gateway = cfg.network.lan_address
    cfg.network.lan_address = str(address)
    cfg.network.lan_prefix = prefix
    cfg.network.dhcp_start = str(start)
    cfg.network.dhcp_end = str(end)
    cfg.network.lease_time = lease
    cfg.network.domain = domain
    cfg.network.dns_servers = dns_servers or [str(address)]
    if not dns_servers and old_gateway != str(address):
        cfg.network.dns_servers = [str(address)]
    settings.save(cfg)
    return netapply.apply_lan_settings(cfg)


def cmd_firewall(args) -> dict[str, Any]:
    cfg = settings.load()
    return netapply.set_firewall_enabled(cfg, args.state == "enable")


def cmd_nat(args) -> dict[str, Any]:
    cfg = settings.load()
    cfg.network.nat_enabled = args.state == "enable"
    settings.save(cfg)
    return netapply.apply(cfg, reason="nat-change", restart=False)


def cmd_pihole_configure(args) -> dict[str, Any]:
    """Point Pi-hole's web/API at loopback and set a machine API credential."""
    import secrets
    import urllib.request

    cfg = settings.load()
    password = _read_secret()
    if len(password) < 16:
        raise NetworkOSError("The generated Pi-hole credential is too short.")

    ftl = shell.binary("pihole-ftl")
    if not ftl:
        raise NetworkOSError("pihole-FTL is not installed.")
    steps: list[str] = []

    def ftl_config(key: str, value: str) -> None:
        result = shell.run(["pihole-ftl", "--config", key, value], timeout=45)
        if not result.ok:
            raise NetworkOSError(f"Could not set {key}: {(result.stderr or result.stdout).strip()[:200]}")

    for key, value in (
        # Pi-hole FTL v6 parses this as a port specification ("8081o" = port
        # 8081, optional), not "address:port". The old value was rejected, FTL
        # fell back to port 80, NetworkOS already owns 80, and every dashboard
        # call to Pi-hole then timed out.
        ("webserver.port", f"{cfg.pihole.api_base.rsplit(':', 1)[-1] or '8081'}o"),
        ("webserver.acl", "127.0.0.1/32"),
        ("dns.listeningMode", "ALL"),
        ("dhcp.active", "false"),
        ("dns.blocking.active", "true"),
        ("ntp.ipv4.active", "false"),
        ("database.maxDBdays", str(cfg.pihole.retention_days)),
        ("misc.privacylevel", "0"),
    ):
        try:
            ftl_config(key, value)
            steps.append(f"{key} = {value}")
        except NetworkOSError as exc:
            steps.append(f"{key} skipped ({exc})")

    # Password: try the documented CLI path first, verify, then fall back.
    configured = False
    setpwd = shell.binary("pihole")
    if setpwd:
        result = shell.run(["pihole", "setpassword"], timeout=45, input_text=f"{password}\n{password}\n")
        configured = result.ok
    if not configured:
        result = shell.run(["pihole-ftl", "--config", "webserver.api.password", password], timeout=45)
        configured = result.ok
    if not configured:
        raise NetworkOSError("Could not set the Pi-hole API password.")

    shell.run(["systemctl", "restart", paths.UNIT_PIHOLE], timeout=90)
    for _ in range(20):
        time.sleep(1)
        try:
            request = urllib.request.Request(
                f"{cfg.pihole.api_base}/api/auth",
                data=json.dumps({"password": password}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=4) as response:
                payload = json.loads(response.read().decode() or "{}")
            if (payload.get("session") or {}).get("valid"):
                configured = True
                break
        except Exception:
            continue
    if not configured:
        raise NetworkOSError("Pi-hole did not accept the new API credential.")

    paths.SECRET_DIR.mkdir(parents=True, exist_ok=True)
    paths.PIHOLE_PASSWORD_FILE.write_text(password + "\n", encoding="utf-8")
    os.chmod(paths.PIHOLE_PASSWORD_FILE, 0o640)
    _fix_permissions()
    steps.append("API credential stored for the dashboard")
    return {"ok": True, "steps": steps, "api_base": cfg.pihole.api_base}


def cmd_pihole_blocking(args) -> dict[str, Any]:
    cfg = settings.load()
    result = pihole_service.set_blocking(cfg, args.state == "enable")
    cfg.pihole.blocking_desired = bool(result["enabled"])
    if args.state == "enable":
        cfg.pihole.blocking_desired = True
    settings.save(cfg)
    return result


def cmd_pihole_gravity(args) -> dict[str, Any]:
    return pihole_service.update_gravity()


def cmd_pihole_retention(args) -> dict[str, Any]:
    days = max(1, min(int(args.days), 365))
    result = shell.run(["pihole-ftl", "--config", "database.maxDBdays", str(days)], timeout=45)
    if not result.ok:
        raise NetworkOSError("Could not change the Pi-hole retention: " + (result.stderr or result.stdout)[:200])
    cfg = settings.load()
    cfg.pihole.retention_days = days
    settings.save(cfg)
    return {"retention_days": days}


def cmd_service_restart(args) -> dict[str, Any]:
    if args.name not in systemd.RESTARTABLE:
        raise NetworkOSError("That service cannot be restarted from here.")
    key = args.name
    result = shell.run(["systemctl", "restart", systemd.UNITS[key]], timeout=90)
    if not result.ok:
        raise NetworkOSError(f"Restarting {systemd.UNITS[key]} failed: {(result.stderr or result.stdout)[:200]}")
    return {"unit": systemd.UNITS[key], "restarted": True}


def cmd_reboot(args) -> dict[str, Any]:
    events.audit("administrator", "system-reboot", detail="Reboot requested from the dashboard")
    result = shell.run(["systemctl", "reboot"], timeout=15)
    if not result.ok:
        raise NetworkOSError("The reboot request was refused: " + (result.stderr or result.stdout)[:200])
    return {"ok": True, "detail": "The system is rebooting."}


def cmd_shutdown(args) -> dict[str, Any]:
    events.audit("administrator", "system-shutdown", detail="Shutdown requested from the dashboard")
    result = shell.run(["systemctl", "poweroff"], timeout=15)
    if not result.ok:
        raise NetworkOSError("The shutdown request was refused: " + (result.stderr or result.stdout)[:200])
    return {"ok": True, "detail": "The system is shutting down."}


def cmd_system_update(args) -> dict[str, Any]:
    steps: list[str] = []
    update = shell.run(["apt-get", "update"], timeout=300)
    if not update.ok:
        raise NetworkOSError("Package index update failed: " + (update.stderr or update.stdout)[-300:])
    steps.append("package index refreshed")
    upgrade = shell.run(
        ["apt-get", "-y", "-o", "Dpkg::Options::=--force-confold", "-o", "Dpkg::Options::=--force-confdef", "upgrade"],
        timeout=900,
    )
    if not upgrade.ok:
        raise NetworkOSError("Package upgrade failed: " + (upgrade.stderr or upgrade.stdout)[-300:])
    steps.append("installed package updates")
    return {"ok": True, "steps": steps, "at": util.iso_now()}


def cmd_admin_create(args) -> dict[str, Any]:
    from . import db, security

    password = _read_secret()
    username = str(args.username or "").strip().lower()
    db.init()
    user = security.create_or_update_user(username, password)
    _fix_permissions()
    return {"username": user.username, "created": True}


def cmd_admin_passwd(args) -> dict[str, Any]:
    from . import db, security

    password = _read_secret()
    db.init()
    user = security.get_user(str(args.username or "").strip().lower())
    if not user:
        raise NetworkOSError("That administrator account does not exist.")
    security.set_password(user.id, password)
    security.delete_user_sessions(user.id)
    _fix_permissions()
    return {"username": user.username, "password_changed": True}


def cmd_backup_create(args) -> dict[str, Any]:
    name = backup.create(args.label or "manual")
    if not name:
        raise NetworkOSError("The backup could not be created.")
    return {"backup": name}


def cmd_backup_list(args) -> list[dict[str, Any]]:
    return backup.list_backups()


def cmd_backup_restore(args) -> dict[str, Any]:
    restored = backup.restore(args.name)
    cfg = settings.load(use_cache=False)
    applied = netapply.apply(cfg, reason="restore")
    return {"restored": restored, "applied": applied.get("ok", False)}


def cmd_healthcheck(args) -> dict[str, Any]:
    cfg = settings.load()
    report = health.run(cfg, remediate=bool(args.remediate))
    return report


def cmd_scan(args) -> dict[str, Any]:
    """Scan neighbouring networks to help pick a clear channel.

    A single-radio access point cannot scan while it is beaconing, so the scan
    pauses hostapd and restarts it afterwards (clients reconnect automatically).
    """
    cfg = settings.load()
    iface = cfg.network.lan_interface
    if not shell.available("iw"):
        raise NetworkOSError("iw is not installed, so scanning is unavailable.")
    paused = False
    try:
        if args.pause_ap and cfg.wifi.enabled:
            shell.run(["systemctl", "stop", paths.UNIT_AP], timeout=30)
            paused = True
            time.sleep(2)
        shell.run(["ip", "link", "set", iface, "up"], timeout=8)
        result = shell.run(["iw", "dev", iface, "scan"], timeout=30)
    finally:
        if paused:
            shell.run(["systemctl", "start", paths.UNIT_AP], timeout=45)
            time.sleep(3)
    if not result.ok:
        raise NetworkOSError(
            "The scan failed: "
            + (result.stderr or result.stdout).strip()[:200]
            + " (the access point was restarted)"
        )
    networks = []
    current: dict[str, Any] | None = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("BSS "):
            if current:
                networks.append(current)
            current = {"bssid": stripped.split()[1].split("(")[0], "ssid": "", "channel": None, "signal": None}
        elif current is not None:
            if stripped.startswith("SSID:"):
                current["ssid"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("signal:"):
                current["signal"] = util.to_float(stripped.split(":", 1)[1].replace("dBm", "").strip())
            elif stripped.startswith("DS Parameter set: channel"):
                current["channel"] = util.to_int(stripped.rsplit(" ", 1)[-1])
    if current:
        networks.append(current)
    usage: dict[int, int] = {}
    for network in networks:
        channel = network.get("channel")
        if isinstance(channel, int):
            usage[channel] = usage.get(channel, 0) + 1
    recommended = min(range(1, 12), key=lambda ch: (usage.get(ch, 0), abs(ch - 6)))
    return {"networks": networks, "channel_usage": usage, "recommended_channel": recommended}


def cmd_mdns_configure(args) -> dict[str, Any]:
    """Publish the dashboard name through Avahi (works on any address change)."""
    from . import configgen  # noqa: F401  (kept for symmetry with the installer)

    cfg = settings.load()
    conf = "/etc/avahi/avahi-daemon.conf"
    if not os.path.exists(conf):
        raise NetworkOSError("Avahi is not installed, so the .local name cannot be published.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    keep = f"/etc/avahi/avahi-daemon.conf.networkos-{stamp}.bak"
    with open(conf, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    if not os.path.exists(keep):
        with open(keep, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
    host = cfg.system.dashboard_hostname.split(".")[0]
    output: list[str] = []
    in_server = False
    applied = False
    for line in lines:
        if line.strip().startswith("["):
            in_server = line.strip().lower() == "[server]"
            output.append(line)
            continue
        if in_server and line.strip().startswith("host-name="):
            output.append(f"host-name={host}\n")
            applied = True
            continue
        output.append(line)
    if not applied:
        insert_at = next((i for i, line in enumerate(output) if line.strip().lower() == "[server]"), None)
        if insert_at is None:
            output.insert(0, f"[server]\nhost-name={host}\n")
        else:
            output.insert(insert_at + 1, f"host-name={host}\n")
    with open(conf, "w", encoding="utf-8") as handle:
        handle.writelines(output)
    shell.run(["systemctl", "restart", "avahi-daemon"], timeout=30)
    return {"hostname": cfg.system.dashboard_hostname, "published": host}


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="networkosctl",
        description=f"{__version__} - XPDevs NetworkOS privileged control utility",
        epilog="This tool is restricted by a sudo policy; see docs/SECURITY.md.",
    )
    parser.add_argument("--json", action="store_true", help="print machine readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("state", help="collect the privileged system snapshot")
    p.set_defaults(func=cmd_state, needs_root=False)

    p = sub.add_parser("net-apply", help="apply the whole network configuration")
    p.add_argument("--reason", default="manual")
    p.add_argument(
        "--no-restart",
        action="store_true",
        help="render and apply without restarting the access point (boot ordering)",
    )
    p.set_defaults(func=cmd_net_apply)

    p = sub.add_parser("wifi-apply", help="validate and apply wireless settings")
    p.add_argument("--ssid")
    p.add_argument("--channel")
    p.add_argument("--hw-mode")
    p.add_argument("--country")
    p.add_argument("--hidden", choices=["0", "1"])
    p.add_argument("--isolate", choices=["0", "1"])
    p.add_argument("--max-clients")
    p.add_argument("--password-stdin", action="store_true", help="read the Wi-Fi password from stdin")
    p.set_defaults(func=cmd_wifi_apply)

    p = sub.add_parser("wifi-power", help="enable or disable the access point")
    p.add_argument("state", choices=["enable", "disable"])
    p.set_defaults(func=cmd_wifi_power)

    p = sub.add_parser("ap-restart", help="restart hostapd and verify the access point")
    p.set_defaults(func=cmd_ap_restart)

    p = sub.add_parser("device-block", help="block a client by MAC address")
    p.add_argument("mac")
    p.set_defaults(func=cmd_device_block)

    p = sub.add_parser("device-unblock", help="unblock a client by MAC address")
    p.add_argument("mac")
    p.set_defaults(func=cmd_device_unblock)

    p = sub.add_parser("lan-apply", help="change LAN addressing and DHCP")
    p.add_argument("--address", required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--dhcp-start", required=True)
    p.add_argument("--dhcp-end", required=True)
    p.add_argument("--lease", required=True)
    p.add_argument("--domain", required=True)
    p.add_argument("--dns", default="")
    p.set_defaults(func=cmd_lan_apply)

    p = sub.add_parser("firewall", help="enable or disable the firewall")
    p.add_argument("state", choices=["enable", "disable"])
    p.set_defaults(func=cmd_firewall)

    p = sub.add_parser("nat", help="enable or disable NAT for wireless clients")
    p.add_argument("state", choices=["enable", "disable"])
    p.set_defaults(func=cmd_nat)

    p = sub.add_parser("pihole-configure", help="configure the Pi-hole API for the dashboard")
    p.add_argument("--password-stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_pihole_configure)

    p = sub.add_parser("pihole-blocking", help="enable or disable Pi-hole blocking")
    p.add_argument("state", choices=["enable", "disable"])
    p.set_defaults(func=cmd_pihole_blocking)

    p = sub.add_parser("pihole-gravity", help="rebuild the Pi-hole gravity database")
    p.set_defaults(func=cmd_pihole_gravity)

    p = sub.add_parser("pihole-retention", help="set Pi-hole query retention in days")
    p.add_argument("days")
    p.set_defaults(func=cmd_pihole_retention)

    p = sub.add_parser("service-restart", help="restart a managed service")
    p.add_argument("name", choices=list(systemd.RESTARTABLE))
    p.set_defaults(func=cmd_service_restart)

    p = sub.add_parser("system-reboot", help="reboot the system")
    p.set_defaults(func=cmd_reboot)

    p = sub.add_parser("system-shutdown", help="power the system off")
    p.set_defaults(func=cmd_shutdown)

    p = sub.add_parser("system-update", help="install operating system updates")
    p.set_defaults(func=cmd_system_update)

    p = sub.add_parser("admin-create", help="create or reset the administrator account")
    p.add_argument("--username", required=True)
    p.add_argument("--password-stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_admin_create)

    p = sub.add_parser("admin-passwd", help="change the administrator password")
    p.add_argument("--username", required=True)
    p.add_argument("--password-stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_admin_passwd)

    p = sub.add_parser("backup-create", help="snapshot the network configuration")
    p.add_argument("--label", default="manual")
    p.set_defaults(func=cmd_backup_create)

    p = sub.add_parser("backup-list", help="list configuration snapshots")
    p.set_defaults(func=cmd_backup_list, needs_root=False)

    p = sub.add_parser("backup-restore", help="restore a configuration snapshot")
    p.add_argument("name")
    p.set_defaults(func=cmd_backup_restore)

    p = sub.add_parser("healthcheck", help="run the health checks")
    p.add_argument("--remediate", action="store_true")
    p.add_argument("--json", action="store_true", dest="json_flag")
    p.set_defaults(func=cmd_healthcheck)

    p = sub.add_parser("scan", help="scan for nearby wireless networks")
    p.add_argument("--pause-ap", action="store_true", help="pause the access point while scanning")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("mdns-configure", help="publish the dashboard .local name")
    p.set_defaults(func=cmd_mdns_configure)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    wants_json = bool(getattr(args, "json", False) or getattr(args, "json_flag", False))
    needs_root = getattr(args, "needs_root", True)
    if needs_root and os.geteuid() != 0:
        print("networkosctl: this command must run as root", file=sys.stderr)
        return 2
    try:
        settings.load()
        payload = args.func(args)
        if getattr(args, "command", "") != "state":
            _fix_permissions()
        if getattr(args, "command", "") == "state":
            print(json.dumps(payload, default=str))
        else:
            _out(payload, as_json=wants_json)
        return 0
    except NetworkOSError as exc:
        print(f"networkosctl: {exc}", file=sys.stderr)
        if wants_json:
            print(json.dumps({"ok": False, "error": exc.to_payload()}, default=str))
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - unexpected failure path
        print(f"networkosctl: unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
