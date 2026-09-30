"""옛 자연어 입력(/events/parse, 슬롯필링)과 새 어시스턴트(/assistant/chat)의 토큰·비용 비교. 실제 API를 부른다.

    python -m app.scripts.compare_nl_paths
    python -m app.scripts.compare_nl_paths --only study_11_to_1,mat389_deadline_2330
    python -m app.scripts.compare_nl_paths --summarize "tests/assistant_eval/results/compare_nl_paths_*.json"  (API 호출 없음)

기준은 "일정 하나를 확정할 때까지"다. 케이스마다 두 방식을 각자의 메모리 SQLite(평가 세트 seed)에서 돌리고, 시각은
케이스의 now로 고정한다 (freezegun — 옛 코드가 date.today()를 직접 쓰기 때문). 실제 schreduler.db는 쓰지 않는다.

- 옛 방식: 되물으면 케이스의 예상 답변 중 질문에 맞는(ask 정규식) 첫 답을 보낸다. 맞는 답이 없으면 "추가 질문 발생"으로
  멈춘다. 초안·확인 대기가 나오면 [만들기] 버튼과 같은 경로(execute_pending)로 확정한다.
- 새 어시스턴트: 제안이 나오면 [만들기] 버튼(agent.confirm)으로 확정한다. 확정 버튼은 LLM을 부르지 않는다(두 방식 모두).
- 사용량: 옛 방식은 llm_client.collect_chat_usage(), 새 방식은 assistant_turn_logs에서 코드로 모은다.
결과는 표로 출력하고 tests/assistant_eval/results/compare_nl_paths_*.json으로 저장한다.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import re
import time as time_module
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.exceptions import AppError
from app.models import AssistantTurnLog, Base, Event, EventInstance, User
from app.models.enums import EventInstanceStatus
from app.schemas.event_parse import EventParseRequest
from app.scripts.eval_assistant import PRICES, PRICES_CHECKED, RESULTS_DIR, ROOT, load_cases, seed_database
from app.services import event_command_service, event_parse_service
from app.services.assistant import agent
from app.services.llm_client import LLMClientError, collect_chat_usage, resolve_reasoning_effort
from app.services.slot_fill_session import clear_all_pending_actions, clear_all_sessions, take_pending_action

MAX_TURNS = 6
NlPath = Literal["old", "new"]

# 옛 방식으로도 할 수 있는 케이스 (생성 위주 + 수정 2개). 발화·now·seed는 cases.yaml에서 가져온다.
# answers: 되물으면 ask(정규식)가 질문에 맞는 첫 미사용 답을 보낸다. check: 확정 뒤 DB에서 확인할 값.
COMPARE_CASES: list[dict[str, Any]] = [
    {
        "id": "biweekly_lab_from_0922",
        "answers": [
            {"ask": "언제까지|기간", "say": "Lecture Period 동안"},
            {"ask": "오전|오후", "say": "오전"},
            {"ask": "자주|주기|간격", "say": "2주마다"},
        ],
        "check": {"title_contains": "ECE360 Lab", "rrule_contains": ["INTERVAL=2", "BYDAY=TU"], "start": "09:00", "end": "12:00",
                  "importance": 4, "recurrence_start": "2026-09-22", "first_date": "2026-10-06", "date_range": "Lecture Period"},
    },
    {
        "id": "study_11_to_1",
        "answers": [
            {"ask": "오전|오후", "say": "오전"},
            {"ask": "자주|반복", "say": "반복 없이 한번만"},
        ],
        "check": {"title_contains": "스터디", "first_date": "2026-10-02", "start": "11:00", "end": "13:00", "recurring": False},
    },
    {
        "id": "utkesa_meeting_one_hour",
        "answers": [
            {"ask": "오전|오후", "say": "오후"},
            {"ask": "자주|반복", "say": "반복 없이 한번만"},
        ],
        "check": {"title_contains": "UTKESA", "first_date": "2026-10-01", "start_in": ["08:00", "20:00"], "duration_minutes": 60,
                  "recurring": False},
    },
    {
        "id": "sleep_overnight",
        "answers": [
            {"ask": "며칠|날짜|언제", "say": "오늘"},
            {"ask": "자주|반복", "say": "반복 없이 한번만"},
        ],
        "check": {"start": "23:00", "end": "07:00", "duration_minutes": 480},
    },
    {
        "id": "mat389_deadline_2330",
        "answers": [],
        "check": {"title_contains": "MAT389", "event_type": "deadline", "first_date": "2026-09-29", "end": "23:30", "recurring": False},
    },
    {
        "id": "friday_lab_report_deadline",
        "answers": [
            {"ask": "마감 시각|몇 시", "say": "밤 11시 59분"},
            {"ask": "자주|반복", "say": "반복 없이 한번만"},
        ],
        "check": {"event_type": "deadline", "first_date": "2026-10-02", "end": "23:59"},
    },
    {
        "id": "dentist_next_tuesday_from_wednesday",
        "answers": [
            {"ask": "오전|오후", "say": "오후"},
            {"ask": "끝나|종료", "say": "4시"},
            {"ask": "자주|반복", "say": "반복 없이 한번만"},
        ],
        "check": {"title_contains": "치과", "first_date": "2026-10-06", "start": "15:00", "end": "16:00"},
    },
    {
        "id": "one_off_no_repeat",
        "answers": [
            {"ask": "끝나|종료", "say": "오후 3시"},
        ],
        "check": {"title_contains": "동아리", "first_date": "2026-10-10", "start": "14:00", "recurring": False},
    },
    {
        "id": "importance_update",
        "answers": [
            {"ask": "한 번만|반복 전체|전체", "say": "반복 전체"},
        ],
        "check": {"event_title": "물리 퀴즈", "event_importance": 5},
    },
    {
        "id": "quiz_today_5_to_6",
        "answers": [
            {"ask": "오전|오후", "say": "오후"},
            {"ask": "한 번만|반복 전체|전체", "say": "이번 한 번만"},
        ],
        "check": {"event_title": "물리 퀴즈", "instance_date": "2026-09-26", "instance_start": "18:00"},
    },
]


@dataclass
class TurnUsage:
    say: str
    reply: str = ""
    llm_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    llm_latency_ms: int = 0
    wall_ms: int = 0


@dataclass
class PathRun:
    path: NlPath
    status: str = "running"  # confirmed | 추가 질문 발생 | 턴 한도 | 오류
    correct: bool = False
    problems: list[str] = field(default_factory=list)
    turns: list[TurnUsage] = field(default_factory=list)
    confirm_ms: int = 0
    questions: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    efforts: list[str | None] = field(default_factory=list)

    def total(self, attr: str) -> int:
        return sum(getattr(turn, attr) for turn in self.turns)

    @property
    def wall_ms(self) -> int:
        return self.total("wall_ms") + self.confirm_ms


def cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    """C단계와 같은 가격표. 출력 토큰은 추론 토큰을 포함한 값이다 (두 API 모두)."""
    price = PRICES.get(model)
    if price is None:
        return 0.0
    uncached = max(input_tokens - cached_tokens, 0)
    return (uncached * price["input"] + cached_tokens * price["cached"] + output_tokens * price["output"]) / 1_000_000


# --- 답변 고르기 ----------------------------------------------------------------------------


class Answers:
    def __init__(self, specs: list[dict[str, str]]) -> None:
        self.specs = specs
        self.used: set[int] = set()

    def reply_to(self, question: str) -> str | None:
        for index, spec in enumerate(self.specs):
            if index not in self.used and re.search(spec["ask"], question):
                self.used.add(index)
                return spec["say"]
        return None


# --- 확정 뒤 DB 확인 ------------------------------------------------------------------------


def _hhmm(value: datetime | None) -> str | None:
    return value.strftime("%H:%M") if value else None


def _active(event: Event) -> list[EventInstance]:
    return sorted((i for i in event.instances if i.status != EventInstanceStatus.CANCELLED), key=lambda i: i.date)


def check_result(db: Session, seed_ids: set[int], check: dict[str, Any]) -> list[str]:
    problems: list[str] = []

    def expect(ok: bool, message: str) -> None:
        if not ok:
            problems.append(message)

    if "event_title" in check:
        events = [e for e in db.execute(select(Event).where(Event.parent_event_id.is_(None))).scalars() if check["event_title"] in e.title]
        if len(events) != 1:
            return [f"'{check['event_title']}' 일정 {len(events)}개"]
        event = events[0]
        db.refresh(event)
        if "event_importance" in check:
            expect(event.importance == check["event_importance"], f"importance={event.importance}≠{check['event_importance']}")
        if "instance_date" in check:
            instance = next((i for i in _active(event) if i.date.isoformat() == check["instance_date"]), None)
            if instance is None:
                problems.append(f"{check['instance_date']} 회차 없음")
            else:
                start = _hhmm(instance.effective_start)
                expect(start == check["instance_start"], f"{check['instance_date']} start={start}≠{check['instance_start']}")
        return problems

    created = [
        e for e in db.execute(select(Event).where(Event.parent_event_id.is_(None))).scalars() if e.id not in seed_ids
    ]
    if len(created) != 1:
        return [f"새 일정 {len(created)}개 ({[e.title for e in created]})"]
    event = created[0]
    instances = _active(event)
    if not instances:
        return ["회차 없음"]
    first = instances[0]
    start, end = first.effective_start, first.effective_end
    for key, want in check.items():
        if key == "title_contains":
            expect(want.casefold() in event.title.casefold(), f"title={event.title!r}")
        elif key == "event_type":
            expect(event.event_type.value == want, f"event_type={event.event_type.value}")
        elif key == "first_date":
            expect(first.date.isoformat() == want, f"first_date={first.date}≠{want}")
        elif key == "start":
            expect(_hhmm(start) == want, f"start={_hhmm(start)}≠{want}")
        elif key == "start_in":
            expect(_hhmm(start) in want, f"start={_hhmm(start)}∉{want}")
        elif key == "end":
            expect(_hhmm(end) == want, f"end={_hhmm(end)}≠{want}")
        elif key == "duration_minutes":
            minutes = (end - start).total_seconds() / 60 if start else None
            expect(minutes == want, f"duration={minutes}≠{want}")
        elif key == "recurring":
            expect(event.is_recurring == want, f"recurring={event.is_recurring}")
        elif key == "rrule_contains":
            rule = event.recurrence_rule or ""
            expect(all(part in rule for part in want), f"rrule={rule!r}")
        elif key == "recurrence_start":
            expect(event.anchor_time.date().isoformat() == want, f"dtstart={event.anchor_time.date()}≠{want}")
        elif key == "importance":
            expect(event.importance == want, f"importance={event.importance}≠{want}")
        elif key == "date_range":
            name = event.date_range.name if event.date_range else None
            expect(bool(name) and want.casefold() in name.casefold(), f"date_range={name!r}")
        else:
            problems.append(f"unknown check {key!r}")
    return problems


# --- 두 방식 실행 ---------------------------------------------------------------------------


def run_old(db: Session, user: User, case: dict[str, Any], answers: Answers) -> PathRun:
    run = PathRun("old")
    session_id: str | None = None
    say = case["turns"][0]["say"]
    while len(run.turns) < MAX_TURNS:
        turn = TurnUsage(say=say)
        started = time_module.perf_counter()
        with collect_chat_usage() as records:
            try:
                response = event_parse_service.parse_event_utterance(
                    db, EventParseRequest(user_id=user.id, utterance=say, session_id=session_id)
                )
            except (LLMClientError, AppError) as exc:
                run.status, run.problems = "오류", [f"{type(exc).__name__}: {exc}"[:300]]
                run.turns.append(turn)
                return run
        turn.wall_ms = round((time_module.perf_counter() - started) * 1000)
        for record in records:
            usage = record.usage
            turn.llm_calls += 1
            turn.llm_latency_ms += record.latency_ms
            run.models.append(record.response_model or record.requested_model)
            run.efforts.append(record.reasoning_effort)
            if usage is not None:
                turn.input_tokens += usage.prompt_tokens
                turn.cached_tokens += usage.cached_tokens
                turn.output_tokens += usage.completion_tokens
                turn.reasoning_tokens += usage.reasoning_tokens
        session_id = response.session_id
        command = response.command
        question = response.next_question.question if response.next_question else None
        turn.reply = question or response.message or ""
        run.turns.append(turn)

        if command is not None and command.status == "executed":
            run.status = "confirmed"
            return run
        if command is not None and command.status == "needs_confirmation" and command.confirmation_token:
            # [만들기]/[실행] 버튼: POST /events/commands/confirm과 같은 경로 (LLM 호출 없음)
            started = time_module.perf_counter()
            pending = take_pending_action(command.confirmation_token, user.id)
            event_command_service.execute_pending(db, user, pending, None)
            run.confirm_ms = round((time_module.perf_counter() - started) * 1000)
            run.status = "confirmed"
            return run
        asked = question or response.message or ""
        run.questions.append(asked)
        say = answers.reply_to(asked)
        if say is None:
            run.status = "추가 질문 발생"
            return run
    run.status = "턴 한도"
    return run


def run_new(db: Session, user: User, case: dict[str, Any], answers: Answers) -> PathRun:
    run = PathRun("new")
    session_id: int | None = None
    say = case["turns"][0]["say"]
    while len(run.turns) < MAX_TURNS:
        turn = TurnUsage(say=say)
        started = time_module.perf_counter()
        try:
            result = agent.chat(db, user, say, session_id)
        except LLMClientError as exc:
            run.status, run.problems = "오류", [f"{type(exc).__name__}: {exc}"[:300]]
            run.turns.append(turn)
            return run
        turn.wall_ms = round((time_module.perf_counter() - started) * 1000)
        session_id = result.session_id
        log = db.execute(
            select(AssistantTurnLog).where(AssistantTurnLog.session_id == session_id).order_by(AssistantTurnLog.id.desc()).limit(1)
        ).scalar_one()
        turn.llm_calls, turn.llm_latency_ms = log.llm_calls, log.latency_ms
        turn.input_tokens, turn.cached_tokens = log.input_tokens, log.cached_tokens
        turn.output_tokens, turn.reasoning_tokens = log.output_tokens, log.reasoning_tokens
        run.models.append(log.model)
        run.efforts.append(log.reasoning_effort)
        turn.reply = result.reply
        run.turns.append(turn)

        if result.executed:
            run.status = "confirmed"
            return run
        if result.proposal is not None:
            started = time_module.perf_counter()
            agent.confirm(db, user, session_id, result.proposal["token"])
            run.confirm_ms = round((time_module.perf_counter() - started) * 1000)
            run.status = "confirmed"
            return run
        run.questions.append(result.reply)
        say = answers.reply_to(result.reply)
        if say is None:
            run.status = "추가 질문 발생"
            return run
    run.status = "턴 한도"
    return run


def run_path(path: NlPath, case: dict[str, Any], spec: dict[str, Any], seed: dict[str, Any]) -> PathRun:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    clear_all_sessions()
    clear_all_pending_actions()
    # freezegun은 고정 시각을 UTC로 보고 aware now(tz)를 만든다. 그래서 케이스의 현지 시각을 UTC로 바꿔 고정하면
    # local_now()(새 어시스턴트)는 정확히 케이스의 now가 되고, 옛 코드의 naive date.today()도 같은 날짜가 된다.
    local = datetime.fromisoformat(case["now"]).replace(tzinfo=ZoneInfo(settings.app_timezone))
    frozen = local.astimezone(UTC).replace(tzinfo=None)
    with freeze_time(frozen, tick=True), Session(engine) as db:
        user = seed_database(db, {**seed, **(case.get("seed") or {})}, case.get("language", "ko"))
        seed_ids = set(db.execute(select(Event.id)).scalars())
        runner = run_old if path == "old" else run_new
        run = runner(db, user, case, Answers(spec["answers"]))
        if run.status == "confirmed":
            db.expire_all()
            run.problems = check_result(db, seed_ids, spec["check"])
            run.correct = not run.problems
    engine.dispose()
    return run


# --- 요약·출력 ------------------------------------------------------------------------------


def path_summary(runs: list[PathRun], model: str) -> dict[str, Any]:
    count = max(len(runs), 1)
    totals = {
        attr: sum(r.total(attr) for r in runs)
        for attr in ("llm_calls", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "llm_latency_ms")
    }
    totals["turns"] = sum(len(r.turns) for r in runs)
    totals["wall_ms"] = sum(r.wall_ms for r in runs)
    totals["cost_usd"] = sum(cost_usd(model, r.total("input_tokens"), r.total("cached_tokens"), r.total("output_tokens")) for r in runs)
    return {
        "cases": len(runs),
        "confirmed": sum(r.status == "confirmed" for r in runs),
        "correct": sum(r.correct for r in runs),
        "totals": totals,
        "averages": {k: v / count for k, v in totals.items()},
        "cache_hit_rate": totals["cached_tokens"] / totals["input_tokens"] if totals["input_tokens"] else 0.0,
        "cost_per_1000_usd": totals["cost_usd"] / count * 1000,
    }


def _row(run: dict[str, Any], model: str) -> str:
    t = run["totals"]
    cost = cost_usd(model, t["input_tokens"], t["cached_tokens"], t["output_tokens"])
    mark = "O" if run["correct"] else ("X" if run["status"] == "confirmed" else run["status"])
    return (
        f"{t['turns']:>2} {t['llm_calls']:>3} {t['input_tokens']:>6} {t['cached_tokens']:>6} {t['output_tokens']:>5} "
        f"{t['reasoning_tokens']:>5} {cost * 1000:>7.3f} {run['wall_ms'] / 1000:>5.1f} {mark:<6}"
    )


def print_report(report: dict[str, Any]) -> None:
    old, new = report["paths"]["old"], report["paths"]["new"]
    print(f"\n== 옛 방식 vs 새 어시스턴트 (일정 하나 확정까지) {report['run_at']}")
    print(f"옛 방식: /events/parse, Chat Completions, 모델 {old['model']} (실제 응답: {', '.join(old['response_models']) or '-'}), "
          f"reasoning_effort {old['effort']}")
    print(f"새 방식: /assistant/chat, Responses API, 모델 {new['model']} (실제 응답: {', '.join(new['response_models']) or '-'}), "
          f"reasoning_effort {new['effort']}")
    cols = "턴 호출   입력   캐시  출력  추론  m$/건   초 결과  "
    print(f"\n{'case':38} | {cols}| {cols}")
    print(f"{'':38} | {'옛 방식':<{len(cols)}}| 새 어시스턴트")
    for case in report["cases"]:
        print(f"{case['id']:38} | {_row(case['old'], old['model'])} | {_row(case['new'], new['model'])}")
        for label, key in (("옛", "old"), ("새", "new")):
            for problem in case[key]["problems"]:
                print(f"{'':40}{label}: {problem}")
    print()
    for label, key in (("옛 방식", "old"), ("새 어시스턴트", "new")):
        s = report["paths"][key]["summary"]
        t, a = s["totals"], s["averages"]
        print(
            f"{label:8} 확정 {s['confirmed']}/{s['cases']}, 정답 {s['correct']}/{s['cases']} | "
            f"평균: 턴 {a['turns']:.1f}, 호출 {a['llm_calls']:.1f}, 입력 {a['input_tokens']:.0f} (캐시 {s['cache_hit_rate']:.0%}), "
            f"출력 {a['output_tokens']:.0f} (추론 {a['reasoning_tokens']:.0f}), {a['wall_ms'] / 1000:.1f}초, ${a['cost_usd']:.5f}/건 | "
            f"합계 ${t['cost_usd']:.4f} | 1,000건당 ${s['cost_per_1000_usd']:.2f}"
        )
    # 한쪽이 중간에 멈춘 케이스는 덜 쓴 채로 끝나 싸 보이므로, 둘 다 확정한 케이스만 따로 비교한다.
    both = [c for c in report["cases"] if c["old"]["status"] == "confirmed" and c["new"]["status"] == "confirmed"]
    if both:
        parts = []
        for label, key in (("옛", "old"), ("새", "new")):
            t = {attr: sum(c[key]["totals"][attr] for c in both) for attr in ("input_tokens", "cached_tokens", "output_tokens", "llm_calls", "turns")}
            cost = cost_usd(report["paths"][key]["model"], t["input_tokens"], t["cached_tokens"], t["output_tokens"])
            parts.append(f"{label} ${cost:.5f} (턴 {t['turns']}, 호출 {t['llm_calls']}, 1,000건당 ${cost / len(both) * 1000:.2f})")
        print(f"둘 다 확정한 {len(both)}건만: " + " / ".join(parts))
    price = report["prices"]
    print(f"\n가격 (USD/1M, {price['checked']} 확인): " + "; ".join(f"{m} 입력 {p['input']} 캐시 {p['cached']} 출력 {p['output']}" for m, p in price["models"].items()))


def _effort_label(efforts: list[str | None], fallback: str) -> str:
    sent = sorted({e for e in efforts if e is not None})
    if not efforts:
        return fallback
    return ", ".join(sent) if sent else "미지정 (요청에 넣지 않음 → API 기본값)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="쉼표로 구분한 케이스 id만")
    parser.add_argument("--summarize", help="저장된 결과 JSON(glob)을 다시 표로 (API 호출 없음)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if args.summarize:
        for name in sorted(glob.glob(args.summarize)):
            print_report(json.loads(Path(name).read_text(encoding="utf-8")))
        return 0

    data = load_cases()
    by_id = {case["id"]: case for case in data["cases"]}
    specs = [s for s in COMPARE_CASES if not args.only or s["id"] in args.only.split(",")]
    runs: dict[NlPath, list[PathRun]] = {"old": [], "new": []}
    cases = []
    for spec in specs:
        case = by_id[spec["id"]]
        result: dict[str, Any] = {"id": spec["id"], "say": case["turns"][0]["say"], "now": case["now"]}
        for path in ("old", "new"):
            run = run_path(path, case, spec, data["seed"])
            runs[path].append(run)
            result[path] = {**asdict(run), "totals": {
                attr: run.total(attr)
                for attr in ("llm_calls", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "llm_latency_ms")
            } | {"turns": len(run.turns)}, "wall_ms": run.wall_ms}
            print(f"{spec['id']:38} {path}: {run.status}, correct={run.correct}", flush=True)
        cases.append(result)

    old_model, new_model = settings.llm_model, settings.assistant_model
    report = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "criterion": "일정 하나를 확정할 때까지 (확정은 두 방식 모두 버튼 경로, LLM 호출 없음)",
        "paths": {
            "old": {
                "endpoint": "/events/parse",
                "api": "chat.completions",
                "model": old_model,
                "response_models": sorted({m for r in runs["old"] for m in r.models}),
                "effort": _effort_label([e for r in runs["old"] for e in r.efforts], "-"),
                "summary": path_summary(runs["old"], old_model),
            },
            "new": {
                "endpoint": "/assistant/chat",
                "api": "responses",
                "model": new_model,
                "response_models": sorted({m for r in runs["new"] for m in r.models if m}),
                "effort": resolve_reasoning_effort(new_model, settings.assistant_reasoning_effort),
                "summary": path_summary(runs["new"], new_model),
            },
        },
        "prices": {"checked": PRICES_CHECKED, "models": {m: PRICES[m] for m in {old_model, new_model} if m in PRICES}},
        "cases": cases,
    }
    print_report(report)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"compare_nl_paths_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n저장: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
