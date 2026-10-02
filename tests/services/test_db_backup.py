from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.core.config import settings
from app.scripts import backup_db
from app.services import db_backup


@pytest.fixture
def database(tmp_path: Path) -> str:
    path = tmp_path / "data" / "schreduler.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany("INSERT INTO notes (body) VALUES (?)", [("a",), ("b",)])
    return f"sqlite:///{path.as_posix()}"


def _rows(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT body FROM notes ORDER BY id")]


def test_backup_is_a_complete_readable_copy(database: str, tmp_path: Path) -> None:
    written = db_backup.backup_sqlite(database, tmp_path / "backups", date(2026, 10, 1))

    assert written == tmp_path / "backups" / "schreduler-2026-10-01.db"
    assert _rows(written) == ["a", "b"]
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_backup_while_another_connection_holds_a_write_transaction(database: str, tmp_path: Path) -> None:
    source = Path(database.removeprefix("sqlite:///"))
    writer = sqlite3.connect(source)
    writer.execute("INSERT INTO notes (body) VALUES ('uncommitted')")  # open transaction, not committed
    try:
        written = db_backup.backup_sqlite(database, tmp_path / "backups", date(2026, 10, 1))
    finally:
        writer.rollback()
        writer.close()

    assert _rows(written) == ["a", "b"]


def test_only_the_newest_14_are_kept_and_a_same_day_rerun_replaces(database: str, tmp_path: Path) -> None:
    target = tmp_path / "backups"
    start = date(2026, 9, 1)
    for offset in range(20):
        db_backup.backup_sqlite(database, target, start + timedelta(days=offset))
    db_backup.backup_sqlite(database, target, start + timedelta(days=19))

    names = sorted(p.name for p in target.glob("*.db"))
    assert len(names) == 14
    assert names[0] == "schreduler-2026-09-07.db" and names[-1] == "schreduler-2026-09-20.db"


def test_other_databases_are_skipped(tmp_path: Path) -> None:
    assert db_backup.backup_sqlite("postgresql+psycopg://u:p@localhost/db", tmp_path, date(2026, 10, 1)) is None
    assert db_backup.backup_sqlite("sqlite:///:memory:", tmp_path, date(2026, 10, 1)) is None
    assert db_backup.backup_dir("postgresql+psycopg://u:p@localhost/db") is None


def test_default_folder_is_next_to_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "backup_dir", None)
    assert db_backup.backup_dir("sqlite:////data/schreduler.db") == Path("/data/backups")
    monkeypatch.setattr(settings, "backup_dir", "/somewhere/else")
    assert db_backup.backup_dir("sqlite:////data/schreduler.db") == Path("/somewhere/else")


def test_manual_script_writes_a_backup(database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(settings, "database_url", database)
    monkeypatch.setattr(settings, "backup_dir", None)

    assert backup_db.main([]) == 0

    written = list((tmp_path / "data" / "backups").glob("schreduler-*.db"))
    assert len(written) == 1 and _rows(written[0]) == ["a", "b"]
    assert "Backed up to" in capsys.readouterr().out


def test_job_logs_instead_of_raising(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{(tmp_path / 'missing.db').as_posix()}")
    monkeypatch.setattr(settings, "backup_dir", None)

    db_backup.run_backup_job()

    assert "does not exist" in caplog.text
