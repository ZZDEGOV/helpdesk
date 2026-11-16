"""Groups (teams): who is in them, and which groups someone can file a ticket under.

A ticket placed in a group is visible to every member of that group, on top of
its owner and the admins. A ticket with no group stays private to its owner.
Root manages groups and membership; the visibility rule itself lives in
auth.can_view_ticket and repo.VISIBLE_SQL.
"""
import sqlite3

from . import auth
from .db import utcnow


def list_groups(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT g.*,"
        " (SELECT COUNT(*) FROM group_members m WHERE m.group_id = g.id) AS member_count,"
        " (SELECT COUNT(*) FROM tickets t WHERE t.group_id = g.id"
        "   AND t.deleted_at IS NULL) AS ticket_count"
        " FROM user_groups g ORDER BY g.name COLLATE NOCASE").fetchall()


def get_group(conn, group_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM user_groups WHERE id = ?", (group_id,)).fetchone()


def _clean_name(conn, name: str, except_id: int | None = None) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("A group needs a name.")
    if len(name) > 60:
        raise ValueError("Keep the group name under 60 characters.")
    clash = conn.execute("SELECT id FROM user_groups WHERE name = ? COLLATE NOCASE",
                         (name,)).fetchone()
    if clash and clash["id"] != except_id:
        raise ValueError(f"There is already a group called '{name}'.")
    return name


def create_group(conn, name: str) -> int:
    name = _clean_name(conn, name)
    return conn.execute("INSERT INTO user_groups (name, created_at) VALUES (?,?)",
                        (name, utcnow())).lastrowid


def rename_group(conn, group_id: int, name: str) -> None:
    conn.execute("UPDATE user_groups SET name = ? WHERE id = ?",
                 (_clean_name(conn, name, except_id=group_id), group_id))


def delete_group(conn, group_id: int) -> int:
    """Removes the group. Its tickets are kept and become private to their
    owners again. Returns how many tickets that was."""
    count = conn.execute("SELECT COUNT(*) c FROM tickets WHERE group_id = ?",
                         (group_id,)).fetchone()["c"]
    # Explicit, not left to ON DELETE SET NULL: a database upgraded from before
    # groups may have had its tickets table rebuilt without that clause.
    conn.execute("UPDATE tickets SET group_id = NULL WHERE group_id = ?", (group_id,))
    conn.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
    conn.execute("DELETE FROM user_groups WHERE id = ?", (group_id,))
    return count


# ---------------------------------------------------------------- members

def members(conn, group_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT u.id, u.username, u.display_name, u.email, u.role, u.is_active"
        " FROM group_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.group_id = ? ORDER BY u.username COLLATE NOCASE", (group_id,)).fetchall()


def add_member(conn, group_id: int, user_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?,?)",
                 (group_id, user_id))


def remove_member(conn, group_id: int, user_id: int) -> None:
    conn.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
                 (group_id, user_id))


def is_member(conn, group_id: int | None, user_id: int) -> bool:
    if group_id is None:
        return False
    return bool(conn.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id)).fetchone())


def groups_of(conn, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT g.* FROM user_groups g JOIN group_members m ON m.group_id = g.id"
        " WHERE m.user_id = ? ORDER BY g.name COLLATE NOCASE", (user_id,)).fetchall()


def selectable(conn, user) -> list[sqlite3.Row]:
    """Groups this person may put a ticket in: their own, or any for someone
    who already sees every ticket."""
    if auth.can(user, "view_all_tickets"):
        return list_groups(conn)
    return groups_of(conn, user["id"])


def adopt_tickets(conn, group_id: int, user_id: int) -> int:
    """Put a member's ungrouped tickets into the group, so work that predates
    the group (or was filed as "only me") becomes visible to the team."""
    return conn.execute(
        "UPDATE tickets SET group_id = ? WHERE owner_id = ? AND group_id IS NULL"
        " AND deleted_at IS NULL", (group_id, user_id)).rowcount
