# 일정 어시스턴트(에이전트) 설계 — v4 전환안

- 작성: 2026-09-27
- 배경: 자연어 일정 입력을 "슬롯필링 + 손으로 짠 규칙"(event_parse_service, 1093줄)에서 "LLM이 도구를 써서 초안을 제안하는 에이전트"로 바꾼다. 최근 버그(2주마다 무시, 장소 수정 불가, 마감일 422, 대화 상태 누수, 오전 11시~새벽 1시)는 모두 슬롯과 규칙을 하나씩 덧붙이는 구조에서 나왔다.
- 원칙 한 줄: **LLM은 추론하고 제안한다. 백엔드는 검증하고 저장한다. 사용자는 확인한다.**

---

## 1. 동작 방식 (사용자 입장)

1. 사용자가 자유롭게 말한다. 예: "ECE360 Lab 격주 화요일 9-12시 9/22부터 Lecture period 동안"
2. 어시스턴트는 되묻기보다 **상식적으로 추론해서 초안을 만든다.** 빠진 값(중요도, 종료 시각, 반복 기간 등)은 합리적인 기본값으로 채우고, 추론한 항목에는 "추정" 표시를 붙인다.
3. 확인 카드로 "이렇게 만들까요?"를 보여준다. 반복이면 처음 3회차 날짜도 보여준다. 이상한 값(자정을 넘는 일정, 12시간 초과, 과거 날짜 등)은 경고로 표시한다.
4. 마음에 안 들면 자연어로 고친다("7시로", "매주로", "장소는 Bahen"). 어시스턴트가 초안을 다시 만든다.
5. [만들기] 버튼이나 "좋아" 같은 말로 확정하면 그때 DB에 저장된다. 모든 실행은 되돌리기 기록을 남긴다.
6. **되묻는 경우는 추론이 불가능할 때만.** 예: 무엇을 할지 전혀 알 수 없음, 삭제 대상 후보가 여러 개인데 고를 근거가 없음.

## 2. API 선택

- **Responses API(`/v1/responses`) + 추론(reasoning) 켜기.** gpt-5.6-luna는 Chat Completions에서 도구와 추론을 같이 쓸 수 없다. 날짜·격주·오전/오후처럼 추론이 가장 필요한 곳이 바로 이 기능이므로 추론을 끄지 않는다.
- `reasoning.effort`는 환경변수(`ASSISTANT_REASONING_EFFORT`, 기본 `medium` — C단계 결과로 확정)로 둔다. 평가 세트(§8)로 none/low/medium을 비교해 최종값을 정한다.
- **모델도 따로 설정한다:** `ASSISTANT_MODEL`(기본 `gpt-5.6-luna`, `LLM_MODEL`과 따로). 페르소나·체크인은 계속 `LLM_MODEL`을 쓴다. 더 저렴한 모델(예: `gpt-5-nano` 계열)로 내릴지는 평가 세트로 결정한다 — 후보 모델 × effort 조합을 돌려서 **통과율 기준(예: 90% 이상, 격주·오전/오후·마감 케이스는 전부 통과)을 넘는 것 중 가장 싼 조합**을 고른다.
- 기존 Chat Completions 경로(페르소나 대화, 미준수 피드백, 체크인)는 **그대로 둔다.** 새 Responses 클라이언트는 옆에 추가한다.
- 대화 상태는 OpenAI 쪽 `previous_response_id`에 맡기지 않고 **우리 DB에 저장**한다 (모델·공급자 교체 가능성, 테스트 용이성).

## 3. 도구 (LLM에게 주는 것)

도구는 **읽기 또는 제안만** 한다. DB에 쓰는 도구는 없다. 단, `confirm_pending`만 예외이며 §5의 조건을 통과해야 실행된다.

| 도구 | 역할 | 비고 |
|---|---|---|
| `search_events(query?, date_from?, date_to?, weekday?)` | 제목(느슨한 매칭)·날짜·요일로 이벤트/회차 검색 | 반환 ID만 이후 도구에 쓸 수 있음. 기존 match_events/resolve/list_instances_in_range 재사용 |
| `propose_create_event(...)` | 일반/마감(Task) 이벤트 초안 | event_type, title, 날짜·시각, importance, recurrence{freq, interval, by_day, start_date}, date_range(기존 이름 또는 new{name,start,end}), location(기존 이름 또는 new{name, travel_minutes}), inferred_fields[] |
| `propose_update_event(target_ids, scope, changes)` | 수정 초안 | scope = instance / series. changes에 시각·제목·중요도·장소·마감·반복 포함 |
| `propose_delete_event(target_ids, scope)` | 삭제 초안 | |
| `propose_date_range(action, ...)` | 반복 기간 생성·수정·삭제 초안 | 사용 중인 기간 삭제는 경고 |
| `confirm_pending(token)` | 사용자가 채팅으로 승인했을 때 대기 중인 초안 실행 | §5 조건 |

- 한 턴에 여러 초안을 묶어 제안할 수 있다 (예: 새 기간 + 그 기간을 쓰는 이벤트).
- 제안 도구는 백엔드 검증(§4)을 거친 **정규화된 초안 + 경고/에러**를 돌려준다. 에러면 LLM이 고쳐서 다시 부른다.
- 루프 제한: 한 턴에 LLM 호출 최대 6회, 총 20초. 넘으면 지금까지의 초안을 보여주거나 짧게 되묻는다.

## 4. 백엔드 검증 (안전망)

event_parse_service에 엉켜 있는 규칙을 **순수 함수 모듈**(예: `app/services/draft_rules.py`)로 떼어 낸다. 도구와 기존 코드가 같은 함수를 쓴다 (규칙이 두 벌이 되지 않게).

- 에러 (초안 거절): 종료 ≤ 시작, deadline인데 start_time 있음, RRULE 조립 불가, 존재하지 않는 ID.
- 경고 (카드에 표시, 사용자 확인 필요): 자정을 넘음, 12시간 초과, 과거 날짜, 반복 시작일이 요일과 불일치, 사용 중인 기간 삭제, 여러 개를 한꺼번에 삭제/수정.
- 정규화: 연도 없는 날짜, RRULE 조립(INTERVAL 포함), dtstart = 첫 회차, 미리보기 3회차, 시간 표시("오전 11:00 – 오후 1:00 (2시간)", "(다음 날)").

## 5. 확인과 실행

- 제안 결과는 `PendingProposal`(DB)에 토큰과 함께 저장, 30분 뒤 만료. 같은 세션의 새 제안이 이전 제안을 대체한다.
- 실행 경로 두 가지:
  1. 버튼: 기존 `POST /events/commands/confirm` 흐름 재사용 (또는 `/assistant/confirm`).
  2. 채팅 승인("좋아", "만들어줘"): LLM이 `confirm_pending`을 부른다. 백엔드는 **그 제안이 이전 턴에 이미 사용자에게 보여진 것**인지 확인한다. 같은 턴에 만든 제안을 스스로 승인하는 건 거절한다.
- 실행은 기록이 남는 서비스 함수(create_event_from_nl, execute, date_range_command_service 등)만 쓴다. 기록 없는 함수(create_event, delete_event 등)는 도구에서 호출하지 않는다.

## 6. 프롬프트 구성 (캐싱 고려)

순서 (앞쪽일수록 덜 바뀜):
1. 역할·행동 원칙·기본값 규칙 (고정)
2. 도구 정의 (고정)
3. 반복 기간 목록, 장소 목록 (가끔 바뀜)
4. 현재 시각 블록: 오늘 날짜 + **요일** + 현재 시각 + 시간대(America/Toronto)
5. 최근 대화(요약 포함), 대기 중인 초안
6. 사용자 발화

이벤트 제목 목록은 프롬프트에 넣지 않고 `search_events`로 찾는다 (일정이 바뀔 때마다 캐시가 깨지는 문제 해결).

**상식 기본값 (추론 가이드, 확인 카드에 "추정" 표시):**
- 종료 시각 없음 → 1시간 (마감은 제외).
- 오전/오후 없음 → 상식으로 판단. "11-1"은 오전 11시~오후 1시. 밤을 넘기려면 "밤", "새벽" 같은 근거가 있어야 한다.
- 마감 시각 없음 → 23:59.
- 중요도 없음 → 제목으로 추정 (시험 5, 퀴즈·과제 4, 강의·랩 3, 약속 2, 개인 1, 수면 없음).
- 반복 기간 없음 → 이름이 관련된 기간을 추정 (예: 강의·랩 → "Lecture Period"). 맞는 게 없으면 가장 최근 학기 기간.
- "과제 제출", "~까지", "마감" → deadline.

## 7. 저장소 (새 테이블)

- `AssistantSession`: id, user_id, created_at, updated_at
- `AssistantMessage`: id, session_id, role(user/assistant/tool), content(JSON: 텍스트, 도구 호출, 도구 결과), created_at
- `PendingProposal`: id, session_id, token, proposals(JSON), shown_at, expires_at, status(pending/confirmed/cancelled/expired/superseded)
- `AssistantTurnLog`: id, session_id, llm_calls, input_tokens, cached_tokens, output_tokens, reasoning_tokens, latency_ms, reasoning_effort, created_at (비용·지연 추적)

## 8. 평가 세트

- `tests/assistant_eval/cases.yaml`: 실제 문장 25~30개와 기대하는 초안 속성 (예: `rrule contains INTERVAL=2`, `start 11:00`, `end 13:00`, `event_type deadline`).
- 첫 케이스들은 실제로 실패했던 문장들: 격주 ECE360 Lab, "11:00-1:00", "9/29 11:30 pm MAT389 과제 제출날이야", "월요일의 ECE360 Lecture에 장소 넣어줘 Galbraith 304", "Lecture End Date는 12월 8일이니깐 2학기 시작부터 그때까지", "10월 1일 8시 UTKESA 미팅 1시간", "반복 없이 한번만", "전부 없애줘" 등.
- `python -m app.scripts.eval_assistant --effort low`: 실제 모델로 돌려 통과율, 평균 LLM 호출 수, 토큰, 지연을 표로 출력. pytest에는 넣지 않는다 (실제 API 비용).
- 단위 테스트는 "도구 호출 순서를 미리 적어 둔 가짜 LLM"으로 시나리오를 검증한다.

## 9. 전환 단계

| 단계 | 내용 | 기존 기능 |
|---|---|---|
| A | Responses API 클라이언트 + 사용량 로깅 + 가짜 LLM 도구, 검증 규칙을 draft_rules.py로 분리 | 그대로 동작 (규칙만 공용 함수로 이동) |
| B | 에이전트 코어: 도구, 루프, DB 세션/제안, `POST /assistant/chat`, 확인 흐름, 시나리오 테스트 | 그대로 |
| C | 평가 세트 + eval 스크립트, 모델 × reasoning effort 비교 후 기본값 확정 | 그대로 |
| D | 프론트엔드: 이벤트 탭에 "새 어시스턴트" 전환 스위치, 추정 배지·경고·미리보기 카드 | 스위치로 옛 방식 선택 가능 |
| E ✅ | 며칠 실사용 후 옛 `/events/parse`·slot fill 코드와 관련 테스트 제거, 기획보고서 FR-2 v4 반영 (2026-10-01 완료, §11.10) | 제거 |

각 단계는 Claude Code 새 세션(`/clear`)에서 이 문서를 먼저 읽고 시작한다.

## 10. 열린 질문

1. 어시스턴트 모델과 reasoning effort 최종 조합 (C단계 평가 후). 싼 모델 후보가 Responses API에서 도구 + 추론을 지원하는지도 확인 필요.
2. 되돌리기를 채팅("방금 거 취소해줘")으로도 할지. 지금은 버튼만.
3. 사용자별 시간대 필드(User.timezone). 지금은 서버 설정 하나(America/Toronto).
4. 장기 기억: 페르소나 대화(FR-9)의 장기 문맥 기억에는 MemMachine 도입을 검토한다 (어시스턴트 전환 이후 별도 단계). 어시스턴트의 세션·초안·확인 토큰처럼 정확해야 하는 상태는 계속 SQLite 테이블(§7)에 둔다.

## 11. A단계 이후 확정 사항

(2026-09-27, B단계에서 정함)

### 11.1 A단계 주의사항 반영

1. **격주 첫 회차.** 반복 시작일을 말하지 않으면 dtstart는 기간 시작일이 아니라 **"기간 시작일 이후 첫 해당 요일"**이다.
   `draft_rules.first_occurrence`가 INTERVAL을 뺀 규칙으로 첫 해당 날짜를 찾고, 그 날짜가 dtstart가 되어 격주 리듬의 기준이 된다.
   예: 목요일 9/3에 시작하는 기간의 격주 화요일 → 첫 회차 9/8 (전에는 9/15). 기존 `/events/parse` 흐름도 같은 함수를 써서 함께
   고쳐졌다. 이미 DB에 있는 이벤트는 건드리지 않는다.
2. **reasoning effort 검증.** `llm_client.REASONING_EFFORTS_BY_MODEL`에 모델별 허용 값을 둔다 (OpenAI 모델 문서, 2026-09 확인):
   - gpt-5.6-luna: none, low, medium, high, xhigh, max
   - gpt-5.4-nano / gpt-5.4-mini: none, low, medium, high, xhigh
   - gpt-5-nano / gpt-5-mini: minimal, low, medium, high
   "추론 최소"인 none ↔ minimal은 모델에 맞게 서로 바꿔 준다 (gpt-5-nano에 none → minimal). 그 밖에 지원하지 않는 값이면 앱
   시작(lifespan) 때 `LLMConfigError`로 멈춘다. 목록에 없는 모델은 검증하지 않고 그대로 보낸다.
3. **규칙 에러는 도구 에러로.** `build_rrule`·`draft_rules`의 예외(ValueError 계열)와 AppError는 500으로 새지 않고 도구 결과의
   `errors`로 LLM에게 돌아간다. LLM이 값을 고쳐 다시 부른다.
4. **시간 기준.** 어시스턴트 코드의 "오늘"·"현재 시각"은 전부 `core.clock.local_now()`(APP_TIMEZONE)에서 나온다. 기존 코드의
   `date.today()`는 E단계에서 정리한다.

### 11.2 저장소

- 테이블: `assistant_sessions`(+ `seen_ids`: search_events가 돌려준 ID), `assistant_messages`, `pending_proposals`(+ `warnings`),
  `assistant_turn_logs`(+ `model`, `stop_reason`). 시각은 앱 시간대의 naive 값.
- `assistant_messages`의 assistant 행은 Responses API 응답의 `output` 원본 전체(암호화된 reasoning 포함)를 그대로 저장한다.
  요청은 `store=false` + `include: ["reasoning.encrypted_content"]`. (문서상 stateless 모드에서는 기본으로 붙지만 include도 받는다.)
- 모든 조회는 `X-User-Id` 사용자의 세션만 본다. 다른 사용자의 세션·토큰은 404.

### 11.3 대상 ID 규칙 (§3 보완)

- 수정·삭제 도구의 `target_ids`는 **이번 세션에서 search_events가 돌려준 ID만** 받는다 (`assistant_sessions.seen_ids`).
  DB에 실제로 있는 ID라도 검색으로 찾지 않았으면 `unknown_id` 에러로 돌려준다.

### 11.4 문맥 규칙 (§6 보완)

- **한 턴 안의 도구 루프:** 직전 응답의 `output` 전체(암호화된 reasoning 포함)를 다음 입력에 그대로 다시 넣는다.
- **턴과 턴 사이:** 최근 20개 항목(사용자 말, 어시스턴트 말, 도구 호출, 도구 결과)만 넣고 reasoning 항목은 뺀다. 도구 결과는
  1,500자에서 자른다. reasoning 없이 보내는 function_call은 원래 `id`(fc_…)를 떼고 `call_id`만 남긴다 — id가 있으면 API가
  짝이 되는 reasoning 항목을 요구한다. 잘려서 짝이 맞지 않는 호출·결과는 버린다.
- 입력 순서: 고정 지시(instructions) → 도구 정의 → [응답 언어 + 반복 기간·장소 목록] → [현재 시각 + 14일 날짜표] → 최근 대화 →
  [대기 중인 제안] → 사용자 발화. 고정부(지시+도구)는 약 3,300토큰이라 캐시 최소 길이(1,024)를 넘는다.

### 11.5 C단계 후보 모델

`gpt-5.6-luna`(현재 기본), `gpt-5.4-nano`, `gpt-5.4-mini`, `gpt-5-nano` × reasoning effort 조합을 평가 세트(§8)로 비교한다.

### 11.6 반복 일정의 과거 회차 (C단계 전 수정)

- 반복 일정의 dtstart(`Event.start_time`)는 **격주 리듬의 기준으로만** 쓴다: 사용자가 말한 시작일, 없으면 기간 시작일 이후 첫
  해당 요일(11.1-1).
- 실제 EventInstance는 **오늘(`local_now`) 이후 날짜만** 만든다 (`draft_rules.upcoming_occurrences`). 예: 오늘 9/27에 "9/22부터 격주
  화요일" → 리듬은 9/22 기준, 회차는 10/6, 10/20, … 미리보기 3회차도 오늘 이후 회차다. 어시스턴트 카드의 `date`는 첫 실제 회차,
  `recurrence_start`는 리듬 기준일이다.
- 그래서 반복 일정에는 `past_date` 경고가 붙지 않는다. 단발 일정이 과거 날짜면 지금처럼 경고한다.
- 적용 범위: 어시스턴트와 `/events/parse`(둘 다 `create_event_from_nl` → `build_event(instances_from=오늘)`), 기간 수정 시 회차
  다시 맞추기(`sync_range_instances`). 이동시간 하위 일정은 부모의 가장 이른 회차부터 만들어 부모와 날짜를 맞춘다. 화면의
  `POST /events`와 이미 DB에 있는 이벤트는 바뀌지 않는다.

### 11.7 C단계 모델 선택 기준

- 평가 세트(`tests/assistant_eval/cases.yaml`) **통과율 90% 이상**이고, 필수 범주(**격주, 오전오후, 마감, 확인안전**)는 **100%** 통과한
  조합 중 **턴당 예상 비용이 가장 낮은 것**. 비용이 비슷하면 턴 지연이 짧은 쪽.
- 1차는 후보 모델 × effort(low, medium) 조합당 1회, 2차는 기준을 통과한 상위 2개를 2회 더 돌려 결과가 흔들리지 않는지 본다.
- 결정(기본값 변경)은 사람이 한다. 스크립트는 표와 추천만 낸다.

### 11.8 D단계 프론트엔드

- 기본값: `ASSISTANT_MODEL=gpt-5.6-luna`, `ASSISTANT_REASONING_EFFORT=medium` (LLM_MODEL과 따로 간다).
- 이벤트 탭 위 [새 어시스턴트 | 기존 방식] 스위치, 기본은 새 어시스턴트. 선택은 localStorage(`schreduler.eventsMode`).
- `frontend/assistant.js`가 대화·카드를, `format.describeAssistantItem`이 카드 한 장의 행·추정 배지·경고를 만든다.
  시간은 백엔드 `time_display`를 그대로 쓴다. 반복 기준일 문구("9/22 기준 격주")는 INTERVAL이 2 이상이고
  `recurrence_start`가 첫 회차와 다를 때만 보인다 (매주 반복에서는 의미가 없다).
- 실행 버튼 이름은 전부 생성이면 [만들기], 수정·삭제가 섞이면 [실행].
- 탭을 옮겼다 돌아오면 같은 세션이면 화면을 다시 그리지 않는다 (결과 말풍선의 되돌리기 버튼 유지). 새로고침 뒤에는
  서버 기록(사용자 말·어시스턴트 답)으로 다시 그리므로 지난 결과의 되돌리기는 "최근 변경"에서 한다.

### 11.9 옛 방식과 비용 비교 (2026-09-30)

- `python -m app.scripts.compare_nl_paths`(E단계에서 스크립트는 지우고 결과 JSON만 남김): 평가 세트 10건(생성 8, 수정 2)을 옛 `/events/parse`와 새 어시스턴트로 "일정 하나 확정까지"
  돌려 턴·호출·토큰·비용을 나란히 비교한다. 옛 경로 사용량은 `llm_client.collect_chat_usage()`로 코드에서 모으고, Chat
  Completions의 `completion_tokens_details.reasoning_tokens`도 읽는다 (과금은 원래 completion_tokens에 포함돼 있었다).
- 1회 결과(`results/compare_nl_paths_20260930-190127.json`, 둘 다 gpt-5.6-luna, 옛 방식은 reasoning_effort 미지정):
  둘 다 확정한 8건 기준 1,000건당 옛 $0.74 / 새 $0.75로 사실상 같다. 옛 방식은 2건이 예상 밖의 중요도 질문에서 멈췄다
  (새 방식 10/10 확정·정답). 새 방식은 호출이 많아 캐시 안 된 입력이 약 3배, 옛 방식은 JSON 슬롯 출력·추론 토큰이 많아
  출력이 약 1.8배라 서로 상쇄된다.

### 11.10 E단계: 옛 경로 제거 (2026-10-01)

- 제거 직전 커밋에 태그 `before-remove-nl-parse`. 커밋: 시나리오 이전 → 옛 코드 제거 → `date.today()` 정리 → 문서.
- **지운 것:** `POST /events/parse`, `POST /events/commands/confirm`, `event_parse_service`, `slot_fill_session`(메모리 세션·옛 확인
  토큰), `schemas/event_parse`, `llm_client`의 슬롯필링·초안 수정(1,281 → 644줄), `event_command_service`의 `resolve`·확인 토큰 흐름,
  `compare_nl_paths` 스크립트, 안 쓰게 된 i18n 키 53개(101 → 48), 프론트엔드 [기존 방식] 모드와 전환 스위치(`schreduler.eventsMode`
  키는 페이지를 열 때 지운다).
- **남긴 것 (이름은 옛날 것):** `event_command_service`(`execute`, `create_event_from_nl`, `match_events`, `similar_events`,
  `delete_*_from_ui`), `date_range_command_service`, `ActionSource.NL`(`"nl"`). `CommandDescription.from_dict`는 저장된 제안의
  예전 키(`all`, `target_weekday`)를 무시한다. 옛 경로 전용 DB 테이블·컬럼은 없었다.
- **테스트:** 의미 있는 시나리오는 `tests/assistant_flow.py`(가짜 LLM 대본으로 채팅 → [만들기])로 옮겼다 —
  `test_assistant_scenarios.py`(기간·장소·회차/시리즈 수정·되돌리기), `test_search.py`(제목 매칭), test_undo·test_single_instances·
  test_i18n_regression의 자연어 부분, LLM 실패 502/500. 평가 세트에 `draft_add_recurrence`, `draft_remove_recurrence`,
  `draft_switch_to_deadline`, `list_ranges`(옛 `list_message` 대신 프롬프트의 기간 목록으로 답하는지, `reply_contains`) 추가 → 34건.
  pytest는 1,006(이전 직후) → 795.
- **시간 기준:** 서비스·API·스크립트·모델 기본값의 `date.today()`/naive `datetime.now()`를 `local_today()`/`local_wall_now()`
  (APP_TIMEZONE)로 바꿨다. `utcnow`를 쓰던 미준수 리포트·에스컬레이션은 aware로 계산하고 저장은 naive UTC 그대로다.
- **제거 후 평가 (luna/medium 1회, `results/gpt-5.6-luna_medium_20260930-234747.json`):** 32/34(94%), 기준 미충족(오전오후 4/5).
  이전 1차는 29/30(97%). 어시스턴트의 프롬프트·도구·요청은 제거 전과 같다(diff 확인) — 실패 2건은 이번 변경과 무관하다.
  - `delete_all_followup`: 지금까지 4회 모두 실패. "전부 다 없애줘"를 직전 삭제 제안의 승인으로 보고 실행한다 (알려진 실패).
  - `sleep_overnight`("밤 11시부터 새벽 7시까지 수면"): **알려진 흔들림, 4회 중 2회 실패.** 날짜·반복 여부가 없어 초안 대신
    "어느 날짜로/매일 반복할까요?"라고 되묻는 경우가 있다. 다음 작업에서 "날짜 없는 단발 일정은 오늘(지났으면 내일)로 추정"
    같은 규칙으로 따로 고친다.

### 11.11 반복 실패 수정 (2026-10-01, luna/medium 유지)

- **날짜 없는 일정:** 상식 기본값에 "날짜 없음 → 가장 가까운 미래 시점(시작 시각이 지났으면 내일), 반복을 말하지 않으면 단발,
  되묻지 않고 date를 inferred_fields에" 추가. "밤 11시부터 새벽 7시까지 수면" → 오늘 23:00 ~ 내일 07:00.
- **새 기간 vs 기존 기간 수정:** 새 이름이나 "등록해줘/만들어줘"는 새 기간, 날짜 수정은 그 기간을 분명히 가리킬 때만.
  "2학기 시작부터"처럼 참조한 기간은 날짜를 가져오는 데만 쓰고 수정하지 않는다 (Lecture End Date 예시를 프롬프트에).
- **"변경 사항 없음"(`no_change`) 도구 에러:** `propose_update_event`에서 요청한 값이 대상의 지금 값과 전부 같으면, 그리고
  새 초안이 이전 턴의 대기 제안(초안 하나)과 완전히 같으면 에러로 돌려보낸다 (`TurnContext.pending`). 여러 초안이 묶인
  제안은 일부를 그대로 다시 내는 게 정상이라 검사하지 않는다. 프롬프트에는 "도구 결과 값이 요청과 같은지 확인, 확정 전엔
  '이렇게 바꿀까요?'"를 넣었다.
- **이름 표기와 매칭:** 프롬프트의 기간·장소 목록을 `name="Bahen Centre", travel_minutes=10`처럼 이름과 부가 정보를 나눠
  보여주고 도구 인자에는 name만 쓰게 했다. 백엔드 이름 매칭(`common.name_key`)은 괄호 안 설명·대소문자·공백을 무시한다
  (장소·반복 기간 공통).
- **"A에서 B로":** A는 지금 값, B는 새 값이라는 규칙과 예시("5시에서 6시 시작으로" → 17:00 → 18:00, 길이 유지).
- 고정 지시가 길어져 턴당 입력이 약 7.8k → 9.2k 토큰(대부분 캐시)이 됐다.
- **평가 세트 (34 → 38건):** `sleep_after_bedtime_is_tomorrow`, `new_named_range_is_created`, `location_name_with_parenthetical`,
  `move_3_to_4_today` 추가. `sleep_overnight`은 날짜·단발·추정 표시까지, `lecture_end_date_new_range`는 새 기간 생성과
  `update_range` 없음까지 확인한다. `delete_all_followup`은 1턴의 전체 삭제 제안을 2턴에 승인(confirm_pending)하는 것도
  통과(`any_of`). 실행기에 `any_of`, `previous_items`, `proposal.kinds_exclude`, `inferred_includes`, `date_range_new` 조건 추가.
- **결과 (2회):**

  | 실행 | 통과 | 기준 | 기간 | 기타 | 삭제 | 수정 | 오전오후 | 장소 | 턴당 비용 | 지연 |
  |---|---|---|---|---|---|---|---|---|---|---|
  | 이전 (09-30, 34건) | 32/34 94% | 미충족 | 4/4 | 5/5 | 2/3 | 6/6 | 4/5 | 2/2 | $0.000627 | 4.3초 |
  | 1회 (`20261001-012715`) | 37/38 97% | 충족 | 5/5 | 6/6 | 3/3 | 6/7 | 5/5 | 3/3 | $0.000685 | 4.9초 |
  | 2회 (`20261001-012823`) | 36/38 95% | 충족 | 5/5 | 6/6 | 3/3 | 5/7 | 5/5 | 3/3 | $0.000512 | 4.5초 |

  필수 범주(격주·오전오후·마감·확인안전)는 두 번 모두 100%. `sleep_overnight`·`sleep_after_bedtime_is_tomorrow`,
  기간·장소·"A에서 B로"·`change_to_7_supersedes`·`delete_all_followup`은 두 번 모두 통과.
- **남은 실패는 케이스 쪽 오류였다 (실행 뒤 케이스를 고침, 고친 케이스로는 아직 실제 실행 안 함):**
  - `draft_switch_to_deadline`(2회 모두): 모델이 1턴에서 이미 15:00 마감으로 제안해 2턴엔 바꿀 게 없었고, `no_change` 덕분에
    "이미 반영돼 있어요"라고 답했다. 예전엔 같은 초안을 다시 내서 통과했다. → 1턴 제안이 이미 그 마감이면 제안 없음도 통과.
  - `draft_remove_recurrence`(2회차): 기준 시각 9/27이 일요일이라 "이번 주 수요일"은 날짜 규칙(월~일)상 9/23이 맞다.
    → 기준 시각을 월요일(9/28)로 바꿈.

