# Loyaltri HRMS integration

Loyaltri is an **external data source**. Switrus owns its users, authentication and authorization.

```
Loyaltri API  ->  sync worker (separate process)  ->  Switrus DB  ->  Switrus API  ->  UI
```

- No API request (login, listing, editing, disabling, leave, health checks) calls Loyaltri. A test enforces this (`tests/test_loyaltri_outage.py`).
- Syncs run in the sync worker: the `sync-worker` container, or a background thread when `LOYALTRI_SCHEDULER=embedded`. A hanging Loyaltri cannot slow the API.
- A failed sync is recorded in the sync log and changes nothing.

## Status: what is still needed from Loyaltri

The Loyaltri API contract has **not** been supplied, so no endpoint, auth scheme or field name has been assumed. The HTTP adapter is generic and every detail is configuration. To connect, obtain from Loyaltri:

| Needed | Setting |
|---|---|
| API base URL (HTTPS) | `LOYALTRI_BASE_URL` |
| Path of the "list employees" endpoint | `LOYALTRI_EMPLOYEES_PATH` |
| Authentication: header name, scheme, credential (and whether tokens expire / need an OAuth exchange) | `LOYALTRI_AUTH_HEADER`, `LOYALTRI_AUTH_SCHEME`, `LOYALTRI_API_KEY` |
| Fixed query parameters (company/tenant id, active-only filter) | `LOYALTRI_EXTRA_QUERY` (JSON) |
| Where the employee list sits in the response | `LOYALTRI_RECORDS_PATH` (e.g. `data.items`; empty if the body is a list) |
| Pagination style and parameter names | `LOYALTRI_PAGINATION`, `LOYALTRI_PAGE_PARAM`, `LOYALTRI_PAGE_SIZE_PARAM`, `LOYALTRI_CURSOR_PARAM`, `LOYALTRI_NEXT_CURSOR_PATH` |
| Rate limits | tune `LOYALTRI_PAGE_SIZE`, `LOYALTRI_SYNC_INTERVAL_MINUTES` (429 + `Retry-After` is honoured) |
| **Which Loyaltri field equals the Switrus EMPID**, and that it is stable | `LOYALTRI_FIELD_MAP.emp_id` |
| Field names for name, email, phone, department, branch, manager | `LOYALTRI_FIELD_MAP` |
| How Loyaltri department/branch codes relate to Switrus `DepartmentID` / `BRANCH_ID` | decide whether to sync them at all (a translation table is not built) |
| A sandbox account for testing | — |

If OAuth client-credentials or signed requests are required, add a small auth step to `HttpLoyaltriClient.__init__`/`_get` in `services/loyaltri/loyaltri_client.py`. Nothing else changes.

Example once the contract is known (illustrative values only):

```
LOYALTRI_CLIENT=http
LOYALTRI_BASE_URL=https://<tenant>.loyaltri.example
LOYALTRI_EMPLOYEES_PATH=/<path from docs>
LOYALTRI_AUTH_HEADER=Authorization
LOYALTRI_AUTH_SCHEME=Bearer
LOYALTRI_API_KEY=<secret>
LOYALTRI_RECORDS_PATH=data
LOYALTRI_PAGINATION=page
LOYALTRI_PAGE_PARAM=page
LOYALTRI_PAGE_SIZE_PARAM=limit
LOYALTRI_FIELD_MAP={"emp_id":"<code field>","emp_name":"<name field>","email":"<email field>","phone":"<phone field>","manager_id":"<manager code field>"}
```

Run one sync from the command line to verify: `python -m services.loyaltri.loyaltri_scheduler --once`.

## Field ownership policy

| Category | Default | Behaviour |
|---|---|---|
| Owned by Loyaltri | `phone`, `manager_id` | A change in Loyaltri is copied to Switrus. If an admin changed the same field in Switrus since the last sync, the Switrus value is **kept** and the record is reported as "kept local value" (3-way check against a stored baseline). `LOYALTRI_OVERWRITE_LOCAL_CHANGES=true` lets Loyaltri win. On the very first sync there is no baseline, so owned fields are taken from Loyaltri. |
| Fill if empty | `email`, `department_id`, `branch_id` | Written only when Switrus has no value. |
| Never synced (hard-coded) | EMPID, passwords, OTP, `Roles`, `IsDisabled`, leave dates, `DeptEmail` | Not configurable. |

Allowed values for the lists: `emp_name, email, phone, department_id, branch_id, manager_id`. Syncing `emp_name` is possible but not recommended: it is the sign-in user name.

Other guarantees:

- Matching is by employee ID only (case-insensitive). Names are never used to match.
- A blank or missing value in Loyaltri never clears a Switrus value.
- Employees absent from Loyaltri are left untouched: never deleted or disabled. Loyaltri's employment status is read but not acted on; deciding to disable leavers automatically is a business rule that has not been agreed.
- A manager ID that is not a Switrus user is ignored for that record.
- Duplicate employee IDs in one Loyaltri response: those records are rejected.
- Loyaltri is fetched completely before anything is written; a failure mid-pagination writes nothing.
- Records are applied one by one; a failure on one does not stop the others (run status `partial`).
- A second run with identical data writes nothing.
- Every change is written to the audit trail with actor `loyaltri-sync` and the run id.

### New employees

`LOYALTRI_CREATE_MODE=off` (default): reported in the run as "Not in Switrus", not created.
`LOYALTRI_CREATE_MODE=disabled`: created **disabled**, with role `LOYALTRI_DEFAULT_ROLE` and an unusable random password. An admin reviews and enables them; the employee sets a password through the existing OTP reset. Needs name, email and department; a name already used by another user is reported as a conflict.

## Reliability

- Timeout per request (`LOYALTRI_TIMEOUT_SECONDS`), bounded retries with exponential backoff on timeouts, connection errors, 429 and 5xx. 401/403 and other 4xx are not retried.
- One sync at a time across all processes and containers: a lease row in `LOYALTRI_SYNC_LOCK` (atomic conditional update). A run left `running` by a crashed worker is closed as failed by the next worker that takes the lease.
- `LOYALTRI_MAX_PAGES` and a repeated-cursor check stop runaway pagination.
- The credential is read from the environment, sent only in the configured header, and never returned by the status endpoint or written to logs.

## Monitoring

The admin page **Loyaltri sync** (and `GET /integrations/loyaltri/status`) shows: configuration problems, whether a worker is alive (heartbeat), last successful sync, last attempt with counts and error, next scheduled run, history and per-record results. Tables come from `database/migrations/001_audit_and_loyaltri.sql`.

## Local development

`LOYALTRI_CLIENT=mock` reads `services/loyaltri/mock_employees.json` (its field names are placeholders for the mock, not Loyaltri's). Simulate failures with `LOYALTRI_MOCK_FAILURE=outage|timeout|server_error|auth_error`.
