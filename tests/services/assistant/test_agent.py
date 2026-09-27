"""일정 어시스턴트 시나리오 (docs/assistant_design.md §9 B단계). LLM은 tests/fake_responses의 대본으로 대신한다.

기준 시각은 2026-09-27(일) 14:00 America/Toronto. 'Lecture Period'는 목요일(9/3)에 시작한다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.exceptions import ConflictError, ExpiredError
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
from app.schemas.event import EventCreate
from app.services import event_service
from app.services.assistant import agent, drafts
from app.services.assistant import execution as execution_module
from tests.fake_responses import FakeResponsesClient, ScriptedCall, call, calls, say

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 9, 27, 14, 0, tzinfo=TZ)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def user(db: Session) -> User:
    user = User(name="June", preferred_language="ko")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def lecture_period(db: Session, user: User) -> ImportantDateRange:
    date_range = ImportantDateRange(user_id=user.id, name="Lecture Period", start_date=date(2026, 9, 3), end_date=date(2026, 12, 8))
    db.add(date_range)
    db.commit()
    return date_range


def _weekly(db: Session, user: User, date_range: ImportantDateRange, title: str, days: str, start: datetime, hours: int = 1) -> Event:
    return event_service.create_event(
        db,
        EventCreate(
            user_id=user.id,
            title=title,
            start_time=start,
            end_time=start + timedelta(hours=hours),
            importance=3,
            is_recurring=True,
            recurrence_rule=f"FREQ=WEEKLY;BYDAY={days}",
            date_range_id=date_range.id,
        ),
    )


@pytest.fixture
def lecture(db: Session, user: User, lecture_period: ImportantDateRange) -> Event:
    return _weekly(db, user, lecture_period, "ECE360 Lecture", "MO,WE", datetime(2026, 9, 7, 10))


@pytest.fixture
def lab(db: Session, user: User, lecture_period: ImportantDateRange) -> Event:
    return _weekly(db, user, lecture_period, "ECE360 Lab", "TH", datetime(2026, 9, 3, 14), hours=3)


def _counts(db: Session) -> tuple[int, ...]:
    return tuple(db.scalar(select(func.count()).select_from(model)) for model in (Event, EventInstance, ImportantDateRange, Location))


def _turn(db: Session, user: User, script: list, message: str, session_id: int | None = None, now: datetime = NOW, **kwargs):
    fake = FakeResponsesClient(script)
    before = _counts(db)
    result = agent.chat(db, user, message, session_id, client=fake, now=now, **kwargs)
    if not result.executed:
        assert _counts(db) == before, "nothing may be written before the user confirms"
    assert fake.finished or kwargs.get("timer"), "the agent must consume the whole script"
    return result, fake


def _outputs(fake: FakeResponsesClient) -> list[dict]:
    """모든 도구 결과를 호출 순서대로."""
    found: dict[str, dict] = {}
    for request in fake.requests:
        found.update(request.tool_outputs())
    return [found[key] for key in sorted(found, key=lambda k: tuple(int(p) for p in k.split("_")[1:]))]


def _create(**overrides) -> dict:
    args = {
        "event_type": "scheduled",
        "title": "스터디",
        "date": "2026-10-02",
        "start_time": "11:00",
        "end_time": "13:00",
        "importance": 1,
        "recurrence": None,
        "date_range": None,
        "location": None,
        "inferred_fields": ["importance"],
        "draft_id": None,
    }
    return {**args, **overrides}


def _new_range() -> ScriptedCall:
    # call()의 첫 인자 이름이 name이라 name 인자가 있는 도구는 직접 만든다.
    return ScriptedCall(
        "propose_date_range",
        {"action": "create", "name": "Winter Term", "new_name": None, "start_date": "2027-01-05", "end_date": "2027-04-10",
         "mode": None, "inferred_fields": [], "draft_id": None},
    )


# --- 생성 초안 ------------------------------------------------------------------------------


def test_biweekly_lab_with_start_date(db: Session, user: User, lecture_period: ImportantDateRange) -> None:
    args = _create(
        title="ECE360 Lab",
        date=None,
        start_time="09:00",
        end_time="12:00",
        importance=3,
        recurrence={"frequency": "WEEKLY", "interval": 2, "by_day": ["TU"], "start_date": "2026-09-22"},
        date_range={"name": "Lecture Period", "start_date": None, "end_date": None},
    )
    result, _ = _turn(db, user, [call("propose_create_event", **args), say("격주 화요일 Lab 초안이에요.")], "ECE360 Lab 격주 화요일 9-12시 9/22부터 Lecture period 동안")

    item = result.proposal["items"][0]
    assert "INTERVAL=2" in item["recurrence_rule"]
    assert item["recurrence_start"] == "2026-09-22", "격주 리듬의 기준은 말한 시작일"
    assert item["date"] == "2026-10-06", "회차는 오늘(9/27) 이후만"
    assert item["preview_dates"] == ["2026-10-06", "2026-10-20", "2026-11-03"]
    assert item["warnings"] == [], "반복 일정은 리듬 시작일이 지나도 past_date 경고가 없다"
    assert item["date_range"]["id"] == lecture_period.id
    assert item["time_display"] == "오전 9:00 – 오후 12:00 (3시간)"


def test_biweekly_without_start_date_keeps_first_tuesday_of_a_thursday_period(db: Session, user: User, lecture_period: ImportantDateRange) -> None:
    args = _create(
        title="ECE360 Lab",
        date=None,
        start_time="09:00",
        end_time="12:00",
        recurrence={"frequency": "WEEKLY", "interval": 2, "by_day": ["TU"], "start_date": None},
        date_range={"name": "Lecture Period", "start_date": None, "end_date": None},
    )
    result, _ = _turn(db, user, [call("propose_create_event", **args), say("초안이에요.")], "ECE360 Lab 격주 화요일 9-12시")

    item = result.proposal["items"][0]
    assert item["recurrence_start"] == "2026-09-08", "기간 시작(목 9/3) 이후 첫 화요일이 리듬 기준"
    assert item["preview_dates"] == ["2026-10-06", "2026-10-20", "2026-11-03"]


def test_eleven_to_one_is_a_two_hour_daytime_draft(db: Session, user: User) -> None:
    result, _ = _turn(db, user, [call("propose_create_event", **_create()), say("스터디 초안이에요.")], "10월 2일 11:00-1:00 스터디 추가해줘")

    item = result.proposal["items"][0]
    assert (item["start_time"], item["end_time"]) == ("2026-10-02T11:00:00", "2026-10-02T13:00:00")
    assert item["time_display"] == "오전 11:00 – 오후 1:00 (2시간)"
    assert item["warnings"] == [] and result.proposal["warnings"] == []


def test_llm_proposing_eleven_to_one_am_gets_crosses_midnight_warning(db: Session, user: User) -> None:
    result, _ = _turn(db, user, [call("propose_create_event", **_create(end_time="01:00")), say("초안")], "10월 2일 11:00-1:00 스터디")

    item = result.proposal["items"][0]
    assert item["end_time"] == "2026-10-03T01:00:00"
    assert [w["code"] for w in item["warnings"]] == ["crosses_midnight", "over_12_hours"]
    assert "(다음 날)" in item["time_display"]


@pytest.mark.parametrize("times", [{"start_time": None, "end_time": "23:30"}, {"start_time": "23:30", "end_time": None}])
def test_assignment_submission_is_a_2330_deadline(db: Session, user: User, times: dict) -> None:
    args = _create(event_type="deadline", title="MAT389 과제 제출", date="2026-09-29", importance=4, **times)
    result, _ = _turn(db, user, [call("propose_create_event", **args), say("마감 초안")], "9/29 11:30 pm MAT389 과제 제출날이야")

    item = result.proposal["items"][0]
    assert item["event_type"] == "deadline"
    assert item["start_time"] is None
    assert item["end_time"] == "2026-09-29T23:30:00"
    assert item["time_display"] == "오후 11:30 마감"


def test_deadline_without_time_defaults_to_2359_and_is_marked_inferred(db: Session, user: User) -> None:
    args = _create(event_type="deadline", title="보고서 마감", start_time=None, end_time=None, inferred_fields=[])
    result, _ = _turn(db, user, [call("propose_create_event", **args), say("초안")], "10/2까지 보고서")

    item = result.proposal["items"][0]
    assert item["end_time"] == "2026-10-02T23:59:00"
    assert "end_time" in item["inferred_fields"]


def test_next_tuesday_on_a_sunday_comes_from_the_calendar_table(db: Session, user: User) -> None:
    result, fake = _turn(db, user, [call("propose_create_event", **_create(date="2026-09-29", end_time=None)), say("초안")], "다음 주 화요일 11시 스터디")

    context = "\n".join(str(item.get("content")) for item in fake.requests[0].input_items if item.get("role") == "developer")
    assert "오늘: 2026-09-27 (Sunday, SU)" in context
    assert "2026-09-29 화 Tue TU | 다음 주" in context
    item = result.proposal["items"][0]
    assert item["date"] == "2026-09-29"
    assert item["end_time"] == "2026-09-29T12:00:00"  # 종료 없음 → 1시간
    assert "end_time" in item["inferred_fields"]


# --- 기존 일정 --------------------------------------------------------------------------------


def test_location_for_monday_lecture_uses_search_then_new_location_with_estimated_travel(db: Session, user: User, lecture: Event) -> None:
    script = [
        call("search_events", query="ECE360 Lecture", date_from=None, date_to=None, weekday="MO"),
        call(
            "propose_update_event",
            target_ids=[{"event_id": lecture.id, "instance_id": None}],
            scope="series",
            changes={"title": None, "date": None, "start_time": None, "end_time": None, "importance": None,
                     "location": {"action": "set", "name": "Galbraith 304", "travel_minutes": None}},
            inferred_fields=[],
            draft_id=None,
        ),
        say("ECE360 Lecture에 Galbraith 304를 넣을게요. 이동 시간은 15분으로 추정했어요."),
    ]
    result, fake = _turn(db, user, script, "월요일의 ECE360 Lecture에 장소 넣어줘 Galbraith 304")

    search = _outputs(fake)[0]
    assert [r["event_id"] for r in search["results"]] == [lecture.id]
    item = result.proposal["items"][0]
    assert item["kind"] == "update_event" and item["scope"] == "series"
    assert item["changes"]["location"] == {"action": "set", "id": None, "name": "Galbraith 304", "travel_minutes": 15, "is_new": True}
    assert "changes.location.travel_minutes" in item["inferred_fields"]


def test_new_range_and_event_in_one_proposal_are_confirmed_in_one_transaction(db: Session, user: User) -> None:
    script = [
        calls(
            _new_range(),
            call("propose_create_event", **_create(title="MAT401 Lecture", date=None, start_time="10:00", end_time="11:00", importance=3,
                 recurrence={"frequency": "WEEKLY", "interval": 1, "by_day": ["MO"], "start_date": None},
                 date_range={"name": "Winter Term", "start_date": None, "end_date": None})),
        ),
        say("새 기간과 강의 초안이에요."),
    ]
    result, _ = _turn(db, user, script, "Winter Term 1/5~4/10 만들고 거기에 월요일 10시 MAT401 Lecture")
    kinds = [item["kind"] for item in result.proposal["items"]]
    assert kinds == ["create_range", "create_event"]
    event_card = result.proposal["items"][1]
    assert event_card["date_range"]["is_new"] and event_card["preview_dates"] == ["2027-01-11", "2027-01-18", "2027-01-25"]

    confirmed = agent.confirm(db, user, result.session_id, result.proposal["token"], now=NOW + timedelta(minutes=1))

    assert len(confirmed.executed) == 2 and all(item["action_id"] for item in confirmed.executed)
    winter = db.execute(select(ImportantDateRange).where(ImportantDateRange.name == "Winter Term")).scalars().all()
    assert len(winter) == 1
    event = db.execute(select(Event).where(Event.title == "MAT401 Lecture")).scalar_one()
    assert event.date_range_id == winter[0].id
    assert db.get(PendingProposal, 1).status == "confirmed"


def test_a_failing_item_rolls_back_the_whole_proposal(db: Session, user: User, monkeypatch: pytest.MonkeyPatch) -> None:
    script = [
        calls(
            _new_range(),
            call("propose_create_event", **_create()),
        ),
        say("초안"),
    ]
    result, _ = _turn(db, user, script, "기간이랑 스터디")
    before = _counts(db)

    def broken(*args, **kwargs):
        raise ConflictError("boom")

    monkeypatch.setattr(execution_module, "create_event_from_nl", broken)
    with pytest.raises(ConflictError):
        agent.confirm(db, user, result.session_id, result.proposal["token"], now=NOW + timedelta(minutes=1))

    assert _counts(db) == before
    assert db.execute(select(PendingProposal)).scalar_one().status == "pending"


def test_change_after_draft_supersedes_previous_proposal(db: Session, user: User) -> None:
    first, _ = _turn(db, user, [call("propose_create_event", **_create(start_time="18:00", end_time=None)), say("6시 초안")], "10/2 6시 스터디")
    second, fake = _turn(
        db, user,
        [call("propose_create_event", **_create(start_time="19:00", end_time=None)), say("7시로 바꿨어요")],
        "7시로 바꿔줘", first.session_id, NOW + timedelta(minutes=1),
    )

    pending_context = [i["content"] for i in fake.requests[0].input_items if i.get("role") == "developer" and "[대기 중인 제안" in i["content"]]
    assert first.proposal["token"] in pending_context[0]
    statuses = {p.token: p.status for p in db.execute(select(PendingProposal)).scalars()}
    assert statuses == {first.proposal["token"]: "superseded", second.proposal["token"]: "pending"}
    assert second.proposal["items"][0]["start_time"] == "2026-10-02T19:00:00"

    with pytest.raises(ConflictError):
        agent.confirm(db, user, first.session_id, first.proposal["token"], now=NOW + timedelta(minutes=2))


def test_chat_approval_runs_confirm_pending(db: Session, user: User) -> None:
    first, _ = _turn(db, user, [call("propose_create_event", **_create()), say("이렇게 만들까요?")], "10/2 11-1 스터디")
    token = first.proposal["token"]

    second, fake = _turn(
        db, user, [call("confirm_pending", token=token), say("스터디를 만들었어요.")], "좋아", first.session_id, NOW + timedelta(minutes=1)
    )

    assert second.executed and second.executed[0]["action_id"]
    assert "executed" in _outputs(fake)[0]
    assert db.execute(select(Event).where(Event.title == "스터디")).scalar_one().start_time == datetime(2026, 10, 2, 11)
    assert db.execute(select(PendingProposal)).scalar_one().status == "confirmed"
    assert second.proposal is None


def test_self_approval_in_the_same_turn_is_refused(db: Session, user: User) -> None:
    first, _ = _turn(db, user, [call("propose_create_event", **_create()), say("초안")], "10/2 스터디")
    script = [
        call("propose_create_event", **_create(title="다른 스터디")),
        call("confirm_pending", token=first.proposal["token"]),
        say("확인해 주세요."),
    ]
    second, fake = _turn(db, user, script, "다른 스터디도 만들어", first.session_id, NOW + timedelta(minutes=1))

    assert _outputs(fake)[1]["errors"][0]["code"] == "self_approval"
    assert second.executed == []
    assert db.execute(select(Event).where(Event.title.like("%스터디"))).first() is None


def test_delete_everything_is_a_multi_target_draft_and_changes_nothing_until_confirmed(
    db: Session, user: User, lecture: Event, lab: Event
) -> None:
    script = [
        call("search_events", query="ECE360", date_from=None, date_to=None, weekday=None),
        call(
            "propose_delete_event",
            target_ids=[{"event_id": lecture.id, "instance_id": None}, {"event_id": lab.id, "instance_id": None}],
            scope="series",
            inferred_fields=[],
            draft_id=None,
        ),
        say("ECE360 일정 2개를 모두 지울까요?"),
    ]
    result, _ = _turn(db, user, script, "ECE360 전부 없애줘")

    item = result.proposal["items"][0]
    assert item["kind"] == "delete_event" and len(item["targets"]) == 2
    assert [w["code"] for w in result.proposal["warnings"]] == ["multiple_targets"]

    agent.confirm(db, user, result.session_id, result.proposal["token"], now=NOW + timedelta(minutes=1))
    assert db.execute(select(Event).where(Event.parent_event_id.is_(None))).first() is None


def test_invented_ids_are_refused(db: Session, user: User, lecture: Event) -> None:
    script = [
        call("propose_delete_event", target_ids=[{"event_id": 999, "instance_id": None}], scope="series", inferred_fields=[], draft_id=None),
        call("propose_delete_event", target_ids=[{"event_id": lecture.id, "instance_id": None}], scope="series", inferred_fields=[], draft_id=None),
        say("어떤 일정인지 찾지 못했어요."),
    ]
    result, fake = _turn(db, user, script, "그거 지워줘")

    outputs = _outputs(fake)
    assert outputs[0]["errors"][0]["code"] == "unknown_id"
    assert outputs[1]["errors"][0]["code"] == "unknown_id"  # 실제로 있어도 search_events로 찾지 않은 ID
    assert result.proposal is None


def test_expired_token_is_refused_by_button_and_by_chat(db: Session, user: User) -> None:
    first, _ = _turn(db, user, [call("propose_create_event", **_create()), say("초안")], "10/2 스터디")
    later = NOW + timedelta(minutes=31)

    with pytest.raises(ExpiredError):
        agent.confirm(db, user, first.session_id, first.proposal["token"], now=later)
    second, fake = _turn(db, user, [call("confirm_pending", token=first.proposal["token"]), say("만료됐어요.")], "좋아", first.session_id, later)

    assert _outputs(fake)[0]["errors"][0]["code"] == "proposal_expired"
    assert second.executed == []


def test_rrule_errors_come_back_as_tool_errors(db: Session, user: User, lecture_period: ImportantDateRange, monkeypatch: pytest.MonkeyPatch) -> None:
    bad_day = _create(
        date=None,
        recurrence={"frequency": "WEEKLY", "interval": 1, "by_day": ["XX"], "start_date": None},
        date_range={"name": "Lecture Period", "start_date": None, "end_date": None},
    )
    result, fake = _turn(db, user, [call("propose_create_event", **bad_day), say("요일을 알려 주세요.")], "매주 XX요일 스터디")
    assert _outputs(fake)[0]["errors"][0]["code"] == "invalid_recurrence"
    assert result.proposal is None

    def explode(*args, **kwargs):
        raise ValueError("rrule exploded")

    monkeypatch.setattr(drafts.rules, "build_rrule", explode)
    good_day = {**bad_day, "recurrence": {**bad_day["recurrence"], "by_day": ["TU"]}}
    result, fake = _turn(db, user, [call("propose_create_event", **good_day), say("다시 해 볼게요.")], "매주 화요일 스터디")
    error = _outputs(fake)[0]["errors"][0]
    assert error["code"] == "invalid_value" and "rrule exploded" in error["message"]
    assert result.reply == "다시 해 볼게요."


# --- 루프 한도 ---------------------------------------------------------------------------------


def _search(query: str = "x"):
    return call("search_events", query=query, date_from=None, date_to=None, weekday=None)


def test_llm_call_limit_answers_normally(db: Session, user: User) -> None:
    result, _ = _turn(db, user, [_search() for _ in range(6)], "뭔가 해줘")

    assert result.reply == "요청을 끝까지 처리하지 못했어요. 조금 더 구체적으로 말해 줄래요?"
    log = db.execute(select(AssistantTurnLog)).scalar_one()
    assert (log.llm_calls, log.stop_reason) == (6, "llm_call_limit")


def test_call_limit_with_drafts_shows_them(db: Session, user: User) -> None:
    result, _ = _turn(db, user, [call("propose_create_event", **_create()), *[_search() for _ in range(5)]], "스터디")

    assert result.proposal is not None and len(result.proposal["items"]) == 1
    assert "초안" in result.reply


def test_turn_time_limit_stops_between_calls(db: Session, user: User) -> None:
    clock = {"t": 0.0}

    class SlowFake(FakeResponsesClient):
        def create(self, **kwargs):
            clock["t"] += 15
            return super().create(**kwargs)

    fake = SlowFake([call("propose_create_event", **_create()), _search(), say("never reached")])
    result = agent.chat(db, user, "스터디", client=fake, now=NOW, timer=lambda: clock["t"])

    assert len(fake.requests) == 2
    assert result.proposal is not None
    log = db.execute(select(AssistantTurnLog)).scalar_one()
    assert (log.llm_calls, log.stop_reason, log.latency_ms) == (2, "time_limit", 30000)


def test_turn_log_records_tokens_model_and_effort(db: Session, user: User) -> None:
    _turn(db, user, [_search(), say("없어요")], "x 찾아줘")

    log = db.execute(select(AssistantTurnLog)).scalar_one()
    assert (log.llm_calls, log.input_tokens, log.output_tokens, log.model, log.reasoning_effort) == (2, 200, 20, "fake", "low")


# --- 문맥 --------------------------------------------------------------------------------------


def test_reasoning_is_carried_within_a_turn_but_not_between_turns(db: Session, user: User) -> None:
    first, fake1 = _turn(db, user, [_search("스터디"), say("찾지 못했어요.")], "스터디 찾아줘")

    second_request = fake1.requests[1].input_items
    assert {"type": "reasoning", "id": "rs_0", "summary": [], "encrypted_content": "enc_0"} in second_request
    stored = db.execute(select(AssistantMessage).where(AssistantMessage.role == "assistant")).scalars().first()
    assert stored.content["output"][0]["encrypted_content"] == "enc_0"

    _, fake2 = _turn(db, user, [say("네")], "고마워", first.session_id, NOW + timedelta(minutes=1))
    history = fake2.requests[0].input_items
    assert not any(item.get("type") == "reasoning" for item in history)
    calls_ = [item for item in history if item.get("type") == "function_call"]
    assert calls_ and "id" not in calls_[0]
    assert any(item.get("type") == "function_call_output" for item in history)
    assert {"role": "user", "content": "스터디 찾아줘"} in history
    assert {"role": "assistant", "content": "찾지 못했어요."} in history


def test_history_keeps_only_the_last_20_items_and_pairs(db: Session, user: User) -> None:
    session_id = None
    for index in range(8):
        result, _ = _turn(db, user, [_search(f"q{index}"), say(f"a{index}")], f"u{index}", session_id, NOW + timedelta(minutes=index))
        session_id = result.session_id
    _, fake = _turn(db, user, [say("끝")], "마지막", session_id, NOW + timedelta(minutes=10))

    items = fake.requests[0].input_items
    history = [i for i in items if i.get("role") != "developer"][:-1]
    assert len(history) <= 20
    call_ids = {i["call_id"] for i in history if i.get("type") == "function_call"}
    output_ids = {i["call_id"] for i in history if i.get("type") == "function_call_output"}
    assert call_ids == output_ids


def test_long_tool_output_is_truncated_between_turns(db: Session, user: User) -> None:
    session = agent.create_session(db, user, NOW)
    db.add(AssistantMessage(session_id=session.id, role="user", content={"text": "q"}, created_at=NOW.replace(tzinfo=None)))
    db.add(AssistantMessage(
        session_id=session.id, role="assistant",
        content={"output": [{"type": "function_call", "id": "fc", "call_id": "c1", "name": "search_events", "arguments": "{}"}], "text": ""},
        created_at=NOW.replace(tzinfo=None),
    ))
    db.add(AssistantMessage(
        session_id=session.id, role="tool", content={"call_id": "c1", "name": "search_events", "output": json.dumps({"x": "y" * 5000})},
        created_at=NOW.replace(tzinfo=None),
    ))
    db.commit()

    _, fake = _turn(db, user, [say("네")], "다음", session.id, NOW + timedelta(minutes=1))
    output = next(i for i in fake.requests[0].input_items if i.get("type") == "function_call_output")["output"]
    assert len(output) < 1600 and "truncated" in output


def test_sessions_are_private_to_their_user(db: Session, user: User) -> None:
    other = User(name="Other", preferred_language="en")
    db.add(other)
    db.commit()
    result, _ = _turn(db, user, [call("propose_create_event", **_create()), say("초안")], "스터디")

    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        agent.chat(db, other, "hi", result.session_id, client=FakeResponsesClient([say("x")]), now=NOW)
    with pytest.raises(NotFoundError):
        agent.confirm(db, other, result.session_id, result.proposal["token"], now=NOW)
    assert agent.current_session(db, other, NOW) is None
    assert agent.current_session(db, user, NOW).id == result.session_id


def test_one_occurrence_found_by_date_is_updated_with_instance_scope(db: Session, user: User, lecture: Event) -> None:
    monday = db.execute(select(EventInstance).where(EventInstance.event_id == lecture.id, EventInstance.date == date(2026, 9, 28))).scalar_one()
    script = [
        call("search_events", query="ECE360", date_from="2026-09-28", date_to="2026-09-28", weekday=None),
        call(
            "propose_update_event",
            target_ids=[{"event_id": lecture.id, "instance_id": monday.id}],
            scope="instance",
            changes={"title": None, "date": None, "start_time": "11:00", "end_time": None, "importance": None, "location": None},
            inferred_fields=[],
            draft_id=None,
        ),
        say("내일 강의만 11시로 옮길까요?"),
    ]
    result, fake = _turn(db, user, script, "내일 ECE360 강의만 11시로")

    assert _outputs(fake)[0]["results"][0]["instance_id"] == monday.id
    target = result.proposal["items"][0]["targets"][0]
    assert result.proposal["items"][0]["scope"] == "instance"
    assert target["new_time_display"] == "오전 11:00 – 오후 12:00 (1시간)"

    agent.confirm(db, user, result.session_id, result.proposal["token"], now=NOW + timedelta(minutes=1))
    db.refresh(monday)
    assert monday.start_time_override == datetime(2026, 9, 28, 11)


def test_redrafting_with_draft_id_replaces_it_in_the_same_turn(db: Session, user: User) -> None:
    script = [
        call("propose_create_event", **_create(start_time="11:00", end_time="01:00")),
        call("propose_create_event", **_create(start_time="11:00", end_time="13:00", draft_id="d1")),
        say("초안"),
    ]
    result, _ = _turn(db, user, script, "10/2 11-1 스터디")

    assert len(result.proposal["items"]) == 1
    assert result.proposal["items"][0]["end_time"] == "2026-10-02T13:00:00"


def test_confirmed_recurring_event_keeps_rhythm_but_creates_only_upcoming_instances(db: Session, user: User, lecture_period: ImportantDateRange) -> None:
    args = _create(
        title="ECE360 Lab", date=None, start_time="09:00", end_time="12:00",
        recurrence={"frequency": "WEEKLY", "interval": 2, "by_day": ["TU"], "start_date": "2026-09-22"},
        date_range={"name": "Lecture Period", "start_date": None, "end_date": None},
    )
    result, _ = _turn(db, user, [call("propose_create_event", **args), say("초안")], "9/22부터 격주 화요일 Lab")
    agent.confirm(db, user, result.session_id, result.proposal["token"], now=NOW + timedelta(minutes=1))

    event = db.execute(select(Event).where(Event.title == "ECE360 Lab")).scalar_one()
    assert event.start_time == datetime(2026, 9, 22, 9)
    dates = sorted(i.date for i in event.instances)
    assert dates[:3] == [date(2026, 10, 6), date(2026, 10, 20), date(2026, 11, 3)] and dates[-1] == date(2026, 12, 1)


def test_one_off_in_the_past_still_warns(db: Session, user: User) -> None:
    result, _ = _turn(db, user, [call("propose_create_event", **_create(date="2026-09-25")), say("초안")], "9/25 스터디")
    assert [w["code"] for w in result.proposal["items"][0]["warnings"]] == ["past_date"]
