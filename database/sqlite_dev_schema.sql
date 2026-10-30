-- SQLite approximation of the Switrus schema, for local development and tests.
-- SW_USER mirrors the columns the code uses; the real SQL Server table may
-- have more columns and different types.

CREATE TABLE SW_USER (
    EMPID             TEXT,
    EmpName           TEXT,
    EmpPassword       TEXT,
    Email             TEXT,
    DepartmentID      TEXT,      -- nvarchar(50) in SQL Server: SALES, VISA, ...
    Roles             TEXT,
    BRANCH_ID         TEXT,
    Phone             TEXT,
    MANAGER_ID        TEXT,
    DeptEmail         TEXT,
    DeptPassword      TEXT,
    IsDisabled        INTEGER DEFAULT 0,
    suspend_start     DATE,
    suspend_end       DATE,
    PasswordChangeOTP TEXT
);

CREATE TABLE BPO_BRANCH_ASSIGN (
    SW_USER_ID TEXT,
    BRANCH_ID  TEXT
);

CREATE TABLE SW_USER_AUDIT (
    audit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   TEXT NOT NULL,
    actor_type    TEXT NOT NULL,
    actor_id      TEXT,
    action        TEXT NOT NULL,
    target_emp_id TEXT,
    details       TEXT,
    source_ip     TEXT
);

CREATE TABLE LOYALTRI_SYNC_RUN (
    run_id            TEXT PRIMARY KEY,
    trigger_type      TEXT NOT NULL,
    requested_by      TEXT,
    status            TEXT NOT NULL,
    requested_at      TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    records_received  INTEGER NOT NULL DEFAULT 0,
    records_processed INTEGER NOT NULL DEFAULT 0,
    created           INTEGER NOT NULL DEFAULT 0,
    updated           INTEGER NOT NULL DEFAULT 0,
    unchanged         INTEGER NOT NULL DEFAULT 0,
    skipped           INTEGER NOT NULL DEFAULT 0,
    conflicts         INTEGER NOT NULL DEFAULT 0,
    failed            INTEGER NOT NULL DEFAULT 0,
    error_message     TEXT
);

CREATE TABLE LOYALTRI_SYNC_ITEM (
    item_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    emp_id     TEXT,
    outcome    TEXT NOT NULL,
    fields     TEXT,
    message    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE LOYALTRI_EMPLOYEE_LINK (
    emp_id         TEXT PRIMARY KEY,
    source_id      TEXT,
    synced_values  TEXT,
    source_hash    TEXT,
    first_seen_at  TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE LOYALTRI_SYNC_LOCK (
    lock_name    TEXT PRIMARY KEY,
    owner        TEXT,
    locked_until TEXT,
    heartbeat_at TEXT
);

CREATE TABLE SW_UI_SESSION_REVOKED (
    jti        TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
