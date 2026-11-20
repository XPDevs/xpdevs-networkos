"""SQLite persistence: users, sessions, devices, events and audit trail.

A single small database keeps the microSD card quiet: WAL journaling with
``synchronous=NORMAL`` batches writes, and retention limits keep the file from
growing without bound.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

from . import paths, util

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    password_changed_at INTEGER NOT NULL,
    last_login_at       INTEGER,
    is_active           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token  TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    ip          TEXT,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at INTEGER NOT NULL,
    ip           TEXT,
    username     TEXT,
    success      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_ip ON login_attempts(ip, attempted_at);

CREATE TABLE IF NOT EXISTS devices (
    mac          TEXT PRIMARY KEY,
    hostname     TEXT,
    ip           TEXT,
    first_seen   INTEGER,
    last_seen    INTEGER,
    blocked      INTEGER NOT NULL DEFAULT 0,
    blocked_at   INTEGER,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    level    TEXT NOT NULL,
    category TEXT NOT NULL,
    message  TEXT NOT NULL,
    details  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    username TEXT,
    action   TEXT NOT NULL,
    target   TEXT,
    detail   TEXT,
    ip       TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

_LOCAL = threading.local()
_INIT_LOCK = threading.Lock()
_INITIALISED = False


def connect() -> sqlite3.Connection:
    """Return a per-thread connection configured for low write amplification."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        return conn
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        paths.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise sqlite3.OperationalError(f"cannot create the state directory {paths.STATE_DIR}: {exc}") from exc
    conn = sqlite3.connect(str(paths.DB_FILE), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        os.chmod(paths.DB_FILE, 0o600)  # hashes and sessions: owner only
    except OSError:
        pass
    # The privileged helper runs as root while the dashboard runs unprivileged.
    # A root-created 0600 database made every device and event query fail for the
    # web service ("unable to open database file"), so hand the file to the
    # service account when we are root and that account exists.
    if os.geteuid() == 0:
        try:
            import pwd

            uid = pwd.getpwnam(paths.APP_SLUG).pw_uid
            os.chown(paths.DB_FILE, uid, -1)
            os.chmod(paths.DB_FILE, 0o660)
        except (KeyError, OSError):
            pass
    # Any caller may be the first to touch the database (boot unit, timer, web
    # app), so make sure the schema exists before it is queried.
    try:
        ready = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if ready is None:
            conn.executescript(SCHEMA)
    except sqlite3.Error as exc:
        log.warning("could not initialise the database schema: %s", exc)
    _LOCAL.conn = conn
    return conn


def close() -> None:
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _LOCAL.conn = None


def init() -> None:
    """Create the schema once per process."""
    global _INITIALISED
    with _INIT_LOCK:
        if _INITIALISED:
            return
        try:
            conn = connect()
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            _INITIALISED = True
        except sqlite3.Error as exc:  # pragma: no cover - fatal misconfiguration
            log.error("database initialisation failed: %s", exc)
            raise


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return list(connect().execute(sql, params).fetchall())


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def scalar(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    row = query_one(sql, params)
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    cur = connect().execute(sql, params)
    return cur.lastrowid or 0


def executemany(sql: str, rows: Iterable[Sequence[Any]]) -> None:
    connect().executemany(sql, list(rows))


# --------------------------------------------------------------------------- #
# Application settings stored in the database (small mutable preferences)
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), util.epoch_now()),
    )


def get_json_setting(key: str, default: Any = None) -> Any:
    raw = get_setting(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def set_json_setting(key: str, value: Any) -> None:
    set_setting(key, json.dumps(value, separators=(",", ":")))


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def prune(retention_days: int = 30) -> None:
    """Delete old rows so the database stays small on a microSD card."""
    cutoff = util.epoch_now() - max(1, retention_days) * 86400
    with transaction() as conn:
        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM audit WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (util.epoch_now(),))


def checkpoint() -> None:
    """Fold the WAL back into the main database file."""
    try:
        connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        log.debug("wal checkpoint failed: %s", exc)


def now() -> int:
    return int(time.time())
