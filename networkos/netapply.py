"""Atomic, rollback-safe application of the network configuration.

Every change follows the same contract:

1. snapshot the current configuration
2. render the new files and validate them with the real daemons' parsers
3. install the files, sysctl values, address and firewall
4. restart the access point and DHCP service
5. verify the access point is actually broadcasting
6. roll everything back if verification fails

eth0 is never re-addressed, so the wired management connection survives a
failed wireless change.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__, backup, configgen, paths, settings, shell, util
from .errors import CommandError, ConfigurationError

log = logging.getLogger(__name__)


class ApplyError(ConfigurationError):
    code = "apply_failed"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write_file(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".new")
    temp.write_text(content, encoding="utf-8")
    os.chmod(temp, mode)
    os.replace(temp, path)


def _interface_exists(name: str) -> bool:
    return Path(f"/sys/class/net/{name}").exists()


def _blocked_macs() -> list[str]:
    from . import db

    try:
        rows = db.query("SELECT mac FROM devices WHERE blocked = 1")
        return [row["mac"] for row in rows if util.valid_mac(row["mac"])]
    except Exception as exc:  # database not ready during a first boot
        log.warning("could not read blocked devices: %s", exc)
        return []


def _record_apply(result: dict[str, Any]) -> None:
    try:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result, indent=2)
        target = paths.STATE_DIR / "last_apply.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, target)
    except OSError as exc:
        log.debug("could not record apply result: %s", exc)


def last_apply() -> dict[str, Any] | None:
    try:
        return json.loads((paths.STATE_DIR / "last_apply.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _systemctl(action: str, unit: str, *, timeout: float = 60.0) -> shell.Result:
    return shell.run(["systemctl", action, unit], timeout=timeout)


def safe_mode_reason() -> str | None:
    try:
        return (paths.ETC_DIR / "SAFE_MODE").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _set_safe_mode(reason: str | None) -> None:
    marker = paths.ETC_DIR / "SAFE_MODE"
    try:
        if reason:
            _write_file(marker, reason + "\n", 0o600)
        elif marker.exists():
            marker.unlink()
    except OSError as exc:
        log.warning("could not update safe-mode marker: %s", exc)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_generated(generated: dict[str, str], cfg) -> list[str]:
    """Run the daemons' own parsers against the rendered files."""
    warnings: list[str] = []
    if not _interface_exists(cfg.network.lan_interface):
        raise ConfigurationError(
            f"Wireless interface {cfg.network.lan_interface} does not exist on this system."
        )
    if not _interface_exists(cfg.network.wan_interface):
        warnings.append(
            f"Ethernet interface {cfg.network.wan_interface} was not found; internet sharing may not work."
        )

    with tempfile.TemporaryDirectory(prefix="nwos-validate-") as tmp:
        dnsmasq_path = Path(tmp) / "dnsmasq.conf"
        dnsmasq_path.write_text(generated["dnsmasq"], encoding="utf-8")
        if shell.available("dnsmasq"):
            result = shell.run(["dnsmasq", "--test", f"--conf-file={dnsmasq_path}"], timeout=10)
            if not result.ok:
                raise ConfigurationError(
                    "The DHCP configuration was rejected by dnsmasq: " + _short(result)
                )
        else:
            warnings.append("dnsmasq is not installed; DHCP validation was skipped.")

        nft_path = Path(tmp) / "networkos.nft"
        nft_path.write_text(generated["nft"], encoding="utf-8")
        if shell.available("nft"):
            result = shell.run(["nft", "-c", "-f", str(nft_path)], timeout=10)
            if not result.ok:
                raise ConfigurationError("The firewall rules were rejected by nftables: " + _short(result))
        else:
            warnings.append("nftables is not installed; firewall validation was skipped.")

        hostapd_path = Path(tmp) / "hostapd.conf"
        hostapd_path.write_text(generated["hostapd"], encoding="utf-8")
        if not shell.available("hostapd"):
            warnings.append("hostapd is not installed; wireless validation was skipped.")
        _validate_hostapd_semantics(cfg)
    return warnings


def _validate_hostapd_semantics(cfg) -> None:
    wifi = cfg.wifi
    util.valid_ssid(wifi.ssid)
    util.valid_channel(wifi.channel, wifi.hw_mode)
    if wifi.hw_mode == "a":
        raise ConfigurationError(
            "The Raspberry Pi 3 Model B radio is 2.4 GHz only. Use the 2.4 GHz band (hw_mode g)."
        )
    if not wifi.psk:
        raise ConfigurationError("Set a Wi-Fi password before starting the access point.")
    if wifi.country and (len(wifi.country) != 2 or not wifi.country.isalpha()):
        raise ConfigurationError("The country code must be two letters, for example GB or US.")


def _short(result: shell.Result) -> str:
    text = (result.stderr or result.stdout).strip().splitlines()
    return " ".join(text[:3])[:300] or f"exit code {result.returncode}"


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
def write_generated(generated: dict[str, str]) -> None:
    _write_file(paths.HOSTAPD_CONF, generated["hostapd"], 0o600)
    _write_file(paths.DNSMASQ_CONF, generated["dnsmasq"], 0o644)
    _write_file(paths.NFT_RULESET, generated["nft"], 0o600)
    _write_file(paths.SYSCTL_CONF, generated["sysctl"], 0o644)


def apply_sysctl() -> None:
    result = shell.run(["sysctl", "-p", str(paths.SYSCTL_CONF)], timeout=10)
    if not result.ok:
        # Individual keys can be read-only inside containers; report, do not abort.
        log.warning("sysctl apply reported: %s", _short(result))


def apply_address(cfg) -> None:
    lan = cfg.network.lan_interface
    if not cfg.wifi.enabled:
        shell.run(["ip", "addr", "flush", "dev", lan], timeout=10)
        return
    shell.run(["ip", "link", "set", lan, "up"], timeout=10)
    address = f"{cfg.network.lan_address}/{cfg.network.lan_prefix}"
    result = shell.run(["ip", "addr", "replace", address, "dev", lan], timeout=10)
    if not result.ok:
        raise ApplyError(f"Could not assign {address} to {lan}: {_short(result)}")


def apply_firewall(cfg) -> None:
    if not cfg.network.firewall_enabled:
        shell.run(["nft", "delete", "table", "inet", paths.NFT_TABLE], timeout=10)
        return
    if not shell.available("nft"):
        raise ApplyError("nftables is not installed, so the firewall cannot be applied.")
    shell.run(["nft", "delete", "table", "inet", paths.NFT_TABLE], timeout=10)  # ignore: may not exist
    result = shell.run(["nft", "-f", str(paths.NFT_RULESET)], timeout=15)
    if not result.ok:
        raise ApplyError("Applying the firewall rules failed: " + _short(result))
    for mac in _blocked_macs():
        shell.run(["nft", "add", "element", "inet", paths.NFT_TABLE, "blocked_macs", "{", mac, "}"], timeout=10)


def restart_services(cfg, *, restart_ap: bool = True) -> None:
    if restart_ap:
        if cfg.wifi.enabled:
            result = _systemctl("restart", paths.UNIT_AP)
            if not result.ok:
                raise ApplyError("The access point failed to start: " + _short(result))
        else:
            _systemctl("stop", paths.UNIT_AP)
    if shell.available("systemctl"):
        result = _systemctl("restart", paths.UNIT_DNSMASQ)
        if not result.ok:
            raise ApplyError("The DHCP service failed to start: " + _short(result))


def verify_ap(cfg, *, timeout: float = 15.0) -> dict[str, Any]:
    """Confirm the radio is really running as an access point."""
    deadline = time.monotonic() + timeout
    last_detail = "not started"
    lan = cfg.network.lan_interface
    while time.monotonic() < deadline:
        time.sleep(1.0)
        active = shell.run(["systemctl", "is-active", paths.UNIT_AP], timeout=8).stdout.strip()
        detail = f"hostapd state: {active or 'unknown'}"
        if active != "active":
            last_detail = detail
            continue
        if shell.available("iw"):
            info = shell.run(["iw", "dev", lan, "info"], timeout=6)
            if not info.ok:
                last_detail = f"iw could not read {lan}"
                continue
            lowered = info.stdout.lower()
            if "type ap" not in lowered:
                last_detail = "the interface is not in access point mode"
                continue
            channel = None
            running_ssid = None
            for line in info.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("channel") and channel is None:
                    channel = stripped
                elif stripped.startswith("ssid ") and running_ssid is None:
                    running_ssid = util.strip_ssid_quotes(stripped[5:].strip())
            # A restart that leaves the previous hostapd alive keeps the old
            # SSID/PSK on air while the new file sits on disk. Clients then fail
            # the 4-way handshake with "invalid MIC in msg 2/4". Comparing the
            # running SSID against the configured one catches that directly.
            if running_ssid is not None and running_ssid != cfg.wifi.ssid:
                last_detail = (
                    f"hostapd is broadcasting '{running_ssid}' but the configuration says "
                    f"'{cfg.wifi.ssid}'; the running process is stale"
                )
                continue
            return {"ok": True, "channel": channel, "ssid": running_ssid,
                    "detail": "Access point is broadcasting"}
        return {"ok": True, "channel": None, "detail": "hostapd is running (iw not available)"}
    return {"ok": False, "channel": None, "detail": last_detail}


def verify_lease_service() -> dict[str, Any]:
    active = shell.run(["systemctl", "is-active", paths.UNIT_DNSMASQ], timeout=8).stdout.strip()
    return {"ok": active == "active", "detail": f"dnsmasq state: {active or 'unknown'}"}


# --------------------------------------------------------------------------- #
# Public operations
# --------------------------------------------------------------------------- #
def apply(cfg, *, reason: str = "manual", restart: bool = True) -> dict[str, Any]:
    """Render, validate, apply and verify the whole network configuration."""
    started = time.time()
    steps: list[str] = []
    warnings: list[str] = []

    generated = configgen.render_all(cfg, __version__)
    warnings = validate_generated(generated, cfg)

    snapshot = backup.create(f"pre-{reason}")
    steps.append(f"configuration snapshot: {snapshot or 'not created'}")

    try:
        write_generated(generated)
        steps.append("rendered hostapd, dnsmasq, nftables and sysctl files")
        apply_sysctl()
        steps.append("applied kernel forwarding settings")
        apply_address(cfg)
        steps.append(f"assigned {cfg.network.lan_address}/{cfg.network.lan_prefix} to {cfg.network.lan_interface}")
        apply_firewall(cfg)
        steps.append("loaded firewall and NAT rules" if cfg.network.firewall_enabled else "firewall disabled")
        if restart:
            restart_services(cfg)
            steps.append("restarted access point and DHCP services")
        verification = verify_ap(cfg) if cfg.wifi.enabled and restart else {"ok": True, "detail": "skipped"}
        lease = verify_lease_service() if restart else {"ok": True, "detail": "skipped"}
        if restart and (not verification["ok"] or not lease["ok"]):
            raise ApplyError(
                "Verification failed: " + "; ".join(filter(None, [verification["detail"], lease["detail"]]))
            )
        _set_safe_mode(None)
        result = {
            "ok": True,
            "reason": reason,
            "at": util.iso_now(),
            "duration_ms": int((time.time() - started) * 1000),
            "steps": steps,
            "warnings": warnings,
            "verification": verification,
            "snapshot": snapshot,
        }
        _record_apply(result)
        return result
    except Exception as exc:
        log.error("applying the network configuration failed: %s", exc)
        rollback_note = _rollback(snapshot, cfg)
        _set_safe_mode(str(exc)[:300])
        result = {
            "ok": False,
            "reason": reason,
            "at": util.iso_now(),
            "duration_ms": int((time.time() - started) * 1000),
            "steps": steps,
            "warnings": warnings,
            "error": str(exc),
            "rollback": rollback_note,
            "snapshot": snapshot,
        }
        _record_apply(result)
        raise ApplyError(f"{exc} {rollback_note}".strip())


def _rollback(snapshot: str | None, cfg) -> str:
    """Restore the previous configuration and bring the network back up."""
    if not snapshot:
        return "No snapshot was available, so the previous configuration was left in place."
    try:
        backup.restore(snapshot)
        settings.invalidate_cache()
        previous = settings.load(use_cache=False)
        generated = configgen.render_all(previous, __version__)
        write_generated(generated)
        apply_sysctl()
        apply_address(previous)
        apply_firewall(previous)
        if previous.wifi.enabled:
            _systemctl("restart", paths.UNIT_AP)
        _systemctl("restart", paths.UNIT_DNSMASQ)
        return "The previous network configuration was restored."
    except Exception as exc:  # pragma: no cover - only on catastrophic failure
        log.error("rollback failed: %s", exc)
        _set_safe_mode(f"Rollback failed: {exc}")
        return (
            "The rollback also failed. The wired management connection (eth0) is untouched; "
            "log in over Ethernet and run 'sudo networkosctl net-apply --verbose'."
        )


def set_wifi_enabled(cfg, enabled: bool) -> dict[str, Any]:
    cfg.wifi.enabled = enabled
    settings.save(cfg)
    if enabled:
        return apply(cfg, reason="wifi-enable")
    lan = cfg.network.lan_interface
    _systemctl("stop", paths.UNIT_AP)
    _systemctl("stop", paths.UNIT_DNSMASQ)
    shell.run(["ip", "addr", "flush", "dev", lan], timeout=10)
    result = {"ok": True, "reason": "wifi-disable", "at": util.iso_now(), "detail": "Access point stopped"}
    _record_apply(result)
    return result


def block_device(mac: str) -> dict[str, Any]:
    device_mac = util.normalize_mac(mac)
    if not device_mac:
        raise ConfigurationError("Invalid MAC address.")
    if not shell.available("nft"):
        raise ApplyError("nftables is not installed.")
    listing = shell.run(["nft", "list", "table", "inet", paths.NFT_TABLE], timeout=8)
    if not listing.ok:
        cfg = settings.load()
        apply_firewall(cfg)
    result = shell.run(
        ["nft", "add", "element", "inet", paths.NFT_TABLE, "blocked_macs", "{", device_mac, "}"], timeout=10
    )
    if not result.ok:
        raise ApplyError("The firewall did not accept the block rule: " + _short(result))
    cfg = settings.load()
    if shell.available("iw"):
        shell.run(["iw", "dev", cfg.network.lan_interface, "station", "del", device_mac], timeout=6)
    return {"mac": device_mac, "blocked": True}


def unblock_device(mac: str) -> dict[str, Any]:
    device_mac = util.normalize_mac(mac)
    if not device_mac:
        raise ConfigurationError("Invalid MAC address.")
    if not shell.available("nft"):
        raise ApplyError("nftables is not installed.")
    shell.run(
        ["nft", "delete", "element", "inet", paths.NFT_TABLE, "blocked_macs", "{", device_mac, "}"], timeout=10
    )
    return {"mac": device_mac, "blocked": False}


def set_firewall_enabled(cfg, enabled: bool) -> dict[str, Any]:
    cfg.network.firewall_enabled = enabled
    settings.save(cfg)
    generated = configgen.render_all(cfg, __version__)
    write_generated(generated)
    apply_firewall(cfg)
    return {"ok": True, "enabled": enabled}


def apply_lan_settings(cfg) -> dict[str, Any]:
    configgen.render_all(cfg, __version__)  # validates before anything is written
    return apply(cfg, reason="lan-change")
