"""FR-11 다국어 회귀 테스트.

preferred_language(ko/en)에 따라 사용자에게 보이는 네 가지 출력이 모두 그 언어로 나오는지 한 번에 확인한다.
1. FR-6 카테고리 라벨   2. 알림(푸시/텔레그램) 문구   3. 페르소나 LLM 응답   4. 슬롯필링 되묻는 질문

LLM은 가짜 응답을 쓰므로 3·4는 "요청에 올바른 언어 규칙이 들어가고, 모델 응답이 그대로 사용자에게 전달되는지"를
검증한다. 가짜 모델은 시스템 프롬프트의 preferred_language를 읽고 그 언어로 답하도록 흉내 낸다.
"""

import json
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.clock import utc_now_naive
from app.core.config import settings
from app.core.db import get_db
from app.i18n import load_notification_templates
from app.main import app
from app.models import EngagementScope, Event, EventInstance, EventInstanceStatus, EventType, User
from app.models.base import Base
from app.schemas.persona import PersonaCreate
from app.services import daily_checkin as daily_checkin_module
from app.services import engagement_service
from app.services.assistant import agent
from app.services import llm_client as llm_client_module
from app.services import notification as notification_module
from app.services import persona_service
from app.services import sleep_checkin as sleep_checkin_module
from app.services.engagement_service import evaluate_escalation, get_or_create_engagement_state
from tests.assistant_flow import confirm, create, propose
from tests.auth_helpers import as_user

LANGUAGES = ("ko", "en")
HANGUL = re.compile(r"[가-힣]")
_REAL_HTTPX_CLIENT = httpx.Client  # 몽키패치 전에 원본을 캡처 (안 하면 자기 자신을 재귀 호출함)

# 가짜 모델이 preferred_language를 보고 고르는 응답
FAKE_REPLIES = {"ko": "오늘도 수고했어요!", "en": "Great job today!"}
NATIVE_REPLY_RULES = {"ko": "반드시 한국어로만 답하세요.", "en": "Respond only in English."}
ASSISTANT_LANGUAGE_RULES = {"ko": "반드시 한국어로 답하세요.", "en": "Respond only in English."}


def assert_in_language(text: str, language: str) -> None:
    if language == "en":
        assert not HANGUL.search(text), f"영어 사용자에게 한글이 섞임: {text!r}"
    else:
        assert HANGUL.search(text), f"한국어 사용자에게 한글이 없음: {text!r}"


# --- fixtures ---


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    # 스케줄러 잡(알림/체크인)도 같은 테스트 DB를 보게 한다.
    for module in (notification_module, sleep_checkin_module, daily_checkin_module):
        monkeypatch.setattr(module, "SessionLocal", testing_session_local)

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    def fake_send(token: str, title: str, body: str) -> None:
        sent.append((title, body))

    for module in (notification_module, sleep_checkin_module, daily_checkin_module):
        monkeypatch.setattr(module, "send_push_notification", fake_send)
    monkeypatch.setattr(User, "fcm_token", "device-token", raising=False)
    return sent


@pytest.fixture
def telegrams(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(engagement_service, "send_telegram_message", lambda user, text: sent.append(text))
    return sent


@pytest.fixture
def llm_requests(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """preferred_language를 읽어 그 언어로 답하는 가짜 모델. 받은 요청을 기록한다."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        language = re.search(r"reply_language: (\w+)", payload["messages"][0]["content"]).group(1)
        content = FAKE_REPLIES[language]
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(
        llm_client_module.httpx, "Client", lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler))
    )
    return captured


@pytest.fixture
def world(client: TestClient, engine) -> dict[str, int]:
    """사용자 1명 + 페르소나 선택 + 일반 일정 1개 + 마감 1개 + 에스컬레이션 상태를 준비한다."""
    # Created directly: the persona API is admin-only, and an extra admin account would receive notifications too.
    with Session(engine) as session:
        persona_service.create_persona(session, PersonaCreate(**{
            "name": "Hana",
            "display_name": {"ko": "하나", "en": "Hana"},
            "description": {"ko": "상냥한 대학생", "en": "Kind college student"},
            "example_lines": {
                "ko": [{"situation": "칭찬", "line": "정말 잘했어요!"}],
                "en": [{"situation": "praise", "line": "You did great!"}],
            },
        }))

    with Session(engine) as session:
        user = User(name="Alex", preferred_language="ko", telegram_opt_in=True, telegram_chat_id="1")
        session.add(user)
        session.flush()
        scheduled = Event(
            user_id=user.id, title="Gym", start_time=datetime(2026, 9, 24, 19), end_time=datetime(2026, 9, 24, 20)
        )
        deadline = Event(
            user_id=user.id, title="Essay", event_type=EventType.DEADLINE, start_time=None, end_time=datetime(2026, 9, 25, 23, 59)
        )
        session.add_all([scheduled, deadline])
        session.flush()
        scheduled_instance = EventInstance(event_id=scheduled.id, date=date(2026, 9, 24), status=EventInstanceStatus.MISSED)
        deadline_instance = EventInstance(event_id=deadline.id, date=date(2026, 9, 25), status=EventInstanceStatus.PENDING)
        session.add_all([scheduled_instance, deadline_instance])
        session.commit()
        ids = {
            "user": user.id,
            "scheduled_event": scheduled.id,
            "scheduled_instance": scheduled_instance.id,
            "deadline_instance": deadline_instance.id,
        }

    assert client.put("/users/me/persona", json={"persona_name": "Hana"}, headers=_headers(ids["user"])).status_code == 200
    return ids


def _headers(user_id: int) -> dict[str, str]:
    return as_user(user_id)


def _set_language(engine, user_id: int, language: str) -> None:
    with Session(engine) as session:
        session.get(User, user_id).preferred_language = language
        session.commit()


# --- 각 출력 경로를 실제 코드로 실행하는 헬퍼 ---


def _category_labels(client: TestClient, world: dict[str, int]) -> list[str]:
    body = client.get("/compliance-reports/categories", headers=_headers(world["user"])).json()
    return [item["label"] for item in body]


def _notifications(engine, world: dict[str, int], pushes: list, telegrams: list) -> list[str]:
    pushes.clear()
    telegrams.clear()
    notification_module._send_notification(world["scheduled_instance"], "start")
    notification_module._send_notification(world["scheduled_instance"], "end")
    notification_module._send_notification(world["deadline_instance"], "deadline_reminder")
    sleep_checkin_module.send_daily_sleep_checkin_reminders()
    daily_checkin_module.send_daily_checkin_reminders()

    with Session(engine) as session:
        for scope, ref_event_id in ((EngagementScope.EVENT, world["scheduled_event"]), (EngagementScope.GLOBAL, None)):
            state = get_or_create_engagement_state(session, world["user"], scope, ref_event_id)
            state.escalation_stage = state.escalation_stage.NORMAL
            state.last_response_at = utc_now_naive() - timedelta(weeks=1)
            session.commit()
            evaluate_escalation(session, state)

    assert len(pushes) == 5 and len(telegrams) == 2
    return [text for pair in pushes for text in pair] + telegrams


def _persona_replies(client: TestClient, world: dict[str, int], llm_requests: list) -> tuple[list[str], list[dict]]:
    llm_requests.clear()
    checkin = client.post("/daily-actual-logs/checkin", json={"utterance": "ok"}, headers=as_user(world["user"]))  # 애매한 입력 → 화면 언어로 답한다
    feedback = client.post(
        "/compliance-reports",
        json={"event_instance_id": world["scheduled_instance"], "reason_category": "other", "reason_text": "ECE360"},
        headers=as_user(world["user"]),
    )
    assert checkin.status_code == 200 and feedback.status_code == 201
    return [checkin.json()["reply"], feedback.json()["llm_feedback"]], list(llm_requests)


def _assistant_texts(client: TestClient, world: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], str]:
    """어시스턴트 카드의 경고(자정 넘김)와 [만들기] 뒤 실행 문구, 그리고 모델에 보낸 입력 전체."""
    chat = propose(
        client, monkeypatch, world["user"],
        create(title="Study", date="2026-10-01", start_time="22:00", end_time="06:00"),
        message="ok",
    )
    [card] = chat["proposal"]["items"]
    done = confirm(client, world["user"], chat)
    # 되돌려서 다음 언어 차례에 같은 상태로 시작한다 (남겨 두면 "비슷한 일정이 이미 있어요" 경고가 붙는다)
    for action in done["executed"]:
        client.post(f"/actions/{action['action_id']}/undo", headers=as_user(world["user"]))
    fake = agent.ResponsesClient()  # propose()가 바꿔 둔 가짜 클라이언트
    sent = json.dumps(fake.requests[0].input_items, ensure_ascii=False)
    return [w["message"] for w in card["warnings"]] + [done["reply"]], sent


# --- 1. 카테고리 라벨 ---


@pytest.mark.parametrize("language", LANGUAGES)
def test_category_labels(client: TestClient, engine, world: dict[str, int], language: str) -> None:
    _set_language(engine, world["user"], language)

    labels = _category_labels(client, world)
    created = client.post(
        "/compliance-reports",
        json={"event_instance_id": world["scheduled_instance"], "reason_category": "overslept"},
        headers=as_user(world["user"]),
    ).json()

    assert len(labels) == 7
    for label in [*labels, created["reason_category_label"]]:
        assert_in_language(label, language)
    assert created["reason_category"] == "overslept"
    assert created["reason_category_label"] == {"ko": "늦잠/기상 실패", "en": "Overslept"}[language]


# --- 2. 알림 문구 ---


@pytest.mark.parametrize("language", LANGUAGES)
def test_notification_texts(
    client: TestClient, engine, world: dict[str, int], pushes: list, telegrams: list, language: str
) -> None:
    _set_language(engine, world["user"], language)

    texts = _notifications(engine, world, pushes, telegrams)

    t = load_notification_templates(language)
    assert texts == [
        t["event_start.title"].format(title="Gym"),
        t["event_start.body"],
        t["event_end.title"].format(title="Gym"),
        t["event_end.body"],
        t["deadline_reminder.title"].format(title="Essay"),
        t["deadline_reminder.body"],
        t["sleep_checkin.title"],
        t["sleep_checkin.body"],
        t["daily_checkin.title"],
        t["daily_checkin.body"],
        t["escalation.week_1"].format(subject=t["escalation.subject_event"].format(title="Gym")),
        t["escalation.week_1"].format(subject=t["escalation.subject_app"]),
    ]
    # "[Schreduler] Gym"처럼 일정 제목만 들어가는 알림 제목은 언어와 무관하므로 문자 체계 검사에서 뺀다
    for text in texts:
        if not re.fullmatch(r"\[Schreduler\] (Gym|Essay)", text):
            assert_in_language(text, language)


# --- 3. 페르소나 응답 ---


@pytest.mark.parametrize("language", LANGUAGES)
def test_persona_replies(
    client: TestClient, engine, world: dict[str, int], llm_requests: list, language: str
) -> None:
    _set_language(engine, world["user"], language)

    replies, requests = _persona_replies(client, world, llm_requests)

    assert replies == [FAKE_REPLIES[language]] * 2
    assert len(requests) == 2  # 체크인 + 미준수 피드백
    for request in requests:
        system_message = request["messages"][0]["content"]
        assert f"reply_language: {language}" in system_message
        assert system_message.endswith(NATIVE_REPLY_RULES[language])
        assert {"ko": "하나", "en": "Hana"}[language] in system_message  # 페르소나 설정도 같은 언어
        assert {"ko": "정말 잘했어요!", "en": "You did great!"}[language] in system_message


# --- 4. 일정 어시스턴트의 카드 경고·실행 문구 ---


@pytest.mark.parametrize("language", LANGUAGES)
def test_assistant_texts(client: TestClient, engine, world: dict[str, int], monkeypatch: pytest.MonkeyPatch, language: str) -> None:
    _set_language(engine, world["user"], language)

    texts, sent = _assistant_texts(client, world, monkeypatch)

    assert len(texts) == 2  # 자정 넘김 경고 + "✔ … 생성"
    for text in texts:
        assert_in_language(text, language)
    assert ASSISTANT_LANGUAGE_RULES[language] in sent
    other = "en" if language == "ko" else "ko"
    assert ASSISTANT_LANGUAGE_RULES[other] not in sent


# --- 같은 사용자의 언어를 바꿔가며 전부 따라 바뀌는지 ---


def test_switching_language_updates_every_output(
    client: TestClient, engine, world: dict[str, int], pushes: list, telegrams: list, llm_requests: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, dict[str, object]] = {}

    for language in ("ko", "en", "ko"):
        _set_language(engine, world["user"], language)
        labels = _category_labels(client, world)
        texts = _notifications(engine, world, pushes, telegrams)
        replies, persona_requests = _persona_replies(client, world, llm_requests)
        assistant, _ = _assistant_texts(client, world, monkeypatch)

        snapshot = {"labels": labels, "notifications": texts, "replies": replies, "assistant": assistant}
        if language in seen:
            assert snapshot == seen[language], "같은 언어로 돌아왔는데 출력이 다르다 (이전 언어가 캐시됐을 가능성)"
        seen[language] = snapshot

        for request in persona_requests:
            assert f"reply_language: {language}" in request["messages"][0]["content"]

    assert seen["ko"]["labels"] != seen["en"]["labels"]
    assert seen["ko"]["notifications"] != seen["en"]["notifications"]
    assert seen["ko"]["replies"] != seen["en"]["replies"]
    assert seen["ko"]["assistant"] != seen["en"]["assistant"]
    for text in [*seen["en"]["labels"], *seen["en"]["notifications"], *seen["en"]["replies"], *seen["en"]["assistant"]]:
        assert_in_language(text, "en")
