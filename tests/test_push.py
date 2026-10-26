"""Push notifications: mock httpx; webhook + telegram + dedupe + evaluate hook."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    db_file = tmp_path / "test_push.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.delenv("PUSH_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("PUSH_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PUSH_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("PUSH_INCLUDE_WATCH", raising=False)
    monkeypatch.delenv("AI_API_KEY", raising=False)

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


def _seed_listing(db, *, title="MacBook Air M2 16G", price=5200.0, verdict_setup=False):
    from app.models.tables import Listing, ProductConfig

    cfg = ProductConfig(
        name="test",
        keyword="MacBook",
        price_max=6500,
        enabled=True,
    )
    db.add(cfg)
    db.flush()
    listing = Listing(
        config_id=cfg.id,
        external_id="ext-push-1",
        title=title,
        price=price,
        url="https://www.goofish.com/item?id=1",
        content_hash="abc123",
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    db.refresh(cfg)
    return cfg, listing


def _mock_httpx_ok():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"errcode": 0, "ok": True}
    return resp


def test_should_push_verdict(monkeypatch):
    from app.push.notify import should_push_verdict

    monkeypatch.delenv("PUSH_INCLUDE_WATCH", raising=False)
    assert should_push_verdict("推荐") is True
    assert should_push_verdict("待观察") is False
    assert should_push_verdict("不推荐") is False

    monkeypatch.setenv("PUSH_INCLUDE_WATCH", "true")
    assert should_push_verdict("待观察") is True


def test_push_status_no_secrets(monkeypatch):
    from app.push.notify import push_status

    monkeypatch.setenv(
        "PUSH_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRETKEY",
    )
    monkeypatch.setenv("PUSH_TELEGRAM_BOT_TOKEN", "tokensecret")
    monkeypatch.setenv("PUSH_TELEGRAM_CHAT_ID", "42")
    st = push_status()
    assert st["webhook_configured"] is True
    assert st["telegram_configured"] is True
    assert "SECRETKEY" not in str(st)
    assert "tokensecret" not in str(st)
    assert st["webhook_host"] == "qyapi.weixin.qq.com"


def test_deliver_wecom_webhook_mocked(monkeypatch):
    from app.push import notify as nmod

    monkeypatch.setenv(
        "PUSH_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
    )
    monkeypatch.delenv("PUSH_TELEGRAM_BOT_TOKEN", raising=False)

    captured = {}

    def fake_post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return _mock_httpx_ok()

    with patch("httpx.Client") as Client:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.side_effect = fake_post
        Client.return_value = client
        result = nmod.deliver_push(
            title="好价 MacBook",
            price=5000,
            reason="硬条件全过",
            url="https://www.goofish.com/item?id=9",
            verdict="推荐",
            listing_id=1,
        )

    assert result["ok"] is True
    assert "webhook" in result["channels"]
    assert captured["json"]["msgtype"] == "text"
    assert "好价 MacBook" in captured["json"]["text"]["content"]
    assert "推荐" in captured["json"]["text"]["content"]


def test_deliver_telegram_mocked(monkeypatch):
    from app.push import notify as nmod

    monkeypatch.delenv("PUSH_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("PUSH_TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("PUSH_TELEGRAM_CHAT_ID", "-1001")

    captured = {}

    def fake_post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return _mock_httpx_ok()

    with patch("httpx.Client") as Client:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.side_effect = fake_post
        Client.return_value = client
        result = nmod.deliver_push(
            title="TG item",
            price=100,
            reason="ok",
            url="https://example.com/x",
            verdict="推荐",
        )

    assert result["ok"] is True
    assert "telegram" in result["channels"]
    assert "bot123:ABC/sendMessage" in captured["url"]
    assert captured["json"]["chat_id"] == "-1001"


def test_maybe_push_skipped_not_configured(db_session, monkeypatch):
    from app.models.tables import RunLog
    from app.push.service import maybe_push_evaluation

    monkeypatch.delenv("PUSH_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("PUSH_TELEGRAM_BOT_TOKEN", raising=False)
    _, listing = _seed_listing(db_session)

    out = maybe_push_evaluation(
        db_session, listing, verdict="推荐", reason="测试"
    )
    assert out["skipped"] is True
    assert out["skip_reason"] == "not_configured"
    logs = db_session.query(RunLog).filter(RunLog.event == "push_skipped").all()
    assert len(logs) == 1
    assert logs[0].details["reason"] == "not_configured"


def test_maybe_push_sent_and_dedupe(db_session, monkeypatch):
    from app.models.tables import PushRecord, RunLog
    from app.push.service import maybe_push_evaluation

    monkeypatch.setenv(
        "PUSH_WEBHOOK_URL", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"
    )
    _, listing = _seed_listing(db_session)

    with patch("app.push.notify._post_json") as post:
        post.return_value = None
        out1 = maybe_push_evaluation(
            db_session, listing, verdict="推荐", reason="硬条件全过"
        )
        out2 = maybe_push_evaluation(
            db_session, listing, verdict="推荐", reason="硬条件全过"
        )

    assert out1["pushed"] is True
    assert "webhook" in out1["channels"]
    assert out2["skipped"] is True
    assert out2["skip_reason"] == "already_pushed"
    assert post.call_count == 1

    assert db_session.query(PushRecord).count() == 1
    sent = db_session.query(RunLog).filter(RunLog.event == "push_sent").count()
    skipped = (
        db_session.query(RunLog)
        .filter(RunLog.event == "push_skipped")
        .filter(RunLog.details["reason"].as_string() == "already_pushed")
        .count()
        if False
        else 0
    )
    # SQLite JSON filter varies — count skipped events instead
    skipped_rows = [
        r
        for r in db_session.query(RunLog).filter(RunLog.event == "push_skipped").all()
        if (r.details or {}).get("reason") == "already_pushed"
    ]
    assert sent == 1
    assert len(skipped_rows) == 1


def test_evaluate_batch_triggers_push(db_session, monkeypatch):
    """New 推荐 evaluation in evaluate_listings_for_config → push_sent."""
    from app.models.tables import Listing, ProductConfig, RunLog
    from app.services.evaluate import evaluate_listings_for_config

    monkeypatch.setenv(
        "PUSH_WEBHOOK_URL", "https://example.test/hook"
    )
    # Loose rules: no price_max hard fail
    cfg = ProductConfig(name="cfg", keyword="MacBook", enabled=True)
    db_session.add(cfg)
    db_session.flush()
    listing = Listing(
        config_id=cfg.id,
        external_id="e-eval-push",
        title="MacBook Pro 14 个人闲置",
        price=5500,
        url="https://www.goofish.com/item?id=99",
        content_hash="hash-eval-1",
        seller_type="personal",
    )
    db_session.add(listing)
    db_session.commit()

    with patch("app.push.notify._post_json") as post:
        post.return_value = None
        counts = evaluate_listings_for_config(
            db_session, [listing.id], cfg, force=False, task_id=None
        )

    assert counts["evaluated"] >= 1
    assert counts.get("pushed", 0) >= 1
    assert post.called
    sent = db_session.query(RunLog).filter(RunLog.event == "push_sent").count()
    assert sent >= 1


def test_settings_page_shows_push_status(db_session, monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv(
        "PUSH_WEBHOOK_URL", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET"
    )
    monkeypatch.delenv("PUSH_TELEGRAM_BOT_TOKEN", raising=False)

    from fastapi.testclient import TestClient
    import app.db as dbmod
    from app.main import app

    # ensure app uses our test session factory (already patched by fixture)
    with TestClient(app) as client:
        r = client.get("/settings")
    assert r.status_code == 200
    body = r.text
    assert "PUSH_WEBHOOK_URL" in body
    assert "已配置" in body
    assert "SECRET" not in body
    assert "PUSH_TELEGRAM" in body
    assert "PUSH_INCLUDE_WATCH" in body
