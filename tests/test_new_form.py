"""Smoke: /new form page + create config/task via API (form targets)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_new.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("COLLECTOR_BACKEND", "stub")
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

    from app.main import app

    with TestClient(app) as c:
        yield c

    engine.dispose()


def test_new_page_200(client: TestClient):
    res = client.get("/new")
    assert res.status_code == 200
    body = res.text
    assert "开始新监测" in body
    assert 'id="new-monitor-form"' in body
    assert 'name="keyword"' in body
    assert 'name="start_immediately"' in body
    assert "/api/configs" in body
    assert "/api/tasks" in body
    assert "打开 API 文档" not in body  # docs CTA replaced


def test_form_apis_create_config_and_running_task(client: TestClient):
    """Mirrors the /new form JS: POST config then POST running task."""
    cfg = client.post(
        "/api/configs",
        json={
            "name": "UI 表单联调",
            "keyword": "MacBook Air M2",
            "keyword_variants": ["MBA M2", "苹果笔记本"],
            "model": "Air M2",
            "size": "13.6",
            "ram": "16GB",
            "storage": "512GB",
            "price_min": 3000,
            "price_max": 5500,
            "seller_preference": "personal_only",
            "exclude_keywords": ["碎屏", "进水"],
            "sort_default": "newest",
            "hard_requirements": {
                "model": "Air M2",
                "ram": "16GB",
                "storage": "512GB",
            },
            "enabled": True,
        },
    )
    assert cfg.status_code == 201, cfg.text
    config_id = cfg.json()["id"]
    assert cfg.json()["sort_default"] == "newest"
    assert cfg.json()["keyword_variants"] == ["MBA M2", "苹果笔记本"]

    task = client.post(
        "/api/tasks",
        json={
            "config_id": config_id,
            "interval_minutes": 15,
            "status": "running",
        },
    )
    assert task.status_code == 201, task.text
    body = task.json()
    assert body["config_id"] == config_id
    assert body["status"] == "running"
    assert body["interval_minutes"] == 15

    # Flash landing page
    page = client.get(f"/tasks?flash=started&config_id={config_id}&task_id={body['id']}")
    assert page.status_code == 200
    assert "任务管理" in page.text
