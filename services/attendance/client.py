"""Attendance Muster clients.

* HttpAttendanceClient - GET <ATTENDANCE_API_URL>?fromDate=..&toDate=.. with
  the key in a header (never the URL). Bounded timeout, one retry on network
  errors and 5xx/429 (the call is a read, so retrying is safe).
* MockAttendanceClient - development and tests. Generates a muster for any
  range in the documented response shape, and can simulate failures. Its
  employees and field names are fixtures, not real Switrus data.

Error messages are shown to signed-in users, so they never contain the key,
the URL or the upstream response body.
"""
import logging
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

import httpx

from services.attendance.config import AttendanceSettings

logger = logging.getLogger(__name__)


class AttendanceError(Exception):
    """Base class. `status` is the HTTP status our API answers with."""
    status = 502


class AttendanceNotConfigured(AttendanceError):
    status = 503


class AttendanceUnavailable(AttendanceError):
    status = 503


class AttendanceTimeout(AttendanceError):
    status = 504


class AttendanceAuthError(AttendanceError):
    status = 502


class AttendanceResponseError(AttendanceError):
    """The service answered, but not with a usable muster."""
    status = 502


class AttendanceClient:
    def fetch(self, from_date: date, to_date: date) -> Any:  # pragma: no cover - interface
        raise NotImplementedError


class HttpAttendanceClient(AttendanceClient):
    def __init__(self, settings: AttendanceSettings, transport: Optional[httpx.BaseTransport] = None,
                 sleep: Callable[[float], None] = time.sleep):
        problems = settings.problems()
        if problems:
            raise AttendanceNotConfigured("; ".join(problems))
        self.s = settings
        self._sleep = sleep
        self._transport = transport

    def fetch(self, from_date: date, to_date: date) -> Any:
        params = {"fromDate": from_date.isoformat(), "toDate": to_date.isoformat()}
        headers = {"Accept": "application/json", self.s.api_key_header: self.s.api_key}
        attempts = self.s.max_retries + 1
        last: AttendanceError = AttendanceUnavailable("The attendance service is unavailable. Try again later.")
        with httpx.Client(timeout=httpx.Timeout(self.s.timeout_seconds), transport=self._transport,
                          follow_redirects=False) as http:
            for attempt in range(attempts):
                try:
                    resp = http.get(self.s.api_url, params=params, headers=headers)
                except httpx.TimeoutException:
                    last = AttendanceTimeout(
                        f"The attendance service did not answer within {self.s.timeout_seconds:g} seconds.")
                except httpx.TransportError as e:
                    logger.warning("Attendance service connection failed: %s", type(e).__name__)
                    last = AttendanceUnavailable("The attendance service cannot be reached. Try again later.")
                else:
                    if resp.status_code in (401, 403):
                        logger.error("Attendance service rejected the API key (HTTP %s)", resp.status_code)
                        raise AttendanceAuthError(
                            "The attendance service rejected Switrus's credentials. An administrator must check the API key.")
                    if resp.status_code == 429 or resp.status_code >= 500:
                        logger.warning("Attendance service answered HTTP %s", resp.status_code)
                        last = AttendanceUnavailable("The attendance service is unavailable. Try again later.")
                    elif resp.status_code >= 400:
                        logger.error("Attendance service answered HTTP %s", resp.status_code)
                        raise AttendanceResponseError(
                            f"The attendance service refused the request (HTTP {resp.status_code}).")
                    else:
                        try:
                            return resp.json()
                        except ValueError:
                            raise AttendanceResponseError("The attendance service sent a response that is not JSON.")
                if attempt < attempts - 1:
                    self._sleep(min(2.0 * (attempt + 1), 5.0))
        raise last


# Fixture employees for the mock. Codes default to the SQLite dev seed so the
# mapping can be exercised locally (ATTENDANCE_MOCK_EMPLOYEE_CODES overrides).
MOCK_UNMATCHED_CODE = "MOCK-NOT-IN-SWITRUS"


class MockAttendanceClient(AttendanceClient):
    def __init__(self, settings: AttendanceSettings, codes: Optional[List[str]] = None,
                 sleep: Callable[[float], None] = time.sleep):
        import os
        self.s = settings
        self._sleep = sleep
        raw = os.getenv("ATTENDANCE_MOCK_EMPLOYEE_CODES") or "E1000,E1001,E1002,E1003"
        self.codes = codes if codes is not None else [c.strip() for c in raw.split(",") if c.strip()]

    @staticmethod
    def _day(code_index: int, d: date) -> Dict[str, Any]:
        if d.weekday() == 6:
            return {"status": "Off Day"}
        cycle = (d.toordinal() + code_index) % 11
        if cycle == 3:
            return {"status": "Absent"}
        if cycle == 5:
            return {"status": "Half Day", "inTime": "09:30", "outTime": "13:45", "workHours": "04:15", "overtime": "00:00"}
        if cycle == 7:
            return {"status": "Casual Leave"}
        if cycle == 9:
            # A state this module has no category for: shown as returned, counted as "other".
            return {"status": "Work From Home", "workHours": "08:00", "overtime": "00:00"}
        ot = "01:00" if cycle == 1 else "00:00"
        return {"status": "Present", "inTime": "09:05", "outTime": "18:20", "workHours": "08:15", "overtime": ot}

    def fetch(self, from_date: date, to_date: date) -> Any:
        failure = self.s.mock_failure
        if failure == "outage":
            raise AttendanceUnavailable("The attendance service cannot be reached. Try again later. (simulated)")
        if failure == "timeout":
            raise AttendanceTimeout(f"The attendance service did not answer within {self.s.timeout_seconds:g} seconds. (simulated)")
        if failure == "server_error":
            raise AttendanceUnavailable("The attendance service is unavailable. Try again later. (simulated)")
        if failure == "auth_error":
            raise AttendanceAuthError("The attendance service rejected Switrus's credentials. (simulated)")
        if failure == "malformed":
            return {"success": True, "data": {"attendance": "not-a-list"}}
        if failure == "not_success":
            return {"success": False, "message": "simulated failure"}

        days = [from_date + timedelta(n) for n in range((to_date - from_date).days + 1)]
        records = []
        for i, code in enumerate(self.codes + [MOCK_UNMATCHED_CODE]):
            details = {d.isoformat(): self._day(i, d) for d in days}
            records.append({
                "employeeId": 5000 + i,  # the service's own numeric id; not a Switrus EMPID
                "employeeDetails": {
                    "employeeCode": code,
                    "employeeName": f"Mock Employee {i + 1}",
                    "designation": "Fixture",
                    # Payroll fields the real payload may carry; they must never reach the UI.
                    "grossSalary": 45000 + i,
                    "bankAccountNumber": "000000000000",
                },
                "attendanceDetails": details,
                "attendanceSummary": {
                    "presentDays": sum(1 for v in details.values() if v["status"] == "Present"),
                    "absentDays": sum(1 for v in details.values() if v["status"] == "Absent"),
                    "grossSalary": 45000 + i,
                },
            })
        return {
            "success": True,
            "data": {"companyId": 1, "fromDate": from_date.isoformat(), "toDate": to_date.isoformat(),
                     "attendance": records},
        }


def build_client(settings: AttendanceSettings) -> AttendanceClient:
    problems = settings.problems()
    if problems:
        raise AttendanceNotConfigured("; ".join(problems))
    if settings.client_mode == "mock":
        return MockAttendanceClient(settings)
    return HttpAttendanceClient(settings)
