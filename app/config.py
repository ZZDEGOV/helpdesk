"""Settings, loaded from .env in the project root."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()

DATA_DIR = Path(os.environ.get("HELPDESK_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("HELPDESK_DB_PATH", DATA_DIR / "helpdesk.db"))

HOST = os.environ.get("HELPDESK_HOST", "0.0.0.0")
PORT = int(os.environ.get("HELPDESK_PORT", "8000"))

# Blank password disables the login gate entirely.
PASSWORD = os.environ.get("HELPDESK_PASSWORD", "").strip()
SECRET_KEY = os.environ.get("HELPDESK_SECRET_KEY", "") or secrets.token_hex(32)

TIMEZONE = os.environ.get("HELPDESK_TIMEZONE", "America/New_York")
OWNER = os.environ.get("HELPDESK_OWNER", "me")
OWNER_USERNAME = os.environ.get("HELPDESK_OWNER_USERNAME", "ZachZ")

# Business hours used by the SLA clock (24h, local time).
WORK_START_HOUR = int(os.environ.get("HELPDESK_WORK_START", "8"))
WORK_END_HOUR = int(os.environ.get("HELPDESK_WORK_END", "17"))

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
               "user": "User"}

SESSION_DAYS = int(os.environ.get("HELPDESK_SESSION_DAYS", "7"))
MAX_FAILED_LOGINS = int(os.environ.get("HELPDESK_MAX_FAILED_LOGINS", "5"))
LOCKOUT_MINUTES = int(os.environ.get("HELPDESK_LOCKOUT_MINUTES", "15"))

PRIORITY_LABELS = {1: "P1 — Critical", 2: "P2 — High", 3: "P3 — Normal",
                   4: "P4 — Low", 5: "P5 — Trivial"}

DEFAULT_CATEGORIES = [
    "Complaints", "Commendation", "Service Request", "Information Request",
    "Billing", "Account Access", "App / Technical", "Accessibility (ADA)", "Other",
]


# Soft-deleted records are purged after this many days.
RETENTION_DAYS = int(os.environ.get("HELPDESK_RETENTION_DAYS", "7"))

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

# Substrings that identify you in a pasted thread, for guessing message direction.
OWNER_HINTS = [h.strip().lower() for h in
               os.environ.get("HELPDESK_OWNER_HINTS", OWNER).split(",") if h.strip()]
