"""Persist structured run logs for collect / evaluate / scheduler."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.tables import RunLog

logger = logging.getLogger(__name__)

VALID_LEVELS = frozenset({"info", "warn", "error"})
VALID_EVENTS = frozenset(
    {
        "collect_start",
        "collect_done",
        "evaluate_done",
        "error",
        "scheduler",
        "login_required",
        "push_sent",
        "push_skipped",
    }
)

EVENT_LABELS_CN = {
    "collect_start": "开始采集",
    "collect_done": "采集完成",
    "evaluate_done": "评估完成",
    "error": "错误",
    "scheduler": "调度",
    "login_required": "需要登录",
    "push_sent": "推送成功",
    "push_skipped": "推送跳过",
}


def write_run_log(
    db: Session,
    *,
    level: str = "info",
    event: str,
    message: str,
    task_id: Optional[int] = None,
    config_id: Optional[int] = None,
    details: Optional[dict[str, Any]] = None,
    commit: bool = True,
) -> RunLog:
    """Insert one run_logs row. Safe to call inside an existing transaction when commit=False."""
    lvl = (level or "info").lower()
    if lvl not in VALID_LEVELS:
        lvl = "info"
    ev = (event or "error").lower()
    if ev not in VALID_EVENTS:
        # Allow unknown events but keep them short
        ev = ev[:32] or "error"
    row = RunLog(
        task_id=task_id,
        config_id=config_id,
        level=lvl,
        event=ev,
        message=(message or "")[:512],
        details=details,
    )
    db.add(row)
    if commit:
        try:
            db.commit()
            db.refresh(row)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("[run_log] failed to persist event=%s", ev)
            raise
    else:
        db.flush()
    return row


def list_run_logs(
    db: Session,
    *,
    limit: int = 50,
    task_id: Optional[int] = None,
    level: Optional[str] = None,
    event: Optional[str] = None,
) -> list[RunLog]:
    q = db.query(RunLog)
    if task_id is not None:
        q = q.filter(RunLog.task_id == task_id)
    if level is not None:
        q = q.filter(RunLog.level == level.lower())
    if event is not None:
        q = q.filter(RunLog.event == event)
    return q.order_by(RunLog.id.desc()).limit(min(max(limit, 1), 500)).all()
