"""테스트용 사용자 1명을 DB에 넣는 seed 스크립트.

실행 (프로젝트 루트에서): python -m app.scripts.seed
"""

from app.core.db import SessionLocal
from app.models import User

TEST_USER_NAME = "테스트 사용자"


def seed_test_user() -> User:
    with SessionLocal() as session:
        existing = session.query(User).filter_by(name=TEST_USER_NAME).one_or_none()
        if existing is not None:
            print(f"이미 존재함: id={existing.id}, name={existing.name}")
            return existing

        user = User(name=TEST_USER_NAME, preferred_language="ko")
        session.add(user)
        session.commit()
        session.refresh(user)
        print(f"생성됨: id={user.id}, name={user.name}")
        return user


if __name__ == "__main__":
    seed_test_user()
