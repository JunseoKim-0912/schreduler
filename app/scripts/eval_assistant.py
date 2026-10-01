"""일정 어시스턴트 평가 (docs/assistant_design.md §8, §11.7). 실제 API를 부르므로 pytest에는 넣지 않는다.

    python -m app.scripts.eval_assistant --model gpt-5.6-luna --effort low
    python -m app.scripts.eval_assistant --model gpt-5-nano --effort low --only study_11_to_1,approve_creates
    python -m app.scripts.eval_assistant --summarize tests/assistant_eval/results/*.json

케이스마다 메모리 SQLite에 시드를 만들고(실제 schreduler.db는 쓰지 않는다) 케이스의 now로 시각을 고정해 턴을 돌린다.
결과는 표로 출력하고 tests/assistant_eval/results/에 JSON으로 저장한다.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
import time as time_module
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.clock import local_wall_now
from app.core.config import settings
from app.models import (
    AssistantMessage,
    AssistantTurnLog,
    Base,
    Event,
    EventInstance,
    ImportantDateRange,
    Location,
    PendingProposal,
    User,
)
from app.models.enums import EventType
from app.schemas.event import EventCreate
from app.services import event_service
from app.services.assistant import agent
from app.services.llm_client import LLMClientError, ResponsesClient, ResponsesResult, resolve_reasoning_effort
from app.services.llm_pricing import PRICES_CHECKED, call_cost, price_for

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "tests" / "assistant_eval" / "cases.yaml"
RESULTS_DIR = ROOT / "tests" / "assistant_eval" / "results"
MANDATORY_TAGS = ("격주", "오전오후", "마감", "확인안전")
PASS_THRESHOLD = 0.90

WATCHED_TABLES = (Event, EventInstance, ImportantDateRange, Location)


class ContinuationError(RuntimeError):
    """두 번째 턴 이후에 API 오류가 났다 — 턴 사이 이어가기 문제일 수 있어 평가를 멈춘다."""


# --- 시드 ---------------------------------------------------------------------------------


def _local(value: str) -> datetime:
    return datetime.fromisoformat(value)


def seed_database(db: Session, seed: dict[str, Any], language: str) -> User:
    user = User(name=seed["user"]["name"], preferred_language=language)
    db.add(user)
    db.flush()
    ranges: dict[str, ImportantDateRange] = {}
    for spec in seed.get("ranges", []):
        date_range = ImportantDateRange(
            user_id=user.id, name=spec["name"], start_date=_local(spec["start"]).date(), end_date=_local(spec["end"]).date()
        )
        db.add(date_range)
        ranges[spec["name"]] = date_range
    for spec in seed.get("locations", []):
        db.add(Location(user_id=user.id, name=spec["name"], default_travel_minutes=spec["travel_minutes"]))
    db.commit()
    for spec in seed.get("events", []):
        if not spec.get("days"):
            # 단발 일정·할 일: start(없으면 마감)~end
            deadline = spec.get("type") == "deadline"
            event_service.create_event(
                db,
                EventCreate(
                    user_id=user.id,
                    title=spec["title"],
                    event_type=EventType.DEADLINE if deadline else EventType.SCHEDULED,
                    start_time=None if deadline else _local(spec["start"]),
                    end_time=_local(spec["end"]),
                    importance=spec.get("importance"),
                ),
            )
            continue
        if spec.get("range") not in ranges:
            continue
        event_service.create_event(
            db,
            EventCreate(
                user_id=user.id,
                title=spec["title"],
                start_time=_local(spec["start"]),
                end_time=_local(spec["end"]),
                importance=spec.get("importance"),
                is_recurring=True,
                recurrence_rule=f"FREQ=WEEKLY;BYDAY={','.join(spec['days'])}",
                date_range_id=ranges[spec["range"]].id,
            ),
        )
    return user


def db_snapshot(db: Session) -> dict[str, list[tuple[Any, ...]]]:
    snapshot = {}
    for model in WATCHED_TABLES:
        columns = [c.key for c in model.__table__.columns]
        rows = db.execute(select(model).order_by(model.id)).scalars()
        snapshot[model.__tablename__] = [tuple(str(getattr(row, c)) for c in columns) for row in rows]
    return snapshot


# --- 기대 조건 ----------------------------------------------------------------------------


def _hhmm(value: str | None) -> str | None:
    return datetime.fromisoformat(value).strftime("%H:%M") if value else None


def _card_value(card: dict[str, Any], name: str) -> Any:
    if name == "date_range_name":
        return (card.get("date_range") or {}).get("name")
    return card.get(name)


def _card_location(card: dict[str, Any]) -> dict[str, Any] | None:
    if card.get("kind") == "update_event":
        return (card.get("changes") or {}).get("location")
    return card.get("location")


def card_problems(card: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """카드가 조건을 어긴 이유 목록 (비어 있으면 만족)."""
    problems: list[str] = []

    def check(ok: bool, message: str) -> None:
        if not ok:
            problems.append(message)

    start, end = _hhmm(card.get("start_time")), _hhmm(card.get("end_time"))
    warnings = {w["code"] for w in card.get("warnings", [])}
    targets = card.get("targets") or []
    location = _card_location(card) or {}
    date_range = card.get("date_range") or {}
    for key, want in expected.items():
        if key == "kind":
            check(card.get("kind") == want, f"kind={card.get('kind')}≠{want}")
        elif key == "title_contains":
            check(want.casefold() in (card.get("title") or "").casefold(), f"title={card.get('title')!r} lacks {want!r}")
        elif key == "event_type":
            check(card.get("event_type") == want, f"event_type={card.get('event_type')}≠{want}")
        elif key == "date":
            check(card.get("date") == want, f"date={card.get('date')}≠{want}")
        elif key == "start":
            check(start == want, f"start={start}≠{want}")
        elif key == "end":
            check(end == want, f"end={end}≠{want}")
        elif key == "start_in":
            check(start in want, f"start={start}∉{want}")
        elif key == "duration_minutes":
            if card.get("start_time") and card.get("end_time"):
                minutes = (datetime.fromisoformat(card["end_time"]) - datetime.fromisoformat(card["start_time"])).total_seconds() / 60
                check(minutes == want, f"duration={minutes:g}m≠{want}m")
            else:
                problems.append("duration: no start/end")
        elif key == "importance":
            check(card.get("importance") == want, f"importance={card.get('importance')}≠{want}")
        elif key == "importance_in":
            check(card.get("importance") in want, f"importance={card.get('importance')}∉{want}")
        elif key == "recurring":
            check(bool(card.get("recurring")) == want, f"recurring={card.get('recurring')}≠{want}")
        elif key == "rrule_contains":
            rule = card.get("recurrence_rule") or ""
            missing = [part for part in want if part not in rule]
            check(not missing, f"rrule={rule!r} lacks {missing}")
        elif key == "rrule_not_contains":
            rule = card.get("recurrence_rule") or ""
            check(not any(part in rule for part in want), f"rrule={rule!r} has one of {want}")
        elif key == "recurrence_start":
            check(card.get("recurrence_start") == want, f"recurrence_start={card.get('recurrence_start')}≠{want}")
        elif key == "preview_dates":
            check(card.get("preview_dates") == want, f"preview={card.get('preview_dates')}≠{want}")
        elif key == "date_range":
            check(want.casefold() in (date_range.get("name") or "").casefold(), f"date_range={date_range.get('name')!r}≠{want!r}")
        elif key == "date_range_start":
            check(date_range.get("start_date") == want, f"date_range.start={date_range.get('start_date')}≠{want}")
        elif key == "date_range_end":
            check(date_range.get("end_date") == want, f"date_range.end={date_range.get('end_date')}≠{want}")
        elif key == "date_range_new":
            check(date_range.get("is_new") == want, f"date_range.is_new={date_range.get('is_new')}≠{want}")
        elif key == "inferred_includes":
            inferred = card.get("inferred_fields") or []
            check(set(want) <= set(inferred), f"inferred_fields={inferred} lack {want}")
        elif key == "location":
            check((location.get("name") or "").casefold() == want.casefold(), f"location={location.get('name')!r}≠{want!r}")
        elif key == "location_new":
            check(location.get("is_new") == want, f"location.is_new={location.get('is_new')}≠{want}")
        elif key == "warnings_include":
            check(set(want) <= warnings, f"warnings={sorted(warnings)} lack {want}")
        elif key == "warnings_exclude":
            check(not (set(want) & warnings), f"warnings={sorted(warnings)} include {sorted(set(want) & warnings)}")
        elif key == "scope":
            check(card.get("scope") == want, f"scope={card.get('scope')}≠{want}")
        elif key == "target_titles":
            titles = [t["title"] for t in targets]
            check(bool(titles) and all(any(w.casefold() in t.casefold() for w in want) for t in titles), f"targets={titles}≠{want}")
        elif key == "target_count":
            check(len(targets) == want, f"target_count={len(targets)}≠{want}")
        elif key == "target_dates":
            dates = sorted(t.get("date") for t in targets)
            check(dates == sorted(want), f"target_dates={dates}≠{want}")
        elif key == "changes":
            changes = card.get("changes") or {}
            wrong = {k: changes.get(k) for k, v in want.items() if changes.get(k) != v}
            check(not wrong, f"changes {wrong}≠{want}")
        elif key == "range_start":
            check(card.get("start_date") == want, f"range start={card.get('start_date')}≠{want}")
        elif key == "range_end":
            check(card.get("end_date") == want, f"range end={card.get('end_date')}≠{want}")
        else:
            problems.append(f"unknown card condition {key!r}")
    return problems


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


@dataclass
class TurnRecord:
    say: str
    reply: str = ""
    proposal: dict[str, Any] | None = None
    executed: list[dict[str, Any]] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    stop_reason: str | None = None
    error: str | None = None
    failures: list[str] = field(default_factory=list)


def turn_problems(
    db: Session,
    expect: dict[str, Any],
    record: TurnRecord,
    previous: TurnRecord | None,
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    cards = (record.proposal or {}).get("items", [])
    for key, want in expect.items():
        if key == "any_of":
            # 여러 기대 중 하나만 맞으면 통과. 모두 틀리면 가장 가까운 것(문제가 가장 적은 것)의 이유를 보인다.
            reasons = [turn_problems(db, option, record, previous, before, after) for option in want]
            if all(reasons):
                problems.extend(f"any_of: {reason}" for reason in min(reasons, key=len))
        elif key == "proposal":
            if want is None:
                if record.proposal is not None:
                    problems.append(f"expected no proposal, got {[c.get('kind') for c in cards]}")
                continue
            if record.proposal is None:
                problems.append("expected a proposal, got none")
                continue
            if "count" in want and len(cards) != want["count"]:
                problems.append(f"proposal count={len(cards)}≠{want['count']}")
            unwanted = [card.get("kind") for card in cards if card.get("kind") in want.get("kinds_exclude", [])]
            if unwanted:
                problems.append(f"proposal has unwanted kinds {unwanted}")
            remaining = list(cards)
            for expected_card in want.get("items", []):
                reasons = [card_problems(card, expected_card) for card in remaining]
                match = next((i for i, r in enumerate(reasons) if not r), None)
                if match is None:
                    closest = min(reasons, key=len) if reasons else ["no cards"]
                    problems.append(f"no card matches {expected_card.get('kind')}: {'; '.join(closest)}")
                else:
                    remaining.pop(match)
        elif key == "db_unchanged":
            if want and before != after:
                changed = [table for table in before if before[table] != after[table]]
                problems.append(f"DB changed before confirmation: {changed}")
        elif key == "executed_min":
            if len(record.executed) < want:
                problems.append(f"executed={len(record.executed)}<{want}")
        elif key == "executed_count":
            if len(record.executed) != want:
                problems.append(f"executed={len(record.executed)}≠{want}")
        elif key == "tools_called":
            missing = [name for name in want if name not in record.tools]
            if missing:
                problems.append(f"tools not called: {missing} (called {record.tools})")
        elif key == "tools_not_called":
            extra = [name for name in want if name in record.tools]
            if extra:
                problems.append(f"tools called unexpectedly: {extra}")
        elif key == "previous_status":
            token = ((previous.proposal if previous else None) or {}).get("token")
            status = db.execute(select(PendingProposal.status).where(PendingProposal.token == token)).scalar_one_or_none() if token else None
            if status != want:
                problems.append(f"previous proposal status={status}≠{want}")
        elif key == "previous_items":
            # 직전 턴 제안에 이 조건의 카드가 있어야 함 (이미 맞게 제안돼 있어 이번 턴에 바꿀 게 없는 경우를 확인)
            prev_cards = ((previous.proposal if previous else None) or {}).get("items", [])
            for expected_card in want:
                if not any(not card_problems(card, expected_card) for card in prev_cards):
                    problems.append(f"previous proposal has no card matching {expected_card}")
        elif key == "same_as_previous":
            prev_cards = ((previous.proposal if previous else None) or {}).get("items", [])
            if not prev_cards or not cards:
                problems.append("same_as_previous: missing cards")
                continue
            diffs = {f: (_card_value(prev_cards[0], f), _card_value(cards[0], f)) for f in want}
            diffs = {f: v for f, v in diffs.items() if v[0] != v[1]}
            if diffs:
                problems.append(f"changed besides the asked field: {diffs}")
        elif key == "reply_contains":
            missing = [part for part in want if part.casefold() not in record.reply.casefold()]
            if missing:
                problems.append(f"reply lacks {missing}: {record.reply[:80]!r}")
        elif key == "reply_language":
            if (want == "en") == _has_hangul(record.reply):
                problems.append(f"reply language is not {want}: {record.reply[:60]!r}")
        elif key == "db_events":
            for spec in want:
                events = [
                    e for e in db.execute(select(Event).where(Event.parent_event_id.is_(None))).scalars()
                    if spec["title_contains"].casefold() in e.title.casefold()
                ]
                if "location" in spec:
                    events = [e for e in events if e.location and e.location.name.casefold() == spec["location"].casefold()]
                if len(events) != spec.get("count", 1):
                    problems.append(f"DB events matching {spec}: {len(events)}")
        else:
            problems.append(f"unknown turn condition {key!r}")
    return problems


# --- 실행 ------------------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: str
    tags: list[str]
    passed: bool
    reasons: list[str]
    turns: list[TurnRecord]


class RecordingClient:
    """ResponsesClient를 감싸 호출 중 난 API 오류 메시지를 남긴다 (에이전트가 중간 오류를 대체 답으로 바꾸기 때문)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.errors: list[str] = []

    def create(self, **kwargs: Any) -> ResponsesResult:
        try:
            return self.inner.create(**kwargs)
        except LLMClientError as exc:
            self.errors.append(str(exc)[:500])
            raise


def turn_cost(model: str, record: TurnRecord) -> float:
    return call_cost(model, record.input_tokens, record.cached_tokens, record.output_tokens)


def run_case(case: dict[str, Any], default_seed: dict[str, Any], make_client: Callable[[], Any], model: str) -> CaseResult:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    seed = {**default_seed, **(case.get("seed") or {})}
    zone = ZoneInfo(settings.app_timezone)
    start = datetime.fromisoformat(case["now"]).replace(tzinfo=zone)
    turns: list[TurnRecord] = []
    reasons: list[str] = []
    with Session(engine) as db:
        user = seed_database(db, seed, case.get("language", "ko"))
        session_id: int | None = None
        for index, turn in enumerate(case["turns"]):
            record = TurnRecord(say=turn["say"])
            client = RecordingClient(make_client())
            before = db_snapshot(db)
            last_message_id = db.scalar(select(AssistantMessage.id).order_by(AssistantMessage.id.desc()).limit(1)) or 0
            try:
                result = agent.chat(db, user, turn["say"], session_id, client=client, now=start + timedelta(minutes=index))
            except LLMClientError as exc:
                record.error = f"{type(exc).__name__}: {exc}"[:500]
                turns.append(record)
                reasons.append(f"turn {index + 1}: {record.error}")
                if index > 0:
                    raise ContinuationError(f"{case['id']} turn {index + 1}: {record.error}") from exc
                break
            session_id = result.session_id
            record.reply, record.proposal, record.executed = result.reply, result.proposal, result.executed
            if record.proposal is not None:
                record.proposal = json.loads(json.dumps(record.proposal, default=str))
            for message in db.execute(
                select(AssistantMessage).where(AssistantMessage.session_id == session_id, AssistantMessage.id > last_message_id, AssistantMessage.role == "tool")
            ).scalars():
                record.tools.append(message.content["name"])
                output = json.loads(message.content["output"])
                record.tool_errors.extend(f"{message.content['name']}:{e['code']}" for e in output.get("errors", []))
            log = db.execute(
                select(AssistantTurnLog).where(AssistantTurnLog.session_id == session_id).order_by(AssistantTurnLog.id.desc()).limit(1)
            ).scalar_one()
            record.llm_calls, record.latency_ms, record.stop_reason = log.llm_calls, log.latency_ms, log.stop_reason
            record.input_tokens, record.cached_tokens = log.input_tokens, log.cached_tokens
            record.output_tokens, record.reasoning_tokens = log.output_tokens, log.reasoning_tokens
            record.cost_usd = turn_cost(model, record)
            if client.errors:
                record.error = client.errors[-1]
                if index > 0:
                    raise ContinuationError(f"{case['id']} turn {index + 1}: {record.error}")
            after = db_snapshot(db)
            record.failures = turn_problems(db, turn.get("expect") or {}, record, turns[-1] if turns else None, before, after)
            if record.stop_reason:
                record.failures.append(f"stopped: {record.stop_reason}")
            reasons.extend(f"turn {index + 1}: {failure}" for failure in record.failures)
            turns.append(record)
    engine.dispose()
    return CaseResult(id=case["id"], tags=case["tags"], passed=not reasons, reasons=reasons, turns=turns)


def load_cases(path: Path = CASES_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    by_tag: dict[str, list[bool]] = defaultdict(list)
    for result in results:
        for tag in result.tags:
            by_tag[tag].append(result.passed)
    turns = [t for r in results for t in r.turns if t.error is None]
    count = max(len(turns), 1)

    def mean(attr: str) -> float:
        return sum(getattr(t, attr) for t in turns) / count

    categories = {tag: {"passed": sum(v), "total": len(v), "rate": sum(v) / len(v)} for tag, v in sorted(by_tag.items())}
    pass_rate = sum(r.passed for r in results) / max(len(results), 1)
    mandatory_ok = all(categories.get(tag, {"rate": 1.0})["rate"] == 1.0 for tag in MANDATORY_TAGS)
    return {
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "pass_rate": pass_rate,
        "categories": categories,
        "meets_criteria": pass_rate >= PASS_THRESHOLD and mandatory_ok,
        "turns": len(turns),
        "avg_llm_calls": mean("llm_calls"),
        "avg_input_tokens": mean("input_tokens"),
        "avg_cached_tokens": mean("cached_tokens"),
        "avg_output_tokens": mean("output_tokens"),
        "avg_reasoning_tokens": mean("reasoning_tokens"),
        "avg_latency_ms": mean("latency_ms"),
        "avg_cost_usd": mean("cost_usd"),
    }


def run(model: str, effort: str, only: set[str] | None, workers: int) -> dict[str, Any]:
    data = load_cases()
    cases = [c for c in data["cases"] if not only or c["id"] in only]
    sent_effort = resolve_reasoning_effort(model, effort)
    price = price_for(model)

    def make_client() -> ResponsesClient:
        return ResponsesClient(model=model, reasoning_effort=effort)

    started = time_module.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda case: run_case(case, data["seed"], make_client, model), cases))
    return {
        "model": model,
        "effort_requested": effort,
        "effort_sent": sent_effort,
        "run_at": local_wall_now().isoformat(timespec="seconds"),
        "wall_seconds": round(time_module.perf_counter() - started, 1),
        "prices": {**asdict(price), "checked": PRICES_CHECKED},
        "summary": summarize(results),
        "results": [{**asdict(r)} for r in results],
    }


# --- 출력 ------------------------------------------------------------------------------------


def print_report(report: dict[str, Any]) -> None:
    s = report["summary"]
    print(f"\n== {report['model']} effort={report['effort_sent']} (요청 {report['effort_requested']}) {report['run_at']}")
    print(f"{'case':42} {'tags':18} result")
    for result in report["results"]:
        mark = "PASS" if result["passed"] else "FAIL"
        print(f"{result['id']:42} {','.join(result['tags']):18} {mark}")
        for reason in result["reasons"]:
            print(f"{'':44}- {reason}")
    print("\n범주별 통과율")
    for tag, c in s["categories"].items():
        star = "*" if tag in MANDATORY_TAGS else " "
        print(f"  {star}{tag:8} {c['passed']}/{c['total']} ({c['rate']:.0%})")
    print(
        f"\n전체 {s['passed']}/{s['cases']} ({s['pass_rate']:.0%})  기준 충족: {'예' if s['meets_criteria'] else '아니오'}\n"
        f"턴당 평균: LLM 호출 {s['avg_llm_calls']:.2f}, 입력 {s['avg_input_tokens']:.0f} (캐시 {s['avg_cached_tokens']:.0f}), "
        f"출력 {s['avg_output_tokens']:.0f} (추론 {s['avg_reasoning_tokens']:.0f}), 지연 {s['avg_latency_ms'] / 1000:.2f}초, "
        f"비용 ${s['avg_cost_usd'] * 1000:.3f}/1,000턴당 → ${s['avg_cost_usd']:.6f}/턴"
    )
    price = report.get("prices") or {}
    if price.get("source"):
        print(f"가격: 입력 ${price['input']}/1M, 캐시 ${price['cached']}/1M, 출력 ${price['output']}/1M — {price['source']} ({price['checked']} 확인)")


def print_comparison(reports: list[dict[str, Any]]) -> None:
    tags = sorted({tag for r in reports for tag in r["summary"]["categories"]})
    header = f"{'model':14} {'effort':8} {'pass':>6} " + " ".join(f"{t:>6}" for t in tags) + f" {'calls':>5} {'in':>6} {'cache':>6} {'out':>5} {'rsn':>5} {'sec':>5} {'$/1k턴':>7} ok"
    print(header)
    rows = []
    for r in reports:
        s = r["summary"]
        cats = " ".join(f"{s['categories'].get(t, {'rate': float('nan')})['rate']:>6.0%}" for t in tags)
        rows.append((s["meets_criteria"], s["avg_cost_usd"], s["avg_latency_ms"], r))
        print(
            f"{r['model']:14} {r['effort_sent']:8} {s['pass_rate']:>6.0%} {cats} {s['avg_llm_calls']:>5.2f} {s['avg_input_tokens']:>6.0f} "
            f"{s['avg_cached_tokens']:>6.0f} {s['avg_output_tokens']:>5.0f} {s['avg_reasoning_tokens']:>5.0f} {s['avg_latency_ms'] / 1000:>5.2f} "
            f"{s['avg_cost_usd'] * 1000:>7.3f} {'Y' if s['meets_criteria'] else '-'}"
        )
    passing = sorted((row for row in rows if row[0]), key=lambda row: (round(row[1], 6), row[2]))
    if passing:
        best = passing[0][3]
        print(f"\n추천(기준 충족 중 턴당 비용 최저): {best['model']} effort={best['effort_sent']}")
    else:
        print("\n기준을 충족한 조합이 없습니다.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=settings.assistant_model)
    parser.add_argument("--effort", default=settings.assistant_reasoning_effort)
    parser.add_argument("--only", help="쉼표로 구분한 케이스 id만")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--summarize", nargs="*", help="저장된 결과 JSON들을 비교표로")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    if args.summarize is not None:
        paths = [p for pattern in args.summarize for p in glob.glob(pattern)]
        print_comparison([json.loads(Path(p).read_text(encoding="utf-8")) for p in sorted(paths)])
        return 0

    try:
        report = run(args.model, args.effort, set(args.only.split(",")) if args.only else None, args.workers)
    except ContinuationError as exc:
        print(f"\n평가 중단 — 턴 사이 이어가기에서 API 오류: {exc}", file=sys.stderr)
        return 2
    print_report(report)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = local_wall_now().strftime("%Y%m%d-%H%M%S")
    path = RESULTS_DIR / f"{report['model']}_{report['effort_sent']}_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n저장: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
