"""Ticket visibility: users see the tickets they own; admin and root see all.

A ticket someone can't see answers 404 -- the same as one that doesn't exist --
on every route, including the ones keyed by an event or question id.
"""
import pytest

from app import repo
from app.db import connect

from .conftest import NR, create_account, new_ticket


@pytest.fixture
def two_users(env):
    """env plus a second user, so there's someone else's ticket to not see."""
    env["other"] = create_account(env["root"], "dclark", "user")
    return env


def _first_event(ticket_id: str) -> int:
    with connect() as conn:
        return repo.get_events(conn, int(ticket_id))[0]["id"]


def test_list_shows_only_your_own(two_users):
    mine = new_ticket(two_users["user"], "Mine")
    theirs = new_ticket(two_users["other"], "Theirs")
    page = two_users["user"].get("/").text
    assert f"/tickets/{mine}" in page and f"/tickets/{theirs}" not in page


def test_admin_and_root_see_everything(two_users):
    theirs = new_ticket(two_users["other"], "Theirs")
    assert f"/tickets/{theirs}" in two_users["admin"].get("/").text
    assert two_users["admin"].get(f"/tickets/{theirs}", **NR).status_code == 200
    assert two_users["root"].get(f"/tickets/{theirs}", **NR).status_code == 200


def test_someone_elses_ticket_is_not_found(two_users):
    theirs = new_ticket(two_users["other"])
    user = two_users["user"]
    assert user.get(f"/tickets/{theirs}", **NR).status_code == 404
    assert user.post(f"/tickets/{theirs}/status", data={"status": "open"},
                     **NR).status_code == 404
    assert user.post(f"/tickets/{theirs}/note", data={"body": "x"}, **NR).status_code == 404
    with connect() as conn:
        assert repo.get_ticket(conn, int(theirs))["status"] == "new"


def test_event_routes_check_the_events_ticket(two_users):
    """The form's hidden ticket_id isn't trusted -- the event's real ticket is."""
    mine, theirs = new_ticket(two_users["user"]), new_ticket(two_users["other"])
    r = two_users["user"].post(f"/events/{_first_event(theirs)}/void",
                               data={"ticket_id": mine, "reason": "x"}, **NR)
    assert r.status_code == 404
    with connect() as conn:
        assert repo.get_events(conn, int(theirs))[0]["voided_at"] is None


def test_dashboard_counts_are_scoped(two_users):
    new_ticket(two_users["other"])
    new_ticket(two_users["other"])
    with connect() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='mreed'").fetchone()["id"]
        assert repo.dashboard_stats(conn, uid)["counts"]["new"] == 0
        assert repo.dashboard_stats(conn)["counts"]["new"] == 2


def test_customer_page_hides_other_peoples_tickets(two_users):
    new_ticket(two_users["user"], "Mine", customer_email="shared@example.com")
    theirs = new_ticket(two_users["other"], "Theirs", customer_email="shared@example.com")
    with connect() as conn:
        customer_id = repo.get_ticket(conn, int(theirs))["customer_id"]
    page = two_users["user"].get(f"/customers/{customer_id}").text
    assert "Mine" in page and f"/tickets/{theirs}" not in page


def test_api_applies_the_same_rules(two_users):
    mine, theirs = new_ticket(two_users["user"]), new_ticket(two_users["other"])
    ids = {t["id"] for t in two_users["user"].get("/api/v1/tickets").json()}
    assert int(mine) in ids and int(theirs) not in ids
    assert two_users["user"].get(f"/api/v1/tickets/{theirs}").status_code == 404
    detail = two_users["user"].get(f"/api/v1/tickets/{mine}").json()
    assert detail["events"][0]["type"] == "created"


# ---------------------------------------------------------------- replies

def test_owner_placeholder_signs_as_the_person_drafting(env):
    tid = new_ticket(env["user"])
    with connect() as conn:
        tpl = repo.save_template(conn, template_id=None, name="Sig", category=None,
                                 body="Regards, {{owner}}")
        conn.commit()
    page = env["user"].get(f"/tickets/{tid}?template_id={tpl}").text
    assert "Regards, mreed" in page


# ---------------------------------------------------------------- access

def test_docs_and_api_require_sign_in(env):
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    assert anon.get("/docs", **NR).status_code == 303
    assert anon.get("/openapi.json", **NR).status_code == 303
    assert anon.get("/api/v1/tickets", **NR).status_code == 401
    assert anon.get("/health").json() == {"status": "ok"}
    assert env["user"].get("/docs").status_code == 200
    assert "/api/v1/tickets" in env["user"].get("/openapi.json").json()["paths"]


def test_invalid_form_gets_a_page_not_json(env):
    tid = new_ticket(env["user"])
    r = env["user"].post(f"/tickets/{tid}/status", data={"status": "bogus"}, **NR)
    assert r.status_code == 400 and "text/html" in r.headers["content-type"]
