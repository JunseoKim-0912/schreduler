from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, User, UserSession
from app.scripts import create_admin
from app.services import auth_service


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([User(name="First"), User(name="Second", email="taken@example.com")])
        session.commit()
    monkeypatch.setattr(create_admin, "SessionLocal", sessionmaker(bind=engine))
    return engine


def _typed(*answers: str) -> Iterator[str]:
    return iter(answers)


def _prompt(answers: Iterator[str]):
    return lambda _label: next(answers)


def _user(engine, user_id: int) -> User:
    with Session(engine) as session:
        return session.get(User, user_id)


def test_makes_user_1_an_admin_who_can_sign_in(engine) -> None:
    code = create_admin.main(["--email", " Me@Example.com "], prompt=_prompt(_typed("long enough pw", "long enough pw")))

    assert code == 0
    user = _user(engine, 1)
    assert (user.email, user.is_admin) == ("me@example.com", True)
    with Session(engine) as session:
        signed_in, _ = auth_service.login(session, "me@example.com", "long enough pw", "127.0.0.1", "en")
        assert signed_in.id == 1


def test_password_is_asked_again_until_long_enough_and_matching(engine, capsys) -> None:
    answers = _typed("short", "long enough pw", "different one!", "long enough pw", "long enough pw")

    create_admin.main(["--email", "me@example.com"], prompt=_prompt(answers))

    assert next(answers, None) is None
    err = capsys.readouterr().err
    assert "At least 8" in err and "differ" in err


def test_the_password_is_never_a_command_line_argument() -> None:
    with pytest.raises(SystemExit):
        create_admin.main(["--email", "me@example.com", "--password", "secret123"], prompt=_prompt(_typed()))


def test_email_of_another_user_is_refused(engine) -> None:
    with pytest.raises(SystemExit, match="already belongs to user 2"):
        create_admin.main(["--email", "TAKEN@example.com"], prompt=_prompt(_typed("long enough pw", "long enough pw")))


def test_reset_changes_the_password_and_signs_out_everywhere(engine) -> None:
    create_admin.main(["--email", "me@example.com"], prompt=_prompt(_typed("first password", "first password")))
    with Session(engine) as session:
        auth_service.login(session, "me@example.com", "first password", "127.0.0.1", "en")

    create_admin.main(["--email", "me@example.com", "--reset"], prompt=_prompt(_typed("second password", "second password")))

    with Session(engine) as session:
        assert session.execute(select(UserSession)).first() is None
        with pytest.raises(auth_service.UnauthorizedError):
            auth_service.login(session, "me@example.com", "first password", "127.0.0.1", "en")
        assert auth_service.login(session, "me@example.com", "second password", "127.0.0.1", "en")[0].id == 1


def test_reset_for_an_unknown_email_fails(engine) -> None:
    with pytest.raises(SystemExit, match="no user"):
        create_admin.main(["--email", "nobody@example.com", "--reset"], prompt=_prompt(_typed("whatever123", "whatever123")))
