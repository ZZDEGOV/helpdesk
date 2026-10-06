"""Passwords, sessions, and permission checks.

No email addresses anywhere, so password recovery is root-initiated only: root
issues a temporary password and the account is flagged must_change_password. That
way root never learns anyone's working password.
"""
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config
from .db import utcnow

# scrypt parameters. n=2**14 is ~50ms per hash on a normal desktop — slow enough
# to make guessing expensive, fast enough that login doesn't feel laggy.
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 64


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt.encode("utf-8"),
                            n=_N, r=_R, p=_P, dklen=_DKLEN)
    return digest.hex(), salt


def verify_password(password: str, stored_hash: str | None, salt: str | None) -> bool:
    if not stored_hash or not salt:
        return False
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


# ---------------------------------------------------------------- capabilities
#
# Deletion is split three ways:
#   user  -- cannot delete anything. Striking through timeline entries is not
#            deletion and remains available to everyone who can work tickets.
#   admin -- queues a deletion (soft, recoverable, purged after the retention
#            window) and can restore from the trash.
#   root  -- deletes immediately and permanently, from its own admin area.
#
CAPABILITIES: dict[str, set[str]] = {
    "root": {"manage_users", "delete_users", "hard_delete", "view_trash",
             "restore", "view_tickets", "view_customers", "view_analytics"},
    "admin": {"work_tickets", "soft_delete", "view_trash", "restore",
              "view_tickets", "view_customers"},
    "user": {"work_tickets", "view_tickets", "view_customers"},
}


def can(user, capability: str) -> bool:
    if user is None:
        return False
    return capability in CAPABILITIES.get(user["role"], set())


# ---------------------------------------------------------------- users

def get_user(conn, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(conn, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                        (username.strip(),)).fetchone()


def list_users(conn, include_inactive: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM users"
    if not include_inactive:
        sql += " WHERE is_active = 1"
    return conn.execute(sql + " ORDER BY role, username COLLATE NOCASE").fetchall()


def has_root(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM users WHERE role = 'root' AND password_hash IS NOT NULL"
    ).fetchone())


def create_user(conn, *, username: str, display_name: str, password: str | None,
                role: str = "user", created_by: int | None = None,
                must_change: bool = True) -> int:
    username = username.strip()
    if not username:
        raise ValueError("username is required")
    if role not in config.ROLES:
        raise ValueError(f"unknown role: {role}")
    if get_user_by_name(conn, username):
        raise ValueError(f"username '{username}' is taken")

    pw_hash, salt = hash_password(password) if password else (None, None)
    cur = conn.execute(
        "INSERT INTO users (username, display_name, password_hash, password_salt,"
        " role, is_active, must_change_password, created_by, created_at)"
        " VALUES (?,?,?,?,?,1,?,?,?)",
        (username, display_name.strip() or username, pw_hash, salt, role,
         1 if (must_change and password) else 0, created_by, utcnow()))
    return cur.lastrowid


def set_password(conn, user_id: int, password: str, must_change: bool = False) -> None:
    pw_hash, salt = hash_password(password)
    conn.execute(
        "UPDATE users SET password_hash = ?, password_salt = ?,"
        " must_change_password = ?, failed_attempts = 0, locked_until = NULL"
        " WHERE id = ?", (pw_hash, salt, 1 if must_change else 0, user_id))


def update_user(conn, user_id: int, *, display_name: str | None = None,
                role: str | None = None, is_active: bool | None = None) -> None:
    changes: dict = {}
    if display_name is not None:
        changes["display_name"] = display_name.strip()
    if role is not None:
        if role not in config.ROLES:
            raise ValueError(f"unknown role: {role}")
        changes["role"] = role
    if is_active is not None:
        changes["is_active"] = 1 if is_active else 0
    if not changes:
        return
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE users SET {sets} WHERE id = ?", [*changes.values(), user_id])
    if changes.get("is_active") == 0:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def delete_user(conn, user_id: int, reassign_to: int | None = None) -> dict:
    """Permanently remove an account.

    tickets.owner_id has no ON DELETE clause, so ownership must be resolved
    explicitly before the row goes -- otherwise the delete is refused by the
    foreign key. Timeline entries and audit rows keep their history but lose the
    link (ON DELETE SET NULL), so the record of what happened survives even
    though the account doesn't.
    """
    user = get_user(conn, user_id)
    if not user:
        return {"tickets": 0}

    owned = conn.execute("SELECT COUNT(*) c FROM tickets WHERE owner_id = ?",
                         (user_id,)).fetchone()["c"]
    conn.execute("UPDATE tickets SET owner_id = ? WHERE owner_id = ?",
                 (reassign_to, user_id))
    conn.execute("UPDATE users SET created_by = NULL WHERE created_by = ?", (user_id,))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"tickets": owned, "username": user["username"]}


# ---------------------------------------------------------------- login

def _locked(user: sqlite3.Row) -> bool:
    if not user["locked_until"]:
        return False
    until = datetime.fromisoformat(user["locked_until"])
    if not until.tzinfo:
        until = until.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < until


def authenticate(conn, username: str, password: str) -> tuple[sqlite3.Row | None, str]:
    """Returns (user, error_message). Error text is deliberately vague about
    whether the username or the password was wrong."""
    user = get_user_by_name(conn, username)
    if not user:
        return None, "Incorrect username or password."
    if not user["is_active"]:
        return None, "That account is disabled."
    if _locked(user):
        return None, f"Too many attempts. Try again in {config.LOCKOUT_MINUTES} minutes."
    if not user["password_hash"]:
        return None, "No password has been set for that account. Ask root to set one."

    if not verify_password(password, user["password_hash"], user["password_salt"]):
        failed = (user["failed_attempts"] or 0) + 1
        locked_until = None
        if failed >= config.MAX_FAILED_LOGINS:
            locked_until = (datetime.now(timezone.utc)
                            + timedelta(minutes=config.LOCKOUT_MINUTES)
                            ).isoformat(timespec="seconds")
            failed = 0
        conn.execute("UPDATE users SET failed_attempts = ?, locked_until = ?"
                     " WHERE id = ?", (failed, locked_until, user["id"]))
        return None, "Incorrect username or password."

    conn.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL,"
                 " last_login_at = ? WHERE id = ?", (utcnow(), user["id"]))
    return get_user(conn, user["id"]), ""


# ---------------------------------------------------------------- sessions

def start_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc)
               + timedelta(days=config.SESSION_DAYS)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO sessions (token, user_id, created_at, expires_at)"
                 " VALUES (?,?,?,?)", (token, user_id, utcnow(), expires))
    return token


def session_user(conn, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token = ? AND s.expires_at > ? AND u.is_active = 1",
        (token, utcnow())).fetchone()
    return row


def end_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_sessions(conn) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (utcnow(),))


# ---------------------------------------------------------------- audit

def audit(conn, user_id: int | None, action: str, target: str = "",
          detail: str = "") -> None:
    """Separate from the ticket timeline. Root's read-only ticket views land here
    so there's a record of who looked at what."""
    conn.execute("INSERT INTO audit_log (user_id, action, target, detail, created_at)"
                 " VALUES (?,?,?,?,?)", (user_id, action, target, detail, utcnow()))


def recent_audit(conn, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON u.id = a.user_id"
        " ORDER BY a.created_at DESC, a.id DESC LIMIT ?", (limit,)).fetchall()
