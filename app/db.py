"""SQLite database engine and session helpers."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# Project root: /workspace/goofish-monitor
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "monitor.db"

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")


class Base(DeclarativeBase):
    pass


# check_same_thread=False required for SQLite + FastAPI multi-thread access
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency: yield a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create data directory and all tables; light-migrate new columns."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Import models so metadata is registered
    from app.models import tables  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_listing_columns()
    _migrate_evaluation_columns()


def _migrate_listing_columns() -> None:
    """SQLite create_all does not ADD columns — patch Listing freshness fields."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    from sqlalchemy import text

    alters = {
        "first_seen": "ALTER TABLE listings ADD COLUMN first_seen DATETIME",
        "last_seen": "ALTER TABLE listings ADD COLUMN last_seen DATETIME",
        "content_hash": "ALTER TABLE listings ADD COLUMN content_hash VARCHAR(64)",
    }
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(listings)")).fetchall()
        if not rows:
            return
        existing = {r[1] for r in rows}
        for col, sql in alters.items():
            if col not in existing:
                conn.execute(text(sql))


def _migrate_evaluation_columns() -> None:
    """Add verdict / tags / config_id on evaluations for rule pipeline."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    from sqlalchemy import text

    alters = {
        "config_id": "ALTER TABLE evaluations ADD COLUMN config_id INTEGER",
        "verdict": "ALTER TABLE evaluations ADD COLUMN verdict VARCHAR(16)",
        "tags": "ALTER TABLE evaluations ADD COLUMN tags JSON",
    }
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(evaluations)")).fetchall()
        if not rows:
            return
        existing = {r[1] for r in rows}
        for col, sql in alters.items():
            if col not in existing:
                conn.execute(text(sql))
