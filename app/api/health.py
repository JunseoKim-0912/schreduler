from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.version import APP_VERSION

router = APIRouter(tags=["health"])


class HealthRead(BaseModel):
    status: str = Field(examples=["ok"])
    version: str = Field(description="앱 버전 (app/core/version.py). 화면 구석의 'Prototype · v…' 표시에 쓴다", examples=["0.1.0"])
    demo_mode: bool = Field(description="POST /auth/demo가 켜져 있는지 (DEMO_MODE_ENABLED). 로그인 화면이 데모 버튼을 보일지 정한다")


@router.get("/health", response_model=HealthRead, summary="서버 상태 확인")
def get_health() -> HealthRead:
    """서버가 요청을 받을 수 있는지 확인한다. 모니터링·헬스체크용."""
    return HealthRead(status="ok", version=APP_VERSION, demo_mode=settings.demo_mode_enabled)
