"""First-run setup, sign-in, sign-out, and changing your own password."""
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config
from ..deps import DB, SESSION_COOKIE, SignedIn
from ..schemas import Login, PasswordChange, Setup
from ..web import render, see_other

router = APIRouter(tags=["accounts"])

MIN_PASSWORD = 8


def _home(user) -> str:
    return "/root" if user["role"] == "root" else "/"


def _signed_in_response(user, token: str) -> RedirectResponse:
    resp = see_other(_home(user))
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=config.SECURE_COOKIES, max_age=86400 * config.SESSION_DAYS)
    return resp


def _password_problem(password: str, confirm: str) -> str:
    if password != confirm:
        return "Passwords did not match"
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters"
    return ""


@router.get("/setup", response_class=HTMLResponse, summary="First-run setup page")
def setup_form(request: Request, conn: DB, error: str = ""):
    if auth.has_root(conn):
        return see_other("/login")
    return render(request, "setup.html", {"error": error})


@router.post("/setup", response_class=RedirectResponse, status_code=303,
             summary="Create the root account")
def setup(form: Annotated[Setup, Form()], conn: DB):
    """Only works while no root account exists."""
    if auth.has_root(conn):
        return see_other("/login")
    if problem := _password_problem(form.password, form.confirm):
        return see_other(f"/setup?error={quote(problem)}")
    existing = auth.get_user_by_name(conn, form.username)
    if existing:
        auth.set_password(conn, existing["id"], form.password, must_change=False)
        auth.update_user(conn, existing["id"], role="root")
        user_id = existing["id"]
    else:
        user_id = auth.create_user(conn, username=form.username,
                                   display_name=form.display_name or form.username,
                                   password=form.password, role="root", must_change=False)
    auth.audit(conn, user_id, "setup", f"user:{form.username}", "root account created")
    return _signed_in_response(auth.get_user(conn, user_id), auth.start_session(conn, user_id))


@router.get("/login", response_class=HTMLResponse, summary="Sign-in page")
def login_form(request: Request, conn: DB, error: str = ""):
    if not auth.has_root(conn):
        return see_other("/setup")
    return render(request, "login.html", {"error": error})


@router.post("/login", response_class=RedirectResponse, status_code=303, summary="Sign in")
def login(form: Annotated[Login, Form()], conn: DB):
    user, error = auth.authenticate(conn, form.username, form.password)
    if not user:
        auth.audit(conn, None, "login_failed", f"user:{form.username}", error)
        return see_other(f"/login?error={quote(error)}")
    token = auth.start_session(conn, user["id"])
    auth.audit(conn, user["id"], "login", f"user:{user['username']}")
    return _signed_in_response(user, token)


@router.post("/logout", response_class=RedirectResponse, status_code=303, summary="Sign out")
def logout(request: Request, conn: DB):
    auth.end_session(conn, request.cookies.get(SESSION_COOKIE))
    resp = see_other("/login")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/change-password", response_class=HTMLResponse,
            summary="Change-password page")
def change_password_form(request: Request, user: SignedIn, error: str = ""):
    return render(request, "change_password.html", {"error": error, "u": user})


@router.post("/change-password", response_class=RedirectResponse, status_code=303,
             summary="Change your own password")
def change_password(form: Annotated[PasswordChange, Form()], user: SignedIn, conn: DB):
    """The current password isn't asked for when root has forced a change."""
    if problem := _password_problem(form.password, form.confirm):
        return see_other(f"/change-password?error={quote(problem)}")
    if not user["must_change_password"] and not auth.verify_password(
            form.current, user["password_hash"], user["password_salt"]):
        return see_other("/change-password?error=" + quote("Current password is wrong"))
    auth.set_password(conn, user["id"], form.password, must_change=False)
    auth.audit(conn, user["id"], "password_changed", f"user:{user['username']}")
    return see_other(_home(user))
