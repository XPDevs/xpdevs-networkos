"""Packaging regressions: the shipped units and launcher must stay correct."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def _unit(name: str) -> str:
    return (PACKAGING / "systemd" / name).read_text(encoding="utf-8")


def _setting(text: str, key: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return None


def test_launcher_changes_into_the_application_directory():
    """python -m networkos.cli only resolves from /opt/xpdevs-networkos."""
    script = (PACKAGING / "networkosctl").read_text(encoding="utf-8")
    assert 'APP_DIR="/opt/xpdevs-networkos"' in script
    cd_index = script.index('cd "${APP_DIR}"')
    exec_index = script.index("exec ")
    assert cd_index < exec_index, "the launcher must cd before exec"
    assert "venv/bin/python" in script
    assert "-m networkos.cli" in script


def test_launcher_is_valid_posix_shell():
    shell = shutil.which("sh")
    if not shell:
        pytest.skip("no POSIX shell available")
    result = subprocess.run([shell, "-n", str(PACKAGING / "networkosctl")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_access_point_unit_supervises_the_foreground_process():
    unit = _unit("xpdevs-networkos-ap.service")
    assert _setting(unit, "Type") == "simple"
    assert _setting(unit, "PIDFile") is None
    exec_start = _setting(unit, "ExecStart")
    assert exec_start and "-P " not in exec_start, "hostapd must not write a PID file"
    assert _setting(unit, "KillMode") == "control-group"


def test_dhcp_unit_can_drop_privileges():
    unit = _unit("xpdevs-networkos-dnsmasq.service")
    caps = (_setting(unit, "CapabilityBoundingSet") or "").split()
    for required in ("CAP_SETUID", "CAP_SETGID", "CAP_CHOWN", "CAP_NET_BIND_SERVICE"):
        assert required in caps, f"{required} is required for dnsmasq to drop privileges"
    assert _setting(unit, "Type") == "simple"


def test_dashboard_unit_can_bind_port_80_without_root():
    unit = _unit("xpdevs-networkos-web.service")
    assert "CAP_NET_BIND_SERVICE" in (_setting(unit, "AmbientCapabilities") or "")
    assert "CAP_NET_BIND_SERVICE" in (_setting(unit, "CapabilityBoundingSet") or "")
    assert _setting(unit, "User") == "xpdevs-networkos"
    # sudo needs setuid binaries to stay usable.
    assert _setting(unit, "NoNewPrivileges") == "no"


def test_boot_unit_runs_after_the_system_firewall():
    unit = _unit("xpdevs-networkos-net.service")
    after = (_setting(unit, "After") or "").split()
    assert "nftables.service" in after, "our ruleset must load after nftables.service flushes"


def test_every_shipped_unit_parses():
    units = sorted((PACKAGING / "systemd").glob("*.service")) + sorted((PACKAGING / "systemd").glob("*.timer"))
    assert units, "no units found"
    for unit in units:
        text = unit.read_text(encoding="utf-8")
        assert "[Unit]" in text or "[Timer]" in text, unit.name
        assert "ExecStart=" in text or "[Timer]" in text, unit.name
