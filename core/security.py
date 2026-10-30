"""Request authentication and authorization.

Two kinds of caller are accepted:

* integration - a trusted server (CRM, accounting) sending X-API-Key (or the
  legacy ?api_key= query parameter while ALLOW_API_KEY_QUERY is on; it is not
  offered in the API docs). Behaves exactly as before: full access.
* session     - a person using the User Management UI, signed in with
  POST /auth/login. Authenticated by an HttpOnly session cookie. The user's role and
  disabled flag are re-read from SW_USER on every request, unsafe methods must
  carry a CSRF token, and cross-site origins are rejected.
* no-login    - only with UI_AUTH_DISABLED (never in production): a browser
  request with no credential acts as UI_DEFAULT_EMPID, with the same role,
  disabled and origin checks as a session.

The browser never sees the API key or the JWT signing secret.
"""
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from core import config, permissions
from database.database import Database

logger = logging.getLogger(__name__)

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
# The legacy ?api_key= parameter is read by hand (see _query_api_key) so it is
# not advertised in /docs, where Swagger would put the key into the URL.
API_KEY_QUERY_NAME = "api_key"

# Reachable without any credential.
PUBLIC_PATHS = {
    "/docs", "/openapi.json", "/favicon.ico",
    "/health", "/health/ready",
    "/auth/login", "/auth/logout",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Principal:
    kind: str                      # "integration" | "session"
    emp_id: Optional[str] = None
    emp_name: Optional[str] = None
    role: Optional[str] = None
    is_admin: bool = False
    key_id: Optional[str] = None   # which API key matched (fingerprint), integrations only
    # Read scope (core/permissions.py). Integrations are unscoped.
    scope: str = permissions.SCOPE_ALL
    branch_id: Optional[str] = None       # set when scope limits by branch
    department_id: Optional[str] = None   # set when scope limits by department
    home_branch_id: Optional[str] = None      # the employee's own SW_USER values,
    home_department_id: Optional[str] = None  # shown in the UI

    @property
    def actor_id(self) -> str:
        return self.emp_id if self.kind == "session" else (self.key_id or "api-key")

    @property
    def scope_filter(self) -> Tuple[Optional[str], Optional[str]]:
        """(branch, department) every user query must be limited to; None = any."""
        return self.branch_id, self.department_id


def is_disabled_value(value) -> bool:
    """Same truthiness rule /login has always used for SW_USER.IsDisabled.

    NULL (and 0, False, "") means enabled: 6 real rows have NULL, and they
    have always been able to sign in.
    """
    return str(value).strip().lower() in ("1", "true", "y", "yes")


def role_is_admin(role: Optional[str]) -> bool:
    policy = permissions.policy_for(role)
    return bool(policy and policy.is_admin)


def role_can_use_ui(role: Optional[str]) -> bool:
    return permissions.policy_for(role) is not None


NO_ROLE_ACCESS = "Your role does not have access to User Management"


def build_ui_principal(emp_id, emp_name, role, branch_id, department_id) -> Principal:
    """Principal for a person, from their own SW_USER row. Raises 403 when the
    role has no UI access or a scoped role lacks a usable branch/department."""
    emp_id = str(emp_id).strip()
    policy = permissions.policy_for(role)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NO_ROLE_ACCESS)
    try:
        branch, department = permissions.resolve_scope(policy, branch_id, department_id)
    except permissions.ScopeError as e:
        logger.error("UI access refused for %s: role %r needs a valid %s, SW_USER has %r",
                     emp_id, role, e, branch_id if str(e) == "branch" else department_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your account has no valid {e} assigned, so User Management cannot "
                   "decide which employees you may see. Ask an administrator to correct it.",
        )
    return Principal(
        kind="session",
        emp_id=emp_id,
        emp_name=emp_name,
        role=role,
        is_admin=policy.is_admin,
        scope=policy.scope,
        branch_id=branch,
        department_id=department,
        home_branch_id=permissions.clean_assignment(branch_id),
        home_department_id=permissions.clean_assignment(department_id),
    )


def _key_fingerprint(key: str) -> str:
    return "api-key:" + hashlib.sha256(key.encode()).hexdigest()[:8]


def _match_api_key(provided: Optional[str]) -> Optional[str]:
    if not provided:
        return None
    for key in config.API_KEYS:
        if secrets.compare_digest(provided.encode(), key.encode()):
            return _key_fingerprint(key)
    return None


# ---------------------------------------------------------------- sessions

def issue_session(emp_id: str) -> Tuple[str, str, int]:
    """Return (jwt, csrf_token, max_age_seconds) for a new browser session."""
    csrf = secrets.token_urlsafe(32)
    max_age = config.SESSION_TTL_MINUTES * 60
    jti = secrets.token_urlsafe(16)
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "typ": "session",
            "sub": emp_id,
            "csrf": csrf,
            "jti": jti,
            "iat": now,
            "exp": now + timedelta(seconds=max_age),
        },
        config.SESSION_SECRET,
        algorithm="HS256",
    )
    return token, csrf, max_age


def decode_session(token: str) -> Optional[Dict]:
    try:
        claims = jwt.decode(token, config.SESSION_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != "session" or not claims.get("sub") or not claims.get("csrf") or not claims.get("jti"):
        return None
    return claims


# ---------------------------------------------------------------- revocation
# Sign-out revokes the session id server-side, so a copied cookie stops working
# immediately. Stored in SW_UI_SESSION_REVOKED (shared by all API instances);
# if that table has not been created yet, revocations are kept in memory.

_revoked_local: Dict[str, float] = {}
# When SW_UI_SESSION_REVOKED does not exist, stop asking the database for it on
# every request; look again after this many seconds (the migration may run).
_REVOKED_TABLE_RECHECK_SECONDS = 300
_revoked_table_missing_until = 0.0


def revoke_session(claims: Dict) -> None:
    jti, exp = claims.get("jti"), claims.get("exp")
    if not jti:
        return
    expires = datetime.fromtimestamp(int(exp or time.time()), timezone.utc).replace(tzinfo=None)
    _revoked_local[jti] = float(exp or time.time())
    ok, err = Database().db_update(
        "INSERT INTO SW_UI_SESSION_REVOKED (jti, expires_at) VALUES (%s, %s)", (jti, expires)
    )
    if not ok:
        logger.warning("Session revocation kept in memory only (%s)", err)
    else:
        Database().db_update(
            "DELETE FROM SW_UI_SESSION_REVOKED WHERE expires_at < %s",
            (datetime.now(timezone.utc).replace(tzinfo=None),),
        )


def is_revoked(jti: str) -> bool:
    now = time.time()
    for k in [k for k, exp in _revoked_local.items() if exp < now]:
        _revoked_local.pop(k, None)
    if jti in _revoked_local:
        return True
    global _revoked_table_missing_until
    if now < _revoked_table_missing_until:
        return False
    ok, rows = Database().db_query("SELECT jti FROM SW_UI_SESSION_REVOKED WHERE jti = %s", (jti,))
    if not ok:
        logger.warning("Session revocation table unavailable, using in-memory list (%s)", rows)
        _revoked_table_missing_until = now + _REVOKED_TABLE_RECHECK_SECONDS
        return False
    return bool(rows)


def origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # same-origin navigations and many same-origin GETs omit it
    if origin in config.SESSION_ALLOWED_ORIGINS:
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return bool(host) and urlparse(origin).netloc == host


def load_session_principal(token: str) -> Tuple[Principal, Dict]:
    claims = decode_session(token)
    if not claims or is_revoked(claims["jti"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired. Sign in again.")
    return _ui_principal(claims["sub"], "Session expired. Sign in again."), claims


def _ui_principal(emp_id: str, gone_detail: str) -> Principal:
    """Re-read role, branch, department and disabled flag on every request, so
    changes in SW_USER apply at once; reject roles without UI access."""
    ok, rows = Database().db_query(
        "SELECT EMPID, EmpName, Roles, IsDisabled, BRANCH_ID, DepartmentID FROM SW_USER "
        "WHERE RTRIM(LTRIM(EMPID)) = RTRIM(LTRIM(%s))",
        (emp_id,),
    )
    if not ok:
        logger.error("Session check failed, database error: %s", rows)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Cannot verify your session right now. Try again shortly.")
    if not rows or is_disabled_value(rows[0][3]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=gone_detail)

    emp_id, emp_name, role, _, branch_id, department_id = rows[0]
    return build_ui_principal(emp_id, emp_name, role, branch_id, department_id)


# ---------------------------------------------------------------- dependencies

def _query_api_key(request: Request) -> Optional[str]:
    provided = request.query_params.get(API_KEY_QUERY_NAME)
    if not provided:
        return None
    if not config.ALLOW_API_KEY_QUERY:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="API keys in the URL are not accepted. Send the X-API-Key header.")
    return provided


def _is_browser(request: Request) -> bool:
    # Browsers send Sec-Fetch-* on every request; server-to-server callers don't.
    return bool(request.headers.get("sec-fetch-mode") or request.headers.get("sec-fetch-site"))


def authenticate_request(
    request: Request,
    api_key_h: str = Depends(api_key_header),
):
    """App-wide dependency. Sets request.state.principal (None on public paths)."""
    request.state.principal = None

    if request.method == "OPTIONS":
        return True
    if request.url.path in PUBLIC_PATHS:
        return True

    api_key_q = None if api_key_h else _query_api_key(request)
    key_id = _match_api_key(api_key_h or api_key_q)
    if key_id:
        if api_key_q:
            logger.warning("Deprecated ?api_key= used by %s on %s %s; switch it to the X-API-Key header",
                           key_id, request.method, request.url.path)
        request.state.principal = Principal(kind="integration", key_id=key_id, is_admin=True)
        return True

    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    if token:
        if not origin_allowed(request):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-site request rejected")
        principal, claims = load_session_principal(token)
        if request.method not in SAFE_METHODS:
            sent = request.headers.get(config.CSRF_HEADER_NAME) or ""
            if not sent or not secrets.compare_digest(sent.encode(), str(claims["csrf"]).encode()):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing or invalid")
        request.state.principal = principal
        request.state.session_claims = claims
        return True

    if config.UI_AUTH_DISABLED:
        if not origin_allowed(request):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-site request rejected")
        request.state.principal = _ui_principal(
            config.UI_DEFAULT_EMPID, f"UI_DEFAULT_EMPID {config.UI_DEFAULT_EMPID} not found or disabled")
        return True

    if _is_browser(request) or request.url.path == "/auth/me":
        # A browser with no (or an expired) session: the UI shows its sign-in page.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    # Unchanged response for integrations.
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing API Key")


def get_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    return principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    """Admin actions: trusted integrations, or a session whose role is an admin role."""
    if principal.kind == "integration" or principal.is_admin:
        return principal
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required")


def require_session(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.kind != "session":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Requires a browser session")
    return principal


def client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None
