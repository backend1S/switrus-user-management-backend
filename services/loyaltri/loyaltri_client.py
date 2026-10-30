"""Loyaltri clients.

LoyaltriClient is the adapter interface the sync depends on. Two
implementations:

* HttpLoyaltriClient - generic, configuration-driven HTTP adapter. Endpoint,
  auth header, records location and pagination all come from settings, because
  Loyaltri's real contract has not been supplied. Bounded timeouts and retries.
* MockLoyaltriClient - local development and tests. Reads a JSON file and can
  simulate outages, timeouts and server/auth errors.

Clients are only ever used by the sync worker; no request path of the user
management API calls them.
"""
import json
import logging
import os
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

import httpx

from services.loyaltri.config import LoyaltriSettings

logger = logging.getLogger(__name__)

MOCK_DATA_FILE = os.path.join(os.path.dirname(__file__), "mock_employees.json")


class LoyaltriError(Exception):
    """Base class. The message is shown to admins, so it never contains secrets."""


class LoyaltriNotConfigured(LoyaltriError):
    pass


class LoyaltriUnavailable(LoyaltriError):
    """Network failure, timeout or 5xx/429 after all retries."""


class LoyaltriAuthError(LoyaltriError):
    pass


class LoyaltriResponseError(LoyaltriError):
    """Loyaltri answered, but not with something we can use."""


class LoyaltriClient:
    """Adapter interface: yield pages (lists) of raw employee records."""

    def iter_pages(self) -> Iterator[List[Dict[str, Any]]]:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


def get_path(data: Any, path: str) -> Any:
    """Read a dotted path ("data.items") from parsed JSON. Empty path = data."""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


class HttpLoyaltriClient(LoyaltriClient):
    def __init__(
        self,
        settings: LoyaltriSettings,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        problems = settings.problems()
        if problems:
            raise LoyaltriNotConfigured("; ".join(problems))
        self.s = settings
        self._sleep = sleep
        headers = {"Accept": "application/json"}
        if settings.api_key:
            value = f"{settings.auth_scheme} {settings.api_key}".strip()
            headers[settings.auth_header] = value
        self._http = httpx.Client(
            base_url=settings.base_url,
            headers=headers,
            timeout=httpx.Timeout(settings.timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def _get(self, params: Dict[str, Any]) -> Any:
        attempts = self.s.max_retries + 1
        last = "no response"
        for attempt in range(attempts):
            retry_after: Optional[float] = None
            try:
                resp = self._http.get(self.s.employees_path, params=params)
            except httpx.TimeoutException:
                last = f"timed out after {self.s.timeout_seconds:g}s"
            except httpx.TransportError as e:
                last = f"connection failed ({type(e).__name__})"
            else:
                if resp.status_code in (401, 403):
                    raise LoyaltriAuthError(
                        f"Loyaltri rejected the credentials (HTTP {resp.status_code}). Check LOYALTRI_API_KEY."
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                    ra = resp.headers.get("retry-after", "")
                    retry_after = float(ra) if ra.replace(".", "", 1).isdigit() else None
                elif resp.status_code >= 400:
                    raise LoyaltriResponseError(f"Loyaltri returned HTTP {resp.status_code} for the employee request")
                else:
                    try:
                        return resp.json()
                    except ValueError:
                        raise LoyaltriResponseError("Loyaltri returned a response that is not JSON")

            if attempt < attempts - 1:
                delay = retry_after if retry_after is not None else self.s.retry_backoff_seconds * (2 ** attempt)
                logger.warning("Loyaltri request failed (%s); retry %d/%d in %.1fs",
                               last, attempt + 1, attempts - 1, delay)
                self._sleep(min(delay, 30.0))
        raise LoyaltriUnavailable(f"Loyaltri is unreachable: {last} (tried {attempts} time(s))")

    def _records(self, body: Any) -> List[Dict[str, Any]]:
        records = get_path(body, self.s.records_path)
        if not isinstance(records, list):
            where = self.s.records_path or "the response root"
            raise LoyaltriResponseError(f"Expected a list of employees at {where}; check LOYALTRI_RECORDS_PATH")
        return records

    def iter_pages(self) -> Iterator[List[Dict[str, Any]]]:
        s = self.s
        base = dict(s.extra_query)
        if s.pagination == "none":
            yield self._records(self._get(base))
            return

        page = s.first_page
        offset = 0
        cursor: Optional[str] = None
        seen_cursors = set()
        for _ in range(s.max_pages):
            params = dict(base)
            if s.page_size_param:
                params[s.page_size_param] = s.page_size
            if s.pagination == "page":
                params[s.page_param] = page
            elif s.pagination == "offset":
                params[s.page_param] = offset
            elif s.pagination == "cursor" and cursor:
                params[s.cursor_param] = cursor

            body = self._get(params)
            records = self._records(body)
            yield records

            if s.pagination == "cursor":
                nxt = get_path(body, s.next_cursor_path)
                if not nxt:
                    return
                nxt = str(nxt)
                if nxt in seen_cursors:
                    raise LoyaltriResponseError("Loyaltri pagination repeated a cursor; stopping")
                seen_cursors.add(nxt)
                cursor = nxt
            else:
                if len(records) < s.page_size or not records:
                    return
                page += 1
                offset += len(records)
        raise LoyaltriResponseError(f"Stopped after LOYALTRI_MAX_PAGES={s.max_pages} pages")


class MockLoyaltriClient(LoyaltriClient):
    """Serves records from a JSON file (a list, or {"employees": [...]})."""

    def __init__(self, settings: LoyaltriSettings, sleep: Callable[[float], None] = time.sleep):
        self.s = settings
        self._sleep = sleep

    def iter_pages(self) -> Iterator[List[Dict[str, Any]]]:
        failure = self.s.mock_failure
        if failure == "outage":
            raise LoyaltriUnavailable("Loyaltri is unreachable: connection failed (simulated outage)")
        if failure == "timeout":
            self._sleep(min(self.s.timeout_seconds, 1.0))
            raise LoyaltriUnavailable(f"Loyaltri is unreachable: timed out after {self.s.timeout_seconds:g}s (simulated)")
        if failure == "server_error":
            raise LoyaltriUnavailable("Loyaltri is unreachable: HTTP 503 (simulated)")
        if failure == "auth_error":
            raise LoyaltriAuthError("Loyaltri rejected the credentials (HTTP 401, simulated)")

        path = self.s.mock_file or MOCK_DATA_FILE
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise LoyaltriResponseError(f"Mock data file could not be read: {type(e).__name__}")
        records = data.get("employees") if isinstance(data, dict) else data
        if not isinstance(records, list):
            raise LoyaltriResponseError("Mock data file must contain a list of employees")
        size = max(1, self.s.page_size)
        for i in range(0, len(records), size):
            yield records[i:i + size]


def build_client(settings: LoyaltriSettings) -> LoyaltriClient:
    problems = settings.problems()
    if problems:
        raise LoyaltriNotConfigured("Loyaltri is not configured: " + "; ".join(problems))
    if settings.client_mode == "mock":
        return MockLoyaltriClient(settings)
    return HttpLoyaltriClient(settings)
