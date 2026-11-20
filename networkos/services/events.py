"""System event log and administrator audit trail."""

from __future__ import annotations

import json
import logging
from typing import Any

from .. import db, paths, util

log = logging.getLogger("networkos.events")
audit_log = logging.getLogger("networkos.audit")

LEVELS = {"debug", "info", "success", "warning", "error", "critical"}
CATEGORIES = {"system", "network", "wireless", "devices", "adblock", "security", "install", "update"}


def record(
    category: str,
    level: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    persist: bool = True,
) -> None:
    """Write an event to the database, the log file and journald."""
    category = category if category in CATEGORIES else "system"
    level = level if level in LEVELS else "info"
    message = str(message)[:500]
    payload = json.dumps(details, separators=(",", ":"))[:2000] if details else None
    if persist:
        try:
            db.execute(
                "INSERT INTO events(ts, level, category, message, details) VALUES(?,?,?,?,?)",
                (util.epoch_now(), level, category, message, payload),
            )
        except Exception as exc:  # never let logging break a request
            log.warning("could not persist event: %s", exc)
    log.log(
        {"debug": 10, "info": 20, "success": 20, "warning": 30, "error": 40, "critical": 50}[level],
        message,
        extra={"event": category, "category": category, "detail": payload},
    )


def recent(limit: int = 20, category: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    if category:
        rows = db.query(
            "SELECT id, ts, level, category, message FROM events WHERE category = ? ORDER BY id DESC LIMIT ?",
            (category, limit),
        )
    else:
        rows = db.query(
            "SELECT id, ts, level, category, message FROM events ORDER BY id DESC LIMIT ?", (limit,)
        )
    return [
        {
            "id": row["id"],
            "at": util.iso(row["ts"]),
            "level": row["level"],
            "category": row["category"],
            "message": row["message"],
        }
        for row in rows
    ]


def audit(username: str, action: str, *, target: str = "", detail: str = "", ip: str = "") -> None:
    """Record a state-changing administrator action."""
    clean_detail = str(detail)[:500]
    try:
        db.execute(
            "INSERT INTO audit(ts, username, action, target, detail, ip) VALUES(?,?,?,?,?,?)",
            (util.epoch_now(), username or "unknown", action[:80], target[:120], clean_detail, ip[:64]),
        )
    except Exception as exc:
        log.warning("could not persist audit entry: %s", exc)
    audit_log.info(
        "%s %s %s",
        username or "unknown",
        action,
        clean_detail,
        extra={"event": "audit", "category": "security", "detail": clean_detail},
    )


def audit_entries(limit: int = 50) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 200)),))
    return [
        {
            "at": util.iso(row["ts"]),
            "username": row["username"],
            "action": row["action"],
            "target": row["target"],
            "detail": row["detail"],
            "ip": row["ip"],
        }
        for row in rows
    ]


def disk_usage() -> dict[str, Any]:
    """Number of events retained (helps the Settings page explain retention)."""
    count = db.scalar("SELECT COUNT(*) FROM events", default=0) or 0
    oldest = db.scalar("SELECT MIN(ts) FROM events")
    return {"count": int(count), "oldest": util.iso(oldest) if oldest else None}
