/*
  Switrus User Management - audit trail and Loyaltri sync tables.

  * Additive only: SW_USER and every other existing table are NOT modified.
  * Idempotent: safe to run more than once.
  * Run with a login that can CREATE TABLE in the Switrus database, e.g.
      sqlcmd -S <server> -d <database> -U <user> -i 001_audit_and_loyaltri.sql

  Without these tables the API still works: audit entries go to logs/audit.log
  only, and the Loyaltri page reports that the sync tables are missing.
*/

IF OBJECT_ID(N'dbo.SW_USER_AUDIT', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.SW_USER_AUDIT (
        audit_id       BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_SW_USER_AUDIT PRIMARY KEY,
        occurred_at    DATETIME2(0)   NOT NULL,          -- UTC
        actor_type     NVARCHAR(20)   NOT NULL,          -- session | integration | loyaltri-sync
        actor_id       NVARCHAR(100)  NULL,              -- EMPID, api-key fingerprint, or sync run id
        action         NVARCHAR(50)   NOT NULL,          -- user.create, user.update, user.disable, ...
        target_emp_id  NVARCHAR(50)   NULL,
        details        NVARCHAR(MAX)  NULL,              -- JSON, never contains passwords
        source_ip      NVARCHAR(64)   NULL
    );
    CREATE INDEX IX_SW_USER_AUDIT_target ON dbo.SW_USER_AUDIT (target_emp_id, occurred_at);
END;

IF OBJECT_ID(N'dbo.LOYALTRI_SYNC_RUN', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.LOYALTRI_SYNC_RUN (
        run_id             NVARCHAR(36)   NOT NULL CONSTRAINT PK_LOYALTRI_SYNC_RUN PRIMARY KEY,
        trigger_type       NVARCHAR(20)   NOT NULL,      -- schedule | manual | manual-cli
        requested_by       NVARCHAR(100)  NULL,
        status             NVARCHAR(20)   NOT NULL,      -- queued | running | succeeded | partial | failed
        requested_at       DATETIME2(0)   NOT NULL,
        started_at         DATETIME2(0)   NULL,
        finished_at        DATETIME2(0)   NULL,
        records_received   INT NOT NULL CONSTRAINT DF_LSR_received  DEFAULT 0,
        records_processed  INT NOT NULL CONSTRAINT DF_LSR_processed DEFAULT 0,
        created            INT NOT NULL CONSTRAINT DF_LSR_created   DEFAULT 0,
        updated            INT NOT NULL CONSTRAINT DF_LSR_updated   DEFAULT 0,
        unchanged          INT NOT NULL CONSTRAINT DF_LSR_unchanged DEFAULT 0,
        skipped            INT NOT NULL CONSTRAINT DF_LSR_skipped   DEFAULT 0,
        conflicts          INT NOT NULL CONSTRAINT DF_LSR_conflicts DEFAULT 0,
        failed             INT NOT NULL CONSTRAINT DF_LSR_failed    DEFAULT 0,
        error_message      NVARCHAR(2000) NULL
    );
    CREATE INDEX IX_LOYALTRI_SYNC_RUN_status ON dbo.LOYALTRI_SYNC_RUN (status, requested_at);
END;

IF OBJECT_ID(N'dbo.LOYALTRI_SYNC_ITEM', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.LOYALTRI_SYNC_ITEM (
        item_id     BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_LOYALTRI_SYNC_ITEM PRIMARY KEY,
        run_id      NVARCHAR(36)   NOT NULL,
        emp_id      NVARCHAR(50)   NULL,
        outcome     NVARCHAR(20)   NOT NULL,             -- created | updated | conflict | new | skipped | failed | unchanged
        fields      NVARCHAR(1000) NULL,                 -- comma-separated field names
        message     NVARCHAR(1000) NULL,
        created_at  DATETIME2(0)   NOT NULL
    );
    CREATE INDEX IX_LOYALTRI_SYNC_ITEM_run ON dbo.LOYALTRI_SYNC_ITEM (run_id, item_id);
END;

IF OBJECT_ID(N'dbo.LOYALTRI_EMPLOYEE_LINK', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.LOYALTRI_EMPLOYEE_LINK (
        emp_id          NVARCHAR(50)   NOT NULL CONSTRAINT PK_LOYALTRI_EMPLOYEE_LINK PRIMARY KEY, -- SW_USER.EMPID
        source_id       NVARCHAR(100)  NULL,             -- Loyaltri's own record id, if mapped
        synced_values   NVARCHAR(MAX)  NULL,             -- JSON: last value synced per field (3-way merge baseline)
        source_hash     NVARCHAR(64)   NULL,
        first_seen_at   DATETIME2(0)   NOT NULL,
        last_synced_at  DATETIME2(0)   NOT NULL
    );
END;

IF OBJECT_ID(N'dbo.LOYALTRI_SYNC_LOCK', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.LOYALTRI_SYNC_LOCK (
        lock_name     NVARCHAR(50)   NOT NULL CONSTRAINT PK_LOYALTRI_SYNC_LOCK PRIMARY KEY,
        owner         NVARCHAR(100)  NULL,
        locked_until  DATETIME2(0)   NULL,
        heartbeat_at  DATETIME2(0)   NULL
    );
END;

IF OBJECT_ID(N'dbo.SW_UI_SESSION_REVOKED', N'U') IS NULL
BEGIN
    -- Browser sessions signed out before expiry (rows are pruned after expires_at).
    CREATE TABLE dbo.SW_UI_SESSION_REVOKED (
        jti         NVARCHAR(64)  NOT NULL CONSTRAINT PK_SW_UI_SESSION_REVOKED PRIMARY KEY,
        expires_at  DATETIME2(0)  NOT NULL
    );
END;
