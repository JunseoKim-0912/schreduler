from datetime import date

from app.models.enums import NonComplianceCategory
from app.schemas.persona import PersonaRead
from app.services import llm_client
from app.services.llm_client import DateRangeOption, _build_request_payload

RORDON = PersonaRead.model_validate(
    {
        "name": "Rordon",
        "display_name": {"ko": "로든 갬지", "en": "Rordon Gamsay"},
        "description": {"ko": "깐깐한 쉐프", "en": "Strict chef"},
        "example_lines": {
            "ko": [{"situation": "지각", "line": "남극에서 스테이크 굽는 게 더 빠르겠군."}],
            "en": [{"situation": "late", "line": "Faster to grill a steak in Antarctica."}],
        },
        "backstory": None,
    }
)

SEMESTER = DateRangeOption(id=5, name="2026 가을학기", start_date=date(2026, 9, 1), end_date=date(2026, 12, 20))


def _capture_payload(monkeypatch, fn, *args, **kwargs) -> dict:
    captured: dict = {}

    def fake_call(payload: dict, http_client: object) -> str:
        captured.update(payload)
        return "ok"

    monkeypatch.setattr(llm_client, "_call_chat_completion", fake_call)
    fn(*args, **kwargs)
    return captured


def test_slot_fill_prefix_is_stable_across_dynamic_inputs() -> None:
    first = _build_request_payload("매주 월요일 헬스", [SEMESTER], date(2026, 9, 17))
    second = _build_request_payload(
        "매일 아침 7시 기상", [SEMESTER], date(2026, 9, 18), known_slots={"title": "기상"}
    )

    assert first["messages"][0] == second["messages"][0]
    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert first["prompt_cache_key"].startswith("event_slot_fill:")

    user_message = second["messages"][1]["content"]
    assert "2026-09-18" in user_message
    assert '"title": "기상"' in user_message
    assert user_message.endswith("사용자 발화: 매일 아침 7시 기상")
    assert "2026-09-18" not in second["messages"][0]["content"]


def test_slot_fill_cache_key_changes_when_date_ranges_change() -> None:
    with_range = _build_request_payload("헬스", [SEMESTER], date(2026, 9, 17))
    without_range = _build_request_payload("헬스", [], date(2026, 9, 17))

    assert with_range["prompt_cache_key"] != without_range["prompt_cache_key"]


def test_daily_checkin_puts_persona_in_prefix_and_summary_after(monkeypatch) -> None:
    first = _capture_payload(
        monkeypatch, llm_client.generate_daily_checkin_reply, "요약 A", "안녕", persona=RORDON
    )
    second = _capture_payload(
        monkeypatch, llm_client.generate_daily_checkin_reply, "요약 B", "잘 자", persona=RORDON
    )

    system_message = first["messages"][0]["content"]
    assert "로든 갬지" in system_message
    assert "남극에서 스테이크 굽는 게 더 빠르겠군." in system_message
    assert system_message.index("[페르소나]") > system_message.index("저녁 체크인")

    assert first["messages"][0] == second["messages"][0]
    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert first["messages"][1]["content"] == "오늘 요약:\n요약 A\n\n사용자 발화: 안녕"


def test_persona_block_follows_user_language(monkeypatch) -> None:
    payload = _capture_payload(
        monkeypatch,
        llm_client.generate_compliance_feedback,
        NonComplianceCategory.OTHER,
        "버스가 안 왔어요",
        persona=RORDON,
        language="en",
    )

    system_message = payload["messages"][0]["content"]
    assert "Rordon Gamsay" in system_message
    assert "Faster to grill a steak in Antarctica." in system_message
    assert "영어로 답하라" in system_message
    assert "버스가 안 왔어요" in payload["messages"][1]["content"]


def test_default_persona_when_none_selected(monkeypatch) -> None:
    payload = _capture_payload(
        monkeypatch, llm_client.generate_compliance_feedback, NonComplianceCategory.OTHER, None
    )

    assert llm_client.DEFAULT_PERSONA_BLOCK in payload["messages"][0]["content"]
    assert payload["prompt_cache_key"].startswith("compliance_feedback:")
