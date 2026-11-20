"""Shared helpers for the web layer: responses, guards, CSRF and auditing."""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from flask import Response, g, jsonify, redirect, request, url_for

from .. import PRODUCT_NAME, VENDOR_URL, __version__, paths, security, settings, util
from ..errors import ApiError, NetworkOSError
from ..services import events

log = logging.getLogger(__name__)

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def cfg():
    return settings.load()


def client_ip() -> str:
    """The address a decision is made for.

    ``X-Forwarded-For`` is honoured only when the immediate peer is loopback,
    which is the case for a reverse proxy on the same host. A header sent by a
    remote client is ignored so it can never widen the LAN-only guard.
    """
    remote = (request.remote_addr or "0.0.0.0")[:64]
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and remote in ("127.0.0.1", "::1"):
        candidate = forwarded.split(",")[0].strip()[:64]
        if util.valid_ipv4(candidate):
            return candidate
    return remote


def json_ok(data: Any = None, **extra: Any) -> Response:
    payload: dict[str, Any] = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return jsonify(payload)


def json_error(error: NetworkOSError) -> Response:
    response = jsonify({"ok": False, "error": error.to_payload()})
    response.status_code = error.status
    return response


def login_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if getattr(g, "session", None) is None:
            if request.path.startswith("/api/"):
                raise ApiError("Your session has expired. Sign in again.", status=401, code="unauthorized")
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def page_context(page: str, **extra: Any) -> dict[str, Any]:
    session = getattr(g, "session", None)
    config = cfg()
    context: dict[str, Any] = {
        "product": PRODUCT_NAME,
        "version": __version__,
        "vendor_url": VENDOR_URL,
        "page": page,
        "csrf_token": session.csrf_token if session else "",
        "username": session.username if session else "",
        "dashboard_hostname": config.system.dashboard_hostname,
        "theme_default": config.ui.theme_default,
    }
    context.update(extra)
    return context


def audit(action: str, *, target: str = "", detail: str = "") -> None:
    session = getattr(g, "session", None)
    username = session.username if session else "administrator"
    events.audit(username, action, target=target, detail=detail, ip=client_ip())


def same_origin() -> bool:
    """Reject cross-site state changes even before the CSRF token is checked."""
    origin = request.headers.get("Origin")
    if origin:
        host = request.host
        return origin.rstrip("/").endswith(host)
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        return False
    return True


def csrf_from_request() -> str | None:
    token = request.headers.get(security.CSRF_HEADER)
    if token:
        return token
    if request.is_json:
        body = request.get_json(silent=True) or {}
        return body.get("csrf_token")
    return request.form.get(security.CSRF_FORM_FIELD)


def guard_api() -> None:
    """Raise a JSON error when an API call is unauthenticated or cross-site."""
    session = getattr(g, "session", None)
    if session is None:
        raise ApiError("Your session has expired. Sign in again.", status=401, code="unauthorized")
    if request.method in MUTATING_METHODS:
        if not same_origin():
            raise ApiError("Cross-site request rejected.", status=403, code="cross_origin")
        if not security.csrf_valid(session, csrf_from_request()):
            raise ApiError("The security token expired. Reload the page and try again.", status=403, code="csrf")


def payload() -> dict[str, Any]:
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    return {key: value for key, value in request.form.items()}


def want_bool(data: dict[str, Any], key: str, default: bool | None = None) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on", "enabled"):
            return True
        if lowered in ("0", "false", "no", "off", "disabled"):
            return False
    if default is not None:
        return default
    raise ApiError(f"'{key}' must be true or false.", code="invalid_boolean")


def require_text(data: dict[str, Any], key: str, label: str, *, maximum: int = 200) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ApiError(f"Enter {label}.", code=f"missing_{key}")
    if len(value) > maximum:
        raise ApiError(f"{label.capitalize()} is too long.", code=f"invalid_{key}")
    return value


def confirm_token(kind: str) -> str:
    return f"CONFIRM-{kind}".upper()


def require_confirmation(data: dict[str, Any], kind: str) -> None:
    """Destructive actions require the explicit confirmation phrase."""
    supplied = str(data.get("confirm", "")).strip().upper()
    expected = confirm_token(kind)
    if supplied != expected:
        raise ApiError(
            f"Type {expected} to confirm this action.",
            code="confirmation_required",
        )


def safe_redirect_target(candidate: str | None) -> str:
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


def environment() -> dict[str, Any]:
    return {
        "dev": paths.CONFIG_FILE.exists() is False,
        "control_available": bool(cfg().security.bind_port),
        "version": __version__,
    }


def summarise_error(exc: Exception) -> str:
    if isinstance(exc, NetworkOSError):
        return exc.message
    return str(exc)[:300]


def iso(value: Any) -> str:
    return util.iso(value)
