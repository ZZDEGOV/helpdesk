"""FastAPI dependencies: the database connection, the signed-in user, and
permission checks.

Every route declares what it needs in its signature, e.g.

    def change_status(ticket: WorkableTicket, form: ..., conn: DB): ...

so the rule a route enforces is visible right where the route is defined.
Failures raise the exceptions below; main.py turns them into a redirect or an
error page for browsers, and a JSON error for /api.
"""
import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request

from . import auth, repo
from .db import Connection, connect

SESSION_COOKIE = "hd_session"


# ---------------------------------------------------------------- failures

class SetupRequired(Exception):
    """No root account exists yet; everything goes to /setup."""


class LoginRequired(Exception):
    pass


class PasswordChangeRequired(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, message: str = "You don't have access to that.", back: str = "/"):
        self.message, self.back = message, back


class NotFound(Exception):
    def __init__(self, message: str = "That doesn't exist, or you don't have access to it.",
                 back: str = "/"):
        self.message, self.back = message, back


# ---------------------------------------------------------------- database

def get_db() -> Iterator[Connection]:
    """One connection per request: committed if the route succeeds, rolled back
    if it raises. Used with scope="function" so the commit lands before the
    response is sent -- otherwise a redirect could reach the browser, and the
    follow-up GET be served, before the write is visible."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


DB = Annotated[Connection, Depends(get_db, scope="function")]


# ---------------------------------------------------------------- user

def _signed_in(request: Request, conn: DB) -> sqlite3.Row:
    """The session's user, without the forced-password-change check. Only the
    change-password and logout routes use this directly."""
    if not auth.has_root(conn):
        raise SetupRequired()
    user = auth.session_user(conn, request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise LoginRequired()
    request.state.user = user          # base.html reads this for the nav bar
    conn.user_id = user["id"]          # attributes timeline entries (repo.log_event)
    return user


def _current_user(user: Annotated[sqlite3.Row, Depends(_signed_in)]) -> sqlite3.Row:
    if user["must_change_password"]:
        raise PasswordChangeRequired()
    return user


SignedIn = Annotated[sqlite3.Row, Depends(_signed_in)]
CurrentUser = Annotated[sqlite3.Row, Depends(_current_user)]


_ROOT_READ_ONLY = ("The root account administers the system and cannot modify "
                   "tickets. Sign in with a ticket account.")


def require(capability: str):
    """Dependency factory: the current user, if they hold `capability`."""
    def check(user: CurrentUser) -> sqlite3.Row:
        if not auth.can(user, capability):
            if user["role"] == "root" and capability in ("work_tickets", "soft_delete"):
                raise Forbidden(_ROOT_READ_ONLY, back="/root")
            if capability == "view_trash":
                raise Forbidden("Deleting tickets is handled by an admin. "
                                "Ask one to remove it for you.")
            raise Forbidden()
        return user
    return Depends(check)


Worker = Annotated[sqlite3.Row, require("work_tickets")]
SoftDeleter = Annotated[sqlite3.Row, require("soft_delete")]
Restorer = Annotated[sqlite3.Row, require("restore")]
TrashViewer = Annotated[sqlite3.Row, require("view_trash")]
CustomerViewer = Annotated[sqlite3.Row, require("view_customers")]
RootUser = Annotated[sqlite3.Row, require("manage_users")]


def visible_owner(user: sqlite3.Row) -> int | None:
    """The owner_id to scope ticket queries by: None means "all tickets"."""
    return None if auth.can(user, "view_all_tickets") else user["id"]


# ---------------------------------------------------------------- tickets

def _viewable_ticket(ticket_id: int, conn: DB, user: CurrentUser) -> sqlite3.Row:
    ticket = repo.get_ticket(conn, ticket_id)
    # Same answer for "missing" and "not yours", so ticket ids can't be probed.
    if ticket is None or not auth.can_view_ticket(user, ticket):
        raise NotFound()
    return ticket


def _workable_ticket(ticket: Annotated[sqlite3.Row, Depends(_viewable_ticket)],
                     user: Worker) -> sqlite3.Row:
    return ticket


ViewableTicket = Annotated[sqlite3.Row, Depends(_viewable_ticket)]
WorkableTicket = Annotated[sqlite3.Row, Depends(_workable_ticket)]


def ticket_for_worker(conn: Connection, user: sqlite3.Row, ticket_id: int | None) -> int:
    """For routes keyed by an event or question id: resolve and check the ticket
    it belongs to."""
    ticket = repo.get_ticket(conn, ticket_id) if ticket_id else None
    if ticket is None or not auth.can_view_ticket(user, ticket):
        raise NotFound()
    return ticket["id"]
