# Hardware compatibility

## Primary target — Raspberry Pi 3 Model B

| | |
| --- | --- |
| SoC | Broadcom BCM2837, 4 × Cortex-A53 @ 1.2 GHz |
| Memory | 1 GB LPDDR2 |
| Ethernet | 10/100 Mbit, attached through the on-board USB 2.0 hub |
| Wi-Fi | BCM43438, 802.11 b/g/n, **2.4 GHz only**, 1 × 1, up to 150 Mbit PHY |
| Bluetooth | 4.1, shares the single antenna with Wi-Fi |
| Storage | microSD (SDXC) |
| Power | 5 V / 2.5 A through micro-USB |

### What that means in practice

* **The radio cannot do 5 GHz.** Channels 36 and above are rejected by the API and
  the installer only offers 1–13. This is a hardware limit, not a setting.
* **Use channels 1, 6 or 11.** They do not overlap; anything else shares spectrum
  with a neighbour and loses throughput.
* **Leave Bluetooth off.** It shares the antenna; NetworkOS does not enable it.
  `dtoverlay=disable-bt` in `/boot/firmware/config.txt` frees the UART and the aerial.
* **Ethernet and Wi-Fi share one USB 2.0 bus.** Combined throughput tops out around
  90–95 Mbit/s; a wireless client will typically see 20–45 Mbit/s of real TCP
  throughput on 802.11n. That is the ceiling of this board, and the dashboard reports
  the link speed the radio negotiated rather than a theoretical number.
* **802.11ac/ax, MU-MIMO and beamforming are not available.** NetworkOS advertises
  exactly what the radio supports (`ieee80211n=1` for HT rates) and nothing more.
* **Power matters.** A weak supply causes `hostapd` to drop clients and the board to
  throttle. Check with `vcgencmd get_throttled` — `0x0` is healthy.

### Access-point support

The BCM43438 firmware supports AP mode through `nl80211`. Verify on your unit:

```bash
sudo apt-get install -y iw
iw list | grep -A8 "Supported interface modes"
```

`* AP` must appear. If it does not, the installed firmware/OS combination cannot
host an access point and XPDevs NetworkOS will refuse to configure one.

## Supported operating systems

| OS | Status |
| --- | --- |
| Raspberry Pi OS Lite 64-bit (bookworm) | Target configuration |
| Raspberry Pi OS Lite 64-bit (trixie) | Expected to work; same packages |
| Raspberry Pi OS Lite 32-bit (bullseye/bookworm) | Works, with a warning: 32-bit limits the address space available to Pi-hole |
| Debian 12/13 arm64 | Works when the wireless firmware and `hostapd` are present |
| Ubuntu Server 24.04 arm64 | Works when `hostapd`/`dnsmasq` are available |

The installer refuses to continue without a wired interface, a wireless interface, a
default route and internet access, because it cannot install packages otherwise.

## Other boards

| Board | Behaviour |
| --- | --- |
| **Raspberry Pi 4 / 5** | Fully supported. The radio does 5 GHz: set `hw_mode = "a"` in `[wifi]` of `/etc/xpdevs-networkos/config.toml` and the dashboard accepts channels 36–165. Gigabit Ethernet removes the throughput ceiling. |
| **Raspberry Pi Zero 2 W** | AP mode and Pi-hole work; there is no Ethernet, so a USB Ethernet adapter is required for the uplink. 512 MB is tight but sufficient for a small network. |
| **Raspberry Pi 3 A+/B+/Zero W** | Same 2.4 GHz radio; supported with the same limits. |
| **Raspberry Pi 1 / 2** | Not supported: the radio (or its absence) and the CPU cannot sustain routing plus Pi-hole. |
| **USB Wi-Fi adapters** | Supported when the driver exposes AP mode through `nl80211` (`iw list` shows `AP`) and the adapter is powered from a hub. The interface name is detected automatically (for example `wlan1`). |

The installer identifies the board from the device tree and prints it; it does not
block installation on unknown hardware, but it does block installation when the radio
cannot host an access point.

## Storage

* **8 GB minimum, 32 GB recommended.** Pi-hole's gravity database alone is 100–200 MB
  after a full blocklist download.
* Use an A1/A2 class card from a reputable brand. Cheap cards fail under the write
  pattern of a router (frequent small writes) long before they run out of space.
* The card is protected by design:
  * SQLite runs in WAL mode with `synchronous=NORMAL`, batching writes.
  * Logs rotate weekly at 512 KB with two compressed generations.
  * Pi-hole query retention defaults to 7 days and is configurable (1–30 days) in
    the dashboard; NetworkOS prunes its own event and audit tables on the same
    schedule.
  * `PYTHONDONTWRITEBYTECODE=1` for the service, so no `__pycache__` churn.
* A USB SSD via the USB 2.0 port is a worthwhile upgrade for a busy network; nothing
  in NetworkOS depends on the medium.

## Network layout

```
        upstream router / switch
                  │  eth0 (DHCP, untouched by the installer)
          ┌───────┴────────┐
          │  Raspberry Pi  │   nftables NAT, Pi-hole on 127.0.0.1:53
          │  NetworkOS     │   dashboard on :80 (LAN only)
          └───────┬────────┘
                  │  wlan0 — 10.42.0.1/24, hostapd + DHCP 10.42.0.10-200
        phones, laptops, TVs, IoT
```

The Pi's wired address stays whatever the upstream router assigned. The wireless
network is a separate subnet that is NATed out through `eth0`, so the Pi never
disturbs the existing LAN's addressing and the administrative interface is not
reachable from the internet.

## Measured on this repository's hardware?

No. The software was developed and tested on x86_64 Debian/Ubuntu without a wireless
radio. Memory, throughput and thermal numbers for the Pi 3 B are therefore stated as
board specifications and design targets, not as measurements taken by this project.
After installing on a Pi, `free -m`, `vcgencmd measure_temp` and the dashboard's
Overview page report the real values for your unit.
