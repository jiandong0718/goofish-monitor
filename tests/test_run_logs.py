"""Smoke: run logs written by collect cycle + GET /api/logs + /logs page."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "sample_listings.json"


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    db_file = tmp_path / "test_logs.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("COLLECTOR_BACKEND", "playwright")
    monkeypatch.setenv("GOOFISH_FIXTURE_PATH", str(FIXTURE))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    import app.db as dbmod
    from app.db import Base
    from app.models import tables  # noqa: F401

    engine = create_engine(url, connect_args={"check_same_thread": False})
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    dbmod.engine = engine
    dbmod.SessionLocal = TestingSession
    dbmod.DATABASE_URL = url

    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_write_run_log_unit(db_session):
    from app.models.tables import RunLog
    from app.services.run_logs import write_run_log

    row = write_run_log(
        db_session,
        level="info",
        event="collect_start",
        message="单元写入测试",
        task_id=None,
        details={"ok": True},
    )
    assert row.id is not None
    found = db_session.query(RunLog).filter(RunLog.id == row.id).one()
    assert found.event == "collect_start"
    assert found.message == "单元写入测试"


def test_collect_cycle_writes_logs(db_session):
    from app.models.tables import MonitorTask, ProductConfig, RunLog
    from app.services.listings import run_collect_cycle

    cfg = ProductConfig(
        name="日志联调",
        keyword="MacBook",
        price_max=6000,
        exclude_keywords=["碎屏"],
        sort_default="newest",
        enabled=True,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    task = MonitorTask(config_id=cfg.id, interval_minutes=30, status="paused")
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    result = asyncio.run(run_collect_cycle(db_session, task.id, allow_paused=True))
    assert result["ok"] is True

    events = [
        r.event
        for r in db_session.query(RunLog).order_by(RunLog.id.asc()).all()
    ]
    assert "collect_start" in events
    assert "collect_done" in events
    assert "evaluate_done" in events


def test_api_and_page_logs(db_session, monkeypatch):
    """GET /api/logs and GET /logs return 200 with rows after a collect."""
    from app.models.tables import MonitorTask, ProductConfig
    from app.services.listings import run_collect_cycle

    cfg = ProductConfig(
        name="API 日志",
        keyword="MacBook",
        price_max=6000,
        enabled=True,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)
    task = MonitorTask(config_id=cfg.id, interval_minutes=30, status="paused")
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    result = asyncio.run(run_collect_cycle(db_session, task.id, allow_paused=True))
    assert result["ok"] is True

    # Import app after DB monkeypatch so lifespan uses temp DB
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/logs?limit=20")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert any(row["event"] == "collect_done" for row in data)

        r2 = client.get(f"/api/logs?task_id={task.id}&level=info")
        assert r2.status_code == 200
        assert len(r2.json()) >= 1

        page = client.get("/logs")
        assert page.status_code == 200
        assert "运行日志" in page.text
        assert "collect_done" in page.text or "采集完成" in page.text
