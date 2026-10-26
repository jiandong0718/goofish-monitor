"""Smoke: fixture collect cycle upserts into SQLite."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "sample_listings.json"


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("COLLECTOR_BACKEND", "playwright")
    monkeypatch.setenv("GOOFISH_FIXTURE_PATH", str(FIXTURE))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    # Rebuild engine/session bound to temp DB
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


def test_run_collect_cycle_upserts(db_session):
    from app.models.tables import Listing, MonitorTask, ProductConfig
    from app.services.listings import run_collect_cycle

    cfg = ProductConfig(
        name="MBA 联调",
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
    assert result["fetched"] == 1
    assert result["inserted"] == 1
    assert result["updated"] == 0
    assert result["errors"] == 0

    rows = db_session.query(Listing).all()
    assert len(rows) == 1
    assert rows[0].external_id == "fixture-mb-001"
    assert rows[0].content_hash
    assert rows[0].first_seen is not None
    assert rows[0].last_seen is not None

    # second run → update
    result2 = asyncio.run(run_collect_cycle(db_session, task.id, allow_paused=True))
    assert result2["ok"] is True
    assert result2["inserted"] == 0
    assert result2["updated"] == 1
    assert db_session.query(Listing).count() == 1
