"""Container start, step 1: make sure the folders for the SQLite file and its backups exist and belong to the app user.

Railway mounts a volume as root, while the server runs as an unprivileged user, so the entrypoint runs this as root
and only then drops privileges. Without root (e.g. RAILWAY_RUN_UID set to a non-root user) it just creates what it can.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.core.config import settings
from app.services.db_backup import backup_dir, sqlite_path

APP_UID = APP_GID = 1000


def prepare(database_url: str) -> list[Path]:
    db_file = sqlite_path(database_url)
    if db_file is None:
        return []
    folders = [db_file.parent.resolve()]
    backups = backup_dir(database_url)
    if backups is not None:
        folders.append(backups.resolve())
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(folder, APP_UID, APP_GID)
            if db_file.exists() and db_file.parent.resolve() == folder:
                for path in folder.glob(f"{db_file.name}*"):  # the db and its -wal/-shm files
                    os.chown(path, APP_UID, APP_GID)
    return folders


def main() -> int:
    for folder in prepare(settings.database_url):
        print(f"[prepare_volume] ready: {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
