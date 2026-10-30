# Attendance Muster (read-only)

The User Management UI can show attendance from the Switrus attendance system. The backend calls the
attendance API; the browser only ever talks to this backend. The integration is **read-only**: it never
writes to SW_USER or any other table, and it is unrelated to the Loyaltri employee sync.

```
browser ──session cookie──▶ GET /attendance/muster ──x-api-key──▶ attendance API
                                   │
                                   └─ SELECT EMPID, EmpName, BRANCH_ID, DepartmentID, IsDisabled
                                      FROM SW_USER  (limited to the caller's scope)
```

## Endpoints

| Endpoint | Who | What |
|---|---|---|
| `GET /attendance/access` | any signed-in user | `{allowed, reason, configured, mapping_verified, max_range_days}`; admins also get `problems` and a safe config summary. Never calls the attendance API. |
| `GET /attendance/muster?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD` | see *Access* | The muster for the range, limited to the caller's employees. |

Browser sessions only. Requests with `X-API-Key` (integrations) are refused with 403.

`from_date` and `to_date` are required, `YYYY-MM-DD`, inclusive, `to_date >= from_date`, at most
`ATTENDANCE_MAX_RANGE_DAYS` days. Missing → 422; wrong format, impossible date, reversed or too long → 400.

### Response (abridged)

```json
{
  "from_date": "2026-10-01", "to_date": "2026-10-03",
  "dates": ["2026-10-01", "2026-10-02", "2026-10-03"],
  "access": {"scope": "branch_department", "branch_id": "B008", "department_id": "SALES"},
  "mapping_verified": true,
  "employees": [{
    "emp_id": "SHPL/AMD/0426/2026", "emp_name": "…", "branch_id": "B008", "department_id": "SALES",
    "is_disabled": false, "matched": true,
    "days": {"2026-10-01": {"status": "Present", "category": "present", "in_time": "09:00",
                            "out_time": "18:00", "work_minutes": 510, "overtime_minutes": 75}},
    "summary": {"present": 1, "absent": 0, "off_day": 0, "half_day": 0, "leave": 0, "other": 0,
                "days_recorded": 1, "work_minutes": 510, "overtime_minutes": 75},
    "source_summary": {"presentDays": 1}
  }],
  "totals": {"employees": 1, "present": 1, "…": 0},
  "states": [{"label": "Present", "category": "present", "count": 1}],
  "warnings": [],
  "mapping": {"upstream_records": 4, "matched": 3, "unmatched": 1, "missing_code": 0,
              "duplicate_code_records": 0, "show_unmatched": false}
}
```

`mapping` is returned to administrators only. Name, branch and department come from **SW_USER**, not
from the attendance system.

## Access

| Role | Sees | Condition |
|---|---|---|
| ADMIN | `ATTENDANCE_ADMIN_SCOPE=matched`: matched employees, every branch. `=all`: also records that match no Switrus user (flagged "Not in Switrus") | `none` (default) refuses: company-wide attendance must be authorized explicitly |
| MANAGER | matched employees in own branch **and** department | only when `ATTENDANCE_MAPPING_VERIFIED=true` |
| BRANCHMANAGER | matched employees in own branch | only when `ATTENDANCE_MAPPING_VERIFIED=true` |
| anything else (incl. read-only viewer roles, integrations) | nothing (403) | |

Roles, branch and department are the ones core/permissions.py already uses, re-read from SW_USER on each
request. The scope is applied in the SQL that loads SW_USER, so an attendance record for someone outside
the caller's area is never matched and never returned. Query parameters cannot widen it.

## Employee mapping

* The attendance record's `employeeDetails.employeeCode` is compared with `SW_USER.EMPID`, trimmed and
  case-insensitive.
* The numeric `employeeId` is the attendance system's own id and is **never** treated as a Switrus ID.
* A code carried by more than one attendance record is ambiguous: none of those records is shown.
* Records with no code, or a code that is not in SW_USER, are not shown to scoped roles.

**This mapping has not been verified on real data.** Until someone confirms on a sample of real records
that `employeeCode` holds the SW_USER EMPID (same format, e.g. `SHPL/AMD/0426/2026`), keep
`ATTENDANCE_MAPPING_VERIFIED=false`: managers and branch managers then see a "not available yet" message.
Administrators can check the match counts on the page (`matched` / `unmatched`) with
`ATTENDANCE_ADMIN_SCOPE=matched` first.

## What is read from the attendance response

Allowlist only. From each record: `employeeDetails.employeeCode`, a name (`employeeName`, `fullName`,
`name`, or `firstName`/`lastName`; shown only for unmatched records), `attendanceDetails`, and the numeric
entries of `attendanceSummary` whose key is not payroll-like (any key containing words such as salary,
gross, net, pay, bank, account, PF, ESI, tax, CTC is dropped). `grossSalary`, bank details and every other
`employeeDetails` field are never read, so they cannot appear in a response or a log.

`attendanceDetails` is keyed by date. Accepted keys: `YYYY-MM-DD` (optionally with a time),
`DD-MM-YYYY`, `DD/MM/YYYY`. Entries outside the requested range are dropped; unreadable entries are dropped
and reported as a warning. A day value may be a status string or an object; the status is read from
`status` (or `attendanceStatus`, `attendance`, `dayStatus`, `statusName`, `type`), times from
`inTime`/`checkIn`/…, hours from `workHours`/`workingHours`/`totalHours`/… (`"08:30"` or `8.5`), overtime
from `overtime`/`otHours`/…

**The day object's field names are a best guess until a real response is available**; confirm them and
adjust `services/attendance/parser.py` (`STATUS_KEYS`, `IN_KEYS`, …) if needed.

Status labels are shown exactly as returned. For the counts they are grouped: Present / P → present,
Absent / A → absent, Off Day / Weekly Off / WO / Holiday → off day, Half Day / HD → half day,
anything containing "leave" and CL / SL / EL / PL / ML / LOP / LWP / CO → leave, everything else → other.

## Errors

| Situation | Status | Message (shown to the user) |
|---|---|---|
| Role not allowed / admin scope not authorized / mapping unverified | 403 | explains which |
| Not configured | 503 | "Attendance is not configured yet." (+ the missing settings, for admins) |
| Attendance API unreachable, 5xx, 429 (after `ATTENDANCE_MAX_RETRIES` retries) | 503 | unavailable, try later |
| Timeout | 504 | did not answer within N seconds |
| 401/403 from the attendance API | 502 | rejected Switrus's credentials |
| Other 4xx, not JSON, `success` not true, `data.attendance` not a list | 502 | readable description |

Messages never include the API key, the URL or the upstream body. The key is sent only in the
`ATTENDANCE_API_KEY_HEADER` header, never in a query string; `ATTENDANCE_API_URL` with a `?` is refused.

## Configuration (.env)

| Variable | Default | |
|---|---|---|
| `ATTENDANCE_CLIENT` | `disabled` | `disabled` \| `mock` \| `http` |
| `ATTENDANCE_API_URL` | | Full https URL of the muster endpoint, no query string |
| `ATTENDANCE_API_KEY` | | Sent as `x-api-key`. Server-side only; never in the UI, a URL or a log |
| `ATTENDANCE_API_KEY_HEADER` | `x-api-key` | |
| `ATTENDANCE_TIMEOUT_SECONDS` | `20` | |
| `ATTENDANCE_MAX_RETRIES` | `1` | network errors, 5xx and 429 only (0–3) |
| `ATTENDANCE_MAX_RANGE_DAYS` | `31` | 1–92 |
| `ATTENDANCE_CACHE_SECONDS` | `60` | the same range is fetched at most once per window (0 = off) |
| `ATTENDANCE_ADMIN_SCOPE` | `none` | `none` \| `matched` \| `all` |
| `ATTENDANCE_MAPPING_VERIFIED` | `false` | set `true` only after the employeeCode ↔ EMPID check |
| `ATTENDANCE_MOCK_EMPLOYEE_CODES` | `E1000,E1001,E1002,E1003` | mock only: which EMPIDs the fixture uses |
| `ATTENDANCE_MOCK_FAILURE` | `none` | mock only: `outage` `timeout` `server_error` `auth_error` `malformed` `not_success` |

Restart the API after changing these.

### Local development with the mock

```
DB_ENGINE=sqlite
ATTENDANCE_CLIENT=mock
ATTENDANCE_ADMIN_SCOPE=matched
ATTENDANCE_MAPPING_VERIFIED=true
```

The mock generates fixture records (in the documented shape, including fake payroll fields to prove they are
filtered out) for the SQLite seed users plus one code that matches nobody.

### Going live

1. Set `ATTENDANCE_CLIENT=http`, `ATTENDANCE_API_URL`, `ATTENDANCE_API_KEY`.
2. Set `ATTENDANCE_ADMIN_SCOPE=matched`, keep `ATTENDANCE_MAPPING_VERIFIED=false`.
3. As an admin, load a short range. Check the match counts and spot-check a few employees against HR.
   Confirm the day fields (status, times, hours) appear; if not, adjust the key lists in the parser.
4. Only then set `ATTENDANCE_MAPPING_VERIFIED=true` so managers and branch managers get access.
