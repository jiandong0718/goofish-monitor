"""Persist rule-based (+ optional AI) evaluations for listings.

Persistence choice (documented):
- Always store a ``rules`` Evaluation when rules run.
- When AI runs, store a separate ``ai`` Evaluation (same listing/config,
  ``analyzer='ai'``) with AI reason / tags / seller_profile.
- Skipping unchanged content_hash skips both; force re-runs both.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.analyzers.ai import (
    ANALYZER_NAME as AI_ANALYZER,
    evaluate_ai,
    should_run_ai,
)
from app.analyzers.rules import ANALYZER_NAME, evaluate_rules
from app.models.tables import Evaluation, Listing, ProductConfig
from app.services.run_logs import write_run_log

logger = logging.getLogger(__name__)


def _latest_eval(
    db: Session,
    listing_id: int,
    config_id: Optional[int],
    analyzer: str,
) -> Optional[Evaluation]:
    q = (
        db.query(Evaluation)
        .filter(
            Evaluation.listing_id == listing_id,
            Evaluation.analyzer == analyzer,
        )
        .order_by(Evaluation.id.desc())
    )
    if config_id is not None:
        q = q.filter(Evaluation.config_id == config_id)
    return q.first()


def _latest_rules_eval(
    db: Session, listing_id: int, config_id: Optional[int]
) -> Optional[Evaluation]:
    return _latest_eval(db, listing_id, config_id, ANALYZER_NAME)


def _persist_ai_evaluation(
    db: Session,
    listing: Listing,
    config: ProductConfig,
    rules_result: dict[str, Any],
    *,
    commit: bool,
) -> Optional[Evaluation]:
    """Run AI when gated; insert analyzer=ai row. Returns row or None."""
    if not should_run_ai(rules_result.get("verdict")):
        logger.debug(
            "[eval] skip AI listing_id=%s verdict=%s",
            listing.id,
            rules_result.get("verdict"),
        )
        return None

    ai_result = evaluate_ai(listing, config, rules_result=rules_result)
    if not ai_result:
        return None

    details = dict(ai_result.get("details") or {})
    details["content_hash"] = listing.content_hash
    details["verdict"] = ai_result.get("verdict")
    details["tags"] = ai_result.get("tags")
    details["seller_profile"] = ai_result.get("seller_profile")
    details["rules_verdict"] = rules_result.get("verdict")

    row = Evaluation(
        listing_id=listing.id,
        config_id=config.id,
        verdict=ai_result.get("verdict"),
        tags=ai_result.get("tags"),
        score=ai_result.get("score"),
        passed_hard=ai_result.get("passed_hard"),
        soft_score=ai_result.get("soft_score"),
        reason=ai_result.get("reason"),
        analyzer=AI_ANALYZER,
        details=details,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    logger.info(
        "[eval] ai listing_id=%s verdict=%s seller_profile=%s",
        listing.id,
        row.verdict,
        details.get("seller_profile"),
    )
    return row


def evaluate_and_persist(
    db: Session,
    listing: Listing,
    config: ProductConfig,
    *,
    force: bool = False,
    commit: bool = True,
    task_id: Optional[int] = None,
    push: bool = True,
) -> tuple[Optional[Evaluation], bool]:
    """
    Run rules analyzer and insert an Evaluation row; optionally AI after.

    Skip when force=False, a rules evaluation already exists for this
    listing (+ config_id), and its details.content_hash matches the
    listing's current content_hash. Documented choice: do NOT re-eval
    unchanged content; re-eval when content_hash changes or force=True.

    Persistence: rules row always when created; AI row added when AI runs
    (analyzer field distinguishes). Returns (rules_evaluation, created_new).
    """
    existing = _latest_rules_eval(db, listing.id, config.id)
    if (
        not force
        and existing is not None
        and listing.content_hash
        and isinstance(existing.details, dict)
        and existing.details.get("content_hash") == listing.content_hash
    ):
        logger.debug(
            "[eval] skip listing_id=%s hash=%s", listing.id, listing.content_hash
        )
        return existing, False

    result = evaluate_rules(listing, config)
    details = dict(result.get("details") or {})
    details["content_hash"] = listing.content_hash
    details["verdict"] = result["verdict"]
    details["tags"] = result["tags"]

    row = Evaluation(
        listing_id=listing.id,
        config_id=config.id,
        verdict=result["verdict"],
        tags=result["tags"],
        score=result.get("score"),
        passed_hard=result.get("passed_hard"),
        soft_score=result.get("soft_score"),
        reason=result.get("reason"),
        analyzer=ANALYZER_NAME,
        details=details,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    logger.info(
        "[eval] listing_id=%s verdict=%s tags=%s",
        listing.id,
        row.verdict,
        row.tags,
    )

    # AI after rules (same commit batch when commit=False)
    _persist_ai_evaluation(db, listing, config, result, commit=commit)

    if commit and push:
        from app.push.service import maybe_push_evaluation

        maybe_push_evaluation(
            db,
            listing,
            verdict=row.verdict,
            reason=row.reason,
            task_id=task_id,
            config_id=config.id,
            commit_logs=True,
        )
    return row, True


def evaluate_listings_for_config(
    db: Session,
    listing_ids: list[int],
    config: ProductConfig,
    *,
    force: bool = False,
    task_id: Optional[int] = None,
) -> dict[str, int]:
    """Evaluate a batch of listing ids; returns evaluated / skipped counts.

    After commit, pushes for new 推荐 (and 待观察 if PUSH_INCLUDE_WATCH).
    """
    from app.push.service import maybe_push_evaluation

    evaluated = 0
    skipped = 0
    pending_push: list[tuple[Listing, Evaluation]] = []
    for lid in listing_ids:
        listing = db.get(Listing, lid)
        if not listing:
            continue
        row, created = evaluate_and_persist(
            db, listing, config, force=force, commit=False, task_id=task_id, push=False
        )
        if created:
            evaluated += 1
            if row is not None:
                pending_push.append((listing, row))
        else:
            skipped += 1
    db.commit()
    pushed = 0
    for listing, row in pending_push:
        # refresh after commit
        db.refresh(listing)
        db.refresh(row)
        out = maybe_push_evaluation(
            db,
            listing,
            verdict=row.verdict,
            reason=row.reason,
            task_id=task_id,
            config_id=config.id,
            commit_logs=True,
        )
        if out.get("pushed"):
            pushed += 1
    write_run_log(
        db,
        level="info",
        event="evaluate_done",
        message=(
            f"评估完成「{config.name}」：新评 {evaluated}，跳过 {skipped}"
            f"（共 {len(listing_ids)} 条），推送 {pushed}"
        ),
        task_id=task_id,
        config_id=config.id,
        details={
            "evaluated": evaluated,
            "skipped": skipped,
            "listing_count": len(listing_ids),
            "force": force,
            "pushed": pushed,
        },
    )
    return {"evaluated": evaluated, "skipped": skipped, "pushed": pushed}
