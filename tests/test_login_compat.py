"""POST /login, the contract existing integrations (CRM, accounting) rely on."""
import jwt

from conftest import API_KEY, PASSWORD, add_user, query


def test_login_returns_token_signed_with_secret_key(api):
    add_user("E1", "Asha Nair", role="ADMIN", branch="BR01", manager="E0", phone="9876543210")
    r = api.post("/login", json={"username": "Asha Nair", "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "User validated successfully"
    assert set(body["user"]) == {"emp_id", "emp_name", "branch_id", "dep_id", "role", "email",
                                 "manager_id", "phone", "assigned_branches"}
    claims = jwt.decode(body["token"], API_KEY, algorithms=["HS256"])
    assert claims["emp_id"] == "E1" and claims["role"] == "ADMIN"


def test_login_trims_username(api):
    add_user("E1", "Asha Nair")
    assert api.post("/login", json={"username": "  Asha Nair ", "password": PASSWORD}).status_code == 200


def test_login_wrong_password_401(api):
    add_user("E1", "Asha Nair")
    r = api.post("/login", json={"username": "Asha Nair", "password": "nope"})
    assert r.status_code == 401 and r.json() == {"detail": "Invalid credentials"}


def test_login_unknown_user_401(api):
    assert api.post("/login", json={"username": "ghost", "password": PASSWORD}).status_code == 401


def test_login_disabled_user_403(api):
    """Regression: the old code swallowed this 403 and let disabled users in."""
    add_user("E1", "Asha Nair", disabled=1)
    r = api.post("/login", json={"username": "Asha Nair", "password": PASSWORD})
    assert r.status_code == 403 and r.json() == {"detail": "User account is disabled"}


def test_login_bpo_gets_assigned_branches(api):
    add_user("E5", "Bpo User", role="BPO")
    import sqlite3, os
    conn = sqlite3.connect(os.environ["SQLITE_PATH"])
    conn.executemany("INSERT INTO BPO_BRANCH_ASSIGN VALUES (?, ?)", [("E5", "BR01"), ("E5", "BR09")])
    conn.commit(); conn.close()
    r = api.post("/login", json={"username": "Bpo User", "password": PASSWORD})
    assert sorted(r.json()["user"]["assigned_branches"]) == ["BR01", "BR09"]


def test_missing_or_wrong_api_key_403(client):
    for headers in ({}, {"X-API-Key": "wrong"}):
        r = client.post("/login", json={"username": "x", "password": "y"}, headers=headers)
        assert r.status_code == 403 and r.json() == {"detail": "Invalid or missing API Key"}
    assert client.get("/users").status_code == 403


def test_api_key_query_param_still_accepted(client):
    add_user("E1", "Asha Nair")
    r = client.post(f"/login?api_key={API_KEY}", json={"username": "Asha Nair", "password": PASSWORD})
    assert r.status_code == 200


def test_api_key_query_param_not_in_openapi(client):
    """Swagger must not offer to put the key in the URL."""
    spec = client.get("/openapi.json").json()
    assert "api_key" not in str(spec)
    assert spec["components"]["securitySchemes"] == {
        "APIKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}


def test_api_key_query_param_can_be_turned_off(client, monkeypatch):
    from core import config
    monkeypatch.setattr(config, "ALLOW_API_KEY_QUERY", False)
    r = client.get(f"/users?api_key={API_KEY}")
    assert r.status_code == 403 and "X-API-Key header" in r.json()["detail"]
    assert client.get("/users", headers={"X-API-Key": API_KEY}).status_code == 200


def test_api_key_query_use_is_logged_without_the_key(client, caplog):
    client.get(f"/users?api_key={API_KEY}")
    # The test client's own httpx request log is not the API's; skip it.
    api_logs = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    assert "Deprecated ?api_key=" in api_logs and API_KEY not in api_logs


def test_access_log_redacts_api_key():
    import logging
    import main
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4", "GET", f"/users?x=1&api_key={API_KEY}", "1.1", 200), None)
    main._RedactSecrets().filter(rec)
    assert API_KEY not in rec.getMessage() and "api_key=[redacted]" in rec.getMessage()


def test_database_errors_are_not_returned_to_clients(api, monkeypatch):
    from database.database import Database
    monkeypatch.setattr(Database, "db_query", lambda self, *a, **k: (False, "Login failed for user 'sa' on 10.0.0.9"))
    r = api.get("/users")
    assert r.status_code == 500 and r.json() == {"detail": "Database error"}


def test_existing_read_endpoints_still_route(api):
    """Route-order regression: the new /users/{emp_id} must not swallow these."""
    add_user("E1", "Asha Nair", role="ADMIN", branch="BR01", dept=5)
    add_user("E2", "Off Line", role="ADMIN", branch="BR01", dept=5, disabled=1)

    r = api.get("/users/filter", params={"role": "admin", "branch_id": "BR01"})
    assert r.status_code == 200
    assert [u["emp_id"] for u in r.json()] == ["E1"]
    assert set(r.json()[0]) == {"emp_id", "emp_name", "department_id", "role", "branch_id", "is_suspended"}

    r = api.get("/users/department/5")
    assert r.status_code == 200 and [u["emp_id"] for u in r.json()] == ["E1"]

    r = api.get("/users/by-empid", params={"emp_id": "E1"})
    assert r.status_code == 200 and r.json()["emp_id"] == "E1"
    assert api.get("/users/by-empid", params={"emp_id": "E2"}).status_code == 404


def test_filter_accepts_comma_separated_roles(api):
    add_user("E1", "A", role="ADMIN")
    add_user("E2", "B", role="BPO")
    add_user("E3", "C", role="SALES")
    r = api.get("/users/filter", params={"role": "admin, bpo", "branch_id": "BR01"})
    assert sorted(u["emp_id"] for u in r.json()) == ["E1", "E2"]


def test_filter_is_not_sql_injectable(api):
    add_user("E1", "A", role="ADMIN")
    r = api.get("/users/filter", params={"role": "x' OR '1'='1", "branch_id": "x' OR '1'='1"})
    assert r.status_code == 200 and r.json() == []
    assert query("SELECT COUNT(*) FROM SW_USER")[0][0] == 1


def test_health_endpoints_need_no_key_and_ignore_loyaltri(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ok", "database": "ok"}
