"""Admin endpoints for the Loyaltri integration.

These only read the sync log and queue work for the sync worker. None of them
calls Loyaltri, so they respond instantly even when Loyaltri is down.
"""
import logging
from datetime import timedelta
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core import audit
from core.security import Principal, client_ip, require_admin
from services.loyaltri import config as lconfig
from services.loyaltri import loyaltri_scheduler
from services.loyaltri.loyaltri_sync import SyncStore, SyncStoreError, parse_dt, utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/loyaltri", tags=["Loyaltri sync (admin)"],
                   dependencies=[Depends(require_admin)])

MIGRATION_HINT = ("Sync tables are not installed. Run "
                  "database/migrations/001_audit_and_loyaltri.sql on the Switrus database.")


def _store() -> SyncStore:
    return SyncStore()


@router.get("/status", summary="Configuration, worker and last sync status")
def sync_status() -> Dict:
    settings = lconfig.get_settings()
    problems = settings.problems()
    body: Dict = {
        "configured": not problems,
        "problems": problems,
        "config": settings.public_summary(),
        "tables_ready": False,
        "worker": {"alive": False, "last_heartbeat": None},
        "last_success": None,
        "last_attempt": None,
        "active_run": None,
        "next_scheduled_at": None,
    }
    store = _store()
    if not store.tables_ready():
        body["problems"] = problems + [MIGRATION_HINT]
        return body
    body["tables_ready"] = True
    try:
        hb = store.worker_heartbeat()
        if hb:
            fresh = utcnow() - hb["_dt"] <= timedelta(seconds=max(60, settings.poll_seconds * 4))
            body["worker"] = {"alive": fresh, "last_heartbeat": hb["heartbeat_at"]}
        body["last_success"] = store.last_success()
        body["last_attempt"] = store.last_attempt()
        body["active_run"] = store.active_run()
        if settings.sync_enabled and not problems:
            last = body["last_attempt"]
            if last and last.get("finished_at"):
                nxt = parse_dt(last["finished_at"]) + timedelta(minutes=settings.interval_minutes)
                body["next_scheduled_at"] = nxt.isoformat(timespec="seconds") + "Z"
    except SyncStoreError as e:
        logger.error("Loyaltri status read failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not read the sync log from the database")
    return body


@router.post("/sync", status_code=202, summary="Queue a manual sync")
def trigger_sync(request: Request, principal: Principal = Depends(require_admin)):
    settings = lconfig.get_settings()
    problems = settings.problems()
    if problems:
        raise HTTPException(status_code=409, detail="Loyaltri is not configured: " + "; ".join(problems))
    store = _store()
    if not store.tables_ready():
        raise HTTPException(status_code=503, detail=MIGRATION_HINT)
    try:
        active = store.active_run()
        if active:
            return JSONResponse(status_code=200, content={"run": active, "already_active": True})
        run_id = store.create_run("manual", principal.actor_id)
        run = store.get_run(run_id)
    except SyncStoreError as e:
        logger.error("Could not queue Loyaltri sync: %s", e)
        raise HTTPException(status_code=500, detail="Could not queue the sync")

    audit.record("loyaltri.sync.requested", None, principal.kind, principal.actor_id,
                 {"run_id": run_id}, client_ip(request))
    loyaltri_scheduler.wake()
    return {"run": run, "already_active": False}


@router.get("/runs", summary="Sync history, newest first")
def list_runs(limit: int = Query(20, ge=1, le=100)) -> List[Dict]:
    store = _store()
    if not store.tables_ready():
        return []
    return store.list_runs(limit)


@router.get("/runs/{run_id}", summary="One sync run with its per-record results")
def get_run(run_id: str) -> Dict:
    store = _store()
    if not store.tables_ready():
        raise HTTPException(status_code=503, detail=MIGRATION_HINT)
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"No sync run {run_id}")
    run["items"] = store.get_items(run_id)
    return run
