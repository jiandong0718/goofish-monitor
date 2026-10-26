"""SQLAlchemy ORM tables."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class ProductConfig(Base):
    """商品监控配置：硬条件 + 软偏好。"""

    __tablename__ = "product_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    keyword: Mapped[str] = mapped_column(String(256), nullable=False)
    keyword_variants: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    # Optional specs
    brand: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    size: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ram: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    storage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Price / condition
    price_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    condition_min: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    battery_health_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # personal_only | any
    seller_preference: Mapped[str] = mapped_column(String(32), default="any")
    # Hard requirements vs soft preferences (arbitrary JSON dicts)
    hard_requirements: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    soft_preferences: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    exclude_keywords: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    # newest | price_asc | price_desc | ...
    sort_default: Mapped[str] = mapped_column(String(32), default="newest")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    tasks: Mapped[list["MonitorTask"]] = relationship(
        "MonitorTask", back_populates="config", cascade="all, delete-orphan"
    )


class MonitorTask(Base):
    """监控任务：绑定配置 + 调度间隔。"""

    __tablename__ = "monitor_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("product_configs.id"), nullable=False
    )
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    # running | paused | error
    status: Mapped[str] = mapped_column(String(16), default="paused")
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    config: Mapped["ProductConfig"] = relationship(
        "ProductConfig", back_populates="tasks"
    )


class Listing(Base):
    """采集到的商品列表项。"""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("product_configs.id"), nullable=True
    )
    task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("monitor_tasks.id"), nullable=True
    )
    external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    seller_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    condition: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    raw: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Dedup / freshness tracking (platform = goofish for now; key = external_id)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    evaluations: Mapped[list["Evaluation"]] = relationship(
        "Evaluation", back_populates="listing", cascade="all, delete-orphan"
    )


class Evaluation(Base):
    """对 Listing 的规则/AI 评估结果。"""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("listings.id"), nullable=False
    )
    config_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("product_configs.id"), nullable=True
    )
    # 推荐 | 待观察 | 不推荐
    verdict: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    tags: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    passed_hard: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    soft_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    analyzer: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    listing: Mapped["Listing"] = relationship("Listing", back_populates="evaluations")


class RunLog(Base):
    """调度 / 采集 / 评估运行日志。"""

    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("monitor_tasks.id"), nullable=True, index=True
    )
    config_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("product_configs.id"), nullable=True, index=True
    )
    # info | warn | error
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)
    # collect_start | collect_done | evaluate_done | error
    event: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class PushRecord(Base):
    """Dedupe: one push per listing_id + verdict."""

    __tablename__ = "push_records"
    __table_args__ = (
        UniqueConstraint("listing_id", "verdict", name="uq_push_listing_verdict"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("listings.id"), nullable=False, index=True
    )
    # 推荐 | 待观察
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    channels: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    pushed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
