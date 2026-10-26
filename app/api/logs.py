"""Run logs API."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BeforeValidator
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.schemas import RunLogOut
from app.models.tables import RunLog
from app.services.run_logs import list_run_logs

router = APIRouter(prefix="/api", tags=["logs"])

def _empty_to_none(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


OptionalQueryInt = Annotated[Optional[int], BeforeValidator(_empty_to_none)]



@router.get("/logs", response_model=list[RunLogOut])
def get_logs(
    limit: int = Query(50, ge=1, le=500),
    task_id: OptionalQueryInt = None,
    level: str | None = None,
    event: str | None = None,
    db: Session = Depends(get_db),
) -> list[RunLog]:
    """List recent run logs (newest first). Filter by task_id / level / event."""
    return list_run_logs(
        db, limit=limit, task_id=task_id, level=level, event=event
    )
