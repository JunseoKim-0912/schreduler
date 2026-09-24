"""personas_seed_data.json의 페르소나를 DB에 upsert하는 seed 스크립트 (FR-9).

페르소나 내용은 JSON 파일에서만 가져온다. 파일이 없거나 형식이 잘못되면 아무것도 쓰지 않고 종료한다.

실행 (프로젝트 루트에서): python -m app.scripts.seed_personas [JSON 경로]
"""

import json
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import Persona
from app.schemas.persona import PersonaCreate

DEFAULT_SEED_PATH = Path(__file__).with_name("personas_seed_data.json")


class PersonaSeedError(Exception):
    pass


def load_personas(path: Path) -> list[PersonaCreate]:
    if not path.is_file():
        raise PersonaSeedError(
            f"페르소나 seed 파일을 찾을 수 없습니다: {path.resolve()}\n"
            "페르소나 내용은 이 JSON 파일에서만 읽어옵니다. 파일을 작성한 뒤 다시 실행하세요."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PersonaSeedError(f"JSON 파싱 실패 ({path}): {exc}") from exc

    try:
        personas = TypeAdapter(list[PersonaCreate]).validate_python(raw)
    except ValidationError as exc:
        raise PersonaSeedError(f"페르소나 형식 오류 ({path}):\n{exc}") from exc

    names = [p.name for p in personas]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise PersonaSeedError(f"중복된 페르소나 name이 있습니다 ({path}): {', '.join(duplicates)}")

    return personas


def upsert_personas(session: Session, personas: list[PersonaCreate]) -> tuple[list[str], list[str]]:
    created: list[str] = []
    updated: list[str] = []

    for data in personas:
        values = data.model_dump(exclude={"name"})
        persona = session.get(Persona, data.name)
        if persona is None:
            session.add(Persona(name=data.name, **values))
            created.append(data.name)
        else:
            for field, value in values.items():
                setattr(persona, field, value)
            updated.append(data.name)

    session.commit()
    return created, updated


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_SEED_PATH

    try:
        personas = load_personas(path)
    except PersonaSeedError as exc:
        print(f"[seed_personas] 오류: {exc}", file=sys.stderr)
        return 1

    with SessionLocal() as session:
        created, updated = upsert_personas(session, personas)

    print(f"생성됨 ({len(created)}): {', '.join(created) or '-'}")
    print(f"업데이트됨 ({len(updated)}): {', '.join(updated) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
