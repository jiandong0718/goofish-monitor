"""Collector 联调 APIs (dry-run without DB write)."""

from __future__ import annotations

from fastapi import APIRouter

from app.collectors import SearchQuery, get_collector
from app.collectors.goofish import CollectorError
from app.models.schemas import DryRunItemOut, DryRunOut, DryRunRequest

router = APIRouter(prefix="/api/collectors", tags=["collectors"])


@router.post("/goofish/dry-run", response_model=DryRunOut)
async def goofish_dry_run(body: DryRunRequest) -> DryRunOut:
    """联调：执行一次搜索，返回 items，不写库。"""
    collector = get_collector()
    query = SearchQuery(
        keyword=body.keyword,
        price_min=body.price_min,
        price_max=body.price_max,
        exclude_keywords=body.exclude_keywords or [],
        page_size=body.page_size,
        sort="newest",
    )
    try:
        items = await collector.search(query)
    except CollectorError as exc:
        return DryRunOut(
            ok=False,
            count=0,
            items=[],
            error=exc.code,
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return DryRunOut(
            ok=False,
            count=0,
            items=[],
            error="exception",
            error_message=str(exc),
        )

    out_items = [
        DryRunItemOut(
            external_id=it.external_id,
            title=it.title,
            price=it.price,
            url=it.url,
            published_at=it.published_at,
            seller_type=it.seller_type,
            condition=it.condition,
        )
        for it in items
    ]
    return DryRunOut(ok=True, count=len(out_items), items=out_items)
