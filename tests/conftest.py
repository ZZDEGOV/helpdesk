"""Point the app at a throwaway database before anything imports app.config.

config resolves DB_PATH once, at import time, and the fixtures in this suite
DELETE FROM every table. If any test module imports `app` before the path is
redirected, the suite wipes the real database. pytest loads conftest.py before
collecting test modules, so this is the one place the redirect is guaranteed
to happen first.
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="helpdesk-tests-"))
os.environ["HELPDESK_DATA_DIR"] = str(_TMP)
os.environ["HELPDESK_DB_PATH"] = str(_TMP / "test.db")

from app import config  # noqa: E402

_REAL_DATA = (config.BASE_DIR / "data").resolve()
if config.DB_PATH.resolve().is_relative_to(_REAL_DATA) or \
        config.DATA_DIR.resolve() == _REAL_DATA:
    raise RuntimeError(f"Refusing to run tests against the real database: {config.DB_PATH}")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402
from app.migrate import run as migrate  # noqa: E402

NR = {"follow_redirects": False}


def sign_in(username: str, password: str = "temp12345") -> TestClient:
    """A client signed in as an account root just created (temporary password),
    past the forced password change."""
    client = TestClient(app)
    client.post("/login", data={"username": username, "password": password}, **NR)
    client.post("/change-password", data={"password": "realpass123",
                                          "confirm": "realpass123"}, **NR)
    return client


def create_account(root: TestClient, username: str, role: str) -> TestClient:
    root.post("/root/users/create", data={"username": username, "display_name": username,
                                          "password": "temp12345", "role": role}, **NR)
    return sign_in(username)


def new_ticket(client: TestClient, subject: str = "Test", **extra) -> str:
    data = {"subject": subject, "priority": "3", **extra}
    return client.post("/tickets/new", data=data, **NR).headers["location"].split("/")[-1]


@pytest.fixture
def env() -> dict[str, TestClient]:
    """A fresh database with root, an admin (ZachZ) and a user (mreed) signed in."""
    migrate(verbose=False)
    with connect() as conn:
        for table in ("sessions", "audit_log", "events", "tickets", "customers", "users"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

    root = TestClient(app)
    root.post("/setup", data={"username": "root", "password": "rootpass123",
                              "confirm": "rootpass123"}, **NR)
    return {"root": root,
            "admin": create_account(root, "ZachZ", "admin"),
            "user": create_account(root, "mreed", "user")}
