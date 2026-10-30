"""Health checks. Neither check contacts Loyaltri: the API's health must never
depend on the external HRMS."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from database.database import Database

router = APIRouter(tags=["Health"])


@router.get("/health", summary="Liveness")
def liveness():
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness (database reachable)")
def readiness():
    ok, err = Database().db_query("SELECT 1")
    if not ok:
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unreachable"})
    return {"status": "ok", "database": "ok"}
