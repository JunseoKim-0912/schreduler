from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.actions import router as actions_router
from app.api.assistant import router as assistant_router
from app.api.compliance_reports import router as compliance_reports_router
from app.api.daily_actual_logs import router as daily_actual_logs_router
from app.api.date_ranges import router as date_ranges_router
from app.api.event_instances import router as event_instances_router
from app.api.events import router as events_router
from app.api.health import router as health_router
from app.api.locations import router as locations_router
from app.api.personas import router as personas_router
from app.api.points import router as points_router
from app.api.sleep import router as sleep_router
from app.api.tasks import router as tasks_router
from app.api.usage import router as usage_router
from app.api.users import router as users_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.core.openapi import APP_DESCRIPTION, TAGS_METADATA
from app.core.scheduler import shutdown_scheduler, start_scheduler
from app.frontend_serving import FRONTEND_DIR, RevalidatedStaticFiles
from app.frontend_serving import router as frontend_router
from app.services import notification
from app.services.daily_checkin import register_daily_checkin_job
from app.services.engagement_service import register_escalation_job
from app.services.llm_client import validate_assistant_settings
from app.services.llm_pricing import validate_configured_models
from app.services.points import register_daily_points_job
from app.services.sleep_checkin import register_sleep_checkin_job

setup_logging()

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    validate_assistant_settings()
    validate_configured_models()
    start_scheduler()
    register_escalation_job()
    register_sleep_checkin_job()
    register_daily_checkin_job()
    register_daily_points_job()
    notification.register_upcoming_notifications()
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
app.include_router(assistant_router)
app.include_router(event_instances_router)
app.include_router(date_ranges_router)
app.include_router(locations_router)
app.include_router(compliance_reports_router)
app.include_router(sleep_router)
app.include_router(daily_actual_logs_router)
app.include_router(personas_router)
app.include_router(points_router)
app.include_router(tasks_router)
app.include_router(users_router)
app.include_router(actions_router)
app.include_router(usage_router)

# API와 같은 서버·같은 origin에서 정적 프론트엔드를 서빙한다 (CORS 불필요). /app/와 /app/v/<버전>/…는 frontend_router가
# 먼저 받고(캐시 무효화), 나머지 파일은 아래 마운트가 서빙한다.
app.include_router(frontend_router)
app.mount("/app", RevalidatedStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
