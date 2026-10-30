"""Attendance Muster: access, scope, mapping, parsing, failures and data protection.

Upstream payloads here are fixtures in the documented shape; no real
attendance data and no real endpoint or key are used.
"""
import json
import logging

import httpx
import pytest

from conftest import API_KEY, add_user, browser_login, query
from services.attendance import client as aclient
from services.attendance import config as aconfig
from services.attendance import service
from services.attendance.config import AttendanceSettings
from services.attendance.parser import categorize, is_sensitive_key, parse_minutes

FAKE_KEY = "attendance-test-key-must-never-leak-0123"
FROM, TO = "2026-10-01", "2026-10-03"


def settings(**over):
    base = dict(client_mode="http", api_url="https://attendance.example.invalid/muster", api_key=FAKE_KEY,
                mapping_verified=True, admin_scope="matched", cache_seconds=0, max_retries=0)
    base.update(over)
    return AttendanceSettings(**base)


def record(code, days, employee_id=1, summary=None, **details):
    return {
        "employeeId": employee_id,
        "employeeDetails": {"employeeCode": code, "employeeName": f"Upstream {code}",
                            "grossSalary": 98765, "bankAccountNumber": "123456789012", **details},
        "attendanceDetails": days,
        "attendanceSummary": summary if summary is not None else {"presentDays": 1, "grossSalary": 98765, "netPay": 91234},
    }


def muster(*records, from_date=FROM, to_date=TO, success=True):
    return {"success": success, "data": {"companyId": 7, "fromDate": from_date, "toDate": to_date,
                                         "attendance": list(records)}}


PRESENT = {"status": "Present", "inTime": "09:00", "outTime": "18:00", "workHours": "08:30", "overtime": "01:15"}


class Upstream:
    """Fake attendance service behind httpx.MockTransport; records every request."""

    def __init__(self, monkeypatch, body=None, status=200, raise_exc=None, raw=None):
        self.requests = []

        def handler(request: httpx.Request):
            self.requests.append(request)
            if raise_exc:
                raise raise_exc
            if raw is not None:
                return httpx.Response(status, content=raw)
            return httpx.Response(status, json=body)

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(service, "build_client",
                            lambda s: aclient.HttpAttendanceClient(s, transport=transport, sleep=lambda _: None))


@pytest.fixture(autouse=True)
def reset():
    aconfig.set_override(settings())
    service.clear_cache()
    yield
    aconfig.set_override(None)
    service.clear_cache()


@pytest.fixture
def org():
    add_user("E9000", "Root Admin", role="ADMIN", dept="Operations", branch="B001")
    add_user("SHPL/AMD/0383/2025", "HEMANG C KANANI", role="Manager", dept="SALES", branch="B008")
    add_user("TEMP/367", "Branch Boss", role="BRANCHMANAGER", dept="SALES", branch="B008")
    add_user("SHPL/AMD/0001/2025", "Sales One", role="CRE", dept="SALES", branch="B008")
    add_user("SHPL/AMD/0010/2025", "Visa Person", role="Executive", dept="VISA", branch="B008")
    add_user("SHPL/DEL/0001/2025", "Delhi Sales", role="CRE", dept="SALES", branch="B003")
    add_user("V1", "Viewer Person", role="VIEWER", dept="SALES", branch="B008")


def org_muster():
    days = {"2026-10-01": PRESENT, "2026-10-02": {"status": "Absent"}, "2026-10-03": {"status": "Off Day"}}
    return muster(
        record("SHPL/AMD/0001/2025", days, employee_id=11),
        record("SHPL/AMD/0010/2025", days, employee_id=12),
        record("SHPL/DEL/0001/2025", days, employee_id=13),
        record("NOT-IN-SWITRUS", days, employee_id=14),
    )


def get(client, params="from_date=%s&to_date=%s" % (FROM, TO)):
    return client.get("/attendance/muster?" + params)


def ids(resp):
    return sorted(e["emp_id"] for e in resp.json()["employees"])


# ------------------------------------------------------------------ dates

def test_valid_range_returns_every_day_and_maps_details_by_date(client, org, monkeypatch):
    up = Upstream(monkeypatch, org_muster())
    r = get(browser_login(client, "E9000"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dates"] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    one = next(e for e in body["employees"] if e["emp_id"] == "SHPL/AMD/0001/2025")
    assert one["days"]["2026-10-01"] == {"status": "Present", "category": "present", "in_time": "09:00",
                                         "out_time": "18:00", "work_minutes": 510, "overtime_minutes": 75}
    assert one["days"]["2026-10-02"]["category"] == "absent"
    assert one["days"]["2026-10-03"]["category"] == "off_day"
    # Upstream got exactly the requested range, with the key in the header and not in the URL.
    req = up.requests[0]
    assert dict(req.url.params) == {"fromDate": FROM, "toDate": TO}
    assert req.headers["x-api-key"] == FAKE_KEY
    assert FAKE_KEY not in str(req.url)


@pytest.mark.parametrize("params,status", [
    ("to_date=2026-10-03", 422),                          # missing from_date
    ("from_date=2026-10-01", 422),                        # missing to_date
    ("from_date=01-10-2026&to_date=2026-10-03", 400),     # wrong format
    ("from_date=2026-02-30&to_date=2026-03-01", 400),     # not a real date
    ("from_date=1700000000&to_date=2026-10-03", 400),     # timestamps are not accepted
    ("from_date=2026-10-05&to_date=2026-10-01", 400),     # reversed
    ("from_date=2026-01-01&to_date=2026-03-31", 400),     # longer than ATTENDANCE_MAX_RANGE_DAYS
])
def test_missing_or_invalid_dates_are_rejected_before_calling_upstream(client, org, monkeypatch, params, status):
    up = Upstream(monkeypatch, org_muster())
    r = get(browser_login(client, "E9000"), params)
    assert r.status_code == status, r.text
    assert up.requests == []


# ------------------------------------------------------------------ upstream failures

@pytest.mark.parametrize("kwargs,status,text", [
    (dict(status=500, body={"error": "boom"}), 503, "unavailable"),
    (dict(status=429, body={}), 503, "unavailable"),
    (dict(raise_exc=httpx.ConnectTimeout("t")), 504, "did not answer"),
    (dict(raise_exc=httpx.ConnectError("c")), 503, "cannot be reached"),
    (dict(status=401, body={"message": "bad key " + FAKE_KEY}), 502, "rejected"),
    (dict(status=403, body={}), 502, "rejected"),
    (dict(status=404, body={}), 502, "refused"),
    (dict(raw=b"<html>gateway</html>"), 502, "not JSON"),
    (dict(body={"success": False, "message": "internal detail"}), 502, "could not produce"),
    (dict(body={"success": True, "data": {"attendance": {"x": 1}}}), 502, "unexpected format"),
    (dict(body=["not", "an", "object"]), 502, "unexpected response"),
])
def test_upstream_failures_become_readable_errors_without_secrets(client, org, monkeypatch, caplog, kwargs, status, text):
    caplog.set_level(logging.DEBUG)
    Upstream(monkeypatch, **kwargs)
    r = get(browser_login(client, "E9000"))
    assert r.status_code == status
    assert text in r.json()["detail"]
    assert FAKE_KEY not in r.text and "internal detail" not in r.text and "attendance.example" not in r.text
    assert FAKE_KEY not in caplog.text


def test_retries_once_on_a_server_error_then_succeeds(client, org, monkeypatch):
    aconfig.set_override(settings(max_retries=1))
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=org_muster())

    monkeypatch.setattr(service, "build_client", lambda s: aclient.HttpAttendanceClient(
        s, transport=httpx.MockTransport(handler), sleep=lambda _: None))
    assert get(browser_login(client, "E9000")).status_code == 200
    assert len(calls) == 2


# ------------------------------------------------------------------ configuration

def test_missing_configuration_is_reported_to_admins_only(client, org):
    aconfig.set_override(settings(api_url="", api_key=""))
    r = get(browser_login(client, "E9000"))
    assert r.status_code == 503
    assert "ATTENDANCE_API_URL is not set" in r.json()["detail"]
    assert "ATTENDANCE_API_KEY is not set" in r.json()["detail"]

    client.cookies.clear()
    r = get(browser_login(client, "SHPL/AMD/0383/2025"))
    assert r.status_code == 503
    assert r.json()["detail"] == "Attendance is not configured yet."


def test_disabled_by_default_and_insecure_url_refused():
    assert AttendanceSettings.from_env().client_mode == "disabled"
    assert AttendanceSettings.from_env().admin_scope == "none"
    assert AttendanceSettings.from_env().mapping_verified is False
    assert "must use https://" in " ".join(settings(api_url="http://attendance.example.invalid").problems())
    assert "query string" in " ".join(settings(api_url="https://a.example.invalid/m?key=x").problems())


def test_access_endpoint_explains_state_without_secrets(client, org):
    r = browser_login(client, "E9000").get("/attendance/access")
    assert r.status_code == 200
    assert r.json()["allowed"] is True
    assert r.json()["config"]["credentials_set"] is True
    assert FAKE_KEY not in r.text and "attendance.example" not in r.text

    aconfig.set_override(settings(mapping_verified=False))
    client.cookies.clear()
    r = browser_login(client, "SHPL/AMD/0383/2025").get("/attendance/access")
    assert r.json()["allowed"] is False
    assert "not been confirmed" in r.json()["reason"]
    assert "config" not in r.json() and "problems" not in r.json()


# ------------------------------------------------------------------ states and summaries

def test_unexpected_states_are_kept_as_returned_and_counted_as_other(client, org, monkeypatch):
    days = {"2026-10-01": {"status": "Work From Home"}, "2026-10-02": "Casual Leave", "2026-10-03": {"status": "HD"}}
    Upstream(monkeypatch, muster(record("SHPL/AMD/0001/2025", days)))
    body = get(browser_login(client, "E9000")).json()
    e = body["employees"][0]
    assert e["days"]["2026-10-01"] == {"status": "Work From Home", "category": "other", "in_time": None,
                                       "out_time": None, "work_minutes": None, "overtime_minutes": None}
    assert e["days"]["2026-10-02"]["category"] == "leave"
    assert e["days"]["2026-10-03"]["category"] == "half_day"
    assert {s["label"]: s["category"] for s in body["states"]} == {
        "Work From Home": "other", "Casual Leave": "leave", "HD": "half_day"}


def test_date_keys_in_other_formats_and_out_of_range_entries(client, org, monkeypatch):
    days = {"01-10-2026": PRESENT, "2026-10-02T00:00:00": {"status": "Absent"},
            "2026-09-30": PRESENT, "next tuesday": PRESENT, "2026-10-03": {"no_status": True}}
    Upstream(monkeypatch, muster(record("SHPL/AMD/0001/2025", days)))
    body = get(browser_login(client, "E9000")).json()
    e = body["employees"][0]
    assert sorted(e["days"]) == ["2026-10-01", "2026-10-02"]
    assert body["mapping"]["out_of_range_dates"] == 1
    assert any("2 day entries could not be read" in w for w in body["warnings"])


def test_summary_totals_match_the_daily_register(client, org, monkeypatch):
    Upstream(monkeypatch, org_muster())
    body = get(browser_login(client, "E9000")).json()
    one = body["employees"][0]["summary"]
    assert one == {"present": 1, "absent": 1, "off_day": 1, "half_day": 0, "leave": 0, "other": 0,
                   "days_recorded": 3, "work_minutes": 510, "overtime_minutes": 75}
    totals = body["totals"]
    assert totals["employees"] == 3  # the unmatched record is not shown to a "matched" admin
    assert (totals["present"], totals["absent"], totals["off_day"]) == (3, 3, 3)
    assert totals["work_minutes"] == 3 * 510 and totals["overtime_minutes"] == 3 * 75


def test_parsing_helpers():
    assert parse_minutes("08:30") == 510 and parse_minutes(8.5) == 510 and parse_minutes("7.25") == 435
    assert parse_minutes("soon") is None and parse_minutes(True) is None and parse_minutes(99) is None
    assert categorize("P") == "present" and categorize("Weekly Off") == "off_day"
    assert categorize("Sick Leave") == "leave" and categorize("LOP") == "leave" and categorize("Half Day") == "half_day"
    assert categorize("Holiday") == "off_day" and categorize("Training") == "other"
    assert is_sensitive_key("grossSalary") and is_sensitive_key("net_pay") and is_sensitive_key("PFAmount")
    assert is_sensitive_key("ESICDeduction") and is_sensitive_key("bank_account_no") and is_sensitive_key("CTC")
    for safe in ("companyId", "presentDays", "absentDays", "totalWorkHours", "overtimeHours", "paidLeaveDays"):
        assert not is_sensitive_key(safe), safe


# ------------------------------------------------------------------ employee mapping

def test_mapping_uses_employee_code_never_the_numeric_employee_id(client, org, monkeypatch):
    add_user("5000", "Numeric Person", role="CRE", dept="SALES", branch="B008")
    # employeeId 5000 equals an EMPID, but the code does not: it must not be attributed to "5000".
    Upstream(monkeypatch, muster(record("UNKNOWN-CODE", {"2026-10-01": PRESENT}, employee_id=5000)))
    body = get(browser_login(client, "E9000")).json()
    assert body["employees"] == []
    assert body["mapping"]["unmatched"] == 1 and body["mapping"]["matched"] == 0


def test_duplicate_and_missing_codes_are_never_attributed(client, org, monkeypatch):
    aconfig.set_override(settings(admin_scope="all"))
    d = {"2026-10-01": PRESENT}
    Upstream(monkeypatch, muster(
        record("SHPL/AMD/0001/2025", d), record("shpl/amd/0001/2025 ", d),  # same code twice (case/space)
        record(None, d), record("SHPL/AMD/0010/2025", d)))
    body = get(browser_login(client, "E9000")).json()
    m = body["mapping"]
    assert (m["duplicate_code_records"], m["missing_code"], m["matched"]) == (2, 1, 1)
    shown = [(e["emp_id"], e["matched"]) for e in body["employees"]]
    assert ("SHPL/AMD/0010/2025", True) in shown
    assert all(e["emp_id"] != "SHPL/AMD/0001/2025" for e in body["employees"])


def test_matching_is_trimmed_and_case_insensitive_and_uses_switrus_details(client, org, monkeypatch):
    Upstream(monkeypatch, muster(record("  shpl/amd/0001/2025 ", {"2026-10-01": PRESENT})))
    e = get(browser_login(client, "E9000")).json()["employees"][0]
    assert e["emp_id"] == "SHPL/AMD/0001/2025"
    assert (e["emp_name"], e["branch_id"], e["department_id"]) == ("Sales One", "B008", "SALES")


def test_admin_all_scope_also_lists_unmatched_records_flagged(client, org, monkeypatch):
    aconfig.set_override(settings(admin_scope="all"))
    Upstream(monkeypatch, org_muster())
    body = get(browser_login(client, "E9000")).json()
    unmatched = [e for e in body["employees"] if not e["matched"]]
    assert [(e["emp_id"], e["emp_name"], e["branch_id"]) for e in unmatched] == [("NOT-IN-SWITRUS", "Upstream NOT-IN-SWITRUS", None)]


# ------------------------------------------------------------------ roles and scope

def test_manager_sees_only_matched_employees_in_own_branch_and_department(client, org, monkeypatch):
    Upstream(monkeypatch, org_muster())
    r = get(browser_login(client, "SHPL/AMD/0383/2025"))
    assert r.status_code == 200
    assert ids(r) == ["SHPL/AMD/0001/2025"]
    assert r.json()["access"] == {"scope": "branch_department", "branch_id": "B008", "department_id": "SALES"}
    assert "mapping" not in r.json()  # company-wide counts are for admins only


def test_branch_manager_sees_matched_employees_in_own_branch_only(client, org, monkeypatch):
    Upstream(monkeypatch, org_muster())
    r = get(browser_login(client, "TEMP/367"))
    assert ids(r) == ["SHPL/AMD/0001/2025", "SHPL/AMD/0010/2025"]


def test_query_parameters_cannot_widen_a_scope(client, org, monkeypatch):
    Upstream(monkeypatch, org_muster())
    r = get(browser_login(client, "SHPL/AMD/0383/2025"),
            f"from_date={FROM}&to_date={TO}&branch_id=B003&department_id=VISA&scope=all")
    assert ids(r) == ["SHPL/AMD/0001/2025"]
    assert "SHPL/DEL/0001/2025" not in r.text and "NOT-IN-SWITRUS" not in r.text


def test_scope_follows_the_live_sw_user_row(client, org, monkeypatch):
    Upstream(monkeypatch, org_muster())
    c = browser_login(client, "SHPL/AMD/0383/2025")
    assert ids(get(c)) == ["SHPL/AMD/0001/2025"]
    import sqlite3, os
    conn = sqlite3.connect(os.environ["SQLITE_PATH"])
    conn.execute("UPDATE SW_USER SET BRANCH_ID='B003' WHERE EMPID='SHPL/AMD/0383/2025'")  # test setup only
    conn.commit()
    conn.close()
    assert ids(get(c)) == ["SHPL/DEL/0001/2025"]


def test_scoped_roles_are_refused_until_the_mapping_is_verified(client, org, monkeypatch):
    aconfig.set_override(settings(mapping_verified=False))
    up = Upstream(monkeypatch, org_muster())
    for emp in ("SHPL/AMD/0383/2025", "TEMP/367"):
        client.cookies.clear()
        r = get(browser_login(client, emp))
        assert r.status_code == 403 and "not been confirmed" in r.json()["detail"]
    assert up.requests == []
    # Admins (explicitly authorized) still see it, with a warning.
    client.cookies.clear()
    body = get(browser_login(client, "E9000")).json()
    assert any("not been confirmed" in w for w in body["warnings"])


def test_admin_needs_explicit_authorization(client, org, monkeypatch):
    aconfig.set_override(settings(admin_scope="none"))
    up = Upstream(monkeypatch, org_muster())
    r = get(browser_login(client, "E9000"))
    assert r.status_code == 403 and "ATTENDANCE_ADMIN_SCOPE" in r.json()["detail"]
    assert up.requests == []


def test_other_roles_and_integrations_are_denied(client, org, monkeypatch):
    up = Upstream(monkeypatch, org_muster())
    r = get(browser_login(client, "V1"))  # read-only viewer role: users yes, attendance no
    assert r.status_code == 403 and r.json()["detail"] == service.NO_ROLE
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    r = client.get(f"/attendance/muster?from_date={FROM}&to_date={TO}", headers={"X-API-Key": API_KEY})
    assert r.status_code == 403
    r = client.get(f"/attendance/muster?from_date={FROM}&to_date={TO}")
    assert r.status_code in (401, 403)
    assert up.requests == []


# ------------------------------------------------------------------ data protection

def test_payroll_and_unneeded_hr_fields_are_never_returned(client, org, monkeypatch):
    aconfig.set_override(settings(admin_scope="all"))
    Upstream(monkeypatch, org_muster())
    r = get(browser_login(client, "E9000"))
    text = r.text
    for forbidden in ("grossSalary", "98765", "netPay", "91234", "bankAccount", "123456789012", "employeeId", "companyId"):
        assert forbidden not in text, forbidden
    e = r.json()["employees"][0]
    assert e["source_summary"] == {"presentDays": 1}
    assert set(e) == {"emp_id", "emp_name", "branch_id", "department_id", "is_disabled", "matched",
                      "days", "summary", "source_summary"}


def test_no_database_writes_and_sw_user_unchanged(client, org, monkeypatch):
    from database.database import Database
    before = query("SELECT * FROM SW_USER ORDER BY EMPID")

    def refuse(*a, **k):
        raise AssertionError("attendance must not write to the database")

    monkeypatch.setattr(Database, "db_update", refuse)
    monkeypatch.setattr(Database, "db_execute", refuse)
    statements = []
    real_query = Database.db_query

    def spy(self, sql, params=None):
        statements.append(sql)
        return real_query(self, sql, params)

    monkeypatch.setattr(Database, "db_query", spy)
    Upstream(monkeypatch, org_muster())
    c = browser_login(client, "E9000")
    assert get(c).status_code == 200
    assert c.get("/attendance/access").status_code == 200
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert query("SELECT * FROM SW_USER ORDER BY EMPID") == before


def test_mock_client_produces_the_documented_shape_and_hides_payroll(client, org):
    aconfig.set_override(AttendanceSettings(client_mode="mock", admin_scope="all", mapping_verified=True, cache_seconds=0))
    raw = aclient.MockAttendanceClient(aconfig.get_settings(), codes=["SHPL/AMD/0001/2025"]).fetch(
        __import__("datetime").date(2026, 10, 1), __import__("datetime").date(2026, 10, 7))
    assert raw["success"] is True and set(raw["data"]) == {"companyId", "fromDate", "toDate", "attendance"}
    assert "grossSalary" in json.dumps(raw)  # the fixture carries payroll data on purpose...
    r = get(browser_login(client, "E9000"), "from_date=2026-10-01&to_date=2026-10-07")
    assert r.status_code == 200
    assert "grossSalary" not in r.text  # ...and it never comes out
    assert {"Present", "Off Day"} <= {s["label"] for s in r.json()["states"]}


def test_results_are_cached_briefly_per_range(client, org, monkeypatch):
    aconfig.set_override(settings(cache_seconds=60))
    up = Upstream(monkeypatch, org_muster())
    c = browser_login(client, "E9000")
    get(c)
    get(c)
    assert len(up.requests) == 1
