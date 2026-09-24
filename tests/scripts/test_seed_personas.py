import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Persona
from app.scripts.seed_personas import PersonaSeedError, load_personas, upsert_personas


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "personas.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _persona(name: str, **overrides: object) -> dict[str, object]:
    persona: dict[str, object] = {
        "name": name,
        "display_name": {"ko": f"{name}-ko", "en": f"{name}-en"},
        "description": {"ko": "설명", "en": "description"},
    }
    persona.update(overrides)
    return persona


def test_missing_file_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(PersonaSeedError, match="찾을 수 없습니다"):
        load_personas(tmp_path / "nope.json")


def test_invalid_shape_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, [_persona("A", display_name={"ko": "에이"})])
    with pytest.raises(PersonaSeedError, match="형식 오류"):
        load_personas(path)


def test_duplicate_names_raise(tmp_path: Path) -> None:
    path = _write(tmp_path, [_persona("A"), _persona("A")])
    with pytest.raises(PersonaSeedError, match="중복"):
        load_personas(path)


def test_upsert_creates_then_updates(tmp_path: Path, session: Session) -> None:
    example_lines = {
        "ko": [{"situation": "지각", "line": "늦었군."}],
        "en": [{"situation": "late", "line": "You're late."}],
    }
    first = load_personas(_write(tmp_path, [_persona("A"), _persona("B", example_lines=example_lines)]))
    created, updated = upsert_personas(session, first)
    assert (created, updated) == (["A", "B"], [])

    b = session.get(Persona, "B")
    assert b is not None
    assert b.example_lines == example_lines
    assert b.backstory is None

    backstory = {"ko": "과거", "en": "past"}
    second = load_personas(_write(tmp_path, [_persona("B", backstory=backstory), _persona("C")]))
    created, updated = upsert_personas(session, second)
    assert (created, updated) == (["C"], ["B"])

    session.expire_all()
    b = session.get(Persona, "B")
    assert b is not None
    assert b.backstory == backstory
    assert b.example_lines is None
    assert session.query(Persona).count() == 3


def test_real_seed_file_is_valid() -> None:
    from app.scripts.seed_personas import DEFAULT_SEED_PATH

    assert load_personas(DEFAULT_SEED_PATH)
