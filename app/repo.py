"""Data access. All ticket mutations go through here so the timeline and the
SLA clock stay consistent."""
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import config
from .db import connect, utcnow


# ---------------------------------------------------------------- helpers

def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status_sql(statuses) -> str:
    """Build an IN (...) clause from a config set.

    Every status filter in this module goes through here. Hardcoding the list in
    SQL is what previously made a newly added status disappear from the default
    ticket view -- the literal and the config had drifted apart."""
    return "(" + ",".join(f"'{s}'" for s in sorted(statuses)) + ")"


OPEN_SQL = _status_sql(config.OPEN_STATUSES)
ACTIVE_SQL = _status_sql(config.ACTIVE_STATUSES)


def next_ref(conn: sqlite3.Connection) -> str:
    """HD-2026-0001, sequential within the calendar year."""
    year = _now().year
    prefix = f"HD-{year}-"
    row = conn.execute(
        "SELECT ref FROM tickets WHERE ref LIKE ? ORDER BY ref DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    seq = int(row["ref"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{seq:04d}"


def log_event(conn, ticket_id: int, type_: str, body: str | None = None,
              payload: dict | None = None, author: str | None = None,
              occurred_at: str | None = None) -> int:
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO events (ticket_id, user_id, type, body, payload, author,"
        " occurred_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (ticket_id, getattr(conn, "user_id", None), type_, body,
         json.dumps(payload) if payload else None, author, occurred_at or now, now),
    )
    return cur.lastrowid


# ---------------------------------------------------------------- clock

def live_active_seconds(ticket: sqlite3.Row | dict) -> int:
    """Accrued active time, plus time since the last status change if the
    ticket is currently in an active status. This is the number to display."""
    base = ticket["active_seconds"]
    if ticket["status"] in config.ACTIVE_STATUSES:
        last = _parse(ticket["last_status_at"])
        if last:
            base += int((_now() - last).total_seconds())
    return base


def _accrue(conn, ticket: sqlite3.Row) -> int:
    """Fold elapsed time into active_seconds. Call before every status change."""
    if ticket["status"] not in config.ACTIVE_STATUSES:
        return ticket["active_seconds"]
    last = _parse(ticket["last_status_at"])
    if not last:
        return ticket["active_seconds"]
    return ticket["active_seconds"] + int((_now() - last).total_seconds())


def is_overdue(ticket) -> bool:
    due = _parse(ticket["due_at"])
    if not due or ticket["status"] not in config.OPEN_STATUSES:
        return False
    return _now() > due


# ---------------------------------------------------------------- customers

def upsert_customer(conn, *, email: str | None, name: str | None = None,
                    phone: str | None = None, account_id: str | None = None,
                    address: str | None = None, city: str | None = None,
                    zip_: str | None = None) -> int | None:
    """Match on email. Only fills blank fields so manual edits aren't clobbered."""
    email = (email or "").strip().lower() or None
    if not email and not name:
        return None

    if email:
        row = find_customer_by_email(conn, email)
        if row:
            updates, params = [], []
            for col, val in (("name", name), ("phone", phone), ("account_id", account_id),
                             ("address", address), ("city", city), ("zip", zip_)):
                if val and not row[col]:
                    updates.append(f"{col} = ?")
                    params.append(val)
            if updates:
                params.extend([utcnow(), row["id"]])
                conn.execute(
                    f"UPDATE customers SET {', '.join(updates)}, updated_at = ? WHERE id = ?",
                    params,
                )
            if phone:
                add_phone(conn, row["id"], phone)
            return row["id"]

    cur = conn.execute(
        "INSERT INTO customers (email, name, phone, account_id, address, city, zip,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (email, name, phone, account_id, address, city, zip_, utcnow(), utcnow()),
    )
    customer_id = cur.lastrowid
    if email:
        add_email(conn, customer_id, email, primary=True)
    if phone:
        add_phone(conn, customer_id, phone, primary=True)
    return customer_id


def get_customer(conn, customer_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


def list_customers(conn, q: str | None = None, include_deleted: bool = False,
                   owner_id: int | None = None) -> list[sqlite3.Row]:
    """The customer directory is shared; owner_id only scopes the ticket counts,
    so a user isn't told how many tickets exist that they can't open."""
    params: list[Any] = []
    owner_clause = ""
    if owner_id is not None:
        owner_clause = " AND t.owner_id = ?"
        params.append(owner_id)
    sql = ("SELECT c.*, COUNT(t.id) AS ticket_count,"
           f" SUM(CASE WHEN t.status IN {OPEN_SQL} THEN 1 ELSE 0 END) AS open_count"
           " FROM customers c LEFT JOIN tickets t"
           " ON t.customer_id = c.id AND t.deleted_at IS NULL" + owner_clause
           + (" WHERE 1=1" if include_deleted else " WHERE c.deleted_at IS NULL"))
    if q:
        sql += (" AND (c.email LIKE ? OR c.name LIKE ? OR c.phone LIKE ?"
                " OR c.account_id LIKE ? OR EXISTS (SELECT 1 FROM customer_emails e"
                " WHERE e.customer_id = c.id AND e.email LIKE ?))")
        params += [f"%{q}%"] * 5
    sql += " GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------- tickets

def create_ticket(conn, *, subject: str, body: str | None = None,
                  customer_email: str | None = None, customer_name: str | None = None,
                  customer_phone: str | None = None, account_id: str | None = None,
                  address: str | None = None, city: str | None = None,
                  zip_: str | None = None, channel: str = "unknown",
                  category: str | None = None, subcategory: str | None = None,
                  priority: int = 3, status: str = "new", ada: bool = False,
                  logged_by: str | None = None, received_at: str | None = None,
                  due_at: str | None = None, external_ref: str | None = None,
                  entry_id: str | None = None, conversation_id: str | None = None,
                  raw: dict | None = None, tags: Iterable[str] = (),
                  owner_id: int | None = None) -> int:
    # Take the write lock before reading the last ref. Otherwise two people
    # creating tickets at once both compute the same next ref and one of them
    # fails on the UNIQUE constraint.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    now = utcnow()
    customer_id = upsert_customer(
        conn, email=customer_email, name=customer_name, phone=customer_phone,
        account_id=account_id, address=address, city=city, zip_=zip_,
    )
    ref = next_ref(conn)
    cur = conn.execute(
        "INSERT INTO tickets (ref, customer_id, owner_id, subject, body, channel,"
        " category, subcategory, status, priority, ada, logged_by, received_at,"
        " created_at, updated_at, due_at, active_seconds, last_status_at,"
        " external_ref, entry_id, conversation_id, raw)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
        (ref, customer_id, owner_id, subject.strip(), body, channel, category, subcategory,
         status, priority, int(ada), logged_by, received_at or now, now, now, due_at,
         now, external_ref, entry_id, conversation_id,
         json.dumps(raw) if raw else None),
    )
    ticket_id = cur.lastrowid
    log_event(conn, ticket_id, "created", f"Ticket {ref} created")
    for tag in tags:
        tag = tag.strip()
        if tag:
            conn.execute("INSERT OR IGNORE INTO tags (ticket_id, tag) VALUES (?,?)",
                         (ticket_id, tag))
    return ticket_id


def get_ticket(conn, ticket_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT t.*, c.email AS customer_email, c.name AS customer_name,"
        " c.phone AS customer_phone, c.account_id AS customer_account_id"
        " FROM tickets t LEFT JOIN customers c ON c.id = t.customer_id"
        " WHERE t.id = ?", (ticket_id,)).fetchone()


def get_ticket_by_ref(conn, ref: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT id FROM tickets WHERE ref = ?", (ref,)).fetchone()
    return get_ticket(conn, row["id"]) if row else None


def list_tickets(conn, *, status: str | None = None, priority: int | None = None,
                 category: str | None = None, channel: str | None = None,
                 q: str | None = None, customer_id: int | None = None,
                 overdue_only: bool = False, sort: str = "priority",
                 limit: int = 500, include_deleted: bool = False,
                 owner_id: int | None = None) -> list[sqlite3.Row]:
    """owner_id restricts the list to one user's tickets -- pass it for anyone
    who can't see all tickets (see deps.visible_owner)."""
    sql = ("SELECT t.*, c.email AS customer_email, c.name AS customer_name,"
           " u.username AS owner_username"
           " FROM tickets t LEFT JOIN customers c ON c.id = t.customer_id"
           " LEFT JOIN users u ON u.id = t.owner_id"
           + (" WHERE 1=1" if include_deleted else " WHERE t.deleted_at IS NULL"))
    params: list[Any] = []

    if owner_id is not None:
        sql += " AND t.owner_id = ?"
        params.append(owner_id)

    if status == "open":
        sql += f" AND t.status IN {OPEN_SQL}"
    elif status == "actionable":
        sql += f" AND t.status IN {ACTIVE_SQL}"
    elif status:
        sql += " AND t.status = ?"
        params.append(status)

    if priority:
        sql += " AND t.priority = ?"
        params.append(priority)
    if category:
        sql += " AND t.category = ?"
        params.append(category)
    if channel:
        sql += " AND t.channel = ?"
        params.append(channel)
    if customer_id:
        sql += " AND t.customer_id = ?"
        params.append(customer_id)
    if overdue_only:
        sql += (" AND t.due_at IS NOT NULL AND t.due_at < ?"
                f" AND t.status IN {OPEN_SQL}")
        params.append(utcnow())
    if q:
        sql += (" AND (t.subject LIKE ? OR t.body LIKE ? OR t.ref LIKE ?"
                " OR c.email LIKE ? OR c.name LIKE ? OR t.external_ref LIKE ?)")
        params += [f"%{q}%"] * 6

    orders = {
        "priority": " ORDER BY t.priority ASC, t.due_at IS NULL, t.due_at ASC",
        "newest": " ORDER BY t.received_at DESC",
        "oldest": " ORDER BY t.received_at ASC",
        "due": " ORDER BY t.due_at IS NULL, t.due_at ASC",
        "updated": " ORDER BY t.updated_at DESC",
    }
    sql += orders.get(sort, orders["priority"]) + " LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def set_status(conn, ticket_id: int, new_status: str, user_id: int | None = None) -> None:
    if new_status not in config.STATUSES:          # was a SQL CHECK constraint
        raise ValueError(f"unknown status: {new_status}")
    ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket or new_status == ticket["status"]:
        return

    accrued = _accrue(conn, ticket)
    now = utcnow()
    fields = {"status": new_status, "active_seconds": accrued,
              "last_status_at": now, "updated_at": now}

    if new_status == "resolved" and not ticket["resolved_at"]:
        fields["resolved_at"] = now
    if new_status == "closed" and not ticket["closed_at"]:
        fields["closed_at"] = now
    if ticket["status"] in ("resolved", "closed") and new_status in config.OPEN_STATUSES:
        fields["reopen_count"] = ticket["reopen_count"] + 1
        fields["resolved_at"] = None
        fields["closed_at"] = None

    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE tickets SET {sets} WHERE id = ?",
                 [*fields.values(), ticket_id])
    log_event(conn, ticket_id, "status_change",
              f"{config.STATUS_LABELS[ticket['status']]} → "
              f"{config.STATUS_LABELS[new_status]}",
              {"from": ticket["status"], "to": new_status})


ALLOWED_FIELDS = {"subject", "body", "priority", "category", "subcategory", "channel",
                  "resolution", "root_cause", "due_at", "ada", "logged_by"}


def update_fields(conn, ticket_id: int, changes: dict[str, Any]) -> None:
    ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        return
    applied = {}
    for key, val in changes.items():
        if key not in ALLOWED_FIELDS:
            continue
        if val == "":
            val = None
        if str(ticket[key] or "") != str(val or ""):
            applied[key] = val
    if not applied:
        return
    applied["updated_at"] = utcnow()
    sets = ", ".join(f"{k} = ?" for k in applied)
    conn.execute(f"UPDATE tickets SET {sets} WHERE id = ?", [*applied.values(), ticket_id])
    for key, val in applied.items():
        if key == "updated_at":
            continue
        log_event(conn, ticket_id, "field_change",
                  f"{key.replace('_', ' ').title()} changed",
                  {"field": key, "from": ticket[key], "to": val})


def add_note(conn, ticket_id: int, body: str) -> None:
    body = body.strip()
    if not body:
        return
    log_event(conn, ticket_id, "note", body)
    conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (utcnow(), ticket_id))


def log_message(conn, ticket_id: int, kind: str, body: str,
                author: str | None = None, occurred_at: str | None = None,
                auto_status: bool = True) -> int:
    """One entry point for every timeline message. `kind` is a key from
    config.MESSAGE_KINDS, which carries direction and party."""
    body = body.strip()
    if not body or kind not in config.MESSAGE_KINDS:
        return 0
    ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    event_id = log_event(conn, ticket_id, kind, body, author=author,
                         occurred_at=occurred_at)

    if auto_status and ticket:
        # A customer writing back puts the ball in our court again.
        if kind == "in_customer" and ticket["status"] == "waiting_customer":
            set_status(conn, ticket_id, "open")
        # An internal team answering does the same for internal waits.
        elif kind == "in_internal" and ticket["status"] == "waiting_internal":
            set_status(conn, ticket_id, "open")
        elif kind == "in_vendor" and ticket["status"] in ("waiting_external",
                                                         "waiting_internal"):
            set_status(conn, ticket_id, "open")

    recompute_first_reply(conn, ticket_id)
    conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (utcnow(), ticket_id))
    return event_id


def recompute_first_reply(conn, ticket_id: int) -> None:
    """Derived from the timeline rather than stored on write, so voiding a
    mis-logged reply corrects the metric instead of leaving it skewed."""
    row = conn.execute(
        "SELECT MIN(occurred_at) t FROM events WHERE ticket_id = ?"
        " AND type = 'out_customer' AND voided_at IS NULL", (ticket_id,)).fetchone()
    conn.execute("UPDATE tickets SET first_reply_at = ? WHERE id = ?",
                 (row["t"] if row else None, ticket_id))


# ---------------------------------------------------------------- event edits

def void_event(conn, event_id: int, reason: str = "") -> None:
    """Strike through rather than delete — a mistake you can still see."""
    row = conn.execute("SELECT ticket_id FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.execute("UPDATE events SET voided_at = ?, void_reason = ? WHERE id = ?",
                 (utcnow(), reason.strip() or None, event_id))
    if row:
        recompute_first_reply(conn, row["ticket_id"])


def unvoid_event(conn, event_id: int) -> None:
    row = conn.execute("SELECT ticket_id FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.execute("UPDATE events SET voided_at = NULL, void_reason = NULL WHERE id = ?",
                 (event_id,))
    if row:
        recompute_first_reply(conn, row["ticket_id"])


def set_event_time(conn, event_id: int, occurred_at: str) -> None:
    """Correct the order of entries logged out of sequence."""
    row = conn.execute("SELECT ticket_id FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.execute("UPDATE events SET occurred_at = ? WHERE id = ?", (occurred_at, event_id))
    if row:
        recompute_first_reply(conn, row["ticket_id"])


def set_event_body(conn, event_id: int, body: str) -> None:
    conn.execute("UPDATE events SET body = ? WHERE id = ?", (body.strip(), event_id))


def event_ticket_id(conn, event_id: int) -> int | None:
    row = conn.execute("SELECT ticket_id FROM events WHERE id = ?", (event_id,)).fetchone()
    return row["ticket_id"] if row else None


def question_ticket_id(conn, question_id: int) -> int | None:
    row = conn.execute("SELECT ticket_id FROM questions WHERE id = ?",
                       (question_id,)).fetchone()
    return row["ticket_id"] if row else None


def get_events(conn, ticket_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE ticket_id = ?"
        " ORDER BY COALESCE(occurred_at, created_at) ASC, id ASC",
        (ticket_id,)).fetchall()


def get_tags(conn, ticket_id: int) -> list[str]:
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM tags WHERE ticket_id = ? ORDER BY tag", (ticket_id,))]


def add_tag(conn, ticket_id: int, tag: str) -> None:
    tag = tag.strip().lower()
    if tag:
        conn.execute("INSERT OR IGNORE INTO tags (ticket_id, tag) VALUES (?,?)",
                     (ticket_id, tag))
        log_event(conn, ticket_id, "tag", f"Tagged {tag}")


def remove_tag(conn, ticket_id: int, tag: str) -> None:
    conn.execute("DELETE FROM tags WHERE ticket_id = ? AND tag = ?", (ticket_id, tag))


def all_tags(conn) -> list[str]:
    return [r["tag"] for r in conn.execute(
        "SELECT tag, COUNT(*) c FROM tags GROUP BY tag ORDER BY c DESC LIMIT 40")]


def all_categories(conn) -> list[str]:
    found = [r["category"] for r in conn.execute(
        "SELECT DISTINCT category FROM tickets WHERE category IS NOT NULL")]
    return sorted(set(config.DEFAULT_CATEGORIES) | set(found))


# ---------------------------------------------------------------- templates

def list_templates(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM templates ORDER BY category, name COLLATE NOCASE").fetchall()


def get_template(conn, template_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()


def save_template(conn, *, template_id: int | None, name: str, category: str | None,
                  body: str) -> int:
    if template_id:
        conn.execute(
            "UPDATE templates SET name=?, category=?, body=?, updated_at=? WHERE id=?",
            (name, category, body, utcnow(), template_id))
        return template_id
    cur = conn.execute(
        "INSERT INTO templates (name, category, body, created_at, updated_at)"
        " VALUES (?,?,?,?,?)", (name, category, body, utcnow(), utcnow()))
    return cur.lastrowid


def delete_template(conn, template_id: int) -> None:
    conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))


def render_template(body: str, ticket: sqlite3.Row | None, signer: str = "") -> str:
    """Fill {{placeholders}} from a ticket. Unknown ones are left visible so you
    notice them rather than sending an empty gap.

    `signer` fills {{owner}} -- the person drafting the reply, not a global
    setting, so each teammate's replies go out under their own name."""
    if ticket is None:
        return body
    name = (ticket["customer_name"] or "").strip()
    due = _parse(ticket["due_at"])
    values = {
        "first_name": name.split()[0] if name else "there",
        "full_name": name or "there",
        "ref": ticket["ref"],
        "subject": ticket["subject"],
        "owner": signer or "{{owner}}",
        "due_date": due.strftime("%B %d, %Y") if due else "shortly",
        "category": ticket["category"] or "",
        "external_ref": ticket["external_ref"] or "",
    }
    out = body
    for key, val in values.items():
        out = out.replace("{{" + key + "}}", str(val))
    return out


# ---------------------------------------------------------------- dashboard

def dashboard_stats(conn, owner_id: int | None = None) -> dict[str, Any]:
    """Counts for the dashboard cards. owner_id scopes them to one user's tickets."""
    scope, scope_params = ("", []) if owner_id is None else (" AND owner_id = ?", [owner_id])

    counts = {s: 0 for s in config.STATUSES}
    for row in conn.execute("SELECT status, COUNT(*) c FROM tickets"
                            " WHERE deleted_at IS NULL" + scope + " GROUP BY status",
                            scope_params):
        counts[row["status"]] = row["c"]

    overdue = conn.execute(
        "SELECT COUNT(*) c FROM tickets WHERE deleted_at IS NULL AND due_at IS NOT NULL"
        f" AND due_at < ? AND status IN {OPEN_SQL}" + scope,
        [utcnow(), *scope_params]).fetchone()["c"]

    week_ago = (_now() - timedelta(days=7)).isoformat(timespec="seconds")
    resolved_week = conn.execute(
        "SELECT COUNT(*) c FROM tickets WHERE deleted_at IS NULL"
        " AND resolved_at IS NOT NULL AND resolved_at > ?" + scope,
        [week_ago, *scope_params]).fetchone()["c"]

    by_category = conn.execute(
        "SELECT COALESCE(category,'Uncategorized') category, COUNT(*) c FROM tickets"
        " WHERE deleted_at IS NULL"
        f" AND status IN {OPEN_SQL}" + scope +
        " GROUP BY category ORDER BY c DESC LIMIT 6", scope_params).fetchall()

    return {
        "counts": counts,
        "actionable": counts["new"] + counts["open"],
        "open_total": sum(counts[s] for s in config.OPEN_STATUSES),
        "overdue": overdue,
        "resolved_week": resolved_week,
        "by_category": by_category,
        "follow_ups": len(due_follow_ups(conn, owner_id)),
        "deleted": conn.execute(
            "SELECT (SELECT COUNT(*) FROM tickets WHERE deleted_at IS NOT NULL)"
            " + (SELECT COUNT(*) FROM customers WHERE deleted_at IS NOT NULL) c"
        ).fetchone()["c"],
    }


# ---------------------------------------------------------------- contacts

def customer_emails(conn, customer_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM customer_emails WHERE customer_id = ?"
        " ORDER BY is_primary DESC, email", (customer_id,)).fetchall()


def customer_phones(conn, customer_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM customer_phones WHERE customer_id = ?"
        " ORDER BY is_primary DESC, phone", (customer_id,)).fetchall()


def add_email(conn, customer_id: int, email: str, primary: bool = False) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    existing = conn.execute("SELECT * FROM customer_emails WHERE email = ?",
                            (email,)).fetchone()
    if existing and existing["customer_id"] != customer_id:
        raise ValueError(f"{email} already belongs to another customer")
    if not existing:
        conn.execute("INSERT INTO customer_emails (customer_id, email, is_primary,"
                     " created_at) VALUES (?,?,0,?)", (customer_id, email, utcnow()))
    if primary or not conn.execute(
            "SELECT 1 FROM customer_emails WHERE customer_id = ? AND is_primary = 1",
            (customer_id,)).fetchone():
        set_primary_email(conn, customer_id, email)


def set_primary_email(conn, customer_id: int, email: str) -> None:
    email = email.strip().lower()
    conn.execute("UPDATE customer_emails SET is_primary = 0 WHERE customer_id = ?",
                 (customer_id,))
    conn.execute("UPDATE customer_emails SET is_primary = 1"
                 " WHERE customer_id = ? AND email = ?", (customer_id, email))
    conn.execute("UPDATE customers SET email = ?, updated_at = ? WHERE id = ?",
                 (email, utcnow(), customer_id))


def remove_email(conn, customer_id: int, email: str) -> None:
    rows = customer_emails(conn, customer_id)
    if len(rows) <= 1:
        return                      # never leave a customer with no way to reach them
    conn.execute("DELETE FROM customer_emails WHERE customer_id = ? AND email = ?",
                 (customer_id, email.strip().lower()))
    remaining = customer_emails(conn, customer_id)
    if remaining and not any(r["is_primary"] for r in remaining):
        set_primary_email(conn, customer_id, remaining[0]["email"])


def add_phone(conn, customer_id: int, phone: str, label: str = "",
              primary: bool = False) -> None:
    phone = (phone or "").strip()
    if not phone:
        return
    conn.execute("INSERT OR IGNORE INTO customer_phones (customer_id, phone, label,"
                 " is_primary, created_at) VALUES (?,?,?,0,?)",
                 (customer_id, phone, label.strip() or None, utcnow()))
    if primary or not conn.execute(
            "SELECT 1 FROM customer_phones WHERE customer_id = ? AND is_primary = 1",
            (customer_id,)).fetchone():
        set_primary_phone(conn, customer_id, phone)


def set_primary_phone(conn, customer_id: int, phone: str) -> None:
    conn.execute("UPDATE customer_phones SET is_primary = 0 WHERE customer_id = ?",
                 (customer_id,))
    conn.execute("UPDATE customer_phones SET is_primary = 1"
                 " WHERE customer_id = ? AND phone = ?", (customer_id, phone.strip()))
    conn.execute("UPDATE customers SET phone = ?, updated_at = ? WHERE id = ?",
                 (phone.strip(), utcnow(), customer_id))


def remove_phone(conn, customer_id: int, phone: str) -> None:
    conn.execute("DELETE FROM customer_phones WHERE customer_id = ? AND phone = ?",
                 (customer_id, phone.strip()))


def find_customer_by_email(conn, email: str) -> sqlite3.Row | None:
    """Looks across every address on file, not just the primary."""
    email = (email or "").strip().lower()
    if not email:
        return None
    row = conn.execute(
        "SELECT c.* FROM customers c JOIN customer_emails e ON e.customer_id = c.id"
        " WHERE e.email = ? AND c.deleted_at IS NULL", (email,)).fetchone()
    return row or conn.execute(
        "SELECT * FROM customers WHERE email = ? AND deleted_at IS NULL",
        (email,)).fetchone()


def update_customer(conn, customer_id: int, **fields) -> None:
    allowed = {"name", "account_id", "address", "city", "zip", "notes"}
    changes = {k: (v or None) for k, v in fields.items() if k in allowed}
    if not changes:
        return
    changes["updated_at"] = utcnow()
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE customers SET {sets} WHERE id = ?",
                 [*changes.values(), customer_id])


def merge_customers(conn, source_id: int, target_id: int) -> int:
    """Fold source into target: move tickets, contacts, and notes, then remove
    the source outright. Used for duplicates from typo'd addresses."""
    if source_id == target_id:
        return 0
    source = get_customer(conn, source_id)
    target = get_customer(conn, target_id)
    if not source or not target:
        return 0

    moved = conn.execute("SELECT COUNT(*) c FROM tickets WHERE customer_id = ?",
                         (source_id,)).fetchone()["c"]
    conn.execute("UPDATE tickets SET customer_id = ? WHERE customer_id = ?",
                 (target_id, source_id))
    conn.execute("UPDATE OR IGNORE customer_emails SET customer_id = ?, is_primary = 0"
                 " WHERE customer_id = ?", (target_id, source_id))
    conn.execute("UPDATE OR IGNORE customer_phones SET customer_id = ?, is_primary = 0"
                 " WHERE customer_id = ?", (target_id, source_id))

    for col in ("name", "account_id", "address", "city", "zip"):
        if source[col] and not target[col]:
            conn.execute(f"UPDATE customers SET {col} = ? WHERE id = ?",
                         (source[col], target_id))
    if source["notes"]:
        merged = "\n\n".join(x for x in (target["notes"], source["notes"]) if x)
        conn.execute("UPDATE customers SET notes = ? WHERE id = ?", (merged, target_id))

    conn.execute("DELETE FROM customers WHERE id = ?", (source_id,))
    conn.execute("UPDATE customers SET updated_at = ? WHERE id = ?", (utcnow(), target_id))
    return moved


# ---------------------------------------------------------------- soft delete

def soft_delete_ticket(conn, ticket_id: int) -> None:
    conn.execute("UPDATE tickets SET deleted_at = ?, updated_at = ? WHERE id = ?",
                 (utcnow(), utcnow(), ticket_id))


def restore_ticket(conn, ticket_id: int) -> None:
    conn.execute("UPDATE tickets SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                 (utcnow(), ticket_id))


def hard_delete_ticket(conn, ticket_id: int) -> None:
    conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))


def soft_delete_customer(conn, customer_id: int) -> None:
    """Hides the customer and their tickets. Purged together after the
    retention window."""
    now = utcnow()
    conn.execute("UPDATE customers SET deleted_at = ?, updated_at = ? WHERE id = ?",
                 (now, now, customer_id))
    conn.execute("UPDATE tickets SET deleted_at = ? WHERE customer_id = ?"
                 " AND deleted_at IS NULL", (now, customer_id))


def restore_customer(conn, customer_id: int) -> None:
    row = conn.execute("SELECT deleted_at FROM customers WHERE id = ?",
                       (customer_id,)).fetchone()
    if not row or not row["deleted_at"]:
        return
    conn.execute("UPDATE tickets SET deleted_at = NULL WHERE customer_id = ?"
                 " AND deleted_at = ?", (customer_id, row["deleted_at"]))
    conn.execute("UPDATE customers SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                 (utcnow(), customer_id))


def hard_delete_customer(conn, customer_id: int) -> None:
    conn.execute("DELETE FROM tickets WHERE customer_id = ?", (customer_id,))
    conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))


def list_deleted(conn) -> dict[str, list]:
    return {
        "tickets": conn.execute(
            "SELECT t.*, c.name AS customer_name, c.email AS customer_email"
            " FROM tickets t LEFT JOIN customers c ON c.id = t.customer_id"
            " WHERE t.deleted_at IS NOT NULL ORDER BY t.deleted_at DESC").fetchall(),
        "customers": conn.execute(
            "SELECT * FROM customers WHERE deleted_at IS NOT NULL"
            " ORDER BY deleted_at DESC").fetchall(),
    }


def purge_expired(conn) -> tuple[int, int]:
    """Permanently remove anything soft-deleted longer than the retention window.
    Cheap enough to run on every dashboard load; no scheduler needed."""
    cutoff = (_now() - timedelta(days=config.RETENTION_DAYS)).isoformat(timespec="seconds")
    customers = conn.execute(
        "SELECT id FROM customers WHERE deleted_at IS NOT NULL AND deleted_at < ?",
        (cutoff,)).fetchall()
    for row in customers:
        hard_delete_customer(conn, row["id"])
    cur = conn.execute(
        "DELETE FROM tickets WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))
    return len(customers), cur.rowcount


# ---------------------------------------------------------------- questions

def add_question(conn, ticket_id: int, text: str) -> None:
    text = text.strip()
    if not text:
        return
    row = conn.execute("SELECT COALESCE(MAX(sort_order),0) m FROM questions"
                       " WHERE ticket_id = ?", (ticket_id,)).fetchone()
    conn.execute("INSERT INTO questions (ticket_id, text, sort_order, created_at)"
                 " VALUES (?,?,?,?)", (ticket_id, text, row["m"] + 1, utcnow()))


def answer_question(conn, question_id: int, answer: str) -> None:
    answer = answer.strip()
    conn.execute("UPDATE questions SET answer = ?, answered_at = ? WHERE id = ?",
                 (answer or None, utcnow() if answer else None, question_id))


def unanswer_question(conn, question_id: int) -> None:
    conn.execute("UPDATE questions SET answer = NULL, answered_at = NULL WHERE id = ?",
                 (question_id,))


def delete_question(conn, question_id: int) -> None:
    conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))


def get_questions(conn, ticket_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM questions WHERE ticket_id = ?"
                        " ORDER BY sort_order, id", (ticket_id,)).fetchall()


def extract_questions(text: str) -> list[str]:
    """Pull candidate questions out of a drafted reply so bullet lists of asks
    become trackable without retyping them."""
    found = []
    for raw in text.splitlines():
        line = raw.strip()
        bullet = re.match(r"^[\*\-\u2022\u00b7]\s+(.*)$", line)
        if bullet:
            line = bullet.group(1).strip()
        elif not line.endswith("?"):
            continue
        if line.endswith("?") and len(line) > 12:
            found.append(line)
    return found


# ---------------------------------------------------------------- follow-ups

def set_follow_up(conn, ticket_id: int, when: str | None, note: str = "") -> None:
    conn.execute("UPDATE tickets SET follow_up_at = ?, follow_up_note = ?,"
                 " follow_up_done = 0, follow_up_notified_at = NULL, updated_at = ?"
                 " WHERE id = ?", (when, note.strip() or None, utcnow(), ticket_id))
    if when:
        log_event(conn, ticket_id, "follow_up", f"Follow-up set for {when[:10]}"
                  + (f" — {note.strip()}" if note.strip() else ""))


def clear_follow_up(conn, ticket_id: int, done: bool = True) -> None:
    conn.execute("UPDATE tickets SET follow_up_done = ?, updated_at = ? WHERE id = ?",
                 (1 if done else 0, utcnow(), ticket_id))


def due_follow_ups(conn, owner_id: int | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT t.*, c.name AS customer_name, c.email AS customer_email"
           " FROM tickets t LEFT JOIN customers c ON c.id = t.customer_id"
           " WHERE t.follow_up_at IS NOT NULL AND t.follow_up_done = 0"
           " AND t.deleted_at IS NULL AND t.follow_up_at <= ?")
    params: list[Any] = [utcnow()]
    if owner_id is not None:
        sql += " AND t.owner_id = ?"
        params.append(owner_id)
    return conn.execute(sql + " ORDER BY t.follow_up_at ASC", params).fetchall()


def pending_notifications(conn) -> list[sqlite3.Row]:
    """Due follow-ups not yet emailed. Phase 2 will drain this into an outbox;
    for now it just backs the dashboard."""
    return conn.execute(
        "SELECT * FROM tickets WHERE follow_up_at IS NOT NULL AND follow_up_done = 0"
        " AND deleted_at IS NULL AND follow_up_notified_at IS NULL"
        " AND follow_up_at <= ?", (utcnow(),)).fetchall()


def mark_notified(conn, ticket_id: int) -> None:
    conn.execute("UPDATE tickets SET follow_up_notified_at = ? WHERE id = ?",
                 (utcnow(), ticket_id))


# ---------------------------------------------------------------- per-user stats

def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def per_user_stats(conn) -> list[dict]:
    """Root dashboard figures. Response time uses the pausable clock, so hours
    spent blocked on a customer or another team don't count against anyone."""
    users = conn.execute(
        "SELECT id, username, display_name, role, is_active, last_login_at"
        " FROM users ORDER BY username COLLATE NOCASE").fetchall()

    week_ago = (_now() - timedelta(days=7)).isoformat(timespec="seconds")
    month_ago = (_now() - timedelta(days=30)).isoformat(timespec="seconds")
    out = []

    for user in users:
        if user["role"] == "root":
            continue
        rows = conn.execute(
            "SELECT * FROM tickets WHERE owner_id = ? AND deleted_at IS NULL",
            (user["id"],)).fetchall()

        first_replies, resolutions = [], []
        for t in rows:
            if t["first_reply_at"] and t["received_at"]:
                delta = (_parse(t["first_reply_at"]) - _parse(t["received_at"])).total_seconds()
                if delta >= 0:
                    first_replies.append(delta)
            if t["resolved_at"]:
                resolutions.append(t["active_seconds"])

        out.append({
            "user": user,
            "total": len(rows),
            "open": sum(1 for t in rows if t["status"] in config.OPEN_STATUSES),
            "overdue": sum(1 for t in rows if is_overdue(t)),
            "resolved_7d": sum(1 for t in rows
                               if t["resolved_at"] and t["resolved_at"] > week_ago),
            "resolved_30d": sum(1 for t in rows
                                if t["resolved_at"] and t["resolved_at"] > month_ago),
            "median_first_reply": _median(first_replies),
            "median_resolution": _median(resolutions),
            "follow_ups_due": sum(1 for t in rows if t["follow_up_at"]
                                  and not t["follow_up_done"]
                                  and t["follow_up_at"] <= utcnow()),
        })
    return out
