"""Sync policy: matching, ownership, local-edit protection, idempotency, failures."""
import pytest

from conftest import add_user, query
from services.loyaltri.config import MOCK_FIELD_MAP, LoyaltriSettings
from services.loyaltri.loyaltri_client import LoyaltriClient, LoyaltriUnavailable
from services.loyaltri.loyaltri_service import LoyaltriSyncService
from services.loyaltri.loyaltri_sync import SyncStore


class FakeClient(LoyaltriClient):
    def __init__(self, records=None, error=None, pages=None):
        self.pages = pages if pages is not None else [records or []]
        self.error = error
        self.calls = 0

    def iter_pages(self):
        self.calls += 1
        for p in self.pages:
            yield p
        if self.error:
            raise self.error


def rec(code, name=None, email=None, mobile=None, dept=None, branch=None, manager=None, rid=None):
    return {"id": rid or f"LY-{code}", "employee_code": code, "full_name": name, "work_email": email,
            "mobile": mobile, "department_code": dept, "branch_code": branch,
            "reporting_manager_code": manager, "status": "active"}


def settings(**kw):
    base = dict(client_mode="mock", field_map=dict(MOCK_FIELD_MAP))
    base.update(kw)
    return LoyaltriSettings(**base)


def run(client, s=None):
    store = SyncStore()
    run_id = store.create_run("manual", "tester")
    result = LoyaltriSyncService(s or settings(), store, client=client).run(run_id)
    result["items"] = store.get_items(run_id)
    return result


def user(emp_id):
    cols = "EmpName, Email, Phone, DepartmentID, BRANCH_ID, MANAGER_ID, Roles, IsDisabled, EmpPassword, suspend_start"
    rows = query(f"SELECT {cols} FROM SW_USER WHERE EMPID = ?", (emp_id,))
    return dict(zip([c.strip() for c in cols.split(",")], rows[0])) if rows else None


@pytest.fixture
def local():
    add_user("E1000", "Ravi Menon", role="ADMIN", phone="9800000001")
    add_user("E1001", "Asha Nair", role="ADMIN", phone="9876543210", email="asha@switrus.com",
             manager=None, leave=("2026-11-03", "2026-11-07"))
    add_user("E1003", "Not In Loyaltri", role="SALES")


def test_updates_owned_fields_only(local):
    before = user("E1001")
    r = run(FakeClient([rec("E1001", name="Asha K Nair", email="new@switrus.com", mobile="9111111111",
                            manager="E1000", dept="9", branch="BR77")]))
    assert r["status"] == "succeeded" and r["updated"] == 1
    after = user("E1001")
    assert after["Phone"] == "9111111111" and after["MANAGER_ID"] == "E1000"       # owned
    assert after["Email"] == "asha@switrus.com"                                      # fill-empty, local set
    assert after["DepartmentID"] == before["DepartmentID"] and after["BRANCH_ID"] == "BR01"
    assert after["EmpName"] == "Asha Nair"                                           # not synced by default
    for protected in ("Roles", "IsDisabled", "EmpPassword", "suspend_start"):
        assert after[protected] == before[protected]
    assert query("SELECT actor_type, action FROM SW_USER_AUDIT WHERE target_emp_id='E1001'") == \
        [("loyaltri-sync", "user.update")]


def test_fill_empty_fields_fill_blanks():
    add_user("E5", "Blank Branch", branch=None)
    run(FakeClient([rec("E5", branch="BR05")]))
    assert user("E5")["BRANCH_ID"] == "BR05"


def test_second_identical_run_is_a_no_op(local):
    records = [rec("E1001", mobile="9111111111", manager="E1000"), rec("E1000", mobile="9800000001")]
    first = run(FakeClient(records))
    audits = query("SELECT COUNT(*) FROM SW_USER_AUDIT")[0][0]
    links = query("SELECT emp_id, synced_values, last_synced_at FROM LOYALTRI_EMPLOYEE_LINK ORDER BY emp_id")
    second = run(FakeClient(records))
    assert first["updated"] == 1 and second["updated"] == 0 and second["unchanged"] == 2
    assert query("SELECT COUNT(*) FROM SW_USER_AUDIT")[0][0] == audits
    assert query("SELECT emp_id, synced_values, last_synced_at FROM LOYALTRI_EMPLOYEE_LINK ORDER BY emp_id") == links


def test_local_admin_edit_is_not_overwritten(local, api):
    run(FakeClient([rec("E1001", mobile="9111111111")]))
    api.patch("/users/E1001", json={"Phone": "9222222222"})     # admin changes it in Switrus

    # Loyaltri unchanged -> the local edit stands silently
    r = run(FakeClient([rec("E1001", mobile="9111111111")]))
    assert user("E1001")["Phone"] == "9222222222" and r["conflicts"] == 0

    # Loyaltri changes too -> conflict reported, local value kept
    r = run(FakeClient([rec("E1001", mobile="9333333333")]))
    assert user("E1001")["Phone"] == "9222222222"
    assert r["conflicts"] == 1 and r["items"][0]["outcome"] == "conflict"
    assert r["items"][0]["fields"] == ["phone"]


def test_overwrite_flag_allows_loyaltri_to_win(local, api):
    run(FakeClient([rec("E1001", mobile="9111111111")]))
    api.patch("/users/E1001", json={"Phone": "9222222222"})
    run(FakeClient([rec("E1001", mobile="9333333333")]), settings(overwrite_local_changes=True))
    assert user("E1001")["Phone"] == "9333333333"


def test_blank_loyaltri_value_never_clears_local(local):
    run(FakeClient([rec("E1001", mobile=None, manager=None)]))
    assert user("E1001")["Phone"] == "9876543210"


def test_missing_from_loyaltri_is_untouched(local):
    before = user("E1003")
    r = run(FakeClient([rec("E1001")]))
    assert user("E1003") == before and r["status"] == "succeeded"


def test_new_employee_reported_not_created_by_default(local):
    r = run(FakeClient([rec("E7000", name="Brand New", email="bn@switrus.com", dept="5")]))
    assert user("E7000") is None
    assert r["skipped"] == 1 and r["items"][0]["outcome"] == "new"


def test_create_mode_disabled_creates_disabled_user_once(local):
    s = settings(create_mode="disabled", default_role="SALES")
    records = [rec("E7000", name="Brand New", email="bn@switrus.com", dept="5", branch="BR02", manager="E1000")]
    r = run(FakeClient(records), s)
    u = user("E7000")
    assert r["created"] == 1
    assert u["IsDisabled"] == 1 and u["Roles"] == "SALES" and u["MANAGER_ID"] == "E1000"
    assert u["EmpPassword"] and u["EmpPassword"].startswith("$argon2")
    again = run(FakeClient(records), s)
    assert again["created"] == 0 and query("SELECT COUNT(*) FROM SW_USER WHERE EMPID='E7000'")[0][0] == 1


def test_create_mode_name_collision_is_conflict(local):
    s = settings(create_mode="disabled", default_role="SALES")
    r = run(FakeClient([rec("E7000", name="Asha Nair", email="x@switrus.com", dept="5")]), s)
    assert r["conflicts"] == 1 and user("E7000") is None


def test_create_mode_requires_role():
    assert any("DEFAULT_ROLE" in p for p in settings(create_mode="disabled").problems())


def test_duplicate_ids_in_source_are_rejected(local):
    r = run(FakeClient([rec("E1001", mobile="9111111111"), rec("e1001", mobile="9222222222")]))
    assert r["failed"] == 2 and user("E1001")["Phone"] == "9876543210"


def test_bad_records_fail_individually(local):
    r = run(FakeClient([{"full_name": "no code"}, "garbage", rec("E1001", mobile="9111111111")]))
    assert r["status"] == "partial" and r["failed"] == 2 and r["updated"] == 1
    assert user("E1001")["Phone"] == "9111111111"


def test_unknown_manager_is_ignored_not_written(local):
    r = run(FakeClient([rec("E1001", manager="E4040")]))
    assert user("E1001")["MANAGER_ID"] is None
    assert "not a Switrus user" in r["items"][0]["message"]


def test_outage_mid_pagination_writes_nothing(local):
    client = FakeClient(pages=[[rec("E1001", mobile="9111111111")]],
                        error=LoyaltriUnavailable("Loyaltri is unreachable: timed out"))
    r = run(client)
    assert r["status"] == "failed" and "unreachable" in r["error_message"]
    assert user("E1001")["Phone"] == "9876543210"
    assert query("SELECT COUNT(*) FROM LOYALTRI_EMPLOYEE_LINK")[0][0] == 0


def test_database_failure_on_one_record_does_not_stop_others(local, monkeypatch):
    svc_update = LoyaltriSyncService._update_user

    def flaky(self, emp_id, changes):
        if emp_id == "E1000":
            raise RuntimeError("deadlock victim")
        return svc_update(self, emp_id, changes)

    monkeypatch.setattr(LoyaltriSyncService, "_update_user", flaky)
    r = run(FakeClient([rec("E1000", mobile="9000000000"), rec("E1001", mobile="9111111111")]))
    assert r["status"] == "partial" and r["failed"] == 1 and r["updated"] == 1
    assert user("E1001")["Phone"] == "9111111111"


def test_empty_response_changes_nothing(local):
    r = run(FakeClient([]))
    assert r["status"] == "succeeded" and "no employees" in r["error_message"]
