"""Admin sync endpoints, the worker loop and overlap protection."""
import json
import threading

from conftest import add_user, query
from services.loyaltri import config as lconfig
from services.loyaltri.config import MOCK_FIELD_MAP, LoyaltriSettings
from services.loyaltri.loyaltri_scheduler import SyncWorker
from services.loyaltri.loyaltri_sync import SyncStore


def use(**kw):
    s = LoyaltriSettings(**{"client_mode": "mock", "field_map": dict(MOCK_FIELD_MAP), **kw})
    lconfig.set_override(s)
    return s


def mock_file(tmp_path, records):
    p = tmp_path / "emps.json"
    p.write_text(json.dumps(records))
    return str(p)


def test_status_when_not_configured(admin):
    body = admin.get("/integrations/loyaltri/status").json()
    assert body["configured"] is False
    assert any("disabled" in p for p in body["problems"])
    assert body["config"]["credentials_set"] is False


def test_status_never_returns_the_credential(admin):
    use(client_mode="http", base_url="https://hr.example.test", employees_path="/e",
        auth_header="X-Auth", api_key="top-secret-value")
    text = admin.get("/integrations/loyaltri/status").text
    assert "top-secret-value" not in text and '"credentials_set":true' in text


def test_manual_sync_rejected_when_not_configured(admin):
    r = admin.post("/integrations/loyaltri/sync")
    assert r.status_code == 409 and "not configured" in r.json()["detail"]


def test_manual_sync_queues_and_worker_runs_it(admin, tmp_path):
    add_user("E1001", "Asha Nair", phone="9876543210")
    use(mock_file=mock_file(tmp_path, [{"employee_code": "E1001", "mobile": "9111111111"}]))

    r = admin.post("/integrations/loyaltri/sync")
    assert r.status_code == 202 and r.json()["run"]["status"] == "queued"
    run_id = r.json()["run"]["run_id"]

    # Second click while queued returns the same run instead of queuing another.
    again = admin.post("/integrations/loyaltri/sync")
    assert again.json()["already_active"] is True and again.json()["run"]["run_id"] == run_id

    assert SyncWorker().tick() == run_id
    run = admin.get(f"/integrations/loyaltri/runs/{run_id}").json()
    assert run["status"] == "succeeded" and run["updated"] == 1
    assert run["items"][0]["emp_id"] == "E1001"
    assert run["trigger_type"] == "manual" and run["requested_by"] == "E9000"

    status = admin.get("/integrations/loyaltri/status").json()
    assert status["configured"] is True and status["worker"]["alive"] is True
    assert status["last_success"]["run_id"] == run_id and status["active_run"] is None
    assert [x["run_id"] for x in admin.get("/integrations/loyaltri/runs").json()] == [run_id]


def test_failed_sync_is_visible_in_status(admin):
    use(mock_failure="outage")
    run_id = admin.post("/integrations/loyaltri/sync").json()["run"]["run_id"]
    SyncWorker().tick()
    status = admin.get("/integrations/loyaltri/status").json()
    assert status["last_attempt"]["run_id"] == run_id
    assert status["last_attempt"]["status"] == "failed"
    assert "unreachable" in status["last_attempt"]["error_message"]
    assert status["last_success"] is None


def test_scheduled_sync_respects_interval():
    use(sync_enabled=True, interval_minutes=60)
    worker = SyncWorker()
    first = worker.tick()
    assert first is not None
    assert worker.tick() is None          # not due again yet
    assert query("SELECT trigger_type FROM LOYALTRI_SYNC_RUN") == [("schedule",)]


def test_no_scheduled_sync_when_disabled():
    use(sync_enabled=False)
    assert SyncWorker().tick() is None


def test_lease_prevents_overlapping_runs():
    store = SyncStore()
    assert store.acquire_lease("worker-a", 600) is True
    assert store.acquire_lease("worker-b", 600) is False
    store.release_lease("worker-a")
    assert store.acquire_lease("worker-b", 600) is True


def test_concurrent_workers_run_one_sync(tmp_path):
    use(mock_file=mock_file(tmp_path, [{"employee_code": "E1"}]))
    SyncStore().create_run("manual", "x")
    results = []
    barrier = threading.Barrier(4)

    def go(i):
        barrier.wait()
        results.append(SyncWorker(owner=f"w{i}").tick())

    threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([r for r in results if r]) == 1
    assert query("SELECT COUNT(*) FROM LOYALTRI_SYNC_RUN")[0][0] == 1


def test_stale_running_run_is_closed():
    store = SyncStore()
    stale = store.create_run("manual", "x")
    store.mark_running(stale)              # worker died here
    use()
    SyncWorker().tick()
    assert store.get_run(stale)["status"] == "failed"


def test_worker_survives_missing_tables(db):
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE LOYALTRI_SYNC_RUN")
    conn.commit(); conn.close()
    use(sync_enabled=True)
    stop = threading.Event()
    t = threading.Thread(target=SyncWorker().run_forever, args=(stop,))
    t.start()
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_status_reports_missing_tables(admin, db):
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE LOYALTRI_SYNC_RUN")
    conn.commit(); conn.close()
    body = admin.get("/integrations/loyaltri/status").json()
    assert body["tables_ready"] is False and any("migrations" in p for p in body["problems"])
    assert admin.get("/users").status_code == 200
