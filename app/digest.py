"""The open-ticket digest: an email listing the tickets that are still open.

There are two kinds of recipient, both set up by root and nobody else:

  Subscribers -- individual accounts root adds by hand (digest_subscribers).
      Nobody is added because of their role. Each has a scope root chooses:
          all   every open ticket
          team  their own tickets plus their groups' tickets
          own   only the tickets they own
  Groups -- root can switch on a digest for a whole group (user_groups). Every
      member with an email address gets the group's open tickets, and people
      added to the group later are included automatically.

Each subscriber and each group has its OWN schedule: weekdays + an hour, and
optionally an email whenever a ticket is created. A schedule is four values:
    days           "0,3" -- weekdays to send on, 0 = Monday. Blank = no schedule.
    hour           local hour, 0-23
    on_new_ticket  also email when a ticket they would see is created
    mark           when the schedule was last saved or last ran. A slot only
                   fires if it is later than this, so saving a schedule never
                   fires a slot that has already passed.

How the email actually leaves the building is mailer.py's business.
"""
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from . import auth, config, groups, mailer, repo
from .db import connect, utcnow
from .web import LOCAL_TZ, templates

log = logging.getLogger("helpdesk.digest")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SCOPE_LABELS = {"all": "Every open ticket",
                "team": "Their own and their groups' tickets",
                "own": "Only tickets they own"}


# ---------------------------------------------------------------- schedules

def parse_days(value: str | None) -> list[int]:
    return sorted({int(d) for d in (value or "").split(",")
                   if d.strip().isdigit() and int(d) <= 6})


def _days_text(days: list[int]) -> str:
    return ",".join(str(d) for d in sorted(set(days)))


def describe_schedule(days: list[int] | str | None, hour: int,
                      on_new_ticket: bool = False) -> str:
    days = parse_days(days) if not isinstance(days, list) else days
    parts = []
    if days:
        when = time(hour).strftime("%I:%M %p").lstrip("0")
        names = ("Every day" if len(days) == 7 else
                 "Weekdays" if days == [0, 1, 2, 3, 4] else
                 ", ".join(DAY_NAMES[d] for d in days))
        parts.append(f"{names} at {when}")
    if on_new_ticket:
        parts.append("on each new ticket")
    return " + ".join(parts) if parts else "Only when you press Send"


def last_slot(days: list[int], hour: int, now_local: datetime) -> datetime | None:
    """The most recent scheduled moment at or before now."""
    for back in range(8):
        day = now_local.date() - timedelta(days=back)
        if day.weekday() in days:
            slot = datetime.combine(day, time(hour), tzinfo=now_local.tzinfo)
            if slot <= now_local:
                return slot
    return None


def _is_due(days_text: str | None, hour: int, mark: str | None, now: datetime) -> bool:
    days = parse_days(days_text)
    if not days:
        return False
    slot = last_slot(days, hour, now.astimezone(LOCAL_TZ))
    if slot is None:
        return False
    return not mark or slot > datetime.fromisoformat(mark)


# ---------------------------------------------------------------- subscribers

def subscribers(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.user_id, s.scope, s.days, s.hour, s.on_new_ticket, s.mark,"
        " u.username, u.display_name, u.email, u.role, u.is_active"
        " FROM digest_subscribers s JOIN users u ON u.id = s.user_id"
        " ORDER BY u.username COLLATE NOCASE").fetchall()


def get_subscriber(conn, user_id: int) -> sqlite3.Row | None:
    return next((s for s in subscribers(conn) if s["user_id"] == user_id), None)


def subscribe(conn, user_id: int, scope: str = "all", days: list[int] | None = None,
              hour: int = 8, on_new_ticket: bool = False) -> None:
    """Add a subscriber, or replace an existing subscriber's settings."""
    if scope not in SCOPE_LABELS:
        raise ValueError(f"unknown scope: {scope}")
    values = (scope, _days_text(days or []), hour, 1 if on_new_ticket else 0, utcnow())
    conn.execute(
        "INSERT INTO digest_subscribers (user_id, scope, days, hour, on_new_ticket, mark,"
        " created_at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(user_id) DO UPDATE SET scope = excluded.scope, days = excluded.days,"
        " hour = excluded.hour, on_new_ticket = excluded.on_new_ticket, mark = excluded.mark",
        (user_id, *values, utcnow()))


def unsubscribe(conn, user_id: int) -> None:
    conn.execute("DELETE FROM digest_subscribers WHERE user_id = ?", (user_id,))


def set_group_schedule(conn, group_id: int, days: list[int], hour: int,
                       on_new_ticket: bool) -> None:
    conn.execute(
        "UPDATE user_groups SET digest_days = ?, digest_hour = ?, digest_on_new_ticket = ?,"
        " digest_mark = ? WHERE id = ?",
        (_days_text(days), hour, 1 if on_new_ticket else 0, utcnow(), group_id))


def skip_reason(person) -> str | None:
    """Why someone would get nothing right now. Checked at send time, so
    disabling an account stops its emails without touching any list."""
    if not person["is_active"]:
        return "account disabled"
    if not person["email"]:
        return "no email address"
    return None


# ---------------------------------------------------------------- building

def _one_line(value: str) -> str:
    """Email subjects are a header: no line breaks allowed."""
    return " ".join((value or "").split())


def _link(ticket) -> str:
    return f"{config.BASE_URL}/tickets/{ticket['id']}" if config.BASE_URL else ""


def _render(*, to: str, name: str, tickets: list, show_owner: bool, scope_text: str,
            label: str = "", new_ticket=None) -> mailer.Message:
    overdue = sum(1 for t in tickets if repo.is_overdue(t))
    count = f"{len(tickets)} open ticket{'' if len(tickets) == 1 else 's'}"
    summary = count + (f", {overdue} overdue" if overdue else "")

    if new_ticket is not None:
        subject = f"New ticket {new_ticket['ref']}: {_one_line(new_ticket['subject'])}"
    else:
        subject = f"Help Desk{' · ' + label if label else ''}: {summary}"
    if config.ENV != "production":
        subject = f"[{config.ENV.upper()}] {subject}"

    html = templates.env.get_template("email_digest.html").render(
        tickets=tickets, overdue=overdue, count=count, show_owner=show_owner,
        new_ticket=new_ticket, link=_link, config=config, is_overdue=repo.is_overdue,
        name=name, scope_text=scope_text, label=label)

    lines = []
    if new_ticket is not None:
        lines += [subject, _link(new_ticket), ""]
    lines += [summary + ".", ""]
    for t in tickets:
        lines.append(f"{t['ref']}  P{t['priority']}  {config.STATUS_LABELS[t['status']]}"
                     + ("  OVERDUE" if repo.is_overdue(t) else "")
                     + f"  {_one_line(t['subject'])}")
        if _link(t):
            lines.append(f"    {_link(t)}")
    return mailer.Message(to=to, subject=subject[:200], html=html, text="\n".join(lines))


def build_for_subscriber(conn, sub, new_ticket=None) -> mailer.Message:
    """One subscriber's email, limited to what their scope allows."""
    scope = {"all": {}, "team": {"viewer_id": sub["user_id"]},
             "own": {"owner_id": sub["user_id"]}}[sub["scope"]]
    return _render(
        to=sub["email"], name=sub["display_name"] or sub["username"],
        tickets=repo.list_tickets(conn, status="open", sort="priority", **scope),
        show_owner=sub["scope"] != "own", new_ticket=new_ticket,
        scope_text={"all": "every open ticket",
                    "team": "your open tickets and your groups'",
                    "own": "the open tickets you own"}[sub["scope"]])


def build_for_group(conn, group, member, new_ticket=None) -> mailer.Message:
    """A group member's email: the open tickets filed under that group."""
    return _render(
        to=member["email"], name=member["display_name"] or member["username"],
        tickets=repo.list_tickets(conn, status="open", sort="priority",
                                  group_id=group["id"]),
        show_owner=True, label=group["name"], new_ticket=new_ticket,
        scope_text=f"the open tickets of the {group['name']} group")


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


def _deliver(result: Result, person, make_message, already: set[str] | None = None) -> None:
    """Send to one person, recording the outcome. One failure never stops the
    rest. `already` holds addresses that have had this same news, so someone who
    is both a subscriber and a group member isn't told twice."""
    reason = skip_reason(person)
    if reason:
        result.skipped.append(f"{person['username']}: {reason}")
        return
    if already is not None:
        if person["email"] in already:
            return
        already.add(person["email"])
    try:
        mailer.send(make_message())
        result.sent.append(person["email"])
    except mailer.MailError as exc:
        result.failed.append(f"{person['email']}: {exc}")


def _audit(conn, result: Result, trigger: str, actor_id: int | None) -> Result:
    if result:
        auth.audit(conn, actor_id, "digest_sent", trigger,
                   f"{mailer.mode()}: {result.summary()}")
    return result


def send_subscribers(conn, trigger: str, actor_id: int | None = None,
                     only_user_id: int | None = None) -> Result:
    """Email every subscriber now (or just one), whatever their schedule."""
    result = Result()
    for sub in subscribers(conn):
        if only_user_id is None or sub["user_id"] == only_user_id:
            _deliver(result, sub, lambda sub=sub: build_for_subscriber(conn, sub))
    return _audit(conn, result, trigger, actor_id)


def send_group(conn, group, trigger: str, actor_id: int | None = None) -> Result:
    """Email a group's open tickets to its members now."""
    result = Result()
    for member in groups.members(conn, group["id"]):
        _deliver(result, member, lambda m=member: build_for_group(conn, group, m))
    return _audit(conn, result, trigger, actor_id)


def run_if_due(now: datetime | None = None) -> Result | None:
    """Called every minute by main.py. Sends to each subscriber and each group
    whose scheduled slot has arrived. If the app was down at the scheduled time,
    the email goes out late rather than not at all."""
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    with closing(connect()) as conn:
        due_subs = [s for s in subscribers(conn)
                    if _is_due(s["days"], s["hour"], s["mark"], now)]
        due_groups = [g for g in groups.list_groups(conn)
                      if _is_due(g["digest_days"], g["digest_hour"], g["digest_mark"], now)]
        if not due_subs and not due_groups:
            return None
        # Mark before sending: if a send crashes, the next minute must not retry
        # and email the people who already got theirs.
        for sub in due_subs:
            conn.execute("UPDATE digest_subscribers SET mark = ? WHERE user_id = ?",
                         (stamp, sub["user_id"]))
        for group in due_groups:
            conn.execute("UPDATE user_groups SET digest_mark = ? WHERE id = ?",
                         (stamp, group["id"]))
        conn.commit()

        result = Result()
        for sub in due_subs:
            _deliver(result, sub, lambda sub=sub: build_for_subscriber(conn, sub))
        for group in due_groups:
            for member in groups.members(conn, group["id"]):
                _deliver(result, member,
                         lambda g=group, m=member: build_for_group(conn, g, m))
        _audit(conn, result, "scheduled", None)
        conn.commit()
        return result


def notify_new_ticket(ticket_id: int) -> None:
    """Runs after the response for a newly created ticket has been sent.

    Told about it: subscribers with new-ticket emails on who can see the ticket
    under their scope, and the members of the ticket's group if that group has
    new-ticket emails on. Never the person who created it, and nobody twice."""
    try:
        with closing(connect()) as conn:
            ticket = repo.get_ticket(conn, ticket_id)
            if ticket is None or ticket["deleted_at"]:
                return
            creator, group_id = ticket["owner_id"], ticket["group_id"]
            result, already = Result(), set()

            for sub in subscribers(conn):
                if not sub["on_new_ticket"] or sub["user_id"] == creator:
                    continue
                sees_it = sub["scope"] == "all" or (
                    sub["scope"] == "team"
                    and groups.is_member(conn, group_id, sub["user_id"]))
                if sees_it:
                    _deliver(result, sub,
                             lambda sub=sub: build_for_subscriber(conn, sub, ticket), already)

            group = groups.get_group(conn, group_id) if group_id else None
            if group is not None and group["digest_on_new_ticket"]:
                for member in groups.members(conn, group_id):
                    if member["id"] != creator:
                        _deliver(result, member,
                                 lambda m=member: build_for_group(conn, group, m, ticket),
                                 already)

            _audit(conn, result, f"new ticket {ticket['ref']}", creator)
            conn.commit()
    except Exception:
        log.exception("new-ticket notice failed for ticket %s", ticket_id)


def recent_sends(conn, limit: int = 8) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON u.id = a.user_id"
        " WHERE a.action = 'digest_sent' ORDER BY a.created_at DESC, a.id DESC LIMIT ?",
        (limit,)).fetchall()
