# Switrus User Management — Backend

FastAPI + SQL Server API for Switrus users, plus the optional Loyaltri HRMS sync worker.
The web UI is a separate project: `switrus-user-management-frontend`.

```
app/            routes: users, auth (browser sessions), health, Loyaltri admin, attendance (read-only)
core/           settings, request authentication, audit trail
services/       user repository, credential check, e-mail/OTP, loyaltri/ (sync)
database/       SQL Server access, migrations/, SQLite stand-in for dev/tests
models/         request models
tests/          pytest suite (SQLite + mocked Loyaltri)
docs/           API.md, LOYALTRI.md, ATTENDANCE.md
```

Switrus owns its users, sign-in and permissions. Loyaltri only supplies HR data through a separate sync worker; if Loyaltri is down, every Switrus feature keeps working (`tests/test_loyaltri_outage.py`).

## What changed compared with the original service

Kept: every existing endpoint and response shape (`/login`, `POST /users`, `/users/filter`, `/users/department/{id}`, `/users/by-empid`, password OTP), `X-API-Key` for integrations, the `/login` JWT signed with `SECRET_KEY`, port 8001.

Added:
- `GET /users`, `GET /users/stats`, `GET/PATCH /users/{emp_id}`, `PATCH /users/{emp_id}/status`, `PUT/DELETE /users/{emp_id}/leave` per `MISSING_APIS.md`.
- Browser sign-in: `POST /auth/login` (`EmpName` + `DeptPassword`; see "Sign-in credentials") sets an HttpOnly, SameSite=Strict session cookie and returns a CSRF token; `GET /auth/me`, `POST /auth/logout`. Same-origin check, per-request role and disabled re-check, sign-in throttling, server-side sign-out. The browser never receives the API key or JWT secret.
- Role-based access (`core/permissions.py`, see "Roles and access"): admin-only writes; managers and branch managers read only their own branch/department.
- Audit trail (`SW_USER_AUDIT` + `logs/audit.log`) for create, update, disable/enable, leave, sign-out and sync changes.
- Loyaltri integration with configurable adapter, mock, field-ownership policy, scheduler and run log. See `docs/LOYALTRI.md`.
- Read-only Attendance Muster from the attendance API, scoped like the user list and off until configured and authorized. See `docs/ATTENDANCE.md`.
- Health endpoints `/health` and `/health/ready`.

Fixed:
- **Disabled users could log in.** The 403 was raised inside a `try/except Exception` that swallowed it. `/login` now rejects disabled accounts (after the password check).
- **SQL injection in `/users/filter`**: `role` and `branch_id` were interpolated into the SQL. Now parameterized; the documented comma-separated role list now works.
- Duplicate `EmpID` on `POST /users` now returns `409 "Employee ID already exists"` instead of a database error.
- `POST /users` requires an admin when called from a browser session (integrations unchanged).
- **Departments were always `null`.** `SW_USER.DepartmentID` is `nvarchar(50)` (SALES, VISA, ...) but was converted to an integer. It is now returned as text everywhere, and the department filter is case-insensitive.
- **Edit, disable and leave failed for most users.** 201 of 206 real EMPIDs contain `/` (`TEMP/107`), which the server decodes before routing, so `/users/{emp_id}` never matched them. The routes now accept slashes.
- **Swagger offered `?api_key=`**, putting the key in URLs (logs, history). It is no longer in the API docs, is redacted from access logs, every use is logged by key fingerprint, and `ALLOW_API_KEY_QUERY=false` turns it off.
- Database driver messages are no longer returned to clients (`500 "Database error"`); they are logged.
- Stored passwords that are not Argon2 hashes are never compared as text.

Behaviour notes for integrations:
- `/users/filter` now matches roles case-insensitively and trims them, and accepts `role=ADMIN,BPO`.
- DB settings can use `DB_SERVER/DB_NAME/DB_USER/DB_PASSWORD` (the old `SERVER/DATABASE/USER/PASSWORD` still work; `USER` clashes with a shell variable on Linux).
- `SESSION_SECRET` is now required at startup.
- `department_id` in `/users/filter` and `/users/by-empid` is now the stored text (it was `null` for every real department).
- A browser request with no credential gets `401 "Not signed in"`; server-to-server callers still get `403 "Invalid or missing API Key"`.

## How the UI connects

The UI's nginx (or Vite in development) proxies `/api/*` to this API and forwards the browser's `Host` header. The API accepts a session cookie only when the request's `Origin` matches that host, or is listed in `SESSION_ALLOWED_ORIGINS`. If a proxy between the UI and the API rewrites `Host`, add the UI's origin (e.g. `https://users.switrus.com`) to `SESSION_ALLOWED_ORIGINS`.

## Database

Run once on the Switrus database (additive and idempotent; `SW_USER` is not modified):

```
sqlcmd -S <server> -d <database> -U <user> -i database/migrations/001_audit_and_loyaltri.sql
```

Without it the API still works: audit goes to `logs/audit.log` only, sign-out revocation is per process, and the Loyaltri page says the tables are missing.

Checked against the real schema: leave uses `suspend_start`/`suspend_end` (`date`); `IsDisabled` is a nullable `bit` (NULL counts as enabled); `MANAGER_ID`, `DeptEmail`, `Phone`, `Email` exist; text columns are `nvarchar(50)`. Leave does not block login.

## Roles and access

| Role (`SW_USER.Roles`, any case) | Setting | Can change users | Sees (users, counts, every lookup) |
|---|---|---|---|
| ADMIN | `ADMIN_ROLES` | yes: create, edit, enable/disable, leave, Loyaltri | everyone |
| MANAGER | `UI_BRANCH_DEPARTMENT_ROLES` | no (403) | own `BRANCH_ID` **and** own `DepartmentID` |
| BRANCHMANAGER | `UI_BRANCH_ROLES` | no (403) | own `BRANCH_ID`, all departments |
| (none by default) | `UI_VIEWER_ROLES` | no | everyone |
| anything else (CRE, Executive, VISA HEAD, ...) | | cannot sign in (403) | nothing |
| Integration (`X-API-Key`) | | yes, as before | everyone (CRM and accounting look up across branches) |

- The scope is read from the signed-in employee's own SW_USER row on every request and added to the SQL `WHERE` clause of `/users`, `/users/stats`, `/users/{emp_id}`, `/users/filter`, `/users/department/{id}` and `/users/by-empid`. Query parameters can only narrow it. Users outside the scope read as `404`.
- A manager or branch manager whose own branch (or, for MANAGER, department) is empty, a placeholder (`string`) or a role name (one real row has `BRANCH_ID = 'Manager'`) gets `403` and sees nothing. Data errors never widen access.
- `IsDisabled` NULL means enabled (6 real rows); 1/true means disabled and cannot sign in.
- Measured on the production database: every Manager and BRANCHMANAGER has a usable branch and department.

## Sign-in credentials

| Endpoint | User name | Password column | How it is checked |
|---|---|---|---|
| `POST /auth/login` (User Management UI) | `EmpName` (trimmed, case-insensitive) | `DeptPassword` | Legacy plaintext, compared exactly in constant time (`hmac.compare_digest`). An Argon2 value in the column is verified as a hash. `EmpPassword` is never read. |
| `POST /login` (CRM, accounting, other integrations) | `EmpName` | `EmpPassword` | Argon2 with `PASSWORD_PEPPER`, unchanged. Plaintext values never match. |

`POST /auth/login` refuses, with the same `401 "Incorrect user name or password"`: unknown names, names that match more than one account, a NULL or blank `DeptPassword`, and wrong passwords. A disabled account gets `403` (only after the password matched), and a role outside `ADMIN_ROLES`/`UI_VIEWER_ROLES` gets `403`. `DeptPassword` is never returned by any endpoint and never logged.

```bash
python -m database.password_report     # read-only: counts and affected accounts, never the values
```

### Legacy risk

`DeptPassword` is plaintext and, in practice, shared between many accounts, including administrator accounts. Because the user name is just the employee's name, anyone who knows a shared value and an admin's name can sign in as that admin with full write access, and the audit trail will name the admin. Anyone who can read `SW_USER` (database logins, backups, the `POST /users` caller) can read every password. Run `python -m database.password_report` (read-only, never prints values) to measure the current exposure.

### Moving off DeptPassword

Do this per user; nothing here runs automatically and no production password is changed by this code.

1. **Admins first.** Give each admin an individual password nobody else knows, and store it in `DeptPassword` as an Argon2 hash (`python -c "from services.email import pwd_context; print(pwd_context.hash(input()))"`, run by the admin). `POST /auth/login` already verifies hashed values, so this needs no deploy, and the shared plaintext stops working for that account.
2. **Restrict who can use the UI** while step 1 is in progress: `ADMIN_ROLES` and `UI_VIEWER_ROLES` decide who may sign in at all.
3. **Everyone else** gets an individual hashed password the same way (or a reset flow writing an Argon2 hash).
4. **Target:** a dedicated credential column (for example `UiPasswordHash`), or reuse the Argon2 `EmpPassword` if the business agrees UI and integration sign-in may share it. Then switch `authenticate_ui_credentials` to that column only, and stop storing plaintext `DeptPassword` (it may be a mailbox password for `DeptEmail`; confirm what else uses it before changing it).
5. Rotate every value that was shared, since it has been readable in the table.

## Credential rotation

The database password, SMTP app password and `SECRET_KEY` have been handled in plain text (in chat and in `.env`), so rotate them in a coordinated way:

1. **Integration key.** Set `API_KEYS=<old>,<new>` (old = current `SECRET_KEY`), restart, give `<new>` to CRM/accounting and have them send it in the `X-API-Key` header. Watch `logs/app.log` for `Deprecated ?api_key=` warnings to find callers still using the URL. When every caller uses `<new>` in the header, set `API_KEYS=<new>` and `ALLOW_API_KEY_QUERY=false`.
2. **`SECRET_KEY`** signs `/login` JWTs that other systems may verify. Agree a cut-over time with those systems, then change it everywhere at once (open `/login` tokens expire after 2 hours).
3. **`SESSION_SECRET`.** Change any time; it signs everyone out of the UI.
4. **SQL Server login.** Create the new password (or a new least-privilege login for this API), update `DB_PASSWORD`, restart, then disable the old one.
5. **SMTP.** Revoke the Gmail app password and create a new one; update `SMTP_PASSWORD`.

Never commit `.env`. Logs never contain passwords, API keys, cookies or tokens.

## Run locally (no SQL Server needed)

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate on Linux/macOS
pip install -r requirements-dev.txt

export DB_ENGINE=sqlite SQLITE_PATH=dev.sqlite3          # PowerShell: $env:DB_ENGINE="sqlite"; ...
export SECRET_KEY=dev-integration-key-change-me-0123456789
export SESSION_SECRET=dev-session-secret-change-me-0123456789
export SESSION_COOKIE_SECURE=false LOYALTRI_CLIENT=mock
python -m database.sqlite_dev --seed      # seeds user "admin" / "Admin@12345" (DeptPassword and EmpPassword)
uvicorn main:app --reload --port 8001
```

Against the real SQL Server, copy `.env.example` to `.env`, fill it in, and leave `DB_ENGINE` unset.

API reference: `docs/API.md`, or `http://localhost:8001/docs` while running.

## Tests

```bash
python -m pytest        # 255 tests, SQLite + mocked Loyaltri and attendance, no credentials needed
```

## Deploy (Docker)

```bash
cp .env.example .env    # fill in DB, SECRET_KEY, SESSION_SECRET, Loyaltri
docker compose up -d --build
```

| Service | Port | Purpose |
|---|---|---|
| `api` | 8001 | API for the UI and existing integrations (`LOYALTRI_SCHEDULER=external`) |
| `sync-worker` | – | Loyaltri syncs, isolated from the API |

Production checklist:
- TLS in front of the API wherever it is reached over a network; keep `SESSION_COOKIE_SECURE=true`.
- `SESSION_SECRET` long and random, different from `SECRET_KEY`. Consider moving integrations to a new `API_KEYS` value (rotate with `old,new`).
- Run the migration.
- Allow port 8001 only from the UI's server and the integration networks.
- Logs (`./logs/app.log`, `./logs/audit.log`) rotate at 10 MB.
- Scale `api` freely; run one `sync-worker` (extras are safe but idle because of the lease).
