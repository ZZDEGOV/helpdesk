"""The root admin area: accounts, analytics, the audit log, and immediate
permanent deletion.

Every route here requires root (router-level dependency). Root's permanent
deletes live here rather than on the ticket pages, so root still can't use any
ticket route to change a ticket.
"""
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, repo
from ..deps import DB, RootUser, require
from ..schemas import PasswordReset, UserCreate, UserDelete, UserUpdate
from ..web import render, see_other

router = APIRouter(prefix="/root", tags=["root"],
                   dependencies=[require("manage_users")])

Redirect = {"response_class": RedirectResponse, "status_code": 303}


def _users_error(message: str) -> RedirectResponse:
    return see_other("/root/users?error=" + quote(message))


def _is_last_root(conn, user) -> bool:
    roots = [u for u in auth.list_users(conn)
             if u["role"] == "root" and u["is_active"] and u["password_hash"]]
    return user["role"] == "root" and len(roots) <= 1


# ---------------------------------------------------------------- dashboard

@router.get("", response_class=HTMLResponse, summary="Root dashboard")
def root_dashboard(request: Request, conn: DB):
    return render(request, "root_dashboard.html", {
        "users": auth.list_users(conn),
        "stats": repo.dashboard_stats(conn),
        "per_user": repo.per_user_stats(conn),
        "audit": auth.recent_audit(conn, 25),
    })


@router.get("/audit", response_class=HTMLResponse, summary="Audit log")
def root_audit(request: Request, conn: DB):
    return render(request, "root_audit.html", {"audit": auth.recent_audit(conn, 300)})


# ---------------------------------------------------------------- accounts

@router.get("/users", response_class=HTMLResponse, summary="Account management")
def root_users(request: Request, conn: DB, error: str = "", edit: str = ""):
    return render(request, "root_users.html", {
        "users": auth.list_users(conn), "error": error,
        "editing": auth.get_user(conn, int(edit)) if edit.isdigit() else None})


@router.post("/users/create", **Redirect, summary="Create an account")
def root_create_user(form: Annotated[UserCreate, Form()], conn: DB, me: RootUser):
    if len(form.password) < 8:
        return _users_error("Use at least 8 characters")
    try:
        auth.create_user(conn, username=form.username, display_name=form.display_name,
                         password=form.password, role=form.role, created_by=me["id"],
                         must_change=True)
    except ValueError as exc:
        return _users_error(str(exc))
    auth.audit(conn, me["id"], "user_created", f"user:{form.username}", f"role={form.role}")
    return see_other("/root/users")


@router.post("/users/{user_id}/update", **Redirect, summary="Edit an account")
def root_update_user(user_id: int, form: Annotated[UserUpdate, Form()], conn: DB,
                     me: RootUser):
    """The last active root can be neither demoted nor disabled -- that would
    leave nobody able to manage accounts."""
    target = auth.get_user(conn, user_id)
    if target is None:
        return _users_error("That account no longer exists.")
    if _is_last_root(conn, target) and (form.role != "root" or not form.is_active):
        return _users_error("That is the only root account.")
    auth.update_user(conn, user_id, display_name=form.display_name, role=form.role,
                     is_active=form.is_active)
    auth.audit(conn, me["id"], "user_updated", f"user:{target['username']}",
               f"role={form.role} active={form.is_active}")
    return see_other("/root/users")


@router.post("/users/{user_id}/password", **Redirect, summary="Issue a temporary password")
def root_reset_password(user_id: int, form: Annotated[PasswordReset, Form()], conn: DB,
                        me: RootUser):
    """Signs the user out everywhere; they must choose a new password at next sign-in."""
    if len(form.password) < 8:
        return _users_error("Use at least 8 characters")
    target = auth.get_user(conn, user_id)
    if target is None:
        return _users_error("That account no longer exists.")
    auth.set_password(conn, user_id, form.password, must_change=True)
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    auth.audit(conn, me["id"], "password_reset", f"user:{target['username']}",
               "temporary password issued")
    return see_other("/root/users")


@router.post("/users/{user_id}/delete", **Redirect, summary="Delete an account")
def root_delete_user(user_id: int, form: Annotated[UserDelete, Form()], conn: DB,
                     me: RootUser):
    """Their tickets go to `reassign_to`, or are left unowned. Timeline entries
    and audit rows are kept with the link to the account removed."""
    if user_id == me["id"]:
        return _users_error("You cannot delete the account you're using.")
    target = auth.get_user(conn, user_id)
    if target is None:
        return see_other("/root/users")
    if _is_last_root(conn, target):
        return _users_error("That is the only root account.")
    result = auth.delete_user(conn, user_id, reassign_to=form.reassign_to)
    auth.audit(conn, me["id"], "user_deleted", f"user:{result.get('username')}",
               f"{result['tickets']} ticket(s) "
               + (f"reassigned to user {form.reassign_to}" if form.reassign_to
                  else "left unowned"))
    return see_other("/root/users")


# ---------------------------------------------------------------- permanent deletion

@router.get("/tickets", response_class=HTMLResponse, summary="Every ticket, including trash")
def root_tickets(request: Request, conn: DB, q: str = ""):
    return render(request, "root_tickets.html", {
        "tickets": repo.list_tickets(conn, status=None, q=q or None, sort="newest",
                                     include_deleted=True),
        "q": q})


@router.post("/tickets/{ticket_id}/delete", **Redirect, summary="Delete a ticket permanently")
def root_delete_ticket(ticket_id: int, conn: DB, me: RootUser):
    ticket = repo.get_ticket(conn, ticket_id)
    repo.hard_delete_ticket(conn, ticket_id)
    auth.audit(conn, me["id"], "ticket_deleted_permanently",
               ticket["ref"] if ticket else str(ticket_id))
    return see_other("/root/tickets")


@router.get("/customers", response_class=HTMLResponse,
            summary="Every customer, including trash")
def root_customers(request: Request, conn: DB, q: str = ""):
    return render(request, "root_customers.html", {
        "customers": repo.list_customers(conn, q or None, include_deleted=True), "q": q})


@router.post("/customers/{customer_id}/delete", **Redirect,
             summary="Delete a customer and their tickets permanently")
def root_delete_customer(customer_id: int, conn: DB, me: RootUser):
    customer = repo.get_customer(conn, customer_id)
    label = (customer["email"] or customer["name"] or str(customer_id)
             ) if customer else str(customer_id)
    owned = len(repo.list_tickets(conn, customer_id=customer_id, include_deleted=True))
    repo.hard_delete_customer(conn, customer_id)
    auth.audit(conn, me["id"], "customer_deleted_permanently", label,
               f"{owned} ticket(s) removed with them")
    return see_other("/root/customers")
