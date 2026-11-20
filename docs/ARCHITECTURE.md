# Architecture

XPDevs NetworkOS is a Python 3 application that owns the network configuration of a
Raspberry Pi and serves its own dashboard. Nothing else runs: no container runtime,
no message broker, no external database, no cloud service.

## Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│ Browser (HTML, CSS, ES modules, no framework, no CDN)                 │
│   templates/*.html · static/css/app.css · static/js/*.js              │
└───────────────▲──────────────────────────────────┬───────────────────┘
                │ HTML                             │ JSON /api/*
┌───────────────┴──────────────────────────────────▼───────────────────┐
│ Flask application (networkos/web)                                    │
│   auth.py · views.py · api/*.py · common.py (guards, CSRF, auditing)  │
│   served by waitress, 4 threads, one process                         │
└───────────────▲──────────────────────────────────┬───────────────────┘
                │ reads                            │ writes
┌───────────────┴──────────────────────────────────▼───────────────────┐
│ Domain services (networkos/services)                                 │
│   resources · systeminfo · systemd · network · wireless · devices ·   │
│   pihole · events · systemstate                                      │
└───────────────▲──────────────────────────────────┬───────────────────┘
                │ state snapshot                   │ sudo networkosctl
┌───────────────┴──────────────────────────────────▼───────────────────┐
│ Privileged core (networkos/*.py, run as root by the CLI)             │
│   configgen (render) → netapply (validate/apply/rollback)            │
│   settings (TOML) · backup · health · collect · security · db        │
└───────────────▲──────────────────────────────────┬───────────────────┘
                │                                  │
        /etc/xpdevs-networkos                hostapd · dnsmasq · nft · Pi-hole FTL
```

The split is deliberate: the dashboard runs as an unprivileged user and can only
*ask* for changes; everything that touches the kernel, the daemons or the
configuration files happens in the root-run CLI after validation.

## Request lifecycle

1. **LAN guard.** `create_app().before_request` rejects requests whose source address
   is not loopback/RFC1918/link-local with a 403 page, before authentication is even
   considered (unless *Allow requests from public addresses* is enabled).
2. **Session.** The `nwos_session` cookie is hashed and looked up; the session is
   extended on activity until the idle limit, and hard-expires at the absolute limit.
3. **API guard.** Every `/api/*` request requires a session, a same-origin check for
   mutating methods and the per-session CSRF token (`X-CSRF-Token`).
4. **Handler.** Views call the service layer; handlers return the JSON envelope
   `{"ok": true, "data": …}` or `{"ok": false, "error": {"code", "message"}}`.
5. **Headers.** CSP (`default-src 'self'`; no inline script or style), `nosniff`,
   `DENY`, `no-referrer`, COOP, `no-store` for everything except static assets.

## Privileged apply pipeline

`netapply.apply()` is the only path that changes the network, and it is ordered so a
failure cannot strand the device:

1. **Render** `hostapd.conf`, `dnsmasq.conf`, `networkos.nft` and the sysctl file from
   the validated configuration.
2. **Validate** with the real parsers: `dnsmasq --test --conf-file=…` and
   `nft -c -f …`. A syntax error aborts before anything is written.
3. **Snapshot** the current generated directory into
   `/etc/xpdevs-networkos/backups/` so the previous working state is recoverable.
4. **Write** the new files (`0600`, root-owned, atomic replace).
5. **Apply sysctl** settings (forwarding, redirect/source-route hardening, `rp_filter`).
6. **Address `wlan0` only** with `ip addr replace`; `eth0` is never touched.
7. **Load the nftables table** atomically with `nft -f`.
8. **Restart** the access point and DHCP service.
9. **Verify** that `iw dev wlan0 info` reports `type AP`, the address is present and
   the DHCP socket is bound.
10. On any failure: **roll back** to the snapshot, reload the previous ruleset, write a
    `SAFE_MODE` marker with the reason, and report the exact failing step.

Wi-Fi-only changes use the same pipeline with `--no-restart` support so boot ordering
(`xpdevs-networkos-net` runs before hostapd) does not produce a restart loop.

## Boot order

```
multi-user.target
 └─ xpdevs-networkos.target
     ├─ xpdevs-networkos-net.service      oneshot: apply configuration, no restart
     ├─ xpdevs-networkos-ap.service       hostapd, forking, Restart=on-failure,
     │                                    BindsTo=sys-subsystem-net-devices-wlan0.device
     ├─ xpdevs-networkos-dnsmasq.service  dnsmasq in the foreground, Restart=on-failure
     ├─ xpdevs-networkos-web.service      waitress on :80, Restart=always, MemoryMax=256M
     └─ xpdevs-networkos-health.timer     runs the health check every 60 s
```

`pihole-FTL.service` is ordered before the dashboard so statistics are available as
soon as the first page loads. The distribution's `hostapd.service` and
`dnsmasq.service` are masked by the installer: they would otherwise compete for the
radio and port 67, and an `apt` upgrade must not start them.

## Health checks and self-repair

`networkos/health.py` runs twelve checks (uplink link/address/route, internet,
access point service and clients, LAN address, DHCP socket, firewall and forwarding,
DNS listener, storage, temperature) and writes the report to
`/var/lib/xpdevs-networkos/health.json`. With `--remediate` it will restart a stopped
service, reapply the address or reload the ruleset — at most once every five minutes,
so a persistent fault cannot turn into a restart loop. The dashboard and the CLI read
the same report; nothing is recalculated differently in the UI.

## State and caching

Privileged reads (`nft list table`, `iw station dump`) are collected by
`networkosctl state`, cached for eight seconds in `services/systemstate.py` and
persisted by the health timer, so a page load never needs root.

Pi-hole status is cached per detail level, so the Overview page's cheap query and the
Ad blocking page's full statistics do not fight over the FTL API. All other reads
(`/proc`, `/sys`, `ip -j`, `systemctl show`) are cheap enough to run per request.

## Data

One SQLite database at `/var/lib/xpdevs-networkos/networkos.db`, WAL journaling,
`synchronous=NORMAL`:

| Table | Purpose |
| --- | --- |
| `users` | administrator accounts, Argon2id hashes |
| `sessions` | hashed session tokens, CSRF token, timestamps, client address |
| `login_attempts` | throttling window |
| `devices` | first/last seen, hostname, IP, blocked flag |
| `events` | dashboard activity feed |
| `audit` | administrative actions with target and detail |
| `settings` | small UI preferences |
| `meta` | schema version |

Retention pruning removes old events, audits, attempts and expired sessions. The
database is the only writable state; everything else on disk is configuration or logs.

## Pi-hole integration

Pi-hole keeps its own database, blocklists and DNS engine. NetworkOS:

* installs it with the official unattended installer and pre-seeded answers;
* moves its web server to `127.0.0.1:8081` with an ACL limited to loopback, so the
  dashboard at `:80` is the only interface exposed to the LAN;
* leaves DNS answering on all interfaces and turns Pi-hole's own DHCP server off, so
  dnsmasq is the only DHCP authority;
* talks to it through `/api/auth`, `/api/stats/*`, `/api/queries`, `/api/domains/*`
  and `/api/lists` (v6 first, read-only fallback for v5);
* proves blocking with a real DNS query rather than trusting the status flag.

## Failure philosophy

* Nothing is reported as working until it has been observed working: blocking is
  "verified" only after a null answer from a real query; the access point is "running"
  only when hostapd is active *and* `iw` says the interface is in AP mode.
* A configuration that cannot be validated is never applied.
* A configuration that applied but did not verify is rolled back.
* The wired management path is never part of a change, so a wireless mistake cannot
  lock the administrator out.
* Errors are logged with their cause and surfaced in the dashboard; they are not
  swallowed into a generic "something went wrong".
