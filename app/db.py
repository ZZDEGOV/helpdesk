"""SQLite connection management."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def utcnow() -> str:
    """Timestamps are stored as UTC ISO strings throughout."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    """Create tables and seed the owner user + starter templates."""
    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

        # No user is seeded here. A fresh install goes through /setup to create
        # root; an upgraded install gets its owner from migrate._seed_owner.
        row = conn.execute("SELECT COUNT(*) c FROM templates").fetchone()
        if row["c"] == 0:
            for name, cat, body in _STARTER_TEMPLATES:
                conn.execute(
                    "INSERT INTO templates (name, category, body, created_at, updated_at)"
                    " VALUES (?,?,?,?,?)",
                    (name, cat, body, utcnow(), utcnow()),
                )

        # OR IGNORE, not OR REPLACE — migrate.py owns this value once set.
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version','1')"
        )


_STARTER_TEMPLATES = [
    (
        "Acknowledge receipt",
        "General",
        "Hello {{first_name}},\n\nThank you for contacting us regarding {{subject}}. "
        "Your reference number is {{ref}}.\n\nI'm looking into this now and will follow "
        "up with you by {{due_date}}.\n\nRegards,\n{{owner}}",
    ),
    (
        "Need more information",
        "General",
        "Hello {{first_name}},\n\nThanks for your patience on {{ref}}. To continue "
        "investigating, could you confirm the following:\n\n- \n- \n\nOnce I have that "
        "I'll be able to move this forward.\n\nRegards,\n{{owner}}",
    ),
    (
        "Resolved — no action needed",
        "General",
        "Hello {{first_name}},\n\nGood news — the issue described in {{ref}} has been "
        "resolved. No further action is needed on your end.\n\nIf you run into anything "
        "else, just reply to this message.\n\nRegards,\n{{owner}}",
    ),
    (
        "Escalated to another team",
        "General",
        "Hello {{first_name}},\n\nI've escalated {{ref}} to the team that handles this "
        "directly. They'll be in touch, and I'll keep an eye on it in the "
        "meantime.\n\nRegards,\n{{owner}}",
    ),
]
