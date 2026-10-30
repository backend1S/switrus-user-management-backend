"""SW_USER access for the user-management endpoints and the Loyaltri sync.

All SQL is parameterized. Column names that come from requests are mapped
through fixed allow-lists; request keys never become SQL.

Reads take a `scope` = (branch, department) from the caller's Principal
(core/permissions.py); None parts mean unrestricted. It is added to the WHERE
clause, so a scoped caller's filters can only narrow it, never widen it.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from core.permissions import scope_conditions
from core.security import is_disabled_value
from database.database import Database

logger = logging.getLogger(__name__)

# (branch, department) a caller may read; None = unrestricted.
Scope = Tuple[Optional[str], Optional[str]]
UNSCOPED: Scope = (None, None)

# India Standard Time (UTC+5:30), as in app/user.py.
IST = timezone(timedelta(hours=5, minutes=30))

USER_COLUMNS = [
    "EMPID", "EmpName", "DepartmentID", "Roles", "BRANCH_ID", "Email", "Phone",
    "MANAGER_ID", "DeptEmail", "IsDisabled", "suspend_start", "suspend_end",
]
_SELECT = "SELECT " + ", ".join(USER_COLUMNS) + " FROM dbo.SW_USER"

# Request field (PATCH body, database-style name) -> SW_USER column.
EDITABLE_COLUMNS = {
    "EmpName": "EmpName",
    "Email": "Email",
    "DepartmentID": "DepartmentID",
    "Roles": "Roles",
    "BRANCH_ID": "BRANCH_ID",
    "Phone": "Phone",
    "MANAGER_ID": "MANAGER_ID",
    "DeptEmail": "DeptEmail",
}
REQUIRED_FIELDS = {"EmpName", "Email", "DepartmentID", "Roles"}


def today_ist() -> date:
    return datetime.now(IST).date()


def to_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def str_or_none(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def is_on_leave(start, end, today: Optional[date] = None) -> bool:
    s, e = to_date(start), to_date(end)
    t = today or today_ist()
    return bool(s and e and s <= t <= e)


def row_to_user(row) -> Dict[str, Any]:
    r = dict(zip(USER_COLUMNS, row))
    leave_start = to_date(r["suspend_start"])
    leave_end = to_date(r["suspend_end"])
    return {
        "emp_id": str_or_none(r["EMPID"]),
        "emp_name": str_or_none(r["EmpName"]),
        # SW_USER.DepartmentID is nvarchar (SALES, VISA, ...): keep it as text.
        "department_id": str_or_none(r["DepartmentID"]),
        "role": str_or_none(r["Roles"]),
        "branch_id": str_or_none(r["BRANCH_ID"]),
        "email": str_or_none(r["Email"]),
        "phone": str_or_none(r["Phone"]),
        "manager_id": str_or_none(r["MANAGER_ID"]),
        "dept_email": str_or_none(r["DeptEmail"]),
        "is_disabled": is_disabled_value(r["IsDisabled"]),
        "leave_start": leave_start.isoformat() if leave_start else None,
        "leave_end": leave_end.isoformat() if leave_end else None,
        "is_suspended": 1 if is_on_leave(leave_start, leave_end) else 0,
    }


def _raise_db(err) -> None:
    # The driver message can name servers and objects: log it, don't return it.
    logger.error("SW_USER database error: %s", err)
    raise HTTPException(status_code=500, detail="Database error")


def escape_like(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_").replace("[", "\\["))


def list_users(
    include_disabled: bool = False,
    search: Optional[str] = None,
    branch_id: Optional[str] = None,
    department_id: Optional[str] = None,
    role: Optional[str] = None,
    scope: Scope = UNSCOPED,
) -> List[Dict[str, Any]]:
    where, params = scope_conditions(*scope)

    if not include_disabled:
        where.append("(IsDisabled IS NULL OR IsDisabled = 0)")
    if search and search.strip():
        pattern = "%" + escape_like(search.strip().upper()) + "%"
        where.append(
            "(UPPER(EmpName) LIKE %s ESCAPE '\\' OR UPPER(EMPID) LIKE %s ESCAPE '\\' "
            "OR UPPER(Email) LIKE %s ESCAPE '\\')"
        )
        params += [pattern, pattern, pattern]
    if branch_id and branch_id.strip():
        where.append("RTRIM(LTRIM(BRANCH_ID)) = %s")
        params.append(branch_id.strip())
    if department_id and department_id.strip():
        where.append("UPPER(RTRIM(LTRIM(DepartmentID))) = %s")
        params.append(department_id.strip().upper())
    if role and role.strip():
        where.append("UPPER(RTRIM(LTRIM(Roles))) = %s")
        params.append(role.strip().upper())

    sql = _SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY EmpName"

    ok, rows = Database().db_query(sql, tuple(params) if params else None)
    if not ok:
        _raise_db(rows)
    return [row_to_user(r) for r in rows]


def get_user(emp_id: str, scope: Scope = UNSCOPED) -> Optional[Dict[str, Any]]:
    """One user, or None if missing or outside the caller's scope (not told apart)."""
    where, params = scope_conditions(*scope)
    ok, rows = Database().db_query(
        _SELECT + " WHERE " + " AND ".join(["RTRIM(LTRIM(EMPID)) = RTRIM(LTRIM(%s))"] + where),
        tuple([emp_id] + params),
    )
    if not ok:
        _raise_db(rows)
    return row_to_user(rows[0]) if rows else None


def get_user_or_404(emp_id: str, scope: Scope = UNSCOPED) -> Dict[str, Any]:
    user = get_user(emp_id, scope)
    if not user:
        raise HTTPException(status_code=404, detail=f"No user found with EMPID {emp_id}")
    return user


def user_stats(scope: Scope = UNSCOPED) -> Dict[str, int]:
    where, params = scope_conditions(*scope)
    sql = "SELECT IsDisabled, suspend_start, suspend_end FROM dbo.SW_USER"
    if where:
        sql += " WHERE " + " AND ".join(where)
    ok, rows = Database().db_query(sql, tuple(params) if params else None)
    if not ok:
        _raise_db(rows)
    today = today_ist()
    total = len(rows)
    disabled = sum(1 for r in rows if is_disabled_value(r[0]))
    on_leave = sum(1 for r in rows if not is_disabled_value(r[0]) and is_on_leave(r[1], r[2], today))
    return {"total": total, "active": total - disabled, "disabled": disabled, "on_leave": on_leave}


def name_taken_by_other(emp_name: str, emp_id: Optional[str]) -> bool:
    sql = "SELECT EMPID FROM dbo.SW_USER WHERE RTRIM(LTRIM(EmpName)) = RTRIM(LTRIM(%s))"
    params: Tuple = (emp_name,)
    if emp_id is not None:
        sql += " AND RTRIM(LTRIM(EMPID)) <> RTRIM(LTRIM(%s))"
        params = (emp_name, emp_id)
    ok, rows = Database().db_query(sql, params)
    if not ok:
        _raise_db(rows)
    return bool(rows)


def emp_id_exists(emp_id: str) -> bool:
    ok, rows = Database().db_query(
        "SELECT EMPID FROM dbo.SW_USER WHERE RTRIM(LTRIM(EMPID)) = RTRIM(LTRIM(%s))", (emp_id,)
    )
    if not ok:
        _raise_db(rows)
    return bool(rows)


def update_columns(emp_id: str, values: Dict[str, Any]) -> None:
    """UPDATE SW_USER SET <allow-listed columns> for one user."""
    allowed = set(EDITABLE_COLUMNS.values()) | {"IsDisabled", "suspend_start", "suspend_end"}
    bad = [c for c in values if c not in allowed]
    if bad:
        raise ValueError(f"Column(s) not updatable: {bad}")
    if not values:
        return
    cols = list(values.keys())
    sql = (
        "UPDATE dbo.SW_USER SET " + ", ".join(f"{c} = %s" for c in cols)
        + " WHERE RTRIM(LTRIM(EMPID)) = RTRIM(LTRIM(%s))"
    )
    ok, err = Database().db_update(sql, tuple(values[c] for c in cols) + (emp_id,))
    if not ok:
        _raise_db(err)
