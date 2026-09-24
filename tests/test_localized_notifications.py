from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, EngagementScope, Event, EventInstance, EventInstanceStatus, User
from app.services import engagement_service
from app.services import notification as notification_module
from app.services import sleep_checkin as sleep_checkin_module
from app.services.engagement_service import evaluate_escalation, get_or_create_engagement_state


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def sent_pushes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    sent: list[tuple[str, str, str]] = []

    def fake_send(token: str, title: str, body: str) -> None:
        sent.append((token, title, body))

    monkeypatch.setattr(notification_module, "send_push_notification", fake_send)
    monkeypatch.setattr(sleep_checkin_module, "send_push_notification", fake_send)
    # User에 아직 fcm_token 컬럼이 없어서, 발송 경로를 타도록 클래스 속성으로 심는다.
    monkeypatch.setattr(User, "fcm_token", "device-token", raising=False)
    return sent


@pytest.fixture
def sent_telegrams(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(engagement_service, "send_telegram_message", lambda user, text: sent.append(text))
    return sent


def _make_instance(session: Session, language: str) -> EventInstance:
    user = User(name="June", preferred_language=language)
    session.add(user)
    session.flush()
    event = Event(
        user_id=user.id,
        title="Algorithms",
        start_time=datetime(2026, 9, 21, 9, 0),
        end_time=datetime(2026, 9, 21, 10, 0),
    )
    session.add(event)
    session.flush()
    instance = EventInstance(event_id=event.id, date=date(2026, 9, 21), status=EventInstanceStatus.PENDING)
    session.add(instance)
    session.commit()
    return instance


# --- FR-4 시작/종료 알림 ---


@pytest.mark.parametrize(
    ("language", "kind", "expected_body"),
    [
        ("ko", "start", "지금 시작할 시간이에요."),
        ("ko", "end", "종료 시각이에요, 완료 체크 해주세요."),
        ("en", "start", "It's time to start."),
        ("en", "end", "Time's up. Please check it off as done."),
    ],
)
def test_event_notification_uses_user_language(
    engine,
    session: Session,
    sent_pushes: list,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    kind: str,
    expected_body: str,
) -> None:
    monkeypatch.setattr(notification_module, "SessionLocal", sessionmaker(bind=engine))
    instance = _make_instance(session, language)

    notification_module._send_notification(instance.id, kind)

    assert sent_pushes == [("device-token", "[Schreduler] Algorithms", expected_body)]


# --- FR-7 수면 체크인 ---


def test_sleep_checkin_is_sent_in_each_users_language(
    engine, session: Session, sent_pushes: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.add_all([User(name="June", preferred_language="ko"), User(name="Alex", preferred_language="en")])
    session.commit()
    monkeypatch.setattr(sleep_checkin_module, "SessionLocal", sessionmaker(bind=engine))

    sleep_checkin_module.send_daily_sleep_checkin_reminders()

    assert sent_pushes == [
        ("device-token", "[Schreduler] 수면 체크인", "어제 몇 시에 주무셨고 오늘 몇 시에 일어나셨나요?"),
        (
            "device-token",
            "[Schreduler] Sleep check-in",
            "What time did you go to bed last night, and when did you wake up today?",
        ),
    ]


# --- FR-4-1 에스컬레이션 ---


def _escalate(session: Session, language: str, scope: EngagementScope, weeks: int) -> None:
    user = User(name="June", preferred_language=language, telegram_opt_in=True, telegram_chat_id="1")
    session.add(user)
    session.flush()
    ref_event_id = None
    if scope == EngagementScope.EVENT:
        event = Event(
            user_id=user.id,
            title="Gym",
            start_time=datetime(2026, 9, 21, 19, 0),
            end_time=datetime(2026, 9, 21, 20, 0),
        )
        session.add(event)
        session.flush()
        ref_event_id = event.id
    state = get_or_create_engagement_state(session, user.id, scope, ref_event_id)
    state.last_response_at = datetime.utcnow() - timedelta(weeks=weeks)
    session.commit()
    evaluate_escalation(session, state)


@pytest.mark.parametrize(
    ("language", "scope", "weeks", "expected"),
    [
        (
            "ko",
            EngagementScope.EVENT,
            1,
            "[Schreduler] 'Gym' 일정에 대해 1주째 응답이 없어요. 잘 지내고 계신가요?",
        ),
        (
            "en",
            EngagementScope.EVENT,
            1,
            "[Schreduler] We haven't heard from you about your \"Gym\" event for a week. How are you doing?",
        ),
        (
            "en",
            EngagementScope.GLOBAL,
            1,
            "[Schreduler] We haven't heard from you about using the app for a week. How are you doing?",
        ),
        (
            "ko",
            EngagementScope.GLOBAL,
            3,
            "[Schreduler] 앱 사용에 대해 3주째 응답이 없어 더 이상 알림을 보내지 않습니다. "
            "다시 시작하고 싶으면 앱에서 아무 일정이나 완료 체크해주세요.",
        ),
        (
            "en",
            EngagementScope.EVENT,
            3,
            "[Schreduler] We haven't heard from you about your \"Gym\" event for 3 weeks, so we'll stop "
            "sending notifications. To start again, check off any event as done in the app.",
        ),
    ],
)
def test_escalation_message_uses_user_language(
    session: Session,
    sent_telegrams: list[str],
    language: str,
    scope: EngagementScope,
    weeks: int,
    expected: str,
) -> None:
    _escalate(session, language, scope, weeks)

    assert sent_telegrams == [expected]
