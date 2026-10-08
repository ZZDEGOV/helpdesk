"""Database backups, using SQLite's online backup API.

Safe while the app is running: the copy is a consistent snapshot, including
anything still sitting in the WAL file (which a plain file copy can miss).

    python -m app.backup create            snapshot into the backup directory
    python -m app.backup restore FILE      load FILE into an empty database
    python -m app.backup restore FILE --force   ...or over an existing one

The app also takes one snapshot a day on its own (see main.py).
"""
import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .db import connect


def backup_dir() -> Path:
    path = config.BACKUP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def create(keep: int | None = None) -> Path:
    """Write a snapshot and prune all but the `keep` most recent."""
    keep = config.BACKUP_KEEP if keep is None else keep
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir() / f"helpdesk-{stamp}.db"
    with closing(connect()) as src, closing(sqlite3.connect(dest)) as out:
        src.backup(out)
    snapshots = sorted(backup_dir().glob("helpdesk-*.db"), reverse=True)
    for old in snapshots[keep:]:
        old.unlink()
    return dest


def latest_age() -> timedelta | None:
    snapshots = sorted(backup_dir().glob("helpdesk-*.db"))
    if not snapshots:
        return None
    modified = datetime.fromtimestamp(snapshots[-1].stat().st_mtime, tz=timezone.utc)
    return datetime.now(timezone.utc) - modified


def restore(source: Path, force: bool = False) -> int:
    """Replace the live database's contents with `source`. Returns the number of
    tickets restored. Refuses to overwrite a database that has tickets unless
    forced -- stop the app first if you do force it."""
    if not source.is_file():
        raise SystemExit(f"No such file: {source}")
    with closing(connect()) as conn:
        try:
            existing = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        except sqlite3.OperationalError:
            existing = 0
        if existing and not force:
            raise SystemExit(f"The current database has {existing} ticket(s). "
                             "Re-run with --force to overwrite it.")
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src:
            src.backup(conn)
        return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.backup", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="snapshot the database")
    r = sub.add_parser("restore", help="load a snapshot into the database")
    r.add_argument("file", type=Path)
    r.add_argument("--force", action="store_true", help="overwrite existing tickets")
    args = parser.parse_args(argv)

    if args.command == "create":
        print(f"  backed up to {create()}")
    else:
        count = restore(args.file, force=args.force)
        print(f"  restored {count} ticket(s) into {config.DB_PATH}")
        print("  run migrations next if the snapshot is from an older version:"
              " python -m app.migrate")


if __name__ == "__main__":
    sys.exit(main())
