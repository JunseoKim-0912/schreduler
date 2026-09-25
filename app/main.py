from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.compliance_reports import router as compliance_reports_router
from app.api.daily_actual_logs import router as daily_actual_logs_router
from app.api.date_ranges import router as date_ranges_router
from app.api.events import router as events_router
from app.api.health import router as health_router
from app.api.locations import router as locations_router
from app.api.personas import router as personas_router
from app.api.points import router as points_router
from app.api.sleep import router as sleep_router
from app.api.tasks import router as tasks_router
from app.api.users import router as users_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.core.openapi import APP_DESCRIPTION, TAGS_METADATA
from app.core.scheduler import shutdown_scheduler, start_scheduler
from app.services.daily_checkin import register_daily_checkin_job
from app.services.engagement_service import register_escalation_job
from app.services.points import register_daily_points_job
from app.services.sleep_checkin import register_sleep_checkin_job

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    start_scheduler()
    register_escalation_job()
    register_sleep_checkin_job()
    register_daily_checkin_job()
    register_daily_points_job()
    yield
    shutdown_scheduler()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=APP_DESCRIPTION,
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)
register_exception_handlers(app)

app.include_router(health_router)
app.include_router(events_router)
app.include_router(date_ranges_router)
app.include_router(locations_router)
app.include_router(compliance_reports_router)
app.include_router(sleep_router)
app.include_router(daily_actual_logs_router)
app.include_router(personas_router)
app.include_router(points_router)
app.include_router(tasks_router)
app.include_router(users_router)
