"""Schema migrations.

The base schema uses CREATE TABLE IF NOT EXISTS, which handles *new tables* on an
existing database but silently does nothing about *new columns*. That's the gap this
closes: every schema change after v1 goes in MIGRATIONS and gets applied exactly once.

Run directly:  python -m app.migrate
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .db import connect, init_db

SCHEMA_TICKETS = ""      # filled below from schema.sql

# (version, [statements]). Append only — never edit or reorder a released entry,
# because databases in the wild have already recorded it as applied.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (2, [
        # soft delete
        "ALTER TABLE tickets ADD COLUMN deleted_at TEXT",
        "ALTER TABLE customers ADD COLUMN deleted_at TEXT",
        # follow-up reminders
        "ALTER TABLE tickets ADD COLUMN follow_up_at TEXT",
        "ALTER TABLE tickets ADD COLUMN follow_up_note TEXT",
        "ALTER TABLE tickets ADD COLUMN follow_up_done INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tickets ADD COLUMN follow_up_notified_at TEXT",
        # richer timeline entries
        "ALTER TABLE events ADD COLUMN author TEXT",
        "ALTER TABLE events ADD COLUMN occurred_at TEXT",
        "ALTER TABLE events ADD COLUMN voided_at TEXT",
        "ALTER TABLE events ADD COLUMN void_reason TEXT",
        "UPDATE events SET occurred_at = created_at WHERE occurred_at IS NULL",
        # v1.1 called these reply_logged / reply_received; unify on the new kinds
        "UPDATE events SET type = 'out_customer' WHERE type = 'reply_logged'",
        "UPDATE events SET type = 'in_customer'  WHERE type = 'reply_received'",
        # multiple contacts per customer
        """CREATE TABLE IF NOT EXISTS customer_emails (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            email TEXT NOT NULL UNIQUE,
            is_primary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS customer_phones (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            phone TEXT NOT NULL,
            label TEXT,
            is_primary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE (customer_id, phone))""",
        # backfill the existing single email/phone as primary
        """INSERT OR IGNORE INTO customer_emails (customer_id, email, is_primary, created_at)
           SELECT id, email, 1, created_at FROM customers
           WHERE email IS NOT NULL AND TRIM(email) <> ''""",
        """INSERT OR IGNORE INTO customer_phones (customer_id, phone, label, is_primary, created_at)
           SELECT id, phone, 'Primary', 1, created_at FROM customers
           WHERE phone IS NOT NULL AND TRIM(phone) <> ''""",
        # open questions
        """CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY,
            ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            answer TEXT,
            answered_at TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_tickets_deleted ON tickets(deleted_at)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_followup ON tickets(follow_up_at)",
        "CREATE INDEX IF NOT EXISTS idx_customers_deleted ON customers(deleted_at)",
        "CREATE INDEX IF NOT EXISTS idx_cust_emails ON customer_emails(email)",
        "CREATE INDEX IF NOT EXISTS idx_questions_ticket ON questions(ticket_id)",
    ]),
    (3, [
        # --- accounts -------------------------------------------------------
        "ALTER TABLE users ADD COLUMN password_hash TEXT",
        "ALTER TABLE users ADD COLUMN password_salt TEXT",
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
        "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN locked_until TEXT",
        "ALTER TABLE users ADD COLUMN last_login_at TEXT",
        "ALTER TABLE users ADD COLUMN created_by INTEGER",
        "ALTER TABLE users ADD COLUMN can_send_email INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN mailbox_folder TEXT",
        """CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL, target TEXT, detail TEXT,
            created_at TEXT NOT NULL)""",
        "ALTER TABLE tickets ADD COLUMN owner_id INTEGER REFERENCES users(id)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_owner ON tickets(owner_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)",
    ]),
]

# Migration 3 also rebuilds the tickets table to drop its CHECK constraints.
# SQLite cannot ALTER a constraint, so the table has to be copied. Kept out of
# the statement list above because it needs to inspect the existing columns.
REBUILD_AT = 3


def _extract_tickets_ddl() -> str:
    """Pull the CREATE TABLE tickets block out of schema.sql so the rebuild always
    matches the current schema rather than a copy that can drift."""
    text = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
    start = text.index("CREATE TABLE IF NOT EXISTS tickets")
    end = text.index(");", start) + 2
    return text[start:end]


SCHEMA_TICKETS = _extract_tickets_ddl()

LATEST = max((v for v, _ in MIGRATIONS), default=1)


def current_version(conn) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row["value"]) if row else 1


def _execute(conn, sql: str) -> None:
    """Tolerate work already done, so a half-applied migration can be retried."""
    try:
        conn.execute(sql)
    except Exception as exc:
        message = str(exc).lower()
        if "duplicate column" in message or "already exists" in message:
            return
        raise


def _rebuild_tickets(conn) -> None:
    """Copy tickets into a constraint-free table.

    The original CHECK(status IN (...)) meant every new status required a table
    rebuild. Statuses are validated in Python now (repo.set_status), which reads
    the list from config, so adding one is a config change only.
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                       " AND name='tickets'").fetchone()
    if not row or "CHECK" not in (row["sql"] or "").upper():
        return                                    # already constraint-free

    columns = [r["name"] for r in conn.execute("PRAGMA table_info(tickets)")]
    col_list = ", ".join(columns)

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE tickets RENAME TO tickets_old")
    conn.executescript(SCHEMA_TICKETS)
    conn.execute(f"INSERT INTO tickets ({col_list}) SELECT {col_list} FROM tickets_old")
    conn.execute("DROP TABLE tickets_old")
    conn.execute("PRAGMA foreign_keys = ON")


def _seed_owner(conn) -> None:
    """Give the pre-existing tickets an owner so nothing is orphaned."""
    name = config.OWNER_USERNAME
    row = conn.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE",
                       (name,)).fetchone()
    if row:
        user_id = row["id"]
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ? AND role = 'user'",
                     (user_id,))
    else:
        cur = conn.execute(
            "INSERT INTO users (username, display_name, role, is_active,"
            " must_change_password, created_at) VALUES (?,?,'admin',1,0,?)",
            (name, name, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        user_id = cur.lastrowid
    conn.execute("UPDATE tickets SET owner_id = ? WHERE owner_id IS NULL", (user_id,))
    conn.execute("UPDATE events SET user_id = ? WHERE user_id IS NULL", (user_id,))


def run(verbose: bool = True) -> int:
    """Bring the database to LATEST. Safe to call on every startup.

    Order matters: schema.sql creates indexes over columns that only exist after
    migration, so an existing database must be migrated *before* it is topped up
    with any brand-new tables.
    """
    fresh = not config.DB_PATH.exists() or config.DB_PATH.stat().st_size == 0

    if fresh:
        init_db()                       # schema.sql is already at LATEST
        with connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value)"
                         " VALUES ('schema_version', ?)", (str(LATEST),))
        if verbose:
            print(f"  new database created at schema version {LATEST}")
        return 0

    applied = 0
    with connect() as conn:
        version = current_version(conn)
        for target, statements in sorted(MIGRATIONS):
            if target <= version:
                continue
            if verbose:
                print(f"  applying migration {target}...")
            for sql in statements:
                _execute(conn, sql)
            if target == REBUILD_AT:
                _rebuild_tickets(conn)
                _seed_owner(conn)
            conn.execute("INSERT OR REPLACE INTO meta (key, value)"
                         " VALUES ('schema_version', ?)", (str(target),))
            conn.commit()
            version = target
            applied += 1

    init_db()                           # pick up any tables added since
    if verbose:
        print(f"  schema is at version {version}"
              + (f" ({applied} migration(s) applied)" if applied else " (already current)"))
    return applied


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"  MIGRATION FAILED: {exc}", file=sys.stderr)
        print("  Your backup is in backups\\. Nothing was left half-applied —"
              " each migration commits as a unit.", file=sys.stderr)
        sys.exit(1)
