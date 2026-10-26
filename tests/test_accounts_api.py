"""Accounts / settings pages and status API (no live network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_listings.json"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("COLLECTOR_BACKEND", "playwright")
    monkeypatch.setenv("GOOFISH_FIXTURE_PATH", str(FIXTURE))
    monkeypatch.setenv("GOOFISH_USER_DATA_DIR", str(tmp_path / "browser_profile"))
    monkeypatch.setenv("GOOFISH_SKIP_PROBE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")

    # Re-bind engine for this temp DB
    import app.db as dbmod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 't.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    dbmod.engine = engine
    dbmod.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    dbmod.DATABASE_URL = url
    dbmod.init_db()

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_accounts_page_not_placeholder(client):
    r = client.get("/accounts")
    assert r.status_code == 200
    assert "即将开放" not in r.text
    assert "账号管理" in r.text
    assert "login_goofish" in r.text
    assert "browser_profile" in r.text


def test_settings_page_readonly_env(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "即将开放" not in r.text
    assert "COLLECTOR_BACKEND" in r.text
    assert "GOOFISH_USER_DATA_DIR" in r.text or "browser_profile" in r.text


def test_status_api(client):
    r = client.get("/api/accounts/goofish/status?probe=false")
    assert r.status_code == 200
    data = r.json()
    assert "user_data_dir" in data
    assert data["login_script"] == "python -m app.scripts.login_goofish"


def test_open_login_instructions_without_display(client, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    # fixture mode returns fixture message; clear for this call
    monkeypatch.delenv("GOOFISH_FIXTURE_PATH", raising=False)
    r = client.post("/api/accounts/goofish/open-login")
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] in ("instructions", "fixture")
    assert "login_goofish" in data["login_script"]
