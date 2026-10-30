"""The endpoints specified in MISSING_APIS.md, plus POST /users."""
from datetime import date, timedelta

import pytest

from conftest import PASSWORD, add_user, query

FORBIDDEN_KEYS = {"EmpPassword", "DeptPassword", "PasswordChangeOTP", "emp_password", "password"}
USER_KEYS = {"emp_id", "emp_name", "department_id", "role", "branch_id", "email", "phone",
             "manager_id", "dept_email", "is_disabled", "leave_start", "leave_end", "is_suspended"}


@pytest.fixture
def people():
    add_user("E1000", "Ravi Menon", role="ADMIN", dept=1, branch="BR01")
    add_user("E1001", "Asha Nair", role="ADMIN", dept=5, branch="BR01", email="asha@switrus.com",
             manager="E1000", phone="9876543210")
    add_user("E1002", "Kiran Das", role="BPO", dept=5, branch="BR02")
    add_user("E1003", "Old Account", role="SALES", dept=6, branch="BR02", disabled=1)


# ------------------------------------------------------------------ GET /users

def test_list_excludes_disabled_by_default(api, people):
    r = api.get("/users")
    assert r.status_code == 200
    assert [u["emp_id"] for u in r.json()] == ["E1001", "E1002", "E1000"]  # ordered by name
    assert all(set(u) == USER_KEYS for u in r.json())


def test_list_includes_disabled_when_asked(api, people):
    users = api.get("/users", params={"include_disabled": "true"}).json()
    assert {u["emp_id"] for u in users} == {"E1000", "E1001", "E1002", "E1003"}
    assert next(u for u in users if u["emp_id"] == "E1003")["is_disabled"] is True


@pytest.mark.parametrize("params,expected", [
    ({"search": "asha"}, ["E1001"]),
    ({"search": "e100"}, ["E1001", "E1002", "E1000"]),
    ({"search": "ASHA@SWITRUS"}, ["E1001"]),
    ({"search": "100%"}, []),             # wildcard is literal
    ({"branch_id": "BR02"}, ["E1002"]),
    ({"department_id": "5"}, ["E1001", "E1002"]),
    ({"role": "bpo"}, ["E1002"]),
    ({"role": "sales", "include_disabled": "true"}, ["E1003"]),
])
def test_list_filters(api, people, params, expected):
    assert [u["emp_id"] for u in api.get("/users", params=params).json()] == expected


def test_list_empty_is_empty_array(api):
    assert api.get("/users").json() == []


def test_stats(api, people):
    today = date.today().isoformat()
    add_user("E1004", "On Leave", leave=(today, today))
    add_user("E1005", "Disabled On Leave", disabled=1, leave=(today, today))
    assert api.get("/users/stats").json() == {"total": 6, "active": 4, "disabled": 2, "on_leave": 1}


# ------------------------------------------------------------------ GET /users/{emp_id}

def test_get_one_including_disabled(api, people):
    r = api.get("/users/E1003")
    assert r.status_code == 200
    assert r.json()["is_disabled"] is True and r.json()["department_id"] == "6"
    assert not FORBIDDEN_KEYS & set(r.json())


def test_get_one_404(api, people):
    r = api.get("/users/E9999")
    assert r.status_code == 404 and r.json() == {"detail": "No user found with EMPID E9999"}


# ------------------------------------------------------------------ PATCH /users/{emp_id}

def test_patch_partial_update(api, people):
    r = api.patch("/users/E1001", json={"Email": " asha.nair@switrus.com ", "Phone": None, "BRANCH_ID": "BR02"})
    assert r.status_code == 200
    u = r.json()
    assert u["email"] == "asha.nair@switrus.com" and u["phone"] is None and u["branch_id"] == "BR02"
    assert u["emp_name"] == "Asha Nair" and u["manager_id"] == "E1000"   # untouched


def test_patch_empty_body_400(api, people):
    r = api.patch("/users/E1001", json={})
    assert r.status_code == 400 and r.json() == {"detail": "No fields to update"}


@pytest.mark.parametrize("body,msg", [
    ({"Email": None}, "Email cannot be empty"),
    ({"Email": "   "}, "Email cannot be empty"),
    ({"EmpName": ""}, "EmpName cannot be empty"),
    ({"Roles": None}, "Roles cannot be empty"),
    ({"DepartmentID": ""}, "DepartmentID cannot be empty"),
])
def test_patch_blank_required_400(api, people, body, msg):
    r = api.patch("/users/E1001", json=body)
    assert r.status_code == 400 and r.json() == {"detail": msg}


def test_patch_rename_conflict_409_and_same_name_ok(api, people):
    r = api.patch("/users/E1001", json={"EmpName": "Kiran Das"})
    assert r.status_code == 409 and r.json() == {"detail": "User name already exists"}
    assert api.patch("/users/E1001", json={"EmpName": "Asha Nair"}).status_code == 200


def test_patch_rejects_unknown_and_protected_fields(api, people):
    for body in ({"EmpPassword": "x"}, {"IsDisabled": 1}, {"EmpID": "Z"}, {"suspend_start": "2026-01-01"}):
        assert api.patch("/users/E1001", json=body).status_code == 422


def test_patch_validates_types_and_formats(api, people):
    assert api.patch("/users/E1001", json={"Email": "not-an-email"}).status_code == 422
    assert api.patch("/users/E1001", json={"Phone": "abc"}).status_code == 422
    assert api.patch("/users/E1001", json={"Roles": 5}).status_code == 422
    assert api.patch("/users/E1001", json={"DepartmentID": 7}).json()["department_id"] == "7"


def test_patch_manager_must_exist_and_not_be_self(api, people):
    assert api.patch("/users/E1001", json={"MANAGER_ID": "E7777"}).status_code == 400
    assert api.patch("/users/E1001", json={"MANAGER_ID": "E1001"}).status_code == 400
    assert api.patch("/users/E1001", json={"MANAGER_ID": "E1002"}).json()["manager_id"] == "E1002"


def test_patch_404(api, people):
    assert api.patch("/users/E9999", json={"Phone": "9000000000"}).status_code == 404


def test_patch_is_audited_without_secrets(api, people):
    api.patch("/users/E1001", json={"Phone": "9000000000"})
    rows = query("SELECT action, target_emp_id, actor_type, details FROM SW_USER_AUDIT")
    assert rows[-1][:3] == ("user.update", "E1001", "integration")
    assert "9000000000" in rows[-1][3] and "Password" not in rows[-1][3]


# ------------------------------------------------------------------ status

def test_disable_then_enable(api, people):
    r = api.patch("/users/E1001/status", json={"is_disabled": True})
    assert r.status_code == 200 and r.json()["is_disabled"] is True
    assert query("SELECT IsDisabled FROM SW_USER WHERE EMPID='E1001'")[0][0] == 1
    assert "E1001" not in [u["emp_id"] for u in api.get("/users").json()]
    assert api.get("/users/by-empid", params={"emp_id": "E1001"}).status_code == 404

    # idempotent
    assert api.patch("/users/E1001/status", json={"is_disabled": True}).status_code == 200

    r = api.patch("/users/E1001/status", json={"is_disabled": False})
    assert r.json()["is_disabled"] is False
    assert query("SELECT IsDisabled FROM SW_USER WHERE EMPID='E1001'")[0][0] == 0


def test_disabled_user_cannot_login(api, people):
    api.patch("/users/E1001", json={"EmpName": "Asha Nair"})
    api.patch("/users/E1001/status", json={"is_disabled": True})
    r = api.post("/login", json={"username": "Asha Nair", "password": PASSWORD})
    assert r.status_code == 403
    api.patch("/users/E1001/status", json={"is_disabled": False})
    assert api.post("/login", json={"username": "Asha Nair", "password": PASSWORD}).status_code == 200


@pytest.mark.parametrize("body", [{}, {"is_disabled": "yes"}, {"is_disabled": 1}, {"is_disabled": None}])
def test_status_validation_422(api, people, body):
    assert api.patch("/users/E1001/status", json=body).status_code == 422


def test_status_404(api, people):
    assert api.patch("/users/E9999/status", json={"is_disabled": True}).status_code == 404


# ------------------------------------------------------------------ leave

def test_leave_window_containing_today_is_suspended(api, people):
    today = date.today()
    r = api.put("/users/E1001/leave", json={"start_date": (today - timedelta(days=1)).isoformat(),
                                             "end_date": (today + timedelta(days=1)).isoformat()})
    assert r.status_code == 200
    assert r.json()["is_suspended"] == 1
    assert r.json()["leave_start"] == (today - timedelta(days=1)).isoformat()
    # existing endpoints see it too
    assert api.get("/users/by-empid", params={"emp_id": "E1001"}).json()["is_suspended"] == 1


def test_future_leave_not_suspended_and_put_replaces(api, people):
    api.put("/users/E1001/leave", json={"start_date": "2020-01-01", "end_date": "2020-01-05"})
    r = api.put("/users/E1001/leave", json={"start_date": "2099-11-03", "end_date": "2099-11-03"})
    assert r.json()["is_suspended"] == 0
    assert (r.json()["leave_start"], r.json()["leave_end"]) == ("2099-11-03", "2099-11-03")


def test_leave_reversed_400_and_malformed_422(api, people):
    r = api.put("/users/E1001/leave", json={"start_date": "2026-11-07", "end_date": "2026-11-03"})
    assert r.status_code == 400 and r.json() == {"detail": "end_date cannot be before start_date"}
    assert api.put("/users/E1001/leave", json={"start_date": "07/11/2026", "end_date": "2026-11-08"}).status_code == 422
    assert api.put("/users/E1001/leave", json={"start_date": "2026-11-07"}).status_code == 422


def test_clear_leave_and_idempotent(api, people):
    today = date.today().isoformat()
    api.put("/users/E1001/leave", json={"start_date": today, "end_date": today})
    r = api.delete("/users/E1001/leave")
    assert r.status_code == 200
    assert (r.json()["leave_start"], r.json()["leave_end"], r.json()["is_suspended"]) == (None, None, 0)
    assert query("SELECT suspend_start, suspend_end FROM SW_USER WHERE EMPID='E1001'") == [(None, None)]
    assert api.delete("/users/E1001/leave").status_code == 200


def test_leave_404(api):
    assert api.put("/users/E9/leave", json={"start_date": "2026-01-01", "end_date": "2026-01-01"}).status_code == 404
    assert api.delete("/users/E9/leave").status_code == 404


# ------------------------------------------------------------------ POST /users

NEW = {"EmpID": "E2001", "EmpName": "Meera Pillai", "EmpPassword": "Str0ng!Pass",
       "Email": "meera@switrus.com", "DepartmentID": "6", "Roles": "BPO", "BRANCH_ID": "BR02"}


def test_create_user(api, people):
    r = api.post("/users", json=NEW)
    assert r.status_code == 201 and r.json() == {"message": "User Meera Pillai created successfully"}
    u = api.get("/users/E2001").json()
    assert u["emp_name"] == "Meera Pillai" and u["is_disabled"] is False
    assert api.post("/login", json={"username": "Meera Pillai", "password": "Str0ng!Pass"}).status_code == 200
    assert query("SELECT action FROM SW_USER_AUDIT WHERE target_emp_id='E2001'") == [("user.create",)]


def test_create_duplicate_name_409(api, people):
    r = api.post("/users", json={**NEW, "EmpName": "Asha Nair"})
    assert r.status_code == 409 and r.json() == {"detail": "User already exists"}


def test_create_duplicate_empid_409(api, people):
    r = api.post("/users", json={**NEW, "EmpID": " E1001 "})
    assert r.status_code == 409 and r.json() == {"detail": "Employee ID already exists"}
    assert query("SELECT COUNT(*) FROM SW_USER WHERE EMPID='E1001'")[0][0] == 1


def test_create_password_policy_400(api, people):
    r = api.post("/users", json={**NEW, "EmpPassword": "weakpass1"})
    assert r.status_code == 400 and "uppercase" in r.json()["detail"]


def test_create_missing_field_422_and_blank_400(api, people):
    body = dict(NEW); body.pop("Roles")
    assert api.post("/users", json=body).status_code == 422
    assert api.post("/users", json={**NEW, "Email": "  "}).status_code == 400


# ------------------------------------------------------------------ text departments (SW_USER.DepartmentID is nvarchar)

@pytest.fixture
def departments():
    add_user("SHPL/DEL/0417/2026", "AAS MOHAMMED", role="CRE", dept="SALES", branch="B003")
    add_user("E2001", "Visa Person", role="EXECUTIVE", dept="VISA", branch="B001")
    add_user("E2002", "Ops Person", role="MANAGER", dept="Operations", branch="B001")


def test_text_departments_are_returned_as_stored(api, departments):
    users = {x["emp_id"]: x for x in api.get("/users").json()}
    u = users["SHPL/DEL/0417/2026"]
    assert {k: u[k] for k in ("emp_name", "department_id", "role", "branch_id", "is_disabled")} == {
        "emp_name": "AAS MOHAMMED", "department_id": "SALES", "role": "CRE", "branch_id": "B003", "is_disabled": False,
    }
    assert users["E2002"]["department_id"] == "Operations"


def test_department_filter_matches_text_case_insensitively(api, departments):
    assert [u["emp_id"] for u in api.get("/users", params={"department_id": "visa"}).json()] == ["E2001"]
    assert [u["emp_id"] for u in api.get("/users", params={"department_id": "OPERATIONS"}).json()] == ["E2002"]


def test_legacy_lookups_return_text_departments(api, departments):
    r = api.get("/users/filter", params={"role": "cre", "branch_id": "B003"})
    assert r.json()[0]["department_id"] == "SALES"
    r = api.get("/users/by-empid", params={"emp_id": "E2001"})
    assert r.json()["department_id"] == "VISA"
    assert [u["department_id"] for u in api.get("/users/department/Operations").json()] == ["Operations"]


def test_create_and_patch_keep_text_departments(api, departments):
    body = {"EmpID": "E2100", "EmpName": "New Sales", "EmpPassword": PASSWORD, "Email": "ns@switrus.com",
            "DepartmentID": "SALES", "Roles": "CRE"}
    assert api.post("/users", json=body).status_code == 201
    assert api.get("/users/E2100").json()["department_id"] == "SALES"
    assert api.patch("/users/E2100", json={"DepartmentID": "AIRLINE"}).json()["department_id"] == "AIRLINE"
    assert query("SELECT DepartmentID FROM SW_USER WHERE EMPID='E2100'") == [("AIRLINE",)]


def test_field_lengths_match_sql_columns(api, departments):
    assert api.patch("/users/E2001", json={"DepartmentID": "X" * 51}).status_code == 422
    assert api.patch("/users/E2001", json={"EmpName": "X" * 51}).status_code == 422


# ------------------------------------------------------------------ EMPIDs with slashes (201 of 206 real IDs)

@pytest.mark.parametrize("path", ["/users/SHPL/DEL/0417/2026", "/users/SHPL%2FDEL%2F0417%2F2026"])
def test_slash_empid_get(api, departments, path):
    r = api.get(path)
    assert r.status_code == 200 and r.json()["emp_id"] == "SHPL/DEL/0417/2026"


def test_slash_empid_actions(api, departments):
    eid = "SHPL%2FDEL%2F0417%2F2026"  # what the UI sends (encodeURIComponent)
    assert api.patch(f"/users/{eid}", json={"Phone": "9000000000"}).json()["phone"] == "9000000000"
    assert api.patch(f"/users/{eid}/status", json={"is_disabled": True}).json()["is_disabled"] is True
    assert api.patch(f"/users/{eid}/status", json={"is_disabled": False}).json()["is_disabled"] is False
    r = api.put(f"/users/{eid}/leave", json={"start_date": "2026-11-03", "end_date": "2026-11-07"})
    assert (r.json()["leave_start"], r.json()["leave_end"]) == ("2026-11-03", "2026-11-07")
    assert api.delete(f"/users/{eid}/leave").json()["leave_start"] is None
    assert query("SELECT Phone, IsDisabled FROM SW_USER WHERE EMPID='SHPL/DEL/0417/2026'") == [("9000000000", 0)]
    # Other users untouched.
    assert query("SELECT COUNT(*) FROM SW_USER WHERE Phone IS NOT NULL") == [(1,)]
