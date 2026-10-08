"""Jinja2 setup, template filters, and local-time conversion."""
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, config, repo

BASE = Path(__file__).resolve().parent
LOCAL_TZ = ZoneInfo(config.TIMEZONE)     # tzdata is a dependency, so this always resolves

templates = Jinja2Templates(directory=BASE / "templates")


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)


def see_other(url: str) -> RedirectResponse:
    """Post/redirect/get: every form POST answers with a 303 to a page."""
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- time

def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_utc(value: datetime | date | str | None, end_of_day: bool = False) -> str | None:
    """Turn a local date or datetime from a form into a stored UTC ISO string.

    A bare date means "by the end of the work day" when end_of_day is set, and
    midnight otherwise."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        value = datetime.combine(
            value, time(hour=config.WORK_END_HOUR) if end_of_day else time())
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- filters

def fmt_dt(value, fmt="%b %d, %Y %I:%M %p"):
    if not value:
        return "—"
    # %I zero-pads the hour; strip it so 09:05 reads 9:05 (portable, unlike %-I).
    return _parse(value).astimezone(LOCAL_TZ).strftime(fmt).replace(" 0", " ")


def fmt_date(value):
    return fmt_dt(value, "%b %d, %Y")


def date_input(value):
    """YYYY-MM-DD in local time, for <input type=date> round-tripping."""
    if not value:
        return ""
    return _parse(value).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


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
    delta = (datetime.now(timezone.utc) - _parse(value)).total_seconds()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 604800:
        return f"{int(delta // 86400)}d ago"
    return fmt_date(value)


def current_user(request: Request):
    """Set by deps.current_user; None on the login and setup pages."""
    return getattr(request.state, "user", None)


templates.env.filters.update(dt=fmt_dt, date=fmt_date, duration=fmt_duration,
                             relative=relative, date_input=date_input)
templates.env.globals.update(config=config, live_seconds=repo.live_active_seconds,
                             is_overdue=repo.is_overdue, to_utc=to_utc, now_iso=now_iso,
                             can=auth.can, current_user=current_user)
