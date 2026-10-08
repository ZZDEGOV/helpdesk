"""Application assembly: routers, error handling, startup, and the docs pages."""
import asyncio
import logging
from contextlib import asynccontextmanager, closing
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, backup, migrate, repo
from .db import connect
from .deps import (CurrentUser, Forbidden, LoginRequired, NotFound,
                   PasswordChangeRequired, SetupRequired)
from .routers import accounts, api, customers, replies, root, tickets, trash
from .web import BASE, render

log = logging.getLogger("helpdesk")

SWEEP_SECONDS = 3600


def sweep_once() -> None:
    """Hourly housekeeping: purge trash past its retention window and expired
    sessions, and take the day's backup if it hasn't been taken yet."""
    with closing(connect()) as conn:
        repo.purge_expired(conn)
        auth.purge_sessions(conn)
        conn.commit()
    age = backup.latest_age()
    if age is None or age > timedelta(days=1):
        log.info("backup written to %s", backup.create())


async def _sweep_forever() -> None:
    while True:
        try:
            await asyncio.to_thread(sweep_once)
        except Exception:
            log.exception("retention sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate.run(verbose=False)          # idempotent; brings the schema current
    sweeper = asyncio.create_task(_sweep_forever())
    yield
    sweeper.cancel()


app = FastAPI(
    title="Transit Systems Help Desk",
    version="3.0.0",
    summary="Ticket tracking with a pausable SLA clock.",
    description="Most routes serve HTML pages and form posts; `/api/v1` returns JSON. "
                "Everything except `/health` requires signing in.",
    openapi_tags=[
        {"name": "tickets", "description": "Ticket list, ticket pages, and ticket work."},
        {"name": "customers", "description": "The shared customer directory."},
        {"name": "saved replies", "description": "Reply templates with placeholders."},
        {"name": "trash", "description": "Queued deletions (admin)."},
        {"name": "root", "description": "Accounts, analytics, audit, permanent deletion."},
        {"name": "accounts", "description": "Setup, sign-in, passwords."},
        {"name": "api", "description": "Read-only JSON."},
    ],
    lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,   # served below, behind sign-in
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

for module in (accounts, tickets, customers, replies, trash, root, api):
    app.include_router(module.router)


# ---------------------------------------------------------------- errors
#
# Browsers get a redirect or an error page; /api callers get JSON.

def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _json(status: int, detail) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _page(request: Request, status: int, heading: str, message: str, back: str = "/",
          details: list[str] | None = None):
    return render(request, "error.html", {"heading": heading, "message": message,
                                          "back": back, "details": details or []},
                  status_code=status)


@app.exception_handler(SetupRequired)
def _setup_required(request: Request, exc: SetupRequired):
    if _is_api(request):
        return _json(503, "Setup has not been completed.")
    return RedirectResponse("/setup", status_code=303)


@app.exception_handler(LoginRequired)
def _login_required(request: Request, exc: LoginRequired):
    if _is_api(request):
        return _json(401, "Not signed in.")
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(PasswordChangeRequired)
def _password_change_required(request: Request, exc: PasswordChangeRequired):
    if _is_api(request):
        return _json(403, "You must change your password first.")
    return RedirectResponse("/change-password", status_code=303)


@app.exception_handler(Forbidden)
def _forbidden(request: Request, exc: Forbidden):
    if _is_api(request):
        return _json(403, exc.message)
    return _page(request, 403, "Not permitted", exc.message, exc.back)


@app.exception_handler(NotFound)
def _not_found(request: Request, exc: NotFound):
    if _is_api(request):
        return _json(404, exc.message)
    return _page(request, 404, "Not found", exc.message, exc.back)


@app.exception_handler(RequestValidationError)
def _invalid(request: Request, exc: RequestValidationError):
    if _is_api(request):
        return _json(422, jsonable_encoder(exc.errors()))
    details = [f"{'.'.join(str(p) for p in e['loc'][1:]) or 'request'}: {e['msg']}"
               for e in exc.errors()]
    return _page(request, 400, "That didn't go through",
                 "Some of what was submitted wasn't valid.",
                 request.headers.get("referer", "/"), details)


# ---------------------------------------------------------------- health and docs

@app.get("/health", tags=["api"], summary="Liveness check for Docker")
def health() -> dict[str, str]:
    """No sign-in required. Fails if the database can't be read."""
    with closing(connect()) as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/openapi.json", include_in_schema=False)
def openapi(user: CurrentUser):
    return app.openapi()


@app.get("/docs", include_in_schema=False)
def docs(user: CurrentUser):
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} — API")
