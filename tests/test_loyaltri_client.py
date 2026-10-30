"""The configurable HTTP adapter: timeouts, bounded retries, pagination, errors."""
import httpx
import pytest

from services.loyaltri.config import LoyaltriSettings
from services.loyaltri.loyaltri_client import (
    HttpLoyaltriClient, LoyaltriAuthError, LoyaltriNotConfigured,
    LoyaltriResponseError, LoyaltriUnavailable, build_client,
)
from services.loyaltri.loyaltri_mapper import map_record

FIELD_MAP = {"emp_id": "code", "emp_name": "name", "email": "mail", "phone": "mobile"}
BASE = dict(client_mode="http", base_url="https://hr.example.test", employees_path="/employees",
            auth_header="X-Auth", api_key="s3cret-key", field_map=FIELD_MAP, max_retries=2,
            retry_backoff_seconds=0.01, timeout_seconds=2)


def make(handler, **overrides):
    sleeps = []
    client = HttpLoyaltriClient(LoyaltriSettings(**{**BASE, **overrides}),
                                transport=httpx.MockTransport(handler), sleep=sleeps.append)
    return client, sleeps


def test_not_configured_until_required_settings_exist():
    with pytest.raises(LoyaltriNotConfigured) as e:
        build_client(LoyaltriSettings(client_mode="http"))
    msg = str(e.value)
    assert "LOYALTRI_BASE_URL" in msg and "LOYALTRI_EMPLOYEES_PATH" in msg and "LOYALTRI_FIELD_MAP" in msg
    with pytest.raises(LoyaltriNotConfigured):
        build_client(LoyaltriSettings())  # disabled by default


def test_rejects_plain_http_and_protected_fields():
    s = LoyaltriSettings(**{**BASE, "base_url": "http://hr.example.test",
                            "owned_fields": ("phone", "roles")})
    problems = " ".join(s.problems())
    assert "https" in problems and "roles" in problems


def test_sends_auth_header_and_extra_query():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("X-Auth")
        seen["company"] = req.url.params.get("company")
        return httpx.Response(200, json=[{"code": "E1"}])

    client, _ = make(handler, auth_scheme="Bearer", extra_query={"company": "SW"})
    assert list(client.iter_pages()) == [[{"code": "E1"}]]
    assert seen == {"auth": "Bearer s3cret-key", "company": "SW"}


def test_retries_server_errors_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json=[{"code": "E1"}])

    client, sleeps = make(handler)
    assert list(client.iter_pages()) == [[{"code": "E1"}]]
    assert len(calls) == 3 and len(sleeps) == 2


def test_timeouts_are_bounded_and_reported():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    client, sleeps = make(handler)
    with pytest.raises(LoyaltriUnavailable) as e:
        list(client.iter_pages())
    assert len(calls) == 3          # 1 + max_retries
    assert "timed out" in str(e.value)
    assert "s3cret-key" not in str(e.value)


def test_connection_refused_is_unavailable():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    client, _ = make(handler, max_retries=0)
    with pytest.raises(LoyaltriUnavailable):
        list(client.iter_pages())


def test_auth_error_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401)

    client, _ = make(handler)
    with pytest.raises(LoyaltriAuthError):
        list(client.iter_pages())
    assert len(calls) == 1


def test_non_json_and_wrong_records_path():
    client, _ = make(lambda req: httpx.Response(200, text="<html>"))
    with pytest.raises(LoyaltriResponseError):
        list(client.iter_pages())
    client, _ = make(lambda req: httpx.Response(200, json={"data": {"rows": []}}), records_path="data.items")
    with pytest.raises(LoyaltriResponseError) as e:
        list(client.iter_pages())
    assert "LOYALTRI_RECORDS_PATH" in str(e.value)


def test_page_pagination():
    data = [{"code": f"E{i}"} for i in range(5)]

    def handler(req):
        page = int(req.url.params["p"])
        size = int(req.url.params["size"])
        return httpx.Response(200, json={"data": data[(page - 1) * size: page * size]})

    client, _ = make(handler, pagination="page", page_param="p", page_size_param="size",
                     page_size=2, records_path="data")
    pages = list(client.iter_pages())
    assert [len(p) for p in pages] == [2, 2, 1]


def test_cursor_pagination_and_loop_guard():
    def handler(req):
        cur = req.url.params.get("after")
        nxt = {None: "c1", "c1": "c2", "c2": None}[cur]
        return httpx.Response(200, json={"items": [{"code": cur or "start"}], "meta": {"next": nxt}})

    client, _ = make(handler, pagination="cursor", cursor_param="after",
                     next_cursor_path="meta.next", records_path="items")
    assert len(list(client.iter_pages())) == 3

    client, _ = make(lambda req: httpx.Response(200, json={"items": [], "meta": {"next": "same"}}),
                     pagination="cursor", cursor_param="after", next_cursor_path="meta.next",
                     records_path="items")
    with pytest.raises(LoyaltriResponseError):
        list(client.iter_pages())


def test_mapper_normalizes_and_drops_invalid_optional_values():
    emp, err = map_record({"code": " E1 ", "name": "  Asha   Nair ", "mail": "ASHA@Switrus.COM",
                           "mobile": "not a phone"}, FIELD_MAP)
    assert err is None
    assert (emp.emp_id, emp.emp_name, emp.email, emp.phone) == ("E1", "Asha Nair", "asha@switrus.com", None)
    assert any("phone" in w for w in emp.warnings)

    emp, err = map_record({"code": 1001.0}, FIELD_MAP)
    assert emp.emp_id == "1001"
    assert map_record({"name": "No code"}, FIELD_MAP)[0] is None
    assert map_record("garbage", FIELD_MAP)[0] is None
