import pytest

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
    assert "반드시 영어(English)로만 답하라" in system_message
    assert "버스가 안 왔어요" in payload["messages"][1]["content"]


def test_default_persona_when_none_selected(monkeypatch) -> None:
    payload = _capture_payload(
        monkeypatch, llm_client.generate_compliance_feedback, NonComplianceCategory.OTHER, None
    )

    assert llm_client.DEFAULT_PERSONA_BLOCK in payload["messages"][0]["content"]
    assert payload["prompt_cache_key"].startswith("compliance_feedback:")


# --- 응답 언어 강제 (FR-11) ---


@pytest.mark.parametrize("build", ["daily_checkin", "compliance_feedback"])
@pytest.mark.parametrize(
    ("language", "name", "native_rule"),
    [("ko", "한국어(Korean)", "반드시 한국어로만 답하세요."), ("en", "영어(English)", "Respond only in English.")],
)
def test_language_block_is_last_system_block(build: str, language: str, name: str, native_rule: str) -> None:
    payload = _payload(build, language)
    system_message = payload["messages"][0]["content"]
    language_block = system_message.split("\n\n")[-1]

    assert language_block.startswith("[응답 언어]")
    assert f"preferred_language: {language}" in language_block
    assert f"반드시 {name}로만 답하라" in language_block
    assert language_block.endswith(native_rule)
    assert "[응답 언어]" not in payload["messages"][1]["content"]


@pytest.mark.parametrize("build", ["daily_checkin", "compliance_feedback"])
def test_task_instructions_no_longer_name_a_language(build: str) -> None:
    ko_instructions = _payload(build, "ko")["messages"][0]["content"].split("\n\n")[0]
    en_instructions = _payload(build, "en")["messages"][0]["content"].split("\n\n")[0]

    assert ko_instructions == en_instructions
    assert "한국어" not in ko_instructions and "영어" not in ko_instructions


@pytest.mark.parametrize("build", ["daily_checkin", "compliance_feedback"])
def test_each_language_gets_its_own_cache_key(build: str) -> None:
    assert _payload(build, "ko")["prompt_cache_key"] != _payload(build, "en")["prompt_cache_key"]
    assert _payload(build, "en")["prompt_cache_key"] == _payload(build, "en")["prompt_cache_key"]


def test_unsupported_language_falls_back_to_korean() -> None:
    assert _payload("daily_checkin", "ja")["messages"][0] == _payload("daily_checkin", "ko")["messages"][0]


def _payload(build: str, language: str) -> dict:
    if build == "daily_checkin":
        return llm_client.build_daily_checkin_payload("요약", "안녕", persona=RORDON, language=language)
    return llm_client.build_compliance_feedback_payload(
        NonComplianceCategory.OTHER, "버스가 늦었어요", persona=RORDON, language=language
    )


def test_persona_backstory_is_included_in_users_language() -> None:
    persona = RORDON.model_copy(update={"backstory": RORDON.display_name.model_copy(update={"ko": "미슐랭 셰프 출신", "en": "Former Michelin chef"})})

    ko = llm_client.build_daily_checkin_payload("요약", "안녕", persona=persona, language="ko")["messages"][0]["content"]
    en = llm_client.build_daily_checkin_payload("요약", "hi", persona=persona, language="en")["messages"][0]["content"]

    assert "배경: 미슐랭 셰프 출신" in ko
    assert "배경: Former Michelin chef" in en
