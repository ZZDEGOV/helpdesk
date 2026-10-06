"""Accounts, roles, and the status-extensibility guarantee."""
import os
import tempfile

from app import auth, config
import pytest

os.environ["HELPDESK_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "auth_test.db")

from app import repo          # noqa: E402
from app.db import connect                  # noqa: E402
from app.migrate import run as migrate      # noqa: E402


@pytest.fixture
def conn():
    migrate(verbose=False)
    c = connect()
    for table in ("sessions", "audit_log", "events", "tickets", "customers", "users"):
        c.execute(f"DELETE FROM {table}")
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------- passwords

def test_hash_is_salted_and_verifiable():
    h1, s1 = auth.hash_password("correct horse")
    h2, s2 = auth.hash_password("correct horse")
    assert s1 != s2 and h1 != h2          # same password, different salts
    assert auth.verify_password("correct horse", h1, s1)
    assert not auth.verify_password("wrong", h1, s1)


def test_verify_rejects_missing_hash():
    assert not auth.verify_password("anything", None, None)


# ---------------------------------------------------------------- accounts

def test_usernames_are_unique_case_insensitively(conn):
    auth.create_user(conn, username="ZachZ", display_name="Zach", password="pw123456")
    conn.commit()
    with pytest.raises(ValueError):
        auth.create_user(conn, username="zachz", display_name="dup", password="pw123456")


def test_lookup_is_case_insensitive(conn):
    auth.create_user(conn, username="ZachZ", display_name="Zach", password="pw123456")
    conn.commit()
    assert auth.get_user_by_name(conn, "zachz")["username"] == "ZachZ"


def test_created_user_must_change_password(conn):
    uid = auth.create_user(conn, username="new", display_name="", password="temp1234")
    conn.commit()
    assert auth.get_user(conn, uid)["must_change_password"] == 1

    auth.set_password(conn, uid, "chosen123", must_change=False)
    conn.commit()
    assert auth.get_user(conn, uid)["must_change_password"] == 0


def test_unknown_role_rejected(conn):
    with pytest.raises(ValueError):
        auth.create_user(conn, username="x", display_name="", password="pw123456",
                         role="superuser")


# ---------------------------------------------------------------- login

def test_authenticate_happy_path(conn):
    auth.create_user(conn, username="ZachZ", display_name="Zach", password="pw123456")
    conn.commit()
    user, err = auth.authenticate(conn, "ZachZ", "pw123456")
    assert user and not err


def test_error_message_does_not_reveal_whether_user_exists(conn):
    auth.create_user(conn, username="ZachZ", display_name="", password="pw123456")
    conn.commit()
    _, missing = auth.authenticate(conn, "nobody", "whatever")
    _, wrong = auth.authenticate(conn, "ZachZ", "whatever")
    assert missing == wrong


def test_lockout_after_repeated_failures(conn):
    auth.create_user(conn, username="ZachZ", display_name="", password="pw123456")
    conn.commit()
    for _ in range(config.MAX_FAILED_LOGINS):
        auth.authenticate(conn, "ZachZ", "nope")
    conn.commit()
    user, err = auth.authenticate(conn, "ZachZ", "pw123456")   # correct password
    assert user is None and "Too many" in err


def test_disabled_account_cannot_log_in(conn):
    uid = auth.create_user(conn, username="gone", display_name="", password="pw123456")
    auth.update_user(conn, uid, is_active=False)
    conn.commit()
    user, err = auth.authenticate(conn, "gone", "pw123456")
    assert user is None and "disabled" in err


def test_disabling_kills_existing_sessions(conn):
    uid = auth.create_user(conn, username="gone", display_name="", password="pw123456")
    token = auth.start_session(conn, uid)
    conn.commit()
    assert auth.session_user(conn, token) is not None

    auth.update_user(conn, uid, is_active=False)
    conn.commit()
    assert auth.session_user(conn, token) is None


def test_expired_session_is_rejected(conn):
    uid = auth.create_user(conn, username="u", display_name="", password="pw123456")
    token = auth.start_session(conn, uid)
    conn.execute("UPDATE sessions SET expires_at = '2020-01-01T00:00:00+00:00'"
                 " WHERE token = ?", (token,))
    conn.commit()
    assert auth.session_user(conn, token) is None


# ---------------------------------------------------------------- statuses
#
# These guard the bug where a new status was added to config but tickets using it
# silently vanished from the default view, because the status list was also
# hardcoded inside SQL strings.

def test_waiting_external_exists():
    assert "waiting_external" in config.STATUSES
    assert "waiting_external" in config.STATUS_LABELS
    assert "waiting_external" in config.OPEN_STATUSES
    assert "waiting_external" not in config.ACTIVE_STATUSES   # clock must pause


def test_every_status_is_labelled():
    assert set(config.STATUSES) == set(config.STATUS_LABELS)


def test_status_sets_are_subsets_of_statuses():
    assert config.OPEN_STATUSES <= set(config.STATUSES)
    assert config.ACTIVE_STATUSES <= config.OPEN_STATUSES


def test_status_sql_is_generated_from_config():
    """If someone adds a status to config, the SQL filters must follow."""
    for status in config.OPEN_STATUSES:
        assert f"'{status}'" in repo.OPEN_SQL
    for status in config.ACTIVE_STATUSES:
        assert f"'{status}'" in repo.ACTIVE_SQL


def test_every_open_status_appears_in_the_open_filter(conn):
    """The regression that broke waiting_external the first time."""
    for status in config.OPEN_STATUSES:
        tid = repo.create_ticket(conn, subject=f"Ticket in {status}")
        repo.set_status(conn, tid, status)
        conn.commit()
        listed = [t["id"] for t in repo.list_tickets(conn, status="open")]
        assert tid in listed, f"{status} is missing from the Open filter"


def test_unknown_status_is_rejected(conn):
    tid = repo.create_ticket(conn, subject="x")
    conn.commit()
    with pytest.raises(ValueError):
        repo.set_status(conn, tid, "not_a_status")


def test_waiting_external_pauses_the_clock(conn):
    tid = repo.create_ticket(conn, subject="Bytemark refund")
    repo.set_status(conn, tid, "waiting_external")
    conn.commit()
    frozen = repo.get_ticket(conn, tid)["active_seconds"]
    assert repo.live_active_seconds(repo.get_ticket(conn, tid)) == frozen


def test_vendor_reply_clears_external_wait(conn):
    tid = repo.create_ticket(conn, subject="Bytemark refund")
    repo.set_status(conn, tid, "waiting_external")
    conn.commit()
    repo.log_message(conn, tid, "in_vendor", "Refund processed.")
    conn.commit()
    assert repo.get_ticket(conn, tid)["status"] == "open"


# ---------------------------------------------------------------- attribution

def test_events_record_the_acting_user(conn):
    uid = auth.create_user(conn, username="ZachZ", display_name="Zach", password="pw123456")
    conn.commit()
    token = repo.CURRENT_USER_ID.set(uid)
    try:
        tid = repo.create_ticket(conn, subject="Attributed")
        repo.log_message(conn, tid, "note", "did a thing")
        conn.commit()
        assert all(e["user_id"] == uid for e in repo.get_events(conn, tid))
    finally:
        repo.CURRENT_USER_ID.reset(token)


def test_per_user_stats_exclude_root(conn):
    auth.create_user(conn, username="root", display_name="", password="pw123456",
                     role="root")
    auth.create_user(conn, username="ZachZ", display_name="Zach", password="pw123456",
                     role="admin")
    conn.commit()
    assert [r["user"]["username"] for r in repo.per_user_stats(conn)] == ["ZachZ"]
