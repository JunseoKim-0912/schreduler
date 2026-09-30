import json

import httpx
import pytest

from app.core.config import settings
from app.scripts import compare_nl_paths
from app.scripts.compare_nl_paths import Answers, cost_usd, run_path
from app.scripts.eval_assistant import load_cases
from app.services.assistant import agent
from tests.fake_responses import FakeResponsesClient, call, say

STUDY = next(spec for spec in compare_nl_paths.COMPARE_CASES if spec["id"] == "study_11_to_1")


@pytest.fixture
def data() -> dict:
    return load_cases()


@pytest.fixture
def study_case(data: dict) -> dict:
    return next(case for case in data["cases"] if case["id"] == "study_11_to_1")


def _slot_fill_transport(replies: list[dict]) -> httpx.MockTransport:
    """옛 슬롯필링 응답을 차례로 돌려주는 가짜 Chat Completions."""

    def handler(request: httpx.Request) -> httpx.Response:
        content = replies.pop(0)
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.6-luna-test",
                "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
                "usage": {
                    "prompt_tokens": 2000,
                    "completion_tokens": 300,
                    "prompt_tokens_details": {"cached_tokens": 1536},
                    "completion_tokens_details": {"reasoning_tokens": 200},
                },
            },
        )

    return httpx.MockTransport(handler)


def test_answers_pick_the_first_unused_match() -> None:
    answers = Answers([{"ask": "오전|오후", "say": "오전"}, {"ask": "반복", "say": "한번만"}])

    assert answers.reply_to("반복할까요?") == "한번만"
    assert answers.reply_to("오전 11시인가요, 오후 11시인가요?") == "오전"
    assert answers.reply_to("오전인가요?") is None  # 이미 쓴 답은 다시 쓰지 않는다 → 추가 질문


def test_cost_counts_cached_tokens_at_the_cached_price() -> None:
    assert cost_usd("gpt-5.6-luna", 2000, 1500, 300) == pytest.approx((500 * 0.20 + 1500 * 0.02 + 300 * 1.20) / 1_000_000)


def test_old_path_asks_answers_and_confirms(monkeypatch: pytest.MonkeyPatch, data: dict, study_case: dict) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    replies = [
        {"title": "스터디", "date": "2026-10-02", "start_time": "11:00", "end_time": "13:00", "importance": 1,
         "missing_slots": ["frequency"], "clarifying_questions": [{"slot": "frequency", "question": "반복할까요?"}]},
        {"title": "스터디", "date": "2026-10-02", "start_time": "11:00", "end_time": "13:00", "importance": 1},
    ]
    transport = _slot_fill_transport(replies)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(transport=transport))

    run = run_path("old", study_case, STUDY, data["seed"])

    assert run.status == "confirmed", (run.questions, run.problems)
    assert run.correct, run.problems
    assert run.total("llm_calls") >= 1
    assert run.total("reasoning_tokens") == 200 * run.total("llm_calls")
    assert run.total("cached_tokens") == 1536 * run.total("llm_calls")
    assert set(run.models) == {"gpt-5.6-luna-test"}
    assert set(run.efforts) == {None}


def test_new_path_confirms_the_proposal_with_the_button(monkeypatch: pytest.MonkeyPatch, data: dict, study_case: dict) -> None:
    fake = FakeResponsesClient([
        call("propose_create_event", title="스터디", event_type="scheduled", date="2026-10-02", start_time="11:00", end_time="13:00",
             importance=1, inferred_fields=["importance"]),
        say("이렇게 만들까요?"),
    ])
    monkeypatch.setattr(agent, "ResponsesClient", lambda: fake)

    run = run_path("new", study_case, STUDY, data["seed"])

    assert run.status == "confirmed"
    assert run.correct, run.problems
    assert (len(run.turns), run.total("llm_calls"), run.total("input_tokens")) == (1, 2, 200)


def test_unexpected_question_stops_the_run(monkeypatch: pytest.MonkeyPatch, data: dict, study_case: dict) -> None:
    fake = FakeResponsesClient([say("무슨 스터디인가요?")])
    monkeypatch.setattr(agent, "ResponsesClient", lambda: fake)

    run = run_path("new", study_case, STUDY, data["seed"])

    assert run.status == "추가 질문 발생"
    assert run.questions == ["무슨 스터디인가요?"]
