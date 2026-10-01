"""평가 스크립트 자체의 검사 (실제 API 없이 가짜 LLM으로). 실제 모델 평가는 pytest에 넣지 않는다."""

from __future__ import annotations

import pytest

from app.scripts import eval_assistant as ev
from tests.fake_responses import FakeResponsesClient, call, say

TAGS = {"격주", "오전오후", "마감", "확인안전", "수정", "삭제", "기간", "장소", "다국어", "기타"}
STUDY = {
    "event_type": "scheduled", "title": "스터디", "date": "2026-10-02", "start_time": "11:00", "end_time": "13:00",
    "importance": 1, "recurrence": None, "date_range": None, "location": None, "inferred_fields": [], "draft_id": None,
}


@pytest.fixture(scope="module")
def data() -> dict:
    return ev.load_cases()


def _case(data: dict, case_id: str) -> dict:
    return next(c for c in data["cases"] if c["id"] == case_id)


def test_cases_are_well_formed(data: dict) -> None:
    cases = data["cases"]
    assert 25 <= len(cases) <= 40
    assert len({c["id"] for c in cases}) == len(cases)
    for case in cases:
        assert set(case["tags"]) <= TAGS, case["id"]
        assert case["turns"] and any(t.get("expect") for t in case["turns"]), case["id"]
        for turn in case["turns"]:
            for item in ((turn.get("expect") or {}).get("proposal") or {}).get("items", []):
                assert not [p for p in ev.card_problems({}, item) if p.startswith("unknown")], (case["id"], item)
    for tag in ev.MANDATORY_TAGS:
        assert any(tag in c["tags"] for c in cases), tag


def _fake(*steps):
    fake = FakeResponsesClient(list(steps))
    return lambda: fake


def test_run_case_passes_a_correct_script(data: dict) -> None:
    result = ev.run_case(_case(data, "study_11_to_1"), data["seed"], _fake(call("propose_create_event", **STUDY), say("초안")), "gpt-5.6-luna")

    assert result.passed, result.reasons
    turn = result.turns[0]
    assert (turn.llm_calls, turn.input_tokens, turn.output_tokens) == (2, 200, 20)
    assert turn.cost_usd == pytest.approx((200 * 0.20 + 20 * 1.20) / 1_000_000)


def test_run_case_reports_why_it_failed(data: dict) -> None:
    wrong = {**STUDY, "end_time": "01:00"}
    result = ev.run_case(_case(data, "study_11_to_1"), data["seed"], _fake(call("propose_create_event", **wrong), say("초안")), "gpt-5.6-luna")

    assert not result.passed
    assert any("end=01:00≠13:00" in reason for reason in result.reasons)


def test_approval_case_checks_confirm_and_db(data: dict) -> None:
    case = _case(data, "approve_creates")
    fakes = iter([
        FakeResponsesClient([call("propose_create_event", **STUDY), say("이렇게 만들까요?")]),
        FakeResponsesClient([say("네")]),  # confirm_pending을 부르지 않음
    ])
    result = ev.run_case(case, data["seed"], lambda: next(fakes), "gpt-5.6-luna")

    assert not result.passed
    assert any("confirm_pending" in reason for reason in result.reasons)
    assert any("executed=0<1" in reason for reason in result.reasons)


def test_summary_applies_the_selection_criteria() -> None:
    results = [ev.CaseResult(id=f"c{i}", tags=["격주" if i == 0 else "기타"], passed=i != 1, reasons=[], turns=[]) for i in range(10)]
    summary = ev.summarize(results)
    assert summary["pass_rate"] == 0.9 and summary["meets_criteria"]

    results[0].passed = False
    assert not ev.summarize(results)["meets_criteria"], "필수 범주(격주)가 100%가 아니면 탈락"


def test_list_question_is_checked_against_the_reply(data: dict) -> None:
    case = _case(data, "list_ranges")

    listed = ev.run_case(case, data["seed"], _fake(say("등록된 기간: 2026-2학기(9/1~12/20), Lecture Period(9/3~12/8)")), "gpt-5.6-luna")
    vague = ev.run_case(case, data["seed"], _fake(say("기간이 두 개 있어요.")), "gpt-5.6-luna")

    assert listed.passed, listed.reasons
    assert not vague.passed and "reply lacks" in vague.reasons[0]


def _delete_all_followup(data: dict, second_turn: list, monkeypatch: pytest.MonkeyPatch) -> ev.CaseResult:
    monkeypatch.setattr(ev.agent.secrets, "token_urlsafe", lambda n: "tok")
    quiz = [{"event_id": 2, "instance_id": None}]  # seed의 두 번째 일정
    fakes = iter([
        FakeResponsesClient([
            call("search_events", query="물리 퀴즈", date_from=None, date_to=None, weekday=None),
            call("propose_delete_event", target_ids=quiz, scope="series", inferred_fields=[], draft_id=None),
            say("물리 퀴즈 반복 전체를 삭제할까요?"),
        ]),
        FakeResponsesClient(second_turn),
    ])
    return ev.run_case(_case(data, "delete_all_followup"), data["seed"], lambda: next(fakes), "gpt-5.6-luna")


def test_any_of_accepts_approval_of_an_earlier_full_delete(data: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _delete_all_followup(data, [call("confirm_pending", token="tok"), say("삭제했어요")], monkeypatch)

    assert result.passed, result.reasons


def test_any_of_fails_when_no_option_matches(data: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _delete_all_followup(data, [say("어떤 걸 지울까요?")], monkeypatch)

    assert not result.passed
    assert all(reason.startswith("turn 2: any_of:") for reason in result.reasons)


def test_switch_to_deadline_accepts_no_new_proposal_when_already_a_deadline(data: dict) -> None:
    deadline = {**STUDY, "title": "ECE360 랩 리포트", "event_type": "deadline", "date": "2026-10-05", "start_time": None, "end_time": "15:00"}
    fakes = iter([
        FakeResponsesClient([call("propose_create_event", **deadline), say("마감으로 만들까요?")]),
        FakeResponsesClient([say("이미 3시 마감으로 되어 있어요. 이대로 만들까요?")]),
    ])

    result = ev.run_case(_case(data, "draft_switch_to_deadline"), data["seed"], lambda: next(fakes), "gpt-5.6-luna")

    assert result.passed, result.reasons


def test_changing_an_existing_deadline_must_update_not_create(data: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ev.agent.secrets, "token_urlsafe", lambda n: "tok")
    case = _case(data, "change_existing_deadline_time")
    target = [{"event_id": 1, "instance_id": None}]  # 이 케이스의 seed는 MAT389 과제 하나
    changes = {"title": None, "date": None, "start_time": None, "end_time": "23:59", "importance": None, "location": None}
    updating = iter([
        FakeResponsesClient([
            call("search_events", query="MAT389 과제", date_from=None, date_to=None, weekday=None),
            call("propose_update_event", target_ids=target, scope="series", changes=changes, inferred_fields=[], draft_id=None),
            say("이렇게 바꿀까요?"),
        ]),
        FakeResponsesClient([call("confirm_pending", token="tok"), say("바꿨어요")]),
    ])
    duplicating = iter([
        FakeResponsesClient([
            call("propose_create_event", **{**STUDY, "title": "MAT389 과제", "event_type": "deadline", "start_time": None, "end_time": "23:59"}),
            say("이렇게 만들까요?"),
        ]),
        FakeResponsesClient([call("confirm_pending", token="tok"), say("만들었어요")]),
    ])

    good = ev.run_case(case, data["seed"], lambda: next(updating), "gpt-5.6-luna")
    bad = ev.run_case(case, data["seed"], lambda: next(duplicating), "gpt-5.6-luna")

    assert good.passed, good.reasons
    assert not bad.passed
    assert any("unwanted kinds ['create_event']" in r for r in bad.reasons)
    assert any("DB events matching" in r for r in bad.reasons), "확정 후 같은 제목 할 일이 둘이 되면 실패"
