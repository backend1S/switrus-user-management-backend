"""Read-only Attendance Muster endpoints for the User Management UI.

Browser sessions only; access and scope are decided in
services/attendance/service.py. Nothing here writes to any database.
"""
import re
from datetime import date
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query

from core.security import Principal, get_principal
from services.attendance import service

router = APIRouter(prefix="/attendance", tags=["Attendance (read-only)"])

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date(name: str, value: str) -> date:
    if not _ISO.match(value or ""):
        raise HTTPException(status_code=400, detail=f"{name} must be a date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} is not a valid date")


@router.get("/access", summary="Whether the signed-in user may view attendance, and why not")
def attendance_access(principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
    return service.access_status(principal)


@router.get("/muster", summary="Attendance muster for a date range, limited to the caller's employees")
def attendance_muster(
    from_date: str = Query(..., description="First day, YYYY-MM-DD"),
    to_date: str = Query(..., description="Last day, YYYY-MM-DD (inclusive)"),
    principal: Principal = Depends(get_principal),
) -> Dict[str, Any]:
    return service.get_muster(principal, _date("from_date", from_date), _date("to_date", to_date))
