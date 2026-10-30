"""CRITICAL: Switrus keeps working when Loyaltri is completely unavailable.

A tripwire counts every attempt to reach Loyaltri. User-management operations
must succeed and must never touch it; only the sync worker does, and its
failure is contained in the sync log.
"""
import threading
import time
from datetime import date

import httpx
import pytest

from conftest import PASSWORD, add_user, browser_login, query
from services.loyaltri import config as lconfig
from services.loyaltri import loyaltri_scheduler
from services.loyaltri import loyaltri_service
from services.loyaltri.config import LoyaltriSettings
from services.loyaltri.loyaltri_client import LoyaltriClient, LoyaltriUnavailable
from services.loyaltri.loyaltri_scheduler import SyncWorker

DOWN = LoyaltriSettings(
    client_mode="http", base_url="https://loyaltri.unreachable.test", employees_path="/employees",
    auth_header="Authorization", auth_scheme="Bearer", api_key="k",
    field_map={"emp_id": "code", "phone": "mobile"},
    sync_enabled=True, interval_minutes=5, max_retries=1, retry_backoff_seconds=0, timeout_seconds=1,
)


@pytest.fixture
def loyaltri_down(monkeypatch):
    """Every network call to Loyaltri fails, and is counted."""
    calls = []

    def refuse(self, request):
        calls.append(str(request.url))
        raise httpx.ConnectError("connection refused", request=request)

    # The real network transport (TestClient uses its own in-process transport).
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    lconfig.set_override(DOWN)
    return calls


def exercise_user_management(c):
    """Every Switrus user-management feature, end to end. Returns nothing; asserts."""
    assert c.get("/health").status_code == 200
    assert c.get("/health/ready").status_code == 200

    r = c.post("/users", json={"EmpID": "E3001", "EmpName": "New Joiner", "EmpPassword": "Str0ng!Pass",
                               "Email": "new.joiner@switrus.com", "DepartmentID": "5", "Roles": "SALES"})
    assert r.status_code == 201, r.text

    users = c.get("/users", params={"include_disabled": "true", "search": "joiner"}).json()
    assert [u["emp_id"] for u in users] == ["E3001"]
    assert c.get("/users/stats").status_code == 200
    assert c.get("/users/E3001").json()["emp_name"] == "New Joiner"

    assert c.patch("/users/E3001", json={"Phone": "9000000001", "BRANCH_ID": "BR09"}).json()["branch_id"] == "BR09"

    assert c.patch("/users/E3001/status", json={"is_disabled": True}).json()["is_disabled"] is True
    assert c.patch("/users/E3001/status", json={"is_disabled": False}).json()["is_disabled"] is False

    today = date.today().isoformat()
    assert c.put("/users/E3001/leave", json={"start_date": today, "end_date": today}).json()["is_suspended"] == 1
    assert c.delete("/users/E3001/leave").json()["leave_start"] is None

    # Existing integration endpoints too
    assert c.get("/users/by-empid", params={"emp_id": "E3001"}).status_code == 200


def test_everything_works_while_loyaltri_is_down(client, loyaltri_down):
    add_user("E9000", "root admin", role="ADMIN")

    # Integration login (/login)
    r = client.post("/login", json={"username": "root admin", "password": PASSWORD},
                    headers={"X-API-Key": "test-secret-key-integrations-only-0123456789"})
    assert r.status_code == 200
    browser_login(client, "E9000")

    exercise_user_management(client)
    assert loyaltri_down == [], "a user-management request tried to reach Loyaltri"

    # The admin can still open the sync page and queue a sync; nothing blocks.
    assert client.get("/integrations/loyaltri/status").json()["configured"] is True
    run_id = client.post("/integrations/loyaltri/sync").json()["run"]["run_id"]
    assert loyaltri_down == []

    # The worker tries, fails, and records it.
    before = query("SELECT * FROM SW_USER ORDER BY EMPID")
    assert SyncWorker().tick() == run_id
    assert len(loyaltri_down) == 2                       # 1 try + 1 bounded retry
    run = client.get(f"/integrations/loyaltri/runs/{run_id}").json()
    assert run["status"] == "failed" and "unreachable" in run["error_message"]
    assert query("SELECT * FROM SW_USER ORDER BY EMPID") == before   # nothing touched

    # And Switrus still works after the failed sync.
    client.delete("/users/E3001/leave")
    import sqlite3, os
    conn = sqlite3.connect(os.environ["SQLITE_PATH"])
    conn.execute("DELETE FROM SW_USER WHERE EMPID = 'E3001'")
    conn.commit(); conn.close()
    exercise_user_management(client)
    status = client.get("/integrations/loyaltri/status").json()
    assert status["last_attempt"]["status"] == "failed" and status["last_success"] is None


class HangingClient(LoyaltriClient):
    """Simulates Loyaltri hanging until its timeout."""

    def __init__(self, started: threading.Event, release: threading.Event):
        self.started, self.release = started, release

    def iter_pages(self):
        self.started.set()
        self.release.wait(10)
        raise LoyaltriUnavailable("Loyaltri is unreachable: timed out after 30s")
        yield  # pragma: no cover


def test_hanging_sync_does_not_block_the_api(admin, monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(loyaltri_service, "build_client", lambda s: HangingClient(started, release))
    lconfig.set_override(LoyaltriSettings(client_mode="mock", field_map={"emp_id": "code"},
                                          scheduler_mode="embedded", poll_seconds=2))
    assert loyaltri_scheduler.start_embedded()
    try:
        run_id = admin.post("/integrations/loyaltri/sync").json()["run"]["run_id"]
        assert started.wait(5), "embedded worker did not pick up the run"

        t0 = time.monotonic()
        exercise_user_management(admin)
        assert time.monotonic() - t0 < 5, "user management slowed down while a sync was hanging"
        assert admin.get("/integrations/loyaltri/status").json()["active_run"]["status"] == "running"
    finally:
        release.set()
        loyaltri_scheduler.stop_embedded()
        if loyaltri_scheduler._thread:
            loyaltri_scheduler._thread.join(5)

    run = admin.get(f"/integrations/loyaltri/runs/{run_id}").json()
    assert run["status"] == "failed"


def test_no_request_handler_imports_a_loyaltri_client():
    """Structural guard: only the sync service builds Loyaltri clients."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in list((root / "app").glob("*.py")) + [root / "main.py"]:
        text = path.read_text(encoding="utf-8")
        if "loyaltri_client" in text or "build_client" in text:
            offenders.append(path.name)
    assert offenders == []
