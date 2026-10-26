"""Rule-based evaluation: MacBook config → 超预算 / 推荐."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "sample_listings.json"


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    db_file = tmp_path / "test_rules.db"
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


def _macbook_pro_config(**overrides):
    """Sample: budget 6500, 14寸 M2 Pro."""
    from app.models.tables import ProductConfig

    data = dict(
        name="MacBook Pro 14 M2",
        keyword="MacBook Pro M2",
        model="M2 Pro",
        size="14寸",
        price_max=6500,
        price_min=4000,
        ram="16G",
        storage="512G",
        seller_preference="personal_only",
        exclude_keywords=["碎屏", "进水", "维修机"],
        enabled=True,
    )
    data.update(overrides)
    return ProductConfig(**data)


def test_over_budget_reject():
    from app.analyzers.rules import evaluate_rules

    cfg = {
        "name": "MBP",
        "keyword": "MacBook Pro",
        "model": "M2 Pro",
        "size": "14寸",
        "price_max": 6500,
        "seller_preference": "any",
        "exclude_keywords": [],
    }
    listing = {
        "title": "MacBook Pro 14寸 M2 Pro 16G 512G 成色好",
        "price": 8900,
        "seller_type": "personal",
    }
    result = evaluate_rules(listing, cfg)
    assert result["verdict"] == "不推荐"
    assert "超预算" in result["tags"]
    assert result["passed_hard"] is False
    assert "预算" in result["reason"] or "超" in result["reason"]


def test_matching_item_recommend():
    from app.analyzers.rules import evaluate_rules

    cfg = {
        "keyword": "MacBook Pro",
        "model": "M2 Pro",
        "size": "14寸",
        "price_max": 6500,
        "price_min": 4000,
        "ram": "16G",
        "seller_preference": "personal_only",
        "exclude_keywords": ["碎屏"],
    }
    listing = {
        "title": "个人出 MacBook Pro 14寸 M2 Pro 16G 512G 95新",
        "price": 6200,
        "seller_type": "personal",
        "condition": "95新",
    }
    result = evaluate_rules(listing, cfg)
    assert result["verdict"] == "推荐"
    assert result["passed_hard"] is True
    assert result["score"] is not None and result["score"] >= 0.8


def test_exclude_keyword_reject():
    from app.analyzers.rules import evaluate_rules

    cfg = {
        "price_max": 6500,
        "exclude_keywords": ["碎屏", "维修机"],
    }
    listing = {
        "title": "MacBook Air M1 碎屏维修机低价出",
        "price": 2800,
    }
    result = evaluate_rules(listing, cfg)
    assert result["verdict"] == "不推荐"
    assert "命中排除词" in result["tags"]


def test_size_mismatch_reject():
    from app.analyzers.rules import evaluate_rules

    cfg = {"model": "M2", "size": "14寸", "price_max": 10000}
    listing = {
        "title": "苹果 MacBook Pro M2 16寸 32G 1T 成色好",
        "price": 8900,
    }
    result = evaluate_rules(listing, cfg)
    assert result["verdict"] == "不推荐"
    assert "尺寸不符" in result["tags"]


def test_soft_miss_watch():
    from app.analyzers.rules import evaluate_rules

    cfg = {
        "model": "M2",
        "price_max": 7000,
        "ram": "32G",  # soft: listing has 16G
    }
    listing = {
        "title": "MacBook Air M2 16G 512G 午夜色",
        "price": 5200,
        "seller_type": "personal",
    }
    result = evaluate_rules(listing, cfg)
    assert result["verdict"] == "待观察"
    assert result["passed_hard"] is True
    assert "内存不符" in result["tags"]


def test_persist_and_skip_unchanged(db_session):
    from app.models.tables import Evaluation, Listing
    from app.services.evaluate import evaluate_and_persist

    cfg = _macbook_pro_config(model="M2", size=None, ram=None, storage=None)
    # Relax for fixture-mb-001 style air
    cfg.model = "M2"
    cfg.size = None
    cfg.price_max = 6500
    cfg.seller_preference = "any"
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    listing = Listing(
        config_id=cfg.id,
        external_id="eval-test-001",
        title="MacBook Air M2 16G 512G 个人闲置",
        price=5200,
        seller_type="personal",
        content_hash="abc123hash",
    )
    db_session.add(listing)
    db_session.commit()
    db_session.refresh(listing)

    row1, created1 = evaluate_and_persist(db_session, listing, cfg, force=False)
    assert created1 is True
    assert row1.verdict == "推荐"
    assert row1.analyzer == "rules"
    assert row1.config_id == cfg.id
    assert "超预算" not in (row1.tags or [])

    n1 = db_session.query(Evaluation).count()
    row2, created2 = evaluate_and_persist(db_session, listing, cfg, force=False)
    assert created2 is False
    assert row2.id == row1.id
    assert db_session.query(Evaluation).count() == n1

    # content change → re-eval
    listing.content_hash = "changed999"
    listing.price = 9000
    db_session.commit()
    row3, created3 = evaluate_and_persist(db_session, listing, cfg, force=False)
    assert created3 is True
    assert row3.verdict == "不推荐"
    assert "超预算" in (row3.tags or [])


def test_collect_cycle_writes_evaluations(db_session):
    from app.models.tables import Evaluation, MonitorTask, ProductConfig
    from app.services.listings import run_collect_cycle

    # price_max 6000 → fixture only returns mb-001; should be 推荐
    cfg = ProductConfig(
        name="MBA",
        keyword="MacBook",
        model="M2",
        price_max=6000,
        exclude_keywords=["碎屏"],
        seller_preference="any",
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
    assert result["inserted"] == 1
    assert result.get("evaluated", 0) == 1

    evs = db_session.query(Evaluation).all()
    assert len(evs) == 1
    assert evs[0].verdict == "推荐"
    assert evs[0].analyzer == "rules"

    # second cycle: content unchanged → skip
    result2 = asyncio.run(run_collect_cycle(db_session, task.id, allow_paused=True))
    assert result2["ok"] is True
    assert result2.get("eval_skipped", 0) == 1
    assert result2.get("evaluated", 0) == 0
    assert db_session.query(Evaluation).count() == 1
