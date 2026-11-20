# XPDevs NetworkOS

**Version 1.0.0** · XPDevs · <https://xpdevs.github.io/>

XPDevs NetworkOS turns a Raspberry Pi 3 Model B into a complete wireless router: it
broadcasts a WPA2 access point on the built-in radio, hands out addresses over DHCP,
routes and NATs traffic to the wired uplink, blocks advertising and tracking domains
network-wide with Pi-hole, and presents all of it through a single responsive
administration dashboard at `http://networkos.local`.

One command installs it:

```bash
sudo bash install.sh
```

The first time you open the dashboard it asks you to create the administrator
account. That password — stored on the Pi as a one-way Argon2id hash — is required
for every page and every setting; until it exists, nothing else in the panel is
reachable.

---

## What it does

| Area | Capability |
| --- | --- |
| Wireless | WPA2-AES access point via hostapd, configurable SSID, passphrase, channel, hidden SSID, client isolation, 1–13 (2.4 GHz) |
| DHCP | dnsmasq in DHCP-only mode, configurable range, gateway and lease duration, per-device blocking |
| Routing | nftables NAT with a default-drop input policy, DNS interception so clients cannot bypass Pi-hole |
| Ad blocking | Pi-hole installed and configured automatically; blocking switch, statistics, top blocked and top permitted domains, blocklists, custom exact allow/deny entries |
| Dashboard | Overview, Wireless, Devices, Ad blocking, Network, Security, System and Settings, light/dark/system themes, mobile-first layout |
| Security | First-run setup chooses the administrator password, Argon2id hashes, server-side sessions, CSRF double-submit and per-session tokens, login throttling, CSP and strict headers, LAN-only access guard |
| Operations | systemd units with restart policies, health timer with safe remediation, configuration validation before every apply, automatic rollback and safe mode |
| Data | One SQLite database (WAL), retention pruning, no telemetry, no cloud services, no Docker |

Everything the dashboard shows is read from the running system: `ip`, `iw`,
`nft`, `/proc`, `/sys`, `systemctl`, the dnsmasq lease file and the Pi-hole API.
There are no simulated values and no placeholder controls.

## Hardware

* Raspberry Pi 3 Model B (1 GB) — primary target.
* Raspberry Pi OS Lite 64-bit (bookworm or trixie), fresh or existing.
* Wired Ethernet for the uplink (`eth0`) and the built-in 2.4 GHz radio (`wlan0`).
* 8 GB microSD minimum, 32 GB recommended, and a 5 V/2.5 A supply.

The Pi 3 radio is **2.4 GHz only**. 5 GHz channels are rejected by the API and the
installer; see [docs/HARDWARE.md](docs/HARDWARE.md) for the full matrix and the
alternatives (Pi 4/5, USB adapters).

## Installation

```bash
# copy the project to the Pi (or clone it), then
cd xpdevs-networkos
sudo bash install.sh
```

The installer detects the board, the OS, the architecture, the interfaces and
access-point support, verifies the wired uplink and internet access, backs up the
current network configuration, installs the packages and Pi-hole, writes the
configuration, installs the services, creates the administrator account, applies
the network, starts the dashboard and runs a connectivity check. It asks for the
Wi-Fi name, the Wi-Fi password and the administrator password, and it never writes
those secrets to a log.

Default addresses:

| | |
| --- | --- |
| Dashboard | `http://networkos.local` |
| From Wi-Fi | `http://10.42.0.1` |
| From Ethernet | `http://<ethernet address>` |
| Wi-Fi network | `XPDevs NetworkOS` (SSID chosen during installation) |
| DHCP range | `10.42.0.10 – 10.42.0.200`, 12-hour leases |

Full instructions, unattended options and the post-install checklist:
[docs/INSTALL.md](docs/INSTALL.md).

## Project layout

```
install.sh                 one-command installer (idempotent, with rollback)
uninstall.sh               removes the product, keeps data unless --purge
VERSION                    product version
requirements.txt           runtime dependencies (Flask, waitress, argon2-cffi)

networkos/                 backend package
  cli.py                   privileged entry point (networkosctl)
  settings.py              typed configuration, atomic writes
  configgen.py             hostapd / dnsmasq / nftables / sysctl renderers
  netapply.py              validate -> snapshot -> apply -> verify -> rollback
  security.py              Argon2id, sessions, CSRF, throttling
  shell.py                 allowlisted subprocess execution, no shells
  backup.py  health.py  collect.py  db.py  util.py  paths.py  logging_setup.py
  services/                system, network, wireless, devices, Pi-hole, events
  web/                     Flask application factory, views, JSON API, auth
    templates/             dashboard pages (no inline scripts or styles)
    static/                design system, page controllers, Inter font

packaging/
  systemd/                 five units and the product target
  sudoers/                 the single allowlisted privileged command
  networkosctl             root-owned wrapper installed to /usr/local/sbin
  logrotate/               log rotation policy
  avahi/                   mDNS advertisement for networkos.local

docs/                      installation, troubleshooting, hardware, architecture,
                           security, changelog
tests/                     pytest suite (validators, config, auth, API guards)
```

## Configuration

The authoritative configuration lives in `/etc/xpdevs-networkos/config.toml`
(mode `0640`, root:`xpdevs-networkos`). Daemon configuration is rendered from it
into `/etc/xpdevs-networkos/generated/` on every apply; those files are marked as
generated and are overwritten. Change settings in the dashboard or with
`sudo networkosctl`, never by editing the rendered files.

The Wi-Fi passphrase is never stored. The configuration keeps only the derived
WPA2 pre-shared key, so changing the SSID requires entering the passphrase again —
the dashboard and the API enforce that rule.

## Privileged operations

The dashboard runs as the unprivileged `xpdevs-networkos` account. It can execute
exactly one program through `sudo`: `/usr/local/sbin/networkosctl`, a wrapper around
`python -m networkos.cli`. Every subcommand validates its arguments, and no shell is
ever involved, so a value typed into the dashboard cannot become a command.

```
state  net-apply  wifi-apply  wifi-power  ap-restart  device-block  device-unblock
lan-apply  firewall  nat  pihole-configure  pihole-blocking  pihole-gravity
pihole-retention  service-restart  system-reboot  system-shutdown  system-update
admin-create  admin-passwd  backup-create  backup-list  backup-restore  healthcheck
scan  mdns-configure
```

## Verification status

Developed and exercised on Debian/Ubuntu x86_64 in a sandbox:

* 63 automated tests pass (`pytest -q`), covering input validation, generated
  daemon configuration, password hashing, session and CSRF handling, login
  throttling, the command allowlist, every dashboard page, the JSON API guards,
  the confirmation phrases, the LAN-only access rule and the first-run setup flow.
* The dashboard was served by waitress and every page and API endpoint was
  requested over HTTP, including a live first-run setup: redirect to `/setup`
  while no account exists, account creation, the dashboard opening with the new
  session, and `/setup` closing afterwards. The installer and uninstaller pass
  `bash -n`.

**Not tested here, because the hardware is absent:** access-point creation on the
Pi 3 radio, phone/laptop association, DHCP allocation to real clients, NAT
throughput, Pi-hole installation and blocking verification, reboot persistence on
the Pi, and the installer's package and service steps. Those steps are documented
in [docs/INSTALL.md](docs/INSTALL.md) with the exact checks to run on the device.
No result in this repository claims a hardware test that was not performed.

## Documentation

* [docs/INSTALL.md](docs/INSTALL.md) — installation, unattended use, verification
* [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — symptom-driven recovery
* [docs/HARDWARE.md](docs/HARDWARE.md) — supported boards, radio limits, storage
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, apply pipeline, data flow
* [docs/SECURITY.md](docs/SECURITY.md) — threat model, controls, hardening notes
* [docs/CHANGELOG.md](docs/CHANGELOG.md) — release history

## Uninstalling

```bash
sudo bash uninstall.sh              # remove the product, keep data and Pi-hole
sudo bash uninstall.sh --purge      # also remove configuration, database and logs
```

The uninstaller never touches `eth0`, so the wired management connection survives.

## Licence and support

XPDevs NetworkOS is provided by XPDevs — <https://xpdevs.github.io/>.
Pi-hole is a separate project by the Pi-hole team and is installed from its
official installer; it keeps its own licence and update path.
