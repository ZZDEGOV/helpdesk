"""Core logic tests. The SLA clock is the part most worth guarding."""
from datetime import datetime, timedelta, timezone

import pytest

from app import repo
from app.db import connect, init_db


@pytest.fixture
def conn():
    init_db()
    c = connect()
    c.execute("DELETE FROM events")
    c.execute("DELETE FROM tags")
    c.execute("DELETE FROM tickets")
    c.execute("DELETE FROM customers")
    c.commit()
    yield c
    c.close()


def _mk(conn, **kw):
    kw.setdefault("subject", "Test issue")
    tid = repo.create_ticket(conn, **kw)
    conn.commit()
    return tid


# ---------------------------------------------------------------- refs

def test_refs_are_sequential_and_unique(conn):
    refs = {repo.get_ticket(conn, _mk(conn))["ref"] for _ in range(5)}
    assert len(refs) == 5
    year = datetime.now(timezone.utc).year
    assert all(r.startswith(f"HD-{year}-") for r in refs)


# ---------------------------------------------------------------- clock

def test_clock_accrues_while_active(conn):
    tid = _mk(conn)
    conn.execute("UPDATE tickets SET last_status_at = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), tid))
    conn.commit()
    assert 7000 < repo.live_active_seconds(repo.get_ticket(conn, tid)) < 7400


def test_clock_pauses_when_waiting(conn):
    """The whole point: waiting on someone else must not burn SLA time."""
    tid = _mk(conn)
    conn.execute("UPDATE tickets SET last_status_at = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(), tid))
    conn.commit()

    repo.set_status(conn, tid, "waiting_customer")
    conn.commit()
    frozen = repo.get_ticket(conn, tid)["active_seconds"]
    assert 10600 < frozen < 11000

    # backdate again — a paused ticket must not accrue any more
    conn.execute("UPDATE tickets SET last_status_at = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(), tid))
    conn.commit()
    assert repo.live_active_seconds(repo.get_ticket(conn, tid)) == frozen


def test_clock_resumes_on_reopen(conn):
    tid = _mk(conn)
    repo.set_status(conn, tid, "waiting_internal")
    conn.commit()
    before = repo.get_ticket(conn, tid)["active_seconds"]

    repo.set_status(conn, tid, "open")
    conn.execute("UPDATE tickets SET last_status_at = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), tid))
    conn.commit()
    assert repo.live_active_seconds(repo.get_ticket(conn, tid)) > before + 3400


# ---------------------------------------------------------------- lifecycle

def test_resolve_and_reopen(conn):
    tid = _mk(conn)
    repo.set_status(conn, tid, "resolved")
    conn.commit()
    t = repo.get_ticket(conn, tid)
    assert t["resolved_at"] and t["reopen_count"] == 0

    repo.set_status(conn, tid, "open")
    conn.commit()
    t = repo.get_ticket(conn, tid)
    assert t["reopen_count"] == 1 and t["resolved_at"] is None


def test_status_change_is_logged(conn):
    tid = _mk(conn)
    repo.set_status(conn, tid, "open")
    conn.commit()
    types = [e["type"] for e in repo.get_events(conn, tid)]
    assert "created" in types and "status_change" in types


def test_noop_status_change_logs_nothing(conn):
    tid = _mk(conn)
    n = len(repo.get_events(conn, tid))
    repo.set_status(conn, tid, "new")
    conn.commit()
    assert len(repo.get_events(conn, tid)) == n


def test_overdue_only_counts_open_tickets(conn):
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    tid = _mk(conn, due_at=past)
    assert repo.is_overdue(repo.get_ticket(conn, tid))

    repo.set_status(conn, tid, "closed")
    conn.commit()
    assert not repo.is_overdue(repo.get_ticket(conn, tid))


def test_first_reply_is_the_earliest_one(conn):
    tid = _mk(conn)
    repo.log_message(conn, tid, "out_customer", "First response")
    conn.commit()
    first = repo.get_ticket(conn, tid)["first_reply_at"]

    repo.log_message(conn, tid, "out_customer", "Second response")
    conn.commit()
    assert repo.get_ticket(conn, tid)["first_reply_at"] == first


# ---------------------------------------------------------------- customers

def test_same_email_reuses_customer(conn):
    a = _mk(conn, customer_email="Rider@Example.com", customer_name="Test Rider")
    b = _mk(conn, customer_email="rider@example.com", subject="Second issue")
    assert repo.get_ticket(conn, a)["customer_id"] == repo.get_ticket(conn, b)["customer_id"]


def test_upsert_fills_blanks_but_keeps_edits(conn):
    _mk(conn, customer_email="r@example.com", customer_name="Original")
    _mk(conn, customer_email="r@example.com", customer_name="Different", customer_phone="555")
    c = conn.execute("SELECT * FROM customers WHERE email='r@example.com'").fetchone()
    assert c["name"] == "Original"     # not clobbered
    assert c["phone"] == "555"         # blank field filled


# ---------------------------------------------------------------- filters

def test_search_matches_ref_and_subject(conn):
    _mk(conn, subject="Bytemark receipt missing")
    _mk(conn, subject="Bus late on Route 15")
    assert len(repo.list_tickets(conn, q="Bytemark")) == 1
    assert len(repo.list_tickets(conn, q="Route 15")) == 1


def test_actionable_excludes_waiting(conn):
    _mk(conn)
    tid = _mk(conn)
    repo.set_status(conn, tid, "waiting_customer")
    conn.commit()
    assert len(repo.list_tickets(conn, status="actionable")) == 1
    assert len(repo.list_tickets(conn, status="open")) == 2


# ---------------------------------------------------------------- templates

def test_placeholders_render(conn):
    tid = _mk(conn, customer_email="r@example.com", customer_name="Estanislao Laureta")
    t = repo.get_ticket(conn, tid)
    out = repo.render_template("Hi {{first_name}}, ref {{ref}}.", t)
    assert "Estanislao" in out and t["ref"] in out and "{{" not in out


def test_unknown_placeholder_stays_visible(conn):
    """Better to see {{oops}} than to send a sentence with a hole in it."""
    t = repo.get_ticket(conn, _mk(conn))
    assert "{{oops}}" in repo.render_template("Hi {{oops}}", t)


# ---------------------------------------------------------------- tags

def test_tags_are_deduped(conn):
    tid = _mk(conn)
    repo.add_tag(conn, tid, "bytemark")
    repo.add_tag(conn, tid, "Bytemark")
    conn.commit()
    assert repo.get_tags(conn, tid) == ["bytemark"]


# ---------------------------------------------------------------- inbound

def test_inbound_reply_resumes_clock(conn):
    """A customer writing back means the ball is ours again."""
    tid = _mk(conn)
    repo.set_status(conn, tid, "waiting_customer")
    conn.commit()
    repo.log_message(conn, tid, "in_customer", "Still not working.")
    conn.commit()
    t = repo.get_ticket(conn, tid)
    assert t["status"] == "open"
    assert "in_customer" in [e["type"] for e in repo.get_events(conn, tid)]


def test_inbound_does_not_disturb_other_statuses(conn):
    """Waiting on an internal team isn't resolved by the customer nudging us."""
    tid = _mk(conn)
    repo.set_status(conn, tid, "waiting_internal")
    conn.commit()
    repo.log_message(conn, tid, "in_customer", "Any update?")
    conn.commit()
    assert repo.get_ticket(conn, tid)["status"] == "waiting_internal"


def test_empty_message_is_ignored(conn):
    tid = _mk(conn)
    n = len(repo.get_events(conn, tid))
    repo.log_message(conn, tid, "in_customer", "   ")
    repo.log_message(conn, tid, "bogus_kind", "real text")
    conn.commit()
    assert len(repo.get_events(conn, tid)) == n


# ---------------------------------------------------------------- v1.2

def test_message_kinds_and_auto_status(conn):
    tid = _mk(conn)
    repo.set_status(conn, tid, "waiting_customer")
    conn.commit()
    repo.log_message(conn, tid, "in_customer", "Here are the details you asked for.")
    conn.commit()
    assert repo.get_ticket(conn, tid)["status"] == "open"

    repo.set_status(conn, tid, "waiting_internal")
    conn.commit()
    repo.log_message(conn, tid, "in_internal", "Dev team: reproduced it.")
    conn.commit()
    assert repo.get_ticket(conn, tid)["status"] == "open"


def test_internal_reply_does_not_unblock_customer_wait(conn):
    tid = _mk(conn)
    repo.set_status(conn, tid, "waiting_customer")
    conn.commit()
    repo.log_message(conn, tid, "in_internal", "Dev team poking at it.")
    conn.commit()
    assert repo.get_ticket(conn, tid)["status"] == "waiting_customer"


def test_voiding_a_reply_corrects_first_reply_time(conn):
    """The whole point of striking through instead of deleting."""
    tid = _mk(conn)
    eid = repo.log_message(conn, tid, "out_customer", "Oops, wrong ticket.")
    conn.commit()
    assert repo.get_ticket(conn, tid)["first_reply_at"] is not None

    repo.void_event(conn, eid, "logged against the wrong ticket")
    conn.commit()
    assert repo.get_ticket(conn, tid)["first_reply_at"] is None

    repo.unvoid_event(conn, eid)
    conn.commit()
    assert repo.get_ticket(conn, tid)["first_reply_at"] is not None


def test_events_order_by_occurred_not_created(conn):
    tid = _mk(conn)
    late = repo.log_message(conn, tid, "note", "Logged second, happened first")
    repo.log_message(conn, tid, "note", "Logged third, happened second")
    conn.commit()
    repo.set_event_time(conn, late, "2020-01-01T00:00:00+00:00")
    conn.commit()
    bodies = [e["body"] for e in repo.get_events(conn, tid)]
    assert bodies[0] == "Logged second, happened first"


def test_soft_delete_hides_ticket(conn):
    tid = _mk(conn)
    repo.soft_delete_ticket(conn, tid)
    conn.commit()
    assert not any(t["id"] == tid for t in repo.list_tickets(conn, status=None))
    assert any(t["id"] == tid for t in repo.list_deleted(conn)["tickets"])
    repo.restore_ticket(conn, tid)
    conn.commit()
    assert any(t["id"] == tid for t in repo.list_tickets(conn, status=None))


def test_deleting_customer_hides_their_tickets(conn):
    tid = _mk(conn, customer_email="gone@example.com")
    cid = repo.get_ticket(conn, tid)["customer_id"]
    repo.soft_delete_customer(conn, cid)
    conn.commit()
    assert not repo.list_tickets(conn, status=None)
    repo.restore_customer(conn, cid)
    conn.commit()
    assert len(repo.list_tickets(conn, status=None)) == 1


def test_purge_respects_retention_window(conn):
    tid = _mk(conn)
    repo.soft_delete_ticket(conn, tid)
    conn.commit()
    assert repo.purge_expired(conn) == (0, 0)          # inside the window

    old = (datetime.now(timezone.utc) - timedelta(days=99)).isoformat()
    conn.execute("UPDATE tickets SET deleted_at = ? WHERE id = ?", (old, tid))
    conn.commit()
    assert repo.purge_expired(conn)[1] == 1
    conn.commit()
    assert repo.get_ticket(conn, tid) is None


def test_multiple_emails_resolve_to_one_customer(conn):
    tid = _mk(conn, customer_email="rider@example.com", customer_name="Rider")
    cid = repo.get_ticket(conn, tid)["customer_id"]
    repo.add_email(conn, cid, "rider.alt@example.com")
    conn.commit()
    tid2 = _mk(conn, customer_email="rider.alt@example.com", subject="Second")
    assert repo.get_ticket(conn, tid2)["customer_id"] == cid


def test_cannot_remove_last_email(conn):
    tid = _mk(conn, customer_email="only@example.com")
    cid = repo.get_ticket(conn, tid)["customer_id"]
    repo.remove_email(conn, cid, "only@example.com")
    conn.commit()
    assert len(repo.customer_emails(conn, cid)) == 1


def test_merge_moves_tickets_and_contacts(conn):
    a = _mk(conn, customer_email="typo@gmai.com", customer_name="Muhammad Rafay")
    b = _mk(conn, customer_email="real@gmail.com", subject="Other")
    src = repo.get_ticket(conn, a)["customer_id"]
    dst = repo.get_ticket(conn, b)["customer_id"]

    assert repo.merge_customers(conn, src, dst) == 1
    conn.commit()
    assert repo.get_customer(conn, src) is None
    assert len(repo.list_tickets(conn, customer_id=dst)) == 2
    assert "typo@gmai.com" in [e["email"] for e in repo.customer_emails(conn, dst)]
    assert repo.get_customer(conn, dst)["name"] == "Muhammad Rafay"


def test_questions_lifecycle(conn):
    tid = _mk(conn)
    repo.add_question(conn, tid, "What device and OS are you using?")
    conn.commit()
    q = repo.get_questions(conn, tid)[0]
    assert q["answered_at"] is None

    repo.answer_question(conn, q["id"], "iPhone 17, iOS 26")
    conn.commit()
    assert repo.get_questions(conn, tid)[0]["answered_at"] is not None


def test_question_extraction_from_a_draft(conn):
    body = ("Dear Muhammad,\n\nSome questions:\n\n"
            "* What type of device and operating system are you using?\n"
            "* Have you noticed any error messages?\n"
            "* Has this problem happened before this?\n\nThanks")
    found = repo.extract_questions(body)
    assert len(found) == 3
    assert all(q.endswith("?") and not q.startswith("*") for q in found)


def test_follow_up_becomes_due(conn):
    tid = _mk(conn)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    repo.set_follow_up(conn, tid, past, "Chase for device info")
    conn.commit()
    assert any(f["id"] == tid for f in repo.due_follow_ups(conn))

    repo.clear_follow_up(conn, tid)
    conn.commit()
    assert not repo.due_follow_ups(conn)


def test_future_follow_up_not_due_yet(conn):
    tid = _mk(conn)
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    repo.set_follow_up(conn, tid, future, "later")
    conn.commit()
    assert not repo.due_follow_ups(conn)
