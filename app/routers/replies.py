"""Saved replies: shared templates with {{placeholders}} filled from a ticket.

The URL path stays /templates so existing bookmarks keep working; in code they're
"replies" to avoid confusion with Jinja templates.
"""
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import repo
from ..deps import DB, Worker
from ..schemas import ReplyTemplateSave
from ..web import render, see_other

router = APIRouter(prefix="/templates", tags=["saved replies"])


@router.get("", response_class=HTMLResponse, summary="Saved replies")
def replies_page(request: Request, conn: DB, user: Worker, edit: str = ""):
    """Placeholders: {{first_name}} {{full_name}} {{ref}} {{subject}} {{due_date}}
    {{owner}} {{category}} {{external_ref}}. {{owner}} is whoever loads the reply."""
    editing = repo.get_template(conn, int(edit)) if edit.strip().isdigit() else None
    return render(request, "templates.html",
                  {"items": repo.list_templates(conn), "editing": editing})


@router.post("/save", response_class=RedirectResponse, status_code=303,
             summary="Create or update a saved reply")
def save_reply(form: Annotated[ReplyTemplateSave, Form()], conn: DB, user: Worker):
    repo.save_template(conn, template_id=form.template_id, name=form.name,
                       category=form.category or None, body=form.body)
    return see_other("/templates")


@router.post("/{template_id}/delete", response_class=RedirectResponse, status_code=303,
             summary="Delete a saved reply")
def delete_reply(template_id: int, conn: DB, user: Worker):
    repo.delete_template(conn, template_id)
    return see_other("/templates")
