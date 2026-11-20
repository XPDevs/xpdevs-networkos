# Security

XPDevs NetworkOS administers a router. A compromise of the dashboard is a compromise
of the network, so the design keeps the privileged surface small and verifies every
assumption it can.

## Threat model

| Adversary | In scope |
| --- | --- |
| A device on the wireless or wired LAN trying to reach the dashboard or brute-force it | Yes |
| A malicious web page in an administrator's browser (CSRF, clickjacking) | Yes |
| A client trying to bypass ad blocking or the firewall | Yes |
| Someone reading files on the SD card after physical theft | Partly (secrets are root-only, but there is no disk encryption) |
| Someone with root or physical access to the board | No — root on the Pi is game over by definition |
| A remote attacker on the public internet | Only if you deliberately enable public access; the dashboard refuses public sources by default |

## Authentication

* **Argon2id** with `time_cost=3`, `memory_cost=32 MiB`, `parallelism=2`
  (argon2-cffi). If the module is missing, a memory-hard `scrypt` hash
  (N=2¹⁴, r=8, p=1) is used and the installer warns about it. Both formats are
  recognised on verification, so an upgrade does not invalidate the password.
* Minimum length 10 characters, rejected usernames, rejected common passwords, and a
  check that the password is not the username. Strength is enforced in one function
  shared by the dashboard, the CLI and the installer.
* A missing account is checked against a dummy hash so a wrong username and a wrong
  password take the same time.
* **No default credentials exist.** The first visit to the dashboard opens a setup
  screen that creates the only administrator account. Until that account exists,
  every page and every API endpoint redirects there (the API answers
  `409 setup_required`), so the panel cannot be used, read or configured at all.
  The setup screen is reachable only from the local network, requires a CSRF token,
  enforces the password policy server-side and is rate limited; it closes permanently
  once the account exists and is recorded in the audit log. Unattended installations
  can pre-create the account with `NETWORKOS_ADMIN_PASSWORD` instead.
* The password typed on the setup screen travels from the browser to the local
  process over the LAN and is stored only as an Argon2id hash with a random salt. It
  is never written to a log, a temporary file or the configuration, and cannot be
  recovered from the device — a forgotten password is reset with root access
  (`sudo networkosctl admin-create --username admin --password-stdin`).
* Changing the password revokes every other session and is recorded in the audit log.

## Sessions

* 256-bit token from `secrets.token_urlsafe`, stored as a SHA-256 hash — a database
  leak does not leak usable cookies.
* Cookie `nwos_session`: `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` when
  `security.cookie_secure` is enabled (do this when you terminate TLS in front of the
  dashboard).
* Idle timeout 120 minutes and absolute lifetime 24 hours, both configurable. Each
  session carries its own CSRF token and its client address; the Security page lists
  sessions and can revoke them individually.
* Sign-out destroys the row, not just the cookie.

## Cross-site request forgery

* The login form uses a double-submit token: a random value is placed in a
  `HttpOnly` cookie and in the form, and both must match.
* Every mutating `/api/*` request requires the session's CSRF token in the
  `X-CSRF-Token` header (or the `csrf_token` field) and a same-origin `Origin` /
  `Sec-Fetch-Site`. Requests without it are refused with `403 csrf`.
* Cookies are `SameSite=Lax`, so a cross-site form post cannot carry them anyway.
* `X-Frame-Options: DENY` and `frame-ancestors 'none'` prevent clickjacking.

## Login throttling

Failures are counted per address *and* per username over 15 minutes. Five failures
lock further attempts, and the lockout doubles for each additional failure up to one
hour. Successful sign-in clears the counters. Every blocked attempt is logged as a
security event and shown in the dashboard's activity feed.

## Network exposure

* The dashboard listens on port 80 on all interfaces, but `before_request` refuses any
  source address outside loopback, `10/8`, `172.16/12`, `192.168/16` and
  `169.254/16` with an explanatory 403 page — before authentication runs.
  `X-Forwarded-For` is honoured only from a loopback peer, so a remote client cannot
  spoof its way in.
* nftables' `input` chain has `policy drop`: only loopback, established flows,
  DHCP/Pi-hole DNS on the wireless interface, the dashboard ports, ICMP and (optionally)
  SSH are accepted. `forward` drops by default and allows only `wlan0 → eth0`.
* DNS from wireless clients is redirected to Pi-hole, so a client cannot avoid
  filtering by configuring its own resolver.
* Pi-hole's web server is moved to `127.0.0.1:8081` and restricted with
  `webserver.acl=127.0.0.1/32`; the dashboard is the only web interface on the LAN.
* `eth0` is never reconfigured by the installer or by any apply step.

## Privilege separation

The dashboard runs as the system account `xpdevs-networkos` with no shell. Its only
route to privilege is:

```
xpdevs-networkos ALL=(root) NOPASSWD: /usr/local/sbin/networkosctl
```

`networkosctl` is a root-owned wrapper around `python -m networkos.cli`. Inside it:

* every subcommand parses and validates its arguments; anything invalid exits with a
  message and changes nothing;
* every external program is invoked from a fixed table of absolute paths with an
  argument vector and `shell=False` — there is no `shell=True` anywhere in the
  codebase, so shell metacharacters in input are inert;
* the child environment is reduced to `PATH`, `LC_ALL`, `LANG`, `HOME` and `TERM`;
* MAC addresses, IP addresses, channels, SSIDs, domains, unit names and durations
  are all validated by the functions in `networkos/util.py` before use.

Destructive operations require an explicit confirmation phrase, checked on the server:
`CONFIRM-REBOOT`, `CONFIRM-SHUTDOWN`, `CONFIRM-UPDATE`, `CONFIRM-RESTORE`,
`CONFIRM-LAN` and `CONFIRM-PUBLIC`. A request that merely has a valid session and CSRF
token cannot reboot the router.

## Secrets at rest

| Secret | Where | Protection |
| --- | --- | --- |
| Administrator password | SQLite `users.password_hash` (database mode `0600`) | Argon2id hash with a random salt; not reversible, not recoverable |
| Wi-Fi passphrase | **never stored** | converted to the WPA2 PSK, plain text discarded |
| Wi-Fi PSK | `config.toml` (`wifi.psk`) | `0640 root:xpdevs-networkos`; `wpa_psk=` lines are redacted from logs |
| Pi-hole API credential | `/etc/xpdevs-networkos/secrets/pihole-api.key` | `0640 root:xpdevs-networkos`, mode `0750` directory |
| Session signing key | `/etc/xpdevs-networkos/secrets/session.key` | same |

The installer keeps answers in a `0700` temporary directory, shreds the files when it
exits, and never writes a password to `install.log`. The logging layer runs a
redaction filter over every record as a second line of defence (passphrases, PSKs,
`password=`, `token=`, `secret=`).

## First-run window

Between installation and the moment you create the account, anyone on the local
network who reaches the Pi could claim the administrator account. This is the same
behaviour as consumer routers, and the window is normally a few seconds. The
installer prints the dashboard address and the setup screen is announced plainly; if
you cannot complete setup immediately, pre-create the account with
`NETWORKOS_ADMIN_PASSWORD` during installation, or disconnect the Pi's Ethernet cable
until you are ready.

Once an account exists, the setup route redirects to the sign-in page for everyone,
including a signed-in browser.

## Transport security

Plain HTTP is the default because the dashboard is only reachable from the local
network and a self-signed certificate produces constant browser warnings. If you
expose it further, terminate TLS in front of it (for example with a local reverse
proxy), enable `security.cookie_secure` in `config.toml`, and keep
*Allow requests from public addresses* off unless you really need it.

## Auditing

Sign-ins, failures, blocked attempts, configuration changes, device blocks, service
restarts, backups and restores are written to `events` (dashboard feed) and `audit`
(with actor, action, target and client address), and mirrored into
`/var/log/xpdevs-networkos/audit.log`. Retention is bounded so the SD card stays
healthy.

## Hardening checklist for a deployment

1. Use a long, unique administrator password (15+ characters).
2. Use a 20+ character Wi-Fi passphrase — it is the only thing between the neighbourhood
   and your LAN.
3. Keep *Allow requests from public addresses* off.
4. Keep SSH on the LAN only (`wan_ssh = false`, the default) unless you need it on the
   wireless side.
5. Lower query retention if the card is small; keep the SD card out of reach.
6. Review **Security** after any suspicious activity: sessions, recent failures, audit
   trail.
7. Re-run `sudo bash install.sh` after an OS upgrade to repair permissions and units.

## Known limitations

* No multi-factor authentication and a single administrator role. The product is
  designed for a home or small-office router, not for multi-tenant administration.
* WPA2-PSK (AES/CCMP) is the baseline, as specified. WPA3-SAE is not enabled: it
  depends on driver support and on client devices, and enabling it silently would lock
  older devices out.
* The dashboard does not protect against an attacker who already has root on the Pi.
* Pi-hole has its own authentication for its own API; NetworkOS holds a credential for
  it and does not weaken it.
* The firewall filters addresses, ports and connection state; it is not a deep packet
  inspection engine.
