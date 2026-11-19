"""Saved views: named ticket-list filters.

A view is just the ticket list's query string with a name. Everyone gets the
built-in views; each person can save their own on top. Saved views are
personal -- nobody else sees them.

Query strings are normalized before they are stored or compared, so the same
filters always produce the same string, whatever order the browser sent them
in. That is how the ticket list knows which view you're looking at.
"""
import sqlite3
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode

from . import auth
from .db import utcnow

# The ticket list's filter parameters (routers/tickets.py index). Anything
# else in a query string is dropped.
FILTER_KEYS = ("q", "status", "priority", "category", "channel", "show", "assignee",
               "idle", "sort", "overdue")
MAX_VIEWS = 20
MAX_NAME = 40

# (name, query, needs work_tickets). Queries are written normalized.
BUILT_IN = [
    ("All open", "status=open", False),
    ("Needs action", "status=actionable", False),
    ("Assigned to me", "assignee=me&status=open", True),
    ("Unassigned", "assignee=none&status=open", False),
    ("Overdue", "overdue=1&status=open", False),
    ("Untouched 3+ days", "idle=3&status=open", False),
]


def normalize(pairs: str | Iterable[tuple[str, str]]) -> str:
    """A canonical query string for a set of ticket-list filters.

    No status at all means the list's default, Open (any); a blank status means
    every status, so it is kept as `status=`. Sorting by priority is the
    default and is dropped."""
    if isinstance(pairs, str):
        pairs = parse_qsl(pairs.lstrip("?"), keep_blank_values=True)
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in FILTER_KEYS and key not in values:
            values[key] = value.strip()
    values.setdefault("status", "open")
    if values.get("sort") in ("", "priority"):
        values.pop("sort")
    if "overdue" in values:
        if values["overdue"] in ("1", "true", "on", "yes"):
            values["overdue"] = "1"
        else:
            values.pop("overdue")
    kept = {k: v for k, v in values.items() if v or k == "status"}
    return urlencode(sorted(kept.items()))


def built_in(user) -> list[dict]:
    return [{"name": name, "query": query}
            for name, query, needs_work in BUILT_IN
            if not needs_work or auth.can(user, "work_tickets")]


def saved(conn, user_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM saved_views WHERE user_id = ?"
                        " ORDER BY name COLLATE NOCASE", (user_id,)).fetchall()


def save(conn, user_id: int, name: str, query: str) -> int:
    """Create a view, or replace the one with the same name."""
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Give the view a name.")
    if len(name) > MAX_NAME:
        raise ValueError(f"Keep the name under {MAX_NAME} characters.")
    query = normalize(query)
    existing = conn.execute("SELECT id FROM saved_views WHERE user_id = ? AND name = ?",
                            (user_id, name)).fetchone()
    if existing:
        conn.execute("UPDATE saved_views SET query = ? WHERE id = ?", (query, existing["id"]))
        return existing["id"]
    count = conn.execute("SELECT COUNT(*) c FROM saved_views WHERE user_id = ?",
                         (user_id,)).fetchone()["c"]
    if count >= MAX_VIEWS:
        raise ValueError(f"You can keep up to {MAX_VIEWS} views. Delete one first.")
    return conn.execute("INSERT INTO saved_views (user_id, name, query, created_at)"
                        " VALUES (?,?,?,?)", (user_id, name, query, utcnow())).lastrowid


def delete(conn, user_id: int, view_id: int) -> None:
    """Only ever your own."""
    conn.execute("DELETE FROM saved_views WHERE id = ? AND user_id = ?", (view_id, user_id))
