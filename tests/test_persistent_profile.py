"""Persistent profile path + fixture still works; live network mocked/skipped."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "sample_listings.json"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLECTOR_BACKEND", "playwright")
    monkeypatch.setenv("GOOFISH_FIXTURE_PATH", str(FIXTURE))
    monkeypatch.setenv("GOOFISH_USER_DATA_DIR", str(tmp_path / "browser_profile"))
    monkeypatch.setenv("GOOFISH_SKIP_PROBE", "true")
    monkeypatch.delenv("GOOFISH_COOKIES", raising=False)
    monkeypatch.delenv("GOOFISH_COOKIES_FILE", raising=False)


def test_user_data_dir_from_env(tmp_path, monkeypatch):
    from app.collectors import goofish_profile as gp

    target = tmp_path / "my_profile"
    monkeypatch.setenv("GOOFISH_USER_DATA_DIR", str(target))
    assert gp.user_data_dir() == target
    p = gp.ensure_user_data_dir()
    assert p.is_dir()


def test_fixture_still_uses_playwright_collector():
    from app.collectors import SearchQuery, get_collector

    c = get_collector()
    assert c.name == "goofish_playwright"
    assert c.user_data_dir  # persistent path configured even in fixture mode
    items = asyncio.run(c.search(SearchQuery(keyword="MacBook")))
    assert len(items) == 3


def test_session_status_without_probe():
    from app.collectors.goofish_profile import session_status_summary

    summary = session_status_summary(probe=None)
    assert "user_data_dir" in summary
    assert summary["state"] in ("empty", "profile_present", "unknown", "login_required", "logged_in")


def test_probe_skipped_under_fixture():
    from app.collectors.goofish_profile import probe_session

    result = asyncio.run(probe_session())
    assert result.get("method") in ("skipped_fixture", "skipped_env")


def test_login_required_maps_to_event(monkeypatch):
    """CollectorError(login_required) should be recorded as login_required event."""
    from app.collectors.goofish import CollectorError
    from app.db import SessionLocal, init_db
    from app.models.tables import MonitorTask, ProductConfig, RunLog
    from app.services.listings import run_collect_cycle

    monkeypatch.delenv("GOOFISH_FIXTURE_PATH", raising=False)
    init_db()
    db = SessionLocal()
    try:
        cfg = ProductConfig(name="t", keyword="MacBook", enabled=True)
        db.add(cfg)
        db.flush()
        task = MonitorTask(config_id=cfg.id, interval_minutes=30, status="running")
        db.add(task)
        db.commit()
        tid = task.id

        async def boom(query):
            raise CollectorError(
                "login_required",
                "need login",
                details={"hint": "login script"},
            )

        with patch("app.collectors.get_collector") as gc:
            mock_c = MagicMock()
            mock_c.search = AsyncMock(side_effect=boom)
            gc.return_value = mock_c
            # patch where listings imports get_collector inside function — already patched module
            result = asyncio.run(run_collect_cycle(db, tid, allow_paused=False))

        assert result["ok"] is False
        assert result["error"] == "login_required"
        db.refresh(db.get(MonitorTask, tid))
        task2 = db.get(MonitorTask, tid)
        assert task2.status == "error"
        row = (
            db.query(RunLog)
            .filter(RunLog.task_id == tid, RunLog.event == "login_required")
            .order_by(RunLog.id.desc())
            .first()
        )
        assert row is not None
        assert "登录" in row.message
    finally:
        db.close()


def test_open_persistent_context_called_when_live(monkeypatch, tmp_path):
    """Live path should call launch_persistent_context (mocked, no network)."""
    monkeypatch.delenv("GOOFISH_FIXTURE_PATH", raising=False)
    monkeypatch.setenv("GOOFISH_USER_DATA_DIR", str(tmp_path / "bp"))

    from app.collectors.goofish import GoofishCollector, CollectorError
    from app.collectors import SearchQuery

    collector = GoofishCollector()

    class FakePage:
        url = "https://www.goofish.com/search?q=x"

        def set_default_timeout(self, *_a, **_k):
            pass

        def on(self, *_a, **_k):
            pass

        async def goto(self, *_a, **_k):
            pass

        def locator(self, *_a, **_k):
            loc = MagicMock()
            loc.count = AsyncMock(return_value=0)
            loc.inner_text = AsyncMock(return_value="")
            loc.first = loc
            return loc

        def get_by_text(self, *_a, **_k):
            loc = MagicMock()
            loc.count = AsyncMock(return_value=0)
            loc.first = loc
            return loc

        async def title(self):
            return "goofish"

        async def wait_for_timeout(self, *_a, **_k):
            pass

    class FakeContext:
        pages = []

        async def new_page(self):
            return FakePage()

        async def add_cookies(self, *_a, **_k):
            pass

        async def close(self):
            pass

    class FakeChromium:
        def __init__(self):
            self.called = False
            self.kwargs = None

        async def launch_persistent_context(self, *args, **kwargs):
            self.called = True
            self.kwargs = kwargs
            self.args = args
            return FakeContext()

    class FakePW:
        def __init__(self):
            self.chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake_pw = FakePW()

    with patch("playwright.async_api.async_playwright", return_value=fake_pw):
        with pytest.raises(CollectorError) as ei:
            asyncio.run(collector.search(SearchQuery(keyword="MacBook")))

    assert fake_pw.chromium.called
    assert "browser_profile" in str(fake_pw.chromium.args[0]) or str(tmp_path) in str(
        fake_pw.chromium.args[0]
    )
    # empty results or login_required both ok for this mock
    assert ei.value.code in ("empty_results", "login_required", "blocked")
