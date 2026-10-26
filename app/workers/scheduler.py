"""APScheduler — register interval jobs that collect + upsert listings."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from app.models.tables import MonitorTask

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
SCHEDULER_TIMEZONE = os.getenv("SCHEDULER_TIMEZONE", "Asia/Shanghai")


def get_scheduler() -> Optional[BackgroundScheduler]:
    return _scheduler


def _job_id(task_id: int) -> str:
    return f"monitor_task_{task_id}"


def _run_task(task_id: int) -> None:
    """Job body: one collect cycle (search → upsert)."""
    from app.db import SessionLocal
    from app.services.listings import run_collect_cycle
    from app.services.run_logs import write_run_log
    from app.models.tables import MonitorTask

    logger.info("[scheduler] running task_id=%s", task_id)
    db = SessionLocal()
    try:
        task = db.get(MonitorTask, task_id)
        write_run_log(
            db,
            level="info",
            event="scheduler",
            message=f"调度器触发任务 #{task_id}",
            task_id=task_id,
            config_id=task.config_id if task else None,
            details={"source": "apscheduler"},
        )
        result = asyncio.run(run_collect_cycle(db, task_id, allow_paused=False))
        logger.info("[scheduler] task_id=%s result=%s", task_id, result)
        if not result.get("ok"):
            # collect cycle already wrote error; add scheduler context if missing
            pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("[scheduler] task_id=%s crashed", task_id)
        try:
            write_run_log(
                db,
                level="error",
                event="error",
                message=f"调度执行异常：{exc}"[:512],
                task_id=task_id,
                details={"error": "scheduler_exception"},
            )
        except Exception:  # noqa: BLE001
            logger.exception("[scheduler] failed to write error log")
    finally:
        db.close()


def sync_task_job(task: "MonitorTask") -> None:
    """Register / pause / remove a job according to task.status."""
    if _scheduler is None:
        return
    jid = _job_id(task.id)
    existing = _scheduler.get_job(jid)
    if task.status == "running":
        trigger = IntervalTrigger(minutes=max(task.interval_minutes, 1))
        if existing:
            existing.reschedule(trigger=trigger)
            if existing.next_run_time is None:
                existing.resume()
            logger.info("[scheduler] rescheduled %s every %sm", jid, task.interval_minutes)
        else:
            _scheduler.add_job(
                _run_task,
                trigger=trigger,
                id=jid,
                args=[task.id],
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("[scheduler] registered %s every %sm", jid, task.interval_minutes)
    else:
        if existing:
            _scheduler.remove_job(jid)
            logger.info("[scheduler] removed %s (status=%s)", jid, task.status)


def remove_task_job(task_id: int) -> None:
    if _scheduler is None:
        return
    jid = _job_id(task_id)
    if _scheduler.get_job(jid):
        _scheduler.remove_job(jid)
        logger.info("[scheduler] removed %s", jid)


def start_scheduler() -> None:
    global _scheduler
    if not SCHEDULER_ENABLED:
        logger.info("[scheduler] disabled via SCHEDULER_ENABLED")
        return
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=SCHEDULER_TIMEZONE)
    _scheduler.start()
    logger.info("[scheduler] started timezone=%s", SCHEDULER_TIMEZONE)

    from app.db import SessionLocal
    from app.models.tables import MonitorTask

    db = SessionLocal()
    try:
        for task in db.query(MonitorTask).filter(MonitorTask.status == "running").all():
            sync_task_job(task)
    finally:
        db.close()


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("[scheduler] shut down")
