"""Goofish monitor FastAPI entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()

from app.api import accounts, collectors, configs, dashboard, health, listings, logs, tasks
from app.db import PROJECT_ROOT, init_db
from app.workers.scheduler import shutdown_scheduler, start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("database ready")
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(
    title="Goofish Monitor",
    description="闲鱼关键词监控 + 自定义商品配置 + 评估流水线（采集：stub / Playwright 联调）",
    version="0.1.0",
    lifespan=lifespan,
)

static_dir = PROJECT_ROOT / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(dashboard.router)
app.include_router(health.router)
app.include_router(configs.router)
app.include_router(tasks.router)
app.include_router(listings.router)
app.include_router(collectors.router)
app.include_router(accounts.router)
app.include_router(logs.router)
