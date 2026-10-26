"""Push after evaluation: dedupe + run_log events."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.tables import Listing, PushRecord
from app.push.notify import (
    any_channel_configured,
    deliver_push,
    should_push_verdict,
)
from app.services.run_logs import write_run_log

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.utcnow()


def was_pushed(db: Session, listing_id: int, verdict: str) -> bool:
    row = (
        db.query(PushRecord)
        .filter(PushRecord.listing_id == listing_id, PushRecord.verdict == verdict)
        .first()
    )
    return row is not None


def mark_pushed(
    db: Session,
    listing_id: int,
    verdict: str,
    *,
    channels: Optional[list[str]] = None,
    commit: bool = True,
) -> PushRecord:
    existing = (
        db.query(PushRecord)
        .filter(PushRecord.listing_id == listing_id, PushRecord.verdict == verdict)
        .first()
    )
    if existing:
        existing.pushed_at = _utcnow()
        if channels is not None:
            existing.channels = channels
        row = existing
    else:
        row = PushRecord(
            listing_id=listing_id,
            verdict=verdict,
            pushed_at=_utcnow(),
            channels=list(channels or []),
        )
        db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def maybe_push_evaluation(
    db: Session,
    listing: Listing,
    *,
    verdict: Optional[str],
    reason: Optional[str] = None,
    task_id: Optional[int] = None,
    config_id: Optional[int] = None,
    commit_logs: bool = True,
) -> dict[str, Any]:
    """
    Push for new 推荐 (and 待观察 when PUSH_INCLUDE_WATCH).
    Dedupes by listing_id + verdict. Writes push_sent / push_skipped run_logs.
    """
    result: dict[str, Any] = {
        "pushed": False,
        "skipped": True,
        "skip_reason": None,
        "channels": [],
    }

    if not should_push_verdict(verdict):
        result["skip_reason"] = "verdict_not_eligible"
        return result

    assert verdict is not None
    lid = listing.id
    cid = config_id if config_id is not None else listing.config_id

    if not any_channel_configured():
        result["skip_reason"] = "not_configured"
        write_run_log(
            db,
            level="info",
            event="push_skipped",
            message=f"推送跳过：未配置 webhook/Telegram（{listing.title[:40]}）",
            task_id=task_id,
            config_id=cid,
            details={
                "listing_id": lid,
                "verdict": verdict,
                "reason": "not_configured",
            },
            commit=commit_logs,
        )
        return result

    if was_pushed(db, lid, verdict):
        result["skip_reason"] = "already_pushed"
        write_run_log(
            db,
            level="info",
            event="push_skipped",
            message=f"推送跳过：已推送过「{verdict}」listing#{lid}",
            task_id=task_id,
            config_id=cid,
            details={
                "listing_id": lid,
                "verdict": verdict,
                "reason": "already_pushed",
            },
            commit=commit_logs,
        )
        return result

    delivery = deliver_push(
        title=listing.title or "",
        price=listing.price,
        reason=reason,
        url=listing.url,
        verdict=verdict,
        listing_id=lid,
    )

    if not delivery.get("ok"):
        result["skip_reason"] = "delivery_failed"
        result["errors"] = delivery.get("errors") or []
        write_run_log(
            db,
            level="warn",
            event="push_skipped",
            message=f"推送失败：{listing.title[:40]}（{', '.join(result['errors']) or 'unknown'}）"[:512],
            task_id=task_id,
            config_id=cid,
            details={
                "listing_id": lid,
                "verdict": verdict,
                "reason": "delivery_failed",
                "errors": result["errors"],
            },
            commit=commit_logs,
        )
        return result

    mark_pushed(db, lid, verdict, channels=delivery.get("channels") or [], commit=commit_logs)
    write_run_log(
        db,
        level="info",
        event="push_sent",
        message=(
            f"已推送「{verdict}」：{listing.title[:48]}"
            f"（渠道：{','.join(delivery.get('channels') or [])}）"
        )[:512],
        task_id=task_id,
        config_id=cid,
        details={
            "listing_id": lid,
            "verdict": verdict,
            "channels": delivery.get("channels"),
            "price": listing.price,
            "url": listing.url,
        },
        commit=commit_logs,
    )
    result["pushed"] = True
    result["skipped"] = False
    result["channels"] = delivery.get("channels") or []
    return result
