# Installing XPDevs NetworkOS

This document takes a blank microSD card to a working wireless router with
network-wide ad blocking.

## 1. Prepare the SD card

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. Under the
   advanced options set a hostname, enable SSH, and give the Pi a wired DHCP
   lease on your existing network.
2. Insert the card, connect the Ethernet cable to your router/switch, and power
   the Pi with a 5 V/2.5 A supply. A weaker supply is the most common cause of
   the radio dropping clients.
3. Wait for the Pi to appear on your network, then:

```bash
ssh pi@raspberrypi.local
sudo apt-get update && sudo apt-get upgrade -y
```

## 2. Check the prerequisites

```bash
# board and OS
cat /proc/device-tree/model; echo
cat /etc/os-release | head -3
uname -m                      # must be aarch64 (64-bit)

# interfaces
ip -br addr                   # eth0 must have an address, wlan0 must exist
ip route | grep default       # must go through eth0

# radio capability (install iw first if missing)
sudo apt-get install -y iw
iw list | grep -A1 "Supported interface modes" | grep AP

# free space: 1.5 GB is the installer minimum
df -h /
```

If `iw list` does not list `AP` under supported interface modes, the radio cannot
host an access point and the installer stops. That is a hardware limitation, not a
configuration problem.

## 3. Copy the project and run the installer

```bash
# from your computer
scp -r xpdevs-networkos pi@raspberrypi.local:/home/pi/

# on the Pi
cd ~/xpdevs-networkos
sudo bash install.sh
```

### What it asks

| Prompt | Notes |
| --- | --- |
| Wireless network name (SSID) | 1–32 bytes; defaults to `XPDevs NetworkOS` |
| Wi-Fi password | 8–63 printable ASCII characters, typed twice |
| Wireless channel | 1–13; 6 is a reasonable default |
| Country code | Two letters; sets the radio's regulatory domain |
| Administrator account | Created in the browser on the first visit (see step 11) |

The Wi-Fi passphrase is written to a file with mode `0600` in a private temporary
directory, used by the configuration writer and deleted (shredded) when the
installer exits. It is converted to its WPA2 key and the plain text is discarded.

The **administrator password is never entered into the installer**. After
installation the dashboard opens a setup screen where you choose it; the password
goes from the browser straight into an Argon2id hash in the local database and is
never written to a log or a temporary file. Until that account exists, every page
and API of the panel redirects to the setup screen.

### What it does, in order

1. Detects board model, OS, architecture, free space, `eth0`, `wlan0` and AP support.
2. Verifies the uplink has an address, a default route and internet access.
3. Snapshots `/etc/dhcpcd.conf`, `/etc/network/interfaces*`, `/etc/hostapd`,
   `/etc/dnsmasq.conf`, `/etc/nftables.conf`, `/etc/avahi` and `/etc/pihole` into
   `/var/backups/xpdevs-networkos/pre-install-<timestamp>.tar.gz` (mode `0600`).
4. Installs hostapd, dnsmasq, nftables, iw, avahi, Python 3 and friends.
5. Creates the `xpdevs-networkos` system account and the directory layout.
6. Copies the application to `/opt/xpdevs-networkos` and builds its virtualenv.
7. Writes `/etc/xpdevs-networkos/config.toml` and renders the daemon configuration.
8. Stops, disables and masks the distribution `hostapd.service` and
   `dnsmasq.service` so they cannot fight for the radio or port 53.
9. Installs Pi-hole with its official unattended installer, then moves its web
   server to `127.0.0.1:8081`, binds it to loopback, sets DNS to answer on the LAN,
   disables Pi-hole's own DHCP server, sets 7-day retention and issues a random API
   credential kept in `/etc/xpdevs-networkos/secrets/pihole-api.key` (mode `0640`).
10. Installs the systemd units, the sudo policy (validated with `visudo -c`), the
    logrotate policy and the mDNS advertisement.
11. Applies the network configuration (which
    validates with `dnsmasq --test` and `nft -c -f` first), starts the dashboard and
    waits for it to answer on port 80. The administrator account is created on the
    first visit to the dashboard unless a password was supplied up front (see
    *Unattended installation*).
12. Runs the health check and prints the dashboard addresses and Wi-Fi credentials.

`eth0` is never reconfigured. If the wireless step fails, the installer leaves the
wired connection exactly as it found it and the dashboard remains reachable on the
Ethernet address.

## 4. Verify on the device

Run these on the Pi after installation:

```bash
# services
systemctl --no-pager status xpdevs-networkos-net xpdevs-networkos-ap \
  xpdevs-networkos-dnsmasq xpdevs-networkos-web pihole-FTL

# the radio really is an access point
sudo iw dev wlan0 info | grep -E "type|ssid|channel"

# the wireless address and the DHCP server
ip -br addr show wlan0
sudo ss -ulpn | grep ':67'

# the firewall
sudo nft list table inet networkos

# the blocking verdict (resolves an ad domain through Pi-hole)
sudo networkosctl healthcheck --json
```

Then from a phone or laptop:

1. Connect to the SSID and confirm an address in `10.42.0.x` was issued.
2. Open `http://networkos.local` (or `http://10.42.0.1`), create the administrator
   account on the setup screen, and confirm the dashboard opens afterwards.
3. Browse a site that normally serves ads and compare with mobile data.
4. In the dashboard open **Ad blocking** and press **Verify**; the result comes from
   an actual DNS query, not from Pi-hole's own status flag.

## Upgrading an existing installation

Re-run the installer from the new copy of the project:

```bash
cd xpdevs-networkos
sudo bash install.sh
```

What is preserved: `/etc/xpdevs-networkos/config.toml`, the derived WPA2 key, the
administrator account and the SQLite database under `/var/lib/xpdevs-networkos`, the
Pi-hole database, and the log history. What is replaced: the Python package, the systemd
units, the sudoers policy, the launcher and the generated daemon configuration (rendered
again from the stored configuration).

The installer stops the distribution `hostapd` and `dnsmasq` units, masks them, masks
`wpa_supplicant@wlan0` and marks `wlan0` unmanaged in NetworkManager before the
NetworkOS units start. It never re-addresses `eth0`, so the management connection
survives.

After an upgrade that changes the firewall (1.1.0 does), reapply it once so the new rules
are live before the next reboot:

```bash
sudo networkosctl net-apply --reason upgrade
```

### Upgrading from 1.0.0

`network.wan_ssh` was replaced by `network.mgmt_ssh`. The old key is ignored on load and
management SSH stays enabled, so an upgrade cannot lock you out of Ethernet. The
behaviour reverses: SSH is now on the wired uplink and off the wireless AP, which is the
opposite of 1.0.0.

## 5. Rebooting and re-running

Everything is enabled at boot: `xpdevs-networkos-net` reapplies the configuration,
then hostapd, dnsmasq and the dashboard start, and the health timer checks every
minute.

The installer is idempotent. Running it again repairs a broken installation,
reinstalls missing packages and can change the SSID, channel, country or
administrator account. When an existing configuration is found it offers to keep
the current wireless settings.

## Unattended installation

Every prompt has a flag or an environment variable:

```bash
sudo bash install.sh \
  --yes --ssid "Studio Wi-Fi" --channel 11 --country GB --lan-address 10.42.0.1 \
  --skip-pihole
```

| Flag | Effect |
| --- | --- |
| `--yes` | Accept defaults, ask nothing (provide secrets with environment variables) |
| `--ssid`, `--channel`, `--country`, `--lan-address` | Network values |
| `--skip-pihole` | Install without Pi-hole (DNS filtering unavailable) |
| `--skip-packages` | Skip `apt-get` (dependencies already present) |

| Environment variable | Purpose |
| --- | --- |
| `NETWORKOS_SSID`, `NETWORKOS_WIFI_PASSWORD` | Wireless network |
| `NETWORKOS_CHANNEL`, `NETWORKOS_COUNTRY` | Radio settings |
| `NETWORKOS_LAN_ADDRESS`, `NETWORKOS_DHCP_START`, `NETWORKOS_DHCP_END` | Addressing plan |
| `NETWORKOS_ADMIN_USER`, `NETWORKOS_ADMIN_PASSWORD` | Optional: pre-create the administrator account instead of using the setup screen |

```bash
sudo NETWORKOS_SSID="Studio Wi-Fi" NETWORKOS_WIFI_PASSWORD='…' \
     NETWORKOS_ADMIN_PASSWORD='…' NETWORKOS_ADMIN_USER=admin \
     bash install.sh --yes
```

Without `NETWORKOS_ADMIN_PASSWORD` the installation is complete except for the
administrator account, which is created on the first visit to the dashboard.
Prefer interactive mode on a shared machine: environment variables are visible to
other processes through `/proc`.

## Where things live

| Path | Contents |
| --- | --- |
| `/opt/xpdevs-networkos` | application and virtualenv |
| `/etc/xpdevs-networkos/config.toml` | authoritative configuration (`0640`) |
| `/etc/xpdevs-networkos/generated/` | rendered hostapd, dnsmasq, nft and sysctl files |
| `/etc/xpdevs-networkos/secrets/` | Pi-hole API key, session key (`0750`) |
| `/etc/xpdevs-networkos/backups/` | configuration snapshots taken before each apply |
| `/var/lib/xpdevs-networkos/networkos.db` | SQLite database (WAL) |
| `/var/lib/xpdevs-networkos/health.json` | last health report |
| `/var/log/xpdevs-networkos/` | `networkos.log`, `audit.log`, `install.log` |
| `/var/backups/xpdevs-networkos/` | pre-install network snapshots |

## Uninstalling

```bash
sudo bash uninstall.sh                 # stops and removes the product
sudo bash uninstall.sh --purge         # also deletes configuration, database, logs
sudo bash uninstall.sh --purge --purge-pihole
sudo bash uninstall.sh --restore-distro-services   # unmask hostapd/dnsmasq again
```

After removal the distribution's `dhcpcd` (or NetworkManager) takes `wlan0` back, so
the Pi behaves like a stock Raspberry Pi OS machine again. `eth0` is untouched.
