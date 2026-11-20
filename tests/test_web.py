"""End-to-end checks for the dashboard: pages, auth, CSRF and headers."""

from __future__ import annotations

import pytest

from networkos import paths, settings

from .conftest import ADMIN, PASSWORD, csrf_token, login

PAGES = ["/", "/wireless", "/devices", "/adblock", "/network", "/security", "/system", "/settings"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "XPDevs NetworkOS" in body
    assert 'name="csrf-token"' in body
    assert "Exception" not in body


def test_static_assets_are_served(client):
    for asset in ("/static/css/app.css", "/static/js/core.js", "/static/img/favicon.svg"):
        assert client.get(asset).status_code == 200


def test_health_probe_is_public(anon):
    response = anon.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_pages_require_a_session(anon):
    response = anon.get("/devices")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_api_requires_a_session(anon):
    response = anon.get("/api/overview")
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "unauthorized"


def test_api_rejects_a_missing_csrf_token(client):
    response = client.post("/api/wireless/restart", json={})
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "csrf"


def test_api_accepts_the_session_csrf_token(client):
    token = csrf_token(client)
    # The restart itself fails without root, but the request must pass the guards.
    response = client.post("/api/wireless/restart", json={}, headers={"X-CSRF-Token": token})
    assert response.status_code != 403


def test_cross_origin_requests_are_rejected(client):
    token = csrf_token(client)
    response = client.post(
        "/api/wireless/restart",
        json={},
        headers={"X-CSRF-Token": token, "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "cross_origin"


def test_destructive_actions_need_the_confirmation_phrase(client):
    token = csrf_token(client)
    response = client.post(
        "/api/system/action",
        json={"action": "reboot"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "confirmation_required"


def test_network_endpoint_returns_the_lan_plan(client):
    payload = client.get("/api/network").get_json()["data"]
    assert payload["config"]["lan_address"] == "10.42.0.1"
    assert payload["config"]["dns_servers"] == ["10.42.0.1"]


def test_security_headers_are_present(client):
    headers = client.get("/").headers
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"].startswith("no-store")


def test_public_requests_are_refused_when_lan_only(app):
    from networkos.web import create_app  # noqa: F401

    public = app.test_client()
    response = public.get("/login", headers={"X-Forwarded-For": "8.8.8.8"})
    assert response.status_code == 403
    assert "only reachable from your own network" in response.get_data(as_text=True)


def test_login_rejects_a_bad_password(app, config):
    from networkos import security

    security.create_or_update_user(ADMIN, PASSWORD)
    anonymous = app.test_client()
    response = login(anonymous, password="not-the-password")
    assert response.status_code == 401
    assert "Incorrect username or password" in response.get_data(as_text=True)


def test_login_rate_limit_blocks_after_repeated_failures(app, config):
    from networkos import security

    security.create_or_update_user(ADMIN, PASSWORD)
    anonymous = app.test_client()
    statuses = [login(anonymous, password="wrong-password").status_code for _ in range(6)]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429


def test_logout_ends_the_session(client):
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert client.get("/").status_code == 302
