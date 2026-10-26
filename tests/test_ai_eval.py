"""AI analyzer: mock httpx; no real API key required."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    db_file = tmp_path / "test_ai.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("AI_ALWAYS", raising=False)

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


def _mock_chat_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            }
        ]
    }
    return resp


def test_should_run_ai_gates(monkeypatch):
    from app.analyzers.ai import should_run_ai

    monkeypatch.delenv("AI_API_KEY", raising=False)
    assert should_run_ai("推荐") is False

    monkeypatch.setenv("AI_API_KEY", "sk-test")
    monkeypatch.delenv("AI_ALWAYS", raising=False)
    assert should_run_ai("推荐") is True
    assert should_run_ai("待观察") is True
    assert should_run_ai("不推荐") is False

    monkeypatch.setenv("AI_ALWAYS", "true")
    assert should_run_ai("不推荐") is True


def test_evaluate_ai_no_key_returns_none(monkeypatch, caplog):
    from app.analyzers import ai as aimod

    monkeypatch.delenv("AI_API_KEY", raising=False)
    with caplog.at_level("INFO"):
        result = aimod.evaluate_ai(
            {"title": "x", "price": 1},
            {"price_max": 10},
            rules_result={"verdict": "推荐", "tags": [], "reason": "ok"},
        )
    assert result is None
    assert any("AI_API_KEY" in r.message for r in caplog.records)


def test_evaluate_ai_parses_mock_httpx(monkeypatch):
    from app.analyzers import ai as aimod

    monkeypatch.setenv("AI_API_KEY", "sk-test")
    monkeypatch.setenv("AI_API_BASE", "https://example.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")

    ai_payload = {
        "verdict_adjust": None,
        "tags": ["个人闲置", "价格合理"],
        "reason": "标题像个人出闲置，价格在预算内，建议关注。",
        "seller_profile": "个人",
    }
    mock_resp = _mock_chat_response(ai_payload)

    with patch("app.analyzers.ai.httpx.Client") as ClientCls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.return_value = mock_resp
        ClientCls.return_value = client

        result = aimod.evaluate_ai(
            {
                "title": "个人出 MacBook Air M2 16G 512G",
                "price": 5200,
                "seller_type": "personal",
                "raw": {"desc": "自用一年"},
            },
            {
                "model": "M2",
                "price_max": 6500,
                "seller_preference": "personal_only",
            },
            rules_result={
                "verdict": "推荐",
                "tags": ["硬条件通过"],
                "reason": "符合监控条件",
                "passed_hard": True,
                "soft_score": 1.0,
                "score": 0.95,
            },
        )

    assert result is not None
    assert result["analyzer"] == "ai"
    assert result["seller_profile"] == "个人"
    assert result["verdict"] == "推荐"
    assert "个人闲置" in result["tags"]
    assert "建议关注" in result["reason"] or "个人" in result["reason"]

    # verify OpenAI-compatible path + auth
    args, kwargs = client.post.call_args
    assert args[0].endswith("/chat/completions")
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    body = kwargs["json"]
    assert body["model"] == "test-model"
    assert body["response_format"] == {"type": "json_object"}


def test_persist_rules_and_ai(db_session, monkeypatch):
    from app.models.tables import Evaluation, Listing, ProductConfig
    from app.services.evaluate import evaluate_and_persist

    monkeypatch.setenv("AI_API_KEY", "sk-test")
    monkeypatch.setenv("AI_API_BASE", "https://example.test/v1")
    monkeypatch.delenv("AI_ALWAYS", raising=False)

    cfg = ProductConfig(
        name="MBA",
        keyword="MacBook",
        model="M2",
        price_max=6500,
        seller_preference="any",
        enabled=True,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    listing = Listing(
        config_id=cfg.id,
        external_id="ai-test-001",
        title="MacBook Air M2 16G 512G 个人闲置",
        price=5200,
        seller_type="personal",
        content_hash="hash-ai-1",
    )
    db_session.add(listing)
    db_session.commit()
    db_session.refresh(listing)

    ai_payload = {
        "verdict_adjust": "待观察",
        "tags": ["疑似商家话术"],
        "reason": "标题偏营销口吻，建议再核验卖家信用后再决定。",
        "seller_profile": "疑似商家",
    }
    mock_resp = _mock_chat_response(ai_payload)

    with patch("app.analyzers.ai.httpx.Client") as ClientCls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.return_value = mock_resp
        ClientCls.return_value = client

        row, created = evaluate_and_persist(db_session, listing, cfg, force=False)

    assert created is True
    assert row.analyzer == "rules"
    assert row.verdict == "推荐"

    evs = (
        db_session.query(Evaluation)
        .filter(Evaluation.listing_id == listing.id)
        .order_by(Evaluation.id.asc())
        .all()
    )
    assert len(evs) == 2
    assert evs[0].analyzer == "rules"
    assert evs[1].analyzer == "ai"
    assert evs[1].verdict == "待观察"
    assert evs[1].details.get("seller_profile") == "疑似商家"
    assert "核验" in (evs[1].reason or "")


def test_hard_fail_skips_ai_without_always(db_session, monkeypatch):
    from app.models.tables import Evaluation, Listing, ProductConfig
    from app.services.evaluate import evaluate_and_persist

    monkeypatch.setenv("AI_API_KEY", "sk-test")
    monkeypatch.delenv("AI_ALWAYS", raising=False)

    cfg = ProductConfig(
        name="MBA",
        keyword="MacBook",
        price_max=3000,  # force 超预算
        enabled=True,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    listing = Listing(
        config_id=cfg.id,
        external_id="ai-test-reject",
        title="MacBook Air M2 成色好",
        price=5200,
        content_hash="hash-rej",
    )
    db_session.add(listing)
    db_session.commit()
    db_session.refresh(listing)

    with patch("app.analyzers.ai.httpx.Client") as ClientCls:
        row, created = evaluate_and_persist(db_session, listing, cfg)
        ClientCls.assert_not_called()

    assert created is True
    assert row.verdict == "不推荐"
    assert db_session.query(Evaluation).count() == 1
    assert db_session.query(Evaluation).first().analyzer == "rules"


def test_ai_always_runs_on_reject(db_session, monkeypatch):
    from app.models.tables import Evaluation, Listing, ProductConfig
    from app.services.evaluate import evaluate_and_persist

    monkeypatch.setenv("AI_API_KEY", "sk-test")
    monkeypatch.setenv("AI_ALWAYS", "true")
    monkeypatch.setenv("AI_API_BASE", "https://example.test/v1")

    cfg = ProductConfig(
        name="MBA",
        keyword="MacBook",
        price_max=3000,
        enabled=True,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    listing = Listing(
        config_id=cfg.id,
        external_id="ai-always-1",
        title="MacBook 高价",
        price=9000,
        content_hash="hash-always",
    )
    db_session.add(listing)
    db_session.commit()
    db_session.refresh(listing)

    mock_resp = _mock_chat_response(
        {
            "verdict_adjust": None,
            "tags": ["超预算"],
            "reason": "明显超出预算，不建议入手。",
            "seller_profile": "不确定",
        }
    )
    with patch("app.analyzers.ai.httpx.Client") as ClientCls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.return_value = mock_resp
        ClientCls.return_value = client
        evaluate_and_persist(db_session, listing, cfg)

    analyzers = {e.analyzer for e in db_session.query(Evaluation).all()}
    assert analyzers == {"rules", "ai"}
