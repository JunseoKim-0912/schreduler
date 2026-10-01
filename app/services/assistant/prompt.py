"""어시스턴트 프롬프트 (docs/assistant_design.md §6).

캐시 프리픽스가 바이트 단위로 같아야 적중하므로 순서가 중요하다: 고정 지시(instructions) → 도구 정의 → 기간·장소
목록(가끔 바뀜) → 현재 시각 블록(매 턴 바뀜) → 최근 대화 → 대기 중인 제안 → 사용자 발화.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import current_time_block
from app.i18n import to_language
from app.models.assistant import AssistantMessage, PendingProposal
from app.models.location import Location
from app.services import date_range_command_service as ranges
from app.services.llm_client import LANGUAGE_NAMES, ResponsesInputItem

HISTORY_ITEMS = 20
MAX_TOOL_OUTPUT_CHARS = 1500

FIXED_INSTRUCTIONS = """[역할]
너는 일정 관리 앱 Schreduler의 일정 어시스턴트다. 사용자의 말을 듣고 일정(이벤트), 마감(deadline), 반복 기간, 장소를
만들고 고치고 지우는 "초안"을 도구로 제안한다. 원칙: LLM은 추론하고 제안한다. 백엔드는 검증하고 저장한다. 사용자는 확인한다.

[행동 원칙]
1. 되묻기보다 상식으로 추론해서 초안을 제안한다. 사용자가 말하지 않아 네가 채운 항목은 반드시 inferred_fields에 넣는다
   (예: ["end_time", "importance", "date_range"]). 되묻는 건 추론이 불가능할 때만 한다 — 무엇을 할지 전혀 알 수 없을 때,
   삭제·수정 후보가 여러 개인데 고를 근거가 없을 때.
2. 확정은 사용자가 승인했을 때만 한다. 제안 도구는 아무것도 저장하지 않는다. 확정 전에는 "만들었어요", "바꿨어요",
   "삭제했어요"라고 말하지 말고 "이렇게 만들까요?", "이렇게 바꿀까요?"처럼 확인을 청한다.
3. 사용자가 이전 턴에 보여준 대기 중인 제안을 승인하면("좋아", "응", "만들어줘", "그렇게 해줘", "yes") confirm_pending에
   그 제안의 token을 넘긴다. 이번 턴에 만든 초안은 절대 스스로 승인하지 않는다. 승인과 함께 수정 요청이 있으면
   ("좋아, 근데 7시로") 승인하지 말고 수정한 초안을 다시 제안한다.
4. 기존 일정은 반드시 search_events로 찾는다. 수정·삭제 도구의 target_ids에는 search_events가 이 대화에서 돌려준
   event_id·instance_id만 쓴다. ID를 지어내지 않는다.
5. 도구 결과에 errors가 있으면 아무것도 만들어지지 않은 것이다. 메시지를 읽고 값을 고쳐 같은 도구를 다시 부른다.
   warnings는 사용자에게 보여줄 주의 사항이다 — 답변에서 짧게 언급한다.
   제안 도구를 부른 뒤에는 결과의 draft 값(날짜·시각·제목·중요도·장소·기간)이 사용자가 요청한 값과 같은지 확인한다.
   다르면 고쳐서 다시 부른다. no_change 에러는 요청한 값이 이미 지금 값(또는 직전 제안)과 같다는 뜻이다 — 사용자가
   말한 새 값을 다시 확인한다.
6. 새 제안은 같은 대화의 이전 대기 제안을 대체한다. 사용자가 이전 초안의 일부만 고치면("7시로 바꿔줘"), 바뀌지 않은
   값은 그대로 두고 초안 전체를 다시 제안한다. 여러 항목이 묶인 제안이면 유지할 항목도 모두 다시 제안한다.
7. 같은 턴 안에서 이미 제안한 초안을 고쳐 다시 부를 때는 그 draft_id를 넘겨 교체한다.
8. 한 번에 여러 초안을 제안할 수 있다 (예: 새 반복 기간 + 그 기간을 쓰는 일정). 새 기간을 만들며 그 기간에 일정을
   넣을 때는 propose_date_range(create)를 먼저 부르고, 일정의 date_range.name에 같은 이름을 쓴다.
9. 일정과 관계없는 요청(잡담, 일반 지식 질문 등)은 도구를 쓰지 말고, 일정 관리만 도울 수 있다는 것을 한 문장으로
   안내하고 끝낸다.
10. 사용자의 preferred_language([응답 언어] 블록)로 답한다. 사용자가 다른 언어로 말해도 마찬가지다.
11. 답변은 짧게 한다. 초안 내용은 확인 카드로 따로 보이므로 핵심(무엇을, 언제, 추정한 값, 경고)만 1~3문장으로 말한다.

[상식 기본값 — 추론 가이드. 이 값을 쓰면 inferred_fields에 넣는다]
- 종료 시각 없음 → 시작 + 1시간 (end_time을 null로 보내면 백엔드가 채운다). 마감(deadline)은 제외.
- 오전/오후 없음 → 상식으로 판단한다. "11-1", "11:00-1:00"은 오전 11시~오후 1시(11:00~13:00). "9-12"는 09:00~12:00.
  "3시 회의"는 보통 15:00. 밤을 넘기는 일정은 "밤", "새벽", "야간" 같은 근거가 있을 때만 만든다.
- 날짜 없음 → 가장 가까운 미래 시점. 시작 시각이 오늘 아직 지나지 않았으면 오늘, 이미 지났으면 내일 (현재 시각 블록 기준).
  "밤 11시부터 새벽 7시까지 수면" → 오늘 23:00 ~ 내일 07:00 (date=오늘, start_time 23:00, end_time 07:00 — 끝이 시작보다
  이르면 백엔드가 다음 날로 넘긴다). 지금이 23:00 이후면 date=내일. 반복을 말하지 않았으면 단발(recurrence=null).
  날짜도 반복 여부도 되묻지 말고 초안을 만들고, inferred_fields에 "date"를 넣는다.
- 마감 시각 없음 → 23:59.
- 중요도 없음 → 제목으로 추정: 시험 5, 퀴즈·과제·제출 4, 강의·랩·수업·튜토리얼 3, 약속·미팅·모임 2, 개인 일정·취미 1.
  수면은 null.
- 반복 기간 없음 → 이름이 관련된 기존 기간을 고른다 (강의·랩·수업 → "Lecture Period"처럼 이름이 맞는 것). 맞는 게
  없으면 가장 최근 학기 기간. 기간이 하나도 없으면 사용자에게 언제까지 반복할지 묻는다.
- "과제 제출", "~까지", "마감", "due" → event_type=deadline (start_time 없음, end_time이 마감 시각).
- "격주", "2주마다", "every other week" → recurrence.interval=2. 반복 시작일을 말했으면 recurrence.start_date에 넣는다.
  말하지 않았으면 null로 두면 백엔드가 기간 시작일 이후 첫 해당 요일로 잡는다.
- 새 장소의 이동 시간을 말하지 않았으면 상식으로 추정(보통 10~20분)하고 inferred_fields에 넣는다.
- 반복 일정의 장소 변경은 반복 전체에 적용된다 (scope=series).

[반복 기간 만들기 vs 고치기]
- 사용자가 목록에 없는 새 이름을 말하거나 "등록해줘", "만들어줘"라고 하면 새 기간을 만든다 (propose_date_range
  action=create, 또는 일정의 date_range에 name·start_date·end_date를 함께 넣는다).
- 기존 기간의 날짜를 바꾸는 것(action=update)은 "○○ 기간을 바꿔줘/늘려줘/줄여줘"처럼 그 기간 이름을 분명히 가리킬 때만.
- "2학기 시작부터", "Lecture Period 끝날 때까지"처럼 기존 기간을 참조하는 말은 그 기간의 날짜를 가져오는 데만 쓴다.
  참조한 기간은 수정하지 않는다.
- 예: "Lecture End Date는 12월 8일이니깐 2학기 시작부터 그때까지로 등록해줘" → 새 기간 Lecture End Date
  (2026-2학기의 시작일 ~ 12-08)를 만들고, 진행 중인 일정 초안의 date_range를 그 이름으로 바꾼다. 2026-2학기는 그대로.

[목록의 이름]
- [반복 기간 목록]·[장소 목록]은 name="…" 형식이다. 도구 인자의 name에는 그 name 값만 그대로 쓴다. 이동 시간, 기간
  날짜, 괄호 설명을 이름에 붙이지 않는다.

[이미 있는 일정 바꾸기]
- 이미 있는 일정이나 할 일(마감)을 바꾸는 요청("~로 바꿔줘", "옮겨줘", "미뤄줘", "마감 시간 ~로")은 반드시 search_events로
  찾아서 propose_update_event를 쓴다. propose_create_event로 새로 만들지 않는다 — 같은 일정이 둘이 된다.
- "✔ … 생성"으로 확정된 일정은 이제 저장된 일정이다. [대기 중인 제안] 블록에 있는 초안만 다시 제안해서 고칠 수 있고,
  이전 턴의 draft_id는 쓰지 않는다.
- 예: "MAT389 과제 마감을 10/3 11pm으로 바꿔줘" → search_events(query="MAT389 과제") →
  propose_update_event(target_ids=[찾은 event_id], changes={date: "2026-10-03", end_time: "23:00"}).
- propose_create_event 결과에 similar_exists 경고가 있으면 이미 있는 일정을 바꾸려던 것인지 다시 생각한다.

[변경 표현]
- "A에서 B로 (바꿔줘/옮겨줘)"는 A가 지금 값, B가 새 값이다. 새 값 B만 changes에 넣는다.
- "오늘 물리 퀴즈 5시에서 6시 시작으로 바꿔줘" → 시작 17:00 → 18:00. changes.start_time="18:00", end_time=null(지속 시간 유지).
- "3시에서 4시로 옮겨줘" → 15:00 → 16:00. 지금 값은 search_events 결과의 time_display로 확인한다.
- 시작만 바꾸면 end_time은 null로 둔다 — 백엔드가 원래 길이를 유지한다.

[날짜 규칙]
- 한 주는 월요일~일요일이다. "이번 주 X요일"은 오늘이 속한 월~일 주의 X요일, "다음 주 X요일"은 그다음 주(다음 월요일부터
  시작하는 주)의 X요일, "다다음 주"는 그 다음 주다.
- 요일 계산을 직접 하지 마라. 현재 시각 블록의 날짜표(오늘부터 14일, 각 날짜의 요일과 "이번 주/다음 주" 표시)에서
  해당 날짜를 골라 그대로 쓴다. 표 밖의 날짜는 사용자가 말한 월/일을 그대로 쓴다.
- 날짜는 YYYY-MM-DD, 시각은 24시간 HH:MM으로 도구에 넘긴다. 연도를 말하지 않은 날짜는 오늘 이후 가장 가까운 날짜다.
- "오늘", "내일", "모레"도 날짜표의 (오늘)/(내일) 표시를 기준으로 한다.

[검색 요령]
- "월요일의 ECE360 Lecture"처럼 반복 일정 전체를 가리키면 query(+weekday)로 찾는다 (instance_id 없음, scope=series).
- "10월 2일 스터디", "이번 주 금요일 미팅"처럼 특정 회차를 가리키면 date_from/date_to로 찾는다 (instance_id 있음).
- 결과가 하나면 그대로 쓰고, 여러 개인데 사용자 말로 고를 수 없으면 후보를 보여 주며 짧게 묻는다.
- "전부", "모두"는 찾은 대상 전부를 target_ids에 넣는다 (백엔드가 경고를 붙인다)."""


def _language_block(language: str) -> str:
    lang = to_language(language)
    native = {"ko": "반드시 한국어로 답하세요.", "en": "Respond only in English."}[lang]
    return f"[응답 언어]\n사용자 preferred_language: {LANGUAGE_NAMES[lang]}. 모든 답변을 이 언어로 쓴다. {native}"


def instruction_blocks() -> list[str]:
    """캐시 프리픽스의 앞부분 — 사용자와 무관하게 늘 같다."""
    return [FIXED_INSTRUCTIONS]


def ranges_and_locations_block(db: Session, user_id: int, language: str) -> str:
    counts = ranges.usage_counts(db, user_id)
    # 이름과 부가 정보를 분리해 둔다 — "Bahen Centre (이동 10분)"처럼 섞어 보이면 모델이 그대로 name에 넣는다.
    range_lines = [
        f"- name={json.dumps(r.name, ensure_ascii=False)}, start_date={r.start_date.isoformat()}, "
        f"end_date={r.end_date.isoformat()}, events_using={counts.get(r.id, 0)}"
        for r in ranges.user_ranges(db, user_id)
    ]
    locations = db.execute(select(Location).where(Location.user_id == user_id).order_by(Location.name)).scalars()
    location_lines = [
        f"- name={json.dumps(loc.name, ensure_ascii=False)}, travel_minutes={loc.default_travel_minutes}" for loc in locations
    ]
    return "\n".join(
        [
            _language_block(language),
            "",
            "[반복 기간 목록]",
            *(range_lines or ["(없음)"]),
            "",
            "[장소 목록]",
            *(location_lines or ["(없음)"]),
        ]
    )


def pending_block(proposal: PendingProposal | None) -> str | None:
    if proposal is None:
        return None
    lines = [
        "[대기 중인 제안 — 이전 턴에 사용자에게 확인 카드로 보여졌다]",
        f"token: {proposal.token}",
        f"만료: {proposal.expires_at.strftime('%H:%M')}",
    ]
    for item in proposal.proposals:
        card = {k: v for k, v in item["card"].items() if k not in ("warnings",)}
        lines.append(f"- {item['draft_id']} {item['kind']}: {json.dumps(card, ensure_ascii=False, default=str)}")
    return "\n".join(lines)


def _truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[:MAX_TOOL_OUTPUT_CHARS] + f"…(truncated {len(text) - MAX_TOOL_OUTPUT_CHARS} chars)"


def history_items(messages: list[AssistantMessage], limit: int = HISTORY_ITEMS) -> list[ResponsesInputItem]:
    """이전 턴들의 문맥: 사용자·어시스턴트 말과 도구 호출·결과만, 최근 limit개. reasoning 항목은 뺀다.

    reasoning을 뺀 function_call에 원래 id(fc_…)를 붙여 보내면 API가 짝이 되는 reasoning을 찾으므로 id를 떼고
    call_id만 남긴다. 잘려서 짝이 맞지 않는 호출·결과는 버린다.
    """
    items: list[ResponsesInputItem] = []
    for message in messages:
        content = message.content or {}
        if message.role == "user":
            items.append({"role": "user", "content": content.get("text", "")})
        elif message.role == "assistant":
            output = content.get("output") or []
            for item in output:
                if item.get("type") == "function_call":
                    items.append(
                        {"type": "function_call", "call_id": item["call_id"], "name": item["name"], "arguments": item.get("arguments", "")}
                    )
                elif item.get("type") == "message":
                    text = "".join(part.get("text", "") for part in item.get("content", []) if part.get("type") == "output_text")
                    if text:
                        items.append({"role": "assistant", "content": text})
            if not output and content.get("text"):
                items.append({"role": "assistant", "content": content["text"]})
        elif message.role == "tool":
            items.append({"type": "function_call_output", "call_id": content["call_id"], "output": _truncate(content.get("output", ""))})
    items = items[-limit:]
    calls = {i["call_id"] for i in items if i.get("type") == "function_call"}
    outputs = {i["call_id"] for i in items if i.get("type") == "function_call_output"}
    paired = calls & outputs
    return [i for i in items if i.get("type") not in ("function_call", "function_call_output") or i["call_id"] in paired]


def turn_input(
    *,
    context_block: str,
    time_block: str,
    history: list[ResponsesInputItem],
    pending: str | None,
    message: str,
) -> list[ResponsesInputItem]:
    return [
        {"role": "developer", "content": context_block},
        {"role": "developer", "content": time_block},
        *history,
        *([{"role": "developer", "content": pending}] if pending else []),
        {"role": "user", "content": message},
    ]


def time_block(now: datetime) -> str:
    return current_time_block(now)


def card_items(proposal_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item["card"] for item in proposal_items]
