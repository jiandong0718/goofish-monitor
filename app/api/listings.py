"""Listings / evaluations APIs."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BeforeValidator
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.schemas import EvaluationOut, ListingOut
from app.models.tables import Evaluation, Listing, ProductConfig
from app.services.evaluate import evaluate_and_persist

router = APIRouter(prefix="/api", tags=["listings"])

def _empty_to_none(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


OptionalQueryInt = Annotated[Optional[int], BeforeValidator(_empty_to_none)]



@router.get("/listings", response_model=list[ListingOut])
def list_listings(
    config_id: OptionalQueryInt = None,
    task_id: OptionalQueryInt = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[Listing]:
    q = db.query(Listing)
    if config_id is not None:
        q = q.filter(Listing.config_id == config_id)
    if task_id is not None:
        q = q.filter(Listing.task_id == task_id)
    return q.order_by(Listing.id.desc()).limit(min(limit, 200)).all()


@router.get("/listings/{listing_id}", response_model=ListingOut)
def get_listing(listing_id: int, db: Session = Depends(get_db)) -> Listing:
    row = db.get(Listing, listing_id)
    if not row:
        raise HTTPException(status_code=404, detail="listing not found")
    return row


@router.get("/listings/{listing_id}/evaluations", response_model=list[EvaluationOut])
def list_evaluations(
    listing_id: int, db: Session = Depends(get_db)
) -> list[Evaluation]:
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="listing not found")
    return (
        db.query(Evaluation)
        .filter(Evaluation.listing_id == listing_id)
        .order_by(Evaluation.id.desc())
        .all()
    )


@router.post("/listings/{listing_id}/evaluate", response_model=EvaluationOut)
def evaluate_listing(
    listing_id: int,
    force: bool = True,
    config_id: int | None = None,
    db: Session = Depends(get_db),
) -> Evaluation:
    """Manual rule re-evaluation. Defaults to force=True."""
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="listing not found")
    cid = config_id if config_id is not None else listing.config_id
    if cid is None:
        raise HTTPException(
            status_code=400, detail="listing has no config_id; pass ?config_id="
        )
    config = db.get(ProductConfig, cid)
    if not config:
        raise HTTPException(status_code=404, detail="config not found")
    row, _ = evaluate_and_persist(db, listing, config, force=force, commit=True)
    if row is None:
        raise HTTPException(status_code=500, detail="evaluation failed")
    return row


@router.get("/evaluations", response_model=list[EvaluationOut])
def list_all_evaluations(
    verdict: str | None = None,
    config_id: int | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[Evaluation]:
    q = db.query(Evaluation)
    if verdict is not None:
        q = q.filter(Evaluation.verdict == verdict)
    if config_id is not None:
        q = q.filter(Evaluation.config_id == config_id)
    return q.order_by(Evaluation.id.desc()).limit(min(limit, 200)).all()
