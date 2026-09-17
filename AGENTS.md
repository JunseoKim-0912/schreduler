# AGENTS.md

이 파일은 Codex(Codex.ai/code)가 이 저장소에서 작업할 때 참고하는 가이드입니다.

## 프로젝트 개요

**Schreduler**는 단순 일정 기록용 캘린더가 아니라 "계획을 지키게 강제하는" 엄격한 스케쥴 관리 시스템의 백엔드입니다. 전체 기획은 [docs/기획보고서.md](docs/기획보고서.md)를 참고하세요. 이 저장소는 **백엔드(Python/FastAPI)만** 다룹니다 — 클라이언트(Flutter/React Native 모바일 앱)는 별도 저장소에서 REST/WebSocket으로 이 백엔드와 통신합니다.

핵심 차별점 3가지:
1. **자연어 인터페이스** — LLM이 부족한 일정 정보를 되물어 채움 (FR-2)
2. **엄격한 실행 관리 (Enforcement)** — 시작/종료 알림, 완료 체크, 미준수 시 사유 기록 (FR-4, FR-4-1, FR-6)
3. **정서적 완충 장치 + 게이미피케이션** — 페르소나 대화 (FR-9), 포인트/streak (FR-10)

전 기능은 한국어/영어 다국어(FR-11)를 처음부터 지원해야 합니다.

## 기술 스택

| 영역 | 선택 |
|---|---|
| 언어/프레임워크 | Python 3.12+ / FastAPI |
| DB / ORM | SQLite(개발) → PostgreSQL(운영) / SQLAlchemy 2.x (async) |
| 마이그레이션 | Alembic |
| 스케쥴링 | APScheduler (확장 시 Celery + Redis 검토) |
| 푸시 알림 | Firebase Cloud Messaging (FCM) |
| 에스컬레이션 | 텔레그램 봇 (`python-telegram-bot`, opt-in) |
| LLM | 저비용 API 모델 1종 (MVP) — Codex Haiku / GPT-4o-mini급, 프롬프트 캐싱 활용 |
| 검증/직렬화 | Pydantic v2 |
| 테스트 | pytest / pytest-asyncio |

## 폴더 구조 컨벤션

기능(도메인) 단위로 모듈을 나누는 구조를 따릅니다. 레이어 단위(전체 models/, 전체 routers/)로 흩어놓지 않고, 기획서의 FR 단위 도메인이 최대한 한 모듈 안에 응집되도록 합니다.

```
schreduler/
├── app/
│   ├── main.py                  # FastAPI 앱 생성, 라우터 등록
│   ├── config.py                # 환경설정 (pydantic-settings)
│   ├── db.py                    # SQLAlchemy 엔진/세션
│   ├── core/                    # 도메인 공통: 인증, 예외, 미들웨어, i18n 유틸
│   ├── calendar/                # FR-1: 이벤트 CRUD, 반복 규칙, 뷰
│   │   ├── models.py
│   │   ├── schemas.py           # Pydantic 스키마
│   │   ├── service.py           # 비즈니스 로직
│   │   └── router.py            # FastAPI 라우터
│   ├── nlp_event/                # FR-2: LLM 기반 자연어 슬롯필링
│   ├── importance/              # FR-3: 중요도 체계
│   ├── enforcement/              # FR-4, FR-4-1: 알림, 에스컬레이션 엔진
│   ├── child_events/             # FR-5: 이동/준비 시간 하위 이벤트
│   ├── feedback/                 # FR-6: 미준수 사유 + ComplianceReport
│   ├── sleep/                    # FR-7: 수면 체크
│   ├── daily_checkin/             # FR-8: 9시 요약 체크인
│   ├── persona/                  # FR-9: 페르소나 대화, 프롬프트 캐싱
│   ├── points/                   # FR-10: 포인트/streak 엔진
│   ├── i18n/                     # FR-11: 다국어 리소스, 언어별 라벨
│   └── llm/                      # LLM 클라이언트 공통 레이어 (프롬프트 캐싱, 라우팅)
├── alembic/                      # DB 마이그레이션
├── tests/                        # app/ 구조를 그대로 미러링
├── docs/
│   └── 기획보고서.md
└── AGENTS.md
```

- 각 도메인 모듈은 `models.py`(SQLAlchemy) / `schemas.py`(Pydantic) / `service.py`(로직) / `router.py`(엔드포인트)로 구성합니다.
- 도메인 간 의존은 단방향으로 유지합니다 (예: `enforcement`가 `calendar`를 참조하는 것은 되지만 역방향은 지양).
- 테스트는 `tests/`에 `app/`과 동일한 경로 구조로 배치합니다 (예: `app/feedback/service.py` → `tests/feedback/test_service.py`).

## 코딩 스타일

### 타입 힌트
- 모든 함수/메서드 시그니처에 타입 힌트를 필수로 작성합니다 (인자, 반환값 모두).
- `Any`는 최후의 수단으로만 사용하고, 가능하면 Pydantic 모델이나 `TypedDict`/`Literal`로 구체화합니다.
- Optional 값은 `X | None` 문법(PEP 604)을 사용합니다.
- LLM 응답처럼 구조화된 출력이 필요한 경우 Pydantic 모델로 스키마를 정의하고 검증합니다.

### 네이밍 규칙
- **파일/모듈**: `snake_case` (예: `daily_checkin.py`)
- **클래스**: `PascalCase` — SQLAlchemy 모델(`Event`, `ComplianceReport`)과 Pydantic 스키마(`EventCreate`, `EventRead`, `EventUpdate`) 모두 동일하게 적용
- **함수/변수**: `snake_case`
- **상수**: `UPPER_SNAKE_CASE`
- **Pydantic 스키마 접미사 컨벤션**: `{Resource}Create` / `{Resource}Update` / `{Resource}Read` 패턴을 사용 (예: `PersonaCreate`, `PersonaRead`)
- **FastAPI 라우터 함수명**: 동사로 시작 (예: `create_event`, `list_event_instances`, `resolve_compliance_report`)
- **DB 테이블/컬럼명**: `snake_case`, 기획서(docs/기획보고서.md 4절)의 필드명을 그대로 따름

### 기타 원칙
- 비동기 I/O(DB, LLM API 호출, 외부 API)는 `async def` + SQLAlchemy async 세션을 사용합니다.
- LLM 호출은 반드시 `app/llm/` 공통 클라이언트를 경유합니다 — 프롬프트 캐싱 배치 규칙(고정 컨텍스트는 프롬프트 앞부분, 가변 컨텍스트는 뒷부분)을 이 레이어에서 강제합니다.
- FR-6의 "LLM 우회 UI 숏컷"처럼 비용 최적화가 기획에 명시된 로직은 임의로 단순화하지 말고 기획서 그대로 구현합니다.
- 다국어 문자열(페르소나 대사, 카테고리 라벨, 알림 문구)은 코드에 하드코딩하지 않고 `{"ko": ..., "en": ...}` 형태의 JSON 필드 또는 i18n 리소스로 관리합니다.
- 커밋되는 코드에는 불필요한 주석을 달지 않습니다. WHY가 비직관적인 경우(예: 프롬프트 캐싱 프리픽스 순서, 에스컬레이션 리셋 조건)에만 짧은 주석을 남깁니다.
