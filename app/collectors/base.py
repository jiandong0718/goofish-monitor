"""Collector interface — search / fetch_detail. Real scraping not in v1."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SearchQuery:
    keyword: str
    keyword_variants: list[str] = field(default_factory=list)
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    sort: str = "newest"  # 新发布优先
    exclude_keywords: list[str] = field(default_factory=list)
    page: int = 1
    page_size: int = 20
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ListingItem:
    external_id: str
    title: str
    price: Optional[float] = None
    url: Optional[str] = None
    seller_type: Optional[str] = None
    condition: Optional[str] = None
    published_at: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


class BaseCollector(ABC):
    """Abstract collector; httpx / Playwright backends can implement this later."""

    name: str = "base"

    @abstractmethod
    async def search(self, query: SearchQuery) -> list[ListingItem]:
        """Search listings. Prefer sort=newest (新发布)."""

    @abstractmethod
    async def fetch_detail(self, external_id: str) -> Optional[ListingItem]:
        """Fetch a single listing detail by platform id."""
