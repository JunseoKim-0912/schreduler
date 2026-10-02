from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import settings

router = APIRouter(tags=["health"])


class HealthRead(BaseModel):
    status: str = Field(examples=["ok"])
    demo_mode: bool = Field(description="POST /auth/demo가 켜져 있는지 (DEMO_MODE_ENABLED). 로그인 화면이 데모 버튼을 보일지 정한다")


@router.get("/health", response_model=HealthRead, summary="서버 상태 확인")
def get_health() -> HealthRead:
    """서버가 요청을 받을 수 있는지 확인한다. 모니터링·헬스체크용."""
    return HealthRead(status="ok", demo_mode=settings.demo_mode_enabled)
