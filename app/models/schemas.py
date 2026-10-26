"""Pydantic request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


SellerPreference = Literal["personal_only", "any"]
TaskStatus = Literal["running", "paused", "error"]
SortDefault = Literal["newest", "price_asc", "price_desc", "relevance"]


# ── Product Config ──────────────────────────────────────────────


class ProductConfigBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    keyword: str = Field(..., min_length=1, max_length=256)
    keyword_variants: list[str] = Field(default_factory=list)
    brand: Optional[str] = None
    model: Optional[str] = None
    size: Optional[str] = None
    ram: Optional[str] = None
    storage: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    condition_min: Optional[str] = None
    battery_health_min: Optional[int] = Field(None, ge=0, le=100)
    seller_preference: SellerPreference = "any"
    hard_requirements: dict[str, Any] = Field(default_factory=dict)
    soft_preferences: dict[str, Any] = Field(default_factory=dict)
    exclude_keywords: list[str] = Field(default_factory=list)
    sort_default: SortDefault = "newest"
    enabled: bool = True
    notes: Optional[str] = None


class ProductConfigCreate(ProductConfigBase):
    pass


class ProductConfigUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    keyword: Optional[str] = Field(None, min_length=1, max_length=256)
    keyword_variants: Optional[list[str]] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    size: Optional[str] = None
    ram: Optional[str] = None
    storage: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    condition_min: Optional[str] = None
    battery_health_min: Optional[int] = Field(None, ge=0, le=100)
    seller_preference: Optional[SellerPreference] = None
    hard_requirements: Optional[dict[str, Any]] = None
    soft_preferences: Optional[dict[str, Any]] = None
    exclude_keywords: Optional[list[str]] = None
    sort_default: Optional[SortDefault] = None
    enabled: Optional[bool] = None
    notes: Optional[str] = None


class ProductConfigOut(ProductConfigBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Monitor Task ────────────────────────────────────────────────


class MonitorTaskCreate(BaseModel):
    config_id: int
    interval_minutes: int = Field(30, ge=1, le=1440)
    status: TaskStatus = "paused"


class MonitorTaskUpdate(BaseModel):
    config_id: Optional[int] = None
    interval_minutes: Optional[int] = Field(None, ge=1, le=1440)
    status: Optional[TaskStatus] = None


class MonitorTaskOut(BaseModel):
    id: int
    config_id: int
    interval_minutes: int
    status: TaskStatus
    last_run_at: Optional[datetime] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Listing / Evaluation (read-only stubs) ──────────────────────


class ListingOut(BaseModel):
    id: int
    config_id: Optional[int] = None
    task_id: Optional[int] = None
    external_id: Optional[str] = None
    title: str
    price: Optional[float] = None
    url: Optional[str] = None
    seller_type: Optional[str] = None
    condition: Optional[str] = None
    published_at: Optional[datetime] = None
    collected_at: datetime
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    content_hash: Optional[str] = None

    model_config = {"from_attributes": True}


class CollectRunOut(BaseModel):
    ok: bool
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    errors: int = 0
    evaluated: int = 0
    eval_skipped: int = 0
    error: Optional[str] = None
    error_message: Optional[str] = None
    details: Optional[dict[str, Any]] = None


class DryRunRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=256)
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    exclude_keywords: list[str] = Field(default_factory=list)
    page_size: int = Field(20, ge=1, le=50)


class DryRunItemOut(BaseModel):
    external_id: str
    title: str
    price: Optional[float] = None
    url: Optional[str] = None
    published_at: Optional[str] = None
    seller_type: Optional[str] = None
    condition: Optional[str] = None


class DryRunOut(BaseModel):
    ok: bool
    count: int = 0
    items: list[DryRunItemOut] = Field(default_factory=list)
    error: Optional[str] = None
    error_message: Optional[str] = None


class EvaluationOut(BaseModel):
    id: int
    listing_id: int
    config_id: Optional[int] = None
    verdict: Optional[str] = None  # 推荐 | 待观察 | 不推荐
    tags: Optional[list[str]] = None
    score: Optional[float] = None
    passed_hard: Optional[bool] = None
    soft_score: Optional[float] = None
    reason: Optional[str] = None
    analyzer: Optional[str] = None
    details: Optional[dict[str, Any]] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class HealthOut(BaseModel):
    status: str = "ok"
    service: str = "goofish-monitor"
    scheduler: str = "apscheduler"
    collector_backend: str = "stub"


# ── Run logs ─────────────────────────────────────────────────────


class RunLogOut(BaseModel):
    id: int
    task_id: Optional[int] = None
    config_id: Optional[int] = None
    level: str
    event: str
    message: str
    details: Optional[dict[str, Any]] = None
    created_at: datetime

    model_config = {"from_attributes": True}

