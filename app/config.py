"""Settings and domain constants.

Deployment settings come from environment variables prefixed HELPDESK_ (or a .env
file in the project root). Real environment variables win over .env, which is
how Docker and the test suite override them. Everything below the settings block
is domain configuration that lives in code.
"""
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HELPDESK_", env_file=BASE_DIR / ".env",
                                      env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = BASE_DIR / "data"
    db_path: Path | None = Field(None, description="Defaults to <data_dir>/helpdesk.db")

    timezone: str = "America/New_York"

    # Business hours used to turn a target *date* into a deadline (24h, local time).
    work_start: int = 8
    work_end: int = 17

    session_days: int = 7
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    retention_days: int = Field(7, description="Days a soft-deleted record stays in the trash")

    backup_dir: Path | None = Field(None, description="Defaults to <data_dir>/backups")
    backup_keep: int = Field(14, description="Daily snapshots to keep")

    # Set when serving over HTTPS so the session cookie is never sent in clear text.
    secure_cookies: bool = False

    # Username the v2.0 migration assigns pre-account tickets to. Only matters when
    # upgrading a database that predates accounts.
    owner_username: str = "ZachZ"

    # Extra substrings that mark a pasted message as written by the team, on top of
    # the signed-in user's own username and display name. Comma separated.
    owner_hints: str = ""

    # Anything other than "production" shows a banner on every page and forces
    # mail into the outbox, so a staging copy of real data can never email anyone.
    env: str = "production"

    # Where the team reaches the app, e.g. http://172.24.47.172:8000. Used to
    # build ticket links in emails; without it the emails have no links.
    base_url: str = ""

    # --- outgoing mail (the open-ticket digest) -----------------------------
    #   outbox  -- write each email as a .eml file under <data_dir>/outbox. Sends nothing.
    #   webhook -- POST each email as JSON to a URL (a Power Automate flow).
    #   smtp    -- hand each email to a mail server.
    mail_mode: Literal["outbox", "webhook", "smtp"] = "outbox"
    mail_webhook_url: str = Field("", description="Secret: anyone with it can send mail")
    mail_proxy: str = Field("", description="HTTP proxy for the webhook, if one is needed")
    mail_from: str = "helpdesk@localhost"
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False


settings = Settings()

DATA_DIR = settings.data_dir
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = settings.db_path or DATA_DIR / "helpdesk.db"

TIMEZONE = settings.timezone
WORK_START_HOUR = settings.work_start
WORK_END_HOUR = settings.work_end
SESSION_DAYS = settings.session_days
MAX_FAILED_LOGINS = settings.max_failed_logins
LOCKOUT_MINUTES = settings.lockout_minutes
RETENTION_DAYS = settings.retention_days
BACKUP_DIR = settings.backup_dir or DATA_DIR / "backups"
BACKUP_KEEP = settings.backup_keep
SECURE_COOKIES = settings.secure_cookies
OWNER_USERNAME = settings.owner_username
OWNER_HINTS = [h.strip().lower() for h in settings.owner_hints.split(",") if h.strip()]
ENV = settings.env.strip().lower() or "production"
BASE_URL = settings.base_url.strip().rstrip("/")
OUTBOX_DIR = DATA_DIR / "outbox"
# The mail_* and smtp_* settings are read from `settings` at send time (mailer.py).

# ---------------------------------------------------------------- statuses
#
# ADDING A STATUS: add it here and to STATUS_LABELS, then decide which of the two
# sets below it belongs to. Nothing else in the codebase hardcodes this list --
# the SQL filters are generated from these sets (see repo._status_sql).
#
#   ACTIVE_STATUSES -- the SLA clock accrues time
#   OPEN_STATUSES   -- counts as "not finished"; appears under the Open filter,
#                      is eligible to be overdue, and shows on the dashboard
#
STATUSES = ["new", "open", "waiting_customer", "waiting_internal",
            "waiting_external", "resolved", "closed"]

ACTIVE_STATUSES = {"new", "open"}

OPEN_STATUSES = {"new", "open", "waiting_customer", "waiting_internal",
                 "waiting_external"}

STATUS_LABELS = {
    "new": "New",
    "open": "Open",
    "waiting_customer": "Waiting on customer",
    "waiting_internal": "Waiting on internal",
    "waiting_external": "Waiting on external",
    "resolved": "Resolved",
    "closed": "Closed",
}

CHANNELS = ["app", "web", "vui", "phone", "email", "unknown"]
CHANNEL_LABELS = {
    "app": "App",
    "web": "Web",
    "vui": "Phone (VUI)",
    "phone": "Phone (agent)",
    "email": "Email",
    "unknown": "Unknown",
}

ROLES = ["root", "admin", "user"]
ROLE_LABELS = {"root": "Root (system admin)", "admin": "Admin (sees all tickets)",
               "user": "User (sees own tickets)"}

PRIORITY_LABELS = {1: "P1 — Critical", 2: "P2 — High", 3: "P3 — Normal",
                   4: "P4 — Low", 5: "P5 — Trivial"}

DEFAULT_CATEGORIES = [
    "Complaints", "Commendation", "Service Request", "Information Request",
    "Billing", "Account Access", "App / Technical", "Accessibility (ADA)", "Other",
]

# One composer, one dropdown. key -> (label, direction, party)
MESSAGE_KINDS = {
    "out_customer": ("To customer", "out", "customer"),
    "in_customer": ("From customer", "in", "customer"),
    "out_internal": ("To internal team", "out", "internal"),
    "in_internal": ("From internal team", "in", "internal"),
    "out_vendor": ("To vendor", "out", "vendor"),
    "in_vendor": ("From vendor", "in", "vendor"),
    "note": ("Private note", "none", "internal"),
}
MESSAGE_LABELS = {k: v[0] for k, v in MESSAGE_KINDS.items()}
CORRESPONDENCE = set(MESSAGE_KINDS) - {"note"}
