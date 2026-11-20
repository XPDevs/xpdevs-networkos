"""Access point verification must notice a stale hostapd process."""

from __future__ import annotations

from networkos import netapply, settings, util
from networkos.shell import Result


def _cfg(ssid="XPDevs-NetworkOS-Test"):
    cfg = settings.Config()
    cfg.wifi.ssid = ssid
    cfg.wifi.psk = util.wpa_psk(ssid, "test-passphrase-1")
    cfg.network.lan_interface = "wlan0"
    return cfg


def _iw(ssid):
    return Result(
        ok=True,
        returncode=0,
        stdout=(
            "Interface wlan0\n"
            "\tifindex 3\n"
            "\ttype AP\n"
            "\tssid " + ssid + "\n"
            "\tchannel 6 (2437 MHz), width: 20 MHz\n"
        ),
        stderr="",
        command="iw dev wlan0 info",
        duration_ms=1,
    )


def _active(*_args, **_kwargs):
    return Result(ok=True, returncode=0, stdout="active\n", stderr="", command="systemctl", duration_ms=1)


def test_matching_ssid_is_accepted(monkeypatch):
    monkeypatch.setattr(netapply.shell, "available", lambda name: True)
    monkeypatch.setattr(netapply, "_systemctl", _active)
    monkeypatch.setattr(netapply.shell, "run", lambda argv, **kw: _iw("XPDevs-NetworkOS-Test")
                        if argv[:2] == ["iw", "dev"] else _active())
    result = netapply.verify_ap(_cfg(), timeout=1.5)
    assert result["ok"] is True
    assert result["ssid"] == "XPDevs-NetworkOS-Test"


def test_stale_hostapd_is_reported(monkeypatch):
    """The running process still advertises the previous SSID."""
    monkeypatch.setattr(netapply.shell, "available", lambda name: True)
    monkeypatch.setattr(netapply, "_systemctl", _active)
    monkeypatch.setattr(netapply.shell, "run", lambda argv, **kw: _iw("Old-Network")
                        if argv[:2] == ["iw", "dev"] else _active())
    result = netapply.verify_ap(_cfg(), timeout=1.5)
    assert result["ok"] is False
    assert "stale" in result["detail"]
