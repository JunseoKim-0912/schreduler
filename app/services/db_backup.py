"""Daily SQLite backups next to the database (on Railway: /data/backups on the same volume).

sqlite3's backup API copies a consistent snapshot even while the server is writing, which a plain file copy of a
live database doesn't guarantee. Only the newest KEEP files are kept.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.clock import local_today
from app.core.config import settings
from app.core.scheduler import scheduler

logger = logging.getLogger(__name__)

KEEP = 14
BACKUP_HOUR = 4  # APP_TIMEZONE; after the midnight points job, before anyone is up
BACKUP_MINUTE = 30
BACKUP_JOB_ID = "sqlite_backup"
PREFIX = "schreduler-"


def sqlite_path(database_url: str) -> Path | None:
    """File path of a SQLite URL, or None for other databases (Postgres has its own backups)."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def backup_dir(database_url: str) -> Path | None:
    if settings.backup_dir:
        return Path(settings.backup_dir)
    path = sqlite_path(database_url)
    return path.parent / "backups" if path is not None else None


def backup_sqlite(database_url: str, target_dir: Path, day: date, keep: int = KEEP) -> Path | None:
    """Snapshot the database to <target_dir>/schreduler-<day>.db (a rerun on the same day replaces it) and delete
    all but the newest `keep` snapshots. Returns the new file, or None when the database isn't a SQLite file."""
    source_path = sqlite_path(database_url)
    if source_path is None:
        logger.info("[backup] DATABASE_URL is not a SQLite file — skipped")
        return None
    if not source_path.exists():
        logger.warning("[backup] %s does not exist — skipped", source_path)
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{PREFIX}{day.isoformat()}.db"
    partial = target.with_suffix(".db.partial")
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(partial)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    # Swap in only a finished copy, so a crash mid-backup never leaves a truncated file under the real name.
    partial.replace(target)
    for old in sorted(target_dir.glob(f"{PREFIX}*.db"), reverse=True)[keep:]:
        old.unlink()
    logger.info("[backup] wrote %s (%d bytes)", target, target.stat().st_size)
    return target


def run_backup_job() -> None:
    target_dir = backup_dir(settings.database_url)
    if target_dir is None:
        logger.info("[backup] DATABASE_URL is not a SQLite file — skipped")
        return
    try:
        backup_sqlite(settings.database_url, target_dir, local_today())
    except Exception:
        logger.exception("[backup] failed")


def register_backup_job() -> None:
    scheduler.add_job(
        run_backup_job,
        trigger="cron",
        hour=BACKUP_HOUR,
        minute=BACKUP_MINUTE,
        id=BACKUP_JOB_ID,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
