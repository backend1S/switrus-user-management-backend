import os
import sys

# Configure the environment before any application module is imported.
os.environ.update({
    "SECRET_KEY": "test-secret-key-integrations-only-0123456789",
    "SESSION_SECRET": "test-session-secret-different-0123456789abcdef",
    "SESSION_COOKIE_SECURE": "false",
    "DB_ENGINE": "sqlite",
    "ADMIN_ROLES": "ADMIN",
    "UI_VIEWER_ROLES": "VIEWER",
    "LOYALTRI_SCHEDULER": "off",
    "LOYALTRI_CLIENT": "disabled",
    "APP_ENV": "test",
})
os.environ.pop("API_KEYS", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import sqlite_dev  # noqa: E402
from services.email import hash_password  # noqa: E402

API_KEY = os.environ["SECRET_KEY"]
PASSWORD = "Str0ng!Pass"            # EmpPassword (Argon2): POST /login
DEPT_PASSWORD = "Dept-Pa55word"      # DeptPassword (plaintext): POST /auth/login
_HASH = hash_password(PASSWORD)  # argon2 is slow; hash once


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.sqlite3")
    monkeypatch.setenv("SQLITE_PATH", path)
    sqlite_dev.create_schema(path)
    from services.loyaltri import config as lconfig
    lconfig.set_override(None)
    from app import auth
    auth._failures.clear()  # sign-in throttle is per process
    yield path
    lconfig.set_override(None)


def add_user(emp_id, name, role="ADMIN", disabled=0, dept=5, branch="BR01", email=None,
             phone=None, manager=None, leave=(None, None), password_hash=_HASH,
             dept_password=DEPT_PASSWORD):
    import sqlite3
    conn = sqlite3.connect(os.environ["SQLITE_PATH"])
    conn.execute(
        "INSERT INTO SW_USER (EMPID, EmpName, EmpPassword, Email, DepartmentID, Roles, BRANCH_ID, "
        "Phone, MANAGER_ID, IsDisabled, suspend_start, suspend_end, DeptPassword) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (emp_id, name, password_hash, email or f"{emp_id.lower()}@switrus.com", dept, role, branch,
         phone, manager, disabled, leave[0], leave[1], dept_password),
    )
    conn.commit()
    conn.close()


def query(sql, params=()):
    import sqlite3
    conn = sqlite3.connect(os.environ["SQLITE_PATH"])
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


@pytest.fixture
def client():
    from main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def api(client):
    """Client authenticated as a trusted integration (X-API-Key)."""
    client.headers.update({"X-API-Key": API_KEY})
    return client


def browser_login(client, emp_id):
    """Give the client a browser session for emp_id without going through /auth/login."""
    from core import config
    from core.security import issue_session
    token, csrf, _ = issue_session(emp_id)
    client.cookies.set(config.SESSION_COOKIE_NAME, token, domain="testserver.local")
    client.headers.update({"X-CSRF-Token": csrf})
    return client


@pytest.fixture
def admin(client):
    add_user("E9000", "root admin", role="ADMIN")
    return browser_login(client, "E9000")
