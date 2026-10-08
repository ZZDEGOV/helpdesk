"""Backups: a snapshot restores to the same data, and restore won't silently
overwrite a database that has tickets in it."""
from contextlib import closing

import pytest

from app import backup, repo
from app.db import connect
from app.migrate import run as migrate


@pytest.fixture
def conn():
    migrate(verbose=False)
    with closing(connect()) as c:
        for table in ("events", "tags", "tickets", "customers"):
            c.execute(f"DELETE FROM {table}")
        c.commit()
        yield c


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]


def test_snapshot_round_trips(conn):
    repo.create_ticket(conn, subject="Keep me")
    conn.commit()
    snapshot = backup.create()

    conn.execute("DELETE FROM tickets")
    conn.commit()
    assert backup.restore(snapshot) == 1
    assert repo.list_tickets(conn, status=None)[0]["subject"] == "Keep me"


def test_restore_refuses_to_overwrite_tickets(conn):
    repo.create_ticket(conn, subject="Existing")
    conn.commit()
    snapshot = backup.create()
    with pytest.raises(SystemExit):
        backup.restore(snapshot)
    assert backup.restore(snapshot, force=True) == 1


def test_old_snapshots_are_pruned(conn):
    for _ in range(3):
        backup.create(keep=2)
    assert len(list(backup.backup_dir().glob("helpdesk-*.db"))) <= 2
