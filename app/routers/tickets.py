"""The ticket list, ticket pages, and everything done on a ticket: status,
details, assignment, timeline, questions, follow-ups, tags, and queued deletion.
Also the list's saved views and bulk actions, and the ticket page's presence
check-ins."""
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, BackgroundTasks, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import (assignment, auth, config, digest, groups, presence, repo, threadparse,
                views)
from ..db import utcnow
from ..deps import (DB, CurrentUser, Forbidden, Restorer, SoftDeleter, ViewableTicket,
                    Worker, WorkableTicket, ticket_for_worker, viewer_id)
from ..schemas import (AssigneeChange, BulkAction, FollowUp, GroupChange, MessageCreate,
                       PresenceCheckIn, PresenceOut, StatusChange, TicketCreate,
                       TicketUpdate, ViewSave)
from ..web import render, see_other, to_utc

router = APIRouter(tags=["tickets"])

Redirect = {"response_class": RedirectResponse, "status_code": 303}

SortKey = Literal["priority", "due", "newest", "oldest", "updated"]
SORT_KEYS = SortKey.__args__


def _back(ticket_id: int) -> RedirectResponse:
    return see_other(f"/tickets/{ticket_id}")


def _list_url(query: str, **message: str) -> str:
    """The ticket list with these filters, plus an optional notice or error."""
    query = views.normalize(query)
    extra = urlencode({k: v for k, v in message.items() if v})
    return "/?" + "&".join(part for part in (query, extra) if part)


# ---------------------------------------------------------------- list

def _list_filters(user, params: dict[str, str]) -> tuple[dict, str]:
    """Ticket-list query parameters -> repo.list_tickets keywords, and the sort.

    Blank dropdowns submit "" -- they're coerced here rather than rejected. A
    missing `status` means Open (any); a blank one means every status."""
    def get(key: str) -> str:
        return (params.get(key) or "").strip()

    status = params.get("status", "open")
    priority, show, assignee, idle = get("priority"), get("show"), get("assignee"), get("idle")
    filters = {
        "status": (status or "").strip() or None,
        "priority": int(priority) if priority.isdigit() else None,
        "category": get("category") or None,
        "channel": get("channel") or None,
        "q": get("q") or None,
        "overdue_only": get("overdue") in ("1", "true", "on", "yes"),
        "viewer_id": viewer_id(user),
        "owner_id": user["id"] if show == "mine" else None,
        "group_id": int(show) if show.isdigit() else None,
        "assignee_id": user["id"] if assignee == "me" else
                       int(assignee) if assignee.isdigit() else None,
        "unassigned": assignee == "none",
        "idle_days": int(idle) if idle.isdigit() and 1 <= int(idle) <= 365 else None,
    }
    sort = get("sort") if get("sort") in SORT_KEYS else "priority"
    return filters, sort


def _view_chips(conn, user, current: str) -> list[dict]:
    """Built-in views (with a count of what's in them) then the person's own."""
    chips = []
    for view in views.built_in(user):
        filters, _ = _list_filters(user, dict(parse_qsl(view["query"], keep_blank_values=True)))
        chips.append({**view, "count": repo.count_tickets(conn, **filters), "id": None,
                      "on": view["query"] == current})
    for row in views.saved(conn, user["id"]):
        chips.append({"name": row["name"], "query": row["query"], "count": None,
                      "id": row["id"], "on": row["query"] == current})
    return chips


@router.get("/", response_class=HTMLResponse, summary="Ticket list and dashboard")
def index(request: Request, conn: DB, user: CurrentUser,
          status: str = "open", priority: str = "", category: str = "",
          channel: str = "", q: str = "", sort: SortKey = "priority", overdue: str = "",
          show: str = "", assignee: str = "", idle: str = "",
          notice: str = "", error: str = ""):
    """Users see the tickets they own or are assigned, plus those filed under a
    group they belong to; admins and root see every ticket.

    `status` takes a status key, `open` (any unfinished status), `actionable`
    (the clock is running), or blank for all. `show` narrows the list to tickets
    you own (`mine`) or to one group's id. `assignee` is `me`, `none`
    (unassigned) or a user id. `idle` is a number of days without any change."""
    params = {"status": status, "priority": priority, "category": category,
              "channel": channel, "q": q, "sort": sort, "overdue": overdue, "show": show,
              "assignee": assignee, "idle": idle}
    filters, sort = _list_filters(user, params)
    tickets = repo.list_tickets(conn, sort=sort, **filters)
    viewer = viewer_id(user)
    current = views.normalize(request.query_params.multi_items())
    works = auth.can(user, "work_tickets")
    more = [k for k in ("priority", "category", "show", "assignee", "idle", "channel")
            if params[k].strip()]
    return render(request, "index.html", {
        "tickets": tickets,
        "follow_ups": repo.due_follow_ups(conn, viewer),
        "more_filters": len(more) + (1 if filters["overdue_only"] else 0),
        "is_default": current == views.normalize(""),
        "categories": repo.all_categories(conn),
        "groups": groups.selectable(conn, user),
        "people": assignment.choices(conn, user) if works else [],
        "viewing": presence.viewing(conn, user["id"]),
        "chips": _view_chips(conn, user, current),
        "current_query": current,
        "notice": notice, "error": error, "can_bulk": works,
        "filters": {"status": status, "priority": filters["priority"], "category": category,
                    "channel": channel, "q": q, "sort": sort,
                    "overdue": filters["overdue_only"], "show": show.strip(),
                    "assignee": assignee.strip(), "idle": idle.strip()},
    })


# ---------------------------------------------------------------- saved views

@router.post("/views", **Redirect, summary="Save the ticket list's filters as a view")
def save_view(form: Annotated[ViewSave, Form()], conn: DB, user: CurrentUser):
    """Personal to you. Saving under a name you already use replaces that view."""
    try:
        views.save(conn, user["id"], form.name, form.query)
    except ValueError as exc:
        return see_other(_list_url(form.query, error=str(exc)))
    return see_other(_list_url(form.query))


@router.post("/views/{view_id}/delete", **Redirect, summary="Delete one of your views")
def delete_view(view_id: int, conn: DB, user: CurrentUser):
    views.delete(conn, user["id"], view_id)
    return see_other("/")


# ---------------------------------------------------------------- bulk actions

@router.post("/tickets/bulk", **Redirect, summary="Apply one action to many tickets")
def bulk(form: Annotated[BulkAction, Form()], conn: DB, user: Worker):
    """`action` picks which of the other fields is used: `status`, `assignee`
    (`me`, `none` or a user id), `group` (`none` or a group id) or `tag`.

    Each ticket is checked on its own, exactly as if the action were done on its
    page: tickets you can't see, people a ticket can't be assigned to, and
    sharing changes on tickets you don't own (unless you're an admin) are
    skipped and counted rather than failing the whole batch."""
    def done(**message: str) -> RedirectResponse:
        return see_other(_list_url(form.back, **message))

    if not form.ids:
        return done(error="Tick at least one ticket first.")
    target_user = target_group = None
    if form.action == "status" and form.status is None:
        return done(error="Choose a status to set.")
    if form.action == "assign":
        choice = form.assignee.strip()
        if choice not in ("me", "none") and not choice.isdigit():
            return done(error="Choose who to assign them to.")
        target_user = user["id"] if choice == "me" else int(choice) if choice.isdigit() else None
    if form.action == "group":
        choice = form.group.strip()
        if choice != "none" and not choice.isdigit():
            return done(error="Choose a group, or Only the owner.")
        target_group = int(choice) if choice.isdigit() else None
        _check_group(conn, user, target_group)
    tag = form.tag.strip()
    if form.action == "tag" and not tag:
        return done(error="Type the tag to add.")

    changed = skipped = 0
    for ticket_id in list(dict.fromkeys(form.ids))[:500]:
        ticket = repo.get_ticket(conn, ticket_id)
        if (ticket is None or ticket["deleted_at"]
                or not auth.can_view_ticket(conn, user, ticket)):
            skipped += 1
            continue
        if form.action == "status":
            repo.set_status(conn, ticket_id, form.status)
        elif form.action == "assign":
            if not assignment.can_assign_to(conn, ticket, target_user, by=user):
                skipped += 1
                continue
            assignment.set_assignee(conn, ticket_id, target_user)
        elif form.action == "group":
            if not _can_regroup(user, ticket):
                skipped += 1
                continue
            repo.set_group(conn, ticket_id, target_group)
        else:
            repo.add_tag(conn, ticket_id, tag)
        changed += 1

    message = f"{changed} ticket{'' if changed == 1 else 's'} updated"
    if skipped:
        message += f"; {skipped} skipped (not yours to change, or that person can't see it)"
    return done(notice=message)


# ---------------------------------------------------------------- create

@router.get("/tickets/new", response_class=HTMLResponse, summary="New ticket form")
def new_ticket_form(request: Request, conn: DB, user: Worker):
    """Someone in exactly one group has it preselected, so a team's tickets are
    shared unless the person filing chooses otherwise."""
    mine = groups.groups_of(conn, user["id"])
    return render(request, "ticket_new.html", {
        "categories": repo.all_categories(conn),
        "groups": groups.selectable(conn, user),
        "people": assignment.picker(conn, user),
        "default_group": mine[0]["id"] if len(mine) == 1 else None})


def _check_group(conn, user, group_id: int | None) -> None:
    """A ticket can only be put in a group the person belongs to (admins: any)."""
    if group_id is not None and group_id not in {g["id"] for g in groups.selectable(conn, user)}:
        raise Forbidden("You can't share a ticket with a group you aren't in.")


@router.post("/tickets/new", **Redirect, summary="Create a ticket")
def new_ticket(form: Annotated[TicketCreate, Form()], conn: DB, user: Worker,
               background: BackgroundTasks):
    """The creator owns the ticket. The customer is matched on email, or created.
    `group_id` shares it with that group's members; leave it out to keep the
    ticket to yourself. `assignee_id` hands it to someone straight away (same
    rules as the ticket page's Assigned to).

    If root has turned on new-ticket emails, digest subscribers are notified
    after the response is sent, so a slow mail server never delays the page."""
    _check_group(conn, user, form.group_id)
    if not assignment.can_assign_to(conn, {"owner_id": user["id"], "group_id": form.group_id},
                                    form.assignee_id, by=user):
        raise Forbidden("That person isn't in the group you chose, so they wouldn't be able "
                        "to see the ticket. Share it with their group, or leave it unassigned.",
                        back="/tickets/new")
    ticket_id = repo.create_ticket(
        conn, subject=form.subject, body=form.body or None,
        customer_email=form.customer_email or None, customer_name=form.customer_name or None,
        customer_phone=form.customer_phone or None, account_id=form.account_id or None,
        city=form.city or None, channel=form.channel, category=form.category or None,
        subcategory=form.subcategory or None, priority=form.priority, ada=form.ada,
        logged_by=form.logged_by or None, received_at=to_utc(form.received_at),
        due_at=to_utc(form.due_at, end_of_day=True),
        tags=[t.strip() for t in form.tags.split(",") if t.strip()],
        owner_id=user["id"], group_id=form.group_id)
    if form.assignee_id:
        assignment.set_assignee(conn, ticket_id, form.assignee_id)
    background.add_task(digest.notify_new_ticket, ticket_id)
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
                                                sort="newest", viewer_id=viewer_id(user))
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
        "groups": groups.selectable(conn, user),
        "can_regroup": _can_regroup(user, ticket),
        "assignable": assignment.assignable(conn, ticket, by=user),
        "loaded_at": utcnow(),
        "check_in_seconds": presence.CHECK_IN_SECONDS,
    })


def _can_regroup(user, ticket) -> bool:
    """Only the owner or an admin decides who a ticket is shared with -- a
    teammate who can work it shouldn't be able to pull it out of the team."""
    return auth.can(user, "work_tickets") and (
        ticket["owner_id"] == user["id"] or auth.can(user, "view_all_tickets"))


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


@router.post("/tickets/{ticket_id}/group", **Redirect, summary="Change who a ticket is shared with")
def change_group(ticket: WorkableTicket, form: Annotated[GroupChange, Form()], conn: DB,
                 user: Worker):
    """Owner or admin only. A blank `group_id` makes the ticket private to its owner."""
    if not _can_regroup(user, ticket):
        raise Forbidden("Only the ticket's owner or an admin can change who it is shared with.",
                        back=f"/tickets/{ticket['id']}")
    _check_group(conn, user, form.group_id)
    repo.set_group(conn, ticket["id"], form.group_id)
    return _back(ticket["id"])


@router.post("/tickets/{ticket_id}/assign", **Redirect, summary="Assign or unassign")
def assign(ticket: WorkableTicket, form: Annotated[AssigneeChange, Form()], conn: DB,
           user: Worker):
    """Anyone who can work the ticket may hand it to anyone who can see it: its
    owner, its group's members, or an admin. Admins may hand it to anyone.
    Blank `assignee_id` = nobody."""
    if form.assignee_id == ticket["assignee_id"]:
        return _back(ticket["id"])          # e.g. Save pressed without a change
    if not assignment.can_assign_to(conn, ticket, form.assignee_id, by=user):
        raise Forbidden("That person can't see this ticket, so it can't be assigned to "
                        "them. Share it with their group first.",
                        back=f"/tickets/{ticket['id']}")
    assignment.set_assignee(conn, ticket["id"], form.assignee_id)
    return _back(ticket["id"])


# ---------------------------------------------------------------- presence

@router.post("/tickets/{ticket_id}/presence", response_model=PresenceOut,
             summary="Check in from an open ticket page")
def presence_check_in(ticket: ViewableTicket, form: Annotated[PresenceCheckIn, Form()],
                      conn: DB, user: CurrentUser):
    """Called by the ticket page about every 20 seconds while it's visible.
    Records you as on this ticket (and as writing, if your reply box has
    something in it) and answers with who else is on it, and who else has
    added to its timeline since you loaded the page. Root's check-ins aren't
    recorded: root can't change tickets, so it can't collide with anyone."""
    if auth.can(user, "work_tickets"):
        presence.check_in(conn, ticket["id"], user["id"], form.writing)
    return {"others": presence.others(conn, ticket["id"], user["id"]),
            "changed_by": presence.changed_by(conn, ticket["id"], user["id"], form.since)}


@router.post("/tickets/{ticket_id}/presence/leave", status_code=204,
             summary="Left the ticket page")
def presence_leave(ticket: ViewableTicket, conn: DB, user: CurrentUser):
    """Sent as the page closes, so others' warnings clear straight away."""
    presence.leave(conn, ticket["id"], user["id"])
    return Response(status_code=204)


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
