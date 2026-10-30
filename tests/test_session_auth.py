"""Browser sessions: sign-in, cookie auth, CSRF, roles enforced server-side."""
import pytest

from conftest import DEPT_PASSWORD, PASSWORD, add_user, browser_login, query


BROWSER = {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}


def sign_in(client, username, password=DEPT_PASSWORD, **kw):
    return client.post("/auth/login", json={"username": username, "password": password}, **kw)


# ------------------------------------------------------------------ POST /auth/login

def test_sign_in_sets_httponly_cookie_and_returns_csrf(client):
    add_user("E1", "Asha Nair", role="admin")  # role is matched case-insensitively
    r = sign_in(client, "  Asha Nair ")
    assert r.status_code == 200
    body = r.json()
    assert body["user"] == {"emp_id": "E1", "emp_name": "Asha Nair", "role": "admin",
                            "branch_id": "BR01", "department_id": "5"}
    assert body["permissions"] == {"is_admin": True, "can_edit": True, "scope": "all",
                                   "scope_branch_id": None, "scope_department_id": None}
    assert len(body["csrf_token"]) >= 32
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("sw_session=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/" in cookie and "Max-Age=" in cookie
    # No secret or token in the body.
    assert "token" not in body and client.cookies.get("sw_session") not in r.text


def test_full_browser_flow(client):
    add_user("E1", "Asha Nair", role="Admin")
    add_user("E2", "Target")
    csrf = sign_in(client, "Asha Nair").json()["csrf_token"]
    me = client.get("/auth/me").json()
    assert me["user"]["emp_id"] == "E1" and me["csrf_token"] == csrf
    assert client.get("/users", params={"include_disabled": "true"}).status_code == 200
    assert client.get("/users/stats").status_code == 200
    # Writes need the CSRF token from the sign-in response.
    assert client.patch("/users/E2", json={"Phone": "9000000000"}).status_code == 403
    r = client.patch("/users/E2", json={"Phone": "9000000000"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert query("SELECT action, actor_id FROM SW_USER_AUDIT ORDER BY audit_id") == [
        ("session.login", "E1"), ("user.update", "E1")]
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_sign_in_wrong_password_or_unknown_user_401(client):
    add_user("E1", "Asha Nair")
    for name, pw in [("Asha Nair", "Wrong!Pass1"), ("ghost", DEPT_PASSWORD)]:
        r = sign_in(client, name, pw)
        assert r.status_code == 401 and r.json() == {"detail": "Incorrect user name or password"}
    assert "sw_session" not in client.cookies


def test_sign_in_disabled_user_403(client):
    add_user("E1", "Asha Nair", disabled=1)
    r = sign_in(client, "Asha Nair")
    assert r.status_code == 403 and r.json() == {"detail": "User account is disabled"}
    assert "sw_session" not in client.cookies


@pytest.mark.parametrize("value, allowed", [(None, True), (0, True), (1, False), ("1", False), ("true", False)])
def test_sign_in_handles_isdisabled_representations(client, value, allowed):
    import sqlite3, os
    add_user("E1", "Asha Nair")
    conn = sqlite3.connect(os.environ["SQLITE_PATH"])
    conn.execute("UPDATE SW_USER SET IsDisabled = ? WHERE EMPID = 'E1'", (value,))
    conn.commit(); conn.close()
    assert sign_in(client, "Asha Nair").status_code == (200 if allowed else 403)


def test_sign_in_role_without_ui_access_403(client):
    add_user("E1", "Sales Person", role="CRE")
    r = sign_in(client, "Sales Person")
    assert r.status_code == 403 and "does not have access" in r.json()["detail"]
    assert "sw_session" not in client.cookies


def test_viewer_can_sign_in_read_only(client):
    add_user("E1", "Viewer Person", role="viewer")
    r = sign_in(client, "Viewer Person")
    assert r.status_code == 200 and r.json()["permissions"]["is_admin"] is False


# ------------------------------------------------------------------ DeptPassword is the UI credential

def test_sign_in_uses_dept_password_not_emp_password(client):
    add_user("E1", "Asha Nair")  # EmpPassword = Argon2(PASSWORD), DeptPassword = DEPT_PASSWORD
    assert sign_in(client, "Asha Nair", PASSWORD).status_code == 401
    assert sign_in(client, "Asha Nair", DEPT_PASSWORD).status_code == 200


def test_sign_in_works_without_any_emp_password(client):
    add_user("E1", "Asha Nair", password_hash=None)
    assert sign_in(client, "Asha Nair").status_code == 200


@pytest.mark.parametrize("stored", [None, "", "   "])
def test_missing_dept_password_rejected(client, stored):
    add_user("E1", "Asha Nair", dept_password=stored)
    for attempt in ["", " ", "x", DEPT_PASSWORD]:
        r = sign_in(client, "Asha Nair", attempt or "x")
        assert r.status_code == 401 and r.json() == {"detail": "Incorrect user name or password"}


@pytest.mark.parametrize("attempt", ["dept-pa55word", "Dept-Pa55word ", " Dept-Pa55word", "Dept-Pa55wor"])
def test_dept_password_compared_exactly(client, attempt):
    add_user("E1", "Asha Nair")
    assert sign_in(client, "Asha Nair", attempt).status_code == 401


def test_dept_password_compared_in_constant_time(client, monkeypatch):
    import hmac
    from services import auth_service
    seen = []
    real = hmac.compare_digest
    monkeypatch.setattr(auth_service.hmac, "compare_digest", lambda a, b: seen.append(1) or real(a, b))
    add_user("E1", "Asha Nair")
    assert sign_in(client, "Asha Nair").status_code == 200
    assert seen


def test_argon2_dept_password_is_verified_as_hash(client):
    """After the migration in the README, a hashed DeptPassword keeps working."""
    from services.email import pwd_context
    hashed = pwd_context.hash("New-Individual-Pw1")
    add_user("E1", "Asha Nair", dept_password=hashed)
    assert sign_in(client, "Asha Nair", "New-Individual-Pw1").status_code == 200
    assert sign_in(client, "Asha Nair", hashed).status_code == 401  # the hash itself is not a password


def test_duplicate_user_name_is_refused(client):
    add_user("E1", "Asha Nair")
    add_user("E2", " asha nair ", role="VIEWER")
    r = sign_in(client, "Asha Nair")
    assert r.status_code == 401 and r.json() == {"detail": "Incorrect user name or password"}
    assert "sw_session" not in client.cookies


def test_shared_dept_password_signs_in_only_the_named_user(client):
    add_user("E1", "Asha Nair", role="ADMIN")
    add_user("E2", "Kiran Das", role="VIEWER")  # same DeptPassword, as is common in SW_USER
    assert sign_in(client, "Kiran Das").json()["user"]["emp_id"] == "E2"
    assert client.get("/auth/me").json()["permissions"]["is_admin"] is False


def test_dept_password_never_returned(client, api):
    add_user("E1", "Asha Nair")
    bodies = [sign_in(client, "Asha Nair").text, client.get("/auth/me").text,
              api.get("/users").text, api.get("/users/E1").text, api.get("/users/by-empid", params={"emp_id": "E1"}).text]
    assert not any(DEPT_PASSWORD in b or "DeptPassword" in b or "dept_password" in b for b in bodies)


def test_sign_in_cross_site_rejected(client):
    add_user("E1", "Asha Nair")
    r = sign_in(client, "Asha Nair", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "sw_session" not in client.cookies
    assert sign_in(client, "Asha Nair", headers={"Origin": "http://testserver"}).status_code == 200


def test_sign_in_throttled_after_repeated_failures(client, monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "_failures", {})
    add_user("E1", "Asha Nair")
    for _ in range(5):
        assert sign_in(client, "Asha Nair", "Wrong!Pass1").status_code == 401
    r = sign_in(client, "Asha Nair")  # even the right password waits out the window
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert sign_in(client, "Someone Else", "Wrong!Pass1").status_code == 401  # other names unaffected


def test_successful_sign_in_resets_failure_count(client, monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "_failures", {})
    add_user("E1", "Asha Nair")
    for _ in range(4):
        sign_in(client, "Asha Nair", "Wrong!Pass1")
    assert sign_in(client, "Asha Nair").status_code == 200
    for _ in range(4):
        assert sign_in(client, "Asha Nair", "Wrong!Pass1").status_code == 401


def test_sign_in_logs_no_password(client, caplog):
    add_user("E1", "Asha Nair")
    sign_in(client, "Asha Nair", "Wrong!Pass1")
    sign_in(client, "Asha Nair")
    assert "Wrong!Pass1" not in caplog.text and DEPT_PASSWORD not in caplog.text


def test_sign_in_is_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/auth/login"]


# ------------------------------------------------------------------ unauthenticated browser vs integration

def test_browser_without_session_gets_401(client):
    for path in ("/users", "/users/stats", "/auth/me"):
        r = client.get(path, headers=BROWSER)
        assert r.status_code == 401 and r.json() == {"detail": "Not signed in"}
    assert client.get("/auth/me").status_code == 401


def test_integration_without_key_still_gets_403(client):
    r = client.get("/users")
    assert r.status_code == 403 and r.json() == {"detail": "Invalid or missing API Key"}


def test_session_cookie_expires(client, monkeypatch):
    import jwt
    from datetime import datetime, timedelta, timezone
    from core import config
    add_user("E1", "Asha Nair")
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    expired = jwt.encode({"typ": "session", "sub": "E1", "csrf": "c", "jti": "j", "iat": past, "exp": past},
                         config.SESSION_SECRET, algorithm="HS256")
    client.cookies.set("sw_session", expired)
    assert client.get("/users").status_code == 401


def test_session_can_read_and_write_with_csrf(admin):
    add_user("E1", "Target")
    assert admin.get("/users").status_code == 200
    assert admin.get("/auth/me").json()["user"]["emp_id"] == "E9000"
    r = admin.patch("/users/E1", json={"Phone": "9000000000"})
    assert r.status_code == 200
    assert query("SELECT actor_type, actor_id FROM SW_USER_AUDIT WHERE action='user.update'") == [("session", "E9000")]


def test_write_without_csrf_rejected(admin):
    add_user("E1", "Target")
    admin.headers.pop("X-CSRF-Token")
    r = admin.patch("/users/E1", json={"Phone": "9000000000"})
    assert r.status_code == 403 and r.json()["detail"] == "CSRF token missing or invalid"
    admin.headers["X-CSRF-Token"] = "forged"
    assert admin.patch("/users/E1", json={"Phone": "9000000000"}).status_code == 403


def test_cross_site_origin_rejected(admin):
    r = admin.get("/users", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert admin.get("/users", headers={"Origin": "http://testserver"}).status_code == 200


def test_viewer_can_read_but_not_write(client):
    add_user("E1", "Viewer Person", role="VIEWER")
    add_user("E2", "Target")
    browser_login(client, "E1")
    assert client.get("/auth/me").json()["permissions"]["is_admin"] is False
    assert client.get("/users").status_code == 200
    for method, url, body in [
        ("patch", "/users/E2", {"Phone": "9000000000"}),
        ("patch", "/users/E2/status", {"is_disabled": True}),
        ("put", "/users/E2/leave", {"start_date": "2026-01-01", "end_date": "2026-01-02"}),
        ("delete", "/users/E2/leave", None),
        ("post", "/users", {"EmpID": "X", "EmpName": "X", "EmpPassword": "Str0ng!Pass", "Email": "x@x.com",
                            "DepartmentID": "1", "Roles": "ADMIN"}),
        ("post", "/integrations/loyaltri/sync", None),
    ]:
        kwargs = {"json": body} if body is not None else {}
        r = getattr(client, method)(url, **kwargs)
        assert r.status_code == 403, (method, url, r.text)
        assert r.json()["detail"] == "Administrator role required"
    assert client.get("/integrations/loyaltri/status").status_code == 403
    assert query("SELECT Phone FROM SW_USER WHERE EMPID='E2'") == [(None,)]


def test_role_without_ui_access_is_rejected(client):
    add_user("E1", "Sales Person", role="SALES")
    browser_login(client, "E1")
    r = client.get("/users")
    assert r.status_code == 403 and "does not have access" in r.json()["detail"]


def test_disabled_admin_session_stops_working(admin):
    import sqlite3, os
    conn = sqlite3.connect(os.environ["SQLITE_PATH"])
    conn.execute("UPDATE SW_USER SET IsDisabled = 1 WHERE EMPID = 'E9000'")
    conn.commit(); conn.close()
    assert admin.get("/users").status_code == 401


def test_demoted_admin_loses_write_access_immediately(admin):
    add_user("E1", "Target")
    import sqlite3, os
    conn = sqlite3.connect(os.environ["SQLITE_PATH"])
    conn.execute("UPDATE SW_USER SET Roles = 'VIEWER' WHERE EMPID = 'E9000'")
    conn.commit(); conn.close()
    assert admin.patch("/users/E1", json={"Phone": "9000000000"}).status_code == 403


def test_admin_cannot_disable_self(admin):
    r = admin.patch("/users/E9000/status", json={"is_disabled": True})
    assert r.status_code == 400


def test_logout_clears_session(admin):
    assert admin.post("/auth/logout").status_code == 200
    assert admin.get("/users").status_code == 403


def test_forged_session_token_rejected(client):
    import jwt
    add_user("E1", "Asha Nair")
    # Signed with the integration secret instead of the session secret.
    forged = jwt.encode({"typ": "session", "sub": "E1", "csrf": "x"}, "test-secret-key-integrations-only-0123456789")
    client.cookies.set("sw_session", forged)
    assert client.get("/users").status_code == 401


def test_integration_jwt_is_not_a_session(api, client):
    add_user("E1", "Asha Nair")
    token = api.post("/login", json={"username": "Asha Nair", "password": PASSWORD}).json()["token"]
    api.headers.pop("X-API-Key")
    client.cookies.set("sw_session", token)
    assert client.get("/users").status_code == 401


def test_logout_revokes_copied_cookie(client):
    add_user("E1", "Asha Nair")
    browser_login(client, "E1")
    stolen = client.cookies.get("sw_session")
    assert client.post("/auth/logout").status_code == 200
    client.cookies.set("sw_session", stolen)
    assert client.get("/users").status_code == 401
    assert query("SELECT COUNT(*) FROM SW_UI_SESSION_REVOKED")[0][0] == 1


def test_revocation_works_without_the_table(client, db):
    import sqlite3
    conn = sqlite3.connect(db); conn.execute("DROP TABLE SW_UI_SESSION_REVOKED"); conn.commit(); conn.close()
    add_user("E1", "Asha Nair")
    browser_login(client, "E1")
    stolen = client.cookies.get("sw_session")
    client.post("/auth/logout")
    client.cookies.set("sw_session", stolen)
    assert client.get("/users").status_code == 401
