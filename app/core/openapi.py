"""/docs(OpenAPI) 문서용 메타데이터. 태그 순서가 곧 /docs의 섹션 순서다."""

from __future__ import annotations

from typing import Any

APP_DESCRIPTION = """
**Schreduler** — 계획을 "기록"이 아니라 "지키게" 만드는 엄격한 스케쥴 관리 백엔드.

### 인증
정식 인증은 아직 없다. `/users/me/*`, `/tasks`, `/points/summary`,
`/compliance-reports/categories`는 **`X-User-Id` 헤더**의 사용자를 현재 사용자로 본다.
그 밖의 엔드포인트는 요청 본문/쿼리의 `user_id`를 쓴다.

### 다국어 (FR-11)
라벨·알림·LLM 응답은 사용자의 `preferred_language`(`ko`/`en`)를 따른다.
코드값(예: `overslept`, `deadline`)은 언어와 무관하게 그대로다.

### LLM을 호출하는 엔드포인트
`POST /events/parse`, `POST /daily-actual-logs/checkin`, 그리고 `other` 또는 자유 텍스트가 있는
`POST /compliance-reports`. LLM 오류는 500(키 미설정) / 502(호출 실패) / 422(응답 형식 오류)로 돌려준다.
"""

TAGS_METADATA: list[dict[str, Any]] = [
    {"name": "events", "description": "일정 CRUD와 자연어 일정 추가 (FR-1, FR-2). `scheduled`(시작~종료)와 `deadline`(마감만) 두 종류."},
    {"name": "tasks", "description": "`deadline` 일정을 할 일 목록처럼 다루는 편의 API. 별도 테이블 없이 Event/EventInstance를 재사용한다."},
    {"name": "date-ranges", "description": "'2026 가을학기' 같은 중요 기간. 반복 일정의 종료 기준으로 재사용한다 (FR-2)."},
    {"name": "locations", "description": "일정 장소와 기본 이동시간. 이동시간 하위 일정 생성에 쓰인다 (FR-5)."},
    {"name": "compliance-reports", "description": "일정을 못 지킨 사유 기록과 통계 (FR-6). 버튼만 누르면 LLM을 호출하지 않는다."},
    {"name": "sleep-logs", "description": "실제 취침·기상 기록 (FR-7)."},
    {"name": "daily-actual-logs", "description": "하루 실제 기록과 저녁 9시 체크인 대화 (FR-8)."},
    {"name": "personas", "description": "대화 캐릭터(페르소나) 관리 (FR-9). 코드 수정 없이 추가·수정할 수 있다."},
    {"name": "users", "description": "현재 사용자(`X-User-Id`)의 페르소나 선택과 대화 기록."},
    {"name": "points", "description": "규칙성 포인트와 연속 완료(streak) 보너스 (FR-10)."},
    {"name": "health", "description": "서버 상태 확인."},
]

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "대상(또는 참조한 user/date_range 등)을 찾을 수 없음"}}
CONFLICT: dict[int | str, dict[str, Any]] = {409: {"description": "이미 존재하거나 다른 데이터가 참조 중이라 처리할 수 없음"}}
CURRENT_USER: dict[int | str, dict[str, Any]] = {
    401: {"description": "`X-User-Id` 헤더가 없음"},
    404: {"description": "`X-User-Id`의 사용자(또는 대상)를 찾을 수 없음"},
}
LLM_ERRORS: dict[int | str, dict[str, Any]] = {
    500: {"description": "LLM_API_KEY가 설정되지 않음"},
    502: {"description": "LLM API 호출 실패"},
}
