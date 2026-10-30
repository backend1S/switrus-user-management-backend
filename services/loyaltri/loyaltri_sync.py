"""Persistence for the Loyaltri sync: run log, per-record results, the field
baselines used to detect local admin edits, and the lease that prevents
overlapping runs (across threads, processes and containers).

Tables are created by database/migrations/001_audit_and_loyaltri.sql.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from database.database import Database

logger = logging.getLogger(__name__)

SYNC_LOCK = "loyaltri-sync"
WORKER_ROW = "loyaltri-worker"
FINISHED = ("succeeded", "partial", "failed", "skipped")
COUNT_FIELDS = ("records_received", "records_processed", "created", "updated",
                "unchanged", "skipped", "conflicts", "failed")
_RUN_COLS = ["run_id", "trigger_type", "requested_by", "status", "requested_at", "started_at",
             "finished_at", *COUNT_FIELDS, "error_message"]


class SyncStoreError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def parse_dt(v) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


def iso(v) -> Optional[str]:
    dt = parse_dt(v)
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


class SyncStore:
    def __init__(self, db_factory=Database):
        self._db = db_factory

    # ------------------------------------------------------------ helpers
    def _q(self, sql: str, params=None) -> List:
        ok, rows = self._db().db_query(sql, params)
        if not ok:
            raise SyncStoreError(str(rows))
        return rows

    def _u(self, sql: str, params=None) -> None:
        ok, err = self._db().db_update(sql, params)
        if not ok:
            raise SyncStoreError(str(err))

    def _x(self, sql: str, params=None) -> int:
        ok, count = self._db().db_execute(sql, params)
        if not ok:
            raise SyncStoreError(str(count))
        return int(count or 0)

    def tables_ready(self) -> bool:
        ok, _ = self._db().db_query("SELECT run_id FROM LOYALTRI_SYNC_RUN WHERE 1 = 0")
        return bool(ok)

    @staticmethod
    def _run_dict(row) -> Dict[str, Any]:
        r = dict(zip(_RUN_COLS, row))
        for k in ("requested_at", "started_at", "finished_at"):
            r[k] = iso(r[k])
        for k in COUNT_FIELDS:
            r[k] = int(r[k] or 0)
        return r

    _SELECT_RUN = "SELECT " + ", ".join(_RUN_COLS) + " FROM LOYALTRI_SYNC_RUN"

    # ------------------------------------------------------------ runs
    def create_run(self, trigger: str, requested_by: Optional[str]) -> str:
        run_id = str(uuid.uuid4())
        self._u(
            "INSERT INTO LOYALTRI_SYNC_RUN (run_id, trigger_type, requested_by, status, requested_at, "
            "records_received, records_processed, created, updated, unchanged, skipped, conflicts, failed) "
            "VALUES (%s, %s, %s, 'queued', %s, 0, 0, 0, 0, 0, 0, 0, 0)",
            (run_id, trigger, requested_by, utcnow()),
        )
        return run_id

    def mark_running(self, run_id: str) -> None:
        self._u("UPDATE LOYALTRI_SYNC_RUN SET status = 'running', started_at = %s WHERE run_id = %s",
                (utcnow(), run_id))

    def finish_run(self, run_id: str, status: str, counts: Dict[str, int], error: Optional[str]) -> None:
        sets = ", ".join(f"{c} = %s" for c in COUNT_FIELDS)
        self._u(
            f"UPDATE LOYALTRI_SYNC_RUN SET status = %s, finished_at = %s, error_message = %s, {sets} "
            "WHERE run_id = %s",
            (status, utcnow(), (error or None) and error[:2000],
             *[int(counts.get(c, 0)) for c in COUNT_FIELDS], run_id),
        )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        rows = self._q(self._SELECT_RUN + " WHERE run_id = %s", (run_id,))
        return self._run_dict(rows[0]) if rows else None

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._q(self._SELECT_RUN + " ORDER BY requested_at DESC")
        return [self._run_dict(r) for r in rows[:limit]]

    def active_run(self) -> Optional[Dict[str, Any]]:
        rows = self._q(self._SELECT_RUN + " WHERE status IN ('queued', 'running') ORDER BY requested_at")
        return self._run_dict(rows[0]) if rows else None

    def next_queued(self) -> Optional[Dict[str, Any]]:
        rows = self._q(self._SELECT_RUN + " WHERE status = 'queued' ORDER BY requested_at")
        return self._run_dict(rows[0]) if rows else None

    def last_with_status(self, statuses) -> Optional[Dict[str, Any]]:
        marks = ", ".join(["%s"] * len(statuses))
        rows = self._q(self._SELECT_RUN + f" WHERE status IN ({marks}) ORDER BY finished_at DESC",
                       tuple(statuses))
        return self._run_dict(rows[0]) if rows else None

    def last_success(self):
        return self.last_with_status(("succeeded", "partial"))

    def last_attempt(self):
        return self.last_with_status(FINISHED)

    def fail_stale_running(self) -> int:
        """Called while holding the lease: any run still 'running' belongs to a
        worker that died, so close it rather than leave it spinning forever."""
        return self._x(
            "UPDATE LOYALTRI_SYNC_RUN SET status = 'failed', finished_at = %s, "
            "error_message = 'The sync worker stopped before this run finished' WHERE status = 'running'",
            (utcnow(),),
        )

    # ------------------------------------------------------------ items
    def add_item(self, run_id: str, emp_id: Optional[str], outcome: str,
                 fields: Optional[List[str]] = None, message: Optional[str] = None) -> None:
        self._u(
            "INSERT INTO LOYALTRI_SYNC_ITEM (run_id, emp_id, outcome, fields, message, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (run_id, emp_id, outcome, ",".join(fields or []) or None,
             (message or None) and message[:1000], utcnow()),
        )

    def get_items(self, run_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self._q(
            "SELECT emp_id, outcome, fields, message FROM LOYALTRI_SYNC_ITEM WHERE run_id = %s "
            "ORDER BY item_id", (run_id,))
        return [
            {"emp_id": r[0], "outcome": r[1],
             "fields": [f for f in (r[2] or "").split(",") if f], "message": r[3]}
            for r in rows[:limit]
        ]

    # ------------------------------------------------------------ baselines
    def get_links(self) -> Dict[str, Dict[str, Any]]:
        rows = self._q("SELECT emp_id, source_id, synced_values, source_hash FROM LOYALTRI_EMPLOYEE_LINK")
        out = {}
        for emp_id, source_id, synced, h in rows:
            try:
                values = json.loads(synced) if synced else {}
            except ValueError:
                values = {}
            out[str(emp_id).strip().upper()] = {"source_id": source_id, "synced_values": values, "source_hash": h}
        return out

    def upsert_link(self, emp_id: str, source_id: Optional[str], synced_values: Dict[str, Any],
                    source_hash: str) -> None:
        now = utcnow()
        payload = json.dumps(synced_values, sort_keys=True)
        n = self._x(
            "UPDATE LOYALTRI_EMPLOYEE_LINK SET source_id = %s, synced_values = %s, source_hash = %s, "
            "last_synced_at = %s WHERE emp_id = %s",
            (source_id, payload, source_hash, now, emp_id),
        )
        if n == 0:
            self._u(
                "INSERT INTO LOYALTRI_EMPLOYEE_LINK (emp_id, source_id, synced_values, source_hash, "
                "first_seen_at, last_synced_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (emp_id, source_id, payload, source_hash, now, now),
            )

    # ------------------------------------------------------------ lease / heartbeat
    def _ensure_lock_row(self, name: str) -> None:
        self._u(
            "INSERT INTO LOYALTRI_SYNC_LOCK (lock_name) SELECT %s "
            "WHERE NOT EXISTS (SELECT 1 FROM LOYALTRI_SYNC_LOCK WHERE lock_name = %s)",
            (name, name),
        )

    def acquire_lease(self, owner: str, ttl_seconds: int) -> bool:
        """Atomic compare-and-set: succeeds only if the lease is free, expired or already ours."""
        self._ensure_lock_row(SYNC_LOCK)
        now = utcnow()
        n = self._x(
            "UPDATE LOYALTRI_SYNC_LOCK SET owner = %s, locked_until = %s "
            "WHERE lock_name = %s AND (locked_until IS NULL OR locked_until < %s OR owner = %s)",
            (owner, now + timedelta(seconds=ttl_seconds), SYNC_LOCK, now, owner),
        )
        return n == 1

    def release_lease(self, owner: str) -> None:
        self._u(
            "UPDATE LOYALTRI_SYNC_LOCK SET owner = NULL, locked_until = NULL "
            "WHERE lock_name = %s AND owner = %s",
            (SYNC_LOCK, owner),
        )

    def heartbeat(self, owner: str) -> None:
        self._ensure_lock_row(WORKER_ROW)
        self._u("UPDATE LOYALTRI_SYNC_LOCK SET owner = %s, heartbeat_at = %s WHERE lock_name = %s",
                (owner, utcnow(), WORKER_ROW))

    def worker_heartbeat(self) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT owner, heartbeat_at FROM LOYALTRI_SYNC_LOCK WHERE lock_name = %s", (WORKER_ROW,))
        if not rows or not rows[0][1]:
            return None
        return {"owner": rows[0][0], "heartbeat_at": iso(rows[0][1]), "_dt": parse_dt(rows[0][1])}
