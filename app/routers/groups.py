"""Root's page for groups (teams): create them, choose who is in each, and set
each group's email digest.

Being in a group lets someone see and work the tickets filed under it. Root-only,
like everything under /root."""
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, digest, groups
from ..deps import DB, NotFound, RootUser, require
from ..schemas import GroupMember, GroupName, Schedule
from ..web import render, see_other
from .digest import send_notice

router = APIRouter(prefix="/root/groups", tags=["root"],
                   dependencies=[require("manage_users")])

Redirect = {"response_class": RedirectResponse, "status_code": 303}


def _back(group_id: int | None = None, *, notice: str = "", error: str = "") -> RedirectResponse:
    query = f"?error={quote(error)}" if error else (f"?notice={quote(notice)}" if notice else "")
    return see_other("/root/groups" + query + (f"#group-{group_id}" if group_id else ""))


def _group(group_id: int, conn: DB):
    group = groups.get_group(conn, group_id)
    if group is None:
        raise NotFound("That group no longer exists.", back="/root/groups")
    return group


Group = Annotated[object, Depends(_group)]


@router.get("", response_class=HTMLResponse, summary="Groups and their members")
def groups_page(request: Request, conn: DB, notice: str = "", error: str = ""):
    users = auth.list_users(conn)
    cards = []
    for g in groups.list_groups(conn):
        people = groups.members(conn, g["id"])
        inside = {p["id"] for p in people}
        cards.append({
            "g": g, "members": people,
            "others": [u for u in users if u["id"] not in inside and u["is_active"]],
            "days": digest.parse_days(g["digest_days"]),
            "when": digest.describe_schedule(g["digest_days"], g["digest_hour"],
                                             bool(g["digest_on_new_ticket"])),
            "reachable": sum(1 for p in people if digest.skip_reason(p) is None)})
    return render(request, "root_groups.html", {
        "cards": cards, "day_names": digest.DAY_NAMES, "notice": notice, "error": error})


@router.post("/create", **Redirect, summary="Create a group")
def group_create(form: Annotated[GroupName, Form()], conn: DB, me: RootUser):
    try:
        group_id = groups.create_group(conn, form.name)
    except ValueError as exc:
        return _back(error=str(exc))
    auth.audit(conn, me["id"], "group_created", f"group:{form.name.strip()}")
    return _back(group_id, notice="Group created. Add its members below.")


@router.post("/{group_id}/rename", **Redirect, summary="Rename a group")
def group_rename(group: Group, form: Annotated[GroupName, Form()], conn: DB, me: RootUser):
    try:
        groups.rename_group(conn, group["id"], form.name)
    except ValueError as exc:
        return _back(group["id"], error=str(exc))
    auth.audit(conn, me["id"], "group_renamed", f"group:{group['name']}",
               f"now {form.name.strip()}")
    return _back(group["id"], notice="Renamed.")


@router.post("/{group_id}/delete", **Redirect, summary="Delete a group")
def group_delete(group: Group, conn: DB, me: RootUser):
    """The group's tickets are kept. Each becomes private to its owner again."""
    count = groups.delete_group(conn, group["id"])
    auth.audit(conn, me["id"], "group_deleted", f"group:{group['name']}",
               f"{count} ticket(s) returned to their owners only")
    return _back(notice=f"{group['name']} deleted. {count} ticket(s) are now visible "
                        "only to their owners and admins.")


# ---------------------------------------------------------------- members

@router.post("/{group_id}/members", **Redirect, summary="Add someone to a group")
def group_add_member(group: Group, form: Annotated[GroupMember, Form()], conn: DB,
                     me: RootUser):
    """They can see and work every ticket filed under the group from now on."""
    user = auth.get_user(conn, form.user_id)
    if user is None:
        return _back(group["id"], error="That account no longer exists.")
    groups.add_member(conn, group["id"], user["id"])
    auth.audit(conn, me["id"], "group_member_added", f"group:{group['name']}",
               f"user:{user['username']}")
    return _back(group["id"], notice=f"{user['username']} added to {group['name']}.")


@router.post("/{group_id}/members/{user_id}/remove", **Redirect,
             summary="Remove someone from a group")
def group_remove_member(group: Group, user_id: int, conn: DB, me: RootUser):
    """Tickets they filed under the group stay in the group (and stay theirs)."""
    user = auth.get_user(conn, user_id)
    groups.remove_member(conn, group["id"], user_id)
    if user is not None:
        auth.audit(conn, me["id"], "group_member_removed", f"group:{group['name']}",
                   f"user:{user['username']}")
    return _back(group["id"], notice="Removed from the group.")


@router.post("/{group_id}/adopt", **Redirect,
             summary="Share a member's ungrouped tickets with the group")
def group_adopt(group: Group, form: Annotated[GroupMember, Form()], conn: DB, me: RootUser):
    """For tickets that predate the group: every ticket that member owns which
    isn't in any group yet is filed under this one."""
    user = auth.get_user(conn, form.user_id)
    if user is None or not groups.is_member(conn, group["id"], user["id"]):
        return _back(group["id"], error="Add them to the group first.")
    count = groups.adopt_tickets(conn, group["id"], user["id"])
    auth.audit(conn, me["id"], "group_adopted_tickets", f"group:{group['name']}",
               f"{count} ticket(s) of user:{user['username']}")
    return _back(group["id"], notice=f"{count} of {user['username']}'s tickets are now "
                                     f"shared with {group['name']}.")


# ---------------------------------------------------------------- group digest

@router.post("/{group_id}/digest", **Redirect, summary="Set a group's digest schedule")
def group_digest(group: Group, form: Annotated[Schedule, Form()], conn: DB, me: RootUser):
    """Every member with an email address gets the group's open tickets. Tick no
    days and leave new-ticket emails off to turn the group's digest off."""
    digest.set_group_schedule(conn, group["id"], form.days, form.hour, form.on_new_ticket)
    when = digest.describe_schedule(form.days, form.hour, form.on_new_ticket)
    auth.audit(conn, me["id"], "group_digest_changed", f"group:{group['name']}", when)
    return _back(group["id"], notice=f"{group['name']} digest: {when}.")


@router.get("/{group_id}/digest/preview", response_class=HTMLResponse,
            summary="The email a group's members would get right now")
def group_digest_preview(group: Group, conn: DB):
    """Shows the email without sending anything."""
    anyone = {"email": "preview@example.invalid", "username": "member",
              "display_name": f"each member of {group['name']}"}
    return HTMLResponse(digest.build_for_group(conn, group, anyone).html)


@router.post("/{group_id}/digest/send", **Redirect, summary="Send a group's digest now")
def group_digest_send(group: Group, conn: DB, me: RootUser):
    result = digest.send_group(conn, group, f"manual: group {group['name']}",
                               actor_id=me["id"])
    return _back(group["id"], **send_notice(result))
