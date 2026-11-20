"""Administrator credential and session management."""

from __future__ import annotations

import logging

from flask import g, request

from ... import security, security as security_module, util
from ...errors import ApiError
from ...services import events
from .. import common
from ..common import json_ok, payload
from . import api_bp

log = logging.getLogger(__name__)


@api_bp.get("/security")
def security_state():
    session = g.session
    user = g.user
    if user is None:
        raise ApiError("Your session has expired.", status=401, code="unauthorized")
    current_prefix = util.sha256_hex(session.token)[:16]
    sessions = security.list_sessions(user.id)
    for entry in sessions:
        entry["current"] = entry["id"] == current_prefix
    config = common.cfg()
    return json_ok(
        {
            "account": {
                "username": user.username,
                "created_at": util.iso(user.created_at),
                "password_changed_at": util.iso(user.password_changed_at),
                "last_login_at": util.iso(user.last_login_at) if user.last_login_at else None,
                "hash": "Argon2id" if security_module.ARGON2_AVAILABLE else "scrypt",
            },
            "sessions": sessions,
            "audit": events.audit_entries(25),
            "policy": {
                "min_length": security.MIN_PASSWORD_LENGTH,
                "idle_minutes": config.security.session_idle_minutes,
                "absolute_hours": config.security.session_absolute_hours,
                "max_attempts": config.security.login_max_attempts,
                "window_minutes": config.security.login_window_minutes,
                "allow_public_access": config.security.allow_public_access,
                "argon2": security_module.ARGON2_AVAILABLE,
            },
        }
    )


@api_bp.post("/security/password")
def change_password():
    data = payload()
    current = str(data.get("current_password", ""))
    new = str(data.get("new_password", ""))
    confirm = str(data.get("confirm_password", ""))
    user = g.user
    if user is None:
        raise ApiError("Your session has expired.", status=401, code="unauthorized")
    if not security.verify_password(current, user.password_hash):
        events.record("security", "warning", "Password change rejected: current password incorrect")
        raise ApiError("Your current password is not correct.", code="invalid_current_password")
    if new != confirm:
        raise ApiError("The new passwords do not match.", code="password_mismatch")
    security.set_password(user.id, new)
    security.delete_user_sessions(user.id, keep_token=g.session.token)
    common.audit("password-change", detail="Administrator password changed")
    events.record("security", "success", "Administrator password changed")
    return json_ok({"changed": True})


@api_bp.post("/security/sessions/revoke")
def revoke_session():
    data = payload()
    identifier = str(data.get("id", "")).strip()
    if not identifier:
        raise ApiError("Choose a session to sign out.", code="missing_session")
    user = g.user
    if user is None:
        raise ApiError("Your session has expired.", status=401, code="unauthorized")
    current_prefix = util.sha256_hex(g.session.token)[:16]
    if identifier == current_prefix:
        raise ApiError("Use Sign out to end your current session.", code="current_session")
    if not security.revoke_session_by_prefix(user.id, identifier):
        raise ApiError("That session is no longer active.", status=404, code="not_found")
    common.audit("session-revoke", target=identifier)
    events.record("security", "info", "A dashboard session was signed out remotely")
    return json_ok({"revoked": identifier})
