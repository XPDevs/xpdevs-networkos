"""Validation helpers are the single gate between user input and the system."""

from __future__ import annotations

import pytest

from networkos import util


def test_wpa_psk_matches_the_ieee_test_vector():
    assert util.wpa_psk("IEEE", "password") == (
        "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e"
    )


def test_ssid_validation():
    assert util.valid_ssid("  Home Network  ") == "Home Network"
    for bad in ("", "   ", "bad\x00name", "x" * 33):
        with pytest.raises(util.ValidationError):
            util.valid_ssid(bad)


def test_passphrase_bounds():
    assert util.valid_passphrase("12345678") == "12345678"
    for bad in ("short", "x" * 64, "caf\u00e9-caf\u00e9"):
        with pytest.raises(util.ValidationError):
            util.valid_passphrase(bad)


def test_channel_depends_on_the_band():
    assert util.valid_channel(6) == 6
    assert util.valid_channel("13") == 13
    assert util.valid_channel(36, hw_mode="a") == 36
    with pytest.raises(util.ValidationError):
        util.valid_channel(0)
    with pytest.raises(util.ValidationError):
        util.valid_channel(36)  # 5 GHz channel on a 2.4 GHz radio
    with pytest.raises(util.ValidationError):
        util.valid_channel(165, hw_mode="a")


def test_lan_plan_accepts_a_normal_subnet():
    util.check_lan_plan("10.42.0.1", 24, "10.42.0.10", "10.42.0.200")


@pytest.mark.parametrize(
    "gateway,prefix,start,end,field",
    [
        ("10.42.0.1", 24, "10.42.0.10", "10.42.1.200", "dhcp_range"),
        ("10.42.0.1", 24, "10.42.0.200", "10.42.0.10", "dhcp_range"),
        ("10.42.0.1", 24, "10.42.0.1", "10.42.0.200", "dhcp_range"),
        ("192.168.5.1", 30, "192.168.5.2", "192.168.5.2", "dhcp_range"),
        ("10.42.0.1", 24, "10.42.0.0", "10.42.0.200", "dhcp_range"),
    ],
)
def test_lan_plan_rejects_broken_plans(gateway, prefix, start, end, field):
    with pytest.raises(util.ValidationError) as excinfo:
        util.check_lan_plan(gateway, prefix, start, end)
    assert excinfo.value.field == field


def test_lease_time_validation():
    assert util.valid_lease_time("12h") == "12h"
    assert util.valid_lease_time("INFINITE") == "infinite"
    for bad in ("1m", "5y", "abc", ""):
        with pytest.raises(util.ValidationError):
            util.valid_lease_time(bad)


def test_mac_normalisation():
    assert util.normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert util.normalize_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"
    assert util.normalize_mac("zz:bb:cc:dd:ee:ff") is None
    assert util.valid_mac("00:11:22:33:44:55")


def test_private_address_guard():
    assert util.is_private_address("192.168.1.5")
    assert util.is_private_address("10.42.0.1")
    assert util.is_private_address("127.0.0.1")
    assert util.is_private_address("169.254.10.1")
    assert not util.is_private_address("8.8.8.8")
    assert not util.is_private_address("203.0.113.9")
    assert not util.is_private_address("not-an-address")


def test_formatting_helpers():
    assert util.format_duration(90) == "1m 30s"
    assert util.format_duration(3600) == "1h"
    assert util.format_duration(86400 * 2) == "2d"
    assert util.human_bytes(1536) == "1.5 KB"
    assert util.percent(1, 3) == 33.3
    assert util.percent(1, 0) == 0.0
