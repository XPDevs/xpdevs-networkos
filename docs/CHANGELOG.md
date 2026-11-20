# Changelog

All notable changes to XPDevs NetworkOS. This project follows semantic versioning.

## 1.1.0 — repair release

Fixes for faults reproduced on a Raspberry Pi 3 Model B running Debian 13 (trixie).

### Packaging

* `networkosctl` now changes into `/opt/xpdevs-networkos` before running
  `venv/bin/python -m networkos.cli`. Without it every privileged action failed with
  `ModuleNotFoundError: No module named 'networkos'` whenever the caller's working
  directory was not the application directory, which is the case for systemd (`/`) and
  for `sudo` (the caller's directory).
* The access point unit is `Type=simple` with no `PIDFile`. hostapd never forks, so the
  old `Type=forking` left systemd waiting in `activating`, and `systemctl restart` killed
  nothing: the previous hostapd kept the old SSID and PSK on air while the new
  configuration sat on disk.
* dnsmasq gains `CAP_SETUID`, `CAP_SETGID` and `CAP_CHOWN` so it can drop to the
  `dnsmasq` user and the `dip` group after binding UDP 67.
* The dashboard gains `AmbientCapabilities=CAP_NET_BIND_SERVICE`, so the unprivileged
  service user can listen on port 80 without running as root.

### Firewall

* SSH is now allowed on the wired uplink only. The previous renderer opened port 22 on
  the wireless AP and emitted no rule for `eth0`, so SSH over Ethernet timed out against
  the DROP policy while every Wi-Fi client could reach sshd.
* The boot unit is ordered after `nftables.service`, so a distro ruleset flush can no
  longer wipe the NetworkOS table during startup.
* Reapplying remains idempotent: the table is deleted and recreated, never appended to.

### Wi-Fi

* The installer masks `wpa_supplicant@wlan0` and marks `wlan0` unmanaged in
  NetworkManager, so a second supplicant cannot compete with hostapd for the radio.
* `netapply` compares the SSID the running hostapd advertises with the configured one and
  reports a stale process instead of silently succeeding.

### Application

* `shell.run` accepts the product helper path (`/usr/local/sbin/networkosctl`). The
  allowlist previously rejected it whenever the caller was already root.
* `systemstate.snapshot` degrades to the cached snapshot when the helper fails, instead
  of raising `CommandError` and turning the overview page into a 500.

### Settings

* `network.wan_ssh` is replaced by `network.mgmt_ssh` (default `true`). Old configuration
  files keep working: the obsolete key is ignored on load and management access stays
  enabled.

## 1.0.0 — first release

### Product

* First-run setup screen: on the first visit the dashboard asks for the administrator
  username and password, stores it as an Argon2id hash and signs in. Until that
  account exists no page or API is reachable; the screen closes permanently once an
  account exists and is rate limited and audited.
* One-command installation (`sudo bash install.sh`) for Raspberry Pi OS Lite 64-bit on
  a Raspberry Pi 3 Model B, including board/OS/architecture/interface/AP-mode
  detection, an uplink and internet prerequisite check, a backup of the existing
  network configuration, and a written install log.
* Uninstaller (`uninstall.sh`) with `--purge`, `--purge-pihole` and
  `--restore-distro-services`, which never touches the wired uplink.

### Networking

* WPA2-AES access point via hostapd: configurable SSID, passphrase, channel, country,
  hidden SSID, client isolation, maximum clients, HT rates.
* dnsmasq as a DHCP-only server (`port=0`), configurable subnet, gateway, range and
  lease time, with leases surfaced in the dashboard.
* nftables NAT and a default-drop input policy, DNS interception for wireless clients,
  per-device blocking by MAC, optional SSH on the wired side.
* Validation with the real parsers (`dnsmasq --test`, `nft -c -f`) before any change,
  snapshot and automatic rollback with a safe-mode marker if verification fails.

### Ad blocking

* Automatic unattended Pi-hole installation with pre-seeded answers, its web server
  moved to `127.0.0.1:8081` behind a loopback ACL, its DHCP server disabled, DNS left
  answering on the LAN, and a random API credential stored root-only.
* Dashboard control of blocking, statistics, top blocked/permitted domains, recent
  queries, blocklists, gravity updates and exact allow/deny entries.
* Blocking is reported as verified only after a real DNS query returns a null or
  NXDOMAIN answer.

### Dashboard

* Overview, Wireless, Devices, Ad blocking, Network, Security, System and Settings
  pages, all server-rendered with progressive enhancement.
* Design system with light/dark/system themes, Inter (self-hosted), SVG icon sprite,
  responsive tables that stack on phones, and a strict CSP with no inline scripts or
  styles.
* Every value comes from the running system: `ip`, `iw`, `nft`, `/proc`, `/sys`,
  `systemctl`, the dnsmasq lease file and the Pi-hole API.

### Security

* Argon2id passwords (scrypt fallback), server-side sessions with hashed tokens,
  per-session CSRF tokens, double-submit CSRF on the login form, same-origin checks,
  login throttling with exponential lockout, LAN-only access guard with no
  `X-Forwarded-For` spoofing, security headers and audit logging with secret
  redaction.
* All privileged work goes through `networkosctl` behind a single sudoers entry; every
  argument is validated and no shell is ever executed.

### Operations

* systemd units for the network apply, access point, DHCP, dashboard and health timer,
  with restart policies and ordering that survives reboots.
* Health checks (twelve of them) with throttled self-repair, a JSON report on disk and
  the same report in the dashboard.
* Configuration snapshots before every apply plus manual backups and restores.
* Log rotation, SQLite WAL with retention pruning, `MemoryMax=256M` for the dashboard
  service.

### Known limitations

* The Raspberry Pi 3 radio is 2.4 GHz only; 5 GHz channels require a Pi 4/5 or a
  supported USB adapter and `hw_mode = "a"`.
* WPA2-PSK is the supported security mode; WPA3-SAE is not enabled.
* One administrator account, no multi-factor authentication.
* HTTP only by default; use a TLS terminator in front if you expose the dashboard
  beyond the local network.
