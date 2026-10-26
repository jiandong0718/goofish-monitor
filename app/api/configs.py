"""Product config CRUD."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.schemas import (
    ProductConfigCreate,
    ProductConfigOut,
    ProductConfigUpdate,
)
from app.models.tables import ProductConfig

router = APIRouter(prefix="/api/configs", tags=["configs"])


@router.get("", response_model=list[ProductConfigOut])
def list_configs(db: Session = Depends(get_db)) -> list[ProductConfig]:
    return db.query(ProductConfig).order_by(ProductConfig.id.desc()).all()


@router.post("", response_model=ProductConfigOut, status_code=status.HTTP_201_CREATED)
def create_config(
    body: ProductConfigCreate, db: Session = Depends(get_db)
) -> ProductConfig:
    row = ProductConfig(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/{config_id}", response_model=ProductConfigOut)
def get_config(config_id: int, db: Session = Depends(get_db)) -> ProductConfig:
    row = db.get(ProductConfig, config_id)
    if not row:
        raise HTTPException(status_code=404, detail="config not found")
    return row


@router.put("/{config_id}", response_model=ProductConfigOut)
def update_config(
    config_id: int, body: ProductConfigUpdate, db: Session = Depends(get_db)
) -> ProductConfig:
    row = db.get(ProductConfig, config_id)
    if not row:
        raise HTTPException(status_code=404, detail="config not found")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_config(config_id: int, db: Session = Depends(get_db)) -> None:
    row = db.get(ProductConfig, config_id)
    if not row:
        raise HTTPException(status_code=404, detail="config not found")
    db.delete(row)
    db.commit()
