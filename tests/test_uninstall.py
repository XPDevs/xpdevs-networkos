"""The uninstaller must undo everything the installer changed on the host."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "uninstall.sh"
UNITS = (
    "xpdevs-networkos-web.service",
    "xpdevs-networkos-ap.service",
    "xpdevs-networkos-dnsmasq.service",
    "xpdevs-networkos-net.service",
    "xpdevs-networkos-health.timer",
    "xpdevs-networkos-health.service",
    "xpdevs-networkos.target",
)


def _bash() -> str:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not available")
    return bash


@pytest.fixture
def tree(tmp_path):
    """A throw-away host tree plus command shims that record every call."""
    paths = {
        "app": tmp_path / "opt/xpdevs-networkos",
        "etc": tmp_path / "etc/xpdevs-networkos",
        "state": tmp_path / "var/lib/xpdevs-networkos",
        "log": tmp_path / "var/log/xpdevs-networkos",
        "backup": tmp_path / "var/backups/xpdevs-networkos",
        "systemd": tmp_path / "etc/systemd/system",
        "nm": tmp_path / "etc/NetworkManager/conf.d",
        "run": tmp_path / "run",
        "bin": tmp_path / "bin",
    }
    for key in ("app", "etc", "state", "log", "backup", "systemd", "nm", "run", "bin"):
        paths[key].mkdir(parents=True, exist_ok=True)

    (paths["app"] / "networkos").mkdir()
    (paths["app"] / "networkos" / "cli.py").write_text("# product\n")
    (paths["etc"] / "config.toml").write_text('[network]\nlan_interface = "wlan0"\n')
    for unit in UNITS:
        (paths["systemd"] / unit).write_text("[Unit]\n")
    (paths["nm"] / "xpdevs-networkos.conf").write_text("[keyfile]\n")
    (paths["run"] / "xpdevs-hostapd").mkdir()
    (paths["state"] / "networkos.db").write_text("db")
    (paths["log"] / "networkos.log").write_text("log")

    ctl = tmp_path / "usr/local/sbin/networkosctl"
    ctl.parent.mkdir(parents=True, exist_ok=True)
    ctl.write_text("#!/bin/sh\n")
    sudoers = tmp_path / "etc/sudoers.d/xpdevs-networkos"
    sudoers.parent.mkdir(parents=True, exist_ok=True)
    sudoers.write_text("policy\n")
    log = tmp_path / "calls.log"

    for name in ("systemctl", "nft", "ip", "sysctl", "pihole"):
        shim = paths["bin"] / name
        shim.write_text(f'#!/bin/sh\nprintf "%s %s\\n" "{name}" "$*" >> "{log}"\nexit 0\n')
        shim.chmod(0o755)

    env = {
        "PATH": f"{paths['bin']}:{os.environ.get('PATH', '')}",
        "NETWORKOS_APP_DIR": str(paths["app"]),
        "NETWORKOS_ETC_DIR": str(paths["etc"]),
        "NETWORKOS_STATE_DIR": str(paths["state"]),
        "NETWORKOS_LOG_DIR": str(paths["log"]),
        "NETWORKOS_BACKUP_DIR": str(paths["backup"]),
        "NETWORKOS_SYSTEMD_DIR": str(paths["systemd"]),
        "NETWORKOS_NM_DIR": str(paths["nm"]),
        "NETWORKOS_RUN_DIR": str(paths["run"]),
        "NETWORKOS_CTL": str(ctl),
        "NETWORKOS_SUDOERS_FILE": str(sudoers),
        "NETWORKOS_LOGROTATE_FILE": str(tmp_path / "etc/logrotate.d/xpdevs-networkos"),
        "NETWORKOS_AVAHI_FILE": str(tmp_path / "etc/avahi/services/xpdevs-networkos.service"),
    }
    return {"paths": paths, "env": env, "log": log, "ctl": ctl, "sudoers": sudoers}


def _run(tree, *args):
    return subprocess.run(
        [_bash(), str(SCRIPT), "--yes", *args],
        capture_output=True,
        text=True,
        env={**os.environ, **tree["env"]},
        timeout=120,
    )


def test_uninstaller_is_valid_shell():
    result = subprocess.run([_bash(), "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unknown_option_is_rejected(tree):
    result = _run(tree, "--nonsense")
    assert result.returncode == 2
    assert "Unknown option" in result.stderr


def test_help_exits_cleanly(tree):
    result = subprocess.run([_bash(), str(SCRIPT), "--help"], capture_output=True, text=True,
                            env={**os.environ, **tree["env"]})
    assert result.returncode == 0
    assert "uninstaller" in result.stdout


def test_dry_run_changes_nothing(tree):
    result = _run(tree, "--purge", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert tree["paths"]["app"].exists()
    assert (tree["paths"]["etc"] / "config.toml").exists()
    assert (tree["paths"]["systemd"] / UNITS[0]).exists()
    assert not tree["log"].exists() or "rm" not in tree["log"].read_text()


def test_purge_removes_the_product(tree):
    result = _run(tree, "--purge")
    assert result.returncode == 0, result.stderr
    assert not tree["paths"]["app"].exists()
    assert not tree["paths"]["etc"].exists()
    assert not tree["paths"]["state"].exists()
    assert not tree["ctl"].exists()
    assert not tree["sudoers"].exists()
    for unit in UNITS:
        assert not (tree["paths"]["systemd"] / unit).exists()


def test_wireless_is_handed_back_to_the_host(tree):
    """Masking left behind by the installer would leave the Pi with no Wi-Fi."""
    _run(tree, "--purge")
    calls = tree["log"].read_text()
    assert "systemctl unmask wpa_supplicant@wlan0.service" in calls
    assert "systemctl unmask hostapd.service" in calls
    assert "systemctl unmask dnsmasq.service" in calls
    assert not (tree["paths"]["nm"] / "xpdevs-networkos.conf").exists()
    assert "ip addr flush dev wlan0" in calls


def test_firewall_table_and_runtime_files_are_removed(tree):
    _run(tree, "--purge")
    calls = tree["log"].read_text()
    assert "nft delete table inet networkos" in calls
    assert not (tree["paths"]["run"] / "xpdevs-hostapd").exists()


def test_the_wired_uplink_is_never_touched(tree):
    _run(tree, "--purge")
    calls = tree["log"].read_text()
    assert "eth0" not in calls, "the management interface must survive an uninstall"


def test_without_purge_the_configuration_is_kept(tree):
    _run(tree)
    assert not tree["paths"]["app"].exists()
    assert (tree["paths"]["etc"] / "config.toml").exists()
