import json
from datetime import date

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Base, ImportantDateRange, User
from app.models.enums import Importance
from app.services.llm_client import (
    DateRangeOption,
    LLMClientError,
    _build_request_payload,
    _parse_response,
    fill_event_slots,
    fill_event_slots_for_user,
    get_date_range_options,
)


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _chat_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
    )


def test_fill_event_slots_raises_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", None)

    with pytest.raises(LLMClientError):
        fill_event_slots("매주 월요일 아침 9시에 알고리즘 스터디")


def test_fill_event_slots_parses_complete_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == settings.llm_model
        return _chat_response(
            {
                "title": "알고리즘 스터디",
                "frequency": "WEEKLY",
                "by_day": ["MO"],
                "start_time": "09:00",
                "end_time": "10:00",
                "importance": 3,
                "date_range_id": None,
                "missing_slots": [],
                "clarifying_questions": [],
            }
        )

    result = fill_event_slots(
        "매주 월요일 아침 9시에 알고리즘 스터디",
        http_client=_mock_client(handler),
    )

    assert result.title == "알고리즘 스터디"
    assert result.frequency == "WEEKLY"
    assert result.by_day == ["MO"]
    assert result.start_time == "09:00"
    assert result.end_time == "10:00"
    assert result.importance == Importance.OBLIGATION_NO_CHECK
    assert result.date_range_id is None
    assert result.is_complete is True


def test_fill_event_slots_reports_missing_slots_and_questions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(
            {
                "title": "발표 준비",
                "frequency": None,
                "by_day": None,
                "start_time": None,
                "end_time": None,
                "importance": None,
                "date_range_id": None,
                "missing_slots": ["frequency", "start_time", "end_time"],
                "clarifying_questions": [
                    {"slot": "frequency", "question": "얼마나 자주 반복하나요?"},
                    {"slot": "start_time", "question": "몇 시에 시작하나요?"},
                    {"slot": "end_time", "question": "몇 시에 끝나나요?"},
                ],
            }
        )

    result = fill_event_slots("발표 준비 해야 돼", http_client=_mock_client(handler))

    assert result.is_complete is False
    assert set(result.missing_slots) == {"frequency", "start_time", "end_time"}
    assert [q.slot for q in result.clarifying_questions] == ["frequency", "start_time", "end_time"]
    assert result.start_time is None


def test_fill_event_slots_leaves_importance_none_when_not_missing() -> None:
    """importance=None인데 missing_slots에 없으면 '없음(수면)'이라는 명시적 값이지,
    모른다는 뜻이 아니다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(
            {
                "title": "수면",
                "frequency": "DAILY",
                "by_day": None,
                "start_time": "23:00",
                "end_time": "07:00",
                "importance": None,
                "date_range_id": None,
                "missing_slots": [],
                "clarifying_questions": [],
            }
        )

    result = fill_event_slots("매일 밤 11시에 자서 아침 7시에 일어나", http_client=_mock_client(handler))

    assert result.importance is None
    assert result.is_complete is True


def test_fill_event_slots_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    with pytest.raises(LLMClientError):
        fill_event_slots("아무 발화", http_client=_mock_client(handler))


def test_fill_event_slots_raises_on_malformed_json_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "이건 JSON이 아님"}}]}
        )

    with pytest.raises(LLMClientError):
        fill_event_slots("아무 발화", http_client=_mock_client(handler))


def test_fill_event_slots_raises_when_schema_does_not_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response({"importance": "매우 중요함", "missing_slots": "all"})

    with pytest.raises(LLMClientError):
        fill_event_slots("아무 발화", http_client=_mock_client(handler))


def test_build_request_payload_includes_date_range_candidates() -> None:
    candidates = [
        DateRangeOption(
            id=5, name="2026 가을학기", start_date=date(2026, 9, 1), end_date=date(2026, 12, 20)
        )
    ]

    payload = _build_request_payload("공강까지 매주 화요일 헬스", candidates, date(2026, 9, 17))

    user_message = payload["messages"][1]["content"]
    assert "2026 가을학기" in user_message
    assert '"id": 5' in user_message
    assert payload["response_format"]["json_schema"]["name"] == "event_slot_fill"


def test_build_request_payload_omits_temperature() -> None:
    """이 모델은 temperature 커스텀 값을 지원하지 않아 키 자체를 안 보내야 한다."""
    payload = _build_request_payload("아무 발화", [], date(2026, 9, 17))

    assert "temperature" not in payload


def test_parse_response_raises_on_invalid_json() -> None:
    with pytest.raises(LLMClientError):
        _parse_response("이건 JSON이 아님")


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_get_date_range_options_returns_only_that_users_ranges_sorted_by_start(
    db_session: Session,
) -> None:
    user1 = User(name="June", preferred_language="ko")
    user2 = User(name="Other", preferred_language="en")
    db_session.add_all([user1, user2])
    db_session.flush()

    later = ImportantDateRange(
        user_id=user1.id, name="인턴십", start_date=date(2027, 1, 5), end_date=date(2027, 2, 28)
    )
    earlier = ImportantDateRange(
        user_id=user1.id, name="2026 가을학기", start_date=date(2026, 9, 1), end_date=date(2026, 12, 20)
    )
    other_users = ImportantDateRange(
        user_id=user2.id, name="다른 사람 학기", start_date=date(2026, 9, 1), end_date=date(2026, 12, 20)
    )
    db_session.add_all([later, earlier, other_users])
    db_session.commit()

    options = get_date_range_options(db_session, user1.id)

    assert [option.name for option in options] == ["2026 가을학기", "인턴십"]
    assert all(isinstance(option, DateRangeOption) for option in options)


def test_fill_event_slots_for_user_passes_users_date_ranges_as_candidates(
    db_session: Session,
) -> None:
    user = User(name="June", preferred_language="ko")
    db_session.add(user)
    db_session.flush()

    date_range = ImportantDateRange(
        user_id=user.id, name="2026 가을학기", start_date=date(2026, 9, 1), end_date=date(2026, 12, 20)
    )
    db_session.add(date_range)
    db_session.commit()

    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return _chat_response(
            {
                "title": "헬스",
                "frequency": "WEEKLY",
                "by_day": ["TU"],
                "start_time": "19:00",
                "end_time": "20:00",
                "importance": 1,
                "date_range_id": date_range.id,
                "missing_slots": [],
                "clarifying_questions": [],
            }
        )

    result = fill_event_slots_for_user(
        db_session,
        user.id,
        "공강까지 매주 화요일 저녁 7시에 헬스",
        http_client=_mock_client(handler),
    )

    user_message = captured_payload["messages"][1]["content"]
    assert "2026 가을학기" in user_message
    assert f'"id": {date_range.id}' in user_message
    assert result.date_range_id == date_range.id


def test_fill_event_slots_for_user_with_no_registered_ranges_sends_empty_candidates(
    db_session: Session,
) -> None:
    user = User(name="June", preferred_language="ko")
    db_session.add(user)
    db_session.commit()

    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return _chat_response(
            {
                "title": "헬스",
                "missing_slots": ["frequency", "start_time", "end_time", "importance", "date_range_id"],
                "clarifying_questions": [
                    {"slot": "date_range_id", "question": "언제까지 반복할까요?"}
                ],
            }
        )

    result = fill_event_slots_for_user(
        db_session, user.id, "매주 화요일 헬스", http_client=_mock_client(handler)
    )

    user_message = captured_payload["messages"][1]["content"]
    assert "등록된 기간(date_range) 후보: []" in user_message
    assert "date_range_id" in result.missing_slots
