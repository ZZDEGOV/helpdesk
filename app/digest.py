"""The open-ticket digest: an email listing the tickets that are still open.

Who gets it is a list root keeps by hand (digest_subscribers). Nobody is added
because of their role. Each subscriber has a scope root chooses:

    all -- every open ticket
    own -- only the tickets that subscriber owns

It goes out on a schedule root sets (weekdays + an hour), when root presses
"Send now", and optionally whenever a ticket is created. How the email actually
leaves the building is mailer.py's business.

The schedule lives in the meta table so root can change it without a restart:
    digest_days           "0,3" -- weekdays to send on, 0 = Monday. Blank = off.
    digest_hour           local hour, 0-23
    digest_on_new_ticket  "1" to also email when a ticket is created
    digest_mark           when the schedule was last saved or last ran. A slot
                          only fires if it is later than this, so saving a
                          schedule never fires a slot that has already passed.
"""
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from . import auth, config, mailer, repo
from .db import connect, utcnow
from .web import LOCAL_TZ, templates

log = logging.getLogger("helpdesk.digest")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SCOPE_LABELS = {"all": "Every open ticket", "own": "Only tickets they own"}


# ---------------------------------------------------------------- schedule

def _meta(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def _set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))


def get_schedule(conn) -> dict:
    days = sorted({int(d) for d in _meta(conn, "digest_days").split(",")
                   if d.strip().isdigit() and int(d) <= 6})
    hour = _meta(conn, "digest_hour", "8")
    return {"days": days,
            "hour": int(hour) if hour.isdigit() and int(hour) <= 23 else 8,
            "on_new_ticket": _meta(conn, "digest_on_new_ticket") == "1",
            "mark": _meta(conn, "digest_mark")}


def save_schedule(conn, days: list[int], hour: int, on_new_ticket: bool) -> None:
    _set_meta(conn, "digest_days", ",".join(str(d) for d in sorted(set(days))))
    _set_meta(conn, "digest_hour", str(hour))
    _set_meta(conn, "digest_on_new_ticket", "1" if on_new_ticket else "0")
    _set_meta(conn, "digest_mark", utcnow())


def describe_schedule(schedule: dict) -> str:
    if not schedule["days"]:
        return "No schedule"
    when = time(schedule["hour"]).strftime("%I:%M %p").lstrip("0")
    return ", ".join(DAY_NAMES[d] for d in schedule["days"]) + f" at {when}"


def last_slot(days: list[int], hour: int, now_local: datetime) -> datetime | None:
    """The most recent scheduled moment at or before now."""
    for back in range(8):
        day = now_local.date() - timedelta(days=back)
        if day.weekday() in days:
            slot = datetime.combine(day, time(hour), tzinfo=now_local.tzinfo)
            if slot <= now_local:
                return slot
    return None


# ---------------------------------------------------------------- subscribers

def subscribers(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.user_id, s.scope, u.username, u.display_name, u.email, u.role,"
        " u.is_active FROM digest_subscribers s JOIN users u ON u.id = s.user_id"
        " ORDER BY u.username COLLATE NOCASE").fetchall()


def get_subscriber(conn, user_id: int) -> sqlite3.Row | None:
    return next((s for s in subscribers(conn) if s["user_id"] == user_id), None)


def subscribe(conn, user_id: int, scope: str = "all") -> None:
    if scope not in SCOPE_LABELS:
        raise ValueError(f"unknown scope: {scope}")
    conn.execute("INSERT INTO digest_subscribers (user_id, scope, created_at) VALUES (?,?,?)"
                 " ON CONFLICT(user_id) DO UPDATE SET scope = excluded.scope",
                 (user_id, scope, utcnow()))


def unsubscribe(conn, user_id: int) -> None:
    conn.execute("DELETE FROM digest_subscribers WHERE user_id = ?", (user_id,))


def skip_reason(subscriber) -> str | None:
    """Why a subscriber would get nothing right now. Checked at send time, so
    disabling an account stops its emails without touching the list."""
    if not subscriber["is_active"]:
        return "account disabled"
    if not subscriber["email"]:
        return "no email address"
    return None


# ---------------------------------------------------------------- building

def _one_line(value: str) -> str:
    """Email subjects are a header: no line breaks allowed."""
    return " ".join((value or "").split())


def build(conn, subscriber, new_ticket=None) -> mailer.Message:
    """The email for one subscriber, limited to what their scope allows."""
    show_all = subscriber["scope"] == "all"
    tickets = repo.list_tickets(conn, status="open", sort="priority",
                                owner_id=None if show_all else subscriber["user_id"])
    overdue = sum(1 for t in tickets if repo.is_overdue(t))
    count = f"{len(tickets)} open ticket{'' if len(tickets) == 1 else 's'}"

    if new_ticket is not None:
        subject = f"New ticket {new_ticket['ref']}: {_one_line(new_ticket['subject'])}"
    else:
        subject = f"Help Desk: {count}" + (f", {overdue} overdue" if overdue else "")
    if config.ENV != "production":
        subject = f"[{config.ENV.upper()}] {subject}"

    def link(ticket) -> str:
        return f"{config.BASE_URL}/tickets/{ticket['id']}" if config.BASE_URL else ""

    html = templates.env.get_template("email_digest.html").render(
        tickets=tickets, overdue=overdue, count=count, show_owner=show_all,
        new_ticket=new_ticket, link=link, config=config, is_overdue=repo.is_overdue,
        name=subscriber["display_name"] or subscriber["username"],
        scope_label=SCOPE_LABELS[subscriber["scope"]].lower())

    lines = []
    if new_ticket is not None:
        lines += [f"New ticket {new_ticket['ref']}: {_one_line(new_ticket['subject'])}",
                  link(new_ticket), ""]
    lines.append(count + (f", {overdue} overdue" if overdue else "") + ".")
    lines.append("")
    for t in tickets:
        lines.append(f"{t['ref']}  P{t['priority']}  {config.STATUS_LABELS[t['status']]}"
                     + ("  OVERDUE" if repo.is_overdue(t) else "")
                     + f"  {_one_line(t['subject'])}")
        if link(t):
            lines.append(f"    {link(t)}")
    return mailer.Message(to=subscriber["email"], subject=subject[:200], html=html,
                          text="\n".join(line for line in lines if line is not None))


# ---------------------------------------------------------------- sending

@dataclass
class Result:
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.sent or self.failed or self.skipped)

    def summary(self) -> str:
        parts = [f"{len(self.sent)} sent"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed ({'; '.join(self.failed)})")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped ({'; '.join(self.skipped)})")
        return ", ".join(parts)


def send_all(conn, trigger: str, actor_id: int | None = None,
             only_user_id: int | None = None, new_ticket=None) -> Result:
    """Email every subscriber (or just one). One failure doesn't stop the rest.

    A new-ticket notice only goes to scope-all subscribers other than the
    ticket's creator: the creator already knows, and an own-scope subscriber
    can't see someone else's ticket."""
    result = Result()
    for sub in subscribers(conn):
        if only_user_id is not None and sub["user_id"] != only_user_id:
            continue
        if new_ticket is not None and (sub["scope"] != "all"
                                       or sub["user_id"] == new_ticket["owner_id"]):
            continue
        reason = skip_reason(sub)
        if reason:
            result.skipped.append(f"{sub['username']}: {reason}")
            continue
        try:
            mailer.send(build(conn, sub, new_ticket))
            result.sent.append(sub["email"])
        except mailer.MailError as exc:
            result.failed.append(f"{sub['email']}: {exc}")
    if result:
        auth.audit(conn, actor_id, "digest_sent", trigger,
                   f"{mailer.mode()}: {result.summary()}")
    return result


def run_if_due(now: datetime | None = None) -> Result | None:
    """Called every minute by main.py. Sends the scheduled digest once its slot
    has arrived. If the app was down at the scheduled time, it goes out late
    rather than not at all."""
    with closing(connect()) as conn:
        schedule = get_schedule(conn)
        if not schedule["days"]:
            return None
        now = now or datetime.now(timezone.utc)
        slot = last_slot(schedule["days"], schedule["hour"], now.astimezone(LOCAL_TZ))
        if slot is None:
            return None
        if schedule["mark"] and slot <= datetime.fromisoformat(schedule["mark"]):
            return None
        # Mark before sending: if a send crashes, the next minute must not retry
        # and email the people who already got theirs.
        _set_meta(conn, "digest_mark",
                  now.astimezone(timezone.utc).isoformat(timespec="seconds"))
        conn.commit()
        result = send_all(conn, "scheduled")
        conn.commit()
        return result


def notify_new_ticket(ticket_id: int) -> None:
    """Runs after the response for a newly created ticket has been sent."""
    try:
        with closing(connect()) as conn:
            if not get_schedule(conn)["on_new_ticket"]:
                return
            ticket = repo.get_ticket(conn, ticket_id)
            if ticket is None or ticket["deleted_at"]:
                return
            send_all(conn, f"new ticket {ticket['ref']}", actor_id=ticket["owner_id"],
                     new_ticket=ticket)
            conn.commit()
    except Exception:
        log.exception("new-ticket notice failed for ticket %s", ticket_id)


def recent_sends(conn, limit: int = 8) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON u.id = a.user_id"
        " WHERE a.action = 'digest_sent' ORDER BY a.created_at DESC, a.id DESC LIMIT ?",
        (limit,)).fetchall()
