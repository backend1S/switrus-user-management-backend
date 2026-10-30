from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import List, Dict, Any, Optional
import logging
from datetime import date, datetime, timedelta, timezone

# India Standard Time (UTC+5:30). Server may run in UTC, so compute
# "today" in IST for suspend-window checks rather than server-local time.
IST = timezone(timedelta(hours=5, minutes=30))

from core import audit
from core.permissions import scope_conditions
from core.security import Principal, client_ip, get_principal, require_admin
from database.database import Database
from models.models import LeaveUpdate, StatusUpdate, UserUpdate
from services import user_repository as users_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["Users"])

#################### GET /users and GET /users/stats ####################
# Declared before the /{emp_id} routes so literal paths win.
# Every read is limited to the caller's scope (core/permissions.py): admins
# and integrations see everyone, MANAGER their branch + department,
# BRANCHMANAGER their branch. Query parameters can only narrow that.

@router.get("", summary="List users (optionally including disabled)")
def list_all_users(
    include_disabled: bool = Query(False),
    search: Optional[str] = Query(None, max_length=100),
    branch_id: Optional[str] = Query(None, max_length=50),
    department_id: Optional[str] = Query(None, max_length=50),
    role: Optional[str] = Query(None, max_length=50),
    principal: Principal = Depends(get_principal),
) -> List[Dict]:
    return users_repo.list_users(include_disabled, search, branch_id, department_id, role,
                                 scope=principal.scope_filter)


@router.get("/stats", summary="User counts for the dashboard (within the caller's scope)")
def get_user_stats(principal: Principal = Depends(get_principal)) -> Dict[str, int]:
    return users_repo.user_stats(scope=principal.scope_filter)


@router.get("/filter", summary="Get users by Role and Branch")
def get_users_by_role_branch(
    role: str = Query(...,),
    branch_id: str = Query(...,),
    principal: Principal = Depends(get_principal),
) -> List[Dict]:

    # Normalize and accept comma-separated list
    roles = [r.strip() for r in role.split(",") if r.strip()]
    if not roles:
        raise HTTPException(status_code=400, detail="role query param is required")

    # Use uppercase comparison to make it case-insensitive
    roles_upper = [r.upper() for r in roles]

    # Parameterized IN() clause. The previous version interpolated `role` and
    # `branch_id` straight into the SQL text (SQL injection); the comma-separated
    # list it documented is now honoured as well.
    try:
        placeholders = ",".join(["%s"] * len(roles_upper))
        sql = f"""
        SELECT
            EMPID,
            EmpName,
            DepartmentID,
            Roles,
            BRANCH_ID,
            suspend_start,
            suspend_end
        FROM dbo.SW_USER
        WHERE UPPER(RTRIM(LTRIM(Roles))) IN ({placeholders})
        AND BRANCH_ID = %s
        AND IsDisabled = 0
        {{scope_sql}}
        ORDER BY EmpName
        """
        scope_where, scope_params = scope_conditions(*principal.scope_filter)
        sql = sql.replace("{scope_sql}", "".join(f"AND {w} " for w in scope_where))

        db = Database()
        status, rows_or_err = db.db_query(sql, tuple(roles_upper) + (branch_id,) + tuple(scope_params))

    except Exception as e:
        logger.exception("Unexpected DB error while querying SW_USER: %s", e)
        raise HTTPException(status_code=500, detail="Unexpected database error")

    if not status:
        logger.error("DB returned error for SW_USER: %s", rows_or_err)
        raise HTTPException(status_code=500, detail="Database error")

    if not isinstance(rows_or_err, list):
        logger.error("DB returned non-list result for SW_USER: %r", rows_or_err)
        raise HTTPException(status_code=500, detail="DB returned unexpected result type")

    results: List[Dict] = []
    skipped_rows = 0

    columns = ["EMPID", "EMPNAME", "DEPARTMENTID", "ROLES", "BRANCH_ID", "SUSPEND_START", "SUSPEND_END"]

    for idx, r in enumerate(rows_or_err):
        try:
            if isinstance(r, dict):
                rowd = {k.upper(): v for k, v in r.items()}
            elif isinstance(r, (list, tuple)):
                if len(r) < len(columns):
                    r = tuple(list(r) + [None] * (len(columns) - len(r)))
                rowd = dict(zip(columns, r))
            else:
                logger.warning("Skipping row %d: unsupported type %s", idx, type(r))
                skipped_rows += 1
                continue

            suspend_start = rowd.get("SUSPEND_START")
            suspend_end = rowd.get("SUSPEND_END")
            today = datetime.now(IST).date()

            def _to_date(v):
                if v is None:
                    return None
                # datetime is a subclass of date; normalize it to a pure date
                # so later date<=date comparisons don't raise TypeError.
                if isinstance(v, datetime):
                    return v.date()
                if isinstance(v, date):
                    return v
                try:
                    return date.fromisoformat(str(v)[:10])
                except Exception:
                    return None

            s_start = _to_date(suspend_start)
            s_end = _to_date(suspend_end)
            is_suspended = 1 if (s_start and s_end and s_start <= today <= s_end) else 0

            results.append(
                {
                    "emp_id": str(rowd.get("EMPID")) if rowd.get("EMPID") is not None else None,
                    "emp_name": rowd.get("EMPNAME"),
                    "department_id": users_repo.str_or_none(rowd.get("DEPARTMENTID")),
                    "role": rowd.get("ROLES"),
                    "branch_id": rowd.get("BRANCH_ID"),
                    "is_suspended": is_suspended
                }
            )

        except Exception as e:
            logger.exception("Error processing SW_USER row %d: %s", idx, e)
            skipped_rows += 1
            continue

    logger.info(
        "Fetched %d users for roles %s and branch %s (skipped %d rows)",
        len(results),
        roles,
        branch_id,
        skipped_rows
    )

    return results


@router.get("/department/{department_id}", summary="Get users by Department ID")
def get_users_by_department(department_id: str, principal: Principal = Depends(get_principal)) -> List[Dict]:

    sql = """
    SELECT
        EMPID,
        EmpName,
        DepartmentID,
        Roles,
        BRANCH_ID,
        suspend_start,
        suspend_end
    FROM dbo.SW_USER
    WHERE DepartmentID = %s
    AND IsDisabled = 0
    {scope_sql}
    ORDER BY EmpName
    """
    scope_where, scope_params = scope_conditions(*principal.scope_filter)
    sql = sql.replace("{scope_sql}", "".join(f"AND {w} " for w in scope_where))

    try:
        db = Database()
        status, rows_or_err = db.db_query(sql, (department_id,) + tuple(scope_params))
    except Exception as e:
        logger.exception("Unexpected DB error while querying SW_USER by department: %s", e)
        raise HTTPException(status_code=500, detail="Unexpected database error")

    if not status:
        logger.error("DB returned error for SW_USER: %s", rows_or_err)
        raise HTTPException(status_code=500, detail="Database error")

    if not isinstance(rows_or_err, list):
        logger.error("DB returned non-list result for SW_USER: %r", rows_or_err)
        raise HTTPException(status_code=500, detail="DB returned unexpected result type")

    results: List[Dict] = []
    skipped_rows = 0

    columns = ["EMPID", "EMPNAME", "DEPARTMENTID", "ROLES", "BRANCH_ID", "SUSPEND_START", "SUSPEND_END"]

    for idx, r in enumerate(rows_or_err):
        try:
            if isinstance(r, dict):
                rowd = {k.upper(): v for k, v in r.items()}
            elif isinstance(r, (list, tuple)):
                if len(r) < len(columns):
                    r = tuple(list(r) + [None] * (len(columns) - len(r)))
                rowd = dict(zip(columns, r))
            else:
                logger.warning("Skipping row %d: unsupported type %s", idx, type(r))
                skipped_rows += 1
                continue

            suspend_start = rowd.get("SUSPEND_START")
            suspend_end = rowd.get("SUSPEND_END")
            today = datetime.now(IST).date()

            def _to_date(v):
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

            s_start = _to_date(suspend_start)
            s_end = _to_date(suspend_end)
            is_suspended = 1 if (s_start and s_end and s_start <= today <= s_end) else 0

            results.append(
                {
                    "emp_id": str(rowd.get("EMPID")) if rowd.get("EMPID") is not None else None,
                    "emp_name": rowd.get("EMPNAME"),
                    "department_id": users_repo.str_or_none(rowd.get("DEPARTMENTID")),
                    "role": rowd.get("ROLES"),
                    "branch_id": rowd.get("BRANCH_ID"),
                    "is_suspended": is_suspended
                }
            )

        except Exception as e:
            logger.exception("Error processing SW_USER row %d: %s", idx, e)
            skipped_rows += 1
            continue

    logger.info(
        "Fetched %d users for department %s (skipped %d rows)",
        len(results),
        department_id,
        skipped_rows
    )

    return results


@router.get("/by-empid", summary="Get user details by EMPID")
def get_user_by_empid(
    emp_id: str = Query(..., alias="emp_id"),
    principal: Principal = Depends(get_principal),
) -> Dict:

    sql = """
    SELECT
        EMPID,
        EmpName,
        DepartmentID,
        Roles,
        BRANCH_ID,
        suspend_start,
        suspend_end,
        Email,
        Phone,
        MANAGER_ID
    FROM dbo.SW_USER
    WHERE EMPID = %s
    AND IsDisabled = 0
    {scope_sql}
    """
    scope_where, scope_params = scope_conditions(*principal.scope_filter)
    sql = sql.replace("{scope_sql}", "".join(f"AND {w} " for w in scope_where))

    try:
        db = Database()
        status, rows_or_err = db.db_query(sql, (emp_id,) + tuple(scope_params))
    except Exception as e:
        logger.exception("Unexpected DB error while querying SW_USER by EMPID: %s", e)
        raise HTTPException(status_code=500, detail="Unexpected database error")

    if not status:
        logger.error("DB returned error for SW_USER: %s", rows_or_err)
        raise HTTPException(status_code=500, detail="Database error")

    if not isinstance(rows_or_err, list):
        logger.error("DB returned non-list result for SW_USER: %r", rows_or_err)
        raise HTTPException(status_code=500, detail="DB returned unexpected result type")

    if not rows_or_err:
        raise HTTPException(status_code=404, detail=f"No user found with EMPID {emp_id}")

    columns = ["EMPID", "EMPNAME", "DEPARTMENTID", "ROLES", "BRANCH_ID", "SUSPEND_START", "SUSPEND_END", "EMAIL", "PHONE", "MANAGER_ID"]

    r = rows_or_err[0]
    if isinstance(r, dict):
        rowd = {k.upper(): v for k, v in r.items()}
    elif isinstance(r, (list, tuple)):
        if len(r) < len(columns):
            r = tuple(list(r) + [None] * (len(columns) - len(r)))
        rowd = dict(zip(columns, r))
    else:
        logger.error("Unsupported row type for SW_USER: %s", type(r))
        raise HTTPException(status_code=500, detail="DB returned unexpected row type")

    suspend_start = rowd.get("SUSPEND_START")
    suspend_end = rowd.get("SUSPEND_END")
    today = datetime.now(IST).date()

    def _to_date(v):
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

    s_start = _to_date(suspend_start)
    s_end = _to_date(suspend_end)
    is_suspended = 1 if (s_start and s_end and s_start <= today <= s_end) else 0

    result = {
        "emp_id": str(rowd.get("EMPID")) if rowd.get("EMPID") is not None else None,
        "emp_name": rowd.get("EMPNAME"),
        "department_id": users_repo.str_or_none(rowd.get("DEPARTMENTID")),
        "role": rowd.get("ROLES"),
        "branch_id": rowd.get("BRANCH_ID"),
        "is_suspended": is_suspended,
        "email": rowd.get("EMAIL"),
        "phone": rowd.get("PHONE"),
        "manager_id": rowd.get("MANAGER_ID")
    }

    logger.info("Fetched user details for EMPID %s", emp_id)

    return result

#################### /users/{emp_id} (must stay after the literal paths above) ####################
# EMPIDs contain slashes (SHPL/DEL/0417/2026, TEMP/107) and the server decodes
# %2F before routing, so these routes use the path converter. The /status and
# /leave routes are declared before /{emp_id:path} so they match first.

# PATCH body field -> key in the user object, for audit diffs.
_FIELD_TO_KEY = {
    "EmpName": "emp_name",
    "Email": "email",
    "DepartmentID": "department_id",
    "Roles": "role",
    "BRANCH_ID": "branch_id",
    "Phone": "phone",
    "MANAGER_ID": "manager_id",
    "DeptEmail": "dept_email",
}


def _audit(request: Request, principal: Principal, action: str, emp_id: str, details: Dict) -> None:
    audit.record(action, emp_id, principal.kind, principal.actor_id, details, client_ip(request))


@router.patch("/{emp_id:path}/status", summary="Disable or enable a user")
def set_user_status(
    emp_id: str,
    payload: StatusUpdate,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> Dict:
    before = users_repo.get_user_or_404(emp_id)
    target = before["emp_id"]

    if (payload.is_disabled and principal.kind == "session"
            and principal.emp_id and principal.emp_id.upper() == target.upper()):
        raise HTTPException(status_code=400, detail="You cannot disable your own account")

    users_repo.update_columns(target, {"IsDisabled": 1 if payload.is_disabled else 0})
    after = users_repo.get_user_or_404(target)

    if before["is_disabled"] != after["is_disabled"]:
        _audit(request, principal, "user.disable" if payload.is_disabled else "user.enable", target,
               {"is_disabled": {"from": before["is_disabled"], "to": after["is_disabled"]}})
    return after


@router.put("/{emp_id:path}/leave", summary="Mark leave (replaces any existing window)")
def set_user_leave(
    emp_id: str,
    payload: LeaveUpdate,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> Dict:
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="end_date cannot be before start_date")

    before = users_repo.get_user_or_404(emp_id)
    target = before["emp_id"]
    users_repo.update_columns(
        target, {"suspend_start": payload.start_date, "suspend_end": payload.end_date}
    )
    after = users_repo.get_user_or_404(target)
    _audit(request, principal, "user.leave.set", target, {
        "from": {"leave_start": before["leave_start"], "leave_end": before["leave_end"]},
        "to": {"leave_start": after["leave_start"], "leave_end": after["leave_end"]},
    })
    return after


@router.delete("/{emp_id:path}/leave", summary="Clear leave")
def clear_user_leave(
    emp_id: str,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> Dict:
    before = users_repo.get_user_or_404(emp_id)
    target = before["emp_id"]
    users_repo.update_columns(target, {"suspend_start": None, "suspend_end": None})
    after = users_repo.get_user_or_404(target)
    if before["leave_start"] or before["leave_end"]:
        _audit(request, principal, "user.leave.clear", target, {
            "from": {"leave_start": before["leave_start"], "leave_end": before["leave_end"]},
        })
    return after


@router.get("/{emp_id:path}", summary="Get one user (including disabled)")
def get_user_detail(emp_id: str, principal: Principal = Depends(get_principal)) -> Dict:
    # Outside the caller's scope reads as 404, so other branches can't be probed.
    return users_repo.get_user_or_404(emp_id, scope=principal.scope_filter)


@router.patch("/{emp_id:path}", summary="Edit user (partial update)")
def update_user(
    emp_id: str,
    payload: UserUpdate,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> Dict:
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update")

    for field in sorted(users_repo.REQUIRED_FIELDS):
        if field in changes and changes[field] is None:
            raise HTTPException(status_code=400, detail=f"{field} cannot be empty")

    before = users_repo.get_user_or_404(emp_id)
    target = before["emp_id"]

    if "EmpName" in changes and users_repo.name_taken_by_other(changes["EmpName"], target):
        raise HTTPException(status_code=409, detail="User name already exists")

    manager = changes.get("MANAGER_ID")
    if manager is not None:
        if manager.upper() == target.upper():
            raise HTTPException(status_code=400, detail="A user cannot be their own manager")
        if not users_repo.emp_id_exists(manager):
            raise HTTPException(status_code=400, detail=f"Manager {manager} does not exist")

    users_repo.update_columns(
        target, {users_repo.EDITABLE_COLUMNS[k]: v for k, v in changes.items()}
    )
    after = users_repo.get_user_or_404(target)

    diff = {
        f: {"from": before[_FIELD_TO_KEY[f]], "to": after[_FIELD_TO_KEY[f]]}
        for f in changes
        if before[_FIELD_TO_KEY[f]] != after[_FIELD_TO_KEY[f]]
    }
    _audit(request, principal, "user.update", target, {"changes": diff})
    logger.info("User %s updated fields %s", target, sorted(diff))
    return after
