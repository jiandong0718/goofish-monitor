"""Goofish (闲鱼) collector stub — no real network scraping."""

from __future__ import annotations

import logging
from typing import Optional

from app.collectors.base import BaseCollector, ListingItem, SearchQuery

logger = logging.getLogger(__name__)


class GoofishStubCollector(BaseCollector):
    """
    Stub collector for 闲鱼.

    Intended later backends:
      - httpx for API-like endpoints
      - Playwright for browser automation
    Sort default: newest (新发布) for 捡漏.
    """

    name = "goofish_stub"

    async def search(self, query: SearchQuery) -> list[ListingItem]:
        logger.info(
            "[stub] goofish search keyword=%r sort=%s page=%s (no real fetch)",
            query.keyword,
            query.sort,
            query.page,
        )
        # Return empty list — real crawl not implemented in v1
        return []

    async def fetch_detail(self, external_id: str) -> Optional[ListingItem]:
        logger.info("[stub] goofish fetch_detail id=%s (no real fetch)", external_id)
        return None


def get_collector() -> BaseCollector:
    """Deprecated local factory — prefer app.collectors.get_collector."""
    return GoofishStubCollector()
