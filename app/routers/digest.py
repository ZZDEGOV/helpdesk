"""Root's page for the open-ticket digest: who receives it, what each person
sees, when it goes out, and a button to send it now.

Root-only, like everything under /root."""
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, digest, mailer
from ..deps import DB, NotFound, RootUser, require
from ..schemas import DigestSchedule, DigestScopeChange, DigestSend, DigestSubscribe
from ..web import render, see_other

router = APIRouter(prefix="/root/digest", tags=["root"],
                   dependencies=[require("manage_users")])

Redirect = {"response_class": RedirectResponse, "status_code": 303}


def _back(*, notice: str = "", error: str = "") -> RedirectResponse:
    query = f"?error={quote(error)}" if error else (f"?notice={quote(notice)}" if notice else "")
    return see_other("/root/digest" + query)


@router.get("", response_class=HTMLResponse, summary="Open-ticket digest settings")
def digest_page(request: Request, conn: DB, notice: str = "", error: str = ""):
    subs = digest.subscribers(conn)
    taken = {s["user_id"] for s in subs}
    schedule = digest.get_schedule(conn)
    outbox = (len(list(config.OUTBOX_DIR.glob("*.eml")))
              if config.OUTBOX_DIR.is_dir() else 0)
    return render(request, "root_digest.html", {
        "subscribers": [{"row": s, "skip": digest.skip_reason(s)} for s in subs],
        "candidates": [u for u in auth.list_users(conn, include_inactive=False)
                       if u["id"] not in taken],
        "schedule": schedule, "schedule_text": digest.describe_schedule(schedule),
        "day_names": digest.DAY_NAMES, "scope_labels": digest.SCOPE_LABELS,
        "mail_mode": mailer.mode(), "mail_text": mailer.describe(), "outbox_count": outbox,
        "sends": digest.recent_sends(conn), "notice": notice, "error": error})


# ---------------------------------------------------------------- subscribers

@router.post("/subscribers", **Redirect, summary="Add a subscriber")
def digest_subscribe(form: Annotated[DigestSubscribe, Form()], conn: DB, me: RootUser):
    """The account must be enabled and have an email address. Pass `email` to
    set the address in the same step."""
    user = auth.get_user(conn, form.user_id)
    if user is None or not user["is_active"]:
        return _back(error="That account doesn't exist or is disabled.")
    if form.email.strip():
        try:
            auth.update_user(conn, user["id"], email=form.email)
        except ValueError as exc:
            return _back(error=str(exc))
        user = auth.get_user(conn, user["id"])
    if not user["email"]:
        return _back(error=f"{user['username']} has no email address yet. Enter one "
                           "in the Email box when adding them.")
    digest.subscribe(conn, user["id"], form.scope)
    auth.audit(conn, me["id"], "digest_subscribed", f"user:{user['username']}",
               f"scope={form.scope} email={user['email']}")
    return _back(notice=f"{user['username']} will receive the digest.")


@router.post("/subscribers/{user_id}/scope", **Redirect,
             summary="Change what a subscriber sees")
def digest_scope(user_id: int, form: Annotated[DigestScopeChange, Form()], conn: DB,
                 me: RootUser):
    sub = digest.get_subscriber(conn, user_id)
    if sub is None:
        return _back(error="That person is no longer a subscriber.")
    digest.subscribe(conn, user_id, form.scope)
    auth.audit(conn, me["id"], "digest_scope_changed", f"user:{sub['username']}",
               f"scope={form.scope}")
    return _back(notice=f"{sub['username']} now sees: "
                        f"{digest.SCOPE_LABELS[form.scope].lower()}.")


@router.post("/subscribers/{user_id}/remove", **Redirect, summary="Remove a subscriber")
def digest_unsubscribe(user_id: int, conn: DB, me: RootUser):
    sub = digest.get_subscriber(conn, user_id)
    if sub is not None:
        digest.unsubscribe(conn, user_id)
        auth.audit(conn, me["id"], "digest_unsubscribed", f"user:{sub['username']}")
    return _back(notice="Removed." if sub else "")


# ---------------------------------------------------------------- schedule and sending

@router.post("/schedule", **Redirect, summary="Set when the digest goes out")
def digest_schedule(form: Annotated[DigestSchedule, Form()], conn: DB, me: RootUser):
    """Tick no days to turn the schedule off."""
    digest.save_schedule(conn, form.days, form.hour, form.on_new_ticket)
    schedule = digest.get_schedule(conn)
    auth.audit(conn, me["id"], "digest_schedule_changed", "",
               digest.describe_schedule(schedule)
               + ("; also on new ticket" if schedule["on_new_ticket"] else ""))
    return _back(notice="Schedule saved: " + digest.describe_schedule(schedule) + ".")


@router.get("/preview/{user_id}", response_class=HTMLResponse,
            summary="The email a subscriber would get right now")
def digest_preview(user_id: int, conn: DB):
    """Shows the email without sending anything."""
    sub = digest.get_subscriber(conn, user_id)
    if sub is None:
        raise NotFound("That person is not a subscriber.", back="/root/digest")
    return HTMLResponse(digest.build(conn, sub).html)


@router.post("/send", **Redirect, summary="Send the digest now")
def digest_send(form: Annotated[DigestSend, Form()], conn: DB, me: RootUser):
    """To every subscriber, or only `user_id`."""
    result = digest.send_all(conn, "manual", actor_id=me["id"], only_user_id=form.user_id)
    if not result:
        return _back(error="There is nobody to send to. Add a subscriber first.")
    if result.failed or (result.skipped and not result.sent):
        return _back(error=result.summary())
    return _back(notice=("Written to the outbox: " if mailer.mode() == "outbox" else "")
                        + result.summary() + ".")
