"""Three-tier deletion model.

  user  -- no deletion at all. Striking through timeline entries is NOT deletion
           and stays available.
  admin -- queued (soft) deletion and restore.
  root  -- immediate permanent deletion, from its own admin area only.
"""
import pytest

from app import auth, repo
from app.db import connect

from .conftest import NR, new_ticket as _new_ticket


# ---------------------------------------------------------------- capabilities

@pytest.mark.parametrize("role,capability,expected", [
    ("user", "soft_delete", False), ("user", "hard_delete", False),
    ("user", "view_trash", False), ("user", "work_tickets", True),
    ("admin", "soft_delete", True), ("admin", "hard_delete", False),
    ("admin", "view_trash", True), ("admin", "restore", True),
    ("root", "hard_delete", True), ("root", "delete_users", True),
    ("root", "work_tickets", False), ("root", "soft_delete", False),
])
def test_capability_matrix(role, capability, expected):
    assert auth.can({"role": role}, capability) is expected


def test_anonymous_can_do_nothing():
    assert not auth.can(None, "work_tickets")


# ---------------------------------------------------------------- user

def test_user_cannot_delete_anything(env):
    tid = _new_ticket(env["user"], customer_email="u@example.com", customer_name="U")
    assert env["user"].post(f"/tickets/{tid}/delete", data={}, **NR).status_code == 403
    assert env["user"].post("/customers/1/delete", data={}, **NR).status_code == 403
    assert env["user"].post(f"/root/tickets/{tid}/delete", data={}, **NR).status_code == 403


def test_user_cannot_reach_trash(env):
    assert env["user"].get("/trash", **NR).status_code == 403
    assert "/trash" not in env["user"].get("/").text


def test_user_can_still_strike_through(env):
    """Voiding is not deletion — it stays available to everyone working tickets."""
    tid = _new_ticket(env["user"])
    with connect() as conn:
        event_id = repo.get_events(conn, int(tid))[0]["id"]
    r = env["user"].post(f"/events/{event_id}/void",
                         data={"ticket_id": tid, "reason": "mislogged"}, **NR)
    assert r.status_code == 303
    with connect() as conn:
        assert repo.get_events(conn, int(tid))[0]["voided_at"] is not None


# ---------------------------------------------------------------- admin

def test_admin_queues_rather_than_removes(env):
    tid = _new_ticket(env["admin"])
    assert env["admin"].post(f"/tickets/{tid}/delete", data={}, **NR).status_code == 303
    with connect() as conn:
        ticket = repo.get_ticket(conn, int(tid))
    assert ticket is not None and ticket["deleted_at"] is not None


def test_admin_can_restore(env):
    tid = _new_ticket(env["admin"])
    env["admin"].post(f"/tickets/{tid}/delete", data={}, **NR)
    assert env["admin"].post(f"/tickets/{tid}/restore", data={}, **NR).status_code == 303
    with connect() as conn:
        assert repo.get_ticket(conn, int(tid))["deleted_at"] is None


def test_admin_cannot_hard_delete(env):
    tid = _new_ticket(env["admin"])
    assert env["admin"].post(f"/root/tickets/{tid}/delete", data={}, **NR).status_code == 403
    assert env["admin"].get("/root/customers", **NR).status_code == 403
    with connect() as conn:
        assert repo.get_ticket(conn, int(tid)) is not None


# ---------------------------------------------------------------- root

def test_root_deletes_immediately(env):
    tid = _new_ticket(env["admin"])
    assert env["root"].post(f"/root/tickets/{tid}/delete", data={}, **NR).status_code == 303
    with connect() as conn:
        assert repo.get_ticket(conn, int(tid)) is None


def test_root_still_cannot_use_ticket_routes(env):
    """Instant delete lives under /root, so the read-only rule is unbroken."""
    tid = _new_ticket(env["admin"])
    assert env["root"].post(f"/tickets/{tid}/delete", data={}, **NR).status_code == 403
    assert env["root"].post(f"/tickets/{tid}/status",
                            data={"status": "open"}, **NR).status_code == 403


def test_root_customer_delete_takes_their_tickets(env):
    tid = _new_ticket(env["admin"], customer_email="c@example.com", customer_name="C")
    with connect() as conn:
        customer_id = repo.get_ticket(conn, int(tid))["customer_id"]
    env["root"].post(f"/root/customers/{customer_id}/delete", data={}, **NR)
    with connect() as conn:
        assert repo.get_customer(conn, customer_id) is None
        assert repo.get_ticket(conn, int(tid)) is None


# ---------------------------------------------------------------- accounts

def test_deleting_a_user_reassigns_their_tickets(env):
    tid = _new_ticket(env["user"])
    with connect() as conn:
        ids = {u["username"]: u["id"] for u in auth.list_users(conn)}
    env["root"].post(f"/root/users/{ids['mreed']}/delete",
                     data={"reassign_to": str(ids["ZachZ"])}, **NR)
    with connect() as conn:
        assert auth.get_user(conn, ids["mreed"]) is None
        assert repo.get_ticket(conn, int(tid))["owner_id"] == ids["ZachZ"]


def test_deleting_a_user_keeps_their_timeline(env):
    """History survives the account — the entry stays, the link is nulled."""
    tid = _new_ticket(env["user"])
    with connect() as conn:
        ids = {u["username"]: u["id"] for u in auth.list_users(conn)}
        before = len(repo.get_events(conn, int(tid)))
    env["root"].post(f"/root/users/{ids['mreed']}/delete", data={}, **NR)
    with connect() as conn:
        events = repo.get_events(conn, int(tid))
    assert len(events) == before
    assert all(e["user_id"] is None for e in events)


def test_unowned_is_allowed_when_no_target_given(env):
    tid = _new_ticket(env["user"])
    with connect() as conn:
        ids = {u["username"]: u["id"] for u in auth.list_users(conn)}
    env["root"].post(f"/root/users/{ids['mreed']}/delete", data={"reassign_to": ""}, **NR)
    with connect() as conn:
        assert repo.get_ticket(conn, int(tid))["owner_id"] is None


def test_cannot_delete_the_account_you_are_using(env):
    with connect() as conn:
        rid = auth.get_user_by_name(conn, "root")["id"]
    r = env["root"].post(f"/root/users/{rid}/delete", data={}, **NR)
    assert "using" in r.headers.get("location", "")
    with connect() as conn:
        assert auth.get_user(conn, rid) is not None


def test_a_second_root_can_be_deleted(env):
    env["root"].post("/root/users/create",
                     data={"username": "root2", "display_name": "R2",
                           "password": "temp12345", "role": "root"}, **NR)
    with connect() as conn:
        rid2 = auth.get_user_by_name(conn, "root2")["id"]
    env["root"].post(f"/root/users/{rid2}/delete", data={}, **NR)
    with connect() as conn:
        assert auth.get_user(conn, rid2) is None


def test_admin_cannot_delete_users(env):
    with connect() as conn:
        uid = auth.get_user_by_name(conn, "mreed")["id"]
    assert env["admin"].post(f"/root/users/{uid}/delete", data={}, **NR).status_code == 403
    with connect() as conn:
        assert auth.get_user(conn, uid) is not None


def test_destructive_actions_are_audited(env):
    tid = _new_ticket(env["admin"])
    env["root"].post(f"/root/tickets/{tid}/delete", data={}, **NR)
    with connect() as conn:
        actions = [a["action"] for a in auth.recent_audit(conn)]
    assert "ticket_deleted_permanently" in actions
