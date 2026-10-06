-- Help Desk schema v1
-- Phase 1: standalone tracker. Columns marked (p2) are unused now but reserved
-- so email integration doesn't require a migration.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY,
    username             TEXT UNIQUE NOT NULL,
    display_name         TEXT,
    password_hash        TEXT,
    password_salt        TEXT,
    role                 TEXT NOT NULL DEFAULT 'user',
    is_active            INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    locked_until         TEXT,
    last_login_at        TEXT,
    created_by           INTEGER,
    -- reserved for phases 3/4
    can_send_email       INTEGER NOT NULL DEFAULT 0,
    mailbox_folder       TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,
    target     TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY,
    email      TEXT UNIQUE,
    name       TEXT,
    phone      TEXT,
    account_id TEXT,
    address    TEXT,
    city       TEXT,
    zip        TEXT,
    notes      TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_emails (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    email       TEXT NOT NULL UNIQUE,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_phones (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    phone       TEXT NOT NULL,
    label       TEXT,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (customer_id, phone)
);

CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY,
    ref             TEXT UNIQUE NOT NULL,
    customer_id     INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    owner_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    assignee_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,

    subject         TEXT NOT NULL,
    body            TEXT,
    channel         TEXT NOT NULL DEFAULT 'unknown',
    category        TEXT,
    subcategory     TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    priority        INTEGER NOT NULL DEFAULT 3,
    ada             INTEGER NOT NULL DEFAULT 0,
    logged_by       TEXT,

    resolution      TEXT,
    root_cause      TEXT,

    received_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    due_at          TEXT,
    first_reply_at  TEXT,
    resolved_at     TEXT,
    closed_at       TEXT,

    -- pausable clock: only accrues while status is 'new' or 'open'
    active_seconds  INTEGER NOT NULL DEFAULT 0,
    last_status_at  TEXT NOT NULL,
    reopen_count    INTEGER NOT NULL DEFAULT 0,
    deleted_at      TEXT,

    follow_up_at    TEXT,
    follow_up_note  TEXT,
    follow_up_done  INTEGER NOT NULL DEFAULT 0,
    follow_up_notified_at TEXT,

    -- (p2) email linkage
    external_ref    TEXT,
    entry_id        TEXT,
    conversation_id TEXT,
    raw             TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    type        TEXT NOT NULL,
    body        TEXT,
    payload     TEXT,
    author      TEXT,
    occurred_at TEXT,
    voided_at   TEXT,
    void_reason TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    answer      TEXT,
    answered_at TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    tag       TEXT NOT NULL,
    PRIMARY KEY (ticket_id, tag)
);

CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT,
    body       TEXT NOT NULL,
    use_count  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- (p2) suggestion accuracy tracking
CREATE TABLE IF NOT EXISTS extractions (
    id         INTEGER PRIMARY KEY,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    suggested  TEXT,
    confirmed  TEXT,
    tier       INTEGER,
    confidence REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_status   ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_customer ON tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_tickets_due      ON tickets(due_at);
CREATE INDEX IF NOT EXISTS idx_tickets_created  ON tickets(created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_external ON tickets(external_ref);
CREATE INDEX IF NOT EXISTS idx_tickets_conv     ON tickets(conversation_id);
CREATE INDEX IF NOT EXISTS idx_events_ticket    ON events(ticket_id);
CREATE INDEX IF NOT EXISTS idx_customers_email  ON customers(email);
CREATE INDEX IF NOT EXISTS idx_tickets_deleted   ON tickets(deleted_at);
CREATE INDEX IF NOT EXISTS idx_tickets_followup  ON tickets(follow_up_at);
CREATE INDEX IF NOT EXISTS idx_customers_deleted ON customers(deleted_at);
CREATE INDEX IF NOT EXISTS idx_cust_emails       ON customer_emails(email);
CREATE INDEX IF NOT EXISTS idx_questions_ticket  ON questions(ticket_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires   ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_tickets_owner      ON tickets(owner_id);
CREATE INDEX IF NOT EXISTS idx_audit_created      ON audit_log(created_at);
