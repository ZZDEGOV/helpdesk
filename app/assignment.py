"""Assignment: who is working a ticket, as distinct from who logged it.

A ticket's owner is whoever created it and never changes on its own. Its
assignee is the person currently responsible for it, or nobody yet.

Who a ticket can be handed to depends on who is handing it over:
  - an admin (who sees every ticket anyway) can assign it to anyone who works
    tickets;
  - anyone else can assign it only to people who could already see it -- its
    owner, the members of its group, and the admins -- so a user can't use
    assignment to show a private ticket to someone outside their team.
Root is never assigned (it can't work tickets), and neither is a disabled
account. Being assigned lets you see the ticket (auth.can_view_ticket), so a
hand-off keeps working even if the ticket later leaves the group.

Anyone who can work a ticket can change its assignee: teams hand work around
without waiting for a manager.
"""
import sqlite3
from collections.abc import Mapping

from . import auth, repo
from .db import utcnow


def _roles_with(*capabilities: str) -> list[str]:
    return [role for role, caps in auth.CAPABILITIES.items()
            if all(c in caps for c in capabilities)]


def _in(values: list[str]) -> str:
    return "(" + ",".join("?" * len(values)) + ")"


WORKERS = _roles_with("work_tickets")                        # who can be assigned at all
SEE_ALL = _roles_with("work_tickets", "view_all_tickets")    # assignable to any ticket


def _label(row) -> str:
    return (row["display_name"] or "").strip() or row["username"]


_ORDER = " ORDER BY COALESCE(NULLIF(display_name, ''), username) COLLATE NOCASE"


def assignable(conn, ticket: Mapping, by) -> list[sqlite3.Row]:
    """The people `by` may assign this ticket to, by display name. `ticket`
    needs only owner_id and group_id, so it works for one not yet created."""
    if auth.can(by, "view_all_tickets"):
        return conn.execute(
            "SELECT id, username, display_name, role FROM users"
            f" WHERE is_active = 1 AND role IN {_in(WORKERS)}" + _ORDER, WORKERS).fetchall()
    return conn.execute(
        "SELECT id, username, display_name, role FROM users"
        f" WHERE is_active = 1 AND role IN {_in(WORKERS)}"
        f" AND (role IN {_in(SEE_ALL)} OR id = ?"
        "      OR id IN (SELECT user_id FROM group_members WHERE group_id = ?))" + _ORDER,
        [*WORKERS, *SEE_ALL, ticket["owner_id"], ticket["group_id"]]).fetchall()


def can_assign_to(conn, ticket: Mapping, user_id: int | None, by) -> bool:
    """None (unassign) is always allowed."""
    return user_id is None or any(u["id"] == user_id for u in assignable(conn, ticket, by))


def picker(conn, user) -> list[dict]:
    """Everyone this person might assign a new ticket to, for the new-ticket
    form, with the groups each is in. The form uses `groups` and `always` to
    offer only the people who fit the group chosen; the server checks again."""
    people = choices(conn, user)
    member_of: dict[int, list[int]] = {}
    for row in conn.execute("SELECT user_id, group_id FROM group_members"):
        member_of.setdefault(row["user_id"], []).append(row["group_id"])
    see_all = auth.can(user, "view_all_tickets")
    return [{"id": p["id"], "name": _label(p),
             "always": see_all or p["id"] == user["id"] or p["role"] in SEE_ALL,
             "groups": member_of.get(p["id"], [])} for p in people]


def choices(conn, user) -> list[sqlite3.Row]:
    """Everyone this person might hand tickets to, for the bulk-action menu on
    the ticket list: for an admin every active worker, otherwise themselves,
    their groupmates and the admins. Each ticket is still checked on its own
    (can_assign_to) when the action runs."""
    if auth.can(user, "view_all_tickets"):
        extra_sql, extra = "", []
    else:
        extra_sql = (f" AND (role IN {_in(SEE_ALL)} OR id = ? OR id IN"
                     " (SELECT m.user_id FROM group_members m WHERE m.group_id IN"
                     "  (SELECT group_id FROM group_members WHERE user_id = ?)))")
        extra = [*SEE_ALL, user["id"], user["id"]]
    return conn.execute(
        "SELECT id, username, display_name, role FROM users"
        f" WHERE is_active = 1 AND role IN {_in(WORKERS)}" + extra_sql + _ORDER,
        [*WORKERS, *extra]).fetchall()


def set_assignee(conn, ticket_id: int, user_id: int | None) -> bool:
    """Assign (or with None, unassign) and record it on the timeline. Returns
    False if nothing changed. Callers check can_assign_to first."""
    row = conn.execute("SELECT assignee_id FROM tickets WHERE id = ?",
                       (ticket_id,)).fetchone()
    if row is None or row["assignee_id"] == user_id:
        return False

    def name(uid: int | None) -> str:
        user = conn.execute("SELECT username, display_name FROM users WHERE id = ?",
                            (uid,)).fetchone() if uid else None
        return _label(user) if user else "nobody"

    conn.execute("UPDATE tickets SET assignee_id = ?, updated_at = ? WHERE id = ?",
                 (user_id, utcnow(), ticket_id))
    body = (f"Assigned to {name(user_id)}" if user_id else "Unassigned") + (
        f" (was {name(row['assignee_id'])})" if row["assignee_id"] else "")
    repo.log_event(conn, ticket_id, "assigned", body,
                   {"field": "assignee_id", "from": row["assignee_id"], "to": user_id})
    return True
