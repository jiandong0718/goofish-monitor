"""Smoke: fixture path returns sample listings without live network."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "sample_listings.json"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("COLLECTOR_BACKEND", "playwright")
    monkeypatch.setenv("GOOFISH_FIXTURE_PATH", str(FIXTURE))
    monkeypatch.delenv("GOOFISH_COOKIES", raising=False)
    monkeypatch.delenv("GOOFISH_COOKIES_FILE", raising=False)


def test_fixture_file_exists():
    assert FIXTURE.is_file()


def test_fixture_search_returns_items():
    from app.collectors import SearchQuery, get_collector

    collector = get_collector()
    assert collector.name == "goofish_playwright"
    items = asyncio.run(
        collector.search(
            SearchQuery(keyword="MacBook", price_max=6000, exclude_keywords=["碎屏"])
        )
    )
    # fixture has 3; price_max 6000 drops Pro 8900; exclude 碎屏 drops mb-003 → 1 left
    assert len(items) == 1
    assert items[0].external_id == "fixture-mb-001"
    assert items[0].price == 5200


def test_fixture_search_all():
    from app.collectors import SearchQuery, get_collector

    items = asyncio.run(get_collector().search(SearchQuery(keyword="MacBook")))
    assert len(items) == 3


def test_stub_backend_still_works(monkeypatch):
    monkeypatch.setenv("COLLECTOR_BACKEND", "stub")
    # force re-import path via get_collector reading env
    from app.collectors import SearchQuery, get_collector

    collector = get_collector()
    assert collector.name == "goofish_stub"
    items = asyncio.run(collector.search(SearchQuery(keyword="MacBook")))
    assert items == []
