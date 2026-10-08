"""The customer directory: contacts, notes, merging duplicates, queued deletion.

The directory is shared by everyone who works tickets -- customers are matched
on email across the whole team. What each person sees of a customer's tickets
is still limited to the tickets they can open.
"""
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, repo
from ..db import utcnow
from ..deps import DB, CustomerViewer, NotFound, Restorer, SoftDeleter, Worker, visible_owner
from ..schemas import ContactChange, CustomerEdit
from ..web import render, see_other

router = APIRouter(prefix="/customers", tags=["customers"])

Redirect = {"response_class": RedirectResponse, "status_code": 303}


def _customer(conn, customer_id: int):
    customer = repo.get_customer(conn, customer_id)
    if customer is None:
        raise NotFound("That customer doesn't exist.", back="/customers")
    return customer


@router.get("", response_class=HTMLResponse, summary="Customer directory")
def customers_page(request: Request, conn: DB, user: CustomerViewer, q: str = ""):
    return render(request, "customers.html", {
        "customers": repo.list_customers(conn, q or None, owner_id=visible_owner(user)),
        "q": q})


@router.get("/{customer_id}", response_class=HTMLResponse, summary="Customer page")
def customer_detail(request: Request, customer_id: int, conn: DB, user: CustomerViewer,
                    error: str = ""):
    customer = _customer(conn, customer_id)
    return render(request, "customer_detail.html", {
        "c": customer,
        "tickets": repo.list_tickets(conn, customer_id=customer_id, sort="newest",
                                     owner_id=visible_owner(user)),
        "emails": repo.customer_emails(conn, customer_id),
        "phones": repo.customer_phones(conn, customer_id),
        "others": [x for x in repo.list_customers(conn) if x["id"] != customer_id],
        "error": error})


@router.post("/{customer_id}/edit", **Redirect, summary="Edit customer details")
def edit_customer(customer_id: int, form: Annotated[CustomerEdit, Form()], conn: DB,
                  user: Worker):
    _customer(conn, customer_id)
    repo.update_customer(conn, customer_id, **form.model_dump())
    return see_other(f"/customers/{customer_id}")


@router.post("/{customer_id}/contacts", **Redirect, summary="Add, remove, or promote a contact")
def customer_contacts(customer_id: int, form: Annotated[ContactChange, Form()], conn: DB,
                      user: Worker):
    _customer(conn, customer_id)
    actions = {
        "add_email": lambda: repo.add_email(conn, customer_id, form.value),
        "primary_email": lambda: repo.set_primary_email(conn, customer_id, form.value),
        "remove_email": lambda: repo.remove_email(conn, customer_id, form.value),
        "add_phone": lambda: repo.add_phone(conn, customer_id, form.value, form.label),
        "primary_phone": lambda: repo.set_primary_phone(conn, customer_id, form.value),
        "remove_phone": lambda: repo.remove_phone(conn, customer_id, form.value),
    }
    try:
        actions[form.action]()
    except ValueError as exc:
        return see_other(f"/customers/{customer_id}?error={quote(str(exc))}")
    return see_other(f"/customers/{customer_id}")


@router.post("/{customer_id}/merge", **Redirect, summary="Merge into another customer")
def merge_customer(customer_id: int, target_id: Annotated[int, Form()], conn: DB,
                   user: Worker):
    """Moves every ticket and contact to `target_id`, then removes this record."""
    _customer(conn, customer_id)
    _customer(conn, target_id)
    repo.merge_customers(conn, customer_id, target_id)
    return see_other(f"/customers/{target_id}")


@router.post("/{customer_id}/notes", **Redirect, summary="Save customer notes")
def customer_notes(customer_id: int, conn: DB, user: Worker,
                   notes: Annotated[str, Form()] = ""):
    _customer(conn, customer_id)
    conn.execute("UPDATE customers SET notes = ?, updated_at = ? WHERE id = ?",
                 (notes or None, utcnow(), customer_id))
    return see_other(f"/customers/{customer_id}")


@router.post("/{customer_id}/delete", **Redirect, summary="Queue a customer for deletion")
def delete_customer(customer_id: int, conn: DB, user: SoftDeleter):
    """Admin only. Their tickets are queued with them."""
    _customer(conn, customer_id)
    repo.soft_delete_customer(conn, customer_id)
    auth.audit(conn, user["id"], "customer_queued_for_deletion", str(customer_id))
    return see_other("/customers")


@router.post("/{customer_id}/restore", **Redirect, summary="Restore from the trash")
def restore_customer(customer_id: int, conn: DB, user: Restorer):
    _customer(conn, customer_id)
    repo.restore_customer(conn, customer_id)
    auth.audit(conn, user["id"], "customer_restored", str(customer_id))
    return see_other("/trash")
