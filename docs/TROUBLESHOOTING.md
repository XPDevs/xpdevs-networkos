# Troubleshooting XPDevs NetworkOS

Work through the section that matches the symptom. Every command is safe to run;
none of them reconfigures `eth0` unless stated.

## First: collect the facts

```bash
sudo networkosctl healthcheck --json | python3 -m json.tool
sudo networkosctl net-apply --verbose          # re-render, validate and apply
systemctl --no-pager status xpdevs-networkos-{net,ap,dnsmasq,web} pihole-FTL
sudo journalctl -u xpdevs-networkos-ap -n 50 --no-pager
sudo journalctl -u xpdevs-networkos-dnsmasq -n 50 --no-pager
tail -n 50 /var/log/xpdevs-networkos/networkos.log
```

The health report lists every check with an id, a status (`ok`, `warn`, `fail`,
`unknown`) and a one-line detail:

| Check | Meaning when it fails |
| --- | --- |
| `wan_link` | `eth0` has no carrier — cable, switch port or the Pi's Ethernet jack |
| `wan_address` | no DHCP lease from the upstream router |
| `default_route` | no gateway, so nothing can be routed upstream |
| `internet` | no answer to the TCP probe — upstream problem, not NetworkOS |
| `ap_service` | hostapd is not running |
| `ap_clients` | informational: number of associated stations |
| `lan_address` | `10.42.0.1` (or your chosen address) is missing from `wlan0` |
| `dhcp` | dnsmasq is down or UDP 67 is not bound |
| `firewall` | the nftables table is missing or IPv4 forwarding is off |
| `dns` | Pi-hole's FTL is down or nothing listens on port 53 |
| `storage` | the microSD card is filling up |
| `temperature` | the CPU is hot (throttling above 80 °C) |

## I forgot the administrator password

The password is stored only as a one-way hash, so it cannot be recovered — it is
reset from the Pi with root access:

```bash
sudo networkosctl admin-create --username admin --password-stdin
# type the new password, then Ctrl-D
```

Every existing session is revoked when the password changes, so sign in again. If you
still know the password and just want to change it, use **Security → Change
password** in the dashboard instead.

## The setup screen will not go away

The setup screen disappears the moment an account exists. If it keeps coming back,
the database is not being written: check `ls -l /var/lib/xpdevs-networkos/` (the
directory must be writable by `xpdevs-networkos`) and
`journalctl -u xpdevs-networkos-web -n 50`. Running
`sudo bash install.sh` again repairs ownership and permissions.

## The dashboard does not load

**`networkos.local` does not resolve.** mDNS needs avahi on both ends.
`systemctl status avahi-daemon`, and use the numeric address instead:
`http://10.42.0.1` from Wi-Fi or `http://<ethernet-address>` from the wired LAN.
Windows may need Bonjour for `.local` names; Android browsers often do not resolve
mDNS at all — use the IP address there.

**"This dashboard is only reachable from your own network".** The request arrived
from an address outside `10/8`, `172.16/12`, `192.168/16`, `127/8` and
`169.254/16`. You are either on the wrong network, or behind a NAT that rewrote the
source to a public address. Connect through the Pi's own Wi-Fi or wired LAN. If you
deliberately front the dashboard with a reverse proxy, enable **Allow requests from public addresses** in
**Settings** — that is the only supported
way to relax the guard.

**Connection refused on port 80.** `systemctl status xpdevs-networkos-web` and
`journalctl -u xpdevs-networkos-web -n 50`. Port 80 requires the service to start as
root-capable; check that nothing else (nginx, lighttpd, Apache) is bound:

```bash
sudo ss -ltnp | grep ':80'
sudo systemctl disable --now lighttpd nginx apache2   # only if you do not need them
sudo systemctl restart xpdevs-networkos-web
```

## The Wi-Fi network is missing

```bash
sudo iw dev wlan0 info            # type must be AP
sudo rfkill list                  # wlan must not be blocked
sudo iw reg get                   # country must be set
sudo hostapd -dd /etc/xpdevs-networkos/generated/hostapd.conf   # foreground, verbose
```

* `type managed` — hostapd is not running: `sudo systemctl restart xpdevs-networkos-ap`.
* `wlan0` blocked — `sudo rfkill unblock wlan`.
* hostapd reports `Could not set channel` — the channel is not permitted in the
  configured country. Set the correct two-letter country in **Wireless**.
* hostapd reports `nl80211: Driver does not support authentication/association or
  connect commands` — something else (wpa_supplicant, NetworkManager) still owns the
  radio: `sudo systemctl stop wpa_supplicant NetworkManager`.
* The SSID is hidden (that is a setting, not a fault): **Wireless → Hidden network**.

## The Wi-Fi password is rejected (invalid MIC / possible PSK mismatch)

Symptoms in `journalctl -u xpdevs-networkos-ap -f`:

```
WPA: invalid MIC in msg 2/4 of 4-Way Handshake
AP-STA-POSSIBLE-PSK-MISMATCH
```

The AP and the client derived different keys. Work through these in order.

1. **Is the running hostapd the one on disk?** A restart that left the old process alive
   keeps the previous SSID/PSK on air. Check what is actually broadcasting:

   ```bash
   iw dev wlan0 info | grep ssid
   grep -E '^(ssid|wpa_psk)=' /etc/xpdevs-networkos/generated/hostapd.conf
   ```

   If they disagree, the process is stale. Confirm only one hostapd is running
   (`pgrep -a hostapd`) and restart the unit. Version 1.1.0 fixes the unit that caused
   this; on an older install the process type was wrong.

2. **Is a second supplicant holding the radio?** `wpa_supplicant@wlan0` or NetworkManager
   must not manage `wlan0`:

   ```bash
   systemctl status wpa_supplicant@wlan0 2>/dev/null
   nmcli device status | grep wlan0        # must read "unmanaged"
   ```

   The 1.1.0 installer masks the supplicant and writes
   `/etc/NetworkManager/conf.d/xpdevs-networkos.conf`.

3. **Was the key derived from the SSID being advertised?** The PSK is
   `PBKDF2-HMAC-SHA1(passphrase, ssid, 4096, 32)`, so the network name is part of the key.
   Changing the SSID without re-entering the password is refused on purpose. Verify the
   derivation against a known vector:

   ```bash
   python3 - <<'EOF'
   import hashlib
   print(hashlib.pbkdf2_hmac("sha1", b"password", b"IEEE", 4096, 32).hex())
   # must print f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e
   EOF
   ```

4. **Re-enter the password** through **Wireless** or:

   ```bash
   printf '%s' 'new-passphrase' | sudo networkosctl wifi-apply --ssid 'XPDevs-NetworkOS-Test' --password-stdin
   ```

   That regenerates the config atomically and restarts the AP.

5. **Forget the network on the client** before retrying. Android and iOS cache the PSK
   for a known SSID and will keep replaying the old one.

The AP uses WPA2-Personal with AES/CCMP (`wpa=2`, `rsn_pairwise=CCMP`). TKIP is not
offered, so a client that only supports WPA1 cannot join; that is deliberate.

## SSH over Ethernet stopped working

The input chain drops by default, so SSH needs an explicit rule. Version 1.1.0 allows
port 22 on the wired uplink only (`mgmt_ssh`, default on) and never on the AP interface.

```bash
sudo nft list chain inet networkos input | grep 'dport 22'
sudo networkosctl net-apply --reason manual     # reapply from the stored configuration
```

If the rule is missing, `mgmt_ssh` is false. Turn it back on in the configuration and
reapply:

```bash
sudo sed -i 's/^mgmt_ssh = false/mgmt_ssh = true/' /etc/xpdevs-networkos/config.toml
sudo networkosctl net-apply --reason manual
```

The ruleset is regenerated at every boot by `xpdevs-networkos-net.service`, which runs
after `nftables.service`. A hand-added rule (`nft add rule ...`) is not persisted; put the
change in the configuration instead.

## The device list is empty or reports a database error

```bash
sudo ls -l /var/lib/xpdevs-networkos/
sudo -u xpdevs-networkos sqlite3 /var/lib/xpdevs-networkos/networkos.db '.tables'
```

* `unable to open database file` — the directory or file is missing, or owned by root with
  mode 0600. `sudo install -d -o xpdevs-networkos -g xpdevs-networkos /var/lib/xpdevs-networkos`
  and `sudo chown xpdevs-networkos: /var/lib/xpdevs-networkos/networkos.db*`. Version 1.1.1
  creates the schema on first use and fixes ownership itself, so an upgrade repairs this.
* `no such table: devices` — the schema was never created. Restart the dashboard, which now
  initialises it: `sudo systemctl restart xpdevs-networkos-web`.

## The dashboard says Pi-hole timed out

```bash
sudo pihole-FTL --config webserver.port     # must be 8081o
sudo ss -lntp | grep 8081                   # must be listening on 127.0.0.1
sudo systemctl restart pihole-FTL
```

* Empty or `80o` — FTL is on port 80, which NetworkOS owns. Re-run
  `sudo networkosctl pihole-configure --password-stdin < /etc/xpdevs-networkos/secrets/pihole-api.key`.
* Listening on a different port — set `pihole.api_base` in the configuration to match.

## Clients connect but get no address

```bash
sudo ss -ulpn | grep ':67'
sudo dnsmasq --test --conf-file=/etc/xpdevs-networkos/generated/dnsmasq.conf
cat /var/lib/xpdevs-networkos/dnsmasq.leases
sudo journalctl -u xpdevs-networkos-dnsmasq -n 80 --no-pager
```

* Nothing bound to UDP 67 — `sudo systemctl restart xpdevs-networkos-dnsmasq`.
* `dnsmasq: failed to create listening socket for port 53` — another resolver owns
  port 53. NetworkOS runs dnsmasq with `port=0`; if this appears, the unit is not
  using the generated file.
* A client holding an old lease from another network keeps its old address until it
  reconnects; toggle Wi-Fi off and on, or forget the network.

## Clients connect, get an address, but have no internet

```bash
sudo sysctl net.ipv4.ip_forward                 # must be 1
sudo nft list table inet networkos              # must exist
ping -c2 1.1.1.1                                # from the Pi: upstream reachable?
```

* `ip_forward` is 0 — `sudo networkosctl net-apply`.
* No `table inet networkos` — same command; then check
  `/etc/xpdevs-networkos/generated/networkos.nft` for the reason it was rejected.
* The Pi itself has no internet — the problem is upstream of NetworkOS: check
  `wan_link`, `wan_address` and `internet` in the health report.
* One device has no internet while others do — it is probably blocked in
  **Devices**; unblock it there.

## DNS does not resolve

```bash
sudo ss -ulpn | grep ':53'
dig @10.42.0.1 example.com +short      # from a client
pihole status
sudo networkosctl pihole-configure     # re-apply port, ACL, listening mode
```

* Nothing on port 53 — `sudo systemctl restart pihole-FTL`; if FTL will not start:
  `sudo pihole -r` (repair).
* Queries work from the Pi but not from clients — the nftables prerouting rule
  redirects `wlan0` port 53 to the Pi; re-apply the network configuration.
* Pi-hole's web server must stay on loopback: `pihole-FTL --config webserver.port`
  should print `127.0.0.1:8081`. If it prints another port, the dashboard cannot read
  statistics; re-run `sudo networkosctl pihole-configure`.

## Ad blocking says "not verified"

The dashboard distinguishes *Pi-hole reports blocking enabled* from *blocking was
proven with a DNS query*. To prove it, the service resolves a known ad domain
through `10.42.0.1` and requires a null or NXDOMAIN answer.

* "Pi-hole is not answering" — start with the DNS section above.
* "Blocking disabled" — enable it in **Ad blocking**, and check that
  `pihole-FTL --config dns.blocking.active` prints `true`.
* "Gravity database empty" — the blocklists were never downloaded:
  `sudo networkosctl pihole-gravity`, or **Ad blocking → Update blocklists**.
* The verification domain itself is allowlisted on your Pi-hole — use
  **Verify blocking** again after removing the exception.

## A network change failed and the dashboard mentions safe mode

When `net-apply` cannot prove that the access point came back, it restores the
previous configuration from `/etc/xpdevs-networkos/backups/`, writes a
`SAFE_MODE` marker and stops retrying, so a bad setting cannot lock you out of the
wired management connection.

```bash
sudo networkosctl net-apply --verbose     # shows which validation step failed
cat /etc/xpdevs-networkos/generated/hostapd.conf
```

Fix the value in the dashboard (or restore a backup: **System → Configuration backups**), then
apply again. Clear the marker by applying successfully once.

## The interface is unresponsive or slow

```bash
uptime; free -m; df -h /
vcgencmd measure_temp
vcgencmd get_throttled     # 0x0 means no under-voltage or thermal throttling
```

* `get_throttled` shows `0x50000` or similar — the power supply is too weak.
* Memory is nearly exhausted — check for other services:
  `systemctl --no-pager --state=running list-units`.
* The card is slow or nearly full — see [HARDWARE.md](HARDWARE.md) for storage advice.
* Pi-hole query logging is the largest writer on the card. Lower **Query retention**
  under **Settings** (1–30 days); NetworkOS applies it to Pi-hole's database and
  prunes its own tables on the same schedule.

## The installer stopped part-way

Read the last lines of `/var/log/xpdevs-networkos/install.log`. The installer prints
the failing line and exits; the wired connection is untouched, so you can fix the
cause over SSH and run `sudo bash install.sh` again. It resumes rather than
duplicating work.

Common causes:

| Message | Fix |
| --- | --- |
| `No wired Ethernet interface found` | the cable or the USB Ethernet adapter is missing |
| `has no IPv4 address` | upstream DHCP is down; reboot the router |
| `does not advertise AP mode` | the radio cannot host an access point |
| `apt-get update failed` | DNS or upstream connectivity on the Pi |
| `Pi-hole installer reported a problem` | installation continues without filtering; re-run `install.sh` later |
| `administrator account could not be created` | the database directory permissions were changed; re-run the installer |

## Complete reset

```bash
sudo bash uninstall.sh --purge        # product, configuration, database, logs
sudo bash install.sh                  # start again from a clean state
```

Pre-install snapshots of the original network configuration are kept in
`/var/backups/xpdevs-networkos/`; restore one with
`sudo tar -xzf <file> -C /` if you want the stock Raspberry Pi OS networking back.

## Reporting a problem

Include the output of:

```bash
sudo networkosctl healthcheck --json
cat /proc/device-tree/model; echo
cat /etc/os-release | head -3
tail -n 100 /var/log/xpdevs-networkos/networkos.log
```

Do **not** attach `/etc/xpdevs-networkos/secrets/` or any file containing your
Wi-Fi or administrator password.
