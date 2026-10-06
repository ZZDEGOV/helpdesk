"""FastAPI app: routes, auth gate, template filters."""
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from urllib.parse import quote

from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import auth, config, repo, threadparse
from .db import connect

BASE = Path(__file__).resolve().parent
app = FastAPI(title="Help Desk")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


from .migrate import run as _migrate
_migrate(verbose=False)   # idempotent; brings schema current on startup

try:
    LOCAL_TZ = ZoneInfo(config.TIMEZONE)
except Exception:                                  # Windows has no system tzdata
    print(f"  ! Timezone {config.TIMEZONE} unavailable - showing UTC."
          f" Fix with: pip install tzdata")
    LOCAL_TZ = timezone.utc


# ---------------------------------------------------------------- filters

def fmt_dt(value, fmt="%b %d, %Y %-I:%M %p"):
    if not value:
        return "—"
    dt = datetime.fromisoformat(value)
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(LOCAL_TZ).strftime(fmt)
    except ValueError:                       # Windows lacks %-I
        return dt.astimezone(LOCAL_TZ).strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def fmt_date(value):
    return fmt_dt(value, "%b %d, %Y")


def date_input(value):
    """YYYY-MM-DD in local time, for <input type=date> round-tripping."""
    if not value:
        return ""
    dt = datetime.fromisoformat(value)
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def fmt_duration(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def relative(value):
    if not value:
        return "—"
    dt = datetime.fromisoformat(value)
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 604800:
        return f"{int(delta // 86400)}d ago"
    return fmt_date(value)


templates.env.filters.update(dt=fmt_dt, date=fmt_date, duration=fmt_duration,
                             relative=relative, date_input=date_input)
templates.env.globals.update(config=config, live_seconds=repo.live_active_seconds,
                             is_overdue=repo.is_overdue)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_utc(value: str, end_of_day: bool = False) -> str | None:
    """Parse a browser date/datetime-local value into a UTC ISO string."""
    if not value:
        return None
    fmt = "%Y-%m-%dT%H:%M" if "T" in value else "%Y-%m-%d"
    dt = datetime.strptime(value, fmt)
    if fmt == "%Y-%m-%d" and end_of_day:
        dt = dt.replace(hour=config.WORK_END_HOUR)
    return dt.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc).isoformat(timespec="seconds")


templates.env.globals.update(to_utc=to_utc)
templates.env.globals["now_iso"] = _now_iso


# ---------------------------------------------------------------- auth

OPEN_PATHS = {"/login", "/setup"}

# Root administers the system; it does not work tickets. Read access is allowed
# (and audited) so it can answer "why is this stuck", but every mutating route
# outside its own area is refused at the middleware, not merely hidden in the UI.
ROOT_WRITABLE_PREFIXES = ("/root", "/logout", "/change-password")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/static"):
            return await call_next(request)

        with connect() as conn:
            needs_setup = not auth.has_root(conn)
            user = auth.session_user(conn, request.cookies.get("hd_session"))

        if needs_setup:
            if path != "/setup":
                return RedirectResponse("/setup", status_code=303)
            return await call_next(request)

        if user is None:
            if path in OPEN_PATHS:
                return await call_next(request)
            return RedirectResponse("/login", status_code=303)

        if path == "/setup":
            return RedirectResponse("/", status_code=303)

        # Force a password change before anything else is reachable.
        if user["must_change_password"] and path not in ("/change-password", "/logout"):
            return RedirectResponse("/change-password", status_code=303)

        if user["role"] == "root":
            if request.method == "POST" and not path.startswith(ROOT_WRITABLE_PREFIXES):
                return HTMLResponse(
                    "<h3>Not permitted</h3><p>The root account administers the system "
                    "and cannot modify tickets. Sign in with a ticket account.</p>"
                    "<p><a href='/root'>Back to root dashboard</a></p>", status_code=403)
        elif path.startswith("/root"):
            return HTMLResponse("<h3>Not permitted</h3><p>Root access required.</p>"
                                "<p><a href='/'>Back</a></p>", status_code=403)

        if path.startswith("/trash") and not auth.can(user, "view_trash"):
            return HTMLResponse(
                "<h3>Not permitted</h3><p>Deleting tickets is handled by an admin. "
                "Ask one to remove it for you.</p><p><a href='/'>Back</a></p>",
                status_code=403)

        request.state.user = user
        repo.CURRENT_USER_ID.set(user["id"])
        return await call_next(request)


app.add_middleware(AuthMiddleware)


def current_user(request: Request):
    return getattr(request.state, "user", None)


def deny(message: str = "You don't have access to that.", back: str = "/"):
    return HTMLResponse(f"<h3>Not permitted</h3><p>{message}</p>"
                        f"<p><a href='{back}'>Back</a></p>", status_code=403)


def require(request: Request, capability: str):
    """Returns a 403 response if the signed-in user lacks the capability, else None."""
    if not auth.can(current_user(request), capability):
        return deny()
    return None


templates.env.globals["can"] = auth.can


templates.env.globals["current_user"] = current_user


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "setup.html", {"error": error})


@app.post("/setup")
def setup(username: str = Form(...), display_name: str = Form(""),
          password: str = Form(...), confirm: str = Form(...)):
    if password != confirm:
        return RedirectResponse("/setup?error=Passwords+did+not+match", status_code=303)
    if len(password) < 8:
        return RedirectResponse("/setup?error=Use+at+least+8+characters", status_code=303)
    with connect() as conn:
        if auth.has_root(conn):
            return RedirectResponse("/login", status_code=303)
        existing = auth.get_user_by_name(conn, username)
        if existing:
            auth.set_password(conn, existing["id"], password, must_change=False)
            auth.update_user(conn, existing["id"], role="root")
            user_id = existing["id"]
        else:
            user_id = auth.create_user(conn, username=username,
                                       display_name=display_name or username,
                                       password=password, role="root",
                                       must_change=False)
        auth.audit(conn, user_id, "setup", f"user:{username}", "root account created")
        token = auth.start_session(conn, user_id)
    resp = RedirectResponse("/root", status_code=303)
    resp.set_cookie("hd_session", token, httponly=True, samesite="lax",
                    max_age=86400 * config.SESSION_DAYS)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login(username: str = Form(...), password: str = Form("")):
    with connect() as conn:
        user, error = auth.authenticate(conn, username, password)
        if not user:
            auth.audit(conn, None, "login_failed", f"user:{username}", error)
            return RedirectResponse(f"/login?error={quote(error)}", status_code=303)
        token = auth.start_session(conn, user["id"])
        auth.purge_sessions(conn)
        auth.audit(conn, user["id"], "login", f"user:{user['username']}")
    resp = RedirectResponse("/root" if user["role"] == "root" else "/", status_code=303)
    resp.set_cookie("hd_session", token, httponly=True, samesite="lax",
                    max_age=86400 * config.SESSION_DAYS)
    return resp


@app.post("/logout")
def logout(request: Request):
    with connect() as conn:
        auth.end_session(conn, request.cookies.get("hd_session"))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("hd_session")
    return resp


@app.get("/change-password", response_class=HTMLResponse)
def change_password_form(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "change_password.html",
                                      {"error": error, "u": current_user(request)})


@app.post("/change-password")
def change_password(request: Request, current: str = Form(""), password: str = Form(...),
                    confirm: str = Form(...)):
    user = current_user(request)
    if user is None:
        with connect() as conn:
            user = auth.session_user(conn, request.cookies.get("hd_session"))
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if password != confirm:
        return RedirectResponse("/change-password?error=Passwords+did+not+match",
                                status_code=303)
    if len(password) < 8:
        return RedirectResponse("/change-password?error=Use+at+least+8+characters",
                                status_code=303)
    with connect() as conn:
        fresh = auth.get_user(conn, user["id"])
        if not fresh["must_change_password"] and not auth.verify_password(
                current, fresh["password_hash"], fresh["password_salt"]):
            return RedirectResponse("/change-password?error=Current+password+is+wrong",
                                    status_code=303)
        auth.set_password(conn, fresh["id"], password, must_change=False)
        auth.audit(conn, fresh["id"], "password_changed", f"user:{fresh['username']}")
    return RedirectResponse("/root" if fresh["role"] == "root" else "/", status_code=303)


# ---------------------------------------------------------------- tickets

@app.get("/", response_class=HTMLResponse)
def index(request: Request, status: str = "open", priority: str = "",
          category: str = "", channel: str = "", q: str = "",
          sort: str = "priority", overdue: str = ""):
    # Blank dropdowns submit "" — coerce here rather than letting FastAPI 422.
    pri = int(priority) if priority.strip().isdigit() else None
    is_overdue_filter = overdue.strip() in ("1", "true", "on", "yes")
    with connect() as conn:
        repo.purge_expired(conn)          # retention sweep, cheap
        tickets = repo.list_tickets(
            conn, status=status or None, priority=pri,
            category=category or None, channel=channel or None,
            q=q or None, overdue_only=is_overdue_filter, sort=sort)
        return templates.TemplateResponse(request, "index.html", {"tickets": tickets,
            "stats": repo.dashboard_stats(conn),
            "follow_ups": repo.due_follow_ups(conn),
            "categories": repo.all_categories(conn),
            "filters": {"status": status, "priority": pri, "category": category,
                        "channel": channel, "q": q, "sort": sort,
                        "overdue": is_overdue_filter},
        })


@app.get("/tickets/new", response_class=HTMLResponse)
def new_ticket_form(request: Request):
    with connect() as conn:
        return templates.TemplateResponse(request, "ticket_new.html", {"categories": repo.all_categories(conn)})


@app.post("/tickets/new")
def new_ticket(
    request: Request, subject: str = Form(...), body: str = Form(""),
    customer_name: str = Form(""), customer_email: str = Form(""),
    customer_phone: str = Form(""), account_id: str = Form(""),
    city: str = Form(""), channel: str = Form("unknown"),
    category: str = Form(""), subcategory: str = Form(""),
    priority: int = Form(3), ada: str = Form(""), logged_by: str = Form(""),
    received_at: str = Form(""), due_at: str = Form(""), tags: str = Form(""),
):
    with connect() as conn:
        ticket_id = repo.create_ticket(
            conn, subject=subject, body=body or None,
            customer_email=customer_email or None, customer_name=customer_name or None,
            customer_phone=customer_phone or None, account_id=account_id or None,
            city=city or None, channel=channel, category=category or None,
            subcategory=subcategory or None, priority=priority, ada=bool(ada),
            logged_by=logged_by or None,
            received_at=to_utc(received_at), due_at=to_utc(due_at, end_of_day=True),
            tags=[t.strip() for t in tags.split(",") if t.strip()])
        me = current_user(request)
        if me:
            conn.execute("UPDATE tickets SET owner_id = ? WHERE id = ?",
                         (me["id"], ticket_id))
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(request: Request, ticket_id: int, template_id: str = ""):
    with connect() as conn:
        ticket = repo.get_ticket(conn, ticket_id)
        if not ticket:
            return RedirectResponse("/", status_code=303)
        me = current_user(request)
        if me and me["role"] == "root":
            auth.audit(conn, me["id"], "ticket_viewed", ticket["ref"])
        history = []
        if ticket["customer_id"]:
            history = [t for t in repo.list_tickets(
                conn, customer_id=ticket["customer_id"], sort="newest")
                if t["id"] != ticket_id]
        drafted = ""
        if template_id.strip().isdigit():
            tpl = repo.get_template(conn, int(template_id))
            if tpl:
                drafted = repo.render_template(tpl["body"], ticket)
        return templates.TemplateResponse(request, "ticket_detail.html", {"t": ticket,
            "questions": repo.get_questions(conn, ticket_id),
            "emails": repo.customer_emails(conn, ticket["customer_id"]) if ticket["customer_id"] else [],
            "phones": repo.customer_phones(conn, ticket["customer_id"]) if ticket["customer_id"] else [],
            "events": repo.get_events(conn, ticket_id),
            "tags": repo.get_tags(conn, ticket_id),
            "all_tags": repo.all_tags(conn),
            "categories": repo.all_categories(conn),
            "templates_list": repo.list_templates(conn),
            "history": history, "drafted": drafted,
        })


@app.post("/tickets/{ticket_id}/status")
def change_status(ticket_id: int, status: str = Form(...)):
    with connect() as conn:
        repo.set_status(conn, ticket_id, status)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@app.post("/tickets/{ticket_id}/update")
def update_ticket(ticket_id: int, subject: str = Form(...), priority: int = Form(3),
                  category: str = Form(""), subcategory: str = Form(""),
                  channel: str = Form("unknown"), due_at: str = Form(""),
                  resolution: str = Form(""), root_cause: str = Form("")):
    due = to_utc(due_at, end_of_day=True)
    with connect() as conn:
        repo.update_fields(conn, ticket_id, {
            "subject": subject, "priority": priority, "category": category,
            "subcategory": subcategory, "channel": channel, "due_at": due,
            "resolution": resolution, "root_cause": root_cause})
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@app.post("/tickets/{ticket_id}/note")
def add_note(ticket_id: int, body: str = Form(...)):
    with connect() as conn:
        repo.add_note(conn, ticket_id, body)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@app.post("/tickets/{ticket_id}/message")
def log_message(request: Request, ticket_id: int, kind: str = Form("note"),
                body: str = Form(...), author: str = Form(""),
                occurred_at: str = Form(""), track_questions: str = Form("")):
    me = current_user(request)
    with connect() as conn:
        repo.log_message(conn, ticket_id, kind, body,
                         author=author.strip() or (me["display_name"] if me and kind.startswith("out_") else None) or None,
                         occurred_at=to_utc(occurred_at))
        if track_questions:
            for q in repo.extract_questions(body):
                repo.add_question(conn, ticket_id, q)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@app.post("/events/{event_id}/void")
def void_event(event_id: int, ticket_id: int = Form(...), reason: str = Form("")):
    with connect() as conn:
        repo.void_event(conn, event_id, reason)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@app.post("/events/{event_id}/unvoid")
def unvoid_event(event_id: int, ticket_id: int = Form(...)):
    with connect() as conn:
        repo.unvoid_event(conn, event_id)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@app.post("/events/{event_id}/retime")
def retime_event(event_id: int, ticket_id: int = Form(...), occurred_at: str = Form("")):
    with connect() as conn:
        when = to_utc(occurred_at)
        if when:
            repo.set_event_time(conn, event_id, when)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# ---------------------------------------------------------------- thread import

@app.post("/tickets/{ticket_id}/import", response_class=HTMLResponse)
def import_preview(request: Request, ticket_id: int, raw: str = Form(...)):
    with connect() as conn:
        ticket = repo.get_ticket(conn, ticket_id)
        emails = {r["email"] for r in repo.customer_emails(conn, ticket["customer_id"])} \
            if ticket["customer_id"] else set()
        if ticket["customer_email"]:
            emails.add(ticket["customer_email"])
        segments = threadparse.guess_kinds(
            threadparse.split_thread(raw), emails, set(config.OWNER_HINTS))
        return templates.TemplateResponse(request, "thread_import.html", {
            "t": ticket, "segments": segments, "raw": raw})


@app.post("/tickets/{ticket_id}/import/confirm")
async def import_confirm(request: Request, ticket_id: int):
    form = await request.form()
    count = 0
    with connect() as conn:
        index = 0
        while f"body_{index}" in form:
            if form.get(f"include_{index}"):
                repo.log_message(
                    conn, ticket_id, form.get(f"kind_{index}", "note"),
                    str(form.get(f"body_{index}", "")),
                    author=str(form.get(f"author_{index}", "")).strip() or None,
                    occurred_at=to_utc(str(form.get(f"when_{index}", ""))),
                    auto_status=False)
                count += 1
            index += 1
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# ---------------------------------------------------------------- questions

@app.post("/tickets/{ticket_id}/questions/add")
def add_question(ticket_id: int, text: str = Form(...)):
    with connect() as conn:
        repo.add_question(conn, ticket_id, text)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@app.post("/questions/{question_id}/answer")
def answer_question(question_id: int, ticket_id: int = Form(...),
                    answer: str = Form(""), clear: str = Form("")):
    with connect() as conn:
        if clear:
            repo.unanswer_question(conn, question_id)
        else:
            repo.answer_question(conn, question_id, answer)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@app.post("/questions/{question_id}/delete")
def delete_question(question_id: int, ticket_id: int = Form(...)):
    with connect() as conn:
        repo.delete_question(conn, question_id)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# ---------------------------------------------------------------- follow-ups

@app.post("/tickets/{ticket_id}/followup")
def set_followup(ticket_id: int, follow_up_at: str = Form(""),
                 follow_up_note: str = Form(""), done: str = Form("")):
    with connect() as conn:
        if done:
            repo.clear_follow_up(conn, ticket_id)
        else:
            repo.set_follow_up(conn, ticket_id, to_utc(follow_up_at), follow_up_note)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# ---------------------------------------------------------------- delete

@app.post("/tickets/{ticket_id}/delete")
def delete_ticket(request: Request, ticket_id: int):
    """Queued deletion. Recoverable until the retention window expires."""
    if (refused := require(request, "soft_delete")):
        return refused
    me = current_user(request)
    with connect() as conn:
        ticket = repo.get_ticket(conn, ticket_id)
        repo.soft_delete_ticket(conn, ticket_id)
        auth.audit(conn, me["id"], "ticket_queued_for_deletion",
                   ticket["ref"] if ticket else str(ticket_id))
    return RedirectResponse("/", status_code=303)


@app.post("/tickets/{ticket_id}/restore")
def restore_ticket(request: Request, ticket_id: int):
    if (refused := require(request, "restore")):
        return refused
    with connect() as conn:
        repo.restore_ticket(conn, ticket_id)
        auth.audit(conn, current_user(request)["id"], "ticket_restored", str(ticket_id))
    return RedirectResponse("/trash", status_code=303)


@app.get("/trash", response_class=HTMLResponse)
def trash(request: Request):
    with connect() as conn:
        repo.purge_expired(conn)
        return templates.TemplateResponse(request, "trash.html",
                                          {"d": repo.list_deleted(conn)})


@app.post("/tickets/{ticket_id}/tag")
def tag_ticket(ticket_id: int, tag: str = Form(""), remove: str = Form("")):
    with connect() as conn:
        if remove:
            repo.remove_tag(conn, ticket_id, remove)
        elif tag:
            repo.add_tag(conn, ticket_id, tag)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# ---------------------------------------------------------------- customers

@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, q: str = ""):
    with connect() as conn:
        return templates.TemplateResponse(request, "customers.html", {"customers": repo.list_customers(conn, q or None),
            "q": q})


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail(request: Request, customer_id: int, error: str = ""):
    with connect() as conn:
        customer = repo.get_customer(conn, customer_id)
        if not customer:
            return RedirectResponse("/customers", status_code=303)
        return templates.TemplateResponse(request, "customer_detail.html", {"c": customer,
            "tickets": repo.list_tickets(conn, customer_id=customer_id, sort="newest"),
            "emails": repo.customer_emails(conn, customer_id),
            "phones": repo.customer_phones(conn, customer_id),
            "others": [x for x in repo.list_customers(conn) if x["id"] != customer_id],
            "error": error})


@app.post("/customers/{customer_id}/edit")
def edit_customer(customer_id: int, name: str = Form(""), account_id: str = Form(""),
                  address: str = Form(""), city: str = Form(""), zip: str = Form("")):
    with connect() as conn:
        repo.update_customer(conn, customer_id, name=name, account_id=account_id,
                             address=address, city=city, zip=zip)
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@app.post("/customers/{customer_id}/contacts")
def customer_contacts(customer_id: int, action: str = Form(...), value: str = Form(""),
                      label: str = Form("")):
    error = ""
    with connect() as conn:
        try:
            if action == "add_email":
                repo.add_email(conn, customer_id, value)
            elif action == "primary_email":
                repo.set_primary_email(conn, customer_id, value)
            elif action == "remove_email":
                repo.remove_email(conn, customer_id, value)
            elif action == "add_phone":
                repo.add_phone(conn, customer_id, value, label)
            elif action == "primary_phone":
                repo.set_primary_phone(conn, customer_id, value)
            elif action == "remove_phone":
                repo.remove_phone(conn, customer_id, value)
        except ValueError as exc:
            error = str(exc)
    suffix = f"?error={error}" if error else ""
    return RedirectResponse(f"/customers/{customer_id}{suffix}", status_code=303)


@app.post("/customers/{customer_id}/merge")
def merge_customer(customer_id: int, target_id: int = Form(...)):
    with connect() as conn:
        repo.merge_customers(conn, customer_id, target_id)
    return RedirectResponse(f"/customers/{target_id}", status_code=303)


@app.post("/customers/{customer_id}/delete")
def delete_customer(request: Request, customer_id: int):
    if (refused := require(request, "soft_delete")):
        return refused
    with connect() as conn:
        repo.soft_delete_customer(conn, customer_id)
        auth.audit(conn, current_user(request)["id"], "customer_queued_for_deletion",
                   str(customer_id))
    return RedirectResponse("/customers", status_code=303)


@app.post("/customers/{customer_id}/restore")
def restore_customer(request: Request, customer_id: int):
    if (refused := require(request, "restore")):
        return refused
    with connect() as conn:
        repo.restore_customer(conn, customer_id)
        auth.audit(conn, current_user(request)["id"], "customer_restored",
                   str(customer_id))
    return RedirectResponse("/trash", status_code=303)


@app.post("/customers/{customer_id}/notes")
def customer_notes(customer_id: int, notes: str = Form("")):
    from .db import utcnow
    with connect() as conn:
        conn.execute("UPDATE customers SET notes = ?, updated_at = ? WHERE id = ?",
                     (notes or None, utcnow(), customer_id))
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


# ---------------------------------------------------------------- templates

@app.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, edit: str = ""):
    with connect() as conn:
        return templates.TemplateResponse(request, "templates.html", {"items": repo.list_templates(conn),
            "editing": repo.get_template(conn, int(edit)) if edit.strip().isdigit() else None})


@app.post("/templates/save")
def save_template(template_id: str = Form(""), name: str = Form(...),
                  category: str = Form(""), body: str = Form(...)):
    with connect() as conn:
        repo.save_template(conn, template_id=int(template_id) if template_id else None,
                           name=name, category=category or None, body=body)
    return RedirectResponse("/templates", status_code=303)


@app.post("/templates/{template_id}/delete")
def delete_template(template_id: int):
    with connect() as conn:
        repo.delete_template(conn, template_id)
    return RedirectResponse("/templates", status_code=303)


# ---------------------------------------------------------------- root area

@app.get("/root", response_class=HTMLResponse)
def root_dashboard(request: Request):
    with connect() as conn:
        return templates.TemplateResponse(request, "root_dashboard.html", {
            "users": auth.list_users(conn),
            "stats": repo.dashboard_stats(conn),
            "per_user": repo.per_user_stats(conn),
            "audit": auth.recent_audit(conn, 25),
        })


@app.get("/root/users", response_class=HTMLResponse)
def root_users(request: Request, error: str = "", edit: str = ""):
    with connect() as conn:
        return templates.TemplateResponse(request, "root_users.html", {
            "users": auth.list_users(conn), "error": error,
            "editing": auth.get_user(conn, int(edit)) if edit.isdigit() else None})


@app.post("/root/users/create")
def root_create_user(username: str = Form(...), display_name: str = Form(""),
                     password: str = Form(...), role: str = Form("user"),
                     request: Request = None):
    me = current_user(request)
    with connect() as conn:
        try:
            uid = auth.create_user(conn, username=username, display_name=display_name,
                                   password=password, role=role,
                                   created_by=me["id"], must_change=True)
            auth.audit(conn, me["id"], "user_created", f"user:{username}", f"role={role}")
        except ValueError as exc:
            return RedirectResponse(f"/root/users?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/root/users", status_code=303)


@app.post("/root/users/{user_id}/update")
def root_update_user(user_id: int, request: Request, display_name: str = Form(""),
                     role: str = Form("user"), is_active: str = Form("")):
    me = current_user(request)
    with connect() as conn:
        target = auth.get_user(conn, user_id)
        # Don't allow the last root to be demoted or disabled — that would lock
        # everyone out of account management with no way back in.
        roots = [u for u in auth.list_users(conn)
                 if u["role"] == "root" and u["is_active"] and u["password_hash"]]
        if target and target["role"] == "root" and len(roots) <= 1 \
                and (role != "root" or not is_active):
            return RedirectResponse(
                "/root/users?error=" + quote("That is the only root account."),
                status_code=303)
        try:
            auth.update_user(conn, user_id, display_name=display_name, role=role,
                             is_active=bool(is_active))
            auth.audit(conn, me["id"], "user_updated", f"user:{target['username']}",
                       f"role={role} active={bool(is_active)}")
        except ValueError as exc:
            return RedirectResponse(f"/root/users?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/root/users", status_code=303)


@app.post("/root/users/{user_id}/password")
def root_reset_password(user_id: int, request: Request, password: str = Form(...)):
    me = current_user(request)
    if len(password) < 8:
        return RedirectResponse(
            "/root/users?error=" + quote("Use at least 8 characters"), status_code=303)
    with connect() as conn:
        target = auth.get_user(conn, user_id)
        auth.set_password(conn, user_id, password, must_change=True)
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        auth.audit(conn, me["id"], "password_reset", f"user:{target['username']}",
                   "temporary password issued")
    return RedirectResponse("/root/users", status_code=303)


@app.get("/root/audit", response_class=HTMLResponse)
def root_audit(request: Request):
    with connect() as conn:
        return templates.TemplateResponse(request, "root_audit.html",
                                          {"audit": auth.recent_audit(conn, 300)})


# ---------------------------------------------------------------- root: purge
#
# Root's deletes live here rather than on the ticket pages so the middleware rule
# "root cannot POST to ticket routes" stays intact. These are immediate and
# permanent -- no trash, no retention window.

@app.get("/root/tickets", response_class=HTMLResponse)
def root_tickets(request: Request, q: str = ""):
    with connect() as conn:
        return templates.TemplateResponse(request, "root_tickets.html", {
            "tickets": repo.list_tickets(conn, status=None, q=q or None,
                                         sort="newest", include_deleted=True),
            "q": q})


@app.post("/root/tickets/{ticket_id}/delete")
def root_delete_ticket(request: Request, ticket_id: int):
    me = current_user(request)
    with connect() as conn:
        ticket = repo.get_ticket(conn, ticket_id)
        ref = ticket["ref"] if ticket else str(ticket_id)
        repo.hard_delete_ticket(conn, ticket_id)
        auth.audit(conn, me["id"], "ticket_deleted_permanently", ref)
    return RedirectResponse("/root/tickets", status_code=303)


@app.get("/root/customers", response_class=HTMLResponse)
def root_customers(request: Request, q: str = ""):
    with connect() as conn:
        return templates.TemplateResponse(request, "root_customers.html", {
            "customers": repo.list_customers(conn, q or None, include_deleted=True),
            "q": q})


@app.post("/root/customers/{customer_id}/delete")
def root_delete_customer(request: Request, customer_id: int):
    me = current_user(request)
    with connect() as conn:
        customer = repo.get_customer(conn, customer_id)
        label = (customer["email"] or customer["name"] or str(customer_id)
                 ) if customer else str(customer_id)
        owned = len(repo.list_tickets(conn, customer_id=customer_id,
                                      include_deleted=True))
        repo.hard_delete_customer(conn, customer_id)
        auth.audit(conn, me["id"], "customer_deleted_permanently", label,
                   f"{owned} ticket(s) removed with them")
    return RedirectResponse("/root/customers", status_code=303)


@app.post("/root/users/{user_id}/delete")
def root_delete_user(request: Request, user_id: int, reassign_to: str = Form("")):
    me = current_user(request)
    if user_id == me["id"]:
        return RedirectResponse(
            "/root/users?error=" + quote("You cannot delete the account you're using."),
            status_code=303)
    with connect() as conn:
        target = auth.get_user(conn, user_id)
        if not target:
            return RedirectResponse("/root/users", status_code=303)
        roots = [u for u in auth.list_users(conn)
                 if u["role"] == "root" and u["is_active"] and u["password_hash"]]
        if target["role"] == "root" and len(roots) <= 1:
            return RedirectResponse(
                "/root/users?error=" + quote("That is the only root account."),
                status_code=303)
        new_owner = int(reassign_to) if reassign_to.strip().isdigit() else None
        result = auth.delete_user(conn, user_id, reassign_to=new_owner)
        auth.audit(conn, me["id"], "user_deleted", f"user:{result.get('username')}",
                   f"{result['tickets']} ticket(s) "
                   + (f"reassigned to user {new_owner}" if new_owner else "left unowned"))
    return RedirectResponse("/root/users", status_code=303)
