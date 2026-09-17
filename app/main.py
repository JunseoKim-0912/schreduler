from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.compliance_reports import router as compliance_reports_router
from app.api.date_ranges import router as date_ranges_router
from app.api.events import router as events_router
from app.api.health import router as health_router
from app.api.locations import router as locations_router
from app.core.config import settings
from app.core.scheduler import shutdown_scheduler, start_scheduler
from app.services.engagement_service import register_escalation_job


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    start_scheduler()
    register_escalation_job()
    yield
    shutdown_scheduler()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.include_router(health_router)
app.include_router(events_router)
app.include_router(date_ranges_router)
app.include_router(locations_router)
app.include_router(compliance_reports_router)
