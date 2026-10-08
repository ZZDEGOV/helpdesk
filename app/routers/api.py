"""Read-only JSON API.

Uses the same session cookie and the same visibility rules as the web pages:
sign in through the browser, then these work (and are try-able from /docs).
"""
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from .. import repo
from ..deps import DB, CurrentUser, ViewableTicket, visible_owner
from ..schemas import EventOut, TicketDetail, TicketSummary, UserOut

router = APIRouter(prefix="/api/v1", tags=["api"])


def _summary(ticket) -> dict:
    data = dict(ticket)
    data.setdefault("owner_username", None)
    data["overdue"] = repo.is_overdue(ticket)
    data["active_seconds"] = repo.live_active_seconds(ticket)
    return data


@router.get("/me", response_model=UserOut, summary="The signed-in user")
def me(user: CurrentUser):
    return dict(user)


@router.get("/tickets", response_model=list[TicketSummary], summary="List tickets")
def list_tickets(
    conn: DB, user: CurrentUser,
    status: Annotated[str | None, Query(
        description="A status key, `open` (any unfinished), or `actionable` (clock running)."
    )] = "open",
    q: Annotated[str | None, Query(description="Search ref, subject, body, customer")] = None,
    customer_id: int | None = None,
    overdue: bool = False,
    sort: Literal["priority", "due", "newest", "oldest", "updated"] = "priority",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    """Users get the tickets they own; admins and root get every ticket."""
    rows = repo.list_tickets(conn, status=status or None, q=q, customer_id=customer_id,
                             overdue_only=overdue, sort=sort, limit=limit,
                             owner_id=visible_owner(user))
    return [_summary(t) for t in rows]


@router.get("/tickets/{ticket_id}", response_model=TicketDetail, summary="One ticket")
def get_ticket(ticket: ViewableTicket, conn: DB):
    """Includes tags and the full timeline, struck-through entries flagged `voided`."""
    events = [EventOut(id=e["id"], type=e["type"], body=e["body"], author=e["author"],
                       user_id=e["user_id"], occurred_at=e["occurred_at"] or e["created_at"],
                       voided=e["voided_at"] is not None)
              for e in repo.get_events(conn, ticket["id"])]
    return {**_summary(ticket), "tags": repo.get_tags(conn, ticket["id"]), "events": events}
