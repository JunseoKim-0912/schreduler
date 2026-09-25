# Schreduler

계획을 "기록"하는 캘린더가 아니라, **계획을 지키게 만드는** 엄격한 스케쥴 관리 시스템의 백엔드입니다.
자연어로 일정을 추가하고, 시작·종료 알림과 완료 체크로 실행을 관리하며, 페르소나 대화와 포인트로 꾸준함을 돕습니다.

이 저장소는 **백엔드(Python / FastAPI)** 만 다룹니다. 모바일 클라이언트(Flutter)는 별도 저장소에서 REST API로 통신합니다.

- 전체 기획: [`docs/기획보고서.md`](docs/기획보고서.md)
- 클라이언트 개발자용 API 안내: [`docs/api_overview.md`](docs/api_overview.md)
- Postman 컬렉션: [`docs/postman/Schreduler.postman_collection.json`](docs/postman/Schreduler.postman_collection.json)

## 주요 기능

| 기능 | 설명 |
|---|---|
| 일정 (FR-1) | 시작~종료 일정(`scheduled`)과 마감형 일정(`deadline`), RRULE 반복, 학기 같은 중요 기간 |
| 자연어 일정 추가 (FR-2) | LLM이 부족한 정보(요일, 시간, 반복 기간 등)를 되물어 채운 뒤 초안을 만든다 |
| 알림·에스컬레이션 (FR-4) | 시작/종료/마감 알림(FCM), 장기 무응답 시 텔레그램 에스컬레이션 |
| 이동시간 (FR-5) | 장소가 있는 일정 앞에 이동시간 하위 일정을 자동 생성 |
| 미준수 사유 (FR-6) | 버튼만 누르면 LLM 없이 저장, 자유 텍스트일 때만 LLM이 페르소나 말투로 피드백 |
| 수면·하루 체크인 (FR-7, FR-8) | 아침 수면 체크인, 저녁 9시 체크인 대화 (놓친 일정 위주로 컨텍스트 구성) |
| 페르소나 (FR-9) | 대화 캐릭터 선택, 대화 기록 저장, 프롬프트 캐싱을 고려한 프롬프트 배치 |
| 포인트 (FR-10) | 중요도 가중치 × 완료, 연속 100% 완료 streak 보너스 (3일 ×1.1 / 7일 ×1.25 / 14일 ×1.5) |
| 다국어 (FR-11) | 알림 문구, 카테고리 라벨, LLM 응답을 사용자 언어(ko/en)로 |
| 할 일 목록 | 마감형 일정을 할 일처럼 다루는 `/tasks` API (overdue 표시, 완료 처리) |

## 기술 스택

Python 3.12+ · FastAPI · SQLAlchemy 2.x · Alembic · SQLite(개발) / PostgreSQL · APScheduler ·
OpenAI 호환 LLM API · Firebase Cloud Messaging · python-telegram-bot · pytest

## 빠르게 시작하기

### 로컬 실행

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # 값 채우기 (아래 "환경변수" 참고). 최소한 LLM_API_KEY가 있어야 LLM 기능이 동작한다

alembic upgrade head               # DB 스키마 생성/최신화 (기본: ./schreduler.db)
python -m app.scripts.seed         # 테스트 사용자 생성 (생성된 id가 출력된다)
python -m app.scripts.seed_personas  # 기본 페르소나 넣기 (app/scripts/personas_seed_data.json)

uvicorn app.main:app --reload
```

- API 문서(Swagger UI): http://localhost:8000/docs
- 상태 확인: http://localhost:8000/health

### Docker

```bash
docker compose up --build                                                        # SQLite (볼륨에 저장)
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up --build    # PostgreSQL
```

컨테이너가 시작될 때 `alembic upgrade head`를 먼저 실행합니다. 스케줄러가 서버 프로세스 안에서 돌기 때문에 워커는 1개로 실행됩니다.

## 환경변수

`.env` 파일 또는 환경변수로 설정합니다. 항목별 설명과 예시는 [`.env.example`](.env.example)에 있습니다. `.env`는 저장소에 커밋하지 않습니다.

| 이름 | 기본값 | 설명 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./schreduler.db` | SQLAlchemy DB URL. PostgreSQL은 `postgresql+psycopg://user:pw@host:5432/db` |
| `LLM_API_KEY` | (없음) | LLM API 키. 없으면 LLM 엔드포인트가 500을 돌려준다 |
| `LLM_MODEL` | `gpt-5.6-luna` | 사용할 모델 |
| `FIREBASE_CREDENTIALS_PATH` | (없음) | FCM 서비스 계정 JSON 경로. 없으면 푸시는 로그만 남기고 건너뛴다 |
| `TELEGRAM_BOT_TOKEN` | (없음) | 에스컬레이션용 텔레그램 봇 토큰. 없으면 로그만 남긴다 |
| `LOG_LEVEL` | `INFO` | 앱 로그 레벨 |
| `ENVIRONMENT` | `development` | 실행 환경 이름 |

Docker 전용: `DOCKER_DATABASE_URL`(컨테이너 DB URL, 로컬 `.env`의 `DATABASE_URL` 대신 사용), `API_PORT`(기본 8000),
`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`(기본값 모두 `schreduler`, 개발용).

## 테스트

```bash
python -m pytest                          # 전체 테스트 (tests/ 아래만 수집)
python -m pytest --cov=app                # 커버리지
```

테스트는 인메모리 SQLite와 가짜 LLM 응답을 쓰므로 외부 API를 호출하지 않습니다.

## 프로젝트 구조

```
app/
├── main.py              # FastAPI 앱, 라우터·예외 처리기·스케줄 잡 등록
├── api/                 # 라우터 (엔드포인트)
├── models/              # SQLAlchemy 모델
├── schemas/             # Pydantic 요청/응답 스키마
├── services/            # 비즈니스 로직, LLM 클라이언트, 알림, 포인트
├── child_events/        # 이동시간·준비 하위 일정 (FR-5)
├── core/                # 설정, DB, 스케줄러, 인증, 예외 처리, 로깅, OpenAPI 메타데이터
├── i18n/                # 언어별 알림 문구·카테고리 라벨 (ko/en)
└── scripts/             # 시드, Postman 컬렉션 생성, 프롬프트 캐싱 비교
alembic/                 # DB 마이그레이션
tests/                   # pytest
docs/                    # 기획서, API 안내, Postman 컬렉션
```

## 유용한 스크립트

| 명령 | 설명 |
|---|---|
| `python -m app.scripts.seed` | 테스트 사용자 생성 |
| `python -m app.scripts.seed_personas` | JSON 파일의 페르소나를 DB에 upsert |
| `python -m app.scripts.export_postman` | OpenAPI 스펙으로 Postman 컬렉션 재생성 (API 변경 후 실행) |
| `python -m app.scripts.compare_prompt_cache --task daily_checkin --repeat 5` | 프롬프트 캐싱 전후 입력 토큰 비교 (실제 LLM API 호출, 비용 발생) |

## 현재 상태와 제약

개발 중인 MVP입니다. 정식 인증 대신 `X-User-Id` 헤더를 쓰고, 일반 일정의 완료 처리·회차 조회, 사용자 관리,
FCM 디바이스 토큰 등록 API는 아직 없습니다. 자세한 목록은 [`docs/api_overview.md`의 "알려진 제약"](docs/api_overview.md#7-알려진-제약-클라이언트-설계-시-주의)을 참고하세요.

## 라이선스

[MIT](LICENSE) © 2026 Junseo Kim
