import pytest

from app.models.enums import Importance
from app.services.llm_client import ClarifyingQuestion, EventSlotFillResult, SLOT_NAMES
from app.services.slot_fill_session import (
    clear_all_sessions,
    create_session,
    delete_session,
    get_session,
    update_session,
)


@pytest.fixture(autouse=True)
def reset_sessions():
    clear_all_sessions()
    yield
    clear_all_sessions()


def test_create_session_starts_with_all_slots_missing() -> None:
    session = create_session(user_id=1)

    assert session.user_id == 1
    assert set(session.missing_slots) == set(SLOT_NAMES)
    assert session.is_complete is False
    assert get_session(session.session_id) is session


def test_get_session_returns_none_for_unknown_id() -> None:
    assert get_session("does-not-exist") is None


def test_delete_session_removes_it() -> None:
    session = create_session(user_id=1)

    delete_session(session.session_id)

    assert get_session(session.session_id) is None


def test_two_sessions_do_not_interfere() -> None:
    session1 = create_session(user_id=1)
    session2 = create_session(user_id=2)

    update_session(
        session1.session_id,
        EventSlotFillResult(title="스터디", missing_slots=[s for s in SLOT_NAMES if s != "title"]),
    )

    assert get_session(session1.session_id).title == "스터디"
    assert get_session(session2.session_id).title is None


def test_update_session_merges_across_multiple_turns() -> None:
    session = create_session(user_id=1)

    # 1턴: 제목만 알아냄
    update_session(
        session.session_id,
        EventSlotFillResult(
            title="알고리즘 스터디",
            missing_slots=["day_of_week", "start_time", "end_time", "importance", "date_range_id"],
            clarifying_questions=[
                ClarifyingQuestion(slot="day_of_week", question="무슨 요일인가요?")
            ],
        ),
    )
    after_turn1 = get_session(session.session_id)
    assert after_turn1.title == "알고리즘 스터디"
    assert after_turn1.day_of_week is None
    assert after_turn1.is_complete is False

    # 2턴: "월요일 9시부터 10시, 중요도는 딱히 없어" -> importance는 명시적으로 None(없음)
    # 이전 턴에 알아낸 title은 호출부가 컨텍스트로 다시 넘겨줬다고 가정하고 그대로 반영
    update_session(
        session.session_id,
        EventSlotFillResult(
            title="알고리즘 스터디",
            day_of_week="MO",
            start_time="09:00",
            end_time="10:00",
            importance=None,
            missing_slots=["date_range_id"],
            clarifying_questions=[
                ClarifyingQuestion(slot="date_range_id", question="어떤 학기/기간 기준인가요?")
            ],
        ),
    )
    after_turn2 = get_session(session.session_id)
    assert after_turn2.title == "알고리즘 스터디"
    assert after_turn2.day_of_week == "MO"
    assert after_turn2.start_time == "09:00"
    assert after_turn2.end_time == "10:00"
    assert after_turn2.importance is None  # missing_slots에 없으므로 "없음"이 확정값
    assert after_turn2.missing_slots == ["date_range_id"]
    assert after_turn2.is_complete is False

    # 3턴: 기간 확정
    update_session(
        session.session_id,
        EventSlotFillResult(
            title="알고리즘 스터디",
            day_of_week="MO",
            start_time="09:00",
            end_time="10:00",
            importance=None,
            date_range_id=7,
            missing_slots=[],
        ),
    )
    final = get_session(session.session_id)
    assert final.date_range_id == 7
    assert final.is_complete is True


def test_update_session_leaves_still_missing_slots_untouched() -> None:
    session = create_session(user_id=1)
    update_session(
        session.session_id,
        EventSlotFillResult(
            importance=Importance.MUST,
            missing_slots=["title", "day_of_week", "start_time", "end_time", "date_range_id"],
        ),
    )

    result = get_session(session.session_id)

    assert result.importance == Importance.MUST
    assert result.title is None
    assert set(result.missing_slots) == {"title", "day_of_week", "start_time", "end_time", "date_range_id"}


def test_update_session_returns_none_for_unknown_session() -> None:
    assert update_session("does-not-exist", EventSlotFillResult(missing_slots=[])) is None
