# Switrus User Management API

Base URL: the API service (port `8001`). The browser UI reaches the same API under `/api` on its own origin.

## Authentication

| Caller | How | Access |
|---|---|---|
| Trusted integration (CRM, accounting, scripts) | `X-API-Key: <key>` header. The legacy `?api_key=` query parameter still works while `ALLOW_API_KEY_QUERY=true` (the default, for migration), is not shown in `/docs`, is redacted from access logs, and every use is logged with the key's fingerprint so callers can be found and moved to the header. | Everything, as before. Keys are `API_KEYS`, defaulting to `SECRET_KEY`. |
| Person using the UI | `POST /auth/login` with the Switrus user name (`EmpName`) and the `DeptPassword` (legacy plaintext, compared in constant time; `EmpPassword` is not used) sets an HttpOnly, `SameSite=Strict` `sw_session` cookie (`Secure` unless `SESSION_COOKIE_SECURE=false`) and returns a CSRF token. Every POST, PUT, PATCH and DELETE must send it in `X-CSRF-Token`; `GET /auth/me` returns it again after a page reload. | Write: `ADMIN_ROLES` only. Read: ADMIN everyone; MANAGER own branch + department; BRANCHMANAGER own branch; `UI_VIEWER_ROLES` everyone; other roles cannot sign in (README, "Roles and access"). Every user read, including the counts and the older lookups, is limited to that scope in SQL; users outside it are `404`. Role and disabled flag are re-checked on every request. Sessions expire after `SESSION_TTL_MINUTES`; sign-out revokes them server-side. |

No credential: browsers (requests with `Sec-Fetch-*` headers) and `GET /auth/me` get `401 {"detail": "Not signed in"}`; other callers get `403 {"detail": "Invalid or missing API Key"}` (unchanged). Expired/revoked session: `401`. Non-admin session on a write: `403 {"detail": "Administrator role required"}`. Missing CSRF token: `403`. Cross-site `Origin`: `403`. `?api_key=` with `ALLOW_API_KEY_QUERY=false`: `403`.

`UI_AUTH_DISABLED=true` (development only, refused with `APP_ENV=production`) makes browser requests without a session act as `UI_DEFAULT_EMPID`. Leave it off; the UI signs in normally.

Public (no credential): `GET /health`, `GET /health/ready`, `POST /auth/login`, `POST /auth/logout`, `/docs`.

Errors use FastAPI's shape: `{"detail": "message"}`; validation errors (`422`) return `detail` as a list of `{loc, msg, type}`. Database failures return `500 {"detail": "Database error"}`; the driver message is only logged.

Employee IDs contain slashes (`SHPL/DEL/0417/2026`). In paths, send them URL-encoded (`/users/SHPL%2FDEL%2F0417%2F2026`); unencoded slashes work too.

## The user object

```json
{
  "emp_id": "SHPL/DEL/0417/2026", "emp_name": "AAS MOHAMMED", "department_id": "SALES", "role": "CRE",
  "branch_id": "B003", "email": "aas@switrus.com", "phone": "9876543210",
  "manager_id": "TEMP/107", "dept_email": null, "is_disabled": false,
  "leave_start": "2026-11-03", "leave_end": "2026-11-07", "is_suspended": 0
}
```

`department_id` is text, exactly as stored in `SW_USER.DepartmentID` (`nvarchar(50)`: SALES, VISA, Operations, ...). It is also text in `/users/filter`, `/users/by-empid` and `/users/department/{id}`; previously those returned `null` for every non-numeric department. `is_suspended` is `1` when today (IST) is inside the leave window. Password and OTP columns are never returned.

## Users

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/users` | any | Query: `include_disabled` (default false), `search` (name, ID or email, contains, case-insensitive), `branch_id`, `department_id` (case-insensitive), `role` (case-insensitive). Ordered by name. Empty result = `[]`. |
| GET | `/users/stats` | any | `{"total", "active", "disabled", "on_leave"}`; `on_leave` counts active users only. |
| GET | `/users/{emp_id}` | any | Includes disabled users. `404 {"detail": "No user found with EMPID E9999"}` |
| POST | `/users` | admin | Existing contract (see below). |
| PATCH | `/users/{emp_id}` | admin | Partial update, returns the user. |
| PATCH | `/users/{emp_id}/status` | admin | `{"is_disabled": true|false}`, idempotent. |
| PUT | `/users/{emp_id}/leave` | admin | `{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}`, inclusive, replaces any existing window. |
| DELETE | `/users/{emp_id}/leave` | admin | Clears leave, idempotent. |

Existing endpoints unchanged in shape: `/users/filter`, `/users/department/{id}`, `/users/by-empid` (all still hide disabled users), `/login`, `/password/send-otp`, `/password/verify-otp`, `/password`.

### POST /users

```json
{"EmpID": "E2001", "EmpName": "Meera Pillai", "EmpPassword": "Str0ng!Pass", "Email": "meera@switrus.com",
 "DepartmentID": "VISA", "Roles": "BPO", "BRANCH_ID": "BR02", "Phone": null, "DeptEmail": null, "DeptPassword": null, "IsDisabled": 0}
```

`201 {"message": "User Meera Pillai created successfully"}` · `400` password policy or blank required field · `409 "User already exists"` (name) · `409 "Employee ID already exists"` · `422` missing field or a text field longer than its column (50 characters).

### PATCH /users/{emp_id}

Editable: `EmpName`, `Email`, `DepartmentID`, `Roles` (cannot be blanked) and `BRANCH_ID`, `Phone`, `MANAGER_ID`, `DeptEmail` (`null` or `""` clears). Omitted fields are untouched. Any other field (`EmpPassword`, `IsDisabled`, `EmpID`, ...) is a `422`.

| Status | Detail |
|---|---|
| 400 | `No fields to update` · `Email cannot be empty` · `Manager E7777 does not exist` · `A user cannot be their own manager` |
| 404 | `No user found with EMPID ...` |
| 409 | `User name already exists` |
| 422 | invalid email/phone format, wrong types, unknown field |

### PATCH /users/{emp_id}/status

`400 "You cannot disable your own account"` for a session user targeting themselves. Disabled users cannot log in (`/login` returns `403 "User account is disabled"`) and disappear from the existing lookup endpoints.

### Leave

`400 "end_date cannot be before start_date"`; `422` for missing or non-ISO dates. Past dates are allowed.

## Browser session

| Method | Path | Body / result |
|---|---|---|
| POST | `/auth/login` | `{"username": "<EmpName>", "password": "..."}` → sets the session cookie, returns `{"user": {"emp_id", "emp_name", "role", "branch_id", "department_id"}, "permissions": {"is_admin", "can_edit", "scope": "all"|"branch"|"branch_department", "scope_branch_id", "scope_department_id"}, "csrf_token": "..."}`. `401 "Incorrect user name or password"` (also for an unknown name, a name matching several accounts, or no `DeptPassword`) · `403 "User account is disabled"` · `403 "Your role does not have access to User Management"` · `403 "Your account has no valid branch|department assigned ..."` · `403 "Cross-site request rejected"` · `429` after `LOGIN_MAX_FAILURES` wrong passwords for one name from one address within `LOGIN_FAILURE_WINDOW_SECONDS` (with `Retry-After`). |
| GET | `/auth/me` | Same body as sign-in, for the current session. `401` when signed out. |
| POST | `/auth/logout` | Revokes the session and clears the cookie. |

`/auth/login` checks `DeptPassword`; `/login` (integrations) checks the Argon2 `EmpPassword`, unchanged. See the README, "Sign-in credentials", for the legacy risk and the migration plan.

## Loyaltri sync (admin)

| Method | Path | Notes |
|---|---|---|
| GET | `/integrations/loyaltri/status` | configured?, problems, safe config summary (no credential), worker heartbeat, last success, last attempt, active run, next scheduled run |
| POST | `/integrations/loyaltri/sync` | Queues a run (`202`); returns the existing run (`200`, `already_active: true`) if one is queued/running; `409` if not configured; `503` if tables missing |
| GET | `/integrations/loyaltri/runs?limit=20` | History, newest first |
| GET | `/integrations/loyaltri/runs/{run_id}` | Run plus per-record `items` |

## Health

`GET /health` → `{"status": "ok"}` (process up). `GET /health/ready` → database reachable, else `503`. Neither checks Loyaltri.

## Examples

```bash
# Integration (unchanged; always use the header, never ?api_key=)
curl -H "X-API-Key: $API_KEY" "http://localhost:8001/users?include_disabled=true&search=asha"
curl -H "X-API-Key: $API_KEY" -X PATCH -H 'Content-Type: application/json' \
     -d '{"Phone": "9000000000", "MANAGER_ID": "E1000"}' http://localhost:8001/users/E1001
curl -H "X-API-Key: $API_KEY" -X PATCH -H 'Content-Type: application/json' \
     -d '{"is_disabled": true}' http://localhost:8001/users/E1001/status
curl -H "X-API-Key: $API_KEY" -X PUT -H 'Content-Type: application/json' \
     -d '{"start_date": "2026-11-03", "end_date": "2026-11-07"}' http://localhost:8001/users/E1001/leave
curl -H "X-API-Key: $API_KEY" -X DELETE http://localhost:8001/users/E1001/leave
```

Interactive docs: `http://localhost:8001/docs`.
