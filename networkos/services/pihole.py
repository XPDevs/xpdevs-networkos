"""Pi-hole integration.

The dashboard talks to Pi-hole's own HTTP API (v6 REST, falling back to the v5
``api.php`` interface) instead of re-implementing blocking.  Nothing is reported
as "blocking active" unless Pi-hole confirms it *and* a DNS query through the
resolver is actually answered with a blocked result.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import USER_AGENT, paths, shell, util
from ..errors import PiholeError

log = logging.getLogger(__name__)

DEFAULT_TEST_DOMAINS = ("doubleclick.net", "googleadservices.com", "ads.pubmatic.com")
BLOCK_ANSWERS = {"0.0.0.0", "::", "::1"}

_LOCK = threading.Lock()
_STATUS_CACHE: dict[str, Any] = {}
_STATUS_AT = 0.0
_VERIFY_CACHE: dict[str, Any] = {}
_VERIFY_AT = 0.0
STATUS_TTL = 5.0
VERIFY_TTL = 60.0


def _password() -> str | None:
    try:
        value = paths.PIHOLE_PASSWORD_FILE.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _v5_token() -> str | None:
    """Pi-hole v5 authenticates with the WEBPASSWORD hash from setupVars."""
    try:
        with open("/etc/pihole/setupVars.conf", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("WEBPASSWORD="):
                    value = line.split("=", 1)[1].strip()
                    return value or None
    except OSError:
        return None
    return None


class PiholeClient:
    """Minimal JSON client for the Pi-hole API (no external dependencies)."""

    def __init__(self, base: str, password: str | None = None, timeout: float = 5.0):
        self.base = base.rstrip("/")
        self.password = password
        self.timeout = timeout
        self._sid: str | None = None
        self._sid_expires = 0.0
        self._version: int | None = None

    # -- low level ------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> tuple[int, Any]:
        url = f"{self.base}{path}"
        query = dict(params or {})
        if self._version == 5 and auth:
            token = self.password or _v5_token()
            if token:
                query.setdefault("auth", token)
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if auth and self._sid:
            request.add_header("X-FTL-SID", self._sid)
            request.add_header("sid", self._sid)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            status = exc.code
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise PiholeError(
                f"Could not reach Pi-hole at {self.base} ({reason}). "
                "Check that pihole-FTL is running and that its webserver.port matches this address."
            ) from exc
        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return status, None

    def detect_version(self) -> int:
        if self._version:
            return self._version
        status, _ = self._request("GET", "/api/auth", auth=False)
        if status in (200, 401, 400, 405, 415):
            self._version = 6
            return 6
        status, payload = self._request("GET", "/admin/api.php", params={"versions": "true"}, auth=False)
        if status == 200 and isinstance(payload, dict):
            self._version = 5
            return 5
        raise PiholeError("Pi-hole is installed but its API did not respond.")

    # -- session -------------------------------------------------------- #
    def _authenticate(self) -> None:
        if self.detect_version() == 5:
            return
        if not self.password:
            raise PiholeError(
                "The Pi-hole API password is not available. Re-run the installer to repair it."
            )
        status, payload = self._request("POST", "/api/auth", body={"password": self.password}, auth=False)
        if status >= 400 or not isinstance(payload, dict):
            raise PiholeError("Pi-hole rejected the API credentials.")
        session = payload.get("session") or {}
        if not session.get("valid"):
            raise PiholeError("Pi-hole rejected the API password (it may have been changed).")
        self._sid = session.get("sid")
        self._sid_expires = time.time() + max(30, util.to_int(session.get("validity"), 300) - 20)

    def _ensure_session(self) -> None:
        if self.detect_version() == 5:
            return
        if not self._sid or time.time() >= self._sid_expires:
            self._authenticate()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._ensure_session()
        status, payload = self._request("GET", path, params=params)
        if status == 401:
            self._sid = None
            self._ensure_session()
            status, payload = self._request("GET", path, params=params)
        if status >= 400:
            raise PiholeError(_error_message(payload, status))
        return payload

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        self._ensure_session()
        status, payload = self._request("POST", path, body=body)
        if status == 401:
            self._sid = None
            self._ensure_session()
            status, payload = self._request("POST", path, body=body)
        if status >= 400:
            raise PiholeError(_error_message(payload, status))
        return payload

    def _delete(self, path: str) -> Any:
        self._ensure_session()
        status, payload = self._request("DELETE", path)
        if status == 401:
            self._sid = None
            self._ensure_session()
            status, payload = self._request("DELETE", path)
        if status >= 400:
            raise PiholeError(_error_message(payload, status))
        return payload

    # -- API surface ---------------------------------------------------- #
    def summary(self) -> dict[str, Any]:
        if self.detect_version() == 5:
            data = self._get("/admin/api.php", {"summaryRaw": ""})
            if not isinstance(data, dict):
                raise PiholeError("Pi-hole returned an unexpected summary response.")
            return {
                "total": util.to_int(data.get("dns_queries_today")),
                "blocked": util.to_int(data.get("ads_blocked_today")),
                "percent": round(util.to_float(data.get("ads_percentage_today")), 1),
                "unique_domains": util.to_int(data.get("unique_domains")),
                "forwarded": util.to_int(data.get("queries_forwarded")),
                "cached": util.to_int(data.get("queries_cached")),
                "clients_active": util.to_int(data.get("unique_clients")),
                "clients_total": util.to_int(data.get("clients_ever_seen")),
                "gravity_domains": util.to_int(data.get("domains_being_blocked")),
                "gravity_updated": None,
                "blocking": data.get("status") == "enabled",
            }
        data = self._get("/api/stats/summary")
        data = data or {}
        queries = data.get("queries") or {}
        clients = data.get("clients") or {}
        gravity = data.get("gravity") or {}
        last_update = gravity.get("last_update")
        return {
            "total": util.to_int(queries.get("total")),
            "blocked": util.to_int(queries.get("blocked")),
            "percent": round(util.to_float(queries.get("percent_blocked")), 1),
            "unique_domains": util.to_int(queries.get("unique_domains")),
            "forwarded": util.to_int(queries.get("forwarded")),
            "cached": util.to_int(queries.get("cached")),
            "clients_active": util.to_int(clients.get("active")),
            "clients_total": util.to_int(clients.get("total")),
            "gravity_domains": util.to_int(gravity.get("domains_being_blocked")),
            "gravity_updated": util.iso(util.to_int(last_update)) if last_update else None,
            "blocking": None,
        }

    def blocking_state(self) -> str:
        if self.detect_version() == 5:
            data = self._get("/admin/api.php", {"status": ""})
            state = (data or {}).get("status") if isinstance(data, dict) else None
            return "enabled" if state == "enabled" else "disabled"
        data = self._get("/api/dns/blocking") or {}
        return str(data.get("blocking", "unknown"))

    def set_blocking(self, enabled: bool) -> str:
        if self.detect_version() == 5:
            self._get("/admin/api.php", {"enable": ""} if enabled else {"disable": "0"})
            return "enabled" if enabled else "disabled"
        self._post("/api/dns/blocking", {"blocking": bool(enabled)})
        return self.blocking_state()

    def top_domains(self, blocked: bool, count: int = 10) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        count = max(1, min(int(count), 50))
        if self.detect_version() == 5:
            data = self._get("/admin/api.php", {"topItems": str(count)})
            data = data or {}
            source = data.get("top_ads" if blocked else "top_queries") or {}
            return (
                [{"domain": domain, "count": util.to_int(value)} for domain, value in source.items()],
                data,
            )
        data = self._get("/api/stats/top_domains", {"blocked": "true" if blocked else "false", "count": count})
        data = data or {}
        domains = [
            {"domain": item.get("domain", ""), "count": util.to_int(item.get("count"))}
            for item in (data.get("domains") or [])
        ]
        return domains, {"total": util.to_int(data.get("total_queries")), "blocked": util.to_int(data.get("blocked_queries"))}

    def top_clients(self, count: int = 8) -> list[dict[str, Any]]:
        count = max(1, min(int(count), 50))
        if self.detect_version() == 5:
            data = self._get("/admin/api.php", {"topClients": str(count)})
            source = (data or {}).get("top_sources") or {}
            return [
                {"client": name.rstrip("^"), "count": util.to_int(value), "name": None}
                for name, value in source.items()
            ]
        data = self._get("/api/stats/top_clients", {"count": count}) or {}
        return [
            {
                "client": item.get("ip", ""),
                "name": item.get("name") or None,
                "count": util.to_int(item.get("count")),
            }
            for item in (data.get("clients") or [])
        ]

    def recent_queries(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        if self.detect_version() == 5:
            data = self._get("/admin/api.php", {"type": "query", "length": str(limit)})
            rows = (data or {}).get("data") or []
            result = []
            for row in rows:
                if not isinstance(row, list) or len(row) < 6:
                    continue
                status_text = str(row[5])
                result.append(
                    {
                        "domain": str(row[4]),
                        "client": str(row[3]),
                        "client_name": None,
                        "status": status_text,
                        "blocked": "blocked" in status_text.lower(),
                        "type": str(row[1]),
                        "at": util.iso(util.to_int(row[0])),
                    }
                )
            return result
        data = self._get("/api/queries", {"length": limit}) or {}
        result = []
        for row in data.get("queries") or []:
            client = row.get("client") or {}
            status_text = str(row.get("status", ""))
            result.append(
                {
                    "domain": row.get("domain", ""),
                    "client": client.get("ip") or "",
                    "client_name": client.get("name") or None,
                    "status": status_text,
                    "blocked": status_text.upper().startswith(("BLOCKED", "GRAVITY", "DENIED", "REGEX")),
                    "type": row.get("type", ""),
                    "at": util.iso(util.to_int(row.get("time"))),
                }
            )
        return result

    def lists(self) -> list[dict[str, Any]]:
        if self.detect_version() == 5:
            return []
        data = self._get("/api/lists") or {}
        return [
            {
                "address": item.get("address", ""),
                "enabled": bool(item.get("enabled")),
                "comment": item.get("comment") or "",
                "type": item.get("type", "block"),
                "domains": util.to_int(item.get("number_of_domains")),
                "updated": util.iso(util.to_int(item.get("date_updated"))) if item.get("date_updated") else None,
            }
            for item in (data.get("lists") or [])
        ]

    def add_list(self, address: str, comment: str = "Added by XPDevs NetworkOS") -> None:
        if self.detect_version() == 5:
            raise PiholeError("Blocklist management requires Pi-hole 6 or newer.")
        self._post("/api/lists", {"address": address, "type": "block", "enabled": True, "comment": comment})

    def remove_list(self, address: str) -> None:
        if self.detect_version() == 5:
            raise PiholeError("Blocklist management requires Pi-hole 6 or newer.")
        self._delete("/api/lists/" + urllib.parse.quote(address, safe=""))

    def domains(self, kind: str) -> list[dict[str, Any]]:
        kind = "allow" if kind == "allow" else "deny"
        if self.detect_version() == 5:
            data = self._get("/admin/api.php", {"list": "white" if kind == "allow" else "black"})
            rows = data if isinstance(data, list) else []
            return [
                {"domain": item.get("domain", ""), "comment": "", "id": item.get("id")}
                for item in rows
                if isinstance(item, dict)
            ]
        data = self._get(f"/api/domains/{kind}/exact") or {}
        return [
            {
                "domain": item.get("domain", ""),
                "comment": item.get("comment") or "",
                "enabled": bool(item.get("enabled", True)),
                "id": item.get("id"),
            }
            for item in (data.get("domains") or [])
        ]

    def add_domain(self, kind: str, domain: str, comment: str = "Added by XPDevs NetworkOS") -> None:
        kind = "allow" if kind == "allow" else "deny"
        if self.detect_version() == 5:
            key = "customallow" if kind == "allow" else "customdeny"
            self._get("/admin/api.php", {key: domain})
            return
        self._post(f"/api/domains/{kind}/exact", {"domain": domain, "comment": comment})

    def remove_domain(self, kind: str, domain: str) -> None:
        kind = "allow" if kind == "allow" else "deny"
        if self.detect_version() == 5:
            raise PiholeError("Removing individual domains requires Pi-hole 6 or newer.")
        self._delete(f"/api/domains/{kind}/exact/" + urllib.parse.quote(domain, safe=""))

    def version(self) -> dict[str, Any]:
        if self.detect_version() == 5:
            data = self._get("/admin/api.php", {"versions": ""}) or {}
            core = ((data.get("core_update") or {}) if isinstance(data, dict) else {})
            return {"core": data.get("version") if isinstance(data, dict) else None, "raw": data}
        data = self._get("/api/info/version") or {}
        components = data.get("components") or {}
        return {
            "core": ((components.get("core") or {}).get("local") or {}).get("version"),
            "ftl": ((components.get("ftl") or {}).get("local") or {}).get("version"),
            "web": ((components.get("web") or {}).get("local") or {}).get("version"),
        }


def _error_message(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("key"):
            return str(error.get("message") or error["key"])
        if isinstance(error, str):
            return error
    return f"Pi-hole returned HTTP {status}."


# --------------------------------------------------------------------------- #
# Service-level helpers used by the dashboard
# --------------------------------------------------------------------------- #
def client(cfg) -> PiholeClient:
    return PiholeClient(cfg.pihole.api_base, _password())


def _service_active() -> dict[str, Any]:
    from . import systemd

    return systemd.status("pihole")


def status(
    cfg, *, force: bool = False, include_lists: bool = False, include_details: bool = True
) -> dict[str, Any]:
    """Cached Pi-hole status for the dashboard.

    ``include_details=False`` fetches only the summary numbers, blocking state
    and the blocking proof, which keeps the overview poll cheap.
    """
    global _STATUS_CACHE, _STATUS_AT
    cache_key = f"{include_lists}:{include_details}"
    now = time.monotonic()
    with _LOCK:
        cached = _STATUS_CACHE.get(cache_key)
        if not force and cached and now - _STATUS_AT < STATUS_TTL:
            return cached

    service = _service_active()
    result: dict[str, Any] = {
        "installed": bool(shell.available("pihole") or service["active"] in ("active", "inactive")),
        "service": service,
        "available": False,
        "reason": None,
        "api_version": None,
        "enabled": False,
        "api_state": "unknown",
        "verified": False,
        "verified_at": None,
        "verification": None,
        "stats": {},
        "top_blocked": [],
        "top_permitted": [],
        "top_clients": [],
        "lists": [],
        "allow": [],
        "deny": [],
        "version": {},
    }
    if not service["running"]:
        result["reason"] = "Pi-hole FTL is not running"
        with _LOCK:
            _STATUS_CACHE = {cache_key: result}
            _STATUS_AT = now
        return result

    api = client(cfg)
    try:
        result["api_version"] = api.detect_version()
        stats = api.summary()
        api_state = api.blocking_state()
        verification = verify_blocking(api, force=force)
        result.update(
            {
                "available": True,
                "api_version": result["api_version"],
                "api_state": api_state,
                "enabled": api_state == "enabled",
                "stats": stats,
                "verified": bool(api_state == "enabled" and verification.get("blocking")),
                "verified_at": verification.get("checked_at"),
                "verification": verification,
            }
        )
        blocked_domains, _meta = api.top_domains(True, 8)
        result["top_blocked"] = [item for item in blocked_domains if item["domain"]]
        if include_details:
            permitted_domains, _meta2 = api.top_domains(False, 8)
            result["top_permitted"] = [item for item in permitted_domains if item["domain"]]
            result["top_clients"] = api.top_clients(6)
            result["allow"] = api.domains("allow")
            result["deny"] = api.domains("deny")
            if include_lists:
                result["lists"] = api.lists()
            try:
                result["version"] = api.version()
            except PiholeError:
                result["version"] = {}
    except PiholeError as exc:
        result["reason"] = str(exc)
        log.info("Pi-hole integration unavailable: %s", exc)

    with _LOCK:
        _STATUS_CACHE = {cache_key: result}
        _STATUS_AT = now
    return result


def invalidate() -> None:
    global _STATUS_CACHE, _STATUS_AT, _VERIFY_CACHE, _VERIFY_AT
    with _LOCK:
        _STATUS_CACHE = {}
        _STATUS_AT = 0.0
        _VERIFY_CACHE = {}
        _VERIFY_AT = 0.0


def verify_blocking(api: PiholeClient | None = None, *, force: bool = False) -> dict[str, Any]:
    """Prove that blocking works by resolving a known advertisement domain."""
    global _VERIFY_CACHE, _VERIFY_AT
    now = time.monotonic()
    with _LOCK:
        if not force and _VERIFY_CACHE and now - _VERIFY_AT < VERIFY_TTL:
            return _VERIFY_CACHE

    candidates: list[str] = []
    if api is not None:
        try:
            top, _meta = api.top_domains(True, 3)
            candidates = [item["domain"] for item in top if item.get("domain")]
        except PiholeError:
            candidates = []
    candidates = candidates[:2] + [d for d in DEFAULT_TEST_DOMAINS if d not in candidates]

    checked: list[dict[str, Any]] = []
    blocking = False
    for domain in candidates[:3]:
        outcome = _resolve(domain)
        checked.append({"domain": domain, **outcome})
        if outcome["result"] == "blocked":
            blocking = True
            break
    payload = {
        "blocking": blocking,
        "checked_at": util.iso_now(),
        "checks": checked,
        "method": "dns",
    }
    with _LOCK:
        _VERIFY_CACHE, _VERIFY_AT = payload, now
    return payload


def _resolve(domain: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(domain, 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        if exc.errno in (-2, -3, -5, -8):  # NXDOMAIN / NODATA / REFUSED
            return {"result": "blocked", "address": None, "detail": "Name does not resolve", "latency_ms": None}
        return {"result": "unknown", "address": None, "detail": f"Resolver error: {exc.strerror}", "latency_ms": None}
    except OSError as exc:
        return {"result": "unknown", "address": None, "detail": str(exc), "latency_ms": None}
    addresses = sorted({info[4][0] for info in infos})
    blocked = bool(addresses) and all(address in BLOCK_ANSWERS for address in addresses)
    return {
        "result": "blocked" if blocked else "allowed",
        "address": addresses[0] if addresses else None,
        "detail": "Answered with a null address" if blocked else "Resolved to a routable address",
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
    }


def set_blocking(cfg, enabled: bool) -> dict[str, Any]:
    api = client(cfg)
    state = api.set_blocking(enabled)
    invalidate()
    return {"api_state": state, "enabled": state == "enabled"}


def recent_queries(cfg, limit: int = 25) -> list[dict[str, Any]]:
    return client(cfg).recent_queries(limit)


def add_domain(cfg, kind: str, domain: str) -> None:
    api = client(cfg)
    api.add_domain(kind, domain)
    invalidate()


def remove_domain(cfg, kind: str, domain: str) -> None:
    api = client(cfg)
    api.remove_domain(kind, domain)
    invalidate()


def add_list(cfg, address: str) -> None:
    api = client(cfg)
    api.add_list(address)
    invalidate()


def remove_list(cfg, address: str) -> None:
    api = client(cfg)
    api.remove_list(address)
    invalidate()


def update_gravity() -> dict[str, Any]:
    """Run ``pihole -g`` through the privileged helper (blocking gravity rebuild)."""
    from ..errors import CommandError

    result = shell.ctl("pihole-gravity", timeout=600)
    if not result.ok:
        raise CommandError(
            "Updating blocklists failed.",
            returncode=result.returncode,
            stderr=(result.stderr or result.stdout).strip()[-400:],
            command="pihole -g",
        )
    invalidate()
    tail = "\n".join(result.stdout.strip().splitlines()[-6:])
    return {"updated": True, "output": tail}


def set_retention(days: int) -> dict[str, Any]:
    from ..errors import CommandError

    days = max(1, min(int(days), 365))
    result = shell.ctl("pihole-retention", str(days), timeout=60)
    if not result.ok:
        raise CommandError(
            "Could not change the Pi-hole query retention.",
            returncode=result.returncode,
            stderr=(result.stderr or result.stdout).strip()[:400],
            command=f"pihole-FTL --config database.maxDBdays {days}",
        )
    return {"retention_days": days}
