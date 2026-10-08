"""The ticket list, ticket pages, and everything done on a ticket: status,
details, timeline, questions, follow-ups, tags, and queued deletion."""
from typing import Annotated, Literal

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, repo, threadparse
from ..deps import (DB, CurrentUser, Restorer, SoftDeleter, ViewableTicket, Worker,
                    WorkableTicket, ticket_for_worker, visible_owner)
from ..schemas import FollowUp, MessageCreate, StatusChange, TicketCreate, TicketUpdate
from ..web import render, see_other, to_utc

router = APIRouter(tags=["tickets"])

Redirect = {"response_class": RedirectResponse, "status_code": 303}

SortKey = Literal["priority", "due", "newest", "oldest", "updated"]


def _back(ticket_id: int) -> RedirectResponse:
    return see_other(f"/tickets/{ticket_id}")


# ---------------------------------------------------------------- list

@router.get("/", response_class=HTMLResponse, summary="Ticket list and dashboard")
def index(request: Request, conn: DB, user: CurrentUser,
          status: str = "open", priority: str = "", category: str = "",
          channel: str = "", q: str = "", sort: SortKey = "priority", overdue: str = ""):
    """Users see the tickets they own; admins and root see every ticket.

    `status` takes a status key, `open` (any unfinished status), `actionable`
    (the clock is running), or blank for all."""
    # Blank dropdowns submit "" -- coerce here rather than rejecting the request.
    pri = int(priority) if priority.strip().isdigit() else None
    overdue_only = overdue.strip() in ("1", "true", "on", "yes")
    owner = visible_owner(user)
    tickets = repo.list_tickets(
        conn, status=status or None, priority=pri, category=category or None,
        channel=channel or None, q=q or None, overdue_only=overdue_only, sort=sort,
        owner_id=owner)
    return render(request, "index.html", {
        "tickets": tickets,
        "stats": repo.dashboard_stats(conn, owner),
        "follow_ups": repo.due_follow_ups(conn, owner),
        "categories": repo.all_categories(conn),
        "filters": {"status": status, "priority": pri, "category": category,
                    "channel": channel, "q": q, "sort": sort, "overdue": overdue_only},
    })


# ---------------------------------------------------------------- create

@router.get("/tickets/new", response_class=HTMLResponse, summary="New ticket form")
def new_ticket_form(request: Request, conn: DB, user: Worker):
    return render(request, "ticket_new.html", {"categories": repo.all_categories(conn)})


@router.post("/tickets/new", **Redirect, summary="Create a ticket")
def new_ticket(form: Annotated[TicketCreate, Form()], conn: DB, user: Worker):
    """The creator owns the ticket. The customer is matched on email, or created."""
    ticket_id = repo.create_ticket(
        conn, subject=form.subject, body=form.body or None,
        customer_email=form.customer_email or None, customer_name=form.customer_name or None,
        customer_phone=form.customer_phone or None, account_id=form.account_id or None,
        city=form.city or None, channel=form.channel, category=form.category or None,
        subcategory=form.subcategory or None, priority=form.priority, ada=form.ada,
        logged_by=form.logged_by or None, received_at=to_utc(form.received_at),
        due_at=to_utc(form.due_at, end_of_day=True),
        tags=[t.strip() for t in form.tags.split(",") if t.strip()],
        owner_id=user["id"])
    return _back(ticket_id)


# ---------------------------------------------------------------- view

@router.get("/tickets/{ticket_id}", response_class=HTMLResponse, summary="Ticket page")
def ticket_detail(request: Request, ticket: ViewableTicket, conn: DB, user: CurrentUser,
                  template_id: str = ""):
    """Pass `template_id` to pre-fill the reply box from a saved reply."""
    ticket_id = ticket["id"]
    if user["role"] == "root":
        auth.audit(conn, user["id"], "ticket_viewed", ticket["ref"])
    history = []
    if ticket["customer_id"]:
        history = [t for t in repo.list_tickets(conn, customer_id=ticket["customer_id"],
                                                sort="newest", owner_id=visible_owner(user))
                   if t["id"] != ticket_id]
    drafted = ""
    if template_id.strip().isdigit():
        tpl = repo.get_template(conn, int(template_id))
        if tpl:
            drafted = repo.render_template(tpl["body"], ticket,
                                           signer=user["display_name"] or user["username"])
    customer_id = ticket["customer_id"]
    return render(request, "ticket_detail.html", {
        "t": ticket,
        "questions": repo.get_questions(conn, ticket_id),
        "emails": repo.customer_emails(conn, customer_id) if customer_id else [],
        "phones": repo.customer_phones(conn, customer_id) if customer_id else [],
        "events": repo.get_events(conn, ticket_id),
        "tags": repo.get_tags(conn, ticket_id),
        "all_tags": repo.all_tags(conn),
        "categories": repo.all_categories(conn),
        "templates_list": repo.list_templates(conn),
        "history": history, "drafted": drafted,
    })


# ---------------------------------------------------------------- work

@router.post("/tickets/{ticket_id}/status", **Redirect, summary="Change status")
def change_status(ticket: WorkableTicket, form: Annotated[StatusChange, Form()], conn: DB):
    """Moving out of an active status pauses the SLA clock; moving back resumes it."""
    repo.set_status(conn, ticket["id"], form.status)
    return _back(ticket["id"])


@router.post("/tickets/{ticket_id}/update", **Redirect, summary="Edit ticket details")
def update_ticket(ticket: WorkableTicket, form: Annotated[TicketUpdate, Form()], conn: DB):
    changes = form.model_dump()
    changes["due_at"] = to_utc(form.due_at, end_of_day=True)
    repo.update_fields(conn, ticket["id"], changes)
    return _back(ticket["id"])


@router.post("/tickets/{ticket_id}/note", **Redirect, summary="Add a private note")
def add_note(ticket: WorkableTicket, body: Annotated[str, Form()], conn: DB):
    repo.add_note(conn, ticket["id"], body)
    return _back(ticket["id"])


@router.post("/tickets/{ticket_id}/message", **Redirect, summary="Log correspondence")
def log_message(ticket: WorkableTicket, form: Annotated[MessageCreate, Form()],
                conn: DB, user: Worker):
    """An inbound message from whoever the ticket is waiting on reopens it.
    Outbound messages default to your display name as the author."""
    author = form.author.strip() or (
        user["display_name"] if form.kind.startswith("out_") else None) or None
    repo.log_message(conn, ticket["id"], form.kind, form.body, author=author,
                     occurred_at=to_utc(form.occurred_at))
    if form.track_questions:
        for question in repo.extract_questions(form.body):
            repo.add_question(conn, ticket["id"], question)
    return _back(ticket["id"])


# ---------------------------------------------------------------- thread import

@router.post("/tickets/{ticket_id}/import", response_class=HTMLResponse,
             summary="Split a pasted email thread for review")
def import_preview(request: Request, ticket: WorkableTicket, raw: Annotated[str, Form()],
                   conn: DB, user: Worker):
    """Nothing is saved here -- this renders the split for you to correct first."""
    emails = {r["email"] for r in repo.customer_emails(conn, ticket["customer_id"])} \
        if ticket["customer_id"] else set()
    if ticket["customer_email"]:
        emails.add(ticket["customer_email"])
    hints = {*config.OWNER_HINTS, user["username"].lower(),
             (user["display_name"] or "").lower()}
    segments = threadparse.guess_kinds(threadparse.split_thread(raw), emails, hints)
    return render(request, "thread_import.html", {"t": ticket, "segments": segments, "raw": raw})


@router.post("/tickets/{ticket_id}/import/confirm", **Redirect,
             summary="Save the reviewed thread")
async def import_confirm(request: Request, ticket: WorkableTicket, conn: DB):
    """Takes indexed fields from the review page: `include_N`, `kind_N`,
    `body_N`, `author_N`, `when_N`."""
    form = await request.form()
    index = 0
    while f"body_{index}" in form:
        if form.get(f"include_{index}"):
            repo.log_message(
                conn, ticket["id"], str(form.get(f"kind_{index}", "note")),
                str(form.get(f"body_{index}", "")),
                author=str(form.get(f"author_{index}", "")).strip() or None,
                occurred_at=to_utc(str(form.get(f"when_{index}", ""))),
                auto_status=False)
        index += 1
    return _back(ticket["id"])


# ---------------------------------------------------------------- timeline entries

@router.post("/events/{event_id}/void", **Redirect, summary="Strike through an entry")
def void_event(event_id: int, conn: DB, user: Worker,
               reason: Annotated[str, Form()] = ""):
    """Not deletion: the entry stays visible, struck through, and can be restored."""
    ticket_id = ticket_for_worker(conn, user, repo.event_ticket_id(conn, event_id))
    repo.void_event(conn, event_id, reason)
    return _back(ticket_id)


@router.post("/events/{event_id}/unvoid", **Redirect, summary="Restore a struck entry")
def unvoid_event(event_id: int, conn: DB, user: Worker):
    ticket_id = ticket_for_worker(conn, user, repo.event_ticket_id(conn, event_id))
    repo.unvoid_event(conn, event_id)
    return _back(ticket_id)


@router.post("/events/{event_id}/retime", **Redirect, summary="Correct an entry's time")
def retime_event(event_id: int, conn: DB, user: Worker,
                 occurred_at: Annotated[str, Form()] = ""):
    ticket_id = ticket_for_worker(conn, user, repo.event_ticket_id(conn, event_id))
    if when := to_utc(occurred_at or None):
        repo.set_event_time(conn, event_id, when)
    return _back(ticket_id)


# ---------------------------------------------------------------- questions

@router.post("/tickets/{ticket_id}/questions/add", **Redirect, summary="Track a question")
def add_question(ticket: WorkableTicket, text: Annotated[str, Form()], conn: DB):
    repo.add_question(conn, ticket["id"], text)
    return _back(ticket["id"])


@router.post("/questions/{question_id}/answer", **Redirect,
             summary="Record (or clear) an answer")
def answer_question(question_id: int, conn: DB, user: Worker,
                    answer: Annotated[str, Form()] = "", clear: Annotated[bool, Form()] = False):
    ticket_id = ticket_for_worker(conn, user, repo.question_ticket_id(conn, question_id))
    if clear:
        repo.unanswer_question(conn, question_id)
    else:
        repo.answer_question(conn, question_id, answer)
    return _back(ticket_id)


@router.post("/questions/{question_id}/delete", **Redirect, summary="Remove a question")
def delete_question(question_id: int, conn: DB, user: Worker):
    ticket_id = ticket_for_worker(conn, user, repo.question_ticket_id(conn, question_id))
    repo.delete_question(conn, question_id)
    return _back(ticket_id)


# ---------------------------------------------------------------- follow-ups and tags

@router.post("/tickets/{ticket_id}/followup", **Redirect,
             summary="Set or complete a follow-up reminder")
def set_followup(ticket: WorkableTicket, form: Annotated[FollowUp, Form()], conn: DB):
    if form.done:
        repo.clear_follow_up(conn, ticket["id"])
    else:
        repo.set_follow_up(conn, ticket["id"], to_utc(form.follow_up_at), form.follow_up_note)
    return _back(ticket["id"])


@router.post("/tickets/{ticket_id}/tag", **Redirect, summary="Add or remove a tag")
def tag_ticket(ticket: WorkableTicket, conn: DB,
               tag: Annotated[str, Form()] = "", remove: Annotated[str, Form()] = ""):
    if remove:
        repo.remove_tag(conn, ticket["id"], remove)
    elif tag:
        repo.add_tag(conn, ticket["id"], tag)
    return _back(ticket["id"])


# ---------------------------------------------------------------- queued deletion

@router.post("/tickets/{ticket_id}/delete", **Redirect, summary="Queue a ticket for deletion")
def delete_ticket(ticket: ViewableTicket, conn: DB, user: SoftDeleter):
    """Admin only. Recoverable from the trash until the retention window passes."""
    repo.soft_delete_ticket(conn, ticket["id"])
    auth.audit(conn, user["id"], "ticket_queued_for_deletion", ticket["ref"])
    return see_other("/")


@router.post("/tickets/{ticket_id}/restore", **Redirect, summary="Restore from the trash")
def restore_ticket(ticket: ViewableTicket, conn: DB, user: Restorer):
    repo.restore_ticket(conn, ticket["id"])
    auth.audit(conn, user["id"], "ticket_restored", ticket["ref"])
    return see_other("/trash")
