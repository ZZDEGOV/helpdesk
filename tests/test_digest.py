"""The open-ticket digest: root-picked subscribers, per-subscriber scope,
the schedule, and the mail senders."""
import json
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy

import pytest

from app import config, digest, mailer
from app.db import connect
from app.web import LOCAL_TZ

from .conftest import NR, new_ticket


def user_id(username: str) -> int:
    with connect() as conn:
        return conn.execute("SELECT id FROM users WHERE username = ?",
                            (username,)).fetchone()["id"]


def subscribe(root, username: str, scope: str = "all", email: str | None = None):
    return root.post("/root/digest/subscribers", data={
        "user_id": user_id(username), "scope": scope,
        "email": f"{username.lower()}@example.gov" if email is None else email}, **NR)


def outbox() -> list:
    """Every email written so far, parsed, oldest first."""
    files = sorted(config.OUTBOX_DIR.glob("*.eml"), key=lambda p: p.stat().st_mtime_ns)
    return [message_from_bytes(p.read_bytes(), policy=policy.default) for p in files]


def html_of(message) -> str:
    return message.get_body(preferencelist=("html",)).get_content()


# ---------------------------------------------------------------- access

def test_only_root_reaches_the_digest_pages(env):
    for who in ("admin", "user"):
        assert env[who].get("/root/digest", **NR).status_code == 403
        assert env[who].post("/root/digest/send", data={}, **NR).status_code == 403
        assert env[who].post("/root/digest/subscribers",
                             data={"user_id": 1}, **NR).status_code == 403
    assert env["root"].get("/root/digest").status_code == 200


# ---------------------------------------------------------------- account email

def test_root_sets_and_clears_an_account_email(env):
    uid = user_id("mreed")
    base = {"display_name": "M Reed", "role": "user", "is_active": "1"}
    env["root"].post(f"/root/users/{uid}/update", data={**base, "email": "MReed@Example.gov"}, **NR)
    with connect() as conn:
        assert conn.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()[0] \
            == "mreed@example.gov"

    # A form that doesn't mention email leaves it alone; a blank one clears it.
    env["root"].post(f"/root/users/{uid}/update", data=base, **NR)
    with connect() as conn:
        assert conn.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()[0]
    env["root"].post(f"/root/users/{uid}/update", data={**base, "email": ""}, **NR)
    with connect() as conn:
        assert conn.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()[0] is None


@pytest.mark.parametrize("bad", ["nope", "a@b", "two words@x.gov", "a@x.gov\nBcc: evil@x.gov"])
def test_bad_email_is_rejected(env, bad):
    uid = user_id("mreed")
    r = env["root"].post(f"/root/users/{uid}/update", data={
        "display_name": "M", "role": "user", "is_active": "1", "email": bad}, **NR)
    assert "error=" in r.headers["location"]
    with connect() as conn:
        assert conn.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()[0] is None


# ---------------------------------------------------------------- subscribers

def test_nobody_is_subscribed_by_default(env):
    with connect() as conn:
        assert digest.subscribers(conn) == []
    r = env["root"].post("/root/digest/send", data={}, **NR)
    assert "error=" in r.headers["location"]
    assert outbox() == []


def test_subscribing_needs_an_email(env):
    r = subscribe(env["root"], "mreed", email="")
    assert "error=" in r.headers["location"]
    with connect() as conn:
        assert digest.subscribers(conn) == []

    assert "notice=" in subscribe(env["root"], "mreed").headers["location"]
    with connect() as conn:
        subs = digest.subscribers(conn)
    assert [(s["username"], s["email"], s["scope"]) for s in subs] == \
        [("mreed", "mreed@example.gov", "all")]


def test_deleting_an_account_removes_its_subscription(env):
    subscribe(env["root"], "mreed")
    env["root"].post(f"/root/users/{user_id('mreed')}/delete", data={}, **NR)
    with connect() as conn:
        assert digest.subscribers(conn) == []


def test_disabled_account_is_skipped_not_emailed(env):
    subscribe(env["root"], "mreed")
    # An unticked "Account enabled" box is simply absent from the form.
    env["root"].post(f"/root/users/{user_id('mreed')}/update", data={
        "display_name": "M", "role": "user"}, **NR)
    r = env["root"].post("/root/digest/send", data={}, **NR)
    assert "error=" in r.headers["location"] and "disabled" in r.headers["location"]
    assert outbox() == []


# ---------------------------------------------------------------- content and scope

def test_scope_controls_which_tickets_are_in_the_email(env):
    new_ticket(env["admin"], "Fare reader offline")
    new_ticket(env["user"], "Refund request")
    subscribe(env["root"], "ZachZ", scope="all")
    subscribe(env["root"], "mreed", scope="own")

    r = env["root"].post("/root/digest/send", data={}, **NR)
    assert "notice=" in r.headers["location"]

    mail = {m["To"]: m for m in outbox()}
    assert set(mail) == {"zachz@example.gov", "mreed@example.gov"}
    everything, own = html_of(mail["zachz@example.gov"]), html_of(mail["mreed@example.gov"])
    assert "Fare reader offline" in everything and "Refund request" in everything
    assert "Refund request" in own and "Fare reader offline" not in own
    assert mail["zachz@example.gov"]["Subject"] == "Help Desk: 2 open tickets"
    assert mail["mreed@example.gov"]["Subject"] == "Help Desk: 1 open ticket"


def test_root_can_change_scope_and_remove(env):
    subscribe(env["root"], "mreed", scope="own")
    uid = user_id("mreed")
    env["root"].post(f"/root/digest/subscribers/{uid}/scope", data={"scope": "all"}, **NR)
    with connect() as conn:
        assert digest.get_subscriber(conn, uid)["scope"] == "all"
    assert env["root"].post(f"/root/digest/subscribers/{uid}/scope",
                            data={"scope": "everything"}, **NR).status_code == 400
    env["root"].post(f"/root/digest/subscribers/{uid}/remove", data={}, **NR)
    with connect() as conn:
        assert digest.get_subscriber(conn, uid) is None


def test_closed_and_deleted_tickets_are_left_out(env):
    done = new_ticket(env["admin"], "Already handled")
    binned = new_ticket(env["admin"], "Thrown away")
    new_ticket(env["admin"], "Still open")
    env["admin"].post(f"/tickets/{done}/status", data={"status": "resolved"}, **NR)
    env["admin"].post(f"/tickets/{binned}/delete", data={}, **NR)
    subscribe(env["root"], "ZachZ")

    page = env["root"].get(f"/root/digest/preview/{user_id('ZachZ')}").text
    assert "Still open" in page
    assert "Already handled" not in page and "Thrown away" not in page
    assert outbox() == []                      # a preview sends nothing


def test_ticket_text_is_escaped_in_the_email(env):
    new_ticket(env["admin"], "<script>alert(1)</script>")
    subscribe(env["root"], "ZachZ")
    page = env["root"].get(f"/root/digest/preview/{user_id('ZachZ')}").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_send_to_one_subscriber_only(env):
    subscribe(env["root"], "ZachZ")
    subscribe(env["root"], "mreed")
    env["root"].post("/root/digest/send", data={"user_id": user_id("mreed")}, **NR)
    assert [m["To"] for m in outbox()] == ["mreed@example.gov"]


# ---------------------------------------------------------------- schedule

def test_last_slot_picks_the_most_recent_scheduled_moment():
    wednesday_noon = datetime(2026, 11, 18, 12, 0, tzinfo=LOCAL_TZ)
    monday_8 = datetime(2026, 11, 16, 8, 0, tzinfo=LOCAL_TZ)
    assert digest.last_slot([0], 8, wednesday_noon) == monday_8
    # On the day itself, before the hour: last week's slot, not today's.
    assert digest.last_slot([0], 8, monday_8 - timedelta(minutes=1)) == \
        monday_8 - timedelta(days=7)
    assert digest.last_slot([0], 8, monday_8) == monday_8
    assert digest.last_slot([0, 2], 8, wednesday_noon) == \
        datetime(2026, 11, 18, 8, 0, tzinfo=LOCAL_TZ)
    assert digest.last_slot([], 8, wednesday_noon) is None


def test_scheduled_digest_fires_once_per_slot(env):
    subscribe(env["root"], "ZachZ")
    assert digest.run_if_due() is None                      # no schedule yet

    now = datetime.now(timezone.utc)
    env["root"].post("/root/digest/schedule",
                     data={"days": [str(d) for d in range(7)], "hour": "0"}, **NR)
    with connect() as conn:
        assert digest.get_schedule(conn)["days"] == list(range(7))

    # Saving a schedule never fires a slot that has already passed.
    assert digest.run_if_due(now) is None
    assert outbox() == []

    tomorrow = now + timedelta(days=1)
    result = digest.run_if_due(tomorrow)
    assert result is not None and result.sent == ["zachz@example.gov"]
    assert digest.run_if_due(tomorrow) is None              # same slot: not again
    assert len(outbox()) == 1
    assert digest.run_if_due(tomorrow + timedelta(days=1)) is not None   # next slot


def test_ticking_no_days_turns_the_schedule_off(env):
    subscribe(env["root"], "ZachZ")
    env["root"].post("/root/digest/schedule", data={"days": ["0"], "hour": "8"}, **NR)
    env["root"].post("/root/digest/schedule", data={"hour": "8"}, **NR)
    assert digest.run_if_due(datetime.now(timezone.utc) + timedelta(days=8)) is None


# ---------------------------------------------------------------- new-ticket notice

def test_new_ticket_notice_is_off_by_default(env):
    subscribe(env["root"], "ZachZ")
    new_ticket(env["user"], "Quiet")
    assert outbox() == []


def test_new_ticket_notice_respects_scope_and_skips_the_creator(env):
    subscribe(env["root"], "ZachZ", scope="all")
    subscribe(env["root"], "mreed", scope="own")
    env["root"].post("/root/digest/schedule", data={"hour": "8", "on_new_ticket": "1"}, **NR)

    new_ticket(env["user"], "Bus 14 validator down")
    mail = outbox()
    assert [m["To"] for m in mail] == ["zachz@example.gov"]
    assert mail[0]["Subject"].startswith("New ticket HD-")
    assert "Bus 14 validator down" in mail[0]["Subject"]

    new_ticket(env["admin"], "Created by the only all-scope subscriber")
    assert len(outbox()) == 1                  # the creator isn't told about their own


# ---------------------------------------------------------------- senders

def _message() -> mailer.Message:
    return mailer.Message(to="cto@example.gov", subject="Hi", html="<p>Hi</p>", text="Hi")


def test_webhook_posts_json(env, monkeypatch):
    seen = {}

    class FakeOpener:
        def open(self, request, timeout):
            seen.update(url=request.full_url, body=json.loads(request.data),
                        type=request.get_header("Content-type"))

            class Response:
                def __enter__(self): return self
                def __exit__(self, *exc): return False
            return Response()

    monkeypatch.setattr(config.settings, "mail_mode", "webhook")
    monkeypatch.setattr(config.settings, "mail_webhook_url", "https://flow.example/trigger?sig=s3cret")
    monkeypatch.setattr(mailer.urllib.request, "build_opener", lambda *h: FakeOpener())
    mailer.send(_message())
    assert seen["url"].startswith("https://flow.example/")
    assert seen["type"] == "application/json"
    assert seen["body"] == {"to": "cto@example.gov", "subject": "Hi",
                            "html": "<p>Hi</p>", "text": "Hi"}


def test_webhook_failure_is_reported_without_leaking_the_url(env, monkeypatch):
    def refuse(*handlers):
        raise OSError("connection refused")

    monkeypatch.setattr(config.settings, "mail_mode", "webhook")
    monkeypatch.setattr(config.settings, "mail_webhook_url", "https://flow.example/trigger?sig=s3cret")
    monkeypatch.setattr(mailer.urllib.request, "build_opener", refuse)
    subscribe(env["root"], "ZachZ")
    r = env["root"].post("/root/digest/send", data={}, **NR)
    assert "error=" in r.headers["location"]
    with connect() as conn:
        detail = digest.recent_sends(conn)[0]["detail"]
    assert "1 failed" in detail and "s3cret" not in detail and "flow.example" not in detail


def test_webhook_without_a_url_fails_cleanly(env, monkeypatch):
    monkeypatch.setattr(config.settings, "mail_mode", "webhook")
    with pytest.raises(mailer.MailError):
        mailer.send(_message())


def test_non_production_never_sends_for_real(env, monkeypatch):
    """Staging is a copy of production's data, subscribers included."""
    monkeypatch.setattr(config, "ENV", "staging")
    monkeypatch.setattr(config.settings, "mail_mode", "webhook")
    monkeypatch.setattr(config.settings, "mail_webhook_url", "https://flow.example/x")
    monkeypatch.setattr(mailer.urllib.request, "build_opener",
                        lambda *h: pytest.fail("staging tried to send real mail"))
    assert mailer.mode() == "outbox"
    subscribe(env["root"], "ZachZ")
    env["root"].post("/root/digest/send", data={}, **NR)
    assert outbox()[0]["Subject"].startswith("[STAGING] ")
    assert "STAGING" in env["root"].get("/root/digest").text
