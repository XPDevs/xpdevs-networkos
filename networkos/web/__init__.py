"""Flask application factory for the XPDevs NetworkOS dashboard."""

from __future__ import annotations

import logging
import os
import secrets

from flask import Flask, g, jsonify, redirect, render_template, request

from .. import PRODUCT_NAME, __version__, db, logging_setup, paths, security, settings, util
from ..errors import ApiError, NetworkOSError
from . import common

log = logging.getLogger(__name__)

CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)


def _secret_key() -> bytes:
    try:
        value = paths.SESSION_KEY_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value.encode()
    except OSError:
        pass
    return secrets.token_bytes(32)


def create_app() -> Flask:
    config = settings.load(use_cache=False)
    logging_setup.setup(
        level=config.logging.level,
        max_bytes=config.logging.max_bytes,
        backups=config.logging.backups,
    )
    paths.ensure_dirs()
    db.init()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        SECRET_KEY=_secret_key(),
        SESSION_COOKIE_NAME=security.COOKIE_NAME,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(config.security.cookie_secure),
        SEND_FILE_MAX_AGE_DEFAULT=3600,
        TEMPLATES_AUTO_RELOAD=False,
        MAX_CONTENT_LENGTH=256 * 1024,
        JSON_SORT_KEYS=False,
    )
    app.json.sort_keys = False

    from .api import api_bp
    from .auth import auth_bp
    from .views import views_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    @app.before_request
    def _guard():
        if request.path.startswith("/static/"):
            return None
        current = settings.load()
        remote = common.client_ip()
        if not current.security.allow_public_access and not util.is_private_address(remote):
            return (
                render_template(
                    "blocked.html",
                    product=PRODUCT_NAME,
                    version=__version__,
                    remote=remote,
                    dashboard_hostname=current.system.dashboard_hostname,
                ),
                403,
            )
        # No account means the product has not been set up yet: everything
        # except the setup screen itself is unreachable until one exists.
        if security.user_count() == 0:
            if request.path.startswith("/api/"):
                raise ApiError(
                    "Finish the initial setup before using the dashboard.",
                    status=409,
                    code="setup_required",
                )
            if request.path not in ("/setup", "/login", "/healthz"):
                return redirect("/setup")

        token = request.cookies.get(security.COOKIE_NAME)
        session = security.load_session(
            token,
            idle_minutes=current.security.session_idle_minutes,
        )
        g.session = session
        g.user = security.get_user_by_id(session.user_id) if session else None
        if request.path.startswith("/api/"):
            common.guard_api()
        return None

    @app.after_request
    def _headers(response):
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        if not request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store, max-age=0")
        return response

    @app.context_processor
    def _inject():
        return {
            "product": PRODUCT_NAME,
            "version": __version__,
            "vendor_url": "https://xpdevs.github.io/",
        }

    @app.errorhandler(NetworkOSError)
    def _handle_expected(error: NetworkOSError):
        if request.path.startswith("/api/"):
            return common.json_error(error)
        return (
            render_template(
                "error.html",
                product=PRODUCT_NAME,
                version=__version__,
                code=error.status,
                title="Something needs attention",
                message=error.message,
            ),
            error.status,
        )

    @app.errorhandler(ApiError)
    def _handle_api(error: ApiError):
        return common.json_error(error)

    @app.errorhandler(404)
    def _not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": {"code": "not_found", "message": "Unknown endpoint."}}), 404
        return (
            render_template(
                "error.html",
                product=PRODUCT_NAME,
                version=__version__,
                code=404,
                title="Page not found",
                message="The page you asked for does not exist in this dashboard.",
            ),
            404,
        )

    @app.errorhandler(500)
    def _server_error(error):  # pragma: no cover - safety net
        log.exception("unhandled error on %s", request.path)
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": {"code": "server_error", "message": "The request could not be completed."}}), 500
        return (
            render_template(
                "error.html",
                product=PRODUCT_NAME,
                version=__version__,
                code=500,
                title="Unexpected error",
                message="The dashboard hit an unexpected error. Check the system log for details.",
            ),
            500,
        )

    @app.template_filter("bytes")
    def _bytes(value):
        return util.human_bytes(value)

    @app.template_filter("duration")
    def _duration(value):
        return util.format_duration(value)

    @app.template_filter("number")
    def _number(value):
        if value is None or value == "":
            return "—"
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter("temp")
    def _temp(value):
        return f"{value} °C" if value not in (None, "") else "—"

    return app


def serve() -> None:  # pragma: no cover - process entry point
    """Run the dashboard with waitress (small, pure-Python WSGI server)."""
    from waitress import serve as waitress_serve

    config = settings.load(use_cache=False)
    app = create_app()
    host = config.security.bind_host or "0.0.0.0"
    port = int(config.security.bind_port or 80)
    logging_setup.get_logger("web").info(
        "dashboard listening on %s:%s (pid %s)", host, port, os.getpid()
    )
    waitress_serve(
        app,
        host=host,
        port=port,
        threads=4,
        connection_limit=100,
        channel_timeout=30,
        ident=PRODUCT_NAME,
        clear_untrusted_proxy_headers=True,
    )
