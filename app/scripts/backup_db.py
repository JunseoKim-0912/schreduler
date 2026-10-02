"""Back up the SQLite database now (the same thing the daily 04:30 job does).

    python -m app.scripts.backup_db                 # into BACKUP_DIR, or "backups" next to the database file
    python -m app.scripts.backup_db --dir ./snap    # somewhere else

On Railway: railway ssh -- python -m app.scripts.backup_db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.clock import local_today
from app.core.config import settings
from app.services.db_backup import KEEP, backup_dir, backup_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, help="target folder (default: BACKUP_DIR or <db folder>/backups)")
    parser.add_argument("--keep", type=int, default=KEEP, help=f"how many snapshots to keep (default {KEEP})")
    args = parser.parse_args(argv)

    target_dir = args.dir or backup_dir(settings.database_url)
    if target_dir is None:
        print("DATABASE_URL is not a SQLite file; nothing to back up.", file=sys.stderr)
        return 1
    written = backup_sqlite(settings.database_url, target_dir, local_today(), keep=args.keep)
    if written is None:
        print("Nothing was backed up (see the log above).", file=sys.stderr)
        return 1
    print(f"Backed up to {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
