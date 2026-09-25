from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="서버 상태 확인")
def get_health() -> dict[str, str]:
    """서버가 요청을 받을 수 있는지 확인한다. 모니터링·헬스체크용."""
    return {"status": "ok"}
