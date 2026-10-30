import hmac
import logging
from typing import Any, Dict

from fastapi import HTTPException, status

from core.security import is_disabled_value
from database.database import Database
from services.email import pwd_context, verify_password

logger = logging.getLogger(__name__)


def authenticate_credentials(db: Database, username: str, password: str) -> Dict[str, Any]:
    """Validate a username (EmpName) and password against SW_USER.

    Returns the user's login columns. Raises 401 for bad credentials, 403 for a
    disabled account (only after the password matched, so disabled accounts
    cannot be discovered by guessing), 500 on database errors.
    """
    sql_query = (
        "SELECT EMPID, BRANCH_ID, DepartmentID, Roles, EmpName, Email, EmpPassword, IsDisabled, MANAGER_ID, Phone "
        "FROM SW_USER WHERE RTRIM(LTRIM(EmpName)) = RTRIM(LTRIM(%s))"
    )
    ok, rows = db.db_query(sql_query, (username,))
    if not ok:
        logger.error("Database error while validating user %s: %s", username, rows)
        raise HTTPException(status_code=500, detail="Database error")

    if not rows:
        logger.warning("Invalid credentials for username: %s", username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    row = rows[0]
    user = {
        "emp_id": row[0],
        "branch_id": row[1],
        "dep_id": row[2],
        "role": row[3],
        "emp_name": row[4],
        "email": row[5],
        "is_disabled": row[7],
        "manager_id": row[8],
        "phone": row[9],
    }
    stored_hash = row[6]

    if not stored_hash:
        logger.error("No stored password hash for user %s", username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not verify_password(password, stored_hash):
        logger.warning("Invalid credentials for username: %s", username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # Previously this check raised inside a broad `except Exception`, which
    # swallowed the 403 and let disabled users log in. Now it is enforced.
    if is_disabled_value(user["is_disabled"]):
        logger.warning("Login attempt for disabled account: %s", username)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")

    return user


def _dept_password_matches(password: str, stored: str) -> bool:
    """Compare against SW_USER.DeptPassword.

    The column holds legacy plaintext today, so it is compared in constant time
    (hmac.compare_digest) as a compatibility measure. Values are compared
    exactly: no trimming or case folding. If a value is later replaced by an
    Argon2 hash (see README, "Moving off DeptPassword"), it is verified as one.
    """
    try:
        if pwd_context.identify(stored):
            return pwd_context.verify(password, stored)
    except Exception:
        return False
    return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))


def authenticate_ui_credentials(db: Database, username: str, password: str) -> Dict[str, Any]:
    """Validate a User Management sign-in: EmpName + SW_USER.DeptPassword.

    EmpPassword is not read here; POST /login (integrations) keeps using it.
    Raises 401 for an unknown, ambiguous or password-less user or a wrong
    password (one message for all, so none can be told apart), 403 for a
    disabled account (only after the password matched), 500 on database errors.
    The stored password never leaves this function and is never logged.
    """
    ok, rows = db.db_query(
        "SELECT EMPID, EmpName, DeptPassword, Roles, IsDisabled, DepartmentID, BRANCH_ID "
        # Case-insensitive on any collation, so names differing only in case count
        # as duplicates (refused below) rather than as separate accounts.
        "FROM dbo.SW_USER WHERE UPPER(RTRIM(LTRIM(EmpName))) = UPPER(RTRIM(LTRIM(%s)))",
        (username,),
    )
    if not ok:
        logger.error("Database error while validating UI sign-in for %s: %s", username, rows)
        raise HTTPException(status_code=500, detail="Database error")

    invalid = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect user name or password")
    if not rows:
        logger.warning("UI sign-in: unknown user name %s", username)
        raise invalid
    if len(rows) > 1:
        # Two accounts with one name: refuse rather than guess which one is meant.
        logger.error("UI sign-in refused: user name %s matches %d accounts", username, len(rows))
        raise invalid

    emp_id, emp_name, stored, role, disabled, dept_id, branch_id = rows[0]
    if stored is None or not str(stored).strip():
        logger.warning("UI sign-in refused for %s: no DeptPassword set", str(emp_id).strip())
        raise invalid
    if not _dept_password_matches(password, str(stored)):
        logger.warning("UI sign-in: wrong password for %s", username)
        raise invalid
    if is_disabled_value(disabled):
        logger.warning("UI sign-in attempt for disabled account: %s", username)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")

    return {
        "emp_id": str(emp_id).strip(),
        "emp_name": emp_name,
        "role": role,
        "dep_id": dept_id,
        "branch_id": branch_id,
    }
