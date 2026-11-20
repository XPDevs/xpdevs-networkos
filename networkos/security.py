"""Authentication primitives: password hashing, sessions, CSRF and throttling.

Passwords are hashed with Argon2id when the ``argon2-cffi`` module is present
(the installer installs it); otherwise a memory-hard scrypt hash from the
standard library is used, so the product never falls back to a weak digest.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import db, util
from .errors import ApiError, RateLimitError

log = logging.getLogger(__name__)

COOKIE_NAME = "nwos_session"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

try:  # pragma: no cover - depends on the installed wheel
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    # 32 MiB / 3 passes keeps login around 0.4 s on a Pi 3 while staying well
    # above OWASP's Argon2id minimums.
    _PH = PasswordHasher(time_cost=3, memory_cost=32768, parallelism=2, hash_len=32, salt_len=16)
    ARGON2_AVAILABLE = True
except Exception:  # pragma: no cover
    _PH = None
    ARGON2_AVAILABLE = False
    InvalidHashError = VerificationError = VerifyMismatchError = Exception  # type: ignore[assignment]

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1

COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "letmein", "welcome1", "admin123", "raspberry", "raspberrypi",
    "networkos", "xpdevs", "changeme", "iloveyou", "sunshine", "football",
}

MIN_PASSWORD_LENGTH = 10


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    if _PH is not None:
        return _PH.hash(password)
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(derived).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("$argon2") and _PH is not None:
        try:
            return bool(_PH.verify(stored, password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        except Exception:  # pragma: no cover
            return False
    if stored.startswith("scrypt$"):
        try:
            _, n, r, p, salt_b64, hash_b64 = stored.split("$", 5)
            derived = hashlib.scrypt(
                password.encode("utf-8"),
                salt=base64.b64decode(salt_b64),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(base64.b64decode(hash_b64)),
                maxmem=128 * 1024 * 1024,
            )
            return hmac.compare_digest(derived, base64.b64decode(hash_b64))
        except (ValueError, TypeError):
            return False
    return False


def password_problems(password: str, username: str = "") -> list[str]:
    """Return human readable policy violations (empty list means acceptable)."""
    problems: list[str] = []
    if len(password or "") < MIN_PASSWORD_LENGTH:
        problems.append(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password or "") > 128:
        problems.append("Use 128 characters or fewer.")
    lowered = (password or "").lower()
    if username and lowered == username.lower():
        problems.append("The password must not be the same as the username.")
    if lowered in COMMON_PASSWORDS:
        problems.append("That password is too common; choose something unique.")
    if len(set(password or "")) < 4:
        problems.append("Use a password with more variety.")
    return problems


def validate_password(password: str, username: str = "") -> str:
    problems = password_problems(password, username)
    if problems:
        raise ApiError(" ".join(problems), status=400, code="weak_password")
    return password


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
@dataclass
class User:
    id: int
    username: str
    password_hash: str
    created_at: int
    password_changed_at: int
    last_login_at: int | None
    is_active: int


def _user_from_row(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        created_at=row["created_at"],
        password_changed_at=row["password_changed_at"],
        last_login_at=row["last_login_at"],
        is_active=row["is_active"],
    )


def get_user(username: str) -> User | None:
    row = db.query_one("SELECT * FROM users WHERE username = ?", (str(username).lower().strip(),))
    return _user_from_row(row) if row else None


def get_user_by_id(user_id: int) -> User | None:
    row = db.query_one("SELECT * FROM users WHERE id = ?", (int(user_id),))
    return _user_from_row(row) if row else None


def user_count() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM users", default=0) or 0)


def create_or_update_user(username: str, password: str, *, update_password: bool = True) -> User:
    """Create the administrator (idempotent for the installer)."""
    clean = str(username or "").strip().lower()
    if not clean:
        raise ApiError("Choose an administrator username.", code="invalid_username")
    if not (3 <= len(clean) <= 32) or not all(ch.isalnum() or ch in "-_." for ch in clean):
        raise ApiError(
            "Usernames use 3-32 letters, digits, dots, hyphens or underscores.",
            code="invalid_username",
        )
    validate_password(password, clean)
    digest = hash_password(password)
    stamp = util.epoch_now()
    existing = get_user(clean)
    if existing:
        if update_password:
            db.execute(
                "UPDATE users SET password_hash = ?, password_changed_at = ?, is_active = 1 WHERE id = ?",
                (digest, stamp, existing.id),
            )
            delete_user_sessions(existing.id, keep_token=None)
        return get_user(clean)  # type: ignore[return-value]
    db.execute(
        "INSERT INTO users(username, password_hash, created_at, password_changed_at) VALUES(?,?,?,?)",
        (clean, digest, stamp, stamp),
    )
    return get_user(clean)  # type: ignore[return-value]


def set_password(user_id: int, password: str) -> None:
    user = get_user_by_id(user_id)
    if not user:
        raise ApiError("Account not found.", status=404, code="not_found")
    validate_password(password, user.username)
    db.execute(
        "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
        (hash_password(password), util.epoch_now(), user_id),
    )


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    token: str
    csrf_token: str
    user_id: int
    username: str
    created_at: int
    last_seen: int
    expires_at: int
    ip: str
    user_agent: str


def _session_row_to_session(row: sqlite3.Row, token: str) -> Session:
    return Session(
        token=token,
        csrf_token=row["csrf_token"],
        user_id=row["user_id"],
        username=row["username"],
        created_at=row["created_at"],
        last_seen=row["last_seen"],
        expires_at=row["expires_at"],
        ip=row["ip"] or "",
        user_agent=row["user_agent"] or "",
    )


def create_session(user_id: int, ip: str, user_agent: str, *, idle_minutes: int = 120, absolute_hours: int = 24) -> Session:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    stamp = util.epoch_now()
    expires = stamp + max(600, int(absolute_hours) * 3600)
    db.execute(
        "INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, last_seen, expires_at, ip, user_agent) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (util.sha256_hex(token), user_id, csrf, stamp, stamp, expires, ip[:64], user_agent[:200]),
    )
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (stamp, user_id))
    return Session(token, csrf, user_id, "", stamp, stamp, expires, ip, user_agent)


def load_session(token: str | None, *, idle_minutes: int = 120) -> Session | None:
    if not token:
        return None
    token_hash = util.sha256_hex(token)
    row = db.query_one(
        "SELECT s.*, u.username, u.is_active FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ?",
        (token_hash,),
    )
    if not row:
        return None
    stamp = util.epoch_now()
    if row["expires_at"] <= stamp or not row["is_active"]:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        return None
    idle_limit = stamp - max(300, int(idle_minutes) * 60)
    if row["last_seen"] < idle_limit:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        return None
    # Refresh at most once a minute to keep the microSD card quiet.
    if stamp - row["last_seen"] >= 60:
        db.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (stamp, token_hash))
    return _session_row_to_session(row, token)


def destroy_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (util.sha256_hex(token),))


def delete_user_sessions(user_id: int, keep_token: str | None = None) -> None:
    if keep_token:
        db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, util.sha256_hex(keep_token)),
        )
    else:
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def list_sessions(user_id: int) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM sessions WHERE user_id = ? AND expires_at > ? ORDER BY last_seen DESC",
        (user_id, util.epoch_now()),
    )
    return [
        {
            "id": util.sha256_hex(row["token_hash"])[:16],
            "created_at": util.iso(row["created_at"]),
            "last_seen": util.iso(row["last_seen"]),
            "expires_at": util.iso(row["expires_at"]),
            "ip": row["ip"],
            "user_agent": row["user_agent"],
        }
        for row in rows
    ]


def revoke_session_by_prefix(user_id: int, prefix: str) -> bool:
    prefix = str(prefix or "")[:16]
    if not prefix:
        return False
    rows = db.query("SELECT token_hash FROM sessions WHERE user_id = ?", (user_id,))
    for row in rows:
        if util.sha256_hex(row["token_hash"])[: len(prefix)] == prefix:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
            return True
    return False


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
def csrf_valid(session: Session | None, supplied: str | None) -> bool:
    if not session or not supplied:
        return False
    return hmac.compare_digest(str(session.csrf_token), str(supplied))


# --------------------------------------------------------------------------- #
# Login throttling
# --------------------------------------------------------------------------- #
@dataclass
class ThrottleState:
    allowed: bool
    retry_after: int = 0
    failures: int = 0
    max_attempts: int = 5


def record_login_attempt(ip: str, username: str, success: bool) -> None:
    db.execute(
        "INSERT INTO login_attempts(attempted_at, ip, username, success) VALUES(?,?,?,?)",
        (util.epoch_now(), str(ip)[:64], str(username)[:64].lower(), 1 if success else 0),
    )


def throttle_state(ip: str, username: str, *, max_attempts: int = 5, window_minutes: int = 15) -> ThrottleState:
    window_start = util.epoch_now() - max(60, int(window_minutes) * 60)
    rows = db.query(
        "SELECT attempted_at, success FROM login_attempts "
        "WHERE attempted_at >= ? AND (ip = ? OR username = ?) ORDER BY attempted_at DESC",
        (window_start, str(ip)[:64], str(username)[:64].lower()),
    )
    failures = sum(1 for row in rows if not row["success"])
    if failures < max_attempts:
        return ThrottleState(True, 0, failures, max_attempts)
    last_failure = next((row["attempted_at"] for row in rows if not row["success"]), window_start)
    over = failures - max_attempts
    lockout = min(3600, 60 * (2 ** min(over, 6)))
    retry_after = max(0, int(last_failure + lockout - util.epoch_now()))
    return ThrottleState(retry_after <= 0, retry_after, failures, max_attempts)


def enforce_throttle(ip: str, username: str, *, max_attempts: int = 5, window_minutes: int = 15) -> None:
    state = throttle_state(ip, username, max_attempts=max_attempts, window_minutes=window_minutes)
    if not state.allowed:
        minutes = max(1, round(state.retry_after / 60))
        raise RateLimitError(
            f"Too many failed sign-in attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."
        )


def clear_attempts(ip: str, username: str) -> None:
    db.execute("DELETE FROM login_attempts WHERE ip = ? OR username = ?", (str(ip)[:64], str(username)[:64].lower()))


def recent_failures(ip: str, username: str, window_minutes: int = 15) -> int:
    return throttle_state(ip, username, window_minutes=window_minutes).failures
