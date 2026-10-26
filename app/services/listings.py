"""Listing upsert / collect-cycle helpers."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.collectors.base import ListingItem, SearchQuery
from app.models.tables import Listing, MonitorTask, ProductConfig
from app.services.run_logs import write_run_log

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.utcnow()


def content_hash(item: ListingItem) -> str:
    blob = f"{item.title}|{item.price}|{item.url or ''}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    # ISO-ish
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text.replace("Z", ""), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None


def upsert_listings(
    db: Session,
    items: list[ListingItem],
    *,
    config_id: Optional[int] = None,
    task_id: Optional[int] = None,
) -> dict[str, Any]:
    """
    Dedup by external_id. Insert new rows; update existing title/price/url/hash/last_seen.
    Returns counts plus listing_ids touched this cycle (for evaluation).
    """
    now = _utcnow()
    inserted = 0
    updated = 0
    listing_ids: list[int] = []
    for item in items:
        if not item.external_id:
            continue
        ch = content_hash(item)
        existing = (
            db.query(Listing)
            .filter(Listing.external_id == item.external_id)
            .order_by(Listing.id.desc())
            .first()
        )
        published = _parse_dt(item.published_at)
        if existing is None:
            row = Listing(
                config_id=config_id,
                task_id=task_id,
                external_id=item.external_id,
                title=item.title[:512],
                price=item.price,
                url=(item.url or "")[:1024] or None,
                seller_type=item.seller_type,
                condition=item.condition,
                raw=item.raw or None,
                published_at=published,
                collected_at=now,
                first_seen=now,
                last_seen=now,
                content_hash=ch,
            )
            db.add(row)
            db.flush()
            listing_ids.append(row.id)
            inserted += 1
        else:
            existing.last_seen = now
            existing.title = item.title[:512]
            existing.price = item.price
            if item.url:
                existing.url = item.url[:1024]
            if item.seller_type:
                existing.seller_type = item.seller_type
            if item.condition:
                existing.condition = item.condition
            if item.raw:
                existing.raw = item.raw
            if published:
                existing.published_at = published
            if config_id is not None:
                existing.config_id = config_id
            if task_id is not None:
                existing.task_id = task_id
            existing.content_hash = ch
            listing_ids.append(existing.id)
            updated += 1
    db.commit()
    return {
        "fetched": len(items),
        "inserted": inserted,
        "updated": updated,
        "listing_ids": listing_ids,
    }


async def run_collect_cycle(
    db: Session,
    task_id: int,
    *,
    allow_paused: bool = True,
) -> dict[str, Any]:
    """
    One collect cycle for a task: search → upsert → rule evaluate.
    allow_paused=True for 联调 run-once (does not require status=running).
    """
    from app.collectors import get_collector
    from app.collectors.goofish import CollectorError
    from app.services.evaluate import evaluate_listings_for_config

    task = db.get(MonitorTask, task_id)
    if not task:
        write_run_log(
            db,
            level="error",
            event="error",
            message=f"任务 #{task_id} 不存在",
            task_id=task_id,
            details={"error": "task_not_found"},
        )
        return {"ok": False, "error": "task_not_found", "fetched": 0, "inserted": 0, "updated": 0, "errors": 1}

    if not allow_paused and task.status != "running":
        write_run_log(
            db,
            level="warn",
            event="error",
            message=f"任务 #{task_id} 状态为 {task.status}，跳过调度采集",
            task_id=task.id,
            config_id=task.config_id,
            details={"status": task.status},
        )
        return {
            "ok": False,
            "error": f"task_status_{task.status}",
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
            "errors": 0,
        }

    config = db.get(ProductConfig, task.config_id)
    if not config:
        task.status = "error"
        task.error_message = "config missing"
        db.commit()
        write_run_log(
            db,
            level="error",
            event="error",
            message=f"任务 #{task_id} 绑定的配置缺失",
            task_id=task.id,
            config_id=task.config_id,
            details={"error": "config_missing"},
        )
        return {"ok": False, "error": "config_missing", "fetched": 0, "inserted": 0, "updated": 0, "errors": 1}

    write_run_log(
        db,
        level="info",
        event="collect_start",
        message=f"开始采集「{config.keyword}」（任务 #{task.id}）",
        task_id=task.id,
        config_id=config.id,
        details={"keyword": config.keyword, "backend": None},
        commit=True,
    )

    collector = get_collector()
    query = SearchQuery(
        keyword=config.keyword,
        keyword_variants=config.keyword_variants or [],
        price_min=config.price_min,
        price_max=config.price_max,
        sort=config.sort_default or "newest",
        exclude_keywords=config.exclude_keywords or [],
    )

    errors = 0
    error_message: Optional[str] = None
    try:
        items = await collector.search(query)
        counts = upsert_listings(db, items, config_id=config.id, task_id=task.id)
        listing_ids = counts.pop("listing_ids", [])
        eval_counts = evaluate_listings_for_config(
            db, listing_ids, config, force=False, task_id=task.id
        )
        task.last_run_at = _utcnow()
        task.error_message = None
        # Do not force status=running on run-once of paused tasks
        db.commit()
        write_run_log(
            db,
            level="info",
            event="collect_done",
            message=(
                f"采集完成：拉取 {counts.get('fetched', 0)}，"
                f"新增 {counts.get('inserted', 0)}，更新 {counts.get('updated', 0)}，"
                f"评估 {eval_counts.get('evaluated', 0)}"
            ),
            task_id=task.id,
            config_id=config.id,
            details={**counts, **eval_counts},
        )
        return {
            "ok": True,
            "error": None,
            "errors": 0,
            "evaluated": eval_counts["evaluated"],
            "eval_skipped": eval_counts["skipped"],
            **counts,
        }
    except CollectorError as exc:
        errors = 1
        error_message = str(exc)
        logger.warning("[collect] task_id=%s collector error: %s", task_id, exc)
        task.last_run_at = _utcnow()
        # 仅登录墙等硬错误把任务打成 error（调度会摘掉）；
        # empty_results / 浏览器被关 / 配置占用 视为可恢复，保持 running 下轮再试。
        hard = exc.code in ("login_required", "login_wall", "config_missing")
        transient = exc.code in (
            "empty_results",
            "profile_locked",
            "browser_closed",
            "timeout",
            "navigation_failed",
        )
        if hard and task.status == "running":
            task.status = "error"
            task.error_message = error_message
        elif transient:
            # 保留运行中，错误信息写日志；页面上不长期挂「异常」
            task.error_message = None
        else:
            task.error_message = error_message
            if task.status == "running":
                task.status = "error"
        db.commit()
        if task.status == "error":
            try:
                from app.workers.scheduler import sync_task_job
                sync_task_job(task)
            except Exception:  # noqa: BLE001
                logger.exception("[collect] sync_task_job after error failed")
        event = "login_required" if exc.code in ("login_required", "login_wall") else "error"
        level = "warn" if transient else "error"
        msg = (
            f"需要登录闲鱼：请运行 python -m app.scripts.login_goofish 后 resume。"
            if event == "login_required"
            else (
                f"本轮未扫到结果（可恢复）：{error_message}"
                if transient
                else f"采集失败：{error_message}"
            )
        )
        write_run_log(
            db,
            level=level,
            event=event,
            message=msg[:512],
            task_id=task.id,
            config_id=config.id,
            details={"error": exc.code, "details": exc.details, "transient": transient},
        )
        return {
            "ok": False,
            "error": exc.code,
            "error_message": error_message,
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
            "errors": errors,
            "details": exc.details,
        }
    except Exception as exc:  # noqa: BLE001
        errors = 1
        error_message = str(exc)
        low = error_message.lower()
        transient = (
            isinstance(exc, TimeoutError)
            or "profile_locked" in low
            or "has been closed" in low
            or "target page" in low
            or "browser has been closed" in low
        )
        logger.exception("[collect] task_id=%s failed", task_id)
        task.last_run_at = _utcnow()
        if transient:
            # 不挡调度：配置锁 / 浏览器被关属于瞬时冲突
            task.error_message = None
            level = "warn"
            code = "profile_locked" if ("profile_locked" in low or isinstance(exc, TimeoutError)) else "browser_closed"
            msg = f"本轮采集冲突（可恢复）：{error_message}"
        else:
            task.error_message = error_message
            if task.status == "running":
                task.status = "error"
            level = "error"
            code = "exception"
            msg = f"采集异常：{error_message}"
        db.commit()
        write_run_log(
            db,
            level=level,
            event="error",
            message=msg[:512],
            task_id=task.id,
            config_id=config.id,
            details={"error": code, "transient": transient},
        )
        return {
            "ok": False,
            "error": code,
            "error_message": error_message,
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
            "errors": errors,
        }
