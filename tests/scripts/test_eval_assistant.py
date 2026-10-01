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
    result = ev.run_case(_case(data, "study_11_to_1"), data["seed"], _fake(call("propose_create_event", **wrong), say("초안")), "x")

    assert not result.passed
    assert any("end=01:00≠13:00" in reason for reason in result.reasons)


def test_approval_case_checks_confirm_and_db(data: dict) -> None:
    case = _case(data, "approve_creates")
    fakes = iter([
        FakeResponsesClient([call("propose_create_event", **STUDY), say("이렇게 만들까요?")]),
        FakeResponsesClient([say("네")]),  # confirm_pending을 부르지 않음
    ])
    result = ev.run_case(case, data["seed"], lambda: next(fakes), "x")

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
