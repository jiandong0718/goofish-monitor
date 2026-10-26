"""Collectors factory.

COLLECTOR_BACKEND=stub|playwright (default stub).
playwright uses persistent Chromium profile (GOOFISH_USER_DATA_DIR).
"""

from __future__ import annotations

import logging
import os

from app.collectors.base import BaseCollector, ListingItem, SearchQuery
from app.collectors.goofish_stub import GoofishStubCollector

logger = logging.getLogger(__name__)

__all__ = [
    "BaseCollector",
    "ListingItem",
    "SearchQuery",
    "GoofishStubCollector",
    "get_collector",
    "CollectorError",
]


def get_collector() -> BaseCollector:
    """Factory — stub (default) or Playwright goofish (persistent profile)."""
    backend = os.getenv("COLLECTOR_BACKEND", "stub").strip().lower()
    if backend in ("playwright", "goofish", "live"):
        from app.collectors.goofish import GoofishCollector

        logger.info("[collectors] using GoofishCollector (playwright)")
        return GoofishCollector()
    logger.info("[collectors] using GoofishStubCollector (backend=%s)", backend)
    return GoofishStubCollector()


# Lazy re-export so `from app.collectors import CollectorError` works
def __getattr__(name: str):
    if name == "CollectorError":
        from app.collectors.goofish import CollectorError

        return CollectorError
    if name == "GoofishCollector":
        from app.collectors.goofish import GoofishCollector

        return GoofishCollector
    raise AttributeError(name)
