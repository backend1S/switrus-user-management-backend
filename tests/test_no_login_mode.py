"""UI_AUTH_DISABLED: browser requests without a session act as UI_DEFAULT_EMPID."""
import importlib

import pytest

from conftest import add_user, query


@pytest.fixture
def no_login(monkeypatch):
    from core import config
    monkeypatch.setattr(config, "UI_AUTH_DISABLED", True)
    monkeypatch.setattr(config, "UI_DEFAULT_EMPID", "E9000")


def test_off_by_default(client):
    add_user("E9000", "root admin")
    assert client.get("/users").status_code == 403


def test_acts_as_default_user(client, no_login):
    add_user("E9000", "root admin")
    add_user("E1", "Target")
    assert client.get("/users").status_code == 200
    assert client.get("/auth/me").json()["user"]["emp_id"] == "E9000"
    assert client.patch("/users/E1", json={"Phone": "9000000000"}).status_code == 200
    assert query("SELECT actor_type, actor_id FROM SW_USER_AUDIT WHERE action='user.update'") == [("session", "E9000")]


def test_default_user_role_still_enforced(client, no_login):
    add_user("E9000", "Viewer Person", role="VIEWER")
    add_user("E1", "Target")
    assert client.get("/users").status_code == 200
    assert client.patch("/users/E1", json={"Phone": "9000000000"}).status_code == 403


def test_disabled_or_missing_default_user_rejected(client, no_login):
    assert client.get("/users").status_code == 401
    add_user("E9000", "root admin", disabled=1)
    assert client.get("/users").status_code == 401


def test_cross_site_origin_rejected(client, no_login):
    add_user("E9000", "root admin")
    assert client.get("/users", headers={"Origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize("env, message", [
    ({"APP_ENV": "production", "UI_DEFAULT_EMPID": "E9000"}, "APP_ENV=production"),
    ({"UI_DEFAULT_EMPID": ""}, "requires UI_DEFAULT_EMPID"),
])
def test_config_refuses_unsafe_setup(monkeypatch, env, message):
    from core import config
    monkeypatch.setenv("UI_AUTH_DISABLED", "true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    try:
        with pytest.raises(RuntimeError, match=message):
            importlib.reload(config)
    finally:
        monkeypatch.undo()
        importlib.reload(config)
