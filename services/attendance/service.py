"""Attendance Muster: access policy, employee mapping and aggregation.

Read-only. The only database statement is a SELECT on SW_USER (limited to the
caller's scope) used to match the service's employeeCode to SW_USER.EMPID.

Who sees what (enforced here, whatever the browser asks for):

    ADMIN           ATTENDANCE_ADMIN_SCOPE: none (default) -> refused;
                    matched -> employees found in SW_USER, every branch;
                    all -> also records that match no Switrus user
    MANAGER         own branch AND department, matched employees only,
                    and only once ATTENDANCE_MAPPING_VERIFIED=true
    BRANCHMANAGER   own branch, matched employees only, same condition
    anything else   refused (including read-only viewer roles and API keys)

A record is matched only when its employeeCode equals exactly one EMPID
(trimmed, case-insensitive) and no other record carries the same code. The
service's numeric employeeId is never used as a Switrus ID.
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from core import permissions
from core.security import Principal, is_disabled_value
from database.database import Database
from services.attendance import config as aconfig
from services.attendance.client import AttendanceError, build_client
from services.attendance.parser import CATEGORIES, Parsed, Record, parse_muster

logger = logging.getLogger(__name__)

NO_ROLE = "Your role does not have access to attendance."
ADMIN_NOT_ENABLED = ("Company-wide attendance is not enabled. A system administrator must set "
                     "ATTENDANCE_ADMIN_SCOPE to authorize it.")
MAPPING_UNVERIFIED = ("Attendance for your area is not available yet: the attendance system's employee "
                      "codes have not been confirmed to match Switrus employee IDs.")


# ------------------------------------------------------------------ access

def access_for(principal: Principal, settings: aconfig.AttendanceSettings) -> Tuple[Optional[str], Optional[str]]:
    """(denial reason, admin scope). Reason None means allowed."""
    if principal.kind != "session":
        return NO_ROLE, None
    if principal.is_admin:
        if settings.admin_scope not in ("matched", "all"):
            return ADMIN_NOT_ENABLED, None
        return None, settings.admin_scope
    if principal.scope in (permissions.SCOPE_BRANCH, permissions.SCOPE_BRANCH_DEPARTMENT):
        if not settings.mapping_verified:
            return MAPPING_UNVERIFIED, None
        return None, None
    return NO_ROLE, None


def access_status(principal: Principal) -> Dict[str, Any]:
    settings = aconfig.get_settings()
    reason, _ = access_for(principal, settings)
    problems = settings.problems()
    body: Dict[str, Any] = {
        "allowed": reason is None and not problems,
        "reason": reason or ("Attendance is not configured yet." if problems else None),
        "configured": not problems,
        "mapping_verified": settings.mapping_verified,
        "max_range_days": settings.max_range_days,
    }
    if principal.is_admin:
        body["problems"] = problems
        body["config"] = settings.public_summary()
    return body


# ------------------------------------------------------------------ upstream (cached)

_cache: Dict[Tuple[str, str, str], Tuple[float, Parsed]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _fetch(settings: aconfig.AttendanceSettings, from_date: date, to_date: date) -> Parsed:
    key = (settings.client_mode, from_date.isoformat(), to_date.isoformat())
    now = time.monotonic()
    if settings.cache_seconds:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and now - hit[0] < settings.cache_seconds:
            return hit[1]
    parsed = parse_muster(build_client(settings).fetch(from_date, to_date), from_date, to_date)
    if settings.cache_seconds:
        with _cache_lock:
            _cache[key] = (now, parsed)
    return parsed


# ------------------------------------------------------------------ SW_USER (read-only)

def _switrus_users(branch: Optional[str], department: Optional[str]) -> Dict[str, Dict[str, Any]]:
    where, params = permissions.scope_conditions(branch, department)
    sql = "SELECT EMPID, EmpName, BRANCH_ID, DepartmentID, IsDisabled FROM dbo.SW_USER"
    if where:
        sql += " WHERE " + " AND ".join(where)
    ok, rows = Database().db_query(sql, tuple(params) if params else None)
    if not ok:
        logger.error("Attendance: SW_USER read failed: %s", rows)
        raise HTTPException(status_code=500, detail="Database error")
    out: Dict[str, Dict[str, Any]] = {}
    for emp_id, name, br, dept, disabled in rows:
        if emp_id is None:
            continue
        out[str(emp_id).strip().upper()] = {
            "emp_id": str(emp_id).strip(),
            "emp_name": (str(name).strip() or None) if name is not None else None,
            "branch_id": (str(br).strip() or None) if br is not None else None,
            "department_id": (str(dept).strip() or None) if dept is not None else None,
            "is_disabled": is_disabled_value(disabled),
        }
    return out


# ------------------------------------------------------------------ aggregation

def _empty_summary() -> Dict[str, int]:
    s = {c: 0 for c in CATEGORIES}
    s.update(days_recorded=0, work_minutes=0, overtime_minutes=0)
    return s


def _summarize(rec: Record) -> Dict[str, int]:
    s = _empty_summary()
    for day in rec.days.values():
        s[day.category] += 1
        s["days_recorded"] += 1
        s["work_minutes"] += day.work_minutes or 0
        s["overtime_minutes"] += day.overtime_minutes or 0
    return s


def validate_range(from_date: date, to_date: date, settings: aconfig.AttendanceSettings) -> None:
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="to_date cannot be before from_date")
    days = (to_date - from_date).days + 1
    if days > settings.max_range_days:
        raise HTTPException(status_code=400,
                            detail=f"Choose at most {settings.max_range_days} days (this range has {days})")


def get_muster(principal: Principal, from_date: date, to_date: date) -> Dict[str, Any]:
    settings = aconfig.get_settings()
    reason, admin_scope = access_for(principal, settings)
    if reason:
        raise HTTPException(status_code=403, detail=reason)
    problems = settings.problems()
    if problems:
        detail = "Attendance is not configured yet."
        if principal.is_admin:
            detail += " " + "; ".join(problems)
        raise HTTPException(status_code=503, detail=detail)
    validate_range(from_date, to_date, settings)

    try:
        parsed = _fetch(settings, from_date, to_date)
    except AttendanceError as e:
        raise HTTPException(status_code=e.status, detail=str(e))

    branch, department = principal.scope_filter  # (None, None) for admins
    users = _switrus_users(branch, department)

    code_counts: Dict[str, int] = {}
    for rec in parsed.records:
        if rec.code:
            k = rec.code.strip().upper()
            code_counts[k] = code_counts.get(k, 0) + 1

    dates = [(from_date + timedelta(n)).isoformat() for n in range((to_date - from_date).days + 1)]
    employees: List[Dict[str, Any]] = []
    stats = {"upstream_records": len(parsed.records), "matched": 0, "unmatched": 0,
             "missing_code": 0, "duplicate_code_records": 0}
    for rec in parsed.records:
        key = rec.code.strip().upper() if rec.code else None
        if key is None:
            stats["missing_code"] += 1
            match = None
        elif code_counts[key] > 1:
            stats["duplicate_code_records"] += 1  # ambiguous: never attributed to anyone
            continue
        else:
            match = users.get(key)

        if match:
            stats["matched"] += 1
            employee = {**match, "matched": True}
        else:
            if key is not None:
                stats["unmatched"] += 1
            if admin_scope != "all":
                continue  # scoped roles and "matched" admins never see unmatched records
            employee = {"emp_id": rec.code, "emp_name": rec.name, "branch_id": None,
                        "department_id": None, "is_disabled": False, "matched": False}
        employee["days"] = {d: day.to_dict() for d, day in sorted(rec.days.items())}
        employee["summary"] = _summarize(rec)
        employee["source_summary"] = rec.source_summary
        employees.append(employee)

    employees.sort(key=lambda e: ((e["emp_name"] or "").upper(), str(e["emp_id"] or "")))

    totals = _empty_summary()
    states: Dict[str, Dict[str, Any]] = {}
    for e in employees:
        for k in totals:
            totals[k] += e["summary"][k]
        for day in e["days"].values():
            st = states.setdefault(day["status"], {"label": day["status"], "category": day["category"], "count": 0})
            st["count"] += 1
    totals["employees"] = len(employees)

    warnings: List[str] = []
    if parsed.range_mismatch:
        warnings.append("The attendance service returned a different date range than requested.")
    if parsed.unparsed_dates:
        warnings.append(f"{parsed.unparsed_dates} day entr{'y' if parsed.unparsed_dates == 1 else 'ies'} "
                        "could not be read and are not shown.")
    if principal.is_admin and not settings.mapping_verified:
        warnings.append("Matching of attendance employee codes to Switrus employee IDs has not been confirmed.")

    logger.info("Attendance muster %s..%s viewed by %s: %d employee(s)",
                from_date, to_date, principal.actor_id, len(employees))

    body: Dict[str, Any] = {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "dates": dates,
        "access": {"scope": principal.scope, "branch_id": branch, "department_id": department},
        "mapping_verified": settings.mapping_verified,
        "employees": employees,
        "totals": totals,
        "states": sorted(states.values(), key=lambda s: (-s["count"], s["label"])),
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if principal.is_admin:
        body["mapping"] = {**stats, "show_unmatched": admin_scope == "all",
                           "skipped_records": parsed.skipped_records,
                           "out_of_range_dates": parsed.out_of_range_dates}
    return body
