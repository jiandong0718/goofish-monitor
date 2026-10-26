"""Monitor task CRUD + pause/resume."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.schemas import CollectRunOut, MonitorTaskCreate, MonitorTaskOut, MonitorTaskUpdate
from app.models.tables import MonitorTask, ProductConfig
from app.workers.scheduler import sync_task_job

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("", response_model=list[MonitorTaskOut])
def list_tasks(db: Session = Depends(get_db)) -> list[MonitorTask]:
    return db.query(MonitorTask).order_by(MonitorTask.id.desc()).all()


@router.post("", response_model=MonitorTaskOut, status_code=status.HTTP_201_CREATED)
def create_task(
    body: MonitorTaskCreate, db: Session = Depends(get_db)
) -> MonitorTask:
    cfg = db.get(ProductConfig, body.config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="config not found")
    row = MonitorTask(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    sync_task_job(row)
    return row


@router.get("/{task_id}", response_model=MonitorTaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)) -> MonitorTask:
    row = db.get(MonitorTask, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    return row


@router.put("/{task_id}", response_model=MonitorTaskOut)
def update_task(
    task_id: int, body: MonitorTaskUpdate, db: Session = Depends(get_db)
) -> MonitorTask:
    row = db.get(MonitorTask, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    data = body.model_dump(exclude_unset=True)
    if "config_id" in data:
        cfg = db.get(ProductConfig, data["config_id"])
        if not cfg:
            raise HTTPException(status_code=404, detail="config not found")
    for key, value in data.items():
        setattr(row, key, value)
    # Clear error when user manually changes status away from error
    if data.get("status") in ("running", "paused"):
        row.error_message = None
    db.commit()
    db.refresh(row)
    sync_task_job(row)
    return row


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int, db: Session = Depends(get_db)) -> None:
    row = db.get(MonitorTask, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    from app.workers.scheduler import remove_task_job

    remove_task_job(task_id)
    db.delete(row)
    db.commit()


@router.post("/{task_id}/pause", response_model=MonitorTaskOut)
def pause_task(task_id: int, db: Session = Depends(get_db)) -> MonitorTask:
    row = db.get(MonitorTask, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    row.status = "paused"
    row.error_message = None
    db.commit()
    db.refresh(row)
    sync_task_job(row)
    return row


@router.post("/{task_id}/resume", response_model=MonitorTaskOut)
def resume_task(task_id: int, db: Session = Depends(get_db)) -> MonitorTask:
    row = db.get(MonitorTask, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    row.status = "running"
    row.error_message = None
    db.commit()
    db.refresh(row)
    sync_task_job(row)
    return row


@router.post("/{task_id}/run-once", response_model=CollectRunOut)
async def run_task_once(task_id: int, db: Session = Depends(get_db)) -> CollectRunOut:
    """联调：立即执行一次采集周期（search → upsert），返回计数。"""
    from app.services.listings import run_collect_cycle

    row = db.get(MonitorTask, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    result = await run_collect_cycle(db, task_id, allow_paused=True)
    return CollectRunOut(
        ok=bool(result.get("ok")),
        fetched=int(result.get("fetched") or 0),
        inserted=int(result.get("inserted") or 0),
        updated=int(result.get("updated") or 0),
        errors=int(result.get("errors") or 0),
        evaluated=int(result.get("evaluated") or 0),
        eval_skipped=int(result.get("eval_skipped") or 0),
        error=result.get("error"),
        error_message=result.get("error_message"),
        details=result.get("details"),
    )
