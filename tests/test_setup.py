"""First-run setup: the panel is unusable until a password has been chosen."""

from __future__ import annotations

import re

import pytest

from networkos import security

from .conftest import ADMIN, PASSWORD, login


def _token(client) -> str:
    page = client.get("/setup")
    assert page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
    assert match, "the setup page must carry a CSRF token"
    return match.group(1)


def _submit(client, token, *, username=ADMIN, password=PASSWORD, confirm=None):
    return client.post(
        "/setup",
        data={
            "csrf_token": token,
            "username": username,
            "password": password,
            "password_confirm": password if confirm is None else confirm,
        },
        follow_redirects=False,
    )


def test_setup_screen_is_the_only_reachable_page(app):
    client = app.test_client()
    for path in ("/", "/wireless", "/devices", "/system", "/settings"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/setup")
    # The sign-in page cannot be used yet either.
    assert client.get("/login").headers["Location"].endswith("/setup")
    # The liveness probe stays available for the installer.
    assert client.get("/healthz").status_code == 200


def test_api_reports_setup_required(app):
    response = app.test_client().get("/api/overview")
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "setup_required"


def test_setup_screen_renders(app):
    body = app.test_client().get("/setup").get_data(as_text=True)
    assert "Create your administrator account" in body
    assert 'name="password"' in body
    assert 'name="password_confirm"' in body
    assert "Argon2id" in body


def test_setup_rejects_a_post_without_csrf(app):
    response = _submit(app.test_client(), "")
    assert response.status_code == 400
    assert security.user_count() == 0


def test_setup_rejects_a_weak_password(app):
    client = app.test_client()
    response = _submit(client, _token(client), password="short")
    assert response.status_code == 400
    assert "at least" in response.get_data(as_text=True)
    assert security.user_count() == 0


def test_setup_rejects_a_mismatched_confirmation(app):
    client = app.test_client()
    response = _submit(client, _token(client), confirm="something-else-entirely")
    assert response.status_code == 400
    assert "do not match" in response.get_data(as_text=True)
    assert security.user_count() == 0


def test_setup_rejects_an_invalid_username(app):
    client = app.test_client()
    response = _submit(client, _token(client), username="a")
    assert response.status_code == 400
    assert security.user_count() == 0


def test_setup_creates_the_account_and_signs_in(app):
    client = app.test_client()
    response = _submit(client, _token(client))
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")

    assert security.user_count() == 1
    user = security.get_user(ADMIN)
    assert user is not None
    # The password itself is never stored, only a salted one-way hash.
    assert PASSWORD not in user.password_hash
    assert user.password_hash.startswith(("$argon2id$", "scrypt$"))

    # The session created by setup is already valid.
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "XPDevs NetworkOS" in dashboard.get_data(as_text=True)


def test_setup_closes_once_an_account_exists(app):
    client = app.test_client()
    assert _submit(client, _token(client)).status_code == 302

    anonymous = app.test_client()
    response = anonymous.get("/setup")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    # And it cannot be used to add a second account.
    assert security.user_count() == 1


def test_new_password_works_for_a_later_sign_in(app):
    client = app.test_client()
    assert _submit(client, _token(client)).status_code == 302

    fresh = app.test_client()
    assert login(fresh).status_code == 302
    assert fresh.get("/").status_code == 200
    assert login(app.test_client(), password="a-different-password").status_code == 401


def test_setup_is_limited_to_the_local_network(app):
    response = app.test_client().get("/setup", headers={"X-Forwarded-For": "8.8.8.8"})
    assert response.status_code == 403
    assert security.user_count() == 0
