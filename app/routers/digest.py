"""Root's page for the open-ticket digest: the individual subscribers, what each
one sees, each one's own schedule, and buttons to preview or send.

Group digests are set up on the Groups page (routers/groups.py).
Root-only, like everything under /root."""
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, digest, groups, mailer
from ..deps import DB, NotFound, RootUser, require
from ..schemas import DigestSend, DigestSubscribe, DigestSubscriberSettings
from ..web import render, see_other

router = APIRouter(prefix="/root/digest", tags=["root"],
                   dependencies=[require("manage_users")])

Redirect = {"response_class": RedirectResponse, "status_code": 303}


def _back(*, notice: str = "", error: str = "") -> RedirectResponse:
    query = f"?error={quote(error)}" if error else (f"?notice={quote(notice)}" if notice else "")
    return see_other("/root/digest" + query)


def send_notice(result: digest.Result) -> dict[str, str]:
    """Turn a send result into the notice or error to show root."""
    if not result:
        return {"error": "There is nobody to send to."}
    if result.failed or (result.skipped and not result.sent):
        return {"error": result.summary()}
    prefix = {"outbox": "Written to the outbox (not sent): ",
              "folder": "Handed to the drop folder: "}.get(mailer.mode(), "")
    return {"notice": prefix + result.summary() + "."}


@router.get("", response_class=HTMLResponse, summary="Open-ticket digest subscribers")
def digest_page(request: Request, conn: DB, notice: str = "", error: str = ""):
    subs = digest.subscribers(conn)
    taken = {s["user_id"] for s in subs}
    outbox = (len(list(config.OUTBOX_DIR.glob("*.eml")))
              if config.OUTBOX_DIR.is_dir() else 0)
    return render(request, "root_digest.html", {
        "subscribers": [{
            "row": s, "skip": digest.skip_reason(s), "days": digest.parse_days(s["days"]),
            "when": digest.describe_schedule(s["days"], s["hour"], bool(s["on_new_ticket"])),
            "groups": [g["name"] for g in groups.groups_of(conn, s["user_id"])],
        } for s in subs],
        "candidates": [u for u in auth.list_users(conn, include_inactive=False)
                       if u["id"] not in taken],
        "group_digests": [g for g in groups.list_groups(conn)
                          if g["digest_days"] or g["digest_on_new_ticket"]],
        "describe": digest.describe_schedule,
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
    digest.subscribe(conn, user["id"], form.scope, form.days, form.hour, form.on_new_ticket)
    when = digest.describe_schedule(form.days, form.hour, form.on_new_ticket)
    auth.audit(conn, me["id"], "digest_subscribed", f"user:{user['username']}",
               f"scope={form.scope} email={user['email']} schedule={when}")
    return _back(notice=f"{user['username']} added. Schedule: {when}.")


@router.post("/subscribers/{user_id}/update", **Redirect,
             summary="Change a subscriber's scope and schedule")
def digest_update(user_id: int, form: Annotated[DigestSubscriberSettings, Form()],
                  conn: DB, me: RootUser):
    """Replaces all of that subscriber's settings. Tick no days for no schedule."""
    sub = digest.get_subscriber(conn, user_id)
    if sub is None:
        return _back(error="That person is no longer a subscriber.")
    digest.subscribe(conn, user_id, form.scope, form.days, form.hour, form.on_new_ticket)
    when = digest.describe_schedule(form.days, form.hour, form.on_new_ticket)
    auth.audit(conn, me["id"], "digest_subscriber_changed", f"user:{sub['username']}",
               f"scope={form.scope} schedule={when}")
    return _back(notice=f"Saved for {sub['username']}: {when}.")


@router.post("/subscribers/{user_id}/remove", **Redirect, summary="Remove a subscriber")
def digest_unsubscribe(user_id: int, conn: DB, me: RootUser):
    sub = digest.get_subscriber(conn, user_id)
    if sub is not None:
        digest.unsubscribe(conn, user_id)
        auth.audit(conn, me["id"], "digest_unsubscribed", f"user:{sub['username']}")
    return _back(notice="Removed." if sub else "")


# ---------------------------------------------------------------- preview and sending

@router.get("/preview/{user_id}", response_class=HTMLResponse,
            summary="The email a subscriber would get right now")
def digest_preview(user_id: int, conn: DB):
    """Shows the email without sending anything."""
    sub = digest.get_subscriber(conn, user_id)
    if sub is None:
        raise NotFound("That person is not a subscriber.", back="/root/digest")
    return HTMLResponse(digest.build_for_subscriber(conn, sub).html)


@router.post("/send", **Redirect, summary="Send the digest now")
def digest_send(form: Annotated[DigestSend, Form()], conn: DB, me: RootUser):
    """To every subscriber, or only `user_id`. Ignores their schedules."""
    result = digest.send_subscribers(conn, "manual", actor_id=me["id"],
                                     only_user_id=form.user_id)
    return _back(**send_notice(result))
