"""Who else has a ticket open right now -- the "someone else is here" warning.

While a ticket page is open and visible, it checks in about every 20 seconds
(POST /tickets/{id}/presence). Each check-in records that person as viewing,
and as writing if their reply box has something in it, then answers with who
else is there and whether anyone else has changed the ticket since the page
was loaded. Nothing here blocks anyone: it's a warning, not a lock.

Rows are throwaway. Anything older than a few minutes means nothing and is
cleared on the next check-in.
"""
from datetime import datetime, timedelta, timezone

from .db import utcnow

CHECK_IN_SECONDS = 20     # how often the page checks in (base.html script)
VIEWING_SECONDS = 60      # seen this recently = still on the page
WRITING_SECONDS = 45      # reported a draft this recently = still writing
FORGET_SECONDS = 600


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _name(row) -> str:
    return (row["display_name"] or "").strip() or row["username"]


def check_in(conn, ticket_id: int, user_id: int, writing: bool) -> None:
    now = utcnow()
    conn.execute(
        "INSERT INTO ticket_presence (ticket_id, user_id, seen_at, typing_at)"
        " VALUES (?,?,?,?) ON CONFLICT(ticket_id, user_id) DO UPDATE SET"
        " seen_at = excluded.seen_at, typing_at = excluded.typing_at",
        (ticket_id, user_id, now, now if writing else None))
    conn.execute("DELETE FROM ticket_presence WHERE seen_at < ?", (_ago(FORGET_SECONDS),))


def leave(conn, ticket_id: int, user_id: int) -> None:
    conn.execute("DELETE FROM ticket_presence WHERE ticket_id = ? AND user_id = ?",
                 (ticket_id, user_id))


def others(conn, ticket_id: int, user_id: int) -> list[dict]:
    """Everyone else on this ticket now: [{"name": ..., "writing": bool}]."""
    rows = conn.execute(
        "SELECT u.username, u.display_name, p.typing_at FROM ticket_presence p"
        " JOIN users u ON u.id = p.user_id"
        " WHERE p.ticket_id = ? AND p.user_id != ? AND p.seen_at >= ?"
        " ORDER BY p.typing_at IS NULL, u.username COLLATE NOCASE",
        (ticket_id, user_id, _ago(VIEWING_SECONDS))).fetchall()
    cutoff = _ago(WRITING_SECONDS)
    return [{"name": _name(r), "writing": bool(r["typing_at"] and r["typing_at"] >= cutoff)}
            for r in rows]


def viewing(conn, user_id: int) -> dict[int, list[str]]:
    """For the ticket list: ticket id -> names of the others viewing it now."""
    out: dict[int, list[str]] = {}
    for r in conn.execute(
            "SELECT p.ticket_id, u.username, u.display_name FROM ticket_presence p"
            " JOIN users u ON u.id = p.user_id"
            " WHERE p.user_id != ? AND p.seen_at >= ?"
            " ORDER BY u.username COLLATE NOCASE", (user_id, _ago(VIEWING_SECONDS))):
        out.setdefault(r["ticket_id"], []).append(_name(r))
    return out


def changed_by(conn, ticket_id: int, user_id: int, since: str) -> str | None:
    """The most recent other person to add to this ticket's timeline after
    `since` (when the page was loaded), or None."""
    if not since:
        return None
    row = conn.execute(
        "SELECT u.username, u.display_name FROM events e JOIN users u ON u.id = e.user_id"
        " WHERE e.ticket_id = ? AND e.user_id != ? AND e.created_at > ?"
        " ORDER BY e.id DESC LIMIT 1", (ticket_id, user_id, since)).fetchone()
    return _name(row) if row else None
