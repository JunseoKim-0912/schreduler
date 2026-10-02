"""Give an existing user (default: user 1) an email and password and make them an admin, or reset a password.

    python -m app.scripts.create_admin --email you@example.com              # user 1 becomes an admin
    python -m app.scripts.create_admin --email you@example.com --user-id 3  # another existing user
    python -m app.scripts.create_admin --email you@example.com --reset      # new password for that email
    python -m app.scripts.create_admin --email you@example.com --new        # empty database: create the first admin

The password is read from the terminal (twice, not echoed) — never from the command line, where it would end up in
shell history and the process list. Setting a password signs that user out everywhere.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.user import User
from app.services import auth_service

MIN_PASSWORD_LENGTH = 8


def ask_password(prompt: Callable[[str], str] = getpass.getpass) -> str:
    while True:
        password = prompt("New password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"At least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
            continue
        if prompt("Repeat it: ") != password:
            print("The two entries differ, try again.", file=sys.stderr)
            continue
        return password


def make_admin(db: Session, email: str, user_id: int, password: str) -> User:
    email = auth_service.normalize_email(email)
    user = db.get(User, user_id)
    if user is None:
        raise SystemExit(f"user {user_id} does not exist (on a new database, use --new to create the first admin)")
    taken = db.execute(select(User).where(User.email == email, User.id != user_id)).scalar_one_or_none()
    if taken is not None:
        raise SystemExit(f"{email} already belongs to user {taken.id}")
    user.email = email
    user.is_admin = True
    auth_service.set_password(db, user, password)
    db.commit()
    return user


def create_new_admin(db: Session, email: str, password: str) -> User:
    email = auth_service.normalize_email(email)
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none() is not None:
        raise SystemExit(f"{email} already has an account (use --reset to change its password)")
    user = User(name=email.split("@")[0][:100], email=email, is_admin=True, preferred_language="ko")
    db.add(user)
    db.flush()
    auth_service.set_password(db, user, password)
    db.commit()
    return user


def reset_password(db: Session, email: str, password: str) -> User:
    user = db.execute(select(User).where(User.email == auth_service.normalize_email(email))).scalar_one_or_none()
    if user is None:
        raise SystemExit(f"no user with email {email}")
    auth_service.set_password(db, user, password)
    db.commit()
    return user


def main(argv: list[str] | None = None, prompt: Callable[[str], str] = getpass.getpass) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--user-id", type=int, default=1, help="existing user to attach the email to (default 1)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reset", action="store_true", help="only set a new password for the user with this email")
    mode.add_argument("--new", action="store_true", help="create a new admin account with this email (empty database)")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.reset:
            user = reset_password(db, args.email, ask_password(prompt))
            print(f"Password reset for user {user.id} ({user.email}). Existing sessions were signed out.")
        elif args.new:
            user = create_new_admin(db, args.email, ask_password(prompt))
            print(f"Created admin user {user.id} ({user.email}).")
        else:
            user = make_admin(db, args.email, args.user_id, ask_password(prompt))
            print(f"User {user.id} can now sign in as {user.email} (admin).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
