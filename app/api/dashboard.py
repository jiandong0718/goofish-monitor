"""监控概览 HTML dashboard（Jinja2）— 对齐产品截图布局。"""

from __future__ import annotations

import json
from datetime import datetime
from statistics import median
from typing import Any, Optional, Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.db import PROJECT_ROOT, get_db
from app.models.tables import Evaluation, Listing, MonitorTask, ProductConfig, RunLog
from app.services.run_logs import EVENT_LABELS_CN

router = APIRouter(tags=["dashboard"])

TEMPLATES_DIR = PROJECT_ROOT / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _empty_to_none(v: Any) -> Any:
    """HTML forms submit empty strings for optional number fields."""
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


OptionalQueryInt = Annotated[Optional[int], BeforeValidator(_empty_to_none)]



def _fmt_dt(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.strftime("%m-%d %H:%M")


def _relative_cn(value: Optional[datetime], now: Optional[datetime] = None) -> str:
    if value is None:
        return "尚未运行"
    now = now or datetime.utcnow()
    # Naive UTC stored historically
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    delta = now - value
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{secs // 60} 分钟前"
    if secs < 86400:
        return f"{secs // 3600} 小时前"
    return f"{secs // 86400} 天前"


def _truncate(text: Optional[str], n: int = 28) -> str:
    if not text:
        return "—"
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _price_stats(db: Session, task_id: Optional[int], config_id: Optional[int]) -> dict[str, Any]:
    """当前样本均价 / 历史均价 / 样本数 / 唯一商品数 / 日均价曲线。"""
    q = db.query(Listing).filter(Listing.price.isnot(None))
    if task_id is not None:
        q = q.filter(Listing.task_id == task_id)
    elif config_id is not None:
        q = q.filter(Listing.config_id == config_id)

    rows = q.all()
    # Fallback to config-scoped if task-scoped empty
    if not rows and task_id is not None and config_id is not None:
        rows = (
            db.query(Listing)
            .filter(Listing.config_id == config_id, Listing.price.isnot(None))
            .all()
        )

    prices = [float(r.price) for r in rows if r.price is not None]
    sample_count = len(prices)
    unique_ids = {r.external_id or r.id for r in rows}
    unique_count = len(unique_ids)

    current_avg = sum(prices) / sample_count if sample_count else None
    # 历史均价：按唯一商品取最新价再平均；不足则回退全样本均价
    latest_by_key: dict[Any, float] = {}
    for r in sorted(rows, key=lambda x: x.last_seen or x.collected_at or datetime.min):
        key = r.external_id or r.id
        if r.price is not None:
            latest_by_key[key] = float(r.price)
    hist_prices = list(latest_by_key.values())
    historical_avg = (
        sum(hist_prices) / len(hist_prices) if hist_prices else current_avg
    )

    # Daily avg + median for chart
    by_day: dict[str, list[float]] = {}
    for r in rows:
        ts = r.collected_at or r.first_seen or r.last_seen
        if ts is None or r.price is None:
            continue
        day = ts.strftime("%m-%d")
        by_day.setdefault(day, []).append(float(r.price))

    daily_curve = []
    for day in sorted(by_day.keys()):
        vals = by_day[day]
        daily_curve.append(
            {
                "date": day,
                "avg": round(sum(vals) / len(vals), 2),
                "median": round(float(median(vals)), 2),
                "count": len(vals),
            }
        )

    return {
        "sample_count": sample_count,
        "unique_count": unique_count,
        "current_avg": current_avg,
        "historical_avg": historical_avg,
        "daily_curve": daily_curve,
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
    }


def _build_feed(
    db: Session,
    tasks: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """实时动态：最近 listing / 推荐 / 运行中任务。"""
    feed: list[dict[str, Any]] = []

    # Recent listings
    recent_listings = (
        db.query(Listing).order_by(Listing.id.desc()).limit(5).all()
    )
    for li in recent_listings:
        feed.append(
            {
                "kind": "listing",
                "title": _truncate(li.title, 32),
                "sub": f"{li.price and f'¥{li.price:.0f}' or '—'} · {_relative_cn(li.collected_at, now)}",
                "tag": "结果已更新",
                "tag_class": "tag-updated",
                "sort_ts": li.collected_at or datetime.min,
            }
        )

    # AI / 推荐 evaluations
    recommend_rows = (
        db.query(Evaluation)
        .options(joinedload(Evaluation.listing))
        .filter(Evaluation.verdict == "推荐")
        .order_by(Evaluation.id.desc())
        .limit(5)
        .all()
    )
    for ev in recommend_rows:
        listing = ev.listing
        title = _truncate(listing.title if listing else None, 32)
        price = listing.price if listing else None
        tag = "AI 推荐" if (ev.analyzer or "") == "ai" else "关键词推荐"
        feed.append(
            {
                "kind": "recommend",
                "title": title,
                "sub": f"{price and f'¥{price:.0f}' or '—'} · {_relative_cn(ev.created_at, now)}",
                "tag": tag,
                "tag_class": "tag-ai" if tag.startswith("AI") else "tag-kw",
                "sort_ts": ev.created_at or datetime.min,
            }
        )

    # Running tasks
    for t in tasks:
        if t["status"] != "running":
            continue
        feed.append(
            {
                "kind": "task",
                "title": _truncate(t.get("keyword") or t.get("config_name"), 32),
                "sub": "任务正在轮询闲鱼结果",
                "tag": "运行中",
                "tag_class": "tag-running",
                "sort_ts": now,
            }
        )

    # Recent run logs (optional feed source)
    recent_logs = (
        db.query(RunLog).order_by(RunLog.id.desc()).limit(5).all()
    )
    for rl in recent_logs:
        label = EVENT_LABELS_CN.get(rl.event, rl.event)
        if rl.level == "error":
            tag, tag_class = "运行异常", "tag-kw"
        elif rl.event == "collect_done":
            tag, tag_class = "采集完成", "tag-updated"
        elif rl.event == "evaluate_done":
            tag, tag_class = "评估完成", "tag-ai"
        else:
            tag, tag_class = label, "tag-running"
        feed.append(
            {
                "kind": "log",
                "title": _truncate(rl.message, 36),
                "sub": f"{label} · {_relative_cn(rl.created_at, now)}"
                + (f" · 任务 #{rl.task_id}" if rl.task_id else ""),
                "tag": tag,
                "tag_class": tag_class,
                "sort_ts": rl.created_at or datetime.min,
            }
        )

    # If still empty, seed from tasks so the panel isn't blank
    if not feed and tasks:
        t = tasks[0]
        feed.append(
            {
                "kind": "task",
                "title": _truncate(t.get("keyword") or t.get("config_name"), 32),
                "sub": f"状态：{t['status']} · 样本 {t['listing_count']} 条",
                "tag": "任务就绪" if t["status"] != "running" else "运行中",
                "tag_class": "tag-running" if t["status"] == "running" else "tag-updated",
                "sort_ts": now,
            }
        )

    feed.sort(key=lambda x: x.get("sort_ts") or datetime.min, reverse=True)
    # Dedup by title+tag, keep first 6
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in feed:
        key = (item["title"], item["tag"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= 6:
            break
    return out


def _ai_strategy(featured: Optional[dict[str, Any]], recommend_count: int) -> dict[str, str]:
    if featured is None:
        return {
            "headline": "先创建第一个监测任务",
            "body": "配置关键词与预算后启动任务，系统会持续抓取闲鱼并汇总均价与推荐。",
        }
    name = featured.get("keyword") or featured.get("config_name") or "当前任务"
    if recommend_count > 0:
        return {
            "headline": "更高价值任务再压榨一点",
            "body": (
                f"“{name}” 已经出现真实推荐，建议提高抓取频率并聚焦新发布商品。"
            ),
        }
    if featured.get("listing_count", 0) == 0:
        return {
            "headline": "等待首批样本入库",
            "body": (
                f"“{name}” 尚无扫描结果。确认任务为运行中，或通过采集接口跑一轮 stub/Playwright。"
            ),
        }
    return {
        "headline": "扩大覆盖、收紧筛选",
        "body": (
            f"“{name}” 已有 {featured.get('listing_count', 0)} 条样本但尚无推荐。"
            "可略放宽硬条件或缩短轮询间隔，优先抓取新发布低价帖。"
        ),
    }


@router.get("/", response_class=HTMLResponse)
def overview(
    request: Request,
    flash: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """监控概览：侧栏 + 四卡 + 任务均价曲线 + 实时动态 + AI 策略。"""
    now = datetime.utcnow()

    total_tasks = db.query(func.count(MonitorTask.id)).scalar() or 0
    running_tasks = (
        db.query(func.count(MonitorTask.id))
        .filter(MonitorTask.status == "running")
        .scalar()
        or 0
    )
    listings_count = db.query(func.count(Listing.id)).scalar() or 0
    recommend_count = (
        db.query(func.count(Evaluation.id))
        .filter(Evaluation.verdict == "推荐")
        .scalar()
        or 0
    )
    ai_recommend = (
        db.query(func.count(Evaluation.id))
        .filter(Evaluation.verdict == "推荐", Evaluation.analyzer == "ai")
        .scalar()
        or 0
    )
    kw_recommend = (
        db.query(func.count(Evaluation.id))
        .filter(Evaluation.verdict == "推荐", Evaluation.analyzer == "rules")
        .scalar()
        or 0
    )
    # 「结果文件」：有 listing 的任务数（近似截图里的结果文件计数）
    result_files = (
        db.query(func.count(func.distinct(Listing.task_id)))
        .filter(Listing.task_id.isnot(None))
        .scalar()
        or 0
    )
    if result_files == 0 and listings_count > 0:
        result_files = 1

    task_rows = (
        db.query(MonitorTask)
        .options(joinedload(MonitorTask.config))
        .order_by(MonitorTask.id.desc())
        .all()
    )

    tasks: list[dict[str, Any]] = []
    for row in task_rows:
        cfg: Optional[ProductConfig] = row.config
        stats = _price_stats(db, row.id, row.config_id)
        listing_count = (
            db.query(func.count(Listing.id)).filter(Listing.task_id == row.id).scalar()
            or 0
        )
        if listing_count == 0 and row.config_id is not None:
            listing_count = (
                db.query(func.count(Listing.id))
                .filter(Listing.config_id == row.config_id)
                .scalar()
                or 0
            )

        tasks.append(
            {
                "id": row.id,
                "status": row.status,
                "interval_minutes": row.interval_minutes,
                "config_id": row.config_id,
                "config_name": cfg.name if cfg else None,
                "keyword": cfg.keyword if cfg else None,
                "price_min": cfg.price_min if cfg else None,
                "price_max": cfg.price_max if cfg else None,
                "avg_price": stats["current_avg"],
                "historical_avg": stats["historical_avg"],
                "sample_count": stats["sample_count"],
                "unique_count": stats["unique_count"],
                "listing_count": listing_count,
                "daily_curve": stats["daily_curve"],
                "min_price": stats["min_price"],
                "max_price": stats["max_price"],
                "last_run_at": _fmt_dt(row.last_run_at),
                "last_run_relative": _relative_cn(row.last_run_at, now),
                "updated_at": _fmt_dt(row.updated_at),
            }
        )

    # Featured: prefer running, else first
    featured = None
    for t in tasks:
        if t["status"] == "running":
            featured = t
            break
    if featured is None and tasks:
        featured = tasks[0]

    feed = _build_feed(db, tasks, now)
    strategy = _ai_strategy(featured, recommend_count)

    curve_json = "[]"
    if featured and featured.get("daily_curve"):
        curve_json = json.dumps(featured["daily_curve"], ensure_ascii=False)

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "active_nav": "overview",
            "backend_connected": True,
            "running_tasks": running_tasks,
            "total_tasks": total_tasks,
            "listings_count": listings_count,
            "recommend_count": recommend_count,
            "ai_recommend": ai_recommend,
            "kw_recommend": kw_recommend,
            "result_files": result_files,
            "tasks": tasks,
            "featured": featured,
            "feed": feed,
            "strategy": strategy,
            "curve_json": curve_json,
            "flash": flash,
        },
    )


@router.get("/tasks", response_class=HTMLResponse)
def page_tasks(
    request: Request,
    flash: str | None = None,
    config_id: OptionalQueryInt = None,
    task_id: OptionalQueryInt = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """任务管理：列表 + 启停 / 立即执行 / 改间隔 / 创建。"""
    now = datetime.utcnow()
    rows = (
        db.query(MonitorTask)
        .options(joinedload(MonitorTask.config))
        .order_by(MonitorTask.id.desc())
        .all()
    )
    items = []
    running = 0
    paused = 0
    for row in rows:
        cfg = row.config
        if row.status == "running":
            running += 1
        else:
            paused += 1
        items.append(
            {
                "id": row.id,
                "config_id": row.config_id,
                "config_name": cfg.name if cfg else f"任务 #{row.id}",
                "keyword": cfg.keyword if cfg else "—",
                "status": row.status,
                "interval_minutes": row.interval_minutes,
                "last_run_at": _fmt_dt(row.last_run_at),
                "last_run_relative": _relative_cn(row.last_run_at, now),
                "error_message": _truncate(row.error_message, 80) if row.error_message else None,
            }
        )
    configs = [
        {"id": c.id, "name": c.name, "keyword": c.keyword}
        for c in db.query(ProductConfig).order_by(ProductConfig.id.desc()).all()
    ]
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "active_nav": "tasks",
            "backend_connected": True,
            "items": items,
            "configs": configs,
            "total_tasks": len(items),
            "running_tasks": running,
            "paused_tasks": paused,
            "empty_hint": "暂无任务。点击右上角「创建任务」选择配置，或去「开始新监测」创建。",
            "flash": flash,
            "flash_config_id": config_id,
            "flash_task_id": task_id,
        },
    )


@router.get("/accounts", response_class=HTMLResponse)
async def page_accounts(request: Request) -> HTMLResponse:
    """账号管理：持久化浏览器配置会话状态 + 登录说明。"""
    import asyncio
    import os

    from app.collectors.goofish_profile import (
        ensure_user_data_dir,
        probe_session,
        session_status_summary,
    )
    from app.db import PROJECT_ROOT

    ensure_user_data_dir()
    # 页面加载默认不做 live Playwright probe，避免与定时采集抢同一 browser_profile。
    # 需要探测时走 /api/accounts/goofish/status?probe=true
    probe = None
    if os.getenv("GOOFISH_ACCOUNTS_LIVE_PROBE", "").strip().lower() in ("1", "true", "yes"):
        if not os.getenv("GOOFISH_FIXTURE_PATH", "").strip():
            try:
                probe = await asyncio.wait_for(probe_session(timeout_ms=12000), timeout=20)
            except Exception as exc:  # noqa: BLE001
                probe = {
                    "logged_in": None,
                    "method": "probe_error",
                    "message": f"实时探测跳过/失败：{exc}"[:200],
                }
    session = session_status_summary(probe=probe)
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "active_nav": "accounts",
            "backend_connected": True,
            "session": session,
            "project_root": str(PROJECT_ROOT),
        },
    )


@router.get("/results", response_class=HTMLResponse)
def page_results(
    request: Request,
    verdict: str | None = None,
    task_id: OptionalQueryInt = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """结果查看：卡片式评估（推荐/待观察/不推荐）+ 标题/价格/理由/链接。"""
    listing_ids_for_task: list[int] | None = None
    if task_id is not None:
        listing_ids_for_task = [
            lid
            for (lid,) in db.query(Listing.id).filter(Listing.task_id == task_id).all()
        ]

    q = db.query(Evaluation).options(joinedload(Evaluation.listing))
    if verdict:
        q = q.filter(Evaluation.verdict == verdict)
    if listing_ids_for_task is not None:
        if listing_ids_for_task:
            q = q.filter(Evaluation.listing_id.in_(listing_ids_for_task))
        else:
            q = q.filter(Evaluation.id == -1)  # no listings for this task
    eval_rows = q.order_by(Evaluation.id.desc()).limit(80).all()

    # Counts (respect task filter; ignore verdict so stats stay useful)
    count_base = db.query(Evaluation.verdict, func.count(Evaluation.id))
    if listing_ids_for_task is not None:
        if listing_ids_for_task:
            count_base = count_base.filter(
                Evaluation.listing_id.in_(listing_ids_for_task)
            )
        else:
            count_base = count_base.filter(Evaluation.id == -1)
    by_verdict = dict(count_base.group_by(Evaluation.verdict).all())
    counts = {
        "total": sum(by_verdict.values()),
        "recommend": int(by_verdict.get("推荐") or 0),
        "watch": int(by_verdict.get("待观察") or 0),
        "reject": int(by_verdict.get("不推荐") or 0),
    }

    items: list[dict[str, Any]] = []
    for ev in eval_rows:
        listing = ev.listing
        title_full = listing.title if listing else f"评估 #{ev.id}"
        verdict_key = {
            "推荐": "recommend",
            "待观察": "watch",
            "不推荐": "reject",
        }.get(ev.verdict or "", "other")
        analyzer = ev.analyzer or "rules"
        analyzer_label = "AI 评估" if analyzer == "ai" else "规则评估"
        tags = ev.tags if isinstance(ev.tags, list) else []
        items.append(
            {
                "id": ev.id,
                "listing_id": ev.listing_id,
                "task_id": listing.task_id if listing else None,
                "title": _truncate(title_full, 48),
                "title_full": title_full,
                "price": float(listing.price) if listing and listing.price is not None else None,
                "url": listing.url if listing else None,
                "seller_type": listing.seller_type if listing else None,
                "condition": listing.condition if listing else None,
                "verdict": ev.verdict or "—",
                "verdict_key": verdict_key,
                "reason": _truncate(ev.reason, 160) if ev.reason else None,
                "score": float(ev.score) if ev.score is not None else None,
                "analyzer": analyzer,
                "analyzer_label": analyzer_label,
                "tags": tags[:6],
                "created_at": _fmt_dt(ev.created_at),
            }
        )

    if items:
        subtitle = "评估卡片：推荐 / 待观察 / 不推荐，含价格、理由与商品链接。"
        empty = ""
    elif counts["total"] == 0:
        # Fallback: show listings as thin cards when no evaluations yet
        listing_q = db.query(Listing)
        if task_id is not None:
            listing_q = listing_q.filter(Listing.task_id == task_id)
        listing_rows = listing_q.order_by(Listing.id.desc()).limit(40).all()
        for r in listing_rows:
            items.append(
                {
                    "id": r.id,
                    "listing_id": r.id,
                    "task_id": r.task_id,
                    "title": _truncate(r.title, 48),
                    "title_full": r.title,
                    "price": float(r.price) if r.price is not None else None,
                    "url": r.url,
                    "seller_type": r.seller_type,
                    "condition": r.condition,
                    "verdict": "待评估",
                    "verdict_key": "other",
                    "reason": "尚未生成评估，可对 listing 调用 /evaluate 或跑一轮任务。",
                    "score": None,
                    "analyzer": "—",
                    "analyzer_label": "未评估",
                    "tags": [],
                    "created_at": _fmt_dt(r.collected_at),
                }
            )
        subtitle = "尚无评估；先展示最近采集商品。启动任务或 run-once 后会出现结论。"
        empty = "暂无扫描结果。启动任务或调用采集接口后会出现在这里。"
    else:
        subtitle = "当前筛选条件下无评估结果。"
        empty = "没有匹配的评估，试试重置筛选。"

    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "active_nav": "results",
            "backend_connected": True,
            "page_subtitle": subtitle,
            "items": items,
            "counts": counts,
            "filter_verdict": verdict or "",
            "filter_task_id": task_id,
            "empty_hint": empty,
        },
    )


@router.get("/logs", response_class=HTMLResponse)
def page_logs(
    request: Request,
    limit: int = 100,
    task_id: OptionalQueryInt = None,
    level: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """运行日志：真实 run_logs 列表。"""
    q = db.query(RunLog)
    if task_id is not None:
        q = q.filter(RunLog.task_id == task_id)
    if level:
        q = q.filter(RunLog.level == level.lower())
    rows = q.order_by(RunLog.id.desc()).limit(min(max(limit, 1), 500)).all()

    # Resolve task display names
    task_ids = {r.task_id for r in rows if r.task_id}
    task_labels: dict[int, str] = {}
    if task_ids:
        for t in (
            db.query(MonitorTask)
            .options(joinedload(MonitorTask.config))
            .filter(MonitorTask.id.in_(task_ids))
            .all()
        ):
            name = t.config.keyword if t.config else None
            task_labels[t.id] = name or f"任务 #{t.id}"

    items = []
    for r in rows:
        items.append(
            {
                "id": r.id,
                "time": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "—",
                "time_short": _fmt_dt(r.created_at) or "—",
                "level": r.level,
                "event": r.event,
                "event_label": EVENT_LABELS_CN.get(r.event, r.event),
                "message": r.message,
                "task_id": r.task_id,
                "task_label": task_labels.get(r.task_id) if r.task_id else None,
                "config_id": r.config_id,
            }
        )

    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "active_nav": "logs",
            "backend_connected": True,
            "page_title": "运行日志",
            "page_subtitle": "调度、采集与评估的真实运行记录。",
            "items": items,
            "filter_task_id": task_id,
            "filter_level": level or "",
            "empty_hint": "暂无运行日志。启动任务或调用 run-once 后会出现在这里。",
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request) -> HTMLResponse:
    """系统设置：只读展示当前采集相关环境变量。"""
    import os

    from app.collectors.goofish_profile import user_data_dir

    def _g(name: str, default: str = "") -> str:
        return os.getenv(name, default) or default or "（空）"

    env_rows = [
        {
            "label": "COLLECTOR_BACKEND",
            "value": _g("COLLECTOR_BACKEND", "stub"),
            "hint": "stub（安全默认）| playwright（真扫，使用持久化配置）",
        },
        {
            "label": "GOOFISH_USER_DATA_DIR",
            "value": str(user_data_dir()),
            "hint": "Chromium 持久化目录，默认 data/browser_profile/",
        },
        {
            "label": "GOOFISH_HEADLESS",
            "value": _g("GOOFISH_HEADLESS", "true"),
            "hint": "定时采集默认无头；登录脚本会强制有头",
        },
        {
            "label": "GOOFISH_TIMEOUT_MS",
            "value": _g("GOOFISH_TIMEOUT_MS", "30000"),
            "hint": "页面超时（毫秒）",
        },
        {
            "label": "GOOFISH_FIXTURE_PATH",
            "value": _g("GOOFISH_FIXTURE_PATH", ""),
            "hint": "设置后走 JSON fixture，不访问闲鱼（CI/冒烟）",
        },
        {
            "label": "SCHEDULER_ENABLED",
            "value": _g("SCHEDULER_ENABLED", "true"),
            "hint": "是否启动 APScheduler",
        },
        {
            "label": "SCHEDULER_TIMEZONE",
            "value": _g("SCHEDULER_TIMEZONE", "Asia/Shanghai"),
            "hint": "调度时区",
        },
        {
            "label": "默认任务间隔",
            "value": "见各 MonitorTask.interval_minutes（新建默认常为 30）",
            "hint": "在任务管理页可改间隔",
        },
        {
            "label": "AI_API_KEY",
            "value": "已设置" if os.getenv("AI_API_KEY") else "（空，跳过 AI）",
            "hint": "未设置则仅规则评估",
        },
        {
            "label": "PUSH_WEBHOOK_URL",
            "value": "已配置" if os.getenv("PUSH_WEBHOOK_URL", "").strip() else "未配置",
            "hint": "企业微信机器人或通用 Webhook；详情见 README，密钥不在此展示",
        },
        {
            "label": "PUSH_TELEGRAM",
            "value": (
                "已配置"
                if (
                    os.getenv("PUSH_TELEGRAM_BOT_TOKEN", "").strip()
                    and os.getenv("PUSH_TELEGRAM_CHAT_ID", "").strip()
                )
                else "未配置"
            ),
            "hint": "需同时设置 BOT_TOKEN 与 CHAT_ID；不在此展示密钥",
        },
        {
            "label": "PUSH_INCLUDE_WATCH",
            "value": os.getenv("PUSH_INCLUDE_WATCH", "false") or "false",
            "hint": "true 时「待观察」也推送；默认仅「推荐」",
        },
    ]
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active_nav": "settings",
            "backend_connected": True,
            "env_rows": env_rows,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def page_new(request: Request) -> HTMLResponse:
    """开始新监测 — 创建 ProductConfig + 可选启动 MonitorTask 的表单页。"""
    return templates.TemplateResponse(
        request,
        "new.html",
        {
            "active_nav": "overview",
            "backend_connected": True,
        },
    )
