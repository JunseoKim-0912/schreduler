from fastapi import FastAPI

from app.api.events import router as events_router
from app.api.health import router as health_router
from app.core.config import settings

app = FastAPI(title=settings.app_name)

app.include_router(health_router)
app.include_router(events_router)
