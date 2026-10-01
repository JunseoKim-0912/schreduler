# Schreduler API 개요 (Flutter 클라이언트용)

Schreduler 백엔드의 REST API를 클라이언트 개발 관점에서 정리한 문서다. 필드 하나하나의 정확한 스키마는
서버의 `/docs`(Swagger UI)가 기준이고, 이 문서는 **공통 규칙 · 화면별 호출 흐름 · 엔드포인트 목록 · 알려진 제약**을 다룬다.

- 인터랙티브 문서: `{서버 주소}/docs` (요청 예시가 채워져 있어 바로 호출해 볼 수 있다)
- OpenAPI 스펙: `{서버 주소}/openapi.json` — Dart 모델 코드 생성(`openapi-generator` 등)에 쓸 수 있다
- Postman 컬렉션: [`docs/postman/Schreduler.postman_collection.json`](postman/Schreduler.postman_collection.json)

---

## 1. 공통 규칙

### 1.1 기본 사항

| 항목 | 내용 |
|---|---|
| Base URL | 로컬 `http://localhost:8000` (Docker도 동일 포트). Android 에뮬레이터에서는 `http://10.0.2.2:8000` |
| 형식 | 요청·응답 모두 JSON (`Content-Type: application/json`). 삭제는 `204 No Content`, 본문 없음 |
| ID | 모든 리소스 ID는 정수. 예외: 페르소나는 문자열 `name`(예: `"Hana"`), 자연어 입력 세션은 문자열 `session_id` |
| 부분 수정 | `PUT`은 **보낸 필드만** 바꾼다. 필드를 생략하면 그대로, `null`을 보내면 값을 지운다(지울 수 없는 필드에 `null`을 보내면 422) |
| 문자열 | 이름·제목·발화는 앞뒤 공백이 제거되고, 빈 문자열은 422 |

### 1.2 인증 — `X-User-Id` 헤더 (임시)

정식 인증은 아직 없다. 엔드포인트마다 사용자를 지정하는 방식이 두 가지다.

| 방식 | 해당 엔드포인트 |
|---|---|
| **`X-User-Id: <user id>` 헤더** | `/users/me/*`, `/tasks*`, `/event-instances*`, `/points/summary`, `/compliance-reports/categories`, `/actions*`, `/assistant/*` |
| 요청 본문/쿼리의 `user_id` | 그 밖의 전부 (`/events`, `/date-ranges`, `/locations`, `/sleep-logs`, `/daily-actual-logs` 등) |

헤더가 없으면 `401`, 없는 사용자면 `404`. 정식 인증이 들어오면 헤더 방식이 토큰으로 바뀔 예정이므로,
**HTTP 클라이언트 인터셉터에서 모든 요청에 `X-User-Id`를 붙여 두는 것**을 권장한다(필요 없는 곳에 붙어도 무해하다).

### 1.3 날짜·시간

| 타입 | 형식 | 예 |
|---|---|---|
| 날짜 (`date`) | `YYYY-MM-DD` | `"2026-09-24"` |
| 일시 (`date-time`) | 타임존 없는 ISO 8601 | `"2026-09-24T19:00:00"` |

- 서버는 **타임존 없는 로컬 시각**으로 저장·비교한다. `DateTime.toUtc()`나 `Z`/`+09:00`이 붙은 값을 보내지 말고,
  로컬 `DateTime`을 초 단위까지 포맷해서 보낸다. 예: `DateFormat("yyyy-MM-dd'T'HH:mm:ss").format(dt)`
- 받은 값은 `DateTime.parse()`로 읽으면 로컬 시각으로 해석된다.
- "오늘", "이번 주", 자정 포인트 계산, 알림 시각은 모두 **서버 시간대** 기준이다(→ 7. 알려진 제약).

### 1.4 에러 응답

모든 에러 본문은 `{"detail": ...}` 형태지만 `detail`의 타입이 두 가지다. **422는 둘 다 올 수 있으니 반드시 분기해서 처리한다.**

```jsonc
// 요청 형식 검증 실패 (필드 누락, 타입 오류, 빈 문자열, 잘못된 반복 규칙 등) → detail이 배열
{"detail": [{"loc": ["body", "title"], "msg": "String should have at least 1 character", "type": "string_too_short"}]}

// 그 밖의 에러 (없음, 충돌, 기존 데이터와 합쳤을 때의 규칙 위반, LLM 오류 등) → detail이 문자열
{"detail": "date_range 3 is used by 2 events"}
```

| 상태 | 의미 | 클라이언트 처리 제안 |
|---|---|---|
| `401` | `X-User-Id` 헤더 없음 | 로그인(사용자 선택) 화면으로 |
| `404` | 대상 없음, 또는 참조한 `user_id`/`date_range_id` 등이 없음 | 목록 새로고침 |
| `409` | 충돌 — 이미 존재하거나 다른 데이터가 쓰는 중이라 삭제 불가, 되돌리기 순서 위반 등 | `detail`을 그대로 안내 |
| `410` | 어시스턴트 제안 만료 (`POST /assistant/confirm`·`/cancel`, 제안 후 30분) | 요청을 다시 말하도록 안내 |
| `422` | 입력 규칙 위반 (위 두 형식) | 배열이면 `loc`의 마지막 값으로 해당 입력 필드에 표시 |
| `500` | 서버 설정 문제(예: LLM 키 미설정) 또는 예상 못 한 오류 | 일반 오류 안내 + 재시도 |
| `502` | LLM API 호출 실패 | "잠시 후 다시 시도" 안내 |

### 1.5 다국어 (ko/en)

사용자의 `preferred_language`(`ko`/`en`)에 따라 서버가 언어를 골라서 내려주는 값이 있다.

- **코드값은 언어와 무관하게 고정**: `overslept`, `deadline`, `pending` 등. 클라이언트 로직·저장은 코드값으로 한다.
- **서버가 번역해서 내려주는 값**: 미준수 카테고리 `label`, `reason_category_label`, 푸시·텔레그램 알림 문구, 페르소나 LLM 응답, 자연어 입력의 되묻는 질문.
- 페르소나의 `display_name` 등은 `{"ko": "...", "en": "..."}` 두 언어가 모두 오므로 클라이언트가 고른다.
- 메뉴·버튼 같은 고정 UI 문자열은 클라이언트 i18n 리소스로 관리한다.

---

## 2. 코드값(enum)

| 이름 | 값 | 설명 |
|---|---|---|
| `event_type` | `scheduled`(기본), `deadline` | `scheduled`는 시작~종료, `deadline`은 `start_time`이 `null`이고 `end_time`이 마감 일시 |
| `importance` | `null`, `1`~`5`, `6` | `null`=없음(수면), `1` 개인 여가, `2` 타인 약속, `3` 출석 체크 없는 의무, `4` 공식 의무/평가, `5` 반드시, `6`=MAX |
| `status` (일정 회차) | `pending`, `done`, `missed`, `cancelled` | `cancelled`=반복 일정 중 그 회차만 삭제됨. 목록·알림·포인트에서 빠진다 |
| `child_kind` | `travel`, `custom` | 하위 일정 종류. `travel`은 장소 이동시간으로 서버가 자동 생성 |
| `reason_category` | `overslept`, `fatigue`, `priority_shift`, `schedule_conflict`, `forgot`, `transit_issue`, `other` | 미준수 사유. 버튼 라벨은 `GET /compliance-reports/categories`로 받는다 |
| `role` (대화 메시지) | `user`, `assistant` | |
| `kind` (어시스턴트 카드) | `create_event`, `update_event`, `delete_event`, `create_range`, `update_range`, `delete_range` | `proposal.items[].kind` |
| `mode` (사용 중인 기간 삭제) | `range_only`, `with_events` | 기간만 삭제(일정은 이미 만들어진 마지막 회차에서 끝남) / 일정도 함께 삭제. `DELETE /date-ranges/{id}?mode=`와 기간 삭제 카드 |
| `action_type` (변경 기록) | `create`, `delete`, `update` | |
| `source` (변경 기록) | `nl`, `ui` | `nl`=자연어 요청, `ui`=화면 버튼(일정 삭제, 반복 기간 등록·수정·삭제) |
| 포인트 가중치 | 중요도 `null`→0, `1`~`5`→그대로, `6`(MAX)→10 | 연속 100% 완료 3일 ×1.1, 7일 ×1.25, 14일 ×1.5 |

반복 규칙 `recurrence_rule`은 RFC 5545 RRULE 문자열이다. 예: 매주 화요일 `FREQ=WEEKLY;BYDAY=TU`, 매일 `FREQ=DAILY`.
`is_recurring: true`면 `recurrence_rule`이 필수이고, 실제 반복 회차는 `date_range_id`로 지정한 기간 안에서만 생성된다.
반복의 기준일은 일정의 `start_time`(마감이면 `end_time`) 날짜다 — 그 날짜가 첫 회차이고, 회차는 기간 안이면서 그 날짜 이후만 생긴다.
격주는 `INTERVAL=2`(예: `FREQ=WEEKLY;INTERVAL=2;BYDAY=TU`)이며, 리듬은 기준일이 속한 주로 정해져 기간을 늘리거나 줄여도 어긋나지 않는다.
자연어 초안(`draft`)에는 표시용으로 `date_range_name`, `date_range_end`, 처음 3개 회차 `preview_dates`가 함께 온다.

---

## 3. 화면별 호출 흐름

### 3.1 앱 시작 · 페르소나 선택

```
GET  /personas                      → 선택 가능한 페르소나 목록 (display_name.ko/en 중 앱 언어로 표시)
GET  /users/me/persona              → 현재 선택. selected_persona가 null이면 선택 화면으로
PUT  /users/me/persona  {"persona_name": "Hana"}   → 선택 (null이면 해제)
```

선택한 페르소나는 저녁 체크인·미준수 피드백 LLM 응답의 말투에 쓰인다.

### 3.2 자연어로 일정 관리 (일정 어시스턴트)

```
GET  /assistant/sessions/current                          → 오늘의 대화 이어보기 (없으면 session_id: null)
POST /assistant/chat {"message": "10월 2일 11:00-1:00 스터디 추가해줘"}           // session_id 생략 = 새 대화
  → {session_id, reply, proposal: {token, expires_at, items: [카드...], warnings}, executed: []}
POST /assistant/confirm {"session_id": 1, "token": "…"}   → {reply, executed: [{action_id, summary}]}
POST /assistant/cancel  {"session_id": 1, "token": "…"}   → {reply}
POST /assistant/chat {"session_id": 1, "message": "7시로 바꿔줘"}   → 새 proposal (이전 제안은 대체됨)
POST /assistant/chat {"session_id": 1, "message": "좋아"}          → executed에 action_id (채팅 승인)
```

- 어시스턴트는 되묻기보다 추론해서 **초안**을 제안한다. 확인(버튼 또는 채팅 승인) 전에는 아무것도 저장되지 않는다.
- 카드(`proposal.items[]`) 공통: `draft_id`, `kind`, `warnings[]`(`{code, message}`, 사용자 언어), `inferred_fields[]`(추정 배지를 붙일 항목).
  - `create_event`: `title`, `event_type`, `date`(반복이면 첫 회차), `start_time?`, `end_time`, `time_display`("오전 11:00 – 오후 1:00 (2시간)"),
    `importance?`, `recurring`, `recurrence_rule?`, `date_range?`(`{id?, name, start_date, end_date, is_new}`), `location?`(`{id?, name, travel_minutes, is_new}`), `preview_dates[]`(처음 3회차)
  - `update_event` / `delete_event`: `scope`(`instance`/`series`), `targets[]`(`{event_id, instance_id?, title, date, time_display, new_time_display?}`), `changes`(수정만)
  - `create_range` / `update_range` / `delete_range`: `name`, `start_date`, `end_date`, `events_using`, `mode?`
- 경고 코드: `crosses_midnight`, `over_12_hours`, `past_date`, `start_weekday_mismatch`, `multiple_targets`, `range_in_use`.
- 한 제안의 여러 초안(예: 새 기간 + 그 기간의 일정)은 **한 트랜잭션**으로 확정된다. `executed[].action_id`마다 `POST /actions/{id}/undo`로 되돌린다.
- 제안은 30분 뒤 만료(`410`). 이미 확정·취소·대체된 제안은 `409`, 다른 사용자·세션의 토큰은 `404`.
- 한 턴은 LLM 호출 최대 6회·20초. 넘으면 지금까지 만든 초안을 보여주거나 짧게 되묻는다.
- 반복 기간도 같은 입력창에서 만들고·바꾸고·지운다 ("Lecture End Date를 12월 10일까지로 바꿔줘"). 기간을 바꾸면 그 기간을 쓰는
  반복 일정의 회차를 다시 맞춘다: 늘어난 날짜는 회차(와 알림)를 추가하고, 범위 밖의 대기 회차는 `cancelled`(완료·놓침은 그대로).
  쓰는 일정이 있는 기간을 지우는 카드에는 `range_in_use` 경고와 `mode`(기본 `range_only`, 추정)가 붙는다.
- "등록된 기간 보여줘" 같은 질문에는 제안 없이 `reply`로 답한다.

### 3.2.1 되돌리기

```
GET  /actions?limit=10                       → 최근 변경 기록 (최신순, undone=true면 이미 되돌림)
POST /actions/{action_id}/undo               → 되돌린 기록(ActionRead)
```

- 어시스턴트로 실행한 추가·삭제·수정, `DELETE /events/{id}`·`DELETE /event-instances/{id}`, 반복 기간 등록·수정·삭제(`/date-ranges`, 응답 헤더 `X-Action-Id`)가 기록된다. `PUT /events`, `POST /events`는 기록되지 않는다.
- 여러 개를 한 번에 바꾼 요청도 기록 하나 → 되돌리기 한 번으로 모두 복구된다. 삭제를 되돌리면 원래 ID 그대로 돌아온다(미준수 사유 포함).
- 같은 일정을 건드린 더 최근 기록이 남아 있으면 `409` ("더 최근 변경을 먼저 되돌려야 해요"). 이미 되돌린 기록도 `409`.
- 과거 회차가 바뀌었다면 그날부터 어제까지 포인트가 다시 계산된다.

### 3.3 할 일 목록 (마감형 일정)

```
GET  /tasks                                  → 마감이 빠른 순. overdue=true면 마감 지났는데 미완료
POST /tasks {"title": "과제 제출", "end_time": "2026-09-25T23:59:00", "importance": 4}
PUT  /tasks/{event_instance_id}/complete     → 완료 처리 (다시 호출해도 같은 결과)
```

- 반복 할 일(예: 매주 금요일 제출)은 **완료 안 된 가장 이른 회차 하나**가 목록에 나오고, 완료하면 다음 회차로 넘어간다.
- 완료 요청에는 목록의 `event_instance_id`를 쓴다(`event_id` 아님).
- 할 일은 내부적으로 `event_type=deadline`인 이벤트라 `GET /events`에도 나온다.

### 3.4 일정을 못 지켰을 때 (미준수 사유)

```
GET  /compliance-reports/categories     → [{"code": "overslept", "label": "늦잠/기상 실패"}, ...] 버튼 목록
POST /compliance-reports {"event_instance_id": 12, "reason_category": "overslept"}
POST /compliance-reports {"event_instance_id": 12, "reason_category": "other", "reason_text": "버스가 안 왔어요"}
```

- 버튼만 누르면(`other`가 아니고 `reason_text` 없음) LLM을 부르지 않아 즉시 응답하고 `llm_feedback`은 `null`.
- `other`를 고르거나 `reason_text`(최대 150자)를 쓰면 LLM이 페르소나 말투의 피드백을 `llm_feedback`에 담아 준다(수 초 소요).
- 통계 화면: `GET /compliance-reports/stats?user_id=1&days=30` — 모든 카테고리가 `count: 0`까지 포함되어 온다.

### 3.5 저녁 9시 체크인 대화

서버가 매일 21시에 체크인 푸시를 보내도록 되어 있다(현재는 미발송 → 5, 7). 알림 또는 앱 진입점에서 챗 화면을 열고:

```
POST /daily-actual-logs/checkin {"user_id": 1, "utterance": "오늘 좀 피곤했어"}
  ← {"reply": "…", "summary": "오늘 계획한 3개 중 2개 완료. …", "conversation_id": 7}
POST /daily-actual-logs/checkin {"user_id": 1, "utterance": "내일은 잘할게", "conversation_id": 7}   // 같은 대화 이어가기
```

- `conversation_id`를 저장해 두었다가 다음 턴에 보낸다. 빠지면 그 페르소나의 **오늘 대화**에 이어서 저장된다.
  화면에 들어오거나 페르소나를 바꾸면 `GET /personas/{persona_id}/conversations/current`로 오늘 대화를 불러와 이어 보여준다.
  날짜가 바뀌면 새 대화가 되고, [새 대화]는 `POST /personas/{persona_id}/conversations`(이전 대화는 남는다).
  페르소나를 선택하지 않은 사용자는 대화가 저장되지 않아 `null`이 온다.
- 키보드 난타·같은 글자 반복·기호만·1000자 초과·알려진 프롬프트 인젝션은 LLM을 부르지 않고 페르소나의 `fallback_lines`로 답한다.
  이런 턴은 대화 기록의 응답 메시지에 `llm_skipped: true`, `filter_reason`이 남는다.
- 지난 대화: `GET /users/me/persona-conversations?context_type=daily_checkin`, `GET /users/me/persona-conversations/{id}`.
- 하루 실제 기록을 남기려면 `POST /daily-actual-logs`.

### 3.6 포인트 화면

```
GET /points/summary
  ← {"today": {"points_earned": 5, "is_perfect_day": false, ...},
     "week_start": "2026-09-21", "week_points": 20.5, "total_points": 120.5, "current_streak_days": 3}
```

- `today`는 호출 시점까지의 실시간 값이라 완료 처리 직후 다시 부르면 바로 반영된다.
- 지난 날짜의 할 일을 뒤늦게 완료해도 그 날짜부터 어제까지의 포인트가 즉시 다시 계산되어 `week_points`·`total_points`에 바로 반영된다.
- `current_streak_days`는 오늘 일정이 남아 있으면 어제까지의 연속 일수, 오늘 전부 완료하면 오늘 포함, 오늘 하나라도 놓치면 0.

### 3.7 기타 기록

- 수면: 아침 8시 수면 체크인 푸시 → `POST /sleep-logs` (`actual_wake_time`이 `actual_bedtime`보다 늦어야 한다)
- 중요 기간(학기 등): `/date-ranges` — 반복 일정이 쓰는 기간은 `?mode=range_only|with_events` 없이 삭제하면 `409`
- 장소: `/locations` — 장소가 있는 일정에는 이동시간 하위 일정(`child_kind: travel`)이 자동으로 붙는다. 일정이 쓰는 장소는 삭제하면 `409`

---

## 4. 엔드포인트 목록

🔑 = `X-User-Id` 헤더 필요, 🤖 = LLM 호출(느릴 수 있음, `500`/`502` 가능)

### events — 일정

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `POST /events` | 이벤트 생성 | `EventCreate` | `201 EventRead` |
| `GET /events` | 이벤트 목록 (`?user_id=`) | | `EventRead[]` |
| `GET /events/{event_id}` | 이벤트 조회 | | `EventRead` |
| `PUT /events/{event_id}` | 이벤트 수정 (부분) | `EventUpdate` | `EventRead` |
| `DELETE /events/{event_id}` | 이벤트 삭제 (반복 회차·하위 일정 포함). 헤더 `X-Action-Id`로 되돌리기 id | | `204` |

`EventCreate`: `user_id`, `title`, `event_type`(기본 `scheduled`), `start_time`(`deadline`이면 `null`), `end_time`,
`importance?`, `is_recurring?`, `recurrence_rule?`, `date_range_id?`, `parent_event_id?`, `child_kind?`, `location_id?`

> `scheduled` → `deadline`으로 바꿀 때는 `{"event_type": "deadline", "start_time": null}`을 함께 보낸다(하나만 보내면 422).

### assistant — 일정 어시스턴트 🔑

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `POST /assistant/chat` 🤖 | 대화 한 턴 — 초안 제안 또는 채팅 승인 실행 (3.2) | `{session_id?, message}` | `AssistantChatResponse` |
| `POST /assistant/confirm` | 제안 확정 (버튼, 한 트랜잭션) | `{session_id, token}` | `AssistantConfirmResponse` |
| `POST /assistant/cancel` | 제안 취소 (버튼) | `{session_id, token}` | `AssistantConfirmResponse` |
| `GET /assistant/sessions/current` | 오늘의 최근 대화·기록·대기 제안 | | `AssistantSessionRead` |
| `POST /assistant/sessions` | 새 대화 | | `201 AssistantSessionRead` |

`AssistantChatResponse`: `session_id`, `reply`, `proposal?`(`{token, expires_at, items[], warnings[]}`), `executed[]`(`{action_id, summary}`)

`AssistantSessionRead`: `session_id?`, `messages[]`(`{role, text, created_at}`), `proposal?`

### event-instances — 일정 회차 (캘린더) 🔑

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `GET /event-instances` | 기간의 회차 (`?start=YYYY-MM-DD&end=YYYY-MM-DD`, 양 끝 포함, 최대 62일). 취소된 회차 제외 | | `EventInstanceRead[]` |
| `PUT /event-instances/{event_instance_id}/complete` | 회차 완료 처리 (`scheduled`·`deadline` 모두) | | `EventInstanceRead` |
| `DELETE /event-instances/{event_instance_id}` | 이 회차만 삭제(취소). 헤더 `X-Action-Id`로 되돌리기 id | | `204` |

`EventInstanceRead`: `event_instance_id`, `event_id`, `date`, `status`, `completion_method?`, `title`, `event_type`, `importance`,
`is_recurring`, `recurrence_rule?`, `parent_event_id?`, `child_kind?`, `start_time?`, `end_time`, `time_overridden`

- `start_time`/`end_time`은 회차별 시간 변경을 반영한 실제 시각(타임존 없는 로컬 시각, 1.3). `deadline`은 `start_time`이 `null`.
- 비반복(단발) 일정도 회차가 하나 있다. 회차 생성 이전에 만든 **지난 날짜의 단발 일정만** 회차가 없어 `event_instance_id: null`로 온다(완료 불가, 삭제는 `DELETE /events/{event_id}`).

### tasks — 할 일 (마감형) 🔑

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `GET /tasks` | 할 일 목록 (3.3) | | `TaskRead[]` |
| `POST /tasks` | 할 일 생성 | `{title, end_time, importance?, recurrence_rule?, date_range_id?}` | `201 TaskRead` |
| `PUT /tasks/{event_instance_id}/complete` | 할 일 완료 처리 | | `TaskRead` |

`TaskRead`: `event_id`, `event_instance_id`, `title`, `importance`, `due_at`, `status`, `completed`, `overdue`, `is_recurring`, `recurrence_rule`, `date_range_id`

### date-ranges — 중요 기간

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `POST /date-ranges` | 중요 기간 등록. 헤더 `X-Action-Id`로 되돌리기 id | `{user_id, name, start_date, end_date}` | `201 DateRangeRead` |
| `GET /date-ranges` | 목록 (`?user_id=`). `event_count`는 이 기간을 쓰는 일정 수 | | `DateRangeUsageRead[]` |
| `GET /date-ranges/{date_range_id}` | 조회 | | `DateRangeRead` |
| `PUT /date-ranges/{date_range_id}` | 수정 (부분). 쓰는 반복 일정의 회차를 다시 맞춤. 헤더 `X-Action-Id` | `{name?, start_date?, end_date?}` | `DateRangeRead` |
| `DELETE /date-ranges/{date_range_id}` | 삭제 (`?mode=range_only\|with_events`, 사용 중인데 없으면 `409`). 헤더 `X-Action-Id` | | `204` |

### locations — 장소

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `POST /locations` | 장소 등록 | `{user_id, name, default_travel_minutes}` | `201 LocationRead` |
| `GET /locations` | 목록 (`?user_id=`) | | `LocationRead[]` |
| `GET /locations/{location_id}` | 조회 | | `LocationRead` |
| `PUT /locations/{location_id}` | 수정 (부분) | `{name?, default_travel_minutes?}` | `LocationRead` |
| `DELETE /locations/{location_id}` | 삭제 (사용 중이면 `409`) | | `204` |

### compliance-reports — 미준수 사유

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `POST /compliance-reports` 🤖* | 사유 기록 (3.4). *`other`/자유 텍스트일 때만 LLM | `{event_instance_id, reason_category, reason_text?}` | `201 ComplianceReportRead` |
| `GET /compliance-reports/categories` 🔑 | 카테고리 버튼 목록 | | `[{code, label}]` |
| `GET /compliance-reports/stats` | 카테고리별 통계 (`?days=30&user_id=`) | | `{since, until, total, by_category[{reason_category, label, count}]}` |

`ComplianceReportRead`: `id`, `event_instance_id`, `reason_category`, `reason_category_label`, `reason_text`, `llm_triggered`, `created_at`, `llm_feedback`

### sleep-logs — 수면 기록

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `POST /sleep-logs` | 수면 기록 생성 | `{user_id, date, actual_bedtime, actual_wake_time}` | `201 SleepLogRead` |
| `GET /sleep-logs` | 목록 (`?user_id=`) | | `SleepLogRead[]` |
| `GET /sleep-logs/{sleep_log_id}` | 조회 | | `SleepLogRead` |
| `PUT /sleep-logs/{sleep_log_id}` | 수정 (부분) | `{date?, actual_bedtime?, actual_wake_time?}` | `SleepLogRead` |
| `DELETE /sleep-logs/{sleep_log_id}` | 삭제 | | `204` |

### daily-actual-logs — 하루 실제 기록 · 저녁 체크인

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `POST /daily-actual-logs` | 하루 실제 기록 생성 | `{user_id, date, summary_text, actual_events?}` | `201 DailyActualLogRead` |
| `GET /daily-actual-logs` | 목록 (`?user_id=`) | | `DailyActualLogRead[]` |
| `POST /daily-actual-logs/checkin` 🤖 | 저녁 체크인 대화 한 턴 (3.5) | `{user_id, utterance, date?, conversation_id?}` | `{reply, summary, conversation_id}` |
| `GET /daily-actual-logs/{daily_log_id}` | 조회 | | `DailyActualLogRead` |
| `PUT /daily-actual-logs/{daily_log_id}` | 수정 (부분) | `{date?, summary_text?, actual_events?}` | `DailyActualLogRead` |
| `DELETE /daily-actual-logs/{daily_log_id}` | 삭제 | | `204` |

### personas — 페르소나 (관리용)

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `GET /personas` | 페르소나 목록 | | `PersonaRead[]` |
| `POST /personas` | 페르소나 생성 (같은 name이면 `409`) | `PersonaCreate` | `201 PersonaRead` |
| `GET /personas/{name}` | 조회 | | `PersonaRead` |
| `PUT /personas/{name}` | 수정 (부분) | `PersonaUpdate` | `PersonaRead` |
| `DELETE /personas/{name}` | 삭제 (선택한 사용자·대화가 있으면 `409`) | | `204` |
| `GET /personas/{persona_id}/conversations/current` 🔑 | 이 페르소나와의 오늘 대화 (`?context=checkin`, 없으면 `null`) (3.5) | | `PersonaConversationRead \| null` |
| `POST /personas/{persona_id}/conversations` 🔑 | [새 대화]: 오늘 새 대화 시작, 이전 대화는 남김 (`?context=checkin`) | | `201 PersonaConversationRead` |

`PersonaRead`: `name`, `display_name{ko,en}`, `description{ko,en}`, `example_lines{ko[],en[]}|null`, `backstory{ko,en}|null`,
`fallback_lines{ko[],en[]}|null`(의미 없는 입력에 LLM 없이 답할 대사, 비어 있으면 기본 문구).
일반 사용자 앱은 `GET`만 쓰면 된다. 생성·수정·삭제는 관리자 화면용이다.

### users — 내 정보 🔑

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `GET /users/me/persona` | 내 페르소나 조회 | | `{user_id, selected_persona: PersonaRead|null}` |
| `PUT /users/me/persona` | 내 페르소나 선택/해제 | `{persona_name: string|null}` | 위와 같음 |
| `GET /users/me/persona-conversations` | 내 페르소나 대화 목록 (최신순, `?context_type=`) | | `PersonaConversationRead[]` |
| `GET /users/me/persona-conversations/{conversation_id}` | 대화 하나 | | `PersonaConversationRead` |

`PersonaConversationRead`: `id`, `user_id`, `persona_id`, `context_type`, `messages[{role, content, created_at}]`

### points — 포인트 🔑

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `GET /points/summary` | 오늘/이번 주/누적 포인트 (3.6) | | `PointsSummaryRead` |

### actions — 변경 기록 · 되돌리기 🔑

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `GET /actions` | 최근 변경 기록 (`?limit=10`, 최신순) (3.2.1) | | `ActionRead[]` |
| `POST /actions/{action_id}/undo` | 되돌리기 (이미 되돌렸거나 더 최근 변경이 있으면 `409`) | | `ActionRead` |

`ActionRead`: `id`, `action_type`, `source`, `summary_text`, `affected_ids`, `created_at`, `undone_at?`, `undone`

### health

| 메서드 · 경로 | 설명 | 요청 | 응답 |
|---|---|---|---|
| `GET /health` | 서버 상태 확인 | | `{"status": "ok"}` |

---

## 5. 서버가 보내는 알림 (참고)

설계상 보내는 알림과 현재 상태다. **지금은 푸시가 실제로 기기에 도착하지 않는다**(→ 7. 알려진 제약의 FCM 토큰).

| 시각 | 채널 | 내용 | 클라이언트 동작 | 현재 상태 |
|---|---|---|---|---|
| 매일 08:00 | 푸시 | 수면 체크인 | 수면 기록 입력(3.7) | 예약됨 (토큰 없어 미발송) |
| 매일 21:00 | 푸시 | 저녁 체크인 | 체크인 챗(3.5) | 예약됨 (토큰 없어 미발송) |
| 1주/3주 무응답 | 텔레그램 (opt-in) | 에스컬레이션 메시지 | — | 동작 (봇 토큰·chat_id 필요) |
| 매일 00:00 | (서버 내부) | 전날 포인트 확정 | — | 동작 |
| 일정 시작 시각 | 푸시 | 시작 알림 (`scheduled` 일정) | 일정 상세 | 예약됨 (토큰 없어 미발송) |
| 마감 하루 전 | 푸시 | 마감 리마인더 (`deadline` 일정) | 할 일 목록 | 예약됨 (토큰 없어 미발송) |
| 일정 종료·마감 시각 | 푸시 | 완료 체크 요청 | 완료 처리 또는 미준수 사유 입력(3.4) | 예약됨 (토큰 없어 미발송) |

---

## 6. 개발·테스트 팁

- 서버 실행: `docker compose up --build` 또는 `uvicorn app.main:app --reload` (자세한 내용은 `docker-compose.yml`)
- 테스트 사용자 만들기: `python -m app.scripts.seed` (생성된 사용자 id가 출력된다), 페르소나 넣기: `python -m app.scripts.seed_personas`
- Postman 컬렉션의 `{{userId}}` 변수 하나만 바꾸면 헤더·본문의 사용자가 함께 바뀐다.

---

## 7. 알려진 제약 (클라이언트 설계 시 주의)

아래는 현재 백엔드에 **아직 없는 기능**이다. 필요한 화면이 있으면 백엔드에 요청해 달라.

| 제약 | 영향 |
|---|---|
| 사용자 생성·조회·수정 API 없음 | 사용자는 스크립트로만 만든다. `preferred_language`를 앱에서 바꿀 수도 없다 |
| FCM 디바이스 토큰 등록 API 없음 | 서버가 푸시를 보낼 대상 토큰이 없어, 현재 푸시는 실제로 발송되지 않고 서버 로그에만 남는다 |
| 일정별 알림 job이 서버 메모리에만 있음 | 일정을 만들거나 바꿀 때 대기 중인 회차의 알림을 예약하고, 서버를 재시작하면 DB에서 다시 예약한다. 완료·취소한 회차와 이미 지난 시각은 예약하지 않는다 |
| 정식 인증 없음 (`X-User-Id` 신뢰) | 개발용. 운영 전에 토큰 인증으로 바뀐다 |
| 시간대가 서버 설정 하나 | "오늘/이번 주", 알림 시각, 자정 포인트 계산이 서버의 `APP_TIMEZONE`(기본 America/Toronto)을 따른다. 사용자별 시간대는 아직 없다 |
| LLM이 실패하면 미준수 사유가 저장되지 않음 | `other`/자유 텍스트 사유 입력 중 `502`가 나면 사유도 저장되지 않았다. 재시도 또는 버튼만으로 다시 기록하도록 안내 |
