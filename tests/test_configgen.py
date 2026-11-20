"""Generated daemon configuration must be complete and safe by construction."""

from __future__ import annotations

from networkos import configgen, settings, util


def _config() -> settings.Config:
    cfg = settings.Config()
    cfg.wifi.ssid = "XPDevs Testbed"
    cfg.wifi.psk = util.wpa_psk(cfg.wifi.ssid, "test-passphrase-1")
    cfg.wifi.channel = 11
    cfg.wifi.country = "GB"
    cfg.wifi.hidden = True
    cfg.network.lan_address = "192.168.50.1"
    cfg.network.lan_prefix = 24
    cfg.network.dhcp_start = "192.168.50.20"
    cfg.network.dhcp_end = "192.168.50.120"
    return cfg


def test_hostapd_configuration():
    text = configgen.render_hostapd(_config(), "1.0.0")
    assert "interface=wlan0" in text
    assert "driver=nl80211" in text
    assert 'ssid="XPDevs Testbed"' in text
    assert "hw_mode=g" in text
    assert "channel=11" in text
    assert "wpa=2" in text
    assert "wpa_key_mgmt=WPA-PSK" in text
    assert "rsn_pairwise=CCMP" in text
    assert "wpa_psk=" in text
    assert "ignore_broadcast_ssid=1" in text
    assert "test-passphrase-1" not in text  # only the derived key is written
    assert "country_code=GB" in text


def test_dnsmasq_is_dhcp_only_and_points_at_the_gateway():
    text = configgen.render_dnsmasq(_config(), "1.0.0")
    assert "port=0" in text
    assert "dhcp-range=192.168.50.20,192.168.50.120,255.255.255.0,12h" in text
    assert "dhcp-option=option:router,192.168.50.1" in text
    assert "dhcp-option=option:dns-server,192.168.50.1" in text
    assert "except-interface=eth0" in text
    assert "dhcp-authoritative" in text


def test_nftables_ruleset_nats_and_drops_by_default():
    text = configgen.render_nftables(_config(), "1.0.0")
    assert "table inet networkos" in text
    assert "type nat hook postrouting" in text
    assert "masquerade" in text
    assert "iifname wlan0 oifname eth0 counter accept" in text
    assert "policy drop" in text
    assert "ct state established,related accept" in text
    assert "udp dport 67" in text
    assert "udp dport 53" in text
    assert "tcp dport { 80, 443 }" in text


def test_nftables_opens_ssh_on_the_wired_uplink_only():
    """Management SSH belongs on eth0; the wireless AP must never expose it."""
    text = configgen.render_nftables(_config(), "1.0.0")
    assert "iifname eth0 tcp dport 22 accept" in text
    assert "iifname wlan0 tcp dport 22 accept" not in text
    assert "tcp dport 22" in text.split("policy drop")[1]


def test_nftables_can_close_ssh_entirely():
    cfg = _config()
    cfg.network.mgmt_ssh = False
    text = configgen.render_nftables(cfg, "1.0.0")
    assert "tcp dport 22" not in text


def test_nftables_still_allows_dhcp_and_dns_to_wireless_clients():
    text = configgen.render_nftables(_config(), "1.0.0")
    assert "iifname wlan0 udp dport 67 accept" in text
    assert "iifname wlan0 udp dport 53 accept" in text
    assert "iifname wlan0 tcp dport 53 accept" in text


def test_nftables_ruleset_is_idempotent_to_apply():
    """Reapplying must not accumulate rules: the table is replaced wholesale."""
    cfg = _config()
    first = configgen.render_nftables(cfg, "1.0.0")
    second = configgen.render_nftables(cfg, "1.0.0")
    assert first == second
    assert first.count("table inet networkos") == 1


def test_nftables_keeps_the_uplink_dhcp_client_working():
    text = configgen.render_nftables(_config(), "1.0.0")
    assert "iifname eth0 udp dport 68 accept" in text


def test_sysctl_enables_routing_and_blocks_redirects():
    text = configgen.render_sysctl(_config(), "1.0.0")
    assert "net.ipv4.ip_forward = 1" in text
    assert "net.ipv4.conf.all.send_redirects = 0" in text
    assert "net.ipv4.conf.wlan0.send_redirects = 0" in text


def test_render_all_covers_every_daemon():
    generated = configgen.render_all(_config(), "1.0.0")
    assert set(generated) >= {"hostapd", "dnsmasq", "nft", "sysctl"}
    for body in generated.values():
        assert "XPDevs NetworkOS 1.0.0" in body


def test_hostapd_disables_pmf_and_pins_ccmp():
    text = configgen.render_hostapd(_config(), "1.0.0")
    assert "ieee80211w=0" in text
    assert "rsn_pairwise=CCMP" in text
    assert "wpa_pairwise=CCMP" in text
    assert "TKIP" not in text
