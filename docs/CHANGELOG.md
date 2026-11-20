# Changelog

All notable changes to XPDevs NetworkOS. This project follows semantic versioning.

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
