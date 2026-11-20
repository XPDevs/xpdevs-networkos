"""Password hashing, sessions, CSRF and login throttling."""

from __future__ import annotations

from networkos import db, security


def test_password_hash_roundtrip(config):
    stored = security.hash_password("a-strong-password")
    assert stored != "a-strong-password"
    assert stored.startswith(("$argon2id$", "scrypt$"))
    assert security.verify_password("a-strong-password", stored)
    assert not security.verify_password("wrong-password", stored)
    assert not security.verify_password("", "")
    # Hashing the same password twice must not produce the same string.
    assert security.hash_password("a-strong-password") != stored


def test_password_policy():
    assert security.password_problems("short") != []
    assert security.password_problems("admin") != []
    assert security.password_problems("a-perfectly-fine-passphrase") == []


def test_users_and_sessions(config):
    user = security.create_or_update_user("admin", "first-password-123")
    assert security.get_user("ADMIN").id == user.id
    assert security.user_count() == 1

    session = security.create_session(user.id, "10.42.0.20", "pytest", idle_minutes=30, absolute_hours=1)
    loaded = security.load_session(session.token, idle_minutes=30)
    assert loaded is not None and loaded.user_id == user.id
    assert security.csrf_valid(loaded, loaded.csrf_token)
    assert not security.csrf_valid(loaded, "nope")

    security.destroy_session(session.token)
    assert security.load_session(session.token) is None
    assert security.load_session("not-a-token") is None


def test_login_throttle_counts_failures(config):
    security.create_or_update_user("admin", "first-password-123")
    ip, name = "10.42.0.20", "admin"
    for _ in range(3):
        security.record_login_attempt(ip, name, False)
    assert security.recent_failures(ip, name) == 3
    state = security.throttle_state(ip, name, max_attempts=3, window_minutes=15)
    assert not state.allowed
    assert state.retry_after > 0
    security.clear_attempts(ip, name)
    assert security.throttle_state(ip, name, max_attempts=3).allowed


def test_successful_login_does_not_count_as_failure(config):
    security.create_or_update_user("admin", "first-password-123")
    security.record_login_attempt("10.42.0.30", "admin", True)
    assert security.recent_failures("10.42.0.30", "admin") == 0
