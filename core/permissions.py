"""Who may see and change which users in the User Management UI.

One policy, applied in SQL on every user read and checked on every write.
A browser session's role, branch and department always come from that
employee's SW_USER row, re-read on each request; nothing the browser sends
(query parameters, body, headers) can widen what it sees.

Default matrix (role names are case-insensitive; overridable in .env):

    role            access      sees
    ADMIN           read+write  every branch and department      ADMIN_ROLES
    MANAGER         read-only   own branch AND own department    UI_BRANCH_DEPARTMENT_ROLES
    BRANCHMANAGER   read-only   own branch, all departments      UI_BRANCH_ROLES
    (viewer roles)  read-only   everything                       UI_VIEWER_ROLES (empty by default)
    anything else   no access

Trusted integrations (X-API-Key) keep their existing policy: full access,
unscoped, because CRM and accounting look up users across every branch.

A scoped role whose own branch or department is missing or obviously wrong
(a placeholder like "string", or a role name such as "Manager" in BRANCH_ID)
is refused: a data error must never turn into wider access.
"""
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from core import config

SCOPE_ALL = "all"
SCOPE_BRANCH = "branch"
SCOPE_BRANCH_DEPARTMENT = "branch_department"

# Values seen in SW_USER that are not real branch/department codes.
_PLACEHOLDERS = {"STRING", "NULL", "NONE", "N/A", "NA", "-", "0"}


@dataclass(frozen=True)
class Policy:
    is_admin: bool
    scope: str


class ScopeError(Exception):
    """The employee's own branch/department cannot be used to scope access."""


def normalize_role(role: Optional[str]) -> str:
    return str(role or "").strip().upper()


def policy_for(role: Optional[str]) -> Optional[Policy]:
    """The UI policy for a role, or None when the role has no UI access."""
    r = normalize_role(role)
    if not r:
        return None
    if r in config.ADMIN_ROLES:
        return Policy(is_admin=True, scope=SCOPE_ALL)
    # Narrowest scope wins if a role is listed twice.
    if r in config.UI_BRANCH_DEPARTMENT_ROLES:
        return Policy(is_admin=False, scope=SCOPE_BRANCH_DEPARTMENT)
    if r in config.UI_BRANCH_ROLES:
        return Policy(is_admin=False, scope=SCOPE_BRANCH)
    if r in config.UI_VIEWER_ROLES:
        return Policy(is_admin=False, scope=SCOPE_ALL)
    return None


def _all_role_names() -> set:
    return (set(config.ADMIN_ROLES) | set(config.UI_BRANCH_DEPARTMENT_ROLES)
            | set(config.UI_BRANCH_ROLES) | set(config.UI_VIEWER_ROLES))


def clean_assignment(value: Any) -> Optional[str]:
    """A usable branch/department code, or None if missing or a placeholder."""
    s = str(value or "").strip()
    if not s or s.upper() in _PLACEHOLDERS or s.upper() in _all_role_names():
        return None
    return s


def resolve_scope(policy: Policy, branch_id: Any, department_id: Any) -> Tuple[Optional[str], Optional[str]]:
    """(branch, department) the employee is limited to; None means unrestricted.

    Raises ScopeError when the policy needs a value the employee's row lacks.
    """
    if policy.scope == SCOPE_ALL:
        return None, None
    branch = clean_assignment(branch_id)
    if branch is None:
        raise ScopeError("branch")
    if policy.scope == SCOPE_BRANCH:
        return branch, None
    department = clean_assignment(department_id)
    if department is None:
        raise ScopeError("department")
    return branch, department


def scope_conditions(branch: Optional[str], department: Optional[str]) -> Tuple[List[str], List[str]]:
    """SQL WHERE fragments (and params) restricting SW_USER rows to a scope."""
    where: List[str] = []
    params: List[str] = []
    if branch is not None:
        where.append("UPPER(RTRIM(LTRIM(BRANCH_ID))) = %s")
        params.append(branch.upper())
    if department is not None:
        where.append("UPPER(RTRIM(LTRIM(DepartmentID))) = %s")
        params.append(department.upper())
    return where, params
