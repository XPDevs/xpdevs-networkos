"""Test fixtures for XPDevs NetworkOS.

The whole suite runs against a throw-away filesystem: configuration, SQLite
database and logs live in a temporary directory, so nothing on the host is
touched and no root privileges are required.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_BASE = Path(tempfile.mkdtemp(prefix="networkos-tests-"))
for _sub in ("etc", "etc/generated", "etc/backups", "etc/secrets", "state", "log"):
    (_BASE / _sub).mkdir(parents=True, exist_ok=True)

os.environ["NETWORKOS_ETC"] = str(_BASE / "etc")
os.environ["NETWORKOS_CONFIG"] = str(_BASE / "etc" / "config.toml")
os.environ["NETWORKOS_STATE"] = str(_BASE / "state")
os.environ["NETWORKOS_LOG"] = str(_BASE / "log")
os.environ["NETWORKOS_DB"] = str(_BASE / "state" / "networkos.db")
os.environ["NETWORKOS_ROOT"] = str(ROOT)
os.environ["NETWORKOS_DEV_CTL"] = "0"
os.environ.setdefault("TZ", "UTC")

from networkos import db, paths, security, settings, util  # noqa: E402

ADMIN = "admin"
PASSWORD = "test-only-passphrase"


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_BASE, ignore_errors=True)


@pytest.fixture
def config():
    cfg = settings.Config()
    cfg.wifi.ssid = "XPDevs Testbed"
    cfg.wifi.psk = util.wpa_psk(cfg.wifi.ssid, "test-passphrase-1")
    cfg.wifi.channel = 6
    cfg.wifi.country = "GB"
    cfg.network.wan_interface = "eth0"
    cfg.network.lan_interface = "wlan0"
    cfg.security.bind_port = 8080
    cfg.security.bind_host = "127.0.0.1"
    cfg.security.allow_public_access = False
    settings.save(cfg, path=paths.CONFIG_FILE)
    settings.invalidate_cache()
    # Start every test from an empty database: close the thread-local handle
    # first so the file is really replaced rather than shadowed by an open inode.
    db.close()
    db._INITIALISED = False
    try:
        paths.DB_FILE.unlink()
    except FileNotFoundError:
        pass
    db.init()
    return cfg


@pytest.fixture
def app(config):
    from networkos.web import create_app

    application = create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def admin_account(config):
    """An installation that has already been through first-run setup."""
    security.create_or_update_user(ADMIN, PASSWORD)
    return ADMIN


@pytest.fixture
def anon(app, admin_account):
    return app.test_client()


def login(client, password: str = PASSWORD, username: str = ADMIN):
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
    assert match, "login page must carry a CSRF token"
    return client.post(
        "/login",
        data={"csrf_token": match.group(1), "username": username, "password": password},
        follow_redirects=False,
    )


@pytest.fixture
def client(app, admin_account):
    test_client = app.test_client()
    assert login(test_client).status_code == 302
    return test_client


def csrf_token(client) -> str:
    html = client.get("/").get_data(as_text=True)
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert match, "dashboard pages must expose the session CSRF token"
    return match.group(1)
