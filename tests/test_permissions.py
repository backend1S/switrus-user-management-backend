"""Role-based access: ADMIN full, MANAGER own branch + department, BRANCHMANAGER
own branch, everything else refused. Scope comes from SW_USER, never the browser."""
import sqlite3
import os
from datetime import date

import pytest

from conftest import DEPT_PASSWORD, add_user, query

WRITES = [
    ("post", "/users", {"EmpID": "N1", "EmpName": "New", "EmpPassword": "Str0ng!Pass", "Email": "n@x.com",
                        "DepartmentID": "SALES", "Roles": "CRE"}),
    ("patch", "/users/{id}", {"Phone": "9000000000"}),
    ("patch", "/users/{id}/status", {"is_disabled": True}),
    ("put", "/users/{id}/leave", {"start_date": "2026-11-03", "end_date": "2026-11-04"}),
    ("delete", "/users/{id}/leave", None),
]


@pytest.fixture
def org():
    """Mirrors real SW_USER data: mixed-case roles, slash EMPIDs, text departments."""
    today = date.today().isoformat()
    add_user("TEMP/107", "Admin", role="Admin", dept="Operations", branch="B001")
    add_user("SHPL/AMD/0383/2025", "HEMANG C KANANI", role="Manager", dept="SALES", branch="B008")
    add_user("TEMP/367", "Branch Boss", role="BRANCHMANAGER", dept="SALES", branch="B008")
    # B008 / SALES: the manager's team (one disabled, one on leave).
    add_user("SHPL/AMD/0001/2025", "Sales One", role="CRE", dept="SALES", branch="B008")
    add_user("SHPL/AMD/0002/2025", "Sales Two", role="Executive", dept="sales", branch=" b008 ", leave=(today, today))
    add_user("SHPL/AMD/0003/2025", "Sales Gone", role="CRE", dept="SALES", branch="B008", disabled=1)
    # Same branch, other department: branch manager sees, manager doesn't.
    add_user("SHPL/AMD/0010/2025", "Visa Person", role="Executive", dept="VISA", branch="B008")
    # Other branch, same department: nobody scoped sees it.
    add_user("SHPL/DEL/0417/2026", "AAS MOHAMMED", role="CRE", dept="SALES", branch="B003")
    # The real "invalid branch" row.
    add_user("SHPL/XXX/0001/2026", "Bad Branch", role="Executive", dept="SALES", branch="Manager")


def sign_in(client, name):
    r = client.post("/auth/login", json={"username": name, "password": DEPT_PASSWORD})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return r.json()


def ids(client, **params):
    r = client.get("/users", params=params)
    assert r.status_code == 200, r.text
    return sorted(u["emp_id"] for u in r.json())


MANAGER_TEAM = ["SHPL/AMD/0001/2025", "SHPL/AMD/0002/2025", "SHPL/AMD/0383/2025", "TEMP/367"]
BRANCH_B008 = MANAGER_TEAM + ["SHPL/AMD/0010/2025"]


# ------------------------------------------------------------------ ADMIN

def test_admin_sees_everything_and_can_write(client, org):
    s = sign_in(client, "Admin")
    assert s["permissions"] == {"is_admin": True, "can_edit": True, "scope": "all",
                                "scope_branch_id": None, "scope_department_id": None}
    assert len(ids(client, include_disabled="true")) == 9
    assert client.get("/users/stats").json() == {"total": 9, "active": 8, "disabled": 1, "on_leave": 1}
    for method, url, body in WRITES:
        url = url.format(id="SHPL%2FDEL%2F0417%2F2026")
        kw = {"json": body} if body is not None else {}
        assert getattr(client, method)(url, **kw).status_code in (200, 201), (method, url)


@pytest.mark.parametrize("role", ["ADMIN", "Admin", "admin", " admin "])
def test_admin_role_is_case_insensitive(client, role):
    add_user("E1", "Boss", role=role)
    assert sign_in(client, "Boss")["permissions"]["is_admin"] is True


# ------------------------------------------------------------------ MANAGER

def test_manager_signs_in_read_only_scoped_to_branch_and_department(client, org):
    s = sign_in(client, "HEMANG C KANANI")
    assert s["user"]["branch_id"] == "B008" and s["user"]["department_id"] == "SALES"
    assert s["permissions"] == {"is_admin": False, "can_edit": False, "scope": "branch_department",
                                "scope_branch_id": "B008", "scope_department_id": "SALES"}
    assert client.get("/auth/me").json()["permissions"]["scope"] == "branch_department"
    active_team = [i for i in MANAGER_TEAM if i != "SHPL/AMD/0003/2025"]
    assert ids(client) == active_team
    assert ids(client, include_disabled="true") == sorted(MANAGER_TEAM + ["SHPL/AMD/0003/2025"])


def test_manager_dashboard_counts_are_scoped(client, org):
    sign_in(client, "HEMANG C KANANI")
    assert client.get("/users/stats").json() == {"total": 5, "active": 4, "disabled": 1, "on_leave": 1}


@pytest.mark.parametrize("role", ["Manager", "MANAGER", "manager"])
def test_manager_role_is_case_insensitive(client, role):
    add_user("E1", "Mgr", role=role, dept="SALES", branch="B008")
    assert sign_in(client, "Mgr")["permissions"]["scope"] == "branch_department"


def test_manager_cannot_write(client, org):
    sign_in(client, "HEMANG C KANANI")
    for target in ("SHPL%2FAMD%2F0001%2F2025", "SHPL%2FDEL%2F0417%2F2026"):  # in and out of scope
        for method, url, body in WRITES:
            url = url.format(id=target)
            kw = {"json": body} if body is not None else {}
            r = getattr(client, method)(url, **kw)
            assert r.status_code == 403 and r.json()["detail"] == "Administrator role required", (method, url)
    assert query("SELECT COUNT(*) FROM SW_USER WHERE Phone IS NOT NULL OR EMPID='N1'") == [(0,)]
    assert query("SELECT IsDisabled FROM SW_USER WHERE EMPID='SHPL/AMD/0001/2025'") == [(0,)]


def test_manager_cannot_reach_loyaltri(client, org):
    sign_in(client, "HEMANG C KANANI")
    assert client.get("/integrations/loyaltri/status").status_code == 403
    assert client.post("/integrations/loyaltri/sync").status_code == 403


# ------------------------------------------------------------------ BRANCHMANAGER

def test_branch_manager_sees_own_branch_all_departments(client, org):
    s = sign_in(client, "Branch Boss")
    assert s["permissions"]["scope"] == "branch" and s["permissions"]["scope_department_id"] is None
    assert ids(client, include_disabled="true") == sorted(BRANCH_B008 + ["SHPL/AMD/0003/2025"])
    assert client.get("/users/stats").json() == {"total": 6, "active": 5, "disabled": 1, "on_leave": 1}
    r = client.patch("/users/SHPL%2FAMD%2F0001%2F2025", json={"Phone": "9000000000"})
    assert r.status_code == 403


# ------------------------------------------------------------------ bypass attempts

@pytest.mark.parametrize("params", [
    {"branch_id": "B003"},
    {"department_id": "VISA"},
    {"branch_id": "B003", "department_id": "SALES"},
    {"search": "AAS"},
    {"search": "%"},
])
def test_manager_query_params_cannot_widen_scope(client, org, params):
    sign_in(client, "HEMANG C KANANI")
    assert set(ids(client, include_disabled="true", **params)) <= set(MANAGER_TEAM + ["SHPL/AMD/0003/2025"])


@pytest.mark.parametrize("path", [
    "/users/SHPL%2FDEL%2F0417%2F2026",      # other branch
    "/users/SHPL%2FAMD%2F0010%2F2025",      # same branch, other department
    "/users/SHPL/AMD/0010/2025",
    "/users/SHPL%2FXXX%2F0001%2F2026",      # invalid branch row
])
def test_manager_cannot_open_users_outside_scope(client, org, path):
    sign_in(client, "HEMANG C KANANI")
    r = client.get(path)
    assert r.status_code == 404 and "No user found" in r.json()["detail"]


def test_manager_can_open_own_team_member(client, org):
    sign_in(client, "HEMANG C KANANI")
    assert client.get("/users/SHPL%2FAMD%2F0001%2F2025").json()["emp_name"] == "Sales One"


def test_legacy_lookups_are_scoped_for_sessions(client, org):
    sign_in(client, "HEMANG C KANANI")
    r = client.get("/users/filter", params={"role": "cre,executive", "branch_id": "B003"})
    assert r.status_code == 200 and r.json() == []
    assert [u["emp_id"] for u in client.get("/users/department/VISA").json()] == []
    assert sorted(u["emp_id"] for u in client.get("/users/department/SALES").json()) == [
        "SHPL/AMD/0001/2025", "SHPL/AMD/0383/2025", "TEMP/367"]
    assert client.get("/users/by-empid", params={"emp_id": "SHPL/DEL/0417/2026"}).status_code == 404
    assert client.get("/users/by-empid", params={"emp_id": "SHPL/AMD/0001/2025"}).status_code == 200


def test_scope_follows_database_changes_immediately(client, org):
    sign_in(client, "HEMANG C KANANI")
    conn = sqlite3.connect(os.environ["SQLITE_PATH"])
    conn.execute("UPDATE SW_USER SET BRANCH_ID='B003' WHERE EMPID='SHPL/AMD/0383/2025'")
    conn.commit(); conn.close()
    assert ids(client) == ["SHPL/AMD/0383/2025", "SHPL/DEL/0417/2026"]  # B003 SALES, incl. the manager


def test_browser_headers_cannot_claim_admin(client, org):
    sign_in(client, "HEMANG C KANANI")
    r = client.patch("/users/SHPL%2FAMD%2F0001%2F2025", json={"Phone": "9000000000"},
                     headers={"X-Role": "ADMIN", "X-Branch": "B003"})
    assert r.status_code == 403


# ------------------------------------------------------------------ refused accounts

@pytest.mark.parametrize("role", ["CRE", "Executive", "VISA HEAD", "OPERATION MANAGER", "string", "", None])
def test_other_roles_are_refused(client, role):
    add_user("E1", "Someone", role=role, dept="SALES", branch="B008")
    r = client.post("/auth/login", json={"username": "Someone", "password": DEPT_PASSWORD})
    assert r.status_code == 403 and r.json()["detail"] == "Your role does not have access to User Management"
    assert "sw_session" not in client.cookies


@pytest.mark.parametrize("branch, dept, missing", [
    (None, "SALES", "branch"), ("", "SALES", "branch"), ("Manager", "SALES", "branch"), ("string", "SALES", "branch"),
    ("B008", None, "department"), ("B008", "  ", "department"), ("B008", "string", "department"),
])
def test_manager_with_invalid_assignment_gets_no_access(client, branch, dept, missing):
    add_user("E1", "Mgr", role="Manager", dept=dept, branch=branch)
    add_user("E2", "Other", role="CRE", dept="SALES", branch="B008")
    r = client.post("/auth/login", json={"username": "Mgr", "password": DEPT_PASSWORD})
    assert r.status_code == 403 and f"no valid {missing}" in r.json()["detail"]


def test_branch_manager_needs_only_a_branch(client):
    add_user("E1", "BM", role="BRANCHMANAGER", dept=None, branch="COK")
    assert client.post("/auth/login", json={"username": "BM", "password": DEPT_PASSWORD}).status_code == 200
    add_user("E2", "BM2", role="BRANCHMANAGER", dept="SALES", branch=None)
    assert client.post("/auth/login", json={"username": "BM2", "password": DEPT_PASSWORD}).status_code == 403


def test_assignment_broken_after_sign_in_stops_access(client, org):
    sign_in(client, "HEMANG C KANANI")
    conn = sqlite3.connect(os.environ["SQLITE_PATH"])
    conn.execute("UPDATE SW_USER SET DepartmentID=NULL WHERE EMPID='SHPL/AMD/0383/2025'")
    conn.commit(); conn.close()
    r = client.get("/users")
    assert r.status_code == 403 and "no valid department" in r.json()["detail"]


@pytest.mark.parametrize("value, can_sign_in", [(None, True), (0, True), (1, False)])
def test_null_is_disabled_means_enabled(client, value, can_sign_in):
    add_user("E1", "Mgr", role="Manager", dept="SALES", branch="B008", disabled=value)
    r = client.post("/auth/login", json={"username": "Mgr", "password": DEPT_PASSWORD})
    assert r.status_code == (200 if can_sign_in else 403)


def test_disabled_manager_refused_and_null_password_refused(client):
    add_user("E1", "Off Mgr", role="Manager", dept="SALES", branch="B008", disabled=1)
    add_user("E2", "No Pw Mgr", role="Manager", dept="SALES", branch="B008", dept_password=None)
    assert client.post("/auth/login", json={"username": "Off Mgr", "password": DEPT_PASSWORD}).status_code == 403
    assert client.post("/auth/login", json={"username": "No Pw Mgr", "password": DEPT_PASSWORD}).status_code == 401


# ------------------------------------------------------------------ integrations keep their policy

def test_integration_is_unscoped(api, org):
    assert len(api.get("/users", params={"include_disabled": "true"}).json()) == 9
    assert api.get("/users/stats").json()["total"] == 9
    assert api.get("/users/by-empid", params={"emp_id": "SHPL/DEL/0417/2026"}).status_code == 200


# ------------------------------------------------------------------ policy unit checks

def test_policy_matrix():
    from core.permissions import policy_for
    assert policy_for("admin").is_admin and policy_for("admin").scope == "all"
    assert not policy_for("Manager").is_admin and policy_for("Manager").scope == "branch_department"
    assert not policy_for("branchmanager").is_admin and policy_for("BRANCHMANAGER").scope == "branch"
    assert policy_for("VIEWER").scope == "all" and not policy_for("VIEWER").is_admin  # test env
    for role in ("CRE", "BPO", "VISA HEAD", "OPERATION MANAGER", "", None):
        assert policy_for(role) is None
