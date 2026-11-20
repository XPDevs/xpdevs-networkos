"""Sign-in and sign-out."""

from __future__ import annotations

import hmac
import logging
import secrets

from flask import Blueprint, g, make_response, redirect, render_template, request, url_for

from .. import PRODUCT_NAME, __version__, security, settings, util
from ..errors import ApiError
from ..services import events
from . import common

log = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

LOGIN_CSRF_COOKIE = "nwos_login"
_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    """A real hash so a missing user costs the same time as a wrong password."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = security.hash_password(secrets.token_urlsafe(24))
    return _DUMMY_HASH


def _render_login(token: str, error: str | None = None, username: str = "", status: int = 200):
    body = render_template(
        "login.html",
        product=PRODUCT_NAME,
        version=__version__,
        csrf_token=token,
        error=error,
        username=username,
        next=common.safe_redirect_target(request.args.get("next")),
        dashboard_hostname=settings.load().system.dashboard_hostname,
        argon2=security.ARGON2_AVAILABLE,
    )
    response = make_response(body, status)
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        token,
        max_age=900,
        httponly=True,
        samesite="Lax",
        secure=settings.load().security.cookie_secure,
        path="/",
    )
    return response


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if getattr(g, "session", None) is not None:
        return redirect(common.safe_redirect_target(request.args.get("next")))

    # Nothing to sign in to until the first-run setup has created an account.
    if security.user_count() == 0:
        return redirect(url_for("auth.setup"))

    config = settings.load()
    if request.method == "GET":
        return _render_login(secrets.token_urlsafe(32))

    submitted = request.form.get("csrf_token", "")
    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    username = str(request.form.get("username", "")).strip().lower()[:64]
    password = request.form.get("password", "")

    if not cookie_token or not hmac.compare_digest(submitted, cookie_token):
        return _render_login(
            secrets.token_urlsafe(32),
            error="Your sign-in page expired. Please try again.",
            username=username,
            status=400,
        )

    ip = common.client_ip()
    state = security.throttle_state(
        ip,
        username,
        max_attempts=config.security.login_max_attempts,
        window_minutes=config.security.login_window_minutes,
    )
    if not state.allowed:
        minutes = max(1, round(state.retry_after / 60))
        events.record(
            "security",
            "warning",
            "Sign-in blocked by the rate limiter",
            details={"ip": ip, "username": username, "retry_after": state.retry_after},
        )
        return _render_login(
            secrets.token_urlsafe(32),
            error=f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
            username=username,
            status=429,
        )

    user = security.get_user(username)
    valid = security.verify_password(password, user.password_hash if user else _dummy_hash())
    if not user or not valid or not user.is_active:
        security.record_login_attempt(ip, username, False)
        events.record(
            "security",
            "warning",
            "Failed sign-in attempt",
            details={"ip": ip, "username": username},
        )
        return _render_login(
            secrets.token_urlsafe(32),
            error="Incorrect username or password.",
            username=username,
            status=401,
        )

    security.record_login_attempt(ip, username, True)
    security.clear_attempts(ip, username)
    session = security.create_session(
        user.id,
        ip,
        request.headers.get("User-Agent", "")[:200],
        idle_minutes=config.security.session_idle_minutes,
        absolute_hours=config.security.session_absolute_hours,
    )
    events.record("security", "info", f"Administrator {user.username} signed in", details={"ip": ip})
    events.audit(user.username, "sign-in", ip=ip, detail=request.headers.get("User-Agent", "")[:120])

    target = common.safe_redirect_target(request.args.get("next"))
    response = redirect(target or url_for("views.overview"))
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/")
    response.set_cookie(
        security.COOKIE_NAME,
        session.token,
        max_age=max(600, config.security.session_absolute_hours * 3600),
        httponly=True,
        samesite="Lax",
        secure=bool(config.security.cookie_secure),
        path="/",
    )
    return response


# --------------------------------------------------------------------------- #
# First-run setup
# --------------------------------------------------------------------------- #
def _render_setup(
    token: str,
    *,
    username: str = "",
    errors: list[str] | None = None,
    status: int = 200,
):
    config = settings.load()
    body = render_template(
        "setup.html",
        product=PRODUCT_NAME,
        version=__version__,
        csrf_token=token,
        username=username or "admin",
        errors=errors or [],
        argon2=security.ARGON2_AVAILABLE,
        min_length=security.MIN_PASSWORD_LENGTH,
        dashboard_hostname=config.system.dashboard_hostname,
        lan_address=config.network.lan_address,
    )
    response = make_response(body, status)
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        token,
        max_age=1800,
        httponly=True,
        samesite="Lax",
        secure=config.security.cookie_secure,
        path="/",
    )
    return response


@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Create the first administrator account.

    Reachable only while no account exists, so the panel cannot be used at all
    until a password has been chosen and stored as an Argon2id hash. Requests
    arrive only from the local network: the application-wide guard refuses every
    other source before this view runs.
    """
    if security.user_count() > 0:
        if getattr(g, "session", None) is not None:
            return redirect(url_for("views.overview"))
        return redirect(url_for("auth.login"))

    config = settings.load()

    if request.method == "GET":
        return _render_setup(secrets.token_urlsafe(32))

    submitted = request.form.get("csrf_token", "")
    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    username = str(request.form.get("username", "")).strip().lower()[:64]
    password = request.form.get("password", "")
    confirm = request.form.get("password_confirm", "")

    if not cookie_token or not hmac.compare_digest(submitted, cookie_token):
        return _render_setup(
            secrets.token_urlsafe(32),
            username=username,
            errors=["Your setup page expired. Reload the page and try again."],
            status=400,
        )

    ip = common.client_ip()
    state = security.throttle_state(
        ip,
        "__setup__",
        max_attempts=10,
        window_minutes=config.security.login_window_minutes,
    )
    if not state.allowed:
        minutes = max(1, round(state.retry_after / 60))
        return _render_setup(
            secrets.token_urlsafe(32),
            username=username,
            errors=[f"Too many attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."],
            status=429,
        )

    errors: list[str] = []
    if not username:
        errors.append("Choose an administrator username.")
    if not password:
        errors.append("Choose a password.")
    if password != confirm:
        errors.append("The two passwords do not match.")
    if not errors:
        errors.extend(security.password_problems(password, username))
        if password != password.strip():
            errors.append("Do not start or end the password with a space.")

    if errors:
        security.record_login_attempt(ip, "__setup__", False)
        return _render_setup(
            secrets.token_urlsafe(32),
            username=username,
            errors=errors,
            status=400,
        )

    try:
        user = security.create_or_update_user(username, password)
    except ApiError as exc:
        security.record_login_attempt(ip, "__setup__", False)
        return _render_setup(
            secrets.token_urlsafe(32),
            username=username,
            errors=[exc.message],
            status=exc.status,
        )

    security.clear_attempts(ip, "__setup__")
    session = security.create_session(
        user.id,
        ip,
        request.headers.get("User-Agent", "")[:200],
        idle_minutes=config.security.session_idle_minutes,
        absolute_hours=config.security.session_absolute_hours,
    )
    events.record(
        "security",
        "info",
        f"Administrator account {user.username} created during first-run setup",
        details={"ip": ip},
    )
    events.audit(user.username, "account-created", ip=ip, detail="first-run setup")

    response = redirect(url_for("views.overview"))
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/")
    response.set_cookie(
        security.COOKIE_NAME,
        session.token,
        max_age=max(600, config.security.session_absolute_hours * 3600),
        httponly=True,
        samesite="Lax",
        secure=bool(config.security.cookie_secure),
        path="/",
    )
    return response


@auth_bp.route("/logout", methods=["POST"])
@common.login_required
def logout():
    session = g.session
    token = request.cookies.get(security.COOKIE_NAME)
    security.destroy_session(token)
    events.record("security", "info", f"Administrator {session.username} signed out")
    events.audit(session.username, "sign-out", ip=common.client_ip())
    response = redirect(url_for("auth.login"))
    response.delete_cookie(security.COOKIE_NAME, path="/")
    return response


@auth_bp.route("/logout", methods=["GET"])
def logout_get():
    """Signing out must be a POST; send people to the dashboard instead."""
    return redirect("/")


@auth_bp.route("/healthz")
def healthz():
    """Unauthenticated liveness probe with no system detail (used by the installer)."""
    return {"status": "ok", "product": PRODUCT_NAME, "version": __version__}
