"""Health check endpoint."""

import os

from fastapi import APIRouter

from app.models.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(
        status="ok",
        service="goofish-monitor",
        scheduler="apscheduler",
        collector_backend=os.getenv("COLLECTOR_BACKEND", "stub"),
    )
